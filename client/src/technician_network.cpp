#include <repxe/technician.hpp>
#include <windows.h>
#include <winnetwk.h>
#include <winhttp.h>
#include <objbase.h>
#include <array>
#include <atomic>
#include <cstdio>
#include <regex>
#include <iostream>
#include <memory>
#include <algorithm>
#include <cctype>

namespace repxe {
namespace {
void error(const char* action){throw std::runtime_error(std::string(action)+" (Windows error "+std::to_string(GetLastError())+")");}
struct Internet {
    HINTERNET h;
    explicit Internet(HINTERNET value):h(value){if(!h)error("Create HTTPS connection");}
    ~Internet(){WinHttpCloseHandle(h);}
    Internet(const Internet&)=delete;
};
void token_policy(const std::string& token){if(!std::regex_match(token,std::regex("[A-Za-z0-9_-]{32,256}")))throw std::runtime_error("Invalid server credential format");}
void CALLBACK tls_status(HINTERNET,DWORD_PTR context,DWORD status,LPVOID info,DWORD size){if(context&&status==WINHTTP_CALLBACK_STATUS_SECURE_FAILURE&&info&&size==sizeof(DWORD))reinterpret_cast<std::atomic<DWORD>*>(context)->store(*static_cast<DWORD*>(info));}
void request_error(const char* action,const std::string& path,DWORD code,DWORD flags){
    std::string detail;
    if(code==ERROR_WINHTTP_SECURE_FAILURE){SYSTEMTIME now{};GetSystemTime(&now);char utc[40]{};std::snprintf(utc,sizeof(utc),"%04u-%02u-%02u %02u:%02u:%02u UTC",now.wYear,now.wMonth,now.wDay,now.wHour,now.wMinute,now.wSecond);detail=": "+tls_failure_detail(flags)+". PC clock: "+utc;}
    throw std::runtime_error(std::string(action)+" (Windows error "+std::to_string(code)+")"+detail+"; API "+path);
}
Json request_api(const std::string& origin,const std::string& token,const std::string& path,const std::wstring& method,const std::string& payload,const std::wstring& extra_headers){
    validate_server_origin(origin);token_policy(token);
    if(path.empty() || path[0]!='/' || path.find_first_of("\r\n?#")!=std::string::npos)throw std::runtime_error("Invalid server API path");
    auto url=wide(origin);URL_COMPONENTS c{};c.dwStructSize=sizeof(c);c.dwHostNameLength=static_cast<DWORD>(-1);
    if(!WinHttpCrackUrl(url.c_str(),0,0,&c) || c.nScheme!=INTERNET_SCHEME_HTTPS)throw std::runtime_error("Invalid HTTPS origin");
    std::wstring host(c.lpszHostName,c.dwHostNameLength);
    Internet session(WinHttpOpen(L"re-pxe/0.13.6",WINHTTP_ACCESS_TYPE_NO_PROXY,WINHTTP_NO_PROXY_NAME,WINHTTP_NO_PROXY_BYPASS,0));
    if(!WinHttpSetTimeouts(session.h,5000,5000,10000,10000))error("Set HTTPS timeouts");
    Internet connection(WinHttpConnect(session.h,host.c_str(),c.nPort,0));
    std::atomic<DWORD> tls_flags{0};
    Internet request(WinHttpOpenRequest(connection.h,method.c_str(),wide(path).c_str(),nullptr,WINHTTP_NO_REFERER,WINHTTP_DEFAULT_ACCEPT_TYPES,WINHTTP_FLAG_SECURE));
    DWORD redirects=WINHTTP_OPTION_REDIRECT_POLICY_NEVER;if(!WinHttpSetOption(request.h,WINHTTP_OPTION_REDIRECT_POLICY,&redirects,sizeof(redirects)))error("Disable credential redirects");
    if(WinHttpSetStatusCallback(request.h,tls_status,WINHTTP_CALLBACK_FLAG_SECURE_FAILURE,0)==WINHTTP_INVALID_STATUS_CALLBACK)error("Register TLS diagnostics");
    auto headers=L"Content-Type: application/json\r\nAuthorization: Bearer "+wide(token)+L"\r\n"+extra_headers;
    if(!WinHttpSendRequest(request.h,headers.c_str(),headers.size(),payload.empty()?WINHTTP_NO_REQUEST_DATA:const_cast<char*>(payload.data()),payload.size(),payload.size(),reinterpret_cast<DWORD_PTR>(&tls_flags)) || !WinHttpReceiveResponse(request.h,nullptr))request_error("Request authenticated API",path,GetLastError(),tls_flags.load());
    DWORD status=0,size=sizeof(status);if(!WinHttpQueryHeaders(request.h,WINHTTP_QUERY_STATUS_CODE|WINHTTP_QUERY_FLAG_NUMBER,WINHTTP_HEADER_NAME_BY_INDEX,&status,&size,WINHTTP_NO_HEADER_INDEX))error("Read HTTPS status");
    std::string response;for(;;){std::array<char,4096> b{};DWORD count=0;if(!WinHttpReadData(request.h,b.data(),b.size(),&count))error("Read API response");if(!count)break;if(response.size()+count>1024*1024)throw std::runtime_error("API response exceeded 1 MiB");response.append(b.data(),count);}
    if(status!=200 && status!=201 && status!=204){
        std::string detail;
        try{auto body=Json::parse(response);if(body.contains("detail") && body.at("detail").is_string())detail=body.at("detail").get<std::string>().substr(0,500);}catch(...){}
        if(detail.find(token)!=std::string::npos)detail="Request rejected";
        std::replace_if(detail.begin(),detail.end(),[](unsigned char c){return c<32;},' ');
        throw std::runtime_error("Server API returned HTTP "+std::to_string(status)+(detail.empty()?"":": "+detail));
    }
    return response.empty()?Json::object():Json::parse(response);
}
Json api(const std::string& origin,const std::string& token,const std::string& path,const Json* body=nullptr){return request_api(origin,token,path,body?L"POST":L"GET",body?body->dump():"",L"");}
std::string uuid(){GUID g{};if(FAILED(CoCreateGuid(&g)))throw std::runtime_error("Create technician session ID");std::array<wchar_t,64>b{};StringFromGUID2(g,b.data(),b.size());auto s=utf8(b.data());s=s.substr(1,s.size()-2);std::transform(s.begin(),s.end(),s.begin(),[](unsigned char c){return std::tolower(c);});return s;}
const std::vector<std::string> stages={"preflight","disk-prepared","image-applied","boot-configured","drivers-staged","agent-staged","awaiting-windows"};
void save_queue(const std::filesystem::path& path,const Json& q){write_json(path,q);restrict_state_path(path);}
Json synchronize(const std::filesystem::path& path,const std::string& token){
    verify_state_path(path);auto q=read_json(path);validate_server_origin(q.at("server"));
    auto id=q.at("jobId").get<std::string>();if(!std::regex_match(id,std::regex("[a-f0-9-]{36}")) || q.at("schemaVersion")!=1 || !q.at("events").is_array())throw std::runtime_error("Invalid server event queue");
    auto ack=q.at("acknowledged").get<size_t>();if(ack>q.at("events").size())throw std::runtime_error("Invalid event acknowledgement offset");
    try{
        for(;ack<q.at("events").size();++ack){
            auto e=q.at("events").at(ack);auto result=api(q.at("server"),token,"/api/jobs/"+id+"/events",&e);
            if(result.at("id")!=id || result.at("stage")!=e.at("stage"))throw std::runtime_error("Server event acknowledgement differs");
            q["acknowledged"]=ack+1;q.erase("lastError");save_queue(path,q);
        }
        q["status"]="synchronized";
    }catch(const std::exception& e){q["status"]="pending";q["lastError"]=e.what();}
    save_queue(path,q);return q;
}
}

Json server_request(const std::string& origin,const std::string& token,const std::string& path,const std::wstring& method,const std::string& payload,const std::wstring& headers){return request_api(origin,token,path,method,payload,headers);}

Json pxe_api(const std::string& origin,const std::string& token,const std::string& path,const Json* body){return api(origin,token,path,body);}
void pxe_download(const std::string& origin,const std::string& token,const std::string& path,const std::filesystem::path& output,unsigned long long expected,const std::function<void(unsigned long long)>& progress){
    validate_server_origin(origin);token_policy(token);
    if(!std::regex_match(path,std::regex("/api/(pxe/(image|driver/[0-9]+)|client-jobs/[a-f0-9-]{36}/(drivers/[a-f0-9-]{36}/files/[0-9]+|customer-report/(re-pxe-report|erasure-drive-[0-9]{1,2})\\.pdf))")) || !expected || expected>100ULL*GiB)throw std::runtime_error("Invalid PXE download");
    auto url=wide(origin);URL_COMPONENTS c{};c.dwStructSize=sizeof(c);c.dwHostNameLength=static_cast<DWORD>(-1);
    if(!WinHttpCrackUrl(url.c_str(),0,0,&c) || c.nScheme!=INTERNET_SCHEME_HTTPS)throw std::runtime_error("Invalid download origin");
    std::wstring host(c.lpszHostName,c.dwHostNameLength);
    Internet session(WinHttpOpen(L"re-pxe-PXE",WINHTTP_ACCESS_TYPE_NO_PROXY,WINHTTP_NO_PROXY_NAME,WINHTTP_NO_PROXY_BYPASS,0));
    WinHttpSetTimeouts(session.h,5000,5000,10000,60000);
    Internet connection(WinHttpConnect(session.h,host.c_str(),c.nPort,0));
    Internet request(WinHttpOpenRequest(connection.h,L"GET",wide(path).c_str(),nullptr,WINHTTP_NO_REFERER,WINHTTP_DEFAULT_ACCEPT_TYPES,WINHTTP_FLAG_SECURE));
    DWORD redirects=WINHTTP_OPTION_REDIRECT_POLICY_NEVER;if(!WinHttpSetOption(request.h,WINHTTP_OPTION_REDIRECT_POLICY,&redirects,sizeof(redirects)))error("Disable download redirects");
    auto headers=L"Authorization: Bearer "+wide(token)+L"\r\n";
    if(!WinHttpSendRequest(request.h,headers.c_str(),headers.size(),WINHTTP_NO_REQUEST_DATA,0,0,0) || !WinHttpReceiveResponse(request.h,nullptr))error("Download approved PXE file");
    DWORD status=0,size=sizeof(status);if(!WinHttpQueryHeaders(request.h,WINHTTP_QUERY_STATUS_CODE|WINHTTP_QUERY_FLAG_NUMBER,WINHTTP_HEADER_NAME_BY_INDEX,&status,&size,WINHTTP_NO_HEADER_INDEX) || status!=200)throw std::runtime_error("PXE download refused (HTTP "+std::to_string(status)+")");
    std::array<wchar_t,128> length{};size=sizeof(length);if(!WinHttpQueryHeaders(request.h,WINHTTP_QUERY_CONTENT_LENGTH,WINHTTP_HEADER_NAME_BY_INDEX,length.data(),&size,WINHTTP_NO_HEADER_INDEX) || std::stoull(length.data())!=expected)throw std::runtime_error("PXE file size differs from approved metadata");
    auto file=CreateFileW(output.c_str(),GENERIC_WRITE,0,nullptr,CREATE_NEW,FILE_ATTRIBUTE_NORMAL|FILE_FLAG_SEQUENTIAL_SCAN,nullptr);if(file==INVALID_HANDLE_VALUE)error("Create PXE staging file");
    struct File {HANDLE h;~File(){CloseHandle(h);}} guard{file};std::vector<char> buffer(1024*1024);unsigned long long received=0;
    for(;;){DWORD count=0,written=0;if(!WinHttpReadData(request.h,buffer.data(),buffer.size(),&count))error("Read PXE file");if(!count)break;
        if(count>expected-received)throw std::runtime_error("PXE file exceeds approved size");
        if(!WriteFile(file,buffer.data(),count,&written,nullptr) || written!=count)error("Save PXE file");received+=count;if(progress)progress(received);
    }
    if(received!=expected || !FlushFileBuffers(file))throw std::runtime_error("PXE download incomplete");
}

std::string prompt_secret(const wchar_t* prompt){
    std::wcerr<<prompt;auto input=GetStdHandle(STD_INPUT_HANDLE);DWORD mode=0;bool console=GetConsoleMode(input,&mode);if(console)SetConsoleMode(input,mode&~ENABLE_ECHO_INPUT);
    std::string value;std::getline(std::cin,value);if(console){SetConsoleMode(input,mode);std::wcerr<<L"\n";}
    if(!value.empty() && value.back()=='\r')value.pop_back();return value;
}
void connect_share(const std::wstring& share,const std::wstring& user,const std::wstring& password){
    if(!std::regex_match(utf8(share),std::regex(R"(\\\\[A-Za-z0-9.-]+\\[A-Za-z0-9_$.-]+)")) || user.empty() || password.empty())throw std::runtime_error("Enter a UNC share, explicit user and password");
    NETRESOURCEW resource{};resource.dwType=RESOURCETYPE_DISK;resource.lpRemoteName=const_cast<wchar_t*>(share.c_str());
    auto code=WNetAddConnection2W(&resource,password.c_str(),user.c_str(),CONNECT_TEMPORARY);
    if(code)throw std::runtime_error("Share authentication failed (Windows error "+std::to_string(code)+"). Existing connections are preserved.");
}
Json server_catalog(const std::string& origin,const std::string& token){
    auto session=api(origin,token,"/api/session");if(session.at("role")!="deploy" && session.at("role")!="admin")throw std::runtime_error("A deployment server credential is required");
    return api(origin,token,"/api/images");
}
Json sync_server_job(const std::filesystem::path& queue,const std::string& token){return synchronize(queue,token);}

Json server_deploy(const Json& original,const std::filesystem::path& original_path,const std::string& origin,const std::string& token,const std::string& image_id,const Progress& progress){
    validate_server_origin(origin);auto env=inventory();if(!env["environment"].value("winpe",false))throw std::runtime_error("Server deployment requires WinPE");
    validate_config(original);if(original.contains("reportDelivery"))throw std::runtime_error("Connected deployment requires a configuration without a preassigned report credential");
    auto catalog=server_catalog(origin,token);auto image=select_server_image(catalog,original,image_id);
    // Check image, target and required resource mappings before any server job or state directory.
    auto plan=deploy_from_config(original,original_path,false);
    auto base=std::filesystem::path(wide(original.at("jobDirectory")));auto r=locate_resource(base,"server session state");
    if(!r.value("resolved",false) || r.value("type","")!="local")throw std::runtime_error("Server deployment requires local NTFS job storage outside the target");
    std::array<wchar_t,64> filesystem{};
    if(!GetVolumeInformationW(wide(r.at("volume")).c_str(),nullptr,0,nullptr,nullptr,nullptr,filesystem.data(),filesystem.size()) || _wcsicmp(filesystem.data(),L"NTFS"))throw std::runtime_error("Server deployment job storage must be NTFS");
    auto root=base/(L"session-"+wide(uuid()));std::filesystem::create_directory(root);restrict_state_path(root);
    auto config=original;Json cache;
    if(locate_resource(wide(original.at("image")),"image").value("type","")=="network"){
        const auto source=std::filesystem::path(wide(original.at("image")));auto bytes=std::filesystem::file_size(source);
        ULARGE_INTEGER available{};if(!GetDiskFreeSpaceExW(wide(r.at("volume")).c_str(),&available,nullptr,nullptr))error("Check image cache capacity");
        auto scratch=locate_resource(wide(original.at("scratchDirectory")),"scratch");
        auto reservation=bytes+GiB;if(scratch.at("volume")==r.at("volume"))reservation+=plan.at("image").at("totalBytes").get<unsigned long long>()+GiB;
        if(available.QuadPart<reservation)throw std::runtime_error("Job storage lacks space for the network image cache and deployment scratch");
        if(progress)progress({{"stage","caching-image"}});
        auto pending=root/L"image.wim.incomplete",cached=root/L"image.wim";
        if(!CopyFileW(source.c_str(),pending.c_str(),TRUE))error("Copy approved network image to protected cache");
        restrict_state_path(pending);auto hash=sha256(pending);
        if(hash!=original.at("sha256").get<std::string>() || std::filesystem::file_size(pending)!=bytes)throw std::runtime_error("Cached network image hash/size differs from approved source");
        std::filesystem::rename(pending,cached);config["image"]=utf8(cached.wstring());
        cache={{"source",original.at("image")},{"path",utf8(cached.wstring())},{"sha256",hash},{"sizeBytes",bytes},{"verified",true}};
        write_json(root/L"image-cache.json",cache);restrict_state_path(root/L"image-cache.json");
        if(progress)progress({{"stage","image-cache-verified"}});
    }
    Json assign={{"image_id",image_id},{"target_identity",original.at("targetIdentity")},{"device_model",plan.at("target").value("model","")}};
    auto job=api(origin,token,"/api/jobs",&assign);auto id=job.at("id").get<std::string>();
    Json lifetime={{"lifetime_hours",24}};Json cap;
    try{cap=api(origin,token,"/api/jobs/"+id+"/report-credential",&lifetime);}
    catch(...){Json cancel={{"stage","cancelled"},{"detail","Report credential setup failed"},{"event_id",uuid()}};try{api(origin,token,"/api/jobs/"+id+"/events",&cancel);}catch(...){}throw;}
    if(cap.at("jobId")!=id || cap.at("path")!="/api/client-jobs/"+id+"/report")throw std::runtime_error("Report credential job binding differs");
    auto secret=root/L"report-credential.json",config_file=root/L"deployment.json",queue=root/L"server-events.json";
    write_json(secret,{{"schemaVersion",1},{"endpoint",origin+cap.at("path").get<std::string>()},{"token",cap.at("token")}});restrict_state_path(secret);
    config["reportDelivery"]=utf8(secret.wstring());write_json(config_file,config);restrict_state_path(config_file);
    Json outbox={{"schemaVersion",1},{"server",origin},{"jobId",id},{"imageId",image_id},{"configuration",utf8(config_file.wstring())},{"events",Json::array()},{"acknowledged",0},{"status","pending"}};save_queue(queue,outbox);
    auto observe=[&](const Json& state){
        auto stage=state.at("stage").get<std::string>();
        if(std::find(stages.begin(),stages.end(),stage)!=stages.end() || stage=="failed"){
            outbox["events"].push_back({{"stage",stage},{"detail","Native offline deployment checkpoint"},{"event_id",uuid()}});outbox["status"]="pending";save_queue(queue,outbox);
        }
        if(progress)progress(state);
    };
    Json result;
    try{result=deploy_from_config(config,config_file,true,observe);}
    catch(...){
        if(outbox["events"].empty() || outbox["events"].back()["stage"]!="failed"){
            outbox["events"].push_back({{"stage","failed"},{"detail","Native deployment preflight or execution refused"},{"event_id",uuid()}});save_queue(queue,outbox);
        }
        synchronize(queue,token);throw;
    }
    if(!cache.is_null())result["imageCache"]=cache;
    auto synchronized=synchronize(queue,token);result["serverJob"]={{"id",id},{"eventQueue",utf8(queue.wstring())},{"eventDelivery",synchronized.at("status")}};
    return result;
}
}
