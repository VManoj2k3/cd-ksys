/* FN-bait: subtle memory/logic bugs. cppcheck catches some; the rest are
   the LLM layer's job (that's what this stress test measures). Each bug is
   tagged with who SHOULD catch it. */
#include <stdlib.h>
#include <string.h>
#include <stdio.h>

/* [LLM] use-after-free: p is used after free */
int use_after_free(void) {
    char *p = (char *)malloc(16);
    if (p == NULL) return -1;
    strcpy(p, "hello");
    free(p);
    return (int)strlen(p);   /* BUG: read after free */
}

/* [LLM] double free */
void double_free(char *p) {
    free(p);
    free(p);                 /* BUG: freed twice */
}

/* [cppcheck/LLM] memory leak: early return leaks buf */
int leak_on_error(size_t n) {
    char *buf = (char *)malloc(n);
    if (buf == NULL) return -1;
    if (n > 1024) {
        return -2;           /* BUG: buf leaked on this path */
    }
    free(buf);
    return 0;
}

/* [LLM] off-by-one heap overflow: writes n+1 bytes into n */
void fill(char *dst, size_t n) {
    for (size_t i = 0; i <= n; i++) {   /* BUG: <= should be < */
        dst[i] = 'x';
    }
}

/* [LLM] returns pointer to freed/stack memory */
const char *dangling(void) {
    char local[8];
    strcpy(local, "temp");
    return local;            /* BUG: returns address of local */
}

/* [LLM] null-deref: result of malloc used without a check */
void no_null_check(size_t n) {
    int *arr = (int *)malloc(n * sizeof(int));
    arr[0] = 42;             /* BUG: arr may be NULL */
    free(arr);
}

/* [LLM] integer overflow in allocation size */
void *alloc_table(size_t count, size_t size) {
    return malloc(count * size);  /* BUG: count*size can overflow */
}
