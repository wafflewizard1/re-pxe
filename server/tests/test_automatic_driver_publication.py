import json
from test_api import client,auth,start,send,driver_zip
from test_driver_library import archive,job,connect


def test_validation_publishes_for_agent_once_without_hardware_claim(client):
    payload=archive();uid=start(client,payload,role='agent')
    # Native matching requires a numeric build; bind the same declared model/build.
    with client.app.state.store.db() as db:
        spec=json.loads(db.execute('SELECT spec FROM uploads WHERE id=?',(uid,)).fetchone()['spec'])
        spec.update(make='Fixture maker',model='Model A',windows_version='26300')
        db.execute('UPDATE uploads SET spec=? WHERE id=?',(json.dumps(spec),uid))
    assert send(client,uid,payload,role='agent').status_code==200
    result=client.post('/api/uploads/'+uid+'/finish',headers=auth('agent'))
    assert result.status_code==200 and result.json()['status']=='published'
    manifest=client.get('/api/drivers/'+uid+'/client-manifest',headers=auth('deploy')).json()
    assert manifest['review']['hardwareTestPassed'] and 'Automatically published' in manifest['review']['note']
    meta=client.get('/api/drivers',headers=auth()).json()[0]
    assert meta['installation_authorized'] and meta['automatic_publication'] and meta['hardware_test_passed'] is False
    assert client.post('/api/uploads/'+uid+'/finish',headers=auth('agent')).json()['status']=='published'
    assert len(client.get('/api/driver-library',headers=auth()).json())==1
    uid_job,cap=job(client)
    assert len(connect(client,uid_job,cap)['drivers'])==1


def test_rejected_driver_never_enters_library_and_can_cancel_directly(client):
    payload=driver_zip({'bad.inf':'[Version]\nCatalogFile=missing.cat'})
    uid=start(client,payload);send(client,uid,payload)
    assert client.post('/api/uploads/'+uid+'/finish',headers=auth()).status_code==422
    assert client.get('/api/driver-library',headers=auth()).json()==[]
    assert client.delete('/api/uploads/'+uid,headers=auth()).status_code==204


def test_receiving_driver_can_cancel_with_no_confirmation(client):
    payload=driver_zip();uid=start(client,payload)
    assert client.delete('/api/uploads/'+uid,headers=auth()).status_code==204
    assert client.get('/api/uploads/'+uid,headers=auth()).json()['status']=='cancelled'
