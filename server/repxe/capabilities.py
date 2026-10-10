"""Publish the built-in ShredOS assets into immutable, trusted boot releases."""
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import ssl
import stat
import threading
import time
import uuid

from fastapi import HTTPException
from . import workflows

TAG = 'v2025.11_31_x86-64_0.42'
ISO_NAME = 'shredos-2025.11_31_x86-64_v0.42_20260716.iso'
DOWNLOAD_URL = f'https://github.com/PartialVolume/shredos.x86_64/releases/download/{TAG}/{ISO_NAME}'
DOWNLOAD_SHA = '8b731180f9c00f488fcf65dd25f624bfb8445f0096be58d735def4d469d830a6'
DOWNLOAD_SIZE = 360710144
SOURCE_URL = f'https://github.com/PartialVolume/shredos.x86_64/tree/{TAG}'


def initialize(store):
    with store.db() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS optional_capabilities(
            id TEXT PRIMARY KEY,status TEXT NOT NULL,received INTEGER NOT NULL,total INTEGER NOT NULL,
            detail TEXT NOT NULL,updated TEXT NOT NULL)''')
        db.execute("UPDATE optional_capabilities SET status='failed',detail='Preparation interrupted by server restart; automatic preparation will retry' WHERE status='downloading'")


def append_cpio(entries):
    """A small newc initramfs, overlaid after ShredOS' embedded RAM filesystem."""
    archive = bytearray()
    def add(name, payload, mode, inode):
        name = name.encode() + b'\0'
        fields = [inode, mode, 0, 0, 1, 0, len(payload), 0, 0, 0, 0, len(name), 0]
        archive.extend(b'070701' + ''.join(f'{v:08x}' for v in fields).encode())
        archive.extend(name)
        archive.extend(b'\0' * (-len(archive) % 4))
        archive.extend(payload)
        archive.extend(b'\0' * (-len(archive) % 4))
    directories = set()
    for name, _, _ in entries:
        parent = Path(name).parent
        while str(parent) != '.':
            directories.add(parent.as_posix())
            parent = parent.parent
    inode = 1
    for name in sorted(directories, key=lambda x: (x.count('/'), x)):
        add(name, b'', stat.S_IFDIR | 0o755, inode)
        inode += 1
    for name, payload, mode in entries:
        add(name, payload, stat.S_IFREG | mode, inode)
        inode += 1
    add('TRAILER!!!', b'', 0, inode)
    return gzip.compress(bytes(archive), mtime=0)


NWIPE_BUNDLE_SHA = 'c3b52f66fc7c5d899266098d5ef61e66327f28110344fb4633dac5cacf185eff'


def nwipe_bundle():
    folder=Path(__file__).with_name('assets')/'nwipe'
    manifest=json.loads((folder/'manifest.json').read_text())
    bundle=(folder/'bundle.cpio.gz').read_bytes()
    source=(folder/'source.tar.gz').read_bytes()
    if manifest.get('engine_version')!=workflows.erase_policy.NATIVE_VERSION or hashlib.sha256(bundle).hexdigest()!=NWIPE_BUNDLE_SHA or hashlib.sha256(source).hexdigest()!=manifest['source_sha256']:
        raise ValueError('Pinned nwipe sanitize bundle integrity differs')
    return manifest,bundle,source


def companion(origin, ca):
    launcher = b'''#!/bin/sh
# No upstream interactive/autonuke fallback: run only an explicitly armed job.
/usr/bin/python3 /usr/bin/re-pxe-erase.py
printf '\\nre-pxe finished or stopped. Inspect Workflows and this console.\\n'
while :; do sleep 3600; done
'''
    network = b'''#!/bin/sh
# The guarded companion rescans USB adapters and owns one renewing DHCP client.
# Upstream's initial NIC snapshot must not race that interface selection.
exit 0
'''
    dhcp = b'''#!/bin/sh
# BusyBox udhcpc lease hook. All state remains in the ShredOS RAM filesystem.
case "$1" in
  deconfig) /sbin/ifconfig "$interface" 0.0.0.0 ;;
  bound|renew)
    /sbin/ifconfig "$interface" "$ip" netmask "${subnet:-255.255.255.0}" || exit 1
    /sbin/route del default dev "$interface" 2>/dev/null || :
    for gateway in $router; do
      /sbin/route add default gw "$gateway" dev "$interface" || exit 1
      break
    done
    : > /etc/resolv.conf
    for server in $dns; do printf 'nameserver %s\\n' "$server" >> /etc/resolv.conf; done
    ;;
esac
exit 0
'''
    manifest,bundle,source=nwipe_bundle()
    return append_cpio([
        ('etc/nwipe/.re-pxe', b're-pxe companion initializes this configuration directory.\n', 0o644),
        ('etc/re-pxe-shredos-release', (workflows.MEDIA_MARKER + '\n').encode(), 0o644),
        ('etc/re-pxe-server.json', json.dumps({'origin': origin, 'time_floor': int(time.time()) - 300}).encode(), 0o644),
        ('etc/re-pxe-ca.crt', ca.encode(), 0o644),
        ('usr/bin/re-pxe-erase.py', Path(__file__).with_name('shredos_agent.py').read_bytes(), 0o755),
        ('usr/bin/erase_policy.py', Path(__file__).with_name('erase_policy.py').read_bytes(), 0o644),
        ('usr/bin/smart_health.py', Path(__file__).with_name('smart_health.py').read_bytes(), 0o644),
        ('usr/bin/nwipe_launcher', launcher, 0o755),
        ('usr/bin/shredos_net.sh', network, 0o755),
        ('usr/bin/re-pxe-dhcp', dhcp, 0o755),
    ])+bundle


def state(store):
    with store.db() as db:
        row = db.execute("SELECT * FROM optional_capabilities WHERE id='shredos'").fetchone()
    value = dict(row) if row else {'id': 'shredos', 'status': 'not-installed', 'received': 0,
                                  'total': DOWNLOAD_SIZE, 'detail': '', 'updated': None}
    if value['status'] == 'installed':
        try:
            manifest = workflows.media(store)
            if manifest['agent_version'] != workflows.MEDIA_MARKER:
                value.update(status='update-available', detail='Update ShredOS for corrected wipe time estimates; existing RAM sessions keep their original companion')
        except HTTPException as exc:
            value.update(status='failed', detail=str(exc.detail) + '; automatic preparation will retry')
    return {**value, 'name': 'ShredOS disk erasure', 'nwipe_version':workflows.erase_policy.NATIVE_VERSION, 'version': TAG, 'download_bytes': DOWNLOAD_SIZE,
            'upstream_url': 'https://github.com/PartialVolume/shredos.x86_64',
            'download_url': DOWNLOAD_URL, 'source_url': SOURCE_URL,
            'included': True, 'description': 'Built-in ShredOS with inventory, certificates and Windows handoff.'}


def update(store, status, received, detail):
    with store.db() as db:
        db.execute('UPDATE optional_capabilities SET status=?,received=?,detail=?,updated=? WHERE id=\'shredos\'',
                   (status, received, detail, workflows.now()))


def publish_builtin(store, ca, origin, identity=None):
    stage = store.root / 'capabilities/shredos' / str(uuid.uuid4())
    try:
        stage.mkdir(parents=True, mode=0o700)
        assets = Path(__file__).with_name('assets') / 'shredos'
        bundled = json.loads((assets / 'manifest.json').read_text())
        kernel_payload = (assets / 'shredos').read_bytes()
        if bundled['upstream_iso_sha256'] != DOWNLOAD_SHA or len(kernel_payload) != bundled['size'] or hashlib.sha256(kernel_payload).hexdigest() != bundled['sha256']:
            raise ValueError('Built-in ShredOS kernel integrity check failed')
        received = DOWNLOAD_SIZE
        update(store, 'downloading', received, 'Preparing built-in ShredOS with this server’s public certificate')
        kernel = stage / 'shredos'
        kernel.write_bytes(kernel_payload)
        overlay = stage / 'companion.cpio.gz'
        overlay.write_bytes(companion(origin, ca))
        nwipe_manifest,bundle,source_payload=nwipe_bundle()
        source_archive=stage/'nwipe-source.tar.gz'
        source_archive.write_bytes(source_payload)
        files = {p.name: {'size': p.stat().st_size, 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()} for p in (kernel, overlay,source_archive)}
        release = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
        manifest = {'release': release, 'agent_version': workflows.MEDIA_MARKER,
                    'nwipe_version':nwipe_manifest['engine_version'],'nwipe_binary_sha256':nwipe_manifest['binary_sha256'], 'nwipe_source_sha256':nwipe_manifest['source_sha256'],
                    'upstream_version': TAG, 'upstream_commit': TAG, 'upstream_iso_sha256': DOWNLOAD_SHA,
                    'source_url': SOURCE_URL, 'upstream_download_url': DOWNLOAD_URL,
                    'validated': True, 'validation': 'Pinned upstream release and project-tested companion; model-specific acceptance is separate',
                    'files': files, 'created': workflows.now(), 'builtin_identity': identity}
        (stage / 'release.json').write_text(json.dumps(manifest, indent=2) + '\n')
        destination = store.root / 'boot/pxe/shredos' / release
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Keep the original ISO and failed candidates private as evidence.
        if destination.exists():
            same = not destination.is_symlink()
            for name, info in files.items():
                existing = destination / name
                same = same and not existing.is_symlink() and existing.is_file() and hashlib.sha256(existing.read_bytes()).hexdigest() == info['sha256']
            if not same:
                release = hashlib.sha256((release + stage.name).encode()).hexdigest()
                manifest['release'] = release
                (stage / 'release.json').write_text(json.dumps(manifest, indent=2) + '\n')
                destination = destination.parent / release
        if not destination.exists():
            destination.mkdir(mode=0o755)
            for p in (kernel, overlay,source_archive, stage / 'release.json'):
                shutil.copyfile(p, destination / p.name)
                (destination / p.name).chmod(0o644)
        from .app import atomic_json
        atomic_json(destination.parent / 'current.json', manifest)
        update(store, 'installed', received, 'Ready for routine use')
    except Exception as exc:
        detail = str(exc) if isinstance(exc, ValueError) else type(exc).__name__ + ': built-in preparation failed; candidate retained'
        update(store, 'failed', 0, detail[:1000])


def install(app, store, Role, allow):
    @app.get('/api/capabilities')
    def capabilities(role: Role):
        allow(role)
        return [state(store())]



def prepare_builtin(store):
    """No network access or disk execution. Existing RAM runs retain their URLs."""
    address = os.environ.get('REPXE_ADDRESS', '')
    if not re.fullmatch(r'[A-Za-z0-9.-]+', address):
        raise ValueError('Configure REPXE_ADDRESS for built-in boot media')
    ca_path = Path(os.environ.get('REPXE_CA_FILE', str(store.root / 'config/server-public-ca.crt')))
    if ca_path.is_symlink() or not ca_path.is_file():
        return False  # Gateway produces its public certificate after first startup.
    ca = ca_path.read_text().strip()
    if 'PRIVATE KEY' in ca:
        raise ValueError('Only a public CA certificate may enter boot media')
    ssl.create_default_context(cadata=ca)
    origin = f'https://{address}:8443'
    identity = hashlib.sha256((origin + ca + workflows.MEDIA_MARKER + NWIPE_BUNDLE_SHA).encode()).hexdigest()
    with store.lock('builtin-shredos'):
        try:
            current = workflows.media(store)
            if current.get('builtin_identity') == identity:
                with store.db() as db:
                    db.execute("INSERT INTO optional_capabilities VALUES('shredos','installed',?,?,?,?) ON CONFLICT(id) DO UPDATE SET status='installed', detail=excluded.detail", (DOWNLOAD_SIZE, DOWNLOAD_SIZE, 'Built-in ShredOS ready', workflows.now()))
                return True
        except HTTPException:
            current = None
        with store.db() as db:
            active = db.execute("SELECT id FROM workflow_runs WHERE stage NOT IN ('complete','failed','cancelled')").fetchone()
            if active:
                assets = Path(__file__).with_name('assets') / 'shredos/manifest.json'
                pinned = json.loads(assets.read_text())
                if not current or current['files']['shredos']['sha256'] != pinned['sha256']:
                    return False
            db.execute("INSERT INTO optional_capabilities VALUES('shredos','downloading',0,?,?,?) ON CONFLICT(id) DO UPDATE SET status='downloading', received=0, detail=excluded.detail, updated=excluded.updated", (DOWNLOAD_SIZE, 'Preparing built-in ShredOS', workflows.now()))
        publish_builtin(store, ca, origin, identity)
        return state(store)['status'] == 'installed'


def start_builtin(store):
    """Wait for first-start gateway certificate; retry failed preparation quietly."""
    stopped = threading.Event()
    if not (Path(__file__).with_name('assets') / 'shredos/manifest.json').is_file():
        return stopped
    def worker():
        while not stopped.is_set():
            try:
                if prepare_builtin(store):
                    return
            except Exception as exc:
                with store.db() as db:
                    db.execute("INSERT INTO optional_capabilities VALUES('shredos','failed',0,?,?,?) ON CONFLICT(id) DO UPDATE SET status='failed', detail=excluded.detail, updated=excluded.updated", (DOWNLOAD_SIZE, 'Built-in ShredOS: ' + str(exc)[:500], workflows.now()))
            stopped.wait(10)
    threading.Thread(target=worker, name='builtin-shredos', daemon=True).start()
    return stopped
