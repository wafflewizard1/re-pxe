from datetime import datetime, timezone
from io import BytesIO
import json
import zipfile

from pypdf import PdfReader
from test_client_management import lab,auth
from test_job_reports import start_with_specs
from test_multidrive_routines import prepare,erase_one
from repxe.report_settings import ReportSettings


def text(pdf):return '\n'.join(page.extract_text() for page in PdfReader(BytesIO(pdf)).pages)


def test_settings_validation_persistence_and_preview_does_not_save(lab):
    c,root,p=lab;uid,job=start_with_specs(c,p)
    assert c.get('/api/report-settings').status_code==401
    defaults=c.get('/api/report-settings',headers=auth()).json()['settings']
    settings={**defaults,'title':'Workshop device report','organization':'Our workshop','show_cpu':False,'show_network_mac':False,'paper_size':'Letter','layout':'grouped','section_order':['routine','specifications','history','erasure']}
    preview=c.post('/api/job-reports/'+job+'/preview',headers=auth(),json=settings)
    assert preview.status_code==200,preview.text
    assert 'Workshop device report' in text(preview.content) and 'Test CPU' not in text(preview.content)
    reader=PdfReader(BytesIO(preview.content));assert int(reader.pages[0].mediabox.width)==612
    assert c.get('/api/report-settings',headers=auth()).json()['settings']==defaults
    assert c.put('/api/report-settings',headers=auth(),json=settings).status_code==200
    assert c.get('/api/report-settings',headers=auth()).json()['settings']==settings
    assert 'Test CPU' not in text(c.get('/api/job-reports/'+job+'/pdf',headers=auth()).content)
    for bad in ({'accent_color':'red'},{'show_cpu':'false'},{'timezone':'Not/AZone'},{'section_order':['routine']*4},{'title':' '},{'paper_size':'A3'},{'unexpected':True}):
        assert c.put('/api/report-settings',headers=auth(),json={**settings,**bad}).status_code==422


def test_component_search_matches_values_and_multiple_terms(lab):
    c,root,p=lab;uid,job=start_with_specs(c,p)
    with c.app.state.store.db() as db:
        row=db.execute('SELECT inventory FROM client_inventory WHERE client_id=?',(uid,)).fetchone();inventory=json.loads(row['inventory'])
        inventory['graphics']=[{'name':'NVIDIA RTX 4070','manufacturer':'NVIDIA'}]
        inventory['memory']['modules'][0]['part_number']='CT16G4SFD832A'
        db.execute('UPDATE client_inventory SET inventory=? WHERE client_id=?',(json.dumps(inventory),uid))
    for query in ('RTX 4070','ct16g4sfd832a','Test SSD','Test CPU SYS-001','RAM-001','Lab Pro'):
        result=c.get('/api/job-reports',headers=auth(),params={'search':query});assert result.status_code==200,result.text
        assert result.json()['total']==1,query
        assert 'search_inventory' not in result.text
    for query in ('RTX AMD','graphics','%','SYS-001 wrong'):
        assert c.get('/api/job-reports',headers=auth(),params={'search':query}).json()['total']==0


def test_dates_respect_timezone_and_date_field(lab):
    c,root,p=lab;uid,job=start_with_specs(c,p)
    with c.app.state.store.db() as db:
        db.execute("UPDATE jobs SET stage='complete',created='2026-10-01T02:00:00+00:00',updated='2026-10-08T23:30:00+00:00' WHERE id=?",(job,))
    query={'start':'2026-10-09','end':'2026-10-09','date_field':'processed','timezone':'Pacific/Auckland'}
    assert c.get('/api/job-reports',headers=auth(),params=query).json()['total']==1
    assert c.get('/api/job-reports',headers=auth(),params={**query,'timezone':'UTC'}).json()['total']==0
    assert c.get('/api/job-reports',headers=auth(),params={**query,'date_field':'started'}).json()['total']==0
    for values in ({'timezone':'Unknown/Invalid'},{'start':'2026-10-10','end':'2026-10-09'}):
        assert c.get('/api/job-reports',headers=auth(),params=values).status_code==422


def test_matching_export_includes_pages_beyond_visible_and_obeys_limit(lab):
    c,root,p=lab;uid,job=start_with_specs(c,p)
    with c.app.state.store.db() as db:
        image=db.execute('SELECT image_id FROM jobs WHERE id=?',(job,)).fetchone()[0]
        for i in range(55):db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?)',(f'batch-{i:03}','failed',image,'serial:BATCH','Batch machine','2026-10-01T00:00:00+00:00','2026-10-02T23:00:00+00:00',None))
    listing=c.get('/api/job-reports',headers=auth(),params={'search':'Batch machine','page_size':25}).json();assert len(listing['reports'])==25 and listing['total']==55
    response=c.post('/api/job-reports/export',headers=auth(),json={'filters':{'search':'Batch machine'},'include_certificates':False})
    assert response.status_code==200,response.text
    with zipfile.ZipFile(BytesIO(response.content)) as archive:assert len(archive.namelist())==55
    for body in ({'filters':{},'ids':[job]},{'filters':{'search':'no results'}},{'ids':[job,job]},{}):
        assert c.post('/api/job-reports/export',headers=auth(),json=body).status_code==422
    with c.app.state.store.db() as db:
        for i in range(201):db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?)',(f'large-{i:03}','failed',image,'serial:LARGE','Large batch','2026-10-01T00:00:00+00:00','2026-10-02T23:00:00+00:00',None))
    assert c.post('/api/job-reports/export',headers=auth(),json={'filters':{'search':'Large batch'}}).status_code==413


def test_grouped_pages_and_optional_original_certificates(lab):
    c,p,body,intake=prepare(lab)
    erase_one(c,body,intake);erase_one(c,body,intake)
    run=c.get('/api/routines',headers=auth()).json()['runs'][0];report='routine:'+run['id']
    response=c.get('/api/job-reports/'+report+'/pdf',headers=auth());assert response.status_code==200
    pages=[page.extract_text() for page in PdfReader(BytesIO(response.content)).pages]
    specs=next(i for i,t in enumerate(pages) if 'Storage devices' in t)
    routine=next(i for i,t in enumerate(pages) if 'Routine steps' in t)
    erasure=next(i for i,t in enumerate(pages) if 'ShredOS evidence' in t)
    history=next(i for i,t in enumerate(pages) if 'Recorded process timeline' in t)
    assert specs<routine<erasure<history
    uid=run['erase_disks'][0]['id'];original=c.get('/api/workflow-runs/'+uid+'/certificate',headers=auth()).content
    settings=c.get('/api/report-settings',headers=auth()).json()['settings'];settings.update(show_erasure=False,show_timeline=False,show_certificate_hashes=False)
    assert c.put('/api/report-settings',headers=auth(),json=settings).status_code==200
    assert 'ShredOS evidence' not in text(c.get('/api/job-reports/'+report+'/pdf',headers=auth()).content)
    assert c.get('/api/workflow-runs/'+uid+'/certificate',headers=auth()).content==original
    for include,count in ((True,3),(False,1)):
        export=c.post('/api/job-reports/export',headers=auth(),json={'ids':[report],'include_certificates':include});assert export.status_code==200,export.text
        with zipfile.ZipFile(BytesIO(export.content)) as archive:
            assert len(archive.namelist())==count
            if include:assert original in [archive.read(name) for name in archive.namelist() if name.startswith('shredos-')]


def test_large_component_rows_can_split_across_pages(lab):
    c,root,p=lab;uid,job=start_with_specs(c,p)
    from repxe import job_reports
    with c.app.state.store.db() as db:data=job_reports.snapshot(db,job)
    data['specs']['graphics']=[{'name':'Component '+('long descriptive text '*10)+str(i)} for i in range(32)]
    preferences=ReportSettings(font_size=11,show_network_mac=True,show_system_uuid=True,organization='Synthetic layout test')
    pdf=job_reports.render_pdf(data,settings=preferences)
    pages=PdfReader(BytesIO(pdf)).pages
    assert len(pages)>2 and '31' in '\n'.join(p.extract_text() for p in pages)


def test_saved_settings_apply_to_export_and_require_csrf(lab):
    c,root,p=lab;uid,job=start_with_specs(c,p)
    settings=c.get('/api/report-settings',headers=auth()).json()['settings'];settings.update(show_cpu=False,show_graphics=False)
    login=c.post('/api/login',json={'username':'admin','password':'admin'});assert login.status_code==200
    assert c.put('/api/report-settings',json=settings).status_code==403
    assert c.put('/api/report-settings',headers={'X-CSRF-Token':login.json()['csrf_token']},json=settings).status_code==200
    response=c.post('/api/job-reports/export',headers=auth(),json={'ids':[job]});assert response.status_code==200
    with zipfile.ZipFile(BytesIO(response.content)) as archive:assert 'Test CPU' not in text(archive.read(archive.namelist()[0]))
