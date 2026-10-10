"""Build-time packaging of pinned ShredOS and matching nwipe source files."""
from pathlib import Path
import argparse, gzip, hashlib, json, shutil, subprocess, tarfile, tempfile, urllib.request
ROOT = Path(__file__).resolve().parent
ISO_URL = 'https://github.com/PartialVolume/shredos.x86_64/releases/download/v2025.11_31_x86-64_0.42/shredos-2025.11_31_x86-64_v0.42_20260716.iso'
ISO_SHA = '8b731180f9c00f488fcf65dd25f624bfb8445f0096be58d735def4d469d830a6'
ISO_SIZE = 360710144

def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()

def fetch(url, path, expected, size=None, cache=None):
    cached = cache / path.name if cache else None
    if cached and cached.is_file():
        shutil.copyfile(cached, path)
    else:
        with urllib.request.urlopen(url, timeout=90) as response, path.open('xb') as output:
            total = 0
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if size and total > size:
                    raise ValueError('Download exceeds pinned size: ' + path.name)
                output.write(chunk)
    if (size and path.stat().st_size != size) or digest(path) != expected:
        raise ValueError('Pinned asset integrity differs: ' + path.name)

def prepare(assets, cache=None):
    nwipe = assets / 'nwipe'
    manifest = json.loads((nwipe / 'manifest.json').read_text())
    if digest(nwipe / 'bundle.cpio.gz') != manifest['bundle_sha256']:
        raise ValueError('Native nwipe bundle integrity differs')
    with tempfile.TemporaryDirectory(prefix='re-pxe-builtin-') as name:
        work = Path(name)
        iso = work / 'upstream.iso'
        fetch(ISO_URL, iso, ISO_SHA, ISO_SIZE, cache)
        shredos = assets / 'shredos'
        shredos.mkdir(exist_ok=True)
        with (shredos / 'shredos').open('wb') as output:
            subprocess.run(['bsdtar', '-xOf', str(iso), 'boot/bzImage'], stdout=output, check=True)
        (shredos / 'manifest.json').write_text(json.dumps({'upstream_iso_sha256': ISO_SHA, 'sha256': digest(shredos / 'shredos'), 'size': (shredos / 'shredos').stat().st_size}, indent=2) + '\n')
        records = json.loads((nwipe / 'sources/debian-source-manifest.json').read_text())
        deps = work / 'dependency-sources'; deps.mkdir()
        for record in records.values():
            for entry in record['files']:
                fetch(entry['url'], deps / entry['name'], entry['sha256'], entry['size'], cache)
        with (nwipe / 'source.tar.gz').open('wb') as raw, gzip.GzipFile(fileobj=raw, mode='wb', filename='', mtime=0) as compressed, tarfile.open(fileobj=compressed, mode='w') as archive:
            for folder, prefix in [(nwipe / 'sources', 'nwipe-0.43-repxe1-sources'), (deps, 'nwipe-0.43-repxe1-sources/dependency-sources')]:
                for path in sorted(folder.rglob('*')):
                    if not path.is_file() or path.is_symlink():
                        continue
                    entry = tarfile.TarInfo(prefix + '/' + path.relative_to(folder).as_posix())
                    entry.size = path.stat().st_size; entry.mode = 0o644; entry.mtime = 0
                    with path.open('rb') as stream:
                        archive.addfile(entry, stream)
        manifest.update(source_sha256=digest(nwipe / 'source.tar.gz'), source_bytes=(nwipe / 'source.tar.gz').stat().st_size)
        (nwipe / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print('Built-in ShredOS and full corresponding nwipe source prepared.')

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--assets', type=Path, default=ROOT / 'repxe/assets')
    parser.add_argument('--cache', type=Path, help='Optional verified build download cache')
    args = parser.parse_args()
    prepare(args.assets, args.cache)
