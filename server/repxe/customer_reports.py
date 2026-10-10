"""Final customer PDFs, downloadable only by their accepted job credential."""
import hashlib
import hmac
import json
import re
from datetime import datetime, timezone
from fastapi import HTTPException, Request, Response
from . import branding, job_reports, report_settings
from .durable import atomic_json

def validate_finish_evidence(db, uid, snapshot):
    if snapshot.get('stage')!='complete':return
    run=db.execute('SELECT spec FROM routine_runs WHERE job_id=?',(uid,)).fetchone()
    options=next((s.get('finish_options') for s in json.loads(run['spec'])['steps'] if s['kind']=='windows'),None) if run else None
    if options is not None:
        expected={key:value for key,value in options.items() if key!='account_password'}
        if snapshot.get('finishOptions')!=expected:raise HTTPException(422,'Final customer handoff differs from the published routine')

def install(app, store):
    def authorized(db, uid, request):
        value=request.headers.get('authorization','')
        cap=db.execute('SELECT * FROM report_credentials WHERE job_id=?',(uid,)).fetchone()
        if (not value.startswith('Bearer ') or len(value)>300 or not cap or
            not hmac.compare_digest(hashlib.sha256(value[7:].encode()).hexdigest(),cap['token_hash']) or
            datetime.fromisoformat(cap['expires'])<datetime.now(timezone.utc)):
            raise HTTPException(401,'Valid job report credential required')
        job=db.execute('SELECT * FROM jobs WHERE id=?',(uid,)).fetchone()
        run=db.execute('SELECT * FROM routine_runs WHERE job_id=?',(uid,)).fetchone()
        options=next((s.get('finish_options') for s in json.loads(run['spec'])['steps'] if s['kind']=='windows'),None) if run else None
        if not job or job['stage']!='complete' or not cap['payload_hash']:
            raise HTTPException(409,'Final report must be accepted first')
        if not options or not options.get('save_report_desktop'):
            raise HTTPException(403,'Customer report was not selected in this published routine')
        return run

    def package(uid, request):
        target=store().root/'reports'/('customer-'+uid)
        with store().lock('customer-report-'+uid):
            with store().db() as db:
                run=authorized(db,uid,request)
                metadata=target/'manifest.json'
                if metadata.exists():return target,json.loads(metadata.read_text())
                from . import routines
                routines.sync(db)
                data=job_reports.snapshot(db,'routine:'+run['id'])
                originals=[bytes(r['certificate']) for r in db.execute('SELECT w.certificate FROM routine_erase_disks d JOIN workflow_runs w ON w.id=d.workflow_id WHERE d.run_id=? AND w.certificate IS NOT NULL ORDER BY d.ordinal',(run['id'],))]
            logo=branding.read(store())
            files=[('re-pxe-report.pdf',job_reports.render_pdf(data,logo=logo['png'] if logo else None,settings=report_settings.read(store())))]
            files += [('erasure-drive-'+str(i+1)+'.pdf',pdf) for i,pdf in enumerate(originals)]
            if sum(len(pdf) for _,pdf in files)>64*1024**2:raise HTTPException(413,'Customer reports exceed 64 MiB')
            target.mkdir(exist_ok=True)
            from .durable import atomic_bytes
            entries=[]
            for name,pdf in files:
                atomic_bytes(target/name,pdf)
                entries.append({'name':name,'path':f'/api/client-jobs/{uid}/customer-report/{name}','size':len(pdf),'sha256':hashlib.sha256(pdf).hexdigest()})
            result={'schemaVersion':1,'jobId':uid,'files':entries}
            atomic_json(metadata,result)
            return target,result

    @app.get('/api/client-jobs/{uid}/customer-report')
    def manifest(uid:str,request:Request):
        if not re.fullmatch(r'[a-f0-9-]{36}',uid):raise HTTPException(404,'Job not found')
        return package(uid,request)[1]

    @app.get('/api/client-jobs/{uid}/customer-report/{name}')
    def pdf(uid:str,name:str,request:Request):
        if not re.fullmatch(r'[a-f0-9-]{36}',uid):raise HTTPException(404,'Job not found')
        target,manifest=package(uid,request)
        entry=next((item for item in manifest['files'] if item['name']==name),None)
        if not entry:raise HTTPException(404,'Customer PDF not found')
        payload=(target/name).read_bytes()
        if len(payload)!=entry['size'] or hashlib.sha256(payload).hexdigest()!=entry['sha256']:raise HTTPException(503,'Stored customer PDF failed integrity verification')
        return Response(payload,media_type='application/pdf',headers={'Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff'})
