"""Explicit drive-set authorization and technician choices; no host storage."""
import json
import re
import pytest
from repxe import shredos_agent
from test_api import auth
from test_pxe import lab
from test_routines import select, managed_ticket
from test_workflows import install_media, PDF, result, MAC, SIZE, TARGET


def prepare(lab, mode='manual', method='zero', disks=None, verify_image=True):
    c,root,p=lab
    install_media(root)
    path=root/'boot/pxe/shredos/current.json';media=json.loads(path.read_text());media['agent_version']='re-pxe-shredos-4';path.write_text(json.dumps(media))
    path=root/'boot/pxe/current.json';media=json.loads(path.read_text());media['version']='0.12.11' if verify_image else '0.12.16';path.write_text(json.dumps(media))
    spec={'name':'All drives','steps':[{'id':'boot','kind':'boot'},
        {'id':'erase','kind':'erase','method':method,'target_policy':'all-internal'},
        {'id':'windows','kind':'windows','profile_id':p['id'],'target_policy':mode,'verify_image':verify_image}, {'id':'finish','kind':'finish'}]}
    r=c.post('/api/routines',headers=auth(),json=spec);assert r.status_code==201,r.text
    uid=r.json()['id'];r=c.post('/api/routines/'+uid+'/publish',headers=auth(),json={'expected_generation':1});assert r.status_code==200,r.text
    c.put('/api/routine-settings',headers=auth(),json={'default_id':uid});select(c)
    script=c.get('/pxe/managed.ipxe',params={'mac':MAC}).text
    ticket=re.search(r'repxe_ticket=([A-Za-z0-9_-]+)',script)[1]
    inventory={'system_serial':'PC-001','manufacturer':'Example','model':'VM','disks':disks or [
        {'identity':TARGET,'size_bytes':SIZE,'model':'First SSD','transport':'sata'},
        {'identity':'serial:SECOND','size_bytes':SIZE*2,'model':'Second SSD','transport':'sata'},
        {'identity':'serial:USB','size_bytes':SIZE,'transport':'usb'}]}
    body={'mac':MAC,'agent_version':'re-pxe-shredos-4','inventory':inventory}
    r=c.post('/api/intake/claim',headers={'Authorization':'Bearer '+ticket},json=body);assert r.status_code==201,r.text
    return c,p,body,{'Authorization':'Bearer '+r.json()['token']}


def erase_one(c, body, intake, failed=False):
    r=c.post('/api/intake/erase-ticket',headers=intake,json=body);assert r.status_code==200,r.text
    assert len(r.json()['targets'])==2
    ticket=r.json()['ticket']
    assert c.post('/api/intake/erase-ticket',headers=intake,json=body).status_code==409
    r=c.post('/api/erase/claim',headers={'Authorization':'Bearer '+ticket},json=body);assert r.status_code==201,r.text
    run=r.json();h={'Authorization':'Bearer '+run['token']}
    assert c.post('/api/erase/begin',headers=h,json={}).status_code==200
    assert c.post('/api/erase/begin',headers=h,json={}).status_code==409
    assert c.post('/api/erase/progress',headers=h,json={'percent':42,'detail':'Verifying · pass 2/2'}).status_code==200
    r=c.get('/api/routines',headers=auth()).json()['runs'][0]
    assert next(d for d in r['erase_disks'] if d['id']==run['id'])['percent']==42
    assert c.put('/api/erase/certificate',headers={**h,'Content-Type':'application/pdf'},content=PDF).status_code==200
    payload=result(run,target_identity=run['target_identity'],size_bytes=run['size_bytes'])
    if failed: payload.update(result='failed',exit_code=1,errors=1,evidence={'error':'Fixture verification failed'})
    r=c.post('/api/erase/result',headers=h,json=payload);assert r.status_code==200,r.text
    return run,h


def enroll_request(body):
    return {'manufacturer':'Example','model':'VM','client_version':'0.12.11','managed_selection':True,
        'inventory':{'system':{'serial':body['inventory']['system_serial']}},
        'targets':[ {k:d[k] for k in ('identity','size_bytes','model')} for d in body['inventory']['disks'] if d['transport']!='usb']}


def test_batch_same_session_progress_and_manual_portal_choice(lab):
    c,p,body,intake=prepare(lab)
    state=c.get('/api/intake/status',headers=intake).json();assert state['erase_ready']
    first,h=erase_one(c,body,intake)
    state=c.get('/api/erase/status',headers=h).json();assert not state['reboot_for_routine']
    assert c.get('/api/routines',headers=auth()).json()['runs'][0]['stage']=='erase-running'
    # A different PC sharing the USB NIC and a missing drive are refused.
    changed=json.loads(json.dumps(body));changed['inventory']['system_serial']='OTHER-PC'
    assert c.post('/api/intake/erase-ticket',headers=intake,json=changed).status_code==409
    changed=json.loads(json.dumps(body));changed['inventory']['disks'].pop(1)
    assert c.post('/api/intake/erase-ticket',headers=intake,json=changed).status_code==409
    changed=json.loads(json.dumps(body));changed['inventory']['disks'].append({'identity':'serial:NEW-INTERNAL','size_bytes':SIZE,'transport':'sata'})
    assert c.post('/api/intake/erase-ticket',headers=intake,json=changed).status_code==409
    second,h2=erase_one(c,body,intake);assert second['target_identity']=='serial:SECOND'
    assert c.get('/api/erase/status',headers=h2).json()['reboot_for_routine']
    ticket=managed_ticket(c);r=c.post('/api/pxe/enroll',headers={'Authorization':'Bearer '+ticket},json=enroll_request(body));assert r.status_code==201,r.text
    session=r.json();uid=session['clientId'];native={'Authorization':'Bearer '+session['token']}
    assert session['automation']['targetIdentity']=='' and not session['automation']['automaticDiskSelection']
    assert c.get('/api/clients/'+uid,headers=auth()).json()['client']['needs_attention']
    assert c.get('/api/routine-jobs?view=attention',headers=auth()).json()['total']==1
    assert c.post('/api/pxe/start',headers=native,json={}).status_code==409
    r=c.post('/api/clients/'+uid+'/disk',headers=auth(),json={'profile_id':p['id'],'target_identity':'serial:SECOND'});assert r.status_code==200,r.text
    pulse=c.post('/api/pxe/heartbeat',headers=native,json={'stage':'waiting-for-input'}).json()
    assert pulse['automation']['targetIdentity']=='serial:SECOND'
    assert pulse['automation']['automaticDiskSelection']
    assert c.post('/api/pxe/select',headers=native,json={'profile_id':p['id'],'target_identity':TARGET}).status_code==409
    r=c.post('/api/pxe/select',headers=native,json={'profile_id':p['id'],'target_identity':'serial:SECOND'});assert r.status_code==200,r.text
    assert c.post('/api/pxe/start',headers=native,json={}).status_code==409 # countdown still required
    assert c.post('/api/clients/'+uid+'/disk',headers=auth(),json={'profile_id':p['id'],'target_identity':TARGET}).status_code==409
    report_id='routine:'+c.get('/api/routines',headers=auth()).json()['runs'][0]['id']
    data=c.get('/api/job-reports/'+report_id,headers=auth()).json();assert len(data['erasures'])==2
    assert all(d['certificate_url'] for d in data['erasures'])
    import io,zipfile
    archive=c.post('/api/job-reports/export',headers=auth(),json={'ids':[report_id]});assert archive.status_code==200,archive.text
    assert len([n for n in zipfile.ZipFile(io.BytesIO(archive.content)).namelist() if n.startswith('shredos-')])==2


def test_failed_first_drive_never_authorizes_second_or_windows(lab):
    c,p,body,intake=prepare(lab);run,h=erase_one(c,body,intake,True)
    row=c.get('/api/routines',headers=auth()).json()['runs'][0]
    assert row['stage']=='failed' and row['erase_disks'][1]['stage']=='cancelled'
    assert not c.get('/api/erase/status',headers=h).json()['reboot_for_routine']
    assert c.post('/api/intake/erase-ticket',headers=intake,json=body).status_code==409
    assert 'wimboot' not in c.get('/pxe/managed.ipxe',params={'mac':MAC}).text


def test_every_internal_drive_uses_nwipe_independent_of_firmware(lab):
    c,p,body,intake=prepare(lab,method='zero',disks=[
        {'identity':TARGET,'size_bytes':SIZE,'transport':'nvme','capabilities':{'methods':[]}},
        {'identity':'serial:SECOND','size_bytes':SIZE,'transport':'sata','capabilities':{'methods':[]}}])
    row=c.get('/api/routines',headers=auth()).json()['runs'][0]
    assert row['stage']=='erase-running' and len(row['erase_disks'])==2
    with c.app.state.store.db() as db:
        assert {json.loads(r['spec'])['method'] for r in db.execute('SELECT spec FROM workflow_runs')}=={'zero'}


def test_first_detected_disk_and_native_manual_choice(lab):
    c,p,body,intake=prepare(lab,mode='first-disk');erase_one(c,body,intake);erase_one(c,body,intake)
    ticket=managed_ticket(c);req=enroll_request(body);req['targets'].reverse()
    r=c.post('/api/pxe/enroll',headers={'Authorization':'Bearer '+ticket},json=req);assert r.status_code==201,r.text
    assert r.json()['automation']['targetIdentity']=='serial:SECOND'


def test_nwipe_progress_preserves_cumulative_percentage_and_uses_last_sample():
    text='/dev/sda: 25.00%, round 1 of 2, pass 1 of 2, eta 00:01:20, [writing]\n/dev/sda: 75.50%, round 2 of 2, pass 2 of 2, eta 00:00:20, [verifying]\n'
    percent,detail=shredos_agent.nwipe_progress(text,'/dev/sda')
    assert percent==75 and 'verifying' in detail and '2/2' in detail
    assert shredos_agent.nwipe_progress(text,'/dev/sdb')[0] is None


def test_smart_health_is_visible_before_windows_and_retained_in_report(lab):
    health={'status':'available','source':'smartctl -a -j','passed':False,'temperature_c':41,'power_on_hours':17525,'reallocated_sectors':12,'pending_sectors':2,'uncorrectable_sectors':1}
    c,p,body,intake=prepare(lab,disks=[{'identity':TARGET,'size_bytes':SIZE,'model':'First SSD','transport':'sata','health':health},{'identity':'serial:SECOND','size_bytes':SIZE*2,'model':'Second SSD','transport':'sata'}])
    row=c.get('/api/routines',headers=auth()).json()['runs'][0]
    uid=row['id'];detail=c.get('/api/routine-runs/'+uid,headers=auth()).json()
    assert detail['device']['specs']['storage'][0]['health']['passed'] is False
    report=c.get('/api/job-reports/routine:'+uid,headers=auth()).json()
    assert report['specs']['storage'][0]['health']['reallocated_sectors']==12
    from io import BytesIO
    from pypdf import PdfReader
    pdf=c.get('/api/job-reports/routine:'+uid+'/pdf',headers=auth())
    assert pdf.status_code==200
    text='\n'.join(page.extract_text() for page in PdfReader(BytesIO(pdf.content)).pages)
    assert 'SMART health' in text and 'FAILED' in text and 'Reallocated sectors' in text and '17525' in text


def test_manual_disk_attention_can_cancel_from_asset_and_revoke_client(lab):
    c,p,body,intake=prepare(lab);erase_one(c,body,intake);erase_one(c,body,intake)
    r=c.post('/api/pxe/enroll',headers={'Authorization':'Bearer '+managed_ticket(c)},json=enroll_request(body));assert r.status_code==201,r.text
    session=r.json();uid=session['clientId'];row=c.get('/api/clients/'+uid,headers=auth()).json()['client']
    assert row['can_cancel'] and row['disk_selection_ready']
    routine=row['routine'];assert routine['can_cancel']
    response=c.post('/api/clients/'+uid+'/cancel',headers=auth(),json={'expected_job_id':None});assert response.status_code==200,response.text
    assert c.get('/api/routine-runs/'+routine['id'],headers=auth()).json()['stage']=='cancelled'
    assert c.get('/api/clients/'+uid,headers=auth()).json()['client']['stage']=='cancelled'
    assert c.post('/api/pxe/heartbeat',headers={'Authorization':'Bearer '+session['token']},json={'stage':'waiting-for-input'}).status_code==401
    report=c.get('/api/job-reports/routine:'+routine['id'],headers=auth()).json()
    assert len(report['erasures'])==2 and all(d['certificate_url'] for d in report['erasures'])


@pytest.mark.parametrize('erase_stage',['erasing','operation-pending'])
def test_failed_parent_never_allows_cancelling_unresolved_wipe(lab,erase_stage):
    c,p,body,intake=prepare(lab)
    ticket=c.post('/api/intake/erase-ticket',headers=intake,json=body).json()['ticket']
    claimed=c.post('/api/erase/claim',headers={'Authorization':'Bearer '+ticket},json=body).json()
    h={'Authorization':'Bearer '+claimed['token']};assert c.post('/api/erase/begin',headers=h,json={}).status_code==200
    row=c.get('/api/routines',headers=auth()).json()['runs'][0]
    with c.app.state.store.db() as db:
        db.execute("UPDATE routine_runs SET stage='failed' WHERE id=?",(row['id'],))
        db.execute('UPDATE workflow_runs SET stage=? WHERE id=?',(erase_stage,claimed['id']))
    row=c.get('/api/routine-runs/'+row['id'],headers=auth()).json();assert not row['can_cancel']
    assert c.post('/api/routine-runs/'+row['id']+'/action',headers=auth(),json={'action':'cancel','expected_step':row['step']}).status_code==409
    with c.app.state.store.db() as db:
        assert db.execute('SELECT stage FROM workflow_runs WHERE id=?',(claimed['id'],)).fetchone()[0]==erase_stage


def test_image_verification_default_and_explicit_skip_are_pinned_to_routine(lab):
    c,p,body,intake=prepare(lab,verify_image=False);erase_one(c,body,intake);erase_one(c,body,intake)
    ticket=managed_ticket(c);req=enroll_request(body)
    old=c.post('/api/pxe/enroll',headers={'Authorization':'Bearer '+ticket},json=req)
    assert old.status_code==409 and '0.12.16' in old.json()['detail']
    req['client_version']='0.12.16'
    r=c.post('/api/pxe/enroll',headers={'Authorization':'Bearer '+ticket},json=req);assert r.status_code==201,r.text
    session=r.json();assert session['automation']['verifyImage'] is False
    h={'Authorization':'Bearer '+session['token']}
    selected=c.post('/api/pxe/select',headers=h,json={'profile_id':p['id'],'target_identity':TARGET});assert selected.status_code==200,selected.text
    assert selected.json()['verifyImage'] is False
    from repxe.routines import Step
    assert Step(id='windows',kind='windows').verify_image is True
    with pytest.raises(ValueError):Step(id='windows',kind='windows',verify_image='false')


def test_reports_find_saved_shred_pdf_on_earlier_drive_while_current_drive_is_pending(lab):
    c,p,body,intake=prepare(lab)
    listing=c.get('/api/job-reports',headers=auth()).json()['reports']
    assert len(listing)==1 and listing[0]['has_shred_report'] is False
    first,h=erase_one(c,body,intake)
    listing=c.get('/api/job-reports',headers=auth()).json()['reports']
    # The current workflow is the second disk, with no PDF yet.
    assert listing[0]['certificate_id'] is None
    assert listing[0]['has_shred_report'] is True
    original=c.get('/api/workflow-runs/'+first['id']+'/certificate?download=false',headers=auth())
    assert original.content==PDF and original.headers['content-type']=='application/pdf'
    assert 'inline' in original.headers['content-disposition']
    assert "frame-ancestors 'self'" in original.headers['content-security-policy']
    assert c.get('/api/workflow-runs/'+first['id']+'/certificate',headers=auth()).content==original.content
    assert c.get('/api/workflow-runs/'+first['id']+'/certificate').status_code==401
