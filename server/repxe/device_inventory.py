"""Bounded, device-reported specifications without credential or executable fields."""
from typing import Annotated
from pydantic import BaseModel, ConfigDict, Field, model_validator

Text = Annotated[str, Field(max_length=256)]
Number = Annotated[int, Field(ge=0, le=1024**5)]


def serial_key(value):
    value = str(value or '').strip().casefold()
    return '' if value in ('', 'unreported', 'unknown', 'none', 'default string', 'to be filled by o.e.m.', 'not reported', 'system serial number') else value


def asset_key(row):
    serial = serial_key(row.get('system_serial'))
    return 'serial:' + serial if serial else 'legacy-mac:' + row['mac'].lower()

class Facts(BaseModel):
    model_config = ConfigDict(extra='forbid')

class System(Facts):
    manufacturer: Text = ''
    model: Text = ''
    serial: Text = ''
    uuid: Text = ''
    sku: Text = ''
    family: Text = ''
    asset_tag: Text = ''

class Processor(Facts):
    name: Text = ''
    manufacturer: Text = ''
    socket: Text = ''
    cores: Number = 0
    threads: Number = 0
    max_speed_mhz: Number = 0

class MemoryModule(Facts):
    location: Text = ''
    size_bytes: Number = 0
    type: Text = ''
    speed_mts: Number = 0
    manufacturer: Text = ''
    serial: Text = ''
    part_number: Text = ''

class Memory(Facts):
    total_bytes: Number = 0
    modules: list[MemoryModule] = Field(default_factory=list, max_length=64)

class Firmware(Facts):
    type: Text = ''
    manufacturer: Text = ''
    version: Text = ''
    release_date: Text = ''
    secure_boot: bool | None = None
    tpm: Text = ''

class Board(Facts):
    manufacturer: Text = ''
    model: Text = ''
    version: Text = ''
    serial: Text = ''

class Device(Facts):
    name: Text = ''
    manufacturer: Text = ''

class SmartAttribute(Facts):
    id: int = Field(ge=0, le=255)
    name: Text = ''
    value: int | None = Field(default=None,ge=0,le=255)
    worst: int | None = Field(default=None,ge=0,le=255)
    threshold: int | None = Field(default=None,ge=0,le=255)
    raw: Text = ''
    when_failed: Text = ''

class DriveHealth(Facts):
    status: str = Field(default='unavailable',pattern='^(available|partial|unavailable)$')
    source: Text = ''
    collected_utc: Text = ''
    detail: Text = ''
    exit_code: int | None = Field(default=None,ge=0,le=255)
    firmware: Text = ''
    protocol: Text = ''
    passed: bool | None = None
    temperature_c: int | None = Field(default=None,ge=-100,le=200)
    power_on_hours: Number | None = None
    power_cycles: Number | None = None
    percentage_used: Number | None = None
    critical_warning: Number | None = None
    media_errors: Number | None = None
    unsafe_shutdowns: Number | None = None
    error_log_entries: Number | None = None
    reallocated_sectors: Number | None = None
    pending_sectors: Number | None = None
    uncorrectable_sectors: Number | None = None
    bytes_read: int | None = Field(default=None,ge=0,le=1024**8)
    bytes_written: int | None = Field(default=None,ge=0,le=1024**8)
    attributes: list[SmartAttribute] = Field(default_factory=list,max_length=64)

class Storage(Facts):
    identity: Annotated[str, Field(max_length=300)] = ''
    serial: Text = ''
    model: Text = ''
    size_bytes: Number = 0
    bus_type: Text = ''
    removable: bool = False
    health: DriveHealth | None = None

class Battery(Facts):
    name: Text = ''
    manufacturer: Text = ''
    serial: Text = ''
    design_capacity_mwh: Number | None = 0
    full_charge_capacity_mwh: Number | None = 0
    health_percent: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    cycle_count: Number | None = None
    source: Text = ''

    @model_validator(mode='after')
    def capacity_health(self):
        # Capacity health is independent of the current charge level. Keep
        # reported capacities above design (and therefore >100%) intact.
        self.health_percent = (round(self.full_charge_capacity_mwh * 100 / self.design_capacity_mwh, 2)
                               if self.design_capacity_mwh and self.full_charge_capacity_mwh else None)
        return self

class Inventory(Facts):
    system: System = Field(default_factory=System)
    processors: list[Processor] = Field(default_factory=list, max_length=16)
    memory: Memory = Field(default_factory=Memory)
    firmware: Firmware = Field(default_factory=Firmware)
    board: Board = Field(default_factory=Board)
    graphics: list[Device] = Field(default_factory=list, max_length=32)
    network: list[Device] = Field(default_factory=list, max_length=32)
    storage: list[Storage] = Field(default_factory=list, max_length=32)
    batteries: list[Battery] = Field(default_factory=list, max_length=16)
    warnings: list[Text] = Field(default_factory=list, max_length=32)


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS client_inventory(
        client_id TEXT PRIMARY KEY REFERENCES clients(id), inventory TEXT NOT NULL,
        captured_at TEXT NOT NULL)''')


def facts_for(db, client_id=None, report=None, manufacturer='', model='', targets=()):
    row = db.execute('SELECT inventory,captured_at FROM client_inventory WHERE client_id=?', (client_id,)).fetchone() if client_id else None
    facts = Inventory.model_validate_json(row['inventory']).model_dump() if row else Inventory().model_dump()
    # Completion evidence can provide facts for older enrollment clients. Only
    # allowlisted hardware fields are projected; raw native state stays private.
    native = (report or {}).get('native', {})
    hardware = native.get('hardware', {}) if isinstance(native, dict) else {}
    hardware = hardware if isinstance(hardware,dict) else {}
    if isinstance(hardware.get('specs'), dict):
        try: facts = Inventory.model_validate(hardware['specs']).model_dump()
        except ValueError: pass
    facts['system']['manufacturer'] = facts['system']['manufacturer'] or manufacturer or hardware.get('manufacturer', '')
    facts['system']['model'] = facts['system']['model'] or model or hardware.get('model', '')
    if not facts['firmware']['type']: facts['firmware']['type'] = hardware.get('firmware', '')
    if facts['firmware']['secure_boot'] is None and isinstance(hardware.get('secureBootEnabled'), (int, bool)):
        facts['firmware']['secure_boot'] = bool(hardware['secureBootEnabled'])
    if not facts['storage']:
        facts['storage'] = [{'identity': t.get('identity', ''), 'serial': t.get('identity', '').removeprefix('serial:'),
                            'model': t.get('model', ''), 'size_bytes': t.get('size_bytes', 0),
                            'bus_type': '', 'removable': False} for t in targets]
    if client_id:
        import json
        latest=db.execute('SELECT inventory FROM routine_runs WHERE client_id=? AND inventory IS NOT NULL ORDER BY updated DESC LIMIT 1',(client_id,)).fetchone()
        if latest:
            health={d['identity']:d.get('health') for d in json.loads(latest['inventory']).get('disks',[])}
            for disk in facts['storage']:
                if health.get(disk['identity']): disk['health']=DriveHealth.model_validate(health[disk['identity']]).model_dump()
    return {'specs': facts, 'inventory_captured_at': row['captured_at'] if row else None,
            'inventory_available': bool(row or hardware.get('specs'))}


def shredos_facts(inventory):
    facts=Inventory().model_dump()
    facts['system'].update(manufacturer=inventory.get('manufacturer',''),model=inventory.get('model',''),serial=inventory.get('system_serial',''))
    facts['processors']=[Processor(name=inventory.get('cpu','')).model_dump()] if inventory.get('cpu') else []
    facts['memory']['total_bytes']=inventory.get('memory_bytes',0)
    facts['firmware']['version']=inventory.get('firmware','')
    facts['storage']=[Storage(identity=d['identity'],serial=d['identity'].removeprefix('serial:'),model=d.get('model',''),size_bytes=d['size_bytes'],bus_type=d.get('transport',''),removable=d.get('transport')=='usb',health=d.get('health')).model_dump() for d in inventory.get('disks',[])]
    return {'specs':facts,'inventory_available':bool(inventory),'inventory_captured_at':None}
