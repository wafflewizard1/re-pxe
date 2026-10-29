"""PXE tickets, device-scoped sessions and live dashboard projections."""
from . import driver_management, file_workspace, workflows, routines, wimboot_assets, windows_secure_boot
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Literal
import hashlib
import hmac
import json
import os
import re
import secrets
import uuid

from fastapi import Depends, Header, HTTPException, Query
from . import client_management, device_inventory, windows_controls
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field

DDL = '''
CREATE TABLE IF NOT EXISTS clients(
 id TEXT PRIMARY KEY,mac TEXT NOT NULL,ticket_hash TEXT NOT NULL,
 ticket_expires TEXT NOT NULL,token_hash TEXT,expires TEXT,
 manufacturer TEXT NOT NULL DEFAULT '',model TEXT NOT NULL DEFAULT '',
 targets TEXT NOT NULL DEFAULT '[]',selection TEXT,job_id TEXT,
 stage TEXT NOT NULL DEFAULT 'booting',received INTEGER NOT NULL DEFAULT 0,
 total INTEGER NOT NULL DEFAULT 0,rate REAL NOT NULL DEFAULT 0,
 created TEXT NOT NULL,seen TEXT NOT NULL,error TEXT NOT NULL DEFAULT '',
 profile_override TEXT,boot_policy TEXT,countdown_until TEXT);
CREATE INDEX IF NOT EXISTS clients_mac_created ON clients(mac,created);
CREATE TABLE IF NOT EXISTS pxe_settings(id INTEGER PRIMARY KEY CHECK(id=1),spec TEXT NOT NULL);
'''

def timestamp():
    return datetime.now(timezone.utc).isoformat()

def hashed(value):
    return hashlib.sha256(value.encode()).hexdigest()

class Input(BaseModel):
    model_config = ConfigDict(extra='forbid')

class Settings(Input):
    default_profile_id: str | None = Field(default=None,pattern=r'^[a-f0-9-]{36}$')
    automatic_disk_selection: bool = Field(default=True,strict=True)
    target_disk_policy: Literal['format-selected-disk','blank-raw'] = 'format-selected-disk'
    start_delay_seconds: int = Field(default=10,ge=0,le=300,strict=True)
    offline_hide_seconds: int = Field(default=300,ge=30,le=86400,strict=True)

class ClientConfiguration(Input):
    profile_id: str | None = Field(pattern=r'^[a-f0-9-]{36}$')

class Countdown(Input):
    action: Literal['start','cancel']

def initialize(db):
    db.executescript(DDL)
    columns={r['name'] for r in db.execute('PRAGMA table_info(clients)')}
    for column in ('profile_override','boot_policy','countdown_until'):
        if column not in columns:db.execute(f'ALTER TABLE clients ADD COLUMN {column} TEXT')
    client_management.initialize(db)
    device_inventory.initialize(db)
    windows_controls.initialize(db)
    db.execute('INSERT OR IGNORE INTO pxe_settings VALUES(1,?)',(Settings().model_dump_json(),))

def settings_for(db):
    return Settings.model_validate_json(db.execute('SELECT spec FROM pxe_settings WHERE id=1').fetchone()['spec'])

def selectable_profiles(db):
    return [{'id':r['id'],'name':json.loads(r['spec'])['name'],
             'windows_label':json.loads(r['spec'])['windows_label']}
            for r in db.execute('SELECT * FROM deployment_profiles WHERE enabled=1 ORDER BY created DESC')
            if json.loads(r['spec'])['selection_mode']=='choose-version']

class Target(Input):
    identity: str = Field(pattern=r'^serial:[^\x00-\x1f]{1,293}$')
    size_bytes: int = Field(gt=0, le=1024**5)
    model: str = Field(default='',max_length=200)

class Enroll(Input):
    inventory: device_inventory.Inventory | None = None
    manufacturer: str = Field(default='',max_length=120)
    model: str = Field(default='',max_length=120)
    client_version: str = Field(default='',pattern=r'^[0-9]+\.[0-9]+\.[0-9]+$')
    targets: list[Target] = Field(min_length=1,max_length=32)
    managed_selection: bool = Field(default=False,strict=True)

class Selection(Input):
    profile_id: str = Field(pattern=r'^[a-f0-9-]{36}$')
    target_identity: str = Field(pattern=r'^serial:[^\x00-\x1f]{1,293}$')

class OptionRequest(Input):
    target_identity: str = Field(default='',max_length=300)

class Heartbeat(Input):
    progress_percent: int | None = Field(default=None,ge=0,le=100,strict=True)
    stage: Literal['waiting-for-input','choosing','checking-disk','confirming','countdown','paused','preflight','preparing-disk',
                   'disk-partitioned','disk-prepared','downloading','verifying-image',
                   'applying-image','image-applied','boot-configured','recovery-registered',
                   'drivers-staged','agent-staged','awaiting-windows','failed']
    received_bytes: int = Field(default=0,ge=0,le=100*1024**3)
    total_bytes: int = Field(default=0,ge=0,le=100*1024**3)
    error: str = Field(default='',max_length=500)

class CompletionPulse(Input):
    stage: Literal['completing','ready-for-oobe','needs-operator']
    error: str = Field(default='',max_length=500)

def install(app, store, Role, allow, boot_options_for, profile_artifacts, create_job, report_credential, job_event, driver_manifest):
    from .app import NewJob, ReportCredential, JobEvent

    def credential(authorization: Annotated[str | None, Header()] = None):
        if not authorization or not re.fullmatch(r'Bearer [A-Za-z0-9_-]{32,256}',authorization):
            raise HTTPException(401,'Device credential required')
        return authorization[7:]

    def client(token: Annotated[str, Depends(credential)]):
        with store().db() as db:
            row=db.execute('SELECT * FROM clients WHERE token_hash=?',(hashed(token),)).fetchone()
        if not row or row['stage']=='cancelled' or not row['expires'] or row['expires'] <= timestamp():
            raise HTTPException(401,'Device session expired')
        return dict(row)

    Client = Annotated[dict, Depends(client)]
    Credential = Annotated[str, Depends(credential)]

    def enabled_profile(db,uid):
        if uid is not None and uid not in {p['id'] for p in selectable_profiles(db)}:
            raise HTTPException(422,'Choose an enabled configuration with client version selection')

    @app.get('/api/pxe-settings')
    def read_settings(role: Role):
        allow(role)
        with store().db() as db:return settings_for(db).model_dump()

    @app.put('/api/pxe-settings')
    def update_settings(spec: Settings,role: Role):
        allow(role)
        with store().db() as db:
            enabled_profile(db,spec.default_profile_id)
            db.execute('UPDATE pxe_settings SET spec=? WHERE id=1',(spec.model_dump_json(),))
        return spec.model_dump()

    @app.put('/api/pxe-settings/default')
    def update_default(spec: ClientConfiguration,role: Role):
        allow(role)
        with store().db() as db:
            enabled_profile(db,spec.profile_id)
            settings=settings_for(db).model_copy(update={'default_profile_id':spec.profile_id})
            db.execute('UPDATE pxe_settings SET spec=? WHERE id=1',(settings.model_dump_json(),))
        return settings.model_dump()

    @app.put('/api/clients/{uid}/configuration')
    def configure_client(uid: str,spec: ClientConfiguration,role: Role):
        allow(role)
        with store().db() as db:
            enabled_profile(db,spec.profile_id)
            row=db.execute('SELECT * FROM clients WHERE id=?',(uid,)).fetchone()
            if not row:raise HTTPException(404,'Client not found')
            if routines.for_boot(db,uid):raise HTTPException(409,'The routine configuration is pinned')
            if workflows.for_boot(db, uid):raise HTTPException(409,'Workflow configuration is pinned')
            latest=db.execute('SELECT id FROM clients WHERE mac=? ORDER BY created DESC LIMIT 1',(row['mac'],)).fetchone()
            if row['token_hash'] or row['stage']!='booting' or row['job_id'] or row['ticket_expires']<=timestamp() or latest['id']!=uid:
                raise HTTPException(409,'Windows selection has started; configuration is locked')
            db.execute('UPDATE clients SET profile_override=? WHERE id=?',(spec.profile_id,uid))
        return {'clientId':uid,'profileId':spec.profile_id}

    def available(*, optional=False):
        path=store().root/'boot/pxe/current.json'
        if optional and not path.is_file():return None
        if not path.is_file():raise HTTPException(503,'PXE media not published')
        media=json.loads(path.read_text())
        if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+',media['version']):raise HTTPException(503,'Invalid PXE media')
        return media

    def choices(row,target,db=None):
        if target and target not in {t['identity'] for t in json.loads(row['targets'])}:
            raise HTTPException(403,'Target is outside this device session')
        def scoped(connection):
            routine_choices=routines.windows_choices(connection,row,target,profile_artifacts)
            if routine_choices is not None:return routine_choices
            result=boot_options_for(connection,row['manufacturer'],row['model'],target)
            run=workflows.for_boot(connection,row['id'])
            if run:
                if target and target!=run['target']:raise HTTPException(403,'Only the erased disk belongs to this workflow')
                pinned=json.loads(run['spec'])['profile_id']
                result={**result,'choices':[p for p in result['choices'] if p['id']==pinned],'selectedProfileId':pinned}
            return result
        if db is not None:return scoped(db)
        with store().db() as connection:return scoped(connection)

    def current_selection(row):
        if not row['selection']:raise HTTPException(409,'Choose Windows and a target first')
        selected=json.loads(row['selection'])
        current=choices(row,selected['target_identity'])
        match=next((p for p in current['choices'] if p['id']==selected['profile']['id']),None)
        if not match or not same_profile(match, selected['profile']):
            raise HTTPException(409,'Configuration changed; choose it again')
        if selected['profile'].get('driver_source') == 'automatic' and selected['driverManifest']:
            pinned = driver_manifest(selected['profile']['driver_id'], 'deploy')
            if (pinned != selected['driverManifest'] or not pinned['review']['hardwareTestPassed'] or
                pinned['architecture'] != 'amd64' or pinned['windowsBuild'] != selected['profile']['windows_build'] or
                pinned['manufacturer'].casefold() != row['manufacturer'].casefold() or
                pinned['model'].casefold() != row['model'].casefold()):
                raise HTTPException(409, 'Pinned driver package is no longer valid for this client')
        return selected

    def same_profile(current, pinned):
        if pinned.get('driver_source') == 'automatic':
            current = {**current, 'driver_id': pinned['driver_id']}
        return current == pinned

    @app.get('/pxe/boot.ipxe',response_class=PlainTextResponse)
    def boot(mac: str, windows_only: bool=False):
        return boot_script(mac, custom=True, windows_only=windows_only)

    @app.get('/pxe/managed.ipxe',response_class=PlainTextResponse)
    def managed_boot(mac: str, windows_only: bool=False):
        return boot_script(mac, custom=False, windows_only=windows_only)

    def boot_script(mac, custom, windows_only=False):
        if not re.fullmatch(r'[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}',mac):raise HTTPException(422,'MAC required')
        # Network-first firmware can return here after the automatic WinPE
        # restart. Keep the active completion job bound to its installed disk.
        with store().db() as db:
            pending_job=client_management.blocking_job(db,mac.lower())
        if pending_job:
            return PlainTextResponse(f'#!ipxe\necho re-pxe: Windows driver completion is pending. Booting the installed disk.\necho Job: {pending_job}\necho Open the portal Jobs tab to inspect or reset this machine for reinstall.\nexit\n',
                                     headers={'Cache-Control':'no-store'})
        address=os.environ.get('REPXE_ADDRESS','127.0.0.1')
        if not re.fullmatch(r'[A-Za-z0-9.-]+',address):raise HTTPException(503,'Invalid server address')
        mac=mac.lower()
        with store().db() as db:
            routines.sync(db)
            routine_mode=routines.enabled(db)
            current_routine=routines.latest(db,mac)
        windows_only=windows_only or bool(current_routine and current_routine['windows_only'] and current_routine['stage'] not in routines.TERMINAL)
        if windows_only:
            if not routine_mode:
                return routines.script_response('#!ipxe\necho Windows-only Secure Boot requires routine boot to be enabled in the portal.\nprompt Press any key to return to firmware\nexit\n')
            if current_routine and current_routine['stage'] not in routines.TERMINAL and not windows_secure_boot.supported(json.loads(current_routine['spec'])):
                return routines.script_response('#!ipxe\necho The active routine needs erasure or custom boot.\necho Use the regular PXE entry for that routine, or cancel it in the portal and choose a Windows-only routine.\nprompt Press any key to return to firmware\nexit\n')
        if custom and routine_mode and (not current_routine or current_routine['stage'] in routines.TERMINAL):
            if windows_only:windows_secure_boot.load(store().root)
            return routines.script_response(routines.menu_script(store(),mac,address,idle=bool(current_routine),windows_only=windows_only))
        routine_script,routine_run,routine_handled=routines.routing(store(),mac,address)
        if routine_script:return routines.script_response(routine_script)
        if routine_mode and not custom and not routine_handled:
            return routines.script_response(routines.menu_script(store(),mac,address,idle=True,windows_only=windows_only))
        workflow_script,workflow_run=(None,None) if routine_handled else workflows.boot_decision(store(),mac,address)
        if workflow_script:
            return PlainTextResponse(workflow_script,headers={'Cache-Control':'no-store'})
        media=windows_secure_boot.load(store().root)['windowsMedia'] if windows_only else available(optional=bool(routine_run))
        if custom and not windows_only and not workflow_run and file_workspace.boot_settings(store())['enabled']:
            # Completion guard above applies to custom menus as well. No device
            # enrollment/ticket is created until managed imaging is selected.
            return PlainTextResponse(f'''#!ipxe
set re-pxe-server http://{address}:8089
set re-pxe-mac {mac.lower()}
chain ${{re-pxe-server}}/custom/scripts/menu.ipxe || chain ${{re-pxe-server}}/pxe/managed.ipxe?mac=${{re-pxe-mac}}
''',headers={'Cache-Control':'no-store'})
        uid=str(uuid.uuid4());ticket=secrets.token_urlsafe(48);created=timestamp()
        with store().db() as db:
            active=db.execute('SELECT count(*) FROM clients WHERE ticket_expires>?',(created,)).fetchone()[0]
            if active>=256:raise HTTPException(429,'Too many pending boots')
            db.execute('INSERT INTO clients(id,mac,ticket_hash,ticket_expires,created,seen) VALUES(?,?,?,?,?,?)',
                       (uid,mac.lower(),hashed(ticket),(datetime.now(timezone.utc)+timedelta(minutes=15)).isoformat(),created,created))
            if workflow_run:workflows.bind_boot(db,uid,workflow_run)
            if routine_run:routines.bind_boot(db,uid,routine_run)
        if media is None:
            with store().db() as db:
                run = routines.for_boot(db, uid)
                step = json.loads(run['spec'])['steps'][run['step']]
                image, _ = profile_artifacts(db, step['profile'])
            return PlainTextResponse(f'''#!ipxe
kernel --name wimboot http://{address}:8089/pxe/routines/wimboot quiet index={image['image_index']}
initrd --name re-pxe-boot.json http://{address}:8089/pxe/config/{ticket} re-pxe-boot.json
initrd --name boot.wim http://{address}:8089/pxe/routines/image/{ticket} boot.wim
boot
''',headers={'Cache-Control':'no-store'})
        directory=media.get('directory',media['version'])
        if not re.fullmatch(r'(candidate-)?[0-9]+\.[0-9]+\.[0-9]+(-[a-f0-9]{12})?',directory):raise HTTPException(503,'Invalid PXE path')
        base=f'http://{address}:8089/pxe/{directory}'
        return PlainTextResponse(f'''#!ipxe
kernel --name wimboot {base}/wimboot quiet
initrd --name BCD {base}/BCD BCD
initrd --name boot.sdi {base}/boot.sdi boot.sdi
initrd --name bootx64.efi {base}/bootx64.efi bootx64.efi
initrd --name re-pxe-boot.json http://{address}:8089/pxe/config/{ticket} re-pxe-boot.json
initrd --name boot.wim {base}/boot.wim boot.wim
boot
''',headers={'Cache-Control':'no-store'})

    @app.get('/pxe/routines/wimboot')
    def uploaded_wim_loader():
        path = wimboot_assets.ASSETS / 'wimboot'
        if not path.is_file() or wimboot_assets.digest(path) != wimboot_assets.BINARY_SHA256:
            raise HTTPException(503, 'Bundled wimboot is missing or damaged; rebuild the server image')
        return FileResponse(path, media_type='application/octet-stream', headers={'Cache-Control':'no-store'})

    @app.get('/pxe/routines/wimboot/{asset}')
    def uploaded_wim_loader_source(asset: str):
        if asset not in ('wimboot-source.tar.gz', 'COPYING-wimboot', 'manifest.json'):
            raise HTTPException(404, 'Asset not found')
        path = wimboot_assets.ASSETS / asset
        if not path.is_file():raise HTTPException(404, 'Asset not found')
        return FileResponse(path, media_type='application/octet-stream', filename=asset)

    @app.get('/pxe/routines/image/{ticket}')
    def uploaded_wim_image(ticket: str):
        with store().db() as db:
            row = db.execute('SELECT * FROM clients WHERE ticket_hash=?', (hashed(ticket),)).fetchone()
            if not row or row['ticket_expires'] <= timestamp() or row['token_hash']:
                raise HTTPException(401, 'Boot ticket expired or consumed')
            run = routines.for_boot(db, row['id'])
            if not run or run['stage'] != 'windows-boot' or run['client_id'] != row['id']:
                raise HTTPException(409, 'Routine Windows boot is no longer active')
            step = json.loads(run['spec'])['steps'][run['step']]
            current = db.execute('SELECT enabled FROM deployment_profiles WHERE id=?', (step['profile_id'],)).fetchone()
            if not current or not current['enabled']:raise HTTPException(409, 'Routine configuration withdrawn')
            image, _ = profile_artifacts(db, step['profile'])
        path = store().root / 'images/published' / image['id'] / 'image.wim'
        if not path.is_file() or path.stat().st_size != image['size']:
            raise HTTPException(409, 'Published WIM is unavailable')
        return FileResponse(path, media_type='application/octet-stream', filename='boot.wim',
                            headers={'Cache-Control':'no-store'})

    @app.get('/pxe/config/{ticket}')
    def boot_config(ticket: str):
        with store().db() as db:
            row=db.execute('SELECT * FROM clients WHERE ticket_hash=?',(hashed(ticket),)).fetchone()
        if not row or row['ticket_expires']<=timestamp() or row['token_hash']:
            raise HTTPException(401,'Boot ticket expired or consumed')
        address=os.environ.get('REPXE_ADDRESS','127.0.0.1')
        return JSONResponse({'schemaVersion':1,'server':f'https://{address}:8443','ticket':ticket,'serverUtc':timestamp()},headers={'Cache-Control':'no-store'})

    @app.post('/api/pxe/enroll',status_code=201)
    def enroll(spec: Enroll,ticket: Credential):
        if len({t.identity for t in spec.targets})!=len(spec.targets):raise HTTPException(422,'Duplicate target identity')
        with store().db() as db:
            initial=db.execute('SELECT * FROM clients WHERE ticket_hash=?',(hashed(ticket),)).fetchone()
            if not initial or initial['ticket_expires']<=timestamp() or initial['token_hash']:
                raise HTTPException(401,'Boot ticket expired or consumed')
        token=secrets.token_urlsafe(48);expires=(datetime.now(timezone.utc)+timedelta(hours=12)).isoformat()
        with store().db() as db:
            row=db.execute('SELECT * FROM clients WHERE ticket_hash=?',(hashed(ticket),)).fetchone()
            if not row or row['ticket_expires']<=timestamp() or row['token_hash']:
                raise HTTPException(401,'Boot ticket expired or consumed')
            settings=settings_for(db);target=spec.targets[0].identity
            client_version=tuple(map(int,spec.client_version.split('.'))) if spec.client_version else (0,0,0)
            routine=routines.enforce_enroll(db,row['id'],spec.targets,spec.managed_selection,client_version)
            if routine and routine['inventory'] and (spec.inventory is not None or client_version >= (0,12,11)):
                expected=json.loads(routine['inventory'])['system_serial']
                if not spec.inventory or spec.inventory.system.serial!=expected: raise HTTPException(409,'PC serial changed since ShredOS discovery')
            workflow=workflows.enforce_boot(db,row['id'],spec.targets)
            if workflow:
                if not spec.managed_selection:raise HTTPException(409,'Workflow requires managed PXE media')
                target=workflow['target']
            client_version=tuple(map(int,spec.client_version.split('.'))) if spec.client_version else (0,0,0)
            if workflow and target!=spec.targets[0].identity:
                raise HTTPException(409,'Published WinPE automatically selects its first disk; this erased target needs manual media support before installation')
            if settings.target_disk_policy=='format-selected-disk' and client_version < (0,10,0):
                raise HTTPException(409,'PXE media must be updated before full-disk formatting is enabled')
            requested=row['profile_override'] or settings.default_profile_id
            if not spec.managed_selection and requested:
                raise HTTPException(409,'Server-managed selection requires updated PXE media')
            policy={'managedSelection':spec.managed_selection,'clientVersion':spec.client_version or '0.0.0','automaticDiskSelection':spec.managed_selection and settings.automatic_disk_selection,
                    'startDelaySeconds':settings.start_delay_seconds,'targetDiskPolicy':settings.target_disk_policy,
                    'profileId':None,'targetIdentity':target}
            if workflow:
                policy.update(automaticDiskSelection=True,targetDiskPolicy='format-selected-disk',targetIdentity=target)
            if routine:
                target=routine['target'] or ''
                step=json.loads(routine['spec'])['steps'][routine['step']]
                if step.get('finish_options') is not None and client_version<(0,13,0):raise HTTPException(409,'Routine Finish options require WinPE 0.13.0 or newer')
                if not step.get('verify_image',True) and client_version<(0,12,16):raise HTTPException(409,'Skipping Windows image verification requires WinPE 0.12.16 or newer')
                manual=step['target_policy']=='manual' or step['target_policy']=='auto-unless-multiple' and not target
                policy.update(automaticDiskSelection=not manual,startDelaySeconds=5 if step['target_policy'] in ('first-disk','manual','auto-unless-multiple') else 0,targetIdentity=target,targetDiskPolicy='format-selected-disk',manualDiskSelection=manual,verifyImage=step.get('verify_image',True))
                if step.get('finish_options') is not None:policy['finishOptions']=step['finish_options']
                if routine['windows_only']:policy['windowsOnly']=True
            if spec.managed_selection:
                options=routines.windows_choices(db,{**dict(row),'manufacturer':spec.manufacturer,'model':spec.model},target,profile_artifacts) if routine else boot_options_for(db,spec.manufacturer,spec.model,target)
                policy['profileId']=requested or options['selectedProfileId'] or (options['choices'][0]['id'] if options['choices'] else None)
                chosen=next((p for p in options['choices'] if p['id']==policy['profileId']),None)
                if chosen and chosen['driver_source']=='automatic' and client_version < (0,11,0):
                    raise HTTPException(409,'Automatic drivers require updated PXE media (0.11.0 or newer)')
            db.execute("UPDATE clients SET token_hash=?,expires=?,manufacturer=?,model=?,targets=?,stage='choosing',seen=?,boot_policy=? WHERE id=?",
                       (hashed(token),expires,spec.manufacturer,spec.model,json.dumps([t.model_dump() for t in spec.targets]),timestamp(),json.dumps(policy),row['id']))
            if spec.inventory is not None:
                db.execute('INSERT INTO client_inventory VALUES(?,?,?)',(row['id'],spec.inventory.model_dump_json(),timestamp()))
            if policy.get('manualDiskSelection'): db.execute("UPDATE clients SET stage='waiting-for-input' WHERE id=?",(row['id'],))
        return {'schemaVersion':1,'clientId':row['id'],'token':token,'expires':expires,'automation':policy}

    @app.post('/api/pxe/options')
    def options(spec: OptionRequest,row: Client):
        return choices(row,spec.target_identity)

    @app.post('/api/pxe/select')
    def select(spec: Selection,row: Client):
        if row['job_id']:raise HTTPException(409,'This session already has a deployment')
        with store().db() as db:
            # Check latest state under the same transaction as the pinned selection.
            row=dict(client_management.active_session(db,row['id']))
            if row['job_id'] or row['countdown_until']:raise HTTPException(409,'Selection is already locked')
            policy=json.loads(row['boot_policy']) if row['boot_policy'] else {}
            if policy.get('managedSelection') and spec.profile_id!=policy['profileId']:
                raise HTTPException(409,'The dashboard configuration is locked for this boot')
            if policy.get('automaticDiskSelection') and spec.target_identity!=policy['targetIdentity']:
                raise HTTPException(409,'Automatic deployment is limited to the first disk')
            routines.select_windows_target(db,row['id'],spec.target_identity)
            profile=next((p for p in choices(row,spec.target_identity,db)['choices'] if p['id']==spec.profile_id),None)
            if not profile:raise HTTPException(409,'Configuration unavailable for this device')
            if profile['driver_source']=='automatic' and tuple(map(int, policy.get('clientVersion','0.0.0').split('.'))) < (0,11,0):
                raise HTTPException(409,'Automatic drivers require updated PXE media (0.11.0 or newer)')
            image,drivers=profile_artifacts(db,profile)
        selected={'verifyImage':policy.get('verifyImage',True),'profile':profile,'target_identity':spec.target_identity,'target_policy':policy.get('targetDiskPolicy','blank-raw'),'image':image,
                  'driverManifest':driver_manifest(profile['driver_id'],'deploy') if profile['driver_id'] else None}
        if 'finishOptions' in policy:selected['finishOptions']=policy['finishOptions']
        with store().db() as db:
            client_management.active_session(db,row['id'])
            routines.select_windows_target(db,row['id'],spec.target_identity)
            updated=db.execute('UPDATE clients SET selection=?,stage=\'checking-disk\',seen=? WHERE id=? AND job_id IS NULL AND countdown_until IS NULL',
                               (json.dumps(selected),timestamp(),row['id'])).rowcount
            if not updated:raise HTTPException(409,'Session already started')
        return selected

    @app.post('/api/pxe/countdown')
    def countdown(spec: Countdown,row: Client):
        with store().db() as db:
            row=dict(client_management.active_session(db,row['id']))
            policy=json.loads(row['boot_policy']) if row['boot_policy'] else {}
            if row['job_id'] or not row['selection'] or not policy.get('automaticDiskSelection'):
                raise HTTPException(409,'No automatic installation is ready')
            if spec.action=='start' and row['countdown_until']:
                raise HTTPException(409,'Countdown already running')
            deadline=(datetime.now(timezone.utc)+timedelta(seconds=policy['startDelaySeconds'])).isoformat() if spec.action=='start' else None
            db.execute('UPDATE clients SET countdown_until=?,stage=?,seen=? WHERE id=?',
                       (deadline,'countdown' if deadline else 'paused',timestamp(),row['id']))
        return {'accepted':True,'startDelaySeconds':policy['startDelaySeconds'],'deadline':deadline}

    @app.post('/api/pxe/start',status_code=201)
    def start(row: Client):
        selected=current_selection(row)
        # Serialize job allocation and session binding in one database transaction.
        with store().db() as db:
            latest=client_management.active_session(db,row['id'])
            if latest['job_id']:raise HTTPException(409,'A deployment already exists; no destructive retries')
            if latest['expires']<=timestamp():raise HTTPException(401,'Device session expired')
            workflow=workflows.for_boot(db,row['id'])
            routine=routines.for_boot(db,row['id'])
            active=db.execute("SELECT id FROM workflow_runs WHERE mac=? AND stage NOT IN ('complete','failed','cancelled') ORDER BY created DESC LIMIT 1",(latest['mac'],)).fetchone()
            if active and (not workflow or active['id']!=workflow['id']) and (not routine or active['id']!=routine['workflow_id']):raise HTTPException(409,'Erasure workflow must finish first')
            if workflow and workflow['stage']!='install-ready':raise HTTPException(409,'Workflow installation already started or cancelled')
            if latest['selection']!=row['selection']:raise HTTPException(409,'Selection changed before deployment')
            compatible=choices(dict(latest),selected['target_identity'],db)['choices']
            if not any(same_profile(p,selected['profile']) for p in compatible):raise HTTPException(409,'Configuration is no longer available')
            policy=json.loads(latest['boot_policy']) if latest['boot_policy'] else {}
            if policy.get('automaticDiskSelection') and (not latest['countdown_until'] or latest['countdown_until']>timestamp()):
                raise HTTPException(409,'The automatic installation countdown has not finished')
            uid=str(uuid.uuid4());at=timestamp()
            db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?)',(uid,'created',selected['profile']['image_id'],selected['target_identity'],row['model'],at,at,None))
            db.execute('INSERT INTO events(job_id,stage,detail,timestamp) VALUES(?,?,?,?)',(uid,'created','PXE deployment',at))
            db.execute('UPDATE clients SET job_id=?,seen=? WHERE id=?',(uid,at,row['id']))
            routines.started(db,row['id'],uid)
            if workflow:
                db.execute('UPDATE workflow_runs SET job_id=? WHERE id=?',(uid,workflow['id']))
                workflows.event(db,workflow['id'],'installing','Windows deployment started on the erased disk')
        cap=report_credential(uid,ReportCredential(),'deploy')
        return {'id':uid,'stage':'created','reportCredential':cap}

    @app.post('/api/pxe/event')
    def event(spec: JobEvent,row: Client):
        if not row['job_id']:raise HTTPException(409,'No deployment job')
        return job_event(row['job_id'],spec,'deploy')

    @app.post('/api/clients/{uid}/disk')
    def portal_disk(uid: str, spec: Selection, role: Role):
        allow(role)
        with store().db() as db:
            row=client_management.active_session(db,uid)
            policy=json.loads(row['boot_policy']) if row['boot_policy'] else {}
            if not policy.get('manualDiskSelection') or row['stage']!='waiting-for-input' or row['selection'] or row['job_id']:
                raise HTTPException(409,'This PC is no longer waiting for a disk selection')
            if spec.profile_id!=policy['profileId']: raise HTTPException(409,'Routine Windows preset is locked')
            routines.select_windows_target(db,uid,spec.target_identity)
            policy.update(targetIdentity=spec.target_identity,automaticDiskSelection=True)
            db.execute('UPDATE clients SET boot_policy=? WHERE id=?',(json.dumps(policy),uid))
        return {'accepted':True}

    @app.post('/api/pxe/heartbeat')
    def heartbeat(spec: Heartbeat,row: Client):
        if spec.progress_percent is not None and spec.stage!='applying-image':raise HTTPException(422,'Installation percentage requires applying-image stage')
        if spec.received_bytes>spec.total_bytes:raise HTTPException(422,'Transfer count exceeds size')
        with store().db() as db:
            latest=client_management.active_session(db,row['id'])
            previous=datetime.fromisoformat(latest['seen']);at=datetime.now(timezone.utc)
            if spec.progress_percent is not None:
                job=db.execute('SELECT stage FROM jobs WHERE id=?',(latest['job_id'],)).fetchone()
                if not job or job['stage'] in client_management.TERMINAL:raise HTTPException(409,'No active installation job')
                db.execute('''INSERT INTO client_install_progress VALUES(?,?,?,?)
                    ON CONFLICT(client_id) DO UPDATE SET percent=MAX(percent,excluded.percent),captured_at=excluded.captured_at
                    WHERE job_id=excluded.job_id''',(row['id'],latest['job_id'],spec.progress_percent,at.isoformat()))

            elapsed=max(.1,(at-previous).total_seconds())
            if spec.received_bytes<latest['received']:raise HTTPException(409,'Transfer count moved backwards')
            rate=max(0,spec.received_bytes-latest['received'])/elapsed
            db.execute('UPDATE clients SET stage=?,received=?,total=?,rate=?,seen=?,error=? WHERE id=?',
                       (spec.stage,spec.received_bytes,spec.total_bytes,rate,at.isoformat(),spec.error,row['id']))
            policy=json.loads(latest['boot_policy']) if latest['boot_policy'] else {}
        return {'accepted':True,'automation':policy}

    @app.get('/api/pxe/image')
    def image(row: Client):
        selected=current_selection(row)
        if not row['job_id']:raise HTTPException(409,'Confirm and start deployment first')
        return FileResponse(store().root/'images/published'/selected['profile']['image_id']/'image.wim',media_type='application/octet-stream')

    @app.get('/api/pxe/driver/{index}')
    def driver(index: int,row: Client):
        selected=current_selection(row);manifest=selected['driverManifest']
        if not row['job_id'] or not manifest or index<0 or index>=len(manifest['files']):raise HTTPException(404,'Driver file unavailable')
        entry=manifest['files'][index]
        with store().db() as db:
            driver_management.archive_used(db, manifest['packageId'], row['job_id'])
        return FileResponse(store().root/'drivers/packages'/manifest['packageId']/'files'/entry['path'],media_type='application/octet-stream')

    @app.post('/api/client-jobs/{uid}/heartbeat')
    def completion_pulse(uid: str,spec: CompletionPulse,token: Credential):
        with store().db() as db:
            cap=db.execute('SELECT * FROM report_credentials WHERE job_id=?',(uid,)).fetchone()
            if not cap or not hmac.compare_digest(cap['token_hash'],hashed(token)) or cap['expires']<=timestamp() or cap['payload_hash']:
                raise HTTPException(401,'Active job credential required')
            job=db.execute('SELECT stage FROM jobs WHERE id=?',(uid,)).fetchone()
            if not job or job['stage'] in client_management.TERMINAL:raise HTTPException(409,'Job has ended')
            row=db.execute('SELECT id FROM clients WHERE job_id=?',(uid,)).fetchone()
            if not row:raise HTTPException(404,'PXE client not found')
            db.execute('UPDATE clients SET stage=?,seen=?,error=?,rate=0 WHERE id=?',(spec.stage,timestamp(),spec.error,row['id']))
        return {'accepted':True,'jobId':uid}

    client_management.install(app,store,Role,allow,settings_for)
    windows_controls.install(app,store,Role,allow,Credential)

    @app.get('/api/dashboard')
    def dashboard(role: Role,view: client_management.VIEWS='current',search: str=Query(default='',max_length=200),
                  page: int=Query(default=1,ge=1),page_size: int=Query(default=50,ge=1,le=200)):
        allow(role)
        at=datetime.now(timezone.utc)
        with store().db() as db:
            routines.sync(db)
            settings=settings_for(db);profiles=selectable_profiles(db)
            client_data=client_management.listing(db,settings,at,view,search,page,page_size)
            states={r['stage']:r['n'] for r in db.execute('SELECT stage,count(*) n FROM jobs GROUP BY stage')}
            completed_today=db.execute("SELECT count(*) FROM jobs WHERE stage='complete' AND updated>=?",(at.replace(hour=0,minute=0,second=0,microsecond=0).isoformat(),)).fetchone()[0]
            from . import job_reports
            recent=job_reports.list_reports(db,sort='processed',order='desc',page_size=8)['reports']
            routine_data=routine_dashboard(store(),view,search,db,at)
        network=None;path=store().root/'database/server-network.json'
        if path.is_file():
            try:
                data=json.loads(path.read_text())
                if (at-datetime.fromisoformat(data['sampled_at'])).total_seconds()<15:network=data
            except (ValueError,KeyError,OSError):pass
        media=store().root/'boot/pxe/current.json'
        return {'sampled_at':at.isoformat(),**client_data,
                'completed_today':completed_today,'completed':states.get('complete',0),
                'failed':states.get('failed',0),'client_bytes_per_second':sum(c['bytes_per_second'] for c in client_data['clients']),
                'settings':settings.model_dump(),'configurations':profiles,
                'routines':routine_data,
                'attention_count':routine_data['counts']['attention'] + client_data['client_counts']['standalone_attention'],
                'network':network,'recent_jobs':recent,'pxe':json.loads(media.read_text()) if media.is_file() else None,
                'windows_secure_boot':windows_secure_boot.status(store().root)}


def routine_dashboard(store,view='current',search='',db=None,at=None):
    from . import job_reports
    at=at or datetime.now(timezone.utc)
    with (store.db() if db is None else nullcontext(db)) as db:
        settings=dict(db.execute('SELECT enabled,default_id FROM routine_settings WHERE id=1').fetchone())
        choices=[{'id':r['routine_id'],'name':json.loads(r['spec'])['name'],'version':r['version'], 'has_windows':any(s['kind']=='windows' for s in json.loads(r['spec'])['steps'])} for r in db.execute('SELECT v.* FROM routines r JOIN routine_versions v ON v.routine_id=r.id AND v.version=r.published_version ORDER BY r.created')]
        after=(at-timedelta(seconds=settings_for(db).offline_hide_seconds)).isoformat() if view=='online' else None
        data=job_reports.list_reports(db,search,{'current':'pending','online':'pending'}.get(view,view),source='routine',seen_after=after,include_removed=False)
        active=[routines.projection(db,db.execute('SELECT * FROM routine_runs WHERE id=?',(r['run_id'],)).fetchone()) for r in data['reports']]
        counts={r['stage']:r['n'] for r in db.execute('SELECT stage,count(*) n FROM routine_runs WHERE id NOT IN (SELECT run_id FROM routine_removed) GROUP BY stage')}
        counts['active']=sum(v for k,v in counts.items() if k not in routines.TERMINAL)
        counts['attention']=db.execute(job_reports.report_source()+"SELECT count(*) FROM evidence WHERE source='routine' AND stage IN ('failed','needs-operator') AND run_id NOT IN (SELECT run_id FROM routine_removed)").fetchone()[0]
        counts['completed_today']=db.execute("SELECT count(*) FROM routine_runs WHERE stage='complete' AND id NOT IN (SELECT run_id FROM routine_removed) AND updated>=?",(at.replace(hour=0,minute=0,second=0,microsecond=0).isoformat(),)).fetchone()[0]
        recent=job_reports.list_reports(db,sort='processed',order='desc',page_size=8,include_removed=False)['reports']
        for item in recent:
            if item['run_id']:
                run=routines.projection(db,db.execute('SELECT * FROM routine_runs WHERE id=?',(item['run_id'],)).fetchone())
                item['erase_disks']=run.get('erase_disks',[])
    return {**settings,'choices':choices,'active':active,'active_total':data['total'],'view':view,'counts':counts,'recent':recent,'timeout_seconds':5}
