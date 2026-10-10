#include "repxe/core.hpp"
#include <iostream>
#include <stdexcept>
using namespace repxe;
int main(){
 unsigned checks=0;
 auto check=[&](bool ok){++checks;if(!ok)throw std::runtime_error("Progress parser check failed: "+std::to_string(checks));};
 InstallProgress p;
 check(p.feed("Deployment Image Servicing and Management tool\r\nVersion: 10.0.26100.1\r\n").empty());
 check(p.feed("[==== 1").empty());check(p.feed("2.3% ====]\r")==std::vector<int>{12});
 check(p.feed("[12.9%]\r[11.0%]\r[12.0%]").empty());
 check(p.feed("[15,2%]\r[99.9%]\r[100.0%]")==std::vector<int>({15,99,100}));
 check(p.feed("[100%]").empty());
 for(size_t split=0;split<=14;++split){InstallProgress q;std::string bytes;for(char c:std::string("[ 47.6% ]")){bytes+=c;bytes+='\0';}auto a=q.feed(std::string_view(bytes).substr(0,split));auto b=q.feed(std::string_view(bytes).substr(split));a.insert(a.end(),b.begin(),b.end());check(a==std::vector<int>{47});}
 for(auto bad:{"[-1%]","[+1%]","[101%]","[100.1%]","[1000%]","[1.2.3%]","[.5%]","[123456789%]","[abc42%]"}){InstallProgress q;check(q.feed(bad).empty());check(q.feed("[42%]")==std::vector<int>{42});}
 InstallProgress large;check(large.feed(std::string(1024*1024,'x')).empty());check(large.feed(" [27.0%]")==std::vector<int>{27});
 InstallProgress zero;check(zero.feed("[0.0%]")==std::vector<int>{0});
 std::cout<<checks<<" installation progress checks passed\n";
}
