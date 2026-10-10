"""Stable DHCP entry point: safe publication, replacement and failure preservation."""
import hashlib
import importlib.util
import json
from pathlib import Path
import shlex
import shutil

import pytest

module_path = next(parent / 'tools/publish_pxe.py' for parent in Path(__file__).resolve().parents
                   if (parent / 'tools/publish_pxe.py').is_file())
spec = importlib.util.spec_from_file_location('pxe_publication', module_path)
publication = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publication)


class Files:
    def open(self, path, mode):
        return open(path, mode)

    def chmod(self, path, mode):
        Path(path).chmod(mode)

    def posix_rename(self, source, target):
        Path(source).replace(target)

    def remove(self, path):
        Path(path).unlink()


def command(cmd):
    parts = shlex.split(cmd)
    if parts[0] == 'cp':
        shutil.copyfile(parts[-2], parts[-1])
        return ''
    if parts[0] == 'chmod':
        Path(parts[-1]).chmod(int(parts[1], 8))
        return ''
    assert parts[0] == 'sha256sum'
    return hashlib.sha256(Path(parts[1]).read_bytes()).hexdigest() + ' ' + parts[1]


def fixture(root, version, data):
    target = root / version
    target.mkdir()
    (target / 'snponly.efi').write_bytes(data)
    manifest = {'version': version, 'directory': version,
                'files': {'snponly.efi': {'sha256': hashlib.sha256(data).hexdigest()}},
                'dhcp': {'server': 'server', 'filename': f'pxe/{version}/snponly.efi'}}
    (target / 'release.json').write_text(json.dumps(manifest))
    return target, manifest


def test_master_loader_replaces_regular_file_and_preserves_versioned_releases(tmp_path):
    first, first_manifest = fixture(tmp_path, '1.0.0', b'first loader')
    publication.select_media(Files(), command, str(first), first_manifest)
    assert (tmp_path / 'snponly.efi').read_bytes() == b'first loader'
    assert not (tmp_path / 'snponly.efi').is_symlink()
    assert json.loads((tmp_path / 'current.json').read_text())['dhcp']['filename'] == 'pxe/snponly.efi'
    second, second_manifest = fixture(tmp_path, '1.0.1', b'updated loader')
    publication.select_media(Files(), command, str(second), second_manifest)
    assert (tmp_path / 'snponly.efi').read_bytes() == b'updated loader'
    assert (first / 'snponly.efi').read_bytes() == b'first loader'
    assert json.loads((first / 'release.json').read_text()) == first_manifest
    assert json.loads((tmp_path / 'current.json').read_text())['version'] == '1.0.1'
    assert not list(tmp_path.glob('*.tmp'))


def test_failed_loader_verification_preserves_master_and_current(tmp_path):
    first, manifest = fixture(tmp_path, '1.0.0', b'working loader')
    publication.select_media(Files(), command, str(first), manifest)
    current = (tmp_path / 'current.json').read_bytes()
    second, bad = fixture(tmp_path, '1.0.1', b'new loader')
    bad['files']['snponly.efi']['sha256'] = '0' * 64
    with pytest.raises(RuntimeError, match='verification failed'):
        publication.select_media(Files(), command, str(second), bad)
    assert (tmp_path / 'snponly.efi').read_bytes() == b'working loader'
    assert (tmp_path / 'current.json').read_bytes() == current
    assert (second / 'snponly.efi').read_bytes() == b'new loader'
    assert not list(tmp_path.glob('*.tmp'))
