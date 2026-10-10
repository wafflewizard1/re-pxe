#include "repxe/core.hpp"
#include <cctype>

namespace repxe {
std::vector<int> InstallProgress::feed(std::string_view bytes) {
    std::vector<int> values;
    for (unsigned char ch : bytes) {
        // DISM can emit narrow text or UTF-16LE. Numeric progress is ASCII;
        // ignore NUL padding and retain split tokens across reads.
        if (!ch) continue;
        if (ch == '%') {
            auto start = pending.find_last_not_of("0123456789.,");
            auto token = pending.substr(start == std::string::npos ? 0 : start + 1);
            bool valid = !token.empty() && token.size() <= 8;
            unsigned value = 0, digits = 0; bool fraction = false, fractional_nonzero = false;
            for (unsigned char c : token) {
                if (c == '.' || c == ',') { if (fraction || !digits) valid = false; fraction = true; }
                else if (std::isdigit(c)) { if (fraction && c != '0') fractional_nonzero = true; if (!fraction) { value = value * 10 + c - '0'; ++digits; } }
                else valid = false;
            }
            if (start != std::string::npos && (pending[start] == '-' || pending[start] == '+' || std::isalnum(static_cast<unsigned char>(pending[start])))) valid = false;
            if (valid && digits && value <= 100 && !(value == 100 && fractional_nonzero) && static_cast<int>(value) > last) { last = value; values.push_back(last); }
            pending.clear();
        } else {
            pending.push_back(static_cast<char>(ch));
            if (pending.size() > 64) pending.erase(0, pending.size() - 64);
        }
    }
    return values;
}
}
