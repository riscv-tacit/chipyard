/* Diagnostic sweep for the jr_near anomaly: same indirect-jump ring under
 * different sinks and modes, plus direct-jump / lower-rate / call-ret controls.
 * Sized for RTL simulation: ROUNDS x 16 jumps per run, REPS=1 by default.
 * If the slowdown persists with the always-ready sink, it is not the DMA
 * path; if it persists in lossy mode (stall never reaches the ROB), it is
 * not the stall signal; if j_near is clean, it is jalr-specific. */
#include "harness.h"

#define K(n) extern void n(uint64_t, uint64_t, uint64_t)
K(k_jr_near); K(k_j_near); K(k_jr_f3); K(k_call_f0);

#ifndef ROUNDS
#define ROUNDS 1024
#endif

int main(void) {
  harness_init("jr-diag");
  bench_run_cfg("jr_near.dma",       k_jr_near, ROUNDS, 0, 0, TARGET_DMA,    0);
  bench_run_cfg("jr_near.always",    k_jr_near, ROUNDS, 0, 0, TARGET_ALWAYS, 0);
  bench_run_cfg("jr_near.dma-lossy", k_jr_near, ROUNDS, 0, 0, TARGET_DMA,    1);
  bench_run_cfg("j_near.dma",        k_j_near,  ROUNDS, 0, 0, TARGET_DMA,    0);
  bench_run_cfg("jr_f3.dma",         k_jr_f3,   ROUNDS, 0, 0, TARGET_DMA,    0);
  bench_run_cfg("call_f0.dma",       k_call_f0, ROUNDS / 4, 0, 0, TARGET_DMA, 0);
  harness_done();
  return 0;
}
