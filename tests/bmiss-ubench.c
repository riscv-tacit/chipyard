// Directed microbenchmark: force branches to resolve at the ROB head so a
// mispredicted branch commits in the same cycle b2 announces it (exercises
// the debug_bmiss commit bypass in BOOM's ROB, used by the TraceDoctor
// oracle). Recipe: a branch data-dependent on a cache-missing load with
// pseudo-random outcome — while the load misses, older work drains, so
// resolution and commit collapse together, and ~half the branches mispredict.

#include <stdio.h>

#define N (1 << 14) /* 16K ints = 64KB, beyond L1D */
#define ITERS 8000

static int a[N];

int main(void) {
  unsigned idx = 1;
  int acc = 0;

  for (unsigned i = 0; i < N; i++) {
    a[i] = (int)((i * 2654435761u) >> 13);
  }

  for (unsigned iter = 0; iter < ITERS; iter++) {
    int x = a[idx];
    idx = (idx * 1103515245u + 12345u) & (N - 1);
    if (x & 1) { /* unpredictable, depends on the missing load */
      acc += x;
    } else {
      acc -= x >> 1;
    }
  }

  printf("bmiss-ubench done: acc=%d\n", acc);
  return 0;
}
