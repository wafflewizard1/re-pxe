#include <repxe/windows_update.hpp>
#include <windows.h>
#include <oleauto.h>
#include <algorithm>
#include <atomic>
#include <iostream>
#include <map>
#include <stdexcept>

namespace repxe {
std::string utf8(const std::wstring& s){
    auto n=WideCharToMultiByte(CP_UTF8,0,s.data(),s.size(),nullptr,0,nullptr,nullptr);std::string result(n,' ');
    WideCharToMultiByte(CP_UTF8,0,s.data(),s.size(),result.data(),n,nullptr,nullptr);return result;
}
namespace {
std::string scenario;ULONGLONG tick=0;int selected=0,installed=0,aborts=0;bool quiet=false,prompts=true;
class Mock final:public IDispatch {
    std::atomic<ULONG> refs{1};std::wstring kind;int index;
    std::map<DISPID,std::wstring> names;
public:
    Mock(std::wstring type,int n=0):kind(std::move(type)),index(n){}
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID iid,void** out)override{if(!out)return E_POINTER;*out=nullptr;if(iid!=IID_IUnknown&&iid!=IID_IDispatch)return E_NOINTERFACE;*out=this;AddRef();return S_OK;}
    ULONG STDMETHODCALLTYPE AddRef()override{return ++refs;}
    ULONG STDMETHODCALLTYPE Release()override{auto n=--refs;if(!n)delete this;return n;}
    HRESULT STDMETHODCALLTYPE GetTypeInfoCount(UINT* count)override{*count=0;return S_OK;}
    HRESULT STDMETHODCALLTYPE GetTypeInfo(UINT,LCID,ITypeInfo**)override{return E_NOTIMPL;}
    HRESULT STDMETHODCALLTYPE GetIDsOfNames(REFIID,LPOLESTR* input,UINT count,LCID,DISPID* id)override{if(count!=1)return E_INVALIDARG;*id=static_cast<DISPID>(names.size()+1);names[*id]=input[0];return S_OK;}
    HRESULT STDMETHODCALLTYPE Invoke(DISPID id,REFIID,LCID,WORD flags,DISPPARAMS* params,VARIANT* out,EXCEPINFO*,UINT*)override{
        const auto name=names.at(id);VariantInit(out);
        auto object=[&](const wchar_t* type,int n=0){out->vt=VT_DISPATCH;out->pdispVal=new Mock(type,n);return S_OK;};
        auto number=[&](LONG value){out->vt=VT_I4;out->lVal=value;return S_OK;};
        auto boolean=[&](bool value){out->vt=VT_BOOL;out->boolVal=value?VARIANT_TRUE:VARIANT_FALSE;return S_OK;};
        auto text=[&](const wchar_t* value){out->vt=VT_BSTR;out->bstrVal=SysAllocString(value);return S_OK;};
        if(flags==DISPATCH_PROPERTYPUT){
            if(params->cArgs!=1||params->cNamedArgs!=1||params->rgdispidNamedArgs[0]!=DISPID_PROPERTYPUT)return E_INVALIDARG;
            if(name==L"ForceQuiet")quiet=params->rgvarg[0].boolVal==VARIANT_TRUE;
            if(name==L"AllowSourcePrompts")prompts=params->rgvarg[0].boolVal==VARIANT_TRUE;
            if(name==L"ServerSelection"&&params->rgvarg[0].lVal!=2)return E_INVALIDARG;
            return S_OK;
        }
        if(name==L"CreateUpdateSearcher")return object(L"searcher");
        if(name==L"CreateUpdateDownloader")return object(L"downloader");
        if(name==L"CreateUpdateInstaller")return object(L"installer");
        if(name==L"BeginSearch"){
            if(params->cArgs!=3||params->rgvarg[0].vt!=VT_EMPTY||params->rgvarg[1].vt!=VT_DISPATCH||params->rgvarg[2].vt!=VT_BSTR)return E_INVALIDARG;
            if(std::wstring(params->rgvarg[2].bstrVal).find(L"Type='Driver'")==std::wstring::npos)return E_INVALIDARG;
            // Verify the documented Automation callback responds at DISPID zero.
            DISPPARAMS empty{};if(FAILED(params->rgvarg[1].pdispVal->Invoke(0,IID_NULL,0,DISPATCH_METHOD,&empty,nullptr,nullptr,nullptr)))return E_INVALIDARG;
            return object(L"search-job");
        }
        if(name==L"IsCompleted")return boolean(!(scenario=="timeout"&&kind==L"search-job"));
        if(name==L"RequestAbort"){++aborts;return S_OK;}
        if(name==L"CleanUp")return S_OK;
        if(name==L"EndSearch")return object(L"found");
        if(name==L"ResultCode")return number((scenario=="search-failure"&&kind==L"found")||(scenario=="download-failure"&&kind==L"downloaded")||(scenario=="install-failure"&&kind==L"installed")?4:2);
        if(name==L"Updates")return object(L"all");
        if(name==L"Count")return number(kind==L"all"?2:kind==L"selected"?selected:kind==L"bundle"?1:0);
        if(name==L"Item")return object(kind==L"bundle"?(scenario=="bundle-software"?L"software":L"child"):L"update",params->rgvarg[0].lVal);
        if(name==L"Type")return number(kind==L"software"?1:2);
        if(name==L"DriverHardwareID")return text(index==0?L"ACPI\\QEMU0002":L"PCI\\HEALTHY");
        if(name==L"DriverClass")return text(scenario=="firmware"?L"Firmware":scenario=="bundle-firmware"&&kind==L"child"?L"firmWARE":L"System");
        if(name==L"InstallationBehavior")return object(kind==L"child"?L"child-behavior":L"behavior");
        if(name==L"CanRequestUserInput")return boolean(scenario=="prompt"||(scenario=="bundle-prompt"&&kind==L"child-behavior"));
        if(name==L"BundledUpdates")return object(scenario.rfind("bundle-",0)==0&&kind==L"update"?L"bundle":L"empty");
        if(name==L"Identity")return object(L"identity");
        if(name==L"UpdateID")return text(L"driver-id");
        if(name==L"RevisionNumber")return number(1);
        if(name==L"EulaAccepted")return boolean(false);
        if(name==L"AcceptEula")return S_OK;
        if(name==L"Title")return text(L"QEMU driver");
        if(name==L"Add"){++selected;return number(selected-1);}
        if(name==L"BeginDownload")return object(L"download-job");
        if(name==L"EndDownload")return object(L"downloaded");
        if(name==L"GetUpdateResult")return object(L"item-result");
        if(name==L"HResult")return number(0);
        if(name==L"IsBusy")return boolean(scenario=="busy");
        if(name==L"RebootRequiredBeforeInstallation")return boolean(scenario=="pending-reboot");
        if(name==L"BeginInstall"){installed=selected;return object(L"install-job");}
        if(name==L"EndInstall")return object(L"installed");
        if(name==L"RebootRequired")return boolean(true);
        return DISP_E_MEMBERNOTFOUND;
    }
};
}
IDispatch* test_update_object(const wchar_t* progid){return new Mock(std::wstring(progid)==L"Microsoft.Update.Session"?L"session":L"selected");}
ULONGLONG test_update_clock(){return tick;}
void test_update_sleep(DWORD duration){tick+=duration;}
}
int main(){
    using namespace repxe;
    try{
        const auto problems=Json::array({{{"hardwareIds",Json::array({"ACPI\\QEMU0002"})},{"compatibleIds",Json::array()}}});
        int checks=0;auto expect=[&](bool okay){++checks;if(!okay)throw std::runtime_error("Check "+std::to_string(checks)+" failed in "+scenario);};
        for(const auto* test:{"success","timeout","search-failure","download-failure","install-failure","busy","pending-reboot","bundle-software","bundle-firmware","bundle-prompt","firmware","prompt"}){
            scenario=test;tick=0;selected=installed=aborts=0;quiet=false;prompts=true;Json last;int records=0;
            auto report=update_unresolved_drivers(problems,60,[&](const Json& value){last=value;++records;});
            expect(last==report&&records>0);expect(report["automaticReboot"]==false);
            if(scenario=="success"){expect(report["status"]=="succeeded");expect(selected==1&&installed==1);expect(quiet&&!prompts);expect(report["updates"][0]["resultCode"]==2);}
            else if(scenario=="timeout"){expect(report["status"]=="timed-out");expect(aborts==1);expect(tick<=90000&&installed==0);}
            else if(scenario=="pending-reboot"){expect(report["status"]=="succeeded"&&report["rebootRequired"]==true);expect(installed==0);}
            else if(scenario.rfind("bundle-",0)==0||scenario=="firmware"||scenario=="prompt"){expect(report["status"]=="no-applicable-drivers");expect(selected==0&&installed==0);}
            else{expect(report["status"]=="failed");expect(!report["error"].get<std::string>().empty());}
        }
        std::cout<<checks<<" Windows Update Automation checks passed\n";return 0;
    }catch(const std::exception& e){std::cerr<<e.what()<<"\n";return 1;}
}
