import gzip, hashlib, json, ssl, threading
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from repxe.app import create_app
from repxe import capabilities, workflows
from test_api import TOKENS, auth

@pytest.fixture
def builtin(tmp_path, monkeypatch):
    assets = tmp_path/'assets/shredos'; assets.mkdir(parents=True)
    payload=b'fixture kernel'
    (assets/'shredos').write_bytes(payload)
    (assets/'manifest.json').write_text(json.dumps({'upstream_iso_sha256':capabilities.DOWNLOAD_SHA,'size':len(payload),'sha256':hashlib.sha256(payload).hexdigest()}))
    monkeypatch.setattr(capabilities,'__file__',str(tmp_path/'capabilities.py'))
    monkeypatch.setattr(capabilities,'nwipe_bundle',lambda: ({'engine_version':workflows.erase_policy.NATIVE_VERSION,'binary_sha256':'b'*64,'source_sha256':hashlib.sha256(b'source').hexdigest()},b'native bundle',b'source'))
    monkeypatch.setattr(capabilities,'companion',lambda origin,ca: b'overlay:'+origin.encode()+ca.encode())
    monkeypatch.setattr(capabilities,'start_builtin',lambda store: threading.Event())
    monkeypatch.setenv('REPXE_ADDRESS','re-pxe.example')
    monkeypatch.delenv('REPXE_CA_FILE',raising=False)
    return assets

def public_ca():
    pem=Path(ssl.get_default_verify_paths().cafile).read_text()
    return pem[:pem.index('-----END CERTIFICATE-----')+len('-----END CERTIFICATE-----')]

def trust(store):
    path=store.root/'config/server-public-ca.crt';path.parent.mkdir(exist_ok=True);path.write_text(public_ca())

def test_fresh_install_waits_for_gateway_then_prepares_without_operator_or_network(tmp_path,builtin):
    with TestClient(create_app(tmp_path/'data',TOKENS)) as c:
        store=c.app.state.store
        assert capabilities.prepare_builtin(store) is False
        trust(store)
        assert capabilities.prepare_builtin(store) is True
        current=workflows.media(store)
        folder=store.root/'boot/pxe/shredos'/current['release']
        assert (folder/'shredos').read_bytes()==b'fixture kernel'
        assert public_ca().encode() in (folder/'companion.cpio.gz').read_bytes()
        assert current['builtin_identity']
        assert c.get('/api/capabilities',headers=auth()).json()[0]['included'] is True
        assert c.post('/api/capabilities/shredos/install',headers=auth(),json={}).status_code==404
        assert capabilities.prepare_builtin(store) is True
        assert workflows.media(store)['release']==current['release']
        assert len(list((store.root/'capabilities/shredos').iterdir()))==1
    with TestClient(create_app(tmp_path/'data',TOKENS)) as c:
        assert capabilities.prepare_builtin(c.app.state.store) is True
        assert workflows.media(c.app.state.store)['release']==current['release']

def test_corrupt_builtin_never_selected(tmp_path,builtin):
    (builtin/'shredos').write_bytes(b'bad')
    with TestClient(create_app(tmp_path/'data',TOKENS)) as c:
        store=c.app.state.store;trust(store)
        assert capabilities.prepare_builtin(store) is False
        assert not (store.root/'boot/pxe/shredos/current.json').exists()
        assert capabilities.state(store)['status']=='failed'

@pytest.mark.parametrize('content',['invalid','-----BEGIN PRIVATE KEY-----'])
def test_private_or_invalid_ca_rejected(tmp_path,builtin,content):
    with TestClient(create_app(tmp_path/'data',TOKENS)) as c:
        store=c.app.state.store;trust(store)
        (store.root/'config/server-public-ca.crt').write_text(content)
        with pytest.raises((ValueError,ssl.SSLError)):capabilities.prepare_builtin(store)
        assert not (store.root/'boot/pxe/shredos/current.json').exists()

def test_symlink_ca_not_read(tmp_path,builtin):
    with TestClient(create_app(tmp_path/'data',TOKENS)) as c:
        store=c.app.state.store;store.root.joinpath('config').mkdir(exist_ok=True)
        (store.root/'config/server-public-ca.crt').symlink_to(tmp_path/'private.pem')
        assert capabilities.prepare_builtin(store) is False

def test_corrupt_published_release_retained_and_new_candidate_selected(tmp_path,builtin):
    with TestClient(create_app(tmp_path/'data',TOKENS)) as c:
        store=c.app.state.store;trust(store);assert capabilities.prepare_builtin(store)
        old=workflows.media(store);folder=store.root/'boot/pxe/shredos'/old['release'];(folder/'shredos').write_bytes(b'corrupted')
        assert capabilities.prepare_builtin(store)
        assert workflows.media(store)['release']!=old['release']
        assert (folder/'shredos').read_bytes()==b'corrupted'
