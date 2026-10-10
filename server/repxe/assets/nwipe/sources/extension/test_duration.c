#include <assert.h>
#include <stdint.h>
#include <stdio.h>
void convert_seconds_to_hours_minutes_seconds(uint64_t total, int *hours, int *minutes, int *seconds);
int main(void) {
    int h = 0, m = 0, s = 0;
    const uint64_t totals[] = {4271, 671, 3600, 3599, 60, 59, 0, 7261, 11};
    for (unsigned i = 0; i < sizeof(totals) / sizeof(totals[0]); i++) {
        convert_seconds_to_hours_minutes_seconds(totals[i], &h, &m, &s);
        assert(h == (int)(totals[i] / 3600));
        assert(m == (int)(totals[i] % 3600 / 60));
        assert(s == (int)(totals[i] % 60));
    }
    puts("PASS: ETA hour/minute rollover and reused outputs; regular source only");
}
