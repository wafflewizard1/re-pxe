#pragma once
#ifdef _WIN32
#include <windows.h>
#include <stdexcept>

namespace repxe {
// Object existence is not evidence that an operation still owns the mutex.
class OperationLock {
    HANDLE handle = nullptr;
    bool acquired = false;
public:
    explicit OperationLock(const wchar_t* name) {
        handle = CreateMutexW(nullptr, FALSE, name);
        if (!handle) throw std::runtime_error("Create operation lock");
        const auto result = WaitForSingleObject(handle, 0);
        acquired = result == WAIT_OBJECT_0 || result == WAIT_ABANDONED;
        if (result != WAIT_TIMEOUT && !acquired) {
            CloseHandle(handle); handle = nullptr;
            throw std::runtime_error("Acquire operation lock");
        }
    }
    ~OperationLock() { if (acquired) ReleaseMutex(handle); if (handle) CloseHandle(handle); }
    OperationLock(const OperationLock&) = delete;
    OperationLock& operator=(const OperationLock&) = delete;
    explicit operator bool() const { return acquired; }
};
}
#endif
