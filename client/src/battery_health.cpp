#include <repxe/core.hpp>
#include <battery_health_script.hpp>
#include <pugixml.hpp>
#include <algorithm>
#include <charconv>
#include <cmath>
#include <stdexcept>

namespace repxe {
std::string battery_health_script() { return battery_script; }
Json powercfg_battery_health(const std::string& xml) {
    if (xml.size() > 16 * 1024 * 1024) throw std::runtime_error("Battery report exceeds the evidence bound");
    pugi::xml_document document;
    if (!document.load_buffer(xml.data(), xml.size(), pugi::parse_default | pugi::parse_doctype, pugi::encoding_auto))
        throw std::runtime_error("Battery report XML is invalid");
    for (const auto& node : document.children())
        if (node.type() == pugi::node_doctype) throw std::runtime_error("Battery report must not contain a document type");
    auto local = [](const char* value) { std::string name(value); auto colon = name.rfind(':'); return colon == std::string::npos ? name : name.substr(colon + 1); };
    auto child = [&](const pugi::xml_node& parent, const char* name) {
        for (const auto& node : parent.children()) if (local(node.name()) == name) return node;
        return pugi::xml_node{};
    };
    auto root = document.document_element();
    if (local(root.name()) != "BatteryReport") throw std::runtime_error("Battery report XML has an unexpected root");
    auto field = [&](const pugi::xml_node& node, const char* name) {
        std::string text = child(node, name).text().as_string();
        const auto start = text.find_first_not_of(" \t\r\n"), end = text.find_last_not_of(" \t\r\n");
        return start == std::string::npos ? std::string() : text.substr(start, end - start + 1);
    };
    auto number = [&](const pugi::xml_node& node, const char* name, bool allow_zero) -> Json {
        auto text = field(node, name); unsigned long long value = 0;
        auto parsed = std::from_chars(text.data(), text.data() + text.size(), value);
        if (parsed.ec != std::errc{} || parsed.ptr != text.data() + text.size() || (!allow_zero && !value) || value >= 4294967295ULL) return nullptr;
        return value;
    };
    auto text = [&](const pugi::xml_node& node, const char* name) {
        auto value = field(node, name); value.resize(std::min<size_t>(256, value.size()));
        return Json::parse(Json(value).dump(-1, ' ', false, Json::error_handler_t::replace)).get<std::string>();
    };
    Json result = {{"source", "powercfg-batteryreport"}, {"status", "unavailable"}, {"batteries", Json::array()}, {"warnings", Json::array()}};
    for (const auto& node : child(root, "Batteries").children()) {
        if (local(node.name()) != "Battery") continue;
        if (result["batteries"].size() >= 16) throw std::runtime_error("Battery count exceeds the inventory bound");
        auto design = number(node, "DesignCapacity", false), full = number(node, "FullChargeCapacity", false);
        Json health = nullptr;
        if (!design.is_null() && !full.is_null()) {
            health = std::round(full.get<double>() / design.get<double>() * 10000.0) / 100.0;
            result["status"] = "available";
        } else result["warnings"].push_back("Battery capacity was unavailable; health is unknown");
        auto name = text(node, "Id"); if (name.empty()) name = text(node, "Name");
        result["batteries"].push_back({{"name", name}, {"manufacturer", text(node, "Manufacturer")}, {"serial", text(node, "SerialNumber")},
            {"design_capacity_mwh", design}, {"full_charge_capacity_mwh", full}, {"health_percent", health}, {"cycle_count", number(node, "CycleCount", true)}, {"source", "powercfg-batteryreport"}});
    }
    if (result["batteries"].empty()) result["warnings"].push_back("Powercfg reported no installed battery; battery health is unavailable");
    return result;
}
}
