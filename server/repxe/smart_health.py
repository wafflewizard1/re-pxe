"""Read-only smartctl evidence; importing this module never inspects storage."""
import json
import re
import subprocess
from datetime import datetime, timezone


def number(value, maximum=1024**6):
    return value if type(value) is int and 0 <= value <= maximum else None


def section(data,key):
    value=data.get(key,{})
    return value if isinstance(value,dict) else {}


def parse(data, exit_code):
    if not isinstance(data, dict): raise ValueError('SMART response is not an object')
    result={'status':'available','source':'smartctl -a -j','collected_utc':datetime.now(timezone.utc).isoformat(),
            'exit_code':exit_code,'firmware':str(data.get('firmware_version',''))[:256],
            'protocol':str(section(data,'device').get('protocol',''))[:256], 'attributes':[]}
    status=section(data,'smart_status').get('passed')
    result['passed']=status if type(status) is bool else None
    for field,path in [('temperature_c',('temperature','current')),('power_on_hours',('power_on_time','hours'))]:
        part=section(data,path[0]);value=part.get(path[1])
        result[field]=value if type(value) is int and (field!='temperature_c' and 0<=value<=1024**5 or field=='temperature_c' and -100<=value<=200) else None
    result['power_cycles']=number(data.get('power_cycle_count'))
    nvme=section(data,'nvme_smart_health_information_log')
    for field,source in [('percentage_used','percentage_used'),('critical_warning','critical_warning'),('media_errors','media_errors'),('unsafe_shutdowns','unsafe_shutdowns'),('error_log_entries','num_err_log_entries')]:
        result[field]=number(nvme.get(source))
    for field,source in [('bytes_read','data_units_read'),('bytes_written','data_units_written')]:
        units=number(nvme.get(source));result[field]=units*512000 if units is not None else None
    attrs=section(data,'ata_smart_attributes').get('table',[])
    if not isinstance(attrs,list): raise ValueError('SMART attributes are not a list')
    for a in attrs[:64]:
        if not isinstance(a,dict): continue
        ident=number(a.get('id'),255)
        if ident is None: continue
        raw=a.get('raw',{});raw=raw if isinstance(raw,dict) else {}
        item={'id':ident,'name':str(a.get('name',''))[:256],'value':number(a.get('value'),255),'worst':number(a.get('worst'),255),'threshold':number(a.get('thresh'),255),'raw':str(raw.get('string',raw.get('value','')))[:256],'when_failed':str(a.get('when_failed',''))[:256]}
        result['attributes'].append(item)
        field={5:'reallocated_sectors',197:'pending_sectors',198:'uncorrectable_sectors'}.get(ident)
        if field:result[field]=number(raw.get('value'))
    # Health failures are meaningful evidence, not a smartctl execution failure.
    # Bits 0..2 indicate command/open/SMART query problems; bits 3..7 indicate
    # failing health/attributes or logged errors and must remain visible.
    if exit_code & 7:
        result['status']='partial' if result['passed'] is not None or result['attributes'] or nvme else 'unavailable'
        result['detail']='SMART query/open returned status '+str(exit_code)
    elif result['passed'] is None and not result['attributes'] and not nvme:
        result.update(status='unavailable',detail='Drive did not expose SMART health')
    while len(json.dumps(result))>12000 and result['attributes']:result['attributes'].pop()
    return result


def collect(path):
    if not re.fullmatch(r'/dev/(sd[a-z]+|vd[a-z]+|nvme[0-9]+n[0-9]+)',path):
        raise RuntimeError('SMART collection requires an explicit whole-disk path')
    try:
        proc=subprocess.run(['smartctl','-a','-j',path],capture_output=True,text=True,timeout=20)
        if len(proc.stdout)>256*1024:raise ValueError('SMART response exceeds evidence limit')
        return parse(json.loads(proc.stdout),proc.returncode),proc.stdout
    except (OSError,ValueError,subprocess.SubprocessError) as exc:
        return {'status':'unavailable','source':'smartctl -a -j','detail':'SMART collection failed: '+type(exc).__name__,'attributes':[]},''
