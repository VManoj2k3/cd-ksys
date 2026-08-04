/* Planted violations for the C pipeline. This coment has a misspelling. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define BUF_SZ 64

void copy_input(char *dst, const char *user_input) {
    strcpy(dst, user_input);            /* unbounded copy -> flawfinder */
}

int recieve_count(int *values, int n) { /* misspelled identifier -> spell */
    int total;                          /* uninitialized -> cppcheck */
    for (int i = 0; i <= n; i++) {      /* off-by-one -> LLM layer */
        total += values[i];
    }
    return total;
}

char *make_buffer(void) {
    char *buf = (char *)malloc(BUF_SZ);
    if (buf == NULL) {
        return NULL;
    }
    sprintf(buf, "%d", 42);             /* sprintf -> flawfinder */
    return buf;
}

int main(void) {
    char *b = make_buffer();
    printf("%s\n", b);                  /* b never freed -> memleak family */
    return 0;
}
