"""Published handoff settings, scoped immutable customer PDFs and boot appearance."""
from io import BytesIO
import hashlib,json,uuid
import pytest
from pypdf import PdfReader
from repxe.routines import FinishOptions,Step,Menu,menu_script
from test_api import client,native_job,auth
from test_pxe import lab,boot,managed_enroll
from test_routines import make,select,managed_ticket
from test_workflows import install_media

def attach_routine(c,job,options):
    uid=str(uuid.uuid4());spec={'name':'Customer handoff','steps':[{'id':'boot','kind':'boot'},{'id':'windows','kind':'windows','finish_options':options},{'id':'finish','kind':'finish'}]}
    with c.app.state.store.db() as db:
        db.execute('INSERT INTO routine_runs(id,routine_id,version,spec,mac,step,stage,created,updated,job_id,error) VALUES(?,?,1,?,?,1,?,?,?,?,?)',(uid,str(uuid.uuid4()),json.dumps(spec),'52:54:00:12:34:56','windows-running','2026-10-09T01:00:00+00:00','2026-10-09T01:00:00+00:00',job,''))
    return uid

@pytest.mark.parametrize('change',[{'cleanup_client':'true'},{'save_report_desktop':1},{'account_name':'Administrator'},{'account_name':'Bad/Name'},{'account_name':'Customer '},{'account_password':'a\nsecret'},{'locale':'en; command'},{'keyboard':'bad'},{'setup_mode':'skip-oobe'}])
def test_handoff_rejects_invalid_inputs(change):
    with pytest.raises(ValueError):FinishOptions.model_validate(change)

@pytest.mark.parametrize('delay',[-1,301,'5',True])
def test_boot_delay_is_strict_and_bounded(delay):
    with pytest.raises(ValueError):Step.model_validate({'id':'boot','kind':'boot','start_delay_seconds':delay})

def test_customer_pdf_requires_accepted_matching_scoped_report(client,monkeypatch):
    job,cap,state=native_job(client,monkeypatch);options=FinishOptions(save_report_desktop=True,setup_mode='local-account',account_password='never-in-report').model_dump();uid=attach_routine(client,job,options)
    h={'Authorization':'Bearer '+cap['token']};url='/api/client-jobs/'+job+'/customer-report'
    assert client.get(url,headers=h).status_code==409
    assert client.get(url).status_code==401
    bad={**state,'finishOptions':{**options,'save_report_desktop':False}};bad['finishOptions'].pop('account_password')
    assert client.post(cap['path'],headers=h,json=bad).status_code==422
    state['finishOptions']={k:v for k,v in options.items() if k!='account_password'}
    assert client.post(cap['path'],headers=h,json=state).status_code==200
    r=client.get(url,headers=h);assert r.status_code==200,r.text;manifest=r.json()
    entry=manifest['files'][0];pdf=client.get(entry['path'],headers=h);assert pdf.status_code==200
    assert hashlib.sha256(pdf.content).hexdigest()==entry['sha256'] and len(pdf.content)==entry['size']
    text=' '.join(p.extract_text() for p in PdfReader(BytesIO(pdf.content)).pages);assert 'Customer handoff' in text and 'never-in-report' not in text
    with client.app.state.store.db() as db:db.execute('UPDATE routine_runs SET error=? WHERE id=?',('later operator note',uid))
    assert client.get(url,headers=h).json()==manifest and client.get(entry['path'],headers=h).content==pdf.content
    other,other_cap,_=native_job(client,monkeypatch)
    assert client.get(url,headers={'Authorization':'Bearer '+other_cap['token']}).status_code==401
    assert client.get(url+'/other.pdf',headers=h).status_code==404
    detail=client.get('/api/job-reports/routine:'+uid,headers=auth()).json();assert 'never-in-report' not in json.dumps(detail)


def test_customer_pdf_disabled_cannot_be_downloaded(client,monkeypatch):
    job,cap,state=native_job(client,monkeypatch);attach_routine(client,job,FinishOptions().model_dump());state['finishOptions']={k:v for k,v in FinishOptions().model_dump().items() if k!='account_password'};h={'Authorization':'Bearer '+cap['token']}
    assert client.post(cap['path'],headers=h,json=state).status_code==200
    assert client.get('/api/client-jobs/'+job+'/customer-report',headers=h).status_code==403


def test_published_delay_refresh_and_graphics_defaults(lab):
    c,root,p=lab;install_media(root);uid=make(c,p);draft=c.get('/api/routines',headers=auth()).json()['routines'][0];draft['draft']['steps'][0]['start_delay_seconds']=12
    saved=c.put('/api/routines/'+uid,headers=auth(),json={'spec':draft['draft'],'expected_generation':draft['generation']}).json()
    assert c.post('/api/routines/'+uid+'/publish',headers=auth(),json={'expected_generation':saved['generation']}).status_code==200
    script=c.get('/pxe/boot.ipxe',params={'mac':'52:54:00:12:34:56'}).text
    assert '--timeout 12000 --default assigned --retimeout 0' in script
    assert ':refresh\nchain --replace --autofree http://127.0.0.1:8089/pxe/routines/menu.ipxe?mac=52:54:00:12:34:56 ||' in script
    assert 'console --x 800 --y 600' in script and 'cpair' in script and 'PROVISION / ERASE / DELIVER' in script
    response=c.get('/pxe/routines/background.png');assert response.status_code==200 and response.content.startswith(b'\x89PNG')
    from PIL import Image
    assert Image.open(BytesIO(response.content)).size==(800,600)
    with c.app.state.store.db() as db:
        current=db.execute('SELECT spec FROM routine_versions WHERE routine_id=?',(uid,)).fetchone();spec=json.loads(current['spec']);spec['steps'][0]['start_delay_seconds']=0;db.execute('UPDATE routine_versions SET spec=? WHERE routine_id=?',(json.dumps(spec),uid))
    assert '--timeout' not in c.get('/pxe/boot.ipxe',params={'mac':'52:54:00:12:34:56'}).text


def test_handoff_checks_the_enrolled_client_and_is_pinned_to_selection(lab):
    c,root,p=lab;install_media(root);uid=make(c,p);r=c.get('/api/routines',headers=auth()).json()['routines'][0];options=FinishOptions(save_report_desktop=True).model_dump();r['draft']['steps'][1]['finish_options']=options
    saved=c.put('/api/routines/'+uid,headers=auth(),json={'spec':r['draft'],'expected_generation':r['generation']}).json()
    assert c.post('/api/routines/'+uid+'/publish',headers=auth(),json={'expected_generation':saved['generation']}).status_code==200
    select(c);ticket=managed_ticket(c)
    body={'manufacturer':'QEMU','model':'Lab','managed_selection':True,'client_version':'0.12.17','targets':[{'identity':'serial:LAB-001','size_bytes':68719476736}]}
    assert c.post('/api/pxe/enroll',headers={'Authorization':'Bearer '+ticket},json=body).status_code==409
    body['client_version']='0.13.0';session=c.post('/api/pxe/enroll',headers={'Authorization':'Bearer '+ticket},json=body);assert session.status_code==201,session.text
    h={'Authorization':'Bearer '+session.json()['token']};selection=c.post('/api/pxe/select',headers=h,json={'profile_id':p['id'],'target_identity':'serial:LAB-001'});assert selection.status_code==200,selection.text
    assert selection.json()['finishOptions']==options
