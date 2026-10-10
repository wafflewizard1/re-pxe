#pragma once
#include <repxe/core.hpp>
#include <functional>

namespace repxe {
// Called only by the protected, installed Audit Mode worker.
Json update_unresolved_drivers(const Json& problems, int timeout_seconds,
                              const std::function<void(const Json&)>& persist);
Json windows_update_worker();
}
