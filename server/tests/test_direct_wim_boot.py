"""Ticket-scoped direct WIM boot uses regular uploaded fixtures; no VM or disk I/O."""
import hashlib
import re

import pytest
from repxe import wimboot_assets
from test_api import auth
from test_pxe import lab
from test_routines import make, select


def direct(lab):
    c, root, profile = lab
    (root/'boot/pxe/current.json').unlink()
    uid = make(c, profile)
    select(c)
    response = c.get('/pxe/managed.ipxe', params={'mac': '52:54:00:12:34:56'})
    assert response.status_code == 200, response.text
    ticket = re.search('/pxe/config/([A-Za-z0-9_-]+)', response.text)[1]
    return c, root, profile, uid, ticket, response.text


def test_missing_pxe_bundle_attempts_the_exact_preset_wim(lab):
    c, root, profile, uid, ticket, script = direct(lab)
    assert 'kernel --name wimboot http://127.0.0.1:8089/pxe/routines/wimboot quiet index=1' in script
    assert f'initrd --name boot.wim http://127.0.0.1:8089/pxe/routines/image/{ticket} boot.wim' in script
    assert '/BCD' not in script and '/boot.sdi' not in script and 'initrd --name bootx64.efi' not in script
    assert script.endswith('boot\n')
    response = c.get('/pxe/routines/image/'+ticket)
    assert response.status_code == 200 and response.content == b'profile wim fixture'
    assert response.headers['cache-control'] == 'no-store'
    partial = c.get('/pxe/routines/image/'+ticket, headers={'Range': 'bytes=0-6'})
    assert partial.status_code == 206 and partial.content == b'profile'
    assert c.get('/pxe/routines/image/'+'x'*48).status_code == 401
    assert c.get('/api/routines',headers=auth()).json()['runs'][0]['stage'] == 'windows-boot'


def test_direct_wim_ticket_is_revoked_on_cancellation(lab):
    c, root, profile, uid, ticket, script = direct(lab)
    run = c.get('/api/routines',headers=auth()).json()['runs'][0]
    response = c.post('/api/routine-runs/'+run['id']+'/action',headers=auth(),json={'action':'cancel','expected_step':run['step']})
    assert response.status_code == 200, response.text
    assert c.get('/pxe/routines/image/'+ticket).status_code in (401,409)


def test_direct_wim_rechecks_profile_withdrawal(lab):
    c, root, profile, uid, ticket, script = direct(lab)
    c.post('/api/profiles/'+profile['id']+'/availability',headers=auth(),json={'enabled':False})
    assert c.get('/pxe/routines/image/'+ticket).status_code == 409


def test_nonroutine_boot_does_not_serve_an_arbitrary_image(lab):
    c, root, profile = lab
    (root/'boot/pxe/current.json').unlink()
    assert c.get('/pxe/managed.ipxe',params={'mac':'52:54:00:12:34:56'}).status_code == 503


def test_pinned_wimboot_and_corresponding_source_are_served(lab):
    c, root, profile = lab
    # The pinned public build asset was prepared locally; no Windows media is used.
    if not (wimboot_assets.ASSETS/'wimboot').is_file():
        pytest.skip('Run python -m repxe.wimboot_assets before asset integration checks')
    response = c.get('/pxe/routines/wimboot')
    assert response.status_code == 200 and hashlib.sha256(response.content).hexdigest() == wimboot_assets.BINARY_SHA256
    response = c.get('/pxe/routines/wimboot/wimboot-source.tar.gz')
    assert response.status_code == 200 and hashlib.sha256(response.content).hexdigest() == wimboot_assets.SOURCE_SHA256
    assert c.get('/pxe/routines/wimboot/COPYING-wimboot').status_code == 200
    assert c.get('/pxe/routines/wimboot/not-allowed').status_code == 404
