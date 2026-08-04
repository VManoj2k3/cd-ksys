/* Clean C — the deterministic layers must stay silent. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define MAX_NAME_LEN 32U

struct sensor_reading {
    unsigned int id;
    long value;
};

static long sum_readings(const struct sensor_reading *readings, size_t count) {
    long total = 0L;
    for (size_t i = 0U; i < count; i++) {
        total += readings[i].value;
    }
    return total;
}

int main(void) {
    struct sensor_reading readings[2] = {
        {1U, 10L},
        {2U, 20L},
    };
    char name[MAX_NAME_LEN];
    (void)snprintf(name, sizeof(name), "sensor-%u", readings[0].id);
    printf("%s total=%ld\n", name, sum_readings(readings, 2U));
    return 0;
}
