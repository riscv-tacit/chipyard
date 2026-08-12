#include "tacit.h"
#include <stdio.h>
#include <riscv-pk/encoding.h>

// int get_hart_id() {
//   return read_csr("mhartid");
// }

int main(int argc, char **argv) {

  static volatile uint8_t *dma_buffer = (uint8_t *)0x7000000; // scratchpad start address
  int hart_id = 0;

  LTraceEncoderType *encoder = l_trace_encoder_get(hart_id);
  // l_trace_encoder_configure_branch_mode(encoder, BRANCH_MODE_PREDICT);
  l_trace_encoder_configure_branch_mode(encoder, BRANCH_MODE_TARGET);

  LTraceSinkDmaType *sink_dma = l_trace_sink_dma_get(hart_id);
  l_trace_sink_dma_configure_addr(sink_dma, (uint64_t)dma_buffer, 1);
  l_trace_encoder_configure_target(encoder, TARGET_DMA);

  l_trace_encoder_start(encoder);

  printf("Hello, magical world from %d\n", hart_id);

  l_trace_encoder_stop(encoder);

  l_trace_sink_dma_read(sink_dma, dma_buffer);
}

void l_trace_sink_dma_read(LTraceSinkDmaType *sink_dma, uint8_t *buffer) {
  l_trace_sink_dma_flush(sink_dma);
  uint64_t count = sink_dma->TR_SK_DMA_COUNT;
  printf("[l_trace_sink_dma_read] count: %lld\n", count);
  for (uint8_t i = 0; i < count; i++) {
    printf("%02x ", buffer[i]);
  }
  printf("\n");
}

void l_trace_sink_dma_flush(LTraceSinkDmaType *sink_dma) {
  sink_dma->TR_SK_DMA_FLUSH = 1;
  while (sink_dma->TR_SK_DMA_FLUSH_DONE == 0) {
    // printf("waiting for flush done\n");
  }
  // printf("[l_trace_sink_dma_read] flush done\n");
}