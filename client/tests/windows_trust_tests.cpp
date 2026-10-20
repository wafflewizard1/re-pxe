#include <repxe/windows_trust.hpp>
#include <windows.h>
#include <array>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>

int main() {
    using namespace repxe;
    int checks=0;
    auto check=[&](bool pass,const char* description){if(!pass)throw std::runtime_error(description);++checks;};
    std::array<wchar_t,32768> buffer{};
    if(!GetWindowsDirectoryW(buffer.data(),buffer.size()))return 1;
    const auto system=std::filesystem::path(buffer.data())/L"System32";
    check(trusted_microsoft_windows_file(system/L"ntoskrnl.exe"),"Installed Windows kernel has a valid Microsoft signature");
    check(trusted_microsoft_windows_file(system/L"kernel32.dll"),"Windows catalog-signed system file is recognized");
    check(trusted_microsoft_windows_kernel(system/L"ntoskrnl.exe"),"Microsoft kernel identity is required for serviced Windows");
    check(!trusted_microsoft_windows_kernel(system/L"kernel32.dll"),"Another Microsoft-signed system file cannot replace the kernel");
    check(!trusted_microsoft_windows_file(system/L"repxe-file-that-does-not-exist.exe"),"Missing file must be refused");
    GetModuleFileNameW(nullptr,buffer.data(),buffer.size());
    check(!trusted_microsoft_windows_file(buffer.data()),"Unsigned re-pxe test executable must be refused");
    if(!GetTempPathW(buffer.size(),buffer.data()))return 1;
    auto path=std::filesystem::path(buffer.data())/(L"repxe-kernel-trust-"+std::to_wstring(GetCurrentProcessId())+L"-"+std::to_wstring(GetTickCount64())+L".exe");
    struct Cleanup {std::filesystem::path path;~Cleanup(){std::error_code error;std::filesystem::remove(path,error);}} cleanup{path};
    std::filesystem::copy_file(system/L"ntoskrnl.exe",path);
    check(trusted_microsoft_windows_file(path),"Valid copied kernel retains its signature binding");
    {
        std::fstream file(path,std::ios::binary|std::ios::in|std::ios::out);
        file.seekg(4096);char byte=0;file.read(&byte,1);byte^=1;
        file.seekp(4096);file.write(&byte,1);file.flush();
        if(!file)throw std::runtime_error("Cannot construct tampered kernel fixture");
    }
    check(!trusted_microsoft_windows_file(path),"Tampered kernel must be refused despite its copied signature");
    std::cout<<checks<<" Windows signature checks passed\n";
}
