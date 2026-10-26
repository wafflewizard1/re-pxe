import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import vm from 'node:vm';
import test from 'node:test';
import assert from 'node:assert/strict';
const controlsSource=readFileSync(fileURLToPath(new URL('../repxe/static/controls.js',import.meta.url)),'utf8');
const updatesSource=readFileSync(fileURLToPath(new URL('../repxe/static/updates.js',import.meta.url)),'utf8');
const navigationSource=readFileSync(fileURLToPath(new URL('../repxe/static/navigation.js',import.meta.url)),'utf8');
const routinesSource=readFileSync(fileURLToPath(new URL('../repxe/static/routines.js',import.meta.url)),'utf8');
const reportsSource=readFileSync(fileURLToPath(new URL('../repxe/static/reports.js',import.meta.url)),'utf8');
const dashboardSource=readFileSync(fileURLToPath(new URL('../repxe/static/dashboard.js',import.meta.url)),'utf8');
const clientsSource=readFileSync(fileURLToPath(new URL('../repxe/static/clients.js',import.meta.url)),'utf8');
const appearanceSource=readFileSync(fileURLToPath(new URL('../repxe/static/appearance.js',import.meta.url)),'utf8');
const interfaceSource=readFileSync(fileURLToPath(new URL('../repxe/static/interface.js',import.meta.url)),'utf8');
const workflowsSource=readFileSync(fileURLToPath(new URL('../repxe/static/workflows.js',import.meta.url)),'utf8');
const filesSource=readFileSync(fileURLToPath(new URL('../repxe/static/files.js',import.meta.url)),'utf8');
const source=readFileSync(fileURLToPath(new URL('../repxe/static/app.js',import.meta.url)),'utf8');
function fixture(saved={},darkSystem=false){
 const elements=new Map(),requests=[],persistent=new Map(Object.entries(saved)),sessionValues=new Map(),systemEvents={},windowEvents={},documentEvents={},confirmations=[];
 const location={hash:''},navigationEntries=[];let navigationIndex=-1;
 const browserHistory={state:null,replaceState(state,title,url){this.state=state;location.hash=url;if(navigationIndex<0)navigationIndex=0;navigationEntries[navigationIndex]={state,url};},pushState(state,title,url){this.state=state;location.hash=url;navigationEntries.splice(++navigationIndex);navigationEntries.push({state,url});},async back(){if(navigationIndex<=0)return;const entry=navigationEntries[--navigationIndex];this.state=entry.state;location.hash=entry.url;await windowEvents.popstate?.({state:entry.state});},async forward(){if(navigationIndex>=navigationEntries.length-1)return;const entry=navigationEntries[++navigationIndex];this.state=entry.state;location.hash=entry.url;await windowEvents.popstate?.({state:entry.state});}};
 const storage=values=>({getItem:key=>values.get(key)||null,setItem:(key,value)=>values.set(key,value)});
 const media={matches:darkSystem,addEventListener:(name,fn)=>{systemEvents[name]=fn;}};
 const element=id=>{if(!elements.has(id))elements.set(id,{id,value:'',innerHTML:'',textContent:'',hidden:false,disabled:false,listeners:{},dataset:{},attributes:{},style:{setProperty(){}},setAttribute(key,value){this.attributes[key]=value;},classList:{toggle(){},add(){},remove(){}},addEventListener(type,fn){this.listeners[type]=fn;},click(){this.listeners.click?.({target:this});},blur(){document.activeElement=null;},focus(){document.activeElement=this;},close(){if(this.open){this.open=false;this.listeners.close?.();}},removeAttribute(key){delete this.attributes[key];},showModal(){this.open=true;},getContext(){return new Proxy({},{get:()=>()=>{}});},clientWidth:600,clientHeight:480,scrollLeft:0,scrollTop:0,getBoundingClientRect(){return {left:0,top:0,width:600,height:480};},querySelector(){return {style:{}};},querySelectorAll(){return [];},appendChild(){}});return elements.get(id);};
 const document={documentElement:{dataset:{}},getElementById:element,querySelectorAll:()=>[],activeElement:null,hidden:false,addEventListener(name,fn){documentEvents[name]=fn;}};
 const configurations=[{id:'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',name:'Windows Pro'},{id:'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb',name:'Windows Alternate'}];
 const settings={default_profile_id:configurations[0].id,automatic_disk_selection:true,start_delay_seconds:10,offline_hide_seconds:300};
 const client={id:'client-1',mac:'52:54:00:00:00:01',model:'VM',target:'',windows:'Windows Pro',configuration_id:configurations[0].id,configuration_override:null,selection_locked:false,stage:'booting',online:true,last_seen:'2026-10-06T06:00:00Z',received_bytes:0,total_bytes:0,bytes_per_second:0,job_id:null,error:''};
 const dashboard={settings,configurations,clients:[client],online_clients:1,active_jobs:0,completed_today:0,completed:0,failed:0,network:null,recent_jobs:[],sampled_at:'2026-10-06T06:00:00Z',pxe:null};
 const context=vm.createContext({document,window:{addEventListener(name,fn){windowEvents[name]=fn;},history:browserHistory,location,confirm(value){confirmations.push(value);return true;},devicePixelRatio:1,matchMedia:()=>media},localStorage:storage(persistent),sessionStorage:storage(sessionValues),setInterval(){},setTimeout(){},clearTimeout(){},console,Blob,URLSearchParams,
 fetch:async(path,options)=>{requests.push({path,options});if(path==='/api/pxe-settings/default')settings.default_profile_id=JSON.parse(options.body).profile_id;if(path==='/api/pxe-settings')Object.assign(settings,JSON.parse(options.body));if(path.includes('/configuration')){client.configuration_override=JSON.parse(options.body).profile_id;client.configuration_id=client.configuration_override||settings.default_profile_id;}return {ok:true,status:200,json:async()=>path.startsWith('/api/dashboard')?dashboard:path==='/api/overview'?{counts:{image:1,drivers:0,pending:0},storage:{free:100}}:settings};}});
 vm.runInContext(appearanceSource,context);vm.runInContext(source,context);vm.runInContext(clientsSource,context);vm.runInContext(reportsSource,context);vm.runInContext(dashboardSource,context);vm.runInContext(filesSource,context);vm.runInContext(interfaceSource,context);vm.runInContext(workflowsSource,context);vm.runInContext(routinesSource,context);vm.runInContext(controlsSource,context);vm.runInContext(navigationSource,context);vm.runInContext(updatesSource,context);vm.runInContext("webSession={csrf_token:'fixture-csrf',user:{id:'admin-id',username:'admin',default_password:true}};",context);
 const call=code=>vm.runInContext(code,context);
 context.fixtureDashboard=dashboard;
 return {element,document,dashboard,client,settings,configurations,requests,call,context,persistent,sessionValues,media,systemEvents,windowEvents,browserHistory,navigationEntries,confirmations,documentEvents};
}

test('client choices default while booting and lock to the claimed choice',()=>{
 const f=fixture();f.call('renderClients(fixtureDashboard)');let html=f.element('client-table').innerHTML;
 assert.match(html,/data-client-config="client-1"/);assert.match(html,/<option value="" selected>Default · Windows Pro/);assert.doesNotMatch(html,/aria-label="Configuration for Serial unavailable · VM" disabled/);
 f.client.selection_locked=true;f.client.configuration_id=f.configurations[1].id;f.call('renderClients(fixtureDashboard)');html=f.element('client-table').innerHTML;
 assert.match(html,/aria-label="Configuration for Serial unavailable · VM" disabled/);assert.ok(html.includes('value="'+f.configurations[1].id+'" selected'));
});

test('active choices survive polling but a server lock replaces them',()=>{
 const f=fixture();const select=f.element('active-select');select.dataset.clientConfig=f.client.id;select.closest=()=>select;f.document.activeElement=select;f.element('client-table').innerHTML='open dropdown';
 f.call('renderClients(fixtureDashboard)');assert.equal(f.element('client-table').innerHTML,'open dropdown');
 f.client.selection_locked=true;f.call('renderClients(fixtureDashboard)');assert.notEqual(f.element('client-table').innerHTML,'open dropdown');
});

test('per-client change sends an override and releases the disabled control',async()=>{
 const f=fixture();f.call("tab='jobs'");const select=f.element('row-select');select.dataset.clientConfig=f.client.id;select.value=f.configurations[1].id;select.closest=()=>select;f.document.activeElement=select;
 await f.element('client-table').listeners.change({target:select});
 const request=f.requests.find(r=>r.path==='/api/clients/client-1/configuration');assert.equal(request.options.method,'PUT');assert.deepEqual(JSON.parse(request.options.body),{profile_id:f.configurations[1].id});assert.equal(select.disabled,false);assert.equal(f.document.activeElement,null);
 assert.ok(f.element('client-table').innerHTML.includes('value="'+f.configurations[1].id+'" selected'));
});

test('default control changes only the default profile',async()=>{
 const f=fixture();const select=f.element('dashboard-default');select.value=f.configurations[1].id;
 await select.listeners.change({target:select});const request=f.requests.find(r=>r.path==='/api/pxe-settings/default');assert.deepEqual(JSON.parse(request.options.body),{profile_id:f.configurations[1].id});assert.equal(f.settings.start_delay_seconds,10);assert.equal(select.disabled,false);
});

test('settings polling preserves edits and saving converts minutes to seconds',async()=>{
 const f=fixture();f.call('renderSettings(fixtureDashboard)');assert.equal(f.element('settings-delay').value,10);assert.equal(f.element('settings-offline').value,5);
 f.element('settings-form').listeners.input();f.element('settings-delay').value='20';f.call('renderSettings(fixtureDashboard)');assert.equal(f.element('settings-delay').value,'20');
 f.element('settings-default').value=f.configurations[0].id;f.element('settings-disk').value='automatic';f.element('settings-offline').value='7';
 await f.element('settings-form').listeners.submit({preventDefault(){},submitter:{disabled:false}});
 const request=f.requests.find(r=>r.path==='/api/pxe-settings');assert.equal(JSON.parse(request.options.body).offline_hide_seconds,420);assert.equal(JSON.parse(request.options.body).start_delay_seconds,20);
});

test('unavailable configurations remain visible and labels are escaped',()=>{
 const f=fixture();f.client.selection_locked=true;f.client.configuration_id='gone';f.client.configuration_name='<img src=x onerror=bad>';f.call('renderClients(fixtureDashboard)');const html=f.element('client-table').innerHTML;assert.ok(html.includes('value="gone" selected'));assert.ok(html.includes('&lt;img'));assert.ok(!html.includes('<img'));
});

test('automatic driver source hides explicit model options and packages',()=>{
 const f=fixture();f.element('profile-driver-source').value='automatic';f.call('driverSourceForm()');
 assert.equal(f.element('profile-driver-packages').hidden,true);assert.equal(f.element('profile-drivers').disabled,true);assert.equal(f.element('profile-hardware-option').hidden,true);
 f.element('profile-driver-source').value='explicit';f.call('driverSourceForm()');
 assert.equal(f.element('profile-driver-packages').hidden,false);assert.equal(f.element('profile-drivers').disabled,false);assert.equal(f.element('profile-hardware-option').hidden,false);
});

test('Needs attention explains remaining devices and escapes update errors',()=>{
 const f=fixture();f.element('details').showModal=()=>{};
 f.call(`showJob({id:'job-1',stage:'needs-operator',events:[],report:{native:{error:'<script>bad</script>',drivers:{problems:[{instanceId:'ACPI\\\\QEMU0002',problemCode:28}]}}}})`);
 const html=f.element('job-summary').innerHTML;assert.match(html,/&lt;script&gt;/);assert.doesNotMatch(html,/<script>/);assert.match(html,/QEMU0002/);assert.match(html,/28/);
});


test('website requests use cookie sessions and CSRF instead of bearer tokens',async()=>{
 const f=fixture();await f.call("api('/pxe-settings/default',{method:'PUT',body:JSON.stringify({profile_id:null})})");
 const request=f.requests.at(-1);assert.equal(request.options.credentials,'same-origin');assert.equal(request.options.headers['X-CSRF-Token'],'fixture-csrf');assert.equal(request.options.headers.Authorization,undefined);
});

test('password mismatch stays local and preserves the session',async()=>{
 const f=fixture();f.element('new-password').value='first-password';f.element('confirm-password').value='second-password';
 await f.element('password-form').listeners.submit({preventDefault(){},submitter:{disabled:false}});
 assert.equal(f.requests.length,0);assert.equal(f.element('message').textContent,'Passwords do not match.');assert.equal(f.call('webSession.user.id'),'admin-id');
});

test('changing the current password signs out and clears password fields',async()=>{
 const f=fixture();f.element('password-user').value='admin-id';f.element('new-password').value='new-fixture-password';f.element('confirm-password').value='new-fixture-password';const button={disabled:false};
 await f.element('password-form').listeners.submit({preventDefault(){},submitter:button});
 const request=f.requests.find(r=>r.path==='/api/users/admin-id/password');assert.equal(request.options.method,'PUT');assert.equal(JSON.parse(request.options.body).password,'new-fixture-password');assert.equal(f.call('webSession'),null);assert.equal(f.element('workspace').hidden,true);assert.equal(f.element('login').hidden,false);assert.equal(f.element('new-password').value,'');assert.equal(f.element('confirm-password').value,'');assert.equal(button.disabled,false);
});

test('expired website sessions return to login',async()=>{
 const f=fixture();f.context.fetch=async()=>({ok:false,status:401,json:async()=>({detail:'Sign in required'})});
 await assert.rejects(f.call("api('/users')"),/Sign in required/);assert.equal(f.call('webSession'),null);assert.equal(f.element('login').hidden,false);
});


test('driver catalog presents individual drivers and model tags',async()=>{
 const f=fixture();f.context.fixtureDrivers=[{id:'driver-id',name:'network.inf',content_hash:'a'.repeat(64),provider:'<vendor>',class:'Net',version:'1.0',size:123,tags:[{manufacturer:'HP',model:'EliteBook',windows_build:'26300'}]}];
 f.context.fetch=async path=>({ok:true,status:200,json:async()=>path==='/api/driver-library'?f.context.fixtureDrivers:{counts:{image:1,drivers:1,pending:0},storage:{free:100}}});
 await f.call("tab='drivers';refresh()");const html=f.element('table').innerHTML;assert.match(html,/network.inf/);assert.match(html,/HP \/ EliteBook/);assert.match(html,/&lt;vendor&gt;/);assert.doesNotMatch(html,/Driver set/);assert.match(f.element('tag-driver').innerHTML,/driver-id/);
});

test('adding a model tag reuses the selected driver without a file upload',async()=>{
 const f=fixture();f.element('tag-driver').value='driver-id';f.element('tag-manufacturer').value='HP';f.element('tag-model').value='EliteBook';f.element('tag-build').value='26300';f.element('tag-tested').checked=true;f.element('tag-note').value='Checked devices on the model';
 await f.element('driver-tag-form').listeners.submit({preventDefault(){},submitter:{disabled:false}});
 const request=f.requests.find(r=>r.path==='/api/driver-library/driver-id/tags');assert.equal(request.options.method,'POST');assert.deepEqual(JSON.parse(request.options.body),{manufacturer:'HP',model:'EliteBook',windows_build:'26300',hardware_test_passed:true,validation_note:'Checked devices on the model'});assert.equal(request.options.headers['X-CSRF-Token'],'fixture-csrf');assert.equal(f.element('tag-tested').checked,false);
});

function driverFixture(){
 const f=fixture();f.context.fixtureDrivers=[
 {id:'alpha',name:'NIC.inf',provider:'Acme',class:'Net',version:'1.0',content_hash:'a'.repeat(64),size:1048576,created:'2026-01-01T00:00:00Z',last_used:null,use_count:0,confirmed_count:0,group:'Retest',tags:[{manufacturer:'HP',model:'EliteBook',windows_build:'26300'}]},
 {id:'beta',name:'Storage.inf',provider:'Other',class:'SCSIAdapter',version:'2.0',content_hash:'b'.repeat(64),size:3145728,created:'2026-03-01T00:00:00Z',last_used:'2026-01-01T00:00:00Z',use_count:5,confirmed_count:3,group:'Keep',tags:[{manufacturer:'Dell',model:'Latitude',windows_build:'26100'}]},
 {id:'gamma',name:'Video.inf',provider:'Acme',class:'Display',version:'3.0',content_hash:'c'.repeat(64),size:2097152,created:'2026-04-01T00:00:00Z',last_used:'2026-10-06T00:00:00Z',use_count:2,confirmed_count:1,group:'Retest',tags:[{manufacturer:'HP',model:'EliteBook',windows_build:'26300'}]}
 ];f.call('renderDriverLibrary(fixtureDrivers)');return f;
}
test('driver search, tags, usage, date and size filters combine',()=>{
 const f=driverFixture();f.element('driver-search').value='EliteBook';f.element('driver-min').value='1.5';assert.deepEqual(Array.from(f.call('filteredDrivers().map(r=>r.id)')),['gamma']);
 f.element('driver-search').value='';f.element('driver-min').value='';f.element('driver-usage').value='never';assert.deepEqual(Array.from(f.call('filteredDrivers().map(r=>r.id)')),['alpha']);
 f.element('driver-usage').value='stale';assert.deepEqual(Array.from(f.call("filteredDrivers(Date.parse('2026-10-07')).map(r=>r.id)")),['beta']);
 f.element('driver-usage').value='';f.element('driver-before').value='2026-02-01';assert.deepEqual(Array.from(f.call('filteredDrivers().map(r=>r.id)')),['beta']);
 f.element('driver-before').value='';f.element('driver-build').value='26300';assert.equal(f.call('filteredDrivers().length'),2);
});
test('driver sorting, grouping and hidden selections remain stable on polling',()=>{
 const f=driverFixture();f.element('driver-sort').value='size';f.element('driver-order').value='desc';assert.deepEqual(Array.from(f.call('filteredDrivers().map(r=>r.id)')),['beta','gamma','alpha']);
 f.element('driver-group-by').value='group';f.call('renderDrivers()');assert.match(f.element('table').innerHTML,/data-select-group="Retest"/);
 f.call('selectDriverRows([fixtureDrivers[0]])');f.element('driver-search').value='Storage';f.call('renderDrivers()');assert.match(f.element('driver-selection').textContent,/includes hidden rows/);
 f.call('renderDriverLibrary(fixtureDrivers)');assert.equal(f.call('selectedDrivers.has("alpha")'),true);
 f.call('renderDriverLibrary(fixtureDrivers.slice(1))');assert.equal(f.call('selectedDrivers.size'),0);
});
test('deletion preview blocks confirmation without making a delete request',async()=>{
 const f=driverFixture();f.call('selectedDrivers.add("alpha")');f.element('driver-delete-dialog').showModal=()=>{};
 f.context.fetch=async(path,options)=>{f.requests.push({path,options});return {ok:true,status:200,json:async()=>({count:1,logical_bytes:100,blocked:1,entries:[{id:'alpha',name:'<bad>',size:100,blockers:['Enabled configuration: Workshop'],retained_references:[]}]})};};
 await f.element('driver-delete').listeners.click();assert.equal(f.element('driver-delete-confirm').disabled,true);assert.match(f.element('driver-delete-entries').innerHTML,/&lt;bad&gt;/);assert.ok(f.requests.every(r=>r.path!=='/api/driver-library/delete'));
});
test('guide search renders setup material and escapes dynamic server values',()=>{
 const f=fixture();f.call("guideEndpoints={tftp_server:'<bad>',boot_filename:'pxe/0.12.0/snponly.efi',website:'https://fixture',boot_http:'http://fixture'};activeGuide='network';renderGuides();");assert.match(f.element('guide-article').innerHTML,/&lt;bad&gt;/);assert.match(f.element('guide-article').innerHTML,/30000–30031/);
 f.element('guide-search').value='WIM';f.call('activeGuide=null;renderGuides()');assert.match(f.element('guide-list').innerHTML,/Windows WIM/);
});

test('a dependency introduced after preview stays visible inside the delete dialog',async()=>{
 const f=driverFixture();f.call("deleteIds=['alpha']");f.context.fetch=async()=>({ok:false,status:409,json:async()=>({detail:{message:'Active job now pins this driver',blocked:1}})});
 await f.element('driver-delete-confirm').listeners.click();assert.equal(f.element('driver-delete-error').hidden,false);assert.match(f.element('driver-delete-error').textContent,/Active job now pins/);assert.equal(f.call('driverRows.length'),3);
});

function editorFixture(){const f=fixture();f.context.fixtureFile={root:'custom-boot',path:'scripts/menu.ipxe',content:'#!ipxe\necho Original\n',revision:'a'.repeat(64),modified:'2026-10-07T00:00:00Z',editable:true,public:true};f.call('setEditorFile(fixtureFile)');return f;}
test('answer-file editor shows guidance and keeps rejected XML drafts',async()=>{
 const f=editorFixture();f.call("setEditorFile({...fixtureFile,root:'answer-files',path:'audit.xml',content:'<unattend/>'})");assert.equal(f.element('editor-answer-note').hidden,false);
 f.element('editor-content').value='<broken>';f.element('editor-content').listeners.input();f.context.fetch=async()=>({ok:false,status:422,json:async()=>({detail:'Keep the re-pxe Audit startup command'})});
 assert.equal(await f.call('saveEditor()'),false);assert.equal(f.element('editor-content').value,'<broken>');assert.equal(f.call('editorDirty'),true);assert.match(f.element('editor-error').textContent,/Audit startup/);
 f.call('clearFileSession()');assert.equal(f.element('editor-answer-note').hidden,true);
});
test('answer-file shortcuts open the active templates in the normal editor',async()=>{
 const f=fixture();f.context.fetch=async(path,options)=>{f.requests.push({path,options});return {ok:true,status:200,json:async()=>({root:'answer-files',path:path.includes('audit.xml')?'audit.xml':'oobe.xml',content:'<unattend/>',revision:'a'.repeat(64),editable:true})};};
 f.element('open-audit-answer').listeners.click();await new Promise(resolve=>setImmediate(resolve));assert.equal(f.call('editorFile.path'),'audit.xml');assert.match(f.requests[0].path,/root=answer-files&path=audit.xml/);
 f.element('open-oobe-answer').listeners.click();await new Promise(resolve=>setImmediate(resolve));assert.equal(f.call('editorFile.path'),'oobe.xml');assert.equal(f.call('tab'),'editor');assert.equal(f.element('editor-answer-note').hidden,false);
});
test('editor keeps dirty contents across navigation and sends the opened revision when saving',async()=>{
 const f=editorFixture();f.element('editor-content').value='#!ipxe\necho Edited\n';f.element('editor-content').listeners.input();f.call("switchTab('files');switchTab('editor')");assert.equal(f.element('editor-content').value,'#!ipxe\necho Edited\n');
 f.context.fetch=async(path,options)=>{f.requests.push({path,options});return {ok:true,status:200,json:async()=>({...f.context.fixtureFile,content:'#!ipxe\necho Edited\n',revision:'b'.repeat(64)})};};
 assert.equal(await f.call('saveEditor()'),true);const request=f.requests.find(r=>r.path==='/api/files/text');assert.equal(JSON.parse(request.options.body).expected_revision,'a'.repeat(64));assert.equal(f.call('editorDirty'),false);
});
test('editor conflict preserves text and displays a local error',async()=>{
 const f=editorFixture();f.element('editor-content').value='My unsaved changes';f.element('editor-content').listeners.input();f.context.fetch=async()=>({ok:false,status:409,json:async()=>({detail:'File changed since it was opened'})});
 assert.equal(await f.call('saveEditor()'),false);assert.equal(f.element('editor-content').value,'My unsaved changes');assert.equal(f.call('editorDirty'),true);assert.match(f.element('editor-error').textContent,/File changed/);
});
test('opening another file asks about unsaved edits and read-only files cannot be saved',async()=>{
 const f=editorFixture();f.element('editor-content').value='Draft';f.element('editor-content').listeners.input();let opened=false;f.element('editor-unsaved-dialog').showModal=()=>{opened=true;};f.context.runNext=()=>{f.context.didRunNext=true;};f.call('ensureEditorChange(runNext)');assert.equal(opened,true);assert.equal(f.context.didRunNext,undefined);
 f.element('editor-keep').listeners.click();assert.equal(f.call('editorDirty'),true);
 f.call('setEditorFile({...fixtureFile,editable:false})');f.element('editor-content').value='attempt';assert.equal(await f.call('saveEditor()'),false);assert.equal(f.element('editor-content').readOnly,true);
});
test('file explorer uses real paths, labels UUID folders secondarily and escapes names',()=>{
 const f=fixture();f.context.fixtureRoots=[{id:'configuration',name:'Configuration',data_path:'config',writable:true},{id:'boot-releases',name:'Boot releases',data_path:'boot/pxe',writable:false}];f.context.fixtureEntries=[{name:'<unsafe>',path:'config/unsafe.txt',root:'configuration',relative_path:'unsafe.txt',directory:false,size:123,modified:'2026-10-07',text:true,editable:true},{name:'uuid-folder',display_name:'Workshop scripts',path:'config/uuid-folder',directory:true,modified:'2026-10-07'}];f.call("fileRoots=fixtureRoots;fileEntries=fixtureEntries;fileLocation={root:'workspace',path:'config'};fileFolder={root:'configuration',relative_path:'',writable:true};renderFileTree();renderFileList()");assert.match(f.element('file-list').innerHTML,/&lt;unsafe&gt;/);assert.match(f.element('file-list').innerHTML,/Workshop scripts/);assert.match(f.element('file-tree').innerHTML,/data-file-folder="config"/);assert.match(f.element('file-breadcrumb').innerHTML,/\/data/);assert.equal(f.element('file-new-text').disabled,false);
 f.call("fileLocation={root:'workspace',path:'boot/pxe'};fileFolder={writable:false};renderFileTree()");assert.equal(f.element('file-new-text').disabled,false);
});

test('a slow file-open response cannot replace a newer editor draft',async()=>{
 const f=editorFixture();const waiting=[];f.context.fetch=async()=>new Promise(resolve=>waiting.push(resolve));
 const first=f.call("openFile('configuration','first.txt')"),second=f.call("openFile('configuration','second.txt')");
 waiting[1]({ok:true,status:200,json:async()=>({...f.context.fixtureFile,root:'configuration',path:'second.txt',content:'Second file'})});await second;
 f.element('editor-content').value='My new draft';f.element('editor-content').listeners.input();
 waiting[0]({ok:true,status:200,json:async()=>({...f.context.fixtureFile,root:'configuration',path:'first.txt',content:'Old request'})});await first;
 assert.equal(f.element('editor-content').value,'My new draft');assert.equal(f.call('editorFile.path'),'second.txt');assert.equal(f.call('editorDirty'),true);
});


test('sidebar collapses to an accessible icon menu and remembers its state',()=>{
 const f=fixture();f.element('sidebar-toggle').listeners.click();assert.equal(f.document.documentElement.dataset.sidebar,'collapsed');assert.equal(f.element('sidebar-toggle').attributes['aria-expanded'],'false');assert.equal(f.element('sidebar-toggle').attributes['aria-label'],'Expand sidebar');
 const reload=fixture(Object.fromEntries(f.persistent));assert.equal(reload.document.documentElement.dataset.sidebar,'collapsed');reload.element('sidebar-toggle').listeners.click();assert.equal(reload.document.documentElement.dataset.sidebar,'expanded');
});
test('appearance follows system changes and explicit dark/theme choices survive reload',()=>{
 const f=fixture({},true);assert.equal(f.document.documentElement.dataset.mode,'dark');f.media.matches=false;f.systemEvents.change();assert.equal(f.document.documentElement.dataset.mode,'light');
 f.element('appearance-mode').value='dark';f.element('appearance-theme').value='blue';f.element('appearance-form').listeners.submit({preventDefault(){}});assert.equal(f.document.documentElement.dataset.mode,'dark');assert.equal(f.document.documentElement.dataset.theme,'blue');
 const reload=fixture(Object.fromEntries(f.persistent));assert.equal(reload.document.documentElement.dataset.mode,'dark');assert.equal(reload.document.documentElement.dataset.theme,'blue');reload.element('theme-toggle').listeners.click();assert.equal(reload.document.documentElement.dataset.mode,'light');assert.equal(reload.document.documentElement.dataset.theme,'blue');
});
test('invalid browser preferences fall back without preventing page initialization',()=>{
 const f=fixture({'repxe.appearance.v1':'null'});assert.equal(f.document.documentElement.dataset.mode,'light');assert.equal(f.document.documentElement.dataset.theme,'blue');
 const bad=fixture({'repxe.appearance.v1':'{broken'});assert.equal(bad.document.documentElement.dataset.sidebar,'expanded');
});
test('dismissing the default-password notice does not change account state and resets on sign out',()=>{
 const f=fixture();f.call('updatePasswordNotice()');assert.equal(f.element('default-password-notice').hidden,false);f.element('dismiss-password-notice').listeners.click();assert.equal(f.element('default-password-notice').hidden,true);assert.equal(f.call('webSession.user.default_password'),true);
 f.call('updatePasswordNotice()');assert.equal(f.element('default-password-notice').hidden,true);f.call('clearInterfaceSession();updatePasswordNotice()');assert.equal(f.element('default-password-notice').hidden,false);
});
function wizardFixture(){const f=fixture();f.context.wizardFixtureData={server:{boot:{version:'0.13.3',validated:true},storage:{free:100},endpoints:{tftp_server:'server',boot_filename:'pxe/test.efi',website:'https://server',boot_http:'http://server'}},dashboard:f.dashboard,images:[],drivers:[],configs:[],queue:[{status:'ready',spec:{kind:'image'}}]};f.call('loadWizardProgress();wizardData=wizardFixtureData');return f;}
test('hash input is optional for WIM and required for driver ZIP uploads',()=>{
 const f=fixture();f.call('$("kind").value="image";updateUploadKind()');assert.equal(f.element('hash').required,false);assert.match(f.element('hash-help').textContent,/server calculates/);
 f.call('$("kind").value="drivers";updateUploadKind()');assert.equal(f.element('hash').required,true);assert.match(f.element('hash-label').textContent,/required/);
});
for(const hash of ['', 'a'.repeat(64)])test('WIM upload '+(hash?'resumes only the matching expected hash':'without a hash starts a fresh upload'),async()=>{
 const f=fixture();f.context.expectedHash=hash;f.call('refresh=async()=>{};$("kind").value="image";$("hash").value=expectedHash;$("image-index").value="1";uploads=[{id:"old-upload",status:"receiving",received:4,spec:{kind:"image",filename:"install.wim",size:8,sha256:expectedHash||null}}]');
 f.element('file').files=[{name:'install.wim',size:8,slice:(start,end)=>new Blob(['abcdefgh'.slice(start,end)])}];
 f.context.fetch=async(path,options)=>{f.requests.push({path,options});return {ok:true,status:200,json:async()=>path==='/api/uploads'?{id:'new-upload',received:0}:path==='/api/uploads/old-upload'?{id:'old-upload',received:4}:path.endsWith('/content')?{received:8}:{status:'ready'}};};
 await f.element('upload-form').listeners.submit({preventDefault(){}});
 const create=f.requests.find(r=>r.path==='/api/uploads'),content=f.requests.find(r=>r.path.endsWith('/content'));
 if(hash){assert.equal(create,undefined);assert.equal(content.path,'/api/uploads/old-upload/content');assert.equal(content.options.headers['Upload-Offset'],'4');}
 else{assert.equal(JSON.parse(create.options.body).sha256,null);assert.equal(content.path,'/api/uploads/new-upload/content');assert.equal(content.options.headers['Upload-Offset'],'0');assert.ok(!f.requests.some(r=>r.path==='/api/uploads/old-upload'));}
 assert.equal(f.element('progress').textContent,'Windows image ready for review.');
});
test('portal update requires confirmation and pins the displayed revision',async()=>{
 const f=fixture();f.call('renderPortalUpdates({enabled:true,busy:false,can_update:true,current_revision:"a".repeat(40),target_revision:"b".repeat(40),summary:"<release>"})');
 assert.equal(f.element('update-apply').disabled,false);assert.equal(f.element('update-summary').textContent,'<release>');
 f.context.window.confirm=()=>false;await f.call('runPortalUpdate("apply")');assert.equal(f.requests.length,0);
 f.context.window.confirm=()=>true;await f.call('runPortalUpdate("apply")');const request=f.requests.find(r=>r.path==='/api/system-updates/apply');assert.equal(request.options.headers['X-CSRF-Token'],'fixture-csrf');assert.deepEqual(JSON.parse(request.options.body),{expected_revision:'b'.repeat(40),confirm:true});
});
test('portal update controls handle restart disconnection and recovery',async()=>{
 const f=fixture();f.call('tab="settings";settingsSection="server";renderPortalUpdates({enabled:true,busy:true,phase:"restarting",detail:"Restarting services"})');assert.equal(f.element('update-apply').disabled,true);assert.equal(f.element('update-check').disabled,true);
 f.context.fetch=async()=>{throw Error('network disconnected')};await f.call('refreshPortalUpdates()');assert.match(f.element('update-status').textContent,/Waiting for the portal to reconnect/);
 f.context.fetch=async()=>({ok:true,status:200,json:async()=>({enabled:true,busy:false,phase:'failed',recovery_required:true,detail:'Recover previous version'})});await f.call('refreshPortalUpdates()');assert.equal(f.element('update-recover').hidden,false);assert.equal(f.element('update-recover').disabled,false);assert.equal(f.element('update-check').disabled,true);
 f.call('signedOut()');assert.equal(f.call('portalUpdateState'),null);
});
test('Windows wizard accepts a published WIM and enabled preset without validated PXE media',()=>{
 const f=wizardFixture();f.call('wizardFixtureData.images=[{name:"Windows USB install.wim",image_index:1,selected_image:{build:"26300"}}];wizardFixtureData.configs=[{enabled:true}];wizardFixtureData.server.boot=null;setWizardStep(3)');
 assert.equal(f.call('wizardReady()[3]'),true);assert.equal(f.call('wizardReady()[5]'),true);assert.equal(f.element('wizard-next').disabled,false);
 assert.match(f.element('wizard-body').innerHTML,/Uploaded WIM via wimboot/);assert.match(f.element('wizard-body').innerHTML,/image controls its startup/);assert.match(f.element('wizard-body').innerHTML,/data-guide="windows-media"/);
 f.call('wizardFixtureData.server.boot={version:"0.13.3",validated:false};renderWizard()');assert.equal(f.call('wizardReady()[3]'),true);assert.match(f.element('wizard-body').innerHTML,/Selected PXE bundle/);
 f.call('wizardFixtureData.server.boot.validated=true;renderWizard()');assert.equal(f.call('wizardReady()[3]'),true);assert.equal(f.call('wizardReady()[5]'),true);assert.equal(f.element('wizard-next').disabled,false);
 f.call('wizardFixtureData.server.boot.validated=false;setWizardStep(5)');assert.doesNotMatch(f.element('wizard-body').innerHTML,/Return to Components/);assert.equal(f.element('wizard-next').disabled,false);
});
test('erasure-only wizard does not require Windows images or WinPE',()=>{
 const f=wizardFixture();f.call('wizardFixtureData.server.boot=null;wizardFixtureData.dashboard.routines={enabled:true,default_id:"wipe",choices:[{id:"wipe",has_windows:false}]};wizardFixtureData.routines={routines:[{id:"wipe",draft:{steps:[{kind:"erase"}]}}]};wizardFixtureData.capabilities=[{status:"installed"}];setWizardStep(3)');
 assert.equal(f.call('wizardReady()[3]'),true);assert.equal(f.call('wizardReady()[5]'),true);assert.doesNotMatch(f.element('wizard-body').innerHTML,/data-guide="windows-media"/);
});
test('Windows media guidance is reachable from the routine editor and explains publication',()=>{
 const f=fixture();f.call('routineDraft={steps:[{kind:"windows"}]};routineEditorStatus()');assert.equal(f.element('routine-windows-media').hidden,false);
 f.call('activeGuide="windows-media";renderGuides()');assert.match(f.element('guide-article').innerHTML,/uploaded WIM/);assert.match(f.element('guide-article').innerHTML,/Build-WinPE.ps1/);assert.match(f.element('guide-article').innerHTML,/publish_pxe.py --validated/);
 f.call('routineDraft={steps:[{kind:"erase"}]};routineEditorStatus()');assert.equal(f.element('routine-windows-media').hidden,true);
});
test('wizard checks published content and policy instead of treating validated uploads as complete',()=>{
 const f=wizardFixture();assert.equal(f.call('wizardReady()[3]'),false);assert.equal(f.call('wizardReady()[5]'),false);f.call('wizardFixtureData.images=[{name:"Published image",image_index:1,selected_image:{build:"26300"}}];wizardFixtureData.configs=[{id:"disabled",name:"Disabled",enabled:false}]');assert.equal(f.call('wizardReady()[3]'),true);assert.equal(f.call('wizardReady()[5]'),false);
 f.call('wizardFixtureData.configs[0].enabled=true;wizardFixtureData.dashboard.settings.default_profile_id="missing"');assert.equal(f.call('wizardReady()[5]'),false);
 f.call('setWizardStep(3);renderWizard()');assert.match(f.element('wizard-body').innerHTML,/Published image/);assert.equal(f.element('wizard-next').disabled,false);
});
test('wizard requires manual network/validation confirmations, persists them per user, and escapes names',()=>{
 const f=wizardFixture();f.call('setWizardStep(1)');assert.equal(f.element('wizard-next').disabled,true);f.element('wizard-body').listeners.change({target:{dataset:{wizardConfirm:'network'},checked:true}});assert.equal(f.call('wizardReady()[1]'),true);
 f.call('setWizardStep(6)');assert.equal(f.element('wizard-next').disabled,true);f.element('wizard-body').listeners.change({target:{dataset:{wizardConfirm:'validated'},checked:true}});assert.equal(f.call('wizardReady()[6]'),true);
 f.call('loadWizardProgress()');assert.equal(f.call('wizardProgress.step'),6);assert.equal(f.call('wizardProgress.network'),true);assert.equal(f.call('wizardProgress.validated'),true);
 f.call('wizardData=wizardFixtureData;wizardFixtureData.configs=[{name:"<script>bad</script>",enabled:true}];setWizardStep(5)');assert.match(f.element('wizard-body').innerHTML,/&lt;script&gt;/);assert.doesNotMatch(f.element('wizard-body').innerHTML,/<script>/);
 f.call('webSession.user.id="another-user";loadWizardProgress()');assert.equal(f.call('wizardProgress.network'),false);
});
test('wizard tools use the normal forms and keep a return path',()=>{
 const f=wizardFixture();f.call("setWizardStep(4);openWizardTool('upload-drivers')");assert.equal(f.call('tab'),'uploads');assert.equal(f.element('kind').value,'drivers');assert.equal(f.element('file').accept,'.zip');assert.equal(f.element('wizard-return').hidden,false);assert.match(f.element('wizard-return-text').textContent,/Step 5 of 8/);
 f.call("openWizardTool('pxe')");assert.equal(f.call('tab'),'settings');assert.equal(f.call('settingsSection'),'pxe');assert.equal(f.element('wizard-return').hidden,false);
});

test('the password reminder can be closed even when browser storage is blocked',()=>{
 const f=fixture();f.context.sessionStorage.setItem=()=>{throw Error('Blocked');};f.context.sessionStorage.getItem=()=>{throw Error('Blocked');};f.call('updatePasswordNotice()');f.element('dismiss-password-notice').listeners.click();assert.equal(f.element('default-password-notice').hidden,true);
});
test('wizard server errors prevent stale data from allowing completion',async()=>{
 const f=wizardFixture();f.call('tab="wizard";setWizardStep(4)');assert.equal(f.element('wizard-next').disabled,false);f.context.fetch=async()=>({ok:false,status:503,json:async()=>({detail:'Server unavailable'})});await f.call('refresh()');assert.equal(f.element('wizard-next').disabled,true);assert.match(f.element('wizard-status').textContent,/Server unavailable/);
});

test('finishing or restarting the wizard changes only the browser checklist',()=>{
 const f=wizardFixture();f.call('wizardFixtureData.images=[{name:"Image",image_index:1,selected_image:{build:"26300"}}];wizardFixtureData.configs=[{id:"configuration",name:"Configuration",enabled:true}];wizardProgress.network=true;wizardProgress.validated=true;setWizardStep(7)');assert.equal(f.element('wizard-next').disabled,false);
 f.element('wizard-next').listeners.click();assert.equal(f.call('wizardProgress.completed'),true);assert.match(f.element('wizard-body').innerHTML,/Setup checklist complete/);assert.equal(f.requests.length,0);
 f.element('wizard-restart').listeners.click();assert.equal(f.call('wizardProgress.completed'),false);assert.equal(f.call('wizardProgress.validated'),false);assert.equal(f.call('wizardData.configs.length'),1);assert.equal(f.requests.length,0);
});

test('Users still shows default-password status after the reminder is dismissed',async()=>{
 const f=fixture();f.element('dismiss-password-notice').listeners.click();f.call('tab="settings";settingsSection="users"');f.context.fetch=async()=>({ok:true,status:200,json:async()=>[{id:'admin-id',username:'admin',enabled:true,default_password:true}]});await f.call('refreshUsers()');assert.equal(f.element('default-password-notice').hidden,true);assert.match(f.element('users-table').innerHTML,/Default password/);assert.equal(f.call('webSession.user.default_password'),true);
});


test('workflow drafts survive refresh controls and Windows choices require a profile',()=>{
 const f=fixture();f.element('workflow-name').value='Unsaved process';f.element('workflow-rounds').value='3';
 f.element('workflow-after').value='automatic';f.call('workflowAfterChange()');assert.equal(f.element('workflow-profile-label').hidden,false);assert.equal(f.element('workflow-profile').required,true);
 f.element('workflow-after').value='stop';f.call('workflowAfterChange()');assert.equal(f.element('workflow-profile').disabled,true);
 assert.equal(f.element('workflow-name').value,'Unsaved process');assert.equal(f.element('workflow-rounds').value,'3');
});

test('workflow erasure is disabled until the optional media is ready',()=>{
 const f=fixture();f.call("workflowData={media:{ready:false,reason:'Download ShredOS first'},templates:[]};workflowReadiness()");assert.equal(f.element('workflow-queue').disabled,true);assert.equal(f.element('workflow-readiness').textContent,'Download ShredOS first');
 f.call('workflowData.media.ready=true;workflowReadiness()');assert.equal(f.element('workflow-queue').disabled,false);
});

test('workflow records escape specs and keep failed erasure visible',()=>{
 const f=fixture();const row={id:'fixture',asset_tag:'<script>bad</script>',workflow:{name:'Workshop'},mac:'52:54:00:00:00:01',target:'serial:LAB',size:1024,stage:'failed',certificate_url:null,inventory:{manufacturer:'<img>',model:'PC'}};
 f.context.fixtureRun=row;const html=f.call('workflowRunRows([fixtureRun]).flat().join(" " )');assert.match(html,/&lt;script&gt;/);assert.match(html,/&lt;img&gt;/);assert.match(html,/installation blocked/);assert.doesNotMatch(html,/<script>/);
});

test('queueing a wipe requires an exact local disk confirmation before any request',async()=>{
 const f=fixture();f.call('workflowData={media:{ready:true},templates:[]}');f.element('workflow-target').value='serial:LAB';f.element('workflow-confirm').value='serial:OTHER';
 await f.element('workflow-run-form').listeners.submit({preventDefault(){}});assert.equal(f.requests.length,0);assert.match(f.element('message').textContent,/exact target disk/);
});

test('workflow template sends typed options and the existing CSRF cookie session',async()=>{
 const f=fixture();f.element('workflow-name').value='Erase only';f.element('workflow-after').value='stop';f.element('workflow-method').value='zero';f.element('workflow-rounds').value='2';f.element('workflow-blank').checked=true;f.element('workflow-usb').checked=false;
 await f.element('workflow-form').listeners.submit({preventDefault(){}});
 const request=f.requests.find(r=>r.path==='/api/workflows'&&r.options.method==='POST');const spec=JSON.parse(request.options.body);assert.equal(spec.rounds,2);assert.equal(spec.final_blank,true);assert.equal(spec.profile_id,null);assert.equal(request.options.headers['X-CSRF-Token'],'fixture-csrf');
});


test('intake cannot be enabled before media and discovered disks require a fresh confirmation',()=>{
 const f=fixture();f.call('workflowData={media:{ready:false},intake_enabled:false};renderIntake()');assert.equal(f.element('workflow-intake-toggle').disabled,true);
 f.call('workflowData.media.ready=true;renderIntake()');assert.equal(f.element('workflow-intake-toggle').disabled,false);
 f.call("workflowDevices=[{mac:'52:54:00:12:34:56',disks:[{identity:'serial:DEMO',model:'SSD',size_bytes:1024}]}]");
 f.element('workflow-device').value='52:54:00:12:34:56';f.element('workflow-confirm').value='old serial';f.element('workflow-device').listeners.change();
 assert.equal(f.element('workflow-mac').value,'52:54:00:12:34:56');assert.equal(f.element('workflow-confirm').value,'');assert.equal(f.element('workflow-target').value,'');
 f.element('workflow-disk').value='serial:DEMO';f.element('workflow-disk').listeners.change();assert.equal(f.element('workflow-target').value,'serial:DEMO');assert.equal(f.element('workflow-size').value,1024);assert.equal(f.element('workflow-confirm').value,'');
});

test('pending clients expose boot holds, useful identity and controls safely',()=>{
 const f=fixture();Object.assign(f.client,{manufacturer:'HP',model:'<script>bad</script>',target:'serial:TEST',stage:'needs-operator',job_stage:'awaiting-windows',job_id:'job-pending',pending:true,needs_attention:true,boot_blocked:true,online:false,can_cancel:true,can_reset:true,can_delete:false,error:'<bad>'});
 f.dashboard.client_counts={pending:1,needs_attention:1};f.call('renderClients(fixtureDashboard)');
 const html=f.element('client-table').innerHTML;assert.match(html,/Driver completion pending/);assert.match(html,/Serial unavailable/);assert.doesNotMatch(html,/52:54:00:00:00:01/);assert.match(html,/serial:TEST/);assert.match(html,/data-client-action="cancel"/);assert.match(html,/Reinstall/);assert.doesNotMatch(html,/data-client-action="delete"/);assert.doesNotMatch(html,/<script>/);assert.match(html,/&lt;bad&gt;/);assert.equal(f.element('client-pending').textContent,'1 pending');
});

test('client filters, search and pagination are sent to the server',async()=>{
 const f=fixture();f.call("tab='jobs'");f.element('client-search').value='HP serial';await f.element('client-search-form').listeners.submit({preventDefault(){}});await new Promise(resolve=>setImmediate(resolve));
 assert.ok(f.requests.some(r=>r.path.includes('/api/dashboard?')&&r.path.includes('search=HP+serial')));
 await f.element('client-view').listeners.change({target:{value:'pending'}});await new Promise(resolve=>setImmediate(resolve));
 assert.ok(f.requests.some(r=>r.path.includes('view=pending')));
 f.call('clientPage=2');assert.match(f.call('clientQuery()'),/page=2/);
});

test('confirmed job cancellation retains its observed job binding',async()=>{
 const f=fixture();Object.assign(f.client,{can_cancel:true,job_id:'job-1'});f.call('renderClients(fixtureDashboard)');
 f.context.fetch=async(path,options)=>{f.requests.push({path,options});return {ok:true,status:200,json:async()=>path.includes('/cancel')?{}:path.startsWith('/api/dashboard')?f.dashboard:{counts:{image:0,drivers:0,pending:0},storage:{free:100}}};};
 await f.call("startClientControl('client-1','cancel')");
 const request=f.requests.find(r=>r.path.endsWith('/cancel'));assert.equal(request.options.method,'POST');assert.deepEqual(JSON.parse(request.options.body),{expected_job_id:'job-1',reason:'Operator requested cancel job'});assert.equal(request.options.headers['X-CSRF-Token'],'fixture-csrf');assert.equal(f.element('client-control-dialog').hidden,false);
});

test('client model is clickable and last contact uses server-relative minutes',()=>{
 const f=fixture();f.dashboard.sampled_at='2026-10-06T06:15:00Z';f.call('renderClients(fixtureDashboard)');const html=f.element('client-table').innerHTML;
 assert.match(html,/class="client-name" data-client-details="client-1"/);assert.match(html,/15 minutes ago/);assert.doesNotMatch(html,/06:00/);
 assert.equal(f.call("clientMinutesAgo('2026-10-06T06:00:00Z','2026-10-06T06:01:00Z')"),'1 minute ago');
 assert.equal(f.call("clientMinutesAgo('bad')"),'Not recorded');
});

test('client checkboxes survive filtering and bulk actions retain failed selections',async()=>{
 const f=fixture();Object.assign(f.client,{can_cancel:true,job_id:'job-1'});f.call('renderClients(fixtureDashboard)');
 f.call("clientSelectionChanged({target:{id:'client-select-visible',checked:true,dataset:{}}})");assert.equal(f.call('selectedClients.size'),1);
 f.call("lastClientsData={...fixtureDashboard,clients:[]};updateClientSelection()");assert.equal(f.element('client-selection').textContent,'1 selected · 1 hidden');
 f.context.fetch=async(path,options)=>{f.requests.push({path,options});return {ok:true,status:200,json:async()=>path==='/api/clients/bulk'?{results:[{clientId:'client-1',ok:false,error:'Client changed'}]}:path.startsWith('/api/dashboard')?f.dashboard:{counts:{image:0,drivers:0,pending:0},storage:{free:100}}};};
 await f.call("bulkClientControl('cancel')");assert.equal(f.call('selectedClients.size'),1);assert.match(f.element('message').textContent,/Client changed/);
 const request=f.requests.find(r=>r.path==='/api/clients/bulk');assert.deepEqual(JSON.parse(request.options.body),{action:'cancel',sessions:[{client_id:'client-1',expected_job_id:'job-1'}]});
});


test('dashboard keeps a simple overview while Jobs owns session controls',()=>{
 const f=fixture();Object.assign(f.client,{system_serial:'SYS-001',can_cancel:true,targets:[]});f.call('renderDashboard(fixtureDashboard)');const html=f.element('dashboard-clients').innerHTML;
 assert.match(html,/SYS-001/);assert.match(html,/data-client-details/);assert.doesNotMatch(html,/client-select|Cancel session|data-client-config/);
 f.call('renderClients(fixtureDashboard)');assert.match(f.element('client-table').innerHTML,/client-select|Cancel session/);
});

test('device specs show serials and components with escaped firmware strings',()=>{
 const f=fixture();f.context.device={inventory_available:true,specs:{system:{model:'Laptop',serial:'SYS-001'},processors:[{name:'CPU <script>',cores:8,threads:16}],memory:{total_bytes:17179869184,modules:[{location:'DIMM0',serial:'MEM-1',size_bytes:17179869184}]},firmware:{secure_boot:false},graphics:[{name:'GPU'}],storage:[{serial:'SSD-1',size_bytes:128849018880}]}};const html=f.call('deviceSpecs(device)');
 assert.match(html,/SYS-001/);assert.match(html,/MEM-1/);assert.match(html,/SSD-1/);assert.match(html,/GPU/);assert.match(html,/8 cores/);assert.match(html,/&lt;script&gt;/);assert.doesNotMatch(html,/<script>/);
});

test('report search sends outcome, dates and paging to the server',()=>{
 const f=fixture();f.call("reportPage=3;reportFilters={search:'SYS-001',status:'failed',start:'2026-10-01',end:'2026-10-08',sort:'processed',order:'desc'}");const query=f.call('reportQuery()');assert.match(query,/page=3/);assert.match(query,/search=SYS-001/);assert.match(query,/status=failed/);assert.match(query,/start=2026-10-01/);
});

test('report checkboxes keep hidden selections and bound bulk PDF downloads',()=>{
 const f=fixture();f.context.reports={reports:[{id:'job-1',model:'Laptop',target_identity:'serial:SSD',stage:'failed',status_label:'Failed',updated:'2026-10-07T12:00:00Z'}],total:1,page:1,pages:1};f.call('renderReports(reports)');f.element('report-select-visible').listeners.change({target:{checked:true}});assert.equal(f.call('selectedReports.size'),1);f.call('reportRows=[];updateReportSelection()');assert.match(f.element('report-selection').textContent,/1 hidden/);assert.equal(f.element('report-download-selected').disabled,false);f.call('for(let i=0;i<201;i++)selectedReports.set("x"+i,{});updateReportSelection()');assert.equal(f.element('report-download-selected').disabled,true);
});

test('installation progress uses its own percentage in Dashboard, Jobs and details',()=>{
 const f=fixture();Object.assign(f.client,{stage:'applying-image',install_percent:37,received_bytes:100,total_bytes:100});f.call('renderDashboard(fixtureDashboard);renderClients(fixtureDashboard)');
 for(const id of ['dashboard-clients','client-table']){const html=f.element(id).innerHTML;assert.match(html,/37% installed/);assert.match(html,/value="37"/);assert.doesNotMatch(html,/100%.*download/);}
});

test('download and later installation stages cannot display a false installed percentage',()=>{
 const f=fixture();Object.assign(f.client,{stage:'applying-image',install_percent:null,received_bytes:100,total_bytes:100});assert.match(f.call('clientProgress(fixtureDashboard.clients[0])'),/Waiting for installation progress/);
 f.client.stage='downloading';assert.match(f.call('clientProgress(fixtureDashboard.clients[0])'),/100% downloaded/);
 f.client.stage='boot-configured';f.client.install_percent=100;assert.doesNotMatch(f.call('clientProgress(fixtureDashboard.clients[0])'),/%/);
});

test('dashboard layout recovery bounds corrupted widths and ignores duplicate/unknown widgets',()=>{
 const f=fixture();f.context.badLayout={widgets:[{id:'storage',visible:false,width:6},{id:'storage',width:12},{id:'unknown',width:4},{id:'clients',width:-1}],clientMode:'garbage',clientFilter:'invalid'};
 const layout=f.call('normalizeDashboardLayout(badLayout)');assert.equal(layout.widgets.length,6);assert.equal(layout.widgets[0].id,'storage');assert.equal(layout.widgets[0].visible,false);assert.equal(layout.widgets[0].width,6);assert.equal(layout.widgets[1].width,12);assert.equal(layout.clientMode,'table');assert.equal(layout.clientFilter,'current');
});

test('dashboard saved views are scoped to the signed-in account and labels escaped',()=>{
 const saved={'repxe.dashboard.v1.admin-id':JSON.stringify({selected:'view-one',views:[{id:'view-one',name:'<img src=x>',widgets:[{id:'clients',visible:true,width:12}],clientMode:'cards',clientFilter:'attention'}]})};
 const f=fixture(saved);f.call('loadDashboardViews()');assert.match(f.element('dashboard-view').innerHTML,/&lt;img src=x&gt;/);assert.equal(f.call('dashboardClientQuery()'),'?view=attention');assert.equal(f.call('dashboardLayout().clientMode'),'cards');
 f.call("webSession.user.id='second-user';loadDashboardViews()");assert.equal(f.call('dashboardViews.selected'),'overview');assert.doesNotMatch(f.element('dashboard-view').innerHTML,/img src=x/);
});

test('quick clear cancels before retiring the same job and never requests a reset',async()=>{
 const f=fixture();Object.assign(f.client,{can_cancel:true,can_delete:false,job_id:'job-guard'});f.call('renderSimpleClients(fixtureDashboard);refresh=async()=>{}');await f.call("dismissClient('client-1')");
 const requests=f.requests.filter(r=>r.path.startsWith('/api/clients/'));assert.deepEqual(requests.map(r=>[r.path,r.options.method]),[['/api/clients/client-1/cancel','POST'],['/api/clients/client-1','DELETE']]);for(const r of requests)assert.equal(JSON.parse(r.options.body).expected_job_id,'job-guard');assert.equal(f.call('clientBusy'),false);
});

test('quick clear preserves a changed session when cancellation is rejected',async()=>{
 const f=fixture();Object.assign(f.client,{can_cancel:true,job_id:'job-original'});f.call('renderSimpleClients(fixtureDashboard);refresh=async()=>{}');f.context.fetch=async(path,options)=>{f.requests.push({path,options});return {ok:false,status:409,json:async()=>({detail:'Client changed'})};};await f.call("dismissClient('client-1')");
 assert.equal(f.requests.filter(r=>r.path.startsWith('/api/clients/')).length,1);assert.match(f.element('message').textContent,/Client changed/);assert.equal(f.call('clientBusy'),false);
});


test('wizard reminder can be closed without resetting its saved step',()=>{
 const f=fixture();f.call("loadWizardProgress();wizardProgress.step=4;wizardStarted=true;tab='images';updateWizardReturn();");
 assert.equal(f.element('wizard-return').hidden,false);
 f.element('wizard-dismiss').listeners.click();assert.equal(f.element('wizard-return').hidden,true);
 assert.equal(f.call('wizardProgress.step'),4);
});

test('routine controls contain only pinned nwipe methods',()=>{
 const f=fixture();f.element('routine-new').listeners.click();f.call("addRoutineStep('erase',1);renderRoutineStep();");
 const html=f.element('routine-step-settings').innerHTML;
 for(const name of ['DoD 5220.22-M','RCMP TSSIT','HMG IS5','Schneier','BMB21','Verify zeros','Fill with ones'])assert.ok(html.includes(name));
 assert.doesNotMatch(html,/Fallback method|ATA Sanitize|NVMe Sanitize|data-routine-field="unfreeze"/);
 assert.ok(f.call("Object.keys(routineMethods).length")>=12);
});

test('routine reordering preserves Boot and Finish and protects unsaved drafts',()=>{
 const f=fixture();f.element('routine-new').listeners.click();f.call("addRoutineStep('erase',1);moveRoutineStep(1,2);");
 assert.deepEqual(Array.from(f.call('routineDraft.steps.map(s=>s.kind)')),['boot','windows','erase','finish']);
 assert.equal(f.call('routineDirty'),true);assert.equal(f.element('routine-publish').disabled,true);
 f.call('moveRoutineStep(0,2)');assert.equal(f.call('routineDraft.steps[0].kind'),'boot');
 f.call("routineData={routines:[{id:'other',generation:1,draft:{name:'Other',steps:[]}}]};openRoutine('other')");assert.equal(f.call('routineEditingId'),null);
});

test('dashboard default uses published routines when routine boot is enabled',async()=>{
 const f=fixture();f.dashboard.routines={enabled:true,default_id:'routine-id',choices:[{id:'routine-id',name:'Erase then install'}]};
 f.call('renderDashboard(fixtureDashboard)');assert.match(f.element('dashboard-default').innerHTML,/Erase then install/);
 f.element('dashboard-default').value='routine-id';await f.element('dashboard-default').listeners.change({target:f.element('dashboard-default')});
 const r=f.requests.find(r=>r.path==='/api/routine-settings');assert.deepEqual(JSON.parse(r.options.body),{enabled:true,default_id:'routine-id'});
});

test('wizard follows published routine readiness and its default rather than the legacy configuration',()=>{
 const f=wizardFixture();f.call("wizardFixtureData.dashboard.routines={enabled:false,default_id:null,choices:[]};setWizardStep(2)");
 assert.equal(f.call('wizardReady()[2]'),false);assert.match(f.element('wizard-body').innerHTML,/Edit routines and assignments/);
 f.call("wizardFixtureData.dashboard.routines={enabled:true,default_id:'routine',choices:[{id:'routine',name:'Wipe & install'}]};renderWizard()");
 assert.equal(f.call('wizardReady()[2]'),true);assert.match(f.element('wizard-body').innerHTML,/Wipe &amp; install/);assert.match(f.element('wizard-body').innerHTML,/5 seconds/);
 f.call("wizardFixtureData.dashboard.routines.default_id='withdrawn'");assert.equal(f.call('wizardReady()[5]'),false);
 f.call("openWizardTool('routines')");assert.equal(f.call('tab'),'routines');
});

test('erase-only setup starts with routines and requires its capability rather than Windows content',()=>{
 const f=wizardFixture();f.call("wizardFixtureData.dashboard.routines={enabled:true,default_id:'wipe',choices:[{id:'wipe',name:'Erase',has_windows:false}]};wizardFixtureData.routines={routines:[{id:'wipe',draft:{steps:[{kind:'boot'},{kind:'erase'},{kind:'finish'}]}}]};wizardFixtureData.capabilities=[];setWizardStep(2)");
 assert.match(f.element('wizard-heading').textContent,/Create your routine/);assert.equal(f.call('wizardReady()[2]'),true);assert.equal(f.call('wizardReady()[3]'),false);
 f.call("wizardFixtureData.capabilities=[{status:'installed'}];setWizardStep(3)");assert.equal(f.call('wizardReady()[3]'),true);assert.match(f.element('wizard-body').innerHTML,/does not install Windows/);assert.equal(f.call('wizardReady()[5]'),true);
 f.call("wizardFixtureData.dashboard.routines.default_id='missing'");assert.equal(f.call('wizardReady()[5]'),false);
});

test('wizard progress migrates its previous routine step without losing confirmations',()=>{
 const f=fixture({'repxe.wizard.v1.admin-id':JSON.stringify({step:5,network:true,validated:true})});f.call('loadWizardProgress()');assert.equal(f.call('wizardProgress.step'),2);assert.equal(f.call('wizardProgress.network'),true);assert.equal(f.call('wizardProgress.validated'),true);
});

function sampleRoutineRun(overrides={}){return {id:'run-1',name:'Erase and install',model:'Test laptop',mac:'52:54:00:00:00:01',version:2,step:1,steps:[{kind:'boot'},{kind:'erase'},{kind:'finish'}],stage:'checkpoint',can_cancel:true,can_continue:true,report_id:'routine:run-1',last_activity:'2026-10-08T00:00:00Z',events:[],...overrides};}

test('routine Jobs retain hidden selections and only cancel selected eligible runs with their step guard',async()=>{
 const f=fixture();f.context.first=sampleRoutineRun();f.context.second=sampleRoutineRun({id:'run-2',model:'Other PC',step:2});f.context.activeErase=sampleRoutineRun({id:'run-3',can_cancel:false,stage:'erase-running',erase_stage:'erasing'});
 f.call('renderRoutineJobs({runs:[first],total:3,page:1,pages:3});selectedRoutineJobs.set(first.id,first);renderRoutineJobs({runs:[second,activeErase],total:3,page:2,pages:3});selectedRoutineJobs.set(second.id,second);selectedRoutineJobs.set(activeErase.id,activeErase);updateRoutineJobSelection();tab="editor"');
 assert.equal(f.element('routine-job-selection').textContent,'3 selected · 1 hidden');
 f.context.fetch=async(path,options)=>{f.requests.push({path,options});return {ok:path!='/api/routine-runs/run-2/action',status:200,json:async()=>path==='/api/overview'?{counts:{image:0,drivers:0,pending:0},storage:{free:100}}:{detail:'Step changed; refresh'}};};
 await f.element('routine-job-cancel-selected').listeners.click();
 const actions=f.requests.filter(r=>r.path.endsWith('/action'));assert.equal(actions.length,2);assert.deepEqual(JSON.parse(actions[0].options.body),{action:'cancel',expected_step:1});assert.equal(f.call('selectedRoutineJobs.has("run-1")'),false);assert.equal(f.call('selectedRoutineJobs.has("run-2")'),true);assert.match(f.element('message').textContent,/Step changed/);
});

test('routine client details stay in Jobs and include checkpoint controls and report links',async()=>{
 const f=fixture();f.context.detail=sampleRoutineRun({system_serial:'SER-001',inventory:{cpu:'CPU',memory_bytes:8192}});f.context.fetch=async()=>({ok:true,status:200,json:async()=>f.context.detail});f.call("tab='jobs'");await f.call("showRoutineRun('run-1')");
 assert.equal(f.call('tab'),'jobs');assert.match(f.element('details-title').textContent,/SER-001/);assert.match(f.element('job-summary').innerHTML,/SER-001/);assert.match(f.element('detail-actions').innerHTML,/data-routine-action="continue"/);assert.match(f.element('detail-actions').innerHTML,/data-routine-report="routine:run-1"/);
});

test('dashboard cards follow routine clients without duplicating their Windows sessions',()=>{
 const f=fixture();f.context.run=sampleRoutineRun({stage:'windows-running',client_stage:'applying-image',install_percent:37});f.dashboard.routines={active:[f.context.run]};f.client.routine={id:'run-1'};f.call("dashboardViews.selected='clients';renderSimpleClients(fixtureDashboard)");
 assert.match(f.element('dashboard-routine-clients').innerHTML,/dashboard-client-card/);assert.match(f.element('dashboard-routine-clients').innerHTML,/37% installed/);assert.doesNotMatch(f.element('dashboard-clients').innerHTML,/data-client-details/);
});

test('Erase only creates a routine draft without a Windows step',()=>{
 const f=fixture();f.element('routine-new-erase').click();assert.deepEqual(Array.from(f.call('routineDraft.steps.map(s=>s.kind)')),['boot','erase','finish']);assert.equal(f.call('routineDirty'),true);assert.equal(f.element('routine-name').value,'Erase and certify');
});

test('NIST presets and nwipe native methods retain scoped controls',()=>{
 const f=fixture();f.element('routine-new-erase').click();const html=f.element('routine-step-settings').innerHTML;
 assert.equal(f.call('Object.keys(routineMethods).length'),17);
 assert.doesNotMatch(html,/ata_secure|nvme_sanitize|data-routine-field="(?:unfreeze|readback|fallback_method)"/);
 assert.match(html,/value="sanitize_block"/);assert.match(html,/NIST Clear/);assert.match(html,/NIST Purge/);
 assert.match(html,/original per-drive PDF/);
});

test('read-only verification disables rounds, blanking and random generator',()=>{
 const f=fixture();f.element('routine-new-erase').click();
 f.call("routineDraft.steps[1].method='verify_zero';normalizeRoutineWipe(routineDraft.steps[1]);renderRoutineStep();");
 const html=f.element('routine-step-settings').innerHTML;
 for(const field of ['rounds','verification','prng'])assert.match(html,new RegExp('data-routine-field="'+field+'"[^>]*disabled'));
 assert.match(html,/data-routine-field="final_blank" type="checkbox" disabled/);
});

test('graph failure port connects to custom iPXE without changing the success route',()=>{
 const f=fixture();f.element('routine-new-erase').click();
 f.call("addRoutineStep('ipxe');connectRoutine(routineDraft.steps[1].id,routineDraft.steps[2].id,'failure');");
 const edges=f.call('routineEdges()');
 assert.equal(f.call('routineDraft.graph'),true);
 assert.equal(edges.filter(e=>e.outcome==='failure').length,1);
 assert.match(f.element('routine-flow').innerHTML,/routine-edge failure/);
 assert.match(f.element('routine-flow').innerHTML,/data-routine-output=/);
 assert.match(f.element('routine-step-settings').innerHTML,/Create failure screen/);
});

test('disconnecting every graph edge leaves an incomplete graph instead of recreating a chain',()=>{
 const f=fixture();f.element('routine-new-erase').click();
 f.call('materializeRoutineEdges();routineDraft.connections=[];renderRoutineFlow();');
 assert.equal(f.call('routineEdges().length'),0);
 assert.doesNotMatch(f.element('routine-flow').innerHTML,/class="routine-edge success"/);
});

test('nwipe random stream settings remain editable without fallback',()=>{
 const f=fixture();f.element('routine-new-erase').click();f.call("routineDraft.steps[1].method='prng';renderRoutineStep();");
 assert.match(f.element('routine-step-settings').innerHTML,/data-routine-field="prng"/);
 assert.doesNotMatch(f.element('routine-step-settings').innerHTML,/Fallback settings/);
});

test('devices sharing a USB NIC stay labelled and naturally ordered by their serials',()=>{
 const f=fixture();Object.assign(f.client,{system_serial:'SYS-10',can_cancel:true});f.dashboard.clients.push({...f.client,id:'client-2',system_serial:'SYS-2'});f.call('renderSimpleClients(fixtureDashboard);renderClients(fixtureDashboard)');
 for(const id of ['dashboard-clients','client-table']){const html=f.element(id).innerHTML;assert.ok(html.indexOf('SYS-2')<html.indexOf('SYS-10'));assert.doesNotMatch(html,/52:54:00:00:00:01/);}
 f.context.serialRuns=[sampleRoutineRun({id:'run-10',system_serial:'SYS-10'}),sampleRoutineRun({id:'run-2',system_serial:'SYS-2'})];f.call('renderRoutineJobs({runs:serialRuns,total:2,page:1,pages:1})');const html=f.element('routine-jobs-table').innerHTML;assert.ok(html.indexOf('SYS-2')<html.indexOf('SYS-10'));assert.doesNotMatch(html,/52:54:00:00:00:01/);
});

test('declining device removal leaves its session and reports untouched',async()=>{
 const f=fixture();Object.assign(f.client,{system_serial:'SYS-001',can_cancel:true,job_id:'job-guard'});f.call('renderSimpleClients(fixtureDashboard)');f.context.window.confirm=value=>{f.confirmations.push(value);return false;};await f.call("dismissClient('client-1')");
 assert.equal(f.requests.length,0);assert.equal(f.call('clientBusy'),false);assert.match(f.confirmations[0],/SYS-001/);assert.match(f.confirmations[0],/reports.*retained/);assert.doesNotMatch(f.confirmations[0],/52:54/);
});

test('browser Back and Forward restore portal tabs and settings without duplicate history',async()=>{
 const f=fixture();f.call('refresh=async()=>{}');await f.call('initializePortalNavigation()');f.call("switchTab('settings');switchSettings('network');switchSettings('appearance');switchTab('jobs');switchTab('jobs')");
 assert.equal(f.navigationEntries.length,5);await f.browserHistory.back();assert.equal(f.call('tab'),'settings');assert.equal(f.call('settingsSection'),'appearance');await f.browserHistory.back();assert.equal(f.call('settingsSection'),'network');await f.browserHistory.forward();assert.equal(f.call('settingsSection'),'appearance');assert.equal(f.context.window.location.hash,'#settings?section=appearance');
});

test('browser Back restores file folders, wizard steps and guide menus',async()=>{
 const f=fixture();f.call('refresh=async()=>{};refreshFiles=async()=>{}');await f.call('initializePortalNavigation()');f.call("switchTab('files')");await f.call("navigateFiles('configuration','answer-files')");f.call("switchTab('wizard');setWizardStep(3);activeGuide='boot';switchTab('guides');activeGuide=null;rememberPortalNavigation()");
 await f.browserHistory.back();assert.equal(f.call('activeGuide'),'boot');await f.browserHistory.back();assert.equal(f.call('tab'),'wizard');assert.equal(f.call('wizardProgress.step'),3);await f.browserHistory.back();await f.browserHistory.back();assert.equal(f.call('tab'),'files');assert.equal(f.call('fileLocation.path'),'config/answer-files');
});

test('browser Back closes device details and Forward reloads their current controls',async()=>{
 const f=fixture();Object.assign(f.client,{system_serial:'SYS-001',targets:[],can_cancel:true});f.call('refresh=async()=>{}');await f.call('initializePortalNavigation()');f.call("switchTab('jobs')");f.context.fetch=async()=>({ok:true,status:200,json:async()=>({client:f.client,job:null,device:{},sessions:[],controls:[]})});await f.call("showClient('client-1')");
 assert.equal(f.element('details').open,true);assert.match(f.element('detail-actions').innerHTML,/data-client-action="cancel"/);await f.browserHistory.back();assert.equal(f.element('details').open,false);assert.equal(f.call('tab'),'jobs');await f.browserHistory.forward();assert.equal(f.element('details').open,true);assert.equal(f.element('details-title').textContent,'Device · SYS-001');
});

test('history preserves unsaved text when returning to another editor file',async()=>{
 const f=fixture();f.call('refresh=async()=>{}');await f.call('initializePortalNavigation()');f.call("setEditorFile({root:'configuration',path:'first.txt',content:'first',editable:true});setEditorFile({root:'configuration',path:'second.txt',content:'second',editable:true});editorDirty=true");f.element('editor-content').value='unsaved';await f.browserHistory.back();
 assert.equal(f.element('editor-content').value,'unsaved');assert.equal(f.call('editorFile.path'),'second.txt');assert.match(f.element('message').textContent,/unsaved text remains open/);
});

test('battery health and serial-bound ShredOS attachments are visible with the asset',()=>{
 const f=fixture();f.context.batteryDevice={inventory_available:true,specs:{system:{serial:'SYS-001'},batteries:[{name:'Internal battery',design_capacity_mwh:50000,full_charge_capacity_mwh:40000,health_percent:80,cycle_count:120}]}};const specs=f.call('deviceSpecs(batteryDevice)');assert.match(specs,/80%/);assert.match(specs,/40000 mWh full charge/);assert.match(specs,/120 cycles/);
 f.context.attachments=[{report_id:'routine:run-1',system_serial:'SYS-001',name:'Erase <script>',stage:'complete',created:'2026-10-08T00:00:00Z',pdf_url:'/api/job-reports/routine%3Arun-1/pdf',certificate_url:'/api/workflow-runs/wipe-1/certificate'}];const html=f.call('assetReports(attachments)');assert.match(html,/Asset reports/);assert.match(html,/View re-pxe report/);assert.match(html,/ShredOS report/);assert.match(html,/SYS-001/);assert.doesNotMatch(html,/<script>/);
});

test('dialog close and frequent controls precede scrollable details and reports',()=>{
 const html=readFileSync(fileURLToPath(new URL('../repxe/static/index.html',import.meta.url)),'utf8');
 for(const [dialog,control,content] of [['details','detail-close','job-summary'],['report-viewer','report-viewer-close','report-viewer-summary'],['driver-delete-dialog','driver-delete-confirm','driver-delete-entries'],['file-create-dialog','file-create-cancel','file-create-name']]){const start=html.indexOf('id="'+dialog+'"'),end=html.indexOf('</dialog>',start),section=html.slice(start,end);assert.ok(section.indexOf('id="'+control+'"')<section.indexOf('id="'+content+'"'));assert.match(section,/dialog-toolbar/);}
});

test('rapid tab changes queue a refresh so a slow earlier response cannot strand the new view',async()=>{
 const f=fixture();let release,overviewCalls=0;const firstOverview=new Promise(resolve=>{release=resolve;});f.context.fetch=async path=>{if(path==='/api/overview'&&overviewCalls++===0)await firstOverview;return {ok:true,status:200,json:async()=>path==='/api/overview'?{counts:{image:0,drivers:0,pending:0},storage:{free:100}}:[]};};
 f.call("tab='images'");const pending=f.call('refresh()');f.call("switchTab('drivers')");assert.equal(f.call('refreshQueued'),true);release();await pending;await new Promise(resolve=>setImmediate(resolve));assert.equal(f.call('tab'),'drivers');assert.match(f.element('table').innerHTML,/No drivers published yet/);assert.equal(f.call('refreshing'),false);
});


test('firmware serial placeholders remain explicitly unavailable and sort after real serials',()=>{
 const f=fixture();for(const serial of ['', 'Unknown','default string','To Be Filled By O.E.M.','not reported','system serial number']){f.context.placeholder={system_serial:serial,mac:'52:54:00:00:00:01',model:'Laptop'};assert.equal(f.call('deviceSerial(placeholder)'),'');assert.equal(f.call('deviceLabel(placeholder)'),'Serial unavailable · Laptop');}
 f.context.placeholderRows=[{id:'a',system_serial:'Unknown'},{id:'z',system_serial:'SYS-001'}];assert.equal(f.call('sortDevices(placeholderRows)[0].id'),'z');
});

test('rapid history changes reject a late device details response from the earlier view',async()=>{
 const f=fixture();f.call('refresh=async()=>{}');await f.call('initializePortalNavigation()');f.call("switchTab('jobs')");let release;const held=new Promise(resolve=>{release=resolve;});f.context.fetch=async()=>{await held;return {ok:true,status:200,json:async()=>({client:{...f.client,targets:[],system_serial:'SYS-001'},job:null,device:{},sessions:[],controls:[]})};};
 f.context.staleDetailState={user:'admin-id',index:2,route:{tab:'jobs',modal:{kind:'client',id:'client-1'}}};const pending=f.call('restorePortalNavigation(staleDetailState)');f.call("switchTab('images')");release();await pending;assert.equal(f.call('tab'),'images');assert.notEqual(f.element('details').open,true);
});

test('manual Windows choice shows models and capacities without selecting a default',()=>{
 const f=fixture();f.context.fixtureChoice={id:'pc',configuration_id:'preset',disk_selection_ready:true,targets:[{identity:'serial:ONE',model:'SSD <One>',size_bytes:64*1024**3},{identity:'serial:TWO',model:'Second SSD',size_bytes:128*1024**3}]};
 const html=f.call('diskSelectionHtml(fixtureChoice)');assert.match(html,/Needs attention/);assert.match(html,/Choose a drive/);assert.match(html,/SSD &lt;One&gt;/);assert.match(html,/128/);assert.doesNotMatch(html,/selected/);
 f.context.fixtureChoice.disk_selection_ready=false;assert.equal(f.call('diskSelectionHtml(fixtureChoice)'), '');
});
test('erasure telemetry shows measured per-drive percent, ETA and stale estimates',()=>{
 const f=fixture();f.context.fixtureRun={erase_disks:[{target:'serial:ONE',size:123,stage:'erasing',percent:42,detail:'Verifying',remaining_seconds:536},{target:'serial:TWO',stage:'batch-wait',percent:null}]};let html=f.call('eraseProgressHtml(fixtureRun)');assert.match(html,/value="42"/);assert.match(html,/8m 56s remaining/);assert.match(html,/serial:TWO/);assert.doesNotMatch(html,/null%/);f.context.fixtureRun.erase_disks[0].progress_stale=true;html=f.call('eraseProgressHtml(fixtureRun)');assert.match(html,/Updates stopped · last estimate/);assert.match(html,/Inspect the PC/);
});
test('routine attention distinguishes manual disk choice from technician checkpoints',()=>{
 const f=fixture();assert.equal(f.call("routineRunStatus({needs_attention:true,client_stage:'waiting-for-input'})"),'Needs attention · select Windows disk');
 assert.equal(f.call("routineRunStatus({needs_attention:true,stage:'checkpoint'})"),'Needs attention · technician checkpoint');
});

test('dashboard attention navigation sets both lists and clears hidden searches',async()=>{
 const f=fixture();f.call("clientSearch='old serial';clientPage=4;routineJobsSearch='old routine';routineJobsPage=3;routineJobsView='active';portalNavigationReady=true;rememberPortalNavigation(null,true)");
 f.call("openJobsView('attention');switchTab('jobs')");
 assert.equal(f.call('clientView'),'attention');assert.equal(f.call('routineJobsView'),'attention');assert.equal(f.call('clientSearch'),'');assert.equal(f.call('routineJobsSearch'),'');
 assert.equal(f.element('routine-job-view').value,'attention');assert.equal(f.element('windows-sessions-panel').open,true);
 assert.equal(f.call('clientPage'),1);assert.equal(f.call('routineJobsPage'),1);
 assert.equal(f.call('portalRoute().routineView'),'attention');
 await f.browserHistory.back();assert.equal(f.call('tab'),'dashboard');
});

test('attention count includes standalone sessions alongside routines and failed status stays visible',()=>{
 const f=fixture();f.dashboard.attention_count=3;f.dashboard.routines={enabled:true,choices:[],counts:{attention:2},active:[],recent:[]};f.call('renderDashboard(fixtureDashboard)');assert.equal(f.element('needs-attention').textContent,3);
 assert.equal(f.call("routineRunStatus({needs_attention:true,stage:'failed',client_stage:'waiting-for-input'})"),'Failed');
 assert.equal(f.call("routineRunStatus({needs_attention:false,stage:'cancelled',client_stage:'waiting-for-input'})"),'Cancelled');
 assert.equal(f.call("routineRunStatus({needs_attention:true,stage:'erase-running',erase_stage:'operation-pending'})"),'Needs attention · controller outcome pending');
 assert.equal(f.call("routineRunStatus({needs_attention:true,stage:'windows-running',client_stage:'needs-operator',effective_stage:'needs-operator'})"),'Needs attention');
});

test('rejecting cancellation confirmation leaves single and bulk jobs untouched',async()=>{
 const f=fixture();Object.assign(f.client,{can_cancel:true,system_serial:'SERIAL-1'});f.call('renderClients(fixtureDashboard);refresh=async()=>{}');f.context.window.confirm=()=>false;
 await f.call("startClientControl('client-1','cancel')");f.call("selectedClients.set('client-1',clientRows.get('client-1'))");await f.call("bulkClientControl('cancel')");
 f.context.run=sampleRoutineRun();f.call("selectedRoutineJobs.set(run.id,run)");await f.element('routine-job-cancel-selected').listeners.click();await f.call('cancelRoutineJob(run)');
 assert.equal(f.requests.length,0);assert.equal(f.call('selectedClients.size'),1);assert.equal(f.call('selectedRoutineJobs.size'),1);
});

test('attention disk picker and Windows controls are separate routed windows and close never mutates jobs',async()=>{
 const f=fixture();Object.assign(f.client,{system_serial:'SERIAL-1',stage:'waiting-for-input',needs_attention:true,disk_selection_ready:true,can_cancel:true,targets:[{identity:'serial:DISK-A',model:'SSD <A>',size_bytes:128e9},{identity:'serial:DISK-B',model:'Second SSD',size_bytes:256e9}]});
 f.call('refresh=async()=>{}');await f.call('initializePortalNavigation()');f.context.fetch=async(path,options)=>{f.requests.push({path,options});return {ok:true,status:200,json:async()=>({client:f.client,device:{},job:null,sessions:[],controls:[]})};};
 await f.call("showClient('client-1')");assert.doesNotMatch(f.element('job-summary').innerHTML,/portal-windows-disk/);
 await f.call("showWindowsControls('client-1')");assert.equal(f.element('details').open,false);assert.equal(f.element('windows-controls-dialog').open,true);assert.doesNotMatch(f.element('windows-controls-content').innerHTML,/device-specs/);
 await f.call("showAttention('client','client-1')");assert.equal(f.element('windows-controls-dialog').open,false);assert.equal(f.element('attention-dialog').open,true);assert.match(f.element('attention-content').innerHTML,/SSD &lt;A&gt;.*Second SSD/);assert.match(f.element('attention-actions').innerHTML,/Use this drive.*Cancel job/);
 await f.browserHistory.back();assert.equal(f.element('attention-dialog').open,false);assert.equal(f.element('windows-controls-dialog').open,true);await f.browserHistory.forward();assert.equal(f.element('attention-dialog').open,true);
 f.element('attention-close').listeners.click();assert.equal(f.element('attention-dialog').open,false);assert.ok(f.requests.every(r=>!r.options.method));assert.equal(f.call('portalNavigationModal'),null);
});

test('failed routine attention explains the error and offers explicit cancellation',async()=>{
 const f=fixture();f.context.run=sampleRoutineRun({stage:'failed',needs_attention:true,can_continue:false,error:'Unsupported <operation>',system_serial:'SERIAL-FAILED',client_id:null});
 f.context.fetch=async(path,options)=>({ok:true,status:200,json:async()=>f.context.run});await f.call("showAttention('routine','run-1')");
 assert.match(f.element('attention-content').innerHTML,/Unsupported &lt;operation&gt;/);assert.match(f.element('attention-actions').innerHTML,/Cancel job/);assert.doesNotMatch(f.element('attention-content').innerHTML,/portal-windows-disk/);
});

test('Windows portal sends power commands to the displayed job and uploads raw file bytes with CSRF',async()=>{
 const f=fixture(),job='11111111-2222-3333-4444-555555555555';
 const state={available:true,busy:true,files_during_drivers:true,job_id:job,commands:[{filename:'<notes>.txt',action:'file',status:'failed',size:4,result:{error:'<error>'}}]};
 f.context.fetch=async(path,options)=>{f.requests.push({path,options});return {ok:true,status:200,json:async()=>path.endsWith('/windows')?state:{client:f.client,job:null,controls:[]}};};
 await f.call("showWindowsControls('client-1')");
 assert.match(f.element('windows-remote-actions').innerHTML,/Restart Windows.*Shut down Windows.*Upload to desktop/);
 assert.match(f.element('windows-remote-status').innerHTML,/Files transfer immediately.*Restart and shutdown wait/);
 assert.match(f.element('windows-remote-actions').innerHTML,/data-windows-send>Upload/);
 assert.match(f.element('windows-remote-status').innerHTML,/&lt;notes&gt;\.txt/);
 assert.doesNotMatch(f.element('windows-remote-status').innerHTML,/<error>/);
 f.context.powerEvent={target:{closest:s=>s==='[data-windows-power]'?{dataset:{windowsPower:'restart'}}:null}};
 await f.call('windowsRemoteClick(powerEvent)');
 const power=f.requests.find(r=>r.path.endsWith('/windows/commands'));
 assert.deepEqual(JSON.parse(power.options.body),{action:'restart',expected_job_id:job});
 assert.equal(power.options.headers['X-CSRF-Token'],'fixture-csrf');
 const file=new Blob(['bytes']);file.name='Desktop notes.txt';f.element('windows-desktop-file').files=[file];
 f.context.uploadEvent={target:{closest:s=>s==='[data-windows-send]'?{}:null}};
 await f.call('windowsRemoteClick(uploadEvent)');
 const upload=f.requests.find(r=>r.path.includes('/windows/files?'));
 assert.equal(upload.options.body,file);assert.equal(upload.options.headers['Content-Type'],'application/octet-stream');
 assert.equal(upload.options.headers['X-CSRF-Token'],'fixture-csrf');assert.match(upload.path,/filename=Desktop\+notes.txt/);assert.match(upload.path,new RegExp('expected_job_id='+job));
});

test('an older busy Windows client is asked to update instead of silently queueing files',async()=>{
 const f=fixture();
 f.context.fetch=async(path,options)=>{f.requests.push({path,options});return {ok:true,status:200,json:async()=>path.endsWith('/windows')?{available:true,busy:true,files_during_drivers:false,job_id:'old',commands:[]}:{client:f.client,job:null,controls:[]}};};
 await f.call("showWindowsControls('client-1')");
 assert.match(f.element('windows-remote-actions').innerHTML,/data-windows-send disabled/);
 assert.match(f.element('windows-remote-status').innerHTML,/0\.14\.1/);
 f.context.uploadEvent={target:{closest:s=>s==='[data-windows-send]'?{}:null}};
 await f.call('windowsRemoteClick(uploadEvent)');
 assert.ok(f.requests.every(r=>!r.options.method));
});

test('Windows control polling cannot paint a different client after navigation',async()=>{
 const f=fixture();f.call("windowsRemoteClient='first';$('windows-controls-dialog').open=true");
 let resolve;f.context.fetch=()=>new Promise(r=>{resolve=r;});
 const waiting=f.call("refreshWindowsRemote('first')");f.call("windowsRemoteClient='second'");
 f.element('windows-remote-status').innerHTML='second client';resolve({ok:true,status:200,json:async()=>({available:true,commands:[]})});await waiting;
 assert.equal(f.element('windows-remote-status').innerHTML,'second client');
});

test('dashboard recent attention badges open the same routine or client action instead of a report',()=>{
 const f=fixture();f.dashboard.routines={enabled:true,active:[],choices:[],counts:{},recent:[{run_id:'run-attention',system_serial:'RECENT-1',stage:'needs-operator',status_label:'Needs attention'},{client_id:'client-attention',system_serial:'RECENT-2',stage:'failed',status_label:'Failed'}]};f.call('renderDashboard(fixtureDashboard)');
 const html=f.element('recent-jobs').innerHTML;assert.match(html,/data-attention-routine="run-attention"/);assert.match(html,/data-attention-client="client-attention"/);assert.match(html,/Needs attention · Failed/);
});

test('routine Windows image verification defaults on and persists explicit skip',()=>{
 const f=fixture();f.call("routineDraft={name:'Install',steps:[{id:'boot',kind:'boot'},{id:'windows',kind:'windows',method:'purge',target_policy:'manual'},{id:'finish',kind:'finish'}],connections:[]};routineSelectedStep='windows';renderRoutineStep()");
 assert.match(f.element('routine-step-settings').innerHTML,/data-routine-field="verify_image" checked/);
 f.call("routineDraft.steps[1].verify_image=false;renderRoutineStep()");const html=f.element('routine-step-settings').innerHTML;assert.match(html,/data-routine-field="verify_image">/);assert.match(html,/0\.12\.16/);
});

test('routine handoff choices default to cleanup and preserve customer preferences',()=>{
 const f=fixture();f.element('routine-new').click();
 assert.equal(f.call('routineDraft.steps[1].finish_options.cleanup_client'),true);
 assert.match(f.element('routine-step-settings').innerHTML,/Save report and erasure PDFs on the customer desktop/);
 const change=f.element('routine-step-settings').listeners.change;
 change({target:{dataset:{routineFinish:'save_report_desktop'},type:'checkbox',checked:true}});
 change({target:{dataset:{routineFinish:'setup_mode'},type:'select-one',value:'local-account'}});
 change({target:{dataset:{routineFinish:'account_name'},type:'text',value:'Owner'}});
 assert.equal(f.call('routineDraft.steps[1].finish_options.account_name'),'Owner');
 assert.equal(f.call('routineDraft.steps[1].finish_options.save_report_desktop'),true);
 assert.match(f.element('routine-step-settings').innerHTML,/Local account name/);
 assert.equal(f.call('routineDirty'),true);
});
test('blank canvas panning preserves graph and zoom remains anchored',()=>{
 const f=fixture();f.element('routine-new').click();const flow=f.element('routine-flow');flow.scrollLeft=200;flow.scrollTop=150;
 f.call('routineDirty=false');let prevented=false;
 flow.listeners.pointerdown({button:0,clientX:100,clientY:100,pointerId:1,target:{closest:()=>null},preventDefault(){prevented=true;}});
 f.documentEvents.pointermove({clientX:150,clientY:125});f.documentEvents.pointerup();
 assert.equal(prevented,true);assert.equal(flow.scrollLeft,150);assert.equal(flow.scrollTop,125);assert.equal(f.call('routineDirty'),false);
 const worldX=(flow.scrollLeft+300)/f.call('routineZoom'),worldY=(flow.scrollTop+200)/f.call('routineZoom');
 flow.listeners.wheel({ctrlKey:true,deltaY:-100,clientX:300,clientY:200,preventDefault(){}});
 assert.ok(Math.abs((flow.scrollLeft+300)/f.call('routineZoom')-worldX)<.01);
 assert.ok(Math.abs((flow.scrollTop+200)/f.call('routineZoom')-worldY)<.01);
 assert.equal(f.call('routineDirty'),false);
});
test('new erasure steps choose nwipe and inline original PDF controls are scoped',()=>{
 const f=fixture();assert.equal(f.call("newRoutineStep('erase').method"),'nist_clear');
 const listener=f.element('report-viewer-drive-certificates').listeners.click;
 listener({target:{closest:()=>({dataset:{erasurePdf:'/api/workflow-runs/12345678-abcd-abcd-abcd-123456789012/certificate'}})}});
 assert.match(f.element('report-frame').src,/certificate\?download=false$/);
 listener({target:{closest:()=>({dataset:{erasurePdf:'https://example.com'}})}});
 assert.match(f.element('report-frame').src,/^\/api\/workflow-runs\//);
});

test('dashboard routine percentage follows connected step order and only completes on success',()=>{
 const f=fixture();f.context.run={stage:'windows-running',step:2,client_stage:'applying-image',install_percent:64,steps:[{id:'b',kind:'boot'},{id:'e',kind:'erase'},{id:'w',kind:'windows'},{id:'f',kind:'finish'}],connections:[{source:'b',target:'e',outcome:'success'},{source:'e',target:'w',outcome:'success'},{source:'w',target:'f',outcome:'success'}]};
 assert.equal(f.call('routineOverallPercent({...run,step:0})'),0);assert.equal(f.call('routineOverallPercent(run)'),66);
 assert.equal(f.call('routineOverallPercent({...run,client_stage:"recovery-registered",install_percent:null})'),75);
 assert.equal(f.call('routineOverallPercent({...run,stage:"failed",step:3})'),75);assert.equal(f.call('routineOverallPercent({...run,stage:"complete"})'),100);
 // Array order is not necessarily execution order, and disconnected failure steps are excluded.
 f.context.graph={stage:'windows-running',step:1,client_stage:'applying-image',install_percent:50,steps:[{id:'b',kind:'boot'},{id:'w',kind:'windows'},{id:'unused',kind:'checkpoint'},{id:'e',kind:'erase'},{id:'f',kind:'finish'}],connections:[{source:'b',target:'e',outcome:'success'},{source:'e',target:'w',outcome:'success'},{source:'w',target:'f',outcome:'success'}]};assert.equal(f.call('routineOverallPercent(graph)'),62);
});

test('overall wipe percentage covers all drives and does not leak into a later step',()=>{
 const f=fixture();f.context.run={stage:'erase-running',step:1,steps:[{kind:'boot'},{kind:'erase'},{kind:'windows'},{kind:'finish'}],erase_disks:[{stage:'complete',percent:100},{stage:'erasing',percent:50}]};
 assert.equal(f.call('routineOverallPercent(run)'),43);assert.equal(f.call('routineOverallPercent({...run,stage:"windows-boot",step:2})'),50);
});

test('dashboard status and drives render in independent aligned columns',()=>{
 const f=fixture();f.dashboard.routines={active:[{id:'run-1',name:'Test',mac:'fixture-mac',system_serial:'SERIAL-1',stage:'windows-running',client_stage:'applying-image',step:1,install_percent:64,steps:[{kind:'boot'},{kind:'windows'},{kind:'finish'}],erase_disks:[{model:'Samsung fixture',target:'serial:DRIVE-1',size:512000000000,stage:'complete'}]}]};f.call('renderSimpleClients(fixtureDashboard)');
 const html=f.element('dashboard-routine-clients').innerHTML,cells=html.match(/<td>.*?<\/td>/g);assert.match(html,/<th>Drives<\/th>/);assert.match(cells[1],/54% of routine/);assert.match(cells[2],/Installing Windows/);assert.match(cells[2],/64% installed/);assert.doesNotMatch(cells[2],/Samsung|DRIVE-1|Verified erasure/);assert.match(cells[3],/Samsung fixture/);
 f.call("dashboardDraft=normalizeDashboardLayout({columns:[{id:'status',visible:true},{id:'client',visible:true}]});renderSimpleClients(fixtureDashboard)");assert.doesNotMatch(f.element('dashboard-routine-clients').innerHTML,/<th>Drives/);assert.match(f.element('dashboard-routine-clients').innerHTML,/<th>Status<\/th><th>Client<\/th>/);
});

test('layout dimensions and columns recover older views and bound malformed preferences',()=>{
 const f=fixture();f.context.settingsLayout={widgets:[{id:'bandwidth',width:8,height:5000},{id:'clients',width:6,height:1},{id:'metrics',width:5,height:400}],columns:[{id:'status',visible:true},{id:'status',visible:false},{id:'invalid',visible:true}]};const layout=f.call('normalizeDashboardLayout(settingsLayout)');
 assert.equal(layout.widgets[0].width,8);assert.equal(layout.widgets[0].height,720);assert.equal(layout.widgets[1].height,220);assert.equal(layout.widgets[2].height,null);assert.equal(layout.columns[0].id,'status');assert.equal(layout.columns.filter(c=>c.visible).length,1);assert.equal(f.call('normalizeDashboardLayout({}).columns.filter(c=>c.visible).length'),6);
 f.call('dashboardDraft=normalizeDashboardLayout({});updateDashboardSize("bandwidth",9,381);');assert.equal(f.call('dashboardDraft.widgets.find(w=>w.id==="bandwidth").height'),380);assert.equal(f.call('dashboardDraft.widgets.find(w=>w.id==="bandwidth").width'),9);f.call('endDashboardEdit()');assert.equal(f.call('dashboardLayout().widgets.find(w=>w.id==="bandwidth").height'),null);
});

test('routine menu colour button opens the active colour script in the editor',()=>{
 const f=fixture();f.call('openedColourFile=null;requestOpenFile=(root,path)=>{openedColourFile={root,path};}');f.element('routine-menu-colours').listeners.click();assert.equal(f.call('openedColourFile.root'),'configuration');assert.equal(f.call('openedColourFile.path'),'routines-appearance.ipxe');
});

test('unattended erasure editor uses NIST presets and explains guarded Failure routing',()=>{
 const f=fixture();f.element('routine-new').listeners.click();f.call("addRoutineStep('erase',1);renderRoutineStep()");const html=f.element('routine-step-settings').innerHTML;assert.match(html,/NIST Clear/);assert.match(html,/NIST Purge/);assert.match(html,/Unsupported preflight follows Failure/);assert.match(html,/never automatically retries/);assert.ok(html.indexOf('value="nist_clear"')<html.indexOf('value="sanitize_block"'));assert.doesNotMatch(html,/data-routine-field="fallback_method"/);
});


test('new NIST Clear defaults fix complete verification without extra blanking',()=>{
 const f=fixture();assert.equal(f.call("newRoutineStep('erase').method"),'nist_clear');assert.equal(f.call("newRoutineStep('erase').final_blank"),false);
 f.element('routine-new-erase').click();f.call("Object.assign(routineDraft.steps[1],{method:'nist_purge',rounds:8,verification:'off',final_blank:true,prng:'mersenne'});normalizeRoutineWipe(routineDraft.steps[1]);renderRoutineStep()");
 assert.equal(f.call('routineDraft.steps[1].rounds'),1);assert.equal(f.call('routineDraft.steps[1].verification'),'all');assert.equal(f.call('routineDraft.steps[1].final_blank'),false);assert.equal(f.call('routineDraft.steps[1].prng'),'auto');
 assert.match(f.element('routine-step-settings').innerHTML,/data-routine-field="verification" disabled/);
});

test('additional erasure is a branch and Failure selection identifies methods',()=>{
 const f=fixture();f.element('routine-new-erase').click();f.call("routineDraft.steps[1].method='nist_purge';materializeRoutineEdges();addRoutineStep('erase');");
 assert.equal(f.call("routineDraft.steps.filter(s=>s.kind==='erase').length"),2);
 assert.equal(f.call("routineDraft.connections.find(e=>e.source===routineDraft.steps[1].id&&e.outcome==='success').target"),f.call('routineDraft.steps.at(-1).id'));
 assert.equal(f.call('routineDraft.connections.some(e=>e.target===routineDraft.steps[2].id)'),false);
 f.call("connectRoutine(routineDraft.steps[1].id,routineDraft.steps[2].id,'failure');routineSelectedStep=routineDraft.steps[1].id;renderRoutineStep()");
 assert.match(f.element('routine-step-settings').innerHTML,/ShredOS · NIST Clear/);assert.equal(f.call("routineEdges().filter(e=>e.outcome==='failure').length"),1);
});

test('Crypto Erase keeps explicit assurance, escapes it and blocks unapproved saves',async()=>{
 const f=fixture();f.element('routine-new-erase').click();f.call("Object.assign(routineDraft.steps[1],{method:'sanitize_crypto',crypto_assurance:'<script>key evidence retained</script>'});normalizeRoutineWipe(routineDraft.steps[1]);renderRoutineStep()");
 const html=f.element('routine-step-settings').innerHTML;assert.match(html,/Crypto Erase assurance/);assert.match(html,/data-routine-field="crypto_approved"/);assert.match(html,/maxlength="1000"/);assert.match(html,/&lt;script&gt;/);assert.doesNotMatch(html,/<script>/);
 await f.call('saveRoutine()');assert.equal(f.requests.length,0);assert.match(f.element('message').textContent,/needs approval/);
 f.call("routineDraft.steps[1].method='nist_clear';normalizeRoutineWipe(routineDraft.steps[1])");assert.equal(f.call('routineDraft.steps[1].crypto_approved'),false);assert.equal(f.call('routineDraft.steps[1].crypto_assurance'),'');
});


test('routine percentage follows the actual unsupported-to-Clear branch without dropping at Windows',()=>{
 const f=fixture();f.context.run={stage:'erase-running',step:3,step_history:[0,1,3],steps:[{id:'b',kind:'boot'},{id:'p',kind:'erase'},{id:'w',kind:'windows'},{id:'c',kind:'erase'},{id:'f',kind:'finish'}],connections:[{source:'b',target:'p',outcome:'success'},{source:'p',target:'w',outcome:'success'},{source:'p',target:'c',outcome:'failure'},{source:'c',target:'w',outcome:'success'},{source:'w',target:'f',outcome:'success'}],erase_disks:[{stage:'erasing',percent:0}]};
 assert.equal(f.call('routineOverallPercent(run)'),40);
 Object.assign(f.context.run,{stage:'windows-running',step:2,step_history:[0,1,3,2],client_stage:'applying-image',install_percent:0});assert.equal(f.call('routineOverallPercent(run)'),60);
 f.context.run.install_percent=50;assert.equal(f.call('routineOverallPercent(run)'),70);f.context.run.stage='complete';assert.equal(f.call('routineOverallPercent(run)'),100);
});

test('routine history excludes untaken branches and bounds malformed indices',()=>{
 const f=fixture();f.context.run={stage:'erase-running',step:1,step_history:[0,0,1,99,-1,'2',null],steps:[{id:'b',kind:'boot'},{id:'p',kind:'erase'},{id:'f',kind:'finish'},{id:'unused',kind:'erase'}],connections:[{source:'b',target:'p',outcome:'success'},{source:'p',target:'f',outcome:'success'},{source:'p',target:'unused',outcome:'failure'},{source:'unused',target:'f',outcome:'success'}],erase_disks:[{stage:'erasing',percent:50}]};
 assert.equal(f.call('routineOverallPercent(run)'),50);f.context.run.stage='failed';assert.equal(f.call('routineOverallPercent(run)'),50);
});


test('original nwipe follow-up PDF opens only the narrow retained phase route',()=>{
 const f=fixture();const listener=f.element('report-viewer-drive-certificates').listeners.click;
 const url='/api/workflow-runs/12345678-abcd-abcd-abcd-123456789012/certificate/followup';
 listener({target:{closest:()=>({dataset:{erasurePdf:url}})}});assert.equal(f.element('report-frame').src,url+'?download=false');
 listener({target:{closest:()=>({dataset:{erasurePdf:url+'/other'}})}});assert.equal(f.element('report-frame').src,url+'?download=false');
 listener({target:{closest:()=>({dataset:{erasurePdf:'https://other.example/certificate'}})}});assert.equal(f.element('report-frame').src,url+'?download=false');
 const html=f.call("assetReports([{report_id:'routine:fixture',name:'Fixture',system_serial:'SERIAL',stage:'complete',created:'2026-10-10',certificates:[{phase:'followup',url:'"+url+"',target:'serial:DISK'}]}])");
 assert.match(html,/ShredOS · followup · serial:DISK/);
});


test('NIST Purge PRNG follow-up defaults on and explicit opt-out persists through routine edits',()=>{
 const f=fixture();f.element('routine-new-erase').click();assert.equal(f.call("newRoutineStep('erase').prng_followup"),true);
 f.call("routineDraft.steps[1].method='nist_purge';normalizeRoutineWipe(routineDraft.steps[1]);renderRoutineStep()");let html=f.element('routine-step-settings').innerHTML;assert.match(html,/data-routine-field="prng_followup" checked/);assert.match(html,/Follow with verified PRNG wipe/);assert.match(html,/one full ChaCha20 PRNG pass/);
 f.element('routine-step-settings').listeners.change({target:{dataset:{routineField:'prng_followup'},type:'checkbox',checked:false,disabled:false}});
 assert.equal(f.call('routineDraft.steps[1].prng_followup'),false);html=f.element('routine-step-settings').innerHTML;assert.doesNotMatch(html,/data-routine-field="prng_followup" checked/);assert.match(html,/Only native sanitize runs/);assert.match(html,/Controller completion/);assert.doesNotMatch(html,/one full ChaCha20 PRNG pass/);assert.doesNotMatch(html,/data-routine-field="(?:rounds|prng|final_blank|io_mode|direction|sync_blocks)"/);
 f.call("routineDraft.steps[1].method='zero';normalizeRoutineWipe(routineDraft.steps[1]);routineDraft.steps[1].method='nist_purge';normalizeRoutineWipe(routineDraft.steps[1]);renderRoutineStep()");assert.equal(f.call('routineDraft.steps[1].prng_followup'),false);
});

test('Purge follow-up visibility is scoped and legacy non-boolean defaults normalize to strict booleans',()=>{
 const f=fixture();f.element('routine-new-erase').click();assert.doesNotMatch(f.element('routine-step-settings').innerHTML,/data-routine-field="prng_followup"/);
 f.call("Object.assign(routineDraft.steps[1],{method:'nist_purge',prng_followup:'false'});normalizeRoutineWipe(routineDraft.steps[1]);renderRoutineStep()");assert.equal(f.call('routineDraft.steps[1].prng_followup'),true);
 f.call("routineDraft.steps[1].method='sanitize_block';normalizeRoutineWipe(routineDraft.steps[1]);renderRoutineStep()");assert.doesNotMatch(f.element('routine-step-settings').innerHTML,/data-routine-field="prng_followup"/);
});

test('Reports defaults to newest activity and toggles its collapsed filter panel',()=>{
 const f=fixture();assert.equal(f.call('reportFilters.sort'),'processed');assert.equal(f.call('reportFilters.order'),'desc');
 assert.equal(f.element('report-sort').value,'processed');assert.equal(f.element('report-order').value,'desc');
 f.element('report-filter-panel').hidden=true;f.element('report-filter-toggle').click();assert.equal(f.element('report-filter-panel').hidden,false);assert.equal(f.element('report-filter-toggle').attributes['aria-expanded'],'true');
 f.element('report-filter-toggle').click();assert.equal(f.element('report-filter-panel').hidden,true);assert.match(f.call('reportQuery()'),/sort=processed&order=desc/);
});

test('Report headings toggle global sort, reset paging and preserve filters and selection',()=>{
 const f=fixture();f.call("reportPage=4;reportFilters.search='PC-123';selectedReports.set('hidden-job',{});refresh=()=>{};");
 f.call("sortReportsBy('serial')");assert.equal(f.call('reportFilters.order'),'asc');assert.equal(f.call('reportPage'),1);assert.equal(f.call('reportFilters.search'),'PC-123');assert.equal(f.call('selectedReports.size'),1);
 f.call("sortReportsBy('serial')");assert.equal(f.call('reportFilters.order'),'desc');assert.equal(f.element('report-order').value,'desc');
 f.call("sortReportsBy('routine')");assert.equal(f.call('reportFilters.sort'),'routine');assert.equal(f.call('reportFilters.order'),'asc');
 assert.match(f.call("reportColumn('Routine','routine')"),/aria-sort="ascending"/);assert.match(f.call("reportColumn('Routine','routine')"),/↑/);
 f.call("sortReportsBy('status');sortReportsBy('status')");assert.equal(f.call('reportFilters.order'),'desc');assert.match(f.call("reportColumn('Outcome','status')"),/aria-sort="descending"/);
 f.call("sortReportsBy('invalid')");assert.equal(f.call('reportFilters.sort'),'status');
});

test('Reports opens the original Shred PDF directly and can switch back to its job PDF',async()=>{
 const f=fixture(),url='/api/workflow-runs/12345678-abcd-abcd-abcd-123456789012/certificate';
 f.context.pdfData={specs:{system:{serial:'SYS-001'}},job:{id:'routine:fixture',created:'2026-10-10'},erasures:[{certificate_url:url,target:'serial:SSD-A',phase_certificates:[{phase:'followup',url:url+'/followup',target:'serial:SSD-A'}]},{certificate_url:url.replace('123456789012','123456789013'),target:'serial:SSD-B'}]};
 f.call('api=async()=>pdfData');
 await f.call("showReport('routine:fixture',false,true)");
 assert.equal(f.element('report-frame').src,url+'?download=false');assert.equal(f.element('report-viewer-download').href,url);
 assert.match(f.element('report-viewer-drive-certificates').innerHTML,/SSD-B/);assert.match(f.element('report-viewer-drive-certificates').innerHTML,/PRNG PDF/);
 f.element('report-viewer-job').click();assert.equal(f.element('report-frame').src,'/api/job-reports/routine%3Afixture/pdf');assert.match(f.element('report-viewer-download').href,/pdf\?download=true$/);
 f.context.pdfData.erasures=[];f.context.pdfData.erasure={certificate_url:url};
 await f.call("showReport('routine:fixture',false,true)");assert.equal(f.element('report-viewer-certificate').hidden,false);f.element('report-viewer-certificate').click();assert.equal(f.element('report-frame').src,url+'?download=false');
 f.context.pdfData.erasure=null;await f.call("showReport('routine:fixture',false,true)");assert.equal(f.element('report-viewer-certificate').hidden,true);assert.equal(f.element('report-frame').src,'/api/job-reports/routine%3Afixture/pdf');
});

test('Reports shows a separate Shred report button only when saved certificates exist',async()=>{
 const f=fixture();f.context.reportFixture={total:2,page:1,pages:1,reports:[{id:'with-shred',has_shred_report:true,stage:'complete',status_label:'Completed'},{id:'no-shred',has_shred_report:false,stage:'failed',status_label:'Failed'}]};f.call('renderReports(reportFixture)');
 assert.match(f.element('reports-table').innerHTML,/data-shred-report="with-shred">Shred report/);assert.doesNotMatch(f.element('reports-table').innerHTML,/data-shred-report="no-shred"/);
 f.call('showReport=async(...args)=>{window.openedReport=args;}');
 await f.element('reports-table').listeners.click({target:{closest:selector=>selector.includes('data-report-view')?{dataset:{shredReport:'with-shred'}}:null}});
 assert.deepEqual([...f.context.window.openedReport],['with-shred',true,true]);
});

test('activity less than a minute reads Now, including clock skew',()=>{const f=fixture();assert.equal(f.call("clientMinutesAgo('2026-10-10T00:00:00Z','2026-10-10T00:00:59Z')"),'Now');assert.equal(f.call("clientMinutesAgo('2026-10-10T00:01:00Z','2026-10-10T00:00:00Z')"),'Now');assert.equal(f.call("clientMinutesAgo('2026-10-10T00:00:00Z','2026-10-10T00:02:00Z')"),'2 minutes ago');});
test('explorer clipboard carries source tokens across folders and clears on sign out',()=>{const f=fixture();f.context.items=[{path:'reports/example.pdf',token:'version',name:'example.pdf',directory:false}];f.call("fileEntries=items;fileSelection=new Set(['reports/example.pdf']);clipboardFiles('move');fileLocation={root:'workspace',path:'config'}");assert.equal(f.call('fileClipboard.paths[0]'),'reports/example.pdf');assert.equal(f.call("fileClipboard.tokens['reports/example.pdf']"),'version');f.call('clearFileSession()');assert.equal(f.call('fileClipboard'),null);assert.equal(f.call('fileSelection.size'),0);assert.equal(f.call('fileLocation.path'),'');});

test('Explorer refresh preserves an address being typed',()=>{const f=fixture();f.element('file-address').value='/data/reports';f.element('file-address').focus();f.call("fileLocation={root:'workspace',path:'config'};fileEntries=[];renderFileTree()");assert.equal(f.element('file-address').value,'/data/reports');f.element('file-address').blur();f.call('renderFileTree()');assert.equal(f.element('file-address').value,'/data/config');});

test('ShredOS is built in with readiness status and no download or CA forms',()=>{
 const html=readFileSync(fileURLToPath(new URL('../repxe/static/index.html',import.meta.url)),'utf8');
 const workflows=readFileSync(fileURLToPath(new URL('../repxe/static/workflows.js',import.meta.url)),'utf8');
 assert.match(html,/Built-in ShredOS/);
 for(const id of ['routine-download','routine-ca','capability-download','capability-form','capability-ca'])assert.doesNotMatch(html,new RegExp('id="'+id+'"'));
 assert.doesNotMatch(workflows,/\/capabilities\/shredos\/install/);assert.doesNotMatch(routinesSource,/\/capabilities\/shredos\/install/);
});
