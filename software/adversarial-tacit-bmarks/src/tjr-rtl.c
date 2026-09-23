/* RTL look at the table-driven jr rings: near (F1-predictable targets, 0.86
 * jumps/cycle), d13 (targets beyond the micro-BTB's 13-bit offset), d24 (6-byte
 * packets). */
#include "harness.h"
#define K(n) extern void n(uint64_t, uint64_t, uint64_t)
K(k_tjr_near); K(k_tjr_d13); K(k_tjr_d24);
extern uint64_t tjr_near_tab[], tjr_d13_tab[], tjr_d24_tab[];
#ifndef ROUNDS
#define ROUNDS 4096 /* x16 = 64K jumps per run */
#endif
int main(void) {
  harness_init("tjr-rtl");
  bench_run("tjr_near", k_tjr_near, ROUNDS, (uint64_t)(uintptr_t)tjr_near_tab, 0);
  bench_run("tjr_d13",  k_tjr_d13,  ROUNDS, (uint64_t)(uintptr_t)tjr_d13_tab,  0);
  bench_run("tjr_d24",  k_tjr_d24,  ROUNDS, (uint64_t)(uintptr_t)tjr_d24_tab,  0);
  harness_done();
  return 0;
}
