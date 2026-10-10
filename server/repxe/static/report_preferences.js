/* Shared presentation settings apply to re-pxe PDFs; evidence stays intact. */
let reportPreferenceData=null,reportPreferenceUser=null,reportPreferencePending=null,reportPreferenceDirty=false,reportDraftOrder=[],reportPreviewUrl=null,reportExportDirty=false;
const reportContentGroups=[
 ['Identity and branding',[['show_logo','Workshop logo'],['show_job_details','Job IDs, target and dates'],['show_asset_tag','Asset tag'],['show_network_mac','Network MAC'],['show_system_uuid','System UUID']]],
 ['Specifications',[['show_cpu','Processors'],['show_memory','Total memory'],['show_firmware','Firmware, Secure Boot and TPM'],['show_board','Motherboard and SKU'],['show_graphics','Graphics cards'],['show_network','Network adapters'],['show_storage','Storage devices'],['show_memory_modules','Memory modules and part numbers'],['show_batteries','Battery capacity and health'],['show_smart','SMART health and counters'],['show_inventory_notes','Inventory notes']]],
 ['Routine and erasure',[['show_routine','Routine and process outcome'],['show_routine_steps','Routine steps'],['show_driver_problems','Remaining device problems'],['show_progress','Installation and transfer progress'],['show_erasure','Erasure results'],['show_certificate_hashes','Certificate hashes']]],
 ['History and export',[['show_timeline','Recorded timeline'],['show_operator_actions','Operator actions'],['export_certificates','Include original erasure PDFs in ZIPs']]]
];
const reportGroupNames={specifications:'Device specifications',routine:'Routine and installation',erasure:'Erasure results',history:'Timeline and operator actions'};
async function loadReportPreferences(force=false){
 if(!webSession)return;
 const user=webSession.user.id;
 if(!force&&reportPreferenceUser===user&&reportPreferenceData)return reportPreferenceData;
 if(reportPreferencePending)return reportPreferencePending;
 reportPreferencePending=(async()=>{const data=await api('/report-settings');if(!webSession||webSession.user.id!==user)return;reportPreferenceData=data;reportPreferenceUser=user;if(!reportExportDirty)$('report-export-certificates').checked=data.settings.export_certificates;return data;})();
 try{return await reportPreferencePending;}finally{reportPreferencePending=null;}
}
function renderReportOrder(){
 $('report-section-order').innerHTML=reportDraftOrder.map((key,index)=>'<div class="report-order-row"><span>'+esc((index+1)+'. '+reportGroupNames[key])+'</span><button type="button" class="secondary compact" data-report-move="'+index+'" data-direction="-1" aria-label="Move '+esc(reportGroupNames[key])+' up"'+(index===0?' disabled':'')+'>↑</button><button type="button" class="secondary compact" data-report-move="'+index+'" data-direction="1" aria-label="Move '+esc(reportGroupNames[key])+' down"'+(index===reportDraftOrder.length-1?' disabled':'')+'>↓</button></div>').join('');
}
function renderReportPreferences(value){
 const text=(key,label,type='text',extra='')=>'<label>'+label+'<input data-report-setting="'+key+'" type="'+type+'" value="'+esc(value[key])+'" '+extra+'></label>';
 const select=(key,label,choices)=>'<label>'+label+'<select data-report-setting="'+key+'">'+choices.map(([v,t])=>'<option value="'+v+'"'+(String(value[key])===String(v)?' selected':'')+'>'+t+'</option>').join('')+'</select></label>';
 let html='<fieldset><legend>Page style</legend><div class="report-preference-grid">'+text('title','Report title','text','maxlength="100" required')+text('organization','Organization','text','maxlength="120"')+text('footer_text','Footer text','text','maxlength="160"')+text('accent_color','Accent color','color')+select('paper_size','Paper size',[['A4','A4'],['Letter','US Letter']])+select('layout','Page grouping',[['grouped','Start each group on a new page'],['continuous','Continue sections on the same page']])+select('density','Spacing',[['comfortable','Comfortable'],['compact','Compact']])+select('font_size','Text size',[[8,'8 pt'],[9,'9 pt'],[10,'10 pt'],[11,'11 pt']])+text('timezone','Report time zone','text','maxlength="64" list="report-timezone-suggestions" required')+'</div><datalist id="report-timezone-suggestions"><option value="Pacific/Auckland"><option value="UTC"><option value="Australia/Sydney"><option value="Europe/London"><option value="America/New_York"></datalist><div class="report-check-grid">'+[['repeat_header','Repeat title and serial on later pages'],['page_numbers','Page numbers'],['hide_missing','Hide unreported rows']].map(([key,label])=>'<label class="check"><input data-report-setting="'+key+'" type="checkbox"'+(value[key]?' checked':'')+'>'+label+'</label>').join('')+'</div></fieldset>';
 html+='<fieldset><legend>Page group order</legend><div id="report-section-order"></div><small class="muted">Hidden groups are skipped. Large specification groups can span several pages.</small></fieldset>';
 for(const [title,fields] of reportContentGroups)html+='<fieldset><legend>'+title+'</legend><div class="report-check-grid">'+fields.map(([key,label])=>'<label class="check"><input data-report-setting="'+key+'" type="checkbox"'+(value[key]?' checked':'')+'>'+label+'</label>').join('')+'</div></fieldset>';
 $('report-preference-fields').innerHTML=html;reportDraftOrder=[...value.section_order];renderReportOrder();reportPreferenceDirty=false;
}
function reportFormValue(){
 const value={section_order:[...reportDraftOrder]};for(const input of $('report-preference-fields').querySelectorAll('[data-report-setting]'))value[input.dataset.reportSetting]=input.type==='checkbox'?input.checked:input.dataset.reportSetting==='font_size'?Number(input.value):input.value.trim();return value;
}
function closeReportPreview(){if(reportPreviewUrl)URL.revokeObjectURL(reportPreviewUrl);reportPreviewUrl=null;$('report-preferences-frame').removeAttribute('src');$('report-preferences-frame').hidden=true;}
function clearReportPreferences(){reportPreferenceData=null;reportPreferenceUser=null;reportPreferenceDirty=false;reportExportDirty=false;$('report-preferences').open=false;closeReportPreview();$('report-preference-fields').innerHTML='';}
$('report-export-certificates').addEventListener('change',()=>{reportExportDirty=true;});
$('report-settings-toggle').addEventListener('click',()=>{$('report-preferences').open=!$('report-preferences').open;if($('report-preferences').open)$('report-preferences').scrollIntoView({block:'start',behavior:'smooth'});});
$('report-preferences').addEventListener('toggle',async()=>{
 if(!$('report-preferences').open){closeReportPreview();return;}try{const data=await loadReportPreferences(true);if(data&&!reportPreferenceDirty)renderReportPreferences(data.settings);$('report-preferences-status').textContent='Applies to all re-pxe PDF views and exports. Serial and outcome remain in the report header.';}catch(error){message(error.message,true);}
});
function changedReportPreferences(){reportPreferenceDirty=true;closeReportPreview();$('report-preferences-status').textContent='Unsaved changes. Preview or save to apply them.';}
$('report-preferences-form').addEventListener('input',changedReportPreferences);
$('report-preferences-form').addEventListener('change',changedReportPreferences);
$('report-preference-fields').addEventListener('click',event=>{const button=event.target.closest('[data-report-move]');if(!button)return;const index=Number(button.dataset.reportMove),other=index+Number(button.dataset.direction);if(other<0||other>=reportDraftOrder.length)return;[reportDraftOrder[index],reportDraftOrder[other]]=[reportDraftOrder[other],reportDraftOrder[index]];changedReportPreferences();renderReportOrder();});
$('report-preferences-close').addEventListener('click',()=>{$('report-preferences').open=false;});
$('report-preferences-reset').addEventListener('click',()=>{if(!reportPreferenceData)return;renderReportPreferences(reportPreferenceData.defaults);reportPreferenceDirty=true;$('report-preferences-status').textContent='Defaults restored in this form. Preview or save to apply them.';closeReportPreview();});
$('report-preferences-form').addEventListener('submit',async event=>{
 event.preventDefault();const button=event.submitter;button.disabled=true;try{const settings=await api('/report-settings',{method:'PUT',body:JSON.stringify(reportFormValue())});reportPreferenceData.settings=settings;reportPreferenceDirty=false;reportExportDirty=false;$('report-export-certificates').checked=settings.export_certificates;closeReportPreview();$('report-preferences-status').textContent='Report settings saved.';message('Report settings saved.');}catch(error){message(error.message,true);}finally{button.disabled=false;}
});
$('report-preferences-preview').addEventListener('click',async event=>{
 const id=selectedReports.keys().next().value||reportRows[0]?.id;if(!id){message('Search for or select a report to preview.',true);return;}
 if(!$('report-preferences-form').reportValidity())return;const button=event.currentTarget;button.disabled=true;const user=webSession?.user.id;
 try{const response=await fetch('/api/job-reports/'+encodeURIComponent(id)+'/preview',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-CSRF-Token':webSession.csrf_token},body:JSON.stringify(reportFormValue())});if(!response.ok){const value=await response.json();throw Error(typeof value.detail==='string'?value.detail:JSON.stringify(value.detail));}const blob=await response.blob();if(!webSession||webSession.user.id!==user||!$('report-preferences').open)return;closeReportPreview();reportPreviewUrl=URL.createObjectURL(blob);$('report-preferences-frame').src=reportPreviewUrl;$('report-preferences-frame').hidden=false;$('report-preferences-status').textContent='Preview of '+(selectedReports.get(id)?.system_serial||reportRows.find(row=>row.id===id)?.system_serial||id)+' using unsaved choices.';}catch(error){message(error.message,true);}finally{button.disabled=false;}
});
