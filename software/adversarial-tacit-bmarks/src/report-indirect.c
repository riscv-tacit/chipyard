/* Final report, indirect family: table-driven jr rings sweeping the UJ target
 * reach (packet 3/4/5/6 bytes = 2/2/3/3 packetizer cycles), plus the jal/ret
 * storm as the "bytes are not the cost" supporting point. */
#include "harness.h"
#define K(n) extern void n(uint64_t, uint64_t, uint64_t)
K(k_tjr_near); K(k_tjr_d13); K(k_tjr_d20); K(k_tjr_d24); K(k_call_f0);
extern uint64_t tjr_near_tab[], tjr_d13_tab[], tjr_d20_tab[], tjr_d24_tab[];
#define ROUNDS (65536) /* x16 = 1M indirect jumps per run */
int main(void) {
  harness_init("report-indirect");
  bench_run("tjr_near", k_tjr_near, ROUNDS, (uint64_t)(uintptr_t)tjr_near_tab, 0);
  bench_run("tjr_d13",  k_tjr_d13,  ROUNDS, (uint64_t)(uintptr_t)tjr_d13_tab,  0);
  bench_run("tjr_d20",  k_tjr_d20,  ROUNDS, (uint64_t)(uintptr_t)tjr_d20_tab,  0);
  bench_run("tjr_d24",  k_tjr_d24,  ROUNDS, (uint64_t)(uintptr_t)tjr_d24_tab,  0);
  bench_run("call_f0",  k_call_f0,  16384, 0, 0); /* x64 = 1M jal + 1M ret */
  harness_done();
  return 0;
}
