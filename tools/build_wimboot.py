#!/usr/bin/env python3
"""Stage pinned upstream wimboot and its corresponding GPL source archive."""
import hashlib
import json
import lzma
from pathlib import Path
import subprocess
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / '.local/pxe'
REVISION = 'e5875648bd52a3b915ae54933d9adf8ac763baca'
BINARY_SHA256 = '5f067ccdc4d084d5bf77b6c853bd0f8402dfc2b4cd1b103d358993ae97fae8e3'
BINARY_URL = 'https://github.com/ipxe/wimboot/releases/download/v2.9.0/wimboot'


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    WORK.mkdir(parents=True, exist_ok=True)
    source = WORK / 'wimboot-build-v2.9.0'
    if not source.exists():
        subprocess.run(['git', 'clone', '--depth', '1', '--branch', 'v2.9.0',
                        'https://github.com/ipxe/wimboot.git', str(source)], check=True)
    actual = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    if actual != REVISION:
        raise RuntimeError(f'Unexpected wimboot source revision: {actual}')
    # Archive the pinned commit, regardless of private diagnostic build changes.
    archive = subprocess.check_output(['git', '-C', str(source), 'archive', '--format=tar', REVISION])
    source_archive = WORK / 'wimboot-source.tar.xz'
    source_archive.write_bytes(lzma.compress(archive, preset=9))
    (WORK / 'COPYING-wimboot').write_bytes(subprocess.check_output(
        ['git', '-C', str(source), 'show', REVISION + ':LICENSE.txt']))
    pending = WORK / 'wimboot.download'
    with urllib.request.urlopen(BINARY_URL, timeout=30) as response:
        pending.write_bytes(response.read(1024 * 1024))
    if digest(pending) != BINARY_SHA256:
        raise RuntimeError('Upstream wimboot SHA-256 differs from the pinned release')
    pending.replace(WORK / 'wimboot')
    manifest = {
        'version': 'v2.9.0', 'sourceRevision': REVISION,
        'sourceUrl': 'https://github.com/ipxe/wimboot', 'license': 'GPL-2.0-or-later',
        'modified': False, 'sourceArchive': source_archive.name,
        'sourceArchiveSha256': digest(source_archive),
        'vendorBinaryUrl': BINARY_URL, 'vendorBinarySha256': BINARY_SHA256,
        'binarySha256': BINARY_SHA256,
    }
    (WORK / 'wimboot-source.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print('wimboot SHA-256:', BINARY_SHA256)
    print('wimboot source archive SHA-256:', manifest['sourceArchiveSha256'])


if __name__ == '__main__':
    main()
