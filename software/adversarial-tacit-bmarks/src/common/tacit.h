/* TACIT encoder + DMA sink MMIO map for bare-metal M-mode use.
 * Derived from firemarshal/example-workloads/bare-hello/tacit.h, extended with
 * the DMA sink's source-ready stall counter (0x24) so sink backpressure is
 * observable alongside the encoder's own stall counter.
 * Register offsets: generators/rocket-chip/.../trace/TraceEncoderController.scala
 *                   generators/tacit/src/main/scala/TraceSinkDMA.scala          */
#ifndef __L_TRACE_ENCODER_H
#define __L_TRACE_ENCODER_H

#include <stddef.h>
#include <stdint.h>

#define __I  volatile const
#define __IO volatile

typedef struct {
  __IO uint32_t TR_TE_CTRL;         /* 0x00: bit1 = enable                     */
  __I  uint32_t TR_TE_INFO;         /* 0x04                                    */
  __IO uint32_t TR_TE_BUBBLE[6];    /* 0x08-0x1C                               */
  __IO uint32_t TR_TE_TARGET;       /* 0x20: sink id                           */
  __IO uint32_t TR_TE_BRANCH_MODE;  /* 0x24: 0 = target (only mode on BOOM)    */
  __IO uint64_t TR_TE_STALL_COUNT;  /* 0x28: cycles queues at high watermark   */
  __IO uint32_t TR_TE_LOSSY;        /* 0x30                                    */
  __IO uint32_t _pad0;              /* 0x34                                    */
  __I  uint64_t TR_TE_GAP_CYCLES;   /* 0x38                                    */
  __I  uint64_t TR_TE_DROPPED;      /* 0x40                                    */
  __I  uint64_t TR_TE_PAUSE_COUNT;  /* 0x48                                    */
  __I  uint64_t TR_TE_DROPPED_INSNS;/* 0x50                                    */
} LTraceEncoderType;

typedef struct {
  __IO uint64_t TR_SK_DMA_ADDR;       /* 0x00 */
  __I  uint64_t TR_SK_DMA_COUNT;      /* 0x08: bytes written to memory so far */
  __IO uint64_t TR_SK_DMA_MAX_SIZE;   /* 0x10 */
  __IO uint32_t TR_SK_DMA_RESET;      /* 0x18 */
  __IO uint32_t TR_SK_DMA_MODE;       /* 0x1C: 0 overflow, 1 ring buffer      */
  __I  uint32_t TR_SK_DMA_WRAP_COUNT; /* 0x20 */
  __I  uint32_t TR_SK_DMA_SRC_STALL;  /* 0x24: cycles no TL source id free    */
} LTraceSinkDmaType;

#define TARGET_ALWAYS 0x0
#define TARGET_DMA    0x1
#define TARGET_FSIM   0x2

#define BRANCH_MODE_TARGET  0x0
#define BRANCH_MODE_PREDICT 0x2

#define DMA_MODE_OVERFLOW    0x0
#define DMA_MODE_RING_BUFFER 0x1

#define L_TRACE_ENCODER_BASE_ADDRESS  0x3000000UL
#define L_TRACE_SINK_DMA_BASE_ADDRESS 0x3010000UL

static inline LTraceEncoderType *l_trace_encoder_get(uint64_t hart_id) {
  return (LTraceEncoderType *)(L_TRACE_ENCODER_BASE_ADDRESS + hart_id * 0x1000);
}
static inline LTraceSinkDmaType *l_trace_sink_dma_get(uint64_t hart_id) {
  return (LTraceSinkDmaType *)(L_TRACE_SINK_DMA_BASE_ADDRESS + hart_id * 0x1000);
}

static inline void l_trace_encoder_start(LTraceEncoderType *e) { e->TR_TE_CTRL |= (1u << 1); }
static inline void l_trace_encoder_stop(LTraceEncoderType *e)  { e->TR_TE_CTRL &= ~(1u << 1); }
static inline uint64_t l_trace_encoder_get_stall_count(LTraceEncoderType *e) { return e->TR_TE_STALL_COUNT; }
static inline void l_trace_encoder_configure_target(LTraceEncoderType *e, uint32_t t) { e->TR_TE_TARGET = t; }
static inline void l_trace_encoder_configure_branch_mode(LTraceEncoderType *e, uint32_t m) { e->TR_TE_BRANCH_MODE = m; }

static inline void l_trace_sink_dma_configure_addr(LTraceSinkDmaType *d, uint64_t a) { d->TR_SK_DMA_ADDR = a; }
static inline void l_trace_sink_dma_configure_max_size(LTraceSinkDmaType *d, uint64_t s) { d->TR_SK_DMA_MAX_SIZE = s; }
static inline void l_trace_sink_dma_configure_mode(LTraceSinkDmaType *d, uint32_t m) { d->TR_SK_DMA_MODE = m; }
static inline uint64_t l_trace_sink_dma_count(LTraceSinkDmaType *d) { return d->TR_SK_DMA_COUNT; }
static inline uint32_t l_trace_sink_dma_src_stall(LTraceSinkDmaType *d) { return d->TR_SK_DMA_SRC_STALL; }

#endif
