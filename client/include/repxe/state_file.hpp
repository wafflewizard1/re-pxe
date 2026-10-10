#pragma once
#include <windows.h>
#include <objbase.h>
#include <filesystem>
#include <stdexcept>
#include <string>

namespace repxe {
// A crashed writer can leave its staging file behind. Each new write owns a
// different file; never truncate or remove another attempt's evidence.
inline void persist_state_file(const std::filesystem::path& path,const std::string& text){
    GUID id{};if(FAILED(CoCreateGuid(&id)))throw std::runtime_error("Create state generation");
    wchar_t generation[40]{};if(!StringFromGUID2(id,generation,40))throw std::runtime_error("Encode state generation");
    auto temporary=path;temporary+=L".tmp."+std::wstring(generation);
    HANDLE file=CreateFileW(temporary.c_str(),GENERIC_WRITE,0,nullptr,CREATE_NEW,FILE_ATTRIBUTE_NORMAL,nullptr);
    auto fail=[](const char* action,DWORD error){throw std::runtime_error(std::string(action)+" (Windows error "+std::to_string(error)+")");};
    if(file==INVALID_HANDLE_VALUE)fail("Create state file",GetLastError());
    try{
        DWORD written=0;
        if(text.size()>MAXDWORD || !WriteFile(file,text.data(),static_cast<DWORD>(text.size()),&written,nullptr) || written!=text.size() || !FlushFileBuffers(file))fail("Persist state file",GetLastError());
        if(!CloseHandle(file)){auto error=GetLastError();file=INVALID_HANDLE_VALUE;fail("Close state file",error);}file=INVALID_HANDLE_VALUE;
        if(!MoveFileExW(temporary.c_str(),path.c_str(),MOVEFILE_REPLACE_EXISTING|MOVEFILE_WRITE_THROUGH))fail("Publish state file",GetLastError());
    }catch(...){if(file!=INVALID_HANDLE_VALUE)CloseHandle(file);DeleteFileW(temporary.c_str());throw;}
}
}
