/* RTL look at the not-taken branch family: nt_f0 (4:1 oversubscription) and
 * nt_f1 (production at the drain rate, stall fraction 0.5 but ~7 % slowdown). */
#include "harness.h"
#define K(n) extern void n(uint64_t, uint64_t, uint64_t)
K(k_nt_f0); K(k_nt_f1);
#ifndef ITERS
#define ITERS 2048 /* x64 = 131K branches per run */
#endif
int main(void) {
  harness_init("nt-rtl");
  bench_run("nt_f1", k_nt_f1, ITERS, 0, 0);
  bench_run("nt_f0", k_nt_f0, ITERS, 0, 0);
  harness_done();
  return 0;
}
