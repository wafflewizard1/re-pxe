#!/usr/bin/env python3
"""Local Docker updater. Only the fixed upstream main branch is accepted.

The portal gets a small Unix-socket protocol, never the Docker socket or a shell.
Release sources, resolved Compose secrets, logs and recovery files stay private.
"""
import argparse
from contextlib import contextmanager, closing
from datetime import datetime, timezone
import fcntl
import hashlib
from http.server import BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import re
import shutil
import socketserver
import sqlite3
import subprocess
import tarfile
import threading
import time
import urllib.request
import uuid

UPSTREAM = 'https://github.com/wafflewizard1/re-pxe.git'
SERVICES = {'api', 'backup', 'network', 'gateway', 'boot', 'tftp'}
SHA = re.compile(r'^[a-f0-9]{40}$')
ACTIVE = {'checking', 'building', 'preparing', 'backing-up', 'restarting', 'recovering'}


def now():
    return datetime.now(timezone.utc).isoformat()


def atomic(path, value):
    path = Path(path)
    pending = path.with_name('.' + path.name + '.' + uuid.uuid4().hex)
    try:
        with pending.open('x') as stream:
            os.chmod(pending, 0o600)
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        pending.replace(path)
        fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        pending.unlink(missing_ok=True)


def read(path, default):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return default


def idle(db):
    """Unknown/nonterminal outcomes hold, including failed-parent active children."""
    for table in ('routine_runs', 'workflow_runs', 'jobs'):
        if db.execute(f"SELECT 1 FROM {table} WHERE stage NOT IN ('complete','failed','cancelled') LIMIT 1").fetchone():
            raise ValueError('Finish or cancel all machine jobs before updating.')
    if db.execute("SELECT 1 FROM clients WHERE stage NOT IN ('complete','failed','cancelled') AND (expires>? OR ticket_expires>?) LIMIT 1", (now(), now())).fetchone():
        raise ValueError('Wait for active client sessions and boot tickets to finish before updating.')
    if db.execute("SELECT 1 FROM uploads WHERE status IN ('receiving','validating') LIMIT 1").fetchone():
        raise ValueError('Finish or cancel uploads before updating.')


class Agent:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.base = self.root / '.local/updater'
        self.control = self.base / 'control'
        self.control.mkdir(parents=True, exist_ok=True, mode=0o750)
        self.private = self.base / 'state.json'
        self.lock = threading.Lock()
        self.env = dict(line.split('=', 1) for line in (self.root / 'deploy/.env').read_text().splitlines() if '=' in line and not line.startswith('#'))
        self.data = Path(self.env['REPXE_DATA_ROOT']).resolve()
        self.installed = read(self.base / 'installed.json', None)
        if self.installed is None:
            self.installed = {'revision': self.git('rev-parse', 'HEAD').strip(), 'config': None}
        self.state = read(self.private, {'phase': 'idle', 'detail': 'Check GitHub for a newer version.'})
        if self.state.get('phase') in ACTIVE or (self.control / 'maintenance.json').exists():
            self.state.update(phase='failed', recovery_required=(self.control / 'maintenance.json').exists(),
                              detail='The updater was interrupted. Recover the previous version before continuing.' if (self.control / 'maintenance.json').exists() else 'The previous check or build was interrupted; check again.')
        self.publish()

    def command(self, args, *, timeout=1800):
        with (self.base / 'host.log').open('ab') as log:
            os.chmod(self.base / 'host.log', 0o600)
            result = subprocess.run(args, cwd=self.root, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=log, timeout=timeout,
                                    env={**os.environ, 'GIT_TERMINAL_PROMPT': '0',
                                         'REPXE_UPDATER_ROOT': str(self.control),
                                         'REPXE_DATA_ROOT': str(self.data)})
        if result.returncode:
            raise RuntimeError('A host command failed; see the private updater host.log.')
        return result.stdout.decode()

    def git(self, *args):
        return self.command(['git', '-C', str(self.root), *args], timeout=120)

    def compose(self, config, *args):
        return self.command(['docker', 'compose', '--project-name', 're-pxe', '-f', str(config), *args])

    def publish(self, **values):
        self.state.update(values, updated=now())
        atomic(self.private, self.state)
        atomic(self.control / 'status.json', self.status())

    def status(self):
        public = {k: self.state[k] for k in ('phase', 'detail', 'updated', 'target_revision', 'summary', 'checked_at', 'recovery_required', 'rolled_back') if k in self.state}
        return {**public, 'enabled': True, 'busy': self.state.get('phase') in ACTIVE,
                'current_revision': self.installed['revision'], 'repository': UPSTREAM,
                'can_update': self.state.get('phase') == 'available',
                'maintenance': (self.control / 'maintenance.json').exists()}

    def start(self, action, body):
        if action not in ('check', 'apply', 'recover'):
            raise ValueError('Unknown update action.')
        if not self.lock.acquire(blocking=False):
            raise ValueError('An updater operation is already running.')
        try:
            if self.state.get('recovery_required') and action != 'recover':
                raise ValueError('Recover the previous version first.')
            if action == 'apply':
                if (set(body) != {'expected_revision', 'confirm'} or body.get('confirm') is not True
                        or not SHA.fullmatch(str(body.get('expected_revision', '')))
                        or self.state.get('phase') != 'available'
                        or body['expected_revision'] != self.state.get('target_revision')):
                    raise ValueError('Check for updates again and confirm the displayed revision.')
            elif body:
                raise ValueError('This action takes no parameters.')
            if action == 'recover' and not self.state.get('recovery_required'):
                raise ValueError('No interrupted update needs recovery.')
            self.publish(phase={'check': 'checking', 'apply': 'building', 'recover': 'recovering'}[action],
                         detail={'check': 'Checking GitHub…', 'apply': 'Preparing the selected release…', 'recover': 'Recovering the previous version…'}[action], rolled_back=False)
            threading.Thread(target=self.work, args=(action,), daemon=True).start()
            return self.status()
        except BaseException:
            self.lock.release()
            raise

    def work(self, action):
        try:
            {'check': self.check, 'apply': self.apply, 'recover': self.recover}[action]()
        except Exception as exc:
            # Command stderr is private. Do not expose environment, credentials or Compose output.
            detail = str(exc) if isinstance(exc, ValueError) else 'Updater failed; inspect the private host.log on the server.'
            self.publish(phase='failed', detail=self.state['detail'] if self.state.get('rolled_back') else detail,
                         recovery_required=(self.control / 'maintenance.json').exists())
        finally:
            self.lock.release()

    def check(self):
        if self.git('remote', 'get-url', 'origin').strip() not in (UPSTREAM, 'git@github.com:wafflewizard1/re-pxe.git'):
            raise ValueError('The updater requires the official re-pxe origin repository.')
        if self.git('status', '--porcelain'):
            raise ValueError('The installation checkout has local changes. Preserve and review them before updating.')
        self.git('fetch', '--no-tags', 'origin', 'refs/heads/main:refs/remotes/origin/main')
        target = self.git('rev-parse', 'refs/remotes/origin/main').strip()
        if not SHA.fullmatch(target):
            raise ValueError('GitHub returned an invalid revision.')
        try:
            self.git('merge-base', '--is-ancestor', self.installed['revision'], target)
        except RuntimeError:
            raise ValueError('The installed source is not on GitHub main history. Merge it into main or review the installation on the host.') from None
        if target != self.installed['revision']:
            try:
                protocol = json.loads(self.git('show', target + ':deploy/update-protocol.json'))
            except (RuntimeError, ValueError):
                raise ValueError('This release needs a host updater upgrade. Run setup on the host first.') from None
            if protocol != {'protocol': 1}:
                raise ValueError('This release needs a host updater upgrade. Run setup on the host first.')
        self.publish(phase='current' if target == self.installed['revision'] else 'available',
                     target_revision=target, summary=self.git('show', '-s', '--format=%s', target).strip()[:300],
                     checked_at=now(), detail='Already up to date.' if target == self.installed['revision'] else 'A new version is available.')

    def config(self, source, output):
        text = self.command(['docker', 'compose', '--project-name', 're-pxe',
                             '--project-directory', str(source / 'deploy'),
                             '--env-file', str(self.root / 'deploy/.env'),
                             '-f', str(source / 'deploy/compose.yaml'), 'config', '--format', 'json'])
        config = json.loads(text)
        if set(config['services']) != SERVICES:
            raise ValueError('This release changes host services; run setup on the host first.')
        atomic(output, config)
        return config

    def snapshot_images(self, current, output, tag):
        config = json.loads(Path(current).read_text())
        for name, service in config['services'].items():
            container = self.compose(current, 'ps', '-q', name).strip()
            if not container or '\n' in container:
                raise ValueError('Every re-pxe service must be running before updating.')
            image = self.command(['docker', 'inspect', '--format', '{{.Image}}', container]).strip()
            label = f're-pxe-recovery:{tag}-{name}'
            self.command(['docker', 'tag', image, label])
            service.update(image=label, pull_policy='never')
            service.pop('build', None)
        config['services']['api']['environment']['REPXE_REVISION'] = self.installed['revision']
        atomic(output, config)

    def build(self, source, directory, tag):
        build_file = directory / 'build.json'
        config = self.config(source, build_file)
        for name, service in config['services'].items():
            if 'build' in service or name == 'backup':
                service['image'] = f're-pxe-update:{tag}-' + ('api' if name == 'backup' else name)
        atomic(build_file, config)
        self.compose(build_file, 'build', '--pull')
        for name, service in config['services'].items():
            if 'build' not in service and name != 'backup':
                self.command(['docker', 'pull', service['image']])
                image = self.command(['docker', 'image', 'inspect', '--format', '{{.Id}}', service['image']]).strip()
                label = f're-pxe-update:{tag}-{name}'
                self.command(['docker', 'tag', image, label])
                service['image'] = label
            service.pop('build', None)
            service['pull_policy'] = 'never'
        config['services']['api']['environment']['REPXE_REVISION'] = self.state['target_revision']
        output = directory / 'candidate.json'
        atomic(output, config)
        return output

    def validate_layout(self, previous, candidate):
        before, after = read(previous, {}), read(candidate, {})
        if before.get('volumes', {}) != after.get('volumes', {}):
            raise ValueError('Persistent volume configuration changed. Review the update on the host first.')
        old_ca = {v['target']: v for v in before['services']['gateway'].get('volumes', [])}
        new_ca = {v['target']: v for v in after['services']['gateway'].get('volumes', [])}
        if any(old_ca.get(target) != new_ca.get(target) for target in ('/data', '/config')):
            raise ValueError('Gateway certificate mounts changed. Review the update on the host first.')
        for name, service in after['services'].items():
            if service.get('privileged') or service.get('devices'):
                raise ValueError('Portal updates cannot add privileged containers or host devices.')
            if name not in ('api', 'backup'):
                continue
            mounts = {v['target']: v for v in service.get('volumes', [])}
            for target, source in (('/data', self.data), ('/run/re-pxe-updater', self.control)):
                mount = mounts.get(target, {})
                if (mount.get('type') != 'bind' or mount.get('source') != str(source)
                        or bool(mount.get('read_only')) != (target == '/run/re-pxe-updater')):
                    raise ValueError('The release changes data or updater mounts. Review setup on the host first.')

    @contextmanager
    def drain(self):
        atomic(self.control / 'maintenance.json', {'started': now()})
        with (self.control / 'requests.lock').open('a') as stream:
            deadline = time.monotonic() + 60
            while True:
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise ValueError('Requests are still running. Retry after they finish.')
                    time.sleep(.1)
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def ensure_idle(self):
        path = self.data / 'database/catalog.sqlite'
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
            idle(db)

    def backup(self, path):
        with closing(sqlite3.connect((self.data / 'database/catalog.sqlite').as_uri() + '?mode=ro', uri=True)) as source:
            with closing(sqlite3.connect(path)) as target:
                source.backup(target)
                if target.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                    raise ValueError('Pre-update database backup failed verification.')
        os.chmod(path, 0o600)
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def healthy(self, config, revision):
        self.compose(config, 'up', '-d', '--no-build', '--force-recreate', '--wait', '--wait-timeout', '180')
        for address in ('http://127.0.0.1:8088/health', 'http://127.0.0.1:8080/health'):
            with urllib.request.urlopen(address, timeout=10) as response:
                result = json.load(response)
                if result.get('status') != 'ok' or result.get('revision') != revision:
                    raise ValueError('The updated portal did not report the expected revision.')

    def apply(self):
        self.ensure_idle()
        target = self.state['target_revision']
        if self.git('status', '--porcelain'):
            raise ValueError('The installation checkout has local changes; update stopped.')
        directory = self.base / 'releases' / uuid.uuid4().hex
        directory.mkdir(parents=True, mode=0o700)
        source = directory / 'source'
        source.mkdir()
        archive = directory / 'source.tar'
        # Archive a pinned Git object, without changing the operator's checkout.
        with archive.open('xb') as stream:
            subprocess.run(['git', '-C', str(self.root), 'archive', '--format=tar', target],
                           stdout=stream, stdin=subprocess.DEVNULL, check=True, timeout=120)
        with tarfile.open(archive) as bundle:
            bundle.extractall(source, filter='data')
        current = directory / 'current.json'
        if self.installed.get('config'):
            atomic(current, read(self.installed['config'], None))
        else:
            self.config(self.root, current)
        previous = directory / 'previous.json'
        self.snapshot_images(current, previous, directory.name)
        candidate = self.build(source, directory, directory.name)
        self.validate_layout(previous, candidate)
        self.publish(phase='preparing', detail='Waiting for requests to finish…',
                     previous_config=str(previous), candidate_config=str(candidate),
                     previous_revision=self.installed['revision'], backup_ready=False,
                     backup=str(directory / 'catalog.sqlite'), stopped=False)
        try:
            with self.drain():
                self.ensure_idle()  # Jobs may have started while the images were building.
                self.publish(phase='backing-up', detail='Saving a verified database recovery copy…', stopped=True)
                self.compose(previous, 'stop', 'backup', 'api')
                sha = self.backup(directory / 'catalog.sqlite')
                self.publish(backup_ready=True, backup_sha256=sha)
                self.publish(phase='restarting', detail='Restarting services and checking the new portal…')
                self.healthy(candidate, target)
                self.installed = {'revision': target, 'config': str(candidate)}
                atomic(self.base / 'installed.json', self.installed)
                (self.control / 'maintenance.json').unlink()
                self.publish(phase='complete', detail='Update complete. All services passed startup checks.', recovery_required=False)
        except Exception:
            if self.state.get('stopped'):
                self.rollback()
            else:
                (self.control / 'maintenance.json').unlink(missing_ok=True)
            raise

    def rollback(self):
        self.publish(phase='recovering', detail='Restoring the previous version…', recovery_required=True)
        self.compose(self.state['candidate_config'], 'stop')
        if self.state.get('backup_ready'):
            path = Path(self.state['backup'])
            if hashlib.sha256(path.read_bytes()).hexdigest() != self.state['backup_sha256']:
                raise ValueError('Recovery backup hash differs. Maintenance remains enabled; inspect the server.')
            failed = path.parent / ('failed-database-' + uuid.uuid4().hex)
            failed.mkdir(mode=0o700)
            for name in ('catalog.sqlite', 'catalog.sqlite-wal', 'catalog.sqlite-shm'):
                original = self.data / 'database' / name
                if original.is_file():
                    shutil.copyfile(original, failed / name)
            with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as source:
                with closing(sqlite3.connect(self.data / 'database/catalog.sqlite')) as destination:
                    source.backup(destination)
        self.healthy(self.state['previous_config'], self.state['previous_revision'])
        self.installed = {'revision': self.state['previous_revision'], 'config': self.state['previous_config']}
        atomic(self.base / 'installed.json', self.installed)
        (self.control / 'maintenance.json').unlink(missing_ok=True)
        self.publish(phase='failed', detail='The update failed. The previous version and database were recovered.', recovery_required=False, rolled_back=True)

    def recover(self):
        if not self.state.get('recovery_required') or not (self.control / 'maintenance.json').exists():
            raise ValueError('No interrupted update needs recovery.')
        with self.drain():
            if self.state.get('stopped'):
                self.rollback()
            else:
                (self.control / 'maintenance.json').unlink(missing_ok=True)
                self.publish(phase='failed', detail='Interrupted preparation cleared; the running version was unchanged.', recovery_required=False)


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def serve(agent):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, *args):
            pass

        def do_GET(self):
            self.reply(200, agent.status()) if self.path == '/status' else self.reply(404, {'detail': 'Unknown endpoint'})

        def do_POST(self):
            try:
                length = int(self.headers.get('Content-Length', '-1'))
                if length < 0 or length > 1024 or self.path not in ('/check', '/apply', '/recover'):
                    raise ValueError('Invalid updater request.')
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError('JSON object required.')
                self.reply(202, agent.start(self.path[1:], body))
            except (ValueError, TypeError):
                self.reply(409, {'detail': 'Update request conflicts with the current state; refresh and retry.'})

        def reply(self, status, value):
            payload = json.dumps(value).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    path = agent.control / 'control.sock'
    path.unlink(missing_ok=True)
    with Server(str(path), Handler) as server:
        os.chmod(path, 0o600)
        server.serve_forever()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--recover', action='store_true', help='Offline recovery; stop the systemd updater first')
    args = parser.parse_args()
    base = args.root.resolve() / '.local/updater'
    base.mkdir(parents=True, exist_ok=True, mode=0o750)
    with (base / 'agent.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        agent = Agent(args.root)
        if args.recover:
            agent.recover()
        else:
            serve(agent)
