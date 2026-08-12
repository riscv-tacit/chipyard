#include <stdio.h>
#include <stdint.h>
#include "rocc.h"

/* CUSTOM_0 prefetch via RoCC */
#define SW_PREFETCH_ROCC(addr) ROCC_INSTRUCTION_S(0, (uint64_t)(addr), 0)

/* ld x0 decode hack: looks like a normal load, decode remaps to uopROCC */
#define SW_PREFETCH_LDX0(addr) do { \
    asm volatile ("ld x0, 0(%0)" :: "r"(addr)); \
} while(0)

/* Select which to test */
#define SW_PREFETCH(addr) SW_PREFETCH_LDX0(addr)

#define PAGE 4096
#define NPAGES 20
#define NLINES 10
static volatile char mem[PAGE * NPAGES] __attribute__((aligned(PAGE)));

static inline uint64_t rdcycle(void) {
    uint64_t c;
    asm volatile ("rdcycle %0" : "=r"(c));
    return c;
}

int main(void) {
    uint64_t t0, t1;
    volatile int64_t val;
    int64_t sum;

    /* Touch first byte of each page to warm TLB */
    for (int i = 0; i < NPAGES; i++)
        mem[i * PAGE] = (char)i;

    /* Pointers to cold cache lines on different pages, offset 2048 */
    volatile int64_t *lines[NLINES];
    for (int i = 0; i < NLINES; i++)
        lines[i] = (volatile int64_t *)&mem[(i + 2) * PAGE + 2048];

    /* Test A: 10 sequential cold loads, no prefetch */
    t0 = rdcycle();
    sum = 0;
    for (int i = 0; i < NLINES; i++)
        sum += *lines[i];
    t1 = rdcycle();
    printf("10 cold loads:          %lu cyc (sum=%ld)\n", (unsigned long)(t1 - t0), (long)sum);

    /* Re-warm TLB, different lines for test B */
    volatile int64_t *lines2[NLINES];
    for (int i = 0; i < NLINES; i++)
        lines2[i] = (volatile int64_t *)&mem[(i + 2) * PAGE + 3072];

    /* Test B: prefetch with ld x0 decode hack */
    t0 = rdcycle();
    sum = 0;
    for (int i = 0; i < NLINES; i++) {
        if (i + 2 < NLINES)
            SW_PREFETCH_LDX0(lines2[i + 2]);
        sum += *lines2[i];
    }
    t1 = rdcycle();
    printf("10 loads + ld x0 pf:   %lu cyc (sum=%ld)\n", (unsigned long)(t1 - t0), (long)sum);

    // /* Re-warm TLB, different lines for test C */
    // volatile int64_t *lines3[NLINES];
    // for (int i = 0; i < NLINES; i++)
    //     lines3[i] = (volatile int64_t *)&mem[(i + 2) * PAGE + 4096 - 64];

    // /* Test C: prefetch with CUSTOM_0 RoCC */
    // t0 = rdcycle();
    // sum = 0;
    // for (int i = 0; i < NLINES; i++) {
    //     if (i + 2 < NLINES)
    //         SW_PREFETCH_ROCC(lines3[i + 2]);
    //     sum += *lines3[i];
    // }
    // t1 = rdcycle();
    // printf("10 loads + CUSTOM_0 pf: %lu cyc (sum=%ld)\n", (unsigned long)(t1 - t0), (long)sum);

    return 0;
}
