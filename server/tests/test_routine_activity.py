"""Routine-level activity and evidence, using synthetic client inventory only."""
from io import BytesIO
import json
import re
import zipfile

from fastapi.testclient import TestClient
from pypdf import PdfReader
from repxe.app import create_app
from test_api import auth, TOKENS
from test_pxe import lab, managed_enroll
from test_routines import intake, make, managed_ticket, select
from test_workflows import install_media, MAC, PDF, result, TARGET


def wipe_only(c, root, checkpoint=False, method='zero', fallback=None):
    install_media(root)
    steps=[{'id':'boot','kind':'boot'},{'id':'wipe','kind':'erase','method':method}]
    if fallback:steps[1]['fallback_method']=fallback
    if checkpoint:steps.append({'id':'review','kind':'checkpoint','note':'Inspect certificate'})
    steps.append({'id':'finish','kind':'finish'})
    response=c.post('/api/routines',headers=auth(),json={'name':'Erase and certify','steps':steps})
    assert response.status_code==201,response.text
    uid=response.json()['id']
    assert c.post('/api/routines/'+uid+'/publish',headers=auth(),json={'expected_generation':1}).status_code==200
    assert c.put('/api/routine-settings',headers=auth(),json={'default_id':uid}).status_code==200
    select(c)
    return c.get('/api/routine-jobs',headers=auth()).json()['runs'][0]


def finish_erase(c, method='zero'):
    inv,_=intake(c)
    run=c.get('/api/routine-jobs',headers=auth()).json()['runs'][0]
    script=c.get('/pxe/managed.ipxe',params={'mac':MAC}).text
    ticket=re.search(r'repxe_ticket=([A-Za-z0-9_-]+)',script)[1]
    claimed=c.post('/api/erase/claim',headers={'Authorization':'Bearer '+ticket},json={'mac':MAC,'agent_version':'re-pxe-shredos-1','inventory':inv})
    assert claimed.status_code==201,claimed.text
    h={'Authorization':'Bearer '+claimed.json()['token']}
    assert c.post('/api/erase/begin',headers=h,json={}).status_code==200
    assert not c.get('/api/routine-jobs',headers=auth()).json()['runs'][0]['can_cancel']
    assert c.put('/api/erase/certificate',headers={**h,'Content-Type':'application/pdf'},content=PDF).status_code==200
    assert c.post('/api/erase/result',headers=h,json=result(run,method=method)).status_code==200
    return run


def pdf_text(content):
    return '\n'.join(page.extract_text() for page in PdfReader(BytesIO(content)).pages)


def test_new_install_uses_routines_and_restart_preserves_explicit_legacy_mode(tmp_path):
    with TestClient(create_app(tmp_path,TOKENS)) as c:
        assert c.get('/api/routines',headers=auth()).json()['settings']['enabled']
        assert 'Select other routine' in c.get('/pxe/boot.ipxe',params={'mac':MAC}).text
        assert c.put('/api/routine-settings',headers=auth(),json={'enabled':False}).status_code==200
    with TestClient(create_app(tmp_path,TOKENS)) as c:
        assert not c.get('/api/routines',headers=auth()).json()['settings']['enabled']


def test_jobs_show_pre_windows_discovery_checkpoint_and_scoped_cancellation(lab):
    c,root,_=lab
    run=wipe_only(c,root,checkpoint=True)
    assert run['stage']=='discovering' and run['report_id']=='routine:'+run['id']
    assert c.get('/api/job-reports',headers=auth()).json()['total']==1
    finish_erase(c)
    jobs=c.get('/api/routine-jobs?view=attention&search=PC-001',headers=auth()).json()
    assert jobs['total']==1 and jobs['runs'][0]['can_continue']
    run=jobs['runs'][0]
    assert run['model']=='VM' and run['system_serial']=='PC-001'
    endpoint='/api/routine-runs/'+run['id']+'/action'
    assert c.post(endpoint,headers=auth(),json={'action':'cancel','expected_step':1}).status_code==409
    assert c.post(endpoint,headers=auth(),json={'action':'cancel','expected_step':run['step']}).status_code==200
    assert c.get('/api/routine-jobs',headers=auth()).json()['total']==0
    assert c.get('/api/routine-jobs?view=cancelled',headers=auth()).json()['total']==1
    assert c.get('/api/workflow-runs/'+run['workflow_id']+'/certificate',headers=auth()).content==PDF
    report=c.get('/api/job-reports/'+run['report_id'],headers=auth()).json()
    assert report['job']['stage']=='cancelled' and report['erasure']['result']=='passed'
    assert report['processed_at'] and report['furthest_label']=='Technician checkpoint'


def test_erasure_heartbeats_are_routine_job_and_report_activity(lab):
    c,root,_=lab;run=wipe_only(c,root);intake(c)
    run=c.get('/api/routine-jobs',headers=auth()).json()['runs'][0]
    with c.app.state.store.db() as db:
        db.execute("UPDATE routine_runs SET updated='2026-10-01T00:00:00+00:00' WHERE id=?",(run['id'],))
        db.execute("UPDATE workflow_runs SET updated='2026-10-08T03:00:00+00:00' WHERE id=?",(run['workflow_id'],))
    assert c.get('/api/routine-jobs',headers=auth()).json()['runs'][0]['last_activity']=='2026-10-08T03:00:00+00:00'
    assert c.get('/api/job-reports/'+run['report_id'],headers=auth()).json()['as_of']=='2026-10-08T03:00:00+00:00'


def test_erase_only_report_contains_actual_fallback_evidence_without_windows_claim(lab):
    c,root,_=lab
    run=wipe_only(c,root,method='zero')
    finish_erase(c)
    report=c.get('/api/job-reports/'+run['report_id'],headers=auth()).json()
    assert report['job']['stage']=='complete' and report['processed_at']
    assert report['erasure']['actual_method']=='zero' and report['erasure']['certificate_sha256']
    assert report['specs']['system']['serial']=='PC-001'
    assert not any(e['stage']=='fallback' for e in report['events'])
    response=c.get('/api/job-reports/'+run['report_id']+'/pdf',headers=auth())
    assert response.status_code==200,response.text
    text=pdf_text(response.content)
    assert 'ShredOS evidence' in text and 'PC-001' in text and 'An erasure result was received.' in text
    assert 'Windows image' not in text and 'Windows image applied' not in text
    assert c.get('/api/dashboard',headers=auth()).json()['routines']['counts']['completed_today']==1


def test_failed_intake_and_cancelled_pending_have_reports_and_literal_search(lab):
    c,root,_=lab
    run=wipe_only(c,root)
    response=c.get('/api/job-reports/'+run['report_id']+'/pdf',headers=auth())
    assert 'Still pending' in pdf_text(response.content)
    intake(c,[{'identity':TARGET,'size_bytes':64*1024**3},{'identity':'serial:OTHER','size_bytes':64*1024**3}])
    jobs=c.get('/api/routine-jobs?view=attention&search=Erase%20and%20certify',headers=auth()).json()
    assert jobs['total']==1 and jobs['runs'][0]['stage']=='failed'
    assert c.get('/api/routine-jobs?view=all&search=%25',headers=auth()).json()['total']==0
    report=c.get('/api/job-reports/'+run['report_id'],headers=auth()).json()
    assert report['job']['stage']=='failed' and report['error'] and not report['has_completion_report']


def test_windows_job_links_one_routine_report_preserving_old_urls_and_mixed_export(lab):
    c,root,p=lab
    install_media(root);make(c,p);select(c)
    headers,enrolled=managed_enroll(c,managed_ticket(c))
    assert c.post('/api/pxe/select',headers=headers,json={'profile_id':p['id'],'target_identity':TARGET}).status_code==200
    assert c.post('/api/pxe/countdown',headers=headers,json={'action':'start'}).status_code==200
    job=c.post('/api/pxe/start',headers=headers,json={}).json()['id']
    run=c.get('/api/routine-jobs',headers=auth()).json()['runs'][0]
    assert run['job_id']==job and run['client_id']==enrolled['clientId']
    with c.app.state.store.db() as db:
        db.execute("INSERT INTO events(job_id,stage,detail,timestamp) VALUES(?,?,?,?)",(job,'image-applied','Applied Windows','2026-10-08T00:00:00+00:00'))
        image=db.execute('SELECT image_id FROM jobs WHERE id=?',(job,)).fetchone()[0]
        db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?)',('earlier-job','failed',image,TARGET,'Earlier laptop','2026-10-01T00:00:00+00:00','2026-10-01T00:01:00+00:00',None))
    history=c.get('/api/job-reports',headers=auth()).json()
    assert history['total']==2 and {r['id'] for r in history['reports']}=={run['report_id'],'earlier-job'}
    report=c.get('/api/job-reports/'+run['report_id'],headers=auth()).json()
    assert report['windows_job_id']==job and 'Windows image applied' in report['furthest_label']
    assert c.get('/api/job-reports/'+job+'/pdf',headers=auth()).status_code==200
    exported=c.post('/api/job-reports/export',headers=auth(),json={'ids':[run['report_id'],'earlier-job']})
    assert exported.status_code==200,exported.text
    with zipfile.ZipFile(BytesIO(exported.content)) as z:
        assert len(z.namelist())==2 and all(z.read(n).startswith(b'%PDF-') for n in z.namelist())
    dashboard=c.get('/api/dashboard',headers=auth()).json()
    assert len(dashboard['routines']['active'])==1 and len(dashboard['routines']['recent'])==2


def test_routine_job_permissions_and_cookie_mutation_csrf(lab):
    c,root,_=lab;run=wipe_only(c,root)
    for role in ('deploy','capture','agent'):
        assert c.get('/api/routine-jobs',headers=auth(role)).status_code==403
    assert c.get('/api/routine-jobs').status_code==401
    c.post('/api/login',json={'username':'admin','password':'admin'})
    assert c.post('/api/routine-runs/'+run['id']+'/action',json={'action':'cancel','expected_step':run['step']}).status_code==403
    assert c.get('/api/routine-jobs?view=unknown',headers=auth()).status_code==422


def test_routine_history_pagination_retains_failed_and_cancelled_runs(lab):
    c,root,_=lab;run=wipe_only(c,root)
    with c.app.state.store.db() as db:
        original=dict(db.execute('SELECT * FROM routine_runs WHERE id=?',(run['id'],)).fetchone())
        for n in range(205):
            record={**original,'id':f'fixture-{n:03}','stage':'failed' if n%2 else 'cancelled'}
            db.execute('INSERT INTO routine_runs('+','.join(record)+') VALUES('+','.join('?' for _ in record)+')',list(record.values()))
    page=c.get('/api/routine-jobs?view=all&page=5&page_size=50',headers=auth()).json()
    assert page['total']==206 and page['pages']==5 and len(page['runs'])==6
    assert c.get('/api/routine-jobs?view=active',headers=auth()).json()['total']==1
    assert c.get('/api/routine-jobs?view=attention',headers=auth()).json()['total']==102
    assert c.get('/api/job-reports?status=cancelled',headers=auth()).json()['total']==103
    assert c.get('/api/dashboard?view=attention&search=fixture-003',headers=auth()).json()['routines']['active_total']==1
    assert c.get('/api/dashboard?view=all',headers=auth()).json()['routines']['active_total']==206
    assert c.get('/api/dashboard?view=pending',headers=auth()).json()['routines']['active_total']==1
