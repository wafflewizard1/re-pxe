#pragma once
#include <nlohmann/json.hpp>
#include <string>
#include <vector>
#include <string_view>

namespace repxe {
using Json = nlohmann::json;
void validate_finish_options(const Json& options);
std::string finish_answer_xml(const std::string& original, const Json& options);
std::string cleanup_script();
bool validate_downloaded_image(const Json& config,const Json& approved,const Json& actual,unsigned long long size,const std::string& hash);
std::string tls_failure_detail(unsigned long flags);
class InstallProgress {
    std::string pending;
    int last = -1;
public:
    std::vector<int> feed(std::string_view bytes);
};
Json smbios_specs(const std::vector<unsigned char>& data, bool modern_uuid = true);
std::string battery_health_script();
Json powercfg_battery_health(const std::string& xml);
inline constexpr unsigned long long MiB = 1024ULL * 1024;
inline constexpr unsigned long long GiB = 1024ULL * MiB;
std::string completion_action(const std::string& stage, bool new_boot, bool driver_problems);
std::string driver_fallback_action(bool problems, int attempts, int maximum, const std::string& last_status);
Json driver_install_result(int exit_code);
Json driver_attempt_result(int exit_code);
Json driver_pass_summary(const Json& results);
std::string driver_finish_action(const Json& state, const std::string& boot);
std::string completion_seal_action(const std::string& image_state);
bool driver_finish_available(const Json& state);
bool completion_report_terminal(const Json& state);
bool completion_report_accepted(const Json& state);
bool completion_shutdown_ready(const Json& state);
void validate_driver_export(const Json& hardware,const Json& devices);
Json offline_driver_plan(const Json& config,const Json& manifest);
unsigned long long driver_upload_offset(const Json& expected,const Json& remote);
bool driver_update_matches(const std::string& hardware_id, const Json& problems);
Json inspect_xml(const std::string& xml, int index);
Json deployment_plan(const Json& config, const Json& inventory, const Json& image,
                     const std::string& actual_hash, const Json& resources);
std::wstring quote_argument(const std::wstring& argument);
std::wstring command_line(const std::vector<std::wstring>& arguments);
void validate_driver_manifest(const Json& manifest);
bool driver_package_matches(const Json& manifest, const Json& hardware);
void validate_server_origin(const std::string& origin);
Json select_server_image(const Json& catalog, const Json& config, const std::string& id);
std::string nvme_controller_serial(const std::vector<unsigned char>& identify);
size_t pxe_target_index(const Json& disks, const Json& automation);
Json pxe_image_metadata(const Json& selection);
Json pxe_disks(const Json& inventory);
void validate_pxe_automation(const Json& policy, const Json& disks);
bool confirmation_matches(const std::string& expected, const std::string& entered);
void validate_answer_xml(const std::string& content, const std::string& role);
void validate_config(const Json& config);
Json gpt_layout(unsigned long long capacity, int recovery_mib, unsigned int sector_bytes);
void validate_blank_deployment(const Json& config, const Json& target_status,
                               const Json& scratch, unsigned long long scratch_free,
                               unsigned long long expanded_bytes);
Json capture_preflight(const Json& config, const Json& inventory, const Json& source,
                       const Json& resources, unsigned long long destination_free,
                       unsigned long long scratch_free);
}
