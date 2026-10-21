#pragma once
#include <windows.h>
#include <stdexcept>
#include <string>

namespace repxe {
inline constexpr auto completion_run_key=L"Microsoft\\Windows\\CurrentVersion\\Run";
inline constexpr auto completion_run_value=L"repxeCompletion";
inline constexpr auto completion_installed_path=L"%SystemDrive%\\ProgramData\\re-pxe\\re-pxe.exe";

// software is the destination SOFTWARE hive, or a private fixture in tests.
// Use the Windows system drive at logon, never WinPE's temporary drive letter.
inline void set_completion_startup(HKEY software,bool install,const std::wstring& executable){
    auto fail=[](const char* action,LSTATUS code){throw std::runtime_error(std::string(action)+" (Windows error "+std::to_string(code)+")");};
    if(install&&(executable.empty()||executable.find_first_of(L"\"\r\n")!=std::wstring::npos||executable.find(L'\0')!=std::wstring::npos))throw std::runtime_error("Invalid completion startup executable");
    HKEY key=nullptr;
    auto code=install?RegCreateKeyExW(software,completion_run_key,0,nullptr,0,KEY_SET_VALUE|KEY_QUERY_VALUE,nullptr,&key,nullptr):RegOpenKeyExW(software,completion_run_key,0,KEY_SET_VALUE,&key);
    if(!install&&code==ERROR_FILE_NOT_FOUND)return;
    if(code)fail("Open completion startup registry",code);
    struct Key{HKEY h;~Key(){RegCloseKey(h);}} guard{key};
    if(install){
        const auto command=L"\""+executable+L"\" complete";
        code=RegSetValueExW(key,completion_run_value,0,REG_EXPAND_SZ,reinterpret_cast<const BYTE*>(command.c_str()),static_cast<DWORD>((command.size()+1)*sizeof(wchar_t)));
        if(code)fail("Install completion startup",code);
        std::wstring actual(command.size()+1,L'\0');DWORD type=0,size=static_cast<DWORD>(actual.size()*sizeof(wchar_t));
        code=RegQueryValueExW(key,completion_run_value,nullptr,&type,reinterpret_cast<BYTE*>(actual.data()),&size);
        if(code||type!=REG_EXPAND_SZ||size!=(command.size()+1)*sizeof(wchar_t)||actual!=command+L'\0')throw std::runtime_error("Completion startup read-back differs");
    }else{
        code=RegDeleteValueW(key,completion_run_value);
        if(code!=ERROR_SUCCESS&&code!=ERROR_FILE_NOT_FOUND)fail("Remove completion startup",code);
    }
}
}
