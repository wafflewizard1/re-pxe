#include <repxe/core.hpp>
#include <algorithm>
#include <array>
#include <iomanip>
#include <sstream>

namespace repxe {
// Decode only bounded formatted structures and their terminated string sets.
// Layouts follow DMTF DSP0134. No physical memory or disk access is used here.
Json smbios_specs(const std::vector<unsigned char>& data, bool modern_uuid) {
    Json result={{"system",Json::object()},{"processors",Json::array()},
        {"memory",{{"total_bytes",0ULL},{"modules",Json::array()}}},
        {"firmware",Json::object()},{"board",Json::object()},
        {"batteries",Json::array()},{"warnings",Json::array()}};
    if(data.size()>8*1024*1024){result["warnings"].push_back("Firmware table exceeded the inventory bound");return result;}
    size_t position=0,records=0;
    while(position<data.size() && records++<4096){
        if(data.size()-position<4){result["warnings"].push_back("Truncated firmware structure");break;}
        auto type=data[position];size_t length=data[position+1];
        if(length<4 || length>data.size()-position){result["warnings"].push_back("Invalid firmware structure length");break;}
        auto end=position+length;while(end+1<data.size() && !(data[end]==0 && data[end+1]==0))++end;
        if(end+1>=data.size()){result["warnings"].push_back("Unterminated firmware string table");break;}
        auto byte=[&](size_t offset)->unsigned{return offset<length?data[position+offset]:0;};
        auto word=[&](size_t offset)->unsigned{return offset+1<length?byte(offset)|(byte(offset+1)<<8):0;};
        auto dword=[&](size_t offset)->unsigned long long{return offset+3<length?word(offset)|(static_cast<unsigned long long>(word(offset+2))<<16):0;};
        auto text=[&](size_t offset){
            unsigned index=byte(offset);if(!index)return std::string{};
            size_t at=position+length;for(unsigned n=1;n<index && at<end;++n){while(at<end && data[at])++at;++at;}
            if(at>=end)return std::string{};auto stop=at;while(stop<end && data[stop])++stop;
            std::string value;for(size_t i=at;i<stop && value.size()<256;++i)if(data[i]>=32)value+=static_cast<char>(data[i]);
            while(!value.empty() && value.back()==' ')value.pop_back();auto first=value.find_first_not_of(' ');value=first==std::string::npos?"":value.substr(first);
            if(value=="To Be Filled By O.E.M." || value=="Default string" || value=="Not Specified" || value=="Unknown")return std::string{};
            return Json::parse(Json(value).dump(-1,' ',false,Json::error_handler_t::replace)).get<std::string>();
        };
        if(type==0)result["firmware"]={{"manufacturer",text(4)},{"version",text(5)},{"release_date",text(8)}};
        if(type==1){
            auto& system=result["system"];system={{"manufacturer",text(4)},{"model",text(5)},{"serial",text(7)},{"sku",text(25)},{"family",text(26)}};
            if(length>=24){bool zero=true,ones=true;for(size_t i=8;i<24;++i){zero=zero&&byte(i)==0;ones=ones&&byte(i)==255;}
                if(!zero&&!ones){std::ostringstream uuid;uuid<<std::hex<<std::setfill('0');
                    const std::array<unsigned,16> order=modern_uuid?std::array<unsigned,16>{11,10,9,8,13,12,15,14,16,17,18,19,20,21,22,23}:std::array<unsigned,16>{8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23};
                    for(size_t i=0;i<16;++i){if(i==4||i==6||i==8||i==10)uuid<<'-';uuid<<std::setw(2)<<byte(order[i]);}system["uuid"]=uuid.str();}
            }
        }
        if(type==2)result["board"]={{"manufacturer",text(4)},{"model",text(5)},{"version",text(6)},{"serial",text(7)}};
        if(type==3)result["system"]["asset_tag"]=text(8);
        if(type==4 && result["processors"].size()<16 && (byte(24)&64)){
            auto cores=byte(35)==255?word(42):byte(35),threads=byte(37)==255?word(46):byte(37);
            result["processors"].push_back({{"name",text(16)},{"manufacturer",text(7)},{"socket",text(4)},
                {"cores",cores==65535?0:cores},{"threads",threads==65535?0:threads},{"max_speed_mhz",word(20)}});
        }
        if(type==17 && result["memory"]["modules"].size()<64){
            auto size=word(12);unsigned long long capacity=0;
            if(size==32767)capacity=(dword(28)&0x7fffffffULL)*1024*1024;
            else if(size!=65535)capacity=static_cast<unsigned long long>(size&32767)*(size&32768?1024:1024*1024);
            if(capacity){
                auto kind=byte(18);std::string label=kind==18?"DDR":kind==19?"DDR2":kind==24?"DDR3":kind==26?"DDR4":kind==27?"LPDDR":kind==28?"LPDDR2":kind==29?"LPDDR3":kind==30?"LPDDR4":kind==34?"DDR5":kind==35?"LPDDR5":"";
                auto speed=word(32);if(!speed)speed=word(21);auto actual_speed=speed==65535?(dword(word(32)?88:84)&0x7fffffffULL):speed;
                result["memory"]["modules"].push_back({{"location",text(16)},{"size_bytes",capacity},{"type",label},
                    {"speed_mts",actual_speed},{"manufacturer",text(23)},{"serial",text(24)},{"part_number",text(26)}});
                result["memory"]["total_bytes"]=result["memory"]["total_bytes"].get<unsigned long long>()+capacity;
            }
        }
        if(type==22 && result["batteries"].size()<16){auto multiplier=byte(21);result["batteries"].push_back({{"name",text(8)},
            {"manufacturer",text(5)},{"serial",text(7)},{"design_capacity_mwh",word(10)*(multiplier?multiplier:1)}});}
        position=end+2;if(type==127)break;
    }
    if(records>=4096)result["warnings"].push_back("Firmware structure count exceeded the inventory bound");
    return result;
}
}
