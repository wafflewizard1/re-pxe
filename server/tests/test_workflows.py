"""No host disks: HTTP state/evidence tests and synthetic agent inventory only."""
import gzip
import hashlib
import json
from pathlib import Path
import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from repxe.app import create_app
from repxe import capabilities, shredos_agent
from test_api import TOKENS, auth
from test_pxe import lab, boot, enroll, managed_enroll

MAC = '52:54:00:12:34:56'
TARGET = 'serial:LAB-001'
SIZE = 64 * 1024**3
PDF = b'%PDF-1.4\nfixture certificate\n%%EOF\n'


def install_media(root):
    folder = root / 'boot/pxe/shredos'
    release = 'a' * 64
    (folder / release).mkdir(parents=True)
    files = {}
    for name in ('shredos', 'companion.cpio.gz'):
        p = folder / release / name
        p.write_bytes(b'fixture')
        files[name] = {'size': 7, 'sha256': hashlib.sha256(b'fixture').hexdigest()}
    (folder / 'current.json').write_text(json.dumps({'agent_version': 're-pxe-shredos-2', 'validated': True,
                                                 'release': release, 'files': files, 'upstream_commit': 'fixture'}))
    p = root / 'boot/pxe/current.json'
    current = json.loads(p.read_text())
    current['validated'] = True
    p.write_text(json.dumps(current))


def template(c, profile=None, after='stop'):
    value = {'name': 'Workshop', 'after_erasure': after, 'profile_id': profile}
    r = c.post('/api/workflows', headers=auth(), json=value)
    assert r.status_code == 201, r.text
    return r.json()


def queue(c, t, mac=MAC, target=TARGET):
    r = c.post('/api/workflow-runs', headers=auth(), json={'template_id': t['id'], 'mac': mac,
                'target_identity': target, 'size_bytes': SIZE, 'confirmation': target, 'asset_tag': 'PC-42'})
    assert r.status_code == 201, r.text
    return r.json()


def claim(c, target=TARGET, mac=MAC, size=SIZE):
    script = c.get('/pxe/boot.ipxe?mac=' + mac)
    assert script.status_code == 200, script.text
    assert '/shredos/' in script.text and 'companion.cpio.gz' in script.text
    ticket = re.search(r'repxe_ticket=([A-Za-z0-9_-]+)', script.text)[1]
    inventory = {'manufacturer': 'Example', 'model': 'VM', 'system_serial': 'PC-001', 'cpu': 'Test CPU',
                 'memory_bytes': 8 * 1024**3, 'disks': [{'identity': target, 'size_bytes': size}]}
    response = c.post('/api/erase/claim', headers={'Authorization': 'Bearer ' + ticket}, json={
                     'mac': mac, 'agent_version': 're-pxe-shredos-1', 'inventory': inventory})
    return response, ticket


def result(run, **changes):
    stamp = datetime.now(timezone.utc).isoformat()
    return {'target_identity': TARGET, 'size_bytes': SIZE, 'method': 'zero', 'verification': 'all',
            'rounds': 1, 'final_blank': True, 'result': 'passed', 'exit_code': 0, 'errors': 0,
            'started': stamp, 'finished': stamp, 'certificate_sha256': hashlib.sha256(PDF).hexdigest(), **changes}


def successful_erase(c, run):
    claimed, _ = claim(c)
    assert claimed.status_code == 201, claimed.text
    headers = {'Authorization': 'Bearer ' + claimed.json()['token']}
    assert c.post('/api/erase/begin', headers=headers, json={}).status_code == 200
    assert c.put('/api/erase/certificate', headers={**headers, 'Content-Type': 'application/pdf'}, content=PDF).status_code == 200
    response = c.post('/api/erase/result', headers=headers, json=result(run))
    assert response.status_code == 200, response.text
    return headers


def test_capability_gate_and_template_validation(lab):
    c, root, p = lab
    t = template(c)
    payload = {'template_id': t['id'], 'mac': MAC, 'target_identity': TARGET, 'size_bytes': SIZE, 'confirmation': TARGET}
    assert c.post('/api/workflow-runs', headers=auth(), json=payload).status_code == 503
    assert c.post('/api/workflows', headers=auth(), json={'name': 'Bad', 'after_erasure': 'automatic'}).status_code == 422
    assert c.post('/api/workflows', headers=auth(), json={'name': 'Bad', 'rounds': True}).status_code == 422
    install_media(root)
    assert c.post('/api/workflow-runs', headers=auth(), json={**payload, 'confirmation': 'wrong'}).status_code == 422
    for role in ('deploy', 'agent', 'capture'):
        assert c.post('/api/workflow-runs', headers=auth(role), json=payload).status_code == 403
    assert c.get('/api/workflows').status_code == 401


def test_armed_boot_binds_discovered_pc_serial_without_kernel_argument_injection(lab):
    c, root, p = lab
    install_media(root)
    run = queue(c, template(c))
    first = c.get('/pxe/managed.ipxe?mac=' + MAC).text
    assert 'repxe_serial_sha256=' not in first  # Manual queue has no discovered PC binding.
    serial = 'PC serial with spaces; repxe_mode=intake'
    inventory = {'system_serial': serial, 'disks': [{'identity': TARGET, 'size_bytes': SIZE}]}
    with c.app.state.store.db() as db:
        db.execute('UPDATE workflow_runs SET inventory=? WHERE id=?', (json.dumps(inventory), run['id']))
    script = c.get('/pxe/managed.ipxe?mac=' + MAC).text
    assert 'repxe_serial_sha256=' + hashlib.sha256(serial.encode()).hexdigest() in script
    assert serial not in script and 'repxe_mode=intake' not in script
    ticket = re.search(r'repxe_ticket=([A-Za-z0-9_-]+)', script)[1]
    wrong_pc = c.post('/api/erase/claim', headers={'Authorization': 'Bearer ' + ticket}, json={
        'mac': MAC, 'agent_version': shredos_agent.VERSION,
        'inventory': {**inventory, 'system_serial': 'OTHER-PC'}})
    assert wrong_pc.status_code == 409 and 'PC serial changed' in wrong_pc.text
    correct = c.post('/api/erase/claim', headers={'Authorization': 'Bearer ' + ticket}, json={
        'mac': MAC, 'agent_version': shredos_agent.VERSION, 'inventory': inventory})
    assert correct.status_code == 201, correct.text
    repeated = c.post('/api/erase/claim', headers={'Authorization': 'Bearer ' + ticket}, json={
        'mac': MAC, 'agent_version': shredos_agent.VERSION, 'inventory': inventory})
    assert repeated.status_code == 401


def test_erasure_claim_scoped_consumed_and_no_repeat_boot(lab):
    c, root, p = lab
    install_media(root)
    run = queue(c, template(c))
    wrong, _ = claim(c, target='serial:OTHER')
    assert wrong.status_code == 409
    response, ticket = claim(c)
    assert response.status_code == 201
    token = response.json()['token']
    headers = {'Authorization': 'Bearer ' + token}
    assert c.get('/api/images', headers=headers).status_code == 401
    duplicate = c.post('/api/erase/claim', headers={'Authorization': 'Bearer ' + ticket}, json={
        'mac': MAC, 'agent_version': 're-pxe-shredos-1', 'inventory': {'system_serial': 'PC', 'disks': [{'identity': TARGET, 'size_bytes': SIZE}]}})
    assert duplicate.status_code == 401
    assert 'kernel' not in c.get('/pxe/managed.ipxe?mac=' + MAC).text
    assert c.post('/api/erase/begin', headers=headers, json={}).status_code == 200
    assert c.post('/api/erase/begin', headers=headers, json={}).status_code == 409
    records = c.get('/api/workflow-runs', headers=auth())
    for secret in ('token_hash', 'ticket_hash', token, ticket):
        assert secret not in records.text
    assert c.get('/api/workflow-runs/' + run['id'], headers=auth()).json()['inventory']['cpu'] == 'Test CPU'


def test_certificate_integrity_immutable_result_and_failure_hold(lab):
    c, root, p = lab
    install_media(root)
    run = queue(c, template(c, p['id'], 'automatic'))
    response, _ = claim(c)
    headers = {'Authorization': 'Bearer ' + response.json()['token']}
    assert c.post('/api/erase/begin', headers=headers, json={}).status_code == 200
    assert c.post('/api/erase/result', headers=headers, json=result(run)).status_code == 409
    assert c.put('/api/erase/certificate', headers={**headers, 'Content-Type': 'application/pdf'}, content=b'bad').status_code == 422
    cert_headers = {**headers, 'Content-Type': 'application/pdf'}
    assert c.put('/api/erase/certificate', headers=cert_headers, content=PDF).status_code == 200
    assert c.put('/api/erase/certificate', headers=cert_headers, content=PDF).json()['duplicate']
    assert c.put('/api/erase/certificate', headers=cert_headers, content=PDF.replace(b'fixture', b'changed')).status_code == 409
    assert c.post('/api/erase/result', headers=headers, json=result(run, certificate_sha256='0' * 64)).status_code == 409
    assert c.post('/api/erase/result', headers=headers, json=result(run, errors=1)).status_code == 422
    failed = result(run, result='failed', exit_code=1, errors=1)
    assert c.post('/api/erase/result', headers=headers, json=failed).json()['stage'] == 'failed'
    assert c.post('/api/erase/result', headers=headers, json=failed).json()['duplicate']
    assert c.post('/api/erase/result', headers=headers, json=result(run)).status_code == 409
    assert 'wimboot' not in c.get('/pxe/boot.ipxe?mac=' + MAC).text
    assert c.get('/api/workflow-runs/' + run['id'] + '/certificate', headers=auth()).content == PDF
    assert c.get('/api/workflow-runs/' + run['id'] + '/certificate', headers=headers).status_code == 401


@pytest.mark.parametrize('after,stage', [('stop', 'complete'), ('approval', 'awaiting-approval'), ('automatic', 'install-ready')])
def test_success_modes_and_evidence_survive_restart(lab, after, stage):
    c, root, p = lab
    install_media(root)
    run = queue(c, template(c, p['id'] if after != 'stop' else None, after))
    headers = successful_erase(c, run)
    assert c.get('/api/erase/status', headers=headers).json()['stage'] == stage
    if after == 'approval':
        assert 'wimboot' not in c.get('/pxe/boot.ipxe?mac=' + MAC).text
        assert c.post('/api/workflow-runs/' + run['id'] + '/action', headers=auth(), json={'action': 'approve-install'}).status_code == 200
        assert c.get('/api/erase/status', headers=headers).json()['reboot_for_windows']
    with TestClient(create_app(root, TOKENS)) as restarted:
        record = restarted.get('/api/workflow-runs/' + run['id'], headers=auth()).json()
        assert record['inventory']['system_serial'] == 'PC-001'
        assert record['result']['result'] == 'passed'
        assert restarted.get(record['certificate_url'], headers=auth()).content == PDF


def test_auto_install_pins_profile_disk_and_allocates_only_one_job(lab):
    c, root, p = lab
    install_media(root)
    run = queue(c, template(c, p['id'], 'automatic'))
    successful_erase(c, run)
    ticket = boot(c)
    headers, enrolled = managed_enroll(c, ticket)
    assert enrolled['automation']['targetIdentity'] == TARGET
    assert enrolled['automation']['profileId'] == p['id']
    assert c.put('/api/clients/' + enrolled['clientId'] + '/configuration', headers=auth(), json={'profile_id': None}).status_code == 409
    assert c.post('/api/pxe/select', headers=headers, json={'profile_id': p['id'], 'target_identity': TARGET}).status_code == 200
    assert c.post('/api/pxe/countdown', headers=headers, json={'action': 'start'}).status_code == 200
    with c.app.state.store.db() as db:
        db.execute('UPDATE clients SET countdown_until=?', ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),))
    started = c.post('/api/pxe/start', headers=headers, json={})
    assert started.status_code == 201, started.text
    assert c.post('/api/pxe/start', headers=headers, json={}).status_code == 409
    assert 'kernel' not in c.get('/pxe/boot.ipxe?mac=' + MAC).text
    r = c.get('/api/workflow-runs/' + run['id'], headers=auth()).json()
    assert r['job_id'] == started.json()['id'] and r['stage'] == 'installing'


def test_wrong_disk_disabled_profile_and_cancel_block_handoff(lab):
    c, root, p = lab
    install_media(root)
    run = queue(c, template(c, p['id'], 'automatic'))
    successful_erase(c, run)
    ticket = boot(c)
    request = {'manufacturer': 'Example', 'model': 'VM', 'client_version': '0.12.0', 'managed_selection': True,
               'targets': [{'identity': 'serial:OTHER', 'size_bytes': SIZE}]}
    assert c.post('/api/pxe/enroll', headers={'Authorization': 'Bearer ' + ticket}, json=request).status_code == 409
    assert c.post('/api/workflow-runs/' + run['id'] + '/action', headers=auth(), json={'action': 'cancel'}).status_code == 200
    request['targets'][0]['identity'] = TARGET
    assert c.post('/api/pxe/enroll', headers={'Authorization': 'Bearer ' + ticket}, json=request).status_code == 409
    assert 'kernel' not in c.get('/pxe/boot.ipxe?mac=' + MAC).text


def test_queue_invalidates_previous_windows_ticket(lab):
    c, root, p = lab
    install_media(root)
    ticket = boot(c)
    queue(c, template(c))
    request = {'client_version': '0.12.0', 'targets': [{'identity': TARGET, 'size_bytes': SIZE}]}
    assert c.post('/api/pxe/enroll', headers={'Authorization': 'Bearer ' + ticket}, json=request).status_code == 401


def test_cookie_writes_keep_csrf_protection(lab):
    c, root, p = lab
    c.post('/api/login', json={'username': 'admin', 'password': 'admin'})
    assert c.post('/api/workflows', headers={'Cookie': 'repxe_session=' + c.cookies.get('repxe_session')}, json={'name': 'Unsafe request'}).status_code == 403


def test_agent_only_explicit_unmounted_whole_disk_and_summary():
    disks = [{'type': 'disk', 'serial': 'LAB-001', 'size': SIZE, 'path': '/dev/vda', 'ro': False,
              'tran': 'virtio', 'mountpoints': [None]}]
    assert shredos_agent.select_target(disks, TARGET, SIZE, False)['path'] == '/dev/vda'
    for altered in ([{**disks[0], 'size': SIZE - 1}], disks * 2,
                    [{**disks[0], 'children': [{'mountpoints': ['/windows']}]}],
                    [{**disks[0], 'tran': 'usb'}], [{**disks[0], 'path': '/dev/vda1'}]):
        with pytest.raises(RuntimeError):
            shredos_agent.select_target(altered, TARGET, SIZE, False)
    args = shredos_agent.nwipe_args({'method': 'zero', 'verification': 'all', 'rounds': 1, 'final_blank': True}, '/dev/vda', Path('/run/evidence'))
    assert args[-1] == '/dev/vda' and '--verify=all' in args
    assert '--autopoweroff' not in args
    assert shredos_agent.passed(' vda | 0 | 0 | 0\n vda | 16777216 | 16777216 | 100.00%\n vda | Erased | 100MB/s | 00:00:01 | VM/LAB-001', '/dev/vda', 0)
    assert not shredos_agent.passed('! vda |-FAILED-| 100MB/s', '/dev/vda', 0)
    assert not shredos_agent.passed('', '/dev/vda', 0)
    assert not shredos_agent.passed(' vda | Erased |', '/dev/vda', 1)


def test_companion_archive_replaces_launcher_without_fork():
    data = gzip.decompress(capabilities.companion('https://server:8443', 'PUBLIC CA'))
    assert b're-pxe-erase.py' in data and b'usr/bin/nwipe_launcher' in data
    assert b'https://server:8443' in data and b'PUBLIC CA' in data
    assert b'--autonuke' in data  # engine invocation is inside the guarded agent
    assert b'while :; do sleep' in data  # console never respawns an erase


def test_intake_is_opt_in_read_only_and_queue_triggers_reboot(lab):
    c, root, p = lab
    install_media(root)
    assert c.get('/api/workflows', headers=auth()).json()['intake_enabled'] is False
    assert c.put('/api/workflow-intake', headers=auth('deploy'), json={'enabled': True}).status_code == 403
    assert c.put('/api/workflow-intake', headers=auth(), json={'enabled': True}).status_code == 200
    script = c.get('/pxe/boot.ipxe?mac=' + MAC)
    assert 'repxe_mode=intake' in script.text and 'wimboot' not in script.text
    ticket = re.search('repxe_ticket=([A-Za-z0-9_-]+)', script.text)[1]
    spec = {'mac': MAC, 'agent_version': 're-pxe-shredos-1', 'inventory': {'manufacturer': 'Example',
            'model': 'VM', 'system_serial': 'PC-42', 'cpu': 'CPU', 'memory_bytes': SIZE,
            'disks': [{'identity': TARGET, 'size_bytes': SIZE}]}}
    response = c.post('/api/intake/claim', headers={'Authorization': 'Bearer ' + ticket}, json=spec)
    assert response.status_code == 201, response.text
    headers = {'Authorization': 'Bearer ' + response.json()['token']}
    assert c.post('/api/erase/begin', headers=headers, json={}).status_code == 401
    assert not c.get('/api/intake/status', headers=headers).json()['reboot_for_workflow']
    devices = c.get('/api/workflow-devices', headers=auth()).json()
    assert devices[0]['disks'][0]['size_bytes'] == SIZE and devices[0]['inventory']['cpu'] == 'CPU'
    queue(c, template(c))
    assert c.get('/api/intake/status', headers=headers).json()['reboot_for_workflow']
    erase = c.get('/pxe/managed.ipxe?mac=' + MAC).text
    assert 'repxe_mode=intake' not in erase and '/shredos/' in erase


def test_intake_ticket_is_one_use_and_media_is_required(lab):
    c, root, p = lab
    assert c.put('/api/workflow-intake', headers=auth(), json={'enabled': True}).status_code == 503
    install_media(root)
    c.put('/api/workflow-intake', headers=auth(), json={'enabled': True})
    script = c.get('/pxe/boot.ipxe?mac=' + MAC).text
    ticket = re.search('repxe_ticket=([A-Za-z0-9_-]+)', script)[1]
    spec = {'mac': MAC, 'agent_version': 're-pxe-shredos-1', 'inventory': {'system_serial': 'PC', 'disks': [{'identity': TARGET, 'size_bytes': SIZE}]}}
    headers = {'Authorization': 'Bearer ' + ticket}
    assert c.post('/api/intake/claim', headers=headers, json={**spec, 'mac': '52:54:00:12:34:57'}).status_code == 403
    assert c.post('/api/intake/claim', headers=headers, json=spec).status_code == 201
    assert c.post('/api/intake/claim', headers=headers, json=spec).status_code == 401


def test_long_erasure_renews_only_its_scoped_session(lab):
    c, root, p = lab
    install_media(root)
    run = queue(c, template(c))
    response, _ = claim(c)
    headers = {'Authorization': 'Bearer ' + response.json()['token']}
    c.post('/api/erase/begin', headers=headers, json={})
    with c.app.state.store.db() as db:
        short = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
        db.execute('UPDATE workflow_runs SET expires=? WHERE id=?', (short, run['id']))
    assert c.get('/api/erase/status', headers=headers).json()['stage'] == 'erasing'
    with c.app.state.store.db() as db:
        expires = db.execute('SELECT expires FROM workflow_runs WHERE id=?', (run['id'],)).fetchone()['expires']
        assert expires > (datetime.now(timezone.utc) + timedelta(hours=47)).isoformat()
        db.execute('UPDATE workflow_runs SET expires=? WHERE id=?', ('2000-01-01', run['id']))
    assert c.get('/api/erase/status', headers=headers).status_code == 401


def test_nwipe_heartbeat_never_restarts_engine_on_network_failure(monkeypatch):
    calls, waits = [], [False, False, True]
    class Process:
        def __init__(self, args):
            calls.append(args)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def wait(self, timeout):
            if not waits.pop(0):
                raise shredos_agent.subprocess.TimeoutExpired('nwipe', timeout)
            return 0
    class API:
        def request(self, *args, **kwargs):
            raise TimeoutError('network unavailable')
    monkeypatch.setattr(shredos_agent.subprocess, 'Popen', Process)
    assert shredos_agent.run_nwipe(['fixture-nwipe'], API(), 'fixture-token') == 0
    assert calls == [['fixture-nwipe']]
