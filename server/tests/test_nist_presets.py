"""NIST policy/routing and immutable phase evidence; synthetic HTTP only."""
import hashlib
import json
import re
import pytest
from repxe import erase_policy,workflows
from test_api import auth
from test_pxe import lab
from test_workflows import install_media,MAC,TARGET,SIZE,PDF,result
from test_routines import select,intake


def media(root):
    install_media(root)
    path=root/'boot/pxe/shredos/current.json';value=json.loads(path.read_text())
    value.update(agent_version='re-pxe-shredos-9',nwipe_version=erase_policy.NATIVE_VERSION)
    path.write_text(json.dumps(value))


def caps(methods=None,status='ready',target=TARGET):
    return {'methods':methods if methods is not None else ['sanitize_block'],'engine_version':erase_policy.NATIVE_VERSION,
            'sanitize_status':status,'scope':'controller','controller_id':'TEST-CONTROLLER-'+target,
            'scope_targets':[{'identity':target,'size_bytes':SIZE}]}


def graph(c,p,method='nist_purge',fallback=True,prng_followup=True):
    steps=[{'id':'boot','kind':'boot'},{'id':'purge','kind':'erase','method':method,'target_policy':'all-internal','prng_followup':prng_followup},
           *([{'id':'clear','kind':'erase','method':'nist_clear','target_policy':'all-internal'}] if fallback else []),
           {'id':'windows','kind':'windows','profile_id':p['id']},{'id':'finish','kind':'finish'}]
    edges=[{'source':'boot','target':'purge'},{'source':'purge','target':'windows'},{'source':'windows','target':'finish'}]
    if fallback:edges.extend([{'source':'purge','target':'clear','outcome':'failure'},{'source':'clear','target':'windows'}])
    response=c.post('/api/routines',headers=auth(),json={'name':'Sanitize policy','graph':True,'steps':steps,'connections':edges})
    assert response.status_code==201,response.text
    uid=response.json()['id'];published=c.post('/api/routines/'+uid+'/publish',headers=auth(),json={'expected_generation':1})
    assert published.status_code==200,published.text
    c.put('/api/routine-settings',headers=auth(),json={'default_id':uid})
    return uid


def queue_inventory(c,capabilities=None):
    select(c)
    return intake(c,[{'identity':TARGET,'size_bytes':SIZE,'capabilities':capabilities or caps()}])


def claim(c,capabilities=None,begin=False,inventory=None):
    script=c.get('/pxe/managed.ipxe',params={'mac':MAC}).text
    match=re.search(r'repxe_ticket=([A-Za-z0-9_-]+)',script);assert match,script
    ticket=match[1]
    inv=inventory or {'system_serial':'PC-001','disks':[{'identity':TARGET,'size_bytes':SIZE,'capabilities':capabilities or caps()}]}
    response=c.post('/api/erase/claim',headers={'Authorization':'Bearer '+ticket},json={'mac':MAC,'agent_version':'re-pxe-shredos-9','inventory':inv})
    assert response.status_code==201,response.text
    run=response.json();headers={'Authorization':'Bearer '+run['token']}
    if begin:assert c.post('/api/erase/begin',headers=headers,json={}).status_code==200
    return run,headers,inv


def completed(c,run,headers,evidence,method=None):
    assert c.put('/api/erase/certificate',headers={**headers,'Content-Type':'application/pdf'},content=PDF).status_code==200
    payload=result(run,method=method or run['method'],final_blank=False,run_id=run['id'],system_serial=run['system_serial'],settings_sha256=run['settings_sha256'],evidence=evidence)
    return c.post('/api/erase/result',headers=headers,json=payload)


def evidence(run,purge=False):
    sha=hashlib.sha256(PDF).hexdigest()
    value={'engine_version':erase_policy.NATIVE_VERSION,'operation_started':True,'controller_state':'completed',
           'native_method':run['execution_method'],'target_identity':TARGET,'size_bytes':SIZE,
           'scope_targets':[{'identity':TARGET,'size_bytes':SIZE}], 'native_certificate_sha256':sha,
           'achieved_classification':'purge','phases':[{'phase':'native','result':'passed','exit_code':0,'errors':0,'certificate_sha256':sha}]}
    if run['method']=='nist_purge':value['prng_followup']=run.get('prng_followup',True)
    if purge:value['phases'].append({'phase':'followup','method':'prng','verification':'all','rounds':1,'final_blank':False,
                                    'result':'passed','exit_code':0,'errors':0,'certificate_sha256':sha})
    return value


def test_legacy_hashes_and_fixed_clear_controls():
    assert erase_policy.fingerprint({'method':'zero'})=='a0180d9311b7907aad9ca58d5a403f180ffa7adbf9d44cb037744030287cd1d6'
    policy={'method':'prng','rounds':3,'verification':'last','prng':'chacha20','final_blank':False,'io_mode':'direct','direction':'reverse','sync_blocks':100,'retry_io_errors':False,'abort_on_block_errors':False}
    assert erase_policy.fingerprint(policy)=='6959bb1caa50d09c2207c7f24cd4f1f1c1b36313881eb5fec70445b156b00ea0'
    clear=erase_policy.settings({'method':'nist_clear','rounds':10,'verification':'off','final_blank':True})
    assert (clear['execution_method'],clear['rounds'],clear['verification'],clear['final_blank'])==('zero',1,'all',False)
    assert erase_policy.fingerprint(clear)!=erase_policy.fingerprint({'method':'zero','final_blank':False})


def test_purge_selection_scope_unknown_and_crypto_preconditions():
    disk={'identity':TARGET,'size_bytes':SIZE,'rotational':False}
    assert erase_policy.resolve({'method':'nist_purge'},caps(),disk,[disk])[0]['execution_method']=='sanitize_block'
    disk['rotational']=True
    assert erase_policy.resolve({'method':'nist_purge'},caps(['sanitize_overwrite']),disk,[disk])[0]['execution_method']=='sanitize_overwrite'
    with pytest.raises(erase_policy.Unsupported):erase_policy.resolve({'method':'nist_purge'},caps([],'unsupported'),disk,[disk])
    with pytest.raises(ValueError) as error:erase_policy.resolve({'method':'nist_purge'},caps([],'uncertain'),disk,[disk])
    assert not isinstance(error.value,erase_policy.Unsupported)
    other=caps();other['scope_targets'].append({'identity':'serial:UNAUTHORIZED','size_bytes':SIZE})
    with pytest.raises(erase_policy.Unsupported):erase_policy.resolve({'method':'sanitize_block'},other,disk,[disk])
    with pytest.raises(ValueError,match='assurance'):erase_policy.settings({'method':'sanitize_crypto'})


def test_unsupported_batch_uses_failure_clear_in_same_session(lab):
    c,root,p=lab;media(root);graph(c,p);queue_inventory(c,caps([],'unsupported'))
    routine=c.get('/api/routines',headers=auth()).json()['runs'][0]
    assert routine['stage']=='erase-running' and routine['steps'][routine['step']]['id']=='clear'
    assert any(e['stage']=='unsupported' for e in c.get('/api/routine-runs/'+routine['id'],headers=auth()).json()['events'])
    with c.app.state.store.db() as db:
        rows=db.execute('SELECT * FROM workflow_runs').fetchall();assert len(rows)==1 and json.loads(rows[0]['spec'])['method']=='nist_clear'
    run,h,_=claim(c,caps([],'unsupported'),begin=True)
    response=completed(c,run,h,{'execution_method':'zero','achieved_classification':'clear'})
    assert response.status_code==200,response.text
    routine=c.get('/api/routines',headers=auth()).json()['runs'][0]
    assert routine['stage']=='windows-boot' and not routine['error']
    report=c.get('/api/job-reports/'+routine['report_id'],headers=auth()).json()
    assert report['erasure']['actual_method']=='zero' and report['erasure']['requested_method']=='nist_clear' and any(e['stage']=='unsupported' for e in report['events'])


def test_uncertain_probe_never_authorizes_failure_erase(lab):
    c,root,p=lab;media(root);graph(c,p);queue_inventory(c,caps([],'uncertain'))
    routine=c.get('/api/routines',headers=auth()).json()['runs'][0]
    assert routine['stage']=='failed' and 'uncertain' in routine['error']
    with c.app.state.store.db() as db:assert not db.execute('SELECT * FROM workflow_runs').fetchall()


def test_uncertain_controller_holds_explicit_clear_too(lab):
    c,root,p=lab;media(root);graph(c,p,method='nist_clear',fallback=False)
    queue_inventory(c,caps([],'uncertain'))
    routine=c.get('/api/routines',headers=auth()).json()['runs'][0]
    assert routine['stage']=='failed' and 'uncertain' in routine['error']
    with c.app.state.store.db() as db:assert not db.execute('SELECT * FROM workflow_runs').fetchall()


def test_all_drives_preflight_before_any_workflow(lab):
    c,root,p=lab;media(root);graph(c,p);select(c)
    intake(c,[{'identity':TARGET,'size_bytes':SIZE,'capabilities':caps()},
              {'identity':'serial:OTHER','size_bytes':SIZE,'capabilities':caps([],'unsupported','serial:OTHER')}])
    with c.app.state.store.db() as db:
        rows=db.execute('SELECT spec FROM workflow_runs').fetchall()
        assert len(rows)==2 and all(json.loads(row['spec'])['method']=='nist_clear' for row in rows)


def test_started_native_failure_is_held_and_never_retries(lab):
    c,root,p=lab;media(root);graph(c,p);queue_inventory(c);run,h,_=claim(c,begin=True)
    response=c.post('/api/erase/unsupported',headers=h,json={'error':'unsupported','evidence':{'operation_started':False,'controller_state':'unsupported','engine_version':erase_policy.NATIVE_VERSION,'target_identity':TARGET,'size_bytes':SIZE}})
    assert response.status_code==409
    response=c.post('/api/erase/result',headers=h,json=result(run,method='nist_purge',final_blank=False,result='failed',exit_code=22,errors=1,certificate_sha256=None,evidence={'safe_to_reboot':True}))
    assert response.status_code==200 and response.json()['stage']=='operation-pending' and not response.json()['reboot_for_routine']
    with c.app.state.store.db() as db:assert len(db.execute('SELECT * FROM workflow_runs').fetchall())==1


def test_prestart_unsupported_fresh_inventory_handoff(lab):
    c,root,p=lab;media(root);graph(c,p)
    select(c)
    script=c.get('/pxe/managed.ipxe',params={'mac':MAC}).text;ticket=re.search(r'repxe_ticket=([A-Za-z0-9_-]+)',script)[1]
    inv={'system_serial':'PC-001','disks':[{'identity':TARGET,'size_bytes':SIZE,'capabilities':caps()}]}
    response=c.post('/api/intake/claim',headers={'Authorization':'Bearer '+ticket},json={'mac':MAC,'agent_version':'re-pxe-shredos-9','inventory':inv})
    assert response.status_code==201;intake_h={'Authorization':'Bearer '+response.json()['token']}
    # This direct claim fixture obtains a normal queued ticket after discovery.
    run,h,_=claim(c)
    response=c.post('/api/erase/unsupported',headers=h,json={'error':'Unsupported after read-only reprobe','evidence':{'operation_started':False,'controller_state':'unsupported','engine_version':erase_policy.NATIVE_VERSION,'target_identity':TARGET,'size_bytes':SIZE}})
    assert response.status_code==200 and response.json()['inventory_needed']
    assert c.get('/api/intake/status',headers=intake_h).json()['inventory_needed']
    inv['disks'][0]['capabilities']=caps([],'unsupported')
    refreshed=c.post('/api/intake/inventory',headers=intake_h,json={'mac':MAC,'agent_version':'re-pxe-shredos-9','inventory':inv})
    assert refreshed.status_code==200 and refreshed.json()['erase_ready']
    with c.app.state.store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM workflow_runs WHERE stage='unsupported'").fetchone()[0]==1
        assert db.execute('SELECT COUNT(*) FROM routine_erase_disks').fetchone()[0]==2


def test_capability_changed_to_unsupported_can_report_without_begin(lab):
    c,root,p=lab;media(root);graph(c,p);queue_inventory(c)
    run,h,_=claim(c,caps([],'unsupported'))
    assert c.post('/api/erase/begin',headers=h,json={}).status_code==409
    response=c.post('/api/erase/unsupported',headers=h,json={'error':'Unsupported on fresh read-only probe','evidence':{'operation_started':False,'controller_state':'unsupported','engine_version':erase_policy.NATIVE_VERSION,'target_identity':TARGET,'size_bytes':SIZE}})
    assert response.status_code==200 and response.json()['inventory_needed']
    with c.app.state.store.db() as db:assert not db.execute("SELECT 1 FROM workflow_events WHERE stage='erasing'").fetchone()


def test_inconsistent_controller_topology_stops_before_any_write(lab):
    c,root,p=lab;media(root);graph(c,p);select(c)
    first,second=caps(),caps(target='serial:OTHER');second['controller_id']=first['controller_id']
    intake(c,[{'identity':TARGET,'size_bytes':SIZE,'capabilities':first},{'identity':'serial:OTHER','size_bytes':SIZE,'capabilities':second}])
    routine=c.get('/api/routines',headers=auth()).json()['runs'][0]
    assert routine['stage']=='failed' and 'topology' in routine['error']
    with c.app.state.store.db() as db:assert not db.execute('SELECT * FROM workflow_runs').fetchall()


def test_purge_requires_native_and_followup_immutable_pdfs(lab):
    c,root,p=lab;media(root);graph(c,p);queue_inventory(c);run,h,_=claim(c,begin=True)
    response=completed(c,run,h,evidence(run,purge=True));assert response.status_code==409 and 'follow-up' in response.text
    endpoint='/api/erase/certificate/followup';headers={**h,'Content-Type':'application/pdf'}
    assert c.put(endpoint,headers=headers,content=PDF).status_code==200
    assert c.put(endpoint,headers=headers,content=PDF).json()['duplicate']
    assert c.put(endpoint,headers=headers,content=PDF.replace(b'fixture',b'changed')).status_code==409
    value=evidence(run,purge=True);value['controller_state']='unknown'
    assert completed(c,run,h,value).status_code==409
    response=completed(c,run,h,evidence(run,purge=True));assert response.status_code==200,response.text
    assert c.get('/api/workflow-runs/'+run['id']+'/certificate/followup',headers=auth()).content==PDF


def test_partial_native_batch_cannot_fallback_after_prior_drive_started(lab):
    c,root,p=lab;media(root);graph(c,p);select(c)
    disks=[{'identity':TARGET,'size_bytes':SIZE,'capabilities':caps()},
           {'identity':'serial:OTHER','size_bytes':SIZE,'capabilities':caps(target='serial:OTHER')}]
    intake(c,disks)
    run,h,_=claim(c,begin=True,inventory={'system_serial':'PC-001','disks':disks})
    # Keep the first operation's start evidence, then model a second claimed
    # drive. The endpoint must reject fallback of the already-started batch.
    with c.app.state.store.db() as db:
        first=db.execute('SELECT * FROM workflow_runs WHERE id=?',(run['id'],)).fetchone()
        second=db.execute("SELECT * FROM workflow_runs WHERE stage='batch-wait'").fetchone()
        token='B'*48
        db.execute("UPDATE workflow_runs SET stage='claimed',token_hash=?,expires=?,inventory=? WHERE id=?",(workflows.digest(token),first['expires'],first['inventory'],second['id']))
    response=c.post('/api/erase/unsupported',headers={'Authorization':'Bearer '+token},json={'error':'Unsupported before second drive','evidence':{'operation_started':False,'controller_state':'unsupported','engine_version':erase_policy.NATIVE_VERSION,'target_identity':'serial:OTHER','size_bytes':SIZE}})
    assert response.status_code==409 and 'batch already started' in response.text


def test_clear_cannot_claim_purge_from_fallback(lab):
    c,root,p=lab;media(root);graph(c,p);queue_inventory(c,caps([],'unsupported'))
    run,h,_=claim(c,caps([],'unsupported'),begin=True)
    response=completed(c,run,h,{'execution_method':'zero','achieved_classification':'purge'})
    assert response.status_code==409 and 'NIST Clear' in response.text


def test_native_requires_pinned_media_and_no_implicit_fallback(lab):
    c,root,p=lab;install_media(root)
    steps=[{'id':'boot','kind':'boot'},{'id':'erase','kind':'erase','method':'nist_purge'},{'id':'finish','kind':'finish'}]
    response=c.post('/api/routines',headers=auth(),json={'name':'Not yet ready','steps':steps});assert response.status_code==201
    assert c.post('/api/routines/'+response.json()['id']+'/publish',headers=auth(),json={'expected_generation':1}).status_code==409
    assert c.post('/api/workflows',headers=auth(),json={'name':'No implicit fallback','method':'nist_purge','fallback_method':'nist_clear'}).status_code==422


def test_old_companion_cannot_run_clear_preset(lab):
    c,root,p=lab;install_media(root)
    spec={'name':'Clear requires new protocol','steps':[{'id':'boot','kind':'boot'},{'id':'erase','kind':'erase','method':'nist_clear'},{'id':'finish','kind':'finish'}]}
    response=c.post('/api/routines',headers=auth(),json=spec);assert response.status_code==201
    assert c.post('/api/routines/'+response.json()['id']+'/publish',headers=auth(),json={'expected_generation':1}).status_code==409
    path=root/'boot/pxe/shredos/current.json';value=json.loads(path.read_text());value['agent_version']='re-pxe-shredos-8';path.write_text(json.dumps(value))
    template=c.post('/api/workflows',headers=auth(),json={'name':'Clear','method':'nist_clear'}).json()
    response=c.post('/api/workflow-runs',headers=auth(),json={'template_id':template['id'],'mac':MAC,'target_identity':TARGET,'size_bytes':SIZE,'confirmation':TARGET})
    assert response.status_code==409
    value.update(agent_version='re-pxe-shredos-9',nwipe_version=erase_policy.NATIVE_VERSION);path.write_text(json.dumps(value))
    graph(c,p,method='nist_clear',fallback=False);queue_inventory(c,caps([],'unsupported'))
    script=c.get('/pxe/managed.ipxe',params={'mac':MAC}).text;ticket=re.search(r'repxe_ticket=([A-Za-z0-9_-]+)',script)[1]
    response=c.post('/api/erase/claim',headers={'Authorization':'Bearer '+ticket},json={'mac':MAC,'agent_version':'re-pxe-shredos-8','inventory':{'system_serial':'PC-001','disks':[{'identity':TARGET,'size_bytes':SIZE}]}})
    assert response.status_code==409
