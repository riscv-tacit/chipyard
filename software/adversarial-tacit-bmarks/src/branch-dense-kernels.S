/* Branch-dense kernels: flood the encoder with 1-byte compressed packets.
 * Each unit is one perfectly predictable branch plus FILL independent ALU
 * fillers (rotating destination registers so they never serialize).
 * Signature: void k(uint64_t iters, uint64_t unused, uint64_t unused)
 * One iteration retires 64 units.                                        */
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
    .if \n >= 8
    addi t0, t0, 1
    .endif
    .if \n >= 9
    addi t1, t1, 1
    .endif
    .if \n >= 10
    addi t2, t2, 1
    .endif
    .if \n >= 11
    addi t3, t3, 1
    .endif
    .if \n >= 12
    addi t4, t4, 1
    .endif
    .if \n >= 13
    addi t5, t5, 1
    .endif
    .if \n >= 14
    addi t6, t6, 1
    .endif
    .if \n >= 15
    addi a1, a1, 1
    .endif
    .endm

/* never-taken branch (condition is statically false) to the block end */
    .macro NT_UNIT fill
    bne zero, zero, 9f
    FILLERS \fill
    .endm

/* always-taken branch hopping over one dead instruction */
    .macro TK_UNIT fill
    beq zero, zero, 1f
    addi a1, a1, 1          /* skipped */
1:
    FILLERS \fill
    .endm

    .macro NT_KERNEL name, fill
    .globl \name
    .balign 16
\name:
7:
    .rept 64
    NT_UNIT \fill
    .endr
9:
    addi a0, a0, -1
    bnez a0, 7b
    ret
    .endm

    .macro TK_KERNEL name, fill
    .globl \name
    .balign 16
\name:
7:
    .rept 64
    TK_UNIT \fill
    .endr
    addi a0, a0, -1
    bnez a0, 7b
    ret
    .endm

    NT_KERNEL k_nt_f0,  0
    NT_KERNEL k_nt_f1,  1
    NT_KERNEL k_nt_f3,  3
    NT_KERNEL k_nt_f7,  7
    NT_KERNEL k_nt_f15, 15

    TK_KERNEL k_tk_f0,  0
    TK_KERNEL k_tk_f1,  1
    TK_KERNEL k_tk_f3,  3
    TK_KERNEL k_tk_f7,  7
