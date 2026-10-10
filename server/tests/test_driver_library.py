import hashlib
import io
import json
import uuid
import zipfile

from test_api import client, auth, start, send, publish
from repxe.app import now, create_app
from fastapi.testclient import TestClient


def archive(folder='driver', sys=b'system fixture', second=False):
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w') as zipped:
        zipped.writestr(folder+'/driver.inf','[Version]\nCatalogFile=driver.cat\nClass=Net\nProvider="Fixture provider"\nDriverVer=01/01/2026,1.2.3.4\n')
        zipped.writestr(folder+'/driver.cat',b'fixture catalog, not a signed package')
        zipped.writestr(folder+'/driver.sys',sys)
        if second:zipped.writestr(folder+'/other.inf','[Version]\nCatalogFile=driver.cat\nClass=System\n')
    return stream.getvalue()


def reviewed(c,folder='driver',model='Model A',sys=b'system fixture',second=False):
    payload=archive(folder,sys,second)
    spec={'kind':'drivers','name':'Fixture driver','filename':'drivers.zip','size':len(payload),
          'sha256':hashlib.sha256(payload).hexdigest(),'make':'Fixture maker','model':model,'windows_version':'26300'}
    response=c.post('/api/uploads',json=spec,headers=auth('agent'));assert response.status_code==201,response.text
    uid=response.json()['id'];assert send(c,uid,payload,role='agent').status_code==200
    assert c.post(f'/api/uploads/{uid}/finish',headers=auth('agent')).status_code==200
    response=publish(c,uid);assert response.status_code==200,response.text
    return uid,response.json()['drivers']


def job(c,model='Model A',enrolled=True):
    image_id=str(uuid.uuid4());metadata={'id':image_id,'architecture':'amd64','selected_image':{'build':'26300'},'image_index':1,'sha256':'a'*64}
    with c.app.state.store.db() as db:
        db.execute('INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?)',(image_id,'image','Fixture image','a'*64,1,json.dumps(metadata),now(),'Driver API fixture'))
    response=c.post('/api/jobs',json={'image_id':image_id,'target_identity':'serial:FIXTURE','device_model':model},headers=auth('deploy'))
    assert response.status_code==201,response.text
    uid=response.json()['id']
    credential=c.post(f'/api/jobs/{uid}/report-credential',json={},headers=auth('deploy')).json()['token']
    with c.app.state.store.db() as db:
        db.execute("UPDATE jobs SET stage='awaiting-windows' WHERE id=?",(uid,))
        if enrolled:
            db.execute('INSERT INTO clients(id,mac,ticket_hash,ticket_expires,manufacturer,model,job_id,created,seen) VALUES(?,?,?,?,?,?,?,?,?)',
                       (str(uuid.uuid4()),'52:54:00:00:00:01','ticket',now(),'Fixture maker',model,uid,now(),now()))
    return uid,{'Authorization':'Bearer '+credential}


def connect(c,uid,credential,model='Model A',expected=200):
    response=c.post(f'/api/client-jobs/{uid}/drivers/catalog',json={'manufacturer':'Fixture maker','model':model},headers=credential)
    assert response.status_code==expected,response.text
    return response.json()


def test_identical_driver_reuses_content_and_adds_model_tags(client):
    old,first=reviewed(client);new,second=reviewed(client,folder='renamed-export-folder',model='Model B')
    assert first['new']==1 and first['reused']==0
    assert second['new']==0 and second['reused']==1
    assert first['driver_ids']==second['driver_ids']
    entries=client.get('/api/driver-library',headers=auth()).json();assert len(entries)==1
    entry=entries[0];assert entry['provider']=='Fixture provider' and entry['class']=='Net'
    assert {tag['model'] for tag in entry['tags']}=={'Model A','Model B'}
    blobs=list((client.app.state.store.root/'drivers/blobs').iterdir());assert len(blobs)==3
    source=client.app.state.store.root/'drivers/packages'/old/'files/driver/driver.sys'
    target=client.app.state.store.root/'drivers/packages'/new/'files/renamed-export-folder/driver.sys'
    assert source.stat().st_ino==target.stat().st_ino
    assert client.get('/api/overview',headers=auth()).json()['counts']['drivers']==1


def test_new_version_keeps_distinct_entry_but_reuses_shared_files(client):
    reviewed(client);reviewed(client,sys=b'an updated driver binary')
    assert len(client.get('/api/driver-library',headers=auth()).json())==2
    assert len(list((client.app.state.store.root/'drivers/blobs').iterdir()))==4


def test_multiple_infs_preserve_supporting_files(client):
    _,result=reviewed(client,second=True);assert result['new']==2
    entries=client.get('/api/driver-library',headers=auth()).json();assert len(entries)==2
    assert all(len(entry['files'])==4 for entry in entries)
    assert len(list((client.app.state.store.root/'drivers/blobs').iterdir()))==4


def test_add_reviewed_model_tag_without_upload(client):
    _,result=reviewed(client);uid=result['driver_ids'][0]
    tag={'manufacturer':'Other maker','model':'Model C','windows_build':'26300','hardware_test_passed':False,'validation_note':'Hardware checked on disposable fixture'}
    assert client.post(f'/api/driver-library/{uid}/tags',json=tag,headers=auth()).status_code==422
    tag['hardware_test_passed']=True
    assert client.post(f'/api/driver-library/{uid}/tags',json=tag,headers=auth('agent')).status_code==403
    assert client.post(f'/api/driver-library/{uid}/tags',json=tag,headers=auth()).status_code==200
    assert len(client.get('/api/driver-library',headers=auth()).json()[0]['tags'])==2
    assert len(list((client.app.state.store.root/'drivers/blobs').iterdir()))==3


def test_job_catalog_is_bound_to_enrollment_and_retrieves_verified_content(client):
    reviewed(client);uid,credential=job(client)
    catalog=connect(client,uid,credential);assert len(catalog['drivers'])==1
    manifest=catalog['drivers'][0]['manifest'];assert manifest['model']=='Model A' and manifest['windowsBuild']=='26300'
    for index,entry in enumerate(manifest['files']):
        response=client.get(f"/api/client-jobs/{uid}/drivers/{manifest['packageId']}/files/{index}",headers=credential)
        assert response.status_code==200
        assert hashlib.sha256(response.content).hexdigest()==entry['sha256']
    connect(client,uid,credential,'Different model',403)
    other,other_credential=job(client,'Model B')
    connect(client,other,credential,'Model B',401)
    assert client.get(f"/api/client-jobs/{other}/drivers/{manifest['packageId']}/files/0",headers=other_credential).status_code==409
    assert connect(client,other,other_credential,'Model B')['drivers']==[]
    assert client.get(f"/api/client-jobs/{other}/drivers/{manifest['packageId']}/files/0",headers=other_credential).status_code==404
    assert client.get('/api/driver-library',headers=credential).status_code==401


def test_job_upload_is_automatic_bound_and_owned(client):
    uid,credential=job(client);connect(client,uid,credential)
    data=archive();spec={'kind':'drivers','name':'Console fixture','filename':'drivers.zip','size':len(data),'sha256':hashlib.sha256(data).hexdigest(),
                        'make':'forged maker','model':'forged model','windows_version':'99999'}
    uploaded=client.post(f'/api/client-jobs/{uid}/driver-uploads',json=spec,headers=credential)
    assert uploaded.status_code==201,uploaded.text
    upload_id=uploaded.json()['id']
    assert uploaded.json()['spec']['make']=='Fixture maker'
    assert uploaded.json()['spec']['model']=='Model A' and uploaded.json()['spec']['windows_version']=='26300'
    other,other_credential=job(client,'Model B');connect(client,other,other_credential,'Model B')
    assert client.get(f'/api/client-jobs/{other}/driver-uploads/{upload_id}',headers=other_credential).status_code==403
    assert client.patch(f'/api/client-jobs/{uid}/driver-uploads/{upload_id}/content',content=data,headers={**credential,'Upload-Offset':'0'}).status_code==200
    assert client.post(f'/api/client-jobs/{uid}/driver-uploads/{upload_id}/finish',headers=credential).json()['status']=='published'
    assert len(client.get('/api/driver-library',headers=auth()).json())==1
    assert client.post(f'/api/uploads/{upload_id}/publish',json={'validation_note':'Cannot publish with scoped credential','hardware_test_passed':True},headers=credential).status_code==401
    assert publish(client,upload_id).status_code==200
    assert len(connect(client,uid,credential)['drivers'])==1
    spec['kind']='image';spec['filename']='capture.wim'
    assert client.post(f'/api/client-jobs/{uid}/driver-uploads',json=spec,headers=credential).status_code==403


def test_completed_or_rotated_credentials_cannot_access_drivers(client):
    uid,credential=job(client);connect(client,uid,credential)
    client.post(f'/api/jobs/{uid}/report-credential',json={},headers=auth('deploy'))
    connect(client,uid,credential,expected=401)
    with client.app.state.store.db() as db:
        db.execute("UPDATE jobs SET stage='complete' WHERE id=?",(uid,))
    new=client.post(f'/api/jobs/{uid}/report-credential',json={},headers=auth('deploy'))
    assert new.status_code==409


def test_console_report_requires_verified_restart_even_after_manual_repair(client,monkeypatch):
    from test_api import native_job
    _,cap,snapshot=native_job(client,monkeypatch)
    snapshot.update(driverConsole=True,driverRebootRequired=False,driverInstallationMayBeInProgress=False,
                    driverVerifiedBoot='third',windowsUpdatePolicyRestored=True,
                    bootObservations=[{'bootId':'first'},{'bootId':'second'},{'bootId':'third'}],
                    windowsUpdatePolicy={'timeoutSeconds':60,'maxAttempts':2},
                    windowsUpdateAttempts=[{'status':'failed','result':{'automaticReboot':False}}])
    for field,value in [('driverVerifiedBoot','first'),('driverRebootRequired',True),('driverInstallationMayBeInProgress',True),('windowsUpdatePolicyRestored',False)]:
        bad={**snapshot,field:value}
        assert client.post(cap['path'],json=bad,headers={'Authorization':'Bearer '+cap['token']}).status_code==422
    bad={**snapshot,'windowsUpdateAttempts':[{'status':'running'}]}
    assert client.post(cap['path'],json=bad,headers={'Authorization':'Bearer '+cap['token']}).status_code==422
    response=client.post(cap['path'],json=snapshot,headers={'Authorization':'Bearer '+cap['token']})
    assert response.status_code==200,response.text
