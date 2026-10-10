#include <repxe/state_file.hpp>
#include <fstream>
#include <iostream>
#include <thread>
#include <vector>

std::string read(const std::filesystem::path& p){std::ifstream f(p,std::ios::binary);return {std::istreambuf_iterator<char>(f),{}};}
int main(){
    auto root=std::filesystem::temp_directory_path()/(L"repxeStateFixture-"+std::to_wstring(GetCurrentProcessId()));
    if(std::filesystem::exists(root))return 2;std::filesystem::create_directory(root);
    try{
        auto state=root/L"completion.json",stale=root/L"completion.json.tmp";
        std::ofstream(stale)<<"retained interrupted attempt";
        repxe::persist_state_file(state,"before");
        repxe::persist_state_file(state,"after");
        if(read(state)!="after"||read(stale)!="retained interrupted attempt")throw std::runtime_error("Stale state blocks recovery or was changed");
        auto hold=CreateFileW(state.c_str(),GENERIC_READ,FILE_SHARE_READ,nullptr,OPEN_EXISTING,0,nullptr);
        if(hold==INVALID_HANDLE_VALUE)throw std::runtime_error("Fixture lock");
        bool rejected=false;try{repxe::persist_state_file(state,"unpublished");}catch(const std::exception&){rejected=true;}
        CloseHandle(hold);
        if(!rejected||read(state)!="after")throw std::runtime_error("Failed publication changed committed state");
        unsigned files=0;for(const auto& p:std::filesystem::directory_iterator(root)){(void)p;++files;}
        if(files!=2)throw std::runtime_error("Failed writer left its own temporary file");
        repxe::persist_state_file(state,"recovered");
        if(read(state)!="recovered")throw std::runtime_error("Retry failed");
        std::filesystem::remove_all(root);std::cout<<"PASS: stale staging evidence retained; replacement, failed publication and retry checked\n";return 0;
    }catch(const std::exception& e){std::cerr<<e.what()<<"\nFixture preserved: "<<root<<"\n";return 1;}
}
