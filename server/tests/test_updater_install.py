"""Exercise host installation using private files and simulated systemctl calls."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest


@pytest.fixture
def installation(tmp_path, monkeypatch):
    deploy = Path(__file__).resolve().parents[2] / 'deploy'
    monkeypatch.syspath_prepend(str(deploy))
    spec = importlib.util.spec_from_file_location('installer_test', deploy / 'install_updater.py')
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    root = tmp_path / 'installation'
    (root / 'deploy').mkdir(parents=True)
    (root / 'deploy/.env').write_text('REPXE_ADDRESS=192.0.2.1\n')
    monkeypatch.setattr(installer, 'UNIT_PATH', tmp_path / 'updater.service')
    monkeypatch.setattr(installer.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(installer.os, 'chown', lambda *args: None)
    monkeypatch.setattr(installer.pwd, 'getpwnam',
                        lambda username: SimpleNamespace(pw_uid=1000, pw_gid=1000))
    original_stat = Path.stat

    def stat(path, *args, **kwargs):
        result = original_stat(path, *args, **kwargs)
        if path == root:
            values = list(result)
            values[4] = 1000
            return os.stat_result(values)
        return result

    monkeypatch.setattr(Path, 'stat', stat)
    monkeypatch.setattr(installer.subprocess, 'check_output', lambda *args, **kwargs: 'a' * 40 + '\n')
    calls = []
    failures = {}

    def run(command, **kwargs):
        calls.append(command)
        result = subprocess.CompletedProcess(command, failures.get(command[1], 0), '', 'systemctl detail\n')
        if kwargs.get('check'):
            result.check_returncode()
        return result

    monkeypatch.setattr(installer.subprocess, 'run', run)
    return installer, root, calls, failures


@pytest.mark.parametrize('stop_result', [0, 5])
def test_install_starts_service_when_previous_service_is_stopped_or_missing(installation, stop_result, capsys):
    installer, root, calls, failures = installation
    failures['stop'] = stop_result
    installer.install(root, 'james')
    assert [command[1] for command in calls] == ['daemon-reload', 'enable', 'stop', 'restart']
    assert 'User=james\n' in installer.UNIT_PATH.read_text()
    installed = root / '.local/updater/installed.json'
    assert json.loads(installed.read_text()) == {'revision': 'a' * 40, 'config': None}
    output = capsys.readouterr()
    assert 'Portal updates enabled.' in output.out
    assert not output.err


@pytest.mark.parametrize('stop_result', [1, 4])
def test_failed_stop_preserves_existing_updater_state(installation, stop_result, capsys):
    installer, root, calls, failures = installation
    base = root / '.local/updater'
    base.mkdir(parents=True)
    original = {'installed.json': '{"revision":"previous"}', 'state.json': '{"phase":"idle"}'}
    for name, value in original.items():
        (base / name).write_text(value)
    failures['stop'] = stop_result
    with pytest.raises(subprocess.CalledProcessError) as error:
        installer.install(root, 'james')
    assert error.value.returncode == stop_result
    assert 'systemctl detail' in capsys.readouterr().err
    assert 'restart' not in [command[1] for command in calls]
    assert not list(base.glob('*.before-setup-*'))
    for name, value in original.items():
        assert (base / name).read_text() == value


@pytest.mark.parametrize('failure', ['daemon-reload', 'enable', 'restart'])
def test_service_installation_errors_still_fail_setup(installation, failure, capsys):
    installer, root, calls, failures = installation
    failures.update(stop=5, **{failure: 1})
    with pytest.raises(subprocess.CalledProcessError) as error:
        installer.install(root, 'james')
    assert error.value.cmd == ['systemctl', failure] + ([] if failure == 'daemon-reload' else ['re-pxe-updater.service'])
    assert 'Portal updates enabled.' not in capsys.readouterr().out
