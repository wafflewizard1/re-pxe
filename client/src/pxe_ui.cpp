#include <repxe/technician.hpp>
#include <windows.h>
#include <array>
#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdio>
#include <map>
#include <memory>
#include <mutex>
#include <thread>

namespace repxe {
namespace {
constexpr UINT notice=WM_APP+71;
constexpr UINT_PTR countdown_timer=9, attention_timer=10;
enum class NextAction {None,LoadOptions,Review,Countdown,Restart};
enum {Version=201,Disk,Next,Install=205,Back,Status,Detail};
struct Message {std::string kind;Json value;};
std::wstring friendly(const std::string& stage){
    static const std::map<std::string,std::wstring> labels={{"waiting-for-input",L"Needs attention · choose the Windows disk"},{"choosing",L"Choose Windows and a disk"},{"checking-disk",L"Checking disk"},{"confirming",L"Confirm installation"},{"countdown",L"Starting installation"},{"paused",L"Installation cancelled"},{"failed",L"Stopped"},{"preflight",L"Checking disk"},{"preparing-disk",L"Preparing disk"},{"disk-partitioned",L"Preparing disk"},{"disk-prepared",L"Disk ready"},{"downloading",L"Downloading Windows"},{"verifying-image",L"Verifying image"},{"installing-offline-drivers",L"Preparing drivers"},{"applying-image",L"Installing Windows"},{"image-applied",L"Windows installed"},{"boot-configured",L"Preparing boot files"},{"recovery-registered",L"Preparing recovery"},{"drivers-staged",L"Preparing drivers"},{"agent-staged",L"Preparing Windows startup"},{"awaiting-windows",L"Ready to restart"}};
    auto found=labels.find(stage);return found==labels.end()?L"Working…":found->second;
}
struct Ui {
    HWND window{};HFONT font{},heading{};std::map<int,HWND> controls;
    std::thread worker,heartbeat;std::atomic<bool> closing=false;bool busy=false,reviewed=false,finished=false;
    bool attention_on=false;bool automatic=false,countdown_running=false,countdown_due=false;ULONGLONG countdown_end=0;NextAction next_action=NextAction::None;Json automation;
    std::mutex state_mutex;Json live={{"stage","choosing"},{"received_bytes",0},{"total_bytes",0},{"error",""}};
    std::string origin,token,client_id;Json disks=Json::array(),options=Json::array(),selection,config,metadata,result;
    std::filesystem::path boot,config_file;
    ~Ui(){closing=true;if(worker.joinable())worker.join();if(heartbeat.joinable())heartbeat.join();if(!token.empty())SecureZeroMemory(token.data(),token.size());DeleteObject(font);DeleteObject(heading);}
    void set(int id,const std::wstring& value){SetWindowTextW(controls.at(id),value.c_str());}
    void post(const std::string& kind,const Json& value){auto m=new Message{kind,value};if(!PostMessageW(window,notice,0,reinterpret_cast<LPARAM>(m)))delete m;}
    void report(const std::string& stage,unsigned long long received=0,unsigned long long total=0,const std::string& error="",int percent=-1){
        {std::lock_guard lock(state_mutex);live["stage"]=stage;if(total){live["received_bytes"]=received;live["total_bytes"]=total;}live["error"]=error;live.erase("progress_percent");if(stage=="applying-image" && percent>=0)live["progress_percent"]=percent;}
        post("progress",{{"stage",stage},{"received",received},{"total",total},{"percent",percent}});
    }
    void enable(){
        EnableWindow(controls[Version],FALSE);EnableWindow(controls[Disk],!automatic&&!busy&&!reviewed&&!finished);
        EnableWindow(controls[Next],!busy&&!reviewed&&!finished&&SendMessageW(controls[Version],CB_GETCURSEL,0,0)>=0&&SendMessageW(controls[Disk],LB_GETCURSEL,0,0)>=0);
        EnableWindow(controls[Install],!busy&&(finished||(reviewed&&(!automatic||!countdown_running))));
        EnableWindow(controls[Back],!busy&&reviewed&&!finished&&(!automatic||countdown_running));
    }
    void start(std::function<void()> operation){if(busy)return;if(worker.joinable())worker.join();busy=true;enable();worker=std::thread([this,operation]{try{operation();}catch(const std::exception& e){report("failed",0,0,e.what());post("error",{{"message",e.what()}});}post("idle",Json::object());});}
    void poll(){heartbeat=std::thread([this]{while(!closing){Json value;{std::lock_guard lock(state_mutex);value=live;}if(value.at("stage")=="installing-offline-drivers")value["stage"]="image-applied";try{auto response=pxe_api(origin,token,"/api/pxe/heartbeat",&value);if(value.at("stage")=="waiting-for-input" && response.contains("automation"))post("disk-decision",response.at("automation"));}catch(...){}for(int i=0;i<30&&!closing;++i)std::this_thread::sleep_for(std::chrono::milliseconds(100));}});}
};
HWND control(Ui& u,const wchar_t* type,const std::wstring& text,DWORD style,int x,int y,int width,int height,int id){auto h=CreateWindowExW((style&WS_BORDER)?WS_EX_CLIENTEDGE:0,type,text.c_str(),WS_CHILD|WS_VISIBLE|style,x,y,width,height,u.window,reinterpret_cast<HMENU>(static_cast<INT_PTR>(id)),GetModuleHandleW(nullptr),nullptr);if(!h)throw std::runtime_error("Create PXE control");SendMessageW(h,WM_SETFONT,reinterpret_cast<WPARAM>(u.font),TRUE);u.controls[id]=h;return h;}
void load_options(Ui* u,const std::string& target){
    validate_pxe_automation(u->automation,u->disks);
    Json request={{"target_identity",target}};auto options=pxe_api(u->origin,u->token,"/api/pxe/options",&request);
    auto chosen=u->automation.at("profileId");
    if(std::none_of(options.at("choices").begin(),options.at("choices").end(),[&](const Json& p){return p.at("id")==chosen;}))
        throw std::runtime_error("No enabled configuration supports this client or disk. Check its hardware policy and disk assignment.");
    u->post("options",options);
}
void begin_countdown(Ui* u){u->start([u]{Json request={{"action","start"}};u->post("countdown",pxe_api(u->origin,u->token,"/api/pxe/countdown",&request));});}
void countdown_label(Ui* u){auto now=GetTickCount64();auto seconds=now>=u->countdown_end?0ULL:(u->countdown_end-now+999)/1000;u->set(Status,L"Starting in "+std::to_wstring(seconds)+L" seconds");}
LRESULT CALLBACK procedure(HWND h,UINT m,WPARAM w,LPARAM l){
    auto u=reinterpret_cast<Ui*>(GetWindowLongPtrW(h,GWLP_USERDATA));if(m==WM_NCCREATE){u=static_cast<Ui*>(reinterpret_cast<CREATESTRUCTW*>(l)->lpCreateParams);u->window=h;SetWindowLongPtrW(h,GWLP_USERDATA,reinterpret_cast<LONG_PTR>(u));}if(!u)return DefWindowProcW(h,m,w,l);
    try{
        if(m==WM_CREATE){u->font=CreateFontW(-18,0,0,0,FW_NORMAL,0,0,0,DEFAULT_CHARSET,0,0,CLEARTYPE_QUALITY,0,L"Segoe UI");u->heading=CreateFontW(-32,0,0,0,FW_SEMIBOLD,0,0,0,DEFAULT_CHARSET,0,0,CLEARTYPE_QUALITY,0,L"Segoe UI");
            auto title=control(*u,L"STATIC",L"Install Windows",0,40,32,650,44,301);SendMessageW(title,WM_SETFONT,reinterpret_cast<WPARAM>(u->heading),TRUE);
            control(*u,L"STATIC",L"Windows version",0,40,112,650,24,302);control(*u,L"COMBOBOX",L"",WS_TABSTOP|CBS_DROPDOWNLIST|WS_VSCROLL,40,144,680,200,Version);
            control(*u,L"STATIC",L"Target disk",0,40,208,650,24,303);control(*u,L"LISTBOX",L"",WS_TABSTOP|WS_BORDER|WS_VSCROLL|WS_HSCROLL|LBS_NOTIFY|LBS_NOINTEGRALHEIGHT,40,240,680,108,Disk);
            SendMessageW(u->controls[Disk],LB_SETHORIZONTALEXTENT,1400,0);
            control(*u,L"BUTTON",L"Continue",WS_TABSTOP,540,354,180,40,Next);
            control(*u,L"STATIC",L"Connecting…",0,40,404,680,30,Status);control(*u,L"STATIC",L"",0,40,440,680,76,Detail);
            control(*u,L"BUTTON",L"Back",WS_TABSTOP,350,522,170,42,Back);control(*u,L"BUTTON",L"Install",WS_TABSTOP,540,522,180,42,Install);
            for(int id:{Back,Install})ShowWindow(u->controls[id],SW_HIDE);u->enable();return 0;
        }
        if(m==WM_COMMAND){auto id=LOWORD(w),notification=HIWORD(w);if(u->busy)return 0;
            if((id==Next||id==Back||id==Install)&&notification!=BN_CLICKED)return 0;
            if(id==Disk&&notification==LBN_SELCHANGE){auto n=SendMessageW(u->controls[Disk],LB_GETCURSEL,0,0);u->reviewed=false;
                if(n>=0){const auto& d=u->disks.at(n);auto size=d.at("sizeBytes").get<unsigned long long>();u->set(Detail,wide(d.value("model","Disk"))+L" · "+std::to_wstring(size/1000000000ULL)+L" GB ("+std::to_wstring(size/GiB)+L" GiB)\r\n"+wide(d.at("identity"))+L"\r\nArrow keys choose a drive. Enter or Continue reviews it. Tab moves to the next control.");}
                u->enable();return 0;}

            if(id==Version&&notification==CBN_SELCHANGE){u->enable();return 0;}
            if(id==Back&&u->automatic){KillTimer(h,countdown_timer);u->countdown_running=false;u->countdown_due=false;u->report("paused");u->start([u]{Json request={{"action","cancel"}};pxe_api(u->origin,u->token,"/api/pxe/countdown",&request);u->post("paused",Json::object());});return 0;}
            if(id==Back){u->reviewed=false;for(int c:{Back,Install})ShowWindow(u->controls[c],SW_HIDE);ShowWindow(u->controls[Next],SW_SHOW);u->set(Detail,L"");u->report("choosing");u->enable();SetFocus(u->controls[Disk]);return 0;}
            if(id==Next){auto di=SendMessageW(u->controls[Disk],LB_GETCURSEL,0,0),vi=SendMessageW(u->controls[Version],CB_GETCURSEL,0,0);if(di<0||vi<0)throw std::runtime_error("Choose Windows and a disk");auto target=u->disks.at(di).at("identity").get<std::string>(),profile=u->automation.at("profileId").get<std::string>();
                u->start([u,target,profile]{u->report("checking-disk");load_options(u,target);Json request={{"profile_id",profile},{"target_identity",target}};auto selected=pxe_api(u->origin,u->token,"/api/pxe/select",&request);
                    const auto policy=u->automation.at("targetDiskPolicy").get<std::string>();
                    if(selected.at("target_policy")!=policy || selected.at("target_identity")!=target)throw std::runtime_error("Server disk policy or target changed; reboot to load current PXE settings");
                    auto meta=pxe_image_metadata(selected);
                    if(selected.contains("verifyImage")&&!selected.at("verifyImage").is_boolean())throw std::runtime_error("Invalid image verification policy");
                    if(!selected.at("driverManifest").is_null()){auto facts=inventory();facts["build"]=meta.at("selectedImage").at("build");if(!driver_package_matches(selected.at("driverManifest"),facts))throw std::runtime_error("No reviewed driver set matches this device");}
                    auto root=u->boot.parent_path()/L"re-pxe-PXE";std::filesystem::create_directory(root);auto session=root/wide(u->client_id);std::filesystem::create_directory(session);
                    Json config={{"schemaVersion",1},{"image",utf8((session/L"pending.wim").wstring())},{"sha256",selected.at("profile").at("image_sha256")},{"imageIndex",selected.at("profile").at("image_index")},{"targetIdentity",target},{"scratchDirectory",utf8(session.wstring())},{"jobDirectory",utf8(session.wstring())},{"recoveryMiB",selected.at("profile").at("recovery_mib")},{"targetPolicy",policy},{"bootEx",selected.at("profile").at("boot_ex")}};
                    if(selected.contains("answerFiles"))config["answerFiles"]=selected.at("answerFiles");
                    if(selected.contains("finishOptions"))config["finishOptions"]=selected.at("finishOptions");
                    config["driverConsole"]=true;config["verifyImage"]=selected.value("verifyImage",true);
                    config["offlineDriverMode"]="all-reviewed";
                    if(selected.at("profile").value("driver_source","")=="automatic")config["windowsUpdate"]={{"timeoutSeconds",selected.at("profile").at("windows_update_timeout_seconds")},{"maxAttempts",selected.at("profile").at("windows_update_max_attempts")}};
                    auto path=session/L"review.json";write_json(path,config);auto plan=pxe_preflight(config,path,meta);u->post("review",{{"selection",selected},{"config",config},{"metadata",meta},{"path",utf8(path.wstring())},{"plan",plan}});});return 0;
            }
            if(id==Install){
                if(u->finished){u->set(Status,L"Restarting into Windows…");u->start([u]{try{restart_system(u->config_file.parent_path()/(L"restart-"+std::to_wstring(GetTickCount64())+L".log"));}catch(const std::exception& e){u->post("restart-error",{{"message",e.what()}});}});return 0;}
                if(u->automatic&&!u->countdown_due){if(!u->reviewed)throw std::runtime_error("Check the disk before starting");begin_countdown(u);return 0;}
                if(!u->reviewed)throw std::runtime_error("Review the selected disk before installing");
                KillTimer(h,countdown_timer);u->countdown_running=false;u->countdown_due=false;u->reviewed=false;
                u->start([u]{
                    Json request=Json::object();auto started=pxe_api(u->origin,u->token,"/api/pxe/start",&request);auto cap=started.at("reportCredential");auto queue=u->config_file.parent_path()/L"pxe-events.json";
                    Json outbox={{"schemaVersion",1},{"jobId",started.at("id")},{"events",Json::array()},{"acknowledged",0}};
                    auto save=[&]{write_json(queue,outbox);};
                    auto synchronize=[&]{try{auto n=outbox.at("acknowledged").get<size_t>();for(;n<outbox.at("events").size();++n){auto e=outbox.at("events").at(n);auto ack=pxe_api(u->origin,u->token,"/api/pxe/event",&e);if(ack.at("id")!=started.at("id")||ack.at("stage")!=e.at("stage"))throw std::runtime_error("Event binding differs");outbox["acknowledged"]=n+1;save();}}catch(const std::exception& e){outbox["lastError"]=e.what();save();}};
                    auto observe=[&](const Json& state){auto stage=state.at("stage").get<std::string>();u->report(stage,0,0,"",state.value("progressPercent",-1));
                        if(state.contains("pxeStaging"))queue=std::filesystem::path(wide(state.at("pxeStaging")))/L"jobs\\pxe-events.json";
                        static const std::array<std::string,8> event_stages={"preflight","disk-prepared","image-applied","boot-configured","drivers-staged","agent-staged","awaiting-windows","failed"};
                        if(std::find(event_stages.begin(),event_stages.end(),stage)!=event_stages.end()){outbox["events"].push_back({{"stage",stage},{"detail","Native PXE checkpoint"},{"event_id",u->client_id+"-"+std::to_string(outbox["events"].size())}});save();synchronize();}
                    };
                    auto source=[&](Json& c,const std::filesystem::path& root){auto pending=root/L"image.wim.incomplete",image=root/L"image.wim";auto size=u->selection.at("image").at("size").get<unsigned long long>();
                        u->report("downloading",0,size);pxe_download(u->origin,u->token,"/api/pxe/image",pending,size,[&](unsigned long long n){u->report("downloading",n,size);});if(c.value("verifyImage",true))u->report("verifying-image",size,size);
                        restrict_state_path(pending);std::filesystem::rename(pending,image);c["image"]=utf8(image.wstring());
                        auto manifest=u->selection.at("driverManifest");if(!manifest.is_null()){validate_driver_manifest(manifest);auto directory=root/L"drivers";std::filesystem::create_directory(directory);restrict_state_path(directory);
                            for(size_t i=0;i<manifest.at("files").size();++i){auto f=manifest.at("files").at(i);auto path=directory/wide(f.at("path"));std::filesystem::create_directories(path.parent_path());pxe_download(u->origin,u->token,"/api/pxe/driver/"+std::to_string(i),path,f.at("size"));restrict_state_path(path);if(sha256(path)!=f.at("sha256").get<std::string>())throw std::runtime_error("Driver file hash differs");}
                            auto path=directory/L"manifest.json";write_json(path,manifest);restrict_state_path(path);c["driverManifest"]=utf8(path.wstring());c["driverManifestSha256"]=sha256(path);
                        }
                        auto secret=root/L"report-secret.json";write_json(secret,{{"schemaVersion",1},{"endpoint",u->origin+cap.at("path").get<std::string>()},{"token",cap.at("token")}});restrict_state_path(secret);c["reportDelivery"]=utf8(secret.wstring());
                    };
                    try{auto result=pxe_deploy(u->config,u->config_file,u->metadata,source,observe);u->post("result",result);}
                    catch(...){if(outbox.at("events").empty()||outbox.at("events").back().at("stage")!="failed"){outbox["events"].push_back({{"stage","failed"},{"detail","Native PXE execution stopped"},{"event_id",u->client_id+"-"+std::to_string(outbox["events"].size())}});save();synchronize();}throw;}
                });return 0;
            }
        }
        if(m==WM_TIMER&&w==countdown_timer){
            if(u->countdown_running){countdown_label(u);if(GetTickCount64()>=u->countdown_end&&!u->busy){KillTimer(h,countdown_timer);u->countdown_running=false;u->countdown_due=true;PostMessageW(h,WM_COMMAND,Install,0);}}
            return 0;
        }
        if(m==WM_TIMER && w==attention_timer){
            Json live;{std::lock_guard lock(u->state_mutex);live=u->live;}
            if(live.at("stage")!="waiting-for-input"){KillTimer(h,attention_timer);u->set(303,L"Target disk");SetWindowTextW(h,L"re-pxe · Install Windows");}
            else{u->attention_on=!u->attention_on;u->set(303,u->attention_on?L"! NEEDS ATTENTION · select the Windows disk":L"Target disk · waiting for your selection");}
            return 0;
        }
        if(m==notice){std::unique_ptr<Message> message(reinterpret_cast<Message*>(l));auto& v=message->value;
            if(message->kind=="disk-decision" && !u->busy && !u->reviewed && !u->automatic && v.value("automaticDiskSelection",false)){
                validate_pxe_automation(v,u->disks);u->automation=v;u->automatic=true;
                auto index=pxe_target_index(u->disks,v);SendMessageW(u->controls[Disk],LB_SETCURSEL,index,0);
                u->report("checking-disk");ShowWindow(u->controls[Next],SW_HIDE);
                auto target=u->disks.at(index).at("identity").get<std::string>();u->start([u,target]{load_options(u,target);});return 0;
            }
            if(message->kind=="idle"){u->busy=false;u->enable();if(!u->automatic&&u->reviewed&&!u->finished){SetFocus(u->controls[Install]);}if(!u->automatic&&!u->reviewed&&!u->finished){PostMessageW(h,WM_COMMAND,MAKEWPARAM(Disk,LBN_SELCHANGE),0);SetFocus(u->controls[Disk]);}auto action=u->next_action;u->next_action=NextAction::None;
                if(action==NextAction::LoadOptions){auto target=u->disks.at(pxe_target_index(u->disks,u->automation)).at("identity").get<std::string>();u->start([u,target]{load_options(u,target);});}
                if(action==NextAction::Review)PostMessageW(h,WM_COMMAND,Next,0);
                if(action==NextAction::Countdown)begin_countdown(u);
                if(action==NextAction::Restart)PostMessageW(h,WM_COMMAND,Install,0);
                return 0;
            }
            if(message->kind=="ready"){u->origin=v.at("origin");u->token=v.at("session").at("token");u->client_id=v.at("session").at("clientId");u->disks=v.at("disks");u->automation=v.at("session").at("automation");u->automatic=u->automation.at("automaticDiskSelection").get<bool>();for(const auto& d:u->disks){auto size=d.at("sizeBytes").get<unsigned long long>();auto label=L"Disk "+std::to_wstring(d.value("number",0))+L" · "+wide(d.value("model","Disk"))+L" · "+std::to_wstring(size/1000000000ULL)+L" GB ("+std::to_wstring(size/GiB)+L" GiB) · "+wide(d.at("identity"));SendMessageW(u->controls[Disk],LB_ADDSTRING,0,reinterpret_cast<LPARAM>(label.c_str()));}auto target_index=pxe_target_index(u->disks,u->automation);SendMessageW(u->controls[Disk],LB_SETCURSEL,target_index,0);u->next_action=NextAction::LoadOptions;if(u->automation.value("manualDiskSelection",false)&&!u->automatic){u->next_action=NextAction::None;SendMessageW(u->controls[Version],CB_ADDSTRING,0,reinterpret_cast<LPARAM>(L"Routine Windows preset · review the selected drive"));SendMessageW(u->controls[Version],CB_SETCURSEL,0,0);}if(u->automatic)ShowWindow(u->controls[Next],SW_HIDE);u->poll();auto& secret=v["session"]["token"].get_ref<std::string&>();SecureZeroMemory(secret.data(),secret.size());u->set(Status,L"Choose a drive with the arrow keys, then press Enter to review.");if(u->automation.value("manualDiskSelection",false)){u->report("waiting-for-input");SetTimer(h,attention_timer,1000,nullptr);SetWindowTextW(h,L"re-pxe · Needs attention");FLASHWINFO flash{sizeof(flash),h,FLASHW_ALL|FLASHW_TIMERNOFG,0,0};FlashWindowEx(&flash);}}
            if(message->kind=="options"){u->options=v.at("choices");SendMessageW(u->controls[Version],CB_RESETCONTENT,0,0);for(const auto& p:u->options){auto label=wide(p.at("windows_label"));SendMessageW(u->controls[Version],CB_ADDSTRING,0,reinterpret_cast<LPARAM>(label.c_str()));}for(size_t i=0;i<u->options.size();++i)if(u->options.at(i).at("id")==u->automation.at("profileId"))SendMessageW(u->controls[Version],CB_SETCURSEL,i,0);
                u->set(Status,u->automation.value("manualDiskSelection",false)&&!u->automatic?L"Needs attention · choose a disk here or on the portal":L"Configuration selected by the dashboard.");if(u->automatic)u->next_action=NextAction::Review;}
            if(message->kind=="review"){u->selection=v.at("selection");u->config=v.at("config");u->metadata=v.at("metadata");u->config_file=wide(v.at("path"));u->reviewed=true;u->set(Status,L"Confirm installation");auto warning=u->config.at("targetPolicy")=="format-selected-disk"?L"All partitions and data on this disk will be erased.":L"Only a fully zeroed blank disk can be changed.";u->set(Detail,std::wstring(warning)+L"\r\n"+wide(u->config.at("targetIdentity"))+L"\r\nPress Install to continue.");ShowWindow(u->controls[Next],SW_HIDE);for(int id:{Back,Install})ShowWindow(u->controls[id],SW_SHOW);u->report("confirming");SetFocus(u->controls[Install]);
                if(u->automatic){ShowWindow(u->controls[Install],SW_HIDE);u->set(Back,L"Cancel");u->set(Detail,wide(u->selection.at("profile").at("windows_label"))+L"\r\n"+warning+L"\r\n"+wide(u->config.at("targetIdentity")));u->next_action=NextAction::Countdown;}
            }
            if(message->kind=="countdown"){auto delay=v.at("startDelaySeconds").get<int>();if(delay!=u->automation.at("startDelaySeconds"))throw std::runtime_error("Server countdown policy changed");
                u->countdown_end=GetTickCount64()+static_cast<ULONGLONG>(delay)*1000;u->countdown_running=true;u->countdown_due=false;
                ShowWindow(u->controls[Install],SW_HIDE);ShowWindow(u->controls[Back],SW_SHOW);u->report("countdown");countdown_label(u);
                if(!SetTimer(h,countdown_timer,200,nullptr))throw std::runtime_error("Start installation countdown timer");
            }
            if(message->kind=="paused"){u->set(Status,L"Installation cancelled");u->set(Install,L"Start countdown");ShowWindow(u->controls[Back],SW_HIDE);ShowWindow(u->controls[Install],SW_SHOW);}
            if(message->kind=="progress"){auto stage=v.at("stage").get<std::string>();auto percent=v.value("percent",-1);u->set(Status,friendly(stage)+(stage=="applying-image" && percent>=0?L" - "+std::to_wstring(percent)+L"%":L""));
                if(stage=="applying-image")u->set(Detail,percent>=0?L"Applying Windows image: "+std::to_wstring(percent)+L"%":L"Applying Windows image...");
                else if(v.value("total",0ULL))u->set(Detail,L"Downloaded "+std::to_wstring(v.at("received").get<unsigned long long>()*100/v.at("total").get<unsigned long long>())+L"%");
                else if(stage!="countdown" && stage!="failed" && stage!="confirming" && stage!="waiting-for-input" && stage!="choosing")u->set(Detail,L"");}
            if(message->kind=="result"){if(v.at("stage")!="awaiting-windows")throw std::runtime_error("Installation did not reach its saved Windows startup checkpoint");u->finished=true;u->result=v;u->set(Status,L"Windows installed successfully");u->set(Detail,L"Restarting automatically into Windows for driver installation.");u->set(Install,L"Restart");ShowWindow(u->controls[Back],SW_HIDE);ShowWindow(u->controls[Install],SW_SHOW);u->next_action=NextAction::Restart;}
            if(message->kind=="restart-error"){u->set(Status,L"Windows installed; restart needs attention");u->set(Detail,wide(v.at("message"))+L"\r\nUse Restart to retry. The installation result is saved.");}
            if(message->kind=="error"){KillTimer(h,countdown_timer);u->countdown_running=false;u->countdown_due=false;u->next_action=NextAction::None;u->reviewed=false;u->set(Status,L"Stopped");u->set(Detail,wide(v.at("message")));ShowWindow(u->controls[Install],SW_HIDE);ShowWindow(u->controls[Back],SW_HIDE);}
            u->enable();return 0;
        }
        if(m==WM_CLOSE){if(u->busy)return 0;KillTimer(h,countdown_timer);u->countdown_running=false;DestroyWindow(h);return 0;}if(m==WM_DESTROY){PostQuitMessage(0);return 0;}
    }catch(const std::exception& e){MessageBoxW(h,wide(e.what()).c_str(),L"re-pxe",MB_OK|MB_ICONERROR);}return DefWindowProcW(h,m,w,l);
}
}
int pxe_ui(const std::filesystem::path& boot){
    if(!inventory().at("environment").value("winpe",false))throw std::runtime_error("PXE client requires WinPE");
    Ui u;u.boot=boot;SetProcessDPIAware();WNDCLASSW cls{};cls.hInstance=GetModuleHandleW(nullptr);cls.lpfnWndProc=procedure;cls.lpszClassName=L"repxePXE";cls.hCursor=LoadCursorW(nullptr,IDC_ARROW);cls.hbrBackground=reinterpret_cast<HBRUSH>(COLOR_WINDOW+1);RegisterClassW(&cls);
    auto h=CreateWindowExW(WS_EX_CONTROLPARENT,cls.lpszClassName,L"re-pxe",WS_OVERLAPPED|WS_CAPTION|WS_SYSMENU,100,20,780,700,nullptr,nullptr,cls.hInstance,&u);if(!h)throw std::runtime_error("Open PXE client");auto console=GetConsoleWindow();if(console)ShowWindow(console,SW_HIDE);ShowWindow(h,SW_SHOW);UpdateWindow(h);
    u.start([&u]{auto boot=read_json(u.boot);if(boot.at("schemaVersion")!=1)throw std::runtime_error("Invalid boot ticket");auto origin=boot.at("server").get<std::string>();auto ticket=boot.at("ticket").get<std::string>();validate_server_origin(origin);
        // PXE supplies the server clock just as network boot supplies the runtime.
        // Correct only the disposable WinPE clock; HTTPS chain/hostname checks still apply.
        if(boot.contains("serverUtc")){SYSTEMTIME clock{};unsigned year,month,day,hour,minute,second;auto value=boot.at("serverUtc").get<std::string>();if(std::sscanf(value.c_str(),"%u-%u-%uT%u:%u:%u",&year,&month,&day,&hour,&minute,&second)!=6 || year<2025 || year>2099 || month<1 || month>12 || day<1 || day>31 || hour>23 || minute>59 || second>59)throw std::runtime_error("Invalid server boot clock");TIME_ZONE_INFORMATION zone{};std::copy_n(L"UTC",4,zone.StandardName);if(SetTimeZoneInformation(&zone)==0)throw std::runtime_error("Set WinPE UTC timezone");clock.wYear=year;clock.wMonth=month;clock.wDay=day;clock.wHour=hour;clock.wMinute=minute;clock.wSecond=second;if(!SetSystemTime(&clock))throw std::runtime_error("Set WinPE network boot clock");}
        auto inv=inventory();auto disks=pxe_disks(inv);Json targets=Json::array();for(const auto& d:disks){targets.push_back({{"identity",d.at("identity")},{"size_bytes",d.at("sizeBytes")},{"model",d.at("model")}});}if(disks.empty())throw std::runtime_error("No eligible target disk");
        Json request={{"inventory",inv.at("specs")},{"manufacturer",inv.value("manufacturer","")},{"model",inv.value("model","")},{"client_version",inv.value("toolVersion","")},{"targets",targets},{"managed_selection",true}};auto session=pxe_api(origin,ticket,"/api/pxe/enroll",&request);SecureZeroMemory(ticket.data(),ticket.size());std::filesystem::remove(u.boot);u.post("ready",{{"origin",origin},{"session",session},{"disks",disks}});
    });MSG message{};while(GetMessageW(&message,nullptr,0,0)>0){
        if(message.message==WM_KEYDOWN&&message.wParam==VK_TAB){auto next=GetNextDlgTabItem(h,GetFocus(),GetKeyState(VK_SHIFT)<0);if(next)SetFocus(next);continue;}
        if(message.message==WM_KEYDOWN&&message.wParam==VK_RETURN&&(GetFocus()==u.controls[Disk]||GetFocus()==u.controls[Next])&&IsWindowEnabled(u.controls[Next])){PostMessageW(h,WM_COMMAND,Next,0);continue;}
        if(message.message==WM_KEYDOWN&&message.wParam==VK_RETURN){auto focus=GetFocus();bool used=false;for(int id:{Back,Install})if(focus==u.controls[id]&&IsWindowEnabled(focus)){PostMessageW(h,WM_COMMAND,id,0);used=true;break;}if(used)continue;}
        if(!IsDialogMessageW(h,&message)){TranslateMessage(&message);DispatchMessageW(&message);}}if(console)ShowWindow(console,SW_SHOW);return 0;
}
}
