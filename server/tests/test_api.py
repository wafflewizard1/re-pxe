import hashlib
import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient
from repxe.app import create_app

TOKENS = {r: r + '-' + 'x' * 40 for r in ('admin', 'capture', 'agent', 'deploy')}


def auth(role='admin'):
    return {'Authorization': 'Bearer ' + TOKENS[role]}


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path, TOKENS)) as c:
        yield c


def driver_zip(entries=None):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        for name, data in (entries or {'nic/driver.inf': '[Version]\nCatalogFile=driver.cat\n',
                                      'nic/driver.cat': 'test fixture, not a signed driver',
                                      'nic/driver.sys': 'fixture'}).items():
            archive.writestr(name, data)
    return stream.getvalue()


def start(c, payload, kind='drivers', role='admin', sha=None):
    spec = {'kind': kind, 'name': 'Test artifact', 'filename': 'test.zip' if kind == 'drivers' else 'test.wim',
            'size': len(payload), 'sha256': sha or hashlib.sha256(payload).hexdigest(),
            'make': 'Example', 'model': 'VM', 'windows_version': 'Windows 11 AMD64',
            'reference_preparation': 'Disposable generalized test reference'}
    result = c.post('/api/uploads', json=spec, headers=auth(role))
    assert result.status_code == 201, result.text
    return result.json()['id']


def send(c, uid, payload, offset=0, role='admin'):
    return c.patch(f'/api/uploads/{uid}/content', content=payload,
                   headers={**auth(role), 'Upload-Offset': str(offset)})


def ready(c, payload=None, kind='drivers'):
    payload = payload or driver_zip()
    uid = start(c, payload, kind)
    assert send(c, uid, payload).status_code == 200
    result = c.post(f'/api/uploads/{uid}/finish', headers=auth())
    assert result.status_code == 200, result.text
    return uid


def publish(c, uid, hardware=True):
    return c.post(f'/api/uploads/{uid}/publish', json={
        'validation_note': 'Disposable fixture validation only', 'hardware_test_passed': hardware}, headers=auth())


def test_auth_and_roles(client):
    assert client.get('/health').status_code == 200
    assert client.get('/api/images').status_code == 401
    assert client.get('/api/overview', headers=auth('capture')).status_code == 403
    payload = driver_zip()
    uid = start(client, payload, role='agent')
    assert send(client, uid, payload, role='capture').status_code == 403
    assert client.post(f'/api/uploads/{uid}/publish', json={'validation_note': 'Reviewed in lab'}, headers=auth('agent')).status_code == 403


def test_resume_offsets_and_incomplete(client):
    payload = driver_zip()
    uid = start(client, payload)
    assert send(client, uid, payload[:30]).json()['received'] == 30
    assert send(client, uid, payload[30:]).status_code == 409
    assert client.post(f'/api/uploads/{uid}/finish', headers=auth()).status_code == 409
    assert client.get('/api/drivers', headers=auth()).json() == []
    assert send(client, uid, payload[30:], 30).status_code == 200
    assert client.post(f'/api/uploads/{uid}/finish', headers=auth()).status_code == 200


def test_reject_hash_mismatch(client):
    payload = driver_zip()
    uid = start(client, payload, sha='0'*64)
    assert send(client, uid, payload).status_code == 200
    assert client.post(f'/api/uploads/{uid}/finish', headers=auth()).status_code == 422
    assert publish(client, uid).status_code == 409
    assert client.get('/api/drivers', headers=auth()).json() == []


@pytest.mark.parametrize('entries', [
    {'../escape.inf': 'bad'}, {'/absolute.inf': 'bad'}, {'nic\\escape.inf': 'bad'},
    {'nic/driver.inf': '[Version]\nCatalogFile=missing.cat'},
    {'nic/driver.inf': '[Version]'}, {'nul.inf': 'bad'},
    {'nic/A.inf': 'a', 'nic/a.INF': 'b'}, {'a.zip': 'no drivers'},
])
def test_archive_validation(client, entries):
    payload = driver_zip(entries)
    uid = start(client, payload)
    assert send(client, uid, payload).status_code == 200
    assert client.post(f'/api/uploads/{uid}/finish', headers=auth()).status_code == 422
    assert publish(client, uid).status_code == 409


def test_driver_publication_immutable(client, tmp_path):
    uid = ready(client)
    assert publish(client, uid, hardware=False).status_code == 200
    assert publish(client, uid).status_code == 200
    assert publish(client, uid).status_code == 200
    rows = client.get('/api/drivers?make=example&model=vm', headers=auth('deploy')).json()
    assert len(rows) == 1 and rows[0]['inf_count'] == 1
    assert client.get('/api/drivers?model=other', headers=auth()).json() == []
    assert client.delete(f'/api/uploads/{uid}', headers=auth()).status_code == 409
    assert (tmp_path / 'drivers/packages' / uid / 'files/nic/driver.inf').exists()


def test_persistence_and_offset_reconciliation(tmp_path):
    payload = driver_zip()
    with TestClient(create_app(tmp_path, TOKENS)) as c:
        uid = start(c, payload)
        send(c, uid, payload[:40])
    # Simulate interruption between durable file append and the SQLite offset update.
    with (tmp_path / 'uploads/staging' / f'{uid}.part').open('ab') as stream:
        stream.write(payload[40:60])
    with TestClient(create_app(tmp_path, TOKENS)) as c:
        assert c.get(f'/api/uploads/{uid}', headers=auth()).json()['received'] == 60
        assert send(c, uid, payload[60:], 60).status_code == 200
        assert c.post(f'/api/uploads/{uid}/finish', headers=auth()).status_code == 200
        assert publish(c, uid).status_code == 200
    with TestClient(create_app(tmp_path, TOKENS)) as c:
        assert len(c.get('/api/drivers', headers=auth()).json()) == 1


def test_publication_crash_reconciliation(client, tmp_path, monkeypatch):
    payload=driver_zip();uid=start(client,payload);assert send(client,uid,payload).status_code==200
    # Already renamed publication directory with no catalog commit.
    import repxe.app as module
    rename = module.os.rename

    def crash(src, dst):
        rename(src, dst)
        raise OSError('simulated power loss')

    monkeypatch.setattr(module.os, 'rename', crash)
    with pytest.raises(OSError):
        client.post(f'/api/uploads/{uid}/finish',headers=auth())
    monkeypatch.setattr(module.os, 'rename', rename)
    assert client.delete(f'/api/uploads/{uid}', headers=auth()).status_code == 409
    assert publish(client, uid).status_code == 200
    assert len(client.get('/api/drivers', headers=auth()).json()) == 1


def test_job_stage_and_report_contract(client, monkeypatch):
    # State-machine test isolates WIM validation; the remote smoke test uses real wimlib.
    monkeypatch.setattr('repxe.app.verify_wim', lambda p, s: {'selected_image': {'build': 'test'}})
    uid = ready(client, b'wim fixture', kind='image')
    assert publish(client, uid).status_code == 200
    result = client.post('/api/jobs', json={'image_id': uid, 'target_identity': 'VM-DISK-001'}, headers=auth('deploy'))
    assert result.status_code == 201, result.text
    job = result.json()['id']
    endpoint = f'/api/jobs/{job}/events'
    assert client.post(endpoint, json={'stage': 'image-applied'}, headers=auth('deploy')).status_code == 409
    for stage in ['preflight','disk-prepared','image-applied','boot-configured','drivers-staged','agent-staged','awaiting-windows']:
        assert client.post(endpoint, json={'stage': stage}, headers=auth('deploy')).status_code == 200
    assert client.post(endpoint, json={'stage': 'completing'}, headers=auth('deploy')).status_code == 403
    assert client.post(endpoint, json={'stage': 'needs-operator'}, headers=auth('agent')).status_code == 200
    assert client.post(endpoint, json={'stage': 'completing'}, headers=auth('agent')).status_code == 200
    report = {'result':'complete','summary':'Fixture completion','recovery_available':False}
    assert client.post(f'/api/jobs/{job}/report', json=report, headers=auth('agent')).status_code == 409
    assert client.post(endpoint, json={'stage': 'ready-for-oobe'}, headers=auth('agent')).status_code == 200
    assert client.post(f'/api/jobs/{job}/report', json={**report,'driver_problems':['NIC missing']}, headers=auth('agent')).status_code == 409
    assert client.post(f'/api/jobs/{job}/report', json=report, headers=auth('agent')).status_code == 200
    assert client.post(f'/api/jobs/{job}/report', json=report, headers=auth('agent')).status_code == 409
    assert client.post(endpoint, json={'stage':'failed'}, headers=auth()).status_code == 409
    stored = client.get(f'/api/jobs/{job}/report', headers=auth()).json()
    assert stored['image_sha256'] == hashlib.sha256(b'wim fixture').hexdigest()


def test_target_and_image_validation(client):
    assert client.post('/api/jobs', json={'image_id':'missing','target_identity':'VM'}, headers=auth()).status_code == 422
    assert client.post('/api/jobs', json={'image_id':'missing','target_identity':''}, headers=auth()).status_code == 422


def profile_image(c, monkeypatch):
    monkeypatch.setattr('repxe.app.verify_wim', lambda p, s: {'selected_image': {'build': '26100'}})
    uid = ready(c, b'profile wim fixture', kind='image')
    assert publish(c, uid).status_code == 200
    return uid


def test_profiles_roles_publication_and_choices(client, monkeypatch):
    uid = profile_image(client, monkeypatch)
    spec = {'name': 'Workshop', 'windows_label': 'Windows 11 Pro', 'image_id': uid}
    for role in ['capture', 'agent', 'deploy']:
        assert client.post('/api/profiles', json=spec, headers=auth(role)).status_code == 403
    assert client.post('/api/profiles', json={**spec, 'password': 'secret'}, headers=auth()).status_code == 422
    assert client.post('/api/profiles', json={**spec, 'recovery_mib': 100}, headers=auth()).status_code == 422
    profile = client.post('/api/profiles', json=spec, headers=auth()).json()
    assert not profile['enabled'] and profile['requires_disk_confirmation']
    assert client.get('/api/profiles', headers=auth('deploy')).json() == []
    assert client.post('/api/profiles/'+profile['id']+'/availability', json={'enabled': True}, headers=auth('deploy')).status_code == 403
    assert client.post('/api/profiles/'+profile['id']+'/availability', json={'enabled': True}, headers=auth()).status_code == 200
    options = client.get('/api/boot-options', headers=auth('deploy')).json()
    assert options['selectedProfileId'] is None and options['requiresDiskConfirmation']
    assert options['choices'][0]['windows_build'] == '26100'
    assert options['choices'][0]['image_sha256'] == hashlib.sha256(b'profile wim fixture').hexdigest()
    assert options['choices'][0]['driver_id'] is None
    assert client.get('/api/boot-options', headers=auth('capture')).status_code == 403
    assert client.get('/api/jobs', headers=auth()).json() == []


def test_device_assignments_fail_closed_and_remove(client, monkeypatch):
    uid = profile_image(client, monkeypatch)
    profile = client.post('/api/profiles', json={'name': 'Assigned', 'windows_label': 'Pro', 'image_id': uid,
                          'selection_mode': 'assigned-device'}, headers=auth()).json()
    assignment = {'profile_id': profile['id'], 'target_identity': 'serial:VM-001'}
    assert client.post('/api/device-assignments', json=assignment, headers=auth()).status_code == 422
    client.post('/api/profiles/'+profile['id']+'/availability', json={'enabled': True}, headers=auth())
    assert client.get('/api/boot-options', headers=auth('deploy')).json()['choices'] == []
    for role in ['capture', 'agent', 'deploy']:
        assert client.post('/api/device-assignments', json=assignment, headers=auth(role)).status_code == 403
    assert client.post('/api/device-assignments', json={**assignment, 'target_identity': 'unstable:PhysicalDrive0'}, headers=auth()).status_code == 422
    a = client.post('/api/device-assignments', json=assignment, headers=auth()).json()
    assert client.post('/api/device-assignments', json=assignment, headers=auth()).json()['id'] == a['id']
    options = client.get('/api/boot-options?target_identity=serial:VM-001', headers=auth('deploy')).json()
    assert options['selectedProfileId'] == profile['id'] and len(options['choices']) == 1
    client.post('/api/profiles/'+profile['id']+'/availability', json={'enabled': False}, headers=auth())
    assert client.get('/api/boot-options?target_identity=serial:VM-001', headers=auth('deploy')).status_code == 409
    assert client.delete('/api/device-assignments/'+a['id'], headers=auth()).status_code == 204
    assert client.get('/api/boot-options?target_identity=serial:VM-001', headers=auth('deploy')).json()['selectedProfileId'] is None


def test_profile_driver_build_and_exact_model(client, monkeypatch):
    image = profile_image(client, monkeypatch)
    payload = driver_zip()
    spec = {'kind': 'drivers', 'name': 'VM drivers', 'filename': 'drivers.zip', 'size': len(payload),
            'sha256': hashlib.sha256(payload).hexdigest(), 'make': 'Example', 'model': 'VM', 'windows_version': '26100'}
    uid = client.post('/api/uploads', json=spec, headers=auth()).json()['id']
    assert send(client, uid, payload).status_code == 200
    assert client.post(f'/api/uploads/{uid}/finish', headers=auth()).status_code == 200
    assert publish(client, uid).status_code == 200
    config = {'name': 'Matched', 'windows_label': 'Windows Pro', 'image_id': image, 'driver_ids': [uid]}
    assert client.post('/api/profiles', json={**config, 'driver_ids': [uid,uid]}, headers=auth()).status_code == 422
    profile = client.post('/api/profiles', json=config, headers=auth()).json()
    client.post('/api/profiles/'+profile['id']+'/availability', json={'enabled': True}, headers=auth())
    assert client.get('/api/boot-options?manufacturer=Other&model=VM', headers=auth('deploy')).json()['choices'] == []
    choice = client.get('/api/boot-options?manufacturer=example&model=vm', headers=auth('deploy')).json()['choices'][0]
    assert choice['driver_id'] == uid
    monkeypatch.setattr('repxe.app.verify_wim', lambda p,s: {'selected_image': {'build': '26300'}})
    other = ready(client, b'other profile fixture', kind='image');publish(client, other)
    assert client.post('/api/profiles', json={**config, 'image_id': other}, headers=auth()).status_code == 422


def test_profile_assignments_persist(tmp_path, monkeypatch):
    root = tmp_path/'profiles'
    with TestClient(create_app(root, TOKENS)) as c:
        image = profile_image(c, monkeypatch)
        profile = c.post('/api/profiles', json={'name': 'Persistent', 'windows_label': 'Pro', 'image_id': image,
                        'selection_mode': 'assigned-device'}, headers=auth()).json()
        c.post('/api/profiles/'+profile['id']+'/availability', json={'enabled': True}, headers=auth())
        c.post('/api/device-assignments', json={'target_identity': 'serial:VM-001', 'profile_id': profile['id']}, headers=auth())
    with TestClient(create_app(root, TOKENS)) as c:
        assert c.get('/api/boot-options?target_identity=serial:VM-001', headers=auth('deploy')).json()['selectedProfileId'] == profile['id']
        assert c.get('/api/profiles', headers=auth()).json()[0]['enabled']


def test_chunk_limits_and_cancel(client):
    uid = start(client, driver_zip())
    assert send(client, uid, b'x'*(16*1024*1024+1)).status_code == 413
    assert send(client, uid, b'').status_code == 422
    assert send(client, uid, b'x'*20000).status_code == 413
    assert client.delete(f'/api/uploads/{uid}', headers=auth()).status_code == 204
    assert send(client, uid, b'x').status_code == 409


def native_job(c, monkeypatch):
    monkeypatch.setattr('repxe.app.verify_wim', lambda p, s: {'selected_image': {'build': 'test'}})
    uid = ready(c, b'wim native fixture', kind='image')
    assert publish(c, uid).status_code == 200
    job = c.post('/api/jobs', json={'image_id': uid, 'target_identity': 'serial:VM'}, headers=auth('deploy')).json()['id']
    cap = c.post(f'/api/jobs/{job}/report-credential', json={}, headers=auth('deploy')).json()
    for stage in ['preflight', 'disk-prepared', 'image-applied', 'boot-configured', 'drivers-staged', 'agent-staged', 'awaiting-windows']:
        assert c.post(f'/api/jobs/{job}/events', json={'stage': stage}, headers=auth('deploy')).status_code == 200
    snapshot = {'schemaVersion': 1, 'stage': 'complete', 'target': {'identity': 'serial:VM'},
                'imageSha256': hashlib.sha256(b'wim native fixture').hexdigest(), 'imageIndex': 1,
                'generalizationPerformed': False, 'drivers': {'problems': []},
                'bootObservations': [{'bootId': 'first'}, {'bootId': 'second'}],
                'startupTriggerRemoved': True, 'sealedImageState': 'IMAGE_STATE_SPECIALIZE_RESEAL_TO_OOBE',
                'oobeBootVerified': False, 'history': [{'stage': s} for s in ['ready-for-oobe', 'sealing-oobe', 'complete']]}
    return job, cap, snapshot


def test_native_report_capability_and_retry(client, monkeypatch):
    job, cap, snapshot = native_job(client, monkeypatch)
    endpoint = cap['path']; headers = {'Authorization': 'Bearer ' + cap['token']}
    assert client.get('/api/images', headers=headers).status_code == 401
    assert client.post(endpoint, json=snapshot, headers=auth('agent')).status_code == 401
    payload = json.dumps(snapshot).encode()
    result = client.post(endpoint, content=payload, headers=headers)
    assert result.status_code == 200, result.text
    assert result.json()['sha256'] == hashlib.sha256(payload).hexdigest()
    assert client.post(endpoint, content=payload, headers=headers).json()['duplicate'] is True
    snapshot['summary'] = 'Changed retry'
    assert client.post(endpoint, json=snapshot, headers=headers).status_code == 409
    assert client.get(f'/api/jobs/{job}/report', headers=auth()).json()['native']['stage'] == 'complete'


def test_native_report_binding_and_evidence(client, monkeypatch):
    _, cap, snapshot = native_job(client, monkeypatch)
    headers = {'Authorization': 'Bearer ' + cap['token']}
    for path, value in [('imageSha256', '0'*64), ('imageIndex', 2), ('target', {'identity': 'serial:OTHER'})]:
        wrong = {**snapshot, path: value}
        assert client.post(cap['path'], json=wrong, headers=headers).status_code == 409
    for path, value in [('drivers', {'problems': ['Missing driver']}), ('startupTriggerRemoved', False),
                        ('bootObservations', [{'bootId': 'same'}, {'bootId': 'same'}]), ('history', [])]:
        assert client.post(cap['path'], json={**snapshot, path: value}, headers=headers).status_code == 422
    assert client.post(cap['path'], content=b'x'*(512*1024+1), headers=headers).status_code == 413


def test_native_report_rotation_and_expiry(client, monkeypatch):
    job, cap, snapshot = native_job(client, monkeypatch)
    assert client.post(f'/api/jobs/{job}/report-credential', json={}, headers=auth('agent')).status_code == 403
    new = client.post(f'/api/jobs/{job}/report-credential', json={}, headers=auth('deploy')).json()
    assert client.post(cap['path'], json=snapshot, headers={'Authorization': 'Bearer '+cap['token']}).status_code == 401
    with client.app.state.store.db() as db:
        db.execute('UPDATE report_credentials SET expires=? WHERE job_id=?', ('2000-01-01T00:00:00+00:00', job))
    assert client.post(new['path'], json=snapshot, headers={'Authorization': 'Bearer '+new['token']}).status_code == 401


def test_native_report_operator_stop(client, monkeypatch):
    job, cap, snapshot = native_job(client, monkeypatch)
    snapshot.update(stage='needs-operator', drivers={'problems': [{'problemCode': 28}]}, history=[])
    assert client.post(cap['path'], json=snapshot, headers={'Authorization': 'Bearer '+cap['token']}).status_code == 200
    assert client.get(f'/api/jobs/{job}', headers=auth()).json()['stage'] == 'needs-operator'


def test_native_driver_manifest_contract(client):
    uid = ready(client)
    assert publish(client, uid).status_code == 200
    assert client.get(f'/api/drivers/{uid}/client-manifest', headers=auth('deploy')).status_code == 422
    # Require a reviewed numeric build before a package can feed the native exact matcher.
    with client.app.state.store.db() as db:
        row = db.execute('SELECT metadata FROM artifacts WHERE id=?', (uid,)).fetchone()
        metadata = json.loads(row['metadata']); metadata['windows_version'] = '26300'
        db.execute('UPDATE artifacts SET metadata=? WHERE id=?', (json.dumps(metadata), uid))
    manifest = client.get(f'/api/drivers/{uid}/client-manifest', headers=auth('deploy')).json()
    assert manifest['windowsBuild'] == '26300' and manifest['review']['hardwareTestPassed']
    assert manifest['infPaths'] == ['nic/driver.inf']
    assert len(manifest['files']) == 3
    assert client.get('/api/drivers/missing/client-manifest', headers=auth()).status_code == 404


def test_native_report_cross_job_and_malformed_body(client, monkeypatch):
    job, cap, snapshot = native_job(client, monkeypatch)
    other, other_cap, _ = native_job(client, monkeypatch)
    headers = {'Authorization': 'Bearer ' + cap['token']}
    assert client.post(other_cap['path'], json=snapshot, headers=headers).status_code == 401
    for payload in [{}, {'stage': 'complete'}, [], {'unexpected': True}]:
        assert client.post(cap['path'], json=payload, headers=headers).status_code == 422
    assert client.post(cap['path'], json={**snapshot, 'imageIndex': True}, headers=headers).status_code == 422


def test_event_retry_binding_roles_and_restart(tmp_path, monkeypatch):
    monkeypatch.setattr('repxe.app.verify_wim', lambda p, s: {'selected_image': {'build': 'test'}})
    with TestClient(create_app(tmp_path, TOKENS)) as c:
        uid = ready(c, b'wim retry fixture', kind='image')
        assert publish(c, uid).status_code == 200
        jobs = [c.post('/api/jobs', json={'image_id': uid, 'target_identity': f'VM-{i}'}, headers=auth('deploy')).json()['id'] for i in range(2)]
        endpoint = f'/api/jobs/{jobs[0]}/events'
        event = {'stage': 'preflight', 'detail': 'Durable checkpoint', 'event_id': 'lost-ack-1'}
        first = c.post(endpoint, json=event, headers=auth('deploy'))
        assert first.status_code == 200
        assert c.post(endpoint, json={**event, 'detail': 'Changed'}, headers=auth('deploy')).status_code == 409
        assert c.post(endpoint, json=event, headers=auth('agent')).status_code == 403
        assert c.post(endpoint, json={**event, 'event_id': '../bad'}, headers=auth('deploy')).status_code == 422
        # The same ID on another job has independent state and cannot return the first job's ACK.
        second = c.post(f'/api/jobs/{jobs[1]}/events', json=event, headers=auth('deploy'))
        assert second.status_code == 200 and second.json()['id'] == jobs[1]
        assert c.post(endpoint, json={'stage': 'cancelled', 'event_id': 'stop-1'}, headers=auth('deploy')).status_code == 200
    with TestClient(create_app(tmp_path, TOKENS)) as c:
        # A lost acknowledgement remains retryable after restart and a terminal transition.
        assert c.post(endpoint, json=event, headers=auth('deploy')).json() == first.json()
        state = c.get(f'/api/jobs/{jobs[0]}', headers=auth('deploy')).json()
        assert state['stage'] == 'cancelled'
        assert [e['stage'] for e in state['events']] == ['created', 'preflight', 'cancelled']
        assert c.post(endpoint, json={**event, 'event_id': 'new'}, headers=auth('deploy')).status_code == 409


def test_native_report_additional_driver_reboots_are_bounded(client,monkeypatch):
    job,cap,snapshot=native_job(client,monkeypatch)
    url=f'/api/client-jobs/{job}/report';headers={'Authorization':'Bearer '+cap['token']}
    snapshot['windowsUpdatePolicy']={'timeoutSeconds':60,'maxAttempts':2}
    snapshot['windowsUpdatePolicyRestored']=True
    snapshot['windowsUpdateAttempts']=[{'status':'succeeded','result':{'automaticReboot':False}}]
    snapshot['bootObservations']=[{'bootId':str(i)} for i in range(5)]
    assert client.post(url,json=snapshot,headers=headers).status_code==422
    snapshot['bootObservations']=[{'bootId':str(i)} for i in range(3)]
    assert client.post(url,json=snapshot,headers=headers).status_code==200


def test_vendor_driver_filenames_and_manifest(client):
    folder = 'Intel(R) + Audio [v2] & Français'
    payload = driver_zip({folder+'/driver.inf': '[Version]\nCatalogFile=driver.cat\n', folder+'/driver.cat':'fixture', folder+'/driver.sys':'fixture'})
    uid = ready(client,payload)
    with client.app.state.store.db() as db:
        metadata=json.loads(db.execute('SELECT metadata FROM artifacts WHERE id=?',(uid,)).fetchone()['metadata'])
        metadata['windows_version']='26300'
        db.execute('UPDATE artifacts SET metadata=? WHERE id=?',(json.dumps(metadata),uid))
    manifest=client.get(f'/api/drivers/{uid}/client-manifest',headers=auth('deploy')).json()
    assert manifest['infPaths']==[folder+'/driver.inf']
    assert any(f['path']==folder+'/driver.sys' for f in manifest['files'])


@pytest.mark.parametrize('filename',['bad?.inf','bad*.inf','bad|.inf','bad<.inf','bad".inf','con .inf','COM¹.inf','bad\x01.inf','a'*256+'.inf'])
def test_invalid_windows_driver_filename_rejected_on_upload(client,filename):
    payload=driver_zip({filename:'bad'})
    uid=start(client,payload)
    assert send(client,uid,payload).status_code==200
    assert client.post(f'/api/uploads/{uid}/finish',headers=auth()).status_code==422


def technician_snapshot(snapshot):
    snapshot.update(driverConsole=True,driverRebootRequired=False,driverInstallationMayBeInProgress=False,
                    driverVerifiedBoot='second',windowsUpdatePolicyRestored=True,
                    drivers={'problems':[{'description':'Missing test adapter','instanceId':'PCI\\TEST','problemCode':28}]},
                    technicianFinish={'requested':True,'requestedUtc':'2026-10-08T06:00:00Z','sealedUtc':'2026-10-08T06:01:00Z','unresolvedDeviceCount':1})
    return snapshot


def test_technician_finish_retains_problems_and_cancels_unfinished_upload(client,monkeypatch):
    job,cap,snapshot=native_job(client,monkeypatch)
    snapshot=technician_snapshot(snapshot);headers={'Authorization':'Bearer '+cap['token']}
    unfinished=start(client,driver_zip())
    with client.app.state.store.db() as db:db.execute('UPDATE uploads SET owner=? WHERE id=?',('job:'+job,unfinished))
    result=client.post(cap['path'],json=snapshot,headers=headers)
    assert result.status_code==200,result.text
    assert client.post(cap['path'],json=snapshot,headers=headers).json()['duplicate']
    stored=client.get(f'/api/jobs/{job}/report',headers=auth()).json()
    assert stored['result']=='complete' and stored['driver_problems']==snapshot['drivers']['problems']
    assert '1 unresolved devices' in stored['summary']
    assert client.get(f'/api/uploads/{unfinished}',headers=auth()).json()['status']=='cancelled'
    detail=client.get(f'/api/job-reports/{job}',headers=auth()).json()
    assert detail['technician_finish']['requested'] and detail['driver_problems'][0]['problem_code']==28
    from pypdf import PdfReader
    text=' '.join(p.extract_text() for p in PdfReader(io.BytesIO(client.get(f'/api/job-reports/{job}/pdf',headers=auth()).content)).pages)
    assert 'Technician finish' in text and 'Missing test adapter' in text


@pytest.mark.parametrize('change',[
    {'technicianFinish':None}, {'driverConsole':False}, {'driverRebootRequired':True},
    {'driverInstallationMayBeInProgress':True},{'driverVerifiedBoot':'first'},
    {'sealedImageState':'IMAGE_STATE_COMPLETE'}, {'startupTriggerRemoved':False},
    {'technicianFinish':{'requested':False,'unresolvedDeviceCount':1}},
    {'technicianFinish':{'requested':True,'unresolvedDeviceCount':0}},
])
def test_technician_finish_keeps_completion_integrity_checks(client,monkeypatch,change):
    _,cap,snapshot=native_job(client,monkeypatch)
    snapshot=technician_snapshot(snapshot);snapshot.update(change)
    assert client.post(cap['path'],json=snapshot,headers={'Authorization':'Bearer '+cap['token']}).status_code==422


def test_driver_upload_cancel_is_scoped_to_active_job(client,monkeypatch):
    job,cap,_=native_job(client,monkeypatch)
    other,other_cap,_=native_job(client,monkeypatch)
    uid=start(client,driver_zip())
    with client.app.state.store.db() as db:db.execute('UPDATE uploads SET owner=? WHERE id=?',('job:'+job,uid))
    endpoint=f'/api/client-jobs/{job}/driver-uploads/{uid}/cancel'
    assert client.post(endpoint,json={},headers={'Authorization':'Bearer '+other_cap['token']}).status_code==401
    assert client.post(f'/api/client-jobs/{other}/driver-uploads/{uid}/cancel',json={},headers={'Authorization':'Bearer '+other_cap['token']}).status_code==403
    assert client.post(endpoint,json={},headers={'Authorization':'Bearer '+cap['token']}).status_code==200
    assert client.get(f'/api/uploads/{uid}',headers=auth()).json()['status']=='cancelled'
