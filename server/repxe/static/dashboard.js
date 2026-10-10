'use strict';
const dashboardWidgets=[['metrics','Status tiles'],['clients','Clients'],['server','Server'],['storage','Storage'],['bandwidth','Bandwidth'],['recent','Recent jobs']];
const dashboardColumns=[['client','Client'],['routine','Routine / step'],['status','Status'],['drives','Drives'],['activity','Last activity'],['controls','Controls'],['model','Manufacturer / model'],['serial','System serial'],['mac','MAC'],['version','Routine version'],['error','Error']];
const dashboardColumnDefaults=()=>dashboardColumns.map(([id],i)=>({id,visible:i<6}));
const dashboardHeightBounds={clients:[220,900],server:[220,700],storage:[220,700],bandwidth:[220,720],recent:[200,900]};
const dashboardDefaults=()=>dashboardWidgets.map(([id])=>({id,visible:true,width:['metrics','clients','recent'].includes(id)?12:4,height:null}));
const dashboardPresets={
 overview:{name:'Overview',widgets:dashboardDefaults(),clientMode:'table',clientFilter:'current'},
 clients:{name:'Clients',widgets:dashboardDefaults().map(w=>({...w,visible:['metrics','clients','recent'].includes(w.id)})),clientMode:'cards',clientFilter:'current'},
 storage:{name:'Storage',widgets:dashboardDefaults().map(w=>({...w,visible:['metrics','storage','server','bandwidth'].includes(w.id),width:w.id==='storage'||w.id==='server'?6:12})),clientMode:'table',clientFilter:'current'}
};
let dashboardViews={selected:'overview',views:[]},dashboardDraft=null,dashboardStorageValue=null,dashboardStorageAt=0;
function normalizeDashboardLayout(value={}){
 const known=new Set(dashboardWidgets.map(([id])=>id)),widgets=[];
 for(const w of Array.isArray(value?.widgets)?value.widgets:[]){
  if(!w||!known.delete(w.id))continue;
  const bounds=dashboardHeightBounds[w.id];
  widgets.push({id:w.id,visible:w.visible!==false,width:Number.isInteger(w.width)&&w.width>=3&&w.width<=12?w.width:12,height:bounds&&Number.isFinite(w.height)?Math.max(bounds[0],Math.min(bounds[1],Math.round(w.height/20)*20)):null});
 }
 for(const w of dashboardDefaults())if(known.has(w.id))widgets.push(w);
 const columnIds=new Set(dashboardColumns.map(([id])=>id)),columns=[];
 for(const c of Array.isArray(value?.columns)?value.columns:[]){if(c&&columnIds.delete(c.id))columns.push({id:c.id,visible:c.visible!==false});}
 for(const c of dashboardColumnDefaults())if(columnIds.has(c.id))columns.push({...c,visible:Array.isArray(value?.columns)?false:c.visible});
 if(!columns.some(c=>c.visible))columns.splice(0,columns.length,...dashboardColumnDefaults());
 return {widgets,columns,clientMode:value?.clientMode==='cards'?'cards':'table',clientFilter:['current','pending','attention','online','all'].includes(value?.clientFilter)?value.clientFilter:'current'};
}
function dashboardPreferenceKey(){return 'repxe.dashboard.v1.'+webSession.user.id;}
function dashboardLayout(){return dashboardDraft||(dashboardPresets[dashboardViews.selected]||dashboardViews.views.find(v=>v.id===dashboardViews.selected)||dashboardPresets.overview);}
function saveDashboardPreferences(){writeBrowserValue(dashboardPreferenceKey(),dashboardViews);}
function loadDashboardViews(){
 const saved=readBrowserValue(dashboardPreferenceKey(),{}),ids=new Set();
 dashboardViews.views=(Array.isArray(saved?.views)?saved.views:[]).slice(0,12).flatMap(v=>{
  if(!v||typeof v.id!=='string'||!/^view-[a-z0-9-]{1,50}$/.test(v.id)||ids.has(v.id))return [];
  ids.add(v.id);const layout=normalizeDashboardLayout(v);if(!layout.widgets.some(w=>w.visible))layout.widgets=dashboardDefaults();
  return [{id:v.id,name:typeof v.name==='string'?v.name.trim().slice(0,48)||'My dashboard':'My dashboard',...layout}];
 });
 dashboardViews.selected=dashboardPresets[saved?.selected]||dashboardViews.views.some(v=>v.id===saved?.selected)?saved.selected:'overview';dashboardDraft=null;$('dashboard-layout-editor').hidden=true;renderDashboardViews();applyDashboardLayout();
}
function renderDashboardViews(){
 $('dashboard-view').innerHTML=Object.entries(dashboardPresets).map(([id,v])=>'<option value="'+id+'">'+v.name+'</option>').join('')+dashboardViews.views.map(v=>'<option value="'+esc(v.id)+'">'+esc(v.name)+'</option>').join('');
 $('dashboard-view').value=dashboardViews.selected;$('dashboard-delete-view').hidden=!!dashboardPresets[dashboardViews.selected];
}
function applyDashboardLayout(){
 const layout=normalizeDashboardLayout(dashboardLayout()),grid=$('dashboard-grid');
 for(const w of layout.widgets){const node=$('dashboard-widget-'+w.id);node.hidden=!w.visible;node.dataset.width=String(w.width);node.style.setProperty('--widget-columns',w.width);node.style.setProperty('--widget-height',w.height===null?'auto':w.height+'px');node.classList.toggle('widget-fixed-height',w.height!==null);if(!dashboardResize)grid.appendChild(node);
  if(!node.querySelector('[data-resize-widget]')){const handle=document.createElement('button');handle.type='button';handle.className='widget-resize';handle.dataset.resizeWidget=w.id;handle.textContent='↘';handle.setAttribute('aria-label','Resize '+dashboardWidgets.find(([id])=>id===w.id)[1]);handle.title='Drag to resize. Arrow keys change width'+(dashboardHeightBounds[w.id]?' and height.':'.');node.appendChild(handle);}}
 grid.classList.toggle('layout-editing',!!dashboardDraft);if(lastDashboard&&tab==='dashboard')renderSimpleClients(lastDashboard);
 if(tab==='dashboard')drawBandwidth();
}
function renderDashboardEditor(){
 $('dashboard-widget-controls').innerHTML=dashboardDraft.widgets.map((w,i)=>'<div class="widget-control"><label class="check"><input type="checkbox" data-widget-visible="'+w.id+'"'+(w.visible?' checked':'')+'>'+dashboardWidgets.find(([id])=>id===w.id)[1]+'</label><select data-widget-width="'+w.id+'" aria-label="Width of '+w.id+'">'+Array.from({length:10},(_,i)=>[i+3,({4:'Third',6:'Half',12:'Full'})[i+3]||((i+3)+' / 12')]).map(([width,name])=>'<option value="'+width+'"'+(w.width===width?' selected':'')+'>'+name+'</option>').join('')+'</select>'+(dashboardHeightBounds[w.id]?'<label class="widget-height-label">Height<input type="number" min="'+dashboardHeightBounds[w.id][0]+'" max="'+dashboardHeightBounds[w.id][1]+'" step="20" placeholder="Auto" data-widget-height="'+w.id+'" value="'+(w.height??'')+'" aria-label="Height of '+w.id+' in pixels"></label>':'')+'<button type="button" class="secondary compact" data-widget-move="'+w.id+'" data-direction="-1" aria-label="Move '+w.id+' up"'+(i===0?' disabled':'')+'>↑</button><button type="button" class="secondary compact" data-widget-move="'+w.id+'" data-direction="1" aria-label="Move '+w.id+' down"'+(i===dashboardDraft.widgets.length-1?' disabled':'')+'>↓</button></div>').join('');
 $('dashboard-column-controls').innerHTML=dashboardDraft.columns.map((c,i)=>'<div class="column-control"><label class="check"><input type="checkbox" data-column-visible="'+c.id+'"'+(c.visible?' checked':'')+'>'+dashboardColumns.find(([id])=>id===c.id)[1]+'</label><button type="button" class="secondary compact" data-column-move="'+c.id+'" data-direction="-1" aria-label="Move '+c.id+' column left"'+(i===0?' disabled':'')+'>←</button><button type="button" class="secondary compact" data-column-move="'+c.id+'" data-direction="1" aria-label="Move '+c.id+' column right"'+(i===dashboardDraft.columns.length-1?' disabled':'')+'>→</button></div>').join('');
 $('dashboard-client-mode').value=dashboardDraft.clientMode;$('dashboard-client-filter').value=dashboardDraft.clientFilter;
}
function endDashboardEdit(){dashboardResize=null;dashboardDraft=null;$('dashboard-layout-editor').hidden=true;applyDashboardLayout();}
function moveDashboardWidget(id,to){const widgets=dashboardDraft.widgets,from=widgets.findIndex(w=>w.id===id);if(from<0||to<0||to>=widgets.length)return;widgets.splice(to,0,widgets.splice(from,1)[0]);renderDashboardEditor();applyDashboardLayout();}
function dashboardClientQuery(){return '?view='+normalizeDashboardLayout(dashboardLayout()).clientFilter;}
async function refreshDashboardStorage(){
 if(dashboardStorageValue&&Date.now()-dashboardStorageAt<30000)return;
 const user=webSession?.user.id;
 try{const info=await api('/server-info');if(webSession?.user.id===user){dashboardStorageValue=info.storage;dashboardStorageAt=Date.now();}}
 catch{/* Keep a previous sample while the server is unavailable. */}
}
function clearDashboardState(){dashboardViews={selected:'overview',views:[]};dashboardDraft=null;dashboardStorageValue=null;dashboardStorageAt=0;$('dashboard-layout-editor').hidden=true;}
$('dashboard-view').addEventListener('change',e=>{dashboardViews.selected=e.target.value;dashboardDraft=null;$('dashboard-layout-editor').hidden=true;saveDashboardPreferences();renderDashboardViews();applyDashboardLayout();refresh();});
$('dashboard-edit-layout').addEventListener('click',()=>{dashboardDraft=normalizeDashboardLayout(dashboardLayout());$('dashboard-view-name').value=dashboardPresets[dashboardViews.selected]?'My dashboard':dashboardLayout().name||dashboardViews.views.find(v=>v.id===dashboardViews.selected).name;$('dashboard-layout-editor').hidden=false;renderDashboardEditor();applyDashboardLayout();$('dashboard-view-name').focus();});
$('dashboard-layout-cancel').addEventListener('click',()=>{endDashboardEdit();refresh();});
$('dashboard-layout-reset').addEventListener('click',()=>{dashboardDraft=normalizeDashboardLayout(dashboardPresets.overview);renderDashboardEditor();applyDashboardLayout();refresh();});
$('dashboard-layout-form').addEventListener('change',e=>{
 const input=e.target,w=dashboardDraft.widgets.find(w=>w.id===(input.dataset.widgetVisible||input.dataset.widgetWidth||input.dataset.widgetHeight));
 if(w){if(input.dataset.widgetVisible)w.visible=input.checked;else if(input.dataset.widgetHeight){const bounds=dashboardHeightBounds[w.id];w.height=input.value===''?null:Math.max(bounds[0],Math.min(bounds[1],Math.round(Number(input.value)/20)*20));input.value=w.height??'';}else w.width=Number(input.value);}
 const column=dashboardDraft.columns.find(c=>c.id===input.dataset.columnVisible);if(column){column.visible=input.checked;if(!dashboardDraft.columns.some(c=>c.visible)){column.visible=true;input.checked=true;message('Show at least one column.',true);}}
 if(input.id==='dashboard-client-mode')dashboardDraft.clientMode=input.value;
 if(input.id==='dashboard-client-filter'){dashboardDraft.clientFilter=input.value;refresh();}
 applyDashboardLayout();
});
$('dashboard-widget-controls').addEventListener('click',e=>{const b=e.target.closest('button[data-widget-move]');if(b)moveDashboardWidget(b.dataset.widgetMove,dashboardDraft.widgets.findIndex(w=>w.id===b.dataset.widgetMove)+Number(b.dataset.direction));});
$('dashboard-layout-form').addEventListener('submit',e=>{
 e.preventDefault();if(!dashboardDraft.widgets.some(w=>w.visible)){message('Show at least one widget.',true);return;}
 const name=$('dashboard-view-name').value.trim();if(!name){$('dashboard-view-name').focus();return;}
 const index=dashboardViews.views.findIndex(v=>v.id===dashboardViews.selected);
 if(index<0&&dashboardViews.views.length>=12){message('Delete a saved view before adding another.',true);return;}
 const id=index>=0?dashboardViews.selected:'view-'+Date.now().toString(36)+'-'+Math.random().toString(36).slice(2,8),view={id,name:name.slice(0,48),...normalizeDashboardLayout(dashboardDraft)};
 if(index>=0)dashboardViews.views[index]=view;else dashboardViews.views.push(view);
 dashboardViews.selected=id;saveDashboardPreferences();renderDashboardViews();endDashboardEdit();refresh();message('Dashboard view saved.');
});
$('dashboard-delete-view').addEventListener('click',()=>{dashboardViews.views=dashboardViews.views.filter(v=>v.id!==dashboardViews.selected);dashboardViews.selected='overview';saveDashboardPreferences();renderDashboardViews();endDashboardEdit();refresh();message('View deleted.');});
let draggedDashboardWidget=null;
$('dashboard-grid').addEventListener('dragstart',e=>{const handle=e.target.closest('[data-drag-widget]');if(!dashboardDraft||!handle){e.preventDefault();return;}draggedDashboardWidget=handle.dataset.dragWidget;e.dataTransfer.setData('text/plain',draggedDashboardWidget);e.dataTransfer.effectAllowed='move';});
$('dashboard-grid').addEventListener('dragover',e=>{if(dashboardDraft&&draggedDashboardWidget&&e.target.closest('[data-dashboard-widget]'))e.preventDefault();});
$('dashboard-grid').addEventListener('drop',e=>{if(!dashboardDraft||!draggedDashboardWidget)return;e.preventDefault();const target=e.target.closest('[data-dashboard-widget]');if(target)moveDashboardWidget(draggedDashboardWidget,dashboardDraft.widgets.findIndex(w=>w.id===target.dataset.dashboardWidget));draggedDashboardWidget=null;});
$('dashboard-grid').addEventListener('dragend',()=>{draggedDashboardWidget=null;});

$('dashboard-column-controls').addEventListener('click',e=>{const b=e.target.closest('[data-column-move]');if(!dashboardDraft||!b)return;const columns=dashboardDraft.columns,from=columns.findIndex(c=>c.id===b.dataset.columnMove),to=from+Number(b.dataset.direction);if(from<0||to<0||to>=columns.length)return;columns.splice(to,0,columns.splice(from,1)[0]);renderDashboardEditor();applyDashboardLayout();});
let dashboardResize=null;
function updateDashboardSize(id,width,height){if(!dashboardDraft)return;const w=dashboardDraft.widgets.find(w=>w.id===id);if(!w)return;w.width=Math.max(3,Math.min(12,Math.round(width)));const bounds=dashboardHeightBounds[id];if(bounds&&height!==null)w.height=Math.max(bounds[0],Math.min(bounds[1],Math.round(height/20)*20));applyDashboardLayout();}
$('dashboard-grid').addEventListener('pointerdown',e=>{const handle=e.target.closest('[data-resize-widget]');if(!dashboardDraft||!handle||e.button!==0)return;e.preventDefault();handle.focus({preventScroll:true});const id=handle.dataset.resizeWidget,w=dashboardDraft.widgets.find(w=>w.id===id),node=$('dashboard-widget-'+id),grid=$('dashboard-grid').getBoundingClientRect();dashboardResize={id,x:e.clientX,y:e.clientY,width:w.width,height:node.getBoundingClientRect().height,column:(grid.width+16)/12,pointer:e.pointerId};handle.setPointerCapture(e.pointerId);});
$('dashboard-grid').addEventListener('pointermove',e=>{if(!dashboardDraft||!dashboardResize||e.pointerId!==dashboardResize.pointer)return;const r=dashboardResize;updateDashboardSize(r.id,r.width+(e.clientX-r.x)/r.column,dashboardHeightBounds[r.id]?r.height+e.clientY-r.y:null);});
function endDashboardResize(){if(!dashboardResize)return;dashboardResize=null;if(dashboardDraft)renderDashboardEditor();}
$('dashboard-grid').addEventListener('pointerup',endDashboardResize);$('dashboard-grid').addEventListener('pointercancel',endDashboardResize);
$('dashboard-grid').addEventListener('keydown',e=>{const handle=e.target.closest('[data-resize-widget]');if(!dashboardDraft||!handle||!['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(e.key))return;e.preventDefault();const id=handle.dataset.resizeWidget,w=dashboardDraft.widgets.find(w=>w.id===id),vertical=e.key==='ArrowUp'||e.key==='ArrowDown';updateDashboardSize(id,w.width+(e.key==='ArrowLeft'?-1:e.key==='ArrowRight'?1:0),vertical?($('dashboard-widget-'+id).getBoundingClientRect().height+(e.key==='ArrowUp'?-20:20)):null);renderDashboardEditor();});
function routineOverallPercent(r){
 if(r.stage==='complete')return 100;
 const steps=r.steps||[];if(!steps.length)return 0;
 const edges=r.connections||[],path=[],seen=new Set(),history=Array.isArray(r.step_history)?r.step_history.filter(i=>Number.isInteger(i)&&i>=0&&i<steps.length):[];
 const next=index=>{const edge=edges.find(e=>e.source===steps[index]?.id&&e.outcome==='success');return edges.length?steps.findIndex(s=>s.id===edge?.target):index+1;};
 let index=0;
 if(history.length){for(const i of history)if(!seen.has(i)){path.push(i);seen.add(i);}if(Number.isInteger(r.step)&&r.step>=0&&r.step<steps.length){if(!seen.has(r.step)){path.push(r.step);seen.add(r.step);}index=steps[r.step].kind==='finish'?-1:next(r.step);}}
 while(index>=0&&index<steps.length&&!seen.has(index)){path.push(index);seen.add(index);if(steps[index].kind==='finish')break;index=next(index);}
 // Follow the executed branch, then the remaining Success route; terminal failure stays below 100%.
 let done=path.indexOf(r.step),total=path.length;if(done<0){done=Math.max(0,Math.min(steps.length-1,r.step||0));total=steps.length;}
 let fraction=0;const kind=steps[r.step]?.kind;
 if(kind==='erase'){const disks=r.erase_disks||[];if(disks.length)fraction=disks.reduce((sum,d)=>sum+(d.stage==='complete'||d.stage==='install-ready'?1:Number.isFinite(d.percent)?Math.max(0,Math.min(100,d.percent))/100:0),0)/disks.length;}
 if(kind==='windows'){if(r.client_stage==='applying-image'&&Number.isFinite(r.install_percent))fraction=Math.max(0,Math.min(100,r.install_percent))/100;else if(['image-applied','boot-configured','recovery-registered','drivers-staged','agent-staged','awaiting-windows','completing','ready-for-oobe'].includes(r.client_stage))fraction=1;}
 return Math.max(0,Math.min(99,Math.floor((done+fraction)/Math.max(1,total)*100)));
}
function dashboardRoutineCell(r){const p=routineOverallPercent(r);return '<strong>'+esc(r.name)+'</strong><small>'+esc(r.step_label||routineKinds[r.steps?.[r.step]?.kind]||r.stage)+'</small><progress max="100" value="'+p+'" aria-label="Overall routine progress"></progress><small>'+p+'% of routine</small>';}
function dashboardStepProgress(r){
 if(r.stage==='windows-running'&&r.client_stage==='applying-image')return clientProgress({stage:'applying-image',install_percent:r.install_percent});
 if(r.stage==='erase-running'){const disks=r.erase_disks||[],complete=disks.filter(d=>['complete','install-ready'].includes(d.stage)).length,active=disks.find(d=>d.stage==='erasing'),p=Number.isFinite(active?.percent)?active.percent:null;return '<small>'+complete+' / '+disks.length+' drives complete</small>'+(active?'<progress max="100" '+(p===null?'':'value="'+p+'"')+' aria-label="Current drive wipe"></progress><small>'+(p===null?'Measuring progress':p+'%')+' · '+esc(wipeRemaining(active.remaining_seconds))+(active.progress_stale?' · Updates stopped':'')+'</small>':'');}
 return '';
}
function dashboardDrives(r){const targets=r.windows_targets||r.targets||[],disks=r.erase_disks?.length?r.erase_disks:r.target?[{target:r.target,size:r.size}]:targets.map(t=>({target:t.identity,size:t.size_bytes,model:t.model}));return disks.length?disks.map(d=>{const t=targets.find(t=>t.identity===d.target)||{};return '<div class="dashboard-drive"><strong>'+esc(d.model||t.model||'Disk')+'</strong><small>'+esc(d.target)+(d.size?' · '+bytes(d.size):'')+'</small></div>';}).join(''):'<span class="muted">Not selected</span>';}
function dashboardRow(values,columns){return columns.map(c=>values[c.id]||'<span class="muted">—</span>');}
function dashboardJobTable(columns,rows){return '<div class="dashboard-jobs-table">'+table(columns.map(c=>dashboardColumns.find(([id])=>id===c.id)[1]),rows.map(v=>dashboardRow(v,columns)))+'</div>';}
function dashboardJobCards(columns,rows){return '<div class="dashboard-client-cards">'+rows.map(v=>'<article class="dashboard-client-card">'+columns.map(c=>'<div class="dashboard-card-field"><small class="dashboard-field-label">'+dashboardColumns.find(([id])=>id===c.id)[1]+'</small>'+v[c.id]+'</div>').join('')+'</article>').join('')+'</div>';}
