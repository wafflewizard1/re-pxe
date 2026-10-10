"""Pending visibility, scoped cancellation, stale controls and retained evidence."""
import json
from datetime import datetime, timedelta, timezone
import pytest
from test_api import auth, TOKENS
from test_pxe import lab, boot, enroll


def start(c, p, mac='52:54:00:12:34:56'):
    headers, uid = enroll(c, boot(c, mac))
    assert c.post('/api/pxe/select', headers=headers, json={'profile_id':p['id'], 'target_identity':'serial:LAB-001'}).status_code == 200
    result = c.post('/api/pxe/start', headers=headers, json={}).json()
    return headers, uid, result['id'], {'Authorization':'Bearer '+result['reportCredential']['token']}


def control(mac='52:54:00:12:34:56', job=None, stopped=True):
    return {'expected_job_id':job, 'reason':'Test operator reset'}


def old(c, uid, job, stage='awaiting-windows'):
    at = (datetime.now(timezone.utc)-timedelta(days=2)).isoformat()
    with c.app.state.store.db() as db:
        db.execute('UPDATE clients SET seen=?,expires=?,stage=? WHERE id=?', (at,at,'needs-operator',uid))
        db.execute('UPDATE jobs SET stage=? WHERE id=?', (stage,job))


def test_pending_survives_timeout_and_details_are_secret_free(lab):
    c, root, p = lab; headers, uid, job, cap = start(c,p); old(c,uid,job)
    d=c.get('/api/dashboard',headers=auth()).json()
    assert d['active_jobs']==1 and d['client_counts']['pending']==1
    row=d['clients'][0]
    assert row['id']==uid and row['pending'] and row['boot_blocked'] and not row['online']
    assert 'completion is pending' in c.get('/pxe/boot.ipxe?mac='+row['mac']).text
    details=c.get('/api/clients/'+uid,headers=auth())
    assert details.json()['job']['id']==job and details.json()['client']['targets'][0]['identity']=='serial:LAB-001'
    for secret in (*TOKENS.values(), headers['Authorization'][7:], cap['Authorization'][7:], 'token_hash', 'ticket_hash'):
        assert secret not in details.text
    assert c.get('/api/clients?view=pending',headers=auth()).json()['client_total']==1
    assert c.get('/api/clients?view=attention',headers=auth()).json()['client_total']==1
    assert c.get('/api/clients?view=online',headers=auth()).json()['client_total']==0
    assert c.get('/api/clients?search=LAB-001',headers=auth()).json()['client_total']==1


def test_completion_contact_uses_completion_credential_not_expired_winpe(lab):
    c,root,p=lab;headers,uid,job,cap=start(c,p);old(c,uid,job)
    assert c.post('/api/client-jobs/'+job+'/heartbeat',headers=cap,json={'stage':'completing'}).status_code==200
    row=c.get('/api/clients',headers=auth()).json()['clients'][0]
    assert row['online'] and not row['credential_expired']
    with c.app.state.store.db() as db:
        db.execute('UPDATE report_credentials SET expires=? WHERE job_id=?',((datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(),job))
    row=c.get('/api/clients',headers=auth()).json()['clients'][0]
    assert row['pending'] and not row['online'] and row['credential_expired']


def test_cancel_revokes_all_channels_and_unblocks_boot(lab):
    c,root,p=lab;headers,uid,job,cap=start(c,p)
    with c.app.state.store.db() as db: db.execute("UPDATE jobs SET stage='awaiting-windows' WHERE id=?",(job,))
    # Cancellation is immediate; the observed job still prevents stale actions.
    path='/api/clients/'+uid+'/cancel'
    assert c.post(path,headers=auth(),json=control(job=None)).status_code==409
    assert c.post(path,headers=auth(),json=control(job=job)).status_code==200
    assert c.get('/api/jobs/'+job,headers=auth()).json()['stage']=='cancelled'
    assert c.post('/api/pxe/heartbeat',headers=headers,json={'stage':'downloading'}).status_code==401
    assert c.post('/api/pxe/start',headers=headers,json={}).status_code==401
    assert c.get('/api/pxe/image',headers=headers).status_code==401
    assert c.post('/api/client-jobs/'+job+'/heartbeat',headers=cap,json={'stage':'completing'}).status_code==401
    assert c.post('/api/client-jobs/'+job+'/report',headers=cap,json={}).status_code==401
    assert c.post('/api/client-jobs/'+job+'/drivers/catalog',headers=cap,json={'manufacturer':'Example','model':'VM'}).status_code==401
    assert c.post('/api/jobs/'+job+'/events',headers=auth('agent'),json={'stage':'needs-operator'}).status_code==409
    assert c.get('/api/clients?view=pending',headers=auth()).json()['client_total']==0
    boot(c)


def test_reset_all_old_sessions_but_reject_newer_session(lab):
    c,root,p=lab;h1,u1,j1,cap1=start(c,p)
    h2,u2,j2,cap2=start(c,p)
    path='/api/clients/'+u1+'/reset'
    assert c.post(path,headers=auth(),json=control(job=j1)).status_code==409
    assert c.post('/api/clients/'+u2+'/reset',headers=auth(),json=control(job=j2)).json()['cancelled_jobs']==[j2,j1]
    for job in (j1,j2):assert c.get('/api/jobs/'+job,headers=auth()).json()['stage']=='cancelled'
    assert c.get('/api/clients?view=pending',headers=auth()).json()['client_total']==0
    boot(c)
    assert c.post('/api/clients/'+u2+'/reset',headers=auth(),json=control(job=j2)).status_code==409


def test_delete_retains_report_history_without_resurrecting_old_guard(lab):
    c,root,p=lab;h1,u1,j1,cap1=start(c,p);h2,u2,j2,cap2=start(c,p)
    with c.app.state.store.db() as db:
        db.execute("UPDATE jobs SET stage='awaiting-windows' WHERE id=?",(j1,))
        db.execute("UPDATE jobs SET stage='complete',report=? WHERE id=?",(json.dumps({'summary':'Retained evidence','driver_problems':[]}),j2))
    assert c.request('DELETE','/api/clients/'+u1,headers=auth(),json=control(job=j1)).status_code==409
    assert c.request('DELETE','/api/clients/'+u2,headers=auth(),json=control(job=j2)).status_code==200
    assert c.get('/api/jobs/'+j2+'/report',headers=auth()).json()['summary']=='Retained evidence'
    assert c.get('/api/clients/'+u2,headers=auth()).json()['client']['deleted_at']
    assert all(r['id']!=u2 for r in c.get('/api/clients?view=history',headers=auth()).json()['clients'])
    boot(c) # retiring latest successful session does not revive the old job hold
    assert c.get('/api/clients?view=pending',headers=auth()).json()['client_total']==1


def test_paging_counts_expired_boots_and_role_validation(lab):
    c,root,p=lab
    for i in range(5):boot(c,f'52:54:00:12:34:{i:02x}')
    with c.app.state.store.db() as db:db.execute('UPDATE clients SET seen=?,ticket_expires=?',tuple([(datetime.now(timezone.utc)-timedelta(days=1)).isoformat()]*2))
    assert c.get('/api/clients',headers=auth()).json()['clients']==[]
    d=c.get('/api/clients?view=all&page_size=2&page=2',headers=auth()).json()
    assert len(d['clients'])==2 and d['client_total']==5 and d['client_pages']==3 and d['client_page']==2
    assert c.get('/api/clients?view=all&page_size=2&page=99',headers=auth()).json()['client_page']==3
    uid=d['clients'][0]['id']
    for role in ('capture','deploy','agent'):
        assert c.get('/api/clients',headers=auth(role)).status_code==403
        assert c.get('/api/clients/'+uid,headers=auth(role)).status_code==403
        assert c.post('/api/clients/'+uid+'/reset',headers=auth(role),json=control()).status_code==403
    assert c.get('/api/clients?view=invalid',headers=auth()).status_code==422
    assert c.get('/api/clients?page_size=201',headers=auth()).status_code==422
    assert c.get('/api/clients',headers={}).status_code==401
    assert c.request('DELETE','/api/clients/'+uid,headers=auth(),json=control(mac=d['clients'][0]['mac'])).status_code==200


def test_pending_sessions_are_not_hidden_by_later_boot_or_200_row_cutoff(lab):
    c,root,p=lab;headers,uid,job,cap=start(c,p);old(c,uid,job,stage='created')
    # A later abandoned boot for the same MAC must not hide the real pending job.
    boot(c)
    at=datetime.now(timezone.utc).isoformat()
    with c.app.state.store.db() as db:
        for i in range(205):
            db.execute('INSERT INTO clients(id,mac,ticket_hash,ticket_expires,created,seen) VALUES(?,?,?,?,?,?)',
                       (f'fixture-{i}',f'00:00:00:00:{i//256:02x}:{i%256:02x}',f'hash-{i}',at,at,at))
    d=c.get('/api/clients?view=pending',headers=auth()).json()
    assert d['client_total']==1 and d['clients'][0]['id']==uid
    assert c.get('/api/dashboard?view=pending',headers=auth()).json()['clients'][0]['job_id']==job
    assert c.post('/api/clients/'+uid+'/reset',headers=auth(),json=control(job=job)).status_code==409
    assert c.post('/api/clients/'+uid+'/cancel',headers=auth(),json=control(job=job)).status_code==200
    assert c.post('/api/jobs/'+job+'/report-credential',headers=auth('deploy'),json={}).status_code==409


def test_mutation_rechecks_session_after_dependency_authentication(lab,monkeypatch):
    c,root,p=lab;headers,uid,job,cap=start(c,p)
    from repxe import client_management
    original=client_management.active_session
    def cancelled_between_auth_and_write(db,client_id,at=None):
        db.execute("UPDATE clients SET stage='cancelled',expires='2000-01-01T00:00:00+00:00' WHERE id=?",(client_id,))
        return original(db,client_id,at)
    monkeypatch.setattr(client_management,'active_session',cancelled_between_auth_and_write)
    assert c.post('/api/pxe/heartbeat',headers=headers,json={'stage':'downloading'}).status_code==401
    # The dependency had valid credentials; the transaction rejected its stale snapshot.
    with c.app.state.store.db() as db:assert db.execute('SELECT stage FROM clients WHERE id=?',(uid,)).fetchone()['stage']=='checking-disk'


def test_website_control_requires_csrf(lab):
    c,root,p=lab;headers,uid,job,cap=start(c,p)
    result=c.post('/api/login',json={'username':'admin','password':'admin'})
    assert result.status_code==200,result.text
    path='/api/clients/'+uid+'/cancel'
    assert c.post(path,json=control(job=job)).status_code==403
    assert c.post(path,headers={'X-CSRF-Token':result.json()['csrf_token']},json=control(job=job)).status_code==200


def test_bulk_cancel_is_immediate_partial_and_retains_reports(lab):
    c,root,p=lab;h1,u1,j1,cap1=start(c,p);h2,u2,j2,cap2=start(c,p,'52:54:00:12:34:57')
    body={'action':'cancel','sessions':[{'client_id':u1,'expected_job_id':j1},{'client_id':u2,'expected_job_id':None}]}
    result=c.post('/api/clients/bulk',headers=auth(),json=body)
    assert result.status_code==200,result.text
    assert result.json()['succeeded']==1 and result.json()['failed']==1
    assert c.get('/api/jobs/'+j1,headers=auth()).json()['stage']=='cancelled'
    assert c.get('/api/jobs/'+j2,headers=auth()).json()['stage']=='created'
    assert c.post('/api/pxe/heartbeat',headers=h1,json={'stage':'downloading'}).status_code==401
    assert c.post('/api/clients/bulk',headers=auth('deploy'),json=body).status_code==403
    body['sessions']=[body['sessions'][0]]*2
    assert c.post('/api/clients/bulk',headers=auth(),json=body).status_code==422


def test_cancel_failed_asset_clears_attention_and_retains_failed_report(lab):
    c,root,p=lab;headers,uid,job,cap=start(c,p)
    report={'summary':'Installation failed; retained evidence','driver_problems':[]}
    with c.app.state.store.db() as db:
        db.execute("UPDATE jobs SET stage='failed',report=? WHERE id=?",(json.dumps(report),job))
        db.execute("UPDATE clients SET stage='failed',error='Fixture failure' WHERE id=?",(uid,))
    row=c.get('/api/clients/'+uid,headers=auth()).json()['client'];assert row['needs_attention'] and row['can_cancel']
    assert c.post('/api/clients/'+uid+'/cancel',headers=auth(),json=control(job=job)).status_code==200
    row=c.get('/api/clients/'+uid,headers=auth()).json()['client'];assert row['stage']=='cancelled' and not row['needs_attention']
    assert c.get('/api/jobs/'+job,headers=auth()).json()['stage']=='failed'
    assert c.get('/api/jobs/'+job+'/report',headers=auth()).json()==report
