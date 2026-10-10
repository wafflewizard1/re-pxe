from io import BytesIO
from pypdf import PdfReader
from test_client_management import lab,start,auth,control

def test_installation_progress_persists_separately_from_download_and_survives_cancel(lab):
    c,root,p=lab;token,uid,job,cap=start(c,p)
    body={'stage':'applying-image','received_bytes':100,'total_bytes':100,'progress_percent':37}
    assert c.post('/api/pxe/heartbeat',headers=token,json=body).status_code==200
    row=c.get('/api/clients',headers=auth()).json()['clients'][0]
    assert row['install_percent']==37 and row['received_bytes']==row['total_bytes']==100
    assert c.get('/api/dashboard',headers=auth()).json()['clients'][0]['install_percent']==37
    assert c.post('/api/pxe/heartbeat',headers=token,json=body|{'progress_percent':12}).status_code==200
    assert c.get('/api/clients',headers=auth()).json()['clients'][0]['install_percent']==37
    assert c.post('/api/pxe/heartbeat',headers=token,json=body|{'progress_percent':100}).status_code==200
    assert c.post('/api/pxe/heartbeat',headers=token,json={'stage':'boot-configured','received_bytes':100,'total_bytes':100}).status_code==200
    assert c.get('/api/clients',headers=auth()).json()['clients'][0]['install_percent'] is None
    with c.app.state.store.db() as db:assert db.execute('SELECT stage FROM jobs WHERE id=?',(job,)).fetchone()[0]!='complete'
    assert c.post('/api/clients/'+uid+'/cancel',headers=auth(),json=control(job=job)).status_code==200
    report=c.get('/api/job-reports/'+job,headers=auth()).json();assert report['install_percent']==100
    text='\n'.join(p.extract_text() for p in PdfReader(BytesIO(c.get('/api/job-reports/'+job+'/pdf',headers=auth()).content)).pages)
    assert 'Last reported Windows image application: 100%' in text
    assert c.post('/api/pxe/heartbeat',headers=token,json=body).status_code==401

def test_progress_requires_active_job_correct_stage_and_integer_bounds(lab):
    c,root,p=lab;token,uid,job,cap=start(c,p)
    for percent in (-1,101,True,1.5,'20'):
        assert c.post('/api/pxe/heartbeat',headers=token,json={'stage':'applying-image','progress_percent':percent}).status_code==422
    assert c.post('/api/pxe/heartbeat',headers=token,json={'stage':'downloading','progress_percent':40}).status_code==422
    # An older client that has no percentage remains compatible and never
    # substitutes download completion for installation progress.
    assert c.post('/api/pxe/heartbeat',headers=token,json={'stage':'applying-image'}).status_code==200
    assert c.get('/api/clients',headers=auth()).json()['clients'][0]['install_percent'] is None
    with c.app.state.store.db() as db:db.execute("UPDATE jobs SET stage='complete' WHERE id=?",(job,))
    assert c.post('/api/pxe/heartbeat',headers=token,json={'stage':'applying-image','progress_percent':40}).status_code==409
