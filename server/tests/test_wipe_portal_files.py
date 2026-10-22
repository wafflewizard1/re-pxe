"""Portal progress, interrupted-run recovery and safe raster file replacement."""
from datetime import datetime,timedelta,timezone
from io import BytesIO
import json
import os
import signal
from unittest.mock import patch
from PIL import Image
from test_api import client,auth
from test_pxe import lab
from test_multidrive_routines import prepare
from repxe import shredos_agent


def start_wipe(lab,method='zero'):
    c,p,body,intake=prepare(lab,method=method)
    ticket=c.post('/api/intake/erase-ticket',headers=intake,json=body).json()['ticket']
    run=c.post('/api/erase/claim',headers={'Authorization':'Bearer '+ticket},json=body).json()
    credential={'Authorization':'Bearer '+run['token']}
    assert c.post('/api/erase/begin',headers=credential,json={}).status_code==200
    job=c.get('/api/routine-jobs',headers=auth()).json()['runs'][0]
    return c,job,run,credential


def test_measured_progress_eta_attention_and_removal_retain_report(lab):
    c,job,run,credential=start_wipe(lab)
    payload={'percent':18,'detail':'Round 1/1 · pass 1/1 · ETA 00:08:56 · [writing]'}
    assert c.post('/api/erase/progress',headers=credential,json=payload).status_code==200
    current=c.get('/api/routine-runs/'+job['id'],headers=auth()).json()
    disk=current['erase_disks'][0]
    assert disk['percent']==18 and disk['remaining_seconds']==536 and not disk['progress_stale']
    standalone=c.get('/api/workflow-runs/'+run['id'],headers=auth()).json()
    assert standalone['progress']['remaining_seconds']==536
    action={'action':'remove','expected_step':job['step'],'stopped_locally':True,'expected_progress_updated':disk['progress_updated']}
    assert c.post('/api/routine-runs/'+job['id']+'/action',headers=auth(),json=action).status_code==409
    old=(datetime.now(timezone.utc)-timedelta(minutes=5)).isoformat()
    with c.app.state.store.db() as db:db.execute('UPDATE workflow_progress SET updated=? WHERE run_id=?',(old,run['id']))
    current=c.get('/api/routine-runs/'+job['id'],headers=auth()).json()
    assert current['can_confirm_stopped'] and current['can_remove'] and current['needs_attention']
    assert c.get('/api/routine-jobs?view=attention',headers=auth()).json()['total']==1
    action['expected_progress_updated']=old
    action['stopped_locally']=False
    assert c.post('/api/routine-runs/'+job['id']+'/action',headers=auth(),json=action).status_code==409
    action['stopped_locally']=True
    action['expected_progress_updated']='different'
    assert c.post('/api/routine-runs/'+job['id']+'/action',headers=auth(),json=action).status_code==409
    action['expected_progress_updated']=old
    assert c.post('/api/routine-runs/'+job['id']+'/action',headers=auth(),json=action).status_code==200
    assert c.get('/api/routine-jobs?view=all',headers=auth()).json()['total']==0
    assert c.get('/api/job-reports',headers=auth()).json()['total']==1
    evidence=c.get('/api/job-reports/routine:'+job['id'],headers=auth()).json()
    assert evidence['routine']['stage']=='cancelled'
    assert 'unverified' in c.get('/api/workflow-runs/'+run['id'],headers=auth()).json()['error']
    assert c.post('/api/erase/progress',headers=credential,json=payload).status_code in (401,409)
    with c.app.state.store.db() as db:
        assert db.execute('SELECT count(*) FROM routine_erase_disks').fetchone()[0]==2
        assert db.execute("SELECT count(*) FROM workflow_runs WHERE stage='cancelled'").fetchone()[0]==1
    assert c.post('/api/routine-runs/'+job['id']+'/action',headers=auth(),json=action).status_code==200


def test_hardware_operation_cannot_be_cleared_based_on_stale_progress(lab):
    c,job,run,credential=start_wipe(lab)
    with c.app.state.store.db() as db:
        spec=json.loads(db.execute('SELECT spec FROM workflow_runs WHERE id=?',(run['id'],)).fetchone()[0]);spec['method']='nvme_format_user'
        db.execute('UPDATE workflow_runs SET spec=? WHERE id=?',(json.dumps(spec),run['id']))
    old=(datetime.now(timezone.utc)-timedelta(minutes=5)).isoformat()
    with c.app.state.store.db() as db:
        db.execute('UPDATE workflow_runs SET updated=? WHERE id=?',(old,run['id']))
    current=c.get('/api/routine-runs/'+job['id'],headers=auth()).json()
    assert not current['can_confirm_stopped'] and not current['can_remove']
    assert c.post('/api/routine-runs/'+job['id']+'/action',headers=auth(),json={'action':'remove','expected_step':job['step'],'stopped_locally':True}).status_code==409


def image(color):
    out=BytesIO();Image.new('RGB',(640,480),color).save(out,format='PNG');return out.getvalue()


def test_real_structure_and_versioned_background_safe_replacement(client,tmp_path):
    c=client;root=c.app.state.store.root
    tree=c.get('/api/files/tree',headers=auth()).json()
    assert {r['name'] for r in tree['entries']}=={p.name for p in root.iterdir() if (p.is_dir() or p.is_file()) and not p.is_symlink() and not p.name.startswith('.')}
    assert not next(r for r in tree['entries'] if r['name']=='database')['locked']
    assert c.get('/api/files/tree?path=database',headers=auth()).status_code==200
    assert c.get('/api/files/tree?path=../database',headers=auth()).status_code==422
    folder=c.get('/api/files/tree?path=boot/custom/scripts',headers=auth()).json()
    menu=next(r for r in folder['entries'] if r['name']=='menu.ipxe')
    assert menu['path']=='boot/custom/scripts/menu.ipxe' and menu['root']=='workspace' and menu['relative_path']=='boot/custom/scripts/menu.ipxe'
    endpoint='/api/files/boot-background';initial=c.get(endpoint,headers=auth()).json();assert not initial['custom']
    assert c.put(endpoint,headers=auth('agent'),content=image('red')).status_code==403
    assert c.put(endpoint,headers=auth(),content=b'bad').status_code==422
    first=c.put(endpoint,headers=auth(),content=image('red')+b'ignored metadata').json();assert first['custom']
    assert not (root/first['path']).read_bytes().endswith(b'ignored metadata')
    assert c.put(endpoint,headers=auth(),content=image('blue')).status_code==409
    second=c.put(endpoint,params={'expected_revision':first['revision']},headers=auth(),content=image('blue')).json()
    assert second['revision']!=first['revision']
    png=c.get('/pxe/routines/background.png');assert png.status_code==200
    output=Image.open(BytesIO(png.content));assert output.size==(800,600) and output.getpixel((0,400))==(0,0,255)
    old=next(r for r in second['history'] if r['revision']==first['revision'])
    restored=c.post(endpoint+'/restore/'+old['id'],params={'expected_revision':second['revision']},headers=auth()).json();assert restored['revision']==first['revision']
    assert c.delete(endpoint,params={'expected_revision':restored['revision']},headers=auth()).json()['custom'] is False
    sentinel=tmp_path/'sentinel.png';sentinel.write_bytes(image('green'))
    (root/'boot/custom/assets/ipxe-background.png').symlink_to(sentinel)
    assert c.put(endpoint,headers=auth(),content=image('blue')).status_code==403
    assert sentinel.read_bytes()==image('green')


def test_ctrl_c_waits_for_nwipe_then_returns_failed_exit():
    class Process:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def wait(self,timeout=None):
            if timeout==5:raise KeyboardInterrupt()
            return -signal.SIGINT
        def send_signal(self,value):assert value==signal.SIGINT
    with patch.object(shredos_agent.subprocess,'Popen',return_value=Process()):
        assert shredos_agent.run_nwipe(['nwipe','/dev/private-fixture'],object(),'private')==130


def test_companion_only_upgrade_preserves_active_run_and_immutable_old_media(lab,monkeypatch):
    import hashlib
    from repxe import capabilities
    c,job,run,credential=start_wipe(lab)
    root=c.app.state.store.root
    old=json.loads((root/'boot/pxe/shredos/current.json').read_text())
    pem=(root/'fixture-ca.crt')
    import subprocess
    subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1','-subj','/CN=fixture','-keyout',str(root/'fixture-key.pem'),'-out',str(pem)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    monkeypatch.setenv('REPXE_ADDRESS','fixture.example')
    config=root/'config/server-public-ca.crt';config.write_text(pem.read_text())
    assets=root/'fixture-assets/assets/shredos';assets.mkdir(parents=True)
    kernel=(root/'boot/pxe/shredos'/old['release']/'shredos').read_bytes()
    monkeypatch.setattr(capabilities,'__file__',str(root/'fixture-assets/capabilities.py'))
    (assets/'manifest.json').write_text(json.dumps({'sha256':'mismatch'}))
    assert capabilities.prepare_builtin(c.app.state.store) is False
    (assets/'manifest.json').write_text(json.dumps({'sha256':hashlib.sha256(kernel).hexdigest()}))
    def prepare(store,ca,origin,identity):
        assert ca.strip()==pem.read_text().strip()
        with store.db() as db:db.execute("UPDATE optional_capabilities SET status='installed' WHERE id='shredos'")
    monkeypatch.setattr(capabilities,'publish_builtin',prepare)
    monkeypatch.setattr(capabilities,'state',lambda store: {'status':'installed'})
    assert capabilities.prepare_builtin(c.app.state.store) is True
    with c.app.state.store.db() as db:
        assert db.execute('SELECT stage FROM workflow_runs WHERE id=?',(run['id'],)).fetchone()[0]=='erasing'
    assert json.loads((root/'boot/pxe/shredos/current.json').read_text())==old
    assert c.get('/api/erase/status',headers=credential).status_code==200


def test_remove_failed_batch_retains_failed_result_and_unstarted_drives(lab):
    from test_multidrive_routines import erase_one
    c,p,body,intake=prepare(lab,failure_halt=True)
    first,credential=erase_one(c,body,intake,failed=True)
    jobs=c.get('/api/routine-jobs?view=all',headers=auth()).json()['runs'];job=jobs[0]
    assert job['stage']=='failed' and job['can_remove']
    assert job['erase_disks'][1]['stage']=='cancelled'
    original_hash=job['erase_disks'][0]['certificate_sha256']
    response=c.post('/api/routine-runs/'+job['id']+'/action',headers=auth(),json={'action':'remove','expected_step':job['step']})
    assert response.status_code==200,response.text
    data=c.get('/api/job-reports/routine:'+job['id'],headers=auth()).json()
    assert data['routine']['stage']=='failed' and data['erasures'][0]['result']['result']=='failed'
    assert data['erasures'][1]['stage']=='cancelled' and data['erasures'][1]['result'] is None
    assert data['erasures'][0]['certificate_sha256']==original_hash
    assert c.get(data['erasures'][0]['certificate_url'],headers=auth()).status_code==200


def test_uploaded_background_has_no_art_shading_or_reserved_heading_strip(client):
    from PIL import ImageOps
    c=client
    picture=Image.new('RGB',(100,70),'#26cabb')
    for x in range(100):
        for y in range(70):picture.putpixel((x,y),(x*2,y*3,100))
    stream=BytesIO();picture.save(stream,format='PNG')
    uploaded=c.put('/api/files/boot-background',headers=auth(),content=stream.getvalue());assert uploaded.status_code==200
    settings=c.get('/api/routine-menu',headers=auth()).json()
    for size,dimensions in [('large',(800,600)),('standard',(1024,768)),('compact',(1280,800))]:
        spec={**settings['spec'],'text_size':size,'show_banner':True}
        response=c.put('/api/routine-menu',headers=auth(),json={'spec':spec,'expected_revision':settings['revision']});assert response.status_code==200
        settings=c.get('/api/routine-menu',headers=auth()).json()
        output=Image.open(BytesIO(c.get('/pxe/routines/background.png').content)).convert('RGB')
        expected=ImageOps.fit(picture,dimensions,method=Image.Resampling.LANCZOS)
        assert output.tobytes()==expected.tobytes()
        assert '--top 24' in settings['preview']
        assert 'PROVISION / ERASE / DELIVER' not in settings['preview']
    assert c.delete('/api/files/boot-background',headers=auth(),params={'expected_revision':uploaded.json()['revision']}).status_code==200
    assert '--top 120' in c.get('/api/routine-menu',headers=auth()).json()['preview']
