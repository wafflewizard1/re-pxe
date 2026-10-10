"""Synthetic sysfs/network fixtures only; never inspect or alter host storage."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from repxe import shredos_agent as agent

MAC = '52:54:00:12:34:56'
USB_MAC = '00:e0:4c:68:11:22'


def nic(name='usb0', mac=USB_MAC, usb=True, linked=True):
    return {'name': name, 'mac': mac, 'usb': usb, 'linked': linked}


def test_rescan_uses_physical_ethernet_type_and_usb_bus_not_interface_name(tmp_path):
    root = tmp_path / 'net'; root.mkdir()
    usb_bus = tmp_path / 'bus/usb'; usb_bus.mkdir(parents=True)
    device = tmp_path / 'devices/usb1/1-2/1-2:1.0'; device.mkdir(parents=True)
    (device / 'subsystem').symlink_to(usb_bus)
    for name, kind in [('usb0', '1'), ('lo', '772'), ('wlan0', '1'), ('veth0', '1')]:
        path = root / name; path.mkdir()
        (path / 'type').write_text(kind)
        (path / 'address').write_text(USB_MAC)
        (path / 'carrier').write_text('1')
        if name != 'veth0': (path / 'device').symlink_to(device)
        if name == 'wlan0': (path / 'wireless').mkdir()
    assert agent.network_interfaces(root) == [nic()]
    (root / 'usb0/address').unlink()  # Disconnected during the next enumeration.
    assert agent.network_interfaces(root) == []


def test_usb_firmware_mac_change_requires_discovery_serial_for_armed_boots():
    usb = nic()
    assert agent.boot_interface([usb], MAC) is None
    assert agent.boot_interface([usb], MAC, intake=True) == usb
    assert agent.boot_interface([usb], MAC, bound_serial=True) == usb
    assert agent.boot_interface([nic(usb=False)], MAC, bound_serial=True) is None
    assert agent.boot_interface([nic(linked=False)], MAC, bound_serial=True) is None


def test_exact_boot_adapter_has_priority_and_ambiguous_adapters_are_refused():
    exact = nic('eth0', MAC, False)
    assert agent.boot_interface([nic(), exact, nic('usb1', '00:e0:4c:68:11:23')], MAC, intake=True) == exact
    with pytest.raises(RuntimeError, match='multiple Ethernet'):
        agent.boot_interface([exact, {**exact, 'name': 'eth1'}], MAC)
    with pytest.raises(RuntimeError, match='ambiguous'):
        agent.boot_interface([nic(), nic('usb1', '00:e0:4c:68:11:23')], MAC, bound_serial=True)


def test_serial_binding_rejects_wrong_pc_unreported_serial_and_malformed_hash(monkeypatch):
    monkeypatch.setattr(agent, 'inventory_serial', lambda: 'PC-42')
    assert not agent.serial_binding('')
    assert agent.serial_binding(hashlib.sha256(b'PC-42').hexdigest())
    for value in [hashlib.sha256(b'PC-OTHER').hexdigest(), 'PC-42', 'f' * 64]:
        with pytest.raises(RuntimeError, match='serial'):
            agent.serial_binding(value)
    monkeypatch.setattr(agent, 'inventory_serial', lambda: 'unreported')
    with pytest.raises(RuntimeError, match='serial'):
        agent.serial_binding(hashlib.sha256(b'unreported').hexdigest())


def network_fixture(monkeypatch, scans, ready):
    clock = [0]
    monkeypatch.setattr(agent.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(agent.time, 'sleep', lambda delay: clock.__setitem__(0, clock[0] + delay))
    scans = iter(scans)
    monkeypatch.setattr(agent, 'network_interfaces', lambda: next(scans))
    monkeypatch.setattr(agent, 'ipv4_ready', lambda name: ready())
    actions = []
    monkeypatch.setattr(agent.subprocess, 'run', lambda args, **kw: actions.append(args))
    process = SimpleNamespace(poll=lambda: None, terminate=lambda: actions.append(['terminate-dhcp']))
    monkeypatch.setattr(agent.subprocess, 'Popen', lambda args, **kw: actions.append(args) or process)
    return actions


def test_late_usb_adapter_gets_one_renewing_dhcp_process_and_retained_diagnostics(tmp_path, monkeypatch):
    readiness = iter([False, True])
    actions = network_fixture(monkeypatch, [[], [nic(linked=False)], [nic()], [nic()]], lambda: next(readiness))
    chosen = agent.prepare_network(MAC, True, '', tmp_path, timeout=10)
    assert chosen == nic()
    assert actions[0] == ['ip', 'link', 'set', 'dev', 'usb0', 'up']
    assert len(actions) == 2 and actions[1][0:4] == ['udhcpc', '-f', '-i', 'usb0']
    diagnostics = json.loads((tmp_path / 'network.json').read_text())
    assert diagnostics['inventory_only'] and not diagnostics['serial_bound']
    assert diagnostics['selected'] == 'usb0' and diagnostics['boot_mac'] == MAC


def test_armed_usb_boot_without_serial_binding_never_starts_dhcp(tmp_path, monkeypatch):
    actions = network_fixture(monkeypatch, [[nic()]] * 3, lambda: False)
    with pytest.raises(RuntimeError, match='discovery before an armed erase'):
        agent.prepare_network(MAC, False, '', tmp_path, timeout=3)
    assert actions == []
    assert (tmp_path / 'network.json').is_file()


def test_serial_bound_usb_boot_keeps_the_firmware_mac_as_run_identity(tmp_path, monkeypatch):
    actions = network_fixture(monkeypatch, [[nic()]], lambda: True)
    monkeypatch.setattr(agent, 'inventory_serial', lambda: 'PC-42')
    assert agent.prepare_network(MAC, False, hashlib.sha256(b'PC-42').hexdigest(), tmp_path) == nic()
    assert len(actions) == 2
    assert json.loads((tmp_path / 'network.json').read_text())['boot_mac'] == MAC


def test_failed_dhcp_stops_without_any_erase_or_reboot(tmp_path, monkeypatch):
    actions = network_fixture(monkeypatch, [[nic(mac=MAC)]] * 3, lambda: False)
    with pytest.raises(RuntimeError, match='no DHCP address'):
        agent.prepare_network(MAC, False, '', tmp_path, timeout=3)
    assert actions[-1] == ['terminate-dhcp']
    assert [a[0] for a in actions] == ['ip', 'udhcpc', 'terminate-dhcp']


def test_https_readiness_retries_only_credential_free_health(tmp_path, monkeypatch):
    import errno,urllib.error
    calls=[]
    class Response:
        status=200
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def read(self,bound):return b'{"status":"ok"}'
    class Opener:
        def open(self,url,**kw):
            calls.append(url)
            if len(calls)<3:raise urllib.error.URLError(OSError(errno.ENETUNREACH,'secret-token-in-URL'))
            return Response()
    api=agent.API.__new__(agent.API);api.origin='https://fixture:8443';api.opener=Opener()
    monkeypatch.setattr(agent.time,'sleep',lambda n:None)
    api.ready(tmp_path,0)
    assert calls==['https://fixture:8443/health']*3
    assert json.loads((tmp_path/'connection.json').read_text())['status']=='ready'


def test_connection_diagnostics_never_echo_request_credentials():
    import errno,urllib.error,ssl
    e=urllib.error.URLError(OSError(errno.ECONNREFUSED,'https://secret@server/run-token'))
    assert 'secret' not in str(agent.connection_error(e))
    assert 'refused' in agent.connection_error(e)['message']
    error=ssl.SSLCertVerificationError(1,'secret');error.verify_code=9
    assert agent.connection_error(urllib.error.URLError(error))['verify_code']==9


def test_wrong_ca_is_not_retried_or_clock_bootstrapped(tmp_path, monkeypatch):
    import urllib.error,ssl
    error=ssl.SSLCertVerificationError(1,'untrusted');error.verify_code=20
    class Opener:
        def open(self,*a,**kw):raise urllib.error.URLError(error)
    api=agent.API.__new__(agent.API);api.origin='https://fixture:8443';api.opener=Opener()
    monkeypatch.setattr(api,'synchronize_clock',lambda *a:pytest.fail('Wrong CA cannot bootstrap time'))
    with pytest.raises(RuntimeError,match='CA differs'):api.ready(tmp_path,123)
    assert json.loads((tmp_path/'connection.json').read_text())['verify_code']==20
