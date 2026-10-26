"""Allowlisted report-table fields, calculated before pagination for numeric sorting."""
from typing import Literal

Sort = Literal['serial','processed','created','model','status','routine','manufacturer','cpu',
               'memory_bytes','storage_bytes','drive_count','storage_models','storage_serials',
               'battery_health','battery_cycles','asset_tag','firmware_version','secure_boot',
               'tpm','graphics','smart','power_on_hours','ssd_wear','image','target','mac']


def array(name, fallback=None):
    alternate = "CASE WHEN json_type(search_pre_inventory,'$."+fallback+"')='array' THEN json_extract(search_pre_inventory,'$."+fallback+"') ELSE '[]' END" if fallback else "'[]'"
    selected="CASE WHEN json_type(search_inventory,'$."+name+"')='array' AND json_array_length(search_inventory,'$."+name+"')>0 THEN json_extract(search_inventory,'$."+name+"') ELSE "+alternate+' END'
    return '(SELECT json_group_array(json(value)) FROM json_each('+selected+") WHERE type='object')"


STORAGE = "(SELECT json_group_array(json_patch(s.value,CASE WHEN json_type(p.value,'$.health')='object' THEN json_object('health',json_extract(p.value,'$.health')) ELSE '{}' END)) FROM json_each("+array('storage','disks')+") s LEFT JOIN json_each(COALESCE(json_extract(search_pre_inventory,'$.disks'),'[]')) p ON json_extract(p.value,'$.identity')=json_extract(s.value,'$.identity'))"
BATTERIES = array('batteries')


def number(path, minimum=0, document='value'):
    field = "json_extract("+document+",'$."+path+"')"
    return "CASE WHEN json_type("+document+",'$."+path+"') IN ('integer','real') AND "+field+'>='+str(minimum)+' THEN '+field+' END'


def text(path, document='value'):
    return "CASE WHEN json_type("+document+",'$."+path+"')='text' THEN NULLIF(json_extract("+document+",'$."+path+"'),'') END"


def disk_counter(path):
    return '(SELECT max('+number('health.'+path)+') FROM json_each('+STORAGE+'))'


FIELDS = {
    'manufacturer': 'COALESCE('+text('system.manufacturer','search_inventory')+',manufacturer)',
    'cpu': "COALESCE((SELECT group_concat("+text('name')+",'; ') FROM json_each("+array('processors')+')),'+text('cpu','search_pre_inventory')+')',
    'memory_bytes': 'COALESCE('+number('memory.total_bytes',1,'search_inventory')+','+number('memory_bytes',1,'search_pre_inventory')+')',
    'storage_bytes': '(SELECT sum('+number('size_bytes',1)+') FROM json_each('+STORAGE+'))',
    'drive_count': '(SELECT NULLIF(count(*),0) FROM json_each('+STORAGE+'))',
    'storage_models': "(SELECT group_concat("+text('model')+",'; ') FROM json_each("+STORAGE+'))',
    'storage_serials': "(SELECT group_concat(COALESCE("+text('serial')+','+text('identity')+"),'; ') FROM json_each("+STORAGE+'))',
    'battery_health': "(SELECT min(round(("+number('full_charge_capacity_mwh',1)+")*100.0/("+number('design_capacity_mwh',1)+"),2)) FROM json_each("+BATTERIES+'))',
    'battery_cycles': '(SELECT max('+number('cycle_count')+') FROM json_each('+BATTERIES+'))',
    'asset_tag': text('system.asset_tag','search_inventory'),
    'firmware_version': 'COALESCE('+text('firmware.version','search_inventory')+','+text('firmware','search_pre_inventory')+')',
    'secure_boot': "CASE WHEN json_type(search_inventory,'$.firmware.secure_boot') IN ('true','false') THEN json_extract(search_inventory,'$.firmware.secure_boot') END",
    'tpm': text('firmware.tpm','search_inventory'),
    'graphics': "(SELECT group_concat("+text('name')+",'; ') FROM json_each("+array('graphics')+'))',
    'smart': "(SELECT CASE WHEN sum(json_type(value,'$.health.passed')='false')>0 THEN 'Failed' WHEN count(*)>0 AND sum(json_type(value,'$.health.passed')='true')=count(*) THEN 'Passed' WHEN sum(json_type(value,'$.health.passed')='true')>0 THEN 'Partial' END FROM json_each("+STORAGE+'))',
    'power_on_hours': disk_counter('power_on_hours'),
    'ssd_wear': disk_counter('percentage_used'),
}

NUMERIC = {'memory_bytes','storage_bytes','drive_count','battery_health','battery_cycles','secure_boot','power_on_hours','ssd_wear'}


def source(prefix):
    return prefix+', report_columns AS (SELECT evidence.*, '+', '.join(sql+' AS column_'+key for key,sql in FIELDS.items())+' FROM evidence) '


def ordering(key):
    if key in FIELDS:
        value='column_'+key
        expression=value if key in NUMERIC else 'lower('+value+')'
    else:
        expression='lower('+{'manufacturer':'manufacturer','image':'image_name','target':'target_identity','mac':'mac'}[key]+')'
        value=expression
    # Missing observations stay last in either direction; zero remains a value.
    return "CASE WHEN "+value+" IS NULL OR CAST("+value+" AS TEXT)='' THEN 1 ELSE 0 END,"+expression
