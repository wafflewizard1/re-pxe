#pragma once
#include <repxe/core.hpp>
#include <filesystem>
#include <fstream>
#include <stdexcept>

namespace repxe {
inline Json read_json_file(const std::filesystem::path& path,unsigned long long maximum,const char* oversized){
    // Measure the opened generation, rather than a pathname that an atomic
    // state writer could replace between the size check and opening the file.
    std::ifstream stream(path,std::ios::binary|std::ios::ate);
    if(!stream)throw std::runtime_error("Cannot read configuration");
    const auto size=static_cast<std::streamoff>(stream.tellg());
    if(size<0)throw std::runtime_error("Cannot read JSON file size");
    if(static_cast<unsigned long long>(size)>maximum)throw std::runtime_error(oversized);
    stream.seekg(0);
    return Json::parse(stream);
}
}
