#include <repxe/core.hpp>
#include <repxe/windows_controls.hpp>
#include <functional>
#include <fstream>
#include <iostream>
#include <stdexcept>

using namespace repxe;
namespace {
int checks=0;
void check(bool condition,const std::string& label){++checks;if(!condition)throw std::runtime_error(label);}
void rejects(const std::function<void()>& action,const std::string& label){bool rejected=false;try{action();}catch(const std::exception&){rejected=true;}check(rejected,label);}
Json config(){return {{"schemaVersion",1},{"image","\\\\server\\images\\image.wim"},{"sha256",std::string(64,'a')},
    {"imageIndex",1},{"targetIdentity","serial:LAB001"},{"scratchDirectory","X:\\scratch"},{"jobDirectory","Z:\\jobs"}};}
Json inv(){return {{"environment",{{"winpe",true},{"firmware","uefi"},{"architecture","amd64"}}},
    {"disks",Json::array({{{"number",1},{"identity","serial:LAB001"},{"stableIdentity",true},{"sizeBytes",64*GiB},{"removable",false},{"writable",true}},
                         {{"number",2},{"identity","serial:KEEP"},{"stableIdentity",true},{"sizeBytes",100*GiB},{"removable",false},{"writable",true}}})}};}
Json image(){return {{"selectedImage",{{"index",1},{"architecture","9"},{"totalBytes",20*GiB}}}};}
Json resources(){return Json::array({{{"role","image"},{"resolved",true},{"diskNumbers",Json::array()}},
    {{"role","scratch"},{"resolved",true},{"diskNumbers",Json::array()}},{{"role","logs"},{"resolved",true},{"diskNumbers",Json::array({2})}}});}
}
int main(int argc,char** argv){
    {
        const std::string job="11111111-2222-3333-4444-555555555555",id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",path="/api/client-jobs/"+job;
        Json command={{"id",id},{"job_id",job},{"action","restart"}};
        validate_windows_command(command,path);check(true,"Accept a restart bound to this Windows job");
        Json receipts=Json::object();check(prepare_windows_receipt(receipts,command,"now"),"A new remote command receives one execution receipt");
        const auto started=receipts;
        receipts[id]["result"]={{"status","completed"},{"path",""},{"error",""}};
        check(!prepare_windows_receipt(receipts,command,"later")&&receipts[id]["result"]["status"]=="completed","Lost power acknowledgement is retried without executing a second restart");
        receipts=started;check(!prepare_windows_receipt(receipts,command,"after crash")&&receipts[id]["result"]["status"]=="failed","Interrupted power command cannot replay after reboot");
        auto different=command;different["action"]="shutdown";rejects([&]{prepare_windows_receipt(receipts,different,"later");},"A receipt cannot authorize a different command");
        auto bad=command;bad["job_id"]=id;rejects([&]{validate_windows_command(bad,path);},"Refuse commands for another installation");
        for(const auto* action:{"execute","shell","finish","wipe"}){bad=command;bad["action"]=action;rejects([&]{validate_windows_command(bad,path);},"Remote controls never accept arbitrary execution or installation actions");}
        command["action"]="file";command["filename"]="Technician notes.txt";command["size"]=123;command["sha256"]=std::string(64,'a');command["download_path"]=path+"/windows/files/"+id;
        validate_windows_command(command,path);check(true,"Accept a bounded desktop transfer with pinned bytes and closed download path");
        for(const auto* name:{"../escape","C:\\escape","bad:stream","CON.txt","NUL","COM1.log","LPT1.txt","trailing.","trailing ","bad/name","bad|name"}){bad=command;bad["filename"]=name;rejects([&]{validate_windows_command(bad,path);},"Refuse nonportable desktop filenames and traversal");}
        for(long long size:{-1LL,0LL,100LL*1024*1024*1024+1}){bad=command;bad["size"]=size;rejects([&]{validate_windows_command(bad,path);},"Reject invalid transfer sizes");}
        bad=command;bad["download_path"]=path+"/windows/files/"+id+"?redirect=1";rejects([&]{validate_windows_command(bad,path);},"Reject alternate transfer endpoints");
        bad=command;bad["sha256"]="bad";rejects([&]{validate_windows_command(bad,path);},"A desktop transfer requires SHA-256 metadata");
    }
    {
        const std::string client(64,'a'),original_kernel(64,'b'),updated_kernel(64,'c');
        Json state={{"schemaVersion",1},{"clientSha256",client},{"kernelSha256",original_kernel}};
        check(!validate_completion_bindings(state,client,client,original_kernel,false),"Original reviewed kernel retains its exact binding");
        check(validate_completion_bindings(state,client,client,updated_kernel,true),"Microsoft-verified kernel servicing permits post-reboot resume");
        rejects([&]{validate_completion_bindings(state,client,client,updated_kernel,false);},"Changed unsigned or untrusted kernel is refused");
        rejects([&]{validate_completion_bindings(state,updated_kernel,client,updated_kernel,true);},"Kernel servicing never authorizes a different installed client");
        rejects([&]{validate_completion_bindings(state,client,updated_kernel,updated_kernel,true);},"Kernel servicing never authorizes a different running client");
        auto bad=state;bad["schemaVersion"]=2;
        rejects([&]{validate_completion_bindings(bad,client,client,updated_kernel,true);},"Unknown completion schema cannot resume");
        bad=state;bad["kernelSha256"]="";
        rejects([&]{validate_completion_bindings(bad,client,client,updated_kernel,true);},"Kernel servicing requires original image provenance");
        rejects([&]{validate_completion_bindings(state,client,client,"",true);},"Missing current kernel is never trusted");
    }
    {
        Json disk={{"schemaVersion",1},{"stage","awaiting-windows"},{"kernelSha256",std::string(64,'a')},{"driverAutomaticAttempted",false}};
        Json verified=disk;verified["windowsKernelBinding"]={{"imageSha256",disk.at("kernelSha256")},{"currentSha256",std::string(64,'b')},{"changed",true},{"microsoftSignatureVerified",true}};
        const auto snapshot=verified;
        auto working=completion_state_after_reload(disk,verified);
        check(working.at("windowsKernelBinding")==snapshot.at("windowsKernelBinding")&&!disk.contains("windowsKernelBinding"),"First driver scan accepts a fresh state without persisted derived kernel evidence");
        disk["stage"]="driver-menu";disk["driverAutomaticAttempted"]=true;
        disk["pendingDriverUpload"]={{"id","retained-upload"},{"received",8*MiB}};
        working=completion_state_after_reload(disk,verified);
        check(working.at("pendingDriverUpload")==disk.at("pendingDriverUpload")&&working.at("stage")=="driver-menu"&&working.at("driverAutomaticAttempted")==true,"Upload reload preserves fresh progress instead of restoring the stale verified snapshot");
        check(verified==snapshot,"Driver actions never mutate the independently verified installation snapshot");
        disk["windowsKernelBinding"]={{"currentSha256","stale on-disk evidence"}};
        working=completion_state_after_reload(disk,verified);
        check(working.at("windowsKernelBinding")==snapshot.at("windowsKernelBinding")&&working.at("kernelSha256")==disk.at("kernelSha256"),"Reload uses current verified kernel evidence while preserving original provenance");
        auto& alias=verified;alias=completion_state_after_reload(disk,verified);
        check(alias.at("windowsKernelBinding")==snapshot.at("windowsKernelBinding")&&alias.at("pendingDriverUpload")==disk.at("pendingDriverUpload"),"An aliased destination cannot discard kernel evidence before the reload finishes");
        rejects([&]{completion_state_after_reload(disk,Json::object());},"Missing verified evidence is not replaced by trusting the disk record");
        auto invalid=snapshot;invalid["windowsKernelBinding"]=nullptr;
        rejects([&]{completion_state_after_reload(disk,invalid);},"Malformed runtime evidence remains an error");
        rejects([&]{completion_state_after_reload(Json(),snapshot);},"Malformed reloaded state remains an error");
        auto sealed=disk;sealed["stage"]="complete";sealed["deliveryStatus"]="delivered";
        working=completion_state_after_reload(sealed,snapshot);
        check(working.at("stage")=="complete"&&working.at("deliveryStatus")=="delivered","Finish and report reloads preserve terminal state rather than repeating driver work");
    }
    {
        Json disks=Json::array({{{"identity","serial:FIRST"}},{{"identity","serial:PINNED"}}});
        Json policy={{"automaticDiskSelection",true},{"targetIdentity","serial:PINNED"}};
        if(pxe_target_index(disks,policy)!=1)throw std::runtime_error("Pinned second target was ignored");
        policy["automaticDiskSelection"]=false;
        if(pxe_target_index(disks,policy)!=0)throw std::runtime_error("Manual selection changed");
        policy["automaticDiskSelection"]=true;policy["targetIdentity"]="serial:MISSING";
        bool rejected=false;try{pxe_target_index(disks,policy);}catch(const std::exception&){rejected=true;}
        if(!rejected)throw std::runtime_error("Missing assigned disk was accepted");
        policy["targetIdentity"]="serial:PINNED";disks.push_back(disks.at(1));rejected=false;
        try{pxe_target_index(disks,policy);}catch(const std::exception&){rejected=true;}
        if(!rejected)throw std::runtime_error("Ambiguous assigned disk was accepted");
    }
try{
    if(argc==3 && std::string(argv[1])=="--answer-corpus"){
        std::ifstream source(argv[2]);Json cases;source>>cases;
        for(const auto& item:cases){bool accepted=true;try{validate_answer_xml(item.at("content"),item.at("role"));}catch(const std::exception&){accepted=false;}check(accepted==item.at("accepted"),item.at("name"));}
        std::cout<<checks<<" answer-file cases passed\n";return 0;
    }
    const std::string oobe=R"(<unattend xmlns="urn:schemas-microsoft-com:unattend"><settings pass="oobeSystem"><component name="Microsoft-Windows-Deployment" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS"><Reseal><Mode>OOBE</Mode></Reseal></component></settings></unattend>)";
    validate_answer_xml(oobe,"oobe");check(true,"Accept final OOBE template");
    auto replace_answer=[&](const std::string& from,const std::string& to){auto changed=oobe;changed.replace(changed.find(from),from.size(),to);return changed;};
    rejects([&]{validate_answer_xml(replace_answer("OOBE","Audit"),"oobe");},"Preserve OOBE reseal");
    rejects([&]{validate_answer_xml(replace_answer("oobeSystem","windowsPE"),"oobe");},"Reject Setup-only passes");
    rejects([&]{validate_answer_xml(replace_answer("amd64","arm64"),"oobe");},"Reject incompatible answer architecture");
    rejects([&]{validate_answer_xml(oobe+"<unattend/>","oobe");},"Reject multiple XML roots");
    rejects([&]{validate_answer_xml("<!DOCTYPE unattend>"+oobe,"oobe");},"Reject answer DTDs");
    rejects([&]{validate_answer_xml(oobe+std::string(65536,' '),"oobe");},"Bound answer-file size");
    const std::string audit=R"(<unattend xmlns="urn:schemas-microsoft-com:unattend" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State"><settings pass="oobeSystem"><component name="Microsoft-Windows-Deployment" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS"><Reseal><Mode>Audit</Mode></Reseal></component></settings><settings pass="auditUser"><component name="Microsoft-Windows-Deployment" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS"><RunAsynchronous><RunAsynchronousCommand wcm:action="add"><Order>1</Order><Path>"%SystemDrive%\ProgramData\re-pxe\re-pxe.exe" complete</Path><Description>repxeCompletion-v1</Description></RunAsynchronousCommand></RunAsynchronous></component></settings></unattend>)";
    validate_answer_xml(audit,"audit");check(true,"Accept Audit completion handoff");
    auto broken_audit=audit;broken_audit.replace(broken_audit.find(" complete"),9," shell");
    rejects([&]{validate_answer_xml(broken_audit,"audit");},"Require completion startup command");
    rejects([&]{validate_answer_xml(oobe,"audit");},"Refuse missing Audit startup");
    auto base=config();auto hardware=inv();auto wim=image();auto paths=resources();auto hash=std::string(64,'a');
    Json downloaded={{"selectedImage",wim.at("selectedImage")}},approved={{"downloadBytes",1234},{"selectedImage",wim.at("selectedImage")}};
    check(validate_downloaded_image(base,approved,downloaded,1234,hash),"Default image hash verification is enabled");
    rejects([&]{validate_downloaded_image(base,approved,downloaded,1234,std::string(64,'b'));},"Reject changed image hash by default");
    auto no_verify=base;no_verify["verifyImage"]=false;check(!validate_downloaded_image(no_verify,approved,downloaded,1234,""),"Explicit routine skips full hash read");
    rejects([&]{validate_downloaded_image(no_verify,approved,downloaded,1233,"");},"Skipped hash still requires complete download");
    auto other_image=downloaded;other_image["selectedImage"]["build"]="99999";rejects([&]{validate_downloaded_image(no_verify,approved,other_image,1234,"");},"Skipped hash still pins Windows metadata");
    no_verify["verifyImage"]="false";rejects([&]{validate_config(no_verify);},"Image verification policy must be boolean");
    check(tls_failure_detail(32).find("PC clock")!=std::string::npos,"TLS date failure explains clock");
    check(tls_failure_detail(8|16).find("untrusted")!=std::string::npos&&tls_failure_detail(8|16).find("server name")!=std::string::npos,"TLS reports multiple validation causes");

    auto with_answers=base;with_answers["answerFiles"]={{"schemaVersion",1},{"audit",{{"content",audit},{"sha256",hash}}},{"oobe",{{"content",oobe},{"sha256",hash}}}};
    validate_config(with_answers);check(true,"Accept bounded answer-file bundle");
    with_answers["answerFiles"]["oobe"]["sha256"]="wrong";rejects([&]{validate_config(with_answers);},"Require answer digests");
    auto candidates=pxe_disks(hardware);check(candidates.at(0).at("identity")=="serial:LAB001","Automatic disk choice preserves inventory order");
    auto filtered=hardware;filtered["disks"][0]["removable"]=true;check(pxe_disks(filtered).at(0).at("identity")=="serial:KEEP","Exclude removable disks from automatic selection");
    filtered=hardware;filtered["disks"][0]["writable"]=false;check(pxe_disks(filtered).at(0).at("identity")=="serial:KEEP","Exclude read-only disks from automatic selection");
    filtered=hardware;for(auto& d:filtered["disks"])d["stableIdentity"]=false;rejects([&]{pxe_disks(filtered);},"Stop when no disk has a stable identity");
    filtered=hardware;filtered["disks"][0]["allBytesZero"]=false;check(pxe_disks(filtered).at(0).at("identity")=="serial:LAB001","Do not fall back to another disk when the first contains data");
    Json automatic={{"targetDiskPolicy","format-selected-disk"},{"managedSelection",true},{"automaticDiskSelection",true},{"startDelaySeconds",10},{"profileId","12345678-1234-1234-1234-123456789abc"},{"targetIdentity","serial:LAB001"}};
    std::vector<unsigned char> identify(4096,0);identify[0]=0x44;identify[1]=0x14;
    std::fill(identify.begin()+4,identify.begin()+24,' ');std::string sn="S63XNE0R483911";std::copy(sn.begin(),sn.end(),identify.begin()+4);
    check(nvme_controller_serial(identify)==sn,"NVMe controller serial agrees with Linux, independently of Windows EUI");
    auto broken=identify;broken.resize(23);rejects([&]{nvme_controller_serial(broken);},"Reject truncated Identify data");
    broken=identify;broken[4]=0;rejects([&]{nvme_controller_serial(broken);},"Reject embedded NUL in controller SN");
    broken=identify;std::fill(broken.begin()+4,broken.begin()+24,' ');rejects([&]{nvme_controller_serial(broken);},"Reject unavailable controller serial");
    validate_pxe_automation(automatic,candidates);check(true,"Accept a pinned first-disk countdown policy");
    auto bad_policy=automatic;bad_policy["targetIdentity"]="serial:KEEP";validate_pxe_automation(bad_policy,candidates);check(true,"Accept an explicitly pinned second disk");
    bad_policy["targetIdentity"]="serial:MISSING";rejects([&]{validate_pxe_automation(bad_policy,candidates);},"Refuse missing target identity");
    bad_policy=automatic;bad_policy["automaticDiskSelection"]=false;bad_policy["manualDiskSelection"]=true;bad_policy["targetIdentity"]="";validate_pxe_automation(bad_policy,candidates);check(true,"Manual routine waits without choosing a drive");
    for(const auto& delay:Json::array({-1,301,10.5,"10",true})){bad_policy=automatic;bad_policy["startDelaySeconds"]=delay;rejects([&]{validate_pxe_automation(bad_policy,candidates);},"Reject invalid server countdown limits or types");}
    bad_policy=automatic;bad_policy["profileId"]=nullptr;rejects([&]{validate_pxe_automation(bad_policy,candidates);},"Stop without a server-selected configuration");
    bad_policy=automatic;bad_policy["automaticDiskSelection"]="true";rejects([&]{validate_pxe_automation(bad_policy,candidates);},"Refuse a malformed automatic-start flag");
    bad_policy=automatic;bad_policy["managedSelection"]=false;rejects([&]{validate_pxe_automation(bad_policy,candidates);},"Require server-managed image selection");
    bad_policy=automatic;bad_policy["startDelaySeconds"]=0;validate_pxe_automation(bad_policy,candidates);check(true,"Permit an explicitly configured immediate start");
    auto plan=deployment_plan(base,hardware,wim,hash,paths);
    check(plan["toolVersion"]=="0.14.1","Report current planner version");
    check(plan["readOnly"]==true && plan["diskWritesPerformed"]==false,"Planning must not perform writes");
    check(plan["target"]["number"]==1 && plan["preserveOtherDisks"]==true,"Resolve exactly the requested target");
    check(plan["executionAvailable"]==false,"Execution requires an explicit target policy");
    auto copy=hardware;copy["environment"]["winpe"]=false;rejects([&]{deployment_plan(base,copy,wim,hash,paths);},"Reject running Windows");
    copy=hardware;copy["environment"]["firmware"]="bios";rejects([&]{deployment_plan(base,copy,wim,hash,paths);},"Reject BIOS");
    rejects([&]{deployment_plan(base,hardware,wim,std::string(64,'b'),paths);},"Reject hash mismatch");
    copy=hardware;copy["disks"].push_back(copy["disks"][0]);rejects([&]{deployment_plan(base,copy,wim,hash,paths);},"Reject duplicate identities");
    copy=hardware;copy["disks"][0]["stableIdentity"]=false;rejects([&]{deployment_plan(base,copy,wim,hash,paths);},"Reject unstable disk identity");
    copy=hardware;copy["disks"][0]["sizeBytes"]=25*GiB;rejects([&]{deployment_plan(base,copy,wim,hash,paths);},"Reject insufficient capacity");
    copy=hardware;copy["disks"][0]["removable"]=true;rejects([&]{deployment_plan(base,copy,wim,hash,paths);},"Reject removable targets");
    copy=hardware;copy["disks"][0]["writable"]=false;rejects([&]{deployment_plan(base,copy,wim,hash,paths);},"Reject read-only targets");
    copy=paths;copy[0]["diskNumbers"]=Json::array({1});rejects([&]{deployment_plan(base,hardware,wim,hash,copy);},"Protect source image disk");
    copy=paths;copy[1]["resolved"]=false;rejects([&]{deployment_plan(base,hardware,wim,hash,copy);},"Refuse unresolved resource mappings");
    copy=base;copy["schemaVersion"]=99;rejects([&]{validate_config(copy);},"Reject unknown schema");
    copy=base;copy["schemaVersion"]=1.5;rejects([&]{validate_config(copy);},"Reject fractional schema versions");
    copy=base;copy["imageIndex"]=1.5;rejects([&]{validate_config(copy);},"Reject fractional image indices");
    copy=base;copy["imageIndex"]=4294967297ULL;rejects([&]{validate_config(copy);},"Reject overflowing image indices");
    copy=base;copy["autoFormatAllDisks"]=true;rejects([&]{validate_config(copy);},"Reject unknown configuration fields");
    copy=base;copy["recoveryMiB"]=64;rejects([&]{validate_config(copy);},"Reject undersized recovery layout");
    copy=wim;copy["selectedImage"]["architecture"]="0";rejects([&]{deployment_plan(base,hardware,copy,hash,paths);},"Reject x86 image");
    copy=wim;copy["selectedImage"]["index"]=2;rejects([&]{deployment_plan(base,hardware,copy,hash,paths);},"Reject wrong index");
    copy=wim;copy["selectedImage"]["totalBytes"]=0;rejects([&]{deployment_plan(base,hardware,copy,hash,paths);},"Reject absent expanded size");
    const std::string xml="<WIM><IMAGE INDEX=\"1\"><NAME>A &amp; B</NAME><TOTALBYTES>12345</TOTALBYTES><WINDOWS><ARCH>9</ARCH><EDITIONID>Professional</EDITIONID><VERSION><BUILD>26100</BUILD></VERSION></WINDOWS></IMAGE></WIM>";
    check(inspect_xml(xml,1)["selectedImage"]["name"]=="A & B","Parse XML escaped names");
    rejects([&]{inspect_xml(xml,2);},"Reject nonexistent WIM index");
    rejects([&]{inspect_xml("<broken>",1);},"Reject malformed WIM XML");
    check(quote_argument(L"C:\\image folder\\")==L"\"C:\\image folder\\\\\"","Escape trailing backslashes");
    check(quote_argument(L"x\"y")==L"\"x\\\"y\"","Escape embedded quotes");
    check(command_line({L"tool.exe",L"a & b"})==L"\"tool.exe\" \"a & b\"","Keep shell metacharacters in literal arguments");
    check(command_line({L"format.com",L"/FS:FAT32",L"/?"})==L"\"format.com\" /FS:FAT32 /?","Keep legacy formatter switches unquoted");
    Json capture={{"schemaVersion",1},{"sourceVolume","W:\\"},{"destination","Z:\\ref.wim"},{"scratchDirectory","X:\\scratch"},
        {"jobDirectory","Z:\\jobs"},{"name","Reference"},{"referencePreparation","Generalized disposable reference"}};
    auto capture_paths=paths;capture_paths[0]["role"]="destination";capture_paths[0]["volume"]="store";capture_paths[1]["type"]="local";capture_paths[1]["volume"]="scratch";capture_paths[1]["diskNumbers"]=Json::array({2});
    Json source={{"windowsInstallation",true},{"filesystem","NTFS"},{"mappingResolved",true},{"diskNumbers",Json::array({3})},{"usedBytes",10*GiB}};
    check(capture_preflight(capture,hardware,source,capture_paths,30*GiB,30*GiB)["readOnly"]==true,"Capture preflight is read-only");
    copy=capture_paths;copy[0]["diskNumbers"]=Json::array({3});rejects([&]{capture_preflight(capture,hardware,source,copy,30*GiB,30*GiB);},"Capture must not write the source disk");
    rejects([&]{capture_preflight(capture,hardware,source,capture_paths,5*GiB,30*GiB);},"Reject inadequate capture destination");
    copy=capture;copy["referencePreparation"]="";rejects([&]{capture_preflight(copy,hardware,source,capture_paths,30*GiB,30*GiB);},"Require preparation declaration");
    copy=hardware;copy["environment"]["winpe"]=false;rejects([&]{capture_preflight(capture,copy,source,capture_paths,30*GiB,30*GiB);},"Reject online OS capture");
    rejects([&]{capture_preflight(capture,hardware,source,capture_paths,30*GiB,512*MiB);},"Reject inadequate verification scratch space");
    copy=capture_paths;copy[1]["type"]="ramdisk";copy[1]["diskNumbers"]=Json::array();rejects([&]{capture_preflight(capture,hardware,source,copy,30*GiB,30*GiB);},"Reject RAM scratch even if capacity is reported large");
    copy=capture_paths;copy[1]["type"]="network";rejects([&]{capture_preflight(capture,hardware,source,copy,30*GiB,30*GiB);},"Reject network scratch");
    copy=capture_paths;copy[1]["volume"]="store";
    rejects([&]{capture_preflight(capture,hardware,source,copy,12*GiB,12*GiB);},"Count output and scratch reservations together on one volume");
    check(capture_preflight(capture,hardware,source,copy,22*GiB,22*GiB)["estimatedSharedVolumeRequiredBytes"]==22*GiB,"Accept combined reservation exactly at the bound");
    copy=capture_paths;copy.erase(copy.begin()+1);rejects([&]{capture_preflight(capture,hardware,source,copy,30*GiB,30*GiB);},"Reject missing scratch resource");
    copy=base;copy["targetPolicy"]="blank-raw";check(deployment_plan(copy,hardware,wim,hash,paths)["executionAvailable"]==true,"Expose only explicit blank-disk execution");
    copy=base;copy["targetPolicy"]="wipe-anything";rejects([&]{validate_config(copy);},"Refuse unsupported destructive policy");
    copy=base;copy["bootEx"]="true";rejects([&]{validate_config(copy);},"Reject nonboolean boot certificate option");
    auto layout=gpt_layout(64*GiB,1536,4096);
    check(layout[0]["offset"]==MiB && layout[1]["length"]==16*MiB,"Use aligned EFI and MSR layout");
    unsigned long long last=0;for(const auto& p:layout){auto start=p["offset"].get<unsigned long long>(),length=p["length"].get<unsigned long long>();check(start>=last && start%MiB==0 && length%MiB==0,"Partition offsets do not overlap and stay aligned");last=start+length;}
    check(last==64*GiB-MiB,"Leave space for backup GPT");
    rejects([&]{gpt_layout(64*GiB,1536,1024);},"Refuse unsupported sectors");
    rejects([&]{gpt_layout(8*GiB,1536,512);},"Refuse undersized raw layout");
    Json raw={{"partitionStyle","raw"},{"partitionCount",0},{"allBytesZero",true}};auto physical_scratch=capture_paths[1];copy=base;copy["targetPolicy"]="blank-raw";
    validate_blank_deployment(copy,raw,physical_scratch,30*GiB,20*GiB);check(true,"Accept explicit zeroed RAW target with physical scratch");
    rejects([&]{validate_blank_deployment(base,raw,physical_scratch,30*GiB,20*GiB);},"Refuse implicit destructive execution");
    auto invalid_raw=raw;invalid_raw["partitionStyle"]="gpt";rejects([&]{validate_blank_deployment(copy,invalid_raw,physical_scratch,30*GiB,20*GiB);},"Refuse existing partitions");
    invalid_raw=raw;invalid_raw["allBytesZero"]=false;rejects([&]{validate_blank_deployment(copy,invalid_raw,physical_scratch,30*GiB,20*GiB);},"Refuse nonzero RAW data");
    auto ram_scratch=physical_scratch;ram_scratch["type"]="ramdisk";rejects([&]{validate_blank_deployment(copy,raw,ram_scratch,30*GiB,20*GiB);},"Refuse deployment verification in RAM scratch");
    rejects([&]{validate_blank_deployment(copy,raw,physical_scratch,GiB,20*GiB);},"Refuse insufficient deployment scratch");
    check(completion_action("awaiting-windows",false,true)=="checkpoint-reboot","Persist initial completion reboot even with driver problems");
    check(completion_action("reboot-pending",false,false)=="await-reboot","Do not seal on the checkpoint boot");
    check(completion_action("reboot-pending",true,true)=="needs-operator","Block OOBE for unresolved devices");
    check(completion_action("reboot-pending",true,false)=="seal-oobe","Seal only after a new boot with healthy devices");
    for(const auto* terminal:{"complete","needs-operator","failed"})check(completion_action(terminal,true,false)=="stop","Do not repeat terminal completion");
    rejects([&]{completion_action("sealing-oobe",true,false);},"Do not repeat interrupted Sysprep");
    Json package={{"schemaVersion",1},{"packageId","reviewed-qemu"},{"architecture","amd64"},{"manufacturer","QEMU"},{"model","Q35"},{"windowsBuild","26300"},
        {"review",{{"hardwareTestPassed",true},{"note","Signed catalog and private hardware test"}}},
        {"files",Json::array({{{"path","driver.inf"},{"size",100},{"sha256",hash}},{{"path","driver.cat"},{"size",100},{"sha256",hash}}})},{"infPaths",Json::array({"driver.inf"})}};
    Json facts={{"manufacturer","QEMU"},{"model","Q35"},{"build","26300"}};
    check(driver_package_matches(package,facts),"Match reviewed package to exact hardware/build");
    {
        auto large=package;large["files"]=Json::array();large["infPaths"]=Json::array();
        for(int i=0;i<501;++i){auto path="driver-"+std::to_string(i)+(i<132?".inf":".cat");large["files"].push_back({{"path",path},{"size",100},{"sha256",hash}});if(i<132)large["infPaths"].push_back(path);}
        validate_driver_manifest(large);check(true,"Accept an HP-sized reviewed package with 132 explicit INFs and 501 files");
        large["bootCriticalInfPaths"]=Json::array({"driver-131.inf"});
        auto plan=offline_driver_plan({{"offlineDriverMode","all-reviewed"}},large);
        check(plan.size()==132&&plan[0].at("inf")=="driver-131.inf"&&plan[0].at("required")==true&&plan[131].at("inf")=="driver-130.inf","Large packages retain every reviewed INF and service required drivers first");
        auto invalid=large;invalid["infPaths"].push_back("DRIVER-0.INF");rejects([&]{validate_driver_manifest(invalid);},"Large packages still reject duplicate selected INFs");
        invalid=large;invalid["infPaths"][131]="missing.inf";rejects([&]{validate_driver_manifest(invalid);},"Large packages still reject INFs outside the manifest");
        for(const auto& empty:std::vector<Json>{Json::array(),Json(),Json("driver-0.inf")}){invalid=large;invalid["infPaths"]=empty;rejects([&]{validate_driver_manifest(invalid);},"Explicit nonempty INF arrays remain required");}
        invalid=package;invalid["infPaths"]=Json::array({"driver.inf","other.inf","extra.inf"});
        bool count_error=false;try{validate_driver_manifest(invalid);}catch(const std::exception& e){count_error=std::string(e.what())=="Selected INF count exceeds driver package file count";}
        check(count_error,"Excess INF count reports the package limit instead of a missing selection");
        large.erase("bootCriticalInfPaths");large["files"]=Json::array();large["infPaths"]=Json::array();
        for(int i=0;i<10000;++i){auto path="driver-"+std::to_string(i)+".inf";large["files"].push_back({{"path",path},{"size",100},{"sha256",hash}});large["infPaths"].push_back(path);}
        validate_driver_manifest(large);check(true,"Explicit INF selections support the full server archive limit of 10000 files");
        large["files"].push_back({{"path","extra.inf"},{"size",100},{"sha256",hash}});large["infPaths"].push_back("extra.inf");
        rejects([&]{validate_driver_manifest(large);},"Driver archive file count remains bounded at 10000");
    }
    copy=package;copy["files"][1]["size"]=5*GiB;validate_driver_manifest(copy);check(true,"Accept large driver payload without fixed byte caps");
    copy["files"][1]["size"]=0;rejects([&]{validate_driver_manifest(copy);},"Driver payload sizes must stay positive");
    for(const auto* key:{"manufacturer","model","build"}){copy=facts;copy[key]="other";check(!driver_package_matches(package,copy),"Reject unrelated hardware/build package");}
    for(const auto* path:{"../driver.inf","/driver.inf","C:/driver.inf","nul.inf","x/driver.inf.","x/driver.inf ","x\\driver.inf","x//driver.inf"}){copy=package;copy["files"][0]["path"]=path;rejects([&]{validate_driver_manifest(copy);},"Reject unsafe driver paths");}
    for(const auto* path:{"Intel(R)/driver.inf","Audio + Video/driver.inf","Vendor & Co/driver.inf","OEM [v2]/driver.inf","Fran\xc3\xa7" "ais/driver.inf","\xe6\x97\xa5\xe6\x9c\xac/driver.inf"}){
        copy=package;copy["files"][0]["path"]=path;copy["infPaths"][0]=path;validate_driver_manifest(copy);check(true,"Valid vendor punctuation and Unicode driver filenames are accepted");
    }
    copy=package;auto long_path=std::string(120,'a')+"/"+std::string(120,'b')+"/driver.inf";copy["files"][0]["path"]=long_path;copy["infPaths"][0]=long_path;validate_driver_manifest(copy);check(true,"Valid nested paths are not rejected by an arbitrary 240-byte cap");
    for(const auto* path:{"x/<bad>.inf","x/bad?.inf","x/bad*.inf","x/bad|.inf","x/bad\".inf","con .inf","COM\xc2\xb9.inf"}){copy=package;copy["files"][0]["path"]=path;copy["infPaths"][0]=path;rejects([&]{validate_driver_manifest(copy);},"Reject invalid Windows characters and device aliases");}
    for(auto path:{std::string("x/")+char(0)+"driver.inf",std::string("x/")+char(31)+"driver.inf",std::string(256,'a')+".inf",std::string("x/")+char(0xc0)+char(0xaf)+"driver.inf"}){copy=package;copy["files"][0]["path"]=path;copy["infPaths"][0]=path;rejects([&]{validate_driver_manifest(copy);},"Reject controls, overlong components and malformed UTF-8");}
#ifdef _WIN32
    copy=package;copy["files"][0]["path"]="\xc3\xa9.inf";copy["infPaths"][0]="\xc3\xa9.inf";copy["files"][1]["path"]="\xc3\x89.inf";rejects([&]{validate_driver_manifest(copy);},"Reject Windows Unicode case aliases");
#endif
    copy=package;copy["review"]["hardwareTestPassed"]=false;rejects([&]{validate_driver_manifest(copy);},"Require explicit reviewed drivers");
    copy=package;copy["architecture"]="arm64";rejects([&]{validate_driver_manifest(copy);},"Reject wrong driver architecture");
    copy=package;copy["files"].push_back(copy["files"][0]);copy["files"][2]["path"]="DRIVER.INF";rejects([&]{validate_driver_manifest(copy);},"Reject case alias package entries");
    copy=package;copy["infPaths"][0]="missing.inf";rejects([&]{validate_driver_manifest(copy);},"Reject missing selected INF");
    copy=base;copy["driverManifest"]="manifest.json";rejects([&]{validate_config(copy);},"Require pinned manifest digest");
    check(confirmation_matches("serial:LAB001","serial:LAB001"),"Require exact typed target confirmation");
    for(const auto* value:{"", "serial:KEEP", " serial:LAB001", "serial:lab001"})check(!confirmation_matches("serial:LAB001",value),"Reject wrong or normalized confirmation");
    validate_server_origin("https://192.168.1.26:8443");check(true,"Accept explicit HTTPS origin");
    for(const auto* value:{"http://server","https://user:password@server","https://server/path","https://server:0","https://server:65536","https://server?token=secret"})rejects([&]{validate_server_origin(value);},"Reject unsafe server origin");
    auto id="11111111-1111-1111-1111-111111111111";
    auto catalog=Json::array({{{"id",id},{"architecture","amd64"},{"sha256",hash},{"image_index",1}}});
    check(select_server_image(catalog,base,id)["id"]==id,"Bind server job to approved image SHA/index");
    for(const auto* field:{"sha256","image_index","architecture"}){copy=catalog;if(std::string(field)=="image_index")copy[0][field]=2;else copy[0][field]="wrong";rejects([&]{select_server_image(copy,base,id);},"Reject mismatched server catalog image");}
    rejects([&]{select_server_image(catalog,base,"../escape");},"Reject invalid image IDs");
    copy=catalog;copy.push_back(copy[0]);rejects([&]{select_server_image(copy,base,id);},"Reject ambiguous catalog identity");
    Json pxe={{"profile",{{"enabled",true},{"image_id",id},{"image_sha256",hash},{"image_index",1},{"windows_build","26300"},{"driver_id",nullptr}}},
        {"image",{{"id",id},{"architecture","amd64"},{"sha256",hash},{"image_index",1},{"size",8*GiB},{"selected_image",{{"index",1},{"name","Pro"},{"total_bytes",28*GiB},{"architecture","9"},{"edition","Professional"},{"build","26300"}}}}},{"driverManifest",nullptr}};
    check(pxe_image_metadata(pxe)["downloadBytes"]==8*GiB,"Pin approved PXE cache size before disk writes");
    copy=pxe;copy["profile"]["enabled"]=false;rejects([&]{pxe_image_metadata(copy);},"Refuse disabled PXE profile");
    for(const auto* field:{"id","sha256","architecture","image_index"}){copy=pxe;if(std::string(field)=="image_index")copy["image"][field]=2;else copy["image"][field]="wrong";rejects([&]{pxe_image_metadata(copy);},"Refuse PXE image substitution");}
    copy=pxe;copy["image"]["size"]=-1;rejects([&]{pxe_image_metadata(copy);},"Refuse negative PXE file size");
    copy=pxe;copy["image"]["selected_image"]["build"]="other";rejects([&]{pxe_image_metadata(copy);},"Refuse changed PXE Windows build");
    copy=pxe;copy["profile"]["driver_id"]="missing";rejects([&]{pxe_image_metadata(copy);},"Refuse omitted required PXE drivers");
    copy=base;copy["targetPolicy"]="format-selected-disk";
    Json occupied={{"partitionStyle","gpt"},{"partitionCount",4},{"allBytesZero",false}};
    validate_blank_deployment(copy,occupied,physical_scratch,30*GiB,20*GiB);check(true,"Explicit format accepts occupied GPT fixture");
    occupied["partitionStyle"]="mbr";validate_blank_deployment(copy,occupied,physical_scratch,30*GiB,20*GiB);check(true,"Explicit format accepts occupied MBR fixture");
    occupied["partitionStyle"]="unknown";rejects([&]{validate_blank_deployment(copy,occupied,physical_scratch,30*GiB,20*GiB);},"Format still rejects unknown layouts");
    bad_policy=automatic;bad_policy["targetDiskPolicy"]="unsupported";rejects([&]{validate_pxe_automation(bad_policy,candidates);},"Reject malformed server disk policies");
    auto skipped=driver_install_result(259);
    check(skipped.at("status")=="skipped-no-match-or-better-driver" && skipped.at("changed")==false && skipped.at("rebootRequired")==false,"PnPUtil 259 is a completed skip without reboot");
    check(driver_install_result(0).at("changed")==true && driver_install_result(0).at("rebootRequired")==false,"Successful driver installation records a change");
    check(driver_install_result(3010).at("rebootRequired")==true,"Driver reboot-required result is preserved");
    for(int code:{5,87,1460,1641})rejects([&]{driver_install_result(code);},"Actual driver errors and unexpected reboot initiation remain failures");
    int installed_count=0,skipped_count=0;bool reboot=false;
    for(int code:{259,0,3010,259}){auto result=driver_install_result(code);installed_count+=result.at("changed").get<bool>();skipped_count+=!result.at("changed").get<bool>();reboot=reboot||result.at("rebootRequired").get<bool>();}
    check(installed_count==2 && skipped_count==2 && reboot,"Continue a mixed driver pass after non-applicable INFs");
    check(driver_fallback_action(false,0,2,"")=="seal-oobe","Healthy devices skip Windows Update");
    check(driver_fallback_action(true,0,2,"")=="windows-update","Unresolved devices start bounded driver fallback");
    check(driver_fallback_action(true,1,2,"succeeded")=="windows-update","Successful attempt can retry after verified reboot");
    for(const auto* status:{"failed","timed-out","no-applicable-drivers","interrupted"})check(driver_fallback_action(true,1,2,status)=="needs-operator","Failed or interrupted operations do not repeat");
    check(driver_fallback_action(true,2,2,"succeeded")=="needs-operator","Bound driver update retries");
    rejects([&]{driver_fallback_action(true,1,4,"");},"Reject unbounded driver attempts");
    Json unresolved=Json::array({{{"hardwareIds",Json::array({"PCI\\VEN_1234&DEV_5678"})},{"compatibleIds",Json::array({"PCI\\CC_0200"})}}});
    check(driver_update_matches("pci\\ven_1234&dev_5678",unresolved),"Match driver hardware IDs case insensitively");
    check(driver_update_matches("PCI\\CC_0200",unresolved),"Match unresolved compatible IDs");
    check(!driver_update_matches("PCI\\VEN_4321",unresolved),"Exclude healthy or unrelated driver updates");
    check(!driver_update_matches("",unresolved),"Do not match missing update IDs");
    copy=base;copy["windowsUpdate"]={{"timeoutSeconds",60},{"maxAttempts",1}};validate_config(copy);
    copy["windowsUpdate"]["timeoutSeconds"]=0;rejects([&]{validate_config(copy);},"Reject unbounded Windows Update timeouts");
    copy=package;copy["bootCriticalInfPaths"]=Json::array({"driver.inf"});validate_driver_manifest(copy);
    copy["bootCriticalInfPaths"]=Json::array({"missing.inf"});rejects([&]{validate_driver_manifest(copy);},"Boot-critical drivers must belong to reviewed package");
    auto attempts=Json::array({driver_attempt_result(259),driver_attempt_result(5),driver_attempt_result(0),driver_attempt_result(3010)});
    auto totals=driver_pass_summary(attempts);
    check(totals.at("attempted")==4&&totals.at("failed")==1&&totals.at("installed")==2&&totals.at("skipped")==1,"A completed failure does not discard later driver results");
    check(totals.at("rebootRequired")==true&&!totals.at("installationMayBeInProgress").get<bool>(),"Aggregate completed results preserve reboot requirements");
    for(int code:{1460,1641})check(driver_attempt_result(code).at("installationMayBeInProgress")==true,"Timeout and unexpected reboot stop subsequent driver installation");
    check(driver_attempt_result(5).at("status")=="failed"&&!driver_attempt_result(5).at("changed").get<bool>(),"Access failure is recorded without a successful change");
    rejects([&]{driver_pass_summary(Json::array({{{"status","unknown"}}}));},"Unknown results cannot produce a healthy summary");
    Json export_hardware={{"manufacturer","Example"},{"model","Test PC"},{"build","26300"}},export_devices={{"presentDeviceCount",4},{"problems",Json::array()}};
    validate_driver_export(export_hardware,export_devices);check(true,"Healthy identified PCs may collect reusable drivers");
    auto unhealthy=export_devices;unhealthy["problems"].push_back({{"problemCode",28}});
    validate_driver_export(export_hardware,unhealthy);check(true,"Unresolved devices do not prevent collecting available drivers");
    unhealthy=export_devices;unhealthy["presentDeviceCount"]=0;rejects([&]{validate_driver_export(export_hardware,unhealthy);},"Failed or empty inventory blocks collection");
    auto identity=export_hardware;identity["model"]="";rejects([&]{validate_driver_export(identity,export_devices);},"Missing model cannot produce a reusable export");
    Json finish_state={{"stage","driver-menu"},{"driverVerifiedBoot","current"},{"drivers",unhealthy},{"pendingDriverUpload",{{"id","interrupted"}}}};
    check(driver_finish_action(finish_state,"current")=="seal-oobe","Technician can finish with missing drivers and an idle unfinished upload");
    for(const auto* flag:{"driverRebootRequired","driverInstallationMayBeInProgress"}){auto pending=finish_state;pending[flag]=true;check(driver_finish_action(pending,"current")=="checkpoint-reboot","Finish setup handles a required restart before sealing");}
    check(driver_finish_action(finish_state,"new")=="checkpoint-reboot","An unverified boot receives a finish checkpoint");
    for(const auto* stage:{"awaiting-windows","needs-operator","failed"}){auto idle=finish_state;idle["stage"]=stage;check(driver_finish_available(idle)&&driver_finish_action(idle,"current")=="seal-oobe","Idle pending or failed status does not block a technician finish");}
    auto sealed=finish_state;sealed["history"]=Json::array({{{"stage","sealing-oobe"}}});check(driver_finish_available(sealed)&&driver_finish_action(sealed,"current")=="seal-oobe","An explicit technician Finish can recover an earlier stopped sealing attempt");
    sealed=finish_state;sealed["sealedImageState"]="IMAGE_STATE_SPECIALIZE_RESEAL_TO_OOBE";check(driver_finish_available(sealed),"Recorded image state does not permanently disable Finish; live Windows state controls sealing");
    for(const auto* stage:{"driver-menu","needs-operator","failed"}){Json repairable={{"driverConsole",true},{"stage",stage}};check(!completion_report_terminal(repairable),"Repairable console evidence preserves the credential for technician Finish");}
    Json delivery={{"stage","complete"},{"deliveryStatus","pending"}};
    check(!completion_shutdown_ready(delivery)&&driver_finish_available(delivery)&&driver_finish_action(delivery,"")=="retry-report","Sealed pending delivery retries only the report, without a reboot or reseal");
    for(const auto* status:{"pending-terminal-report","pending","unknown",""}){delivery["deliveryStatus"]=status;check(!completion_shutdown_ready(delivery),"Unacknowledged final report never permits silent automatic shutdown");}
    for(const auto* status:{"delivered","local-only"}){delivery["deliveryStatus"]=status;check(completion_shutdown_ready(delivery)&&!driver_finish_available(delivery),"Delivered or explicitly local-only completion permits shutdown");}
    delivery["finishOptions"]={{"cleanup_client",true}};
    check(completion_report_accepted(delivery)&&!completion_shutdown_ready(delivery)&&driver_finish_available(delivery)&&driver_finish_action(delivery,"")=="retry-report","Accepted report with unscheduled cleanup retains Finish without resealing");
    delivery["cleanupScheduled"]=true;check(completion_shutdown_ready(delivery)&&!driver_finish_available(delivery),"Shutdown waits for cleanup task registration");
    delivery["stage"]="failed";check(!completion_shutdown_ready(delivery),"Failed seal cannot be treated as successful completion");
    check(completion_report_terminal({{"stage","failed"}}),"Legacy non-console terminal report delivery remains unchanged");
    check(completion_report_terminal({{"driverConsole",true},{"stage","complete"}}),"Completed technician sealing delivers its immutable final report");
    check(!completion_report_terminal({{"driverConsole",true},{"stage","failed"},{"history",Json::array({{{"stage","sealing-oobe"}}})}}),"A failed sealing attempt preserves its report credential until technician Finish succeeds");
    check(!completion_report_terminal({{"driverConsole",true},{"stage","failed"},{"sealedImageState","IMAGE_STATE_SPECIALIZE_RESEAL_TO_AUDIT"}}),"An unsuccessful recorded seal is repairable");
    for(const auto* stage:{"ready-for-oobe","sealing-oobe","completing"}){auto stopped=finish_state;stopped["stage"]=stage;check(driver_finish_available(stopped),"Stopped completion checkpoint permits explicit Finish after obtaining operation ownership");}
    for(const auto* audit:{"IMAGE_STATE_SPECIALIZE_RESEAL_TO_AUDIT","IMAGE_STATE_GENERALIZE_RESEAL_TO_AUDIT"})check(completion_seal_action(audit)=="seal-oobe","Current Audit state requires actual OOBE sealing");
    check(completion_seal_action("IMAGE_STATE_SPECIALIZE_RESEAL_TO_OOBE")=="finalize-oobe","An actual OOBE checkpoint finalizes without repeating Sysprep");
    for(const auto* other:{"IMAGE_STATE_COMPLETE","IMAGE_STATE_UNDEPLOYABLE","","IMAGE_STATE_GENERALIZE_RESEAL_TO_OOBE"})rejects([&]{completion_seal_action(other);},"Unrecognized/customer/generalized state cannot pretend to be the provisioned OOBE checkpoint");
    for(const auto* stage:{"installing-drivers","updating-drivers","complete"}){auto active=finish_state;active["stage"]=stage;rejects([&]{driver_finish_action(active,"current");},"Finish cannot overlap or repeat a running operation");}
    check(offline_driver_plan(Json::object(),package).empty(),"Existing manual configurations retain boot-critical-only servicing");
    copy=package;copy["bootCriticalInfPaths"]=Json::array({"driver.inf"});auto selected=offline_driver_plan({{"offlineDriverMode","all-reviewed"}},copy);
    check(selected.size()==1&&selected[0].at("required")==true,"All-reviewed servicing retains required boot-driver handling");
    selected=offline_driver_plan({{"offlineDriverMode","all-reviewed"}},package);check(selected.size()==1&&selected[0].at("required")==false,"Other reviewed drivers prepare offline and can defer to Windows");
    check(offline_driver_plan({{"offlineDriverMode","all-reviewed"}},Json()).empty(),"Missing package does not scan arbitrary driver directories");
    rejects([&]{offline_driver_plan({{"offlineDriverInfPaths",Json::array({"missing.inf"})}},package);},"Explicit offline selections must remain inside the pinned package");
    auto deployment=base;deployment["offlineDriverMode"]="all-reviewed";validate_config(deployment);check(true,"Support all-reviewed offline mode");
    deployment["offlineDriverMode"]="anything";rejects([&]{validate_config(deployment);},"Unknown offline policy is refused");
    Json binding={{"id","saved-upload"},{"size",1073742282ULL},{"sha256",std::string(64,'a')}};
    Json remote={{"id","saved-upload"},{"spec",{{"kind","drivers"},{"size",1073742282ULL},{"sha256",std::string(64,'a')}}},{"status","receiving"},{"received",8388608}};
    check(driver_upload_offset(binding,remote)==8388608,"Resume a large upload from independently verified server progress");
    auto changed=remote;changed["spec"]["sha256"]=std::string(64,'b');rejects([&]{driver_upload_offset(binding,changed);},"Never append another archive to a saved upload");
    changed=remote;changed["received"]=1073742283ULL;rejects([&]{driver_upload_offset(binding,changed);},"Reject out-of-range resume position");
    changed=remote;changed["received"]=-1;rejects([&]{driver_upload_offset(binding,changed);},"Reject negative resume position");
    changed=remote;changed["status"]="cancelled";rejects([&]{driver_upload_offset(binding,changed);},"Cancelled upload requires a new transfer");
    changed=remote;changed["status"]="ready";rejects([&]{driver_upload_offset(binding,changed);},"Ready upload must contain the entire archive");
    changed["received"]=binding.at("size");check(driver_upload_offset(binding,changed)==binding.at("size"),"Completed remote upload can recover a lost final acknowledgement");
    std::cout<<checks<<" safety and metadata checks passed\n";return 0;
}catch(const std::exception& e){std::cerr<<"FAILED: "<<e.what()<<"\n";return 1;}}
