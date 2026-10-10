#include <repxe/core.hpp>
#include <pugixml.hpp>
#include <regex>
#include <algorithm>
#include <cctype>
#include <set>
#include <sstream>
#include <stdexcept>
#include "cleanup_script.hpp"
namespace repxe {
namespace {
void check(bool condition,const char* message){if(!condition)throw std::runtime_error(message);}
pugi::xml_node component(pugi::xml_node settings,const char* name){
    for(auto c:settings.children("component"))if(std::string(c.attribute("name").value())==name)return c;
    auto c=settings.append_child("component");
    for(auto [key,value]:std::vector<std::pair<const char*,const char*>>{{"name",name},{"processorArchitecture","amd64"},{"publicKeyToken","31bf3856ad364e35"},{"language","neutral"},{"versionScope","nonSxS"}})c.append_attribute(key)=value;
    return c;
}
void text(pugi::xml_node parent,const char* name,const std::string& value){parent.append_child(name).text().set(value.c_str());}
}
void validate_finish_options(const Json& o){
    check(o.is_object(),"Finish options must be an object");
    const std::set<std::string> keys={"cleanup_client","save_report_desktop","setup_mode","account_name","account_password","locale","keyboard","time_zone"};
    for(auto it=o.begin();it!=o.end();++it)check(keys.contains(it.key()),"Unknown Finish option");
    for(auto key:{"cleanup_client","save_report_desktop"})check(o.contains(key)&&o.at(key).is_boolean(),"Invalid Finish boolean");
    for(auto key:{"setup_mode","account_name","account_password","locale","keyboard","time_zone"})check(o.contains(key)&&o.at(key).is_string(),"Missing Finish setting");
    auto name=o.at("account_name").get<std::string>(),password=o.at("account_password").get<std::string>();
    check(o.at("setup_mode")=="customer-oobe"||o.at("setup_mode")=="local-account","Invalid customer setup mode");
    check(std::regex_match(name,std::regex("[A-Za-z][A-Za-z0-9 _-]{0,19}"))&&name.back()!=' ',"Invalid local account name");
    std::string lower=name;for(auto& c:lower)c=static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    check(!std::set<std::string>{"administrator","guest","defaultaccount","wdagutilityaccount","re-pxe","repxe"}.contains(lower),"Reserved local account name");
    check(password.size()<=512 && std::none_of(password.begin(),password.end(),[](unsigned char c){return c<32;}),"Invalid account password");
    check(std::regex_match(o.at("locale").get<std::string>(),std::regex("[a-z]{2,3}-[A-Z]{2}")),"Invalid locale");
    check(std::regex_match(o.at("keyboard").get<std::string>(),std::regex("[0-9a-fA-F]{4}:[0-9a-fA-F]{8}")),"Invalid keyboard");
    check(std::regex_match(o.at("time_zone").get<std::string>(),std::regex("[A-Za-z0-9 ()+-]{1,64}")),"Invalid time zone");
}
std::string finish_answer_xml(const std::string& original,const Json& o){
    validate_finish_options(o);validate_answer_xml(original,"oobe");
    if(o.at("setup_mode")=="customer-oobe")return original;
    pugi::xml_document doc;check(doc.load_string(original.c_str()),"Read final answer XML");auto root=doc.child("unattend");
    if(!root.attribute("xmlns:wcm"))root.append_attribute("xmlns:wcm")="http://schemas.microsoft.com/WMIConfig/2002/State";
    pugi::xml_node settings;for(auto s:root.children("settings"))if(std::string(s.attribute("pass").value())=="oobeSystem")settings=s;
    auto shell=component(settings,"Microsoft-Windows-Shell-Setup");
    // Routine customer choices replace these settings only in the pinned copy.
    for(auto name:{"UserAccounts","AutoLogon","OOBE","TimeZone"})while(shell.child(name))shell.remove_child(name);
    text(shell,"TimeZone",o.at("time_zone"));auto accounts=shell.append_child("UserAccounts").append_child("LocalAccounts");
    auto account=accounts.append_child("LocalAccount");account.append_attribute("wcm:action")="add";
    auto password=account.append_child("Password");text(password,"Value",o.at("account_password"));text(password,"PlainText","true");
    text(account,"Name",o.at("account_name"));text(account,"DisplayName",o.at("account_name"));text(account,"Group","Administrators");
    auto login=shell.append_child("AutoLogon");password=login.append_child("Password");text(password,"Value",o.at("account_password"));text(password,"PlainText","true");text(login,"Username",o.at("account_name"));text(login,"Enabled","true");text(login,"LogonCount","1");
    auto oobe=shell.append_child("OOBE");for(auto name:{"HideEULAPage","HideOEMRegistrationScreen","HideOnlineAccountScreens","HideWirelessSetupInOOBE"})text(oobe,name,"true");text(oobe,"ProtectYourPC","3");
    auto first=shell.child("FirstLogonCommands");if(!first)first=shell.append_child("FirstLogonCommands");
    std::set<int> orders;for(auto c:first.children("SynchronousCommand"))orders.insert(c.child("Order").text().as_int());int order=1;while(orders.contains(order))++order;check(order<=500,"No available first logon command order");
    auto command=first.append_child("SynchronousCommand");command.append_attribute("wcm:action")="add";text(command,"Order",std::to_string(order));text(command,"Description","Disable one-time automatic sign-in");
    text(command,"CommandLine",R"CMD(cmd.exe /c reg.exe add "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon" /v AutoLogonCount /t REG_DWORD /d 0 /f & reg.exe add "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon" /v AutoAdminLogon /t REG_SZ /d 0 /f & reg.exe delete "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon" /v DefaultPassword /f)CMD");
    auto international=component(settings,"Microsoft-Windows-International-Core");for(auto name:{"InputLocale","SystemLocale","UILanguage","UserLocale"})while(international.child(name))international.remove_child(name);
    text(international,"InputLocale",o.at("keyboard"));for(auto name:{"SystemLocale","UILanguage","UserLocale"})text(international,name,o.at("locale"));
    std::ostringstream output;doc.save(output,"  ",pugi::format_default,pugi::encoding_utf8);auto result=output.str();validate_answer_xml(result,"oobe");return result;
}
std::string cleanup_script(){return installed_cleanup_script;}
}
