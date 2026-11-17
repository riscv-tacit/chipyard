// See LICENSE for license details.
#ifndef INTERVALTRACE_JSON_H
#define INTERVALTRACE_JSON_H
#include "intervaltrace_aggregate.h"
#include <iomanip>
#include <limits>
#include <ostream>
#include <stdexcept>

// Writes one interval in interval JSONL version `version` (2, 3 or 4). Blocks are
// referenced by their capture-local IDs, which the driver defines in the
// incrementally written catalog. Version 3 equals version 2, except that with a
// stall event in the catalog every cycle field excludes stall cycles (see
// intervaltrace_aggregator); version 2 cycle fields are elapsed (wall) cycles.
// Version 4 (+interval-trace-no-blocks) has the fields and cycle basis of version
// 3 without `blocks`, plus `user_instructions` and `user_cycles`, the sums over
// the interval's user-mode blocks; it needs no block catalog.
inline void intervaltrace_write_features(std::ostream &out, const intervaltrace_features &f,
    const std::map<intervaltrace_block_key, uint64_t> &block_ids, int version = 3) {
  if (version < 2 || version > 4) throw std::invalid_argument("interval JSONL version must be 2, 3 or 4");
  out << "{\"interval_format_version\":" << version << ',';
#define FIELD(name) out << "\"" #name "\":" << f.name << ','
  FIELD(epoch); FIELD(snapshot_id); FIELD(nominal_interval);
  FIELD(start_retired); FIELD(end_retired); FIELD(start_cycle); FIELD(end_cycle);
  FIELD(start_physical_retired); FIELD(end_physical_retired);
  FIELD(instructions); FIELD(cycles); FIELD(attributed_cycles); FIELD(unattributed_cycles);
  FIELD(physical_retirements); FIELD(partial_blocks); FIELD(flags); FIELD(bb_flags);
  if (version == 4) { FIELD(user_instructions); FIELD(user_cycles); }
#undef FIELD
  out << "\"ipc\":";
  if (f.cycles)
    out << std::setprecision(std::numeric_limits<double>::max_digits10)
              << static_cast<double>(f.instructions) / static_cast<double>(f.cycles);
  else
    out << "null";
  out << ',';
  out << "\"final\":" << (f.final ? "true" : "false") << ",\"counter_deltas\":[";
  bool comma = false;
  for (auto value : f.counter_deltas) {
    if (comma) out << ',';
    comma = true;
    out << value;
  }
  if (version == 4) {
    out << "]}\n";
    return;
  }
  out << "],\"blocks\":[";
  comma = false;
  for (const auto &entry : f.blocks) {
    if (comma) out << ',';
    comma = true;
    const auto &k = entry.first;
    const auto &v = entry.second;
    out << '[' << block_ids.at(k) << ',' << v.instructions << ',' << v.cycles << ',' << v.executions << ']';
  }
  out << "]}\n";
}
#endif
