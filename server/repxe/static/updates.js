'use strict';
let portalUpdateState=null,portalUpdateBusy=false,portalUpdateTimer=null;
function renderPortalUpdates(value){
 portalUpdateState=value;
 $('update-current').textContent=value.current_revision?.slice(0,12)||'Unavailable';
 $('update-available').textContent=value.target_revision?.slice(0,12)||'Check for updates';
 $('update-summary').textContent=value.summary||'';
 $('update-status').textContent=value.detail||'Check for updates.';
 $('update-status').classList.toggle('error',value.phase==='failed'||value.phase==='unavailable');
 $('update-check').disabled=!!(portalUpdateBusy||!value.enabled||value.busy||value.recovery_required);
 $('update-apply').disabled=!!(portalUpdateBusy||!value.can_update||value.busy||value.recovery_required);
 $('update-recover').hidden=!value.recovery_required;
 $('update-recover').disabled=portalUpdateBusy||value.busy||!value.enabled;
}
function clearPortalUpdates(){if(portalUpdateTimer)clearTimeout(portalUpdateTimer);portalUpdateTimer=null;portalUpdateState=null;portalUpdateBusy=false;}
function schedulePortalUpdates(){
 if(portalUpdateTimer)clearTimeout(portalUpdateTimer);
 portalUpdateTimer=null;
 if(!webSession||tab!=='settings'||settingsSection!=='server')return;
 portalUpdateTimer=setTimeout(()=>{portalUpdateTimer=null;refreshPortalUpdates();},3000);
}
async function refreshPortalUpdates(){
 const user=webSession?.user.id;
 try{
  const state=await api('/system-updates');
  if(webSession?.user.id!==user||tab!=='settings'||settingsSection!=='server')return;
  renderPortalUpdates(state);
 }catch(e){
  if(webSession?.user.id!==user||tab!=='settings'||settingsSection!=='server')return;
  $('update-status').textContent='Waiting for the portal to reconnect… '+e.message;
  $('update-check').disabled=true;$('update-apply').disabled=true;$('update-recover').disabled=true;
 }finally{schedulePortalUpdates();}
}
async function runPortalUpdate(action){
 if(portalUpdateBusy||!portalUpdateState?.enabled||portalUpdateState.busy)return;
 const revision=portalUpdateState.target_revision;
 if(action==='apply'&&(!portalUpdateState.can_update||!window.confirm('Update to '+revision.slice(0,12)+'? Services will restart briefly. Finish all jobs and uploads first. A database recovery copy is saved before the restart.')))return;
 if(action==='recover'&&(!portalUpdateState.recovery_required||!window.confirm('Recover the previous containers and pre-update database? The failed update and recovery evidence will be retained.')))return;
 portalUpdateBusy=true;renderPortalUpdates(portalUpdateState);
 const user=webSession?.user.id;
 try{
  const state=await api('/system-updates/'+action,{method:'POST',body:JSON.stringify(action==='apply'?{expected_revision:revision,confirm:true}:{})});
  if(webSession?.user.id===user)renderPortalUpdates(state);
 }catch(e){if(webSession?.user.id===user)$('update-status').textContent=e.message;}
 finally{if(webSession?.user.id===user){portalUpdateBusy=false;renderPortalUpdates({...portalUpdateState,detail:$('update-status').textContent});schedulePortalUpdates();}}
}
$('update-check').addEventListener('click',()=>runPortalUpdate('check'));
$('update-apply').addEventListener('click',()=>runPortalUpdate('apply'));
$('update-recover').addEventListener('click',()=>runPortalUpdate('recover'));
