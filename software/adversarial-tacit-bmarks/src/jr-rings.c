/* Ring-size sweep (24-byte handlers) for the micro-BTB hypothesis: BOOM's F1
 * micro-BTB has 16 fully associative ways. Rings whose jump fetch-lines fit
 * should have one fast steady state; rings just over it two (flippable);
 * well over it, slow always. Each ring: untraced pass, then traced through the
 * backpressure sink (target 3) so a few brief commit gates occur. Rounds are
 * scaled so every ring executes ~64K jumps. Last: 16-ring with a data-dependent
 * jitter branch in handler 0 (a1 = pseudo-random table). */
#include "harness.h"

#define K(n) extern void n(uint64_t, uint64_t, uint64_t)
K(k_ring8); K(k_ring12); K(k_ring14); K(k_ring15); K(k_ring16); K(k_ring17);
K(k_ring18); K(k_ring20); K(k_ring24); K(k_ring32); K(k_ringjit16);

#define TARGET_BP 3
#define JUMPS (1 << 16)

/* pseudo-random bits, ~1/8 set, for the jitter ring (one word per round) */
#define JIT_ROUNDS ((JUMPS / 16) * 2)  /* warmup + untraced + traced fit */
static uint32_t jit_table[JIT_ROUNDS * 3];

int main(void) {
  harness_init("jr-rings");
  uint32_t x = 0x9e3779b9u;
  for (unsigned i = 0; i < sizeof(jit_table) / sizeof(jit_table[0]); i++) {
    x = x * 1664525u + 1013904223u;
    jit_table[i] = ((x >> 29) == 0) ? 1u : 0u;   /* 1 in 8 */
  }
#define RING(n) bench_run_cfg("ring" #n ".bp", k_ring##n, JUMPS / (n), 0, 0, TARGET_BP, 0)
  RING(8); RING(12); RING(14); RING(15); RING(16); RING(17); RING(18); RING(20); RING(24); RING(32);
  /* jitter ring: the table pointer advances one word per round across warmup+2 passes */
  bench_run_cfg("ringjit16.bp", k_ringjit16, JUMPS / 16, (uint64_t)(uintptr_t)jit_table, 0, TARGET_BP, 0);
  harness_done();
  return 0;
}
