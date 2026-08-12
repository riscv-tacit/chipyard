// prefetch-bench: does the ld x0 software prefetch actually pull lines into the
// cache and hide miss latency? Baremetal M-mode, no VM. Working set (1 MiB) is
// 2x the 512 KiB L2 and walked in Fisher-Yates-shuffled order so the HW NL/stride
// prefetcher can't catch it. We compare a plain walk vs the same walk with an
// ld x0 prefetch issued `dist` iterations ahead. If the prefetch works, the
// prefetched walk takes fewer cycles/access.
//
// Sized for VCS (~5 kHz on MediumBoom): pool is a DRAM pointer (NOT a bss array,
// so crt0 does not zero-fill megabytes at boot); only the working set is touched.
#include <stdio.h>
#include <stdint.h>

#define LINE      64u
#define WSLINES   16384u                 // 1 MiB working set = 2x the 512 KiB L2
#define STRIDE64  (LINE / sizeof(uint64_t))
#define ITERS     12000u                 // shuffled accesses per walk
#define POOL_ADDR 0x88000000UL           // free DRAM well above the program

// The decode hack turns a load with rd=x0 into a prefetch. funct3 picks the hint:
// ld x0 -> M_PFR (read), lw x0 -> M_PFW (write intent). Build with -DPF_WRITE=1 to
// request ownership, which matters when the line is read-modify-written.
#if defined(PF_WRITE) && PF_WRITE
#define PREFETCH(a) asm volatile("lw x0, 0(%0)" :: "r"(a) : "memory")
#else
#define PREFETCH(a) asm volatile("ld x0, 0(%0)" :: "r"(a) : "memory")
#endif

static volatile uint64_t * const pool = (volatile uint64_t *)POOL_ADDR;
static uint32_t order[ITERS + 256];      // shuffled line indices to visit

static inline uint64_t rdcycle(void) { uint64_t c; asm volatile("rdcycle %0" : "=r"(c)); return c; }

static volatile uint64_t sink;

// evict the working set: sweep all WSLINES sequentially (1 MiB > 512 KiB L2)
static void flush_ws(void)
{
  uint64_t s = 0;
  for (uint32_t i = 0; i < WSLINES; i++) s += pool[(uint64_t)i * STRIDE64];
  sink += s;
  asm volatile("fence" ::: "memory");
}

static uint64_t walk(uint32_t dist)
{
  uint64_t sum = 0;
  flush_ws();
  uint64_t t0 = rdcycle();
  for (uint32_t k = 0; k < ITERS; k++) {
    if (dist) PREFETCH(&pool[(uint64_t)order[k + dist] * STRIDE64]);
#if defined(PF_RMW) && PF_RMW
    // Read-modify-write the same line, like liblzma's match-finder hash buckets
    // (cur_match = mf->hash[h]; mf->hash[h] = pos). The decode hack only emits
    // M_PFR, so a read prefetch may leave the line short of exclusive and the
    // store still pays an upgrade -- this mode isolates that effect.
    sum += pool[(uint64_t)order[k] * STRIDE64];
    pool[(uint64_t)order[k] * STRIDE64] = sum;
#else
    sum += pool[(uint64_t)order[k] * STRIDE64];
#endif
  }
  uint64_t t1 = rdcycle();
  sink += sum;
  return t1 - t0;
}

int main(void)
{
  // define the working set (write each line once -> no X-prop under VCS)
  for (uint32_t i = 0; i < WSLINES; i++) pool[(uint64_t)i * STRIDE64] = i * 2654435761u;

  // Fisher-Yates shuffle 0..WSLINES-1 (LCG rng), take ITERS+256 as the walk order
  static uint32_t perm[WSLINES];
  for (uint32_t i = 0; i < WSLINES; i++) perm[i] = i;
  uint64_t rng = 0x123456789abcdef0ull;
  for (uint32_t i = WSLINES - 1; i > 0; i--) {
    rng = rng * 6364136223846793005ull + 1442695040888963407ull;
    uint32_t j = (uint32_t)((rng >> 33) % (i + 1));
    uint32_t t = perm[i]; perm[i] = perm[j]; perm[j] = t;
  }
  for (uint32_t k = 0; k < ITERS + 256; k++) order[k] = perm[k % WSLINES];

  printf("prefetch-bench: ws=1MiB (2x L2) wslines=%u iters=%u (shuffled)\n",
         (unsigned)WSLINES, (unsigned)ITERS);

  uint64_t base = walk(0);
  printf("no-prefetch:      %lu cyc  (%lu cyc/access)\n",
         (unsigned long)base, (unsigned long)(base / ITERS));

  // Build with -DPF_SWEEP=1 for the short-distance sweep. A distance-D stream
  // needs D refills in flight, so useful distances are bounded by the dcache MSHR
  // count (MegaBoom defaults to 8, the prefetch configs raise it to 16).
#if defined(PF_SWEEP) && PF_SWEEP
  const uint32_t dists[] = {2, 4, 8, 16};
#else
  const uint32_t dists[] = {16, 64};
#endif
  for (unsigned d = 0; d < sizeof(dists) / sizeof(dists[0]); d++) {
    uint64_t t = walk(dists[d]);
    long delta = (long)base - (long)t;   // >0 => prefetch faster
    printf("prefetch dist=%lu  %lu cyc  (%lu cyc/access)  base-pf delta=%ld cyc\n",
           (unsigned long)dists[d], (unsigned long)t, (unsigned long)(t / ITERS), delta);
  }
  printf("done (sink=%lu)\n", (unsigned long)sink);
  return 0;
}
