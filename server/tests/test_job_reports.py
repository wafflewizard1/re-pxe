from io import BytesIO
import json
import re
import zipfile

from pypdf import PdfReader
from test_client_management import lab, start, auth, control
from test_pxe import boot

SPECS={'system':{'manufacturer':'Example','model':'Laptop & portable','serial':'SYS-001','asset_tag':'ASSET-007','uuid':'test-uuid'},
       'processors':[{'name':'Test CPU','cores':8,'threads':16}],
       'memory':{'total_bytes':16*1024**3,'modules':[{'location':'DIMM0','size_bytes':16*1024**3,'serial':'RAM-001','type':'DDR4','speed_mts':3200}]},
       'firmware':{'type':'uefi','version':'F.22','secure_boot':False},
       'storage':[{'identity':'serial:LAB-001','serial':'LAB-001','model':'Test SSD','size_bytes':128*1024**3,'bus_type':'NVMe'}]}


def start_with_specs(c,p):
    with c.app.state.store.db() as db: db.execute("UPDATE pxe_settings SET spec=json_set(spec,'$.start_delay_seconds',0)")
    ticket=boot(c,'52:54:00:00:12:34')
    result=c.post('/api/pxe/enroll',headers={'Authorization':'Bearer '+ticket},json={
        'manufacturer':'Example','model':'Laptop & portable','client_version':'0.12.6',
        'managed_selection':False,'targets':[{'identity':'serial:LAB-001','size_bytes':128*1024**3}], 'inventory':SPECS})
    assert result.status_code==201,result.text
    data=result.json();headers={'Authorization':'Bearer '+data['token']}
    assert c.post('/api/pxe/select',headers=headers,json={'profile_id':p['id'],'target_identity':'serial:LAB-001'}).status_code==200
    job=c.post('/api/pxe/start',headers=headers,json={}).json()['id']
    return data['clientId'],job


def test_inventory_roundtrip_and_old_session_fallback(lab):
    c,root,p=lab;uid,job=start_with_specs(c,p)
    detail=c.get('/api/clients/'+uid,headers=auth()).json()
    assert detail['device']['inventory_available']
    assert detail['device']['specs']['system']['serial']=='SYS-001'
    assert detail['device']['specs']['memory']['modules'][0]['serial']=='RAM-001'
    assert c.get('/api/clients?search=SYS-001',headers=auth()).json()['client_total']==1
    report=c.get('/api/job-reports/'+job,headers=auth()).json()
    assert report['specs']['system']['serial']=='SYS-001' and report['processed_at'] is None
    _,old_uid,old_job,_=start(c,p,mac='52:54:00:00:12:35')
    old=c.get('/api/job-reports/'+old_job,headers=auth()).json()
    assert not old['inventory_available'] and not old['specs']['system']['serial']
    assert old['specs']['storage'][0]['serial']=='LAB-001'


def test_pending_failed_and_cancelled_pdf_evidence_is_read_only(lab):
    c,root,p=lab;uid,job=start_with_specs(c,p)
    with c.app.state.store.db() as db:
        db.execute("UPDATE jobs SET stage='image-applied' WHERE id=?",(job,))
        db.execute("INSERT INTO events(job_id,stage,detail,timestamp) VALUES(?,?,?,?)",(job,'image-applied','Image verified and applied','2026-10-07T10:00:00+00:00'))
        before=[tuple(r) for r in db.execute('SELECT * FROM jobs')]
    result=c.get('/api/job-reports/'+job+'/pdf',headers=auth())
    assert result.status_code==200 and result.headers['content-type']=='application/pdf'
    assert 'inline' in result.headers['content-disposition'] and "frame-ancestors 'self'" in result.headers['content-security-policy']
    text='\n'.join(p.extract_text() for p in PdfReader(BytesIO(result.content)).pages)
    assert 'SYS-001' in text and '16.0 GiB' in text and 'Test CPU' in text and 'Windows image applied' in text
    assert 'No completion report' in text and 'Still pending' in text
    with c.app.state.store.db() as db:assert before==[tuple(r) for r in db.execute('SELECT * FROM jobs')]
    assert c.post('/api/clients/'+uid+'/cancel',headers=auth(),json=control(job=job)).status_code==200
    report=c.get('/api/job-reports/'+job,headers=auth()).json()
    assert report['processed_at'] and report['job']['stage']=='cancelled' and report['furthest_stage']=='image-applied'
    assert c.request('DELETE','/api/clients/'+uid,headers=auth(),json=control(job=job)).status_code==200
    assert c.get('/api/job-reports/'+job+'/pdf?download=true',headers=auth()).status_code==200
    assert c.get('/api/job-reports?status=cancelled',headers=auth()).json()['total']==1


def test_report_filters_paging_date_and_escaping(lab):
    c,root,p=lab;uid,job=start_with_specs(c,p)
    with c.app.state.store.db() as db:
        image=db.execute('SELECT image_id FROM jobs WHERE id=?',(job,)).fetchone()[0]
        for i in range(205):db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?)',(f'legacy-{i:03}','failed',image,'serial:OTHER','Other laptop','2026-10-01T00:00:00+00:00','2026-10-02T23:00:00+00:00',None))
    assert c.get('/api/job-reports?search=SYS-001',headers=auth()).json()['total']==1
    assert c.get('/api/job-reports?search=%25',headers=auth()).json()['total']==0
    page=c.get('/api/job-reports?status=failed&start=2026-10-02&end=2026-10-02&page=5&page_size=50',headers=auth()).json()
    assert page['total']==205 and len(page['reports'])==5 and page['pages']==5
    assert c.get('/api/job-reports?start=2026-10-03&end=2026-10-02',headers=auth()).status_code==422
    assert c.get('/api/job-reports?sort=bad',headers=auth()).status_code==422
    assert c.get('/api/job-reports/missing/pdf',headers=auth()).status_code==404


def test_bulk_export_auth_validation_and_pdf_members(lab):
    c,root,p=lab;uid,job=start_with_specs(c,p)
    for role in ('capture','deploy','agent'):
        assert c.get('/api/job-reports',headers=auth(role)).status_code==403
        assert c.get('/api/job-reports/'+job+'/pdf',headers=auth(role)).status_code==403
    assert c.get('/api/job-reports/'+job+'/pdf').status_code==401
    assert c.post('/api/job-reports/export',headers=auth(),json={'ids':[job,job]}).status_code==422
    assert c.post('/api/job-reports/export',headers=auth(),json={'ids':['missing']}).status_code==404
    result=c.post('/api/job-reports/export',headers=auth(),json={'ids':[job]})
    assert result.status_code==200
    with zipfile.ZipFile(BytesIO(result.content)) as z:
        assert z.namelist()==['job-'+job+'.pdf'] and z.read(z.namelist()[0]).startswith(b'%PDF-')


def test_inventory_rejects_unbounded_or_credential_fields(lab):
    c,root,p=lab
    for inventory in ({'token':'private'}, {'system':{'serial':'s'*257}}, {'memory':{'total_bytes':-1}}, {'graphics':[{'name':'GPU'}]*33}):
        assert c.post('/api/pxe/enroll',headers={'Authorization':'Bearer '+boot(c)},json={'targets':[{'identity':'serial:LAB-001','size_bytes':100}], 'inventory':inventory}).status_code==422


def test_pending_completion_pulse_updates_report_progress_without_claiming_completion(lab):
    c,root,p=lab;headers,uid,job,cap=start(c,p)
    assert c.post('/api/client-jobs/'+job+'/heartbeat',headers=cap,json={'stage':'needs-operator','error':'Driver restart required'}).status_code==200
    report=c.get('/api/job-reports/'+job,headers=auth()).json()
    assert report['status_label']=='Needs attention' and not report['processed_at']
    assert report['error']=='Driver restart required' and not report['has_completion_report']
    assert c.get('/api/job-reports?status=needs-operator',headers=auth()).json()['total']==1
