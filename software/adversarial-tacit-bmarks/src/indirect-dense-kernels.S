/* Indirect-jump kernels: flood the encoder with uncompressed UJ packets.
 *
 * (a) call/ret storm: `jal leaf` (IJ, 1 byte compressed) + `ret` (UJ,
 *     header + target + time, never compressed), with FILL fillers.
 * (b) jr dispatch chains: 16 handlers, each computes the next handler's
 *     address with auipc/addi and jumps with jr. Handlers are split into two
 *     islands separated by SKIP bytes so the XOR distance, hence the varint
 *     target width, is controlled: adjacent -> 1 byte, 8 KiB -> 2, 1 MiB -> 3,
 *     16 MiB -> 4.
 * Signature: void k(uint64_t iters, uint64_t unused, uint64_t unused)     */
    .text
    .option norelax

/* single filler: rotate the destination across consecutive units so the
 * fillers form four independent chains instead of one serial a1 chain */
    .set fill_rot, 0
    .macro FILL1
    .if (fill_rot % 4) == 0
    addi a1, a1, 1
    .elseif (fill_rot % 4) == 1
    addi a2, a2, 1
    .elseif (fill_rot % 4) == 2
    addi a3, a3, 1
    .else
    addi a4, a4, 1
    .endif
    .set fill_rot, fill_rot + 1
    .endm

    .macro FILLERS n
    .if \n == 1
    FILL1
    .endif
    .if \n >= 2
    addi a1, a1, 1
    .endif
    .if \n >= 2
    addi a2, a2, 1
    .endif
    .if \n >= 3
    addi a3, a3, 1
    .endif
    .if \n >= 4
    addi a4, a4, 1
    .endif
    .if \n >= 5
    addi a5, a5, 1
    .endif
    .if \n >= 6
    addi a6, a6, 1
    .endif
    .if \n >= 7
    addi a7, a7, 1
    .endif
    .endm

    .balign 16
k_leaf:
    ret

    .macro CALL_UNIT fill
    jal ra, k_leaf
    FILLERS \fill
    .endm

    .macro CALL_KERNEL name, fill
    .globl \name
    .balign 16
\name:
    addi sp, sp, -16
    sd   ra, 8(sp)
7:
    .rept 64
    CALL_UNIT \fill
    .endr
    addi a0, a0, -1
    bnez a0, 7b
    ld   ra, 8(sp)
    addi sp, sp, 16
    ret
    .endm

    CALL_KERNEL k_call_f0, 0
    CALL_KERNEL k_call_f1, 1
    CALL_KERNEL k_call_f3, 3
    CALL_KERNEL k_call_f7, 7

/* pc-relative address of \target into t1, then indirect jump */
    .macro DISPATCH target
1:  auipc t1, %pcrel_hi(\target)
    addi  t1, t1, %pcrel_lo(1b)
    jr    t1
    .endm

/* 16-handler ring. Even handlers in island A, odd handlers in island B,
 * islands separated by \skip bytes. One round = 16 jr + 1 not-taken beqz. */
    .macro JR_CHAIN v, skip
    .globl k_jr_\v
    .balign 64
k_jr_\v:
\v\()_h0:
    addi a0, a0, -1
    beqz a0, \v\()_done
    DISPATCH \v\()_h1
\v\()_h2:
    DISPATCH \v\()_h3
\v\()_h4:
    DISPATCH \v\()_h5
\v\()_h6:
    DISPATCH \v\()_h7
\v\()_h8:
    DISPATCH \v\()_h9
\v\()_h10:
    DISPATCH \v\()_h11
\v\()_h12:
    DISPATCH \v\()_h13
\v\()_h14:
    DISPATCH \v\()_h15
\v\()_done:
    ret
    .skip \skip
    .balign 64
\v\()_h1:
    DISPATCH \v\()_h2
\v\()_h3:
    DISPATCH \v\()_h4
\v\()_h5:
    DISPATCH \v\()_h6
\v\()_h7:
    DISPATCH \v\()_h8
\v\()_h9:
    DISPATCH \v\()_h10
\v\()_h11:
    DISPATCH \v\()_h12
\v\()_h13:
    DISPATCH \v\()_h14
\v\()_h15:
    DISPATCH \v\()_h0
    .endm

    JR_CHAIN near, 0
    JR_CHAIN d13,  (1 << 13)
    JR_CHAIN d20,  (1 << 20)
    JR_CHAIN d24,  (1 << 24)
