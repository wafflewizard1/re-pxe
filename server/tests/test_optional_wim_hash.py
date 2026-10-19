"""Optional expected WIM hashes still produce immutable hashed publications."""
import hashlib

from fastapi.testclient import TestClient
import pytest

from repxe.app import create_app
from test_api import auth, client, publish, send, TOKENS


def image_spec(payload):
    return {'kind': 'image', 'name': 'Windows install.wim', 'filename': 'install.wim',
            'size': len(payload), 'image_index': 1,
            'reference_preparation': 'Disposable offline generalized reference'}


@pytest.mark.parametrize('supplied', ['omitted', None, '', '   '])
def test_optional_wim_hash_is_calculated_and_used_by_published_image(client, monkeypatch, supplied):
    monkeypatch.setattr('repxe.app.verify_wim', lambda path, spec: {'selected_image': {'build': '26100'}})
    payload = b'disposable WIM fixture'
    spec = image_spec(payload)
    if supplied != 'omitted':
        spec['sha256'] = supplied
    created = client.post('/api/uploads', headers=auth(), json=spec)
    assert created.status_code == 201, created.text
    uid = created.json()['id']
    assert created.json()['spec']['sha256'] is None
    assert send(client, uid, payload).status_code == 200
    result = client.post(f'/api/uploads/{uid}/finish', headers=auth())
    assert result.status_code == 200, result.text
    actual = hashlib.sha256(payload).hexdigest()
    assert result.json()['status'] == 'ready' and result.json()['spec']['sha256'] == actual
    assert client.post(f'/api/uploads/{uid}/finish', headers=auth()).json()['spec']['sha256'] == actual
    assert publish(client, uid).status_code == 200
    image = client.get('/api/images', headers=auth()).json()[0]
    assert image['sha256'] == actual
    assert client.get(f'/api/uploads/{uid}', headers=auth()).json()['spec']['sha256'] == actual


def test_optional_hash_does_not_skip_wim_validation(client, monkeypatch):
    def invalid(path, spec):
        raise ValueError('Invalid WIM image metadata')
    monkeypatch.setattr('repxe.app.verify_wim', invalid)
    payload = b'invalid WIM fixture'
    uid = client.post('/api/uploads', headers=auth(), json=image_spec(payload)).json()['id']
    assert send(client, uid, payload).status_code == 200
    result = client.post(f'/api/uploads/{uid}/finish', headers=auth())
    assert result.status_code == 422 and 'WIM image metadata' in result.json()['detail']
    assert publish(client, uid).status_code == 409
    assert client.get('/api/images', headers=auth()).json() == []


def test_supplied_wim_hash_must_match(client, monkeypatch):
    calls = []
    monkeypatch.setattr('repxe.app.verify_wim', lambda *args: calls.append(args))
    payload = b'WIM fixture with wrong expected digest'
    spec = {**image_spec(payload), 'sha256': '0'*64}
    uid = client.post('/api/uploads', headers=auth(), json=spec).json()['id']
    assert send(client, uid, payload).status_code == 200
    result = client.post(f'/api/uploads/{uid}/finish', headers=auth())
    assert result.status_code == 422 and 'SHA-256 does not match' in result.json()['detail']
    assert calls == [] and publish(client, uid).status_code == 409


@pytest.mark.parametrize('sha', ['not-a-hash', 'a'*63, 'g'*64])
def test_optional_does_not_accept_malformed_hashes(client, sha):
    assert client.post('/api/uploads', headers=auth(), json={**image_spec(b'wim'), 'sha256': sha}).status_code == 422


@pytest.mark.parametrize('supplied', ['omitted', None, ''])
def test_driver_hash_remains_required(client, supplied):
    spec = {**image_spec(b'zip fixture'), 'kind': 'drivers', 'filename': 'drivers.zip'}
    if supplied != 'omitted':
        spec['sha256'] = supplied
    result = client.post('/api/uploads', headers=auth(), json=spec)
    assert result.status_code == 422 and 'Driver uploads require a SHA-256 hash' in result.text


def test_unhashed_wim_upload_keeps_offsets_across_restart(tmp_path, monkeypatch):
    monkeypatch.setattr('repxe.app.verify_wim', lambda path, spec: {'selected_image': {'build': '26100'}})
    payload = b'disposable WIM restart fixture'
    with TestClient(create_app(tmp_path, TOKENS)) as c:
        uid = c.post('/api/uploads', headers=auth(), json=image_spec(payload)).json()['id']
        assert send(c, uid, payload[:8]).status_code == 200
        assert c.post(f'/api/uploads/{uid}/finish', headers=auth()).status_code == 409
    with TestClient(create_app(tmp_path, TOKENS)) as c:
        assert c.get(f'/api/uploads/{uid}', headers=auth()).json()['spec']['sha256'] is None
        assert send(c, uid, payload[8:], offset=8).status_code == 200
        result = c.post(f'/api/uploads/{uid}/finish', headers=auth())
        assert result.status_code == 200 and result.json()['spec']['sha256'] == hashlib.sha256(payload).hexdigest()
