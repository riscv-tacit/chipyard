/* Memory-bound kernels with a bursty control-flow tail.
 * A serialized load chain (next index depends on the loaded value) misses to
 * DRAM every iteration. NBR independent, predictable not-taken branches
 * follow each load: they execute early but cannot commit until the load
 * does, so when the miss returns the ROB drains 4-wide and enqueues a burst
 * of up to NBR packets into a 64-entry queue whose stall watermark is 48.
 * Signature: void k(uint64_t iters, uint64_t base, uint64_t mask)
 *   mask must clear bits [1:0] so the lw stays aligned whatever is loaded. */
    .text
    .option norelax

    .macro CHASE_KERNEL name, nbr, stride
    .globl \name
    .balign 16
\name:
    li   t2, 0              /* idx */
    li   t3, \stride
7:
    add  t4, a1, t2
    lw   t0, 0(t4)
    add  t2, t2, t0
    add  t2, t2, t3
    and  t2, t2, a2
    .rept \nbr
    bne  zero, zero, 9f
    .endr
9:
    addi a0, a0, -1
    bnez a0, 7b
    ret
    .endm

/* 65 cache lines: defeats the next-line prefetcher, walks the whole array */
#define MISS_STRIDE 4160
/* sequential words: L1 hits, prefetch-friendly control */
#define SEQ_STRIDE  4

    CHASE_KERNEL k_chase_b0,   0,   MISS_STRIDE
    CHASE_KERNEL k_chase_b8,   8,   MISS_STRIDE
    CHASE_KERNEL k_chase_b32,  32,  MISS_STRIDE
    CHASE_KERNEL k_chase_b64,  64,  MISS_STRIDE
    CHASE_KERNEL k_chase_b120, 120, MISS_STRIDE
    CHASE_KERNEL k_seq_b2,     2,   SEQ_STRIDE
    CHASE_KERNEL k_seq_b8,     8,   SEQ_STRIDE
