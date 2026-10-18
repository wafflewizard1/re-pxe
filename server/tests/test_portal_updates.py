"""No Docker daemon or live installation: real Git/SQLite files, fake engine calls."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import fcntl
from http.server import BaseHTTPRequestHandler
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socketserver
import sqlite3
import subprocess
import tempfile
import threading

import pytest
from repxe import portal_updates
from test_api import auth, client

script = Path(__file__).resolve().parents[2] / 'deploy/update_agent.py'
spec = importlib.util.spec_from_file_location('update_agent_test', script)
updater = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updater)


@pytest.fixture
def control(tmp_path, monkeypatch):
    folder = tmp_path / 'updater-control'
    folder.mkdir()
    (folder / 'requests.lock').touch()
    monkeypatch.setenv('REPXE_UPDATER_CONTROL', str(folder))
    return folder


def test_updater_unavailable_and_admin_role_boundary(client, control):
    result = client.get('/api/system-updates', headers=auth())
    assert result.status_code == 200 and not result.json()['enabled']
    assert 'portal-updates' in result.json()['detail']
    for role in ('capture', 'deploy', 'agent'):
        assert client.get('/api/system-updates', headers=auth(role)).status_code == 403
        for action, body in [('check', {}), ('recover', {}), ('apply', {'expected_revision': 'a'*40, 'confirm': True})]:
            assert client.post('/api/system-updates/'+action, headers=auth(role), json=body).status_code == 403
    assert client.get('/api/system-updates').status_code == 401
    assert client.post('/api/system-updates/check', headers=auth(), json={}).status_code == 503


def test_update_requests_are_fixed_and_require_csrf(client, control, monkeypatch):
    calls = []
    monkeypatch.setattr(portal_updates, 'request', lambda method, path, body=None: calls.append((method, path, body)) or {'enabled': True})
    assert client.post('/api/system-updates/apply', headers=auth(), json={'expected_revision': 'a'*40, 'confirm': True}).status_code == 202
    assert calls == [('POST', '/apply', {'expected_revision': 'a'*40, 'confirm': True})]
    for body in ({'ref': 'evil'}, {'expected_revision': '../main', 'confirm': True}, {'expected_revision': 'a'*40, 'confirm': False}):
        assert client.post('/api/system-updates/apply', headers=auth(), json=body).status_code == 422
    assert client.post('/api/system-updates/check', headers=auth(), json={'command': 'anything'}).status_code == 422
    assert client.post('https://testserver/api/login', json={'username': 'admin', 'password': 'admin'}).status_code == 200
    assert client.post('https://testserver/api/system-updates/check', json={}).status_code == 403
    assert len(calls) == 1


def test_maintenance_holds_machine_requests_and_keeps_status_available(client, control):
    (control / 'maintenance.json').write_text('{}')
    for path in ('/api/images', '/pxe/boot.ipxe?mac=52:54:00:00:00:01'):
        result = client.get(path, headers=auth())
        assert result.status_code == 503 and result.headers['retry-after'] == '5'
    assert client.post('/api/uploads', headers=auth(), json={}).status_code == 503
    assert client.get('/health').status_code == 200
    assert client.get('/api/system-updates', headers=auth()).json()['maintenance']
    (control / 'maintenance.json').unlink()
    with (control / 'requests.lock').open('rb') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert client.get('/api/images', headers=auth()).status_code == 503
    assert client.get('/api/images', headers=auth()).status_code == 200


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()


@pytest.fixture
def installation(tmp_path):
    root = tmp_path / 'installation'
    root.mkdir()
    (root / 'deploy').mkdir()
    (root / 'server').mkdir()
    (root / 'data/database').mkdir(parents=True)
    (root / '.gitignore').write_text('.local/\ndata/\ndeploy/.env\n')
    (root / 'deploy/.env').write_text('REPXE_DATA_ROOT='+str(root/'data')+'\nREPXE_ADDRESS=192.0.2.1\n')
    (root / 'deploy/update-protocol.json').write_text('{"protocol":1}')
    services = {name: {'image': 'fixture:'+name, 'environment': {}} for name in updater.SERVICES}
    services['api']['build'] = {'context': str(root/'server')}
    services['tftp']['build'] = {'context': str(root/'deploy')}
    for name in ('api','backup'):
        services[name]['volumes'] = [
            {'type':'bind','source':str(root/'data'),'target':'/data'},
            {'type':'bind','source':str(root/'.local/updater/control'),'target':'/run/re-pxe-updater','read_only':True}]
    (root / 'deploy/compose.yaml').write_text(json.dumps({'services': services}))
    (root / 'server/revision.txt').write_text('original')
    git(root, 'init', '-b', 'main')
    git(root, 'config', 'user.name', 'Fixture')
    git(root, 'config', 'user.email', 'fixture@example.invalid')
    git(root, 'add', '.')
    git(root, 'commit', '-m', 'original')
    previous = git(root, 'rev-parse', 'HEAD')
    (root / 'server/revision.txt').write_text('updated')
    git(root, 'add', '.')
    git(root, 'commit', '-m', 'new version')
    target = git(root, 'rev-parse', 'HEAD')
    git(root, 'reset', '--hard', previous)  # This newly-created private Git fixture only.
    git(root, 'update-ref', 'refs/remotes/origin/main', target)
    git(root, 'remote', 'add', 'origin', updater.UPSTREAM)
    with closing(sqlite3.connect(root / 'data/database/catalog.sqlite')) as db:
        for table in ('routine_runs', 'workflow_runs', 'jobs'):
            db.execute(f'CREATE TABLE {table}(stage TEXT)')
        db.execute('CREATE TABLE clients(stage TEXT,expires TEXT,ticket_expires TEXT)')
        db.execute('CREATE TABLE uploads(status TEXT)')
        db.execute('CREATE TABLE evidence(value BLOB)')
        db.execute('INSERT INTO evidence VALUES(?)', (b'original report\x00',))
        db.commit()
    (root/'data/image.wim').write_bytes(b'keep installation image')
    (root/'data/boot.wim').write_bytes(b'keep selected WinPE')
    return root, previous, target


@pytest.fixture
def agent(installation, monkeypatch):
    root, previous, target = installation
    worker = updater.Agent(root)
    original = worker.command
    commands = []

    def command(args, **kwargs):
        commands.append(args)
        if args[0] == 'git':
            return '' if 'fetch' in args else original(args, **kwargs)
        if args[:2] == ['docker', 'compose']:
            if 'config' in args:
                config_path = Path(args[args.index('-f')+1])
                return config_path.read_text()
            if 'ps' in args:
                return 'fixture-container-' + args[-1]
            return ''
        if args[:2] == ['docker', 'inspect'] or args[:3] == ['docker', 'image', 'inspect']:
            return 'sha256:'+'a'*64
        return ''

    monkeypatch.setattr(worker, 'command', command)
    healthy = []
    monkeypatch.setattr(worker, 'healthy', lambda config, revision: healthy.append(revision))
    worker.check()
    assert worker.status()['can_update']
    worker.commands = commands
    worker.health_checks = healthy
    return worker


def evidence(worker):
    with closing(sqlite3.connect(worker.data/'database/catalog.sqlite')) as db:
        return db.execute('SELECT value FROM evidence').fetchall()


def test_release_uses_pinned_sources_and_preserves_checkout_media_and_recovery(agent, installation):
    root, previous, target = installation
    agent.apply()
    assert agent.status()['phase'] == 'complete'
    assert agent.status()['current_revision'] == target
    assert agent.health_checks == [target]
    assert git(root, 'rev-parse', 'HEAD') == previous
    assert (root/'server/revision.txt').read_text() == 'original'
    assert (root/'data/image.wim').read_bytes() == b'keep installation image'
    assert (root/'data/boot.wim').read_bytes() == b'keep selected WinPE'
    assert evidence(agent) == [(b'original report\x00',)]
    assert Path(agent.state['backup']).is_file() and not agent.status()['maintenance']
    assert 'backup' not in agent.status() and 'previous_config' not in agent.status()
    config = json.loads(Path(agent.installed['config']).read_text())
    assert all('build' not in service and service['pull_policy'] == 'never' for service in config['services'].values())
    assert config['services']['api']['image'] == config['services']['backup']['image']
    assert config['services']['api']['environment']['REPXE_REVISION'] == target


def test_failed_startup_restores_database_and_images_retaining_failed_evidence(agent, installation, monkeypatch):
    root, previous, target = installation
    def healthy(config, revision):
        if revision == target:
            with closing(sqlite3.connect(agent.data/'database/catalog.sqlite')) as db:
                db.execute('INSERT INTO evidence VALUES(?)', (b'new schema/startup write',))
                db.commit()
            raise RuntimeError('fixture: new container unhealthy')
        agent.health_checks.append(revision)
    monkeypatch.setattr(agent, 'healthy', healthy)
    with pytest.raises(RuntimeError):
        agent.apply()
    assert agent.status()['rolled_back'] and not agent.status()['maintenance']
    assert agent.installed['revision'] == previous
    assert evidence(agent) == [(b'original report\x00',)]
    assert list(Path(agent.state['backup']).parent.glob('failed-database-*/catalog.sqlite'))
    assert agent.health_checks == [previous]


def test_failed_build_never_stops_running_services(agent, monkeypatch):
    monkeypatch.setattr(agent, 'build', lambda *args: (_ for _ in ()).throw(RuntimeError('fixture build failed')))
    with pytest.raises(RuntimeError):
        agent.apply()
    assert not agent.status()['maintenance']
    assert not any('stop' in args for args in agent.commands)
    assert evidence(agent) == [(b'original report\x00',)]


def test_changed_persistent_mounts_are_refused_before_restart(agent, monkeypatch):
    original = agent.build
    for changed in ('data', 'certificates'):
        def build(*args):
            path = original(*args)
            config = json.loads(path.read_text())
            if changed == 'data':
                config['services']['api']['volumes'][0]['source'] = '/unrelated-data'
            else:
                config['services']['gateway']['volumes'] = [{'type':'volume','source':'other-ca','target':'/data'}]
            updater.atomic(path, config)
            return path
        monkeypatch.setattr(agent, 'build', build)
        with pytest.raises(ValueError, match='changes data' if changed == 'data' else 'certificate mounts'):
            agent.apply()
        assert not any('stop' in args for args in agent.commands)
        assert not agent.status()['maintenance']


@pytest.mark.parametrize('table', ['routine_runs', 'workflow_runs', 'jobs', 'uploads', 'clients'])
def test_active_work_blocks_before_any_restart(agent, table):
    with closing(sqlite3.connect(agent.data/'database/catalog.sqlite')) as db:
        if table == 'clients':
            future = (datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat()
            db.execute('INSERT INTO clients VALUES(?,?,?)', ('booting', future, future))
        else:
            db.execute(f'INSERT INTO {table} VALUES(?)', ('receiving' if table == 'uploads' else 'operation-pending',))
        db.commit()
    with pytest.raises(ValueError):
        agent.apply()
    assert not any('stop' in args for args in agent.commands)


def test_job_starting_during_build_is_rechecked_without_stopping_it(agent, monkeypatch):
    original = agent.build
    def build(*args):
        result = original(*args)
        with closing(sqlite3.connect(agent.data/'database/catalog.sqlite')) as db:
            db.execute("INSERT INTO workflow_runs VALUES('erasing')")
            db.commit()
        return result
    monkeypatch.setattr(agent, 'build', build)
    with pytest.raises(ValueError):
        agent.apply()
    assert not agent.status()['maintenance']
    assert not any('stop' in args for args in agent.commands)


def test_interruption_keeps_maintenance_and_recovers_from_durable_state(agent, installation, monkeypatch):
    root, previous, target = installation
    agent.apply()
    agent.state.update(phase='restarting', previous_revision=previous, stopped=True)
    updater.atomic(agent.control/'maintenance.json', {})
    agent.publish()
    restarted = updater.Agent(root)
    assert restarted.status()['recovery_required'] and restarted.status()['maintenance']
    monkeypatch.setattr(restarted, 'command', agent.command)
    monkeypatch.setattr(restarted, 'healthy', lambda *args: None)
    restarted.recover()
    assert restarted.installed['revision'] == previous
    assert not restarted.status()['maintenance']


def test_corrupted_recovery_snapshot_holds_maintenance(agent, monkeypatch):
    agent.apply()
    updater.atomic(agent.control/'maintenance.json', {})
    Path(agent.state['backup']).write_bytes(b'bad backup')
    with pytest.raises(ValueError, match='hash differs'):
        agent.rollback()
    assert agent.status()['maintenance'] and agent.status()['recovery_required']


def test_offline_recovery_cannot_roll_back_a_completed_update(agent):
    agent.apply()
    current = agent.installed['revision']
    with pytest.raises(ValueError, match='No interrupted update'):
        agent.recover()
    assert agent.installed['revision'] == current and not agent.status()['maintenance']


def test_stale_confirmation_and_parallel_requests_cannot_start_an_update(agent, monkeypatch):
    with pytest.raises(ValueError):
        agent.start('apply', {'expected_revision': 'f'*40, 'confirm': True})
    with pytest.raises(ValueError):
        agent.start('apply', {'expected_revision': agent.state['target_revision'], 'confirm': True, 'ref': 'other'})
    agent.lock.acquire()
    try:
        with pytest.raises(ValueError, match='already running'):
            agent.start('check', {})
    finally:
        agent.lock.release()


def test_local_modifications_are_preserved_and_refused(agent, installation):
    (agent.root/'server/revision.txt').write_text('operator edits')
    with pytest.raises(ValueError, match='local changes'):
        agent.check()
    assert (agent.root/'server/revision.txt').read_text() == 'operator edits'


def test_unix_socket_bridge_roundtrip_and_host_conflict(monkeypatch):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            self.send_response(200);self.end_headers()
            self.wfile.write(b'{"enabled":true,"phase":"available"}')
        def do_POST(self):
            self.send_response(409);self.end_headers()
            self.wfile.write(b'{"detail":"Another update is running"}')
    with tempfile.TemporaryDirectory(prefix='re-pxe-socket-') as directory:
        monkeypatch.setenv('REPXE_UPDATER_CONTROL', directory)
        with socketserver.UnixStreamServer(str(Path(directory)/'control.sock'), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                assert portal_updates.request('GET', '/status')['phase'] == 'available'
                from fastapi import HTTPException
                with pytest.raises(HTTPException) as error:
                    portal_updates.request('POST', '/check', {})
                assert error.value.status_code == 409
                assert error.value.detail == 'Another update is running'
            finally:
                server.shutdown()
                thread.join()


def test_real_compose_resolves_persistent_mounts_and_accepts_pinned_runtime(tmp_path):
    compose = ['docker-compose'] if shutil.which('docker-compose') else ['docker', 'compose'] if shutil.which('docker') else None
    if compose is None:
        pytest.skip('Docker Compose CLI unavailable')
    root = script.parents[1]
    env = {**os.environ, 'REPXE_ADDRESS': '192.0.2.1',
           'REPXE_DATA_ROOT': str(tmp_path/'data'), 'REPXE_UPDATER_ROOT': str(tmp_path/'control'),
           **{'REPXE_'+role+'_TOKEN': 'fixture-'+role+'x'*40 for role in ('ADMIN','CAPTURE','AGENT','DEPLOY')}}
    empty = tmp_path/'empty.env';empty.write_text('')
    value = subprocess.check_output([*compose, '--project-name', 're-pxe', '--project-directory', str(root/'deploy'), '--env-file', str(empty), '-f', str(root/'deploy/compose.yaml'), 'config', '--format', 'json'], env=env, text=True)
    config = json.loads(value)
    assert set(config['services']) == updater.SERVICES
    for name in ('api','backup'):
        assert any(v.get('source') == str(tmp_path/'control') and v.get('read_only') and v['target'] == '/run/re-pxe-updater' for v in config['services'][name]['volumes'])
        assert any(v.get('source') == str(tmp_path/'data') and v['target'] == '/data' for v in config['services'][name]['volumes'])
    volumes = config['volumes']
    for name, service in config['services'].items():
        service.pop('build', None)
        service.update(image='re-pxe-fixture:'+name,pull_policy='never')
    runtime = tmp_path/'runtime.json';runtime.write_text(json.dumps(config))
    result = json.loads(subprocess.check_output([*compose,'--project-name','re-pxe','-f',str(runtime),'config','--format','json'],env=env,text=True))
    assert result['volumes'] == volumes
    assert result['services']['gateway']['volumes'] == config['services']['gateway']['volumes']
    help_text = subprocess.check_output([*compose,'up','--help'],text=True)
    assert '--wait-timeout' in help_text and '--wait' in help_text
