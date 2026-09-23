/* Clean indirect-jump limit: table-driven dispatch (ld/addi/jr) through the DMA
 * sink, sweeping the XOR distance between jump and target so the UJ packet is
 * 3, 4, 5 or 6 bytes (2, 2, 3, 3 packetizer cycles). Unlike the auipc rings,
 * only jr uses the jump unit, so there is no scheduler bistability to trigger. */
#include "harness.h"

#define K(n) extern void n(uint64_t, uint64_t, uint64_t)
K(k_tjr_near); K(k_tjr_d13); K(k_tjr_d20); K(k_tjr_d24);
extern uint64_t tjr_near_tab[], tjr_d13_tab[], tjr_d20_tab[], tjr_d24_tab[];

#ifndef ROUNDS
#define ROUNDS 65536 /* x16 = 1M indirect jumps per run */
#endif

int main(void) {
  harness_init("indirect-table");
  bench_run("tjr_near", k_tjr_near, ROUNDS, (uint64_t)(uintptr_t)tjr_near_tab, 0);
  bench_run("tjr_d13",  k_tjr_d13,  ROUNDS, (uint64_t)(uintptr_t)tjr_d13_tab,  0);
  bench_run("tjr_d20",  k_tjr_d20,  ROUNDS, (uint64_t)(uintptr_t)tjr_d20_tab,  0);
  bench_run("tjr_d24",  k_tjr_d24,  ROUNDS, (uint64_t)(uintptr_t)tjr_d24_tab,  0);
  harness_done();
  return 0;
}
