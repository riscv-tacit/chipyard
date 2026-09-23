/* Flip demonstration for RTL (TacitMegaBoomV3BackpressureConfig): jr_f3 traced
 * through the backpressure sink (target 3). With +tacit_bp_mode=every,
 * +tacit_bp_beats=N, +tacit_bp_off=M the sink accepts N beats then refuses M
 * cycles, so the encoder queue fills once and stalls the ROB briefly. If the
 * ring has two operating points, one brief gate should move it from 2.25 to
 * 3.0 cycles per handler for the rest of the run. Always-ready sink as control. */
#include "harness.h"

#define K(n) extern void n(uint64_t, uint64_t, uint64_t)
K(k_jr_f3);

#ifndef ROUNDS
#define ROUNDS 4096
#endif
#define TARGET_BP 3

int main(void) {
  harness_init("jr-rtl-bp");
  bench_run_cfg("jr_f3.always", k_jr_f3, ROUNDS, 0, 0, TARGET_ALWAYS, 0);
  bench_run_cfg("jr_f3.bp",     k_jr_f3, ROUNDS, 0, 0, TARGET_BP,     0);
  bench_run_cfg("jr_f3.always2", k_jr_f3, ROUNDS, 0, 0, TARGET_ALWAYS, 0);
  harness_done();
  return 0;
}
