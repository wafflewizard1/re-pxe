let clientView='current',clientSearch='',clientPage=1,clientBusy=false;const selectedClients=new Map(),clientRows=new Map();let clientDetailId=null,lastClientsData=null;
function clientQuery(){return '?'+new URLSearchParams({view:clientView,search:clientSearch,page:String(clientPage)});}
function clientActions(c,includeDetails=false){return '<div class="client-actions">'+(includeDetails?'<button class="secondary compact" data-client-details="'+esc(c.id)+'">Details</button>':'')+ (c.can_cancel?'<button class="secondary compact" data-client-action="cancel" data-client-id="'+esc(c.id)+'">Cancel job</button>':'')+(c.can_reset?'<button class="secondary compact" data-client-action="reset" data-client-id="'+esc(c.id)+'">Reinstall</button>':'')+(c.can_delete?'<button class="secondary compact" data-client-action="delete" data-client-id="'+esc(c.id)+'">Remove from list</button>':'')+'</div>';}
function renderClients(d){
 d={...d,clients:sortDevices(d.clients)};lastClientsData=d;
 const counts=d.client_counts||{};clientPage=d.client_page||1;for(const c of d.clients)clientRows.set(c.id,c);
 $('client-count').textContent=(d.client_total??d.clients.length)+' clients';
 $('client-pending').textContent=(counts.pending||0)+' pending';$('client-attention').textContent=(counts.needs_attention||0)+' need attention';
 $('client-page-label').textContent='Page '+clientPage+' of '+(d.client_pages||1);$('client-prev').disabled=clientPage<=1;$('client-next').disabled=clientPage>=(d.client_pages||1);
 $('client-view').value=clientView;
 const focused=document.activeElement?.closest('select[data-client-config]');
 if(pendingClientChanges.size||(focused&&d.clients.some(c=>c.id===focused.dataset.clientConfig&&!c.selection_locked)))return;
 const defaultName=d.configurations.find(p=>p.id===d.settings.default_profile_id)?.name||'First compatible';
 $('client-table').innerHTML=d.clients.length?table(['Select','Device','Routine / Windows preset','Status','Last seen','Actions'],d.clients.map(c=>[
 '<input class="client-select" type="checkbox" data-client-select="'+esc(c.id)+'" aria-label="Select '+esc(deviceLabel(c))+'"'+(selectedClients.has(c.id)?' checked':'')+'>',
 clientDismissButton(c)+'<span class="status-dot '+(c.online?'online':'offline')+'"></span><button type="button" class="client-name" data-client-details="'+esc(c.id)+'">'+esc(deviceLabel(c))+'</button><div class="muted">'+esc([c.manufacturer,c.model].filter(Boolean).join(' · '))+'</div><small>'+esc(c.target||'Disk not selected')+'</small>'+(c.client_version?'<div class="muted">Client '+esc(c.client_version)+'</div>':''),
 clientConfiguration(c,d.configurations,defaultName)+(c.windows?'<small>'+esc(c.windows)+'</small>':''),
 clientStatusBadge(c)+(c.boot_blocked?'<div class="client-hold">Driver completion pending · network install held</div>':'')+(c.pending&&!c.online?'<div class="muted">Unfinished · client offline</div>':'')+(c.credential_expired&&c.pending?'<div class="error">Session credential expired</div>':'')+(c.error?'<div class="error">'+esc(c.error)+'</div>':'')+'<div class="transfer">'+clientProgress(c)+'</div>'+(c.job_id?'<small class="client-job">Job '+esc(c.job_id.slice(0,8))+' · '+esc(stageName(c.job_stage||c.stage))+'</small>':''),
 esc(clientMinutesAgo(c.last_seen,d.sampled_at))+'<div class="muted">'+(c.online?'Recent contact':'Offline')+'</div>',clientActions(c)
 ] )):empty('No matching clients','Pending jobs remain visible even when a client goes offline. Try All clients or Session history.');
 updateClientSelection();
}
function setClientView(view){if(tab!=='jobs')switchTab('jobs');clientView=view;clientPage=1;rememberPortalNavigation();refresh();}
$('client-view').addEventListener('change',e=>setClientView(e.target.value));
$('client-search-form').addEventListener('submit',e=>{e.preventDefault();clientSearch=$('client-search').value.trim();clientPage=1;rememberPortalNavigation();refresh();});
$('client-pending').addEventListener('click',()=>setClientView('pending'));$('client-attention').addEventListener('click',()=>setClientView('attention'));
$('client-prev').addEventListener('click',()=>{if(clientPage>1){clientPage--;rememberPortalNavigation();refresh();}});$('client-next').addEventListener('click',()=>{clientPage++;rememberPortalNavigation();refresh();});
const clientControlTitles={cancel:'Cancel job',reset:'Reset for reinstall',delete:'Delete from Clients'};
function clientTimeline(events){return '<ol class="job-events">'+events.map(e=>'<li><strong>'+esc(stageName(e.stage))+'</strong> <small>'+esc(new Date(e.timestamp).toLocaleString())+'</small>'+(e.detail?'<div>'+esc(e.detail)+'</div>':'')+'</li>').join('')+'</ol>';}
async function showClient(id,record=true){
 const generation=portalNavigationGeneration,user=webSession?.user.id;const data=await api('/clients/'+id);if(!webSession||webSession.user.id!==user||generation!==portalNavigationGeneration)return;const c=data.client,job=data.job,report=typeof job?.report==='string'?JSON.parse(job.report):job?.report;
 const problems=report?.native?.drivers?.problems||report?.driver_problems||[];
 clientRows.set(c.id,c);clientDetailId=c.id;
 $('details-title').textContent='Device · '+deviceLabel(c);
 $('detail-actions').innerHTML=clientActions(c,false)+'<button class="secondary compact" data-windows-controls="'+esc(c.id)+'">Windows controls</button>'+(c.job_id?'<button class="secondary compact" data-report-view="'+esc(c.job_id)+'">View report / PDF</button>':'');
 $('job-summary').innerHTML=(c.needs_attention?'<p>'+clientStatusBadge(c)+'</p>':'')+(c.routine?'<p><button class="client-name" data-routine-run="'+esc(c.routine.id)+'">'+esc(c.routine.name)+' · v'+c.routine.version+'</button></p>':'')+deviceSpecs(data.device,c)+assetReports(data.device?.attachments||data.attachments||[])+(c.boot_blocked?'<p class="info-note">Windows driver completion is pending. The network boot returns to the installed disk to protect this unfinished installation. Finish setup in its console, or stop the machine locally and use Reinstall.</p>':'')+(c.error?'<p class="error">'+esc(c.error)+'</p>':'')+stats([
 ['System serial',deviceSerial(c)||'Not reported'],['Manufacturer / model',[c.manufacturer,c.model].filter(Boolean).join(' / ')||'Not enrolled'],['MAC',c.mac],['Disk',c.target||'Not selected'],['Windows',c.windows||'Not selected'],['Windows preset',c.configuration_name||'Not selected'],['Image',c.image_name||'Not selected'],['Client version',c.client_version||'Not recorded'],['Session',c.id],['Job',c.job_id||'Not started'],['Client status',stageName(c.stage)],['Job status',c.job_stage?stageName(c.job_stage):'Not started'],['Last seen',clientMinutesAgo(c.last_seen)],['Credential', (c.credential_expired?'Expired · ':'Expires · ')+dateText(c.credential_expires)],['Boot hold',c.blocking_job_id||'None'],['Last event',c.last_event||'None'],['Installation progress',c.install_percent===null||c.install_percent===undefined?'Not currently reported':c.install_percent+'%'],['Transferred',bytes(c.received_bytes)+' / '+bytes(c.total_bytes)]
 ])+ '<h3>Detected disks</h3>'+ (c.targets.length?table(['Identity','Model','Capacity'],c.targets.map(t=>[esc(t.identity),esc(t.model||'—'),bytes(t.size_bytes)])):'<p class="muted">No inventory received.</p>')+(problems.length?'<h3>Remaining devices</h3><ul>'+problems.map(p=>'<li>'+esc(p.description||p.instanceId)+' · '+esc(p.instanceId)+' · problem '+esc(p.problemCode)+'</li>').join('')+'</ul>':'')+(report?'<button class="secondary compact" data-client-report="'+esc(c.job_id)+'">Download completion report</button>':'')+'<h3>Deployment events</h3>'+clientTimeline(job?.events||[])+'<h3>Sessions for this device</h3>'+table(['Created','Status','Job'],data.sessions.map(r=>[esc(dateText(r.created))+(r.deleted_at?' · deleted':''),esc(stageName(r.stage)),r.job_id?'<button class="secondary compact" data-job="'+esc(r.job_id)+'">'+esc(r.job_id.slice(0,8))+'</button>':'Not started']))+'<h3>Operator actions</h3>'+clientTimeline(data.controls.map(r=>({stage:clientControlTitles[r.action]||r.action,timestamp:r.timestamp,detail:r.reason})));
 $('detail-text').textContent=JSON.stringify(data,null,2);openPortalDialog('details');if(record)rememberPortalNavigation({kind:'client',id});
}
function clientMinutesAgo(value,at=Date.now()){
 const seen=new Date(value).getTime(),now=typeof at==='number'?at:new Date(at).getTime();
 if(!Number.isFinite(seen)||!Number.isFinite(now))return 'Not recorded';
 const minutes=Math.max(0,Math.floor((now-seen)/60000));return minutes===0?'Now':minutes+' '+(minutes===1?'minute':'minutes')+' ago';
}
function updateClientSelection(){
 const visible=new Set((lastClientsData?.clients||[]).map(c=>c.id));const hidden=[...selectedClients.keys()].filter(id=>!visible.has(id)).length;
 $('client-selection').textContent=selectedClients.size+' selected'+(hidden?' · '+hidden+' hidden':'');
 for(const action of ['cancel','reset','delete'])$('client-bulk-'+action).disabled=clientBusy||![...selectedClients.values()].some(c=>c['can_'+action]);
 $('client-clear-selection').disabled=clientBusy||!selectedClients.size;
 const checkbox=$('client-select-visible');if(checkbox){checkbox.checked=visible.size>0&&[...visible].every(id=>selectedClients.has(id));checkbox.indeterminate=!checkbox.checked&&[...visible].some(id=>selectedClients.has(id));checkbox.disabled=clientBusy;}
}
function clientSelectionChanged(e){
 const input=e.target;if(input.id==='client-select-visible'){
  for(const c of lastClientsData?.clients||[]){if(input.checked)selectedClients.set(c.id,{...c});else selectedClients.delete(c.id);}
  renderClients(lastClientsData);return true;
 }
 if(!input.dataset.clientSelect)return false;const id=input.dataset.clientSelect;
 if(input.checked&&clientRows.has(id))selectedClients.set(id,{...clientRows.get(id)});else selectedClients.delete(id);updateClientSelection();return true;
}
$('client-select-visible').addEventListener('change',clientSelectionChanged);
$('client-clear-selection').addEventListener('click',()=>{selectedClients.clear();if(lastClientsData)renderClients(lastClientsData);});
async function startClientControl(id,action){
 if(clientBusy)return;const c=clientRows.get(id);if(!c||!c['can_'+action])throw new Error('This action is unavailable. Refresh the client details.');
 if(action==='cancel'&&!window.confirm('Cancel job for '+deviceLabel(c)+'? This stops the pending routine/session. Reports and erasure evidence will be retained.'))return;
 if(action==='delete'&&!window.confirm('Remove device '+deviceLabel(c)+' from the portal? Its reports will be retained.'))return;
 clientBusy=true;updateClientSelection();
 try{
  if(action==='cancel'&&c.routine?.can_cancel){await cancelRoutineJob(c.routine,false);selectedClients.delete(id);return;}
  await api('/clients/'+id+(action==='delete'?'':'/'+action),{method:action==='delete'?'DELETE':'POST',body:JSON.stringify({expected_job_id:c.job_id,reason:'Operator requested '+clientControlTitles[action].toLowerCase()})});
  selectedClients.delete(id);closePortalDialogs();rememberPortalNavigation();clientDetailId=null;message(clientControlTitles[action]+' completed.');await refresh();
 }finally{clientBusy=false;updateClientSelection();}
}
async function bulkClientControl(action){
 if(clientBusy)return;const selected=[...selectedClients.values()],eligible=selected.filter(c=>c['can_'+action]);if(!eligible.length)return;if(action==='cancel'&&!window.confirm('Cancel jobs for '+eligible.length+' selected devices? Reports and erasure evidence will be retained.'))return;if(action==='delete'&&!window.confirm('Remove '+eligible.length+' selected devices from the portal? Their reports will be retained.'))return;
 clientBusy=true;updateClientSelection();let succeeded=0;const errors=[];
 try{
  for(let start=0;start<eligible.length;start+=200){
   const result=await api('/clients/bulk',{method:'POST',body:JSON.stringify({action,sessions:eligible.slice(start,start+200).map(c=>({client_id:c.id,expected_job_id:c.job_id}))})});
   for(const r of result.results){if(r.ok){succeeded++;selectedClients.delete(r.clientId);}else errors.push(r.clientId.slice(0,8)+': '+r.error);}
  }
  closePortalDialogs();rememberPortalNavigation();clientDetailId=null;await refresh();
  const unavailable=selected.length-eligible.length;message(succeeded+' sessions '+({cancel:'cancelled',reset:'reset',delete:'deleted'})[action]+'.'+(unavailable?' '+unavailable+' unavailable for this action.':'')+(errors.length?' '+errors.join('; '):''),!!errors.length);
 }catch(error){message(succeeded+' sessions updated. '+error.message,true);}finally{clientBusy=false;updateClientSelection();}
}
for(const action of ['cancel','reset','delete'])$('client-bulk-'+action).addEventListener('click',()=>bulkClientControl(action));
async function clientClick(e){const b=e.target.closest('button');if(!b)return;try{
 if(b.dataset.clientDismiss){await dismissClient(b.dataset.clientDismiss);return;}
 if(b.dataset.clientDetails){await showClient(b.dataset.clientDetails);return;}
 if(b.dataset.clientAction){await startClientControl(b.dataset.clientId,b.dataset.clientAction);return;}
 if(b.dataset.reportView){await showReport(b.dataset.reportView);return;}
 if(b.dataset.clientReport){const report=await api('/jobs/'+b.dataset.clientReport+'/report');const url=URL.createObjectURL(new Blob([JSON.stringify(report,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download='re-pxe-report-'+b.dataset.clientReport+'.json';a.click();URL.revokeObjectURL(url);}
 }catch(error){message(error.message,true);}}
$('client-table').addEventListener('click',clientClick);$('detail-actions').addEventListener('click',clientClick);$('job-summary').addEventListener('click',async e=>{await clientClick(e);if(!e.target.closest('button[data-client-details]')&&!e.target.closest('button[data-client-action]'))await openJob(e);});
function clearClientSession(){selectedClients.clear();clientRows.clear();clientDetailId=null;lastClientsData=null;clientBusy=false;clientView='current';clientSearch='';clientPage=1;$('client-search').value='';}

function clientDismissButton(c){
 return c.can_cancel||c.can_delete?'<button type="button" class="client-dismiss secondary" data-client-dismiss="'+esc(c.id)+'" aria-label="'+'Remove device '+esc(deviceLabel(c))+'" title="'+'Remove device'+'"'+(clientBusy?' disabled':'')+'>Remove device</button>':'';
}
async function dismissClient(id){
 if(clientBusy)return;const c=clientRows.get(id);if(!c||(!c.can_cancel&&!c.can_delete))return;
 if(!window.confirm('Remove device '+deviceLabel(c)+' from the portal?'+(c.can_cancel?' Its pending session will be cancelled.':'')+' Job reports and ShredOS evidence will be retained.'))return;
 const job=c.job_id,body=JSON.stringify({expected_job_id:job,reason:'Operator cleared client from dashboard'});clientBusy=true;updateClientSelection();
 document.querySelectorAll('[data-client-dismiss]').forEach(b=>b.disabled=true);
 try{
  if(c.can_cancel)await api('/clients/'+id+'/cancel',{method:'POST',body});
  await api('/clients/'+id,{method:'DELETE',body});selectedClients.delete(id);message('Session cleared. Job reports retained. Network boot the client to start again.');await refresh();
 }catch(error){message(error.message,true);await refresh();}finally{clientBusy=false;updateClientSelection();document.querySelectorAll('[data-client-dismiss]').forEach(b=>b.disabled=false);}
}

function diskSelectionHtml(c){if(!c.disk_selection_ready)return '';return '<section class="disk-attention"><h3>Needs attention · select Windows disk</h3><p>Choose the drive to install Windows on. Installation starts after review and a five-second countdown.</p><label>Windows installation disk<select id="portal-windows-disk" aria-label="Windows installation disk"><option value="">Choose a drive</option>'+c.targets.map(t=>'<option value="'+esc(t.identity)+'">'+esc([t.model||'Disk',t.identity,bytes(t.size_bytes)].join(' · '))+'</option>').join('')+'</select></label><div class="actions"><button class="primary compact" data-client-disk="'+esc(c.id)+'" data-profile="'+esc(c.configuration_id)+'">Use this drive</button></div></section>';}
document.addEventListener('click',async e=>{const b=e.target.closest('[data-client-disk]');if(!b)return;const identity=$('portal-windows-disk')?.value;if(!identity){message('Choose a drive first.',true);return;}if(!window.confirm('Install Windows on '+identity+'? All partitions on this drive will be replaced.'))return;try{b.disabled=true;await api('/clients/'+b.dataset.clientDisk+'/disk',{method:'POST',body:JSON.stringify({profile_id:b.dataset.profile,target_identity:identity})});closePortalDialogs();rememberPortalNavigation();await refresh();message('Windows disk selected. The PC will review it and begin its countdown.');}catch(err){if($('attention-dialog').open)$('attention-error').textContent=err.message;else message(err.message,true);b.disabled=false;}});
