#include <repxe/completion_startup.hpp>
#include <array>
#include <iostream>

int main(){
    using namespace repxe;
    const auto fixture=L"Software\\repxeStartupFixture-"+std::to_wstring(GetCurrentProcessId())+L"-"+std::to_wstring(GetTickCount64());
    HKEY software=nullptr;
    if(RegCreateKeyExW(HKEY_CURRENT_USER,fixture.c_str(),0,nullptr,0,KEY_ALL_ACCESS,nullptr,&software,nullptr))return 2;
    struct Cleanup{HKEY h;std::wstring path;~Cleanup(){RegCloseKey(h);RegDeleteTreeW(HKEY_CURRENT_USER,path.c_str());}} cleanup{software,fixture};
    int checks=0;
    auto check=[&](bool pass,const char* message){if(!pass)throw std::runtime_error(message);++checks;};
    auto read=[&](const wchar_t* name,DWORD expected_type){
        std::array<wchar_t,1024> value{};DWORD type=0,size=sizeof(value);
        auto code=RegGetValueW(software,completion_run_key,name,RRF_RT_ANY|RRF_NOEXPAND,&type,value.data(),&size);
        check(code==ERROR_SUCCESS&&type==expected_type,"Startup value type and read-back");return std::wstring(value.data());
    };
    try{
        set_completion_startup(software,false,L"");check(true,"Removal works when Run key is absent");
        set_completion_startup(software,true,completion_installed_path);
        const auto deferred=read(completion_run_value,REG_EXPAND_SZ);
        check(deferred==L"\"%SystemDrive%\\ProgramData\\re-pxe\\re-pxe.exe\" complete","Offline provisioning defers the drive letter until installed Windows logon");
        std::array<wchar_t,1024> expanded{};auto length=ExpandEnvironmentStringsW(deferred.c_str(),expanded.data(),expanded.size());
        std::array<wchar_t,32> drive{};auto drive_length=GetEnvironmentVariableW(L"SystemDrive",drive.data(),drive.size());
        check(length&&length<=expanded.size()&&drive_length&&drive_length<drive.size()&&std::wstring(expanded.data())==L"\""+std::wstring(drive.data())+L"\\ProgramData\\re-pxe\\re-pxe.exe\" complete","Startup expands to the running Windows system drive");
        HKEY run=nullptr;check(RegOpenKeyExW(software,completion_run_key,0,KEY_SET_VALUE,&run)==ERROR_SUCCESS,"Open fixture Run");
        const wchar_t other[]=L"untouched vendor command";
        auto code=RegSetValueExW(run,L"OtherVendor",0,REG_SZ,reinterpret_cast<const BYTE*>(other),sizeof(other));RegCloseKey(run);check(code==ERROR_SUCCESS,"Create unrelated startup fixture");
        set_completion_startup(software,true,L"C:\\Program Data\\re-pxe\\re-pxe.exe");
        check(read(completion_run_value,REG_EXPAND_SZ)==L"\"C:\\Program Data\\re-pxe\\re-pxe.exe\" complete","Online rearming preserves quoting for executable paths with spaces");
        check(read(L"OtherVendor",REG_SZ)==other,"Rearming preserves unrelated startup values");
        set_completion_startup(software,false,L"");set_completion_startup(software,false,L"");
        DWORD bytes=0;check(RegGetValueW(software,completion_run_key,completion_run_value,RRF_RT_ANY,nullptr,nullptr,&bytes)==ERROR_FILE_NOT_FOUND,"Terminal cleanup removes only re-pxe startup and is idempotent");
        check(read(L"OtherVendor",REG_SZ)==other,"Cleanup preserves unrelated startup values");
        set_completion_startup(software,true,completion_installed_path);
        check(read(completion_run_value,REG_EXPAND_SZ)==deferred,"Startup can be rearmed after cleanup");
        for(const auto& invalid:{std::wstring(),std::wstring(L"bad\"path.exe"),std::wstring(L"bad\npath.exe"),std::wstring(L"bad\0path",8)}){
            bool rejected=false;try{set_completion_startup(software,true,invalid);}catch(const std::exception&){rejected=true;}
            check(rejected,"Invalid startup commands must be refused");
        }
        check(read(completion_run_value,REG_EXPAND_SZ)==deferred,"Rejected commands preserve the committed startup entry");
        std::cout<<checks<<" Windows startup registry checks passed\n";return 0;
    }catch(const std::exception& e){std::cerr<<e.what()<<"\n";return 1;}
}
