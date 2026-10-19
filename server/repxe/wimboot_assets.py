"""Pinned public wimboot assets, packaged at build time without Microsoft media."""
import hashlib
import json
from pathlib import Path
import tarfile
import tempfile
import urllib.request

REVISION = 'e5875648bd52a3b915ae54933d9adf8ac763baca'
BINARY_SHA256 = '5f067ccdc4d084d5bf77b6c853bd0f8402dfc2b4cd1b103d358993ae97fae8e3'
SOURCE_SHA256 = '7c1f0216ecf66e5a2e6afe0bcfa01bca82399a7c749e5a44f3495083a6b8b54d'
BINARY_URL = 'https://github.com/ipxe/wimboot/releases/download/v2.9.0/wimboot'
SOURCE_URL = 'https://codeload.github.com/ipxe/wimboot/tar.gz/' + REVISION
ASSETS = Path(__file__).with_name('assets') / 'wimboot'


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def prepare(assets=ASSETS):
    assets.mkdir(parents=True, exist_ok=True)
    for name, url, expected in (('wimboot', BINARY_URL, BINARY_SHA256),
                                ('wimboot-source.tar.gz', SOURCE_URL, SOURCE_SHA256)):
        target = assets / name
        if target.is_file() and digest(target) == expected:
            continue
        with tempfile.NamedTemporaryFile(dir=assets, prefix=name+'.', suffix='.download', delete=False) as output:
            pending = Path(output.name)
            with urllib.request.urlopen(url, timeout=90) as response:
                output.write(response.read(2 * 1024 * 1024))
        if digest(pending) != expected:
            raise ValueError('Pinned wimboot asset hash differs; download retained')
        pending.chmod(0o644)
        pending.replace(target)
    with tarfile.open(assets / 'wimboot-source.tar.gz') as archive:
        with archive.extractfile('wimboot-' + REVISION + '/LICENSE.txt') as source:
            (assets / 'COPYING-wimboot').write_bytes(source.read())
    (assets / 'manifest.json').write_text(json.dumps({
        'version': '2.9.0', 'sourceRevision': REVISION,
        'sourceUrl': SOURCE_URL, 'binaryUrl': BINARY_URL,
        'binarySha256': BINARY_SHA256, 'sourceSha256': SOURCE_SHA256,
        'license': 'GPL-2.0-or-later'}, indent=2)+'\n')


if __name__ == '__main__':
    prepare()
