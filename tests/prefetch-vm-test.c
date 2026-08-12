// prefetch-vm-test.c
//
// Baremetal SV39 reproducer for the software-prefetch-RoCC hang seen under
// Linux but not under M-mode microbenchmarks.
//
// Why this test exists: in BoomTile the PTW's memory port shares the LSU
// hellacache port (and its single-outstanding hella state machine) with the
// prefetch RoCC through HellaCacheArbiter (v3/common/tile.scala). Baremetal
// M-mode never exercises the PTW; Linux exercises it on every TLB miss. This
// test enables SV39 paging, drops to U-mode, and interleaves ld-x0 prefetches
// with TLB-missing demand loads so page-table walks enter the shared FSM
// while prefetch dcache transactions are still in flight.
//
// Phases (a print precedes each phase, so a hang localizes itself):
//   0: pattern-write test pages through 4 KB mappings (PTW sanity, no prefetch)
//   1: prefetch TLB-warm pages, then load+verify (mirrors working baremetal)
//   2: sfence, prefetch TLB-cold pages (prefetch-triggered walks), load+verify
//   3: sfence, warm target TLB entries, prefetch (dcache misses in flight),
//      immediately demand-load TLB-cold pages -> PTW during prefetch
//   4: hostile prefetches: unmapped leaf, invalid root region, non-canonical
//   5: stress mix with fences and ecall trap boundaries
//   6: MMIO probe: prefetch to CLINT mtimecmp; if the IOMSHR turns M_PFR into
//      a TileLink Put (isRead(M_PFR)=false), the sentinel gets clobbered.
//
// Expected on buggy RTL: hang (BOOM's "Pipeline has hung" assertion) or a
// FATAL trap / data mismatch in phase 2/3/5, and/or a phase-6 BUG report.

#include <stdio.h>
#include <stdint.h>
#include <stddef.h>

#define read_csr(reg) ({ unsigned long __v; \
  asm volatile("csrr %0, " #reg : "=r"(__v)); __v; })
#define write_csr(reg, v) \
  asm volatile("csrw " #reg ", %0" :: "rK"((unsigned long)(v)))
#define set_csr(reg, v) \
  asm volatile("csrs " #reg ", %0" :: "rK"((unsigned long)(v)))

// ---------------------------------------------------------------- constants
#define PAGE      4096UL
#define MEGA      (1UL << 21)

#define PTE_V     (1UL << 0)
#define PTE_R     (1UL << 1)
#define PTE_W     (1UL << 2)
#define PTE_X     (1UL << 3)
#define PTE_U     (1UL << 4)
#define PTE_A     (1UL << 6)
#define PTE_D     (1UL << 7)
#define PPN(pa)   ((((uint64_t)(pa)) >> 12) << 10)

#define PTE_LEAF_RWX (PTE_V | PTE_R | PTE_W | PTE_X | PTE_U | PTE_A | PTE_D)
#define PTE_LEAF_RW  (PTE_V | PTE_R | PTE_W | PTE_U | PTE_A | PTE_D)

#define DRAM_BASE  0x80000000UL
#define CLINT_BASE 0x02000000UL

// U->M ecall numbers
#define SYS_EXIT_PASS 0
#define SYS_SFENCE    1
#define SYS_EXIT_FAIL 2
#define SYS_MSPIN     3 // dwell in the M-mode handler for arg cycles
#define SYS_TIMER_ON  4 // enable a frequent M-mode timer interrupt (arg=interval cyc)
#define SYS_TIMER_OFF 5 // disable the timer interrupt

#define MTIME_ADDR    (CLINT_BASE + 0xBFF8) // CLINT mtime (physical, M-mode)
#define MTIMECMP_ADDR (CLINT_BASE + 0x4000) // CLINT hart0 mtimecmp (physical)
#define MIP_MTIP      (1UL << 7)
#define MIE_MTIE      (1UL << 7)
static uint64_t timer_interval = 0; // set by SYS_TIMER_ON, used by the ISR

// Test window: a virtual region with no identity backing, mapped 4 KB-page
// by 4 KB-page onto a small physical pool (Linux-like non-identity paging).
// Window pages >= NPAGES are left unmapped (prefetch bait).
#define VBASE   0x100000000UL // vaddr of window: vpn[2]=4 -> pt_root[4]
#define NPAGES  256           // mapped/exercised pages (= pool size, 1 MiB:
                              // larger than the 512 KiB L2 so dirty sweeps
                              // force inclusive-L2 evictions -> back-probes)

// The 0x2000000 vaddr region (numerically the CLINT's paddr range) is mapped
// to ordinary RAM pool pages: prefetching there in U-mode is a completely
// innocent heap access (mcf-style pointer). If a queued prefetch instead
// drains during an M-mode window, translation is passthrough and the same
// number becomes the CLINT's PHYSICAL address.
#define V_MCF   (CLINT_BASE + 0x4000) // innocent U vaddr == mtimecmp paddr

// The CLINT itself is reachable through a separate alias window (root[8]),
// used for sentinel writes/reads and the direct-uncacheable-prefetch probe.
#define ABASE          0x200000000UL
#define MTIMECMP_ALIAS (ABASE + 0x4000)

// ------------------------------------------------------------------ statics
static uint64_t pt_root[512]     __attribute__((aligned(4096)));
static uint64_t pt_dram_mid[512] __attribute__((aligned(4096)));
static uint64_t pt_win_mid[512]  __attribute__((aligned(4096)));
static uint64_t pt_leaf[512]     __attribute__((aligned(4096)));
static uint64_t pt_io_mid[512]   __attribute__((aligned(4096)));
static uint64_t pt_io_leaf[512]  __attribute__((aligned(4096)));
static uint64_t pt_al_mid[512]   __attribute__((aligned(4096)));
static uint64_t pt_al_leaf[512]  __attribute__((aligned(4096)));

// Physical backing for the window (kept small: crt0 zeroes all of bss)
static uint8_t pool[NPAGES * PAGE] __attribute__((aligned(4096)));

static uint8_t m_stack[8192] __attribute__((aligned(16)));

extern volatile uint64_t tohost;

// ------------------------------------------------------------------ helpers
#define PREFETCH(a) asm volatile("ld x0, 0(%0)" :: "r"(a) : "memory")
#define FENCE()     asm volatile("fence rw, rw" ::: "memory")

static inline uint64_t rdcycle(void)
{
  uint64_t c;
  asm volatile("rdcycle %0" : "=r"(c));
  return c;
}

static inline long ecall(long num, long arg)
{
  register long a0 asm("a0") = arg;
  register long a7 asm("a7") = num;
  asm volatile("ecall" : "+r"(a0) : "r"(a7) : "memory");
  return a0;
}

// riscv-tests-style direct exit; safe from any mode with identity mapping
static void die(uint64_t code)
{
  tohost = (code << 1) | 1;
  for (;;)
    ;
}

static inline uint64_t pat(size_t page, size_t off)
{
  return 0x5AA5000000000000UL ^ ((uint64_t)page << 16) ^ (uint64_t)off;
}

// -------------------------------------------------------- M-mode trap entry
// Saves caller-saved regs on the M stack (mscratch holds its top), calls
// m_trap_c(frame), restores, mret. Frame layout must match F_* below.
#define F_A0 4
#define F_A7 11

asm(
  ".section .text\n"
  ".align 4\n"
  ".global m_trap_entry\n"
  "m_trap_entry:\n"
  "  csrrw sp, mscratch, sp\n"
  "  addi  sp, sp, -128\n"
  "  sd ra,   0(sp)\n"
  "  sd t0,   8(sp)\n"
  "  sd t1,  16(sp)\n"
  "  sd t2,  24(sp)\n"
  "  sd a0,  32(sp)\n"
  "  sd a1,  40(sp)\n"
  "  sd a2,  48(sp)\n"
  "  sd a3,  56(sp)\n"
  "  sd a4,  64(sp)\n"
  "  sd a5,  72(sp)\n"
  "  sd a6,  80(sp)\n"
  "  sd a7,  88(sp)\n"
  "  sd t3,  96(sp)\n"
  "  sd t4, 104(sp)\n"
  "  sd t5, 112(sp)\n"
  "  sd t6, 120(sp)\n"
  "  mv a0, sp\n"
  "  call m_trap_c\n"
  "  ld ra,   0(sp)\n"
  "  ld t0,   8(sp)\n"
  "  ld t1,  16(sp)\n"
  "  ld t2,  24(sp)\n"
  "  ld a0,  32(sp)\n"
  "  ld a1,  40(sp)\n"
  "  ld a2,  48(sp)\n"
  "  ld a3,  56(sp)\n"
  "  ld a4,  64(sp)\n"
  "  ld a5,  72(sp)\n"
  "  ld a6,  80(sp)\n"
  "  ld a7,  88(sp)\n"
  "  ld t3,  96(sp)\n"
  "  ld t4, 104(sp)\n"
  "  ld t5, 112(sp)\n"
  "  ld t6, 120(sp)\n"
  "  addi  sp, sp, 128\n"
  "  csrrw sp, mscratch, sp\n"
  "  mret\n"
);
extern char m_trap_entry[];

void m_trap_c(uint64_t *f) __attribute__((used));
void m_trap_c(uint64_t *f)
{
  uint64_t cause = read_csr(mcause);
  uint64_t epc   = read_csr(mepc);

  // M-mode timer interrupt: reprogram mtimecmp and resume (mepc unchanged).
  // Each of these is a rob flush taken at the head while U-mode runs the
  // prefetch loop — the directed trigger for the PNR/flush orphan.
  if (cause == ((1UL << 63) | 7)) {
    *(volatile uint64_t *)MTIMECMP_ADDR =
        *(volatile uint64_t *)MTIME_ADDR + timer_interval;
    return; // do NOT advance mepc — resume the interrupted instruction
  }

  if (cause == 8) { // ecall from U-mode
    switch (f[F_A7]) {
    case SYS_SFENCE:
      asm volatile("sfence.vma" ::: "memory");
      break;
    case SYS_TIMER_ON:
      timer_interval = f[F_A0];
      *(volatile uint64_t *)MTIMECMP_ADDR =
          *(volatile uint64_t *)MTIME_ADDR + timer_interval;
      set_csr(mie, MIE_MTIE); // M timer ints fire while in U-mode (not delegated)
      break;
    case SYS_TIMER_OFF:
      write_csr(mie, 0);
      *(volatile uint64_t *)MTIMECMP_ADDR = ~0UL;
      break;
    case SYS_MSPIN: { // dwell in M-mode while queued prefetches drain
      uint64_t t0 = read_csr(mcycle);
      while (read_csr(mcycle) - t0 < f[F_A0])
        ;
      break;
    }
    case SYS_EXIT_PASS:
      printf("ALL PHASES COMPLETE -- no hang\n");
      die(0); // tohost=1 -> *** PASSED ***
    case SYS_EXIT_FAIL:
      printf("TEST FAILED (data mismatch), code=%ld\n", (long)f[F_A0]);
      die(2);
    default:
      printf("unknown ecall %ld\n", (long)f[F_A7]);
      die(3);
    }
    write_csr(mepc, epc + 4);
    return;
  }

  printf("\nFATAL: unexpected trap mcause=0x%lx mepc=0x%lx mtval=0x%lx\n",
         cause, epc, read_csr(mtval));
  die(0x100 | cause);
}

// ------------------------------------------------------------- page tables
static void build_page_tables(void)
{
  // 1 GB of DRAM at 0x80000000: identity 2 MB megapages (code/data/stack)
  for (int i = 0; i < 512; i++)
    pt_dram_mid[i] = PPN(DRAM_BASE + ((uint64_t)i << 21)) | PTE_LEAF_RWX;

  // test window at VBASE: 4 KB pages onto pool[]; pages >= NPAGES unmapped
  pt_win_mid[0] = PPN(pt_leaf) | PTE_V;
  for (int j = 0; j < 512; j++)
    pt_leaf[j] = (j < NPAGES)
               ? (PPN((uint64_t)pool + (uint64_t)j * PAGE) | PTE_LEAF_RW)
               : 0;

  // vaddrs 0x2000000..0x2010000 (numerically the CLINT paddr range) map to
  // ordinary RAM pool pages 112..127 -> prefetching V_MCF in U-mode is a
  // plain heap prefetch, mcf-style
  pt_io_mid[(CLINT_BASE >> 21) & 0x1FF] = PPN(pt_io_leaf) | PTE_V;
  for (int j = 0; j < 16; j++)
    pt_io_leaf[j] = PPN((uint64_t)pool + (uint64_t)(112 + j) * PAGE) | PTE_LEAF_RW;

  // alias window at ABASE: the real CLINT, for sentinel access and the
  // direct uncacheable-prefetch probe
  pt_al_mid[0] = PPN(pt_al_leaf) | PTE_V;
  for (int j = 0; j < 16; j++)
    pt_al_leaf[j] = PPN(CLINT_BASE + (uint64_t)j * PAGE) | PTE_LEAF_RW;

  pt_root[0] = PPN(pt_io_mid) | PTE_V;
  pt_root[2] = PPN(pt_dram_mid) | PTE_V;
  pt_root[4] = PPN(pt_win_mid) | PTE_V; // VBASE window
  pt_root[8] = PPN(pt_al_mid) | PTE_V;  // ABASE alias of the CLINT
}

// ---------------------------------------------------------- U-mode payload
static int fail_mask; // phases 6/7 report-and-continue; checked at the end

static volatile uint64_t *page_u64(size_t page, size_t off)
{
  return (volatile uint64_t *)(VBASE + page * PAGE + off);
}

static void warm_tlb(size_t page)
{
  (void)*(volatile uint8_t *)(VBASE + page * PAGE);
}

static void check(size_t page, size_t off, int code)
{
  uint64_t v = *page_u64(page, off);
  if (v != pat(page, off)) {
    printf("  MISMATCH page %lu off %lu: got 0x%lx want 0x%lx\n",
           page, off, v, pat(page, off));
    ecall(SYS_EXIT_FAIL, code);
  }
}

static void phase0(void)
{
  printf("phase 0: pattern-write %d pages through 4KB mappings (PTW sanity)\n",
         NPAGES);
  for (size_t p = 0; p < NPAGES; p++) {
    *page_u64(p, 0)    = pat(p, 0);
    *page_u64(p, 2048) = pat(p, 2048);
  }
  for (size_t p = 0; p < NPAGES; p += 17)
    check(p, 2048, 10);
  printf("phase 0 OK\n");
}

static void phase1(void)
{
  printf("phase 1: prefetch TLB-warm pages (the known-good baremetal case)\n");
  for (size_t p = 0; p < 16; p++)
    warm_tlb(p); // warm TLB + line 0
  uint64_t t0 = rdcycle();
  for (size_t p = 0; p < 16; p++)
    PREFETCH(page_u64(p, 2048));
  for (size_t p = 0; p < 16; p++)
    check(p, 2048, 11);
  printf("phase 1 OK (%lu cycles)\n", rdcycle() - t0);
}

static void phase2(void)
{
  printf("phase 2: prefetch TLB-cold pages (prefetch-triggered PTW walks)\n");
  ecall(SYS_SFENCE, 0);
  for (size_t p = 32; p < 48; p++)
    PREFETCH(page_u64(p, 2048)); // TLB miss -> walk launched, prefetch dropped
  for (size_t p = 32; p < 48; p++)
    check(p, 2048, 20); // demand loads racing the walks above
  printf("phase 2 OK\n");
}

static void phase3(void)
{
  printf("phase 3: PTW walks while prefetch dcache misses are in flight\n");
  for (int it = 0; it < 24; it++) {
    ecall(SYS_SFENCE, 0);
    size_t base = (size_t)(it * 16) % NPAGES;
    // warm TLB entries for the prefetch targets (touch line 0 only)
    for (size_t k = 0; k < 8; k++)
      warm_tlb((base + k) % NPAGES);
    // prefetches: TLB hit, dcache miss -> MSHR transactions in flight
    size_t off_pf = 1024 + 64 * (it % 16); // fresh line each sweep
    for (size_t k = 0; k < 8; k++)
      PREFETCH(page_u64((base + k) % NPAGES, off_pf));
    // demand loads to TLB-cold pages: PTW enters the shared hella FSM now
    for (size_t k = 0; k < 8; k++)
      check((base + 8 + k) % NPAGES, 2048, 30);
  }
  printf("phase 3 OK\n");
}

static void phase4(void)
{
  printf("phase 4: hostile prefetches (unmapped / invalid root / bad va)\n");
  PREFETCH((void *)(VBASE + 508 * PAGE)); // leaf PTE V=0 -> walk finds invalid
  PREFETCH((void *)0x140000000UL);        // root[5] invalid -> walk fails fast
  PREFETCH((void *)0xFFFF800000000000UL); // non-canonical -> bad_va
  PREFETCH((void *)0x4000000000UL);       // non-canonical (bit 38 set)
  ecall(SYS_SFENCE, 0);
  check(1, 2048, 40); // liveness: a real walk after the failed ones
  printf("phase 4 OK\n");
}

static void phase5(void)
{
  printf("phase 5: stress mix (sfence/fence/ecall + prefetch + cold loads)\n");
  for (int it = 0; it < 40; it++) {
    if ((it & 3) == 0)
      ecall(SYS_SFENCE, 0);
    size_t base = (size_t)(it * 8) % NPAGES;
    size_t off_pf = 1024 + 64 * ((it + 7) % 16);
    for (size_t k = 0; k < 4; k++)
      PREFETCH(page_u64((base + k) % NPAGES, off_pf));
    if ((it & 7) == 0)
      FENCE(); // fence while prefetches may be in flight (io.rocc.busy)
    if ((it % 5) == 0)
      PREFETCH((void *)(VBASE + 509 * PAGE)); // sprinkle an unmapped prefetch
    for (size_t k = 4; k < 8; k++)
      check((base + k) % NPAGES, 2048, 50);
    if ((it % 10) == 9)
      printf("  stress iter %d OK\n", it + 1);
  }
  printf("phase 5 OK\n");
}

static void phase6(void)
{
  printf("phase 6: MMIO probe (does a prefetch to uncacheable become a Put?)\n");
  volatile uint64_t *mtimecmp = (volatile uint64_t *)MTIMECMP_ALIAS;
  const uint64_t sentinel = 0xDEADBEEFCAFEF00DUL;
  *mtimecmp = sentinel;
  FENCE();
  if (*mtimecmp != sentinel) {
    printf("  mmio sanity write failed\n");
    ecall(SYS_EXIT_FAIL, 60);
  }
  PREFETCH(mtimecmp);
  uint64_t t0 = rdcycle();
  while (rdcycle() - t0 < 2000)
    ; // give the (buggy) Put time to land
  uint64_t v = *mtimecmp;
  *mtimecmp = ~0UL; // park the timer far away again
  if (v != sentinel) {
    printf("  BUG CONFIRMED: prefetch to MMIO clobbered mtimecmp -> 0x%lx\n", v);
    fail_mask |= 1;
    return;
  }
  printf("  mtimecmp intact (no stray MMIO write)\n");
  printf("phase 6 OK\n");
}

// The mcf scenario: prefetch a perfectly innocent U-mapped RAM vaddr whose
// NUMBER equals the CLINT mtimecmp paddr, then immediately trap to M-mode and
// dwell there. Queued prefetches drain during the M window; with the global
// dprv the DTLB goes passthrough, so the innocent vaddr is reinterpreted as
// the CLINT's physical address -> (buggy) stray Put clobbers the sentinel.
static void phase7(void)
{
  printf("phase 7: mcf scenario (innocent prefetch draining in an M window)\n");
  volatile uint64_t *mtimecmp = (volatile uint64_t *)MTIMECMP_ALIAS;
  const uint64_t sentinel = 0x1BADC0DE5AFE5AFEUL;
  // sanity: V_MCF really is plain RAM under the U-mode mapping
  *(volatile uint64_t *)V_MCF = 42;
  if (*(volatile uint64_t *)V_MCF != 42) {
    printf("  V_MCF mapping sanity failed\n");
    ecall(SYS_EXIT_FAIL, 70);
  }
  for (int it = 0; it < 20; it++) {
    *mtimecmp = sentinel;
    FENCE();
    // burst of prefetches: fill the RoCC queue so the drain outlives the trap
    for (int k = 0; k < 12; k++)
      PREFETCH((void *)V_MCF);
    ecall(SYS_MSPIN, 800); // trap now; drain happens with dprv=M, passthrough
    uint64_t v = *mtimecmp;
    if (v != sentinel) {
      printf("  BUG CONFIRMED (iter %d): innocent RAM-vaddr prefetch drained\n"
             "  in M-mode, went passthrough, and clobbered mtimecmp -> 0x%lx\n",
             it, v);
      *mtimecmp = ~0UL;
      fail_mask |= 2;
      return;
    }
  }
  *mtimecmp = ~0UL;
  printf("  mtimecmp intact across 20 trap-drain windows\n");
  printf("phase 7 OK\n");
}

// MSHR-saturation pressure: concurrent demand misses (PTW walks + MSHRs),
// prefetch bursts (hella port + MSHRs), and dirty stores (evictions through
// the writeback unit) — the collision windows that only show under load:
// stale prefetch nacks vs waiting PTW walks, Release/ProbeAck source overlap.
static void phase8(void)
{
  printf("phase 8: MSHR/writeback/L2 saturation with prefetch+PTW interleave\n");
  for (int it = 0; it < 64; it++) {
    ecall(SYS_SFENCE, 0);
    size_t base = (size_t)(it * 24) % NPAGES;
    size_t off = 64 * ((it % 13) + 16); // lines 16..28: clear of pattern offs
    // dirty a rotating set of far pages (future eviction victims)
    for (size_t k = 0; k < 8; k++)
      *page_u64((base + 64 + k * 7) % NPAGES, off) = pat(0, 0);
    // prefetch burst: TLB-cold -> walks; misses -> MSHRs
    for (size_t k = 0; k < 8; k++)
      PREFETCH(page_u64((base + k) % NPAGES, off));
    // demand misses to other TLB-cold pages: PTW walks racing the prefetches
    for (size_t k = 8; k < 16; k++)
      check((base + k) % NPAGES, 2048, 80);
    // every 8 iters: dirty-sweep full stripes across the whole 1 MiB pool so
    // the inclusive L2 overflows -> back-probes race Release traffic in the
    // writeback unit while prefetch/PTW pressure continues
    if ((it & 7) == 7) {
      for (int s = 0; s < 4; s++) {
        size_t soff = 64 * (33 + (((size_t)(it / 8) * 4 + (size_t)s) % 31));
        for (size_t p = 0; p < NPAGES; p++)
          *page_u64(p, soff) = pat(0, 0);
      }
      printf("  pressure iter %d OK\n", it + 1);
    }
  }
  printf("phase 8 OK\n");
}

// RoCC-writeback starvation repro: spam ld x0 prefetches while a heavy stream
// of independent cache-missing loads floods the memory-response port
// (ll_wbarb.in(0), highest priority, never backpressured). The prefetch's
// RT_X writeback rides in(2) (lowest priority); if it can't retire, the ROB
// head wedges — the exact mcf hang localized by TracerV.
static void phase9(void)
{
  printf("phase 9: RoCC-writeback starvation repro (ld x0 + heavy load stream)\n");
  const size_t SPAN = (size_t)NPAGES * PAGE; // 1 MiB, larger than L2
  // init the whole pool so loads read defined data (no X-prop under VCS)
  for (size_t p = 0; p < NPAGES; p++)
    for (size_t o = 0; o < PAGE; o += 64)
      *(volatile uint64_t *)(VBASE + p * PAGE + o) = (uint64_t)(p * PAGE + o);
  ecall(SYS_SFENCE, 0);

  volatile uint64_t acc = 0;
  for (int it = 0; it < 4000000; it++) {
    // a prefetch each iteration -> RT_X uop needing an in(2) writeback
    PREFETCH((void *)(VBASE + ((((size_t)it * 4099) % SPAN) & ~63UL)));
    // 24 mutually-distant independent loads: all miss, saturate MSHRs, keep
    // mem_resps streaming on in(0) so in(2) is maximally contended
    size_t b = ((size_t)it * 3904) % SPAN; // 3904 = 61*64, line-strided
    for (int k = 0; k < 24; k++)
      acc += *(volatile uint64_t *)(VBASE + ((b + (size_t)k * 65600) % SPAN));
    if (((unsigned)it & 0x7ffff) == 0x7ffff)
      printf("  iter %d acc=0x%lx\n", it + 1, (unsigned long)acc);
  }
  printf("phase 9 OK acc=0x%lx\n", (unsigned long)acc);
}

// Directed trigger for the PNR/flush orphan (the mcf hang). A rob flush is
// taken at the ROB head and squashes everything younger; if a past-PNR RoCC
// prefetch (between head and PNR) is in the RXQ, stock preserves its entry ->
// orphan -> ROB-head wedge. We amplify the ~1e-5 FPGA rarity by (1) a tight
// pointer chase whose prefetch base is load-produced and frequently L2-misses
// (so prefetches linger past-PNR, unfired, in the RXQ) and (2) a frequent
// M-mode timer interrupt (an async rob flush every ~few-hundred cycles). Each
// interrupt is a fresh chance to catch a lingering prefetch.
static void phase10(void)
{
  printf("phase 10: directed PNR/flush-orphan trigger (freq timer irq + chase pf)\n");
  const size_t LINE   = 64;
  const size_t STRIDE = LINE / sizeof(uintptr_t);        // 8 u64 per line
  const size_t NNODES = (size_t)NPAGES * PAGE / LINE;    // 16384 nodes over 1 MiB (> L2)
  volatile uintptr_t *base = (volatile uintptr_t *)VBASE;
  // Build a permuted ring: node[i] holds &node[(i+STEP) mod NNODES]. NNODES is
  // a power of two, so any odd STEP yields a single full-length cycle.
  size_t idx = 0;
  const size_t STEP = 12421; // odd -> coprime with 2^14
  for (size_t i = 0; i < NNODES; i++) {
    size_t nxt = (idx + STEP) % NNODES;
    base[idx * STRIDE] = (uintptr_t)&base[nxt * STRIDE];
    idx = nxt;
  }
  ecall(SYS_SFENCE, 0);
  ecall(SYS_TIMER_ON, 300); // rob-flush timer irq roughly every 300 cycles

  volatile uintptr_t *cur = &base[0];
  uint64_t acc = 0;
  for (uint64_t i = 0; i < 20000000UL; i++) {
    uintptr_t nxt = *cur;              // ld a4, 0(cur)  -- load-produced base
    PREFETCH((void *)nxt);             // ld x0, 0(a4)   -- prefetch, base = load result
    cur = (volatile uintptr_t *)nxt;   // chase (dependent miss -> prefetch lingers)
    acc += nxt;
    if (((unsigned)i & 0x3ffff) == 0x3ffff)
      printf("  chase iter %lu\n", (unsigned long)(i + 1));
  }
  ecall(SYS_TIMER_OFF, 0);
  printf("phase 10 OK (acc=0x%lx)\n", (unsigned long)acc);
}

// Build the permuted single-cycle ring used by the double-poison repros.
// node[i] holds &node[(i+STEP)%NNODES]; NNODES=2^14, STEP odd -> one full cycle.
static volatile uintptr_t *build_chase_ring(void)
{
  const size_t LINE   = 64;
  const size_t STRIDE = LINE / sizeof(uintptr_t);
  const size_t NNODES = (size_t)NPAGES * PAGE / LINE;   // 16384 nodes / 1 MiB (>> 32KB L1)
  volatile uintptr_t *base = (volatile uintptr_t *)VBASE;
  size_t idx = 0;
  const size_t STEP = 12421;
  for (size_t i = 0; i < NNODES; i++) {
    size_t nxt = (idx + STEP) % NNODES;
    base[idx * STRIDE] = (uintptr_t)&base[nxt * STRIDE];
    idx = nxt;
  }
  ecall(SYS_SFENCE, 0);
  return base;
}

// DOUBLE-POISON repro (2026-07-08). The mcf hang is a spec_ld_wakeup DOUBLE
// poison: issue-slot.scala:215 assert(!next_p1_poisoned) fired in the MEM issue
// queue on FPGA at cyc 5.3B. Mechanism: fast-load-use speculatively wakes a
// dependent load (poison); the SAME producer load, itself squashed by an older
// ldspec_miss, re-fires `incoming` in back-to-back cycles and broadcasts
// spec_ld_wakeup for its pdst twice, double-poisoning its consumer before the
// first poison clears. A tight all-L1-missing dependent pointer chase maximises
// this: every hop's load produces the next hop's address, so every triple of
// consecutive missing hops is a G->P->C candidate.
//
// phase 11 = PURE chase, NO prefetch: does stock fast-load-use double-poison on
// its own? (If yes, the bug is not prefetch-specific; prefetch only amplifies.)
static void phase11(void)
{
  printf("phase 11: spec_ld_wakeup double-poison repro (PURE missing chase, no pf)\n");
  volatile uintptr_t *base = build_chase_ring();
  volatile uintptr_t *cur = &base[0];
  uint64_t acc = 0;
  for (uint64_t i = 0; i < 300000000UL; i++) {
    uintptr_t nxt = *cur;              // dependent, L1-missing load (producer chain)
    cur = (volatile uintptr_t *)nxt;   // chase: next load consumes nxt
    acc += nxt;
    if (((unsigned)i & 0xfffff) == 0xfffff)
      printf("  chase iter %lu\n", (unsigned long)(i + 1));
  }
  printf("phase 11 OK (acc=0x%lx)\n", (unsigned long)acc);
}

// phase 12 = missing chase + a SCATTERED prefetch. The prefetch base is
// load-produced (nxt) but XOR'd to a far line (nxt ^ 0x8000, same 1 MiB pool,
// different cache line, revisited only ~thousands of hops later so it is
// evicted before use) -> it does NOT warm the immediate next chase load, so the
// chain keeps missing while the prefetch feature piles on MSHR/cache pressure.
static void phase12(void)
{
  printf("phase 12: double-poison repro (missing chase + scattered ld x0 prefetch)\n");
  volatile uintptr_t *base = build_chase_ring();
  volatile uintptr_t *cur = &base[0];
  uint64_t acc = 0;
  for (uint64_t i = 0; i < 300000000UL; i++) {
    uintptr_t nxt = *cur;              // dependent, L1-missing load
    PREFETCH((void *)(nxt ^ 0x8000));  // ld x0 on a load-produced, far address
    cur = (volatile uintptr_t *)nxt;   // chase
    acc += nxt;
    if (((unsigned)i & 0xfffff) == 0xfffff)
      printf("  chase iter %lu\n", (unsigned long)(i + 1));
  }
  printf("phase 12 OK (acc=0x%lx)\n", (unsigned long)acc);
}

// phase 13 = phase 10 CONTROL: identical missing chase + 300-cyc timer IRQ but
// NO prefetch. Discriminator for the p10 iq-notreq-p1 wedge: if p13 completes
// (or wedges differently) while p10 wedged, the prefetch is REQUIRED -> the
// flush-loses-base-wakeup wedge is prefetch-specific, not a generic frequent-IRQ
// pathology. (No uopROCC here, so the iqdiag STAGE=iq-notreq-p1 assert cannot
// fire by construction; a generic wedge would show as Pipeline-has-hung.)
static void phase13(void)
{
  printf("phase 13: CONTROL - missing chase + 300-cyc timer IRQ, NO prefetch\n");
  volatile uintptr_t *base = build_chase_ring();
  ecall(SYS_TIMER_ON, 300);
  volatile uintptr_t *cur = &base[0];
  uint64_t acc = 0;
  for (uint64_t i = 0; i < 20000000UL; i++) {
    uintptr_t nxt = *cur;              // dependent, L1-missing load (no prefetch)
    cur = (volatile uintptr_t *)nxt;
    acc += nxt;
    if (((unsigned)i & 0xfffff) == 0xfffff)
      printf("  chase iter %lu\n", (unsigned long)(i + 1));
  }
  ecall(SYS_TIMER_OFF, 0);
  printf("phase 13 OK (acc=0x%lx)\n", (unsigned long)acc);
}

static void u_main(void) __attribute__((noreturn, used));
static void u_main(void)
{
  printf("now in U-mode with SV39 paging on\n");
#if defined(PHASE13_ONLY)
  phase13();
#elif defined(PHASE12_ONLY)
  phase12();
#elif defined(PHASE11_ONLY)
  phase11();
#elif defined(PHASE10_ONLY)
  phase10();
#elif defined(PHASE9_ONLY)
  phase9();
#else
  phase0(); // pattern init: phase 8's checks depend on it
#ifndef PHASE8_ONLY
  phase1();
  phase2();
  phase3();
  phase4();
  phase5();
  phase6();
  phase7();
#endif
  phase8();
#endif
  if (fail_mask) {
    printf("MMIO probes failed, mask=%d (1=direct uncacheable, 2=mcf drain)\n",
           fail_mask);
    ecall(SYS_EXIT_FAIL, fail_mask);
  }
  ecall(SYS_EXIT_PASS, 0);
  __builtin_unreachable();
}

// -------------------------------------------------------------- M-mode main
int main(void)
{
  printf("prefetch-vm-test: SV39 + U-mode, PTW vs prefetch on shared port\n");

  write_csr(mtvec, (uint64_t)m_trap_entry);
  write_csr(mscratch, (uint64_t)(m_stack + sizeof(m_stack)));

  // PMP entry 0: NAPOT covering everything, RWX, so S/U accesses pass
  write_csr(pmpaddr0, ~0UL);
  write_csr(pmpcfg0, 0x1F);

  // let U-mode use rdcycle
  write_csr(mcounteren, ~0UL);
  write_csr(scounteren, ~0UL);

  // all traps to M-mode, interrupts off
  write_csr(medeleg, 0);
  write_csr(mideleg, 0);
  write_csr(mie, 0);

  build_page_tables();
  write_csr(satp, (8UL << 60) | ((uint64_t)pt_root >> 12)); // SV39, ASID 0
  asm volatile("sfence.vma" ::: "memory");

  uint64_t ms = read_csr(mstatus);
  ms &= ~(3UL << 11); // MPP = U
  ms &= ~(1UL << 7);  // MPIE = 0: interrupts stay off after mret
  ms |= (1UL << 18);  // SUM (harmless here)
  ms |= (3UL << 13);  // FS = dirty so FP context stays legal
  write_csr(mstatus, ms);
  write_csr(mepc, (uint64_t)&u_main);
  asm volatile("mret");
  __builtin_unreachable();
}
