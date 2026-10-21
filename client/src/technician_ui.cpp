#include <repxe/technician.hpp>
#include <windows.h>
#include <commdlg.h>
#include <commctrl.h>
#include <array>
#include <thread>
#include <map>
#include <memory>
#include <sstream>
#include <algorithm>

namespace repxe {
namespace {
constexpr UINT update=WM_APP+1;
enum Id {Mode=101,Config, Browse,Target,Preview,Execute,Inventory,Server,Token,SignIn,Image,Share,User,Password,Connect,Output,Status,Details,ConfirmText=201,ConfirmYes,ConfirmNo};
struct Notice {std::string kind;Json value;};
struct Ui {
    HWND window{};HFONT font{},title{},mono{};std::map<int,HWND> controls;
    double scale_x=1,scale_y=1;bool busy=false,reviewed=false,winpe=false;std::thread worker;Json disks=Json::array(),catalog=Json::array(),config,plan,trace=Json::array();
    std::filesystem::path reviewed_path,audit;std::string reviewed_hash,server,token;bool capture=false;Json result;
    ~Ui(){if(worker.joinable())worker.join();if(font)DeleteObject(font);if(title)DeleteObject(title);if(mono)DeleteObject(mono);if(!token.empty())SecureZeroMemory(token.data(),token.size());}
    std::wstring text(int id){auto h=controls.at(id);int n=GetWindowTextLengthW(h);if(n>32768)throw std::runtime_error("UI input exceeds bound");std::wstring s(n+1,L'\0');GetWindowTextW(h,s.data(),s.size());s.resize(n);return s;}
    void set(int id,const std::wstring& value){SetWindowTextW(controls.at(id),value.c_str());}
    void record(const std::string& action,const Json& detail=Json::object()){
        trace.push_back({{"action",action},{"detail",detail}});if(!audit.empty())write_json(audit,{{"schemaVersion",1},{"actions",trace}});
    }
    void post(const std::string& kind,Json value){auto p=new Notice{kind,std::move(value)};if(!PostMessageW(window,update,0,reinterpret_cast<LPARAM>(p)))delete p;}
    void enable(){
        for(const auto& [id,h]:controls)if(id!=Output && id!=Status)EnableWindow(h,!busy);
        EnableWindow(controls.at(Execute),!busy && reviewed && winpe);
        EnableWindow(controls.at(Target),!busy && SendMessageW(controls.at(Mode),CB_GETCURSEL,0,0)==0);
    }
    void invalidate(){reviewed=false;enable();}
    void start(std::function<void()> operation){
        if(busy)return;if(worker.joinable())worker.join();busy=true;enable();set(Status,L"Working... Progress and results appear below.");
        worker=std::thread([this,operation]{try{operation();}catch(const std::exception& e){post("error",{{"message",e.what()}});}post("idle",Json::object());});
    }
};
HWND control(Ui& ui,const wchar_t* cls,const std::wstring& text,DWORD style,int x,int y,int w,int h,int id=0){
    auto window=CreateWindowExW((style&WS_BORDER)?WS_EX_CLIENTEDGE:0,cls,text.c_str(),WS_CHILD|WS_VISIBLE|style,int(x*ui.scale_x),int(y*ui.scale_y),int(w*ui.scale_x),int(h*ui.scale_y),ui.window,reinterpret_cast<HMENU>(static_cast<INT_PTR>(id)),GetModuleHandleW(nullptr),nullptr);
    if(!window)throw std::runtime_error("Create technician control failed");SendMessageW(window,WM_SETFONT,reinterpret_cast<WPARAM>(ui.font),TRUE);if(id)ui.controls[id]=window;return window;
}
void label(Ui& u,const wchar_t* s,int x,int y,int w){control(u,L"STATIC",s,0,x,y,w,22);}
std::filesystem::path browse(HWND parent){std::array<wchar_t,32768> name{};OPENFILENAMEW f{};f.lStructSize=sizeof(f);f.hwndOwner=parent;f.lpstrFilter=L"JSON configuration (*.json)\0*.json\0All files\0*.*\0\0";f.lpstrFile=name.data();f.nMaxFile=name.size();f.Flags=OFN_FILEMUSTEXIST|OFN_PATHMUSTEXIST|OFN_NOCHANGEDIR;return GetOpenFileNameW(&f)?std::filesystem::path(name.data()):std::filesystem::path{};}
std::wstring summary(const Json& p,bool capture,bool details){
    std::wostringstream out;
    if(capture){out<<L"Capture review\r\nOffline source: "<<wide(p.at("source").at("mountPoints").empty()?p.at("source").value("volume",""):p.at("source").at("mountPoints").at(0).get<std::string>())<<L"\r\nA new Windows image will be saved. Review the source before capture.";}
    else{const auto& t=p.at("target"),image=p.at("image");const auto erase=p.at("config").value("targetPolicy","")=="format-selected-disk";out<<L"Windows: "<<wide(image.value("edition","Windows"))<<L"  (build "<<wide(image.value("build",""))<<L")\r\nTarget: "<<wide(t.at("identity"))<<L"\r\nDisk: "<<wide(t.value("model","unknown"))<<L"  |  "<<t.at("sizeBytes").get<unsigned long long>()/GiB<<L" GiB\r\n\r\n"<<(erase?L"All partitions and data on this disk will be erased.":L"Only a fully zeroed blank disk can be changed.")<<L" Other disks will be preserved.\r\nImage integrity and storage checks passed. Confirm this disk to continue.";}
    if(details)out<<L"\r\n\r\nTechnical details\r\n"<<wide(p.dump(2));
    auto s=out.str();std::wstring normalized;for(size_t i=0;i<s.size();++i){if(s[i]==L'\n' && (i==0 || s[i-1]!=L'\r'))normalized.push_back(L'\r');normalized.push_back(s[i]);}return normalized;
}
struct Confirmation {std::string expected;bool done=false,accepted=false;HWND input{},yes{};HFONT font{};};
LRESULT CALLBACK confirm_proc(HWND h,UINT m,WPARAM w,LPARAM l){
    auto c=reinterpret_cast<Confirmation*>(GetWindowLongPtrW(h,GWLP_USERDATA));
    if(m==WM_NCCREATE){c=static_cast<Confirmation*>(reinterpret_cast<CREATESTRUCTW*>(l)->lpCreateParams);SetWindowLongPtrW(h,GWLP_USERDATA,reinterpret_cast<LONG_PTR>(c));}
    if(!c)return DefWindowProcW(h,m,w,l);
    if(m==WM_COMMAND){
        if(LOWORD(w)==ConfirmText && HIWORD(w)==EN_CHANGE){std::array<wchar_t,2048>b{};GetWindowTextW(c->input,b.data(),b.size());EnableWindow(c->yes,confirmation_matches(c->expected,utf8(b.data())));}
        if(LOWORD(w)==ConfirmYes && IsWindowEnabled(c->yes)){c->accepted=true;c->done=true;DestroyWindow(h);return 0;}
        if(LOWORD(w)==ConfirmNo){c->done=true;DestroyWindow(h);return 0;}
    }
    if(m==WM_CLOSE){c->done=true;DestroyWindow(h);return 0;}
    return DefWindowProcW(h,m,w,l);
}
bool confirmation(Ui& u,const std::string& expected,bool capture){
    Confirmation c;c.expected=expected;c.font=u.font;
    const auto erase=!capture&&u.config.value("targetPolicy","")=="format-selected-disk";RECT parent{};GetWindowRect(u.window,&parent);auto h=CreateWindowExW(WS_EX_DLGMODALFRAME,L"repxeConfirmation",capture?L"Confirm reference capture":erase?L"Confirm disk erase":L"Confirm blank disk deployment",WS_CAPTION|WS_SYSMENU,parent.left+200,parent.top+175,660,280,u.window,nullptr,GetModuleHandleW(nullptr),&c);if(!h)throw std::runtime_error("Create confirmation failed");
    auto make=[&](const wchar_t* cls,const std::wstring& text,DWORD style,int x,int y,int w,int height,int id){auto item=CreateWindowExW((style&WS_BORDER)?WS_EX_CLIENTEDGE:0,cls,text.c_str(),WS_CHILD|WS_VISIBLE|style,x,y,w,height,h,reinterpret_cast<HMENU>(static_cast<INT_PTR>(id)),GetModuleHandleW(nullptr),nullptr);SendMessageW(item,WM_SETFONT,reinterpret_cast<WPARAM>(u.font),TRUE);return item;};
    make(L"STATIC",capture?L"Capture writes a new WIM. Confirm the offline source volume.":erase?L"All partitions and data on the selected disk will be erased.":L"Deployment requires a fully zeroed blank disk.",0,20,20,610,30,0);
    make(L"STATIC",L"Type this exact identity to proceed:",0,20,58,610,24,0);make(L"STATIC",wide(expected),0,20,88,610,24,0);
    c.input=make(L"EDIT",L"",WS_BORDER|WS_TABSTOP|ES_AUTOHSCROLL,20,119,610,30,ConfirmText);
    c.yes=make(L"BUTTON",capture?L"Capture reference":L"Deploy Windows",WS_TABSTOP|BS_PUSHBUTTON,350,177,170,34,ConfirmYes);EnableWindow(c.yes,FALSE);
    make(L"BUTTON",L"Cancel",WS_TABSTOP|BS_PUSHBUTTON,530,177,100,34,ConfirmNo);
    EnableWindow(u.window,FALSE);ShowWindow(h,SW_SHOW);SetFocus(c.input);MSG msg{};
    while(!c.done && GetMessageW(&msg,nullptr,0,0)>0){if(msg.message==WM_KEYDOWN && msg.wParam==VK_ESCAPE){SendMessageW(h,WM_CLOSE,0,0);continue;}if(!IsDialogMessageW(h,&msg)){TranslateMessage(&msg);DispatchMessageW(&msg);}}
    EnableWindow(u.window,TRUE);SetActiveWindow(u.window);return c.accepted;
}
void layout(Ui& u){
    RECT area{};GetClientRect(u.window,&area);u.scale_x=double(area.right)/1096;u.scale_y=double(area.bottom)/719;
    u.font=CreateFontW(-16,0,0,0,FW_NORMAL,0,0,0,DEFAULT_CHARSET,0,0,CLEARTYPE_QUALITY,0,L"Segoe UI");u.title=CreateFontW(-29,0,0,0,FW_SEMIBOLD,0,0,0,DEFAULT_CHARSET,0,0,CLEARTYPE_QUALITY,0,L"Segoe UI");u.mono=CreateFontW(-14,0,0,0,FW_NORMAL,0,0,0,DEFAULT_CHARSET,0,0,CLEARTYPE_QUALITY,0,L"Consolas");
    auto heading=control(u,L"STATIC",L"re-pxe",0,24,17,640,38);SendMessageW(heading,WM_SETFONT,reinterpret_cast<WPARAM>(u.title),TRUE);
    label(u,L"Technician workspace  /  0.13.6",24,62,650);control(u,L"BUTTON",L"Technical details",WS_TABSTOP|BS_AUTOCHECKBOX,700,25,190,32,Details);control(u,L"BUTTON",L"Refresh inventory",WS_TABSTOP,898,23,170,36,Inventory);
    label(u,L"Operation",24,100,180);label(u,L"Configuration file",212,100,850);
    control(u,L"COMBOBOX",L"",WS_TABSTOP|CBS_DROPDOWNLIST|WS_VSCROLL,24,124,170,130,Mode);SendMessageW(u.controls[Mode],CB_ADDSTRING,0,reinterpret_cast<LPARAM>(L"Deploy Windows"));SendMessageW(u.controls[Mode],CB_ADDSTRING,0,reinterpret_cast<LPARAM>(L"Capture reference"));SendMessageW(u.controls[Mode],CB_SETCURSEL,0,0);
    control(u,L"EDIT",L"",WS_TABSTOP|WS_BORDER|ES_AUTOHSCROLL,212,124,744,30,Config);control(u,L"BUTTON",L"Browse...",WS_TABSTOP,968,123,100,32,Browse);
    label(u,L"Target disk",24,175,170);control(u,L"COMBOBOX",L"",WS_TABSTOP|CBS_DROPDOWNLIST|WS_VSCROLL,212,172,856,180,Target);
    control(u,L"BUTTON",L"Preview configuration",WS_TABSTOP,24,220,184,36,Preview);control(u,L"BUTTON",L"Deploy Windows",WS_TABSTOP,222,220,184,36,Execute);
    control(u,L"STATIC",L"Loading inventory...",0,425,225,643,30,Status);
    label(u,L"Optional server job and reporting",24,277,1030);
    control(u,L"EDIT",L"https://192.168.1.26:8443",WS_TABSTOP|WS_BORDER|ES_AUTOHSCROLL,24,308,318,30,Server);
    control(u,L"EDIT",L"",WS_TABSTOP|WS_BORDER|ES_AUTOHSCROLL|ES_PASSWORD,354,308,534,30,Token);SendMessageW(u.controls[Token],EM_SETCUEBANNER,TRUE,reinterpret_cast<LPARAM>(L"Deployment API credential"));
    control(u,L"BUTTON",L"Sign in",WS_TABSTOP,900,307,168,32,SignIn);
    label(u,L"Approved image",24,353,170);control(u,L"COMBOBOX",L"",WS_TABSTOP|CBS_DROPDOWNLIST|WS_VSCROLL,212,350,856,180,Image);
    label(u,L"Temporary share connection",24,398,1030);
    control(u,L"EDIT",L"\\\\192.168.1.26\\re-pxe-images",WS_TABSTOP|WS_BORDER|ES_AUTOHSCROLL,24,430,318,30,Share);
    control(u,L"EDIT",L"",WS_TABSTOP|WS_BORDER|ES_AUTOHSCROLL,354,430,190,30,User);SendMessageW(u.controls[User],EM_SETCUEBANNER,TRUE,reinterpret_cast<LPARAM>(L"Share user"));
    control(u,L"EDIT",L"",WS_TABSTOP|WS_BORDER|ES_AUTOHSCROLL|ES_PASSWORD,556,430,332,30,Password);SendMessageW(u.controls[Password],EM_SETCUEBANNER,TRUE,reinterpret_cast<LPARAM>(L"Share password (memory only)"));
    control(u,L"BUTTON",L"Connect share",WS_TABSTOP,900,429,168,32,Connect);
    control(u,L"EDIT",L"Load a configuration, then preview its image, target and required storage.\r\nPasswords are temporary. A server job is optional for local imaging.",WS_BORDER|ES_MULTILINE|ES_READONLY|ES_AUTOVSCROLL|WS_VSCROLL,24,480,1044,222,Output);SendMessageW(u.controls[Output],WM_SETFONT,reinterpret_cast<WPARAM>(u.mono),TRUE);SendMessageW(u.controls[Output],EM_SETLIMITTEXT,1024*1024,0);
    u.enable();
}
LRESULT CALLBACK window_proc(HWND h,UINT m,WPARAM w,LPARAM l){
    auto u=reinterpret_cast<Ui*>(GetWindowLongPtrW(h,GWLP_USERDATA));if(m==WM_NCCREATE){u=static_cast<Ui*>(reinterpret_cast<CREATESTRUCTW*>(l)->lpCreateParams);u->window=h;SetWindowLongPtrW(h,GWLP_USERDATA,reinterpret_cast<LONG_PTR>(u));}
    if(!u)return DefWindowProcW(h,m,w,l);
    try{
        if(m==WM_CREATE){layout(*u);return 0;}
        if(m==WM_COMMAND){int id=LOWORD(w),notification=HIWORD(w);
            if(u->busy)return 0;
            if(id==Details){bool details=SendMessageW(u->controls[Details],BM_GETCHECK,0,0)==BST_CHECKED;if(!u->result.is_null())u->set(Output,details?wide(u->result.dump(2)):L"Windows deployment preparation is complete. Restart into Windows to finish drivers, recovery and setup.\r\nThe result and diagnostic logs have been saved.");else if(!u->plan.is_null())u->set(Output,summary(u->plan,u->capture,details));return 0;}
            if((id==Config && notification==EN_CHANGE) || (id==Target && notification==CBN_SELCHANGE) || (id==Mode && notification==CBN_SELCHANGE)){
                u->invalidate();u->set(Execute,SendMessageW(u->controls[Mode],CB_GETCURSEL,0,0)==0?L"Deploy Windows":L"Capture reference");return 0;
            }
            if((id==Server || id==Token) && notification==EN_CHANGE){
                if(!u->token.empty())SecureZeroMemory(u->token.data(),u->token.size());u->token.clear();u->server.clear();u->catalog=Json::array();SendMessageW(u->controls[Image],CB_RESETCONTENT,0,0);u->invalidate();return 0;
            }
            if(id==Browse){auto p=browse(h);if(!p.empty())u->set(Config,p.wstring());return 0;}
            if(id==Inventory){u->invalidate();u->start([u]{u->post("inventory",inventory());});return 0;}
            if(id==Connect){auto share=u->text(Share),user=u->text(User),password=u->text(Password);u->set(Password,L"");u->start([u,share,user,password]()mutable{try{connect_share(share,user,password);SecureZeroMemory(password.data(),password.size()*sizeof(wchar_t));u->post("share",{{"path",utf8(share)}});}catch(...){SecureZeroMemory(password.data(),password.size()*sizeof(wchar_t));throw;}});return 0;}
            if(id==SignIn){auto server=utf8(u->text(Server)),token=utf8(u->text(Token));u->set(Token,L"");u->invalidate();u->start([u,server,token]()mutable{try{auto c=server_catalog(server,token);u->post("catalog",{{"server",server},{"catalog",c},{"credential",token}});SecureZeroMemory(token.data(),token.size());}catch(...){SecureZeroMemory(token.data(),token.size());throw;}});return 0;}
            if(id==Preview){
                auto path=std::filesystem::path(u->text(Config));bool capture=SendMessageW(u->controls[Mode],CB_GETCURSEL,0,0)==1;auto selection=SendMessageW(u->controls[Target],CB_GETCURSEL,0,0);
                if(!capture && (selection<=0 || static_cast<size_t>(selection)>u->disks.size()))throw std::runtime_error("Choose the exact target disk before preview");
                std::string target=capture?"":u->disks.at(selection-1).at("identity").get<std::string>();u->invalidate();
                u->start([u,path,capture,target]{auto hash=sha256(path);auto c=read_json(path);if(!capture)c["targetIdentity"]=target;auto p=capture?capture_from_config(c,path,false):deploy_from_config(c,path,false);if(sha256(path)!=hash)throw std::runtime_error("Configuration changed during review");u->post("preview",{{"configuration",c},{"plan",p},{"path",utf8(path.wstring())},{"hash",hash},{"capture",capture}});});return 0;
            }
            if(id==Execute){
                if(!u->reviewed || !u->winpe)throw std::runtime_error("A successful WinPE preview is required");
                auto expected=u->capture?u->config.at("sourceVolume").get<std::string>():u->config.at("targetIdentity").get<std::string>();
                if(!confirmation(*u,expected,u->capture)){u->record("confirmation-cancelled");return 0;}
                auto c=u->config;auto path=u->reviewed_path;auto hash=u->reviewed_hash;bool capture=u->capture;auto origin=u->server,token=u->token;std::string image_id;
                if(!capture && !token.empty()){auto n=SendMessageW(u->controls[Image],CB_GETCURSEL,0,0);if(n<0 || static_cast<size_t>(n)>=u->catalog.size())throw std::runtime_error("Choose an approved server image");image_id=u->catalog.at(n).at("id");select_server_image(u->catalog,c,image_id);}
                u->record("execution-confirmed",{{"identity",expected},{"capture",capture}});u->invalidate();
                u->start([u,c,path,hash,capture,origin,token,image_id]()mutable{
                    try{
                        if(sha256(path)!=hash)throw std::runtime_error("Configuration changed after preview; preview it again");
                        auto progress=[u](const Json& state){u->post("progress",{{"stage",state.at("stage")},{"percent",state.value("progressPercent",-1)}});};Json result;
                        if(capture)result=capture_from_config(c,path,true);
                        else if(!token.empty())result=server_deploy(c,path,origin,token,image_id,progress);
                        else{deploy_from_config(c,path,false);auto runtime=std::filesystem::path(wide(c.at("jobDirectory")))/(L"ui-deployment-"+wide(hash.substr(0,16))+L".json");if(std::filesystem::exists(runtime))throw std::runtime_error("This reviewed UI job already exists; review its state before a new job");write_json(runtime,c);result=deploy_from_config(c,runtime,true,progress);}
                        SecureZeroMemory(token.data(),token.size());u->post("result",result);
                    }catch(...){SecureZeroMemory(token.data(),token.size());throw;}
                });return 0;
            }
        }
        if(m==update){
            std::unique_ptr<Notice> p(reinterpret_cast<Notice*>(l));const auto& v=p->value;
            if(p->kind=="idle"){u->busy=false;u->enable();return 0;}
            if(p->kind=="inventory"){
                u->winpe=v.at("environment").value("winpe",false);u->disks=Json::array();SendMessageW(u->controls[Target],CB_RESETCONTENT,0,0);SendMessageW(u->controls[Target],CB_ADDSTRING,0,reinterpret_cast<LPARAM>(L"Choose a target disk"));
                for(const auto& d:v.at("disks"))if(!d.value("removable",true) && d.value("stableIdentity",false) && d.value("writable",false)){
                    u->disks.push_back(d);auto label=wide(d.at("identity"))+L"  |  "+wide(d.value("model","unknown"))+L"  |  "+std::to_wstring(d.at("sizeBytes").get<unsigned long long>()/GiB)+L" GiB";SendMessageW(u->controls[Target],CB_ADDSTRING,0,reinterpret_cast<LPARAM>(label.c_str()));}
                SendMessageW(u->controls[Target],CB_SETCURSEL,0,0);u->set(Status,u->winpe?L"WinPE ready. Select a target and preview first.":L"Windows workstation: imaging execution is disabled.");u->record("inventory-ready",{{"winpe",u->winpe},{"eligibleDiskCount",u->disks.size()}});
            }
            if(p->kind=="catalog"){
                if(!u->token.empty())SecureZeroMemory(u->token.data(),u->token.size());u->token=v.at("credential");u->server=v.at("server");u->catalog=v.at("catalog");SendMessageW(u->controls[Image],CB_RESETCONTENT,0,0);
                for(const auto& image:u->catalog){auto label=wide(image.at("name"))+L"  |  "+wide(image.at("id"));SendMessageW(u->controls[Image],CB_ADDSTRING,0,reinterpret_cast<LPARAM>(label.c_str()));}
                if(!u->catalog.empty())SendMessageW(u->controls[Image],CB_SETCURSEL,0,0);u->set(Status,L"Server authenticated. Select its approved image before deployment.");u->record("catalog-ready",{{"server",u->server},{"imageCount",u->catalog.size()}});
                auto& secret=p->value["credential"].get_ref<std::string&>();SecureZeroMemory(secret.data(),secret.size());
            }
            if(p->kind=="share"){u->set(Status,L"Temporary share connection authenticated.");u->record("share-connected",v);}
            if(p->kind=="preview"){
                u->config=v.at("configuration");u->plan=v.at("plan");u->result=nullptr;u->reviewed_path=wide(v.at("path"));u->reviewed_hash=v.at("hash");u->capture=v.at("capture");u->reviewed=true;u->set(Output,summary(u->plan,u->capture,SendMessageW(u->controls[Details],BM_GETCHECK,0,0)==BST_CHECKED));u->set(Status,L"Preview passed. Confirm the selected disk to continue.");u->record("preview-ready",{{"capture",u->capture},{"configurationHash",u->reviewed_hash}});
            }
            if(p->kind=="progress"){auto percent=v.value("percent",-1);u->set(Status,v.at("stage")=="applying-image"?L"Installing Windows"+(percent>=0?L" - "+std::to_wstring(percent)+L"%":L"..."):L"Deployment: "+wide(v.at("stage")));u->record("deployment-stage",v);}
            if(p->kind=="result"){u->result=v;u->set(Output,SendMessageW(u->controls[Details],BM_GETCHECK,0,0)==BST_CHECKED?wide(v.dump(2)):u->capture?L"The Windows image was captured and verified. Its metadata and logs have been saved.":L"Windows deployment preparation is complete. Restart into Windows to finish drivers, recovery and setup.\r\nThe result and diagnostic logs have been saved.");u->set(Status,L"Offline operation finished.");u->record("operation-result",v);}
            if(p->kind=="error"){u->reviewed=false;u->set(Status,L"Operation stopped. Review the diagnostic below.");u->set(Output,wide(v.at("message")));u->record("operation-error",v);}
            u->enable();return 0;
        }
        if(m==WM_CLOSE){if(u->busy){MessageBoxW(h,L"An operation is running. Wait for its saved result before closing.",L"re-pxe",MB_OK|MB_ICONINFORMATION);return 0;}DestroyWindow(h);return 0;}
        if(m==WM_DESTROY){PostQuitMessage(0);return 0;}
    }catch(const std::exception& e){MessageBoxW(h,wide(e.what()).c_str(),L"re-pxe",MB_OK|MB_ICONERROR);}
    return DefWindowProcW(h,m,w,l);
}
}
int technician_ui(const std::filesystem::path& config,const std::filesystem::path& audit){
    SetProcessDPIAware();Ui ui;ui.audit=audit;auto instance=GetModuleHandleW(nullptr);WNDCLASSW cls{};cls.hInstance=instance;cls.lpfnWndProc=window_proc;cls.lpszClassName=L"repxeTechnician";cls.hCursor=LoadCursorW(nullptr,IDC_ARROW);cls.hbrBackground=reinterpret_cast<HBRUSH>(COLOR_WINDOW+1);if(!RegisterClassW(&cls) && GetLastError()!=ERROR_CLASS_ALREADY_EXISTS)throw std::runtime_error("Register technician window");
    cls.lpfnWndProc=confirm_proc;cls.lpszClassName=L"repxeConfirmation";RegisterClassW(&cls);
    auto h=CreateWindowExW(0,L"repxeTechnician",L"re-pxe 0.13.6 - Technician workspace",WS_OVERLAPPED|WS_CAPTION|WS_SYSMENU|WS_MINIMIZEBOX,0,0,std::min(1120,GetSystemMetrics(SM_CXSCREEN)),std::min(758,GetSystemMetrics(SM_CYSCREEN)),nullptr,nullptr,instance,&ui);if(!h)throw std::runtime_error("Create technician window");
    if(!config.empty())ui.set(Config,config.wstring());auto console=GetConsoleWindow();if(console)ShowWindow(console,SW_HIDE);ShowWindow(h,SW_SHOW);UpdateWindow(h);ui.record("ui-opened");ui.start([&ui]{ui.post("inventory",inventory());});MSG msg{};
    while(GetMessageW(&msg,nullptr,0,0)>0){if(!IsDialogMessageW(h,&msg)){TranslateMessage(&msg);DispatchMessageW(&msg);}}
    if(console)ShowWindow(console,SW_SHOW);return 0;
}
}
