// mcf-sweep: characterize the ld x0 (M_PFR RoCC) software prefetch on the mcf Loop B
// pattern, on the FPGA (fast -> big sweeps). Separates the prefetch PRIMITIVE from the
// address-gen TAX:
//   chase : PREFETCH(arc[i+D].tail/head)  -- address-gen loads from the FAT 72B arc (mcf today)
//   direct: PREFETCH(pfa[..])             -- address-gen loads from a DENSE 16B/arc addr array
// and overhead: 2 prefetches/iter vs 1. Demand load is always the fat-struct chase (like mcf).
#include <stdio.h>
#include <stdint.h>

#define NNODES   131072u                  // 1 MiB node pool (2x the 512 KiB L2) -> random derefs miss
#define NARCS    8192u                     // streamed arc scan; 72B*8192=576KB, flushed between walks
#define NODE_ADDR 0x88000000UL
#define ARC_ADDR  0x88200000UL
#define PFA_ADDR  0x88400000UL             // dense array of the 2 node addrs per arc (16B/arc)

#define PREFETCH(a) asm volatile("ld x0, 0(%0)" :: "r"(a) : "memory")
typedef struct arc { int64_t cost; int64_t *tail; int64_t *head; int64_t ident; int64_t pad[5]; } arc_t; // 72B

static volatile int64_t * const nodes = (volatile int64_t *)NODE_ADDR;
static arc_t * const arcs = (arc_t *)ARC_ADDR;
static uint64_t * const pfa = (uint64_t *)PFA_ADDR;   // pfa[2*i]=tail addr, pfa[2*i+1]=head addr
static volatile int64_t sink;

static inline uint64_t rdcycle(void){ uint64_t c; asm volatile("rdcycle %0":"=r"(c)); return c; }
static void flush(void){ int64_t s=0; for(uint32_t i=0;i<NNODES;i++) s+=nodes[i]; sink+=s; asm volatile("fence":::"memory"); }

// mode: 0=none 1=chase2 2=direct2 3=direct1
static uint64_t walk(int mode, uint32_t dist){
  int64_t sum=0; flush();
  uint64_t t0=rdcycle();
  for(uint32_t i=0;i<NARCS;i++){
    if(dist && i+dist<NARCS){
      uint32_t j=i+dist;
      switch(mode){
        case 1: PREFETCH(arcs[j].tail); PREFETCH(arcs[j].head); break;
        case 2: PREFETCH(pfa[2*j]);     PREFETCH(pfa[2*j+1]);   break;
        case 3: PREFETCH(pfa[2*j]);                             break;
        /* chase2 + also prefetch the ARC STRUCT ahead (deepen the strided arc stream so the
           address-gen arcs[j].tail/head hits) -- tests whether the shallow NL HW prefetcher
           is the real cause of the "address-gen tax". */
        case 4: { uint32_t ja=j+8; if(ja<NARCS) PREFETCH(&arcs[ja]); }
                PREFETCH(arcs[j].tail); PREFETCH(arcs[j].head); break;
        default: break;
      }
    }
    if(arcs[i].ident>0){ int64_t rc=arcs[i].cost - *(arcs[i].tail) + *(arcs[i].head); if(rc<0) sum+=rc; }
  }
  uint64_t t1=rdcycle(); sink+=sum; return t1-t0;
}

int main(void){
  for(uint32_t i=0;i<NNODES;i++) nodes[i]=(int64_t)(i*2654435761u)|1;
  uint64_t rng=0x123456789abcdef0ull;
  for(uint32_t i=0;i<NARCS;i++){
    rng=rng*6364136223846793005ull+1442695040888963407ull; uint32_t t=(rng>>33)%NNODES;
    rng=rng*6364136223846793005ull+1442695040888963407ull; uint32_t h=(rng>>33)%NNODES;
    arcs[i].cost=(int64_t)(i&7); arcs[i].tail=(int64_t*)&nodes[t]; arcs[i].head=(int64_t*)&nodes[h]; arcs[i].ident=1;
    pfa[2*i]=(uint64_t)&nodes[t]; pfa[2*i+1]=(uint64_t)&nodes[h];
  }
  printf("mcf-sweep: narcs=%u nnodes=%u (2 derefs/iter). cyc/iter, gain vs none:\n",(unsigned)NARCS,(unsigned)NNODES);
  uint64_t base=walk(0,0); uint32_t bpi=(uint32_t)(base/NARCS);
  printf("none:            %u cyc/iter\n", bpi);
  const uint32_t dists[]={2,4,8,16,32,64};
  const char* names[]={"","chase2","direct2","direct1","chase2+arcpf"};
  for(int m=1;m<=4;m++){
    printf("-- mode %s --\n", names[m]);
    for(unsigned d=0;d<sizeof(dists)/sizeof(dists[0]);d++){
      uint64_t t=walk(m,dists[d]); uint32_t pi=(uint32_t)(t/NARCS);
      long g=(long)bpi-(long)pi;
      printf("  dist=%-3lu %u cyc/iter  gain=%+ld cyc/iter (%c)\n",(unsigned long)dists[d],pi,g,g>0?'+':'-');
    }
  }
  printf("done (sink=%lu)\n",(unsigned long)sink);
  return 0;
}
