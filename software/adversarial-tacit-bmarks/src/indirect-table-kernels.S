/* Table-driven indirect-jump rings (generated inline): 16 handlers of
 * `ld t1,0(t2); addi t2,t2,8; jr t1`, even handlers in island A, odd in
 * island B, islands SKIP bytes apart so the UJ target varint is 1/2/3/4
 * bytes. No auipc: the jump unit is used only by jr, so the scheduler
 * bistability of the auipc rings cannot occur. a1 = target table. */
    .text
    .option norelax

    .globl k_tjr_near
    .balign 64
k_tjr_near:
tjr_near_h0:
    addi a0, a0, -1
    beqz a0, tjr_near_done
    mv   t2, a1
    ld   t1, 0(t2)
    addi t2, t2, 8
    jr   t1
tjr_near_h2:
    ld   t1, 0(t2)
    addi t2, t2, 8
    jr   t1
tjr_near_h4:
    ld   t1, 0(t2)
    addi t2, t2, 8
    jr   t1
tjr_near_h6:
    ld   t1, 0(t2)
    addi t2, t2, 8
    jr   t1
tjr_near_h8:
    ld   t1, 0(t2)
    addi t2, t2, 8
    jr   t1
tjr_near_h10:
    ld   t1, 0(t2)
    addi t2, t2, 8
    jr   t1
tjr_near_h12:
    ld   t1, 0(t2)
    addi t2, t2, 8
    jr   t1
tjr_near_h14:
    ld   t1, 0(t2)
    addi t2, t2, 8
    jr   t1
tjr_near_done:
    ret
    .skip 0
    .balign 64
tjr_near_h1:
    ld   t1, 0(t2)
    addi t2, t2, 8
    jr   t1
tjr_near_h3:
    ld   t1, 0(t2)
    addi t2, t2, 8
    jr   t1
tjr_near_h5:
    ld   t1, 0(t2)
    addi t2, t2, 8
    jr   t1
tjr_near_h7:
    ld   t1, 0(t2)
    addi t2, t2, 8
    jr   t1
tjr_near_h9:
    ld   t1, 0(t2)
    addi t2, t2, 8
    jr   t1
tjr_near_h11:
    ld   t1, 0(t2)
    addi t2, t2, 8
    jr   t1
tjr_near_h13:
    ld   t1, 0(t2)
    addi t2, t2, 8
    jr   t1
tjr_near_h15:
    ld   t1, 0(t2)
    addi t2, t2, 8
    jr   t1

    .globl k_tjr_d13
    .balign 64
k_tjr_d13:
tjr_d13_h0:
    addi a0, a0, -1
    beqz a0, tjr_d13_done
    mv   t2, a1
    ld   t1, 0(t2)
    addi t2, t2, 8
    jr   t1
tjr_d13_h2:
    ld   t1, 0(t2)
    addi t2, t2, 8
    jr   t1
tjr_d13_h4:
    ld   t1, 0(t2)
    addi t2, t2, 8
    jr   t1
tjr_d13_h6:
    ld   t1, 0(t2)
    addi t2, t2, 8
    jr   t1
tjr_d13_h8:
    ld   t1, 0(t2)
    addi t2, t2, 8
    jr   t1
tjr_d13_h10:
    ld   t1, 0(t2)
    addi t2, t2, 8
    jr   t1
tjr_d13_h12:
    ld   t1, 0(t2)
    addi t2, t2, 8
    jr   t1
tjr_d13_h14:
    ld   t1, 0(t2)
    addi t2, t2, 8
    jr   t1
tjr_d13_done:
    ret
    .skip 8192
    .balign 64
tjr_d13_h1:
    ld   t1, 0(t2)
    addi t2, t2, 8
    jr   t1
tjr_d13_h3:
    ld   t1, 0(t2)
    addi t2, t2, 8
    jr   t1
tjr_d13_h5:
    ld   t1, 0(t2)
    addi t2, t2, 8
    jr   t1
tjr_d13_h7:
    ld   t1, 0(t2)
    addi t2, t2, 8
    jr   t1
tjr_d13_h9:
    ld   t1, 0(t2)
    addi t2, t2, 8
    jr   t1
tjr_d13_h11:
    ld   t1, 0(t2)
    addi t2, t2, 8
    jr   t1
tjr_d13_h13:
    ld   t1, 0(t2)
    addi t2, t2, 8
    jr   t1
tjr_d13_h15:
    ld   t1, 0(t2)
    addi t2, t2, 8
    jr   t1

    .globl k_tjr_d20
    .balign 64
k_tjr_d20:
tjr_d20_h0:
    addi a0, a0, -1
    beqz a0, tjr_d20_done
    mv   t2, a1
    ld   t1, 0(t2)
    addi t2, t2, 8
    jr   t1
tjr_d20_h2:
    ld   t1, 0(t2)
    addi t2, t2, 8
    jr   t1
tjr_d20_h4:
    ld   t1, 0(t2)
    addi t2, t2, 8
    jr   t1
tjr_d20_h6:
    ld   t1, 0(t2)
    addi t2, t2, 8
    jr   t1
tjr_d20_h8:
    ld   t1, 0(t2)
    addi t2, t2, 8
    jr   t1
tjr_d20_h10:
    ld   t1, 0(t2)
    addi t2, t2, 8
    jr   t1
tjr_d20_h12:
    ld   t1, 0(t2)
    addi t2, t2, 8
    jr   t1
tjr_d20_h14:
    ld   t1, 0(t2)
    addi t2, t2, 8
    jr   t1
tjr_d20_done:
    ret
    .skip 1048576
    .balign 64
tjr_d20_h1:
    ld   t1, 0(t2)
    addi t2, t2, 8
    jr   t1
tjr_d20_h3:
    ld   t1, 0(t2)
    addi t2, t2, 8
    jr   t1
tjr_d20_h5:
    ld   t1, 0(t2)
    addi t2, t2, 8
    jr   t1
tjr_d20_h7:
    ld   t1, 0(t2)
    addi t2, t2, 8
    jr   t1
tjr_d20_h9:
    ld   t1, 0(t2)
    addi t2, t2, 8
    jr   t1
tjr_d20_h11:
    ld   t1, 0(t2)
    addi t2, t2, 8
    jr   t1
tjr_d20_h13:
    ld   t1, 0(t2)
    addi t2, t2, 8
    jr   t1
tjr_d20_h15:
    ld   t1, 0(t2)
    addi t2, t2, 8
    jr   t1

    .globl k_tjr_d24
    .balign 64
k_tjr_d24:
tjr_d24_h0:
    addi a0, a0, -1
    beqz a0, tjr_d24_done
    mv   t2, a1
    ld   t1, 0(t2)
    addi t2, t2, 8
    jr   t1
tjr_d24_h2:
    ld   t1, 0(t2)
    addi t2, t2, 8
    jr   t1
tjr_d24_h4:
    ld   t1, 0(t2)
    addi t2, t2, 8
    jr   t1
tjr_d24_h6:
    ld   t1, 0(t2)
    addi t2, t2, 8
    jr   t1
tjr_d24_h8:
    ld   t1, 0(t2)
    addi t2, t2, 8
    jr   t1
tjr_d24_h10:
    ld   t1, 0(t2)
    addi t2, t2, 8
    jr   t1
tjr_d24_h12:
    ld   t1, 0(t2)
    addi t2, t2, 8
    jr   t1
tjr_d24_h14:
    ld   t1, 0(t2)
    addi t2, t2, 8
    jr   t1
tjr_d24_done:
    ret
    .skip 16777216
    .balign 64
tjr_d24_h1:
    ld   t1, 0(t2)
    addi t2, t2, 8
    jr   t1
tjr_d24_h3:
    ld   t1, 0(t2)
    addi t2, t2, 8
    jr   t1
tjr_d24_h5:
    ld   t1, 0(t2)
    addi t2, t2, 8
    jr   t1
tjr_d24_h7:
    ld   t1, 0(t2)
    addi t2, t2, 8
    jr   t1
tjr_d24_h9:
    ld   t1, 0(t2)
    addi t2, t2, 8
    jr   t1
tjr_d24_h11:
    ld   t1, 0(t2)
    addi t2, t2, 8
    jr   t1
tjr_d24_h13:
    ld   t1, 0(t2)
    addi t2, t2, 8
    jr   t1
tjr_d24_h15:
    ld   t1, 0(t2)
    addi t2, t2, 8
    jr   t1

    .section .rodata

    .globl tjr_near_tab
    .balign 64
tjr_near_tab:
    .dword tjr_near_h1, tjr_near_h2, tjr_near_h3, tjr_near_h4, tjr_near_h5, tjr_near_h6, tjr_near_h7, tjr_near_h8, tjr_near_h9, tjr_near_h10, tjr_near_h11, tjr_near_h12, tjr_near_h13, tjr_near_h14, tjr_near_h15, tjr_near_h0

    .globl tjr_d13_tab
    .balign 64
tjr_d13_tab:
    .dword tjr_d13_h1, tjr_d13_h2, tjr_d13_h3, tjr_d13_h4, tjr_d13_h5, tjr_d13_h6, tjr_d13_h7, tjr_d13_h8, tjr_d13_h9, tjr_d13_h10, tjr_d13_h11, tjr_d13_h12, tjr_d13_h13, tjr_d13_h14, tjr_d13_h15, tjr_d13_h0

    .globl tjr_d20_tab
    .balign 64
tjr_d20_tab:
    .dword tjr_d20_h1, tjr_d20_h2, tjr_d20_h3, tjr_d20_h4, tjr_d20_h5, tjr_d20_h6, tjr_d20_h7, tjr_d20_h8, tjr_d20_h9, tjr_d20_h10, tjr_d20_h11, tjr_d20_h12, tjr_d20_h13, tjr_d20_h14, tjr_d20_h15, tjr_d20_h0

    .globl tjr_d24_tab
    .balign 64
tjr_d24_tab:
    .dword tjr_d24_h1, tjr_d24_h2, tjr_d24_h3, tjr_d24_h4, tjr_d24_h5, tjr_d24_h6, tjr_d24_h7, tjr_d24_h8, tjr_d24_h9, tjr_d24_h10, tjr_d24_h11, tjr_d24_h12, tjr_d24_h13, tjr_d24_h14, tjr_d24_h15, tjr_d24_h0
