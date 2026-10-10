#include <repxe/operation_lock.hpp>
#include <future>
#include <iostream>
#include <stdexcept>
#include <string>
using namespace repxe;
int main(){
    int checks=0;auto check=[&](bool value){++checks;if(!value)throw std::runtime_error("Operation lock check failed: "+std::to_string(checks));};
    auto name=L"Local\\repxeLockTest-"+std::to_wstring(GetCurrentProcessId());
    auto retained=CreateMutexW(nullptr,FALSE,name.c_str());check(retained!=nullptr);
    {OperationLock idle(name.c_str());check(static_cast<bool>(idle));
        {OperationLock recursive(name.c_str());check(static_cast<bool>(recursive));}
        auto blocked=std::async(std::launch::async,[&]{OperationLock active(name.c_str());return !active;});check(blocked.get());}
    auto next=std::async(std::launch::async,[&]{OperationLock idle(name.c_str());return static_cast<bool>(idle);});check(next.get());
    auto abandon=std::async(std::launch::async,[&]{auto handle=CreateMutexW(nullptr,FALSE,name.c_str());auto result=WaitForSingleObject(handle,0);CloseHandle(handle);return result==WAIT_OBJECT_0;});check(abandon.get());
    {OperationLock recovery(name.c_str());check(static_cast<bool>(recovery));}
    CloseHandle(retained);
    std::cout<<checks<<" operation ownership checks passed\n";
}
