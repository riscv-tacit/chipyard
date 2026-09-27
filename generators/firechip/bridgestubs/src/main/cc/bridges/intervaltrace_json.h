// See LICENSE for license details.
#ifndef INTERVALTRACE_JSON_H
#define INTERVALTRACE_JSON_H
#include "intervaltrace_aggregate.h"
#include <iomanip>
#include <limits>
#include <ostream>

// The legacy writer remains available to the existing offline CSV processor.
// Compact host captures use capture-local IDs from the incrementally written catalog.
inline void intervaltrace_write_features(std::ostream &out, const intervaltrace_features &f,
    const std::map<intervaltrace_block_key, uint64_t> *block_ids = nullptr) {
  out << '{';
  if (block_ids) out << "\"interval_format_version\":2,";
#define FIELD(name) out << "\"" #name "\":" << f.name << ','
  FIELD(epoch); FIELD(snapshot_id); FIELD(nominal_interval);
  FIELD(start_retired); FIELD(end_retired); FIELD(start_cycle); FIELD(end_cycle);
  FIELD(start_physical_retired); FIELD(end_physical_retired);
  FIELD(instructions); FIELD(cycles); FIELD(attributed_cycles); FIELD(unattributed_cycles);
  FIELD(physical_retirements); FIELD(partial_blocks); FIELD(flags); FIELD(bb_flags);
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
  out << "],\"blocks\":[";
  comma = false;
  for (const auto &entry : f.blocks) {
    if (comma) out << ',';
    comma = true;
    const auto &k = entry.first;
    const auto &v = entry.second;
    out << '[';
    if (block_ids) out << block_ids->at(k);
    else out << k.start_pc << ',' << k.end_pc << ',' << k.privilege << ',' << k.partial_flags;
    out << ',' << v.instructions << ',' << v.cycles << ',' << v.executions << ']';
  }
  out << "]}\n";
}
#endif
