/* Family 3: memory-bound with bursty commit. Average packet rate is low, but
 * every DRAM miss lets a ROB-full burst of branches commit at once. Knob:
 * NBR = branches queued behind each miss. b0 is the pure pointer-chase floor;
 * seq_* are L1-resident sequential controls. */
#include "harness.h"

#define K(n) extern void n(uint64_t, uint64_t, uint64_t)
K(k_chase_b0); K(k_chase_b8); K(k_chase_b32); K(k_chase_b64); K(k_chase_b120);
K(k_seq_b2); K(k_seq_b8);

#define ARR_BYTES (64u << 20) /* 64 MiB, far beyond L2 */
#define MASK      ((uint64_t)(ARR_BYTES - 1) & ~3ULL)
#define ITERS     (32768)

static uint32_t chase_arr[ARR_BYTES / 4] __attribute__((aligned(64)));

int main(void) {
  harness_init("mem-burst");
  uint64_t base = (uint64_t)(uintptr_t)chase_arr;
  bench_run("chase_b0",   k_chase_b0,   ITERS, base, MASK);
  bench_run("chase_b8",   k_chase_b8,   ITERS, base, MASK);
  bench_run("chase_b32",  k_chase_b32,  ITERS, base, MASK);
  bench_run("chase_b64",  k_chase_b64,  ITERS, base, MASK);
  bench_run("chase_b120", k_chase_b120, ITERS, base, MASK);
  bench_run("seq_b2",     k_seq_b2,     ITERS * 8, base, MASK);
  bench_run("seq_b8",     k_seq_b8,     ITERS * 8, base, MASK);
  harness_done();
  return 0;
}
