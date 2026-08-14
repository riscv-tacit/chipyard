#ifndef __L_TRACE_ENCODER_H
#define __L_TRACE_ENCODER_H

#include <stdio.h>

#include "metal.h"
// #include "rocketcore.h"

typedef struct {
  __IO uint32_t TR_TE_CTRL; //0x00
  __I uint32_t TR_TE_INFO; //0x04
  __IO uint32_t TR_TE_BUBBLE[6]; //0x08-0x1C
  __IO uint32_t TR_TE_TARGET; //0x20
  __IO uint32_t TR_TE_BRANCH_MODE; //0x24
} LTraceEncoderType;

typedef struct {
  __IO uint32_t TR_SK_DMA_FLUSH;
  __I uint32_t TR_SK_DMA_FLUSH_DONE;
  __IO uint64_t TR_SK_DMA_ADDR;
  __I uint64_t TR_SK_DMA_COUNT;
} LTraceSinkDmaType;

#define ROCKET
// #define BOOM

// Trace Sink Targets
#if defined(ROCKET)
#define TARGET_PRINT 0x0
#define TARGET_DMA 0x1
#define TARGET_FSIM 0x2
#elif defined(BOOM)
#define TARGET_PRINT 0x0
#define TARGET_FSIM 0x1
#endif

#define L_TRACE_ENCODER_BASE_ADDRESS 0x3000000

// Trace Branch Mode
#define BRANCH_MODE_TARGET    0x0
#define BRANCH_MODE_RESERVED0 0x1
#define BRANCH_MODE_PREDICT   0x2
#define BRANCH_MODE_RESERVED1 0x3

// SBUS Bypass 
#define SBUS_BYPASS_ADDRESS 0x1000000000

#define L_TRACE_ENCODER0 ((LTraceEncoderType *)(L_TRACE_ENCODER_BASE_ADDRESS + 0x0000))
#define L_TRACE_ENCODER1 ((LTraceEncoderType *)(L_TRACE_ENCODER_BASE_ADDRESS + 0x1000))
#define L_TRACE_ENCODER2 ((LTraceEncoderType *)(L_TRACE_ENCODER_BASE_ADDRESS + 0x2000))
#define L_TRACE_ENCODER3 ((LTraceEncoderType *)(L_TRACE_ENCODER_BASE_ADDRESS + 0x3000))

#define L_TRACE_SINK_DMA_BASE_ADDRESS 0x3010000
#define L_TRACE_SINK_DMA0 ((LTraceSinkDmaType *)(L_TRACE_SINK_DMA_BASE_ADDRESS + 0x0000))
#define L_TRACE_SINK_DMA1 ((LTraceSinkDmaType *)(L_TRACE_SINK_DMA_BASE_ADDRESS + 0x1000))
#define L_TRACE_SINK_DMA2 ((LTraceSinkDmaType *)(L_TRACE_SINK_DMA_BASE_ADDRESS + 0x2000))
#define L_TRACE_SINK_DMA3 ((LTraceSinkDmaType *)(L_TRACE_SINK_DMA_BASE_ADDRESS + 0x3000))

#define SBUS_BYPASS_ADDRESS 0x1000000000

static inline LTraceEncoderType *l_trace_encoder_get(uint32_t hart_id) {
  return (LTraceEncoderType *)(L_TRACE_ENCODER_BASE_ADDRESS + hart_id * 0x1000);
}

static inline LTraceSinkDmaType *l_trace_sink_dma_get(uint32_t hart_id) {
  return (LTraceSinkDmaType *)(L_TRACE_SINK_DMA_BASE_ADDRESS + hart_id * 0x1000);
}


/* Oracle window markers: architecturally-unique NOPs (slti x0, x0, imm)
 * committed at trace start/stop. The TracerV bridge's instruction-match
 * trigger (+trace-select=3) arms on START and disarms on STOP, gating the
 * TraceDoctor oracle stream (+tracedoctor-trigger=tracerv) to exactly the
 * TACIT window -- architecturally pinned, no post-hoc alignment.
 * START: slti x0, x0, 0x5A5 = 0x5A502013 -> +trace-start=ffffffff5a502013
 * STOP:  slti x0, x0, 0x5AD = 0x5AD02013 -> +trace-end=ffffffff5ad02013 */
#define L_TRACE_ORACLE_MARKER_START() asm volatile ("slti x0, x0, 0x5A5")
/* Stop marker is emitted 4x: TracerV's insn-trigger tracks arm state per
 * commit slot (upstream RTL bug on superscalar commit: the end-match only
 * clears the slot it commits in). Four consecutive markers sweep all
 * commit slots on <=4-wide cores, guaranteeing the armed slot is cleared. */
#define L_TRACE_ORACLE_MARKER_STOP()  asm volatile (\
  "slti x0, x0, 0x5AD\n\tslti x0, x0, 0x5AD\n\t"\
  "slti x0, x0, 0x5AD\n\tslti x0, x0, 0x5AD")

static inline void l_trace_encoder_start(LTraceEncoderType *encoder) {
  SET_BITS(encoder->TR_TE_CTRL, 0x1 << 1);
  L_TRACE_ORACLE_MARKER_START(); /* after start: oracle window inside tacit window */
}

static inline void l_trace_encoder_stop(LTraceEncoderType *encoder) {
  L_TRACE_ORACLE_MARKER_STOP(); /* before stop: oracle window inside tacit window */
  CLEAR_BITS(encoder->TR_TE_CTRL, 0x1 << 1);
}

static inline void l_trace_encoder_configure_target(LTraceEncoderType *encoder, uint64_t target) {
  encoder->TR_TE_TARGET = target;
}

static inline void l_trace_encoder_configure_branch_mode(LTraceEncoderType *encoder, uint64_t branch_mode) {
  encoder->TR_TE_BRANCH_MODE = branch_mode;
}

static inline void l_trace_sink_dma_configure_addr(LTraceSinkDmaType *sink_dma, uint64_t dma_addr, int bypass) {
  sink_dma->TR_SK_DMA_ADDR = bypass ? (SBUS_BYPASS_ADDRESS|dma_addr) : dma_addr;
}

void l_trace_sink_dma_read(LTraceSinkDmaType *sink_dma, uint8_t *buffer);
#endif /* __L_TRACE_ENCODER_H */
