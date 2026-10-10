"""Shared PDF presentation preferences; recorded evidence remains unchanged."""
import json
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator


def valid_timezone(value):
    try: ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError): raise ValueError('Use a valid IANA time zone, such as Pacific/Auckland or UTC') from None
    return value


class ReportSettings(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    title: str = Field(default='re-pxe / Asset report', min_length=1, max_length=100)
    organization: str = Field(default='', max_length=120)
    footer_text: str = Field(default='re-pxe', max_length=160)
    accent_color: str = Field(default='#276e63', pattern=r'^#[0-9a-fA-F]{6}$')
    paper_size: Literal['A4','Letter'] = 'A4'
    layout: Literal['grouped','continuous'] = 'grouped'
    density: Literal['comfortable','compact'] = 'comfortable'
    font_size: Literal[8,9,10,11] = 9
    timezone: str = Field(default='Pacific/Auckland', min_length=1, max_length=64)
    section_order: list[Literal['specifications','routine','erasure','history']] = Field(default_factory=lambda:['specifications','routine','erasure','history'], min_length=4, max_length=4)
    show_logo: bool = True
    repeat_header: bool = True
    page_numbers: bool = True
    hide_missing: bool = True
    show_job_details: bool = True
    show_asset_tag: bool = True
    show_network_mac: bool = False
    show_system_uuid: bool = False
    show_cpu: bool = True
    show_memory: bool = True
    show_firmware: bool = True
    show_board: bool = True
    show_graphics: bool = True
    show_network: bool = True
    show_storage: bool = True
    show_memory_modules: bool = True
    show_batteries: bool = True
    show_smart: bool = True
    show_inventory_notes: bool = True
    show_routine: bool = True
    show_routine_steps: bool = True
    show_erasure: bool = True
    show_certificate_hashes: bool = True
    show_driver_problems: bool = True
    show_progress: bool = True
    show_timeline: bool = True
    show_operator_actions: bool = True
    export_certificates: bool = True

    @field_validator('timezone')
    @classmethod
    def timezone_exists(cls, value): return valid_timezone(value)

    @field_validator('title')
    @classmethod
    def title_not_blank(cls, value):
        if not value.strip(): raise ValueError('Report title is required')
        return value.strip()

    @field_validator('section_order')
    @classmethod
    def unique_sections(cls, value):
        if len(set(value))!=4: raise ValueError('Include each page group exactly once')
        return value


def initialize(store):
    with store.db() as db:
        db.execute('CREATE TABLE IF NOT EXISTS report_settings(id INTEGER PRIMARY KEY CHECK(id=1),value TEXT NOT NULL)')
        db.execute('INSERT OR IGNORE INTO report_settings VALUES(1,?)',(ReportSettings().model_dump_json(),))


def read(store):
    with store.db() as db: value=json.loads(db.execute('SELECT value FROM report_settings WHERE id=1').fetchone()['value'])
    return ReportSettings.model_validate(value)


def install(app,store,Role,allow):
    @app.get('/api/report-settings')
    def settings(role: Role):
        allow(role)
        return {'settings':read(store()).model_dump(),'defaults':ReportSettings().model_dump()}

    @app.put('/api/report-settings')
    def save(value: ReportSettings,role: Role):
        allow(role)
        with store().db() as db: db.execute('UPDATE report_settings SET value=? WHERE id=1',(value.model_dump_json(),))
        return value.model_dump()
