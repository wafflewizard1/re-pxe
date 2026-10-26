"""Synthetic clients only: no shutdowns, reboots or writes to real desktops."""
from datetime import timedelta
import hashlib
import uuid
import pytest
from repxe import windows_controls as controls
from test_api import auth
from test_pxe import lab, boot, enroll
from test_auth import login, csrf


def session(c, profile, mac='52:54:00:12:34:56'):
    pe, client = enroll(c, boot(c, mac))
    assert c.post('/api/pxe/select', headers=pe, json={'profile_id':profile['id'], 'target_identity':'serial:LAB-001'}).status_code == 200
    job = c.post('/api/pxe/start', headers=pe, json={}).json()
    cap = {'Authorization':'Bearer '+job['reportCredential']['token']}
    base = '/api/client-jobs/'+job['id']+'/windows'
    assert c.post('/api/client-jobs/'+job['id']+'/heartbeat', headers=cap, json={'stage':'completing'}).status_code == 200
    assert c.post(base+'/poll', headers=cap, json={'client_version':'0.14.0','busy':False}).status_code == 200
    return client, job['id'], cap, pe, base


def test_power_waits_for_idle_and_reports_idempotently(lab):
    c,root,p = lab
    client,job,cap,pe,base = session(c,p)
    portal='/api/clients/'+client+'/windows'
    for role in ('agent','capture','deploy'):
        assert c.get(portal,headers=auth(role)).status_code == 403
        assert c.post(portal+'/commands',headers=auth(role),json={'action':'restart','expected_job_id':job}).status_code == 403
    assert c.post(portal+'/commands',headers=auth(),json={'action':'execute','expected_job_id':job}).status_code == 422
    assert c.post(portal+'/commands',headers=auth(),json={'action':'restart','expected_job_id':str(uuid.uuid4())}).status_code == 409
    task=c.post(portal+'/commands',headers=auth(),json={'action':'restart','expected_job_id':job})
    assert task.status_code == 201,task.text
    task=task.json()
    assert c.post(base+'/poll',headers=pe,json={'client_version':'0.14.0','busy':False}).status_code == 401
    assert c.post(base+'/poll',headers=cap,json={'client_version':'0.14.0','busy':True}).json()['command'] is None
    assert c.get(portal,headers=auth()).json()['busy'] is True
    poll=c.post(base+'/poll',headers=cap,json={'client_version':'0.14.0','busy':False}).json()['command']
    assert poll['id']==task['id']
    # Lost responses redeliver the same identifier, consumed once by the client receipt.
    assert c.post(base+'/poll',headers=cap,json={'client_version':'0.14.0','busy':False}).json()['command']['id']==task['id']
    result=base+'/commands/'+task['id']+'/result'
    payload={'status':'completed','path':'','error':''}
    assert c.post(result,headers=cap,json=payload).status_code==200
    assert c.post(result,headers=cap,json=payload).status_code==200
    assert c.post(result,headers=cap,json={**payload,'status':'failed'}).status_code==409
    assert c.post(base+'/poll',headers=cap,json={'client_version':'0.14.0','busy':False}).json()['command'] is None
    assert c.get(portal,headers=auth()).json()['commands'][0]['status']=='completed'


def test_browser_file_delivery_is_bound_to_job_and_desktop_leaf(lab,monkeypatch):
    c,root,p=lab
    client,job,cap,pe,base=session(c,p)
    other,other_job,other_cap,_,other_base=session(c,p,'52:54:00:12:34:57')
    portal='/api/clients/'+client+'/windows'
    payload=b'An ordinary desktop file\n'
    headers={**auth(),'Content-Type':'application/octet-stream'}
    params={'filename':'Technician notes.txt','expected_job_id':job}
    for name in ('../escape.txt','C:\\escape.txt','CON.txt','NUL','COM1.log','bad:stream','bad.','LPT².txt','bad\x00.txt'):
        r=c.post(portal+'/files',headers=headers,params={**params,'filename':name},content=payload)
        assert r.status_code==422,(name,r.text)
    monkeypatch.setattr(controls,'MAX_FILE',8)
    assert c.post(portal+'/files',headers=headers,params=params,content=payload).status_code==413
    assert not list((root/'windows-controls').iterdir())
    monkeypatch.setattr(controls,'MAX_FILE',100*1024**3)
    task=c.post(portal+'/files',headers=headers,params=params,content=payload)
    assert task.status_code==201,task.text
    task=task.json()
    assert task['size']==len(payload) and task['sha256']==hashlib.sha256(payload).hexdigest()
    command=c.post(base+'/poll',headers=cap,json={'client_version':'0.14.0','busy':False}).json()['command']
    path=command['download_path']
    assert c.get(path,headers=other_cap).status_code==401
    assert c.get(other_base+'/files/'+task['id'],headers=other_cap).status_code==404
    assert c.get(path,headers=cap).content==payload
    r=c.post(base+'/commands/'+task['id']+'/result',headers=cap,json={'status':'completed','path':'C:\\Users\\Administrator\\Desktop\\Technician notes.txt'})
    assert r.status_code==200,r.text
    assert not (root/'windows-controls'/task['id']).exists()
    assert 'Desktop' in c.get(portal,headers=auth()).json()['commands'][0]['result']['path']


def test_cookie_upload_requires_csrf_and_preserves_file_bytes(lab):
    c,root,p=lab
    client,job,cap,pe,base=session(c,p)
    web=login(c)
    url='/api/clients/'+client+'/windows/files'
    params={'filename':'test.txt','expected_job_id':job}
    assert c.post(url,headers={'Content-Type':'application/octet-stream'},params=params,content=b'data').status_code==403
    r=c.post(url,headers={**csrf(web),'Content-Type':'application/octet-stream'},params=params,content=b'data')
    assert r.status_code==201,r.text


@pytest.mark.parametrize('power',['restart','shutdown'])
@pytest.mark.parametrize('already_claimed',[False,True])
def test_files_arrive_during_driver_work_ahead_of_waiting_power(lab,power,already_claimed):
    c,root,p=lab
    client,job,cap,pe,base=session(c,p)
    portal='/api/clients/'+client+'/windows'
    power_id=c.post(portal+'/commands',headers=auth(),json={'action':power,'expected_job_id':job}).json()['id']
    if already_claimed:
        assert c.post(base+'/poll',headers=cap,json={'client_version':'0.14.1','busy':False}).json()['command']['id']==power_id
    poll={'client_version':'0.14.1','busy':True}
    assert c.post(base+'/poll',headers=cap,json=poll).json()['command'] is None
    assert c.get(portal,headers=auth()).json()['files_during_drivers'] is True
    payload=b'Desktop delivery while drivers are still running'
    file=c.post(portal+'/files',headers={**auth(),'Content-Type':'application/octet-stream'},
                params={'filename':'notes.txt','expected_job_id':job},content=payload)
    assert file.status_code==201,file.text
    file_id=file.json()['id']
    # A progress-only pulse during a previous transfer must never claim the next file.
    assert c.post(base+'/poll',headers=cap,json={**poll,'accept_commands':False}).json()['command'] is None
    with c.app.state.store.db() as db:
        assert db.execute('SELECT status FROM windows_commands WHERE id=?',(file_id,)).fetchone()[0]=='queued'
    command=c.post(base+'/poll',headers=cap,json=poll).json()['command']
    assert command['id']==file_id and command['action']=='file'
    assert c.get(command['download_path'],headers=cap).content==payload
    assert c.post(base+'/commands/'+file_id+'/result',headers=cap,json={'status':'completed','path':'C:\\Users\\Administrator\\Desktop\\notes.txt'}).status_code==200
    assert c.post(base+'/poll',headers=cap,json=poll).json()['command'] is None
    assert c.post(base+'/poll',headers=cap,json={**poll,'busy':False}).json()['command']['id']==power_id


def test_legacy_busy_client_does_not_claim_files_it_cannot_deliver(lab):
    c,root,p=lab
    client,job,cap,pe,base=session(c,p)
    portal='/api/clients/'+client+'/windows'
    c.post(base+'/poll',headers=cap,json={'client_version':'0.14.0','busy':True})
    response=c.post(portal+'/files',headers={**auth(),'Content-Type':'application/octet-stream'},
                    params={'filename':'notes.txt','expected_job_id':job},content=b'notes')
    assert response.status_code==409 and '0.14.1' in response.text
    assert c.get(portal,headers=auth()).json()['commands']==[]


@pytest.mark.parametrize('end',['expired','terminal','retired','new-session'])
def test_old_commands_cannot_reach_reinstalled_or_ended_client(lab,end):
    c,root,p=lab
    client,job,cap,pe,base=session(c,p)
    portal='/api/clients/'+client+'/windows'
    assert c.post(portal+'/commands',headers=auth(),json={'action':'shutdown','expected_job_id':job}).status_code==201
    with c.app.state.store.db() as db:
        if end=='expired': db.execute('UPDATE report_credentials SET expires=? WHERE job_id=?',((controls.now()-timedelta(seconds=1)).isoformat(),job))
        elif end=='terminal': db.execute("UPDATE jobs SET stage='complete' WHERE id=?",(job,))
        elif end=='retired': db.execute('INSERT INTO client_retirements VALUES(?,?)',(client,controls.now().isoformat()))
        else:
            db.execute("INSERT INTO clients(id,mac,ticket_hash,ticket_expires,created,seen) SELECT ?,mac,'x','x',?,? FROM clients WHERE id=?",(str(uuid.uuid4()),controls.now().isoformat(),controls.now().isoformat(),client))
    assert c.get(portal,headers=auth()).json()['available'] is False
    assert c.post(base+'/poll',headers=cap,json={'client_version':'0.14.0','busy':False}).status_code in (401,409)
    assert c.post(portal+'/commands',headers=auth(),json={'action':'restart','expected_job_id':job}).status_code==409


def test_offline_agent_and_expired_commands(lab):
    c,root,p=lab
    client,job,cap,pe,base=session(c,p)
    portal='/api/clients/'+client+'/windows'
    assert c.post(portal+'/commands',headers=auth(),json={'action':'restart','expected_job_id':job}).status_code==201
    with c.app.state.store.db() as db:
        old=(controls.now()-timedelta(hours=3)).isoformat()
        db.execute('UPDATE windows_agents SET seen=?',(old,))
        db.execute('UPDATE windows_commands SET expires=?',(old,))
    assert c.get(portal,headers=auth()).json()['available'] is False
    assert c.post(portal+'/commands',headers=auth(),json={'action':'shutdown','expected_job_id':job}).status_code==409
    assert c.post(base+'/poll',headers=cap,json={'client_version':'0.14.0','busy':False}).json()['command'] is None
    assert c.get(portal,headers=auth()).json()['commands'][0]['status']=='expired'
