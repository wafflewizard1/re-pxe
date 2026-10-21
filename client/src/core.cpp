#include <repxe/core.hpp>
#include <pugixml.hpp>
#include <algorithm>
#include <limits>
#include <set>
#include <regex>
#include <cctype>
#include <stdexcept>
#ifdef _WIN32
#include <windows.h>
#endif

namespace repxe {
namespace {
void require(bool condition, const std::string& message) {
    if (!condition) throw std::runtime_error(message);
}
bool digest_valid(const std::string& hash) {
    return hash.size() == 64 && std::all_of(hash.begin(), hash.end(), [](char c) {
        return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
    });
}
size_t utf16_length(const std::string& text) {
    // JSON validates UTF-8 before these paths reach Windows' wide APIs.
    try { (void)Json(text).dump(); }
    catch (const Json::exception&) { throw std::runtime_error("Driver file path contains invalid UTF-8"); }
    size_t count=0;
    for(unsigned char c:text)if((c&0xc0)!=0x80)count+=c>=0xf0?2:1;
    return count;
}
std::string driver_path_key(const std::string& text) {
#ifdef _WIN32
    const auto value=pugi::as_wide(text);
    const auto length=LCMapStringEx(LOCALE_NAME_INVARIANT,LCMAP_UPPERCASE,value.data(),static_cast<int>(value.size()),nullptr,0,nullptr,nullptr,0);
    require(length>0,"Cannot normalize Windows driver path");
    std::wstring key(static_cast<size_t>(length),L'\0');
    require(LCMapStringEx(LOCALE_NAME_INVARIANT,LCMAP_UPPERCASE,value.data(),static_cast<int>(value.size()),key.data(),length,nullptr,nullptr,0)>0,"Cannot normalize Windows driver path");
    return pugi::as_utf8(key);
#else
    auto key=text;std::transform(key.begin(),key.end(),key.begin(),[](unsigned char c){return std::toupper(c);});return key;
#endif
}
bool contains_disk(const Json& resource, int disk) {
    if (!resource.contains("diskNumbers") || !resource["diskNumbers"].is_array()) return false;
    for (const auto& value : resource["diskNumbers"]) if (value.get<int>() == disk) return true;
    return false;
}
int integer_field(const Json& config, const char* key, int fallback) {
    if (!config.contains(key)) return fallback;
    const auto& value=config.at(key);
    require(value.is_number_integer(), std::string(key)+" must be an integer");
    require(value >= std::numeric_limits<int>::min() && value <= std::numeric_limits<int>::max(),
            std::string(key)+" exceeds the supported integer range");
    return value.get<int>();
}
void validate_resources(const Json& resources, int excluded_disk) {
    require(resources.is_array() && !resources.empty(), "Resource locations were not resolved");
    for (const auto& resource : resources) {
        const auto role = resource.at("role").get<std::string>();
        require(resource.value("resolved", false), "Cannot resolve disk location for " + role);
        require(!contains_disk(resource, excluded_disk), "Target disk contains required " + role);
    }
}
}

bool validate_completion_bindings(const Json& state,const std::string& installed_hash,const std::string& running_hash,const std::string& kernel_hash,bool trusted_kernel) {
    require(state.at("schemaVersion")==1,"Unsupported completion state schema");
    const auto client=state.at("clientSha256").get<std::string>(),kernel=state.at("kernelSha256").get<std::string>();
    const std::regex hash("[0-9a-f]{64}");
    require(std::regex_match(client,hash)&&std::regex_match(kernel,hash)&&std::regex_match(kernel_hash,hash),"Invalid completion binding hash");
    require(installed_hash==client,"Installed re-pxe executable differs from its protected binding; use the installed-client updater");
    require(running_hash==client,"Running re-pxe executable differs from its protected binding");
    const bool changed=kernel_hash!=kernel;
    require(!changed||trusted_kernel,"Windows kernel changed and its Microsoft signature could not be verified");
    return changed;
}

std::string completion_action(const std::string& stage,bool new_boot,bool driver_problems) {
    if(stage=="awaiting-windows")return "checkpoint-reboot";
    if(stage=="reboot-pending") {
        if(!new_boot)return "await-reboot";
        return driver_problems?"needs-operator":"seal-oobe";
    }
    if(stage=="complete" || stage=="needs-operator" || stage=="failed")return "stop";
    throw std::runtime_error("Completion stage cannot be retried automatically");
}

std::string driver_fallback_action(bool problems,int attempts,int maximum,const std::string& last_status){
    require(maximum>=1 && maximum<=3 && attempts>=0 && attempts<=maximum,"Invalid Windows Update attempt bound");
    if(!problems)return "seal-oobe";
    if(attempts>=maximum || (!last_status.empty() && last_status!="succeeded"))return "needs-operator";
    return "windows-update";
}

Json driver_install_result(int code){
    // PnPUtil /add-driver <one INF> /install: 259 means no matching device
    // or Windows already uses a better/newer driver; it is a completed skip.
    require(code==0 || code==259 || code==3010,"Driver installation failed with exit code "+std::to_string(code));
    return {{"status",code==259?"skipped-no-match-or-better-driver":"installed"},
            {"exitCode",code},{"changed",code!=259},{"rebootRequired",code==3010}};
}

Json driver_attempt_result(int code){
    if(code==0||code==259||code==3010)return driver_install_result(code);
    return {{"status","failed"},{"exitCode",code},{"changed",false},{"rebootRequired",false},
            {"installationMayBeInProgress",code==1460||code==1641},
            {"error","Driver installation failed with exit code "+std::to_string(code)}};
}

Json driver_pass_summary(const Json& results){
    require(results.is_array(),"Driver results must be a list");
    unsigned installed=0,skipped=0,failed=0;bool reboot=false,uncertain=false;
    for(const auto& result:results){
        const auto status=result.at("status").get<std::string>();
        if(status=="installed")++installed;
        else if(status=="skipped-no-match-or-better-driver")++skipped;
        else if(status=="failed")++failed;
        else throw std::runtime_error("Unknown driver result status");
        reboot=reboot||result.value("rebootRequired",false);
        uncertain=uncertain||result.value("installationMayBeInProgress",false);
    }
    return {{"attempted",results.size()},{"installed",installed},{"skipped",skipped},{"failed",failed},
            {"rebootRequired",reboot},{"installationMayBeInProgress",uncertain}};
}

void validate_driver_export(const Json& hardware,const Json& devices){
    for(const auto* key:{"manufacturer","model","build"})
        require(hardware.contains(key)&&hardware.at(key).is_string()&&!hardware.at(key).get<std::string>().empty(),"Driver export requires complete hardware/build identity");
    require(devices.at("presentDeviceCount").is_number_integer()&&devices.at("presentDeviceCount")>0,"Driver export requires a successful device inventory");
    require(devices.at("problems").is_array(),"Driver export requires device problem inventory");
}

bool driver_finish_available(const Json& state){
    const auto stage=state.value("stage",std::string());
    return (stage=="complete" && (state.value("deliveryStatus",std::string())=="pending" || (completion_report_accepted(state)&&!completion_shutdown_ready(state)))) || stage=="driver-menu"||stage=="driver-reboot-pending"||stage=="awaiting-windows"||stage=="needs-operator"||stage=="failed"||stage=="ready-for-oobe"||stage=="sealing-oobe"||stage=="completing";
}

bool completion_report_accepted(const Json& state){
    const auto delivery=state.value("deliveryStatus",std::string());
    return state.value("stage",std::string())=="complete" && (delivery=="delivered" || delivery=="local-only");
}

bool completion_shutdown_ready(const Json& state){
    return completion_report_accepted(state) && (!state.value("finishOptions",Json::object()).value("cleanup_client",false) || state.value("cleanupScheduled",false));
}

bool completion_report_terminal(const Json& state){
    if(!state.value("driverConsole",false))return true;
    return state.value("stage",std::string())=="complete";
}

std::string driver_finish_action(const Json& state,const std::string& boot){
    require(driver_finish_available(state),"Wait for the active operation before finishing setup");
    if(state.value("stage",std::string())=="complete")return "retry-report";
    require(!boot.empty(),"Finish setup requires a boot identity");
    if(state.value("driverRebootRequired",false)||state.value("driverInstallationMayBeInProgress",false)||state.value("driverVerifiedBoot",std::string())!=boot)return "checkpoint-reboot";
    return "seal-oobe";
}

std::string completion_seal_action(const std::string& image_state){
    if(image_state=="IMAGE_STATE_SPECIALIZE_RESEAL_TO_OOBE")return "finalize-oobe";
    require(image_state=="IMAGE_STATE_SPECIALIZE_RESEAL_TO_AUDIT"||image_state=="IMAGE_STATE_GENERALIZE_RESEAL_TO_AUDIT","Finish requires the provisioned Audit installation or its verified OOBE checkpoint");
    return "seal-oobe";
}

bool driver_update_matches(const std::string& hardware_id,const Json& problems){
    if(hardware_id.empty())return false;
    auto fold=[](std::string s){std::transform(s.begin(),s.end(),s.begin(),[](unsigned char c){return std::tolower(c);});return s;};
    for(const auto& device:problems)for(const auto* key:{"hardwareIds","compatibleIds"})
        for(const auto& id:device.value(key,Json::array()))if(fold(id.get<std::string>())==fold(hardware_id))return true;
    return false;
}

unsigned long long driver_upload_offset(const Json& expected,const Json& remote){
    require(remote.at("id")==expected.at("id")&&remote.at("spec").at("kind")=="drivers"&&
            remote.at("spec").at("sha256")==expected.at("sha256")&&remote.at("spec").at("size")==expected.at("size"),"Saved driver upload differs from the server archive binding");
    const auto status=remote.at("status").get<std::string>();
    require(status=="receiving"||status=="ready"||status=="published","Saved upload cannot resume; review its status on the website");
    require(remote.at("received").is_number_integer()&&remote.at("received")>=0&&remote.at("received")<=expected.at("size"),"Server driver upload offset is invalid");
    if(status!="receiving")require(remote.at("received")==expected.at("size"),"Completed driver upload is truncated");
    return remote.at("received").get<unsigned long long>();
}

void validate_driver_manifest(const Json& m) {
    require(m.is_object() && m.at("schemaVersion")==1,"Unsupported driver manifest");
    const std::set<std::string> keys{"schemaVersion","packageId","architecture","manufacturer","model","windowsBuild","review","files","infPaths","bootCriticalInfPaths"};
    for(auto it=m.begin();it!=m.end();++it)require(keys.contains(it.key()),"Unknown driver manifest field");
    for(const auto* key:{"packageId","manufacturer","model","windowsBuild"})require(m.at(key).is_string() && !m.at(key).get<std::string>().empty() && m.at(key).get<std::string>().size()<=200,"Missing driver package identity");
    require(m.at("architecture")=="amd64","Only AMD64 driver packages are supported");
    require(m.at("review").at("hardwareTestPassed")==true && m.at("review").at("note").is_string() && m.at("review").at("note").get<std::string>().size()>=10,"Driver package requires a hardware review declaration");
    const auto& files=m.at("files");require(files.is_array() && !files.empty() && files.size()<=10000,"Driver package file count exceeds policy");
    std::set<std::string> paths;
    auto normalized=[](const std::string& path){return driver_path_key(path);};
    for(const auto& f:files){
        require(f.is_object() && f.size()==3,"Driver file must declare path, size and SHA-256");
        auto path=f.at("path").get<std::string>();
        require(!path.empty()&&utf16_length(path)<=32700&&std::none_of(path.begin(),path.end(),[](unsigned char c){return c<32||std::string_view("\\:<>\"|?*").find(static_cast<char>(c))!=std::string_view::npos;}),"Unsafe driver file path: "+Json(path).dump());
        size_t begin=0;
        while(begin<=path.size()){auto end=path.find('/',begin);if(end==std::string::npos)end=path.size();auto part=normalized(path.substr(begin,end-begin));auto stem=part.substr(0,part.find('.'));
            require(!part.empty() && part!="." && part!=".." && part.back()!='.' && part.back()!=' '&&utf16_length(part)<=255,"Unsafe driver path component: "+Json(path).dump());
            while(!stem.empty()&&stem.back()==' ')stem.pop_back();
            require(stem!="CON" && stem!="PRN" && stem!="AUX" && stem!="NUL" && !std::regex_match(stem,std::regex("(COM|LPT)([1-9]|\xc2\xb9|\xc2\xb2|\xc2\xb3)")),"Reserved Windows driver path: "+Json(path).dump());
            if(end==path.size())break;
            begin=end+1;
        }
        require(paths.insert(normalized(path)).second,"Case-insensitive duplicate driver path: "+Json(path).dump());
        require(f.at("size").is_number_integer() && f.at("size")>0,"Driver file size must be positive");
        require(digest_valid(f.at("sha256").get<std::string>()),"Invalid driver file digest");
    }
    const auto& infs=m.at("infPaths");
    require(infs.is_array() && !infs.empty(),"Explicit INF selection required");
    require(infs.size()<=files.size(),"Selected INF count exceeds driver package file count");
    std::set<std::string> selected;
    for(const auto& value:infs){auto path=normalized(value.get<std::string>());require(path.ends_with(".INF") && paths.contains(path) && selected.insert(path).second,"Selected INF is missing or duplicated");}
    if(m.contains("bootCriticalInfPaths")){
        const auto& critical=m.at("bootCriticalInfPaths");require(critical.is_array() && critical.size()<=256,"Invalid boot-critical INF selection");
        std::set<std::string> seen;
        for(const auto& inf:critical){auto path=normalized(inf.get<std::string>());require(selected.contains(path)&&seen.insert(path).second,"Boot-critical INF must be a unique selected package INF");}
    }
}

Json offline_driver_plan(const Json& config,const Json& manifest){
    const auto mode=config.value("offlineDriverMode",std::string("boot-critical"));
    require(mode=="boot-critical"||mode=="all-reviewed","Unsupported offline driver mode");
    if(manifest.is_null()){
        require(!config.contains("offlineDriverInfPaths"),"Offline selection requires a reviewed package");
        return Json::array();
    }
    validate_driver_manifest(manifest);
    auto fold=[](std::string s){std::transform(s.begin(),s.end(),s.begin(),[](unsigned char c){return std::tolower(c);});return s;};
    std::set<std::string> critical,selected;
    for(const auto& inf:manifest.value("bootCriticalInfPaths",Json::array()))critical.insert(fold(inf.get<std::string>()));
    for(const auto& inf:config.value("offlineDriverInfPaths",Json::array()))critical.insert(fold(inf.get<std::string>()));
    for(const auto& inf:manifest.at("infPaths"))selected.insert(fold(inf.get<std::string>()));
    for(const auto& inf:critical)require(selected.contains(inf),"Offline driver is outside the reviewed package");
    Json result=Json::array();
    for(bool required:{true,false})for(const auto& inf:manifest.at("infPaths")){
        const bool is_critical=critical.contains(fold(inf.get<std::string>()));
        if(is_critical==required&&(is_critical||mode=="all-reviewed"))result.push_back({{"inf",inf},{"required",is_critical}});
    }
    return result;
}

bool driver_package_matches(const Json& manifest,const Json& hardware){
    validate_driver_manifest(manifest);
    auto folded=[](std::string text){std::transform(text.begin(),text.end(),text.begin(),[](unsigned char c){return std::tolower(c);});return text;};
    return folded(manifest.at("manufacturer"))==folded(hardware.at("manufacturer")) && folded(manifest.at("model"))==folded(hardware.at("model")) && manifest.at("windowsBuild")==hardware.at("build");
}

void validate_server_origin(const std::string& origin){
    require(origin.size()<=260 && std::regex_match(origin,std::regex("https://[A-Za-z0-9.-]+(:[0-9]{1,5})?")),"Server must be an HTTPS origin without a path or credentials");
    auto colon=origin.find(':',8);if(colon!=std::string::npos){auto port=std::stoi(origin.substr(colon+1));require(port>=1 && port<=65535,"Invalid HTTPS server port");}
}
Json select_server_image(const Json& catalog,const Json& config,const std::string& id){
    require(catalog.is_array(),"Server image catalog must be an array");
    require(std::regex_match(id,std::regex("[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}")),"Invalid approved image ID");
    Json selected;for(const auto& image:catalog)if(image.at("id")==id){require(selected.is_null(),"Approved image ID is ambiguous");selected=image;}
    require(!selected.is_null(),"Approved image is not in the server catalog");
    require(selected.at("architecture")=="amd64" && selected.at("sha256")==config.at("sha256") && selected.at("image_index")==config.value("imageIndex",1),"Configuration does not match selected approved image hash/index/architecture");
    return selected;
}
bool confirmation_matches(const std::string& expected,const std::string& entered){return !expected.empty() && expected==entered;}

Json pxe_disks(const Json& inventory){
    Json result=Json::array();
    for(const auto& disk:inventory.at("disks"))
        if(!disk.value("removable",true)&&disk.value("stableIdentity",false)&&disk.value("writable",false))result.push_back(disk);
    if(result.empty())throw std::runtime_error("No eligible target disk");
    return result;
}

void validate_pxe_automation(const Json& policy,const Json& disks){
    if(!policy.at("managedSelection").is_boolean()||!policy.at("managedSelection").get<bool>()||
       !policy.at("automaticDiskSelection").is_boolean()||!policy.at("startDelaySeconds").is_number_integer())
        throw std::runtime_error("Invalid server installation policy");
    auto delay=policy.at("startDelaySeconds").get<long long>();
    if(delay<0||delay>300)throw std::runtime_error("Installation countdown is outside the supported range");
    if(!policy.at("profileId").is_string()||!std::regex_match(policy.at("profileId").get<std::string>(),std::regex("[a-f0-9-]{36}")))
        throw std::runtime_error("No server configuration matches this client");
    if(!policy.contains("targetDiskPolicy") ||
       (policy.at("targetDiskPolicy")!="format-selected-disk" && policy.at("targetDiskPolicy")!="blank-raw"))
        throw std::runtime_error("Invalid server disk policy");
    if(disks.empty() || !policy.at("targetIdentity").is_string()) throw std::runtime_error("Invalid target disk policy");
    if(policy.value("manualDiskSelection",false) && !policy.at("automaticDiskSelection").get<bool>() && policy.at("targetIdentity")=="") return;
    auto target=policy.at("targetIdentity");size_t matches=0;
    for(const auto& disk:disks)if(disk.at("identity")==target)++matches;
    if(matches!=1)throw std::runtime_error("Server installation policy names a missing or ambiguous disk");
}

void validate_answer_xml(const std::string& content,const std::string& role){
    require(role=="audit" || role=="oobe","Unknown answer-file role");
    require(!content.empty() && content.size()<=64*1024 && content.find('\0')==std::string::npos,"Answer XML exceeds the UTF-8 size bound");
    auto upper=content;std::transform(upper.begin(),upper.end(),upper.begin(),[](unsigned char c){return std::toupper(c);});
    require(upper.find("<!DOCTYPE")==std::string::npos && upper.find("<!ENTITY")==std::string::npos,"XML DTDs and entities are unsupported");
    pugi::xml_document doc;require(doc.load_buffer(content.data(),content.size(),pugi::parse_default|pugi::parse_declaration,pugi::encoding_utf8),"Invalid answer XML");
    auto declaration=doc.first_child();if(declaration.type()==pugi::node_declaration){auto encoding=std::string(declaration.attribute("encoding").value());std::transform(encoding.begin(),encoding.end(),encoding.begin(),[](unsigned char c){return std::tolower(c);});require(encoding.empty()||encoding=="utf-8"||encoding=="utf8","XML encoding must be UTF-8");}
    int roots=0;for(auto node:doc.children())if(node.type()==pugi::node_element)++roots;require(roots==1,"Exactly one XML document root required");
    auto root=doc.document_element();require(std::string(root.name())=="unattend" && std::string(root.attribute("xmlns").value())=="urn:schemas-microsoft-com:unattend","Microsoft unattend root required");
    std::vector<std::pair<pugi::xml_node,int>> pending{{root,1}};int count=0;
    while(!pending.empty()){
        auto [node,depth]=pending.back();pending.pop_back();require(depth<=32 && ++count<=5000,"Answer XML nesting/count exceeds bounds");
        require(std::string(node.name()).find(':')==std::string::npos,"Use unprefixed answer elements");
        for(auto parent=node;parent;parent=parent.parent())if(parent.attribute("xmlns")){require(std::string(parent.attribute("xmlns").value())=="urn:schemas-microsoft-com:unattend","Unattend namespace changed");break;}
        for(auto child:node.children())if(child.type()==pugi::node_element)pending.emplace_back(child,depth+1);
    }
    std::set<std::string> passes; pugi::xml_node reseal,startup;
    for(auto settings:root.children()){
        if(settings.type()!=pugi::node_element)continue;
        auto pass=std::string(settings.attribute("pass").value());
        require(std::string(settings.name())=="settings" && passes.insert(pass).second &&
                (pass=="oobeSystem" || (role=="audit" && (pass=="specialize"||pass=="auditSystem"||pass=="auditUser"))),"Unsupported or repeated answer pass");
        std::set<std::string> names;
        for(auto c:settings.children()){
            if(c.type()!=pugi::node_element)continue;
            auto name=std::string(c.attribute("name").value());
            require(std::string(c.name())=="component" && !name.empty() && names.insert(name).second && std::string(c.attribute("processorArchitecture").value())=="amd64","Unique named AMD64 components required");
            if(name=="Microsoft-Windows-Deployment"){
                for(auto [key,value]:std::vector<std::pair<const char*,const char*>>{{"publicKeyToken","31bf3856ad364e35"},{"language","neutral"},{"versionScope","nonSxS"}})require(std::string(c.attribute(key).value())==value,"Deployment component identity changed");
                if(pass=="oobeSystem")reseal=c;
                if(pass=="auditUser")startup=c;
            }
        }
    }
    auto element_count=[](pugi::xml_node n){int count=0;for(auto c:n.children())if(c.type()==pugi::node_element)++count;return count;};
    auto r=reseal.child("Reseal");require(r && !r.next_sibling("Reseal") && element_count(r)==1 && std::string(r.first_child().name())=="Mode" && std::string(r.child_value("Mode"))==(role=="audit"?"Audit":"OOBE"),"Required reseal handoff changed");
    if(role=="audit"){
        auto group=startup.child("RunAsynchronous");require(group && !group.next_sibling("RunAsynchronous"),"Audit startup group required");
        std::set<int> orders;int found=0;
        for(auto cmd:group.children()){
            if(cmd.type()!=pugi::node_element)continue;
            auto value=std::string(cmd.child_value("Order"));require(std::regex_match(value,std::regex("[0-9]{1,3}")),"Invalid Audit command order");auto order=std::stoi(value);
            require(std::string(cmd.name())=="RunAsynchronousCommand" && order>=1 && order<=500 && orders.insert(order).second,"Audit command orders must be unique");
            if(order==1){
                require(element_count(cmd)==3 && cmd.child("Order") && cmd.child("Path") && cmd.child("Description") &&
                        std::string(cmd.child_value("Path"))=="\"%SystemDrive%\\ProgramData\\re-pxe\\re-pxe.exe\" complete" && std::string(cmd.child_value("Description"))=="repxeCompletion-v1","Required re-pxe startup command changed");
                int actions=0;
                for(auto attribute:cmd.attributes()){
                    auto name=std::string(attribute.name());auto colon=name.find(':');if(colon==std::string::npos || name.substr(colon+1)!="action")continue;
                    auto key="xmlns:"+name.substr(0,colon);std::string ns;
                    for(auto parent=cmd;parent;parent=parent.parent())if(parent.attribute(key.c_str())){ns=parent.attribute(key.c_str()).value();break;}
                    if(ns=="http://schemas.microsoft.com/WMIConfig/2002/State"){require(std::string(attribute.value())=="add","Audit command action changed");++actions;}
                }
                require(actions==1,"WCM action required");++found;
            }
        }
        require(found==1,"Exactly one re-pxe startup at order 1 required");
    }
}

void validate_config(const Json& config) {
    require(config.is_object(), "Configuration must be an object");
    require(integer_field(config,"schemaVersion",0) == 1, "Unsupported configuration version");
    const std::set<std::string> keys{"schemaVersion","image","sha256","imageIndex",
        "targetIdentity","scratchDirectory","jobDirectory","recoveryMiB","targetPolicy","bootEx","driverManifest","driverManifestSha256","reportDelivery","windowsUpdate","offlineDriverInfPaths","offlineDriverMode","driverConsole","verifyImage","answerFiles","finishOptions"};
    for (auto it = config.begin(); it != config.end(); ++it)
        require(keys.contains(it.key()), "Unknown configuration field: " + it.key());
    for (const auto* key : {"image","sha256","targetIdentity","scratchDirectory","jobDirectory"})
        require(config.contains(key) && config[key].is_string() && !config[key].get<std::string>().empty(),
                std::string("Missing configuration field: ") + key);
    if(config.contains("verifyImage"))require(config.at("verifyImage").is_boolean(),"verifyImage must be a boolean");
    if(config.contains("finishOptions"))validate_finish_options(config.at("finishOptions"));
    require(digest_valid(config.at("sha256")), "SHA-256 must be 64 lowercase hexadecimal characters");
    require(integer_field(config,"imageIndex",1) >= 1, "Image index must be positive");
    const auto recovery = integer_field(config,"recoveryMiB",1536);
    require(recovery >= 1024 && recovery <= 8192, "Recovery size must be between 1024 and 8192 MiB");
    if(config.contains("targetPolicy"))require(config.at("targetPolicy")=="blank-raw" || config.at("targetPolicy")=="format-selected-disk","Unsupported target disk policy");
    if(config.contains("bootEx"))require(config.at("bootEx").is_boolean(),"bootEx must be a boolean");
    require(config.contains("driverManifest")==config.contains("driverManifestSha256"),"Driver manifest path and digest must be supplied together");
    for(const auto* key:{"driverManifest","reportDelivery"})if(config.contains(key))require(config.at(key).is_string() && !config.at(key).get<std::string>().empty(),"Optional resource path must be a nonempty string");
    if(config.contains("driverManifestSha256"))require(digest_valid(config.at("driverManifestSha256").get<std::string>()),"Invalid driver manifest digest");
    if(config.contains("answerFiles")){
        const auto& answers=config.at("answerFiles");
        require(answers.is_object() && answers.size()==3 && answers.at("schemaVersion")==1,"Invalid answer-file bundle");
        for(const auto* role:{"audit","oobe"}){
            const auto& file=answers.at(role);
            require(file.is_object() && file.size()==2 && file.at("content").is_string() && file.at("sha256").is_string() && digest_valid(file.at("sha256")),"Answer files require XML content and SHA-256");
            validate_answer_xml(file.at("content"),role);
        }
    }
    if(config.contains("driverConsole"))require(config.at("driverConsole").is_boolean(),"Invalid driver console option");
    if(config.contains("offlineDriverMode"))require(config.at("offlineDriverMode")=="boot-critical"||config.at("offlineDriverMode")=="all-reviewed","Unsupported offline driver mode");
    if(config.contains("windowsUpdate")){
        const auto& policy=config.at("windowsUpdate");
        require(policy.is_object() && policy.size()==2,"Invalid Windows Update driver policy");
        auto timeout=integer_field(policy,"timeoutSeconds",0),attempts=integer_field(policy,"maxAttempts",0);
        require(timeout>=60 && timeout<=1800 && attempts>=1 && attempts<=3,"Windows Update driver policy exceeds bounds");
    }
    if(config.contains("offlineDriverInfPaths")){
        require(config.contains("driverManifest") && config.at("offlineDriverInfPaths").is_array() && config.at("offlineDriverInfPaths").size()<=256,"Offline drivers require a pinned reviewed package");
        for(const auto& inf:config.at("offlineDriverInfPaths"))require(inf.is_string() && !inf.get<std::string>().empty(),"Offline driver path must be a selected package INF");
    }
}

Json gpt_layout(unsigned long long capacity,int recovery_mib,unsigned int sector_bytes){
    require(sector_bytes==512 || sector_bytes==4096,"Only 512-byte or 4096-byte logical sectors are supported");
    require(recovery_mib>=1024 && recovery_mib<=8192,"Invalid recovery partition size");
    require(capacity<static_cast<unsigned long long>(std::numeric_limits<long long>::max()),"Disk size exceeds supported offsets");
    const auto recovery=static_cast<unsigned long long>(recovery_mib)*MiB;
    const auto end=(capacity/MiB)*MiB-MiB;
    require(capacity>=16*GiB && end>278*MiB+recovery,"Disk is too small for the GPT layout");
    const auto windows=end-recovery-277*MiB;
    return Json::array({{{"role","efi"},{"offset",MiB},{"length",260*MiB},{"filesystem","FAT32"}},
        {{"role","msr"},{"offset",261*MiB},{"length",16*MiB}},
        {{"role","windows"},{"offset",277*MiB},{"length",windows},{"filesystem","NTFS"}},
        {{"role","recovery"},{"offset",end-recovery},{"length",recovery},{"filesystem","NTFS"}}});
}

void validate_blank_deployment(const Json& config,const Json& status,const Json& scratch,
                               unsigned long long scratch_free,unsigned long long expanded_bytes){
    const auto policy=config.value("targetPolicy","");
    require(policy=="blank-raw" || policy=="format-selected-disk","Deployment requires an explicit supported target disk policy");
    if(policy=="blank-raw"){
        require(status.value("partitionStyle","")=="raw" && status.value("partitionCount",-1)==0,"Deployment accepts only an unpartitioned RAW disk");
        require(status.value("allBytesZero",false),"Deployment refuses a RAW disk containing any data");
    }else require(status.value("partitionStyle","")=="raw" || status.value("partitionStyle","")=="mbr" || status.value("partitionStyle","")=="gpt","Selected disk has an unsupported partition layout");
    require(scratch.value("resolved",false) && scratch.value("type","")=="local" && !scratch.at("diskNumbers").empty(),"Deployment requires local physical scratch outside the target");
    require(expanded_bytes>0 && expanded_bytes<std::numeric_limits<unsigned long long>::max()-GiB && scratch_free>=expanded_bytes+GiB,"Deployment scratch lacks space for the expanded image estimate plus 1 GiB");
}

Json inspect_xml(const std::string& xml, int index) {
    require(index > 0, "Image index must be positive");
    pugi::xml_document document;
    auto result = document.load_buffer(xml.data(), xml.size(), pugi::parse_default, pugi::encoding_utf8);
    require(static_cast<bool>(result), "WIM metadata XML could not be parsed");
    auto root = document.child("WIM");
    require(static_cast<bool>(root), "WIM metadata root is missing");
    Json images = Json::array();
    Json selected;
    for (auto node : root.children("IMAGE")) {
        auto windows = node.child("WINDOWS");
        Json item = {{"index", node.attribute("INDEX").as_int()},
            {"name", node.child_value("NAME")}, {"totalBytes", node.child("TOTALBYTES").text().as_ullong()},
            {"architecture", windows.child_value("ARCH")}, {"edition", windows.child_value("EDITIONID")},
            {"build", windows.child("VERSION").child_value("BUILD")}};
        images.push_back(item);
        if (item["index"] == index) selected = item;
    }
    require(!selected.is_null(), "Requested image index does not exist");
    return {{"schemaVersion", 1}, {"images", images}, {"selectedImage", selected}};
}

std::string nvme_controller_serial(const std::vector<unsigned char>& identify) {
    if(identify.size()<4096 || (identify[0]==0 && identify[1]==0) || (identify[0]==255 && identify[1]==255))
        throw std::runtime_error("Invalid NVMe controller Identify response");
    std::string serial(reinterpret_cast<const char*>(identify.data()+4),20);
    if(std::any_of(serial.begin(),serial.end(),[](unsigned char c){return c<32 || c>126;}))
        throw std::runtime_error("Invalid NVMe controller serial");
    auto last=serial.find_last_not_of(' ');if(last==std::string::npos)throw std::runtime_error("NVMe controller serial is unavailable");
    serial.resize(last+1);serial.erase(0,serial.find_first_not_of(' '));return serial;
}

size_t pxe_target_index(const Json& disks, const Json& automation) {
    if (!disks.is_array() || disks.empty()) throw std::runtime_error("No eligible target disks");
    if (!automation.at("automaticDiskSelection").get<bool>()) return 0;
    auto target=automation.at("targetIdentity").get<std::string>();
    size_t index=0, matches=0;
    for(size_t i=0;i<disks.size();++i) if(disks.at(i).at("identity")==target) {index=i;++matches;}
    if(matches!=1) throw std::runtime_error("The assigned target disk is missing or ambiguous");
    return index;
}

Json pxe_image_metadata(const Json& selection){
    const auto& p=selection.at("profile");const auto& i=selection.at("image");const auto& m=i.at("selected_image");
    require(p.at("enabled").get<bool>(),"PXE configuration is disabled");
    require(p.at("image_id")==i.at("id") && p.at("image_sha256")==i.at("sha256") && p.at("image_index")==i.at("image_index"),"PXE image binding differs");
    require(std::regex_match(i.at("sha256").get<std::string>(),std::regex("[a-f0-9]{64}")),"Invalid approved image hash");
    require(i.at("architecture")=="amd64" && m.at("architecture")=="9","PXE requires AMD64 Windows");
    require(p.at("windows_build")==m.at("build") && std::regex_match(m.at("build").get<std::string>(),std::regex("[0-9]{4,6}")),"PXE Windows build differs");
    require(m.at("index")==i.at("image_index") && i.at("image_index")>=1 && i.at("image_index")<=100,"PXE image index differs");
    require(i.at("size").is_number_unsigned() || i.at("size").is_number_integer(),"Invalid PXE download size");
    require(i.at("size")>0 && i.at("size")<=100ULL*GiB && m.at("total_bytes")>0 && m.at("total_bytes")<1024ULL*GiB,"Invalid PXE image size");
    if(!selection.at("driverManifest").is_null()){
        const auto& driver=selection.at("driverManifest");validate_driver_manifest(driver);
        require(driver.at("packageId")==p.at("driver_id") && driver.at("windowsBuild")==m.at("build"),"PXE driver binding differs");
    }else require(p.at("driver_id").is_null(),"Selected PXE drivers are missing");
    return {{"downloadBytes",i.at("size")},{"selectedImage",{{"index",m.at("index")},{"name",m.at("name")},{"totalBytes",m.at("total_bytes")},{"architecture",m.at("architecture")},{"edition",m.at("edition")},{"build",m.at("build")}}}};
}

bool validate_downloaded_image(const Json& config,const Json& approved,const Json& actual,unsigned long long size,const std::string& hash){
    validate_config(config);
    require(size==approved.at("downloadBytes").get<unsigned long long>(),"Downloaded Windows image size differs");
    require(actual.at("selectedImage")==approved.at("selectedImage"),"Downloaded WIM differs from approved metadata");
    const bool verify=config.value("verifyImage",true);
    if(verify)require(hash==config.at("sha256").get<std::string>(),"Downloaded Windows image hash differs");
    return verify;
}
std::string tls_failure_detail(unsigned long flags){
    std::string result;
    for(const auto& [bit,detail]:std::vector<std::pair<unsigned long,std::string>>{{1,"certificate revocation check failed"},{2,"invalid certificate"},{4,"certificate revoked"},{8,"untrusted certificate authority"},{16,"server name does not match certificate"},{32,"certificate date invalid; check the PC clock"},{64,"certificate usage invalid"},{0x80000000UL,"TLS channel negotiation failed"}}){if(flags&bit){if(!result.empty())result+="; ";result+=detail;}}
    return result.empty()?"TLS/certificate validation failed; check the PC clock and provisioned server CA":result;
}

Json deployment_plan(const Json& config, const Json& inventory, const Json& image,
                     const std::string& actual_hash, const Json& resources) {
    validate_config(config);
    require(inventory.at("environment").value("winpe", false), "Deployment planning requires WinPE");
    require(inventory.at("environment").value("firmware", "unknown") == "uefi", "Only UEFI deployment is supported");
    require(inventory.at("environment").value("architecture", "unknown") == "amd64", "AMD64 runtime required");
    require(actual_hash == config.at("sha256").get<std::string>(), "Image SHA-256 mismatch");
    const auto selected_image = image.at("selectedImage");
    require(selected_image.at("index") == config.value("imageIndex", 1), "WIM index does not match configuration");
    require(selected_image.value("architecture", "") == "9", "Selected image is not AMD64 Windows");
    const auto image_bytes = selected_image.at("totalBytes").get<unsigned long long>();
    require(image_bytes > 0 && image_bytes < std::numeric_limits<unsigned long long>::max() / 4, "Invalid WIM expanded size");
    Json target;
    int matches = 0;
    for (const auto& disk : inventory.at("disks")) {
        if (disk.value("identity", "") == config.at("targetIdentity").get<std::string>()) { target = disk; ++matches; }
    }
    require(matches == 1, "Target identity must match exactly one disk");
    require(target.value("stableIdentity", false), "Disk has no stable identity; unattended selection is refused");
    require(!target.value("removable", true), "Removable/USB disks are excluded from deployment");
    require(target.value("writable", false), "Target disk is read-only or writeability is unknown");
    const auto disk_number = target.at("number").get<int>();
    validate_resources(resources, disk_number);
    const auto esp = 260 * MiB, msr = 16 * MiB;
    const auto recovery = static_cast<unsigned long long>(config.value("recoveryMiB", 1536)) * MiB;
    const auto headroom = std::max(8 * GiB, image_bytes / 5);
    const auto required = image_bytes + headroom + esp + msr + recovery + 4 * MiB;
    const auto capacity = target.at("sizeBytes").get<unsigned long long>();
    require(capacity >= required, "Target disk is too small for the image, layout and free-space reserve");
    return {{"schemaVersion", 1}, {"toolVersion", "0.13.6"}, {"readOnly", true}, {"diskWritesPerformed", false},
        {"config", config}, {"target", target}, {"image", selected_image}, {"sha256", actual_hash},
        {"resources", resources}, {"requiredBytes", required}, {"preserveOtherDisks", true},
        {"layout", Json::array({{{"role","efi"},{"filesystem","FAT32"},{"sizeMiB",260}},
            {{"role","msr"},{"sizeMiB",16}}, {{"role","windows"},{"filesystem","NTFS"},{"size","remaining minus recovery"}},
            {{"role","recovery"},{"filesystem","NTFS"},{"sizeMiB",config.value("recoveryMiB",1536)}}})},
        {"executionAvailable", config.value("targetPolicy","")=="blank-raw" || config.value("targetPolicy","")=="format-selected-disk"},
        {"executionPolicy", config.value("targetPolicy","")=="format-selected-disk" ? "Explicit format-selected-disk policy; all existing partitions and data on the selected disk will be erased" : "Explicit blank-raw policy; full disk zero scan and physical scratch required before writes"}};
}

Json capture_preflight(const Json& config, const Json& inventory, const Json& source,
                       const Json& resources, unsigned long long destination_free,
                       unsigned long long scratch_free) {
    require(config.is_object(), "Capture configuration must be an object");
    require(integer_field(config,"schemaVersion",0) == 1, "Unsupported capture configuration version");
    const std::set<std::string> keys{"schemaVersion","sourceVolume","destination","scratchDirectory",
        "jobDirectory","name","referencePreparation"};
    for (auto it=config.begin();it!=config.end();++it) require(keys.contains(it.key()),"Unknown capture field: "+it.key());
    for (const auto* key : {"sourceVolume","destination","scratchDirectory","jobDirectory","name","referencePreparation"})
        require(config.contains(key) && config[key].is_string() && !config[key].get<std::string>().empty(),std::string("Missing capture field: ")+key);
    require(inventory.at("environment").value("winpe", false), "OS capture requires WinPE; running Windows is never captured");
    require(source.value("windowsInstallation", false), "Source is not a discovered Windows installation");
    require(source.value("filesystem", "") == "NTFS", "OS source must be NTFS");
    require(source.value("mappingResolved", false) && source.at("diskNumbers").size()==1,
            "Capture source must map to one physical disk");
    require(resources.is_array() && !resources.empty(),"Capture resources were not resolved");
    const auto source_disk=source.at("diskNumbers")[0].get<int>();
    for (const auto& resource:resources) {
        require(resource.value("resolved",false),"Cannot resolve capture resource location");
        require(!contains_disk(resource,source_disk),"Capture destination/scratch/logs cannot be on the source disk");
    }
    const Json* destination=nullptr; const Json* scratch=nullptr;
    for (const auto& resource:resources) {
        if(resource.value("role","")=="destination") {
            require(!destination,"Ambiguous capture destination resource"); destination=&resource;
        }
        if(resource.value("role","")=="scratch") {
            require(!scratch,"Ambiguous capture scratch resource"); scratch=&resource;
        }
    }
    require(destination && scratch,"Capture destination and scratch resources are required");
    require(scratch->value("type","")=="local" && scratch->contains("volume") && !scratch->at("diskNumbers").empty(),
            "Capture verification requires a local physical scratch volume outside the source disk; RAM/network scratch is refused");
    const auto used=source.at("usedBytes").get<unsigned long long>();
    require(used>0 && used<=(std::numeric_limits<unsigned long long>::max()-2*GiB)/2,"Invalid capture source size");
    // Reserve an uncompressed source-size bound for both output and verification scratch.
    // This deliberately avoids guessing compression ratios or the largest WIM resource.
    require(destination_free>=used+GiB,"Destination lacks space for an uncompressed-size capture estimate");
    require(scratch_free>=used+GiB,"Scratch volume lacks space for verified capture; reserve source used bytes plus 1 GiB");
    const bool shared=destination->contains("volume") && destination->at("volume")==scratch->at("volume");
    if(shared)require(std::min(destination_free,scratch_free)>=2*(used+GiB),
                      "Shared destination/scratch volume lacks combined capture and verification space");
    return {{"schemaVersion",1},{"readOnly",true},{"source",source},{"resources",resources},
            {"estimatedRequiredBytes",used+GiB},{"estimatedScratchRequiredBytes",used+GiB},
            {"sharedDestinationScratchVolume",shared},{"estimatedSharedVolumeRequiredBytes",shared?2*(used+GiB):0},
            {"referencePreparation",config.at("referencePreparation")}};
}

std::wstring quote_argument(const std::wstring& argument) {
    // Windows CommandLineToArgvW/CRT rules; CreateProcess is used directly, never cmd.exe.
    std::wstring out=L"\"";
    unsigned int slashes=0;
    for(wchar_t c:argument) {
        if(c==L'\\') { ++slashes; continue; }
        if(c==L'\"') { out.append(slashes*2+1,L'\\'); out+=c; }
        else { out.append(slashes,L'\\'); out+=c; }
        slashes=0;
    }
    out.append(slashes*2,L'\\'); out+=L'\"'; return out;
}
std::wstring command_line(const std::vector<std::wstring>& arguments) {
    std::wstring result;
    // Legacy system tools such as format.com reject quotes around switches.
    // Quote the executable and arguments that require it; CreateProcess uses no shell.
    for(const auto& arg:arguments) {
        const bool first=result.empty();if(!first)result+=L' ';
        result+=(first || arg.empty() || arg.find_first_of(L" \t\r\n\v\f\"")!=std::wstring::npos)?quote_argument(arg):arg;
    }
    return result;
}
}
