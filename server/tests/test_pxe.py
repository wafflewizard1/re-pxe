import hashlib
import json
import re
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
import pytest
from repxe.app import create_app
from test_api import auth, TOKENS, profile_image, driver_zip, send, publish


@pytest.fixture
def lab(tmp_path,monkeypatch):
    with TestClient(create_app(tmp_path,TOKENS), base_url='https://testserver') as c:
        # Explicit compatibility fixture for the pre-routine client APIs.
        c.put('/api/routine-settings',headers=auth(),json={'enabled':False})
        image=profile_image(c,monkeypatch)
        profile=c.post('/api/profiles',json={'name':'Lab Pro','windows_label':'Windows 11 Pro','image_id':image},headers=auth()).json()
        c.post('/api/profiles/'+profile['id']+'/availability',json={'enabled':True},headers=auth())
        (tmp_path/'boot/pxe').mkdir();(tmp_path/'boot/pxe/current.json').write_text(json.dumps({'version':'0.8.0'}))
        yield c,tmp_path,profile


def boot(c,mac='52:54:00:12:34:56'):
    r=c.get('/pxe/boot.ipxe?mac='+mac);assert r.status_code==200,r.text
    assert r.headers['cache-control']=='no-store' and 'wimboot quiet' in r.text and 'imgstat' not in r.text
    for name in ('BCD','boot.sdi','bootx64.efi','boot.wim','re-pxe-boot.json'):
        # UEFI reads image names; the trailing argument only names the BIOS archive entry.
        assert 'initrd --name '+name+' ' in r.text
    assert '/BCD BCD' in r.text and '/boot.sdi boot.sdi' in r.text
    assert '/bootx64.efi bootx64.efi' in r.text
    ticket=re.search('/pxe/config/([A-Za-z0-9_-]+)',r.text)[1]
    config=c.get('/pxe/config/'+ticket);assert config.status_code==200
    assert not any(value in r.text+config.text for value in TOKENS.values())
    return ticket


def enroll(c,ticket,target='serial:LAB-001'):
    r=c.post('/api/pxe/enroll',headers={'Authorization':'Bearer '+ticket},json={'manufacturer':'Example','model':'VM','client_version':'0.11.0','targets':[{'identity':target,'size_bytes':64*1024**3}]})
    assert r.status_code==201,r.text
    return {'Authorization':'Bearer '+r.json()['token']},r.json()['clientId']


def test_ticket_once_scope_and_expiry(lab):
    c,root,p=lab;ticket=boot(c);headers,uid=enroll(c,ticket)
    assert c.get('/pxe/config/'+ticket).status_code==401
    assert c.post('/api/pxe/enroll',headers={'Authorization':'Bearer '+ticket},json={'targets':[{'identity':'serial:OTHER','size_bytes':64*1024**3}]}).status_code==401
    assert c.get('/api/images',headers=headers).status_code==401
    assert c.get('/api/dashboard',headers=headers).status_code==401
    assert c.post('/api/pxe/options',headers=headers,json={'target_identity':'serial:OTHER'}).status_code==403
    assert c.post('/api/pxe/options',headers=headers,json={'target_identity':'serial:LAB-001'}).json()['choices'][0]['id']==p['id']
    with c.app.state.store.db() as db:db.execute('UPDATE clients SET expires=? WHERE id=?',((datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(),uid))
    assert c.post('/api/pxe/options',headers=headers,json={}).status_code==401


def test_selection_job_events_and_artifact_binding(lab):
    c,root,p=lab;headers,uid=enroll(c,boot(c))
    assert c.post('/api/pxe/start',headers=headers,json={}).status_code==409
    selected=c.post('/api/pxe/select',headers=headers,json={'profile_id':p['id'],'target_identity':'serial:LAB-001'});assert selected.status_code==200
    assert c.get('/api/pxe/image',headers=headers).status_code==409
    started=c.post('/api/pxe/start',headers=headers,json={});assert started.status_code==201,started.text
    assert started.json()['reportCredential']['jobId']==started.json()['id']
    cap={'Authorization':'Bearer '+started.json()['reportCredential']['token']}
    pulse='/api/client-jobs/'+started.json()['id']+'/heartbeat'
    assert c.post(pulse,headers=headers,json={'stage':'completing'}).status_code==401
    assert c.post(pulse,headers=cap,json={'stage':'completing'}).status_code==200
    assert c.get('/api/dashboard',headers=auth()).json()['clients'][0]['stage']=='completing'
    assert c.post(pulse,headers=cap,json={'stage':'complete'}).status_code==422
    assert c.post('/api/pxe/start',headers=headers,json={}).status_code==409
    assert c.post('/api/pxe/select',headers=headers,json={'profile_id':p['id'],'target_identity':'serial:LAB-001'}).status_code==409
    image=c.get('/api/pxe/image',headers=headers);assert image.status_code==200
    assert hashlib.sha256(image.content).hexdigest()==selected.json()['profile']['image_sha256']
    second,_=enroll(c,boot(c,'52:54:00:12:34:57'),'serial:OTHER')
    assert c.get('/api/pxe/image',headers=second).status_code==409
    event={'stage':'preflight','event_id':'retry-001'}
    assert c.post('/api/pxe/event',headers=second,json=event).status_code==409
    assert c.post('/api/pxe/event',headers=headers,json=event).status_code==200
    assert c.post('/api/pxe/event',headers=headers,json=event).status_code==200
    assert c.post('/api/pxe/event',headers=headers,json={**event,'detail':'different'}).status_code==409
    assert c.post('/api/pxe/event',headers=headers,json={'stage':'completing'}).status_code==403


def test_dashboard_stale_clients_rates_and_no_credentials(lab):
    c,root,p=lab;headers,uid=enroll(c,boot(c));enroll(c,boot(c,'52:54:00:12:34:57'),'serial:OTHER')
    assert c.post('/api/pxe/heartbeat',headers=headers,json={'stage':'downloading','received_bytes':100,'total_bytes':1000}).status_code==200
    assert c.post('/api/pxe/heartbeat',headers=headers,json={'stage':'downloading','received_bytes':90,'total_bytes':1000}).status_code==409
    assert c.post('/api/pxe/heartbeat',headers=headers,json={'stage':'downloading','received_bytes':2000,'total_bytes':1000}).status_code==422
    with c.app.state.store.db() as db:db.execute('UPDATE clients SET seen=? WHERE id!=?',((datetime.now(timezone.utc)-timedelta(seconds=120)).isoformat(),uid))
    d=c.get('/api/dashboard',headers=auth());assert d.status_code==200 and d.json()['online_clients']==1
    assert d.json()['network'] is None and d.json()['client_bytes_per_second']>0
    assert all(key not in d.text for key in ['token_hash','ticket_hash',headers['Authorization'][7:]])
    (root/'database/server-network.json').write_text(json.dumps({'sampled_at':datetime.now(timezone.utc).isoformat(),'tx_bytes_per_second':8000,'rx_bytes_per_second':1000}))
    assert c.get('/api/dashboard',headers=auth()).json()['network']['tx_bytes_per_second']==8000
    for role in ('deploy','capture','agent'):assert c.get('/api/dashboard',headers=auth(role)).status_code==403


def test_disabled_selection_and_ticket_not_reused(lab):
    c,root,p=lab;headers,uid=enroll(c,boot(c))
    assert c.post('/api/pxe/select',headers=headers,json={'profile_id':p['id'],'target_identity':'serial:LAB-001'}).status_code==200
    c.post('/api/profiles/'+p['id']+'/availability',headers=auth(),json={'enabled':False})
    assert c.post('/api/pxe/start',headers=headers,json={}).status_code==409
    with c.app.state.store.db() as db:assert db.execute('SELECT count(*) FROM jobs').fetchone()[0]==0
    c.get('/pxe/boot.ipxe?mac=52:54:00:12:34:56')
    assert len(c.get('/api/dashboard',headers=auth()).json()['clients'])==1


def test_selected_published_driver_downloads(lab):
    c,root,p=lab
    payload=driver_zip()
    spec={'kind':'drivers','name':'VM drivers','filename':'drivers.zip','size':len(payload),
          'sha256':hashlib.sha256(payload).hexdigest(),'make':'Example','model':'VM','windows_version':'26100'}
    uid=c.post('/api/uploads',json=spec,headers=auth()).json()['id']
    assert send(c,uid,payload).status_code==200
    assert c.post(f'/api/uploads/{uid}/finish',headers=auth()).status_code==200
    assert publish(c,uid).status_code==200
    profile=c.post('/api/profiles',json={'name':'With drivers','windows_label':'Windows Pro',
        'image_id':p['image_id'],'driver_ids':[uid]},headers=auth()).json()
    c.post('/api/profiles/'+profile['id']+'/availability',json={'enabled':True},headers=auth())
    headers,_=enroll(c,boot(c))
    selected=c.post('/api/pxe/select',headers=headers,json={'profile_id':profile['id'],'target_identity':'serial:LAB-001'})
    assert selected.status_code==200,selected.text
    manifest=selected.json()['driverManifest']
    assert manifest['packageId']==uid
    assert c.get('/api/pxe/driver/0',headers=headers).status_code==404
    assert c.post('/api/pxe/start',headers=headers,json={}).status_code==201
    for index,entry in enumerate(manifest['files']):
        result=c.get(f'/api/pxe/driver/{index}',headers=headers)
        assert result.status_code==200
        assert len(result.content)==entry['size']
        assert hashlib.sha256(result.content).hexdigest()==entry['sha256']
    assert c.get('/api/pxe/driver/-1',headers=headers).status_code==404
    assert c.get('/api/pxe/driver/'+str(len(manifest['files'])),headers=headers).status_code==404


def managed_enroll(c,ticket,targets=None):
    r=c.post('/api/pxe/enroll',headers={'Authorization':'Bearer '+ticket},json={
        'manufacturer':'Example','model':'VM','client_version':'0.11.0','managed_selection':True,
        'targets':targets or [{'identity':'serial:LAB-001','size_bytes':64*1024**3}]})
    assert r.status_code==201,r.text
    return {'Authorization':'Bearer '+r.json()['token']},r.json()


def extra_profile(c,p):
    profile=c.post('/api/profiles',json={'name':'Alternate Pro','windows_label':'Other Windows',
        'image_id':p['image_id']},headers=auth()).json()
    assert c.post('/api/profiles/'+profile['id']+'/availability',json={'enabled':True},headers=auth()).status_code==200
    return profile


def configure(c,**changes):
    settings=c.get('/api/pxe-settings',headers=auth()).json()
    r=c.put('/api/pxe-settings',headers=auth(),json={**settings,**changes})
    assert r.status_code==200,r.text
    return r.json()


def test_default_override_and_atomic_selection_lock(lab):
    c,root,p=lab;alternate=extra_profile(c,p)
    assert configure(c,default_profile_id=p['id'])['start_delay_seconds']==10
    first=boot(c);second=boot(c,'52:54:00:12:34:57')
    rows=c.get('/api/dashboard',headers=auth()).json()['clients']
    assert all(r['configuration_id']==p['id'] and not r['selection_locked'] for r in rows)
    override=next(r['id'] for r in rows if r['mac'].endswith('57'))
    assert c.put('/api/clients/'+override+'/configuration',headers=auth(),json={'profile_id':alternate['id']}).status_code==200
    h1,s1=managed_enroll(c,first);h2,s2=managed_enroll(c,second)
    assert s1['automation']['profileId']==p['id'] and s2['automation']['profileId']==alternate['id']
    configure(c,default_profile_id=alternate['id'],start_delay_seconds=60,automatic_disk_selection=False)
    assert s1['automation']['startDelaySeconds']==10
    for uid in (s1['clientId'],s2['clientId']):
        assert c.put('/api/clients/'+uid+'/configuration',headers=auth(),json={'profile_id':p['id']}).status_code==409
    assert c.post('/api/pxe/select',headers=h1,json={'profile_id':alternate['id'],'target_identity':'serial:LAB-001'}).status_code==409
    assert c.post('/api/pxe/select',headers=h2,json={'profile_id':alternate['id'],'target_identity':'serial:LAB-001'}).status_code==200
    assert all(r['selection_locked'] for r in c.get('/api/dashboard',headers=auth()).json()['clients'])


def test_countdown_start_cancel_resume_and_first_disk_guard(lab):
    c,root,p=lab;configure(c,default_profile_id=p['id'])
    headers,session=managed_enroll(c,boot(c),[
        {'identity':'serial:FIRST','size_bytes':64*1024**3},
        {'identity':'serial:SECOND','size_bytes':64*1024**3}])
    assert session['automation']['targetIdentity']=='serial:FIRST'
    assert c.post('/api/pxe/select',headers=headers,json={'profile_id':p['id'],'target_identity':'serial:SECOND'}).status_code==409
    assert c.post('/api/pxe/select',headers=headers,json={'profile_id':p['id'],'target_identity':'serial:FIRST'}).status_code==200
    assert c.post('/api/pxe/start',headers=headers,json={}).status_code==409
    assert c.post('/api/pxe/countdown',headers=headers,json={'action':'start'}).json()['startDelaySeconds']==10
    assert c.post('/api/pxe/countdown',headers=headers,json={'action':'start'}).status_code==409
    assert c.post('/api/pxe/start',headers=headers,json={}).status_code==409
    assert c.post('/api/pxe/select',headers=headers,json={'profile_id':p['id'],'target_identity':'serial:FIRST'}).status_code==409
    assert c.post('/api/pxe/countdown',headers=headers,json={'action':'cancel'}).status_code==200
    assert c.post('/api/pxe/start',headers=headers,json={}).status_code==409
    with c.app.state.store.db() as db:assert db.execute('SELECT count(*) FROM jobs').fetchone()[0]==0
    assert c.post('/api/pxe/countdown',headers=headers,json={'action':'start'}).status_code==200
    # Advance only the persisted deadline; no sleep or disk execution is involved.
    with c.app.state.store.db() as db:db.execute('UPDATE clients SET countdown_until=? WHERE id=?',((datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(),session['clientId']))
    assert c.post('/api/pxe/start',headers=headers,json={}).status_code==201
    assert c.post('/api/pxe/countdown',headers=headers,json={'action':'cancel'}).status_code==409
    assert c.post('/api/pxe/start',headers=headers,json={}).status_code==409


def test_default_changes_waiting_clients_and_keeps_other_settings(lab):
    c,root,p=lab;alternate=extra_profile(c,p)
    configure(c,default_profile_id=p['id'],start_delay_seconds=47,offline_hide_seconds=600)
    ticket=boot(c)
    r=c.put('/api/pxe-settings/default',headers=auth(),json={'profile_id':alternate['id']})
    assert r.json()['start_delay_seconds']==47 and r.json()['offline_hide_seconds']==600
    _,session=managed_enroll(c,ticket)
    assert session['automation']['profileId']==alternate['id']
    assert session['automation']['startDelaySeconds']==47


def test_management_permissions_bounds_and_legacy_guard(lab):
    c,root,p=lab;ticket=boot(c);uid=c.get('/api/dashboard',headers=auth()).json()['clients'][0]['id']
    for role in ('agent','capture','deploy'):
        assert c.get('/api/pxe-settings',headers=auth(role)).status_code==403
        assert c.put('/api/pxe-settings',headers=auth(role),json={}).status_code==403
        assert c.put('/api/pxe-settings/default',headers=auth(role),json={'profile_id':p['id']}).status_code==403
        assert c.put('/api/clients/'+uid+'/configuration',headers=auth(role),json={'profile_id':p['id']}).status_code==403
    settings=c.get('/api/pxe-settings',headers=auth()).json()
    for key,value in [('start_delay_seconds',-1),('start_delay_seconds',301),('start_delay_seconds',1.5),('start_delay_seconds',True),('automatic_disk_selection','true'),('offline_hide_seconds',29),('offline_hide_seconds',86401),('default_profile_id','00000000-0000-0000-0000-000000000000')]:
        assert c.put('/api/pxe-settings',headers=auth(),json={**settings,key:value}).status_code==422
    c.post('/api/profiles/'+p['id']+'/availability',headers=auth(),json={'enabled':False})
    assert c.put('/api/clients/'+uid+'/configuration',headers=auth(),json={'profile_id':p['id']}).status_code==422
    c.post('/api/profiles/'+p['id']+'/availability',headers=auth(),json={'enabled':True})
    assert c.put('/api/clients/'+uid+'/configuration',headers=auth(),json={'profile_id':p['id']}).status_code==200
    assert c.post('/api/pxe/enroll',headers={'Authorization':'Bearer '+ticket},json={'targets':[{'identity':'serial:LAB-001','size_bytes':64*1024**3}]}).status_code==409
    _,session=managed_enroll(c,ticket)
    assert session['automation']['profileId']==p['id']


def test_configuration_revalidated_after_countdown(lab):
    c,root,p=lab;configure(c,default_profile_id=p['id'],start_delay_seconds=0)
    headers,_=managed_enroll(c,boot(c))
    assert c.post('/api/pxe/select',headers=headers,json={'profile_id':p['id'],'target_identity':'serial:LAB-001'}).status_code==200
    assert c.post('/api/pxe/countdown',headers=headers,json={'action':'start'}).status_code==200
    c.post('/api/profiles/'+p['id']+'/availability',headers=auth(),json={'enabled':False})
    assert c.post('/api/pxe/start',headers=headers,json={}).status_code==409
    with c.app.state.store.db() as db:assert db.execute('SELECT count(*) FROM jobs').fetchone()[0]==0


def test_offline_timeout_keeps_pending_jobs_and_reappears_on_heartbeat(lab,monkeypatch):
    c,root,p=lab;headers,uid=enroll(c,boot(c))
    assert c.post('/api/pxe/select',headers=headers,json={'profile_id':p['id'],'target_identity':'serial:LAB-001'}).status_code==200
    job=c.post('/api/pxe/start',headers=headers,json={}).json()['id']
    at=datetime.now(timezone.utc)
    import repxe.pxe as pxe
    class Frozen(datetime):
        @classmethod
        def now(cls,tz=None):return at
    monkeypatch.setattr(pxe,'datetime',Frozen)
    with c.app.state.store.db() as db:db.execute('UPDATE clients SET seen=? WHERE id=?',((at-timedelta(seconds=389)).isoformat(),uid))
    d=c.get('/api/dashboard',headers=auth()).json();assert len(d['clients'])==1 and not d['clients'][0]['online']
    with c.app.state.store.db() as db:db.execute('UPDATE clients SET seen=? WHERE id=?',((at-timedelta(seconds=390)).isoformat(),uid))
    assert c.get('/api/dashboard',headers=auth()).json()['clients'][0]['pending']
    assert c.get('/api/jobs/'+job,headers=auth()).status_code==200
    configure(c,offline_hide_seconds=600)
    assert len(c.get('/api/dashboard',headers=auth()).json()['clients'])==1
    configure(c,offline_hide_seconds=30)
    assert c.get('/api/dashboard',headers=auth()).json()['clients'][0]['pending']
    assert c.post('/api/pxe/heartbeat',headers=headers,json={'stage':'preflight'}).status_code==200
    assert c.get('/api/dashboard',headers=auth()).json()['clients'][0]['online']


def test_settings_persist_and_old_client_schema_migrates(lab):
    c,root,p=lab;headers,uid=enroll(c,boot(c));configure(c,start_delay_seconds=41,offline_hide_seconds=900)
    # Recreate the previous clients schema while preserving its existing row.
    with c.app.state.store.db() as db:
        for column in ('profile_override','boot_policy','countdown_until'):db.execute('ALTER TABLE clients DROP COLUMN '+column)
    with TestClient(create_app(root,TOKENS)) as restored:
        assert restored.get('/api/pxe-settings',headers=auth()).json()['start_delay_seconds']==41
        row=restored.get('/api/dashboard',headers=auth()).json()['clients'][0]
        assert row['id']==uid and row['selection_locked']
        assert restored.post('/api/pxe/options',headers=headers,json={'target_identity':'serial:LAB-001'}).status_code==200


def test_manual_disk_mode_keeps_server_image_choice(lab):
    c,root,p=lab;alternate=extra_profile(c,p)
    configure(c,default_profile_id=p['id'],automatic_disk_selection=False)
    headers,s=managed_enroll(c,boot(c),[
        {'identity':'serial:FIRST','size_bytes':64*1024**3},
        {'identity':'serial:SECOND','size_bytes':64*1024**3}])
    assert not s['automation']['automaticDiskSelection']
    assert c.post('/api/pxe/select',headers=headers,json={'profile_id':alternate['id'],'target_identity':'serial:SECOND'}).status_code==409
    assert c.post('/api/pxe/select',headers=headers,json={'profile_id':p['id'],'target_identity':'serial:SECOND'}).status_code==200
    assert c.post('/api/pxe/countdown',headers=headers,json={'action':'start'}).status_code==409
    assert c.post('/api/pxe/start',headers=headers,json={}).status_code==201


def library_package(c,make='QEMU',model='Q35',build='26100',critical=None):
    payload=driver_zip()
    spec={'kind':'drivers','name':make+' '+model,'filename':'drivers.zip','size':len(payload),
          'sha256':hashlib.sha256(payload).hexdigest(),'make':make,'model':model,'windows_version':build}
    if critical is not None:spec['boot_critical_inf_paths']=critical
    uid=c.post('/api/uploads',json=spec,headers=auth()).json()['id']
    assert send(c,uid,payload).status_code==200
    assert c.post(f'/api/uploads/{uid}/finish',headers=auth()).status_code==200
    result=publish(c,uid)
    assert result.status_code==200,result.text
    return uid


def automatic_profile(c,p):
    response=c.post('/api/profiles',json={'name':'Generic Windows 11','windows_label':'Windows 11 Pro',
        'image_id':p['image_id'],'driver_source':'automatic'},headers=auth())
    assert response.status_code==201,response.text
    profile=response.json()
    assert c.post('/api/profiles/'+profile['id']+'/availability',json={'enabled':True},headers=auth()).status_code==200
    return profile


def test_automatic_library_is_independent_and_never_blocks_generic_image(lab):
    c,root,p=lab
    qemu=library_package(c);hp=library_package(c,'HP','HP EliteBook 840 G8')
    library_package(c,'HP','HP EliteBook 840 G8','26300')
    generic=automatic_profile(c,p)
    for make,model,expected in [('QEMU','Q35',qemu),('HP','HP EliteBook 840 G8',hp),('Other','Unknown',None)]:
        choices=c.get('/api/boot-options',params={'manufacturer':make,'model':model},headers=auth('deploy')).json()['choices']
        choice=next(x for x in choices if x['id']==generic['id'])
        assert choice['driver_id']==expected
        assert choice['driver_policy']=='server-library-then-windows-update'
        assert choice['driver_ids']==[]
    restricted=c.post('/api/profiles',json={'name':'QEMU only','windows_label':'Pro','image_id':p['image_id'],'driver_ids':[qemu]},headers=auth()).json()
    c.post('/api/profiles/'+restricted['id']+'/availability',json={'enabled':True},headers=auth())
    assert restricted['id'] not in [x['id'] for x in c.get('/api/boot-options?manufacturer=HP&model=HP%20EliteBook%20840%20G8',headers=auth('deploy')).json()['choices']]
    # A package without a successful review or wrong architecture is never selected.
    for key,value in [('installation_authorized',False),('architecture','arm64')]:
        with c.app.state.store.db() as db:
            meta=json.loads(db.execute('SELECT metadata FROM artifacts WHERE id=?',(hp,)).fetchone()['metadata'])
            original=meta[key];meta[key]=value;db.execute('UPDATE artifacts SET metadata=? WHERE id=?',(json.dumps(meta),hp))
        choices=c.get('/api/boot-options?manufacturer=HP&model=HP%20EliteBook%20840%20G8',headers=auth('deploy')).json()['choices']
        assert next(x for x in choices if x['id']==generic['id'])['driver_id'] is None
        with c.app.state.store.db() as db:
            meta[key]=original;db.execute('UPDATE artifacts SET metadata=? WHERE id=?',(json.dumps(meta),hp))


def test_automatic_selection_pins_package_and_absence_stages_nothing(lab):
    c,root,p=lab;uid=library_package(c,'Example','VM');generic=automatic_profile(c,p)
    headers,_=enroll(c,boot(c))
    selected=c.post('/api/pxe/select',headers=headers,json={'profile_id':generic['id'],'target_identity':'serial:LAB-001'})
    assert selected.status_code==200,selected.text
    assert selected.json()['driverManifest']['packageId']==uid
    new=library_package(c,'Example','VM')
    assert new!=uid
    assert c.post('/api/pxe/start',headers=headers,json={}).status_code==201
    assert c.get('/api/pxe/driver/0',headers=headers).status_code==200
    headers,_=enroll(c,boot(c,'52:54:00:12:34:58'))
    with c.app.state.store.db() as db:
        db.execute("UPDATE clients SET manufacturer='HP',model='EliteBook' WHERE token_hash=?",(hashlib.sha256(headers['Authorization'][7:].encode()).hexdigest(),))
    selected=c.post('/api/pxe/select',headers=headers,json={'profile_id':generic['id'],'target_identity':'serial:LAB-001'})
    assert selected.status_code==200 and selected.json()['driverManifest'] is None
    assert c.post('/api/pxe/start',headers=headers,json={}).status_code==201
    assert c.get('/api/pxe/driver/0',headers=headers).status_code==404


def test_disk_policy_and_automatic_driver_version_guards(lab):
    c,root,p=lab;generic=automatic_profile(c,p)
    ticket=boot(c);body={'manufacturer':'Example','model':'VM','client_version':'0.9.0','targets':[{'identity':'serial:LAB-001','size_bytes':64*1024**3}]}
    assert c.post('/api/pxe/enroll',headers={'Authorization':'Bearer '+ticket},json=body).status_code==409
    configure(c,default_profile_id=generic['id'])
    assert c.post('/api/pxe/enroll',headers={'Authorization':'Bearer '+ticket},json={**body,'client_version':'0.10.0','managed_selection':True}).status_code==409
    headers,session=managed_enroll(c,ticket)
    assert session['automation']['targetDiskPolicy']=='format-selected-disk'
    configure(c,target_disk_policy='blank-raw')
    selection=c.post('/api/pxe/select',headers=headers,json={'profile_id':generic['id'],'target_identity':'serial:LAB-001'}).json()
    assert selection['target_policy']=='format-selected-disk'
    ticket=boot(c,'52:54:00:12:34:59');headers,session=managed_enroll(c,ticket)
    assert session['automation']['targetDiskPolicy']=='blank-raw'


def test_automatic_profile_rejects_explicit_packages_and_invalid_waits(lab):
    c,root,p=lab;uid=library_package(c)
    body={'name':'Generic','windows_label':'Pro','image_id':p['image_id'],'driver_source':'automatic'}
    assert c.post('/api/profiles',json={**body,'driver_ids':[uid]},headers=auth()).status_code==422
    for key,value in [('windows_update_timeout_seconds',59),('windows_update_timeout_seconds',1801),('windows_update_max_attempts',0),('windows_update_max_attempts',4)]:
        assert c.post('/api/profiles',json={**body,key:value},headers=auth()).status_code==422


def test_boot_critical_publication_requires_reviewed_package_inf(lab):
    c,root,p=lab
    uid=library_package(c,'Example','VM',critical=['nic/driver.inf'])
    manifest=c.get('/api/drivers/'+uid+'/client-manifest',headers=auth('deploy')).json()
    assert manifest['bootCriticalInfPaths']==['nic/driver.inf']
    payload=driver_zip()
    spec={'kind':'drivers','name':'Invalid boot driver','filename':'drivers.zip','size':len(payload),
          'sha256':hashlib.sha256(payload).hexdigest(),'make':'Example','model':'VM','windows_version':'26100','boot_critical_inf_paths':['../outside.inf']}
    bad=c.post('/api/uploads',json=spec,headers=auth()).json()['id']
    assert send(c,bad,payload).status_code==200
    assert c.post('/api/uploads/'+bad+'/finish',headers=auth()).status_code==422
    assert publish(c,bad).status_code==409


def test_network_first_restart_returns_to_pending_windows(lab):
    c,root,p=lab
    headers,uid=enroll(c,boot(c))
    c.post('/api/pxe/select',headers=headers,json={'profile_id':p['id'],'target_identity':'serial:LAB-001'})
    job=c.post('/api/pxe/start',headers=headers,json={}).json()['id']
    for stage in ('awaiting-windows','completing','needs-operator','ready-for-oobe'):
        with c.app.state.store.db() as db:
            db.execute('UPDATE jobs SET stage=? WHERE id=?',(stage,job))
            before=db.execute('SELECT count(*) FROM clients').fetchone()[0]
        response=c.get('/pxe/boot.ipxe?mac=52:54:00:12:34:56')
        assert response.status_code==200 and response.headers['cache-control']=='no-store'
        assert response.text.endswith('exit\n') and 'initrd' not in response.text and 'Booting the installed disk' in response.text
        with c.app.state.store.db() as db:assert db.execute('SELECT count(*) FROM clients').fetchone()[0]==before
        boot(c,'52:54:00:12:34:57')
    with c.app.state.store.db() as db:db.execute("UPDATE jobs SET stage='complete',report='{}' WHERE id=?",(job,))
    ticket=boot(c)
    later_headers,later_uid=enroll(c,ticket)
    c.post('/api/pxe/select',headers=later_headers,json={'profile_id':p['id'],'target_identity':'serial:LAB-001'})
    later=c.post('/api/pxe/start',headers=later_headers,json={}).json()['id']
    with c.app.state.store.db() as db:
        db.execute("UPDATE jobs SET stage='awaiting-windows',report=NULL WHERE id=?",(job,))
        db.execute("UPDATE jobs SET stage='complete',report='{}' WHERE id=?",(later,))
    boot(c)
