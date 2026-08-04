/* FP-bait: correct but unusual C. Deterministic layers must stay silent
   and the LLM verifier must NOT invent memory bugs where there are none. */
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#define RING_CAP 16U

typedef struct {
    uint32_t slots[RING_CAP];
    size_t head;
    size_t count;
} ring_t;

void ring_init(ring_t *r) {
    memset(r, 0, sizeof(*r));
}

int ring_push(ring_t *r, uint32_t v) {
    if (r->count == RING_CAP) {
        return -1;
    }
    size_t idx = (r->head + r->count) % RING_CAP;
    r->slots[idx] = v;
    r->count++;
    return 0;
}

/* deliberate pointer arithmetic that is correct */
size_t count_nonzero(const uint32_t *data, size_t n) {
    size_t c = 0U;
    for (const uint32_t *p = data; p < data + n; ++p) {
        if (*p != 0U) {
            c++;
        }
    }
    return c;
}

/* bounded copy with explicit truncation — correct */
void safe_label(char *dst, size_t dst_sz, const char *src) {
    if (dst_sz == 0U) {
        return;
    }
    size_t i = 0U;
    for (; i + 1U < dst_sz && src[i] != '\0'; ++i) {
        dst[i] = src[i];
    }
    dst[i] = '\0';
}
