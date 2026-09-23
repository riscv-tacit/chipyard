/* Second diagnostic: the F2 slowdown appeared for 24-byte handlers (jr_f3) and
 * the original two-island layout, but not for contiguous 12-byte handlers.
 * Sweep handler spacing with nops (auipc, addi, k x nop, jr), and re-test the
 * anomalous jr_f3 under sink / lossy controls. Rings are 16 handlers.       */
    .text
    .option norelax

    .macro DISPATCH_JR_PAD target, pad
5:  auipc t1, %pcrel_hi(\target)
    addi  t1, t1, %pcrel_lo(5b)
    .rept \pad
    nop
    .endr
    jr    t1
    .endm

    .macro DISPATCH_JR_F3 target, pad
5:  auipc t1, %pcrel_hi(\target)
    addi  t1, t1, %pcrel_lo(5b)
    addi a1, a1, 1
    addi a2, a2, 1
    addi a3, a3, 1
    jr    t1
    .endm

    .macro DISPATCH_J_F3 target, pad
5:  auipc t1, %pcrel_hi(\target)
    addi  t1, t1, %pcrel_lo(5b)
    addi a1, a1, 1
    addi a2, a2, 1
    addi a3, a3, 1
    j     \target
    .endm

    .macro RING16 v, disp, arg
    .globl k_\v
    .balign 64
k_\v:
\v\()_h0:
    addi a0, a0, -1
    beqz a0, \v\()_done
    \disp \v\()_h1, \arg
\v\()_h1:
    \disp \v\()_h2, \arg
\v\()_h2:
    \disp \v\()_h3, \arg
\v\()_h3:
    \disp \v\()_h4, \arg
\v\()_h4:
    \disp \v\()_h5, \arg
\v\()_h5:
    \disp \v\()_h6, \arg
\v\()_h6:
    \disp \v\()_h7, \arg
\v\()_h7:
    \disp \v\()_h8, \arg
\v\()_h8:
    \disp \v\()_h9, \arg
\v\()_h9:
    \disp \v\()_h10, \arg
\v\()_h10:
    \disp \v\()_h11, \arg
\v\()_h11:
    \disp \v\()_h12, \arg
\v\()_h12:
    \disp \v\()_h13, \arg
\v\()_h13:
    \disp \v\()_h14, \arg
\v\()_h14:
    \disp \v\()_h15, \arg
\v\()_h15:
    \disp \v\()_h0, \arg
\v\()_done:
    ret
    .endm

    /* handler spacing sweep: 12, 16, 20, 24, 32, 64 bytes */
    RING16 jr_p0,  DISPATCH_JR_PAD, 0
    RING16 jr_p1,  DISPATCH_JR_PAD, 1
    RING16 jr_p2,  DISPATCH_JR_PAD, 2
    RING16 jr_p3,  DISPATCH_JR_PAD, 3
    RING16 jr_p5,  DISPATCH_JR_PAD, 5
    RING16 jr_p13, DISPATCH_JR_PAD, 13
    /* the anomalous kernel and its direct-jump twin */
    RING16 jr_f3,  DISPATCH_JR_F3, 0
    RING16 j_f3,   DISPATCH_J_F3, 0

/* original two-island "near" layout from indirect-dense-kernels.S (even
 * handlers first, odd handlers after `done`), which slowed 31% on F2 */
    .macro DISPATCH target
1:  auipc t1, %pcrel_hi(\target)
    addi  t1, t1, %pcrel_lo(1b)
    jr    t1
    .endm

    .globl k_jr_island
    .balign 64
k_jr_island:
isl_h0:
    addi a0, a0, -1
    beqz a0, isl_done
    DISPATCH isl_h1
isl_h2:
    DISPATCH isl_h3
isl_h4:
    DISPATCH isl_h5
isl_h6:
    DISPATCH isl_h7
isl_h8:
    DISPATCH isl_h9
isl_h10:
    DISPATCH isl_h11
isl_h12:
    DISPATCH isl_h13
isl_h14:
    DISPATCH isl_h15
isl_done:
    ret
    .balign 64
isl_h1:
    DISPATCH isl_h2
isl_h3:
    DISPATCH isl_h4
isl_h5:
    DISPATCH isl_h6
isl_h7:
    DISPATCH isl_h8
isl_h9:
    DISPATCH isl_h10
isl_h11:
    DISPATCH isl_h12
isl_h13:
    DISPATCH isl_h14
isl_h15:
    DISPATCH isl_h0
