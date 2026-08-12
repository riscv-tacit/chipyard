// See LICENSE for license details
// Ported from firesim/firesim#1501 (TraceDoctor, EECS-NTNU).
//
// The buffered, multithreaded worker engine. Simulator-independent: tokens
// enter through a caller-supplied pull function (bound to the DMA stream by
// the bridge driver, or to a synthetic generator in unit tests) and are
// dispatched to every registered worker.

#ifndef __TRACEDOCTOR_ENGINE_H_
#define __TRACEDOCTOR_ENGINE_H_

#include <atomic>
#include <condition_variable>
#include <functional>
#include <memory>
#include <queue>
#include <thread>
#include <vector>

#include "tracedoctor_worker.h"

// pull(dest, max_bytes, min_bytes) -> bytes actually delivered.
// min_bytes == 0 requests a non-blocking best-effort drain.
using tracedoctor_pull_fn =
    std::function<size_t(char *dest, size_t max_bytes, size_t min_bytes)>;

class tracedoctor_spinlock {
  std::atomic<bool> lock_ = {false};

public:
  void lock() {
    for (;;) {
      if (!lock_.exchange(true, std::memory_order_acquire)) {
        break;
      }
      while (lock_.load(std::memory_order_relaxed))
        ;
    }
  }

  void unlock() { lock_.store(false, std::memory_order_release); }

  bool try_lock() { return !lock_.exchange(true, std::memory_order_acquire); }
};

class tracedoctor_engine_t {
public:
  // workers:        consumers; every worker sees every buffer.
  // streamDepth:    tokens pulled per drain (i.e. the DMA queue depth).
  // bufferDepth:    number of recyclable host buffers.
  // bufferGrouping: drains accumulated per buffer before dispatch.
  // traceThreads:   -1 = one thread per worker (balanced); 0 = process
  //                 inline on the drain thread; 0 < n < #workers =
  //                 round-robin pool of n threads.
  tracedoctor_engine_t(struct traceInfo const &info,
                       std::vector<std::unique_ptr<tracedoctor_worker>> &&workers,
                       unsigned int streamDepth,
                       unsigned int bufferDepth,
                       unsigned int bufferGrouping,
                       int traceThreads);
  ~tracedoctor_engine_t();

  // Pull up to streamDepth tokens into the current buffer and dispatch full
  // buffers to the workers. With flush set, accept any number of pending
  // tokens and dispatch even a partially filled buffer. Returns true if any
  // tokens were received.
  bool drain(tracedoctor_pull_fn const &pull, bool flush);

  uint64_t total_tokens() const { return totalTokens; }
  size_t num_workers() const { return workers.size(); }
  int num_threads() const { return traceThreads; }
  unsigned int buffer_depth() const { return bufferDepth; }
  unsigned int buffer_grouping() const { return bufferGrouping; }

private:
  struct protectedWorker {
    tracedoctor_spinlock lock;
    std::unique_ptr<tracedoctor_worker> worker;
  };

  struct referencedBuffer {
    char *data = nullptr;
    unsigned int tokens = 0;
    // Set when the buffer content was handed to the workers; its tokens are
    // stale and must be discarded before the buffer is refilled. (The
    // original PR inferred this from the fill level, which double-dispatched
    // tokens after a flush of a partially filled buffer.)
    bool dispatched = false;
    std::atomic<unsigned int> refs = {0};
  };

  void balancedWork(unsigned int threadId);
  void work(unsigned int threadId);

  struct traceInfo info;

  std::vector<std::thread> workerThreads;
  std::vector<std::unique_ptr<struct protectedWorker>> workers;
  std::vector<std::unique_ptr<struct referencedBuffer>> buffers;
  std::vector<std::queue<struct referencedBuffer *>> workQueues;

  tracedoctor_spinlock workQueueLock;
  std::condition_variable_any workQueueCond;
  bool workQueuesMaybeEmpty = true;
  bool workerExit = false;

  unsigned int streamDepth;
  unsigned int bufferDepth;
  unsigned int bufferGrouping;
  int traceThreads;

  unsigned int bufferIndex = 0;
  unsigned int bufferTokenCapacity;
  unsigned int bufferTokenThreshold;
  uint64_t totalTokens = 0;
};

#endif // __TRACEDOCTOR_ENGINE_H_
