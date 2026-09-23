/* Diagnostic kernels for the jr_near anomaly (31% slowdown, ~0 stall).
 * All rings are 16 handlers laid out contiguously (the "near" layout).
 *   k_jr_near : auipc/addi/jr   (UJ packet per hop)  -- the anomalous kernel
 *   k_j_near  : auipc/addi/j    (IJ packet per hop, same footprint, direct)
 *   k_jr_f3   : jr ring with 3 independent fillers per hop (lower packet rate)
 *   k_call_f0 : jal leaf / ret storm (UJ via RAS) -- showed no slowdown on F2
 * Signature: void k(uint64_t iters, uint64_t unused, uint64_t unused)   */
    .text
    .option norelax

    .macro DISPATCH_JR target, fill
5:  auipc t1, %pcrel_hi(\target)
    addi  t1, t1, %pcrel_lo(5b)
    .if \fill >= 1
    addi a1, a1, 1
    .endif
    .if \fill >= 2
    addi a2, a2, 1
    .endif
    .if \fill >= 3
    addi a3, a3, 1
    .endif
    jr    t1
    .endm

    .macro DISPATCH_J target
5:  auipc t1, %pcrel_hi(\target)
    addi  t1, t1, %pcrel_lo(5b)
    j     \target
    .endm

/* 16-handler ring, contiguous. \disp is the dispatch macro, \fill its filler count. */
    .macro RING16 v, disp, fill
    .globl k_\v
    .balign 64
k_\v:
\v\()_h0:
    addi a0, a0, -1
    beqz a0, \v\()_done
    \disp \v\()_h1, \fill
\v\()_h1:
    \disp \v\()_h2, \fill
\v\()_h2:
    \disp \v\()_h3, \fill
\v\()_h3:
    \disp \v\()_h4, \fill
\v\()_h4:
    \disp \v\()_h5, \fill
\v\()_h5:
    \disp \v\()_h6, \fill
\v\()_h6:
    \disp \v\()_h7, \fill
\v\()_h7:
    \disp \v\()_h8, \fill
\v\()_h8:
    \disp \v\()_h9, \fill
\v\()_h9:
    \disp \v\()_h10, \fill
\v\()_h10:
    \disp \v\()_h11, \fill
\v\()_h11:
    \disp \v\()_h12, \fill
\v\()_h12:
    \disp \v\()_h13, \fill
\v\()_h13:
    \disp \v\()_h14, \fill
\v\()_h14:
    \disp \v\()_h15, \fill
\v\()_h15:
    \disp \v\()_h0, \fill
\v\()_done:
    ret
    .endm

/* DISPATCH_J ignores the fill argument; wrapper so RING16 can pass one */
    .macro DISPATCH_J2 target, fill
    DISPATCH_J \target
    .endm

    RING16 jr_near, DISPATCH_JR, 0
    RING16 jr_f3,   DISPATCH_JR, 3
    RING16 j_near,  DISPATCH_J2, 0

    .balign 16
k_leaf:
    ret

    .globl k_call_f0
    .balign 16
k_call_f0:
    addi sp, sp, -16
    sd   ra, 8(sp)
7:
    .rept 64
    jal ra, k_leaf
    .endr
    addi a0, a0, -1
    bnez a0, 7b
    ld   ra, 8(sp)
    addi sp, sp, 16
    ret
