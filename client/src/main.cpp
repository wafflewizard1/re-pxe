#include <repxe/windows.hpp>
#include <repxe/windows_update.hpp>
#include <repxe/technician.hpp>
#include <windows.h>
#include <algorithm>
#include <iostream>
#include <map>
#include <stdexcept>

namespace {
const char* help=R"(re-pxe native AMD64 client 0.13.7

  repxe pxe --boot PATH
  repxe ui [--config PATH] [--audit-log PATH]
  repxe connect-share --path UNC --user USER
  repxe server-images --server HTTPS_ORIGIN
  repxe server-deploy --config PATH --server HTTPS_ORIGIN --image-id ID [--execute]
  repxe sync-job --queue PATH
  repxe deliver-report [--offline-windows PATH]
  repxe complete
  repxe inventory
  repxe inspect-image --image PATH [--index 1]
  repxe hash --file PATH
  repxe locate --path PATH
  repxe plan --config PATH
  repxe capture --config PATH [--execute]
  repxe deploy --config PATH [--execute]

Results are JSON on stdout. Diagnostics/tool progress are separate from plans.
inventory, inspect-image, hash, locate and plan never write disks or state files.
Capture requires WinPE, an offline Windows volume and a preparation declaration.
Deployment requires an explicit disk policy. format-selected-disk erases the selected disk; blank-raw refuses existing partitions or data.
)";
}
int wmain(int argc,wchar_t** argv){
    using namespace repxe;
    try{
        if(argc<2 || std::wstring(argv[1])==L"--help" || std::wstring(argv[1])==L"help"){std::cout<<help;return 0;}
        if(std::wstring(argv[1])==L"version"){std::cout<<"0.13.7\n";return 0;}
        std::map<std::wstring,std::wstring> options;
        for(int i=2;i<argc;++i){std::wstring key=argv[i];if(key==L"--execute"){if(options.contains(key))throw std::runtime_error("Duplicate option");options[key]=L"true";continue;}
            if(key.rfind(L"--",0)!=0 || i+1>=argc)throw std::runtime_error("Options require --name value");
            if(options.contains(key))throw std::runtime_error("Duplicate option");options[key]=argv[++i];}
        const auto command=std::wstring(argv[1]);
        std::map<std::wstring,std::vector<std::wstring>> accepted={{L"pxe",{L"--boot"}},{L"ui",{L"--config",L"--audit-log"}},{L"connect-share",{L"--path",L"--user"}},{L"server-images",{L"--server"}},{L"server-deploy",{L"--config",L"--server",L"--image-id",L"--execute"}},{L"sync-job",{L"--queue"}},{L"deliver-report",{L"--offline-windows"}},{L"update-drivers",{}},{L"complete",{}},{L"completion-status",{}},{L"driver-console",{}},{L"driver-action",{L"--name",L"--file"}},{L"inventory",{}},{L"inspect-image",{L"--image",L"--index"}},
            {L"hash",{L"--file"}},{L"locate",{L"--path"}},{L"plan",{L"--config"}},{L"capture",{L"--config",L"--execute"}},{L"deploy",{L"--config",L"--execute"}}};
        if(!accepted.contains(command))throw std::runtime_error("Unknown command");
        for(const auto& [key,value]:options)if(std::find(accepted[command].begin(),accepted[command].end(),key)==accepted[command].end())throw std::runtime_error("Unknown option: "+utf8(key));
        auto required=[&](const wchar_t* name)->std::filesystem::path{if(!options.contains(name) || options[name].empty())throw std::runtime_error("Missing option: "+utf8(name));return options[name];};
        Json result;
        if(command==L"pxe")return pxe_ui(required(L"--boot"));
        if(command==L"ui")return technician_ui(options.contains(L"--config")?required(L"--config"):std::filesystem::path{},options.contains(L"--audit-log")?required(L"--audit-log"):std::filesystem::path{});
        if(command==L"connect-share"){auto password=wide(prompt_secret(L"Share password: "));try{connect_share(required(L"--path").wstring(),required(L"--user").wstring(),password);}catch(...){SecureZeroMemory(password.data(),password.size()*sizeof(wchar_t));throw;}SecureZeroMemory(password.data(),password.size()*sizeof(wchar_t));result={{"connected",true},{"persistent",false}};}
        if(command==L"server-images"){auto token=prompt_secret(L"Deployment API credential: ");try{result=server_catalog(utf8(required(L"--server").wstring()),token);}catch(...){SecureZeroMemory(token.data(),token.size());throw;}SecureZeroMemory(token.data(),token.size());}
        if(command==L"sync-job"){auto token=prompt_secret(L"Deployment API credential: ");try{result=sync_server_job(required(L"--queue"),token);}catch(...){SecureZeroMemory(token.data(),token.size());throw;}SecureZeroMemory(token.data(),token.size());}
        if(command==L"server-deploy"){
            if(!inventory()["environment"].value("winpe",false))throw std::runtime_error("Server deployment requires WinPE");
            auto path=required(L"--config");auto config=read_json(path);auto token=prompt_secret(L"Deployment API credential: ");
            try{if(options.contains(L"--execute"))result=server_deploy(config,path,utf8(required(L"--server").wstring()),token,utf8(required(L"--image-id").wstring()));
                else{auto catalog=server_catalog(utf8(required(L"--server").wstring()),token);select_server_image(catalog,config,utf8(required(L"--image-id").wstring()));result=deploy_from_config(config,path,false);}}
            catch(...){SecureZeroMemory(token.data(),token.size());throw;}SecureZeroMemory(token.data(),token.size());
        }
        if(command==L"deliver-report")result=deliver_report(options.contains(L"--offline-windows")?required(L"--offline-windows"):std::filesystem::path{});
        if(command==L"completion-status")result=completion_status();
        if(command==L"driver-console")return driver_console();
        if(command==L"driver-action")result=driver_session_action(utf8(required(L"--name").wstring()),options.contains(L"--file")?required(L"--file"):std::filesystem::path{});
        if(command==L"complete"){if(completion_uses_console())return driver_console();result=complete_windows();}
        if(command==L"update-drivers")result=windows_update_worker();
        else if(command==L"inventory")result=inventory();
        if(command==L"inspect-image"){int index=1;if(options.contains(L"--index")){size_t parsed=0;index=std::stoi(options[L"--index"],&parsed);if(parsed!=options[L"--index"].size())throw std::runtime_error("Invalid image index");}result=inspect_wim(required(L"--image"),index);}
        if(command==L"hash"){auto file=required(L"--file");result={{"sha256",sha256(file)},{"file",utf8(file.wstring())}};}
        if(command==L"locate")result=locate_resource(required(L"--path"),"requested path");
        if(command==L"plan"){auto file=required(L"--config");result=plan_from_config(read_json(file),file);}
        if(command==L"capture"){auto file=required(L"--config");result=capture_from_config(read_json(file),file,options.contains(L"--execute"));}
        if(command==L"deploy"){auto file=required(L"--config");result=deploy_from_config(read_json(file),file,options.contains(L"--execute"));}
        std::cout<<result.dump(2)<<"\n";return 0;
    }catch(const std::exception& e){
        std::cerr<<repxe::Json{{"error",e.what()}}.dump()<<"\n";
        if(argc>1&&std::wstring(argv[1])==L"complete"){
            const auto message=repxe::wide(std::string("re-pxe could not resume setup:\n\n")+e.what()+"\n\nKeep this error for diagnosis. In Administrator Command Prompt, run:\nC:\\ProgramData\\re-pxe\\re-pxe.exe completion-status");
            MessageBoxW(nullptr,message.c_str(),L"re-pxe - Startup stopped",MB_OK|MB_ICONERROR|MB_SETFOREGROUND);
        }
        return 1;
    }
}
