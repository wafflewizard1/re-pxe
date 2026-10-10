let reportTotal=0;
let reportPage=1,reportPages=1,reportRows=[],reportBusy=false,viewerClientId=null,viewerRoutineId=null;
function defaultReportFilters(){return {search:'',status:'all',start:'',end:'',sort:'processed',order:'desc',date_field:'activity',timezone:Intl.DateTimeFormat().resolvedOptions().timeZone||'UTC',page_size:'50'};}
let reportFilters=defaultReportFilters();
const selectedReports=new Map();
function reportQuery(){const q=new URLSearchParams({page:String(reportPage)});for(const [key,value] of Object.entries(reportFilters))if(value)q.set(key,value);return '?'+q;}
function clientProgress(c){
 if(c.stage==='applying-image')return c.install_percent===null||c.install_percent===undefined?'<small>Waiting for installation progress</small>':'<progress max="100" value="'+c.install_percent+'" aria-label="Windows installation"></progress><small>'+c.install_percent+'% installed</small>';
 if(c.stage==='downloading'&&c.total_bytes)return '<progress max="'+c.total_bytes+'" value="'+c.received_bytes+'" aria-label="Image download"></progress><small>'+Math.round(c.received_bytes/c.total_bytes*100)+'% downloaded · '+rate(c.bytes_per_second)+'</small>';
 return '<small>'+esc(c.boot_blocked?'Driver completion pending':c.error?'Needs attention':stageName(c.stage))+'</small>';
}
function renderSimpleClients(d){
 const layout=normalizeDashboardLayout(dashboardLayout()),columns=layout.columns.filter(c=>c.visible),cards=layout.clientMode==='cards',render=cards?dashboardJobCards:dashboardJobTable,runs=sortDevices(d.routines?.active||[]);
 const runRows=runs.map(r=>({client:'<button class="client-name" data-routine-run="'+esc(r.id)+'">'+esc(deviceLabel(r))+'</button><small>'+esc([r.manufacturer,r.model].filter(Boolean).join(' · '))+'</small>',routine:dashboardRoutineCell(r),status:'<div class="dashboard-step-status">'+routineStatusBadge(r)+dashboardStepProgress(r)+'</div>',drives:dashboardDrives(r),activity:esc(clientMinutesAgo(r.last_activity||r.updated,d.sampled_at)),controls:'<div class="actions dashboard-job-actions">'+routineJobActions(r)+'</div>',model:esc([r.manufacturer,r.model].filter(Boolean).join(' · ')),serial:esc(r.system_serial||'Not reported'),mac:esc(r.mac),version:esc(r.version),error:r.error?'<span class="error">'+esc(r.error)+'</span>':'<span class="muted">None</span>'}));
 $('dashboard-routine-clients').innerHTML=runs.length?render(columns,runRows):empty('No matching routine jobs');
 if(d.routines?.active_total>runs.length)$('dashboard-routine-clients').innerHTML+='<small>'+runs.length+' of '+d.routines.active_total+' routine jobs · open Jobs for all.</small>';
 const clients=sortDevices(d.clients||[]).filter(c=>!c.routine);for(const c of clients)clientRows.set(c.id,c);
 const rows=clients.slice(0,25).map(c=>({client:'<span class="status-dot '+(c.online?'online':'offline')+'"></span><button class="client-name" data-client-details="'+esc(c.id)+'">'+esc(deviceLabel(c))+'</button><small>'+esc([c.manufacturer,c.model].filter(Boolean).join(' · '))+'</small>',routine:esc(c.configuration_name||c.windows||'Standalone session'),status:'<div class="dashboard-step-status">'+clientStatusBadge(c)+(['applying-image','downloading'].includes(c.stage)?clientProgress(c):'')+'</div>',drives:dashboardDrives(c),activity:esc(clientMinutesAgo(c.last_seen,d.sampled_at)),controls:clientDismissButton(c),model:esc([c.manufacturer,c.model].filter(Boolean).join(' · ')),serial:esc(c.system_serial||'Not reported'),mac:esc(c.mac),version:'<span class="muted">—</span>',error:esc(c.error||'None')}));
 $('dashboard-clients').innerHTML=clients.length?render(columns,rows):runs.length?'':empty('No other sessions');
 if((d.client_total||0)>25)$('dashboard-clients').innerHTML+='<p class="muted">25 of '+d.client_total+' clients · open Jobs for all.</p>';
}

function assetReports(attachments=[]){
 if(!attachments.length)return '';
 return '<section class="asset-reports"><h3>Asset reports</h3>'+attachments.map(a=>'<div class="asset-report"><strong>'+esc(a.name||'Routine report')+'</strong><small>'+esc(a.system_serial||'Serial unavailable')+' · '+esc(stageName(a.stage))+' · '+esc(dateText(a.created))+'</small><div class="actions"><button class="secondary compact" data-report-view="'+esc(a.report_id)+'">View re-pxe report</button>'+(a.pdf_url?'<a class="secondary compact" href="'+esc(a.pdf_url)+'" download>Download PDF</a>':'')+(a.certificates?.length?a.certificates.map(d=>'<a class="secondary compact" href="'+esc(d.url)+'" download>ShredOS'+(d.phase?' · '+esc(d.phase):'')+' · '+esc(d.target)+'</a>').join(''):a.certificate_url?'<a class="secondary compact" href="'+esc(a.certificate_url)+'" download>ShredOS report</a>':'')+'</div></div>').join('')+'</section>';
}
function deviceSpecs(device,c={}){
 const s=device?.specs||{},system=s.system||{},memory=s.memory||{},firmware=s.firmware||{},board=s.board||{};
 const cpus=(s.processors||[]).map(p=>[p.name,p.cores?p.cores+' cores':'',p.threads?p.threads+' threads':'',p.max_speed_mhz?p.max_speed_mhz+' MHz max':''].filter(Boolean).join(' · ')).join('; ');
 const model=system.model||c.model||'Unidentified device';
 let html='<section class="device-specs"><div class="device-hero"><svg viewBox="0 0 96 72" aria-hidden="true"><rect x="17" y="8" width="62" height="43" rx="5"/><path d="M17 51L5 62Q4 67 11 67H85Q92 67 91 62L79 51M34 61H62"/></svg><div><p class="eyebrow">'+esc(system.manufacturer||c.manufacturer||'Device')+'</p><h3>'+esc(model)+'</h3><p class="muted">System serial · '+esc(system.serial||c.system_serial||'Not reported')+'</p></div></div>';
 html+=stats([['Processor',cpus||'Not reported'],['Installed RAM',memory.total_bytes?bytes(memory.total_bytes):'Not reported'],['Asset tag',system.asset_tag||'Not reported'],['SKU / family',[system.sku,system.family].filter(Boolean).join(' / ')||'Not reported'],['Firmware',[firmware.type,firmware.manufacturer,firmware.version,firmware.release_date].filter(Boolean).join(' / ')||'Not reported'],['Secure Boot',firmware.secure_boot===null||firmware.secure_boot===undefined?'Not reported':firmware.secure_boot?'Enabled':'Disabled'],['TPM',firmware.tpm||'Not reported'],['Motherboard',[board.manufacturer,board.model,board.version].filter(Boolean).join(' / ')||'Not reported'],['Board serial',board.serial||'Not reported'],['System UUID',system.uuid||'Not reported'],['Graphics',(s.graphics||[]).map(d=>d.name).join('; ')||'Not reported'],['Network adapters',(s.network||[]).map(d=>d.name).join('; ')||'Not reported']]);
 if(memory.modules?.length)html+='<h3>Memory modules</h3>'+table(['Slot','Capacity / speed','Type / manufacturer','Serial / part'],memory.modules.map(m=>[esc(m.location),bytes(m.size_bytes)+(m.speed_mts?' · '+m.speed_mts+' MT/s':''),esc([m.type,m.manufacturer].filter(Boolean).join(' / ')),esc([m.serial,m.part_number].filter(Boolean).join(' / '))]));
 if(s.storage?.length)html+='<h3>Storage specifications</h3>'+table(['Model','Serial','Capacity / interface'],s.storage.map(d=>[esc(d.model||'Not reported'),esc(d.serial||d.identity||'Not reported'),bytes(d.size_bytes)+' · '+esc(d.bus_type||'Not reported')+(d.removable?' · removable':'')]));
 for(const d of s.storage||[]){const h=d.health;if(!h)continue;html+='<h3>SMART · '+esc(d.serial||d.identity)+'</h3>'+stats([['Health',h.passed===true?'Passed':h.passed===false?'FAILED':h.detail||'Not available'],['Temperature',h.temperature_c==null?'Not reported':h.temperature_c+' °C'],['Power-on hours',h.power_on_hours??'Not reported'],['Power cycles',h.power_cycles??'Not reported'],['NVMe endurance used',h.percentage_used==null?'Not reported':h.percentage_used+'%'],['Reallocated / pending / uncorrectable sectors',[h.reallocated_sectors,h.pending_sectors,h.uncorrectable_sectors].map(v=>v??'—').join(' / ')],['Media errors / critical warning',[h.media_errors,h.critical_warning].map(v=>v??'—').join(' / ')],['Firmware / protocol',[h.firmware,h.protocol].filter(Boolean).join(' / ')||'Not reported'],['Collected',h.collected_utc?dateText(h.collected_utc):'Not reported']]);if(h.attributes?.length)html+='<details><summary>SMART attributes</summary>'+table(['ID / attribute','Value / worst / threshold','Raw / failure'],h.attributes.map(a=>[esc(a.id+' · '+a.name),esc([a.value,a.worst,a.threshold].map(v=>v??'—').join(' / ')),esc(a.raw)+(a.when_failed?'<small>'+esc(a.when_failed)+'</small>':'')]))+'</details>'; }
 if(s.batteries?.length)html+='<h3>Batteries</h3>'+table(['Name / serial','Design / full charge','Health / cycles'],s.batteries.map(d=>[esc(d.name)+'<small>'+esc([d.manufacturer,d.serial].filter(Boolean).join(' / '))+'</small>',(d.design_capacity_mwh?esc(d.design_capacity_mwh+' mWh'):'Not reported')+'<small>'+(d.full_charge_capacity_mwh?esc(d.full_charge_capacity_mwh+' mWh full charge'):'Full charge not reported')+'</small>',(d.health_percent!==null&&d.health_percent!==undefined?esc(d.health_percent+'%'):'Health not reported')+'<small>'+(d.cycle_count!==null&&d.cycle_count!==undefined?esc(d.cycle_count+' cycles'):'Cycles not reported')+'</small>']));
 if(!device?.inventory_available)html+='<p class="info-note">Detailed inventory unavailable for this session.</p>';
 if(s.warnings?.length)html+='<p class="muted">'+s.warnings.map(esc).join(' · ')+'</p>';
 return html+'</section>';
}
function reportColumn(label,key){
 const active=reportFilters.sort===key,descending=reportFilters.order==='desc';
 return '<th scope="col" aria-sort="'+(active?(descending?'descending':'ascending'):'none')+'"><button type="button" class="report-sort-heading" data-report-sort="'+key+'" title="Sort '+esc(label)+' '+(active&&!descending?'descending':'ascending')+'">'+esc(label)+'<span aria-hidden="true">'+(active?(descending?'↓':'↑'):'↕')+'</span></button></th>';
}
function sortReportsBy(key){
 if(!['serial','routine','status','processed'].includes(key))return;
 reportFilters.order=reportFilters.sort===key&&reportFilters.order==='asc'?'desc':'asc';reportFilters.sort=key;reportPage=1;
 $('report-sort').value=key;$('report-order').value=reportFilters.order;refresh();
}
function updateReportFilterToggle(){
 const count=Number(!!reportFilters.search)+Number(reportFilters.status!=='all')+Number(!!(reportFilters.start||reportFilters.end))+Number(reportFilters.date_field!=='activity');
 $('report-filter-toggle').textContent='Filters'+(count?' ('+count+')':'');
}
function renderReports(data){
 const focusedSort=document.activeElement?.dataset.reportSort;
 reportTotal=data.total;reportRows=data.reports;reportPage=data.page;reportPages=data.pages;
 $('report-count').textContent=data.total+' reports';$('report-filter-summary').textContent='Search matches recorded device and component values. Dates use '+(reportFilters.timezone||'UTC')+'.'+(reportTotal>200?' Narrow filters to export all matching reports (maximum 200).':'');$('report-page-label').textContent='Page '+reportPage+' of '+reportPages;
 $('report-prev').disabled=reportPage<=1;$('report-next').disabled=reportPage>=reportPages;
 updateReportFilterToggle();
 const headings='<th scope="col">Select</th>'+reportColumn('Laptop / serial','serial')+reportColumn('Routine','routine')+reportColumn('Outcome','status')+reportColumn('Processed / last activity','processed')+'<th scope="col">Report</th>';
 $('reports-table').innerHTML=reportRows.length?'<table><thead><tr>'+headings+'</tr></thead><tbody>'+reportRows.map(r=>[
 '<input class="report-select" type="checkbox" data-report-select="'+esc(r.id)+'" aria-label="Select report for '+esc(deviceLabel(r))+'"'+(selectedReports.has(r.id)?' checked':'')+'>',
 '<button class="client-name" data-report-view="'+esc(r.id)+'">'+esc(deviceLabel(r))+'</button><div class="muted">'+esc(r.model||'Model unavailable')+'</div>',
 esc(r.routine_name||'Earlier Windows job')+(r.routine_version?'<small>v'+r.routine_version+'</small>':''),
 '<span class="badge '+(['failed','needs-operator'].includes(r.stage)?'warn':'')+'">'+esc(r.status_label)+'</span><small>'+esc(r.id.slice(0,8))+'</small>',
 esc(dateText(r.processed_at||r.updated))+'<small>'+(r.processed_at?'Processed':'Pending · last activity')+'</small>',
 '<button class="secondary compact" data-report-view="'+esc(r.id)+'">View PDF</button> <a class="secondary compact" href="/api/job-reports/'+encodeURIComponent(r.id)+'/pdf?download=true" download>Download</a>'+(r.has_shred_report?' <button class="secondary compact" data-shred-report="'+esc(r.id)+'">Shred report</button>':'')
 ]).map(row=>'<tr>'+row.map(cell=>'<td>'+cell+'</td>').join('')+'</tr>').join('')+'</tbody></table>':empty('No matching reports');
 if(focusedSort)$('reports-table').querySelector('[data-report-sort="'+focusedSort+'"]')?.focus?.();
 updateReportSelection();
}
async function refreshReports(){const query=reportQuery();const [data]=await Promise.all([api('/job-reports'+query),typeof loadReportPreferences==='function'?loadReportPreferences():Promise.resolve()]);if(webSession&&tab==='reports'&&query===reportQuery())renderReports(data);}
function updateReportSelection(){
 const visible=new Set(reportRows.map(r=>r.id)),hidden=[...selectedReports.keys()].filter(id=>!visible.has(id)).length;
 $('report-selection').textContent=selectedReports.size+' selected'+(hidden?' · '+hidden+' hidden':'')+(selectedReports.size>200?' · select up to 200 per export':'');
 $('report-download-matching').disabled=reportBusy||!reportTotal||reportTotal>200;
 $('report-download-matching').textContent='Export matching ('+reportTotal+')';
 const master=$('report-select-visible');master.checked=visible.size>0&&[...visible].every(id=>selectedReports.has(id));master.indeterminate=!master.checked&&[...visible].some(id=>selectedReports.has(id));master.disabled=reportBusy;
 $('report-download-selected').disabled=reportBusy||!selectedReports.size||selectedReports.size>200;$('report-clear-selection').disabled=reportBusy||!selectedReports.size;
}
function closeReportViewer(){ $('report-viewer').close();$('report-frame').removeAttribute('src');viewerClientId=null;viewerRoutineId=null;}
async function showReport(id,record=true,shred=false){
 const generation=portalNavigationGeneration,user=webSession?.user.id;const data=await api('/job-reports/'+encodeURIComponent(id));if(!webSession||webSession.user.id!==user||generation!==portalNavigationGeneration)return;
 $('details').close();const s=data.specs.system,j=data.job;
 $('report-viewer-title').textContent='Report · '+deviceLabel({system_serial:s.serial,model:s.model||j.device_model,id:j.id});
 const certificate=data.erasure?.certificate_url||data.device?.attachments?.find(a=>a.report_id===id)?.certificate_url; $('report-viewer-certificate').hidden=!certificate;if(certificate)$('report-viewer-certificate').dataset.erasurePdf=certificate;
 const driveCertificates=(data.erasures||[]).flatMap(d=>[...(d.certificate_url?[d]:[]),...(d.phase_certificates||[]).map(phase=>({...phase,certificate_url:phase.url,result:d.result}))]);
 $('report-viewer-certificate').hidden=driveCertificates.length>0||!certificate;
 $('report-viewer-drive-certificates').innerHTML=driveCertificates.map(d=>'<button type="button" class="secondary" data-erasure-pdf="'+esc(d.certificate_url)+'" title="'+esc((d.phase==='followup'?'Original nwipe PRNG PDF':'Original nwipe PDF')+' · '+d.target)+'"><span>'+esc(d.phase==='followup'?'Shred PRNG PDF':'Shred report')+'</span><small>'+esc(d.target.replace(/^serial:/,''))+'</small></button>').join('');
 $('report-viewer-summary').innerHTML=stats([['System serial',s.serial||'Not reported'],['Outcome',data.status_label],['Furthest recorded stage',data.furthest_label],['Processed',data.processed_at?dateText(data.processed_at):'Pending'],['Job started',dateText(j.created)],['Routine',data.routine?data.routine.name+' · v'+data.routine.version:'Earlier Windows job'],['Job',j.id]])+(data.error?'<p class="error">'+esc(data.error)+'</p>':'');
 const url='/api/job-reports/'+encodeURIComponent(id)+'/pdf';$('report-viewer-job').dataset.jobPdf=url;const original=driveCertificates[0]?.certificate_url||certificate;if(shred&&original)viewErasurePdf(original);else viewReportPdf(url);
 viewerClientId=data.client_id;viewerRoutineId=data.routine?.id||null;$('report-viewer-client').hidden=!viewerClientId&&!viewerRoutineId;$('report-viewer-client').textContent='Asset details';openPortalDialog('report-viewer');if(record)rememberPortalNavigation({kind:'report',id});
}
function markReportChoice(url){for(const button of $('report-viewer-choices').querySelectorAll('[data-job-pdf],[data-erasure-pdf]'))button.setAttribute('aria-pressed',String((button.dataset.jobPdf||button.dataset.erasurePdf)===url));}
function viewReportPdf(url){markReportChoice(url);$('report-frame').title='re-pxe report PDF viewer';$('report-frame').src=url;$('report-viewer-open').href=url;$('report-viewer-download').href=url+'?download=true';}
function viewErasurePdf(url){if(!/^\/api\/workflow-runs\/[a-f0-9-]{36}\/certificate(?:\/followup)?$/.test(url))return;markReportChoice(url);$('report-frame').title='Shred report PDF viewer';$('report-frame').src=url+'?download=false';$('report-viewer-open').href=url+'?download=false';$('report-viewer-download').href=url;}
$('report-viewer-drive-certificates').addEventListener('click',e=>{const button=e.target.closest('[data-erasure-pdf]');if(button)viewErasurePdf(button.dataset.erasurePdf);});
$('report-viewer-certificate').addEventListener('click',()=>viewErasurePdf($('report-viewer-certificate').dataset.erasurePdf));
$('report-viewer-job').addEventListener('click',()=>viewReportPdf($('report-viewer-job').dataset.jobPdf));
$('report-viewer-close').addEventListener('click',closeReportViewer);
$('report-viewer').addEventListener('cancel',()=>{$('report-frame').removeAttribute('src');viewerClientId=null;viewerRoutineId=null;});
$('report-viewer-client').addEventListener('click',async()=>{const id=viewerClientId,run=viewerRoutineId;try{if(run)await showRoutineRun(run);else if(id)await showClient(id);}catch(e){message(e.message,true);}});
function applyReportFilters(){
 for(const key of Object.keys(reportFilters))reportFilters[key]=$('report-'+key).value.trim();
 if(reportFilters.start&&reportFilters.end&&reportFilters.start>reportFilters.end){message('From date must be before Through date.',true);return;}
 reportPage=1;refresh();
}
let reportSearchTimer;
$('report-filter-toggle').addEventListener('click',()=>{const panel=$('report-filter-panel');panel.hidden=!panel.hidden;$('report-filter-toggle').setAttribute('aria-expanded',String(!panel.hidden));});
$('report-filters').addEventListener('submit',e=>{e.preventDefault();clearTimeout(reportSearchTimer);applyReportFilters();});
$('report-search').addEventListener('input',()=>{clearTimeout(reportSearchTimer);reportSearchTimer=setTimeout(applyReportFilters,350);});
$('report-filters').addEventListener('change',e=>{if(e.target.id==='report-date-preset')return;if(e.target.id==='report-timezone'&&!['all','custom'].includes($('report-date-preset').value)){applyReportDatePreset();return;}if(['report-start','report-end'].includes(e.target.id))$('report-date-preset').value='custom';applyReportFilters();});
$('report-reset').addEventListener('click',()=>{clearTimeout(reportSearchTimer);reportFilters=defaultReportFilters();for(const [key,value] of Object.entries(reportFilters))$('report-'+key).value=value;$('report-date-preset').value='all';reportPage=1;refresh();});
for(const [key,value] of Object.entries(reportFilters))$('report-'+key).value=value;
const reportZone=reportFilters.timezone;if(reportZone!=='UTC')$('report-timezone').innerHTML+='<option value="'+esc(reportZone)+'">'+esc(reportZone)+'</option>';$('report-timezone').value=reportZone;
function applyReportDatePreset(){
 const preset=$('report-date-preset').value;if(preset==='custom')return;
 if(preset==='all'){$('report-start').value='';$('report-end').value='';}
 else{const parts=new Intl.DateTimeFormat('en-CA',{timeZone:$('report-timezone').value,year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(new Date());const pick=k=>parts.find(p=>p.type===k).value;const end=pick('year')+'-'+pick('month')+'-'+pick('day');const start=new Date(end+'T12:00:00Z');start.setUTCDate(start.getUTCDate()-(preset==='today'?0:Number(preset)-1));$('report-start').value=start.toISOString().slice(0,10);$('report-end').value=end;}
 applyReportFilters();
}
$('report-date-preset').addEventListener('change',applyReportDatePreset);
$('report-prev').addEventListener('click',()=>{if(reportPage>1){reportPage--;refresh();}});$('report-next').addEventListener('click',()=>{if(reportPage<reportPages){reportPage++;refresh();}});
$('report-select-visible').addEventListener('change',e=>{for(const r of reportRows){if(e.target.checked)selectedReports.set(r.id,r);else selectedReports.delete(r.id);}renderReports({reports:reportRows,total:Number.parseInt($('report-count').textContent),page:reportPage,pages:reportPages});});
$('report-clear-selection').addEventListener('click',()=>{selectedReports.clear();for(const c of document.querySelectorAll('.report-select'))c.checked=false;updateReportSelection();});
$('reports-table').addEventListener('change',e=>{const id=e.target.dataset.reportSelect;if(!id)return;if(e.target.checked)selectedReports.set(id,reportRows.find(r=>r.id===id));else selectedReports.delete(id);updateReportSelection();});
$('reports-table').addEventListener('click',async e=>{const heading=e.target.closest('button[data-report-sort]');if(heading){sortReportsBy(heading.dataset.reportSort);return;}const b=e.target.closest('button[data-report-view],button[data-shred-report]');if(b)try{await showReport(b.dataset.reportView||b.dataset.shredReport,true,!!b.dataset.shredReport);}catch(e){message(e.message,true);}});
$('dashboard-clients').addEventListener('click',clientClick);
async function exportReports(matching){
 if(reportBusy||!webSession)return;
 const user=webSession.user.id,filters={...reportFilters};delete filters.page_size;for(const key of ['start','end'])if(!filters[key])delete filters[key];
 const selection=matching?{filters}:{ids:[...selectedReports.keys()]};
 reportBusy=true;updateReportSelection();
 try{const res=await fetch('/api/job-reports/export',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-CSRF-Token':webSession.csrf_token},body:JSON.stringify({...selection,include_certificates:$('report-export-certificates').checked})});if(!res.ok){if(res.status===401)signedOut();const body=await res.json();throw Error(typeof body.detail==='string'?body.detail:JSON.stringify(body.detail));}const blob=await res.blob();if(!webSession||webSession.user.id!==user)return;const url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='re-pxe-job-reports.zip';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);message('Report ZIP downloaded.');}catch(e){message(e.message,true);}finally{reportBusy=false;updateReportSelection();}
}
$('report-download-selected').addEventListener('click',()=>exportReports(false));
$('report-download-matching').addEventListener('click',()=>exportReports(true));

function clearReportSession(){clearTimeout(reportSearchTimer);if(typeof clearReportPreferences==='function')clearReportPreferences();reportTotal=0;selectedReports.clear();reportRows=[];reportPage=1;reportBusy=false;closeReportViewer();$('reports-table').innerHTML='';$('report-viewer-summary').innerHTML='';}

function renderStorageChart(storage){
 const valid=Number.isFinite(storage?.total)&&storage.total>0&&Number.isFinite(storage?.free);
 const used=valid?Math.max(0,storage.total-storage.free):0,percent=valid?Math.max(0,Math.min(100,Math.round(used/storage.total*100))):0;
 $('storage-chart').innerHTML='<div class="storage-overview"><svg class="storage-ring" viewBox="0 0 120 120" role="img" aria-label="'+(valid?percent+'% storage used':'Storage unavailable')+'"><circle class="ring-track" cx="60" cy="60" r="46"/><circle class="ring-value" cx="60" cy="60" r="46" pathLength="100" stroke-dasharray="'+percent+' 100" transform="rotate(-90 60 60)"/><text class="ring-number" x="60" y="58" text-anchor="middle">'+(valid?percent+'%':'—')+'</text><text class="ring-label" x="60" y="76" text-anchor="middle">used</text></svg><dl class="server-stats"><div><dt>Free</dt><dd>'+(valid?bytes(storage.free):'—')+'</dd></div><div><dt>Used</dt><dd>'+(valid?bytes(used):'—')+'</dd></div><div><dt>Total</dt><dd>'+(valid?bytes(storage.total):'—')+'</dd></div></dl></div>'+stats([['Images',Number.isFinite(storage?.images?.bytes)?bytes(storage.images.bytes):'—'],['Driver files',Number.isFinite(storage?.drivers?.stored_blob_bytes)?bytes(storage.drivers.stored_blob_bytes):'—']]);
}
