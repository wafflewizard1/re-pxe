#include <repxe/technician.hpp>
#include <repxe/operation_lock.hpp>
#include <windows.h>
#include <commdlg.h>
#include <shellapi.h>
#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <map>
#include <memory>
#include <thread>

namespace repxe {
namespace {
constexpr UINT update=WM_APP+81;
constexpr UINT_PTR countdown=17;
enum{Transcript=501,Command,Run,Install,Upload,Export,UpdateDrivers,Scan,Restart,Finish,Logs,Cancel,Status,Local,Shell,Retry};
struct Notice{std::string kind;Json value;};
struct Console{
    HWND window{};HFONT font{},mono{};std::map<int,HWND> controls;std::thread worker,heartbeat;
    std::atomic<bool> closing=false;bool busy=false,initialized=false,counting=false;ULONGLONG deadline=0;Json state;
    ~Console(){closing=true;if(worker.joinable())worker.join();if(heartbeat.joinable())heartbeat.join();DeleteObject(font);DeleteObject(mono);}
    void set(int id,const std::wstring& value){SetWindowTextW(controls.at(id),value.c_str());}
    void line(const std::string& text){auto control=controls.at(Transcript);auto end=GetWindowTextLengthW(control);if(end>180000){SendMessageW(control,EM_SETSEL,0,60000);SendMessageW(control,EM_REPLACESEL,FALSE,reinterpret_cast<LPARAM>(L""));end=GetWindowTextLengthW(control);}SendMessageW(control,EM_SETSEL,end,end);auto value=wide(text)+L"\r\n";SendMessageW(control,EM_REPLACESEL,FALSE,reinterpret_cast<LPARAM>(value.c_str()));SendMessageW(control,EM_SCROLLCARET,0,0);}
    void post(const std::string& kind,const Json& value){auto p=new Notice{kind,value};if(!PostMessageW(window,update,0,reinterpret_cast<LPARAM>(p)))delete p;}
    void enable(){for(int id:{Command,Run,Install,Upload,Export,UpdateDrivers,Scan,Restart,Finish,Logs,Local,Shell,Retry})EnableWindow(controls.at(id),!busy&&initialized&&!counting);EnableWindow(controls.at(Cancel),!busy&&counting);EnableWindow(controls.at(Retry),!busy&&initialized&&!counting&&state.contains("pendingDriverUpload"));if(initialized&&!state.is_null()){
        const bool sealed=state.value("stage",std::string())=="complete";
        if(sealed)for(int id:{Install,Upload,Export,UpdateDrivers,Scan,Local,Retry})EnableWindow(controls.at(id),FALSE);
        set(Finish,sealed?L"Retry Finish":L"Finish setup");set(Restart,sealed?L"Shut down":L"Restart");
        EnableWindow(controls.at(Finish),!busy&&driver_finish_available(state));}}
    void start(const std::string& action,const std::filesystem::path& path={}){if(busy)return;KillTimer(window,countdown);counting=false;if(worker.joinable())worker.join();busy=true;enable();set(Status,L"Working: "+wide(action));worker=std::thread([this,action,path]{try{auto result=driver_session_action(action,path,[this](const std::string& line){post("line",line);});if(action=="catalog")post("catalog",result);else if(action=="upload"||action=="export-upload"||action=="retry-upload")post("uploaded",result);else post("state",result);}catch(const std::exception& e){post("error",{{"message",e.what()}});}try{post("snapshot",driver_session_snapshot());}catch(const std::exception& e){post("error",{{"message",e.what()}});}post("idle",Json::object());});}
};
HWND control(Console& ui,const wchar_t* type,const wchar_t* text,DWORD style,int x,int y,int width,int height,int id){auto h=CreateWindowExW((style&WS_BORDER)?WS_EX_CLIENTEDGE:0,type,text,WS_CHILD|WS_VISIBLE|style,x,y,width,height,ui.window,reinterpret_cast<HMENU>(static_cast<INT_PTR>(id)),GetModuleHandleW(nullptr),nullptr);if(!h)throw std::runtime_error("Create driver console control");SendMessageW(h,WM_SETFONT,reinterpret_cast<WPARAM>(ui.font),TRUE);ui.controls[id]=h;return h;}
std::filesystem::path choose_zip(HWND owner){std::array<wchar_t,32768> file{};OPENFILENAMEW dialog{};dialog.lStructSize=sizeof(dialog);dialog.hwndOwner=owner;dialog.lpstrFile=file.data();dialog.nMaxFile=file.size();dialog.lpstrFilter=L"Driver ZIP archives\0*.zip\0\0";dialog.lpstrTitle=L"Upload drivers to the server";dialog.Flags=OFN_FILEMUSTEXIST|OFN_PATHMUSTEXIST|OFN_NOCHANGEDIR;return GetOpenFileNameW(&dialog)?std::filesystem::path(file.data()):std::filesystem::path{};}
void menu(Console& ui){ui.line("MAIN MENU: help | status | drivers | install | upload | export-upload | retry-upload | update | restart | finish | logs | local-inf | shell");ui.line("Upload existing ZIP sends an archive you already have. Collect + upload checks this PC, exports installed drivers and adds them to the server library after validation. Retry upload resumes an interrupted transfer.");}
void status(Console& ui){const auto& s=ui.state;
    if(s.value("stage",std::string())=="complete"&&!completion_shutdown_ready(s)){
        ui.line("Windows is sealed. Customer handoff still needs to finish.");
        ui.line(completion_report_accepted(s)?"Final report accepted. Cleanup needs a retry.":"Report or desktop PDF delivery: "+s.value("deliveryError",std::string("Delivery pending; see delivery-queue.json.")));
        if(s.contains("shutdownError"))ui.line("Finish attention: "+s.at("shutdownError").get<std::string>());
        ui.line("Choose Retry Finish to continue delivery and cleanup without sealing Windows again.");
    }const auto& h=s.at("hardware");ui.line(h.at("manufacturer").get<std::string>()+" / "+h.at("model").get<std::string>()+" - Windows build "+h.at("build").get<std::string>());const auto& problems=s.at("drivers").at("problems");ui.line(std::to_string(problems.size())+" devices need attention.");for(const auto& device:problems)ui.line("  "+device.value("description",std::string("Device"))+" | "+device.value("instanceId",std::string())+" | code "+std::to_string(device.value("problemCode",0)));if(s.contains("driverPassSummary")){const auto& totals=s.at("driverPassSummary");ui.line("Last driver pass: "+std::to_string(totals.at("installed").get<unsigned>())+" installed, "+std::to_string(totals.at("skipped").get<unsigned>())+" skipped, "+std::to_string(totals.at("failed").get<unsigned>())+" failed.");}if(s.contains("pendingDriverUpload"))ui.line("An upload is unfinished. Retry upload continues it; Finish setup cancels it.");if(s.contains("driverUploadError"))ui.line("Upload attention: "+s.at("driverUploadError").get<std::string>());if(s.contains("error"))ui.line("Attention: "+s.at("error").get<std::string>());if(s.value("driverRebootRequired",false))ui.line("Restart required before installing more drivers. Finish setup handles the restart automatically.");}
void command(Console& ui,std::string text){
    while(!text.empty()&&text.back()==' ')text.pop_back();auto split=text.find(' ');auto name=text.substr(0,split);auto argument=split==std::string::npos?std::string():text.substr(split+1);
    if(name=="help"||name=="menu"){menu(ui);return;}if(name=="status"){status(ui);return;}
    if(name=="logs"){if(!ui.state.contains("logsDirectory"))throw std::runtime_error("Diagnostics are not loaded");auto result=reinterpret_cast<INT_PTR>(ShellExecuteW(ui.window,L"open",wide(ui.state.at("logsDirectory")).c_str(),nullptr,nullptr,SW_SHOWNORMAL));if(result<=32)throw std::runtime_error("Open diagnostic folder");return;}
    if(name=="shell"){std::array<wchar_t,32768> directory{};GetSystemDirectoryW(directory.data(),directory.size());auto exe=std::wstring(directory.data())+L"\\cmd.exe";if(reinterpret_cast<INT_PTR>(ShellExecuteW(ui.window,L"open",exe.c_str(),nullptr,nullptr,SW_SHOWNORMAL))<=32)throw std::runtime_error("Open command prompt");return;}
    if(name=="local-inf"){std::array<wchar_t,32768> file{};OPENFILENAMEW dialog{};dialog.lStructSize=sizeof(dialog);dialog.hwndOwner=ui.window;dialog.lpstrFile=file.data();dialog.nMaxFile=file.size();dialog.lpstrFilter=L"Driver INF files\0*.inf\0\0";dialog.lpstrTitle=L"Install a local signed driver";dialog.Flags=OFN_FILEMUSTEXIST|OFN_PATHMUSTEXIST|OFN_NOCHANGEDIR;if(GetOpenFileNameW(&dialog))ui.start("local-inf",std::filesystem::path(file.data()));return;}
    if(name=="upload"){auto file=argument.empty()?choose_zip(ui.window):std::filesystem::path(wide(argument));if(!file.empty())ui.start("upload",file);return;}
    if((name=="restart"||name=="shutdown")&&ui.state.value("stage",std::string())=="complete"){
        if(!completion_shutdown_ready(ui.state)&&MessageBoxW(ui.window,L"Windows is sealed, but report delivery or customer cleanup is unfinished. Saved files will remain available for retry. Shut down anyway?",L"Customer handoff pending",MB_YESNO|MB_ICONWARNING|MB_DEFBUTTON2)!=IDYES)return;
        ui.start("shutdown");return;
    }
    if(name=="drivers")name="catalog";if(name=="check")name="scan";
    if(name=="install"||name=="catalog"||name=="export-upload"||name=="retry-upload"||name=="update"||name=="scan"||name=="restart"||name=="finish"){ui.start(name);return;}
    throw std::runtime_error("Unknown command. Type help to see the main menu.");
}
LRESULT CALLBACK procedure(HWND h,UINT m,WPARAM w,LPARAM l){
    auto ui=reinterpret_cast<Console*>(GetWindowLongPtrW(h,GWLP_USERDATA));if(m==WM_NCCREATE){ui=static_cast<Console*>(reinterpret_cast<CREATESTRUCTW*>(l)->lpCreateParams);ui->window=h;SetWindowLongPtrW(h,GWLP_USERDATA,reinterpret_cast<LONG_PTR>(ui));}if(!ui)return DefWindowProcW(h,m,w,l);
    try{
        if(m==WM_CREATE){ui->font=CreateFontW(-17,0,0,0,FW_NORMAL,0,0,0,DEFAULT_CHARSET,0,0,CLEARTYPE_QUALITY,0,L"Segoe UI");ui->mono=CreateFontW(-16,0,0,0,FW_NORMAL,0,0,0,DEFAULT_CHARSET,0,0,CLEARTYPE_QUALITY,0,L"Consolas");
            control(*ui,L"STATIC",L"re-pxe - Driver console",0,24,18,900,30,Status);
            auto transcript=control(*ui,L"EDIT",L"",WS_BORDER|ES_MULTILINE|ES_READONLY|ES_AUTOVSCROLL|WS_VSCROLL,24,56,924,344,Transcript);SendMessageW(transcript,WM_SETFONT,reinterpret_cast<WPARAM>(ui->mono),TRUE);SendMessageW(transcript,EM_SETLIMITTEXT,220000,0);
            control(*ui,L"EDIT",L"",WS_BORDER|WS_TABSTOP|ES_AUTOHSCROLL,24,449,798,32,Command);control(*ui,L"BUTTON",L"Run",WS_TABSTOP|BS_DEFPUSHBUTTON,838,447,110,36,Run);
            control(*ui,L"BUTTON",L"Install server drivers",WS_TABSTOP,24,502,216,38,Install);control(*ui,L"BUTTON",L"Upload existing ZIP",WS_TABSTOP,252,502,216,38,Upload);control(*ui,L"BUTTON",L"Collect + upload",WS_TABSTOP,480,502,216,38,Export);control(*ui,L"BUTTON",L"Windows Update",WS_TABSTOP,708,502,240,38,UpdateDrivers);
            control(*ui,L"BUTTON",L"Check devices",WS_TABSTOP,24,553,216,38,Scan);control(*ui,L"BUTTON",L"Restart",WS_TABSTOP,252,553,216,38,Restart);control(*ui,L"BUTTON",L"Finish setup",WS_TABSTOP,480,553,216,38,Finish);control(*ui,L"BUTTON",L"Open logs",WS_TABSTOP,708,553,240,38,Logs);
            control(*ui,L"BUTTON",L"Cancel automatic install",WS_TABSTOP,24,607,320,36,Cancel);ShowWindow(ui->controls.at(Cancel),SW_HIDE);control(*ui,L"BUTTON",L"Install local driver",WS_TABSTOP,360,607,260,36,Local);control(*ui,L"BUTTON",L"Command prompt",WS_TABSTOP,636,607,312,36,Shell);control(*ui,L"BUTTON",L"Retry upload",WS_TABSTOP,24,410,216,30,Retry);ui->enable();return 0;
        }
        if(m==WM_CTLCOLOREDIT||m==WM_CTLCOLORSTATIC){auto control=reinterpret_cast<HWND>(l);if(control==ui->controls[Transcript]){auto dc=reinterpret_cast<HDC>(w);SetTextColor(dc,RGB(211,235,219));SetBkColor(dc,RGB(20,32,32));return reinterpret_cast<LRESULT>(GetStockObject(BLACK_BRUSH));}}
        if(m==WM_COMMAND){auto id=LOWORD(w);if(id==Cancel&&ui->counting){KillTimer(h,countdown);ui->counting=false;ShowWindow(ui->controls.at(Cancel),SW_HIDE);ui->line("Automatic installation cancelled. Choose an option from the main menu.");ui->set(Status,L"Main menu");ui->enable();return 0;}if(id==Finish&&ui->counting&&!ui->busy&&ui->initialized){KillTimer(h,countdown);ui->counting=false;ShowWindow(ui->controls.at(Cancel),SW_HIDE);}if(ui->busy||ui->counting||!ui->initialized)return 0;
            const std::map<int,std::string> actions={{Install,"install"},{Upload,"upload"},{Export,"export-upload"},{UpdateDrivers,"update"},{Scan,"scan"},{Restart,"restart"},{Finish,"finish"},{Logs,"logs"},{Local,"local-inf"},{Shell,"shell"},{Retry,"retry-upload"}};
            if(actions.contains(id)){command(*ui,actions.at(id));return 0;}
            if(id==Run){std::array<wchar_t,32768> value{};GetWindowTextW(ui->controls.at(Command),value.data(),value.size());ui->set(Command,L"");auto text=utf8(value.data());ui->line("> "+text);command(*ui,text);return 0;}
        }
        if(m==WM_TIMER&&w==countdown){if(!ui->counting)return 0;auto seconds=GetTickCount64()>=ui->deadline?0ULL:(ui->deadline-GetTickCount64()+999)/1000;ui->set(Status,L"Installing matching server drivers in "+std::to_wstring(seconds)+L" seconds");if(!seconds){ui->counting=false;ShowWindow(ui->controls.at(Cancel),SW_HIDE);ui->start("install");}return 0;}
        if(m==update){std::unique_ptr<Notice> p(reinterpret_cast<Notice*>(l));if(p->kind=="line"){ui->line(p->value.get<std::string>());ui->set(Status,wide(p->value.get<std::string>()));}
            if(p->kind=="snapshot"||p->kind=="state"){ui->state=p->value;if(ui->state.contains("hardware")&&ui->state.contains("drivers"))status(*ui);}
            if(p->kind=="catalog"){ui->line("Reviewed drivers for this model:");for(const auto& entry:p->value.at("drivers"))ui->line("  "+entry.at("name").get<std::string>()+" | "+entry.at("provider").get<std::string>()+" | "+entry.at("version").get<std::string>());}
            if(p->kind=="uploaded")ui->line("Server upload saved: "+p->value.at("id").get<std::string>()+". Added to the library after validation.");
            if(p->kind=="error")ui->line("Stopped: "+p->value.at("message").get<std::string>());
            if(p->kind=="idle"){ui->busy=false;if(!ui->state.is_null()&&completion_shutdown_ready(ui->state)){DestroyWindow(h);return 0;}const bool first=!ui->initialized;ui->initialized=!ui->state.is_null();if(first&&ui->initialized&&ui->state.value("driverFinishAfterReboot",false)&&!ui->state.value("driverRebootRequired",false)){ui->line("Restart completed. Continuing the technician's Finish setup request.");ui->start("finish");return 0;}if(first&&ui->initialized&&!ui->state.value("driverAutomaticAttempted",true)&&!ui->state.value("driverRebootRequired",false)){ui->counting=true;ui->deadline=GetTickCount64()+10000;ShowWindow(ui->controls.at(Cancel),SW_SHOW);if(!SetTimer(h,countdown,200,nullptr))throw std::runtime_error("Start driver countdown");ui->line("Automatic server driver installation starts in 10 seconds. Cancel to use the main menu.");}else{ui->set(Status,ui->state.value("stage",std::string())=="complete"?L"Windows sealed - customer handoff pending":L"Main menu");menu(*ui);}ui->enable();}
            return 0;
        }
        if(m==WM_CLOSE){if(ui->busy){ui->line("An operation is running. Wait for its saved result before closing.");return 0;}KillTimer(h,countdown);DestroyWindow(h);return 0;}
        if(m==WM_DESTROY){PostQuitMessage(0);return 0;}
    }catch(const std::exception& e){ui->line("Stopped: "+std::string(e.what()));}return DefWindowProcW(h,m,w,l);
}
}
int driver_console(){
    if(inventory().at("environment").value("winpe",false))throw std::runtime_error("Driver console requires installed Windows");
    OperationLock lock(L"Global\\repxeDriverConsole");if(!lock)return 0;
    Console ui;SetProcessDPIAware();auto instance=GetModuleHandleW(nullptr);WNDCLASSW cls{};cls.hInstance=instance;cls.lpfnWndProc=procedure;cls.lpszClassName=L"repxeDriverConsole";cls.hCursor=LoadCursorW(nullptr,IDC_ARROW);cls.hbrBackground=reinterpret_cast<HBRUSH>(COLOR_WINDOW+1);if(!RegisterClassW(&cls)&&GetLastError()!=ERROR_CLASS_ALREADY_EXISTS)throw std::runtime_error("Register driver console");
    auto h=CreateWindowExW(0,cls.lpszClassName,L"re-pxe - Driver console",WS_OVERLAPPED|WS_CAPTION|WS_SYSMENU|WS_MINIMIZEBOX,30,15,996,704,nullptr,nullptr,instance,&ui);if(!h)throw std::runtime_error("Create driver console");
    auto console=GetConsoleWindow();if(console)ShowWindow(console,SW_HIDE);ShowWindow(h,SW_SHOW);UpdateWindow(h);ui.line("re-pxe driver console.");ui.start("scan");
    ui.heartbeat=std::thread([&ui]{while(!ui.closing){try{driver_session_pulse();driver_remote_controls();}catch(...){}for(int i=0;i<30&&!ui.closing;++i)std::this_thread::sleep_for(std::chrono::milliseconds(100));}});
    MSG msg{};while(GetMessageW(&msg,nullptr,0,0)>0){if(msg.message==WM_KEYDOWN&&msg.wParam==VK_RETURN&&GetFocus()==ui.controls.at(Command)){PostMessageW(h,WM_COMMAND,Run,0);continue;}if(!IsDialogMessageW(h,&msg)){TranslateMessage(&msg);DispatchMessageW(&msg);}}
    ui.closing=true;if(console)ShowWindow(console,SW_SHOW);return 0;
}
}
