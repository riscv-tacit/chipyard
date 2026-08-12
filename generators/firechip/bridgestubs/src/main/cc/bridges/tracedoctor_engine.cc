// See LICENSE for license details
// Ported from firesim/firesim#1501 (TraceDoctor, EECS-NTNU).

#include "tracedoctor_engine.h"

#include <cstdio>
#include <cstdlib>
#include <stdexcept>

#include <unistd.h>

tracedoctor_engine_t::tracedoctor_engine_t(
    struct traceInfo const &info,
    std::vector<std::unique_ptr<tracedoctor_worker>> &&inWorkers,
    unsigned int streamDepth,
    unsigned int bufferDepth,
    unsigned int bufferGrouping,
    int traceThreads)
    : info(info), streamDepth(streamDepth), bufferDepth(bufferDepth),
      bufferGrouping(bufferGrouping), traceThreads(traceThreads) {

  for (auto &w : inWorkers) {
    auto worker = std::make_unique<struct protectedWorker>();
    worker->worker = std::move(w);
    workers.push_back(std::move(worker));
  }
  if (workers.empty()) {
    throw std::invalid_argument("tracedoctor engine requires >= 1 worker");
  }

  if (this->bufferGrouping == 0)
    this->bufferGrouping = 1;
  if (this->bufferDepth == 0)
    this->bufferDepth = 1;

  if (this->traceThreads < 0 ||
      (unsigned int)this->traceThreads >= workers.size()) {
    this->traceThreads = workers.size();
  } else if (this->traceThreads == 0) {
    // Inline processing cannot overlap buffers, one is enough.
    this->bufferDepth = 1;
  }

  // How many tokens fit into one buffer, and the fill level past which
  // another full drain might not fit anymore (dispatch point).
  bufferTokenCapacity = this->bufferGrouping * this->streamDepth;
  bufferTokenThreshold = bufferTokenCapacity - this->streamDepth;

  for (unsigned int i = 0; i < workers.size(); i++) {
    workQueues.push_back(std::queue<referencedBuffer *>());
  }

  long const pageSize = sysconf(_SC_PAGESIZE);
  size_t rawSize = (size_t)bufferTokenCapacity * info.tokenBytes;
  // aligned_alloc requires size to be a multiple of the alignment
  size_t const allocSize = ((rawSize + pageSize - 1) / pageSize) * pageSize;
  for (unsigned int i = 0; i < this->bufferDepth; i++) {
    auto buffer = std::make_unique<struct referencedBuffer>();
    buffer->data = (char *)aligned_alloc(pageSize, allocSize);
    if (!buffer->data) {
      throw std::runtime_error("tracedoctor engine could not allocate buffer");
    }
    buffers.push_back(std::move(buffer));
  }

  if (this->traceThreads > 0) {
    auto const &targetWorkFunc =
        ((unsigned int)this->traceThreads == workers.size())
            ? &tracedoctor_engine_t::balancedWork
            : &tracedoctor_engine_t::work;
    for (unsigned int i = 0; i < (unsigned int)this->traceThreads; i++) {
      workerThreads.emplace_back(std::thread(targetWorkFunc, this, i));
    }
  }
}

tracedoctor_engine_t::~tracedoctor_engine_t() {
  {
    std::unique_lock<tracedoctor_spinlock> queueLock(workQueueLock);
    workerExit = true;
  }
  workQueueCond.notify_all();
  // Worker threads finish processing any queued buffers before exiting.
  for (auto &t : workerThreads) {
    t.join();
  }

  // Destruct workers explicitly so their files flush/close before the
  // buffers they might still reference are freed.
  workers.clear();
  for (auto &b : buffers) {
    free(b->data);
  }
  buffers.clear();

  fprintf(stdout,
          "TraceDoctor@%u: traced_tokens(%lu), traced_bytes(%lu)\n",
          info.tracerId, totalTokens, totalTokens * (uint64_t)info.tokenBytes);
}

// One thread per worker: thread i exclusively serves worker i.
void tracedoctor_engine_t::balancedWork(unsigned int const threadId) {
  if (threadId >= workers.size())
    return;

  struct protectedWorker *myWorker = workers[threadId].get();
  std::queue<struct referencedBuffer *> &myWorkQueue = workQueues[threadId];

  while (true) {
    std::unique_lock<tracedoctor_spinlock> queueLock(workQueueLock);
    workQueueCond.wait(queueLock, [this, &myWorkQueue]() {
      return workerExit || !myWorkQueue.empty();
    });
    if (myWorkQueue.empty()) {
      return;
    }

    struct referencedBuffer *buffer = myWorkQueue.front();
    myWorkQueue.pop();
    queueLock.unlock();
    myWorker->worker->tick(buffer->data, buffer->tokens);
    buffer->refs--;
  }
}

// Fewer threads than workers: round-robin over the work queues.
void tracedoctor_engine_t::work(unsigned int const threadId) {
  struct referencedBuffer *buffer = nullptr;
  struct protectedWorker *worker = nullptr;
  unsigned int const numWorkQueues = workers.size();
  unsigned int robbingId = threadId % numWorkQueues;
  bool foundJob = false;

  while (true) {
    std::unique_lock<tracedoctor_spinlock> queueLock(workQueueLock);
    workQueueCond.wait(queueLock, [this, &foundJob]() {
      return workerExit || foundJob || !workQueuesMaybeEmpty;
    });
    if (workQueuesMaybeEmpty && !foundJob) {
      return;
    }

    foundJob = false;

    for (unsigned int i = 0; i < numWorkQueues; i++) {
      robbingId = (robbingId + 1) % numWorkQueues;
      if (!workQueues[robbingId].empty() &&
          workers[robbingId]->lock.try_lock()) {
        worker = workers[robbingId].get();
        buffer = workQueues[robbingId].front();
        workQueues[robbingId].pop();
        foundJob = true;
        break;
      }
    }

    // If no job was found the queues are either empty or other threads are
    // working on them; either way there is nothing for us until new work is
    // published.
    workQueuesMaybeEmpty = workQueuesMaybeEmpty || !foundJob;
    queueLock.unlock();

    if (foundJob) {
      worker->worker->tick(buffer->data, buffer->tokens);
      worker->lock.unlock();
      buffer->refs--;
    }
  }
}

bool tracedoctor_engine_t::drain(tracedoctor_pull_fn const &pull, bool flush) {
  struct referencedBuffer *buffer = buffers[bufferIndex].get();
  unsigned int tokensReceived = 0;

  // Wait until all workers released this buffer before reusing it.
  while (traceThreads > 0 && buffer->refs > 0) {
    std::this_thread::yield();
  }

  // The buffer's content was already handed to the workers; discard it
  // before refilling.
  if (buffer->dispatched) {
    buffer->tokens = 0;
    buffer->dispatched = false;
  }

  size_t const bytesReceived =
      pull(buffer->data + ((size_t)buffer->tokens * info.tokenBytes),
           (size_t)streamDepth * info.tokenBytes,
           flush ? 0 : ((size_t)streamDepth * info.tokenBytes));
  tokensReceived = bytesReceived / info.tokenBytes;

  buffer->tokens += tokensReceived;

  // Dispatch when another full drain might not fit anymore (normal case:
  // buffer is full), or on flush with any residue.
  if (buffer->tokens > bufferTokenThreshold || (buffer->tokens && flush)) {
    buffer->dispatched = true;
    if (traceThreads > 0) {
      buffer->refs = workers.size();

      workQueueLock.lock();
      for (std::queue<struct referencedBuffer *> &workQueue : workQueues) {
        workQueue.push(buffer);
      }
      workQueuesMaybeEmpty = false;
      workQueueLock.unlock();
      workQueueCond.notify_all();

      bufferIndex = (bufferIndex + 1) % buffers.size();
    } else {
      for (auto &worker : workers) {
        worker->worker->tick(buffer->data, buffer->tokens);
      }
    }
  }

  totalTokens += tokensReceived;

  return tokensReceived > 0;
}
