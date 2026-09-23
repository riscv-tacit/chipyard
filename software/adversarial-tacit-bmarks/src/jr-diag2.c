/* Second jr diagnostic. On F2 (2026-09-06 23:34 run) the contiguous 12-byte
 * handler ring traced for free while the 24-byte (3-filler) ring slowed 33%
 * with ~0 stall. This ELF: (1) TracerV-marked short jr_f3 window for
 * per-instruction commit timing, (2) jr_f3 under always-ready sink and lossy
 * mode, (3) handler spacing sweep with nops, (4) direct-jump twin of jr_f3,
 * (5) the original two-island layout, (6) short-run onset check. */
#include "harness.h"

#define K(n) extern void n(uint64_t, uint64_t, uint64_t)
K(k_jr_p0); K(k_jr_p1); K(k_jr_p2); K(k_jr_p3); K(k_jr_p5); K(k_jr_p13);
K(k_jr_f3); K(k_j_f3); K(k_jr_island);

#ifndef ROUNDS
#define ROUNDS 65536
#endif

int main(void) {
  harness_init("jr-diag2");
  /* TracerV window: 4096 rounds = 64K jumps, untraced then traced */
  bench_run_marked("trv_jr_f3.dma", k_jr_f3, ROUNDS / 16, 0, 0, TARGET_DMA, 0);

  bench_run_cfg("jr_f3.dma",        k_jr_f3, ROUNDS, 0, 0, TARGET_DMA,    0);
  bench_run_cfg("jr_f3.always",     k_jr_f3, ROUNDS, 0, 0, TARGET_ALWAYS, 0);
  bench_run_cfg("jr_f3.dma-lossy",  k_jr_f3, ROUNDS, 0, 0, TARGET_DMA,    1);
  bench_run_cfg("jr_f3.dma-short",  k_jr_f3, 1024,   0, 0, TARGET_DMA,    0);
  bench_run_cfg("j_f3.dma",         k_j_f3,  ROUNDS, 0, 0, TARGET_DMA,    0);

  bench_run_cfg("jr_p0.dma",  k_jr_p0,  ROUNDS, 0, 0, TARGET_DMA, 0);
  bench_run_cfg("jr_p1.dma",  k_jr_p1,  ROUNDS, 0, 0, TARGET_DMA, 0);
  bench_run_cfg("jr_p2.dma",  k_jr_p2,  ROUNDS, 0, 0, TARGET_DMA, 0);
  bench_run_cfg("jr_p3.dma",  k_jr_p3,  ROUNDS, 0, 0, TARGET_DMA, 0);
  bench_run_cfg("jr_p5.dma",  k_jr_p5,  ROUNDS, 0, 0, TARGET_DMA, 0);
  bench_run_cfg("jr_p13.dma", k_jr_p13, ROUNDS, 0, 0, TARGET_DMA, 0);

  bench_run_cfg("jr_island.dma", k_jr_island, ROUNDS, 0, 0, TARGET_DMA, 0);
  harness_done();
  return 0;
}
