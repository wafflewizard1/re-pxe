#!/usr/bin/env python3
"""Stage an immutable Windows-only signed PXE entry, preserving the regular loader."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import tarfile
import tempfile
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
LOCK = Path(__file__).with_name('secure_boot_assets.json')
WINDOWS_FILES = ('boot.wim', 'BCD', 'boot.sdi', 'bootx64.efi', 'wimboot')
WIMBOOT_SHA256 = '5f067ccdc4d084d5bf77b6c853bd0f8402dfc2b4cd1b103d358993ae97fae8e3'


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def fetch(spec, cache):
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / spec['sha256']
    if target.exists():
        if target.stat().st_size != spec['size'] or sha(target) != spec['sha256']:
            raise ValueError('Cached signed asset differs from its pin')
        return target.read_bytes()
    with tempfile.NamedTemporaryFile(dir=cache, delete=False, prefix='download-') as output:
        pending = Path(output.name)
        try:
            with urllib.request.urlopen(spec['url'], timeout=90) as response:
                remaining = spec['size'] + 1
                while remaining:
                    chunk = response.read(min(1024 * 1024, remaining))
                    if not chunk: break
                    output.write(chunk)
                    remaining -= len(chunk)
        except BaseException:
            output.close()
            pending.unlink(missing_ok=True)
            raise
    if pending.stat().st_size != spec['size'] or sha(pending) != spec['sha256']:
        pending.unlink()
        raise ValueError('Downloaded signed asset differs from its pin')
    pending.replace(target)
    return target.read_bytes()


def member(data, name, limit=4*1024*1024):
    # Read exact archive members without extracting paths, links or executables.
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as archive:
        entry = archive.getmember(name)
        if not entry.isfile() or entry.size <= 0 or entry.size > limit:
            raise ValueError('Invalid signed archive member')
        return archive.extractfile(entry).read()


def bootstrap(address):
    if not re.fullmatch(r'[A-Za-z0-9.-]+', address): raise ValueError('Use a LAN address without a scheme or port')
    return (f'#!ipxe\nifopen || goto failed\ndhcp || goto failed\n'
            f'chain --replace --autofree http://{address}:8089/pxe/boot.ipxe?mac=${{netX/mac}}&windows_only=1 || goto failed\n'
            'exit\n:failed\necho re-pxe Windows-only Secure Boot could not start.\n'
            'prompt Press a key to return to firmware.\nexit\n').encode()


def windows_media(root):
    base = root / 'data/boot/pxe'
    media = json.loads((base / 'current.json').read_text())
    directory = media.get('directory', media['version'])
    if not re.fullmatch(r'(candidate-)?[0-9]+\.[0-9]+\.[0-9]+(-[a-f0-9]{12})?', directory):
        raise ValueError('Invalid Windows media directory')
    if media['architecture'] != 'amd64': raise ValueError('AMD64 managed Windows media is required')
    files = {name: media['files'][name] for name in WINDOWS_FILES}
    for name, expected in files.items():
        path = base / directory / name
        if path.stat().st_size != expected['size'] or sha(path) != expected['sha256']:
            raise ValueError('Published Windows media differs: ' + name)
    if files['wimboot']['sha256'] != WIMBOOT_SHA256:
        raise ValueError('Publish the pinned signed wimboot release first')
    return {'version': media['version'], 'directory': directory, 'clientSha256': media['clientSha256'], 'files': files}


def stage(root, address, cache=None, select=False):
    root = Path(root)
    cache = Path(cache) if cache else root / '.local/secureboot-cache'
    lock = json.loads(LOCK.read_text())
    windows = windows_media(root)
    ipxe = fetch(lock['ipxe']['archive'], cache)
    ipxe_source = fetch(lock['ipxe']['source'], cache)
    shim = fetch(lock['shim']['binary'], cache)
    shim_source = fetch(lock['shim']['source'], cache)
    files = {
        'snponly.efi': member(ipxe, 'ipxeboot/x86_64-sb/snponly.efi'),
        'snponly-shim.efi': shim,
        'autoexec.ipxe': bootstrap(address),
        'ipxe-source.tar.gz': ipxe_source,
        'shim-source.tar.gz': shim_source,
        'COPYING-ipxe': member(ipxe_source, 'ipxe-' + lock['ipxe']['revision'] + '/COPYING'),
        'COPYRIGHT-shim': member(shim_source, 'shim-' + lock['shim']['revision'] + '/COPYRIGHT'),
        'vendor-assets.json': (json.dumps(lock, indent=2) + '\n').encode(),
    }
    metadata = {name: {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()} for name, data in files.items()}
    identity = hashlib.sha256(json.dumps({'files': metadata, 'windows': windows}, sort_keys=True).encode()).hexdigest()[:12]
    directory = 'ipxe-2.0.0-shim-16.1-' + identity
    release = {'schemaVersion': 1, 'scope': 'windows-only', 'architecture': 'amd64', 'directory': directory,
               'secureBootValidated': False, 'windowsMedia': windows, 'files': metadata,
               'upstream': {'ipxe': lock['ipxe']['version'], 'shim': lock['shim']['version']}}
    base = root / 'data/boot/pxe/secureboot'
    base.mkdir(parents=True, exist_ok=True)
    destination = base / directory
    if destination.exists():
        if json.loads((destination/'release.json').read_text()) != release:
            raise ValueError('Signed candidate already exists with different metadata')
        for name, item in metadata.items():
            if sha(destination/name) != item['sha256']: raise ValueError('Existing signed candidate differs: ' + name)
    else:
        with tempfile.TemporaryDirectory(dir=base, prefix='stage-') as work:
            pending = Path(work) / directory
            pending.mkdir(mode=0o755)
            for name, data in files.items():
                (pending/name).write_bytes(data)
                (pending/name).chmod(0o644)
            (pending/'release.json').write_text(json.dumps(release, indent=2)+'\n')
            (pending/'release.json').chmod(0o644)
            pending.rename(destination)
    if select:
        pending = base / ('current-' + uuid.uuid4().hex + '.tmp')
        try:
            pending.write_text(json.dumps(release, indent=2)+'\n')
            pending.chmod(0o644)
            pending.replace(base/'current.json')
        finally:
            pending.unlink(missing_ok=True)
    return release


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--address', required=True)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--cache', type=Path)
    parser.add_argument('--select', action='store_true', help='Select this candidate for the separate signed entry; DHCP and normal media are unchanged')
    args = parser.parse_args()
    result = stage(args.root, args.address, args.cache, args.select)
    print('Windows-only signed candidate:', result['directory'])
    print('Test DHCP filename: pxe/secureboot/' + result['directory'] + '/snponly-shim.efi')
    print('Candidate endpoint selected:', args.select)
    print('Regular DHCP filename and PXE media are unchanged. Hardware testing is still required.')


if __name__ == '__main__':
    main()
