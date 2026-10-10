#include <repxe/core.hpp>
#include <iostream>
#include <stdexcept>
using namespace repxe;
int main(){
    int checks=0;auto check=[&](bool value){++checks;if(!value)throw std::runtime_error("Battery check failed: "+std::to_string(checks));};
    auto report=[](const std::string& battery){return "<BatteryReport xmlns='http://schemas.microsoft.com/battery/2012'><Batteries>"+battery+"</Batteries></BatteryReport>";};
    auto a="<Battery><Id>Internal</Id><Manufacturer>Maker</Manufacturer><SerialNumber>BAT-123</SerialNumber><DesignCapacity>50000</DesignCapacity><FullChargeCapacity>41234</FullChargeCapacity><CycleCount>205</CycleCount></Battery>";
    auto b="<Battery><Id>Second</Id><DesignCapacity>24000</DesignCapacity><FullChargeCapacity>24600</FullChargeCapacity><CycleCount>0</CycleCount></Battery>";
    auto result=powercfg_battery_health(report(a));check(result["status"]=="available");check(result["batteries"][0]["health_percent"]==82.47);check(result["batteries"][0]["cycle_count"]==205);check(result["batteries"][0]["serial"]=="BAT-123");
    result=powercfg_battery_health(report(std::string(a)+b));check(result["batteries"].size()==2);check(result["batteries"][1]["health_percent"]==102.5);check(result["batteries"][1]["cycle_count"]==0);
    result=powercfg_battery_health("<p:BatteryReport xmlns:p='test'><p:Batteries><p:Battery><p:DesignCapacity>3</p:DesignCapacity><p:FullChargeCapacity>2</p:FullChargeCapacity></p:Battery></p:Batteries></p:BatteryReport>");check(result["batteries"][0]["health_percent"]==66.67);
    for(const auto* value:{"","0","-1","Unknown","4294967295","50000x","18446744073709551616"}){
        result=powercfg_battery_health(report("<Battery><DesignCapacity>"+std::string(value)+"</DesignCapacity><FullChargeCapacity>40000</FullChargeCapacity></Battery>"));check(result["status"]=="unavailable");check(result["batteries"][0]["health_percent"].is_null());check(!result["warnings"].empty());
    }
    result=powercfg_battery_health(report("<Battery><DesignCapacity>50000</DesignCapacity></Battery>"));check(result["batteries"][0]["full_charge_capacity_mwh"].is_null());check(result["batteries"][0]["cycle_count"].is_null());
    result=powercfg_battery_health(report(""));check(result["status"]=="unavailable");check(!result["warnings"].empty());
    for(const auto& invalid:{std::string("<BatteryReport>"),std::string("<wrong/>"),std::string("<!DOCTYPE BatteryReport [<!ENTITY x '1'>]><BatteryReport/>"),std::string(16*1024*1024+1,'x')}){
        bool rejected=false;try{powercfg_battery_health(invalid);}catch(const std::exception&){rejected=true;}check(rejected);
    }
    std::string many;for(int i=0;i<17;++i)many+=a;bool rejected=false;try{powercfg_battery_health(report(many));}catch(const std::exception&){rejected=true;}check(rejected);
    check(battery_health_script().find("/batteryreport /output")!=std::string::npos);
    std::cout<<checks<<" battery evidence checks passed\n";
}
