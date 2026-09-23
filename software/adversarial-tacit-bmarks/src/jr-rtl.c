/* RTL-simulation reproduction of the jr_f3 slowdown. On F2 the effect needs
 * more than 1024 rounds to appear (dma-short is clean, 4096 rounds is not), so
 * this runs jr_f3 at RTL_ROUNDS (default 8192) with the DMA sink, then the
 * always-ready sink as control. REPS=1. */
#include "harness.h"

#define K(n) extern void n(uint64_t, uint64_t, uint64_t)
K(k_jr_f3);

#ifndef ROUNDS
#define ROUNDS 8192
#endif

int main(void) {
  harness_init("jr-rtl");
  bench_run_cfg("jr_f3.dma",    k_jr_f3, ROUNDS, 0, 0, TARGET_DMA,    0);
  bench_run_cfg("jr_f3.always", k_jr_f3, ROUNDS, 0, 0, TARGET_ALWAYS, 0);
  harness_done();
  return 0;
}
