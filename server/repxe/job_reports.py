"""Authenticated job snapshots, searchable history and printable PDF projections."""
from datetime import date, datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from io import BytesIO
import hashlib
import json
from pathlib import Path
from threading import Lock
from typing import Literal
from xml.sax.saxutils import escape
import zipfile

from fastapi import HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4, letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, KeepTogether, Image, PageBreak
from .activity import routine_stage_sql
from .device_inventory import facts_for, serial_key
from .report_settings import ReportSettings, valid_timezone
from . import report_columns
from .report_columns import Sort

TERMINAL = {'complete', 'failed', 'cancelled'}
MILESTONES = [('created','Job created'),('preflight','Preflight'),('disk-prepared','Disk prepared'),
              ('image-applied','Windows image applied'),('boot-configured','Boot configured'),
              ('drivers-staged','Drivers prepared'),('agent-staged','Completion agent staged'),
              ('awaiting-windows','Windows startup'),('completing','Driver completion'),
              ('ready-for-oobe','Ready for customer setup'),('complete','Completed')]
LABELS = dict(MILESTONES) | {'needs-operator':'Needs attention','failed':'Failed','cancelled':'Cancelled',
    'discovering':'Identifying PC','windows-boot':'Starting Windows install','erase-running':'ShredOS',
    'applying-image':'Installing Windows','downloading':'Downloading Windows','verifying-image':'Verifying image'}
_font_lock = Lock()


def display(stage):
    return LABELS.get(stage, stage.replace('-', ' ').capitalize())


def timestamp_text(value):
    if not value: return 'Not recorded'
    try: return datetime.fromisoformat(value.replace('Z','+00:00')).astimezone(timezone.utc).strftime('%d %b %Y %H:%M UTC')
    except (ValueError, TypeError): return str(value)


def capacity(value):
    if not value: return 'Not reported'
    return f'{value / 1024**3:,.1f} GiB'


JOIN = '''FROM jobs j LEFT JOIN clients c ON c.id=(SELECT x.id FROM clients x
    WHERE x.job_id=j.id ORDER BY x.created DESC,x.rowid DESC LIMIT 1)
    LEFT JOIN client_inventory i ON i.client_id=c.id
    LEFT JOIN artifacts a ON a.id=j.image_id'''
EFFECTIVE_STAGE="CASE WHEN j.stage IN ('complete','failed','cancelled') OR j.report IS NOT NULL THEN j.stage WHEN c.stage IN ('completing','needs-operator','ready-for-oobe','failed') THEN c.stage ELSE j.stage END"
ACTIVITY="CASE WHEN j.stage IN ('complete','failed','cancelled') THEN j.updated ELSE MAX(j.updated,COALESCE(c.seen,j.updated)) END"
SEARCH_TEXT="id||' '||target_identity||' '||device_model||' '||COALESCE(mac,'')||' '||COALESCE(manufacturer,'')||' '||COALESCE(model,'')||' '||COALESCE(system_serial,'')||' '||COALESCE(routine_name,'')||' '||COALESCE(job_id,'')||' '||COALESCE(search_image,'')||' '||COALESCE(error,'')||' '||COALESCE((SELECT group_concat(CAST(value AS TEXT),' ') FROM json_tree(search_inventory) WHERE type IN ('text','integer','real')),'')||' '||COALESCE((SELECT group_concat(CAST(value AS TEXT),' ') FROM json_tree(search_pre_inventory) WHERE type IN ('text','integer','real')),'')"
FIELDS = '''j.id,''' + EFFECTIVE_STAGE + ''' stage,j.stage canonical_stage,j.created,''' + ACTIVITY + ''' updated,j.target_identity,j.device_model,
    j.report IS NOT NULL has_completion_report,c.id client_id,c.mac,
    c.manufacturer,c.model,c.error,a.name image_name,
    COALESCE(NULLIF(json_extract(i.inventory,'$.system.serial'),''),
      json_extract(j.report,'$.native.hardware.specs.system.serial'),'') system_serial'''


def report_source():
    # One report per routine run, with old standalone Windows jobs retained.
    legacy = 'SELECT '+FIELDS+" ,NULL routine_id,NULL routine_name,NULL routine_version,NULL run_id,j.id job_id,'windows' source,NULL certificate_id,COALESCE(json_extract(j.report,'$.native.hardware.specs'),i.inventory,'{}') search_inventory,'{}' search_pre_inventory,COALESCE(a.name,'')||' '||COALESCE(json_extract(c.selection,'$.profile.name'),'')||' '||COALESCE(json_extract(c.selection,'$.profile.windows_label'),'') search_image "+JOIN+" WHERE NOT EXISTS(SELECT 1 FROM routine_runs r WHERE r.job_id=j.id)"
    routine = """SELECT 'routine:'||r.id id,
        """+routine_stage_sql(erase_stale=True)+""" stage,
        r.stage canonical_stage,r.created,
        CASE WHEN r.stage IN ('complete','failed','cancelled') THEN r.updated
          ELSE MAX(r.updated,COALESCE(c.seen,r.updated),COALESCE(j.updated,r.updated),COALESCE(w.updated,r.updated),COALESCE((SELECT updated FROM workflow_progress WHERE run_id=w.id),r.updated)) END updated,
        COALESCE(r.target,'') target_identity,COALESCE(c.model,json_extract(r.inventory,'$.model'),'') device_model,
        (j.report IS NOT NULL OR w.result IS NOT NULL) has_completion_report,c.id client_id,r.mac,
        COALESCE(c.manufacturer,json_extract(r.inventory,'$.manufacturer'),'') manufacturer,
        COALESCE(NULLIF(c.model,''),json_extract(r.inventory,'$.model'),'') model,
        COALESCE(NULLIF(r.error,''),NULLIF(c.error,''),w.error,'') error,a.name image_name,
        COALESCE(NULLIF(json_extract(i.inventory,'$.system.serial'),''),json_extract(r.inventory,'$.system_serial'),json_extract(j.report,'$.native.hardware.specs.system.serial'),'') system_serial,
        r.routine_id,json_extract(r.spec,'$.name') routine_name,r.version routine_version,r.id run_id,j.id job_id,'routine' source,
        CASE WHEN w.certificate_sha256 IS NOT NULL THEN w.id END certificate_id,
        COALESCE(json_extract(j.report,'$.native.hardware.specs'),i.inventory,'{}') search_inventory,COALESCE(r.inventory,'{}') search_pre_inventory,COALESCE(a.name,'')||' '||COALESCE(json_extract(c.selection,'$.profile.name'),'')||' '||COALESCE(json_extract(c.selection,'$.profile.windows_label'),'') search_image
        FROM routine_runs r LEFT JOIN jobs j ON j.id=r.job_id LEFT JOIN clients c ON c.id=r.client_id
        LEFT JOIN client_inventory i ON i.client_id=c.id LEFT JOIN artifacts a ON a.id=j.image_id
        LEFT JOIN workflow_runs w ON w.id=r.workflow_id"""
    return 'WITH evidence AS ('+legacy+' UNION ALL '+routine+') '


def list_reports(db, search='', status='all', start=None, end=None, page=1, page_size=50, sort='serial', order='asc', source=None, seen_after=None, date_field='activity', timezone_name='UTC', include_removed=True, include_columns=False):
    from . import routines
    routines.sync(db)
    filters=[];params=[]
    if not include_removed:filters.append('NOT EXISTS (SELECT 1 FROM routine_removed x WHERE x.run_id=evidence.run_id)')
    if source:filters.append('source=?');params.append(source)
    if search.strip():
        for term in search.split():
            filters.append('instr(lower('+SEARCH_TEXT+'),lower(?))>0')
            params.append(term)
    if status=='pending':filters.append("canonical_stage NOT IN ('complete','failed','cancelled')")
    elif status=='attention':filters.append("stage IN ('failed','needs-operator')")
    elif status!='all':filters.append('stage=?');params.append(status)
    if seen_after:filters.append('updated>=?');params.append(seen_after)
    time_field={'activity':'updated','started':'created','processed':'updated'}[date_field]
    zone=ZoneInfo(timezone_name)
    if date_field=='processed':filters.append("canonical_stage IN ('complete','failed','cancelled')")
    if start:
        filters.append('julianday('+time_field+')>=julianday(?)');params.append(datetime.combine(start,datetime.min.time(),tzinfo=zone).astimezone(timezone.utc).isoformat())
    if end:
        filters.append('julianday('+time_field+')<julianday(?)');params.append(datetime.combine(end+timedelta(days=1),datetime.min.time(),tzinfo=zone).astimezone(timezone.utc).isoformat())
    where=' WHERE '+' AND '.join(filters) if filters else ''
    prefix=report_source()
    total=db.execute(prefix+'SELECT count(*) FROM evidence'+where,params).fetchone()[0]
    pages=max(1,(total+page_size-1)//page_size);page=min(page,pages)
    outcome_order="lower(CASE stage "+' '.join("WHEN '"+stage+"' THEN '"+label.replace("'","''")+"'" for stage,label in LABELS.items())+" ELSE replace(stage,'-',' ') END)"
    ordering={'serial':"CASE WHEN COALESCE(system_serial,'')='' THEN 1 ELSE 0 END,lower(system_serial)",
              'processed':'julianday(updated)','created':'julianday(created)','model':'lower(COALESCE(model,device_model))','status':outcome_order,
              'routine':"lower(COALESCE(NULLIF(routine_name,''),'Earlier Windows job'))"}.get(sort)
    if ordering is None: ordering=report_columns.ordering(sort)
    certificate_available='(certificate_id IS NOT NULL OR EXISTS (SELECT 1 FROM routine_erase_disks d JOIN workflow_runs w ON w.id=d.workflow_id WHERE d.run_id=evidence.run_id AND (w.certificate_sha256 IS NOT NULL OR EXISTS (SELECT 1 FROM workflow_phase_certificates p WHERE p.run_id=w.id))))'
    enriched=include_columns or sort in report_columns.FIELDS
    projection=(report_columns.source(prefix)+'SELECT *, '+certificate_available+' has_shred_report FROM report_columns evidence') if enriched else prefix+'SELECT *, '+certificate_available+' has_shred_report FROM evidence'
    rows=db.execute(projection+where+' ORDER BY '+ordering+' '+order+(',updated DESC' if sort=='serial' else '')+',id '+order+' LIMIT ? OFFSET ?',[*params,page_size,(page-1)*page_size]).fetchall()
    reports=[]
    for r in rows:
        item={k:r[k] for k in r.keys() if k not in ('search_inventory','search_pre_inventory','search_image') and not k.startswith('column_')};item['model']=item['model'] or item['device_model'] or 'Unidentified device'
        if include_columns:item['column_data']={key:r['column_'+key] for key in report_columns.FIELDS}
        item['processed_at']=item['updated'] if item['canonical_stage'] in TERMINAL else None
        item['has_completion_report']=bool(item['has_completion_report']);item['has_shred_report']=bool(item['has_shred_report'])
        item['status_label']=display(item['stage']);reports.append(item)
    return {'reports':reports,'total':total,'page':page,'pages':pages,'page_size':page_size}


def phase_certificates(db, workflow_id, target):
    return [{'target':target,'phase':d['phase'],'sha256':d['sha256'],
             'url':'/api/workflow-runs/'+workflow_id+'/certificate/'+d['phase']}
            for d in db.execute('SELECT phase,sha256 FROM workflow_phase_certificates WHERE run_id=? ORDER BY phase',(workflow_id,))]


def asset_attachments(db, client, sessions):
    """Attach immutable routine evidence by PC serial, never by a shared NIC."""
    serial = serial_key(client.get('system_serial'))
    session_ids = [row['id'] for row in sessions]
    attached = []
    rows = db.execute(report_source() + 'SELECT * FROM evidence ORDER BY created DESC,id')
    for row in rows:
        if not (serial and serial_key(row['system_serial']) == serial or row['client_id'] in session_ids): continue
        workflow = db.execute('SELECT certificate_sha256 FROM workflow_runs WHERE id=?', (row['certificate_id'],)).fetchone() if row['certificate_id'] else None
        certificates=[{'target':d['target'],'sha256':d['certificate_sha256'],'url':'/api/workflow-runs/'+d['id']+'/certificate'} for d in db.execute('SELECT w.id,w.target,w.certificate_sha256 FROM routine_erase_disks d JOIN workflow_runs w ON w.id=d.workflow_id WHERE d.run_id=? AND w.certificate_sha256 IS NOT NULL ORDER BY d.ordinal',(row['run_id'],))] if row['run_id'] else []
        for certificate in list(certificates):
            workflow_id=certificate['url'].split('/')[-2]
            certificates.extend(phase_certificates(db,workflow_id,certificate['target']))
        attached.append({'certificates':certificates,'report_id': row['id'], 'report_url': '/api/job-reports/' + row['id'],
            'pdf_url': '/api/job-reports/' + row['id'] + '/pdf', 'system_serial': row['system_serial'],
            'name': row['routine_name'] or 'Windows job', 'stage': row['stage'], 'created': row['created'],
            'certificate_url': '/api/workflow-runs/' + row['certificate_id'] + '/certificate' if workflow else None,
            'certificate_sha256': workflow['certificate_sha256'] if workflow else None})
    return attached


def routine_snapshot(db, uid):
    from . import routines
    from .device_inventory import Inventory
    routines.sync(db)
    row=db.execute('SELECT * FROM routine_runs WHERE id=?',(uid,)).fetchone()
    if not row:raise HTTPException(404,'Routine report not found')
    run=routines.projection(db,row,True)
    if row['job_id']:
        data=snapshot(db,row['job_id'])
        data['windows_job_id']=row['job_id'];data['job']['id']='routine:'+uid
        inv=run.get('inventory') or {}
        for key,source in [('manufacturer','manufacturer'),('model','model'),('serial','system_serial')]:
            if not data['specs']['system'][key]:data['specs']['system'][key]=inv.get(source,'')
    else:
        inv=run.get('inventory') or {};facts=Inventory().model_dump()
        facts['system'].update(manufacturer=inv.get('manufacturer',''),model=inv.get('model',''),serial=inv.get('system_serial',''))
        facts['processors']=[{'name':inv.get('cpu',''),'manufacturer':'','socket':'','cores':0,'threads':0,'max_speed_mhz':0}] if inv.get('cpu') else []
        facts['memory']['total_bytes']=inv.get('memory_bytes',0);facts['firmware']['version']=inv.get('firmware','')
        facts['storage']=[{'identity':d['identity'],'serial':d['identity'].removeprefix('serial:'),'model':d.get('model',''),'size_bytes':d['size_bytes'],'bus_type':d.get('transport',''),'removable':d.get('transport')=='usb','health':d.get('health')} for d in inv.get('disks',[])]
        data={'schema_version':1,'job':{'id':'routine:'+uid,'created':row['created'],'updated':row['updated'],'device_model':facts['system']['model'],'target_identity':row['target'] or '', 'image_name':''},
              'specs':facts,'inventory_available':bool(inv),'inventory_captured_at':row['created'] if inv else None,
              'client_id':row['client_id'],'mac':row['mac'],'has_completion_report':False,'error':row['error'],
              'install_percent':None,'transfer':{'received_bytes':0,'total_bytes':0},'events':[],'total_events':0,'controls':[],'driver_problems':[],'milestones':[]}
    health_by_disk={d['identity']:d.get('health') for d in inv.get('disks',[])}
    for disk in data['specs']['storage']:
        if health_by_disk.get(disk['identity']): disk['health']=health_by_disk[disk['identity']]
    data['routine']=run;data['report_kind']='routine';data['job']['stage']=row['stage']
    stage=run['effective_stage']
    data.update(reported_stage=stage,status_label=display(stage),processed_at=row['updated'] if row['stage'] in TERMINAL else None,
                as_of=row['updated'] if row['stage'] in TERMINAL else max(run['last_activity'],data.get('as_of',row['updated'])))
    windows_furthest=data.get('furthest_label','Windows install')
    steps=run['steps'];index=row['step'];step=steps[index]
    data['furthest_stage']=step['kind'];data['furthest_label']=routines.STEP_LABELS[step['kind']]
    if row['stage']=='windows-running' and row['job_id']:data['furthest_label']+=' / '+windows_furthest
    workflow=db.execute('SELECT * FROM workflow_runs WHERE id=?',(row['workflow_id'],)).fetchone() if row['workflow_id'] else None
    data['erasures']=[{'id':d['id'],'target':d['target'],'size':d['size'],'stage':d['stage'],'method':json.loads(d['spec'])['method'],'result':json.loads(d['result']) if d['result'] else None,'certificate_sha256':d['certificate_sha256'],'certificate_url':'/api/workflow-runs/'+d['id']+'/certificate' if d['certificate_sha256'] else None} for d in db.execute('SELECT w.* FROM routine_erase_disks d JOIN workflow_runs w ON w.id=d.workflow_id WHERE d.run_id=? ORDER BY d.ordinal',(uid,))]
    for erasure in data['erasures']:
        erasure['phase_certificates']=phase_certificates(db,erasure['id'],erasure['target'])
    data['erasure']=None
    if workflow:
        result=json.loads(workflow['result']) if workflow['result'] else None
        policy=json.loads(workflow['spec'])
        data['erasure']={'stage':workflow['stage'],'requested_method':policy.get('requested_method') or policy['method'],'actual_method':policy.get('execution_method') or policy['method'],'verification':policy['verification'], 'result':result['result'] if result else None,
            'rounds':policy['rounds'],'final_blank':policy['final_blank'],'prng':policy.get('prng','auto'),'io_mode':policy.get('io_mode','auto'),'prng_followup':policy.get('prng_followup',True),
            'readback':policy.get('readback','none'),'settings_sha256':result.get('settings_sha256') if result else None,
            'evidence':result.get('evidence',{}) if result else {},'exit_code':result.get('exit_code') if result else None,
            'errors':result.get('errors') if result else None,'started':result.get('started') if result else None,'finished':result.get('finished') if result else None,
            'certificate_sha256':workflow['certificate_sha256'],'phase_certificates':phase_certificates(db,workflow['id'],workflow['target']),'certificate_url':run.get('certificate_url'),'target':workflow['target'],'size':workflow['size']}
        erase_events=[dict(e) for e in db.execute('SELECT stage,detail,created timestamp FROM workflow_events WHERE run_id=? ORDER BY seq',(workflow['id'],))]
        data['events']=sorted([*erase_events,*data['events']],key=lambda e:e['timestamp']);data['total_events']=len(data['events'])
        data['has_completion_report']=data['has_completion_report'] or bool(result)
    data['events']=sorted([*({'stage':e['stage'],'detail':e['detail'],'timestamp':e['created']} for e in run['events']),*data['events']],key=lambda e:e['timestamp'])
    data['total_events']=len(data['events'])
    return data


def snapshot(db, uid):
    if uid.startswith('routine:'):return routine_snapshot(db,uid.removeprefix('routine:'))
    job=db.execute('SELECT j.*,a.name image_name FROM jobs j LEFT JOIN artifacts a ON a.id=j.image_id WHERE j.id=?',(uid,)).fetchone()
    if not job:raise HTTPException(404,'Job report not found')
    job=dict(job);record=job.pop('report');raw=json.loads(record) if record else {}
    client=db.execute('SELECT * FROM clients WHERE job_id=? ORDER BY created DESC,rowid DESC LIMIT 1',(uid,)).fetchone()
    c=dict(client) if client else {}
    facts=facts_for(db,c.get('id'),raw,c.get('manufacturer',''),c.get('model',job['device_model']),json.loads(c.get('targets','[]')))
    events=[dict(r) for r in db.execute('SELECT stage,detail,timestamp FROM events WHERE job_id=? ORDER BY seq LIMIT 1000',(uid,))]
    total_events=db.execute('SELECT count(*) FROM events WHERE job_id=?',(uid,)).fetchone()[0]
    controls=[dict(r) for r in db.execute('SELECT action,reason,timestamp FROM client_control_events WHERE job_id=? ORDER BY seq',(uid,))]
    install=db.execute('SELECT percent,captured_at FROM client_install_progress WHERE client_id=? AND job_id=?',(c.get('id'),uid)).fetchone()
    observed={e['stage'] for e in events}
    reported_stage=job['stage']
    if job['stage'] not in TERMINAL and not raw:
        observed.add(job['stage']);observed.add(c.get('stage',''))
        if c.get('stage') in ('completing','needs-operator','ready-for-oobe','failed'):reported_stage=c['stage']
    furthest=next((key for key,_ in reversed(MILESTONES) if key in observed), 'created')
    native=raw.get('native') or {};native=native if isinstance(native,dict) else {}
    driver_data=native.get('drivers') or {};driver_data=driver_data if isinstance(driver_data,dict) else {}
    problems=driver_data.get('problems',raw.get('driver_problems',[])) or []
    problems=[{'description':str(p.get('description',''))[:256],'instance_id':str(p.get('instanceId',''))[:512],
               'problem_code':p.get('problemCode')} for p in problems[:100] if isinstance(p,dict)]
    from . import routines
    routines.sync(db)
    routine_row=db.execute('SELECT * FROM routine_runs WHERE job_id=?',(uid,)).fetchone()
    routine=routines.projection(db,routine_row,True) if routine_row else None
    return {'routine':routine,'schema_version':1,'job':job,'client_id':c.get('id'),'mac':c.get('mac',''),**facts,
            'reported_stage':reported_stage,'status_label':display(reported_stage),'processed_at':job['updated'] if job['stage'] in TERMINAL else None,
            'as_of':job['updated'] if job['stage'] in TERMINAL else max(job['updated'],c.get('seen',job['updated'])),'furthest_stage':furthest,'furthest_label':display(furthest),
            'error':c.get('error') or native.get('error') or (raw.get('summary') if job['stage']!='complete' else '') or '',
            'install_percent':install['percent'] if install else None,'install_progress_updated':install['captured_at'] if install else None,
            'has_completion_report':bool(raw),'events':events,'total_events':total_events,'controls':controls,
            'driver_problems':problems,'technician_finish':native.get('technicianFinish'),
            'transfer':{'received_bytes':c.get('received',0),'total_bytes':c.get('total',0)},
            'milestones':[{'stage':key,'label':label,'recorded':key in observed} for key,label in MILESTONES]}


def fonts():
    with _font_lock:
        if 'JobReport' in pdfmetrics.getRegisteredFontNames():return 'JobReport','JobReportBold'
        folder=Path('/usr/share/fonts/truetype/dejavu')
        if not folder.exists():folder=Path('/usr/share/fonts/TTF')
        if (folder/'DejaVuSans.ttf').exists() and (folder/'DejaVuSans-Bold.ttf').exists():
            pdfmetrics.registerFont(TTFont('JobReport',str(folder/'DejaVuSans.ttf')))
            pdfmetrics.registerFont(TTFont('JobReportBold',str(folder/'DejaVuSans-Bold.ttf')))
            pdfmetrics.registerFontFamily('JobReport',normal='JobReport',bold='JobReportBold')
            return 'JobReport','JobReportBold'
        return 'Helvetica','Helvetica-Bold'


def render_pdf(data, logo=None, settings=None):
    settings=settings or ReportSettings()
    if isinstance(settings,dict):settings=ReportSettings.model_validate(settings)
    o=settings;stream=BytesIO();regular,bold=fonts()
    paper=A4 if o.paper_size=='A4' else letter;width,height=paper;content=width-100
    ink=colors.HexColor('#203c47');accent=colors.HexColor(o.accent_color);muted=colors.HexColor('#61747b')
    size=o.font_size;pad=3 if o.density=='compact' else 6
    body=ParagraphStyle('Body',fontName=regular,fontSize=size,leading=size+4,textColor=ink,wordWrap='CJK')
    heading=ParagraphStyle('Heading',parent=body,fontName=bold,fontSize=size+3,leading=size+8,spaceBefore=12,spaceAfter=7,keepWithNext=True,textColor=accent)
    small=ParagraphStyle('Small',parent=body,fontSize=max(7,size-1),leading=size+2,textColor=muted)
    title=ParagraphStyle('Title',parent=body,fontName=bold,fontSize=21,leading=28)
    def p(value,style=body):return Paragraph(escape(str(value if value not in ('',None) else 'Not reported')).replace('\n','<br/>'),style)
    def section(text):return p(text,heading)
    def time(value):
        if not value:return 'Not recorded'
        try:return datetime.fromisoformat(value.replace('Z','+00:00')).astimezone(ZoneInfo(o.timezone)).strftime('%d %b %Y %H:%M %Z')
        except (ValueError,TypeError):return str(value)
    def grid(rows):
        rows=[(label,value) for label,value in rows if not o.hide_missing or value not in ('',None,'Not reported','Not recorded')]
        if not rows:return Spacer(1,1)
        t=Table([[p(label,small),p(value)] for label,value in rows],colWidths=[content*.29,content*.71],hAlign='LEFT',splitInRow=1)
        t.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),8),('RIGHTPADDING',(0,0),(-1,-1),8),('TOPPADDING',(0,0),(-1,-1),pad),('BOTTOMPADDING',(0,0),(-1,-1),pad),('ROWBACKGROUNDS',(0,0),(-1,-1),[colors.HexColor('#f3f7f6'),colors.white])]))
        return t
    def digest(value):return value[:32]+'\n'+value[32:] if value else None
    job=data['job'];spec=data['specs'];system=spec['system'];model=system['model'] or job['device_model'] or 'Unidentified device'
    asset=system['serial'] or 'Serial unavailable'
    story=[]
    if logo and o.show_logo:
        from PIL import Image as RasterImage
        with RasterImage.open(BytesIO(logo)) as image:iw,ih=image.size
        scale=min(110/iw,48/ih);mark=Image(BytesIO(logo),width=iw*scale,height=ih*scale);mark.hAlign='LEFT';story.extend([mark,Spacer(1,8)])
    story.extend([p(o.title,small),p(asset,title),p(' / '.join(filter(None,[system['manufacturer'],model])),small)])
    if o.organization:story.append(p(o.organization,small))
    status=Table([[p(data['status_label'],ParagraphStyle('Status',parent=body,fontName=bold,textColor=accent,fontSize=13)),p('Job '+job['id'] if o.show_job_details else '',small)]],colWidths=[content*.4,content*.6]);status.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,-1),colors.HexColor('#edf3f1')),('BOX',(0,0),(-1,-1),.6,accent),('TOPPADDING',(0,0),(-1,-1),9),('BOTTOMPADDING',(0,0),(-1,-1),9),('VALIGN',(0,0),(-1,-1),'MIDDLE')]))
    story.extend([Spacer(1,10),status,Spacer(1,8)])
    identifiers=[]
    if o.show_asset_tag:identifiers.append(('Asset tag',system['asset_tag']))
    if o.show_network_mac:identifiers.append(('Network MAC',data['mac']))
    if o.show_job_details:
        identifiers.extend([('Target disk',job['target_identity']),('Windows image',job.get('image_name')),('Job started',time(job['created'])),('Asset processed',time(data['processed_at']) if data['processed_at'] else 'Still pending - not recorded as processed'),('Last activity',time(data['as_of']))])
    if identifiers:story.append(grid(identifiers))
    groups={key:[] for key in o.section_order}
    specs=groups['specifications'];rows=[]
    cpu='; '.join(' / '.join(filter(None,[c['name'],f"{c['cores']} cores" if c['cores'] else '',f"{c['threads']} threads" if c['threads'] else '',f"{c['max_speed_mhz']} MHz maximum" if c['max_speed_mhz'] else ''])) for c in spec['processors'])
    bios=spec['firmware'];board=spec['board']
    if o.show_cpu:rows.append(('Processor',cpu))
    if o.show_memory:rows.append(('Installed memory',capacity(spec['memory']['total_bytes'])))
    if o.show_firmware:rows.extend([('Firmware',' / '.join(filter(None,[bios['type'],bios['manufacturer'],bios['version'],bios['release_date']]))),('Secure Boot','Not reported' if bios['secure_boot'] is None else 'Enabled' if bios['secure_boot'] else 'Disabled'),('TPM',bios['tpm'])])
    if o.show_board:rows.extend([('Motherboard',' / '.join(filter(None,[board['manufacturer'],board['model'],board['version']]))),('Board serial',board['serial']),('SKU / family',' / '.join(filter(None,[system['sku'],system['family']])))])
    if o.show_system_uuid:rows.append(('System UUID',system['uuid']))
    if o.show_graphics:rows.append(('Graphics','; '.join(d['name'] for d in spec['graphics'])))
    if o.show_network:rows.append(('Network adapters','; '.join(d['name'] for d in spec['network'])))
    if rows:specs.extend([section('Device specifications'),grid(rows)])
    if specs and not data['inventory_available']:specs.append(p('Detailed component inventory was not supplied for this stage; missing values are shown as not reported.',small))
    for label,key,enabled in [('Storage devices','storage',o.show_storage),('Memory modules','memory',o.show_memory_modules),('Batteries','batteries',o.show_batteries)]:
        entries=spec['memory']['modules'] if key=='memory' else spec[key]
        if not enabled or not entries:continue
        specs.append(section(label))
        for item in entries:
            if key=='storage':rows=[('Model / capacity',item['model']+' / '+capacity(item['size_bytes'])),('Serial / interface',item['serial']+' / '+(item['bus_type'] or 'Not reported')),('Identity',item['identity'])]
            elif key=='memory':rows=[('Slot / capacity',item['location']+' / '+capacity(item['size_bytes'])),('Type / speed',item['type']+' / '+str(item['speed_mts'])+' MT/s'),('Manufacturer / part',' / '.join(filter(None,[item['manufacturer'],item['part_number']]))),('Module serial',item['serial'])]
            else:rows=[('Battery',item['name']),('Manufacturer / serial',' / '.join(filter(None,[item['manufacturer'],item['serial']]))),('Design capacity',str(item['design_capacity_mwh'])+' mWh' if item['design_capacity_mwh'] else None),('Full charge capacity',str(item['full_charge_capacity_mwh'])+' mWh' if item['full_charge_capacity_mwh'] else None),('Battery health',str(item['health_percent'])+'%' if item['health_percent'] is not None else None),('Cycle count',item['cycle_count'])]
            if key=='storage' and o.show_smart and item.get('health'):
                h=item['health'];rows.append(('SMART health','Passed' if h.get('passed') is True else 'FAILED' if h.get('passed') is False else h.get('detail') or 'Not available'))
                for label,field,suffix in [('Temperature','temperature_c',' C'),('Power-on hours','power_on_hours',''),('Power cycles','power_cycles',''),('NVMe endurance used','percentage_used','%'),('Reallocated sectors','reallocated_sectors',''),('Pending sectors','pending_sectors',''),('Uncorrectable sectors','uncorrectable_sectors',''),('Media errors','media_errors',''),('Critical warning','critical_warning',''),('Unsafe shutdowns','unsafe_shutdowns','')]:
                    if h.get(field) is not None:rows.append((label,str(h[field])+suffix))
                if h.get('collected_utc'):rows.append(('SMART collected',time(h['collected_utc'])))
            if key=='batteries' and item['source']:rows.append(('Source',item['source']))
            specs.extend([grid(rows),Spacer(1,6)])
    if o.show_inventory_notes and spec['warnings']:specs.extend([section('Inventory notes'),*[p(w,small) for w in spec['warnings']]])
    process=groups['routine'];routine=data.get('routine')
    if o.show_routine:
        if routine:
            process.extend([section('Routine'),grid([('Name / version',routine['name']+' / '+str(routine['version'])),('Run / status',routine['id']+' / '+routine['stage'])])])
            if o.show_routine_steps:
                process.append(section('Routine steps'))
                for index,step in enumerate(routine['steps'],1):
                    details=[step.get('label') or step['id'],display(step['kind'])]
                    if step['kind']=='erase' and step.get('method'):details.append(step['method'])
                    if step['kind']=='windows':details.append('Image SHA-256 verification: '+('enabled' if step.get('verify_image',True) else 'skipped by routine'))
                    if step['kind']=='boot':details.append('Automatic start: '+(str(step.get('start_delay_seconds',5))+' seconds' if step.get('start_delay_seconds',5) else 'manual'))
                    if step['kind'] in ('erase','windows'):
                        if step.get('target_policy'):details.append(step['target_policy'].replace('-',' '))
                        if step.get('target_identity'):details.append(step['target_identity'])
                    process.append(grid([('Step '+str(index),' / '.join(details))]))
                    options=step.get('finish_options') if step['kind']=='windows' else None
                    if options:
                        process.extend([section('Customer handoff'),grid([('Customer setup','Local account: '+options['account_name'] if options['setup_mode']=='local-account' else 'Customer completes Windows OOBE'),('Desktop PDF copy','Requested' if options['save_report_desktop'] else 'Disabled'),('Client cleanup','After accepted final report' if options['cleanup_client'] else 'Keep client files')])])
        received='An erasure result was received.' if (data.get('erasure') or {}).get('result') and not data.get('windows_job_id') else 'A completion report was received.'
        process.extend([section('Process outcome'),p('Furthest recorded milestone: '+data['furthest_label']),p(received if data['has_completion_report'] else 'No completion report has been received. This PDF records the available job evidence.',small)])
        if data['error']:process.extend([Spacer(1,6),p(data['error'])])
        if o.show_progress:
            if data['install_percent'] is not None:process.append(p('Last reported Windows image application: '+str(data['install_percent'])+'%',small))
            transfer=data['transfer']
            if transfer['total_bytes']:process.append(p('Image transfer: '+capacity(transfer['received_bytes'])+' of '+capacity(transfer['total_bytes']),small))
        if data.get('technician_finish'):process.extend([section('Technician finish'),p('The technician chose to seal Windows. Remaining device problems: '+str(len(data['driver_problems']))+'.')])
    if o.show_driver_problems and data['driver_problems']:
        process.extend([section('Remaining device problems'),*[p(item['description']+' / '+item['instance_id']+' / problem '+str(item['problem_code'])) for item in data['driver_problems']]])
    erase=groups['erasure']
    if o.show_erasure and data.get('erasure'):
        e=data['erasure'];from . import erase_policy
        label=lambda method:erase_policy.METHODS.get(method,method)
        rows=[('Requested method',label(e['requested_method'])),('Actual method',label(e['actual_method'])),('Verification', ('Controller completion + full PRNG readback' if e.get('prng_followup',True) else 'Controller completion') if e['requested_method']=='nist_purge' else 'Full zero readback' if e['requested_method']=='nist_clear' else 'Controller completion' if e['actual_method'] in erase_policy.NATIVE else {'all':'Every pass','last':'Last pass','off':'Disabled'}.get(e['verification'],e['verification'])),('Rounds / passes',e['rounds']),('Random generator / I/O',e['prng']+' / '+e['io_mode']),('Result',e['result'] or e['stage']),('Exit code / errors',str(e['exit_code'])+' / '+str(e['errors'])),('Started / finished',time(e['started'])+' / '+time(e['finished'])),('Target / capacity',e['target']+' / '+capacity(e['size']))]
        if e['actual_method'] in erase_policy.NATIVE:
            if e['requested_method']=='nist_purge' and e.get('prng_followup',True):
                rows=[(key,'ChaCha20 / auto' if key=='Random generator / I/O' else value) for key,value in rows]
            else:rows=[row for row in rows if row[0] not in ('Rounds / passes','Random generator / I/O')]
        if e['requested_method']=='nist_purge':rows.insert(3,('Verified PRNG follow-up','Enabled' if e.get('prng_followup',True) else 'Disabled by routine'))
        if o.show_certificate_hashes:
            rows.append(('Certificate SHA-256',digest(e['certificate_sha256'])))
            for phase in e.get('phase_certificates',[]):rows.append((phase['phase'].capitalize()+' PDF SHA-256',digest(phase['sha256'])))
        erase.extend([section('ShredOS evidence'),grid(rows)])
        if e['evidence']:
            detail=e['evidence'];rows=[('Erase engine',detail.get('engine','Not recorded'))]
            if detail.get('achieved_classification'):rows.append(('Achieved sanitization',{'clear':'Clear','purge':'Purge','unclassified':'Not classified'}.get(detail['achieved_classification'],'Not classified')))
            if detail.get('native_method'):rows.append(('Native nwipe method',label(detail['native_method'])))
            for phase in detail.get('phases',[]):rows.append((phase.get('phase','Phase').capitalize()+' phase',label(phase.get('method') or (detail.get('native_method','') if phase.get('phase')=='native' else ''))+' / '+phase.get('result','unknown')))
            if detail.get('device_errors'):rows.extend((name.replace('_',' ').capitalize(),str(value)) for name,value in detail['device_errors'].items())
            if detail.get('error'):rows.append(('Attention',detail['error']))
            if detail.get('scope'):rows.append(('Erase scope',detail['scope']))
            erase.append(grid(rows))
    if o.show_erasure and len(data.get('erasures',[]))>1:
        erase.append(section('Erasure results - all drives'))
        for disk in data['erasures']:
            result=disk['result'] or {};rows=[('Drive / capacity',disk['target']+' / '+capacity(disk['size'])),('Method / result',disk['method']+' / '+result.get('result',disk['stage'])),('Exit / errors',str(result.get('exit_code','unknown'))+' / '+str(result.get('errors','unknown')))]
            achieved=result.get('evidence',{}).get('achieved_classification')
            if achieved:rows.append(('Achieved sanitization',{'clear':'Clear','purge':'Purge','unclassified':'Not classified'}.get(achieved,'Not classified')))
            if o.show_certificate_hashes:
                rows.append(('Certificate SHA-256',digest(disk['certificate_sha256'])))
                for phase in disk.get('phase_certificates',[]):rows.append((phase['phase'].capitalize()+' PDF SHA-256',digest(phase['sha256'])))
            erase.append(grid(rows))
    history=groups['history']
    if o.show_timeline and data['events']:
        history.append(section('Recorded process timeline'))
        for event in data['events']:history.extend([p(display(event['stage'])+' - '+time(event['timestamp']),ParagraphStyle('Event',parent=body,fontName=bold,spaceBefore=7)),p(event['detail'],small) if event['detail'] else Spacer(1,1)])
        if data['total_events']>len(data['events']):history.append(p('First 1,000 events shown; the full event history remains available through the job API.',small))
    if o.show_operator_actions and data['controls']:
        history.append(section('Operator actions'))
        for event in data['controls']:history.extend([p(event['action'].capitalize()+' - '+time(event['timestamp'])),p(event['reason'],small)])
    populated=0
    for key in o.section_order:
        if not groups[key]:continue
        if populated and o.layout=='grouped':story.append(PageBreak())
        story.extend(groups[key]);populated+=1
    story.extend([Spacer(1,16),p('Specifications and outcomes are device-reported. Missing facts are not inferred. Pending or failed jobs do not certify a completed installation or erasure.',small)])
    def fit(text,max_width):
        while pdfmetrics.stringWidth(text,regular,7)>max_width:text=text[:-4]+'...' if len(text)>4 else ''
        return text
    def footer(canvas,doc):
        canvas.saveState();canvas.setStrokeColor(accent);canvas.line(50,43,width-50,43);canvas.setFont(regular,7);canvas.setFillColor(muted)
        canvas.drawString(50,31,fit((o.footer_text+' | ' if o.footer_text else '')+'Evidence as of '+time(data['as_of']),content-70))
        if o.page_numbers:canvas.drawRightString(width-50,31,f'Page {doc.page}')
        if doc.page>1 and o.repeat_header:canvas.drawString(50,height-30,fit(o.title+' | '+asset,content))
        canvas.restoreState()
    doc=SimpleDocTemplate(stream,pagesize=paper,rightMargin=50,leftMargin=50,topMargin=45,bottomMargin=60,title=o.title+' - '+asset,author=o.organization or 're-pxe')
    doc.build(story,onFirstPage=footer,onLaterPages=footer)
    return stream.getvalue()


class ReportFilters(BaseModel):
    model_config=ConfigDict(extra='forbid')
    search:str=Field(default='',max_length=200)
    status:Literal['all','complete','failed','cancelled','needs-operator','pending']='all'
    start:date|None=None
    end:date|None=None
    sort:Sort='processed'
    order:Literal['asc','desc']='desc'
    date_field:Literal['activity','started','processed']='activity'
    timezone:str=Field(default='UTC',max_length=64)

    @field_validator('timezone')
    @classmethod
    def timezone_exists(cls,value):return valid_timezone(value)

    @model_validator(mode='after')
    def dates_ordered(self):
        if self.start and self.end and self.start>self.end:raise ValueError('Start date must be before end date')
        if self.end==date.max:raise ValueError('Through date must be before 31 December 9999')
        return self


class Export(BaseModel):
    model_config=ConfigDict(extra='forbid')
    ids:list[str]|None=Field(default=None,min_length=1,max_length=200)
    filters:ReportFilters|None=None
    include_certificates:bool|None=None

    @model_validator(mode='after')
    def selection(self):
        if (self.ids is None)==(self.filters is None):raise ValueError('Choose report IDs or matching filters')
        if self.ids and len(set(self.ids))!=len(self.ids):raise ValueError('Duplicate reports selected')
        return self


def install(app,store,Role,allow):
    from . import branding,report_settings
    def logo():
        value=branding.read(store())
        return value['png'] if value else None
    @app.get('/api/job-reports')
    def reports(role:Role,search:str=Query(default='',max_length=200),status:Literal['all','complete','failed','cancelled','needs-operator','pending']='all',
                start:date|None=None,end:date|None=None,page:int=Query(default=1,ge=1),page_size:int=Query(default=50,ge=1,le=200),
                sort:Sort='processed',order:Literal['asc','desc']='desc',
                date_field:Literal['activity','started','processed']='activity',timezone:str=Query(default='UTC',max_length=64)):
        allow(role)
        if start and end and start>end:raise HTTPException(422,'Start date must be before end date')
        if end==date.max:raise HTTPException(422,'Through date must be before 31 December 9999')
        try:valid_timezone(timezone)
        except ValueError as exc:raise HTTPException(422,str(exc)) from None
        with store().db() as db:return list_reports(db,search,status,start,end,page,page_size,sort,order,date_field=date_field,timezone_name=timezone,include_columns=True)

    @app.post('/api/job-reports/export')
    def export(spec:Export,role:Role):
        allow(role)
        preferences=report_settings.read(store());mark=logo()
        include=preferences.export_certificates if spec.include_certificates is None else spec.include_certificates
        with store().db() as db:
            ids=spec.ids
            if ids is None:
                values=spec.filters.model_dump();values['timezone_name']=values.pop('timezone')
                matches=list_reports(db,**values,page=1,page_size=201)
                if matches['total']>200:raise HTTPException(413,'Export up to 200 reports at once; narrow the search or date range')
                ids=[row['id'] for row in matches['reports']]
                if not ids:raise HTTPException(422,'No matching reports to export')
            data=[snapshot(db,uid) for uid in ids]
            certificates={};certificate_bytes=0
            for item in data:
                rows=item.get('erasures') or ([{'id':item['routine'].get('workflow_id')}] if item.get('routine') else [])
                members=[]
                if include:
                    for drive in rows:
                        row=db.execute('SELECT certificate FROM workflow_runs WHERE id=?',(drive['id'],)).fetchone()
                        if row and row['certificate']:
                            certificate_bytes+=len(row['certificate'])
                            if certificate_bytes>100*1024**2:raise HTTPException(413,'Erasure PDFs exceed 100 MiB; select fewer reports')
                            members.append((drive['id'],bytes(row['certificate'])))
                        for phase in db.execute('SELECT phase,payload FROM workflow_phase_certificates WHERE run_id=? ORDER BY phase',(drive['id'],)):
                            certificate_bytes+=len(phase['payload'])
                            if certificate_bytes>100*1024**2:raise HTTPException(413,'Erasure PDFs exceed 100 MiB; select fewer reports')
                            members.append((drive['id']+'-'+phase['phase'],bytes(phase['payload'])))
                certificates[item['job']['id']]=members
        output=BytesIO()
        with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as z:
            for item in data:
                uid=item['job']['id'];safe=''.join(c for c in uid if c.isalnum() or c=='-')[:100]
                z.writestr('job-'+safe+'.pdf',render_pdf(item,logo=mark,settings=preferences))
                for drive_id,pdf in certificates.get(uid,[]):
                    z.writestr('shredos-'+safe+('-'+drive_id if len(certificates[uid])>1 else '')+'.pdf',pdf)
                if output.tell()>100*1024**2:raise HTTPException(413,'Export is over 100 MiB; select fewer reports')
        return Response(output.getvalue(),media_type='application/zip',headers={'Content-Disposition':'attachment; filename="re-pxe-job-reports.zip"','Cache-Control':'private, no-store'})

    @app.post('/api/job-reports/{uid}/preview')
    def preview(uid:str,preferences:ReportSettings,role:Role):
        allow(role)
        with store().db() as db:data=snapshot(db,uid)
        return Response(render_pdf(data,logo=logo(),settings=preferences),media_type='application/pdf',headers={'Cache-Control':'private, no-store'})

    @app.get('/api/job-reports/{uid}')
    def detail(uid:str,role:Role):
        allow(role)
        with store().db() as db:return snapshot(db,uid)

    @app.get('/api/job-reports/{uid}/pdf')
    def pdf(uid:str,role:Role,download:bool=False):
        allow(role)
        with store().db() as db:data=snapshot(db,uid)
        safe=''.join(c for c in uid if c.isalnum() or c=='-')[:100]
        disposition='attachment' if download else 'inline'
        return Response(render_pdf(data,logo=logo(),settings=report_settings.read(store())),media_type='application/pdf',headers={'Content-Disposition':f'{disposition}; filename="re-pxe-job-{safe}.pdf"','Cache-Control':'private, no-store','Content-Security-Policy':"default-src 'none'; frame-ancestors 'self'"})
