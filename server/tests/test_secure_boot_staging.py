"""Offline signed asset publication checks; these do not claim firmware validation."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile

import pytest

tool = Path(__file__).resolve().parents[2]/'tools/stage_secure_boot.py'
spec = importlib.util.spec_from_file_location('stage_secure_boot_test', tool)
staging = importlib.util.module_from_spec(spec)
spec.loader.exec_module(staging)


def archive(name, data):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w:gz') as tar:
        entry = tarfile.TarInfo(name)
        entry.size = len(data)
        tar.addfile(entry, io.BytesIO(data))
    return output.getvalue()


def fixture(root, monkeypatch):
    base = root/'data/boot/pxe/0.14.1'
    base.mkdir(parents=True)
    files = {}
    for name in staging.WINDOWS_FILES:
        data = b'fixture '+name.encode()
        (base/name).write_bytes(data)
        files[name] = {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
    monkeypatch.setattr(staging, 'WIMBOOT_SHA256', files['wimboot']['sha256'])
    manifest = {'version': '0.14.1', 'directory': '0.14.1', 'architecture': 'amd64', 'clientSha256': 'a'*64, 'files': files}
    (base.parent/'current.json').write_text(json.dumps(manifest))
    (base.parent/'snponly.efi').write_bytes(b'working normal loader')
    lock = json.loads(staging.LOCK.read_text())
    assets = {
        lock['ipxe']['archive']['url']: archive('ipxeboot/x86_64-sb/snponly.efi', b'production-signed loader fixture'),
        lock['ipxe']['source']['url']: archive('ipxe-'+lock['ipxe']['revision']+'/COPYING', b'iPXE license'),
        lock['shim']['binary']['url']: b'Microsoft-signed shim fixture',
        lock['shim']['source']['url']: archive('shim-'+lock['shim']['revision']+'/COPYRIGHT', b'shim license'),
    }
    monkeypatch.setattr(staging, 'fetch', lambda item, cache: assets[item['url']])
    return base.parent


def test_signed_staging_is_immutable_and_selection_never_changes_regular_media(tmp_path, monkeypatch):
    base = fixture(tmp_path, monkeypatch)
    normal = (base/'current.json').read_bytes()
    release = staging.stage(tmp_path, '10.10.90.14')
    assert not (base/'secureboot/current.json').exists()
    selected = staging.stage(tmp_path, '10.10.90.14', select=True)
    assert selected == release
    assert not selected['secureBootValidated'] and selected['scope'] == 'windows-only'
    assert (base/'snponly.efi').read_bytes() == b'working normal loader'
    assert (base/'current.json').read_bytes() == normal
    folder = base/'secureboot'/release['directory']
    assert b'netX/mac' in (folder/'autoexec.ipxe').read_bytes()
    assert b'windows_only=1' in (folder/'autoexec.ipxe').read_bytes()
    assert (folder/'COPYING-ipxe').read_bytes() == b'iPXE license'
    assert (folder/'COPYRIGHT-shim').read_bytes() == b'shim license'
    before = (base/'secureboot/current.json').read_bytes()
    (folder/'snponly.efi').write_bytes(b'changed')
    with pytest.raises(ValueError, match='Existing signed candidate differs'):
        staging.stage(tmp_path, '10.10.90.14', select=True)
    assert (base/'secureboot/current.json').read_bytes() == before


def test_damaged_windows_media_never_selects_a_signed_candidate(tmp_path, monkeypatch):
    base = fixture(tmp_path, monkeypatch)
    (base/'0.14.1/boot.wim').write_bytes(b'wrong bytes')
    with pytest.raises(ValueError, match='Windows media differs'):
        staging.stage(tmp_path, '10.10.90.14', select=True)
    assert not (base/'secureboot/current.json').exists()


def test_cache_hash_mismatch_is_not_used_or_silently_replaced(tmp_path):
    data = b'pinned'
    digest = hashlib.sha256(data).hexdigest()
    (tmp_path/digest).write_bytes(b'tampered')
    with pytest.raises(ValueError, match='Cached signed asset differs'):
        staging.fetch({'sha256': digest, 'size': len(data), 'url': 'https://example.invalid'}, tmp_path)


def test_archive_member_and_bootstrap_are_bounded():
    assert staging.member(archive('fixed', b'data'), 'fixed') == b'data'
    with pytest.raises(ValueError): staging.member(archive('fixed', b'data'), 'fixed', limit=2)
    with pytest.raises(ValueError): staging.bootstrap('server\nboot malicious')
    with pytest.raises(ValueError): staging.bootstrap('http://server:8089')
