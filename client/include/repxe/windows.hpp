#pragma once
#include <repxe/core.hpp>
#include <filesystem>
#include <functional>

namespace repxe {
std::string utf8(const std::wstring& text);
std::wstring wide(const std::string& text);
Json read_json(const std::filesystem::path& path);
void write_json(const std::filesystem::path& path, const Json& value);
Json inventory();
Json complete_windows(const std::string& mode = "automatic");
Json driver_session_snapshot();
bool completion_uses_console();
Json completion_status();
void driver_session_pulse();
void driver_remote_controls();
Json driver_session_action(const std::string& action, const std::filesystem::path& upload = {},
                           const std::function<void(const std::string&)>& output = {});
void restart_system(const std::filesystem::path& log);
Json deliver_report(const std::filesystem::path& offline = {});
Json inspect_wim(const std::filesystem::path& path, int index);
std::string sha256(const std::filesystem::path& path);
Json locate_resource(const std::filesystem::path& path, const std::string& role);
Json plan_from_config(const Json& config, const std::filesystem::path& config_path);
Json capture_from_config(const Json& config, const std::filesystem::path& config_path, bool execute);
Json deploy_from_config(const Json& config, const std::filesystem::path& config_path, bool execute, const std::function<void(const Json&)>& progress = {});
void restrict_state_path(const std::filesystem::path& path);
void verify_state_path(const std::filesystem::path& path);
}

namespace repxe {
Json pxe_preflight(const Json& config,const std::filesystem::path& config_path,const Json& metadata);
Json pxe_deploy(const Json& config,const std::filesystem::path& config_path,const Json& metadata,
                const std::function<void(Json&,const std::filesystem::path&)>& source,
                const std::function<void(const Json&)>& progress = {});
}
