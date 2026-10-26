#include <repxe/windows.hpp>
#include <repxe/windows_update.hpp>
#include <repxe/windows_trust.hpp>
#include <repxe/operation_lock.hpp>
#include <repxe/state_file.hpp>
#include <repxe/completion_startup.hpp>
#include <repxe/windows_controls.hpp>
#include <repxe/technician.hpp>
#include <windows.h>
#include <winioctl.h>
#include <bcrypt.h>
#include <aclapi.h>
#include <sddl.h>
#include <setupapi.h>
#include <cfgmgr32.h>
#include <winhttp.h>
#include <wincrypt.h>
#include <pugixml.hpp>
#include <algorithm>
#include <cstring>
#include <array>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <memory>
#include <optional>
#include <set>
#include <regex>
#include <objbase.h>
#include <sstream>
#include <shlobj.h>
#include <stdexcept>

namespace repxe {
namespace {
class Handle {
public:
    HANDLE value=INVALID_HANDLE_VALUE;
    explicit Handle(HANDLE h=INVALID_HANDLE_VALUE):value(h){}
    ~Handle(){if(value!=INVALID_HANDLE_VALUE && value!=nullptr)CloseHandle(value);}
    Handle(const Handle&)=delete;Handle& operator=(const Handle&)=delete;
    explicit operator bool()const{return value!=INVALID_HANDLE_VALUE && value!=nullptr;}
};
void fail(const std::string& action){throw std::runtime_error(action+" (Windows error "+std::to_string(GetLastError())+")");}
std::wstring system_directory(){std::wstring result(32768,L'\0');auto n=GetSystemDirectoryW(result.data(),static_cast<UINT>(result.size()));if(!n || n>=result.size())fail("GetSystemDirectory");result.resize(n);return result;}
std::wstring executable(){std::wstring result(32768,L'\0');auto n=GetModuleFileNameW(nullptr,result.data(),static_cast<DWORD>(result.size()));if(!n || n>=result.size())fail("GetModuleFileName");result.resize(n);return result;}
std::string utc_now(){SYSTEMTIME t{};GetSystemTime(&t);std::ostringstream out;out<<std::setfill('0')<<std::setw(4)<<t.wYear<<'-'<<std::setw(2)<<t.wMonth<<'-'<<std::setw(2)<<t.wDay<<'T'<<std::setw(2)<<t.wHour<<':'<<std::setw(2)<<t.wMinute<<':'<<std::setw(2)<<t.wSecond<<'Z';return out.str();}
std::string trim(std::string value){while(!value.empty() && (value.back()==' ' || value.back()=='\0'))value.pop_back();auto at=value.find_first_not_of(" ");return at==std::string::npos?"":value.substr(at);}
std::string descriptor_string(const std::vector<unsigned char>& data,DWORD offset){if(!offset || offset>=data.size())return "";auto end=offset;while(end<data.size() && data[end])++end;return trim(std::string(reinterpret_cast<const char*>(data.data()+offset),end-offset));}
std::string native_disk_serial(HANDLE h,const std::vector<unsigned char>& descriptor) {
    auto disk=reinterpret_cast<const STORAGE_DEVICE_DESCRIPTOR*>(descriptor.data());
    if(disk->BusType!=BusTypeNvme)return descriptor_string(descriptor,disk->SerialNumberOffset);
    // The generic Windows descriptor may expose an EUI instead of the vendor SN.
    // Read the same controller SN as Linux through a read-only Identify query.
    const auto base=offsetof(STORAGE_PROPERTY_QUERY,AdditionalParameters);
    std::vector<unsigned char> buffer(base+sizeof(STORAGE_PROTOCOL_SPECIFIC_DATA)+4096);
    auto query=reinterpret_cast<STORAGE_PROPERTY_QUERY*>(buffer.data());
    query->PropertyId=StorageAdapterProtocolSpecificProperty;query->QueryType=PropertyStandardQuery;
    auto protocol=reinterpret_cast<STORAGE_PROTOCOL_SPECIFIC_DATA*>(buffer.data()+base);
    protocol->ProtocolType=ProtocolTypeNvme;protocol->DataType=NVMeDataTypeIdentify;
    protocol->ProtocolDataRequestValue=1; // CNS: Identify Controller.
    protocol->ProtocolDataOffset=sizeof(STORAGE_PROTOCOL_SPECIFIC_DATA);protocol->ProtocolDataLength=4096;
    DWORD returned=0;
    if(!DeviceIoControl(h,IOCTL_STORAGE_QUERY_PROPERTY,buffer.data(),static_cast<DWORD>(buffer.size()),buffer.data(),static_cast<DWORD>(buffer.size()),&returned,nullptr))return "";
    if(returned>buffer.size() || returned<sizeof(STORAGE_PROTOCOL_DATA_DESCRIPTOR))return "";
    auto response=reinterpret_cast<const STORAGE_PROTOCOL_DATA_DESCRIPTOR*>(buffer.data());
    const auto& info=response->ProtocolSpecificData;
    if(response->Version!=sizeof(STORAGE_PROTOCOL_DATA_DESCRIPTOR) || response->Size!=sizeof(STORAGE_PROTOCOL_DATA_DESCRIPTOR) || info.ProtocolType!=ProtocolTypeNvme || info.DataType!=NVMeDataTypeIdentify || info.ProtocolDataOffset<sizeof(info) || info.ProtocolDataLength<4096)return "";
    const size_t offset=offsetof(STORAGE_PROTOCOL_DATA_DESCRIPTOR,ProtocolSpecificData)+static_cast<size_t>(info.ProtocolDataOffset);
    if(offset>returned || info.ProtocolDataLength>returned-offset)return "";
    try{return nvme_controller_serial(std::vector<unsigned char>(buffer.begin()+offset,buffer.begin()+offset+4096));}catch(const std::exception&){return "";}
}
std::vector<int> volume_disks(const std::wstring& volume,bool& resolved){
    auto device=volume;if(!device.empty() && device.back()==L'\\')device.pop_back();
    Handle h(CreateFileW(device.c_str(),GENERIC_READ,FILE_SHARE_READ|FILE_SHARE_WRITE,nullptr,OPEN_EXISTING,0,nullptr));
    resolved=false;if(!h)return {};
    std::vector<unsigned char> data(65536);DWORD returned=0;
    if(DeviceIoControl(h.value,IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS,nullptr,0,data.data(),static_cast<DWORD>(data.size()),&returned,nullptr)){
        auto extents=reinterpret_cast<const VOLUME_DISK_EXTENTS*>(data.data());
        if(extents->NumberOfDiskExtents>1024)return {};
        std::vector<int> disks;for(DWORD i=0;i<extents->NumberOfDiskExtents;++i)disks.push_back(static_cast<int>(extents->Extents[i].DiskNumber));
        std::sort(disks.begin(),disks.end());disks.erase(std::unique(disks.begin(),disks.end()),disks.end());resolved=!disks.empty();return disks;
    }
    STORAGE_DEVICE_NUMBER number{};
    if(DeviceIoControl(h.value,IOCTL_STORAGE_GET_DEVICE_NUMBER,nullptr,0,&number,sizeof(number),&returned,nullptr) && number.DeviceType==FILE_DEVICE_DISK){resolved=true;return {static_cast<int>(number.DeviceNumber)};}
    return {};
}
bool admin(){Handle token;if(!OpenProcessToken(GetCurrentProcess(),TOKEN_QUERY,&token.value))return false;TOKEN_ELEVATION value{};DWORD bytes=0;return GetTokenInformation(token.value,TokenElevation,&value,sizeof(value),&bytes) && value.TokenIsElevated;}
bool winpe(){HKEY key{};if(RegOpenKeyExW(HKEY_LOCAL_MACHINE,L"SYSTEM\\CurrentControlSet\\Control\\MiniNT",0,KEY_READ,&key)!=ERROR_SUCCESS)return false;RegCloseKey(key);return true;}
bool file_exists(const std::filesystem::path& p){const auto attrs=GetFileAttributesW(p.c_str());return attrs!=INVALID_FILE_ATTRIBUTES && !(attrs&FILE_ATTRIBUTE_DIRECTORY);}
std::filesystem::path absolute_native_path(const std::filesystem::path& p){
    // MinGW's filesystem::absolute can turn a UNC path into a drive-relative path.
    // Ask Windows to resolve it so SMB resources retain their server/share identity.
    std::wstring result(32768,L'\0');auto length=GetFullPathNameW(p.c_str(),result.size(),result.data(),nullptr);
    if(!length || length>=result.size())fail("Resolve absolute Windows path");result.resize(length);return std::filesystem::path(result);
}
std::wstring final_existing_path(const std::filesystem::path& p){
    auto path=absolute_native_path(p);while(!std::filesystem::exists(path)){auto parent=path.parent_path();if(parent==path || parent.empty())throw std::runtime_error("No existing ancestor for path");path=parent;}
    Handle h(CreateFileW(path.c_str(),0,FILE_SHARE_READ|FILE_SHARE_WRITE|FILE_SHARE_DELETE,nullptr,OPEN_EXISTING,FILE_FLAG_BACKUP_SEMANTICS,nullptr));
    if(!h)fail("Resolve resource path");std::wstring result(32768,L'\0');auto length=GetFinalPathNameByHandleW(h.value,result.data(),static_cast<DWORD>(result.size()),FILE_NAME_NORMALIZED|VOLUME_NAME_DOS);
    if(!length || length>=result.size())fail("Resolve final resource path");result.resize(length);return result;
}
unsigned long long free_bytes(const std::filesystem::path& p){ULARGE_INTEGER free{};auto parent=absolute_native_path(p.empty()?std::filesystem::current_path():p);while(!std::filesystem::exists(parent)){auto next=parent.parent_path();if(next==parent || next.empty())throw std::runtime_error("No existing volume for free-space check");parent=next;}if(!GetDiskFreeSpaceExW(parent.c_str(),&free,nullptr,nullptr))fail("Read volume free space");return free.QuadPart;}
int run_process(const std::vector<std::wstring>& args){
    auto line=command_line(args);STARTUPINFOW startup{};startup.cb=sizeof(startup);PROCESS_INFORMATION process{};
    // Preserve JSON-only stdout by sending child progress to the diagnostic stream.
    Handle input,output;auto current=GetCurrentProcess();
    if(!DuplicateHandle(current,GetStdHandle(STD_INPUT_HANDLE),current,&input.value,0,TRUE,DUPLICATE_SAME_ACCESS) ||
       !DuplicateHandle(current,GetStdHandle(STD_ERROR_HANDLE),current,&output.value,0,TRUE,DUPLICATE_SAME_ACCESS))fail("Prepare tool output");
    startup.dwFlags=STARTF_USESTDHANDLES;startup.hStdInput=input.value;startup.hStdOutput=output.value;startup.hStdError=output.value;
    if(!CreateProcessW(args.at(0).c_str(),line.data(),nullptr,nullptr,TRUE,0,nullptr,nullptr,&startup,&process))fail("Start deployment tool");
    Handle h(process.hProcess),thread(process.hThread);WaitForSingleObject(h.value,INFINITE);DWORD code=0;if(!GetExitCodeProcess(h.value,&code))fail("Get process result");return static_cast<int>(code);
}
class WimApi {
    HMODULE module=nullptr;
    template<class T>T load(const char* name){auto function=GetProcAddress(module,name);if(!function)throw std::runtime_error(std::string("Missing WIMGAPI function: ")+name);return reinterpret_cast<T>(function);}
public:
    using Create=HANDLE(WINAPI*)(PCWSTR,DWORD,DWORD,DWORD,DWORD,PDWORD);
    using Count=DWORD(WINAPI*)(HANDLE);
    using Info=BOOL(WINAPI*)(HANDLE,PVOID*,PDWORD);
    using Close=BOOL(WINAPI*)(HANDLE);
    Create create{};Count count{};Info info{};Close close{};
    WimApi(){auto path=system_directory()+L"\\wimgapi.dll";module=LoadLibraryExW(path.c_str(),nullptr,LOAD_LIBRARY_SEARCH_SYSTEM32);if(!module)fail("Load system WIMGAPI");create=load<Create>("WIMCreateFile");count=load<Count>("WIMGetImageCount");info=load<Info>("WIMGetImageInformation");close=load<Close>("WIMCloseHandle");}
    ~WimApi(){if(module)FreeLibrary(module);}
};
}

std::string utf8(const std::wstring& text){if(text.empty())return {};int length=WideCharToMultiByte(CP_UTF8,WC_ERR_INVALID_CHARS,text.data(),static_cast<int>(text.size()),nullptr,0,nullptr,nullptr);if(!length)fail("Encode UTF-8");std::string value(length,'\0');WideCharToMultiByte(CP_UTF8,WC_ERR_INVALID_CHARS,text.data(),static_cast<int>(text.size()),value.data(),length,nullptr,nullptr);return value;}
std::wstring wide(const std::string& text){if(text.empty())return {};int length=MultiByteToWideChar(CP_UTF8,MB_ERR_INVALID_CHARS,text.data(),static_cast<int>(text.size()),nullptr,0);if(!length)fail("Decode UTF-8");std::wstring value(length,L'\0');MultiByteToWideChar(CP_UTF8,MB_ERR_INVALID_CHARS,text.data(),static_cast<int>(text.size()),value.data(),length);return value;}
Json read_json(const std::filesystem::path& path){if(std::filesystem::file_size(path)>1024*1024)throw std::runtime_error("Configuration exceeds 1 MiB");std::ifstream stream(path,std::ios::binary);if(!stream)throw std::runtime_error("Cannot read configuration");return Json::parse(stream);}
void write_json(const std::filesystem::path& path,const Json& value){
    persist_state_file(path,value.dump(2)+"\n");
}

Json machine_specs(){
    constexpr DWORD provider=0x52534d42; // RSMB, Windows raw SMBIOS provider.
    Json specs;auto needed=GetSystemFirmwareTable(provider,0,nullptr,0);
    if(needed>=8 && needed<=8*1024*1024){std::vector<unsigned char> raw(needed);auto received=GetSystemFirmwareTable(provider,0,raw.data(),needed);
        DWORD size=0;if(received==needed)std::memcpy(&size,raw.data()+4,sizeof(size));
        if(size && size<=received-8)specs=smbios_specs(std::vector<unsigned char>(raw.begin()+8,raw.begin()+8+size),raw[1]>2||(raw[1]==2&&raw[2]>=6));
    }
    if(specs.is_null()){specs=smbios_specs({});specs["warnings"].push_back("Detailed firmware inventory was unavailable");}
    auto reg=[](const wchar_t* key,const wchar_t* field){std::array<wchar_t,512> b{};DWORD size=sizeof(b);return RegGetValueW(HKEY_LOCAL_MACHINE,key,field,RRF_RT_REG_SZ,nullptr,b.data(),&size)==ERROR_SUCCESS?utf8(b.data()):std::string{};};
    const auto bios=L"HARDWARE\\DESCRIPTION\\System\\BIOS";
    if(specs["system"].value("manufacturer","").empty())specs["system"]["manufacturer"]=reg(bios,L"SystemManufacturer");
    if(specs["system"].value("model","").empty())specs["system"]["model"]=reg(bios,L"SystemProductName");
    if(specs["processors"].empty()){
        auto name=reg(L"HARDWARE\\DESCRIPTION\\System\\CentralProcessor\\0",L"ProcessorNameString");
        if(!name.empty())specs["processors"].push_back({{"name",name},{"threads",GetActiveProcessorCount(ALL_PROCESSOR_GROUPS)}});
    }
    MEMORYSTATUSEX memory{};memory.dwLength=sizeof(memory);
    if(!specs["memory"].value("total_bytes",0ULL) && GlobalMemoryStatusEx(&memory))specs["memory"]["total_bytes"]=memory.ullTotalPhys;
    FIRMWARE_TYPE firmware=FirmwareTypeUnknown;GetFirmwareType(&firmware);specs["firmware"]["type"]=firmware==FirmwareTypeUefi?"uefi":firmware==FirmwareTypeBios?"bios":"unknown";
    DWORD secure=0,size=sizeof(secure);if(RegGetValueW(HKEY_LOCAL_MACHINE,L"SYSTEM\\CurrentControlSet\\Control\\SecureBoot\\State",L"UEFISecureBootEnabled",RRF_RT_REG_DWORD,nullptr,&secure,&size)==ERROR_SUCCESS)specs["firmware"]["secure_boot"]=secure!=0;
    auto tbs=LoadLibraryExW(L"tbs.dll",nullptr,LOAD_LIBRARY_SEARCH_SYSTEM32);
    if(tbs){using GetInfo=UINT32(WINAPI*)(UINT32,PVOID);auto fn=reinterpret_cast<GetInfo>(GetProcAddress(tbs,"Tbsi_GetDeviceInfo"));std::array<UINT32,4> info{};
        if(fn){auto code=fn(sizeof(info),info.data());if(!code)specs["firmware"]["tpm"]=info[1]==2?"TPM 2.0":info[1]==1?"TPM 1.2":"Present";else if(code==0x8028400f)specs["firmware"]["tpm"]="Absent";}FreeLibrary(tbs);}
    specs["graphics"]=Json::array();specs["network"]=Json::array();
    auto devices=SetupDiGetClassDevsW(nullptr,nullptr,nullptr,DIGCF_ALLCLASSES|DIGCF_PRESENT);
    if(devices!=INVALID_HANDLE_VALUE){
        for(DWORD index=0;index<4096;++index){SP_DEVINFO_DATA device{};device.cbSize=sizeof(device);if(!SetupDiEnumDeviceInfo(devices,index,&device))break;
            auto property=[&](DWORD key){std::array<wchar_t,512> buffer{};DWORD required=0,type=0;return SetupDiGetDeviceRegistryPropertyW(devices,&device,key,&type,reinterpret_cast<PBYTE>(buffer.data()),sizeof(buffer),&required)&&type==REG_SZ?utf8(buffer.data()):std::string{};};
            auto cls=property(SPDRP_CLASS);auto field=cls=="Display"?"graphics":cls=="Net"?"network":"";
            if(*field && specs[field].size()<32){auto name=property(SPDRP_FRIENDLYNAME);if(name.empty())name=property(SPDRP_DEVICEDESC);if(!name.empty())specs[field].push_back({{"name",name.substr(0,256)},{"manufacturer",property(SPDRP_MFG).substr(0,256)}});}
        }
        SetupDiDestroyDeviceInfoList(devices);
    }
    return specs;
}

Json inventory(){
    SYSTEM_INFO system{};GetNativeSystemInfo(&system);FIRMWARE_TYPE firmware=FirmwareTypeUnknown;GetFirmwareType(&firmware);
    Json disks=Json::array(),volumes=Json::array(),warnings=Json::array();
    for(int number=0;number<256;++number){
        auto path=L"\\\\.\\PhysicalDrive"+std::to_wstring(number);
        Handle disk(CreateFileW(path.c_str(),GENERIC_READ,FILE_SHARE_READ|FILE_SHARE_WRITE,nullptr,OPEN_EXISTING,0,nullptr));
        if(!disk){if(GetLastError()==ERROR_ACCESS_DENIED)warnings.push_back("Access denied for PhysicalDrive"+std::to_string(number));continue;}
        DWORD returned=0;GET_LENGTH_INFORMATION length{};if(!DeviceIoControl(disk.value,IOCTL_DISK_GET_LENGTH_INFO,nullptr,0,&length,sizeof(length),&returned,nullptr))continue;
        STORAGE_PROPERTY_QUERY query{};query.PropertyId=StorageDeviceProperty;query.QueryType=PropertyStandardQuery;std::vector<unsigned char> buffer(65536);
        bool descriptor_ok=DeviceIoControl(disk.value,IOCTL_STORAGE_QUERY_PROPERTY,&query,sizeof(query),buffer.data(),static_cast<DWORD>(buffer.size()),&returned,nullptr);
        std::string serial,model;bool removable=true;int bus=0;
        if(descriptor_ok && returned>=sizeof(STORAGE_DEVICE_DESCRIPTOR)){
            auto descriptor=reinterpret_cast<const STORAGE_DEVICE_DESCRIPTOR*>(buffer.data());
            buffer.resize(returned);serial=native_disk_serial(disk.value,buffer);model=descriptor_string(buffer,descriptor->ProductIdOffset);removable=descriptor->RemovableMedia || descriptor->BusType==BusTypeUsb;bus=descriptor->BusType;
        }
        bool writable=DeviceIoControl(disk.value,IOCTL_DISK_IS_WRITABLE,nullptr,0,nullptr,0,&returned,nullptr);
        disks.push_back({{"number",number},{"devicePath",utf8(path)},{"sizeBytes",length.Length.QuadPart},
            {"model",model},{"serial",serial},{"identity",serial.empty()?"unstable:PhysicalDrive"+std::to_string(number):"serial:"+serial},
            {"stableIdentity",!serial.empty()},{"removable",removable},{"writable",writable},{"busType",bus}});
    }
    std::array<wchar_t,32768> name{};HANDLE search=FindFirstVolumeW(name.data(),static_cast<DWORD>(name.size()));
    if(search!=INVALID_HANDLE_VALUE){
        do{
            std::wstring volume=name.data();DWORD flags=0,serial=0,max_component=0;std::array<wchar_t,256> filesystem{},label{};
            if(!GetVolumeInformationW(volume.c_str(),label.data(),static_cast<DWORD>(label.size()),&serial,&max_component,&flags,filesystem.data(),static_cast<DWORD>(filesystem.size())))continue;
            std::vector<wchar_t> paths(32768);DWORD need=0;Json mounts=Json::array();
            if(GetVolumePathNamesForVolumeNameW(volume.c_str(),paths.data(),static_cast<DWORD>(paths.size()),&need))for(auto current=paths.data();*current;current+=wcslen(current)+1)mounts.push_back(utf8(current));
            bool resolved=false;auto numbers=volume_disks(volume,resolved);ULARGE_INTEGER free{},total{};bool space_ok=GetDiskFreeSpaceExW(volume.c_str(),&free,&total,nullptr);
            bool windows=file_exists(std::filesystem::path(volume)/L"Windows\\System32\\config\\SYSTEM") && file_exists(std::filesystem::path(volume)/L"Windows\\System32\\ntoskrnl.exe");
            volumes.push_back({{"volume",utf8(volume)},{"mountPoints",mounts},{"label",utf8(label.data())},{"filesystem",utf8(filesystem.data())},
                {"diskNumbers",numbers},{"mappingResolved",resolved},{"sizeBytes",space_ok?total.QuadPart:0},
                {"usedBytes",space_ok?total.QuadPart-free.QuadPart:0},{"windowsInstallation",windows}});
        }while(FindNextVolumeW(search,name.data(),static_cast<DWORD>(name.size())));FindVolumeClose(search);
    }
    auto bios=[](const wchar_t* field){std::array<wchar_t,512> b{};DWORD size=sizeof(b);return RegGetValueW(HKEY_LOCAL_MACHINE,L"HARDWARE\\DESCRIPTION\\System\\BIOS",field,RRF_RT_REG_SZ,nullptr,b.data(),&size)==ERROR_SUCCESS?utf8(b.data()):std::string{};};
    auto specs=machine_specs();specs["storage"]=Json::array();
    for(const auto& disk:disks)if(specs["storage"].size()<32){auto bus=disk.at("busType").get<int>();
        specs["storage"].push_back({{"identity",disk.at("identity")},{"serial",disk.at("serial")},{"model",disk.at("model")},
            {"size_bytes",disk.at("sizeBytes")},{"removable",disk.at("removable")},
            {"bus_type",bus==17?"NVMe":bus==11?"SATA":bus==7?"USB":bus==1?"SCSI":bus==8?"RAID":"Other"}});
    }
    return {{"specs",specs},{"manufacturer",bios(L"SystemManufacturer")},{"model",bios(L"SystemProductName")},{"schemaVersion",1},{"toolVersion","0.14.1"},{"readOnly",true},{"environment",{{"winpe",winpe()},{"administrator",admin()},
        {"firmware",firmware==FirmwareTypeUefi?"uefi":firmware==FirmwareTypeBios?"bios":"unknown"},
        {"architecture",system.wProcessorArchitecture==PROCESSOR_ARCHITECTURE_AMD64?"amd64":"unsupported"},
        {"executable",utf8(executable())}}},{"disks",disks},{"volumes",volumes},{"warnings",warnings}};
}

Json inspect_wim(const std::filesystem::path& path,int index){
    WimApi api;DWORD result=0;auto handle=api.create(path.c_str(),0x80000000,3,0,0,&result);if(!handle)fail("Open WIM read-only");
    struct Guard{WimApi& api;HANDLE h;~Guard(){api.close(h);}}guard{api,handle};
    PVOID buffer=nullptr;DWORD size=0;if(!api.info(handle,&buffer,&size))fail("Read WIM metadata");
    std::unique_ptr<void,decltype(&LocalFree)> memory(buffer,LocalFree);
    if(size>16*1024*1024 || size<2)throw std::runtime_error("Invalid WIM metadata size");
    pugi::xml_document document;auto parsed=document.load_buffer(buffer,size,pugi::parse_default,pugi::encoding_auto);
    if(!parsed)throw std::runtime_error("Invalid WIM metadata XML");
    std::ostringstream output;document.save(output,"",pugi::format_raw,pugi::encoding_utf8);
    auto image=inspect_xml(output.str(),index);image["path"]=utf8(std::filesystem::absolute(path).wstring());image["imageCount"]=api.count(handle);image["fileBytes"]=std::filesystem::file_size(path);return image;
}

std::string sha256(const std::filesystem::path& path){
    BCRYPT_ALG_HANDLE algorithm=nullptr;BCRYPT_HASH_HANDLE hash=nullptr;
    if(BCryptOpenAlgorithmProvider(&algorithm,BCRYPT_SHA256_ALGORITHM,nullptr,0)<0)throw std::runtime_error("SHA-256 provider unavailable");
    struct Guard{BCRYPT_ALG_HANDLE& alg;BCRYPT_HASH_HANDLE& h;~Guard(){if(h)BCryptDestroyHash(h);if(alg)BCryptCloseAlgorithmProvider(alg,0);}}guard{algorithm,hash};
    DWORD object_size=0,returned=0;if(BCryptGetProperty(algorithm,BCRYPT_OBJECT_LENGTH,reinterpret_cast<PUCHAR>(&object_size),sizeof(object_size),&returned,0)<0)throw std::runtime_error("SHA-256 property failed");
    std::vector<unsigned char> object(object_size),chunk(1024*1024);std::array<unsigned char,32> digest{};
    if(BCryptCreateHash(algorithm,&hash,object.data(),object_size,nullptr,0,0)<0)throw std::runtime_error("SHA-256 initialization failed");
    Handle file(CreateFileW(path.c_str(),GENERIC_READ,FILE_SHARE_READ,nullptr,OPEN_EXISTING,FILE_FLAG_SEQUENTIAL_SCAN,nullptr));if(!file)fail("Read image for SHA-256");
    while(true){DWORD size=0;if(!ReadFile(file.value,chunk.data(),static_cast<DWORD>(chunk.size()),&size,nullptr))fail("Hash image data");if(!size)break;if(BCryptHashData(hash,chunk.data(),size,0)<0)throw std::runtime_error("SHA-256 update failed");}
    if(BCryptFinishHash(hash,digest.data(),static_cast<ULONG>(digest.size()),0)<0)throw std::runtime_error("SHA-256 finalization failed");
    std::ostringstream out;for(auto c:digest)out<<std::hex<<std::setw(2)<<std::setfill('0')<<static_cast<unsigned int>(c);return out.str();
}

namespace {
std::string answer_content(const Json& config,const char* role){
    const auto& file=config.at("answerFiles").at(role);auto content=file.at("content").get<std::string>();validate_answer_xml(content,role);
    BCRYPT_ALG_HANDLE algorithm=nullptr;BCRYPT_HASH_HANDLE hash=nullptr;
    if(BCryptOpenAlgorithmProvider(&algorithm,BCRYPT_SHA256_ALGORITHM,nullptr,0)<0)throw std::runtime_error("Answer SHA-256 provider unavailable");
    struct Guard{BCRYPT_ALG_HANDLE& a;BCRYPT_HASH_HANDLE& h;~Guard(){if(h)BCryptDestroyHash(h);if(a)BCryptCloseAlgorithmProvider(a,0);}}guard{algorithm,hash};
    DWORD size=0,returned=0;if(BCryptGetProperty(algorithm,BCRYPT_OBJECT_LENGTH,reinterpret_cast<PUCHAR>(&size),sizeof(size),&returned,0)<0)throw std::runtime_error("Answer SHA-256 property failed");
    std::vector<unsigned char> object(size);std::array<unsigned char,32> digest{};
    if(BCryptCreateHash(algorithm,&hash,object.data(),size,nullptr,0,0)<0 ||
       BCryptHashData(hash,reinterpret_cast<PUCHAR>(content.data()),static_cast<ULONG>(content.size()),0)<0 ||
       BCryptFinishHash(hash,digest.data(),static_cast<ULONG>(digest.size()),0)<0)throw std::runtime_error("Answer SHA-256 failed");
    std::ostringstream out;for(auto c:digest)out<<std::hex<<std::setw(2)<<std::setfill('0')<<static_cast<unsigned int>(c);
    if(out.str()!=file.at("sha256").get<std::string>())throw std::runtime_error("Answer file SHA-256 differs from the pinned selection");
    return content;
}
void validate_answer_bindings(const Json& config){if(config.contains("answerFiles"))for(const auto* role:{"audit","oobe"})answer_content(config,role);}
}

Json locate_resource(const std::filesystem::path& path,const std::string& role){
    Json result={{"role",role},{"path",utf8(absolute_native_path(path).wstring())},{"resolved",false},{"diskNumbers",Json::array()}};
    try{
        auto final=final_existing_path(path);result["resolvedPath"]=utf8(final);
        if(final.rfind(L"\\\\?\\UNC\\",0)==0 || (final.rfind(L"\\\\",0)==0 && final.rfind(L"\\\\?\\",0)!=0)){
            result["type"]="network";result["resolved"]=true;return result;
        }
        std::array<wchar_t,32768> root{};if(!GetVolumePathNameW(final.c_str(),root.data(),static_cast<DWORD>(root.size())))return result;
        auto type=GetDriveTypeW(root.data());
        // WinPE RAM volumes can report DRIVE_FIXED. Query the kernel device mapping,
        // rather than assuming that a particular drive letter is a RAM disk.
        std::array<wchar_t,32768> mapping{};std::wstring dos_root=root.data();
        if(dos_root.rfind(L"\\\\?\\",0)==0)dos_root=dos_root.substr(4);
        while(!dos_root.empty() && dos_root.back()==L'\\')dos_root.pop_back();
        bool mapped_ram=QueryDosDeviceW(dos_root.c_str(),mapping.data(),static_cast<DWORD>(mapping.size())) &&
            std::wstring(mapping.data()).rfind(L"\\Device\\Ramdisk",0)==0;
        if(type==DRIVE_RAMDISK || mapped_ram){result["type"]="ramdisk";result["resolved"]=true;return result;}
        if(type==DRIVE_REMOTE){result["type"]="network";result["resolved"]=true;return result;}
        std::array<wchar_t,32768> volume{};if(!GetVolumeNameForVolumeMountPointW(root.data(),volume.data(),static_cast<DWORD>(volume.size())))return result;
        bool resolved=false;auto disks=volume_disks(volume.data(),resolved);result["type"]="local";result["volume"]=utf8(volume.data());result["diskNumbers"]=disks;result["resolved"]=resolved;
    }catch(const std::exception&){result["error"]="Path could not be resolved";}
    return result;
}

namespace {
void no_reparse(const std::filesystem::path&);
void protect_path(const std::filesystem::path&);
void verify_protected(const std::filesystem::path&);
struct PackageInput {
    Json manifest;
    std::filesystem::path directory;
    std::vector<std::unique_ptr<Handle>> locks;
};
PackageInput read_driver_package(const Json& config){
    PackageInput result;if(!config.contains("driverManifest"))return result;
    auto path=std::filesystem::path(wide(config.at("driverManifest")));no_reparse(path);
    result.locks.push_back(std::make_unique<Handle>(CreateFileW(path.c_str(),GENERIC_READ,FILE_SHARE_READ,nullptr,OPEN_EXISTING,0,nullptr)));
    if(!*result.locks.back())fail("Lock driver manifest");
    if(sha256(path)!=config.at("driverManifestSha256").get<std::string>())throw std::runtime_error("Driver manifest SHA-256 mismatch");
    result.manifest=read_json(path);validate_driver_manifest(result.manifest);result.directory=path.parent_path();
    for(const auto& f:result.manifest.at("files")){
        auto file=result.directory/wide(f.at("path"));no_reparse(file);
        result.locks.push_back(std::make_unique<Handle>(CreateFileW(file.c_str(),GENERIC_READ,FILE_SHARE_READ,nullptr,OPEN_EXISTING,0,nullptr)));
        if(!*result.locks.back())fail("Lock approved driver file");
        if(!std::filesystem::is_regular_file(file) || std::filesystem::file_size(file)!=f.at("size") || sha256(file)!=f.at("sha256").get<std::string>())throw std::runtime_error("Approved driver file integrity differs");
    }
    return result;
}
Json read_delivery_config(const Json& config){
    if(!config.contains("reportDelivery"))return nullptr;
    auto path=std::filesystem::path(wide(config.at("reportDelivery")));no_reparse(path);auto d=read_json(path);
    if(!d.is_object() || d.size()!=3 || d.at("schemaVersion")!=1 || !d.at("token").is_string() ||
       !std::regex_match(d.at("token").get<std::string>(),std::regex("[A-Za-z0-9_-]{40,128}")) ||
       !std::regex_match(d.at("endpoint").get<std::string>(),std::regex("https://[A-Za-z0-9.-]+(:[0-9]{1,5})?/api/client-jobs/[a-f0-9-]{36}/report")))throw std::runtime_error("Invalid job-scoped HTTPS report configuration");
    return d;
}
}

Json plan_from_config(const Json& config,const std::filesystem::path& config_path){
    validate_config(config);validate_answer_bindings(config);auto inv=inventory();auto image=inspect_wim(wide(config.at("image")),config.value("imageIndex",1));
    for(const auto* field:{"scratchDirectory","jobDirectory"})if(!std::filesystem::is_directory(wide(config.at(field))))throw std::runtime_error(std::string(field)+" must already exist");
    Json resources=Json::array({locate_resource(wide(config.at("image")),"image"),locate_resource(wide(config.at("scratchDirectory")),"scratch"),
        locate_resource(wide(config.at("jobDirectory")),"logs and state"),locate_resource(config_path,"configuration"),locate_resource(executable(),"runtime")});
    auto package=read_driver_package(config);
    if(!package.manifest.is_null()){
        resources.push_back(locate_resource(wide(config.at("driverManifest")),"driver manifest"));
        for(const auto& f:package.manifest.at("files"))resources.push_back(locate_resource(package.directory/wide(f.at("path")),"approved driver file"));
    }
    if(config.contains("reportDelivery")){read_delivery_config(config);resources.push_back(locate_resource(wide(config.at("reportDelivery")),"report credential"));}
    auto plan=deployment_plan(config,inv,image,sha256(wide(config.at("image"))),resources);return plan;
}

Json capture_from_config(const Json& config,const std::filesystem::path& config_path,bool execute){
    auto inv=inventory();const auto source_path=std::filesystem::path(wide(config.at("sourceVolume")));auto resource=locate_resource(source_path,"source");
    Json source;for(const auto& volume:inv["volumes"])if(resource.contains("volume") && volume["volume"]==resource["volume"])source=volume;
    if(source.is_null())throw std::runtime_error("Source volume was not discovered");
    const auto source_absolute=std::filesystem::absolute(source_path).lexically_normal();bool root=false;
    for(const auto& mount:source["mountPoints"])if(std::filesystem::path(wide(mount)).lexically_normal()==source_absolute)root=true;
    if(!root && std::filesystem::path(wide(source["volume"])).lexically_normal()!=source_absolute)throw std::runtime_error("Capture source must be the discovered volume root");
    auto destination=std::filesystem::path(wide(config.at("destination")));if(destination.extension()!=L".wim")throw std::runtime_error("Capture destination must end with .wim");
    if(std::filesystem::exists(destination))throw std::runtime_error("Capture destination already exists; overwrites are refused");
    for(const auto* field:{"scratchDirectory","jobDirectory"})if(!std::filesystem::is_directory(wide(config.at(field))))throw std::runtime_error(std::string(field)+" must already exist");
    Json resources=Json::array({locate_resource(destination,"destination"),locate_resource(wide(config.at("scratchDirectory")),"scratch"),locate_resource(wide(config.at("jobDirectory")),"logs and state")});
    auto preflight=capture_preflight(config,inv,source,resources,free_bytes(destination.parent_path()),free_bytes(wide(config.at("scratchDirectory"))));
    if(!execute)return preflight;
    if(!inv["environment"].value("administrator",false))throw std::runtime_error("Capture requires administrator rights");
    if(!std::filesystem::is_directory(destination.parent_path()))throw std::runtime_error("Capture destination parent must exist");
    auto id=sha256(config_path).substr(0,16);auto state_path=std::filesystem::path(wide(config.at("jobDirectory")))/(L"capture-"+wide(id)+L".json");
    if(std::filesystem::exists(state_path))throw std::runtime_error("Capture job state already exists; review it before creating a new capture");
    auto pending=destination;pending+=L".incomplete";if(std::filesystem::exists(pending))throw std::runtime_error("An incomplete capture already exists");
    Json state={{"schemaVersion",1},{"id",id},{"stage","capturing"},{"startedUtc",utc_now()},{"source",source},{"referencePreparation",config.at("referencePreparation")},{"destination",utf8(destination.wstring())}};
    write_json(state_path,state);
    try{
        auto code=run_process({system_directory()+L"\\dism.exe",L"/Capture-Image",L"/ImageFile:"+pending.wstring(),L"/CaptureDir:"+source_path.wstring(),
            L"/Name:"+wide(config.at("name")),L"/Compress:max",L"/CheckIntegrity",L"/Verify",L"/ScratchDir:"+wide(config.at("scratchDirectory")),L"/LogPath:"+(state_path.wstring()+L".dism.log")});
        if(code!=0)throw std::runtime_error("DISM capture failed with exit code "+std::to_string(code));
        auto image=inspect_wim(pending,1);if(image["selectedImage"].value("architecture","")!="9")throw std::runtime_error("Captured image lacks AMD64 Windows metadata");
        auto hash=sha256(pending);
        image["path"]=utf8(destination.wstring());
        auto metadata=Json{{"schemaVersion",1},{"id",id},{"imageIndex",1},{"sha256",hash},{"size",std::filesystem::file_size(pending)},
            {"name",config.at("name")},{"capturedUtc",utc_now()},{"referencePreparation",config.at("referencePreparation")},{"wim",image},{"stage","validated"}};
        auto sidecar=destination;sidecar+=L".json";if(std::filesystem::exists(sidecar))throw std::runtime_error("Capture metadata sidecar already exists");
        write_json(sidecar,metadata);
        if(!MoveFileExW(pending.c_str(),destination.c_str(),MOVEFILE_WRITE_THROUGH))fail("Publish completed capture");
        state["stage"]="complete";state["completedUtc"]=utc_now();state["sha256"]=hash;write_json(state_path,state);return state;
    }catch(const std::exception& e){state["stage"]="failed";state["failedUtc"]=utc_now();state["error"]=e.what();write_json(state_path,state);throw;}
}

namespace {
GUID guid(const wchar_t* text){GUID g{};if(FAILED(CLSIDFromString(text,&g)))throw std::runtime_error("Invalid internal GUID");return g;}
GUID new_guid(){GUID g{};if(FAILED(CoCreateGuid(&g)))throw std::runtime_error("Cannot create partition GUID");return g;}
std::string guid_text(const GUID& g){wchar_t buffer[40]{};if(!StringFromGUID2(g,buffer,40))throw std::runtime_error("Cannot encode GUID");return utf8(buffer);}
void ioctl(HANDLE h,DWORD code,void* input,DWORD size,const char* label){DWORD n=0;if(!DeviceIoControl(h,code,input,size,nullptr,0,&n,nullptr))fail(label);}
std::vector<unsigned char> disk_layout(HANDLE h){std::vector<unsigned char> b(128*1024);DWORD n=0;if(!DeviceIoControl(h,IOCTL_DISK_GET_DRIVE_LAYOUT_EX,nullptr,0,b.data(),static_cast<DWORD>(b.size()),&n,nullptr))fail("Read partition layout");if(n<offsetof(DRIVE_LAYOUT_INFORMATION_EX,PartitionEntry))throw std::runtime_error("Invalid partition layout size");b.resize(n);return b;}
void check_bound_disk(HANDLE h,const Json& target){
    GET_LENGTH_INFORMATION length{};DWORD n=0;
    if(!DeviceIoControl(h,IOCTL_DISK_GET_LENGTH_INFO,nullptr,0,&length,sizeof(length),&n,nullptr))fail("Revalidate target capacity");
    STORAGE_PROPERTY_QUERY q{};q.PropertyId=StorageDeviceProperty;q.QueryType=PropertyStandardQuery;
    std::vector<unsigned char> bytes(65536);
    if(!DeviceIoControl(h,IOCTL_STORAGE_QUERY_PROPERTY,&q,sizeof(q),bytes.data(),static_cast<DWORD>(bytes.size()),&n,nullptr) || n<sizeof(STORAGE_DEVICE_DESCRIPTOR))fail("Revalidate target identity");
    auto d=reinterpret_cast<const STORAGE_DEVICE_DESCRIPTOR*>(bytes.data());bytes.resize(n);
    if("serial:"+native_disk_serial(h,bytes)!=target.at("identity").get<std::string>() || static_cast<unsigned long long>(length.Length.QuadPart)!=target.at("sizeBytes").get<unsigned long long>() || d->RemovableMedia || d->BusType==BusTypeUsb)throw std::runtime_error("Bound target identity/capacity changed");
    if(!DeviceIoControl(h,IOCTL_DISK_IS_WRITABLE,nullptr,0,nullptr,0,&n,nullptr))fail("Revalidate target writeability");
}
void verify_zero_disk(HANDLE h,unsigned long long size){
    LARGE_INTEGER begin{};if(!SetFilePointerEx(h,begin,nullptr,FILE_BEGIN))fail("Seek blank target");
    std::vector<unsigned char> b(4*MiB);unsigned long long offset=0;
    while(offset<size){DWORD n=0;auto request=static_cast<DWORD>(std::min<unsigned long long>(b.size(),size-offset));
        if(!ReadFile(h,b.data(),request,&n,nullptr) || n!=request)fail("Scan blank target");
        if(!std::all_of(b.begin(),b.begin()+n,[](unsigned char c){return c==0;}))throw std::runtime_error("Deployment refuses a RAW disk containing any data");offset+=n;
    }
}
unsigned int sector_size(HANDLE h){DISK_GEOMETRY g{};DWORD n=0;if(!DeviceIoControl(h,IOCTL_DISK_GET_DRIVE_GEOMETRY,nullptr,0,&g,sizeof(g),&n,nullptr))fail("Read target sector size");return g.BytesPerSector;}
int run_logged(const std::vector<std::wstring>& args,const std::filesystem::path& log,const std::string& input="",DWORD timeout=INFINITE,const std::function<void(int)>& on_progress={}){
    SECURITY_ATTRIBUTES sa{sizeof(sa),nullptr,TRUE};Handle output(CreateFileW(log.c_str(),GENERIC_WRITE,FILE_SHARE_READ,&sa,CREATE_NEW,FILE_ATTRIBUTE_NORMAL,nullptr));if(!output)fail("Create deployment tool log");
    Handle read,write;if(!CreatePipe(&read.value,&write.value,&sa,0))fail("Create deployment input");if(!SetHandleInformation(write.value,HANDLE_FLAG_INHERIT,0))fail("Protect deployment input");
    if(!input.empty()){DWORD n=0;if(!WriteFile(write.value,input.data(),static_cast<DWORD>(input.size()),&n,nullptr) || n!=input.size())fail("Write tool confirmation");}
    CloseHandle(write.value);write.value=INVALID_HANDLE_VALUE;
    STARTUPINFOW start{};start.cb=sizeof(start);start.dwFlags=STARTF_USESTDHANDLES;start.hStdInput=read.value;start.hStdOutput=output.value;start.hStdError=output.value;
    PROCESS_INFORMATION p{};auto line=command_line(args);
    if(!CreateProcessW(args.at(0).c_str(),line.data(),nullptr,nullptr,TRUE,CREATE_NO_WINDOW,nullptr,nullptr,&start,&p))fail("Start deployment tool");
    Handle process(p.hProcess),thread(p.hThread);
    Handle reader;InstallProgress parser;
    if(on_progress)reader.value=CreateFileW(log.c_str(),GENERIC_READ,FILE_SHARE_READ|FILE_SHARE_WRITE,nullptr,OPEN_EXISTING,FILE_ATTRIBUTE_NORMAL,nullptr);
    auto observe=[&]{
        if(!reader)return;std::array<char,65536> bytes{};DWORD count=0;
        if(ReadFile(reader.value,bytes.data(),static_cast<DWORD>(bytes.size()),&count,nullptr) && count){
            for(int percent:parser.feed(std::string_view(bytes.data(),count)))try{on_progress(percent);}catch(...){/* A display/report failure must not abandon a running disk operation. */}
        }
    };
    auto started=GetTickCount64();DWORD waited;
    do{auto elapsed=GetTickCount64()-started;
       auto remaining=timeout==INFINITE?INFINITE:static_cast<DWORD>(elapsed>=timeout?0:timeout-elapsed);
       waited=WaitForSingleObject(process.value,on_progress?std::min<DWORD>(250,remaining):remaining);
       if(on_progress)observe();
       if(waited!=WAIT_TIMEOUT || (timeout!=INFINITE && GetTickCount64()-started>=timeout))break;
    }while(true);
    if(on_progress && waited==WAIT_OBJECT_0){FlushFileBuffers(output.value);observe();}
    if(waited==WAIT_TIMEOUT){TerminateProcess(process.value,ERROR_TIMEOUT);WaitForSingleObject(process.value,5000);FlushFileBuffers(output.value);return ERROR_TIMEOUT;}
    if(waited!=WAIT_OBJECT_0)fail("Wait for deployment tool");DWORD code=0;if(!GetExitCodeProcess(process.value,&code))fail("Read deployment tool result");FlushFileBuffers(output.value);return static_cast<int>(code);
}
std::wstring find_partition_volume(const GUID& partition,HANDLE disk,const Json& target){
    for(int attempt=0;attempt<60;++attempt){
        check_bound_disk(disk,target);std::array<wchar_t,32768> name{};HANDLE search=FindFirstVolumeW(name.data(),static_cast<DWORD>(name.size()));std::wstring found;
        if(search!=INVALID_HANDLE_VALUE){do{
            std::wstring path=name.data(),device=path;if(device.back()==L'\\')device.pop_back();
            Handle h(CreateFileW(device.c_str(),0,FILE_SHARE_READ|FILE_SHARE_WRITE,nullptr,OPEN_EXISTING,0,nullptr));if(!h)continue;
            PARTITION_INFORMATION_EX info{};DWORD n=0;if(!DeviceIoControl(h.value,IOCTL_DISK_GET_PARTITION_INFO_EX,nullptr,0,&info,sizeof(info),&n,nullptr))continue;
            if(info.PartitionStyle==PARTITION_STYLE_GPT && IsEqualGUID(info.Gpt.PartitionId,partition)){
                bool resolved=false;auto disks=volume_disks(path,resolved);if(!resolved || disks!=std::vector<int>{target.at("number").get<int>()}){FindVolumeClose(search);throw std::runtime_error("New partition maps to an unexpected disk");}
                if(!found.empty()){FindVolumeClose(search);throw std::runtime_error("New partition GUID is ambiguous");}found=path;
            }
        }while(FindNextVolumeW(search,name.data(),static_cast<DWORD>(name.size())));FindVolumeClose(search);}
        if(!found.empty())return found;Sleep(1000);
    }throw std::runtime_error("New partition volume did not arrive");
}
class Mounts {
public:
    std::vector<std::wstring> roots;
    ~Mounts(){for(const auto& root:roots)DeleteVolumeMountPointW(root.c_str());}
    std::wstring assign(const std::wstring& volume,wchar_t preferred){
        // WinPE may already assign basic-data partitions a letter. Windows rejects
        // assigning a second drive letter; reuse the existing mapping and verify it.
        std::vector<wchar_t> paths(32768);DWORD needed=0;
        if(!GetVolumePathNamesForVolumeNameW(volume.c_str(),paths.data(),static_cast<DWORD>(paths.size()),&needed))fail("Read generated partition mounts");
        for(auto p=paths.data();*p;p+=wcslen(p)+1)if(wcslen(p)==3 && p[1]==L':' && p[2]==L'\\')return p;
        auto used=GetLogicalDrives();wchar_t letter=preferred;
        if(used&(1u<<(letter-L'A'))){letter=0;for(wchar_t c=L'Z';c>=L'D';--c)if(c!=L'X' && !(used&(1u<<(c-L'A')))){letter=c;break;}}
        if(!letter)throw std::runtime_error("No free drive letter for deployment");std::wstring root{letter,L':',L'\\'};
        if(!SetVolumeMountPointW(root.c_str(),volume.c_str()))fail("Mount generated partition");roots.push_back(root);return root;
    }
};
std::string read_tool_text(const std::filesystem::path& p){std::ifstream stream(p,std::ios::binary);std::string s((std::istreambuf_iterator<char>(stream)),{});if(s.size()>16*1024*1024)throw std::runtime_error("Tool output exceeded bound");s.erase(std::remove(s.begin(),s.end(),'\0'),s.end());return s;}
std::string loader_guid(const std::string& text){
    std::regex pattern(R"(\{[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\})");
    std::string match;size_t start=0;
    while(start<text.size()){auto end=text.find("\n\r\n",start);if(end==std::string::npos)end=text.find("\n\n",start);if(end==std::string::npos)end=text.size();auto block=text.substr(start,end-start);
        if(block.find("winload.efi")!=std::string::npos){std::smatch m;if(!std::regex_search(block,m,pattern) || !match.empty())throw std::runtime_error("BCD Windows loader identifier is ambiguous");match=m.str();}start=end+1;
    }if(match.empty())throw std::runtime_error("BCD Windows loader identifier was not found");return match;
}
}

namespace { std::vector<unsigned char> public_root(const std::filesystem::path&); void stage_completion(const std::filesystem::path&,const Json&,const GUID&,const GUID&,const std::string&,const Json&,bool,const Json&); }

namespace {
Json deploy_internal(Json config,const std::filesystem::path& config_path,bool execute,const std::function<void(const Json&)>& progress,
                     const Json* metadata,const std::function<void(Json&,const std::filesystem::path&)>& source){
    if(!winpe())throw std::runtime_error("Deployment execution requires WinPE");
    validate_config(config);validate_answer_bindings(config);
    std::unique_ptr<Handle> image_lock;Json plan;
    if(metadata){
        // Require the transferable CA before any destination write, not just WinPE trust.
        public_root(std::filesystem::path(executable()).parent_path()/L"server-root.crt");
        const auto inv=inventory();auto resources=Json::array({locate_resource(executable(),"runtime"),locate_resource(config_path,"configuration"),locate_resource(wide(config.at("jobDirectory")),"logs and state")});
        for(const auto& r:resources)if(r.value("type","")!="ramdisk")throw std::runtime_error("PXE runtime and initial state must be in WinPE RAM");
        plan=deployment_plan(config,inv,*metadata,config.at("sha256"),resources);
        auto expanded=plan.at("image").at("totalBytes").get<unsigned long long>();auto download=metadata->at("downloadBytes").get<unsigned long long>();
        if(!download || download>100ULL*GiB || download+expanded+4*GiB>plan.at("target").at("sizeBytes").get<unsigned long long>()-2*GiB)throw std::runtime_error("Target lacks space for Windows and temporary PXE staging");
    }else{
        image_lock=std::make_unique<Handle>(CreateFileW(std::filesystem::path(wide(config.at("image"))).c_str(),GENERIC_READ,FILE_SHARE_READ,nullptr,OPEN_EXISTING,FILE_FLAG_SEQUENTIAL_SCAN,nullptr));if(!*image_lock)fail("Lock deployment image read-only");
        read_delivery_config(config);plan=plan_from_config(config,config_path);
    }
    auto validate_hardware=[&](const Json& manifest){
        if(manifest.is_null())return;
        auto hardware=inventory();hardware["build"]=plan.at("image").at("build");
        if(!driver_package_matches(manifest,hardware))throw std::runtime_error("Reviewed drivers do not match deployment hardware/build");
    };
    auto package_lock=read_driver_package(config);validate_hardware(package_lock.manifest);
    offline_driver_plan(config,package_lock.manifest);
    if(!admin())throw std::runtime_error("Deployment requires administrator rights");
    auto target=plan.at("target");Handle preflight_disk(CreateFileW(wide(target.at("devicePath")).c_str(),GENERIC_READ,FILE_SHARE_READ|FILE_SHARE_WRITE,nullptr,OPEN_EXISTING,0,nullptr));if(!preflight_disk)fail("Open selected target");
    check_bound_disk(preflight_disk.value,target);auto current_bytes=disk_layout(preflight_disk.value);auto current=reinterpret_cast<const DRIVE_LAYOUT_INFORMATION_EX*>(current_bytes.data());
    if(current->PartitionCount>128 || current_bytes.size()<offsetof(DRIVE_LAYOUT_INFORMATION_EX,PartitionEntry)+current->PartitionCount*sizeof(PARTITION_INFORMATION_EX))
        throw std::runtime_error("Target partition layout is invalid or exceeds the supported limit");
    bool uninitialized=current->PartitionStyle==PARTITION_STYLE_RAW && current->PartitionCount==0;
    // Some disk drivers describe a zeroed disk as MBR with four unused entries.
    // Accept only that empty synthetic layout, then still scan every byte for zero.
    if(current->PartitionStyle==PARTITION_STYLE_MBR && current->Mbr.Signature==0 && current->PartitionCount<=128 &&
       current_bytes.size()>=offsetof(DRIVE_LAYOUT_INFORMATION_EX,PartitionEntry)+current->PartitionCount*sizeof(PARTITION_INFORMATION_EX)){
        uninitialized=true;for(DWORD i=0;i<current->PartitionCount;++i){const auto& p=current->PartitionEntry[i];if(p.StartingOffset.QuadPart || p.PartitionLength.QuadPart || p.Mbr.PartitionType)uninitialized=false;}
    }
    const auto target_policy=config.value("targetPolicy","");
    const bool blank_only=target_policy=="blank-raw";
    if(target_policy!="blank-raw" && target_policy!="format-selected-disk")throw std::runtime_error("Deployment requires an explicit supported target disk policy");
    std::string style=current->PartitionStyle==PARTITION_STYLE_RAW?"raw":current->PartitionStyle==PARTITION_STYLE_MBR?"mbr":current->PartitionStyle==PARTITION_STYLE_GPT?"gpt":"unsupported";
    Json status={{"partitionStyle",uninitialized?"raw":style},{"partitionCount",uninitialized?0:current->PartitionCount},{"reportedStyle",static_cast<int>(current->PartitionStyle)},{"reportedEntries",current->PartitionCount},{"allBytesZero",false}};
    if(style=="unsupported")throw std::runtime_error("Target uses an unsupported partition style");
    if(blank_only){
        if(!uninitialized)throw std::runtime_error("Blank-RAW mode accepts only an unpartitioned disk");
        std::cerr<<"Scanning the selected RAW disk for existing data before writes...\n";verify_zero_disk(preflight_disk.value,target.at("sizeBytes").get<unsigned long long>());status["allBytesZero"]=true;
    }
    Json scratch;for(const auto& r:plan["resources"])if(r["role"]=="scratch")scratch=r;
    if(metadata){
        if(config.value("targetPolicy","")!="blank-raw" && config.value("targetPolicy","")!="format-selected-disk")throw std::runtime_error("PXE has no supported target disk policy");
        if(blank_only && !status["allBytesZero"].get<bool>())throw std::runtime_error("PXE blank-RAW mode requires a fully zeroed disk");
    }else if(!config.value("verifyImage",true))throw std::runtime_error("Skipping image verification requires an approved PXE routine selection");
    else validate_blank_deployment(config,status,scratch,free_bytes(wide(config.at("scratchDirectory"))),plan["image"]["totalBytes"].get<unsigned long long>());
    auto parts=gpt_layout(target.at("sizeBytes").get<unsigned long long>(),config.value("recoveryMiB",1536),sector_size(preflight_disk.value));
    plan["blankTargetVerified"]=blank_only;plan["targetWillBeFormatted"]=target_policy=="format-selected-disk";plan["layoutBytes"]=parts;if(!execute)return plan;
    Handle mutex(CreateMutexW(nullptr,TRUE,L"Global\\repxeDeployment"));if(!mutex)fail("Create deployment lock");if(GetLastError()==ERROR_ALREADY_EXISTS)throw std::runtime_error("Another deployment owns the deployment lock");
    auto id=sha256(config_path).substr(0,16);auto jobs=std::filesystem::path(wide(config.at("jobDirectory")));auto job=jobs/(L"deploy-"+wide(id)+L".json");if(std::filesystem::exists(job))throw std::runtime_error("Deployment job already exists; automatic destructive retries are refused");
    Json state={{"schemaVersion",1},{"toolVersion","0.14.1"},{"id",id},{"stage","preflight"},{"startedUtc",utc_now()},{"plan",plan},{"history",Json::array()},{"diskWritesPerformed",false}};
    auto stage=[&](const char* name){state["stage"]=name;state.erase("progressPercent");if(std::string(name)=="applying-image")state["progressPercent"]=0;state["history"].push_back({{"stage",name},{"utc",utc_now()}});write_json(job,state);std::cerr<<"Deployment stage: "<<name<<"\n";if(progress)progress(state);};
    auto tool=[&](const char* name,std::vector<std::wstring> args,const std::string& input=""){auto log=jobs/(L"deploy-"+wide(id)+L"-"+wide(name)+L".log");std::function<void(int)> observe;
        if(std::string(name)=="apply")observe=[&](int percent){state["progressPercent"]=percent;state["imageApplyPercent"]=percent;write_json(job,state);if(progress)progress(state);};
        auto code=run_logged(args,log,input,INFINITE,observe);
        if(std::string(name)=="apply" && !code && observe)observe(100);
        state["tools"].push_back({{"name",name},{"exitCode",code},{"log",utf8(log.wstring())}});write_json(job,state);if(code)throw std::runtime_error(std::string(name)+" failed with exit code "+std::to_string(code));return log;};
    Mounts mounts;stage("preflight");
    try{
        // Request write access only after every read-only guard and job collision check.
        Handle disk(CreateFileW(wide(target.at("devicePath")).c_str(),GENERIC_READ|GENERIC_WRITE,FILE_SHARE_READ|FILE_SHARE_WRITE,nullptr,OPEN_EXISTING,0,nullptr));if(!disk)fail("Open validated target for preparation");
        check_bound_disk(disk.value,target);const auto msr=guid(L"{e3c9e316-0b5c-4db8-817d-f92df00215ae}");CREATE_DISK initialize{};initialize.PartitionStyle=PARTITION_STYLE_GPT;initialize.Gpt.DiskId=new_guid();initialize.Gpt.MaxPartitionCount=128;
        state["diskWritesPerformed"]=true;stage("preparing-disk");
        if(target_policy=="format-selected-disk" && !uninitialized){
            check_bound_disk(disk.value,target);ioctl(disk.value,IOCTL_DISK_DELETE_DRIVE_LAYOUT,nullptr,0,"Erase existing partition layout");
            ioctl(disk.value,IOCTL_DISK_UPDATE_PROPERTIES,nullptr,0,"Refresh erased layout");check_bound_disk(disk.value,target);
        }
        ioctl(disk.value,IOCTL_DISK_CREATE_DISK,&initialize,sizeof(initialize),"Initialize GPT");ioctl(disk.value,IOCTL_DISK_UPDATE_PROPERTIES,nullptr,0,"Refresh GPT");
        // Some WinPE builds initialize an empty GPT, while other storage stacks
        // generate an MSR. Wait for that generated MSR when one is present.
        auto partition_arrived=[&](const PARTITION_INFORMATION_EX& expected){
            check_bound_disk(disk.value,target);
            auto path=L"\\\\?\\GLOBALROOT\\Device\\Harddisk"+std::to_wstring(target.at("number").get<int>())+L"\\Partition"+std::to_wstring(expected.PartitionNumber);
            Handle arrived(CreateFileW(path.c_str(),0,FILE_SHARE_READ|FILE_SHARE_WRITE,nullptr,OPEN_EXISTING,0,nullptr));
            PARTITION_INFORMATION_EX actual{};DWORD returned=0;
            return arrived && DeviceIoControl(arrived.value,IOCTL_DISK_GET_PARTITION_INFO_EX,nullptr,0,&actual,sizeof(actual),&returned,nullptr) &&
                actual.PartitionStyle==PARTITION_STYLE_GPT && IsEqualGUID(actual.Gpt.PartitionId,expected.Gpt.PartitionId) &&
                actual.StartingOffset.QuadPart==expected.StartingOffset.QuadPart && actual.PartitionLength.QuadPart==expected.PartitionLength.QuadPart;
        };
        bool ready=false;
        for(int attempt=0;attempt<30 && !ready;++attempt){
            auto bytes=disk_layout(disk.value);auto initialized=reinterpret_cast<const DRIVE_LAYOUT_INFORMATION_EX*>(bytes.data());state["initializedPartitionCount"]=initialized->PartitionCount;
            if(initialized->PartitionStyle==PARTITION_STYLE_GPT && initialized->PartitionCount<=128 &&
               bytes.size()>=offsetof(DRIVE_LAYOUT_INFORMATION_EX,PartitionEntry)+initialized->PartitionCount*sizeof(PARTITION_INFORMATION_EX)){
                ready=initialized->PartitionCount==0;
                for(DWORD i=0;i<initialized->PartitionCount && !ready;++i){const auto& part=initialized->PartitionEntry[i];
                    if(part.PartitionStyle==PARTITION_STYLE_GPT && IsEqualGUID(part.Gpt.PartitionType,msr))ready=partition_arrived(part);
                }
            }
            if(!ready)Sleep(1000);
        }
        if(!ready)throw std::runtime_error("Initialized GPT or generated MSR partition did not arrive");
        std::vector<unsigned char> layout_bytes(sizeof(DRIVE_LAYOUT_INFORMATION_EX)+3*sizeof(PARTITION_INFORMATION_EX));auto layout=reinterpret_cast<DRIVE_LAYOUT_INFORMATION_EX*>(layout_bytes.data());
        layout->PartitionStyle=PARTITION_STYLE_GPT;layout->PartitionCount=4;layout->Gpt.DiskId=initialize.Gpt.DiskId;layout->Gpt.StartingUsableOffset.QuadPart=MiB;layout->Gpt.UsableLength.QuadPart=(target["sizeBytes"].get<unsigned long long>()/MiB)*MiB-2*MiB;layout->Gpt.MaxPartitionCount=128;
        const auto efi=guid(L"{c12a7328-f81f-11d2-ba4b-00a0c93ec93b}"),basic=guid(L"{ebd0a0a2-b9e5-4433-87c0-68b6b72699c7}"),recovery=guid(L"{de94bba4-06d1-4d40-a16a-bfd50179d6ac}");
        for(int i=0;i<4;++i){auto& p=layout->PartitionEntry[i];p.PartitionStyle=PARTITION_STYLE_GPT;p.StartingOffset.QuadPart=parts[i]["offset"].get<unsigned long long>();p.PartitionLength.QuadPart=parts[i]["length"].get<unsigned long long>();p.PartitionNumber=i+1;p.RewritePartition=TRUE;p.Gpt.PartitionId=new_guid();p.Gpt.PartitionType=i==0?efi:i==1?msr:basic;p.Gpt.Attributes=0;auto name=wide(parts[i]["role"]);std::copy(name.begin(),name.end(),p.Gpt.Name);parts[i]["partitionId"]=guid_text(p.Gpt.PartitionId);}
        state["partitions"]=parts;check_bound_disk(disk.value,target);ioctl(disk.value,IOCTL_DISK_SET_DRIVE_LAYOUT_EX,layout,static_cast<DWORD>(layout_bytes.size()),"Create GPT partitions");ioctl(disk.value,IOCTL_DISK_UPDATE_PROPERTIES,nullptr,0,"Refresh partitions");
        ready=false;for(int attempt=0;attempt<30 && !ready;++attempt){ready=partition_arrived(layout->PartitionEntry[1]);if(!ready)Sleep(1000);}
        if(!ready)throw std::runtime_error("Generated MSR partition did not arrive");stage("disk-partitioned");
        std::array<std::wstring,4> volumes{};std::array<std::wstring,4> roots{};
        for(int i:{0,2,3}){
            volumes[i]=find_partition_volume(layout->PartitionEntry[i].Gpt.PartitionId,disk.value,target);
            roots[i]=mounts.assign(volumes[i],i==0?L'S':i==2?L'W':L'R');
            std::array<wchar_t,32768> mounted{};bool resolved=false;
            if(!GetVolumeNameForVolumeMountPointW(roots[i].c_str(),mounted.data(),static_cast<DWORD>(mounted.size())) ||
               _wcsicmp(mounted.data(),volumes[i].c_str()) || volume_disks(mounted.data(),resolved)!=std::vector<int>{target.at("number").get<int>()} || !resolved)
                throw std::runtime_error("Generated format mount changed");
            check_bound_disk(disk.value,target);
            tool(("format-"+parts[i]["role"].get<std::string>()).c_str(),{system_directory()+L"\\format.com",roots[i].substr(0,2),L"/FS:"+wide(parts[i]["filesystem"]),L"/Q",L"/Y",L"/V:"+wide(parts[i]["role"])},"Y\r\n");
        }
        if(metadata){
            // This workspace exists only on the newly created, identity-checked Windows partition.
            auto staging=std::filesystem::path(roots[2])/L".re-pxeStage";
            std::filesystem::create_directory(staging);restrict_state_path(staging);
            auto disk_jobs=staging/L"jobs";std::filesystem::create_directory(disk_jobs);restrict_state_path(disk_jobs);
            for(const auto& entry:std::filesystem::directory_iterator(jobs))if(entry.is_regular_file())std::filesystem::copy_file(entry.path(),disk_jobs/entry.path().filename());
            jobs=disk_jobs;job=jobs/(L"deploy-"+wide(id)+L".json");state["pxeStaging"]=utf8(staging.wstring());
            config["scratchDirectory"]=utf8((staging/L"scratch").wstring());config["jobDirectory"]=utf8(jobs.wstring());
            std::filesystem::create_directory(staging/L"scratch");restrict_state_path(staging/L"scratch");
            if(free_bytes(staging)<metadata->at("downloadBytes").get<unsigned long long>()+plan["image"]["totalBytes"].get<unsigned long long>()+GiB)throw std::runtime_error("Prepared partition lacks PXE staging capacity");
            stage("disk-prepared");source(config,staging);
            image_lock=std::make_unique<Handle>(CreateFileW(std::filesystem::path(wide(config.at("image"))).c_str(),GENERIC_READ,FILE_SHARE_READ,nullptr,OPEN_EXISTING,FILE_FLAG_SEQUENTIAL_SCAN,nullptr));if(!*image_lock)fail("Lock downloaded image");
            auto actual=inspect_wim(wide(config.at("image")),config.value("imageIndex",1));
            state["imageVerification"]={{"sha256Checked",validate_downloaded_image(config,*metadata,actual,std::filesystem::file_size(wide(config.at("image"))),config.value("verifyImage",true)?sha256(wide(config.at("image"))):std::string())},{"expectedSha256",config.at("sha256")}};
            package_lock=read_driver_package(config);validate_hardware(package_lock.manifest);read_delivery_config(config);
            auto resource=locate_resource(wide(config.at("image")),"PXE staging");
            if(!resource.value("resolved",false) || resource.at("diskNumbers")!=Json::array({target.at("number")}))throw std::runtime_error("PXE staging changed disk");
        }else stage("disk-prepared");
        auto verify_roots=[&]{check_bound_disk(disk.value,target);for(int i:{0,2,3}){auto r=locate_resource(roots[i],"generated partition");if(!r.value("resolved",false) || r["volume"]!=utf8(volumes[i]) || r["diskNumbers"]!=Json::array({target["number"]}))throw std::runtime_error("Generated partition mount changed");}};
        verify_roots();stage("applying-image");tool("apply",{system_directory()+L"\\dism.exe",L"/Apply-Image",L"/ImageFile:"+wide(config.at("image")),L"/Index:"+std::to_wstring(config.value("imageIndex",1)),L"/ApplyDir:"+roots[2],L"/CheckIntegrity",L"/Verify",L"/ScratchDir:"+wide(config.at("scratchDirectory")),L"/LogPath:"+(jobs/(L"deploy-"+wide(id)+L"-apply-dism.log")).wstring()});stage("image-applied");
        auto windows=std::filesystem::path(roots[2])/L"Windows";if(!file_exists(windows/L"System32\\ntoskrnl.exe"))throw std::runtime_error("Applied Windows installation is missing");
        auto offline_package=read_driver_package(config);
        const auto offline=offline_driver_plan(config,offline_package.manifest);state["offlineDrivers"]=Json::array();
        if(!offline.empty())stage("installing-offline-drivers");
        for(const auto& item:offline){
            const auto inf=item.at("inf").get<std::string>();verify_roots();
            auto name="offline-driver-"+std::to_string(state["offlineDrivers"].size());auto log=jobs/(L"deploy-"+wide(id)+L"-"+wide(name)+L".log");
            std::cerr<<"Preparing driver "<<state["offlineDrivers"].size()+1<<"/"<<offline.size()<<": "<<inf<<"\n";
            auto code=run_logged({system_directory()+L"\\dism.exe",L"/Image:"+roots[2],L"/Add-Driver",L"/Driver:"+(offline_package.directory/wide(inf)).wstring(),L"/ScratchDir:"+wide(config.at("scratchDirectory")),L"/LogPath:"+(jobs/(L"deploy-"+wide(id)+L"-"+wide(name)+L"-dism.log")).wstring()},log,"",900000);
            state["tools"].push_back({{"name",name},{"exitCode",code},{"log",utf8(log.wstring())}});
            state["offlineDrivers"].push_back({{"inf",inf},{"exitCode",code},{"required",item.at("required")},{"status",code&&code!=3010?"deferred-to-windows":"prepared"},{"log",utf8(log.wstring())}});write_json(job,state);
            if(code==1460||code==1641)throw std::runtime_error("Offline driver servicing state is uncertain; deployment requires review");
            if(code&&code!=3010&&item.at("required").get<bool>())throw std::runtime_error("Required boot driver failed: "+inf+" ("+std::to_string(code)+")");
            if(code&&code!=3010)std::cerr<<"Driver deferred to the Windows repair pass: "<<inf<<"\n";
        }
        if(metadata){
            // PXE initialized WinPE and the firmware clock to UTC. A reference
            // image's local time zone would reinterpret that RTC at first boot,
            // breaking certificate dates before Windows can synchronize time.
            verify_roots();tool("initial-time-zone",{system_directory()+L"\\dism.exe",L"/Image:"+roots[2],L"/Set-TimeZone:UTC",L"/ScratchDir:"+wide(config.at("scratchDirectory")),L"/LogPath:"+(jobs/(L"deploy-"+wide(id)+L"-time-zone-dism.log")).wstring()});state["initialTimeZone"]="UTC";
        }
        verify_roots();std::vector<std::wstring> boot={system_directory()+L"\\bcdboot.exe",windows.wstring(),L"/s",roots[0].substr(0,2),L"/f",L"UEFI",L"/v"};if(config.value("bootEx",false))boot.push_back(L"/bootex");tool("bcdboot",boot);if(!file_exists(std::filesystem::path(roots[0])/L"EFI\\Microsoft\\Boot\\BCD"))throw std::runtime_error("Boot files were not created");stage("boot-configured");
        auto winre=windows/L"System32\\Recovery\\Winre.wim";if(!file_exists(winre))throw std::runtime_error("Windows recovery image is missing; deployment cannot be marked ready");
        if(std::filesystem::file_size(winre)+256*MiB>free_bytes(roots[3]))throw std::runtime_error("Recovery partition lacks image and servicing space");
        auto re=std::filesystem::path(roots[3])/L"Recovery\\WindowsRE";std::filesystem::create_directories(re);std::filesystem::copy_file(winre,re/L"Winre.wim");state["recovery"]={{"sha256",sha256(re/L"Winre.wim")},{"path",utf8(re.wstring())},{"status","copied"}};
        verify_roots();tool("register-recovery",{(windows/L"System32\\reagentc.exe").wstring(),L"/setreimage",L"/path",re.wstring(),L"/target",windows.wstring()});
        auto bcd=std::filesystem::path(roots[0])/L"EFI\\Microsoft\\Boot\\BCD";auto bcd_log=tool("bcd-info",{system_directory()+L"\\bcdedit.exe",L"/store",bcd.wstring(),L"/enum",L"all",L"/v"});state["recovery"]["windowsLoaderGuid"]=loader_guid(read_tool_text(bcd_log));state["recovery"]["status"]="registered-offline";
        // Online specialize/completion enables and validates WinRE; this stage registers its location.
        layout->PartitionEntry[3].Gpt.PartitionType=recovery;layout->PartitionEntry[3].Gpt.Attributes=0x8000000000000001ULL;check_bound_disk(disk.value,target);ioctl(disk.value,IOCTL_DISK_SET_DRIVE_LAYOUT_EX,layout,static_cast<DWORD>(layout_bytes.size()),"Set recovery GPT attributes");ioctl(disk.value,IOCTL_DISK_UPDATE_PROPERTIES,nullptr,0,"Refresh recovery attributes");stage("recovery-registered");
        verify_roots();stage_completion(roots[2],target,initialize.Gpt.DiskId,layout->PartitionEntry[2].Gpt.PartitionId,id,config,metadata!=nullptr,state.at("offlineDrivers"));state["completionAgent"]="ProgramData\\re-pxe\\re-pxe.exe";stage("drivers-staged");stage("agent-staged");
        if(metadata){
            // Keep durable diagnostics and completion state; release the temporary image/scratch.
            image_lock.reset();std::filesystem::remove(wide(config.at("image")));std::filesystem::remove_all(wide(config.at("scratchDirectory")));state["pxeImageCacheRemoved"]=true;
        }
        state["bootVerified"]=false;state["sha256"]=config.at("sha256");state["imageIndex"]=config.value("imageIndex",1);state["completedOfflineUtc"]=utc_now();stage("awaiting-windows");return state;
    }catch(const std::exception& e){state["error"]=e.what();stage("failed");throw;}
}
}
Json deploy_from_config(const Json& config,const std::filesystem::path& path,bool execute,const std::function<void(const Json&)>& progress){return deploy_internal(config,path,execute,progress,nullptr,{});}
Json pxe_preflight(const Json& config,const std::filesystem::path& path,const Json& metadata){return deploy_internal(config,path,false,{},&metadata,{});}
Json pxe_deploy(const Json& config,const std::filesystem::path& path,const Json& metadata,const std::function<void(Json&,const std::filesystem::path&)>& source,const std::function<void(const Json&)>& progress){return deploy_internal(config,path,true,progress,&metadata,source);}
namespace {
std::wstring registry_text(const wchar_t* key,const wchar_t* name){
    std::array<wchar_t,4096> text{};DWORD size=sizeof(text);
    auto code=RegGetValueW(HKEY_LOCAL_MACHINE,key,name,RRF_RT_REG_SZ,nullptr,text.data(),&size);
    return code==ERROR_SUCCESS?std::wstring(text.data()):L"";
}
Json registry_number(const wchar_t* key,const wchar_t* name){
    DWORD value=0,size=sizeof(value);auto code=RegGetValueW(HKEY_LOCAL_MACHINE,key,name,RRF_RT_REG_DWORD,nullptr,&value,&size);
    return code==ERROR_SUCCESS?Json(value):Json(nullptr);
}
void no_reparse(const std::filesystem::path& path){
    auto p=std::filesystem::absolute(path);
    while(p!=p.root_path()){
        auto attrs=GetFileAttributesW(p.c_str());
        if(attrs==INVALID_FILE_ATTRIBUTES || (attrs&FILE_ATTRIBUTE_REPARSE_POINT))throw std::runtime_error("Completion path is missing or uses a reparse point");
        p=p.parent_path();
    }
}
void protect_path(const std::filesystem::path& path){
    no_reparse(path);PSECURITY_DESCRIPTOR descriptor=nullptr;
    if(!ConvertStringSecurityDescriptorToSecurityDescriptorW(L"O:BAG:BAD:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)",SDDL_REVISION_1,&descriptor,nullptr))fail("Build completion security descriptor");
    PACL acl=nullptr;PSID owner=nullptr;BOOL present=FALSE,defaulted=FALSE;
    GetSecurityDescriptorDacl(descriptor,&present,&acl,&defaulted);GetSecurityDescriptorOwner(descriptor,&owner,&defaulted);
    auto code=SetNamedSecurityInfoW(const_cast<wchar_t*>(path.c_str()),SE_FILE_OBJECT,OWNER_SECURITY_INFORMATION|DACL_SECURITY_INFORMATION|PROTECTED_DACL_SECURITY_INFORMATION,owner,nullptr,acl,nullptr);
    LocalFree(descriptor);if(code)throw std::runtime_error("Protect completion path (Windows error "+std::to_string(code)+")");
}
void verify_protected(const std::filesystem::path& path){
    no_reparse(path);PSID owner=nullptr;PACL acl=nullptr;PSECURITY_DESCRIPTOR descriptor=nullptr;
    auto code=GetNamedSecurityInfoW(path.c_str(),SE_FILE_OBJECT,OWNER_SECURITY_INFORMATION|DACL_SECURITY_INFORMATION,&owner,nullptr,&acl,nullptr,&descriptor);
    if(code)throw std::runtime_error("Read completion path security");
    std::array<unsigned char,SECURITY_MAX_SID_SIZE> ba{},sy{};DWORD ba_size=ba.size(),sy_size=sy.size();
    CreateWellKnownSid(WinBuiltinAdministratorsSid,nullptr,ba.data(),&ba_size);CreateWellKnownSid(WinLocalSystemSid,nullptr,sy.data(),&sy_size);
    bool valid=owner && (EqualSid(owner,ba.data()) || EqualSid(owner,sy.data())) && acl && acl->AceCount==2;
    bool ba_found=false,sy_found=false;
    if(valid)for(DWORD i=0;i<acl->AceCount;++i){void* entry=nullptr;if(!GetAce(acl,i,&entry)){valid=false;break;}
        auto ace=static_cast<ACCESS_ALLOWED_ACE*>(entry);
        if(ace->Header.AceType!=ACCESS_ALLOWED_ACE_TYPE || (ace->Mask&FILE_ALL_ACCESS)!=FILE_ALL_ACCESS){valid=false;break;}
        auto sid=&ace->SidStart;if(EqualSid(sid,ba.data()))ba_found=true;else if(EqualSid(sid,sy.data()))sy_found=true;else valid=false;
    }
    LocalFree(descriptor);if(!valid || !ba_found || !sy_found)throw std::runtime_error("Completion path is not restricted to Administrators and SYSTEM");
}
void write_text_new(const std::filesystem::path& p,const std::string& text){
    std::ofstream out(p,std::ios::binary);if(!out || !(out<<text) || !out.flush())throw std::runtime_error("Write completion answer file");
}
std::string agent_answer(const std::wstring& path){
    pugi::xml_document doc;auto root=doc.append_child("unattend");root.append_attribute("xmlns")="urn:schemas-microsoft-com:unattend";root.append_attribute("xmlns:wcm")="http://schemas.microsoft.com/WMIConfig/2002/State";
    auto component=[&](const char* pass){auto setting=root.append_child("settings");setting.append_attribute("pass")=pass;auto c=setting.append_child("component");c.append_attribute("name")="Microsoft-Windows-Deployment";c.append_attribute("processorArchitecture")="amd64";c.append_attribute("publicKeyToken")="31bf3856ad364e35";c.append_attribute("language")="neutral";c.append_attribute("versionScope")="nonSxS";return c;};
    component("oobeSystem").append_child("Reseal").append_child("Mode").text()="Audit";
    auto cmd=component("auditUser").append_child("RunAsynchronous").append_child("RunAsynchronousCommand");cmd.append_attribute("wcm:action")="add";cmd.append_child("Order").text()=1;
    cmd.append_child("Path").text().set(utf8(command_line({path,L"complete"})).c_str());cmd.append_child("Description").text()="repxeCompletion-v1";
    std::ostringstream out;doc.save(out);return out.str();
}
std::vector<unsigned char> public_root(const std::filesystem::path& path){
    if(std::filesystem::file_size(path)>65536)throw std::runtime_error("Server CA exceeds bound");
    std::ifstream stream(path,std::ios::binary);std::string bytes((std::istreambuf_iterator<char>(stream)),std::istreambuf_iterator<char>());if(!stream)throw std::runtime_error("Read public server CA");
    std::vector<unsigned char> der;
    if(bytes.find("-----BEGIN CERTIFICATE-----")!=std::string::npos){DWORD count=0;if(!CryptStringToBinaryA(bytes.data(),bytes.size(),CRYPT_STRING_BASE64HEADER,nullptr,&count,nullptr,nullptr))fail("Decode public server CA");der.resize(count);if(!CryptStringToBinaryA(bytes.data(),bytes.size(),CRYPT_STRING_BASE64HEADER,der.data(),&count,nullptr,nullptr))fail("Decode server CA");der.resize(count);}
    else der.assign(bytes.begin(),bytes.end());
    auto certificate=CertCreateCertificateContext(X509_ASN_ENCODING,der.data(),der.size());if(!certificate)fail("Read server CA certificate");
    struct Certificate{PCCERT_CONTEXT p;~Certificate(){CertFreeCertificateContext(p);}} guard{certificate};
    auto ext=CertFindExtension(szOID_BASIC_CONSTRAINTS2,certificate->pCertInfo->cExtension,certificate->pCertInfo->rgExtension);CERT_BASIC_CONSTRAINTS2_INFO* constraints=nullptr;DWORD size=0;
    if(!ext || !CryptDecodeObjectEx(X509_ASN_ENCODING,X509_BASIC_CONSTRAINTS2,ext->Value.pbData,ext->Value.cbData,CRYPT_DECODE_ALLOC_FLAG,nullptr,&constraints,&size))throw std::runtime_error("Server certificate must be a public root CA");
    bool valid=constraints->fCA && CertCompareCertificateName(X509_ASN_ENCODING,&certificate->pCertInfo->Subject,&certificate->pCertInfo->Issuer);LocalFree(constraints);if(!valid)throw std::runtime_error("Server certificate is not a root CA");return der;
}
void install_public_root(const std::filesystem::path& path){
    auto der=public_root(path);auto store=CertOpenStore(CERT_STORE_PROV_SYSTEM_W,0,0,CERT_SYSTEM_STORE_LOCAL_MACHINE,L"ROOT");if(!store)fail("Open installed Windows root store");
    bool ok=CertAddEncodedCertificateToStore(store,X509_ASN_ENCODING,der.data(),der.size(),CERT_STORE_ADD_REPLACE_EXISTING,nullptr);CertCloseStore(store,0);if(!ok)fail("Trust the provisioned server root CA");
}
constexpr auto update_policy_key=L"SOFTWARE\\Policies\\Microsoft\\Windows\\WindowsUpdate\\AU";
struct RegistryKey {HKEY h=nullptr;~RegistryKey(){if(h)RegCloseKey(h);}};
Json edit_offline_software(const std::filesystem::path& windows,const std::function<Json(HKEY)>& edit){
    Handle token;
    if(!OpenProcessToken(GetCurrentProcess(),TOKEN_ADJUST_PRIVILEGES|TOKEN_QUERY,&token.value))fail("Open offline registry privilege");
    struct Privileges {
        HANDLE token;std::array<TOKEN_PRIVILEGES,2> previous{};
        ~Privileges(){for(auto& saved:previous)if(saved.PrivilegeCount)AdjustTokenPrivileges(token,FALSE,&saved,0,nullptr,nullptr);}
    } privileges{token.value};
    int i=0;
    for(const auto* name:{SE_BACKUP_NAME,SE_RESTORE_NAME}){
        TOKEN_PRIVILEGES request{};request.PrivilegeCount=1;request.Privileges[0].Attributes=SE_PRIVILEGE_ENABLED;
        if(!LookupPrivilegeValueW(nullptr,name,&request.Privileges[0].Luid))fail("Resolve offline registry privilege");
        DWORD size=sizeof(TOKEN_PRIVILEGES);SetLastError(ERROR_SUCCESS);
        if(!AdjustTokenPrivileges(token.value,FALSE,&request,size,&privileges.previous[i++],&size)||GetLastError()==ERROR_NOT_ALL_ASSIGNED)fail("Enable offline registry privilege");
    }
    GUID unique{};if(FAILED(CoCreateGuid(&unique)))throw std::runtime_error("Create private offline registry name");
    auto subkey=L"repxeOffline-"+wide(guid_text(unique));
    struct MountedHive {std::wstring name;bool loaded=false;~MountedHive(){if(loaded)RegUnLoadKeyW(HKEY_LOCAL_MACHINE,name.c_str());}} mounted{subkey};
    RegistryKey hive;
    auto code=RegLoadKeyW(HKEY_LOCAL_MACHINE,subkey.c_str(),(windows/L"System32\\config\\SOFTWARE").c_str());
    if(code)throw std::runtime_error("Load destination SOFTWARE hive (Windows error "+std::to_string(code)+")");
    mounted.loaded=true;
    code=RegOpenKeyExW(HKEY_LOCAL_MACHINE,subkey.c_str(),0,KEY_ALL_ACCESS,&hive.h);
    if(code)throw std::runtime_error("Open destination SOFTWARE hive");
    auto result=edit(hive.h);
    if(RegFlushKey(hive.h))throw std::runtime_error("Persist destination SOFTWARE hive");
    RegCloseKey(hive.h);hive.h=nullptr;
    code=RegUnLoadKeyW(HKEY_LOCAL_MACHINE,subkey.c_str());
    if(code)throw std::runtime_error("Unload destination SOFTWARE hive");mounted.loaded=false;
    return result;
}
Json pause_update_policy(const std::filesystem::path& windows){
    return edit_offline_software(windows,[](HKEY hive){
    RegistryKey policy;
    auto code=RegCreateKeyExW(hive,L"Policies\\Microsoft\\Windows\\WindowsUpdate\\AU",0,nullptr,0,KEY_ALL_ACCESS,nullptr,&policy.h,nullptr);
    if(code)throw std::runtime_error("Open offline automatic update policy");
    Json saved=Json::array();
    for(const auto* name:{L"NoAutoUpdate",L"NoAutoRebootWithLoggedOnUsers"}){
        DWORD value=0,type=0,size=sizeof(value);code=RegQueryValueExW(policy.h,name,nullptr,&type,reinterpret_cast<BYTE*>(&value),&size);
        if(code!=ERROR_FILE_NOT_FOUND && (code!=ERROR_SUCCESS||type!=REG_DWORD||size!=sizeof(value)))throw std::runtime_error("Custom Windows Update policy requires review");
        saved.push_back({{"name",utf8(name)},{"existed",code==ERROR_SUCCESS},{"value",value}});
    }
    DWORD disabled=1;
    for(const auto* name:{L"NoAutoUpdate",L"NoAutoRebootWithLoggedOnUsers"})if(RegSetValueExW(policy.h,name,0,REG_DWORD,reinterpret_cast<BYTE*>(&disabled),sizeof(disabled)))throw std::runtime_error("Pause automatic Windows updates");
    return saved;
    });
}
void restore_update_policy(Json& state){
    if(!state.contains("windowsUpdateRegistry")||state.value("windowsUpdatePolicyRestored",false))return;
    RegistryKey policy;
    if(RegOpenKeyExW(HKEY_LOCAL_MACHINE,update_policy_key,0,KEY_QUERY_VALUE|KEY_SET_VALUE,&policy.h))throw std::runtime_error("Open paused Windows Update policy");
    for(const auto& item:state.at("windowsUpdateRegistry")){
        const auto name=wide(item.at("name"));if(name!=L"NoAutoUpdate"&&name!=L"NoAutoRebootWithLoggedOnUsers")throw std::runtime_error("Invalid saved update policy");
        DWORD current=0,type=0,size=sizeof(current);
        if(RegQueryValueExW(policy.h,name.c_str(),nullptr,&type,reinterpret_cast<BYTE*>(&current),&size)||type!=REG_DWORD||current!=1)throw std::runtime_error("Paused update policy changed; operator review required");
        auto value=item.at("value").get<DWORD>();auto code=item.at("existed").get<bool>()?RegSetValueExW(policy.h,name.c_str(),0,REG_DWORD,reinterpret_cast<BYTE*>(&value),sizeof(value)):RegDeleteValueW(policy.h,name.c_str());
        if(code)throw std::runtime_error("Restore automatic Windows Update policy");
    }
    state["windowsUpdatePolicyRestored"]=true;
}
void stage_completion(const std::filesystem::path& volume,const Json& target,const GUID& disk_id,const GUID& partition_id,const std::string& id,const Json& config,bool pxe,const Json& offline){
    auto root=volume/L"ProgramData\\re-pxe";auto answer=volume/L"Windows\\Panther\\Unattend.xml";
    if(std::filesystem::exists(root))throw std::runtime_error("Applied image already contains completion state; operator review required");
    std::vector<std::filesystem::path> previous_answers;
    for(const auto* name:{L"Windows\\Panther\\Unattend.xml",L"Windows\\Panther\\Unattend\\Unattend.xml"}){
        auto p=volume/name;if(!std::filesystem::exists(p))continue;no_reparse(p);pugi::xml_document doc;
        if(!doc.load_file(p.c_str()))throw std::runtime_error("Applied image answer file cannot be parsed");
        auto node=doc.child("unattend");int settings=0;
        for(auto setting:node.children()){
            ++settings;auto c=setting.child("component"),r=c.child("Reseal");
            if(std::string(setting.name())!="settings" || std::string(setting.attribute("pass").as_string())!="oobeSystem" || std::distance(setting.children().begin(),setting.children().end())!=1 ||
               std::string(c.attribute("name").as_string())!="Microsoft-Windows-Deployment" || std::distance(c.children().begin(),c.children().end())!=1 ||
               std::distance(r.children().begin(),r.children().end())!=1 || std::string(r.child_value("Mode"))!="Audit")throw std::runtime_error("Applied image has a custom answer file; operator review required");
        }
        if(settings!=1)throw std::runtime_error("Applied image answer file is not an Audit-only reference declaration");previous_answers.push_back(p);
    }
    std::filesystem::create_directories(root);protect_path(root);
    for(size_t i=0;i<previous_answers.size();++i){auto backup=root/(L"reference-unattend-"+std::to_wstring(i)+L".xml");std::filesystem::copy_file(previous_answers[i],backup);protect_path(backup);std::filesystem::remove(previous_answers[i]);}
    auto exe=root/L"re-pxe.exe";std::filesystem::copy_file(executable(),exe);protect_path(exe);
    Json state={{"schemaVersion",1},{"toolVersion","0.14.1"},{"id",id},{"stage","awaiting-windows"},{"target",target},{"diskGuid",guid_text(disk_id)},{"windowsPartitionGuid",guid_text(partition_id)},
        {"clientSha256",sha256(exe)},{"kernelSha256",sha256(volume/L"Windows\\System32\\ntoskrnl.exe")},{"imageSha256",config.at("sha256")},{"imageIndex",config.value("imageIndex",1)},
        {"history",Json::array()},{"bootObservations",Json::array()},{"driverActions",Json::array()},{"deliveryStatus","local-only"},{"generalizationPerformed",false}};
    if(config.value("driverConsole",false)){state["driverConsole"]=true;state["driverAutomaticAttempted"]=false;if(!config.contains("windowsUpdate"))state["windowsUpdateRegistry"]=pause_update_policy(volume/L"Windows");}
    state["imageVerification"]={{"sha256Checked",config.value("verifyImage",true)},{"expectedSha256",config.at("sha256")}};
    state["offlineDrivers"]=offline;
    if(config.contains("finishOptions")){
        validate_finish_options(config.at("finishOptions"));state["finishOptions"]=config.at("finishOptions");state["finishOptions"].erase("account_password");
        std::string original=config.contains("answerFiles")?answer_content(config,"oobe"):"<unattend xmlns=\"urn:schemas-microsoft-com:unattend\"><settings pass=\"oobeSystem\"><component name=\"Microsoft-Windows-Deployment\" processorArchitecture=\"amd64\" publicKeyToken=\"31bf3856ad364e35\" language=\"neutral\" versionScope=\"nonSxS\"><Reseal><Mode>OOBE</Mode></Reseal></component></settings></unattend>";
        auto saved=root/L"answer-customer.xml";write_text_new(saved,finish_answer_xml(original,config.at("finishOptions")));protect_path(saved);state["finalAnswerSha256"]=sha256(saved);
        if(pxe){auto staging=volume/L".re-pxeStage";verify_protected(staging);auto owner=staging/L"re-pxe-owner.json";write_json(owner,{{"installationId",id},{"clientSha256",state.at("clientSha256")}});protect_path(owner);state["pxeStagingCleanup"]=true;}
    }
    if(config.contains("windowsUpdate")){state["windowsUpdatePolicy"]=config.at("windowsUpdate");state["windowsUpdateAttempts"]=Json::array();state["windowsUpdateRegistry"]=pause_update_policy(volume/L"Windows");}
    auto package=read_driver_package(config);
    if(!package.manifest.is_null()){
        auto drivers=root/L"drivers";std::filesystem::create_directory(drivers);protect_path(drivers);
        for(const auto& f:package.manifest.at("files")){
            auto destination=drivers/wide(f.at("path"));std::filesystem::create_directories(destination.parent_path());
            std::filesystem::copy_file(package.directory/wide(f.at("path")),destination);protect_path(destination);
            if(sha256(destination)!=f.at("sha256").get<std::string>())throw std::runtime_error("Staged driver file integrity differs");
        }
        state["driverPackage"]=package.manifest;
    }
    if(pxe||config.value("driverConsole",false)){
        auto source=std::filesystem::path(executable()).parent_path()/L"server-root.crt";
        if(pxe&&!std::filesystem::exists(source))throw std::runtime_error("PXE media is missing its transferable server CA; use corrected boot media");
        if(std::filesystem::exists(source)){auto der=public_root(source);auto certificate=root/L"server-ca.der";std::ofstream out(certificate,std::ios::binary);out.write(reinterpret_cast<const char*>(der.data()),der.size());out.close();if(!out)throw std::runtime_error("Stage public server CA");protect_path(certificate);state["trustedServerCertificateSha256"]=sha256(certificate);}
    }
    auto delivery=read_delivery_config(config);
    if(!delivery.is_null()){write_json(root/L"delivery-secret.json",delivery);protect_path(root/L"delivery-secret.json");state["deliveryStatus"]="pending-terminal-report";}
    // Expand the system drive at Audit logon; no executable/config is read from removable media.
    if(config.contains("answerFiles")){
        auto oobe=root/L"answer-oobe.xml";write_text_new(oobe,answer_content(config,"oobe"));protect_path(oobe);
        state["answerFiles"]={{"auditSha256",config.at("answerFiles").at("audit").at("sha256")},{"oobeSha256",config.at("answerFiles").at("oobe").at("sha256")}};
    }
    write_text_new(answer,config.contains("answerFiles")?answer_content(config,"audit"):agent_answer(L"%SystemDrive%\\ProgramData\\re-pxe\\re-pxe.exe"));protect_path(answer);
    // auditUser is a Setup launch. Install a persistent logon launch before the
    // first Windows boot, even if that Setup launch exits before its first scan.
    edit_offline_software(volume/L"Windows",[](HKEY hive){set_completion_startup(hive,true,completion_installed_path);return Json();});
    state["startupTrigger"]="HKLM Run + auditUser";state["startupTriggerRemoved"]=false;state["startupTriggerProvisionedOffline"]=true;
    write_json(root/L"completion.json",state);protect_path(root/L"completion.json");
}
Json device_status(){
    auto set=SetupDiGetClassDevsW(nullptr,nullptr,nullptr,DIGCF_ALLCLASSES|DIGCF_PRESENT);if(set==INVALID_HANDLE_VALUE)fail("Enumerate present devices");
    Json problems=Json::array();unsigned int count=0;
    try{
        SP_DEVINFO_DATA device{};device.cbSize=sizeof(device);
        for(DWORD i=0;;++i){
            if(!SetupDiEnumDeviceInfo(set,i,&device)){if(GetLastError()==ERROR_NO_MORE_ITEMS)break;fail("Read present device");}++count;
            ULONG status=0,problem=0;auto code=CM_Get_DevNode_Status(&status,&problem,device.DevInst,0);
            if(code!=CR_SUCCESS || (status&DN_HAS_PROBLEM)){
                std::array<wchar_t,4096> id{},name{};DWORD needed=0,type=0;
                SetupDiGetDeviceInstanceIdW(set,&device,id.data(),id.size(),&needed);
                SetupDiGetDeviceRegistryPropertyW(set,&device,SPDRP_DEVICEDESC,&type,reinterpret_cast<BYTE*>(name.data()),sizeof(name),&needed);
                Json item={{"instanceId",utf8(id.data())},{"description",utf8(name.data())},{"problemCode",problem},{"status",status},{"queryStatus",code}};
                for(const auto& property:std::array<std::pair<DWORD,const char*>,2>{{{SPDRP_HARDWAREID,"hardwareIds"},{SPDRP_COMPATIBLEIDS,"compatibleIds"}}}){
                    std::array<wchar_t,16384> ids{};item[property.second]=Json::array();
                    if(SetupDiGetDeviceRegistryPropertyW(set,&device,property.first,&type,reinterpret_cast<BYTE*>(ids.data()),sizeof(ids),&needed)&&type==REG_MULTI_SZ){
                        size_t pos=0;while(pos<ids.size()&&ids[pos]){auto end=pos;while(end<ids.size()&&ids[end])++end;if(end==ids.size())throw std::runtime_error("Device hardware IDs exceed bounds");item[property.second].push_back(utf8(std::wstring(ids.data()+pos,end-pos)));pos=end+1;}
                    }
                }
                problems.push_back(item);
            }
        }
    }catch(...){SetupDiDestroyDeviceInfoList(set);throw;}
    SetupDiDestroyDeviceInfoList(set);return {{"presentDeviceCount",count},{"problems",problems}};
}
Json hardware_summary(){
    FIRMWARE_TYPE firmware=FirmwareTypeUnknown;GetFirmwareType(&firmware);
    const auto os=L"SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion",bios=L"HARDWARE\\DESCRIPTION\\System\\BIOS";
    Json tpm={{"status","unknown"}};auto module=LoadLibraryExW(L"tbs.dll",nullptr,LOAD_LIBRARY_SEARCH_SYSTEM32);
    if(module){using GetInfo=UINT32(WINAPI*)(UINT32,PVOID);auto function=reinterpret_cast<GetInfo>(GetProcAddress(module,"Tbsi_GetDeviceInfo"));std::array<UINT32,4> info{};
        if(function){auto code=function(sizeof(info),info.data());tpm={{"apiStatus",code},{"status",code==0?"present":code==0x8028400f?"absent":"unknown"}};if(!code)tpm["version"]=info[1];}FreeLibrary(module);}
    return {{"specs",machine_specs()},{"firmware",firmware==FirmwareTypeUefi?"uefi":firmware==FirmwareTypeBios?"bios":"unknown"},{"secureBootEnabled",registry_number(L"SYSTEM\\CurrentControlSet\\Control\\SecureBoot\\State",L"UEFISecureBootEnabled")},
        {"tpm",tpm},{"manufacturer",utf8(registry_text(bios,L"SystemManufacturer"))},{"model",utf8(registry_text(bios,L"SystemProductName"))},
        {"edition",utf8(registry_text(os,L"EditionID"))},{"build",utf8(registry_text(os,L"CurrentBuildNumber"))},{"updateBuildRevision",registry_number(os,L"UBR")}};
}
Json collect_battery_health(const std::filesystem::path& root){
    auto name=L"battery-health-"+wide(guid_text(new_guid()));
    auto script=root/(name+L".ps1"),xml=root/(name+L".xml"),summary=root/(name+L".json"),log=root/(name+L".log");
    Json result={{"source","powercfg-batteryreport"},{"status","failed"},{"collectedUtc",utc_now()},{"batteries",Json::array()},{"warnings",Json::array()},
        {"script",utf8(script.wstring())},{"xml",utf8(xml.wstring())},{"summary",utf8(summary.wstring())},{"log",utf8(log.wstring())}};
    try{
        write_text_new(script,battery_health_script());protect_path(script);result["scriptSha256"]=sha256(script);
        auto code=run_logged({system_directory()+L"\\WindowsPowerShell\\v1.0\\powershell.exe",L"-NoLogo",L"-NoProfile",L"-NonInteractive",L"-ExecutionPolicy",L"Bypass",L"-File",script.wstring(),
            L"-PowercfgPath",system_directory()+L"\\powercfg.exe",L"-ReportPath",xml.wstring(),L"-OutputPath",summary.wstring()},log,"",60000);
        result["exitCode"]=code;
        if(code)throw std::runtime_error("PowerShell battery collection failed with exit code "+std::to_string(code));
        protect_path(xml);protect_path(summary);verify_protected(xml);verify_protected(summary);
        if(std::filesystem::file_size(xml)>16*1024*1024 || std::filesystem::file_size(summary)>64*1024)throw std::runtime_error("Battery evidence exceeds its bound");
        std::ifstream in(xml,std::ios::binary);std::string content((std::istreambuf_iterator<char>(in)),{});if(!in)throw std::runtime_error("Read powercfg battery report");
        result.update(powercfg_battery_health(content));result["xmlSha256"]=sha256(xml);result["summarySha256"]=sha256(summary);
    }catch(const std::exception& e){result["status"]="failed";result["error"]=e.what();result["warnings"].push_back("Battery health collection failed; inspect the retained battery evidence");}
    for(const auto& path:{script,xml,summary,log})if(std::filesystem::exists(path))try{protect_path(path);}catch(const std::exception& e){result["warnings"].push_back(e.what());}
    return result;
}
std::string boot_identity(){
    PSECURITY_DESCRIPTOR sd=nullptr;if(!ConvertStringSecurityDescriptorToSecurityDescriptorW(L"D:P(A;;KA;;;SY)(A;;KA;;;BA)",SDDL_REVISION_1,&sd,nullptr))fail("Build volatile boot key security");
    SECURITY_ATTRIBUTES sa{sizeof(sa),sd,FALSE};HKEY key{};DWORD disposition=0;
    auto code=RegCreateKeyExW(HKEY_LOCAL_MACHINE,L"SOFTWARE\\repxeCompletionBoot",0,nullptr,REG_OPTION_VOLATILE,KEY_ALL_ACCESS,&sa,&key,&disposition);LocalFree(sd);
    if(code)throw std::runtime_error("Create volatile boot identity");
    std::array<wchar_t,128> value{};DWORD size=sizeof(value);
    if(disposition==REG_CREATED_NEW_KEY){auto id=wide(guid_text(new_guid()));code=RegSetValueExW(key,L"Id",0,REG_SZ,reinterpret_cast<const BYTE*>(id.c_str()),static_cast<DWORD>((id.size()+1)*sizeof(wchar_t)));std::copy(id.begin(),id.end(),value.begin());}
    else code=RegGetValueW(key,nullptr,L"Id",RRF_RT_REG_SZ,nullptr,value.data(),&size);
    RegCloseKey(key);if(code || !value[0])throw std::runtime_error("Read volatile boot identity");return utf8(value.data());
}
void completion_trigger(bool install,const std::wstring& exe){
    RegistryKey software;
    auto code=RegOpenKeyExW(HKEY_LOCAL_MACHINE,L"SOFTWARE",0,KEY_READ|KEY_WRITE,&software.h);
    if(code)throw std::runtime_error("Open completion SOFTWARE registry");
    set_completion_startup(software.h,install,exe);
}
void clean_completion_answers(const std::filesystem::path& windows){
    for(const auto* name:{L"Panther\\Unattend.xml",L"Panther\\Unattend\\Unattend.xml"}){
        auto p=windows/name;if(!std::filesystem::exists(p))continue;no_reparse(p);
        auto text=read_tool_text(p);
        if(text.find("repxeCompletion-v1")!=std::string::npos || text.find("ProgramData\\re-pxe\\re-pxe.exe")!=std::string::npos)
            if(!DeleteFileW(p.c_str()))fail("Remove completion answer file");
    }
}
struct HttpHandle {
    HINTERNET value=nullptr;
    explicit HttpHandle(HINTERNET h):value(h){if(!h)fail("Create report HTTP handle");}
    ~HttpHandle(){if(value)WinHttpCloseHandle(value);}
    HttpHandle(const HttpHandle&)=delete;
};
Json post_report(const Json& secret,const std::string& payload){
    auto endpoint=secret.at("endpoint").get<std::string>();
    const auto begin=endpoint.find("/api/");
    if(begin==std::string::npos)throw std::runtime_error("Invalid report endpoint");
    return server_request(endpoint.substr(0,begin),secret.at("token"),endpoint.substr(begin),L"POST",payload,L"");
}
void completion_pulse(const std::filesystem::path& root,const char* stage,const std::string& error=""){
    auto path=root/L"delivery-secret.json";if(!std::filesystem::exists(path))return;
    try{verify_protected(path);auto secret=read_delivery_config({{"reportDelivery",utf8(path.wstring())}});auto endpoint=secret.at("endpoint").get<std::string>();endpoint.replace(endpoint.size()-6,6,"heartbeat");secret["endpoint"]=endpoint;Json pulse={{"stage",std::string(stage)=="needs-operator"?"needs-operator":std::string(stage)=="sealing-oobe"?"ready-for-oobe":"completing"}};pulse["error"]=error.substr(0,2000);post_report(secret,pulse.dump());}catch(...){}
}
Json deliver_queue(const std::filesystem::path& root){
    auto file=root/L"delivery-queue.json";verify_protected(file);auto queue=read_json(file);
    if(queue.at("status")=="delivered"){
        // A crash after durable ACK but before secret deletion is harmless on retry.
        auto secret=root/L"delivery-secret.json";if(std::filesystem::exists(secret)){verify_protected(secret);std::filesystem::remove(secret);}return queue;
    }
    auto report=root/L"report.json",secret=root/L"delivery-secret.json";verify_protected(report);verify_protected(secret);
    if(sha256(report)!=queue.at("sha256").get<std::string>() || std::filesystem::file_size(report)>512*1024)throw std::runtime_error("Queued report integrity differs");
    auto payload=read_tool_text(report);auto delivery=read_delivery_config({{"reportDelivery",utf8(secret.wstring())}});
    queue["attempts"]=queue.value("attempts",0)+1;queue["lastAttemptUtc"]=utc_now();write_json(file,queue);protect_path(file);
    try{
        auto ack=post_report(delivery,payload);
        auto endpoint=delivery.at("endpoint").get<std::string>();auto end=endpoint.rfind("/report"),begin=endpoint.rfind('/',end-1);auto id=endpoint.substr(begin+1,end-begin-1);
        if(ack.at("accepted")!=true || ack.at("sha256")!=queue.at("sha256") || ack.at("jobId")!=id)throw std::runtime_error("Report acknowledgement binding differs");
        queue["acknowledgement"]=ack;write_json(file,queue);protect_path(file);
        auto state=read_json(root/L"completion.json");
        if(state.value("stage",std::string())=="complete"&&state.value("finishOptions",Json::object()).value("save_report_desktop",false)){
            const auto origin=endpoint.substr(0,endpoint.find("/api/")),base="/api/client-jobs/"+id+"/customer-report";
            auto manifest=server_request(origin,delivery.at("token"),base,L"GET");
            if(manifest.at("schemaVersion")!=1||manifest.at("jobId")!=id||!manifest.at("files").is_array()||manifest.at("files").empty()||manifest.at("files").size()>33)throw std::runtime_error("Invalid customer report manifest");
            PWSTR desktop=nullptr;auto code=SHGetKnownFolderPath(FOLDERID_PublicDesktop,0,nullptr,&desktop);if(FAILED(code))throw std::runtime_error("Resolve customer Public Desktop");
            std::filesystem::path parent(desktop);CoTaskMemFree(desktop);no_reparse(parent);auto folder=parent/(L"re-pxe reports "+wide(id.substr(0,8)));std::filesystem::create_directory(folder);no_reparse(folder);
            Json copies=Json::array();unsigned long long total=0;std::set<std::string> names;
            for(const auto& entry:manifest.at("files")){
                auto name=entry.at("name").get<std::string>(),path=entry.at("path").get<std::string>(),hash=entry.at("sha256").get<std::string>();auto size=entry.at("size").get<unsigned long long>();
                if(!std::regex_match(name,std::regex("(re-pxe-report|erasure-drive-[0-9]{1,2})\\.pdf"))||!names.insert(name).second||path!=base+"/"+name||!std::regex_match(hash,std::regex("[a-f0-9]{64}"))||!size||size>64*MiB||total+size>64*MiB)throw std::runtime_error("Customer PDF metadata differs");total+=size;
                auto destination=folder/wide(name);if(std::filesystem::exists(destination))no_reparse(destination);
                if(!std::filesystem::exists(destination)){
                    auto pending=folder/(wide(name)+L"."+wide(guid_text(new_guid()))+L".partial");
                    try{pxe_download(origin,delivery.at("token"),path,pending,size);if(sha256(pending)!=hash)throw std::runtime_error("Customer PDF hash differs");std::filesystem::rename(pending,destination);}catch(...){std::filesystem::remove(pending);throw;}
                }
                if(std::filesystem::file_size(destination)!=size||sha256(destination)!=hash)throw std::runtime_error("Customer PDF already exists with different contents");
                copies.push_back({{"path",utf8(destination.wstring())},{"sha256",hash}});
            }
            if(!names.contains("re-pxe-report.pdf"))throw std::runtime_error("Customer package is missing its final report");queue["customerFiles"]=copies;
        }
        queue["status"]="delivered";queue["deliveredUtc"]=utc_now();queue.erase("lastError");write_json(file,queue);protect_path(file);std::filesystem::remove(secret);
    }catch(const std::exception& e){queue["status"]="pending";queue["lastError"]=e.what();write_json(file,queue);protect_path(file);}
    return queue;
}
void persist_report(const std::filesystem::path& root,Json& state){
    if(!completion_report_terminal(state)){
        // Repairable console failures must not consume the immutable final-report
        // credential. Preserve each attention snapshot before later repairs/sealing.
        auto snapshot=root/(L"attention-report-"+wide(guid_text(new_guid()))+L".json");
        state["deliveryStatus"]=std::filesystem::exists(root/L"delivery-secret.json")?"pending-technician-finish":"local-only";
        write_json(snapshot,state);protect_path(snapshot);
        state["attentionReports"].push_back({{"path",utf8(snapshot.wstring())},{"sha256",sha256(snapshot)},{"stage",state.at("stage")},{"utc",utc_now()}});
        write_json(root/L"report.json",state);protect_path(root/L"report.json");
        write_json(root/L"completion.json",state);protect_path(root/L"completion.json");
        completion_pulse(root,"needs-operator",state.value("error",std::string("Technician attention is required before finishing setup")));
        return;
    }
    auto report=root/L"report.json";write_json(report,state);protect_path(report);
    if(!std::filesystem::exists(root/L"delivery-secret.json"))return;
    auto file=root/L"delivery-queue.json";
    write_json(file,{{"schemaVersion",1},{"status","pending"},{"sha256",sha256(report)},{"attempts",0}});protect_path(file);
    auto queue=deliver_queue(root);state["deliveryStatus"]=queue.at("status");
    if(queue.contains("lastError"))state["deliveryError"]=queue.at("lastError");else state.erase("deliveryError");
    write_json(root/L"completion.json",state);protect_path(root/L"completion.json");
}

void finalize_customer_handoff(const std::filesystem::path& windows,const std::filesystem::path& root,Json& state){
    if(!completion_report_accepted(state)||!state.value("finishOptions",Json::object()).value("cleanup_client",false))return;
    auto id=state.at("id").get<std::string>(),hash=state.at("clientSha256").get<std::string>();
    if(!std::regex_match(id,std::regex("[a-f0-9]{16}"))||!std::regex_match(hash,std::regex("[a-f0-9]{64}")))throw std::runtime_error("Invalid cleanup installation binding");
    auto folder=windows/(L"Temp\\re-pxe-cleanup-"+wide(id));std::filesystem::create_directory(folder);protect_path(folder);auto script=folder/L"cleanup.ps1";
    if(std::filesystem::exists(script)){verify_protected(script);if(read_tool_text(script)!=cleanup_script())throw std::runtime_error("Cleanup script changed");}else{write_text_new(script,cleanup_script());protect_path(script);}
    auto launcher=folder/L"run.ps1";
    const auto content="& (Join-Path $PSScriptRoot 'cleanup.ps1') -InstallationId '"+id+"' -ExpectedClientSha256 '"+hash+"' -AgentProcessId "+std::to_string(GetCurrentProcessId())+"\n";
    write_json(root/L"cleanup-binding.json",{{"installationId",id},{"clientSha256",hash}});protect_path(root/L"cleanup-binding.json");
    if(std::filesystem::exists(launcher)){verify_protected(launcher);std::filesystem::remove(launcher);}write_text_new(launcher,content);protect_path(launcher);
    const auto shell=system_directory()+L"\\WindowsPowerShell\\v1.0\\powershell.exe";
    auto arguments=std::vector<std::wstring>{shell,L"-NoProfile",L"-ExecutionPolicy",L"Bypass",L"-File",launcher.wstring()};
    auto command=command_line(arguments),task=L"re-pxe-cleanup-"+wide(id);
    auto code=run_logged({system_directory()+L"\\schtasks.exe",L"/Create",L"/TN",task,L"/SC",L"ONSTART",L"/RU",L"SYSTEM",L"/RL",L"HIGHEST",L"/TR",command,L"/F"},root/L"schedule-cleanup.log");if(code)throw std::runtime_error("Schedule client cleanup failed with exit code "+std::to_string(code));
    state["cleanupScheduled"]=true;write_json(root/L"completion.json",state);protect_path(root/L"completion.json");
    STARTUPINFOW startup{};startup.cb=sizeof(startup);PROCESS_INFORMATION process{};
    if(!CreateProcessW(shell.c_str(),command.data(),nullptr,nullptr,FALSE,CREATE_NO_WINDOW,nullptr,windows.c_str(),&startup,&process))fail("Start client cleanup");CloseHandle(process.hThread);CloseHandle(process.hProcess);
}

}

void restrict_state_path(const std::filesystem::path& path){protect_path(path);}
void verify_state_path(const std::filesystem::path& path){verify_protected(path);}

namespace {
struct CompletionInstallation {std::filesystem::path windows,root,exe,file;Json state;};
CompletionInstallation completion_installation(const std::filesystem::path& offline){
    if(!offline.empty() && !winpe())throw std::runtime_error("Offline report delivery requires WinPE");
    if(offline.empty() && winpe())throw std::runtime_error("Completion requires installed Windows");
    std::array<wchar_t,32768> buffer{};auto length=GetWindowsDirectoryW(buffer.data(),buffer.size());if(!length || length>=buffer.size())fail("Resolve installed Windows");
    auto windows=offline.empty()?std::filesystem::path(buffer.data()):std::filesystem::absolute(offline),root=windows.parent_path()/L"ProgramData\\re-pxe",exe=root/L"re-pxe.exe",file=root/L"completion.json";
    // Reject workstation/removable invocations before inventory or any state writes.
    if(offline.empty() && _wcsicmp(std::filesystem::path(executable()).c_str(),exe.c_str()))throw std::runtime_error("Completion must run from its protected installed location");
    if(!admin())throw std::runtime_error("Completion requires an elevated Audit user");
    verify_protected(root);verify_protected(exe);verify_protected(file);
    auto state=read_json(file);
    const auto kernel=windows/L"System32\\ntoskrnl.exe";no_reparse(kernel);
    Handle kernel_lock(CreateFileW(kernel.c_str(),GENERIC_READ,FILE_SHARE_READ,nullptr,OPEN_EXISTING,0,nullptr));if(!kernel_lock)fail("Lock completion kernel for verification");
    const auto kernel_hash=sha256(kernel);
    const bool kernel_changed=kernel_hash!=state.at("kernelSha256").get<std::string>();
    validate_completion_bindings(state,sha256(exe),sha256(executable()),kernel_hash,kernel_changed&&trusted_microsoft_windows_kernel(kernel));
    auto resource=locate_resource(windows,"installed Windows");auto inv=inventory();auto target=state.at("target");
    Json actual;for(const auto& d:inv.at("disks"))if(d.at("identity")==target.at("identity")){if(!actual.is_null())throw std::runtime_error("Completion disk identity is ambiguous");actual=d;}
    if(actual.is_null() || !resource.value("resolved",false) || resource["diskNumbers"]!=Json::array({actual.at("number")}) || actual["sizeBytes"]!=target["sizeBytes"] || actual.value("removable",true))throw std::runtime_error("Completion destination identity differs");
    Handle disk(CreateFileW(wide(actual.at("devicePath")).c_str(),GENERIC_READ,FILE_SHARE_READ|FILE_SHARE_WRITE,nullptr,OPEN_EXISTING,0,nullptr));if(!disk)fail("Read completion disk binding");
    auto bytes=disk_layout(disk.value);auto layout=reinterpret_cast<const DRIVE_LAYOUT_INFORMATION_EX*>(bytes.data());
    if(layout->PartitionStyle!=PARTITION_STYLE_GPT || guid_text(layout->Gpt.DiskId)!=state.at("diskGuid").get<std::string>() || layout->PartitionCount!=4)throw std::runtime_error("Completion GPT binding differs");
    auto volume=wide(resource.at("volume"));if(!volume.empty() && volume.back()==L'\\')volume.pop_back();Handle h(CreateFileW(volume.c_str(),0,FILE_SHARE_READ|FILE_SHARE_WRITE,nullptr,OPEN_EXISTING,0,nullptr));PARTITION_INFORMATION_EX partition{};DWORD returned=0;
    if(!h || !DeviceIoControl(h.value,IOCTL_DISK_GET_PARTITION_INFO_EX,nullptr,0,&partition,sizeof(partition),&returned,nullptr) || partition.PartitionStyle!=PARTITION_STYLE_GPT || guid_text(partition.Gpt.PartitionId)!=state.at("windowsPartitionGuid").get<std::string>())throw std::runtime_error("Completion Windows partition binding differs");
    // Keep the original image hash as provenance. Runtime Windows servicing may
    // replace the kernel; only a verified Microsoft replacement is accepted.
    // This evidence is returned read-only by completion-status and is persisted
    // by the next normal state save after all destination bindings have passed.
    state["windowsKernelBinding"]={{"imageSha256",state.at("kernelSha256")},{"currentSha256",kernel_hash},{"changed",kernel_changed},{"microsoftSignatureVerified",kernel_changed}};
    return {windows,root,exe,file,state};
}
}

Json deliver_report(const std::filesystem::path& offline){
    auto installed=completion_installation(offline);
    OperationLock mutex(L"Global\\repxeCompletion");if(!mutex)throw std::runtime_error("Completion already running");
    if(installed.state.contains("trustedServerCertificateSha256")){
        auto certificate=installed.root/L"server-ca.der";verify_protected(certificate);
        if(sha256(certificate)!=installed.state.at("trustedServerCertificateSha256").get<std::string>())throw std::runtime_error("Provisioned server certificate changed");
        install_public_root(certificate);
    }
    auto queue=deliver_queue(installed.root);auto state=installed.state;state["deliveryStatus"]=queue.at("status");
    if(queue.contains("lastError"))state["deliveryError"]=queue.at("lastError");else state.erase("deliveryError");
    write_json(installed.file,state);protect_path(installed.file);finalize_customer_handoff(installed.windows,installed.root,state);return queue;
}

Json windows_update_worker(){
    auto installed=completion_installation({});auto& state=installed.state;
    auto image_state=registry_text(L"SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Setup\\State",L"ImageState");
    if(image_state.find(L"RESEAL_TO_AUDIT")==std::wstring::npos||state.at("stage")!="updating-drivers"||!state.contains("windowsUpdatePolicy"))throw std::runtime_error("Driver worker requires a protected pending Audit completion");
    OperationLock lock(L"Global\\repxeDriverUpdate");if(!lock)throw std::runtime_error("Driver worker is already running");
    const auto attempt=state.at("windowsUpdateAttempts").size();if(!attempt||attempt>3)throw std::runtime_error("Invalid driver update attempt");
    auto result=installed.root/(L"windows-update-"+std::to_wstring(attempt)+L".json");
    if(std::filesystem::exists(result))throw std::runtime_error("Driver attempt already has evidence; do not replay it");
    auto persist=[&](const Json& report){write_json(result,report);protect_path(result);};
    return update_unresolved_drivers(device_status().at("problems"),state.at("windowsUpdatePolicy").at("timeoutSeconds"),persist);
}

Json complete_windows(const std::string& mode){
    auto installed=completion_installation({});auto windows=installed.windows,root=installed.root,exe=installed.exe,file=installed.file;auto state=installed.state;
    OperationLock mutex(L"Global\\repxeCompletion");if(!mutex)return {{"stage","already-running"}};
    state=completion_state_after_reload(read_json(file),installed.state);
    if(state.at("stage")=="complete"){
        if(mode!="finish")return state;
        // Report retry is transport-only: do not run drivers, recovery or Sysprep again.
        if(!completion_report_accepted(state)){
            if(state.contains("trustedServerCertificateSha256")){
                auto certificate=root/L"server-ca.der";verify_protected(certificate);
                if(sha256(certificate)!=state.at("trustedServerCertificateSha256").get<std::string>())throw std::runtime_error("Provisioned server certificate changed");
                install_public_root(certificate);
            }
            auto queue=deliver_queue(root);state["deliveryStatus"]=queue.at("status");
            if(queue.contains("lastError"))state["deliveryError"]=queue.at("lastError");else state.erase("deliveryError");
            write_json(file,state);protect_path(file);
        }
        if(completion_report_accepted(state)){
            finalize_customer_handoff(windows,root,state);
            auto code=run_logged({system_directory()+L"\\shutdown.exe",L"/s",L"/t",L"10"},root/L"report-retry-shutdown.log");
            if(code)throw std::runtime_error("Report accepted; shutdown failed with exit code "+std::to_string(code));
        }
        return state;
    }
    auto image_state=utf8(registry_text(L"SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Setup\\State",L"ImageState"));
    const bool already_sealed=mode=="finish"&&completion_seal_action(image_state)=="finalize-oobe";
    if(!already_sealed&&image_state.find("RESEAL_TO_AUDIT")==std::string::npos)throw std::runtime_error("Completion requires an Audit Mode Windows setup state: "+image_state);
    // Refresh recovery layout only after destination binding has been checked.
    auto resource=locate_resource(windows,"installed Windows");auto disk_number=resource.at("diskNumbers").at(0).get<int>();
    Handle disk(CreateFileW((L"\\\\.\\PhysicalDrive"+std::to_wstring(disk_number)).c_str(),GENERIC_READ,FILE_SHARE_READ|FILE_SHARE_WRITE,nullptr,OPEN_EXISTING,0,nullptr));if(!disk)fail("Read completion layout");
    auto bytes=disk_layout(disk.value);auto layout=reinterpret_cast<const DRIVE_LAYOUT_INFORMATION_EX*>(bytes.data());
    auto stage=[&](const char* name){state["stage"]=name;state["history"].push_back({{"stage",name},{"utc",utc_now()}});write_json(file,state);protect_path(file);completion_pulse(root,name);};
    auto run=[&](const wchar_t* tool,const std::vector<std::wstring>& options,const char* name){std::vector<std::wstring> args{system_directory()+L"\\"+tool};args.insert(args.end(),options.begin(),options.end());auto log=root/(wide(name)+L"-"+std::to_wstring(state["history"].size())+L".log");auto code=run_logged(args,log);state["tools"].push_back({{"name",name},{"exitCode",code},{"log",utf8(log.wstring())}});write_json(file,state);protect_path(file);if(code)throw std::runtime_error(std::string(name)+" failed with exit code "+std::to_string(code));return log;};
    try{
        if(mode=="finish"&&state.value("driverConsole",false)){
            if(driver_finish_action(state,boot_identity())!="seal-oobe"||state.contains("pendingDriverUpload")||!state.value("technicianFinish",Json::object()).value("requested",false))throw std::runtime_error("Finish setup requires its saved technician request and restart checkpoint");
        }
        auto current=state.at("stage").get<std::string>();
        if(current=="updating-drivers"||current=="installing-drivers"){
            state["drivers"]=device_status();state["error"]="Driver installation was interrupted; review saved results before retrying";
            completion_trigger(false,exe.wstring());clean_completion_answers(windows);state["startupTriggerRemoved"]=true;
            // Keep automatic updates paused: a service-side install may still be finishing.
            stage("needs-operator");persist_report(root,state);return state;
        }
        auto action=(mode=="finish"&&current=="driver-menu")?std::string("seal-oobe"):completion_action(current,false,false);if(action=="stop")return state;
        const auto boot=boot_identity();
        if(current=="reboot-pending"){
            bool new_boot=boot!=state.at("checkpointBootId").get<std::string>();
            if(!new_boot)return state; // Startup duplicates cannot consume the reboot checkpoint.
        }
        if(state.contains("trustedServerCertificateSha256")){auto certificate=root/L"server-ca.der";verify_protected(certificate);if(sha256(certificate)!=state.at("trustedServerCertificateSha256").get<std::string>())throw std::runtime_error("Provisioned server certificate changed");install_public_root(certificate);}
        if(!state.value("driverConsole",false)||state["bootObservations"].empty()||state["bootObservations"].back().at("bootId")!=boot)state["bootObservations"].push_back({{"bootId",boot},{"uptimeMs",GetTickCount64()},{"imageState",image_state},{"utc",utc_now()}});
        // Scan immediately; server drivers precede the bounded Windows Update fallback.
        run(L"pnputil.exe",{L"/scan-devices"},"device-scan");Sleep(5000);
        state["hardware"]=hardware_summary();state["drivers"]=device_status();
        if(current=="awaiting-windows"){
            if(state.contains("driverPackage")){
                auto manifest=state.at("driverPackage");
                if(!driver_package_matches(manifest,state.at("hardware")))throw std::runtime_error("Approved driver package does not match destination hardware/build");
                auto drivers=root/L"drivers";verify_protected(drivers);std::vector<std::unique_ptr<Handle>> locks;
                for(const auto& f:manifest.at("files")){
                    auto path=drivers/wide(f.at("path"));verify_protected(path);
                    locks.push_back(std::make_unique<Handle>(CreateFileW(path.c_str(),GENERIC_READ,FILE_SHARE_READ,nullptr,OPEN_EXISTING,0,nullptr)));
                    if(!*locks.back() || std::filesystem::file_size(path)!=f.at("size") || sha256(path)!=f.at("sha256").get<std::string>())throw std::runtime_error("Installed approved driver package integrity differs");
                }
                // Persist before any installation. A interrupted installation cannot automatically repeat.
                state["driversBeforeInstallation"]=state.at("drivers");stage("installing-drivers");
                for(const auto& inf:manifest.at("infPaths")){
                    auto path=drivers/wide(inf),log=root/(L"driver-"+std::to_wstring(state["driverActions"].size())+L".log");
                    auto code=run_logged({system_directory()+L"\\pnputil.exe",L"/add-driver",path.wstring(),L"/install"},log,"",900000);
                    state["driverActions"].push_back({{"packageId",manifest.at("packageId")},{"inf",inf},{"exitCode",code},{"rebootRequired",code==3010},{"log",utf8(log.wstring())}});write_json(file,state);protect_path(file);
                    auto result=driver_attempt_result(code);state["driverActions"].back().update(result);state["driverPassSummary"]=driver_pass_summary(state.at("driverActions"));write_json(file,state);protect_path(file);
                    if(result.value("installationMayBeInProgress",false))throw std::runtime_error("Driver installation state is uncertain; restart and review diagnostics");
                }
                if(state.at("driverPassSummary").at("failed")>0)throw std::runtime_error("Driver pass finished with failures; review the per-driver diagnostics");
                run(L"pnputil.exe",{L"/scan-devices"},"post-driver-scan");Sleep(5000);state["drivers"]=device_status();
            }
            completion_trigger(true,exe.wstring());state["startupTrigger"]="HKLM Run + auditUser";state["checkpointBootId"]=boot;state["checkpointReason"]="verify device and recovery state after restart";stage("reboot-pending");
            run(L"shutdown.exe",{L"/r",L"/t",L"10"},"checkpoint-reboot");return state;
        }
        action=(mode=="finish"&&current=="driver-menu")?"seal-oobe":completion_action(current,true,!state["drivers"]["problems"].empty());
        if(mode=="finish"&&state.value("driverConsole",false)){
            state["technicianFinish"]["unresolvedDeviceCount"]=state["drivers"]["problems"].size();
            state["technicianFinish"]["sealedUtc"]=utc_now();
            // Retain earlier diagnostics and current unresolved devices as report evidence.
            write_json(file,state);protect_path(file);
        }
        if(mode!="finish"&&state.contains("windowsUpdatePolicy")){
            auto& attempts=state["windowsUpdateAttempts"];auto last=attempts.empty()?std::string():attempts.back().value("status",std::string("interrupted"));
            action=driver_fallback_action(!state["drivers"]["problems"].empty(),static_cast<int>(attempts.size()),state["windowsUpdatePolicy"].at("maxAttempts"),last);
            if(action=="windows-update"){
                attempts.push_back({{"status","running"},{"startedUtc",utc_now()},{"unresolvedDevices",state["drivers"]["problems"]}});
                stage("updating-drivers");
                auto index=attempts.size();auto timeout=state["windowsUpdatePolicy"].at("timeoutSeconds").get<DWORD>();
                auto log=root/(L"windows-update-"+std::to_wstring(index)+L".log");
                const auto code=run_logged({exe.wstring(),L"update-drivers"},log,"",(timeout+60)*1000);
                auto evidence=root/(L"windows-update-"+std::to_wstring(index)+L".json");Json result;
                if(std::filesystem::exists(evidence)){verify_protected(evidence);if(std::filesystem::file_size(evidence)>256*1024)throw std::runtime_error("Driver update evidence exceeds bound");result=read_json(evidence);}
                else result={{"status","failed"},{"error","Driver update worker did not record a result"}};
                if(code!=0||result.value("status",std::string())=="running"){
                    result["status"]=code==ERROR_TIMEOUT?"timed-out":"failed";result["error"]="Driver worker stopped without a completed operation";result["installationMayBeInProgress"]=true;
                }
                attempts.back()["result"]=result;attempts.back()["status"]=result.value("status",std::string("failed"));attempts.back()["workerExitCode"]=code;attempts.back()["finishedUtc"]=utc_now();
                write_json(file,state);protect_path(file);
                run(L"pnputil.exe",{L"/scan-devices"},"post-update-scan");Sleep(5000);state["drivers"]=device_status();
                if(result.at("status")=="succeeded"){
                    // All successful attempts receive a new boot before verification,
                    // including installs that report no explicit restart requirement.
                    state["checkpointBootId"]=boot;state["checkpointReason"]="verify Windows Update drivers after restart";stage("reboot-pending");
                    run(L"shutdown.exe",{L"/r",L"/t",L"10"},"driver-update-reboot");return state;
                }
                state["error"]=result.value("error",std::string("Windows Update found no applicable driver for the remaining devices"));action="needs-operator";
            }
            if(action=="needs-operator"&&!state.contains("error"))state["error"]="Unresolved devices remain after the configured Windows Update attempt limit";
        }
        if(action=="needs-operator"){if(!state.contains("windowsUpdateAttempts")||state["windowsUpdateAttempts"].empty()||!state["windowsUpdateAttempts"].back().value("result",Json::object()).value("installationMayBeInProgress",false))restore_update_policy(state);completion_trigger(false,exe.wstring());clean_completion_answers(windows);state["startupTriggerRemoved"]=true;stage("needs-operator");persist_report(root,state);return state;}
        // Refresh components after the driver's final scan, including newly named GPUs.
        // Battery evidence is optional: a desktop or an unsupported controller still seals.
        state["hardware"]["specs"]=inventory().at("specs");state["hardware"]["capturedUtc"]=utc_now();
        state["batteryHealth"]=collect_battery_health(root);
        if(!state["batteryHealth"]["batteries"].empty())state["hardware"]["specs"]["batteries"]=state["batteryHealth"]["batteries"];
        for(const auto& warning:state["batteryHealth"]["warnings"])state["hardware"]["specs"]["warnings"].push_back(warning);
        stage("completing");if(!already_sealed)run(L"reagentc.exe",{L"/enable",L"/auditmode"},"enable-recovery");auto recovery_log=run(L"reagentc.exe",{L"/info"},"recovery-info");
        auto recovery=read_tool_text(recovery_log);state["recovery"]={{"status","enabled-online"},{"infoLog",utf8(recovery_log.wstring())}};
        // Validate the registered recovery image using XML, not localized console text.
        pugi::xml_document re_doc;if(!re_doc.load_file((windows/L"System32\\Recovery\\ReAgent.xml").c_str()))throw std::runtime_error("Read online recovery metadata");
        auto re_root=re_doc.child("WindowsRE"),location=re_root.child("WinreLocation"),bcd=re_root.child("WinreBCD");
        auto expected_offset=layout->PartitionEntry[3].StartingOffset.QuadPart;
        if(location.attribute("offset").as_llong()!=expected_offset || guid_text(guid(wide(location.attribute("guid").as_string()).c_str()))!=state.at("diskGuid").get<std::string>() || !*bcd.attribute("id").as_string())throw std::runtime_error("Online recovery metadata does not match destination recovery partition");
        state["recovery"]["bcdId"]=bcd.attribute("id").as_string();stage("ready-for-oobe");
        // Clear the Audit GUI that Windows opens by default; never affect unrelated programs.
        if(!already_sealed){auto kill_log=run_logged({system_directory()+L"\\taskkill.exe",L"/IM",L"sysprep.exe",L"/F"},root/L"close-audit-sysprep.log");state["auditGuiCloseExitCode"]=kill_log;}
        std::string oobe_content="<unattend xmlns=\"urn:schemas-microsoft-com:unattend\"><settings pass=\"oobeSystem\"><component name=\"Microsoft-Windows-Deployment\" processorArchitecture=\"amd64\" publicKeyToken=\"31bf3856ad364e35\" language=\"neutral\" versionScope=\"nonSxS\"><Reseal><Mode>OOBE</Mode></Reseal></component></settings></unattend>";
        if(state.contains("answerFiles")){
            auto saved=root/L"answer-oobe.xml";verify_protected(saved);if(std::filesystem::file_size(saved)>64*1024 || sha256(saved)!=state.at("answerFiles").at("oobeSha256").get<std::string>())throw std::runtime_error("Pinned OOBE answer changed");
            std::ifstream in(saved,std::ios::binary);oobe_content.assign(std::istreambuf_iterator<char>(in),std::istreambuf_iterator<char>());if(!in)throw std::runtime_error("Read staged OOBE answer");validate_answer_xml(oobe_content,"oobe");
        }
        if(state.contains("finalAnswerSha256")){
            auto saved=root/L"answer-customer.xml";verify_protected(saved);if(std::filesystem::file_size(saved)>64*1024||sha256(saved)!=state.at("finalAnswerSha256").get<std::string>())throw std::runtime_error("Pinned customer answer changed");std::ifstream in(saved,std::ios::binary);oobe_content.assign(std::istreambuf_iterator<char>(in),std::istreambuf_iterator<char>());validate_answer_xml(oobe_content,"oobe");
        }
        auto oobe=root/L"oobe.xml";write_text_new(oobe,oobe_content);protect_path(oobe);
        stage("sealing-oobe");if(!already_sealed)run(L"Sysprep\\sysprep.exe",{L"/oobe",L"/quit",L"/quiet",L"/unattend:"+oobe.wstring()},"seal-oobe");else state["technicianFinish"]["existingOobeCheckpoint"]=true;
        state["sealedImageState"]=utf8(registry_text(L"SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Setup\\State",L"ImageState"));
        if(state["sealedImageState"]!="IMAGE_STATE_SPECIALIZE_RESEAL_TO_OOBE")throw std::runtime_error("Sysprep did not produce an OOBE checkpoint");
        restore_update_policy(state);
        completion_trigger(false,exe.wstring());clean_completion_answers(windows);state["startupTriggerRemoved"]=true;
        state["completedUtc"]=utc_now();state["oobeBootVerified"]=false;stage("complete");persist_report(root,state);
        if(completion_report_accepted(state)){finalize_customer_handoff(windows,root,state);run(L"shutdown.exe",{L"/s",L"/t",L"10"},"completion-shutdown");}return state;
    }catch(const std::exception& e){if(state.value("stage",std::string())=="complete"){state["shutdownError"]=e.what();write_json(file,state);protect_path(file);throw;}state["error"]=e.what();if(!state.contains("drivers"))state["drivers"]={{"problems",Json::array()}};try{completion_trigger(false,exe.wstring());state["startupTriggerRemoved"]=true;}catch(...){}stage("failed");try{persist_report(root,state);}catch(...){}throw;}
}

bool completion_uses_console(){return completion_installation({}).state.value("driverConsole",false);}
Json completion_status(){
    auto installed=completion_installation({});auto state=installed.state;
    state["currentImageState"]=utf8(registry_text(L"SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Setup\\State",L"ImageState"));
    state["finishAvailable"]=driver_finish_available(state);return state;
}

void restart_system(const std::filesystem::path& log){
    const auto tool=winpe()?L"wpeutil.exe":L"shutdown.exe";
    auto args=winpe()?std::vector<std::wstring>{system_directory()+L"\\"+tool,L"Reboot"}:std::vector<std::wstring>{system_directory()+L"\\"+tool,L"/r",L"/t",L"10"};
    auto code=run_logged(args,log);
    if(code)throw std::runtime_error("Restart failed with exit code "+std::to_string(code)+"; diagnostic: "+utf8(log.wstring()));
}

namespace {
void driver_environment(const CompletionInstallation& installed,bool allow_oobe_finish=false){
    if(!installed.state.value("driverConsole",false))throw std::runtime_error("This installation has no provisioned driver console");
    auto image_state=registry_text(L"SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Setup\\State",L"ImageState");
    if(image_state.find(L"RESEAL_TO_AUDIT")==std::wstring::npos && !(allow_oobe_finish&&completion_seal_action(utf8(image_state))=="finalize-oobe"))throw std::runtime_error("The driver console requires the provisioned Windows Audit installation");
    if(installed.state.contains("trustedServerCertificateSha256")){
        auto certificate=installed.root/L"server-ca.der";verify_protected(certificate);
        if(sha256(certificate)!=installed.state.at("trustedServerCertificateSha256").get<std::string>())throw std::runtime_error("Provisioned server certificate changed");
        install_public_root(certificate);
    }
}
struct DriverConnection{std::string origin,path,token;};
DriverConnection driver_connection(const CompletionInstallation& installed){
    const auto secret=installed.root/L"delivery-secret.json";verify_protected(secret);
    auto delivery=read_delivery_config({{"reportDelivery",utf8(secret.wstring())}});
    auto endpoint=delivery.at("endpoint").get<std::string>();auto start=endpoint.find("/api/client-jobs/");
    if(start==std::string::npos||!endpoint.ends_with("/report"))throw std::runtime_error("Invalid provisioned driver server endpoint");
    return {endpoint.substr(0,start),endpoint.substr(start,endpoint.size()-start-7),delivery.at("token")};
}
Json driver_catalog(const CompletionInstallation& installed){
    auto connection=driver_connection(installed);auto hardware=hardware_summary();
    Json request={{"manufacturer",hardware.at("manufacturer")},{"model",hardware.at("model")}};
    auto result=pxe_api(connection.origin,connection.token,connection.path+"/drivers/catalog",&request);
    if(result.at("hardware").at("build")!=hardware.at("build"))throw std::runtime_error("Installed Windows build differs from the driver server binding");
    if(!result.at("drivers").is_array()||result.at("drivers").size()>256)throw std::runtime_error("Driver server catalog exceeds its bound");
    for(const auto& entry:result.at("drivers"))if(!driver_package_matches(entry.at("manifest"),hardware))throw std::runtime_error("Server offered a driver for different hardware or Windows build");
    return result;
}
}

Json driver_session_snapshot(){
    auto installed=completion_installation({});driver_environment(installed,true);
    auto state=installed.state;state["hardware"]=hardware_summary();state["drivers"]=device_status();
    state["serverConfigured"]=std::filesystem::exists(installed.root/L"delivery-secret.json");
    state["logsDirectory"]=utf8(installed.root.wstring());
    return state;
}

void driver_session_pulse(){
    thread_local std::filesystem::path root;
    if(root.empty()){auto installed=completion_installation({});driver_environment(installed);root=installed.root;}
    const auto file=root/L"completion.json";verify_protected(file);if(std::filesystem::file_size(file)>4*MiB)throw std::runtime_error("Driver state exceeds heartbeat bound");
    auto state=read_json(file);if(state.value("stage",std::string())=="complete")return;const bool attention=state.value("stage",std::string())=="driver-menu"&&(state.contains("error")||(state.contains("drivers")&&!state.at("drivers").at("problems").empty()));
    completion_pulse(root,attention?"needs-operator":"completing",attention?state.value("error",std::string("Devices need drivers. Open the driver console for details.")):std::string());
}

void driver_remote_controls(){
    // Probe driver ownership briefly. File transfers never hold driver locks;
    // only power commands must acquire them again before changing Windows.
    thread_local std::optional<CompletionInstallation> cached;
    if(!cached){cached=completion_installation({});driver_environment(*cached);}
    const auto& context=*cached;
    verify_protected(context.file);
    if(std::filesystem::file_size(context.file)>4*MiB)throw std::runtime_error("Driver state exceeds control bound");
    auto state=read_json(context.file);
    if(state.value("stage",std::string())=="complete")return;
    bool busy;
    {
        OperationLock operation(L"Global\\repxeCompletion"),worker(L"Global\\repxeDriverUpdate");
        busy=!operation||!worker||state.value("driverInstallationMayBeInProgress",false);
    }
    const auto connection=driver_connection(context);
    Json request={{"client_version","0.14.1"},{"busy",busy}};
    auto response=pxe_api(connection.origin,connection.token,connection.path+"/windows/poll",&request);
    if(!response.contains("command")||response.at("command").is_null())return;
    auto command=response.at("command");validate_windows_command(command,connection.path);
    const auto id=command.at("id").get<std::string>(),action=command.at("action").get<std::string>();
    std::unique_ptr<OperationLock> operation,worker;
    if(action!="file"){
        operation=std::make_unique<OperationLock>(L"Global\\repxeCompletion");
        worker=std::make_unique<OperationLock>(L"Global\\repxeDriverUpdate");
        if(!*operation||!*worker||read_json(context.file).value("driverInstallationMayBeInProgress",false))return;
    }
    const auto receipt_file=context.root/L"windows-control-receipts.json";
    Json receipts=Json::object();
    if(std::filesystem::exists(receipt_file)){
        verify_protected(receipt_file);
        if(std::filesystem::file_size(receipt_file)>4*MiB)throw std::runtime_error("Windows control receipts exceed bound");
        receipts=read_json(receipt_file);
    }
    auto save=[&]{write_json(receipt_file,receipts);protect_path(receipt_file);};
    // Save before the side effect. A crash or lost acknowledgement redelivers
    // this receipt's outcome without repeating a file copy or power command.
    const bool execute=prepare_windows_receipt(receipts,command,utc_now());save();
    if(execute){
        Json result={{"status","completed"},{"path",""},{"error",""}};
        try{
            const auto installed=completion_installation({});driver_environment(installed);
            if(action=="file"){
                auto directory=installed.root/L"received-files";
                if(!std::filesystem::exists(directory)){std::filesystem::create_directory(directory);protect_path(directory);}
                verify_protected(directory);
                const auto staged=directory/(wide(id)+L".part");
                if(std::filesystem::exists(staged)){no_reparse(staged);std::filesystem::remove(staged);}
                try{
                    auto last_pulse=GetTickCount64();
                    pxe_download(connection.origin,connection.token,command.at("download_path"),staged,command.at("size"),[&](unsigned long long){
                        if(GetTickCount64()-last_pulse<10000)return;
                        last_pulse=GetTickCount64();
                        // Keep the portal online during a large transfer. This
                        // pulse advertises activity without claiming another file.
                        try{
                            completion_pulse(context.root,"completing");
                            Json pulse={{"client_version","0.14.1"},{"busy",true},{"accept_commands",false}};
                            pxe_api(connection.origin,connection.token,connection.path+"/windows/poll",&pulse);
                        }catch(...){}
                    });
                    if(sha256(staged)!=command.at("sha256"))throw std::runtime_error("Uploaded desktop file hash differs");
                    PWSTR known=nullptr;
                    if(FAILED(SHGetKnownFolderPath(FOLDERID_Desktop,KF_FLAG_CREATE,nullptr,&known)))throw std::runtime_error("Cannot locate the signed-in user's Desktop");
                    std::filesystem::path desktop(known);CoTaskMemFree(known);no_reparse(desktop);
                    const auto leaf=wide(command.at("filename"));
                    if(leaf.size()>200)throw std::runtime_error("Desktop filename exceeds Windows bound");
                    auto destination=desktop/leaf;
                    if(std::filesystem::exists(destination))destination=desktop/(std::filesystem::path(leaf).stem().wstring()+L" (re-pxe "+wide(id)+L")"+std::filesystem::path(leaf).extension().wstring());
                    // CopyFile refuses collisions and uses the desktop's normal
                    // inherited ACL, rather than retaining protected staging ACLs.
                    if(!CopyFileW(staged.c_str(),destination.c_str(),TRUE))fail("Copy uploaded file to Desktop");
                    result["path"]=utf8(destination.wstring());std::filesystem::remove(staged);
                }catch(...){std::error_code ec;std::filesystem::remove(staged,ec);throw;}
            }else{
                auto current=completion_state_after_reload(read_json(installed.file),installed.state);
                completion_trigger(true,installed.exe.wstring());current["startupTriggerRemoved"]=false;
                current["startupTrigger"]="HKLM Run + auditUser";
                if(action=="restart"){
                    current["checkpointBootId"]=boot_identity();current["stage"]="driver-reboot-pending";
                    current["history"].push_back({{"stage","driver-reboot-pending"},{"utc",utc_now()},{"reason","portal-restart"}});
                }
                write_json(installed.file,current);protect_path(installed.file);
                auto code=run_logged({system_directory()+L"\\shutdown.exe",action=="restart"?L"/r":L"/s",L"/t",L"10"},installed.root/(wide(id)+L"-portal-power.log"));
                if(code)throw std::runtime_error("Windows power command failed with exit code "+std::to_string(code));
            }
        }catch(const std::exception& e){result["status"]="failed";result["error"]=std::string(e.what()).substr(0,1000);}
        receipts[id]["result"]=result;save();
    }
    const auto result=receipts.at(id).at("result");
    auto ack=pxe_api(connection.origin,connection.token,connection.path+"/windows/commands/"+id+"/result",&result);
    if(!ack.value("accepted",false)||ack.at("id")!=id)throw std::runtime_error("Windows control acknowledgement differs");
}

Json driver_session_action(const std::string& action,const std::filesystem::path& upload,const std::function<void(const std::string&)>& output){
    if(action=="heartbeat"){driver_session_pulse();return Json::object();}
    const auto installed=completion_installation({});driver_environment(installed,action=="finish"||action=="scan"||action=="shutdown");
    auto state=installed.state;auto root=installed.root,file=installed.file;
    if(state.value("stage",std::string())=="complete"){
        if(action=="scan")return state;
        if(action=="finish")return complete_windows("finish");
        if(action=="shutdown"){
            if(output)output("Windows is sealed. Shutting down; any pending final report remains saved locally.");
            auto code=run_logged({system_directory()+L"\\shutdown.exe",L"/s",L"/t",L"10"},root/L"technician-sealed-shutdown.log");
            if(code)throw std::runtime_error("Shutdown failed with exit code "+std::to_string(code));return state;
        }
        throw std::runtime_error("Windows is sealed. Retry its final report or shut down; drivers cannot be changed here.");
    }
    if(!state.contains("tools"))state["tools"]=Json::array();
    if(action=="finish"){
        std::string next;
        OperationLock finish_lock(L"Global\\repxeCompletion");
        if(!finish_lock)throw std::runtime_error("Another driver operation is running");
        OperationLock worker_lock(L"Global\\repxeDriverUpdate");
        if(!worker_lock)throw std::runtime_error("A Windows Update driver operation is still running");
        state=completion_state_after_reload(read_json(file),installed.state);next=driver_finish_action(state,boot_identity());
        const auto live_seal=completion_seal_action(utf8(registry_text(L"SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Setup\\State",L"ImageState")));
        if(live_seal=="finalize-oobe"&&next=="checkpoint-reboot")throw std::runtime_error("OOBE is already sealed but its driver restart checkpoint is incomplete; preserve the report and review the saved restart evidence");
        if(state.contains("error")||state.contains("sealedImageState")){
            state["finishAttemptEvidence"].push_back({{"stage",state.at("stage")},{"error",state.value("error",std::string())},{"sealedImageState",state.value("sealedImageState",std::string())},{"utc",utc_now()}});
            state.erase("error");state.erase("sealedImageState");
        }
        if(!state.contains("technicianFinish"))state["technicianFinish"]={{"requested",true},{"requestedUtc",utc_now()},{"previousStage",state.at("stage")}};
        state["driverAutomaticAttempted"]=true;
        if(state.contains("pendingDriverUpload")){
            auto pending=state.at("pendingDriverUpload");pending["abandonedUtc"]=utc_now();
            try{auto connection=driver_connection(installed);Json empty=Json::object();pxe_api(connection.origin,connection.token,connection.path+"/driver-uploads/"+pending.at("id").get<std::string>()+"/cancel",&empty);pending["serverCancelled"]=true;}
            catch(const std::exception& e){pending["serverCancelled"]=false;pending["error"]=e.what();}
            state["abandonedDriverUploads"].push_back(pending);state.erase("pendingDriverUpload");
            if(output)output("Unfinished upload abandoned. The local archive and diagnostics are retained.");
        }
        if(state.at("stage")!="driver-menu"&&state.at("stage")!="driver-reboot-pending"){
            state["stage"]="driver-menu";state["history"].push_back({{"stage","driver-menu"},{"utc",utc_now()},{"reason","explicit-technician-finish"}});
        }
        write_json(file,state);protect_path(file);
        if(next=="checkpoint-reboot"){
            state["driverFinishAfterReboot"]=true;write_json(file,state);protect_path(file);
            if(output)output("Finish setup requested. Restarting before sealing; remaining device problems will be recorded.");
            completion_trigger(true,installed.exe.wstring());return driver_session_action("restart",{},output);
        }
        state["driverFinishAfterReboot"]=false;write_json(file,state);protect_path(file);
        return complete_windows("finish");
    }
    OperationLock mutex(L"Global\\repxeCompletion");
    if(!mutex)throw std::runtime_error("Another driver operation is running");
    state=completion_state_after_reload(read_json(file),installed.state);
    auto emit=[&](const std::string& line){if(output)output(line);};
    auto save=[&]{write_json(file,state);protect_path(file);};
    auto restart=[&](const std::filesystem::path& log){
        completion_trigger(true,installed.exe.wstring());state["startupTrigger"]="HKLM Run + auditUser";state["startupTriggerRemoved"]=false;save();restart_system(log);
    };
    auto stage=[&](const char* name){state["stage"]=name;state["history"].push_back({{"stage",name},{"utc",utc_now()}});save();completion_pulse(root,name);};
    auto run=[&](const std::vector<std::wstring>& args,const std::string& name,bool driver_install=false){auto log=root/(wide(name)+L"-"+wide(guid_text(new_guid()))+L".log");auto code=run_logged(args,log,"",driver_install?900000:INFINITE);state["tools"].push_back({{"name",name},{"exitCode",code},{"log",utf8(log.wstring())}});save();if(!driver_install&&code&&code!=3010)throw std::runtime_error(name+" failed with exit code "+std::to_string(code));return code;};
    const auto boot=boot_identity();
    if(state["bootObservations"].empty()||state["bootObservations"].back().at("bootId")!=boot){state["bootObservations"].push_back({{"bootId",boot},{"uptimeMs",GetTickCount64()},{"imageState",utf8(registry_text(L"SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Setup\\State",L"ImageState"))},{"utc",utc_now()}});save();}
    const auto previous=state.at("stage").get<std::string>();
    if(previous=="driver-reboot-pending"){
        if(state.at("checkpointBootId")==boot&&action!="restart")throw std::runtime_error("Restart is required before another driver operation");
        if(state.at("checkpointBootId")!=boot){state["driverRebootRequired"]=false;state["driverInstallationMayBeInProgress"]=false;state["driverVerifiedBoot"]=boot;
            if(state.contains("windowsUpdateAttempts"))for(auto& attempt:state["windowsUpdateAttempts"])if(attempt.value("status",std::string())=="running"){
                attempt["status"]="interrupted";attempt["finishedUtc"]=utc_now();attempt["result"]={{"status","interrupted"},{"error","Restarted after an interrupted driver update"},{"installationMayBeInProgress",false}};
            }
            stage("driver-menu");}
    }
    if(previous=="installing-drivers"||previous=="updating-drivers"){
        state["driverAutomaticAttempted"]=true;state["driverInstallationMayBeInProgress"]=true;
        state["driverRebootRequired"]=true;state["error"]="Driver installation was interrupted. Restart before retrying.";
        stage("driver-menu");
    }
    if(action=="restart"){
        state["checkpointBootId"]=boot;state["driverRebootRequired"]=true;stage("driver-reboot-pending");
        restart(root/(L"driver-restart-"+std::to_wstring(GetTickCount64())+L".log"));return state;
    }
    if(action=="catalog")return driver_catalog(installed);
    if(action=="scan"){
        completion_trigger(true,installed.exe.wstring());state["startupTrigger"]="HKLM Run + auditUser";state["startupTriggerRemoved"]=false;
        emit("Scanning connected devices…");run({system_directory()+L"\\pnputil.exe",L"/scan-devices"},"console-device-scan");Sleep(3000);
        state["hardware"]=hardware_summary();state["drivers"]=device_status();
        if(state.at("stage")!="driver-reboot-pending")stage("driver-menu");else save();return state;
    }
    if(action=="local-inf"&&(state.value("driverInstallationMayBeInProgress",false)||state.value("driverRebootRequired",false)))throw std::runtime_error("Restart before installing more drivers");
    if(action=="local-inf"){
        no_reparse(upload);if(!std::filesystem::is_regular_file(upload)||_wcsicmp(upload.extension().c_str(),L".inf"))throw std::runtime_error("Choose a signed INF and retain its supporting files beside it");
        stage("installing-drivers");emit("Installing selected local driver using Windows signature checks…");
        try{auto code=run({system_directory()+L"\\pnputil.exe",L"/add-driver",upload.wstring(),L"/install"},"local-driver-install",true);auto result=driver_install_result(code);result["source"]="operator-selected local INF";result["inf"]=utf8(upload.wstring());state["driverActions"].push_back(result);
            if(!result.at("changed").get<bool>()){state.erase("error");stage("driver-menu");emit("Skipped: no matching device, or Windows already uses a better/newer driver.");return state;}
            state["driverRebootRequired"]=true;state["checkpointBootId"]=boot;stage("driver-reboot-pending");restart(root/(L"driver-restart-"+std::to_wstring(GetTickCount64())+L".log"));return state;}
        catch(const std::exception& e){state["error"]=e.what();state["driverRebootRequired"]=true;state["driverInstallationMayBeInProgress"]=true;stage("driver-menu");throw;}
    }
    if(action=="upload"||action=="export-upload"||action=="retry-upload"){
        state.erase("driverUploadError");save();
        try{
        auto catalog=driver_catalog(installed);(void)catalog;auto connection=driver_connection(installed);auto path=upload;
        if(state.contains("pendingDriverUpload")){
            auto remote=pxe_api(connection.origin,connection.token,connection.path+"/driver-uploads/"+state.at("pendingDriverUpload").at("id").get<std::string>());
            if(remote.at("status")=="cancelled"){state.erase("pendingDriverUpload");save();emit("The previous upload was cancelled on the website. A new archive can be uploaded.");}
        }
        if(action=="retry-upload"){
            if(!state.contains("pendingDriverUpload"))throw std::runtime_error("There is no interrupted upload to retry");
            path=wide(state.at("pendingDriverUpload").at("path"));emit("Checking the saved archive before resuming its upload…");
        }
        if(action=="export-upload"){
            if(state.value("driverRebootRequired",false)||state.value("driverInstallationMayBeInProgress",false))throw std::runtime_error("Restart and check devices before collecting a reusable driver package");
            if(state.contains("pendingDriverUpload"))throw std::runtime_error("An upload is unfinished. Choose Retry upload before collecting another package");
            emit("Checking hardware and connected devices before collecting drivers…");run({system_directory()+L"\\pnputil.exe",L"/scan-devices"},"export-device-scan");Sleep(3000);
            auto hardware=hardware_summary(),before=device_status();state["hardware"]=hardware;state["drivers"]=before;save();validate_driver_export(hardware,before);
            if(!before.at("problems").empty())emit("Collecting available drivers; "+std::to_string(before.at("problems").size())+" unresolved devices are recorded in the collection diagnostics.");
            auto directory=root/(L"export-"+wide(guid_text(new_guid())));if(!std::filesystem::create_directory(directory))throw std::runtime_error("Create a fresh driver export directory");protect_path(directory);
            auto report=directory;report+=L".json";Json evidence={{"schemaVersion",1},{"startedUtc",utc_now()},{"hardware",hardware},{"devicesBefore",before},{"status","collecting"}};
            write_json(report,evidence);protect_path(report);state["driverExports"].push_back({{"report",utf8(report.wstring())},{"directory",utf8(directory.wstring())},{"utc",utc_now()}});save();
            run({system_directory()+L"\\pnputil.exe",L"/enum-drivers"},"export-driver-inventory");evidence["inventoryLog"]=state.at("tools").back().at("log");write_json(report,evidence);protect_path(report);
            emit("Exporting installed third-party drivers…");run({system_directory()+L"\\pnputil.exe",L"/export-driver",L"*",directory.wstring()},"export-drivers");
            evidence["exportLog"]=state.at("tools").back().at("log");auto after=device_status();evidence["devicesAfter"]=after;state["drivers"]=after;state["hardware"]=hardware_summary();save();
            evidence["status"]="checking";write_json(report,evidence);protect_path(report);validate_driver_export(hardware_summary(),after);
            size_t inf_count=0,file_count=0;unsigned long long bytes=0;
            for(const auto& item:std::filesystem::recursive_directory_iterator(directory)){no_reparse(item.path());if(item.is_regular_file()){++file_count;bytes+=item.file_size();if(!_wcsicmp(item.path().extension().c_str(),L".inf"))++inf_count;}}
            if(!inf_count)throw std::runtime_error("No third-party INF packages were exported. The export and diagnostics have been retained");
            evidence["infCount"]=inf_count;evidence["fileCount"]=file_count;evidence["payloadBytes"]=bytes;evidence["status"]="archiving";write_json(report,evidence);protect_path(report);
            emit("Creating an archive of "+std::to_string(inf_count)+" driver packages…");
            path=directory;path+=L".zip";
            auto literal=[](const std::wstring& value){std::wstring result=L"'";for(auto c:value){result+=c;if(c==L'\'')result+=c;}return result+L"'";};
            auto script=L"$ErrorActionPreference='Stop'; Add-Type -AssemblyName System.IO.Compression; Add-Type -AssemblyName System.IO.Compression.FileSystem; $root="+literal(directory.wstring())+
                L"; $zip=[IO.Compression.ZipFile]::Open("+literal(path.wstring())+L",[IO.Compression.ZipArchiveMode]::Create); try { "
                L"foreach($file in Get-ChildItem -LiteralPath $root -Recurse -File) { if($file.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Export contains a reparse point' }; "
                L"$name=$file.FullName.Substring($root.Length+1).Replace('\\','/'); [IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip,$file.FullName,$name,[IO.Compression.CompressionLevel]::Fastest) | Out-Null } } finally { $zip.Dispose() }";
            run({system_directory()+L"\\WindowsPowerShell\\v1.0\\powershell.exe",L"-NoProfile",L"-NonInteractive",L"-Command",script},"archive-drivers");protect_path(path);
            evidence["archive"]=utf8(path.wstring());evidence["status"]="collected";evidence["finishedUtc"]=utc_now();write_json(report,evidence);protect_path(report);
            emit("Drivers and collection diagnostics saved. Uploading to the driver library…");
        }
        no_reparse(path);if(!std::filesystem::is_regular_file(path)||_wcsicmp(path.extension().c_str(),L".zip"))throw std::runtime_error("Choose a driver ZIP archive");
        const auto size=std::filesystem::file_size(path);if(!size)throw std::runtime_error("Driver ZIP must not be empty");
        Handle archive(CreateFileW(path.c_str(),GENERIC_READ,FILE_SHARE_READ,nullptr,OPEN_EXISTING,0,nullptr));if(!archive)fail("Lock driver archive");
        Json spec={{"kind","drivers"},{"name","Drivers from "+hardware_summary().at("model").get<std::string>().substr(0,107)},{"filename","drivers.zip"},{"size",size},{"sha256",sha256(path)}};
        Json uploaded;unsigned long long offset=0;
        if(state.contains("pendingDriverUpload")){
            const auto& pending=state.at("pendingDriverUpload");
            if(pending.at("sha256")!=spec.at("sha256")||pending.at("size")!=size)throw std::runtime_error("The saved upload is for a different archive. Retry the original archive or cancel it on the website");
            uploaded=pxe_api(connection.origin,connection.token,connection.path+"/driver-uploads/"+pending.at("id").get<std::string>());offset=driver_upload_offset(pending,uploaded);
            emit("Resuming the saved upload at byte "+std::to_string(offset)+" of "+std::to_string(size)+".");
        }else{
            uploaded=pxe_api(connection.origin,connection.token,connection.path+"/driver-uploads",&spec);
            state["pendingDriverUpload"]={{"id",uploaded.at("id")},{"sha256",spec.at("sha256")},{"size",size},{"path",utf8(path.wstring())},{"received",0},{"utc",utc_now()}};save();
        }
        const auto id=uploaded.at("id").get<std::string>();
        std::ifstream input(path,std::ios::binary);if(!input)throw std::runtime_error("Read driver archive");input.seekg(static_cast<std::streamoff>(offset));if(!input)throw std::runtime_error("Seek driver archive resume position");
        while(offset<size){std::string chunk(static_cast<size_t>(std::min(8ULL*MiB,size-offset)),'\0');input.read(chunk.data(),chunk.size());if(static_cast<size_t>(input.gcount())!=chunk.size())throw std::runtime_error("Driver archive changed during upload");
            auto ack=server_request(connection.origin,connection.token,connection.path+"/driver-uploads/"+id+"/content",L"PATCH",chunk,L"Upload-Offset: "+std::to_wstring(offset)+L"\r\n");
            offset+=chunk.size();if(ack.at("received")!=offset)throw std::runtime_error("Driver upload offset differs");state["pendingDriverUpload"]["received"]=offset;save();emit("Uploaded "+std::to_string(static_cast<unsigned int>(static_cast<long double>(offset)/size*100))+"%");}
        emit("Checking the archive on the server and tagging it to this hardware…");
        Json empty=Json::object();auto ready=pxe_api(connection.origin,connection.token,connection.path+"/driver-uploads/"+id+"/finish",&empty);
        emit("Upload validated and added to the driver library for this model. Local files and logs have been retained.");state["driverUploads"].push_back({{"id",id},{"sha256",spec.at("sha256")},{"utc",utc_now()}});state.erase("pendingDriverUpload");save();return ready;
        }catch(const std::exception& e){state["driverUploadError"]=e.what();save();throw;}
    }
    if(action!="install"&&action!="update")throw std::runtime_error("Unknown driver action");
    if(state.value("driverInstallationMayBeInProgress",false)||state.value("driverRebootRequired",false))throw std::runtime_error("Restart before installing more drivers");
    state["driverAutomaticAttempted"]=true;state.erase("error");save();bool changed=false;Json pass=Json::array();
    try{
        if(action=="install"){
            emit("Scanning devices before installing server drivers…");
            run({system_directory()+L"\\pnputil.exe",L"/scan-devices"},"console-before-driver-scan");
            std::set<std::string> seen;
            auto apply=[&](const Json& manifest,const std::filesystem::path& directory){
                validate_driver_manifest(manifest);if(!driver_package_matches(manifest,hardware_summary()))throw std::runtime_error("Driver hardware tag differs");
                std::vector<std::unique_ptr<Handle>> locks;
                for(const auto& f:manifest.at("files")){auto path=directory/wide(f.at("path"));verify_protected(path);locks.push_back(std::make_unique<Handle>(CreateFileW(path.c_str(),GENERIC_READ,FILE_SHARE_READ,nullptr,OPEN_EXISTING,0,nullptr)));
                    if(!*locks.back()||std::filesystem::file_size(path)!=f.at("size")||sha256(path)!=f.at("sha256").get<std::string>())throw std::runtime_error("Driver package integrity differs");}
                for(const auto& inf:manifest.at("infPaths")){
                    auto name=inf.get<std::string>();auto slash=name.rfind('/');auto prefix=slash==std::string::npos?std::string():name.substr(0,slash+1);Json grouped=Json::array();
                    for(auto f:manifest.at("files")){auto path=f.at("path").get<std::string>();if(path.starts_with(prefix)){f["path"]=path.substr(prefix.size());grouped.push_back(f);}}
                    auto folded=[](std::string text){std::transform(text.begin(),text.end(),text.begin(),[](unsigned char c){return std::tolower(c);});return text;};
                    std::sort(grouped.begin(),grouped.end(),[&](const Json& a,const Json& b){return folded(a.at("path"))<folded(b.at("path"));});
                    if(!seen.insert(Json{{"files",grouped},{"inf",name.substr(prefix.size())}}.dump()).second)continue;
                    stage("installing-drivers");emit("Installing driver "+std::to_string(pass.size()+1)+": "+name+"…");auto code=run({system_directory()+L"\\pnputil.exe",L"/add-driver",(directory/wide(name)).wstring(),L"/install"},"console-driver-install",true);
                    auto result=driver_attempt_result(code);result["packageId"]=manifest.at("packageId");result["inf"]=inf;result["log"]=state.at("tools").back().at("log");state["driverActions"].push_back(result);pass.push_back(result);state["driverPassSummary"]=driver_pass_summary(pass);changed=changed||result.at("changed").get<bool>();save();
                    if(code==259)emit("Skipped "+name+": no matching device, or Windows already uses a better/newer driver.");
                    else if(result.at("status")=="failed")emit("Failed "+name+" (exit "+std::to_string(code)+"). Diagnostics saved.");
                    if(result.value("installationMayBeInProgress",false)){state["driverInstallationMayBeInProgress"]=true;save();throw std::runtime_error("Driver installation state is uncertain; restart before continuing");}
                }
            };
            if(state.contains("driverPackage")){
                if(!state.value("driverStagedInstalled",false)){const auto first=pass.size();apply(state.at("driverPackage"),root/L"drivers");bool failed=false;for(size_t i=first;i<pass.size();++i)failed=failed||pass[i].at("status")=="failed";state["driverStagedInstalled"]=!failed;save();run({system_directory()+L"\\pnputil.exe",L"/scan-devices"},"console-staged-driver-scan");Sleep(5000);}
            }
            Json catalog;auto connection=driver_connection(installed);
            try{catalog=driver_catalog(installed);}catch(const std::exception& e){
                if(changed&&!state.value("driverStagedRecoveryReboot",false)){
                    state["driverStagedRecoveryReboot"]=true;state["driverAutomaticAttempted"]=false;state["driverRebootRequired"]=true;state["checkpointBootId"]=boot;state["error"]=e.what();stage("driver-reboot-pending");emit("Staged drivers installed. Restarting once before reconnecting to the server.");restart(root/(L"driver-restart-"+std::to_wstring(GetTickCount64())+L".log"));return state;
                }
                throw;
            }
            emit("Server has "+std::to_string(catalog.at("drivers").size())+" reviewed drivers for this model and Windows build.");
            std::vector<std::pair<Json,std::filesystem::path>> packages;
            auto cache=root/(L"library-"+std::to_wstring(GetTickCount64()));std::filesystem::create_directory(cache);protect_path(cache);auto blobs=cache/L"blobs";std::filesystem::create_directory(blobs);protect_path(blobs);
            for(const auto& entry:catalog.at("drivers")){
                auto manifest=entry.at("manifest");auto id=manifest.at("packageId").get<std::string>();auto directory=cache/wide(id);std::filesystem::create_directory(directory);protect_path(directory);emit("Downloading "+entry.at("name").get<std::string>()+"…");
                for(size_t i=0;i<manifest.at("files").size();++i){const auto& f=manifest.at("files").at(i);auto destination=directory/wide(f.at("path"));std::filesystem::create_directories(destination.parent_path());
                    auto blob=blobs/wide(f.at("sha256"));if(!std::filesystem::exists(blob)){pxe_download(connection.origin,connection.token,connection.path+"/drivers/"+id+"/files/"+std::to_string(i),blob,f.at("size"));protect_path(blob);}if(std::filesystem::file_size(blob)!=f.at("size"))throw std::runtime_error("Cached driver size differs");std::filesystem::create_hard_link(blob,destination);protect_path(destination);
                    if(sha256(destination)!=f.at("sha256").get<std::string>())throw std::runtime_error("Downloaded driver integrity differs");}
                packages.push_back({manifest,directory});
            }
            for(const auto& [manifest,directory]:packages)apply(manifest,directory);
            state["driverPassSummary"]=driver_pass_summary(pass);save();const auto& totals=state.at("driverPassSummary");
            emit("Server driver pass: "+std::to_string(totals.at("installed").get<unsigned>())+" installed, "+std::to_string(totals.at("skipped").get<unsigned>())+" skipped, "+std::to_string(totals.at("failed").get<unsigned>())+" failed.");
            if(totals.at("failed")>0)state["error"]="Some driver packages failed. Check devices and review the per-driver logs.";
            run({system_directory()+L"\\pnputil.exe",L"/scan-devices"},"console-after-driver-scan");Sleep(5000);
        }
        state["drivers"]=device_status();state["hardware"]=hardware_summary();
        if((action=="update"||state.contains("windowsUpdatePolicy"))&&!state.at("drivers").at("problems").empty()){
            if(!state.contains("windowsUpdatePolicy"))state["windowsUpdatePolicy"]={{"timeoutSeconds",900},{"maxAttempts",2}};
            if(!state.contains("windowsUpdateAttempts"))state["windowsUpdateAttempts"]=Json::array();
            auto& attempts=state["windowsUpdateAttempts"];const auto maximum=action=="update"?3:state.at("windowsUpdatePolicy").at("maxAttempts").get<int>();
            if(attempts.size()>=static_cast<size_t>(maximum))throw std::runtime_error("Windows Update attempt limit reached. Upload a known driver or review the saved diagnostics");
            emit("Checking Windows Update for drivers for unresolved devices only…");attempts.push_back({{"status","running"},{"startedUtc",utc_now()},{"unresolvedDevices",state.at("drivers").at("problems")}});stage("updating-drivers");
            auto index=attempts.size();auto timeout=state.at("windowsUpdatePolicy").at("timeoutSeconds").get<DWORD>();
            auto code=run_logged({installed.exe.wstring(),L"update-drivers"},root/(L"windows-update-"+std::to_wstring(index)+L".log"),"",(timeout+60)*1000);
            auto evidence=root/(L"windows-update-"+std::to_wstring(index)+L".json");Json result={{"status","failed"},{"error","Driver worker did not record a result"}};
            if(std::filesystem::exists(evidence)){verify_protected(evidence);if(std::filesystem::file_size(evidence)>256*1024)throw std::runtime_error("Driver update evidence exceeds bound");result=read_json(evidence);}
            if(code||result.value("status",std::string())=="running"){result["status"]=code==ERROR_TIMEOUT?"timed-out":"failed";result["installationMayBeInProgress"]=true;}
            attempts.back()["result"]=result;attempts.back()["status"]=result.value("status",std::string("failed"));attempts.back()["workerExitCode"]=code;attempts.back()["finishedUtc"]=utc_now();
            if(result.value("installationMayBeInProgress",false))state["driverInstallationMayBeInProgress"]=true;
            if(result.at("status")=="succeeded")changed=true;
            else state["error"]=result.value("error",std::string("No applicable driver was found for the remaining devices"));save();
        }
        run({system_directory()+L"\\pnputil.exe",L"/scan-devices"},"console-final-device-scan");Sleep(3000);state["drivers"]=device_status();
        state["driverRebootRequired"]=changed||state.value("driverInstallationMayBeInProgress",false);
        if(state.at("driverRebootRequired")==true){state["checkpointBootId"]=boot;stage("driver-reboot-pending");emit("Driver changes saved. Restarting automatically; the main menu will reopen afterward.");restart(root/(L"driver-restart-"+std::to_wstring(GetTickCount64())+L".log"));}
        else{stage("driver-menu");emit("Driver pass finished. Returned to the main menu.");}
        return state;
    }catch(const std::exception& e){
        const bool interrupted=state.at("stage")=="installing-drivers"||state.at("stage")=="updating-drivers";
        state["error"]=e.what();state["driverRebootRequired"]=changed||interrupted||state.value("driverInstallationMayBeInProgress",false);
        if(interrupted)state["driverInstallationMayBeInProgress"]=true;state["drivers"]=device_status();stage("driver-menu");emit("Stopped: "+std::string(e.what()));return state;
    }
}

}
