#include <repxe/core.hpp>
#include <iostream>
#include <stdexcept>
using namespace repxe;
int main(){
    int checks=0;auto expect=[&](bool value){++checks;if(!value)throw std::runtime_error("Hardware inventory check failed: "+std::to_string(checks));};
    auto add=[](std::vector<unsigned char>& table,std::vector<unsigned char> record,const std::vector<std::string>& strings){record[1]=static_cast<unsigned char>(record.size());table.insert(table.end(),record.begin(),record.end());for(const auto& s:strings){table.insert(table.end(),s.begin(),s.end());table.push_back(0);}if(strings.empty())table.push_back(0);table.push_back(0);};
    std::vector<unsigned char> table;std::vector<unsigned char> system(27);system[0]=1;system[4]=1;system[5]=2;system[7]=3;system[25]=4;system[26]=5;
    for(int i=8;i<24;++i)system[i]=static_cast<unsigned char>(i-7);
    add(table,system,{"Example","Laptop","SYS-123","SKU-456","Portable"});
    std::vector<unsigned char> chassis(9);chassis[0]=3;chassis[8]=1;add(table,chassis,{"ASSET-42"});
    std::vector<unsigned char> cpu(48);cpu[0]=4;cpu[4]=1;cpu[7]=2;cpu[16]=3;cpu[24]=64;cpu[20]=0x10;cpu[21]=0x0e;cpu[35]=255;cpu[42]=0;cpu[43]=2;cpu[37]=255;cpu[46]=0;cpu[47]=4;add(table,cpu,{"Socket","CPU maker","Test processor"});
    std::vector<unsigned char> memory(92);memory[0]=17;memory[12]=0xff;memory[13]=0x7f;memory[28]=0;memory[29]=0x80;memory[16]=1;memory[18]=34;memory[23]=2;memory[24]=3;memory[26]=4;memory[32]=0xff;memory[33]=0xff;memory[88]=0x70;memory[89]=0x11;memory[90]=1;add(table,memory,{"DIMM0","RAM maker","RAM-123","PART-123"});
    std::vector<unsigned char> battery(22);battery[0]=22;battery[8]=1;battery[5]=2;battery[7]=3;battery[10]=50;battery[21]=1000/100;add(table,battery,{"Pack","Battery maker","BAT-123"});
    add(table,{127,4,0,0},{});
    auto facts=smbios_specs(table);expect(facts["system"]["serial"]=="SYS-123");expect(facts["system"]["asset_tag"]=="ASSET-42");expect(facts["system"]["uuid"]=="04030201-0605-0807-090a-0b0c0d0e0f10");
    expect(smbios_specs(table,false)["system"]["uuid"]=="01020304-0506-0708-090a-0b0c0d0e0f10");expect(facts["processors"][0]["cores"]==512);expect(facts["processors"][0]["threads"]==1024);expect(facts["processors"][0]["max_speed_mhz"]==3600);
    expect(facts["memory"]["modules"][0]["size_bytes"]==32ULL*1024*1024*1024);expect(facts["memory"]["modules"][0]["type"]=="DDR5");expect(facts["memory"]["modules"][0]["speed_mts"]==70000);expect(facts["memory"]["modules"][0]["serial"]=="RAM-123");expect(facts["batteries"][0]["design_capacity_mwh"]==500);
    for(size_t i=0;i<table.size();++i){auto partial=smbios_specs(std::vector<unsigned char>(table.begin(),table.begin()+i));expect(partial.is_object());}
    expect(smbios_specs({1,2,0,0,0,0})["warnings"].size()==1);expect(smbios_specs({1,8,0,0})["warnings"].size()==1);
    auto unknown=system;unknown[7]=250;unknown[4]=1;for(int i=8;i<24;++i)unknown[i]=0;std::vector<unsigned char> invalid;add(invalid,unknown,{"To Be Filled By O.E.M."});auto empty=smbios_specs(invalid);expect(empty["system"]["serial"]=="");expect(empty["system"]["manufacturer"]=="");expect(!empty["system"].contains("uuid"));
    std::cout<<checks<<" bounded firmware inventory checks passed\n";
}
