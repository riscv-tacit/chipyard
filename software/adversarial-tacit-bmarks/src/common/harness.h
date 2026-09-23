/* Shared measurement harness for the adversarial TACIT micro-benchmarks.
 * Every kernel is `void k(uint64_t iters, uint64_t a1, uint64_t a2)` in
 * assembly. bench_run() runs it untraced and traced (REPS each, after a
 * warmup) and prints one machine-parseable RESULT line per run. */
#ifndef __ADV_HARNESS_H
#define __ADV_HARNESS_H

#include <stdint.h>
#include "tacit.h"

typedef void (*kernel_fn)(uint64_t iters, uint64_t a1, uint64_t a2);

#ifndef REPS
#define REPS 3
#endif

/* DMA trace buffer: 4 GiB into the physical address space, 1 GiB long.
 * DRAM on the F2 MegaBoom design is 16 GiB from 0x8000_0000, so this sits well
 * above any text/bss of these programs (<= ~100 MiB from 0x8000_0000). */
#ifndef DMA_ADDRESS
#define DMA_ADDRESS  0x100000000ULL
#endif
#ifndef DMA_MAX_SIZE
#define DMA_MAX_SIZE 0x40000000ULL
#endif

void harness_init(const char *family);
void bench_run(const char *name, kernel_fn k, uint64_t iters, uint64_t a1, uint64_t a2);
/* same, but choose the sink (TARGET_ALWAYS / TARGET_DMA / TARGET_FSIM) and
 * lossless (0) vs lossy (1) mode for the traced runs of this bench */
void bench_run_cfg(const char *name, kernel_fn k, uint64_t iters, uint64_t a1, uint64_t a2,
                   uint32_t target, uint32_t lossy);
/* like bench_run_cfg with REPS forced to 1, bracketed by TracerV instruction
 * triggers: `slti x0,x0,0x5A5` (arm) before the untraced run and
 * `slti x0,x0,0x5AD` x4 (disarm) after the traced run, so FireSim's TracerV
 * (+trace-select=3) captures committed-instruction timing of both windows. */
void bench_run_marked(const char *name, kernel_fn k, uint64_t iters, uint64_t a1, uint64_t a2,
                      uint32_t target, uint32_t lossy);
void harness_done(void);

#endif
