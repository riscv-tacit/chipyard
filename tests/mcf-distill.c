// mcf-distill: a distillate of mcf primal_bea_mpp Loop B, to see in RTL sim/waveform
// exactly what the ld x0 (M_PFR RoCC) prefetch does. Loop B streams a sequential arc
// array; each arc dereferences TWO random node->potential pointers (the cache misses),
// and (when enabled) prefetches both potentials PF_DIST arcs ahead. This is 2 demand
// loads + 2 prefetches per iter (vs the 1+1 prefetch-bench microbench that got +32%).
// Baremetal M-mode, no VM. DRAM pointers (not bss) so crt0 doesn't zero megabytes.
#include <stdio.h>
#include <stdint.h>

#define NNODES   131072u                 // 1 MiB node pool (2x the 512 KiB L2)
#define NARCS    4096u                    // streamed arc scan (72B*4096=288KB; flush evicts it so arc[i+D] is cold on the stream)
#define NODE_ADDR 0x88000000UL            // node potentials (random-accessed -> miss)
#define ARC_ADDR  0x88400000UL            // arc array (sequential -> streamed/hit)

#define PREFETCH(a) asm volatile("ld x0, 0(%0)" :: "r"(a) : "memory")

// 72-byte arc to match real mcf's sizeof(arc_t): the address-gen load arc[i+D]
// then strides D*72 bytes ahead, pushing it past the HW stream-prefetcher reach.
typedef struct arc { int64_t cost; int64_t *tail; int64_t *head; int64_t ident; int64_t pad[5]; } arc_t;

static volatile int64_t * const nodes = (volatile int64_t *)NODE_ADDR;
static arc_t * const arcs = (arc_t *)ARC_ADDR;
static volatile int64_t sink;

static inline uint64_t rdcycle(void){ uint64_t c; asm volatile("rdcycle %0":"=r"(c)); return c; }

// evict node pool: sweep all NNODES sequentially (1 MiB > 512 KiB L2)
static void flush(void){
  int64_t s=0; for(uint32_t i=0;i<NNODES;i++) s+=nodes[i]; sink+=s;
  asm volatile("fence":::"memory");
}

static uint64_t walk(uint32_t dist){
  int64_t sum=0; flush();
  uint64_t t0=rdcycle();
  for(uint32_t i=0;i<NARCS;i++){
    if(dist && i+dist<NARCS){ PREFETCH(arcs[i+dist].tail); PREFETCH(arcs[i+dist].head); }
    if(arcs[i].ident > 0){
      int64_t rc = arcs[i].cost - *(arcs[i].tail) + *(arcs[i].head);
      if(rc < 0) sum += rc;
    }
  }
  uint64_t t1=rdcycle(); sink+=sum; return t1-t0;
}

int main(void){
  // init node pool
  for(uint32_t i=0;i<NNODES;i++) nodes[i]=(int64_t)(i*2654435761u)|1;
  // init arcs: tail/head point to RANDOM nodes (LCG) -> random-access misses
  uint64_t rng=0x123456789abcdef0ull;
  for(uint32_t i=0;i<NARCS;i++){
    rng=rng*6364136223846793005ull+1442695040888963407ull; uint32_t t=(rng>>33)%NNODES;
    rng=rng*6364136223846793005ull+1442695040888963407ull; uint32_t h=(rng>>33)%NNODES;
    arcs[i].cost=(int64_t)(i&7); arcs[i].tail=(int64_t*)&nodes[t];
    arcs[i].head=(int64_t*)&nodes[h]; arcs[i].ident=1;
  }
  printf("mcf-distill: narcs=%u nnodes=%u (1MiB pool, 2 derefs+2 pf/iter)\n",(unsigned)NARCS,(unsigned)NNODES);
  uint64_t base=walk(0);
  printf("no-prefetch:     %lu cyc  (%lu cyc/iter)\n",(unsigned long)base,(unsigned long)(base/NARCS));
  const uint32_t dists[]={4,16};
  for(unsigned d=0;d<sizeof(dists)/sizeof(dists[0]);d++){
    uint64_t t=walk(dists[d]); long delta=(long)base-(long)t;
    printf("prefetch dist=%lu %lu cyc  (%lu cyc/iter)  base-pf=%ld cyc (%c)\n",
      (unsigned long)dists[d],(unsigned long)t,(unsigned long)(t/NARCS),delta,delta>0?'+':'-');
  }
  printf("done (sink=%lu)\n",(unsigned long)sink);
  return 0;
}
