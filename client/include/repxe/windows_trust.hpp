#pragma once
#ifdef _WIN32
#include <filesystem>

namespace repxe {
// Verifies the file's embedded or registered catalog signature and requires
// a Microsoft-rooted certificate chain. It never changes certificate stores.
bool trusted_microsoft_windows_file(const std::filesystem::path& path);
bool trusted_microsoft_windows_kernel(const std::filesystem::path& path);
}
#endif
