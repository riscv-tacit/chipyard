/* Family 2: indirect-jump-dense. UJ packets are never compressed
 * (header + varint target + varint time = 3..7 bytes, i.e. 2..3 packetizer
 * cycles each) and BOOM commits about one jalr per cycle. Knobs: FILL for
 * the call/ret storm, and XOR distance (target varint width) for jr chains. */
#include "harness.h"

#define K(n) extern void n(uint64_t, uint64_t, uint64_t)
K(k_call_f0); K(k_call_f1); K(k_call_f3); K(k_call_f7);
K(k_jr_near); K(k_jr_d13); K(k_jr_d20); K(k_jr_d24);

#define CALL_ITERS (16384) /* x64 call/ret pairs = 1M jal + 1M ret */
#define JR_ROUNDS  (65536) /* x16 jr = 1M indirect jumps */

int main(void) {
  harness_init("indirect-dense");
  bench_run("call_f0", k_call_f0, CALL_ITERS, 0, 0);
  bench_run("call_f1", k_call_f1, CALL_ITERS, 0, 0);
  bench_run("call_f3", k_call_f3, CALL_ITERS, 0, 0);
  bench_run("call_f7", k_call_f7, CALL_ITERS, 0, 0);
  bench_run("jr_near", k_jr_near, JR_ROUNDS, 0, 0);
  bench_run("jr_d13",  k_jr_d13,  JR_ROUNDS, 0, 0);
  bench_run("jr_d20",  k_jr_d20,  JR_ROUNDS, 0, 0);
  bench_run("jr_d24",  k_jr_d24,  JR_ROUNDS, 0, 0);
  harness_done();
  return 0;
}
