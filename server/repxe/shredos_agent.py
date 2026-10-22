#!/usr/bin/python3
"""re-pxe companion for an unmodified, pinned ShredOS release.

No work is performed on import. The executable refuses a normal host before
looking at its storage. Only invoke it in the dedicated RAM-root boot image.
"""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import base64
import errno
import hashlib
import fcntl
import json
import os
from pathlib import Path
import re
import ssl
import socket
import signal
import stat
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
try:
    from . import erase_policy, smart_health
except ImportError:  # Bundled RAM-root companion.
    import erase_policy, smart_health

VERSION = 're-pxe-shredos-11'
METHODS = set(erase_policy.METHODS)
NATIVE_VERSION = '0.43-repxe1'
HEALTH = {}


def disk_health(disk):
    key=(disk['path'],disk.get('serial'),disk['size'])
    if key not in HEALTH: HEALTH[key]=smart_health.collect(disk['path'])[0]
    return HEALTH[key]


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def guard():
    if Path('/etc/re-pxe-shredos-release').read_text().strip() != VERSION:
        raise RuntimeError('Dedicated ShredOS companion image required')
    roots = [line.split() for line in Path('/proc/mounts').read_text().splitlines() if line.split()[1] == '/']
    if not roots or any(row[2] not in ('rootfs', 'tmpfs', 'ramfs') for row in roots):
        raise RuntimeError('RAM-root ShredOS required; running host storage is prohibited')
    if os.geteuid() != 0:
        raise RuntimeError('Boot-image root user required')


def command(args):
    return subprocess.check_output(args, text=True, timeout=30).strip()


def dmi(name):
    p = Path('/sys/class/dmi/id') / name
    return p.read_text().strip() if p.is_file() else ''


def network_interfaces(root=Path('/sys/class/net')):
    """Rescan Ethernet devices: USB names and enumeration order are not fixed."""
    result = []
    for path in sorted(root.iterdir()):
        try:
            if (path / 'type').read_text().strip() != '1' or not (path / 'device').exists():
                continue
            if (path / 'wireless').exists() or (path / 'phy80211').exists():
                continue
            mac = (path / 'address').read_text().strip().lower()
            if not re.fullmatch(r'[0-9a-f]{2}(:[0-9a-f]{2}){5}', mac) or mac == '00:00:00:00:00:00':
                continue
            device = (path / 'device').resolve()
            usb = any((p / 'subsystem').resolve().name == 'usb' for p in (device, *device.parents))
            try:
                linked = (path / 'carrier').read_text().strip() == '1'
            except OSError:
                linked = False
            result.append({'name': path.name, 'mac': mac, 'usb': usb, 'linked': linked})
        except OSError:
            continue  # A USB adapter may be appearing/disappearing during this scan.
    return result


def serial_binding(expected):
    if not expected:
        return False
    if not re.fullmatch(r'[a-f0-9]{64}', expected):
        raise RuntimeError('Invalid boot PC serial binding')
    serial = inventory_serial()
    if serial == 'unreported' or hashlib.sha256(serial.encode()).hexdigest() != expected:
        raise RuntimeError('PC serial changed since discovery; erasure is refused')
    return True


def boot_interface(interfaces, mac, *, intake=False, bound_serial=False):
    exact = [nic for nic in interfaces if nic['mac'] == mac]
    if len(exact) > 1:
        raise RuntimeError('Boot MAC matches multiple Ethernet devices')
    if exact:
        return exact[0]
    # Firmware MAC passthrough can disappear when Linux loads a USB NIC driver.
    # Intake is read-only. Armed boots need the already discovered PC serial;
    # merely finding a different adapter must never authorize an erase.
    if not (intake or bound_serial):
        return None
    usb = [nic for nic in interfaces if nic['usb'] and nic['linked']]
    if len(usb) > 1:
        raise RuntimeError('Multiple connected USB Ethernet adapters; boot selection is ambiguous')
    return usb[0] if usb else None


def ipv4_ready(name):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            address = fcntl.ioctl(sock.fileno(), 0x8915, struct.pack('256s', name.encode()[:15]))
        return socket.inet_ntoa(address[20:24]) != '0.0.0.0'
    except OSError:
        return False


def prepare_network(mac, intake, expected_serial, evidence, timeout=90):
    bound = serial_binding(expected_serial)
    started = time.monotonic(); raised = set(); selected = None; dhcp = None
    log = evidence / 'network.log'
    while time.monotonic() - started < timeout:
        interfaces = network_interfaces()
        (evidence / 'network.json').write_text(json.dumps({
            'boot_mac': mac, 'serial_bound': bound, 'inventory_only': intake,
            'interfaces': interfaces, 'selected': selected['name'] if selected else None,
        }, indent=2))
        for nic in interfaces:
            if nic['mac'] == mac or nic['usb'] and (intake or bound):
                if nic['name'] not in raised:
                    subprocess.run(['ip', 'link', 'set', 'dev', nic['name'], 'up'], check=True,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
                    raised.add(nic['name'])
        chosen = boot_interface(interfaces, mac, intake=intake, bound_serial=bound)
        if chosen and selected is None:
            selected = chosen
            with log.open('ab') as output:
                # One DHCP process renews its lease throughout a long erase.
                # Retries here concern networking only; the erase has not been claimed.
                dhcp = subprocess.Popen(['udhcpc', '-f', '-i', selected['name'],
                    '-s', '/usr/bin/re-pxe-dhcp', '-t', '5', '-T', '3', '-A', '5'],
                    stdout=output, stderr=subprocess.STDOUT)
        if selected:
            current = next((nic for nic in interfaces if nic['name'] == selected['name']), None)
            if current and current['mac'] != selected['mac']:
                if dhcp and dhcp.poll() is None: dhcp.terminate()
                raise RuntimeError('Ethernet adapter identity changed before claiming the run')
            if current and current['linked'] and ipv4_ready(selected['name']):
                print('Ethernet ready: '+selected['name']+(' (USB firmware MAC passthrough; PC serial verified)' if selected['mac'] != mac and bound else ' (USB inventory discovery)' if selected['mac'] != mac else ''), flush=True)
                return selected
            if dhcp.poll() is not None:
                raise RuntimeError('Ethernet DHCP stopped; inspect network.log in the retained evidence')
        time.sleep(1)
    if dhcp and dhcp.poll() is None: dhcp.terminate()
    if selected:
        raise RuntimeError('Ethernet has no DHCP address; check the cable and DHCP server')
    raise RuntimeError('Boot Ethernet unavailable; check USB NIC support and connection. Firmware MAC passthrough needs read-only discovery before an armed erase')


def disk_list():
    fields = 'NAME,PATH,TYPE,SIZE,SERIAL,MODEL,TRAN,ROTA,MOUNTPOINTS,RO'
    disks=json.loads(command(['lsblk', '-J', '-b', '-o', fields]))['blockdevices']
    for disk in disks:
        # mdev may omit the lsblk transport. Recognize USB ancestry directly so
        # all-internal routines cannot include an unlabelled removable adapter.
        if disk.get('type')=='disk' and re.fullmatch(r'/dev/(sd[a-z]+|vd[a-z]+|nvme[0-9]+n[0-9]+)',disk.get('path','')):
            device=(Path('/sys/class/block')/Path(disk['path']).name/'device').resolve()
            if any((parent/'subsystem').resolve().name=='usb' for parent in (device,*device.parents)):
                disk['tran']='usb'
        # ShredOS uses mdev rather than a populated udev database; lsblk can
        # omit ATA serials. Read identification through smartctl in that case.
        if disk.get('type')=='disk' and not (disk.get('serial') or '').strip() and re.fullmatch(r'/dev/(sd[a-z]+|vd[a-z]+|nvme[0-9]+n[0-9]+)',disk.get('path','')):
            info=subprocess.run(['smartctl','-i','-j',disk['path']],capture_output=True,text=True,timeout=30)
            try:
                identified=json.loads(info.stdout)
            except ValueError:
                continue
            disk['serial']=identified.get('serial_number','')
            disk['model']=disk.get('model') or identified.get('model_name','')
    return disks


def flatten(disks):
    for d in disks:
        yield d
        yield from flatten(d.get('children', []))


def select_target(disks, identity, size, allow_usb):
    matches = [d for d in disks if d.get('type') == 'disk' and 'serial:' + (d.get('serial') or '').strip() == identity]
    if len(matches) != 1:
        raise RuntimeError('Authorized serial is missing or ambiguous')
    disk = matches[0]
    if int(disk['size']) != size or disk.get('ro') or (disk.get('tran') == 'usb' and not allow_usb):
        raise RuntimeError('Disk capacity or write policy differs')
    if any(any(x for x in d.get('mountpoints', [])) for d in flatten([disk])):
        raise RuntimeError('Mounted target or active swap is prohibited')
    if not re.fullmatch(r'/dev/(sd[a-z]+|vd[a-z]+|nvme[0-9]+n[0-9]+)', disk['path']):
        raise RuntimeError('Unsupported whole-disk path')
    return disk


NATIVE_CAPABILITIES = {}


def native_events(path):
    """Read a bounded JSONL snapshot; a partial trailing write is not evidence."""
    try:
        if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode): return []
        with path.open('rb') as stream:
            start=max(0,path.stat().st_size-65536);stream.seek(start)
            payload=stream.read(65536)
        if start:payload=payload.partition(b'\n')[2]
        lines=payload.decode('utf-8',errors='strict').splitlines()
    except (OSError,UnicodeError): return []
    result=[]
    for line in lines:
        try: event=json.loads(line)
        except ValueError: continue
        if isinstance(event,dict) and event.get('schema')==1 and event.get('engine_version')==NATIVE_VERSION:
            result.append(event)
    return result


def native_capabilities(disk, refresh=False, folder=Path('/run/re-pxe/probes')):
    """Only nwipe's read-only probe advertises controller sanitize support."""
    key=(disk['path'],disk.get('serial'),int(disk['size']))
    if key in NATIVE_CAPABILITIES and not refresh: return NATIVE_CAPABILITIES[key]
    software=sorted(erase_policy.NWIPE)
    value={'methods':software, 'scope':'unknown', 'engine_version':NATIVE_VERSION,
           'sanitize_status':'uncertain', 'detail':'Hold: native sanitize capability probe is unavailable'}
    if disk.get('tran')=='usb':
        value.update(scope='disk',sanitize_status='unsupported',detail='Unsupported: native sanitize through USB is excluded')
    else:
        try:
            folder.mkdir(parents=True,exist_ok=True,mode=0o700)
            eventfile=folder/('probe-'+hashlib.sha256(repr(key).encode()).hexdigest()[:20]+'-'+str(time.monotonic_ns())+'.jsonl')
            args=['/usr/bin/nwipe','--nogui','--sanitize=probe','--sanitize-serial='+(disk.get('serial') or '').strip(),
                  '--sanitize-size='+str(disk['size']),'--sanitize-events='+str(eventfile),
                  '--logfile='+str(eventfile.with_suffix('.log')),disk['path']]
            checked=subprocess.run(args,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=45)
            rows=native_events(eventfile)
            latest=rows[-1] if rows else {}
            bound=latest.get('target_identity')=='serial:'+(disk.get('serial') or '').strip() and latest.get('size_bytes')==int(disk['size'])
            inactive=latest.get('state') in ('idle','success') and latest.get('command_attempted') is False
            if bound and checked.returncode in (0,20) and inactive:
                methods=[m for m in latest.get('methods',[]) if m in erase_policy.NATIVE]
                value.update(methods=software+methods, scope='controller' if str(latest.get('protocol','')).lower()=='nvme' else 'disk',
                             controller_id=latest.get('controller_scope',''),
                             scope_targets=[{'identity':item['identity'],'size_bytes':item['size_bytes']} for item in latest.get('scope_targets',[])],
                             sanitize_status='ready' if methods and checked.returncode==0 else 'unsupported',
                             detail='Native nwipe sanitize capability probe confirmed' if methods and checked.returncode==0 else 'Unsupported: native sanitize is not available for this drive or controller scope')
            elif bound and checked.returncode==20 and latest.get('event')=='unsupported_or_refused' and latest.get('operation_started') is False and latest.get('state')=='idle':
                value.update(scope='disk',sanitize_status='unsupported',detail='Unsupported: native sanitize is not available for this drive or controller scope')
            else:
                value['detail']='Hold: nwipe sanitize probe returned an active, failed, unbound or unknown controller status'
        except (OSError,ValueError,KeyError,subprocess.SubprocessError):
            value['detail']='Hold: native sanitize capability query failed; inspect the retained probe evidence'
    NATIVE_CAPABILITIES[key]=value
    return value


def sanitize_args(run, disk, evidence):
    method=run.get('execution_method',run['method'])
    if method not in erase_policy.NATIVE: raise RuntimeError('An explicit native nwipe action is required')
    if method=='sanitize_crypto' and not (run.get('crypto_approved') and run.get('crypto_assurance')):
        raise RuntimeError('Crypto erase requires documented encryption and key assurance')
    return ['/usr/bin/nwipe','--autonuke','--nowait','--sanitize='+method.removeprefix('sanitize_'),
            '--sanitize-serial='+run['target_identity'].removeprefix('serial:'),
            '--sanitize-size='+str(run['size_bytes']), '--sanitize-events='+str(evidence/'sanitize.jsonl'),
            '--PDFreportpath='+str(evidence), '--logfile='+str(evidence/'nwipe.log'),disk['path']]


def native_progress(path):
    rows=native_events(path)
    if not rows:return None,'Native sanitize · awaiting controller status'
    latest=rows[-1];percent=latest.get('progress_pct')
    if not isinstance(percent,(int,float)) or isinstance(percent,bool) or not 0<=percent<=100:percent=None
    seconds=latest.get('remaining_seconds')
    eta=(' · ETA '+f'{int(seconds)//3600:02d}:{int(seconds)%3600//60:02d}:{int(seconds)%60:02d}') if isinstance(seconds,(int,float)) and 0<=seconds<86400*30 else ' · ETA unavailable'
    return min(99,int(percent)) if percent is not None else None, 'Native '+str(latest.get('action','sanitize'))+' · '+str(latest.get('state','unknown'))+eta


def execute_native(api, run, target, evidence):
    """The native nwipe engine starts once, waits, then retains its own PDF."""
    if run['method']=='nist_purge' and type(run.get('prng_followup',True)) is not bool:
        raise RuntimeError('The PRNG follow-up choice must be an explicit boolean')
    followup_enabled=run['method']=='nist_purge' and run.get('prng_followup',True)
    code=run_nwipe(sanitize_args(run,target,evidence),api,run['token'],phase='Native sanitize' if followup_enabled else None)
    events=native_events(evidence/'sanitize.jsonl');last=events[-1] if events else {}
    primary=original_drive_pdf(evidence,target['path'])
    logpath=evidence/'nwipe.log';log=logpath.read_text(errors='replace') if logpath.is_file() else ''
    scope=[{'identity':item.get('identity'),'size_bytes':item.get('size_bytes')} for item in last.get('scope_targets',[]) if isinstance(item,dict)]
    confirmed=code==0 and last.get('event')=='complete' and last.get('controller_state')=='completed' and last.get('operation_started') is True
    confirmed=confirmed and last.get('target_identity')==run['target_identity'] and last.get('size_bytes')==run['size_bytes'] and scope==[{'identity':run['target_identity'],'size_bytes':run['size_bytes']}]
    success=bool(confirmed and primary)
    primary_sha=hashlib.sha256(primary).hexdigest() if primary else None
    detail={'engine':'nwipe','engine_version':NATIVE_VERSION,'execution_method':run.get('execution_method',run['method']),
            'native_method':run.get('execution_method',run['method']), 'operation_started':last.get('operation_started') is True,
            'controller_state':last.get('controller_state','unknown'), 'controller_id':last.get('controller_scope',''),
            'target_identity':run['target_identity'],'size_bytes':run['size_bytes'],'scope_targets':scope,
            'native_certificate_sha256':primary_sha,'completed':success,
            'safe_to_reboot':bool(confirmed), 'achieved_classification':'unclassified',
            'phases':[{'phase':'native','method':run.get('execution_method',run['method']),'result':'passed' if success else 'failed',
                       'exit_code':max(0,min(255,code)), 'errors':0 if success else 1,'certificate_sha256':primary_sha}]}
    if run['method']=='nist_purge':detail['prng_followup']=bool(followup_enabled)
    # Any uncertain start, status query or interruption retains a hold. Never
    # infer completion from command acceptance or from the process's exit alone.
    if not success:
        detail['error']='Native sanitize did not produce confirmed completion and its original PDF; inspect retained evidence'
        return code,primary,log,False,detail
    if followup_enabled:
        transfer_certificate(api,run,'certificate',primary,primary_sha)
        selected=select_target(disk_list(),run['target_identity'],run['size_bytes'],run['allow_usb'])
        if selected['path']!=target['path']: raise RuntimeError('Drive path changed before the verified PRNG follow-up')
        followup=evidence/'followup';followup.mkdir(mode=0o700)
        software=dict(run,method='prng',verification='all',rounds=1,final_blank=False,prng='chacha20')
        next_code=run_nwipe(nwipe_args(software,target['path'],followup),api,run['token'],phase='Verified PRNG follow-up')
        next_log=(followup/'nwipe.log').read_text(errors='replace') if (followup/'nwipe.log').is_file() else ''
        next_pdf=original_drive_pdf(followup,target['path']);ok=passed(next_log,target['path'],next_code) and bool(next_pdf)
        next_sha=hashlib.sha256(next_pdf).hexdigest() if next_pdf else None
        phase={'phase':'followup','method':'prng','verification':'all','rounds':1,'final_blank':False,
               'result':'passed' if ok else 'failed','exit_code':max(0,min(255,next_code)), 'errors':0 if ok else 1,'certificate_sha256':next_sha}
        detail['phases'].append(phase);detail['completed']=ok
        if next_pdf:
            transfer_certificate(api,run,'certificate/followup',next_pdf,next_sha)
        if not ok:detail['error']='Native sanitize completed but its required verified PRNG follow-up failed'
        else:detail['achieved_classification']='purge'
        return next_code,primary,log+'\n--- Verified PRNG follow-up ---\n'+next_log,ok,detail
    if run['method']=='nist_purge':detail['achieved_classification']='purge'
    return code,primary,log,True,detail


def inventory(disks):
    if any(d.get('type')=='disk' and d.get('tran')!='usb' and not (d.get('serial') or '').strip() for d in disks):
        raise RuntimeError('An internal drive has no verifiable serial; inventory and erase authorization are held')
    cpu = next((line.split(':', 1)[1].strip() for line in Path('/proc/cpuinfo').read_text().splitlines() if line.startswith('model name')), '')
    memory = int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemTotal:'))) * 1024
    serial = dmi('product_serial')
    if not serial or serial.lower() in ('none', 'default string', 'to be filled by o.e.m.'):
        serial = dmi('product_uuid')
    return {'manufacturer': dmi('sys_vendor')[:120], 'model': dmi('product_name')[:120],
            'system_serial': serial[:200] or 'unreported', 'cpu': cpu[:300], 'memory_bytes': memory,
            'firmware': (dmi('bios_vendor') + ' ' + dmi('bios_version'))[:200],
            'disks': [{'identity': 'serial:' + d['serial'].strip(), 'size_bytes': int(d['size']),
                       'model': (d.get('model') or '').strip()[:200], 'transport': d.get('tran') or '',
                       'rotational': bool(d.get('rota')), 'capabilities': native_capabilities(d), 'health': disk_health(d)}
                      for d in disks if d.get('type') == 'disk' and (d.get('serial') or '').strip()]}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise RuntimeError('Server redirects are refused')


class API:
    def __init__(self, origin, ca):
        if not re.fullmatch(r'https://[A-Za-z0-9.-]+:[0-9]+', origin):
            raise RuntimeError('Configured HTTPS server origin required')
        self.origin = origin
        self.ca = ca
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=ca)))

    def ready(self, evidence, time_floor, attempts=6):
        """Only credential-free GETs may retry before consuming a boot ticket."""
        self.evidence=evidence
        for attempt in range(attempts):
            try:
                with self.opener.open(self.origin+'/health', timeout=8) as response:
                    if response.status != 200 or json.loads(response.read(4096)).get('status') != 'ok':
                        raise RuntimeError('Server health check did not pass')
                (evidence/'connection.json').write_text(json.dumps({'status':'ready','utc':timestamp(),'attempts':attempt+1}))
                print('HTTPS server ready; collecting drive information.',flush=True)
                return
            except urllib.error.URLError as exc:
                record=connection_error(exc)
                (evidence/'connection.json').write_text(json.dumps({'status':'failed','utc':timestamp(),'attempts':attempt+1,**record},indent=2))
                reason=exc.reason
                if isinstance(reason,ssl.SSLCertVerificationError):
                    if reason.verify_code in (9,10):
                        self.synchronize_clock(evidence,time_floor)
                        continue
                    raise RuntimeError(record['message']) from None
                if attempt+1 == attempts: raise RuntimeError(record['message']) from None
                print('Waiting for HTTPS connectivity ('+str(attempt+1)+'/'+str(attempts)+').',flush=True)
                time.sleep(2)
        raise RuntimeError('HTTPS readiness failed after clock synchronization; inspect connection.json')

    def synchronize_clock(self, evidence, time_floor):
        # Bootstrap time only from a TLS peer authenticated by the bundled CA
        # and hostname. No token or API mutation is sent. The subsequent health
        # check and every operational request enforce certificate dates again.
        if not time_floor: raise RuntimeError('PC clock differs from certificate validity; update ShredOS media or correct firmware time')
        context=ssl.create_default_context(cafile=self.ca)
        context.verify_flags |= 0x200000  # OpenSSL X509_V_FLAG_NO_CHECK_TIME, clock bootstrap only.
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect(),urllib.request.HTTPSHandler(context=context))
        with opener.open(self.origin+'/health',timeout=8) as response:
            server_time=parsedate_to_datetime(response.headers['Date']).timestamp()
        if not time_floor <= server_time <= time_floor+366*86400:
            raise RuntimeError('Authenticated server time is outside this media build window; update media or correct server time')
        previous=timestamp()
        time.clock_settime(time.CLOCK_REALTIME,server_time)
        (evidence/'clock.json').write_text(json.dumps({'previous_utc':previous,'synchronized_utc':timestamp(),'source':'CA-authenticated HTTPS health Date'},indent=2))
        print('Boot clock synchronized with the authenticated server; checking full TLS validity.',flush=True)

    def request(self, path, token, data=None, method='POST', pdf=False, namespace='erase'):
        body = data if pdf else json.dumps(data).encode() if data is not None else None
        headers = {'Authorization': 'Bearer ' + token}
        if body is not None:
            headers['Content-Type'] = 'application/pdf' if pdf else 'application/json'
        req = urllib.request.Request(self.origin + '/api/' + namespace + '/' + path, body, headers, method=method)
        try:
            with self.opener.open(req, timeout=30) as response:
                return json.loads(response.read(1024**2))
        except urllib.error.HTTPError as exc:
            # Only server-provided summary/field names, never echoed inputs,
            # credentials, request URLs or headers in console diagnostics.
            try:
                detail=json.loads(exc.read(4096)).get('detail')
                message=detail if isinstance(detail,str) else 'Invalid fields: '+', '.join('.'.join(map(str,d.get('loc',[]))) for d in detail or [])
            except (ValueError,TypeError):
                message='Request rejected'
            raise RuntimeError(f'Server rejected {namespace}/{path} ({exc.code}): {message}') from None
        except urllib.error.URLError as exc:
            if getattr(self,'evidence',None):
                (self.evidence/'connection.json').write_text(json.dumps({'status':'failed','utc':timestamp(),'phase':namespace+'/'+path,**connection_error(exc)},indent=2))
            raise


def connection_error(exc):
    reason=getattr(exc,'reason',exc)
    if isinstance(reason,ssl.SSLCertVerificationError):
        code=reason.verify_code
        message={9:'TLS certificate is not yet valid: check the PC clock',10:'TLS certificate expired: check the PC/server clock or update media',18:'TLS server certificate is untrusted',20:'TLS server CA differs from the boot media',62:'TLS server hostname differs from the boot media',64:'TLS server address differs from the boot media'}.get(code,'TLS certificate verification failed')
        return {'kind':'tls','verify_code':code,'message':message}
    if isinstance(reason,socket.gaierror): return {'kind':'dns','message':'Server name lookup failed; check DHCP DNS'}
    if isinstance(reason,(TimeoutError,socket.timeout)): return {'kind':'timeout','message':'HTTPS server timed out; check connectivity and firewall'}
    number=getattr(reason,'errno',None)
    message={errno.ENETUNREACH:'No route to the HTTPS server; check DHCP gateway',errno.EHOSTUNREACH:'HTTPS server is unreachable; check cable, VLAN and gateway',errno.ECONNREFUSED:'HTTPS server refused the connection; check server service and port',errno.ECONNRESET:'HTTPS connection reset; check server and network'}.get(number,'HTTPS connection failed; inspect connection.json and network.log')
    return {'kind':'network','errno':number,'message':message}


def prepare_logo(run, folder=Path('/etc/nwipe')):
    """Authenticated claim supplies a normalized 256px JPEG, scoped to this wipe."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / 'logo.jpg'
    value = run.get('branding_logo')
    if not value:
        path.unlink(missing_ok=True)
        return None
    encoded = value.get('jpeg_base64', '')
    if len(encoded) > 128000: raise RuntimeError('Certificate logo exceeds bound')
    payload = base64.b64decode(encoded, validate=True)
    if not payload.startswith(b'\xff\xd8') or not payload.endswith(b'\xff\xd9') or hashlib.sha256(payload).hexdigest() != value.get('sha256'):
        raise RuntimeError('Certificate logo integrity differs')
    path.write_bytes(payload)
    return payload


def original_drive_pdf(evidence, device):
    # Each invocation targets one explicit drive. Select only its upstream
    # per-drive filename; never silently substitute the system summary.
    pdfs = [p for p in evidence.glob('nwipe_report_*_device_'+Path(device).name+'.pdf') if not p.is_symlink()]
    if len(pdfs) != 1: return None
    payload = pdfs[0].read_bytes()
    if not payload.startswith(b'%PDF-') or b'%%EOF' not in payload[-1024:]: return None
    return payload


def nwipe_args(run, path, evidence):
    if run['method'] not in erase_policy.NWIPE or run['verification'] not in ('all', 'last', 'off') or not 1 <= run['rounds'] <= 16:
        raise RuntimeError('Unsupported erase policy')
    args = ['/usr/bin/nwipe', '--autonuke', '--nowait', '--method=' + run['method'],
            '--verify=' + run['verification'], '--rounds=' + str(run['rounds']),
            '--prng=' + run.get('prng', 'auto'),
            '--PDFreportpath=' + str(evidence), '--logfile=' + str(evidence / 'nwipe.log')]
    if not run['final_blank']:
        args.append('--noblank')
    if run.get('io_mode', 'auto') != 'auto':
        args.append({'direct': '--directio', 'cached': '--cachedio'}[run['io_mode']])
    if run.get('direction', 'forward') != 'forward': args.append('--' + run['direction'])
    if run.get('sync_blocks') is not None: args.append('--sync=' + str(run['sync_blocks']))
    if run.get('retry_io_errors') is False: args.append('--no-retry-on-io-errors')
    if run.get('abort_on_block_errors') is False: args.append('--no-abort-on-block-errors')
    # --autonuke operates only on this explicit whole-disk operand.
    return args + [path]


def passed(log, device, returncode):
    # nwipe's per-device summary derives this state from pass, verification and
    # sync errors. Exit status alone cannot prove an erase was completed.
    rows = re.findall(r'^\s*(!?)\s*' + re.escape(Path(device).name) + r'\s*\|\s*(Erased|-FAILED-|UABORTED|INSANITY)\s*\|', log, re.M)
    return returncode == 0 and rows == [('', 'Erased')]


def device_errors(log, device):
    rows = re.findall(r'^\s*!?\s*' + re.escape(Path(device).name) + r'\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|', log, re.M)
    return dict(zip(('pass_errors', 'verification_errors', 'sync_errors'), map(int, rows[0]))) if len(rows) == 1 else {}


def nwipe_progress(log, path):
    rows = re.findall(re.escape(path)+r': ([0-9]+(?:\.[0-9]+)?)%, round (\d+) of (\d+), pass (\d+) of (\d+), eta ([0-9:]+), ([^\n]*)', log)
    if not rows: return None, 'Erasing; waiting for nwipe progress'
    percent, current, total, pass_no, passes, eta, operation = rows[-1]
    if not 1 <= int(current) <= int(total): return None, 'Erasing; waiting for nwipe progress'
    # nwipe round_percent is cumulative across the complete wipe, including
    # all rounds, verification and final blanking. Keep its measured percentage.
    value = min(99, int(float(percent)))
    return value, f'Round {current}/{total} · pass {pass_no}/{passes} · ETA {eta} · {operation}'[:500]


def publish_progress(api, token, percent, detail):
    # nwipe owns the terminal while wiping; do not corrupt its ncurses layout.
    if not sys.stdout.isatty(): print(('['+str(percent)+'%] ' if percent is not None else '[working] ')+detail, flush=True)
    try: api.request('progress', token, {'percent':percent,'detail':detail[:500]})
    except (RuntimeError, urllib.error.URLError, TimeoutError): pass


def run_nwipe(args, api, token, *, phase=None):
    logfile = next((a.split('=',1)[1] for a in args if a.startswith('--logfile=')),None)
    logpath = Path(logfile) if logfile else None
    eventfile = next((a.split('=',1)[1] for a in args if a.startswith('--sanitize-events=')),None)
    with subprocess.Popen(args) as process:
        while True:
            try: return process.wait(timeout=5)
            except KeyboardInterrupt:
                # Ctrl+C reaches both foreground processes. Forward it when only
                # the companion received it, wait for nwipe to stop, then upload
                # failed evidence. Never leave a silent 'erasing' portal record.
                try: process.send_signal(signal.SIGINT)
                except ProcessLookupError: pass
                try: process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try: process.wait(timeout=10)
                    except subprocess.TimeoutExpired: process.kill(); process.wait()
                return 130
            except subprocess.TimeoutExpired:
                # Pinned nwipe 0.42 reports its measured round/pass/ETA on USR1.
                # Wait for its handler to exist before sending this signal.
                try:
                    status=Path('/proc/'+str(process.pid)+'/status').read_text()
                    blocked=re.search(r'^SigBlk:\s*([0-9a-fA-F]+)',status,re.M)
                    started=bool(blocked and int(blocked[1],16) & (1 << (signal.SIGUSR1-1)))
                    if started and not eventfile: process.send_signal(signal.SIGUSR1)
                except (OSError, ProcessLookupError, AttributeError): pass
                log = logpath.read_text(errors='replace')[-64000:] if logpath and logpath.is_file() else ''
                percent, detail = native_progress(Path(eventfile)) if eventfile else nwipe_progress(log, args[-1])
                if phase: detail = phase+' · '+detail
                if phase == 'Native sanitize' and percent is not None: percent = percent // 2
                elif phase == 'Verified PRNG follow-up' and percent is not None: percent = 50 + percent // 2
                publish_progress(api, token, percent, detail)



def transfer_certificate(api, run, path, payload, expected):
    # Transfers can be repeated idempotently; controller commands cannot.
    for attempt in range(10):
        try:
            ack=api.request(path,run['token'],payload,'PUT',pdf=True)
            if ack.get('sha256')!=expected:raise RuntimeError('Original PDF acknowledgement differs')
            return
        except (urllib.error.URLError,TimeoutError):
            if attempt==9:raise
            time.sleep(10)


def upload_evidence(api, run, result, pdf):
    # Retry transfers only. Never retry nwipe or its begin authorization.
    for attempt in range(10):
        try:
            if pdf:
                ack = api.request('certificate', run['token'], pdf, 'PUT', pdf=True)
                if ack['sha256'] != result['certificate_sha256']:
                    raise RuntimeError('Certificate acknowledgement differs')
            return api.request('result', run['token'], result)
        except (urllib.error.URLError, TimeoutError):
            if attempt == 9:
                raise
            time.sleep(10)


def main():
    guard()
    cmdline = dict(item.split('=', 1) for item in Path('/proc/cmdline').read_text().split() if '=' in item)
    if cmdline.get('repxe_audit') == '1':
        checks = {}
        for name, args in [('nwipe', ['/usr/bin/nwipe', '--version']), ('lsblk', ['lsblk', '--version']),
                           ('hdparm', ['hdparm', '-V']), ('nvme', ['nvme', 'version']), ('rtcwake', ['rtcwake', '--version'])]:
            check = subprocess.run(args, capture_output=True, text=True, timeout=30)
            checks[name] = {'code': check.returncode, 'output': check.stdout, 'error': check.stderr}
        print('REPXE_AUDIT ' + json.dumps({'python': sys.version, 'ssl': ssl.OPENSSL_VERSION, **checks}), flush=True)
        return
    ticket = cmdline.get('repxe_ticket', '')
    mac = cmdline.get('repxe_mac', '')
    if not re.fullmatch(r'[A-Za-z0-9_-]{32,256}', ticket) or not re.fullmatch(r'[0-9a-f]{2}(:[0-9a-f]{2}){5}', mac):
        raise RuntimeError('Run-specific boot ticket and MAC required; manual/default wiping is disabled')
    evidence = Path('/run/re-pxe/evidence')
    evidence.mkdir(mode=0o700, parents=True, exist_ok=True)
    prepare_network(mac, cmdline.get('repxe_mode') == 'intake',
                    cmdline.get('repxe_serial_sha256', ''), evidence)
    settings = json.loads(Path('/etc/re-pxe-server.json').read_text())
    api = API(settings['origin'], '/etc/re-pxe-ca.crt')
    api.ready(evidence,settings.get('time_floor',0))
    intake_token = None
    marker = Path('/run/re-pxe/erase-started')
    if marker.exists(): raise RuntimeError('This RAM session already started erasure; automatic retries are refused')
    if cmdline.get('repxe_mode') == 'intake':
        enrolled = api.request('claim',ticket,{'mac':mac,'agent_version':VERSION,'inventory':inventory(disk_list())},namespace='intake')
        intake_token = enrolled['token']
        print('PC specs saved. Waiting for the routine or technician. No erasure has started.',flush=True)
    completed = set()
    while True:
        if intake_token:
            state=api.request('status',intake_token,method='GET',namespace='intake')
            if state.get('routine_stage') in ('failed','cancelled','complete'): return
            if state.get('inventory_needed'):
                api.request('inventory',intake_token,{'mac':mac,'agent_version':VERSION,'inventory':inventory(disk_list())},namespace='intake')
                continue
            if not state.get('erase_ready'):
                if state['reboot_for_workflow']:
                    subprocess.run(['/sbin/reboot'],check=True); return
                time.sleep(5); continue
            fresh = disk_list()
            handoff=api.request('erase-ticket',intake_token,{'mac':mac,'agent_version':VERSION,'inventory':inventory(fresh)},namespace='intake')
            # Prevalidate every explicit whole-disk binding before the first write.
            for target in handoff['targets']:
                select_target(fresh,target['identity'],target['size_bytes'],target['allow_usb'])
            ticket=handoff['ticket']
        run=api.request('claim',ticket,{'mac':mac,'agent_version':VERSION,'inventory':inventory(disk_list())})
        if run['id'] in completed: raise RuntimeError('Duplicate erase run refused')
        if not completed:
            with marker.open('x') as stream: stream.write(timestamp())
        outcome=execute_run(api,run,evidence/run['id'])
        if not outcome: return
        completed.add(run['id'])
        if outcome=='reboot':
            print('All required erasure evidence saved. Restarting for the next routine step.',flush=True)
            subprocess.run(['/sbin/reboot'],check=True); return
        if not intake_token: return


def execute_run(api, run, evidence):
    try:
        if run['method'] not in METHODS: raise RuntimeError('Only nwipe methods are supported; republish the routine')
        target = select_target(disk_list(), run['target_identity'], run['size_bytes'], run['allow_usb'])
        path = Path(target['path'])
        before = path.stat()
        if not stat.S_ISBLK(before.st_mode):
            raise RuntimeError('Whole block disk required inside ShredOS')
        evidence.mkdir(mode=0o700,parents=True,exist_ok=False)
        health_before,smart_before=smart_health.collect(str(path))
        (evidence/'smart-before.json').write_text(smart_before or json.dumps(health_before,indent=2))
        print('Erasing '+run['target_identity']+' · '+(target.get('model') or '')+' · '+str(run['size_bytes'])+' bytes · '+run['method'],flush=True)
        logo = prepare_logo(run)
        batch=run.get('batch_targets') or [{'target_identity':run['target_identity'],'size_bytes':run['size_bytes'],'allow_usb':run['allow_usb'],'execution_method':run.get('execution_method',run['method'])}]
        fresh_disks=disk_list();unsupported=[]
        for binding in batch:
            bound=select_target(fresh_disks,binding['target_identity'],binding['size_bytes'],binding['allow_usb'])
            preflight=native_capabilities(bound,refresh=True)
            if preflight['sanitize_status']=='uncertain':
                raise RuntimeError('Batch controller preflight is uncertain; inspect evidence before any write')
            method=binding.get('execution_method')
            if method in erase_policy.NATIVE and method not in preflight['methods']:
                if preflight['sanitize_status']=='unsupported' or preflight['sanitize_status']=='ready':
                    unsupported.append({'identity':binding['target_identity'],'size_bytes':binding['size_bytes'],'method':method})
                else:raise RuntimeError('Native sanitize preflight is uncertain; inspect evidence before retrying')
        if unsupported:
            api.request('unsupported',run['token'],{'error':'Unsupported: a bound drive does not support the requested native sanitize',
                'evidence':{'operation_started':False,'controller_state':'unsupported','engine_version':NATIVE_VERSION,
                            'target_identity':run['target_identity'],'size_bytes':run['size_bytes'],'unsupported_targets':unsupported}})
            return True
        api.request('begin', run['token'], {})
        publish_progress(api,run['token'],0,'Starting '+run['method'])
        # Revalidate immediately before giving nwipe the explicitly selected disk.
        selected = select_target(disk_list(), run['target_identity'], run['size_bytes'], run['allow_usb'])
        if selected['path'] != str(path) or path.stat().st_rdev != before.st_rdev:
            raise RuntimeError('Disk binding changed before erasure')
        started = timestamp(); detail = {}; code = 0; pdf = None
        native = run.get('execution_method', run['method']) in erase_policy.NATIVE
        if native:
            code, pdf, log, success, detail = execute_native(api,run,target,evidence)
        else:
            software = dict(run, method=run.get('execution_method') or ('zero' if run['method']=='nist_clear' else run['method']))
            code = run_nwipe(nwipe_args(software, str(path), evidence), api, run['token'])
            logpath = evidence / 'nwipe.log'
            log = logpath.read_text(errors='replace') if logpath.is_file() else 'nwipe produced no device summary; exit code ' + str(code)
            pdf = original_drive_pdf(evidence, str(path))
            success = passed(log, str(path), code) and bool(pdf)
            detail = dict(engine='nwipe', operation=software['method'], execution_method=software['method'], completed=success,
                          verification=run['verification'], prng=run.get('prng', 'auto'), io_mode=run.get('io_mode', 'auto'),
                          device_errors=device_errors(log, str(path)))
            if success and run['method']=='nist_clear': detail['achieved_classification']='clear'
        if not success:
            counts = detail.get('device_errors',{})
            detail.setdefault('error', ('nwipe '+run['method']+' failed; exit '+str(code)+'; '+
                ', '.join(k.replace('_',' ')+' '+str(v) for k,v in counts.items()))[:1000])
        health_after,smart_after=smart_health.collect(str(path))
        (evidence/'smart-after.json').write_text(smart_after or json.dumps(health_after,indent=2))
        detail.update(smart_before=health_before,smart_after=health_after)
        finished = timestamp()
        result = {'target_identity': run['target_identity'], 'size_bytes': run['size_bytes'],
                  'method': run['method'], 'verification': run['verification'], 'rounds': run['rounds'],
                  'final_blank': run['final_blank'], 'result': 'passed' if success else 'failed',
                  'exit_code': min(255, max(0, code)), 'errors': 0 if success else max(1,sum(detail.get('device_errors', {}).values())),
                  'started': started, 'finished': finished, 'log': log[-64000:],
                  'run_id': run['id'], 'system_serial': run['system_serial'],
                  'settings_sha256': run['settings_sha256'], 'evidence': detail,
                  'certificate_sha256': hashlib.sha256(pdf).hexdigest() if pdf else None}
        result['evidence']['companion_version'] = VERSION
        result['evidence']['certificate_source'] = 'nwipe-original-per-drive'
        result['evidence']['logo_sha256'] = hashlib.sha256(logo).hexdigest() if logo else None
        (evidence / 'result.json').write_text(json.dumps(result, indent=2))
        ack = upload_evidence(api, run, result, pdf)
        print('Erasure evidence saved. Workflow stage: ' + ack['stage'], flush=True)
        if not success: return 'reboot' if ack.get('reboot_for_routine') else False
        while True:
            state = api.request('status', run['token'], method='GET')
            if state['reboot_for_windows'] or state.get('reboot_for_routine'):
                return 'reboot'
            if state['stage'] != 'awaiting-approval': return True
            time.sleep(5)
    except Exception as exc:
        # Failure reporting is best-effort; evidence stays in RAM and the server
        # remains held even if the network is unavailable. Do not reboot.
        try:
            ack = api.request('failure', run['token'], {'error': str(exc)[:1000] if isinstance(exc, RuntimeError) else 'Companion stopped: ' + type(exc).__name__})
        except Exception:
            pass
        raise


def inventory_serial():
    serial = dmi('product_serial')
    if not serial or serial.lower() in ('none', 'default string', 'to be filled by o.e.m.'):
        serial = dmi('product_uuid')
    return serial[:200] or 'unreported'


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Do not print request URLs/headers or run tokens in diagnostic output.
        print('re-pxe stopped: ' + (str(exc) if isinstance(exc, RuntimeError) else connection_error(exc)['message'] if isinstance(exc,urllib.error.URLError) else type(exc).__name__) + '. Inspect /run/re-pxe/evidence; no automatic erase retry or reboot.', flush=True)
        sys.exit(1)
