/* Final report, branch family: not-taken density (nt_f0 = 4:1 oversubscription,
 * nt_f1 = production at the drain rate, nt_f3 = half rate) and taken density
 * (frontend-capped, expected free). Kernels from branch-dense-kernels.S. */
#include "harness.h"
#define K(n) extern void n(uint64_t, uint64_t, uint64_t)
K(k_nt_f0); K(k_nt_f1); K(k_nt_f3); K(k_nt_f7); K(k_tk_f0); K(k_tk_f1); K(k_tk_f3);
#define ITERS (16384) /* x64 = 1M branches per run */
int main(void) {
  harness_init("report-branch");
  bench_run("nt_f0", k_nt_f0, ITERS, 0, 0);
  bench_run("nt_f1", k_nt_f1, ITERS, 0, 0);
  bench_run("nt_f3", k_nt_f3, ITERS, 0, 0);
  bench_run("nt_f7", k_nt_f7, ITERS, 0, 0);
  bench_run("tk_f0", k_tk_f0, ITERS, 0, 0);
  bench_run("tk_f1", k_tk_f1, ITERS, 0, 0);
  bench_run("tk_f3", k_tk_f3, ITERS, 0, 0);
  harness_done();
  return 0;
}
