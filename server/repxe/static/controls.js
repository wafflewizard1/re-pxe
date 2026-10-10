function attentionLabel(status){return status.includes('Needs attention')?status:'Needs attention · '+status;}
// Status badges open the one action currently needed, without changing the job.
function clientStatusBadge(c){return c.needs_attention?'<button type="button" class="badge warn attention-link" data-attention-client="'+esc(c.id)+'" aria-label="Needs attention for '+esc(deviceLabel(c))+'">'+esc(attentionLabel(stageName(c.stage)))+' ↗</button>':'<span class="badge">'+esc(stageName(c.stage))+'</span>';}
function routineStatusBadge(r){return r.needs_attention?'<button type="button" class="badge warn attention-link" data-attention-routine="'+esc(r.id)+'" aria-label="Needs attention for '+esc(deviceLabel(r))+'">'+esc(attentionLabel(routineRunStatus(r)))+' ↗</button>':'<span class="badge">'+esc(routineRunStatus(r))+'</span>';}
function routineCancelButton(r){return r.can_cancel?'<button type="button" class="danger compact" data-routine-action="cancel" data-run="'+esc(r.id)+'" data-step="'+r.step+'">Cancel job</button>':'';}
async function cancelRoutineJob(r,confirm=true){
 if(confirm&&!window.confirm('Cancel job'+(r.system_serial?' for '+deviceLabel(r):'')+'? This ends the routine and clears its attention request. Reports and erasure evidence will be retained.'))return;
 await api('/routine-runs/'+r.id+'/action',{method:'POST',body:JSON.stringify({action:'cancel',expected_step:Number(r.step)})});
 closePortalDialogs();routineRunId=null;rememberPortalNavigation();await refresh();message('Job cancelled. Reports and erasure evidence retained.');
}
async function showAttention(subject,id,record=true){
 const generation=portalNavigationGeneration,user=webSession?.user.id;
 const r=subject==='routine'?await api('/routine-runs/'+id):null;
 const data=subject==='client'?await api('/clients/'+id):r?.client_id?await api('/clients/'+r.client_id):null,c=data?.client;
 if(!webSession||user!==webSession.user.id||generation!==portalNavigationGeneration)return;
 if(c)clientRows.set(c.id,c);
 const device=r||c;$('attention-title').textContent=deviceLabel(device);$('attention-error').textContent='';
 const disk=c?.disk_selection_ready&&!['failed','complete','cancelled'].includes(r?.stage);
 let actions=disk?'<button type="button" class="primary compact" data-client-disk="'+esc(c.id)+'" data-profile="'+esc(c.configuration_id)+'">Use this drive</button>':'';
 if(r?.can_continue)actions+='<button class="primary compact" data-routine-action="continue" data-run="'+esc(r.id)+'" data-step="'+r.step+'">Continue routine</button>';
 if(r)actions+=routineCancelButton(r)+(r.can_remove?'<button class="secondary compact" data-routine-remove-job="'+esc(r.id)+'">Remove job</button>':'');else if(c?.can_cancel)actions+='<button type="button" class="danger compact" data-client-action="cancel" data-client-id="'+esc(c.id)+'">Cancel job</button>';
 actions+='<button type="button" class="secondary compact" '+(r?'data-routine-run="'+esc(r.id)+'"':'data-client-details="'+esc(c.id)+'"')+'>Asset details</button>';
 $('attention-actions').innerHTML=actions;
 if(disk){$('attention-content').innerHTML=diskSelectionHtml(c).replace(/<div class="actions">[\s\S]*?<\/div>/,'');}
 else if(r?.can_continue){$('attention-content').innerHTML='<h3>Technician checkpoint</h3><p>'+esc(r.steps[r.step]?.note||'Complete the work on the PC, then continue this routine.')+'</p>';}
 else{$('attention-content').innerHTML='<h3>'+esc(r?routineRunStatus(r):stageName(c.stage))+'</h3><p>'+esc(r?.error||c?.error||(!c?.online&&c?.pending?'The unfinished client is offline. Check the PC and network connection.':'Inspect the PC to resolve this request.'))+'</p>'+(['erasing','operation-pending'].includes(r?.erase_stage)?'<p class="info-note">Inspect the erasure on the PC. Cancellation is unavailable while a wipe is running or its controller outcome is unknown.</p>':'')+(c?'<button class="secondary" data-windows-controls="'+esc(c.id)+'">Open Windows controls</button>':'');}
 openPortalDialog('attention-dialog');if(record)rememberPortalNavigation({kind:'attention',subject,id});
}
async function showWindowsControls(id,record=true){
 const generation=portalNavigationGeneration,user=webSession?.user.id,data=await api('/clients/'+id);
 if(!webSession||user!==webSession.user.id||generation!==portalNavigationGeneration)return;
 const c=data.client;clientRows.set(c.id,c);$('windows-controls-title').textContent=deviceLabel(c);
 $('windows-controls-actions').innerHTML=clientActions(c)+'<button class="secondary compact" data-client-details="'+esc(c.id)+'">Asset details</button>';
 $('windows-controls-content').innerHTML='<p>'+clientStatusBadge(c)+'</p>'+stats([['Status',stageName(c.stage)],['Installation disk',c.target||'Not selected'],['Windows',c.windows||'Not selected'],['Windows preset',c.configuration_name||'Not selected'],['Last seen',clientMinutesAgo(c.last_seen)],['Session',c.id]])+'<div class="transfer">'+clientProgress(c)+'</div>'+(c.error?'<p class="error">'+esc(c.error)+'</p>':'')+(c.boot_blocked?'<p class="info-note">Driver completion is pending. Finish setup on the PC, or stop the PC locally before choosing Reinstall.</p>':'')+'<h3>Deployment events</h3>'+clientTimeline(data.job?.events||[])+'<h3>Operator actions</h3>'+clientTimeline(data.controls.map(r=>({stage:clientControlTitles[r.action]||r.action,timestamp:r.timestamp,detail:r.reason})));
 openPortalDialog('windows-controls-dialog');if(record)rememberPortalNavigation({kind:'windows',id});
}
for(const [button,dialog] of [['attention-close','attention-dialog'],['windows-controls-close','windows-controls-dialog']])$(button).addEventListener('click',()=>$(dialog).close());
for(const id of ['attention-actions','windows-controls-actions','windows-controls-content'])$(id).addEventListener('click',clientClick);
document.addEventListener('click',async e=>{const client=e.target.closest('[data-attention-client]'),routine=e.target.closest('[data-attention-routine]'),windows=e.target.closest('[data-windows-controls]');try{if(client)await showAttention('client',client.dataset.attentionClient);else if(routine)await showAttention('routine',routine.dataset.attentionRoutine);else if(windows)await showWindowsControls(windows.dataset.windowsControls);}catch(error){message(error.message,true);}});

$('editor-unsaved-close').addEventListener('click',()=>$('editor-keep').click());

function recentStatusBadge(j){
 const status=j.status_label||stageName(j.stage),attention=['needs-operator','failed','waiting-for-input'].includes(j.stage);
 if(attention&&j.run_id)return '<button type="button" class="badge warn attention-link" data-attention-routine="'+esc(j.run_id)+'">'+esc(attentionLabel(status))+' ↗</button>';
 if(attention&&j.client_id)return '<button type="button" class="badge warn attention-link" data-attention-client="'+esc(j.client_id)+'">'+esc(attentionLabel(status))+' ↗</button>';
 return '<span class="badge">'+esc(status)+'</span>';
}
