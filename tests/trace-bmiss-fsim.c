// TACIT-traced variant of bmiss-ubench for the combined tacit+tracedoctor
// FireSim config: the encoder streams to the RawByte sink (targetId 2 in
// TacitMediumBoomV3RawByteConfig) while the TraceDoctor oracle captures the
// same run. Used for the same-run TACIT vs oracle bbtrace diff.

#include "tacit.h"
#include <stdio.h>

#define TARGET_FSIM_RAWBYTE 0x2 /* WithTraceSinkRawByte(2) */

#define N (1 << 14)
#define ITERS 2000

static int a[N];

int main(void) {
  unsigned idx = 1;
  int acc = 0;

  for (unsigned i = 0; i < N; i++) {
    a[i] = (int)((i * 2654435761u) >> 13);
  }

  LTraceEncoderType *encoder = l_trace_encoder_get(0);
  l_trace_encoder_configure_branch_mode(encoder, BRANCH_MODE_TARGET);
  l_trace_encoder_configure_target(encoder, TARGET_FSIM_RAWBYTE);
  l_trace_encoder_start(encoder);

  for (unsigned iter = 0; iter < ITERS; iter++) {
    int x = a[idx];
    idx = (idx * 1103515245u + 12345u) & (N - 1);
    if (x & 1) {
      acc += x;
    } else {
      acc -= x >> 1;
    }
  }

  l_trace_encoder_stop(encoder);

  printf("trace-bmiss-fsim done: acc=%d\n", acc);
  return 0;
}
