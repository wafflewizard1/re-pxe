import json
import pytest
from types import SimpleNamespace
from repxe import shredos_agent as agent, erase_policy
from test_pxe import lab

PDF=b'%PDF-1.4\nfixture original nwipe\n%%EOF\n'
DISK={'path':'/dev/nvme0n1','serial':'SAFE-001','size':1024,'tran':'nvme'}
RUN={'method':'nist_purge','execution_method':'sanitize_block','target_identity':'serial:SAFE-001','size_bytes':1024,'allow_usb':False,'token':'fixture','rounds':1,'verification':'all','final_blank':False}


def event(**extra):
    return dict(schema=1,engine_version=agent.NATIVE_VERSION,event='complete',state='success',controller_state='completed',operation_started=True,command_attempted=True,
                target_identity=RUN['target_identity'],size_bytes=1024,scope_targets=[{'identity':RUN['target_identity'],'size_bytes':1024}],**extra)


def test_exit_zero_alone_never_proves_controller_completion(tmp_path,monkeypatch):
    calls=[]
    monkeypatch.setattr(agent,'run_nwipe',lambda args,*a,**k:calls.append(args) or 0)
    monkeypatch.setattr(agent,'original_drive_pdf',lambda *a:PDF)
    code,pdf,log,success,detail=agent.execute_native(None,RUN,DISK,tmp_path)
    assert not success and detail['safe_to_reboot'] is False and len(calls)==1
    assert '--sanitize=block' in calls[0] and '--sanitize-serial=SAFE-001' in calls[0]


def test_uncertain_native_never_runs_followup(tmp_path,monkeypatch):
    calls=[]
    (tmp_path/'sanitize.jsonl').write_text(json.dumps({**event(), 'event':'hold','state':'in_progress','controller_state':'in_progress'})+'\n')
    monkeypatch.setattr(agent,'run_nwipe',lambda args,*a,**k:calls.append(args) or 22)
    monkeypatch.setattr(agent,'original_drive_pdf',lambda *a:None)
    _,_,_,success,detail=agent.execute_native(None,RUN,DISK,tmp_path)
    assert not success and not detail['safe_to_reboot'] and len(calls)==1


def test_confirmed_sanitize_requires_verified_followup_original_pdf(tmp_path,monkeypatch):
    calls=[];uploads=[]
    (tmp_path/'sanitize.jsonl').write_text(json.dumps(event())+'\n')
    def execute(args,*a,**kwargs):
        calls.append(args)
        if '--method=prng' in args:
            (tmp_path/'followup/nwipe.log').write_text(' nvme0n1 | Erased |\n')
        return 0
    monkeypatch.setattr(agent,'run_nwipe',execute)
    monkeypatch.setattr(agent,'original_drive_pdf',lambda *a:PDF)
    monkeypatch.setattr(agent,'select_target',lambda *a:DISK)
    monkeypatch.setattr(agent,'disk_list',lambda:[])
    class API:
        def request(self,path,token,payload,*a,**kw):
            import hashlib
            uploads.append((path,payload));return {'sha256':hashlib.sha256(payload).hexdigest()}
    _,primary,_,success,detail=agent.execute_native(API(),RUN,DISK,tmp_path)
    assert success and primary==PDF and detail['achieved_classification']=='purge'
    assert len(calls)==2 and '--method=prng' in calls[1] and '--verify=all' in calls[1] and '--noblank' in calls[1]
    assert uploads==[('certificate',PDF),('certificate/followup',PDF)] and [p['phase'] for p in detail['phases']]==['native','followup']


def test_probe_error_is_uncertain_not_fallback(tmp_path,monkeypatch):
    agent.NATIVE_CAPABILITIES.clear();monkeypatch.setattr(agent.subprocess,'run',lambda *a,**k:SimpleNamespace(returncode=22))
    result=agent.native_capabilities(DISK,folder=tmp_path)
    assert result['sanitize_status']=='uncertain' and not set(erase_policy.NATIVE)&set(result['methods'])


def test_purge_without_followup_keeps_confirmed_native_pdf_and_full_progress(tmp_path,monkeypatch):
    calls=[]
    (tmp_path/'sanitize.jsonl').write_text(json.dumps(event())+'\n')
    monkeypatch.setattr(agent,'run_nwipe',lambda args,*a,**k:calls.append((args,k)) or 0)
    monkeypatch.setattr(agent,'original_drive_pdf',lambda *a:PDF)
    monkeypatch.setattr(agent,'disk_list',lambda: (_ for _ in ()).throw(AssertionError('No follow-up target enumeration')))
    _,primary,_,success,detail=agent.execute_native(None,{**RUN,'prng_followup':False},DISK,tmp_path)
    assert success and primary==PDF and detail['achieved_classification']=='purge'
    assert detail['prng_followup'] is False and len(calls)==1 and calls[0][1]['phase'] is None
    assert [p['phase'] for p in detail['phases']]==['native'] and not (tmp_path/'followup').exists()


def test_malformed_followup_choice_stops_before_native_start(tmp_path,monkeypatch):
    monkeypatch.setattr(agent,'run_nwipe',lambda *a,**k:pytest.fail('Malformed settings started a wipe'))
    with pytest.raises(RuntimeError,match='explicit boolean'):
        agent.execute_native(None,{**RUN,'prng_followup':'false'},DISK,tmp_path)


def test_partial_or_wrong_version_progress_is_not_evidence(tmp_path):
    path=tmp_path/'events';path.write_text('{"schema":1,"engine_version":"other","progress_pct":100}\n{')
    assert agent.native_progress(path)[0] is None
    path.write_text(json.dumps(event(progress_pct=48,remaining_seconds=200,action='block'))+'\n{')
    assert agent.native_progress(path)[0]==48 and 'ETA 00:03:20' in agent.native_progress(path)[1]


def test_unknown_or_unsupported_unstarted_reports_remain_readable(lab):
    from test_nist_presets import media,graph,queue_inventory,claim,caps
    from test_api import auth
    c,root,p=lab;media(root);graph(c,p,fallback=False);queue_inventory(c)
    run,headers,_=claim(c,caps([],'unsupported'))
    response=c.post('/api/erase/unsupported',headers=headers,json={'error':'Unsupported: no sanitize capability',
        'evidence':{'operation_started':False,'controller_state':'unsupported','engine_version':agent.NATIVE_VERSION,
                    'target_identity':run['target_identity'],'size_bytes':run['size_bytes']}})
    assert response.status_code==200,response.text
    reports=c.get('/api/job-reports',headers=auth()).json()['reports'];uid=reports[0]['id']
    detail=c.get('/api/job-reports/'+uid,headers=auth());assert detail.status_code==200,detail.text
    assert detail.json()['erasure']['result']=='unsupported'
    pdf=c.get('/api/job-reports/'+uid+'/pdf',headers=auth());assert pdf.status_code==200 and pdf.content.startswith(b'%PDF-')


@pytest.mark.parametrize('version',['re-pxe-shredos-9','re-pxe-shredos-10'])
def test_eta_companion_update_accepts_existing_and_new_native_claims(lab,version):
    import re
    from test_nist_presets import media,graph,queue_inventory
    from test_workflows import MAC,TARGET,SIZE
    c,root,p=lab;media(root);graph(c,p,fallback=False);queue_inventory(c)
    script=c.get('/pxe/managed.ipxe',params={'mac':MAC}).text
    ticket=re.search(r'repxe_ticket=([A-Za-z0-9_-]+)',script)[1]
    from test_nist_presets import caps
    inv={'system_serial':'PC-001','disks':[{'identity':TARGET,'size_bytes':SIZE,'transport':'nvme','capabilities':caps(['sanitize_block'],'ready')}]}
    response=c.post('/api/erase/claim',headers={'Authorization':'Bearer '+ticket},json={'mac':MAC,'agent_version':version,'inventory':inv})
    assert response.status_code==201,response.text
