'use strict';
let portalNavigationReady=false,portalNavigationRestoring=false,portalNavigationModal=null,portalNavigationState=null,portalNavigationGeneration=0;
const portalNavigationCloseEvents=new Map();
function closePortalDialogs(except=null){for(const id of ['details','report-viewer','attention-dialog','windows-controls-dialog']){if(id===except||!$(id).open)continue;portalNavigationCloseEvents.set(id,(portalNavigationCloseEvents.get(id)||0)+1);$(id).close();}if(except!=='details')routineRunId=null;}
function openPortalDialog(id){closePortalDialogs(id);$(id).showModal();}
function portalRoute(){
 const route={tab};
 if(tab==='settings')route.settings=settingsSection;
 if(tab==='jobs')Object.assign(route,{view:clientView,search:clientSearch,page:clientPage,routineView:routineJobsView,routineSearch:routineJobsSearch,routinePage:routineJobsPage});
 if(tab==='files')route.folder={...fileLocation};
 if(tab==='editor'&&editorFile)route.file={root:editorFile.root,path:editorFile.path};
 if(tab==='guides')route.guide=activeGuide;
 if(tab==='wizard')route.step=wizardProgress.step;
 return {...route,modal:portalNavigationModal};
}
function portalRouteUrl(route){const query=new URLSearchParams();if(route.settings)query.set('section',route.settings);if(route.guide)query.set('guide',route.guide);if(route.step!==undefined)query.set('step',route.step);return '#'+route.tab+(query.size?'?'+query:'');}
function rememberPortalNavigation(modal=null){
 if(!portalNavigationReady||portalNavigationRestoring||!webSession)return;
 portalNavigationModal=modal;const route=portalRoute();
 if(JSON.stringify(route)===JSON.stringify(portalNavigationState?.route))return;
 const state={user:webSession.user.id,index:(portalNavigationState?.index||0)+1,route};
 portalNavigationGeneration++;window.history.pushState({repxe:state},'',portalRouteUrl(route));portalNavigationState=state;
}
function initializePortalNavigation(){
 const saved=window.history.state?.repxe;
 const [name,query='']=window.location.hash.slice(1).split('?'),params=new URLSearchParams(query);
 const route=saved?.user===webSession.user.id?saved.route:{tab:Object.hasOwn(titles,name)?name:'dashboard',settings:params.get('section')||undefined,guide:params.get('guide')||undefined,step:params.has('step')?Number(params.get('step')):undefined,modal:null};
 portalNavigationReady=true;portalNavigationState={user:webSession.user.id,index:saved?.user===webSession.user.id?saved.index:0,route};
 window.history.replaceState({repxe:portalNavigationState},'',portalRouteUrl(route));
 return restorePortalNavigation(portalNavigationState);
}
async function restorePortalNavigation(state){
 if(!webSession||state?.user!==webSession.user.id||!Object.hasOwn(titles,state?.route?.tab))return;
 const generation=++portalNavigationGeneration,route=state.route;portalNavigationRestoring=true;portalNavigationState=state;portalNavigationModal=null;
 try{
  closePortalDialogs();
  for(const id of ['review','driver-delete-dialog','file-create-dialog'])$(id).close();
  if(route.settings&&['general','appearance','server','network','storage','pxe','users'].includes(route.settings))switchSettings(route.settings,false);
  if(route.tab==='jobs'){
   clientView=['current','pending','attention','online','offline','all','history'].includes(route.view)?route.view:'current';clientSearch=route.search||'';clientPage=Math.max(1,Number(route.page)||1);$('client-search').value=clientSearch;
   routineJobsView=['active','attention','all','complete','failed','cancelled'].includes(route.routineView)?route.routineView:'active';routineJobsSearch=route.routineSearch||'';routineJobsPage=Math.max(1,Number(route.routinePage)||1);$('routine-job-search').value=routineJobsSearch;$('routine-job-view').value=routineJobsView;
  }
  if(route.folder&&typeof route.folder.root==='string'&&typeof route.folder.path==='string')fileLocation={...route.folder};
  if(route.tab==='guides')activeGuide=guides.some(g=>g.id===route.guide)?route.guide:null;
  if(route.tab==='wizard'&&Number.isInteger(route.step))setWizardStep(route.step,false);
  switchTab(route.tab,false);portalNavigationModal=route.modal||null;portalNavigationRestoring=false;
  if(route.tab==='editor'&&route.file&&!(editorFile?.root===route.file.root&&editorFile?.path===route.file.path)){
   if(editorDirty)message('Your unsaved text remains open. Save or discard it before returning to another file.',true);
   else await openFile(route.file.root,route.file.path,false);
  }
  if(generation!==portalNavigationGeneration)return;
  if(route.modal?.kind==='client')await showClient(route.modal.id,false);
  else if(route.modal?.kind==='routine')await showRoutineRun(route.modal.id,false);
  else if(route.modal?.kind==='report')await showReport(route.modal.id,false);
  else if(route.modal?.kind==='windows')await showWindowsControls(route.modal.id,false);
  else if(route.modal?.kind==='attention')await showAttention(route.modal.subject,route.modal.id,false);
  if(generation===portalNavigationGeneration)portalNavigationModal=route.modal||null;
 }catch(error){message(error.message,true);}finally{portalNavigationRestoring=false;}
}
window.addEventListener('popstate',event=>{if(event.state?.repxe)return restorePortalNavigation(event.state.repxe);});
for(const [id,kinds] of [['details',['client','routine']],['report-viewer',['report']],['attention-dialog',['attention']],['windows-controls-dialog',['windows']]])$(id).addEventListener('close',()=>{const ignored=portalNavigationCloseEvents.get(id)||0;if(ignored){portalNavigationCloseEvents.set(id,ignored-1);return;}if(kinds.includes(portalNavigationModal?.kind))rememberPortalNavigation();});
