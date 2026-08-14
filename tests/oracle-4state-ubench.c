// Directed validation of the TraceDoctor 4-state oracle (TIP attribution).
// Four phases, each constructed to be dominated by one oracle state:
//   phase_compute -> Computing (independent ALU, predictable branches)
//   phase_stall   -> Stalled   (dependent pointer chase, L1D misses)
//   phase_flush   -> Flushed   (unpredictable branch on cache-resident data)
//   phase_drain   -> Drained   (64KB straight-line code thrashing the L1I)
// Phase boundaries are architectural (separate noinline functions), so the
// oracle's per-PC attribution can be segmented by symbol range and checked
// against per-phase rdcycle deltas measured by the target itself.

#include "tacit.h"
#include <stdint.h>
#include <stdio.h>

#define TARGET_FSIM_RAWBYTE 0x2 /* WithTraceSinkRawByte(2) */

static inline uint64_t rdcycle(void) {
  uint64_t x;
  asm volatile("rdcycle %0" : "=r"(x));
  return x;
}

#define CHASE_N (1 << 13) /* 8K nodes * 8B = 64KB, beyond 16KB L1D */
static uint64_t ring[CHASE_N];
#define FVAL_N (1 << 11) /* 8KB, L1D-resident */
static int fvals[FVAL_N];

__attribute__((noinline)) uint64_t phase_compute(void) {
  uint64_t a = 1, b = 2, c = 3, d = 4;
  for (int i = 0; i < 25000; i++) {
    a += b;
    b ^= c;
    c += d;
    d ^= a;
  }
  return a ^ b ^ c ^ d;
}

__attribute__((noinline)) uint64_t phase_stall(void) {
  uint64_t p = 0;
  for (int i = 0; i < 10000; i++) {
    p = ring[p];
  }
  return p;
}

__attribute__((noinline)) int phase_flush(void) {
  int acc = 0;
  unsigned idx = 1;
  for (int i = 0; i < 20000; i++) {
    int x = fvals[idx & (FVAL_N - 1)];
    idx = idx * 1103515245u + 12345u;
    if (x & 1) { /* ~50/50, data-dependent, no D$ miss */
      acc += x;
    } else {
      acc -= x >> 1;
    }
  }
  return acc;
}

/* 8 x 8KB straight-line code blobs = 64KB text, thrashes 16KB L1I.
 * Each blob uses a distinct immediate so GCC's IPA-ICF cannot fold them
 * back into one function (which would defeat the I$ thrash). */
#define BLOB(name, k)                                                         \
  __attribute__((noinline)) static void name(void) {                          \
    asm volatile(".rept 2048\n\taddi t0, t0, " #k "\n\t.endr" ::: "t0");      \
  }
BLOB(blob0, 1) BLOB(blob1, 2) BLOB(blob2, 3) BLOB(blob3, 4)
BLOB(blob4, 5) BLOB(blob5, 6) BLOB(blob6, 7) BLOB(blob7, 8)

__attribute__((noinline)) void phase_drain(void) {
  for (int r = 0; r < 12; r++) {
    blob0(); blob1(); blob2(); blob3();
    blob4(); blob5(); blob6(); blob7();
  }
}

int main(void) {
  /* Sattolo shuffle: single-cycle random permutation for the chase */
  for (unsigned i = 0; i < CHASE_N; i++)
    ring[i] = i;
  uint32_t s = 12345;
  for (unsigned i = CHASE_N - 1; i > 0; i--) {
    s = s * 1103515245u + 12345u;
    unsigned j = (s >> 16) % i;
    uint64_t t = ring[i];
    ring[i] = ring[j];
    ring[j] = t;
  }
  for (unsigned i = 0; i < FVAL_N; i++)
    fvals[i] = (int)((i * 2654435761u) >> 13);

  uint64_t t[5];
  volatile uint64_t sink = 0;

  LTraceEncoderType *encoder = l_trace_encoder_get(0);
  l_trace_encoder_configure_branch_mode(encoder, BRANCH_MODE_TARGET);
  l_trace_encoder_configure_target(encoder, TARGET_FSIM_RAWBYTE);
  l_trace_encoder_start(encoder);

  t[0] = rdcycle();
  sink += phase_compute();
  t[1] = rdcycle();
  sink += phase_stall();
  t[2] = rdcycle();
  sink += (uint64_t)phase_flush();
  t[3] = rdcycle();
  phase_drain();
  t[4] = rdcycle();

  l_trace_encoder_stop(encoder);

  printf("phase cycles: compute=%lu stall=%lu flush=%lu drain=%lu (sink=%lu)\n",
         t[1] - t[0], t[2] - t[1], t[3] - t[2], t[4] - t[3], (uint64_t)sink);
  return 0;
}
