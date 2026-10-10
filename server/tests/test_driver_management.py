import json
from fastapi.testclient import TestClient
from test_api import client, auth, TOKENS, native_job
from test_driver_library import reviewed, job, connect
from repxe.app import create_app, now


def delete(c, ids, path='delete'):
    return c.post('/api/driver-library/'+path,json={'ids':ids},headers=auth())


def test_delete_auth_gc_and_restart_do_not_restore(client):
    archive,result=reviewed(client);uid=result['driver_ids'][0]
    assert client.post('/api/driver-library/delete',json={'ids':[uid]},headers=auth('agent')).status_code==403
    assert delete(client,[uid],'delete-preview').json()['blocked']==0
    response=delete(client,[uid]);assert response.status_code==200,response.text
    assert response.json()['freed_bytes']>0
    assert client.get('/api/driver-library',headers=auth()).json()==[]
    assert client.get('/api/drivers',headers=auth()).json()==[]
    assert client.get('/api/drivers/'+archive+'/client-manifest',headers=auth('deploy')).status_code==404
    assert not list((client.app.state.store.root/'drivers/blobs').iterdir())
    with TestClient(create_app(client.app.state.store.root,TOKENS)) as restarted:
        assert restarted.get('/api/driver-library',headers=auth()).json()==[]
        assert not (client.app.state.store.root/'drivers/manifests'/f'{archive}.json').exists()
        _,restored=reviewed(restarted);assert restored['driver_ids']==[uid]
        assert len(restarted.get('/api/driver-library',headers=auth()).json())==1
        assert restarted.get('/api/drivers/'+archive+'/client-manifest',headers=auth('deploy')).status_code==404


def test_delete_preserves_shared_files_and_other_entry(client):
    _,first=reviewed(client);_,second=reviewed(client,sys=b'updated fixture')
    ids=first['driver_ids'];other=second['driver_ids'][0]
    response=delete(client,ids);assert response.status_code==200,response.text
    entries=client.get('/api/driver-library',headers=auth()).json();assert [e['id'] for e in entries]==[other]
    for f in entries[0]['files']:
        assert (client.app.state.store.root/'drivers/blobs'/f['sha256']).stat().st_size==f['size']
    assert len(list((client.app.state.store.root/'drivers/blobs').iterdir()))==3


def test_usage_does_not_count_catalog_and_pins_block_atomic_deletion(client):
    _,first=reviewed(client);_,second=reviewed(client,model='Model B',sys=b'unrelated fixture')
    uid,credential=job(client);manifest=connect(client,uid,credential)['drivers'][0]['manifest'];id=manifest['packageId']
    entries=client.get('/api/driver-library',headers=auth()).json();assert all(e['last_used'] is None for e in entries)
    response=delete(client,first['driver_ids']+second['driver_ids']);assert response.status_code==409
    assert len(client.get('/api/driver-library',headers=auth()).json())==2
    for index in (0,1,0):
        assert client.get(f'/api/client-jobs/{uid}/drivers/{id}/files/{index}',headers=credential).status_code==200
    entry=next(e for e in client.get('/api/driver-library',headers=auth()).json() if e['id']==id)
    assert entry['use_count']==1 and entry['last_used'] and entry['confirmed_count']==0
    with client.app.state.store.db() as db:db.execute("UPDATE jobs SET stage='complete' WHERE id=?",(uid,))
    assert delete(client,[id]).status_code==200


def test_saved_profile_retains_files_but_enabled_profile_blocks(client):
    archive,result=reviewed(client);uid=result['driver_ids'][0]
    with client.app.state.store.db() as db:
        db.execute('INSERT INTO deployment_profiles VALUES(?,?,?,?)',('profile',json.dumps({'name':'Workshop','driver_ids':[archive]}),1,now()))
    assert delete(client,[uid]).status_code==409
    with client.app.state.store.db() as db:db.execute('UPDATE deployment_profiles SET enabled=0')
    response=delete(client,[uid]);assert response.status_code==200,response.text
    assert response.json()['freed_bytes']==0
    assert (client.app.state.store.root/'drivers/packages'/archive/'files').is_dir()
    assert len(list((client.app.state.store.root/'drivers/blobs').iterdir()))==3


def test_waiting_pxe_selection_is_protected(client):
    archive,result=reviewed(client);uid=result['driver_ids'][0]
    with client.app.state.store.db() as db:
        db.execute('INSERT INTO clients(id,mac,ticket_hash,ticket_expires,selection,created,seen) VALUES(?,?,?,?,?,?,?)',('fixture','52:54:00:00:00:01','ticket','2099-01-01T00:00:00+00:00',json.dumps({'driverManifest':{'packageId':archive}}),now(),now()))
    response=delete(client,[uid]);assert response.status_code==409,response.text


def test_report_usage_backfill_and_groups(client,monkeypatch):
    archive,result=reviewed(client);uid=result['driver_ids'][0]
    assert client.put('/api/driver-library/group',json={'ids':[uid],'name':'  Retest  '},headers=auth()).json()['group']=='Retest'
    job_id,cap,snapshot=native_job(client,monkeypatch)
    snapshot['driverActions']=[{'packageId':archive,'inf':'driver/driver.inf','exitCode':0},{'packageId':uid,'inf':'driver.inf','exitCode':3010},{'packageId':uid,'inf':'driver.inf','exitCode':1},None]
    response=client.post(cap['path'],json=snapshot,headers={'Authorization':'Bearer '+cap['token']});assert response.status_code==200,response.text
    entry=client.get('/api/driver-library',headers=auth()).json()[0]
    assert entry['use_count']==1 and entry['confirmed_count']==1 and entry['group']=='Retest'
    with client.app.state.store.db() as db:
        db.execute('DELETE FROM driver_history_imported');db.execute('DELETE FROM driver_usage')
    with TestClient(create_app(client.app.state.store.root,TOKENS)) as restarted:
        entry=restarted.get('/api/driver-library',headers=auth()).json()[0]
        assert entry['use_count']==1 and entry['confirmed_count']==1 and entry['group']=='Retest'
    assert delete(client,[uid]).status_code==200
    assert client.get('/api/jobs/'+job_id+'/report',headers=auth()).json()['native']==snapshot


def test_website_preferences_and_status_are_admin_only(client):
    assert client.get('/api/server-info',headers=auth('deploy')).status_code==403
    info=client.get('/api/server-info',headers=auth()).json();assert info['storage']['free']>0
    prefs={'site_name':'Workshop','refresh_seconds':10,'driver_stale_days':180,'notes':'Contact the lead operator'}
    assert client.put('/api/website-settings',json=prefs,headers=auth()).json()==prefs
    with TestClient(create_app(client.app.state.store.root,TOKENS)) as restarted:
        assert restarted.get('/api/website-settings',headers=auth()).json()==prefs
    assert client.put('/api/website-settings',json={**prefs,'refresh_seconds':0},headers=auth()).status_code==422
    assert client.put('/api/website-settings',json={**prefs,'site_name':'   '},headers=auth()).status_code==422


def test_cookie_deletion_and_preferences_require_csrf(client):
    _,result=reviewed(client);uid=result['driver_ids'][0]
    with TestClient(client.app, base_url='https://testserver') as browser_client:
        session=browser_client.post('/api/login',json={'username':'admin','password':'admin'}).json()
        assert browser_client.post('/api/driver-library/delete',json={'ids':[uid]}).status_code==403
        assert browser_client.put('/api/website-settings',json={}).status_code==403
        headers={'X-CSRF-Token':session['csrf_token'],'Origin':'https://testserver'}
        assert browser_client.post('/api/driver-library/delete-preview',json={'ids':[uid]},headers=headers).status_code==200
        assert browser_client.post('/api/driver-library/delete',json={'ids':[uid]},headers=headers).status_code==200


def test_retirement_removes_automatic_archive_selection(client):
    archive,result=reviewed(client);uid=result['driver_ids'][0]
    image='11111111-1111-4111-8111-111111111111'
    with client.app.state.store.db() as db:
        db.execute('INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?)',(image,'image','Fixture image','a'*64,1,json.dumps({'architecture':'amd64','selected_image':{'build':'26300'},'sha256':'a'*64,'image_index':1}),now(),'Fixture image metadata'))
    profile=client.post('/api/profiles',json={'name':'Automatic fixture','windows_label':'Windows fixture','image_id':image,'driver_source':'automatic'},headers=auth()).json()
    assert client.post('/api/profiles/'+profile['id']+'/availability',json={'enabled':True},headers=auth()).status_code==200
    path='/api/boot-options?manufacturer=Fixture%20maker&model=Model%20A'
    assert client.get(path,headers=auth('deploy')).json()['choices'][0]['driver_id']==archive
    assert delete(client,[uid]).status_code==200
    assert client.get(path,headers=auth('deploy')).json()['choices'][0]['driver_id'] is None


def test_cleanup_failure_cannot_revive_missing_legacy_payload(client,monkeypatch):
    import shutil
    archive,result=reviewed(client);uid=result['driver_ids'][0]
    original=shutil.rmtree
    def fail_after_remove(path,*args,**kwargs):
        original(path,*args,**kwargs)
        raise OSError('Fixture interrupted cleanup')
    with monkeypatch.context() as patch:
        patch.setattr('repxe.driver_management.shutil.rmtree',fail_after_remove)
        response=delete(client,[uid]);assert response.status_code==200 and response.json()['cleanup_deferred']
    _,restored=reviewed(client);assert restored['driver_ids']==[uid]
    assert client.get('/api/drivers/'+archive+'/client-manifest',headers=auth('deploy')).status_code==404
