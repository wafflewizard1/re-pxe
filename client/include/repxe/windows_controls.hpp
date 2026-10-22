#pragma once
#include <repxe/core.hpp>
#include <algorithm>
#include <regex>
#include <stdexcept>

namespace repxe {
inline void validate_windows_command(const Json& command,const std::string& job_path){
    const std::regex uuid("[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}");
    const auto id=command.at("id").get<std::string>(),job=command.at("job_id").get<std::string>();
    if(!std::regex_match(id,uuid)||!std::regex_match(job,uuid)||job_path!="/api/client-jobs/"+job)
        throw std::runtime_error("Windows command belongs to another installation");
    const auto action=command.at("action").get<std::string>();
    if(action!="restart"&&action!="shutdown"&&action!="file")throw std::runtime_error("Unknown Windows command");
    if(action!="file")return;
    const auto name=command.at("filename").get<std::string>();
    if(name.empty()||name.size()>800||name=="."||name==".."||name.back()=='.'||name.back()==' '
       ||std::any_of(name.begin(),name.end(),[](unsigned char c){return c<32||c==127||std::string("<>:\"/\\|?*").find(c)!=std::string::npos;})
       ||std::regex_match(name,std::regex("(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(\\..*)?",std::regex::icase)))
        throw std::runtime_error("Invalid Windows desktop filename");
    const auto& size=command.at("size");
    if(!size.is_number_integer()||size.get<long long>()<=0||size.get<unsigned long long>()>100ULL*GiB
       ||!std::regex_match(command.at("sha256").get<std::string>(),std::regex("[a-f0-9]{64}"))
       ||command.at("download_path")!=job_path+"/windows/files/"+id)
        throw std::runtime_error("Windows transfer metadata differs");
}

inline bool prepare_windows_receipt(Json& receipts,const Json& command,const std::string& utc){
    const auto id=command.at("id").get<std::string>();
    Json binding={{"job_id",command.at("job_id")},{"action",command.at("action")}};
    if(command.at("action")=="file")for(const auto* key:{"filename","size","sha256","download_path"})binding[key]=command.at(key);
    if(!receipts.contains(id)){receipts[id]={{"binding",binding},{"started",utc}};return true;}
    auto& receipt=receipts.at(id);
    if(receipt.at("binding")!=binding)throw std::runtime_error("Windows command receipt differs");
    if(!receipt.contains("result"))receipt["result"]={{"status","failed"},{"path",""},{"error","Command interrupted; inspect the client before retrying"}};
    return false;
}
}
