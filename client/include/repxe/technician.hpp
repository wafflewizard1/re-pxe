#pragma once
#include <repxe/windows.hpp>
#include <functional>
namespace repxe {
using Progress = std::function<void(const Json&)>;
int technician_ui(const std::filesystem::path& config = {}, const std::filesystem::path& audit = {});
void connect_share(const std::wstring& share, const std::wstring& user, const std::wstring& password);
Json server_catalog(const std::string& origin, const std::string& token);
Json sync_server_job(const std::filesystem::path& queue, const std::string& token);
Json server_deploy(const Json& config, const std::filesystem::path& config_path,
                   const std::string& origin, const std::string& token, const std::string& image_id,
                   const Progress& progress = {});
std::string prompt_secret(const wchar_t* prompt);
}

namespace repxe {
Json pxe_api(const std::string& origin,const std::string& token,const std::string& path,const Json* body=nullptr);
void pxe_download(const std::string& origin,const std::string& token,const std::string& path,
                  const std::filesystem::path& output,unsigned long long expected,
                  const std::function<void(unsigned long long)>& progress = {});
int pxe_ui(const std::filesystem::path& boot);
int driver_console();
Json server_request(const std::string& origin, const std::string& token, const std::string& path,
                    const std::wstring& method, const std::string& payload = "",
                    const std::wstring& headers = L"");
}
