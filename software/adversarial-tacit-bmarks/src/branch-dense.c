/* Family 1: branch-dense. Compressed TB/NT packets at up to 4 per commit
 * cycle against a packetizer that drains one compressed packet per cycle.
 * Knob: FILL = independent ALU ops per branch (0 -> every retired
 * instruction is a packet). NT = not-taken (can commit 4/cycle), TK = taken
 * (one per fetch bundle, so about 1/cycle). */
#include "harness.h"

#define K(n) extern void n(uint64_t, uint64_t, uint64_t)
K(k_nt_f0); K(k_nt_f1); K(k_nt_f3); K(k_nt_f7); K(k_nt_f15);
K(k_tk_f0); K(k_tk_f1); K(k_tk_f3); K(k_tk_f7);

#define ITERS (16384) /* x64 units = 1M branches per run */

int main(void) {
  harness_init("branch-dense");
  bench_run("nt_f0",  k_nt_f0,  ITERS, 0, 0);
  bench_run("nt_f1",  k_nt_f1,  ITERS, 0, 0);
  bench_run("nt_f3",  k_nt_f3,  ITERS, 0, 0);
  bench_run("nt_f7",  k_nt_f7,  ITERS, 0, 0);
  bench_run("nt_f15", k_nt_f15, ITERS, 0, 0);
  bench_run("tk_f0",  k_tk_f0,  ITERS, 0, 0);
  bench_run("tk_f1",  k_tk_f1,  ITERS, 0, 0);
  bench_run("tk_f3",  k_tk_f3,  ITERS, 0, 0);
  bench_run("tk_f7",  k_tk_f7,  ITERS, 0, 0);
  harness_done();
  return 0;
}
