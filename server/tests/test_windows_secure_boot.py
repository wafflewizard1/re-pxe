"""Signed entry scoping and release binding; no host disks or firmware changes."""
import hashlib
import json
import re

import pytest
from repxe import windows_secure_boot
from test_api import auth
from test_pxe import lab, managed_enroll, boot
from test_routines import make, select
from test_workflows import install_media, MAC


def signed_media(root):
    directory = 'candidate-0.14.1-' + 'a'*12
    base = root/'boot/pxe'/directory
    base.mkdir()
    files = {}
    for name in windows_secure_boot.WINDOWS_FILES:
        (base/name).write_bytes(name.encode())
        files[name] = {'size': len(name), 'sha256': hashlib.sha256(name.encode()).hexdigest()}
    windows = {'version': '0.14.1', 'directory': directory, 'files': files}
    signed = root/'boot/pxe/secureboot'/('ipxe-2.0.0-shim-16.1-'+'b'*12)
    signed.mkdir(parents=True)
    loaders = {}
    for name in windows_secure_boot.LOADER_FILES:
        (signed/name).write_bytes(name.encode())
        loaders[name] = {'size': len(name), 'sha256': hashlib.sha256(name.encode()).hexdigest()}
    manifest = {'schemaVersion': 1, 'scope': 'windows-only', 'architecture': 'amd64',
                'directory': signed.name, 'windowsMedia': windows, 'files': loaders, 'secureBootValidated': False}
    (signed/'release.json').write_text(json.dumps(manifest))
    (signed.parent/'current.json').write_text(json.dumps(manifest))
    return manifest


def menu_start(c):
    response = c.get('/pxe/boot.ipxe', params={'mac': MAC, 'windows_only': 1})
    assert response.status_code == 200, response.text
    assert 'Windows-only Secure Boot candidate' in response.text
    assert '--timeout' not in response.text  # Upstream signed build has no custom countdown patch.
    url = re.search(r'chain --replace --autofree (http[^\n]*start.ipxe[^\n]*) \|\|', response.text)[1]
    return url.replace('http://127.0.0.1:8089', '')


def test_signed_windows_boot_preserves_normal_media_and_binds_scope(lab):
    c, root, profile = lab
    install_media(root)
    uid = make(c, profile)
    manifest = signed_media(root)
    normal = (root/'boot/pxe/current.json').read_bytes()
    start = menu_start(c)
    chosen = c.get(start)
    assert chosen.status_code == 200 and 'windows_only=1' in chosen.text
    # Pinned run retains the restriction even if a later request omits the query parameter.
    response = c.get('/pxe/managed.ipxe', params={'mac': MAC})
    assert response.status_code == 200, response.text
    assert '/pxe/'+manifest['windowsMedia']['directory']+'/boot.wim' in response.text
    assert '/shredos/' not in response.text
    ticket = re.search('/pxe/config/([A-Za-z0-9_-]+)', response.text)[1]
    native, session = managed_enroll(c, ticket)
    assert session['automation']['windowsOnly'] is True
    with c.app.state.store.db() as db:
        assert db.execute('SELECT windows_only FROM routine_runs WHERE routine_id=?', (uid,)).fetchone()[0] == 1
    assert (root/'boot/pxe/current.json').read_bytes() == normal
    status = c.get('/api/dashboard', headers=auth()).json()['windows_secure_boot']
    assert status['available'] and status['scope'] == 'windows-only'
    assert 'hardware testing' in status['validation'] and manifest['directory'] in status['dhcp_filename']


def test_signed_menu_filters_wipes_and_server_refuses_forged_choice(lab):
    c, root, profile = lab
    install_media(root)
    windows_id = make(c, profile)
    erase_id = make(c, profile, {'method': 'zero'})
    signed_media(root)
    response = c.get('/pxe/routines/menu.ipxe', params={'mac': MAC, 'windows_only': 1, 'alternatives': 1})
    assert windows_id in response.text and erase_id not in response.text
    assert 'windows_only=1' in response.text
    start = re.search(r'chain --replace --autofree (http[^\n]*start.ipxe[^\n]*) \|\|', response.text)[1].replace('http://127.0.0.1:8089', '')
    forged = start.replace('routine='+windows_id, 'routine='+erase_id)
    rejected = c.get(forged)
    assert rejected.status_code == 409 and 'Windows-only' in rejected.text
    with c.app.state.store.db() as db:
        assert db.execute('SELECT count(*) FROM routine_runs').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM workflow_runs').fetchone()[0] == 0
    # Rejection does not consume the menu ticket or prevent a supported choice.
    assert c.get(start).status_code == 200


def test_signed_entry_holds_existing_erasure_without_issuing_boot_or_tickets(lab):
    c, root, profile = lab
    install_media(root)
    make(c, profile, {'method': 'zero'})
    select(c)
    signed_media(root)
    with c.app.state.store.db() as db:
        before = db.execute('SELECT count(*) FROM workflow_intake').fetchone()[0]
    response = c.get('/pxe/managed.ipxe', params={'mac': MAC, 'windows_only': 1})
    assert 'active routine needs erasure' in response.text and 'kernel ' not in response.text
    with c.app.state.store.db() as db:
        assert db.execute('SELECT count(*) FROM workflow_intake').fetchone()[0] == before
    # Existing regular entry still supplies ShredOS.
    assert '/shredos/' in c.get('/pxe/managed.ipxe', params={'mac': MAC}).text


def test_every_signed_menu_return_preserves_scope_and_custom_menus_are_bypassed(lab):
    c, root, profile = lab
    install_media(root)
    make(c, profile)
    signed_media(root)
    response = c.get('/pxe/boot.ipxe', params={'mac': MAC, 'windows_only': 1})
    for line in response.text.splitlines():
        if 'chain ' in line and '/menu.ipxe?' in line:
            assert 'windows_only=1' in line
    assert '/custom/' not in response.text


@pytest.mark.parametrize('change', ['missing-loader', 'missing-windows', 'path', 'scope', 'release-mismatch'])
def test_invalid_signed_release_cannot_fall_back_to_unsigned_boot(lab, change):
    c, root, profile = lab
    install_media(root)
    make(c, profile)
    manifest = signed_media(root)
    folder = root/'boot/pxe/secureboot'
    if change == 'missing-loader': (folder/manifest['directory']/'snponly.efi').unlink()
    elif change == 'missing-windows': (root/'boot/pxe'/manifest['windowsMedia']['directory']/'boot.wim').unlink()
    elif change == 'path': manifest['windowsMedia']['directory'] = '../../outside'
    elif change == 'scope': manifest['scope'] = 'anything'
    elif change == 'release-mismatch': manifest['secureBootValidated'] = True
    (folder/'current.json').write_text(json.dumps(manifest))
    rejected = c.get('/pxe/boot.ipxe', params={'mac': MAC, 'windows_only': 1})
    assert rejected.status_code == 503 and 'kernel ' not in rejected.text
    assert not windows_secure_boot.status(root)['available']
    with c.app.state.store.db() as db:
        assert db.execute('SELECT count(*) FROM clients').fetchone()[0] == 0
    # The normal entry is independent of this candidate.
    assert c.get('/pxe/boot.ipxe', params={'mac': MAC}).status_code == 200


def test_signed_entry_requires_routines_and_reports_missing_media(lab):
    c, root, profile = lab
    assert 'routine boot to be enabled' in c.get('/pxe/boot.ipxe', params={'mac': MAC, 'windows_only': 1}).text
    assert not c.get('/api/dashboard', headers=auth()).json()['windows_secure_boot']['available']
    c.put('/api/routine-settings', headers=auth(), json={'enabled': True})
    assert c.get('/pxe/boot.ipxe', params={'mac': MAC, 'windows_only': 1}).status_code == 503


def test_stopped_windows_test_run_can_be_removed_without_deleting_its_report(lab):
    c, root, profile = lab
    install_media(root)
    make(c, profile)
    signed_media(root)
    assert c.get(menu_start(c)).status_code == 200
    script = c.get('/pxe/managed.ipxe', params={'mac': MAC}).text
    ticket = re.search('/pxe/config/([A-Za-z0-9_-]+)', script)[1]
    native, session = managed_enroll(c, ticket)
    run = c.get('/api/routines', headers=auth()).json()['runs'][0]
    assert c.post('/api/clients/'+session['clientId']+'/cancel', headers=auth(), json={'expected_job_id': None}).status_code == 200
    response = c.post('/api/routine-runs/'+run['id']+'/action', headers=auth(), json={'action': 'remove', 'expected_step': run['step']})
    assert response.status_code == 200, response.text
    with c.app.state.store.db() as db:
        assert db.execute('SELECT 1 FROM client_retirements WHERE client_id=?', (session['clientId'],)).fetchone()
        assert db.execute('SELECT token_hash FROM clients WHERE id=?', (session['clientId'],)).fetchone()[0] is None
    assert c.get('/api/job-reports/routine:'+run['id'], headers=auth()).status_code == 200
    assert c.post('/api/pxe/heartbeat', headers=native, json={'stage': 'waiting-for-input'}).status_code == 401
