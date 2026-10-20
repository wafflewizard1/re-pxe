#include <repxe/windows_trust.hpp>
#include <windows.h>
#include <wincrypt.h>
#include <wintrust.h>
#include <softpub.h>
#include <mscat.h>
#include <winver.h>
#include <cwchar>
#include <array>
#include <vector>

namespace repxe {
namespace {
bool microsoft_signature(WINTRUST_DATA& data) {
    GUID policy=WINTRUST_ACTION_GENERIC_VERIFY_V2;
    data.dwUIChoice=WTD_UI_NONE;
    data.fdwRevocationChecks=WTD_REVOKE_NONE;
    data.dwStateAction=WTD_STATEACTION_VERIFY;
    // Deployment can be offline. Use Windows' existing trust material without
    // downloading certificates or changing the machine's trust configuration.
    data.dwProvFlags=WTD_CACHE_ONLY_URL_RETRIEVAL;
    const auto status=WinVerifyTrust(reinterpret_cast<HWND>(INVALID_HANDLE_VALUE),&policy,&data);
    bool trusted=false;
    if(status==ERROR_SUCCESS) {
        const auto module=GetModuleHandleW(L"wintrust.dll");
        const auto provider=reinterpret_cast<decltype(&WTHelperProvDataFromStateData)>(GetProcAddress(module,"WTHelperProvDataFromStateData"));
        const auto signer_from_chain=reinterpret_cast<decltype(&WTHelperGetProvSignerFromChain)>(GetProcAddress(module,"WTHelperGetProvSignerFromChain"));
        const auto chain=provider?provider(data.hWVTStateData):nullptr;
        const auto signer=chain&&signer_from_chain?signer_from_chain(chain,0,FALSE,0):nullptr;
        if(signer&&signer->pChainContext&&signer->csCertChain) {
            CERT_CHAIN_POLICY_PARA parameters{};parameters.cbSize=sizeof(parameters);
            CERT_CHAIN_POLICY_STATUS result{};result.cbSize=sizeof(result);
            std::array<wchar_t,128> organization{};
            const auto certificate=signer->pasCertChain[0].pCert;
            const auto length=certificate?CertGetNameStringW(certificate,CERT_NAME_ATTR_TYPE,0,const_cast<char*>(szOID_ORGANIZATION_NAME),organization.data(),organization.size()):0;
            trusted=length>1&&length<=organization.size()&&wcscmp(organization.data(),L"Microsoft Corporation")==0&&
                CertVerifyCertificateChainPolicy(CERT_CHAIN_POLICY_MICROSOFT_ROOT,signer->pChainContext,&parameters,&result)&&result.dwError==ERROR_SUCCESS;
        }
    }
    data.dwStateAction=WTD_STATEACTION_CLOSE;
    WinVerifyTrust(reinterpret_cast<HWND>(INVALID_HANDLE_VALUE),&policy,&data);
    return trusted;
}

bool microsoft_catalog(const std::filesystem::path& path,HANDLE file,const wchar_t* algorithm) {
    const auto module=GetModuleHandleW(L"wintrust.dll");
    const auto acquire=reinterpret_cast<decltype(&CryptCATAdminAcquireContext2)>(GetProcAddress(module,"CryptCATAdminAcquireContext2"));
    const auto calculate=reinterpret_cast<decltype(&CryptCATAdminCalcHashFromFileHandle2)>(GetProcAddress(module,"CryptCATAdminCalcHashFromFileHandle2"));
    if(!acquire||!calculate)return false;
    HCATADMIN admin=nullptr;GUID subsystem=DRIVER_ACTION_VERIFY;
    if(!acquire(&admin,&subsystem,algorithm,nullptr,0))return false;
    struct Admin {HCATADMIN h;~Admin(){CryptCATAdminReleaseContext(h,0);}} guard{admin};
    DWORD count=0;
    if(!calculate(admin,file,&count,nullptr,0)||!count||count>128)return false;
    std::vector<BYTE> hash(count);
    if(!calculate(admin,file,&count,hash.data(),0))return false;
    auto catalog=CryptCATAdminEnumCatalogFromHash(admin,hash.data(),count,0,nullptr);
    if(!catalog)return false;
    struct Catalog {HCATADMIN admin;HCATINFO h;~Catalog(){CryptCATAdminReleaseCatalogContext(admin,h,0);}} catalog_guard{admin,catalog};
    CATALOG_INFO information{};information.cbStruct=sizeof(information);
    if(!CryptCATCatalogInfoFromContext(catalog,&information,0))return false;
    std::wstring tag;const wchar_t* digits=L"0123456789ABCDEF";
    for(DWORD i=0;i<count;++i){tag+=digits[hash[i]>>4];tag+=digits[hash[i]&15];}
    WINTRUST_CATALOG_INFO member{};member.cbStruct=sizeof(member);
    member.pcwszCatalogFilePath=information.wszCatalogFile;
    member.pcwszMemberTag=tag.c_str();member.pcwszMemberFilePath=path.c_str();
    member.hMemberFile=file;member.pbCalculatedFileHash=hash.data();member.cbCalculatedFileHash=count;
    member.hCatAdmin=admin;
    WINTRUST_DATA data{};data.cbStruct=sizeof(data);data.dwUnionChoice=WTD_CHOICE_CATALOG;data.pCatalog=&member;
    return microsoft_signature(data);
}
}

bool trusted_microsoft_windows_file(const std::filesystem::path& path) {
    const auto file=CreateFileW(path.c_str(),GENERIC_READ,FILE_SHARE_READ,nullptr,OPEN_EXISTING,FILE_ATTRIBUTE_NORMAL,nullptr);
    if(file==INVALID_HANDLE_VALUE)return false;
    struct File {HANDLE h;~File(){CloseHandle(h);}} guard{file};
    WINTRUST_FILE_INFO info{};info.cbStruct=sizeof(info);info.pcwszFilePath=path.c_str();info.hFile=file;
    WINTRUST_DATA data{};data.cbStruct=sizeof(data);data.dwUnionChoice=WTD_CHOICE_FILE;data.pFile=&info;
    if(microsoft_signature(data))return true;
    return microsoft_catalog(path,file,L"SHA256")||microsoft_catalog(path,file,L"SHA1");
}

bool trusted_microsoft_windows_kernel(const std::filesystem::path& path) {
    const auto file=CreateFileW(path.c_str(),GENERIC_READ,FILE_SHARE_READ,nullptr,OPEN_EXISTING,FILE_ATTRIBUTE_NORMAL,nullptr);
    if(file==INVALID_HANDLE_VALUE)return false;
    struct File {HANDLE h;~File(){CloseHandle(h);}} guard{file};
    if(!trusted_microsoft_windows_file(path))return false;
    DWORD unused=0;
    const auto size=GetFileVersionInfoSizeExW(FILE_VER_GET_NEUTRAL,path.c_str(),&unused);
    if(!size||size>1024*1024)return false;
    std::vector<BYTE> version(size);
    if(!GetFileVersionInfoExW(FILE_VER_GET_NEUTRAL,path.c_str(),0,size,version.data()))return false;
    struct Translation {WORD language,codepage;};
    Translation* translations=nullptr;UINT count=0;
    if(!VerQueryValueW(version.data(),L"\\VarFileInfo\\Translation",reinterpret_cast<void**>(&translations),&count)||count%sizeof(Translation))return false;
    for(UINT i=0;i<count/sizeof(Translation);++i) {
        std::array<wchar_t,80> key{};
        swprintf(key.data(),key.size(),L"\\StringFileInfo\\%04x%04x\\OriginalFilename",translations[i].language,translations[i].codepage);
        wchar_t* name=nullptr;UINT length=0;
        if(VerQueryValueW(version.data(),key.data(),reinterpret_cast<void**>(&name),&length)&&length>1&&name[length-1]==L'\0'&&
            (_wcsicmp(name,L"ntkrnlmp.exe")==0||_wcsicmp(name,L"ntoskrnl.exe")==0))return true;
    }
    return false;
}
}
