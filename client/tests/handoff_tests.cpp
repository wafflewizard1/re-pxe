#include <repxe/core.hpp>
#include <pugixml.hpp>
#include <iostream>
#include <stdexcept>
using namespace repxe;
void check(bool value,const char* message){if(!value)throw std::runtime_error(message);}
int main(){try{
    Json options={{"cleanup_client",true},{"save_report_desktop",true},{"setup_mode","customer-oobe"},{"account_name","Customer"},{"account_password","p&<\"word"},{"locale","en-US"},{"keyboard","0409:00000409"},{"time_zone","New Zealand Standard Time"}};
    const auto original=R"(<unattend xmlns="urn:schemas-microsoft-com:unattend"><settings pass="oobeSystem"><component name="Microsoft-Windows-Deployment" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS"><Reseal><Mode>OOBE</Mode></Reseal></component></settings></unattend>)";
    check(finish_answer_xml(original,options)==original,"Customer OOBE must preserve original answer");
    options["setup_mode"]="local-account";auto xml=finish_answer_xml(original,options);pugi::xml_document doc;check(doc.load_string(xml.c_str()),"Generated account answer must parse");
    auto shell=doc.select_node("//component[@name='Microsoft-Windows-Shell-Setup']").node();
    check(std::string(shell.child("UserAccounts").child("LocalAccounts").child("LocalAccount").child("Password").child_value("Value"))=="p&<\"word","Password must roundtrip escaped XML");
    check(std::string(shell.child("AutoLogon").child_value("Username"))=="Customer","Selected local account is automatic sign-in target");
    check(shell.child("FirstLogonCommands").child("SynchronousCommand").child("CommandLine").text().as_string()&&xml.find("DefaultPassword")!=std::string::npos&&xml.find("AutoAdminLogon")!=std::string::npos,"One-time logon must clear persistent login credentials");
    check(xml.find("SkipMachineOOBE")==std::string::npos,"Do not use unsupported OOBE skip flags");
    for(auto name:{"Administrator","Guest","DefaultAccount","Customer ","Bad/Name","$(command)","TooLongAccountNameTwenty"}){auto invalid=options;invalid["account_name"]=name;bool rejected=false;try{validate_finish_options(invalid);}catch(...){rejected=true;}check(rejected,"Unsafe or reserved account name must fail");}
    for(auto key:{"cleanup_client","save_report_desktop"}){auto invalid=options;invalid[key]="true";bool rejected=false;try{validate_finish_options(invalid);}catch(...){rejected=true;}check(rejected,"Finish booleans cannot be coerced");}
    auto script=cleanup_script();check(script.find("ValidateOnly")!=std::string::npos&&script.find("ReparsePoint")!=std::string::npos&&script.find("acknowledgement.sha256")!=std::string::npos,"Cleanup must protect pending data and redirected paths");
    std::cout<<"PASS: customer handoff XML, account validation and cleanup guards\n";return 0;
}catch(const std::exception& e){std::cerr<<e.what()<<'\n';return 1;}}
