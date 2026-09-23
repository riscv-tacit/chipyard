/* Final report, memory family (appendix): pointer-chase floor and the
 * ROB-full burst that exceeds the 48-entry queue watermark. */
#include "harness.h"
#define K(n) extern void n(uint64_t, uint64_t, uint64_t)
K(k_chase_b0); K(k_chase_b64); K(k_chase_b120);
#define ARR_BYTES (64u << 20)
#define MASK      ((uint64_t)(ARR_BYTES - 1) & ~3ULL)
#define ITERS     (32768)
static uint32_t chase_arr[ARR_BYTES / 4] __attribute__((aligned(64)));
int main(void) {
  harness_init("report-mem");
  uint64_t base = (uint64_t)(uintptr_t)chase_arr;
  bench_run("chase_b0",   k_chase_b0,   ITERS, base, MASK);
  bench_run("chase_b64",  k_chase_b64,  ITERS, base, MASK);
  bench_run("chase_b120", k_chase_b120, ITERS, base, MASK);
  harness_done();
  return 0;
}
