'use strict';
let backupDirty=false,backupData=null,backupRestoreName=null;
async function loadBackups(){
 const user=webSession?.user.id,data=await api('/backups');
 if(tab!=='settings'||settingsSection!=='storage'||webSession?.user.id!==user)return;
 backupData=data;
 if(!backupDirty){const s=data.settings;$('backup-enabled').checked=s.enabled;$('backup-interval').value=s.interval_hours;$('backup-retain').value=s.retain;$('backup-nas-enabled').checked=s.nas_enabled;$('backup-destination').value=s.nas_destination;$('backup-user').value=s.username;$('backup-password').placeholder=s.has_password?'Saved password · leave blank to keep':'Blank for guest';}
 const state=data.status;$('backup-status').textContent=state.running?'Backup in progress…':state.error?'Backup failed: '+state.error:state.last_success?'Last verified backup: '+dateText(state.last_success):'No verified backup yet.';
 $('backup-status').classList.toggle('error',!!state.error);$('backup-create').disabled=!!state.running;
 $('backup-folder').textContent=data.settings.nas_destination+data.nas_folder.replaceAll('/','\\')+'\\';
 $('backup-scope').textContent=data.scope;
 $('backup-snapshots').innerHTML=data.snapshots.length?table(['Snapshot','Verification / location','Controls'],data.snapshots.map(r=>[esc(dateText(r.created))+'<small>'+esc(r.name)+(r.pre_restore?' · preserved before restore':'')+'</small>',esc(bytes(r.bytes))+'<small>SHA-256 · '+esc(r.sha256)+'</small><small>'+(r.nas_path?'NAS read-back verified':'Local recovery snapshot')+'</small>','<a class="secondary compact" href="/api/backups/'+esc(r.name)+'/download" download>Download</a> <button class="secondary compact" data-backup-restore="'+esc(r.name)+'">Restore…</button>'])):empty('No recovery snapshots');
}
$('backup-settings').addEventListener('input',()=>{backupDirty=true;});
$('backup-settings').addEventListener('submit',async e=>{
 e.preventDefault();const button=e.submitter;button.disabled=true;
 const body={enabled:$('backup-enabled').checked,interval_hours:Number($('backup-interval').value),retain:Number($('backup-retain').value),nas_enabled:$('backup-nas-enabled').checked,nas_destination:$('backup-destination').value.trim(),username:$('backup-user').value.trim(),clear_credentials:$('backup-clear-password').checked};
 if($('backup-password').value)body.password=$('backup-password').value;
 try{await api('/backups/settings',{method:'PUT',body:JSON.stringify(body)});backupDirty=false;$('backup-password').value='';$('backup-clear-password').checked=false;await loadBackups();message('Backup settings saved.');}catch(error){message(error.message,true);}finally{button.disabled=false;}
});
$('backup-test').addEventListener('click',async e=>{
 if(backupDirty){message('Save backup settings before testing the NAS.',true);return;}
 e.target.disabled=true;try{const r=await api('/backups/test-nas',{method:'POST'});message('NAS connected using '+(r.guest?'guest access':'saved credentials')+'. Only '+r.folder+' will be used.');}catch(error){message(error.message,true);}finally{e.target.disabled=false;}
});
$('backup-create').addEventListener('click',async()=>{
 if(backupDirty){message('Save backup settings before creating a backup.',true);return;}
 $('backup-create').disabled=true;try{await api('/backups/create',{method:'POST'});message('Backup requested. Verification and NAS status will update here.');await loadBackups();}catch(error){message(error.message,true);}finally{if(!backupData?.status.running)$('backup-create').disabled=false;}
});
$('backup-snapshots').addEventListener('click',e=>{
 const button=e.target.closest('[data-backup-restore]');if(!button)return;backupRestoreName=button.dataset.backupRestore;
 const record=backupData.snapshots.find(r=>r.name===backupRestoreName);$('backup-restore-summary').textContent='Restore '+dateText(record.created)+' ('+record.name+').';$('backup-confirm').value='';$('backup-acknowledge').checked=false;$('backup-restore-error').textContent='';$('backup-restore-dialog').showModal();
});
$('backup-restore-cancel').addEventListener('click',()=>$('backup-restore-dialog').close());
$('backup-restore-form').addEventListener('submit',async e=>{
 e.preventDefault();const button=e.submitter;button.disabled=true;
 try{const r=await api('/backups/restore',{method:'POST',body:JSON.stringify({snapshot:backupRestoreName,confirmation:$('backup-confirm').value,acknowledge_data_replacement:$('backup-acknowledge').checked})});$('backup-restore-dialog').close();signedOut();message('Database restored. Sign in with the snapshot’s account credentials. The previous database was preserved as '+r.preserved_current_snapshot+'.');}catch(error){message(error.message,true);$('backup-restore-error').textContent=error.message;}finally{button.disabled=false;}
});
function clearBackupSession(){backupDirty=false;backupData=null;backupRestoreName=null;$('backup-password').value='';$('backup-restore-dialog').close();}
