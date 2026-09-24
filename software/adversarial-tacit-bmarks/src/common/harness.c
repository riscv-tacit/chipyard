#include <stdio.h>
#include <inttypes.h>
#include "tacit.h"
#include "harness.h"

static LTraceEncoderType *enc;
static LTraceSinkDmaType *dma;

/* -DHARNESS_NO_MMIO: functional check on plain spike (no encoder/sink devices):
 * every MMIO access becomes a no-op returning 0. */
#ifdef HARNESS_NO_MMIO
#define MMIO_W(expr) do { } while (0)
#define MMIO_R(expr) ((uint64_t)0)
#else
#define MMIO_W(expr) do { expr; } while (0)
#define MMIO_R(expr) ((uint64_t)(expr))
#endif

static inline uint64_t rd_cycle(void)   { uint64_t v; asm volatile("csrr %0, mcycle"   : "=r"(v)); return v; }
static inline uint64_t rd_instret(void) { uint64_t v; asm volatile("csrr %0, minstret" : "=r"(v)); return v; }
static inline uint64_t rd_hartid(void)  { uint64_t v; asm volatile("csrr %0, mhartid"  : "=r"(v)); return v; }
static inline void fence(void)          { asm volatile("fence" ::: "memory"); }

static void spin(uint64_t cycles) {
  uint64_t t0 = rd_cycle();
  while (rd_cycle() - t0 < cycles) { }
}

void harness_init(const char *family) {
  uint64_t hart = rd_hartid();
  enc = l_trace_encoder_get(hart);
  dma = l_trace_sink_dma_get(hart);
  MMIO_W(l_trace_encoder_stop(enc));
  MMIO_W(l_trace_encoder_configure_branch_mode(enc, BRANCH_MODE_TARGET));
  MMIO_W(l_trace_sink_dma_configure_addr(dma, DMA_ADDRESS));
  MMIO_W(l_trace_sink_dma_configure_max_size(dma, DMA_MAX_SIZE));
  MMIO_W(l_trace_sink_dma_configure_mode(dma, DMA_MODE_OVERFLOW));
  MMIO_W(l_trace_encoder_configure_target(enc, TARGET_DMA));
  fence();
  printf("BENCH_INFO family=%s hart=%" PRIu64 " reps=%d dma_addr=0x%" PRIx64
         " dma_max=0x%" PRIx64 " sink=dma\n",
         family, hart, REPS, (uint64_t)DMA_ADDRESS, (uint64_t)DMA_MAX_SIZE);
}

static void report(const char *name, const char *mode, int rep, uint64_t iters,
                   uint64_t cyc, uint64_t ins, uint64_t stall, uint64_t bytes, uint64_t src,
                   uint64_t t0, uint64_t gap, uint64_t dropped) {
  printf("RESULT bench=%s mode=%s rep=%d iters=%" PRIu64 " cycles=%" PRIu64
         " instret=%" PRIu64 " stall=%" PRIu64 " bytes=%" PRIu64 " srcstall=%" PRIu64
         " start_cycle=%" PRIu64 " gap=%" PRIu64 " dropped=%" PRIu64 "\n",
         name, mode, rep, iters, cyc, ins, stall, bytes, src, t0, gap, dropped);
}

static void run_untraced(const char *name, int rep, kernel_fn k, uint64_t iters, uint64_t a1, uint64_t a2) {
  fence();
  uint64_t c0 = rd_cycle(), i0 = rd_instret();
  k(iters, a1, a2);
  uint64_t c1 = rd_cycle(), i1 = rd_instret();
  report(name, "untraced", rep, iters, c1 - c0, i1 - i0, 0, 0, 0, c0, 0, 0);
}

static void run_traced(const char *name, int rep, kernel_fn k, uint64_t iters, uint64_t a1, uint64_t a2,
                       uint32_t target, uint32_t lossy) {
  /* sink and mode are written while the encoder is disabled (hardware contract) */
  MMIO_W(l_trace_encoder_configure_target(enc, target));
  MMIO_W(enc->TR_TE_LOSSY = lossy);
  /* The encoder's own counters (stall, gap, dropped) are window-scoped since the
   * 2026-09-07 RTL: cleared on enable, frozen on disable, so the value read after
   * disable describes exactly this window and is reported as is. Reading a
   * baseline before enable would subtract the PREVIOUS window's frozen total and
   * wrap. The DMA sink's counters still accumulate from reset, so those two are
   * deltas. */
  uint64_t bytes0 = MMIO_R(l_trace_sink_dma_count(dma));
  uint64_t src0   = MMIO_R(l_trace_sink_dma_src_stall(dma));
  fence();
  MMIO_W(l_trace_encoder_start(enc));
  fence();
  uint64_t c0 = rd_cycle(), i0 = rd_instret();
  k(iters, a1, a2);
  uint64_t c1 = rd_cycle(), i1 = rd_instret();
  fence();
  MMIO_W(l_trace_encoder_stop(enc));
  fence();
  /* let the End sync drain through the queues, packetizer and DMA writes */
  spin(50000);
  uint64_t stall1 = MMIO_R(l_trace_encoder_get_stall_count(enc));
  uint64_t bytes1 = MMIO_R(l_trace_sink_dma_count(dma));
  uint64_t src1   = MMIO_R(l_trace_sink_dma_src_stall(dma));
  uint64_t gap1   = MMIO_R(enc->TR_TE_GAP_CYCLES);
  uint64_t drop1  = MMIO_R(enc->TR_TE_DROPPED);
  report(name, "traced", rep, iters, c1 - c0, i1 - i0, stall1, bytes1 - bytes0, src1 - src0,
         c0, gap1, drop1);
  if (bytes1 + 4096 > DMA_MAX_SIZE)
    printf("WARN dma buffer nearly full (%" PRIu64 " bytes), overflow mode will drop\n", bytes1);
}

void bench_run_cfg(const char *name, kernel_fn k, uint64_t iters, uint64_t a1, uint64_t a2,
                   uint32_t target, uint32_t lossy) {
  /* warmup: caches, predictors */
  k(iters / 8 + 1, a1, a2);
  /* interleave untraced and traced reps so a kernel with more than one steady
   * state shows up as spread within a mode rather than as a mode difference */
  for (int r = 0; r < REPS; r++) {
    run_untraced(name, r, k, iters, a1, a2);
    run_traced(name, r, k, iters, a1, a2, target, lossy);
  }
}

void bench_run_marked(const char *name, kernel_fn k, uint64_t iters, uint64_t a1, uint64_t a2,
                      uint32_t target, uint32_t lossy) {
  k(iters / 8 + 1, a1, a2);
  asm volatile("slti x0, x0, 0x5A5");                    /* TracerV arm   */
  run_untraced(name, 0, k, iters, a1, a2);
  run_traced(name, 0, k, iters, a1, a2, target, lossy);
  asm volatile("slti x0, x0, 0x5AD\n\tslti x0, x0, 0x5AD\n\t"
               "slti x0, x0, 0x5AD\n\tslti x0, x0, 0x5AD"); /* TracerV disarm (all commit slots) */
}

void bench_run(const char *name, kernel_fn k, uint64_t iters, uint64_t a1, uint64_t a2) {
  bench_run_cfg(name, k, iters, a1, a2, TARGET_DMA, 0);
}

void harness_done(void) {
  printf("BENCH_DONE total_trace_bytes=%" PRIu64 " total_stall=%" PRIu64 "\n",
         MMIO_R(l_trace_sink_dma_count(dma)), MMIO_R(l_trace_encoder_get_stall_count(enc)));
}
