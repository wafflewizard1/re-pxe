#include <repxe/windows.hpp>
#include <repxe/windows_update.hpp>
#include <windows.h>
#include <oleauto.h>
#include <atomic>
#include <algorithm>
#include <cctype>
#include <memory>
#include <stdexcept>

namespace repxe {
#ifdef REPXE_WUA_TESTS
IDispatch* test_update_object(const wchar_t*);
ULONGLONG test_update_clock();
void test_update_sleep(DWORD);
#define GetTickCount64 test_update_clock
#define Sleep test_update_sleep
#endif
namespace {
void check(HRESULT hr,const char* action){
    if(FAILED(hr))throw std::runtime_error(std::string(action)+" (HRESULT "+std::to_string(static_cast<unsigned long>(hr))+")");
}
struct Value {
    VARIANT v{};
    Value(){VariantInit(&v);}
    explicit Value(const wchar_t* s):Value(){v.vt=VT_BSTR;v.bstrVal=SysAllocString(s);if(!v.bstrVal)throw std::bad_alloc();}
    explicit Value(LONG n):Value(){v.vt=VT_I4;v.lVal=n;}
    explicit Value(bool b):Value(){v.vt=VT_BOOL;v.boolVal=b?VARIANT_TRUE:VARIANT_FALSE;}
    explicit Value(IDispatch* p):Value(){v.vt=VT_DISPATCH;v.pdispVal=p;p->AddRef();}
    Value(const Value& other):Value(){check(VariantCopy(&v,const_cast<VARIANT*>(&other.v)),"Copy update argument");}
    Value(Value&& other)noexcept:v(other.v){VariantInit(&other.v);}
    ~Value(){VariantClear(&v);}
    LONG number()const{if(v.vt!=VT_I4&&v.vt!=VT_UI4&&v.vt!=VT_INT&&v.vt!=VT_UINT)throw std::runtime_error("Invalid Windows Update numeric result");return v.lVal;}
    bool boolean()const{if(v.vt!=VT_BOOL)throw std::runtime_error("Invalid Windows Update boolean result");return v.boolVal==VARIANT_TRUE;}
    std::string text()const{if(v.vt!=VT_BSTR)throw std::runtime_error("Invalid Windows Update text result");return v.bstrVal?utf8(std::wstring(v.bstrVal,SysStringLen(v.bstrVal))):"";}
};
// WUA's supported Automation interfaces expose the same Begin/End operations
// as wuapi.h. Late binding also builds with MinGW's older WUA declarations.
struct Object {
    std::shared_ptr<IDispatch> p;
    explicit Object(const Value& value){
        IUnknown* unknown=value.v.vt==VT_DISPATCH?static_cast<IUnknown*>(value.v.pdispVal):value.v.vt==VT_UNKNOWN?value.v.punkVal:nullptr;
        if(!unknown)throw std::runtime_error("Missing Windows Update object");
        IDispatch* dispatch=nullptr;check(unknown->QueryInterface(IID_IDispatch,reinterpret_cast<void**>(&dispatch)),"Resolve update Automation interface");
        p={dispatch,[](IDispatch* d){d->Release();}};
    }
    explicit Object(const wchar_t* progid){
        IDispatch* dispatch=nullptr;
#ifdef REPXE_WUA_TESTS
        dispatch=test_update_object(progid);
#else
        CLSID clsid{};check(CLSIDFromProgID(progid,&clsid),"Find Windows Update class");
        check(CoCreateInstance(clsid,nullptr,CLSCTX_INPROC_SERVER,IID_IDispatch,reinterpret_cast<void**>(&dispatch)),"Create Windows Update object");
#endif
        p={dispatch,[](IDispatch* d){d->Release();}};
    }
    Value invoke(const wchar_t* name,WORD flags,std::initializer_list<Value> args={})const{
        DISPID id=0;auto member=const_cast<wchar_t*>(name);
        check(p->GetIDsOfNames(IID_NULL,&member,1,LOCALE_INVARIANT,&id),"Resolve Windows Update member");
        std::vector<VARIANT> reversed(args.size());size_t n=args.size();
        for(const auto& arg:args)reversed[--n]=arg.v;
        DISPID put=DISPID_PROPERTYPUT;DISPPARAMS params{reversed.data(),flags==DISPATCH_PROPERTYPUT?&put:nullptr,static_cast<UINT>(reversed.size()),flags==DISPATCH_PROPERTYPUT?1u:0u};
        Value result;EXCEPINFO exception{};UINT argument=0;
        auto hr=p->Invoke(id,IID_NULL,LOCALE_INVARIANT,flags,&params,&result.v,&exception,&argument);
        if(exception.pfnDeferredFillIn)exception.pfnDeferredFillIn(&exception);
        SysFreeString(exception.bstrSource);SysFreeString(exception.bstrDescription);SysFreeString(exception.bstrHelpFile);
        if(hr==DISP_E_EXCEPTION&&FAILED(exception.scode))hr=exception.scode;
        auto action="Windows Update "+utf8(name);check(hr,action.c_str());return result;
    }
    Value get(const wchar_t* name,std::initializer_list<Value> args={})const{return invoke(name,DISPATCH_PROPERTYGET,args);}
    Value call(const wchar_t* name,std::initializer_list<Value> args={})const{return invoke(name,DISPATCH_METHOD,args);}
    void set(const wchar_t* name,const Value& value)const{invoke(name,DISPATCH_PROPERTYPUT,{value});}
    Value value()const{return Value(p.get());}
};
// Microsoft documents a DISPID 0 Automation callback as the scripting
// alternative to ISearch/IDownload/IInstallationCompletedCallback.
class Callback final : public IDispatch {
    std::atomic<ULONG> refs{1};
public:
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID iid,void** out)override{
        if(!out)return E_POINTER;*out=nullptr;if(iid!=IID_IUnknown&&iid!=IID_IDispatch)return E_NOINTERFACE;
        *out=static_cast<IDispatch*>(this);AddRef();return S_OK;
    }
    ULONG STDMETHODCALLTYPE AddRef()override{return ++refs;}
    ULONG STDMETHODCALLTYPE Release()override{auto n=--refs;if(!n)delete this;return n;}
    HRESULT STDMETHODCALLTYPE GetTypeInfoCount(UINT* count)override{if(!count)return E_POINTER;*count=0;return S_OK;}
    HRESULT STDMETHODCALLTYPE GetTypeInfo(UINT,LCID,ITypeInfo**)override{return E_NOTIMPL;}
    HRESULT STDMETHODCALLTYPE GetIDsOfNames(REFIID,LPOLESTR*,UINT count,LCID,DISPID* ids)override{if(count!=1||!ids)return DISP_E_UNKNOWNNAME;*ids=DISPID_VALUE;return S_OK;}
    HRESULT STDMETHODCALLTYPE Invoke(DISPID id,REFIID,LCID,WORD,DISPPARAMS*,VARIANT* result,EXCEPINFO*,UINT*)override{
        if(id!=DISPID_VALUE)return DISP_E_MEMBERNOTFOUND;if(result)VariantInit(result);return S_OK;
    }
};
void wait_job(const Object& job,ULONGLONG deadline){
    for(;;){
        if(job.get(L"IsCompleted").boolean())return;
        if(GetTickCount64()>=deadline){
            job.call(L"RequestAbort");const auto grace=GetTickCount64()+30000;
            do{Sleep(200);if(job.get(L"IsCompleted").boolean())break;}while(GetTickCount64()<grace);
            throw std::runtime_error("Windows Update operation timed out; abort requested");
        }
        Sleep(200);
    }
}
bool eligible_driver(const Object& update,const Json& problems){
    if(update.get(L"Type").number()!=2)return false; // utDriver
    if(!driver_update_matches(update.get(L"DriverHardwareID").text(),problems))return false;
    auto driver_class=update.get(L"DriverClass").text();
    std::transform(driver_class.begin(),driver_class.end(),driver_class.begin(),[](unsigned char c){return std::tolower(c);});
    if(driver_class=="firmware")return false;
    Object behavior(update.get(L"InstallationBehavior"));if(behavior.get(L"CanRequestUserInput").boolean())return false;
    return true;
}
bool eligible(const Object& update,const Json& problems){
    if(!eligible_driver(update,problems))return false;
    Object bundled(update.get(L"BundledUpdates"));auto count=bundled.get(L"Count").number();if(count<0||count>128)return false;
    for(LONG i=0;i<count;++i){
        Object child(bundled.get(L"Item",{Value(i)}));if(!eligible_driver(child,problems))return false;
        Object nested(child.get(L"BundledUpdates"));if(nested.get(L"Count").number())return false;
    }
    return true;
}
}
Json update_unresolved_drivers(const Json& problems,int timeout_seconds,const std::function<void(const Json&)>& persist){
    Json report={{"schemaVersion",1},{"status","running"},{"phase","search"},{"updates",Json::array()},
                 {"rebootRequired",false},{"automaticReboot",false},{"timeoutSeconds",timeout_seconds}};
    if(timeout_seconds<60||timeout_seconds>1800)throw std::runtime_error("Windows Update timeout exceeds policy");
    const auto deadline=GetTickCount64()+static_cast<ULONGLONG>(timeout_seconds)*1000;
    check(CoInitializeEx(nullptr,COINIT_MULTITHREADED),"Initialize Windows Update COM");
    struct Apartment{~Apartment(){CoUninitialize();}} apartment;
    auto callback=new Callback;struct CallbackOwner{Callback* p;~CallbackOwner(){p->Release();}} owner{callback};
    Value notify(callback),empty;
    try{
        persist(report);
        Object session(L"Microsoft.Update.Session");session.set(L"ClientApplicationID",Value(L"re-pxe driver completion"));
        Object searcher(session.call(L"CreateUpdateSearcher"));searcher.set(L"ServerSelection",Value(2L)); // ssWindowsUpdate
        searcher.set(L"Online",Value(true));searcher.set(L"CanAutomaticallyUpgradeService",Value(false));
        Object search(searcher.call(L"BeginSearch",{Value(L"IsInstalled=0 and IsHidden=0 and Type='Driver' and DeploymentAction='Installation'"),notify,empty}));
        wait_job(search,deadline);Object found(searcher.call(L"EndSearch",{search.value()}));search.call(L"CleanUp");
        report["searchResultCode"]=found.get(L"ResultCode").number();
        if(report["searchResultCode"]!=2)throw std::runtime_error("Windows Update driver search failed or partially succeeded"); // orcSucceeded
        Object all(found.get(L"Updates"));auto count=all.get(L"Count").number();
        if(count<0||count>4096)throw std::runtime_error("Windows Update search exceeded driver bound");
        Object selected(L"Microsoft.Update.UpdateColl");
        for(LONG i=0;i<count;++i){
            Object update(all.get(L"Item",{Value(i)}));if(!eligible(update,problems))continue;
            if(report["updates"].size()>=128)throw std::runtime_error("Applicable drivers exceed installation bound");
            Object identity(update.get(L"Identity"));
            if(!update.get(L"EulaAccepted").boolean())update.call(L"AcceptEula");
            selected.call(L"Add",{update.value()});
            report["updates"].push_back({{"id",identity.get(L"UpdateID").text()},{"revision",identity.get(L"RevisionNumber").number()},
                                         {"title",update.get(L"Title").text().substr(0,300)}});
        }
        if(report["updates"].empty()){report["status"]="no-applicable-drivers";persist(report);return report;}
        report["phase"]="download";persist(report);
        Object downloader(session.call(L"CreateUpdateDownloader"));downloader.set(L"Updates",selected.value());
        Object download(downloader.call(L"BeginDownload",{notify,notify,empty}));wait_job(download,deadline);
        Object downloaded(downloader.call(L"EndDownload",{download.value()}));download.call(L"CleanUp");
        report["downloadResultCode"]=downloaded.get(L"ResultCode").number();
        for(size_t i=0;i<report["updates"].size();++i){
            Object item(downloaded.call(L"GetUpdateResult",{Value(static_cast<LONG>(i))}));
            report["updates"][i]["downloadHresult"]=item.get(L"HResult").number();report["updates"][i]["downloadResultCode"]=item.get(L"ResultCode").number();
        }
        persist(report);if(report["downloadResultCode"]!=2)throw std::runtime_error("Driver downloads failed or partially succeeded");
        Object installer(session.call(L"CreateUpdateInstaller"));installer.set(L"Updates",selected.value());
        installer.set(L"AllowSourcePrompts",Value(false));installer.set(L"ForceQuiet",Value(true));
        if(installer.get(L"IsBusy").boolean())throw std::runtime_error("Windows Update installer is busy");
        if(installer.get(L"RebootRequiredBeforeInstallation").boolean()){
            report["rebootRequired"]=true;report["status"]="succeeded";report["phase"]="pending-reboot";persist(report);return report;
        }
        if(GetTickCount64()>=deadline)throw std::runtime_error("Windows Update operation timed out before installation");
        report["phase"]="install";persist(report);
        Object install(installer.call(L"BeginInstall",{notify,notify,empty}));wait_job(install,deadline);
        Object installed(installer.call(L"EndInstall",{install.value()}));install.call(L"CleanUp");
        report["rebootRequired"]=installed.get(L"RebootRequired").boolean();report["installResultCode"]=installed.get(L"ResultCode").number();
        for(size_t i=0;i<report["updates"].size();++i){
            Object item(installed.call(L"GetUpdateResult",{Value(static_cast<LONG>(i))}));
            report["updates"][i]["hresult"]=item.get(L"HResult").number();report["updates"][i]["resultCode"]=item.get(L"ResultCode").number();
            report["updates"][i]["rebootRequired"]=item.get(L"RebootRequired").boolean();
        }
        if(report["installResultCode"]!=2)throw std::runtime_error("Driver installation failed or partially succeeded");
        report["phase"]="finished";report["status"]="succeeded";persist(report);return report;
    }catch(const std::exception& e){
        report["error"]=e.what();report["status"]=std::string(e.what()).find("timed out")!=std::string::npos?"timed-out":"failed";
        report["installationMayBeInProgress"]=report.at("phase")=="install";persist(report);return report;
    }
}
}
