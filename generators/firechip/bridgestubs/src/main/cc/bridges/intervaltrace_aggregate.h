// See LICENSE for license details.
#ifndef INTERVALTRACE_AGGREGATE_H
#define INTERVALTRACE_AGGREGATE_H

#include <cstdint>
#include <deque>
#include <functional>
#include <map>
#include <stdexcept>
#include <tuple>
#include <utility>
#include <vector>

struct intervaltrace_block_key {
  uint64_t start_pc, end_pc;
  unsigned privilege, partial_flags;
  bool operator<(const intervaltrace_block_key &other) const {
    return std::tie(start_pc, end_pc, privilege, partial_flags) <
           std::tie(other.start_pc, other.end_pc, other.privilege, other.partial_flags);
  }
};
struct intervaltrace_block_features {
  uint64_t instructions = 0, cycles = 0, executions = 0;
};
struct intervaltrace_features {
  uint64_t epoch = 0, snapshot_id = 0, nominal_interval = 0;
  uint64_t start_retired = 0, end_retired = 0, start_cycle = 0, end_cycle = 0;
  uint64_t start_physical_retired = 0, end_physical_retired = 0;
  uint64_t instructions = 0, cycles = 0, attributed_cycles = 0, unattributed_cycles = 0;
  uint64_t physical_retirements = 0, partial_blocks = 0;
  unsigned flags = 0, bb_flags = 0;
  bool final = false;
  std::map<intervaltrace_block_key, intervaltrace_block_features> blocks;
  std::vector<uint64_t> counter_deltas;
};

/** Receives validated intervaltrace_decoder callbacks; no CSV, DMA or simulator
 * dependency. The callback is synchronous: copy the result if retaining it.
 * Pending PMUs are bounded by the producer's retirement batch, and BB storage
 * by the distinct block identities in the current interval.
 *
 * With a stall event (stall_counter >= 0), every cycle quantity excludes stall
 * cycles: a block's cycles are its elapsed cycles minus its stall cycles, and
 * cycle endpoints are snapshot cycles minus the cumulative stall counter. Wall
 * time remains available per interval in counter_deltas.
 */
class intervaltrace_aggregator {
public:
  using callback_type = std::function<void(const intervaltrace_features &)>;
  explicit intervaltrace_aggregator(callback_type callback, int64_t stall_counter = -1)
      : callback(std::move(callback)), stall_counter(stall_counter) {}

  void consume(unsigned kind, unsigned flags, uint64_t epoch, const std::vector<uint64_t> &w) {
    if (kind == 3) {
      current = {};
      current.epoch = epoch;
      current.nominal_interval = w[1];
      pending.clear();
      previous.clear();
    } else if (kind == 2) {
      if (flags & 8) previous = w;
      else pending.push_back({flags, w});
    } else if (kind == 1) {
      auto &block = current.blocks[{w[0], w[1], flags >> 13, flags & 6u}];
      const uint64_t cycles = stall_counter >= 0 ? w[3] - w[4] : w[3]; // decoder checked w[4] <= w[3]
      block.instructions += w[2];
      block.cycles += cycles;
      ++block.executions;
      current.instructions += w[2];
      current.attributed_cycles += cycles;
      current.partial_blocks += bool(flags & 6);
      current.bb_flags |= flags;
      if (flags & 1) emit(false);
    } else if (kind == 4) {
      // Wait for epoch end: the final PMU precedes any final partial BB.
      emit(true);
      if (!pending.empty()) throw std::runtime_error("unused aggregate snapshots");
    }
  }

private:
  struct snapshot { unsigned flags; std::vector<uint64_t> words; };
  // Snapshot cycle position excluding stall cycles since the epoch baseline.
  uint64_t busy_cycle(const std::vector<uint64_t> &w) const {
    return stall_counter >= 0 ? w[2] - w[4 + stall_counter] : w[2];
  }
  void emit(bool final) {
    if (previous.empty() || pending.empty()) throw std::runtime_error("missing aggregate snapshot");
    const auto &next = pending.front();
    const auto &w = next.words;
    if (bool(next.flags & 16) != final || w[1] - previous[1] != current.instructions)
      throw std::runtime_error("aggregate retirement boundary mismatch");
    current.snapshot_id = w[0];
    current.final = final;
    current.flags = next.flags;
    current.start_retired = previous[1]; current.end_retired = w[1];
    current.start_cycle = busy_cycle(previous); current.end_cycle = busy_cycle(w);
    current.start_physical_retired = previous[3]; current.end_physical_retired = w[3];
    current.cycles = current.end_cycle - current.start_cycle;
    current.physical_retirements = w[3] - previous[3];
    if (current.attributed_cycles > current.cycles || (!final && current.attributed_cycles != current.cycles))
      throw std::runtime_error("aggregate cycle boundary mismatch");
    current.unattributed_cycles = current.cycles - current.attributed_cycles;
    for (size_t i = 4; i < w.size(); ++i)
      current.counter_deltas.push_back(w[i] - previous[i]); // modulo 2^64
    callback(current);
    previous = w;
    pending.pop_front();
    const auto epoch = current.epoch, nominal = current.nominal_interval;
    current = {};
    current.epoch = epoch;
    current.nominal_interval = nominal;
  }
  callback_type callback;
  const int64_t stall_counter;
  intervaltrace_features current;
  std::vector<uint64_t> previous;
  std::deque<snapshot> pending;
};
#endif
