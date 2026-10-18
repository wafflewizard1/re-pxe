"""Routine orchestration tests use synthetic inventory and regular media fixtures."""
import json
import re
from datetime import datetime,timedelta,timezone
import pytest
from fastapi.testclient import TestClient
from repxe.app import create_app
from test_api import auth,TOKENS
from test_pxe import lab,managed_enroll
from test_workflows import install_media,PDF,result,TARGET,SIZE,MAC


def make(c,p,erase=None,checkpoint=False):
    steps=[{'id':'boot','kind':'boot'}]
    if erase:steps.append({'id':'erase','kind':'erase',**erase})
    if checkpoint:steps.append({'id':'check','kind':'checkpoint','note':'Check certificate'})
    steps.extend([{'id':'windows','kind':'windows','profile_id':p['id']},{'id':'finish','kind':'finish'}])
    r=c.post('/api/routines',headers=auth(),json={'name':'Workshop','steps':steps});assert r.status_code==201,r.text
    uid=r.json()['id'];r=c.post('/api/routines/'+uid+'/publish',headers=auth(),json={'expected_generation':1});assert r.status_code==200,r.text
    assert c.put('/api/routine-settings',headers=auth(),json={'default_id':uid}).status_code==200
    return uid


def select(c,mac=MAC):
    menu=c.get('/pxe/boot.ipxe',params={'mac':mac});assert menu.status_code==200,menu.text
    assert 'choose --timeout 5000 --default assigned --retimeout 0' in menu.text
    url=re.search(r'chain --replace --autofree (http[^\n]*start.ipxe[^\n]*) \|\|',menu.text)[1]
    url=url.replace('http://127.0.0.1:8089','')
    r=c.get(url);assert r.status_code==200,r.text
    return url


def managed_ticket(c):
    script=c.get('/pxe/managed.ipxe',params={'mac':MAC});assert script.status_code==200,script.text
    assert 'wimboot quiet' in script.text
    return re.search('/pxe/config/([A-Za-z0-9_-]+)',script.text)[1]


@pytest.mark.parametrize('media', [None, {'version': '0.13.3', 'validated': False}])
def test_missing_winpe_explains_recovery_and_preserves_the_draft(lab, media):
    c, root, p = lab
    path = root / 'boot/pxe/current.json'
    if media is None:
        path.unlink()
    else:
        path.write_text(json.dumps(media))
    draft = {'name': 'Windows from install.wim', 'steps': [
        {'id': 'boot', 'kind': 'boot'},
        {'id': 'windows', 'kind': 'windows', 'profile_id': p['id']},
        {'id': 'finish', 'kind': 'finish'}]}
    saved = c.post('/api/routines', headers=auth(), json=draft)
    assert saved.status_code == 201, saved.text
    uid = saved.json()['id']
    before = c.get('/api/routines', headers=auth()).json()['routines']
    response = c.post('/api/routines/' + uid + '/publish', headers=auth(),
                      json={'expected_generation': 1})
    assert response.status_code == 409
    assert 'install.wim' in response.json()['detail']
    assert 'tools/publish_pxe.py --validated' in response.json()['detail']
    assert 'Guides > Windows PXE setup' in response.json()['detail']
    assert c.get('/api/routines', headers=auth()).json()['routines'] == before
    path.write_text(json.dumps({'version': '0.13.3'}))
    install_media(root)
    response = c.post('/api/routines/' + uid + '/publish', headers=auth(),
                      json={'expected_generation': 1})
    assert response.status_code == 200, response.text
    assert response.json()['published_version'] == 1


def test_menu_countdown_alternatives_and_settings_revision(lab):
    c,root,p=lab;install_media(root);uid=make(c,p)
    m=c.get('/pxe/boot.ipxe',params={'mac':MAC});assert 'item assigned Start assigned - Workshop' in m.text
    assert 'reboot --setup' in m.text and 'poweroff' in m.text and ':next\nexit' in m.text
    assert all(t not in m.text for t in TOKENS.values())
    other=c.get('/pxe/routines/menu.ipxe',params={'mac':MAC,'alternatives':1})
    assert uid in other.text and '--timeout' not in other.text
    assert '--timeout' not in c.get('/pxe/routines/menu.ipxe',params={'mac':MAC,'idle':1}).text
    menu=c.get('/api/routine-menu',headers=auth()).json()
    spec={**menu['spec'],'title':'Workshop menu','shutdown':False}
    saved=c.put('/api/routine-menu',headers=auth(),json={'spec':spec,'expected_revision':menu['revision']});assert saved.status_code==200,saved.text
    assert c.put('/api/routine-menu',headers=auth(),json={'spec':spec,'expected_revision':menu['revision']}).status_code==409
    assert 'item shutdown' not in c.get('/pxe/boot.ipxe',params={'mac':MAC}).text
    assert c.get('/api/files/history',headers=auth(),params={'root':'configuration','path':'routines-menu.json'}).json()


def test_assignment_live_change_tickets_no_duplicate_and_restart(lab):
    c,root,p=lab;install_media(root);uid=make(c,p)
    menu=c.get('/pxe/boot.ipxe',params={'mac':MAC}).text
    url=re.search(r'(http[^\n]*start.ipxe[^\n]*) \|\|',menu)[1].replace('http://127.0.0.1:8089','')
    # Clear default after rendering: old countdown cannot start a withdrawn assignment.
    c.put('/api/routine-settings',headers=auth(),json={'default_id':None})
    assert c.get(url).status_code==409
    assert c.put('/api/routine-assignments',headers=auth(),json={'mac':MAC.upper(),'routine_id':uid}).status_code==200
    url=select(c);assert c.get(url).status_code==200
    runs=c.get('/api/routines',headers=auth()).json()['runs'];assert len(runs)==1
    with TestClient(create_app(root,TOKENS)) as restarted:
        assert 'wimboot' in restarted.get('/pxe/boot.ipxe',params={'mac':MAC}).text
        assert len(restarted.get('/api/routines',headers=auth()).json()['runs'])==1


def test_windows_policy_snapshot_guards_and_completion(lab):
    c,root,p=lab;install_media(root);uid=make(c,p);url=select(c)
    ticket=managed_ticket(c);headers,enrolled=managed_enroll(c,ticket)
    assert enrolled['automation']['startDelaySeconds']==0
    assert enrolled['automation']['profileId']==p['id']
    assert 'kernel' not in c.get('/pxe/boot.ipxe',params={'mac':MAC}).text
    assert c.put('/api/clients/'+enrolled['clientId']+'/configuration',headers=auth(),json={'profile_id':None}).status_code==409
    # Editing the configuration and draft cannot rewrite a run's published image choice.
    with c.app.state.store.db() as db:
        profile=json.loads(db.execute('SELECT spec FROM deployment_profiles WHERE id=?',(p['id'],)).fetchone()[0]);profile['name']='Changed during run'
        db.execute('UPDATE deployment_profiles SET spec=? WHERE id=?',(json.dumps(profile),p['id']))
    r=c.post('/api/pxe/select',headers=headers,json={'profile_id':p['id'],'target_identity':TARGET});assert r.status_code==200,r.text
    assert r.json()['profile']['name']=='Lab Pro'
    assert c.post('/api/pxe/countdown',headers=headers,json={'action':'start'}).status_code==200
    job=c.post('/api/pxe/start',headers=headers,json={});assert job.status_code==201,job.text
    run=c.get('/api/routines',headers=auth()).json()['runs'][0];assert run['stage']=='windows-running' and run['job_id']==job.json()['id']
    assert c.post('/api/pxe/start',headers=headers,json={}).status_code==409
    with c.app.state.store.db() as db:db.execute("UPDATE jobs SET stage='complete' WHERE id=?",(job.json()['id'],))
    assert c.get('/api/routines',headers=auth()).json()['runs'][0]['stage']=='complete'
    assert '--timeout 5000' not in c.get('/pxe/boot.ipxe',params={'mac':MAC}).text
    assert c.get(url).status_code==409


def intake(c,disks=None):
    script=c.get('/pxe/managed.ipxe',params={'mac':MAC});assert 'repxe_mode=intake' in script.text
    ticket=re.search(r'repxe_ticket=([A-Za-z0-9_-]+)',script.text)[1]
    inv={'manufacturer':'Example','model':'VM','system_serial':'PC-001','disks':disks or [{'identity':TARGET,'size_bytes':SIZE}]}
    r=c.post('/api/intake/claim',headers={'Authorization':'Bearer '+ticket},json={'mac':MAC,'agent_version':'re-pxe-shredos-1','inventory':inv});assert r.status_code==201,r.text
    return inv,r.json()['token']


def test_erase_discovery_certificate_checkpoint_windows_chaining(lab):
    c,root,p=lab;install_media(root);make(c,p,{'method':'one'},True);select(c)
    inv,token=intake(c)
    assert c.get('/api/intake/status',headers={'Authorization':'Bearer '+token}).json()['reboot_for_workflow']
    run=c.get('/api/routines',headers=auth()).json()['runs'][0];assert run['target']==TARGET
    script=c.get('/pxe/managed.ipxe',params={'mac':MAC});ticket=re.search(r'repxe_ticket=([A-Za-z0-9_-]+)',script.text)[1]
    r=c.post('/api/erase/claim',headers={'Authorization':'Bearer '+ticket},json={'mac':MAC,'agent_version':'re-pxe-shredos-1','inventory':inv});assert r.status_code==201,r.text
    h={'Authorization':'Bearer '+r.json()['token']};assert r.json()['method']=='one'
    assert c.post('/api/erase/begin',headers=h,json={}).status_code==200
    assert c.post('/api/routine-runs/'+run['id']+'/action',headers=auth(),json={'action':'cancel','expected_step':run['step']}).status_code==409
    assert c.post('/api/erase/result',headers=h,json=result(run,method='one')).status_code==409
    assert c.put('/api/erase/certificate',headers={**h,'Content-Type':'application/pdf'},content=PDF).status_code==200
    assert c.post('/api/erase/result',headers=h,json=result(run,method='one')).status_code==200
    r=c.get('/api/routines',headers=auth()).json()['runs'][0];assert r['stage']=='checkpoint'
    assert 'kernel' not in c.get('/pxe/boot.ipxe',params={'mac':MAC}).text
    assert c.post('/api/routine-runs/'+run['id']+'/action',headers=auth(),json={'action':'continue','expected_step':r['step']}).status_code==200
    headers,enrolled=managed_enroll(c,managed_ticket(c));assert enrolled['automation']['targetIdentity']==TARGET
    assert c.post('/api/pxe/select',headers=headers,json={'profile_id':p['id'],'target_identity':TARGET}).status_code==200
    c.post('/api/pxe/countdown',headers=headers,json={'action':'start'})
    job=c.post('/api/pxe/start',headers=headers,json={});assert job.status_code==201,job.text
    assert c.post('/api/clients/'+enrolled['clientId']+'/cancel',headers=auth(),json={'expected_job_id':job.json()['id']}).status_code==200
    assert c.get('/api/routines',headers=auth()).json()['runs'][0]['stage']=='cancelled'
    assert c.get('/api/workflow-runs/'+run['workflow_id']+'/certificate',headers=auth()).content==PDF


def test_ambiguous_disk_failed_hold_and_fallback_preflight_only(lab):
    c,root,p=lab;install_media(root);make(c,p,{'method':'bmb'});select(c)
    intake(c,[{'identity':TARGET,'size_bytes':SIZE},{'identity':'serial:OTHER','size_bytes':SIZE}])
    r=c.get('/api/routines',headers=auth()).json()['runs'][0];assert r['stage']=='failed'
    with c.app.state.store.db() as db:assert not db.execute('SELECT * FROM workflow_runs').fetchall()
    # A new, deliberate menu selection may start another run, never automatic on reboot.
    assert '--timeout 5000' not in c.get('/pxe/boot.ipxe',params={'mac':MAC}).text
    menu=c.get('/pxe/routines/menu.ipxe',params={'mac':MAC,'alternatives':1}).text
    url=re.search(r'(http[^\n]*start.ipxe[^\n]*) \|\|',menu)[1].replace('http://127.0.0.1:8089','');assert c.get(url).status_code==200
    intake(c)
    r=c.get('/api/routines',headers=auth()).json()['runs'][0]
    detail=c.get('/api/routine-runs/'+r['id'],headers=auth()).json()
    assert not any(e['stage']=='fallback' for e in detail['events'])
    with c.app.state.store.db() as db:
        w=db.execute('SELECT * FROM workflow_runs WHERE id=?',(r['workflow_id'],)).fetchone();assert json.loads(w['spec'])['method']=='bmb'
        db.execute("UPDATE workflow_runs SET stage='erasing' WHERE id=?",(w['id'],))
    assert 'kernel' not in c.get('/pxe/managed.ipxe',params={'mac':MAC}).text


def test_validation_nwipe_permissions_and_stale_save(lab):
    c,root,p=lab;install_media(root)
    bad={'name':'Bad','steps':[{'id':'boot','kind':'boot'},{'id':'erase','kind':'erase','method':'zero'},{'id':'finish','kind':'finish'}]}
    r=c.post('/api/routines',headers=auth(),json=bad);uid=r.json()['id']
    assert c.post('/api/routines/'+uid+'/publish',headers=auth(),json={'expected_generation':1}).status_code==200
    assert c.put('/api/routines/'+uid,headers=auth(),json={'spec':bad,'expected_generation':0}).status_code==409
    for role in ('deploy','capture','agent'):
        assert c.get('/api/routines',headers=auth(role)).status_code==403
        assert c.post('/api/routines',headers=auth(role),json=bad).status_code==403
    assert c.get('/api/routines').status_code==401
    assert c.get('/pxe/routines/menu.ipxe',params={'mac':'invalid\nboot'}).status_code==422
    c.post('/api/login',json={'username':'admin','password':'admin'})
    assert c.put('/api/routine-settings',json={'enabled':True}).status_code==403


def test_migration_preserves_profiles_and_default(lab):
    c,root,p=lab;install_media(root)
    c.put('/api/pxe-settings/default',headers=auth(),json={'profile_id':p['id']})
    before=c.get('/api/profiles',headers=auth()).json()
    r=c.post('/api/routines/migrate',headers=auth());assert r.status_code==200,r.text
    d=c.get('/api/routines',headers=auth()).json();assert d['settings']['enabled'] and d['settings']['default_id']==r.json()['created'][0]
    assert c.get('/api/profiles',headers=auth()).json()==before
    assert c.post('/api/routines/migrate',headers=auth()).status_code==409


def test_named_second_disk_requires_updated_media_and_keeps_the_pin(lab):
    c,root,p=lab;install_media(root)
    spec={'name':'Named Windows target','steps':[{'id':'boot','kind':'boot'},
        {'id':'windows','kind':'windows','profile_id':p['id'],'target_policy':'named-disk','target_identity':TARGET},
        {'id':'finish','kind':'finish'}]}
    uid=c.post('/api/routines',headers=auth(),json=spec).json()['id']
    assert c.post('/api/routines/'+uid+'/publish',headers=auth(),json={'expected_generation':1}).status_code==200
    c.put('/api/routine-settings',headers=auth(),json={'default_id':uid});select(c)
    ticket=managed_ticket(c)
    request={'manufacturer':'Example','model':'VM','client_version':'0.12.7','managed_selection':True,
        'targets':[{'identity':'serial:OTHER','size_bytes':SIZE},{'identity':TARGET,'size_bytes':SIZE}]}
    assert c.post('/api/pxe/enroll',headers={'Authorization':'Bearer '+ticket},json=request).status_code==409
    request['client_version']='0.12.8'
    r=c.post('/api/pxe/enroll',headers={'Authorization':'Bearer '+ticket},json=request);assert r.status_code==201,r.text
    assert r.json()['automation']['targetIdentity']==TARGET
    h={'Authorization':'Bearer '+r.json()['token']}
    assert c.post('/api/pxe/select',headers=h,json={'profile_id':p['id'],'target_identity':'serial:OTHER'}).status_code==409
    assert c.post('/api/pxe/select',headers=h,json={'profile_id':p['id'],'target_identity':TARGET}).status_code==200


def test_stale_assigned_menu_cannot_start_a_different_routine(lab):
    c,root,p=lab;install_media(root);uid=make(c,p)
    menu=c.get('/pxe/boot.ipxe',params={'mac':MAC}).text
    url=re.search(r'(http[^\n]*start.ipxe[^\n]*) \|\|',menu)[1].replace('http://127.0.0.1:8089','')
    make(c,p,{'method':'prng'})
    assert c.get(url).status_code==409
    assert c.get('/api/routines',headers=auth()).json()['runs']==[]
