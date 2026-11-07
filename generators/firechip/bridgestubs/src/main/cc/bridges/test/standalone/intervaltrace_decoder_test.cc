#include "intervaltrace_decoder.h"
#include <cassert>
#include <fstream>
#include <functional>
#include <iostream>
#include <iterator>
#include <limits>

static std::vector<uint8_t> packet(unsigned kind, unsigned flags, std::vector<uint64_t> payload) {
  std::vector<uint64_t> words{uint64_t(kind) | (1ull << 8) | (uint64_t(flags) << 16) | (uint64_t(payload.size()) << 32)};
  words.insert(words.end(), payload.begin(), payload.end());
  words.resize(((words.size() + 7) / 8) * 8);
  std::vector<uint8_t> bytes;
  for (auto word : words)
    for (unsigned i = 0; i < 8; ++i)
      bytes.push_back((word >> (i * 8)) & 255);
  return bytes;
}
static void append(std::vector<uint8_t> &out, const std::vector<uint8_t> &in) { out.insert(out.end(), in.begin(), in.end()); }
static void rejects(const std::function<void()> &fn) {
  bool threw = false;
  try { fn(); } catch (const std::runtime_error &) { threw = true; }
  assert(threw);
}
int main(int argc, char **argv) {
  // Arguments: stream, counter count, catalog ID and, optionally, the stall event ID.
  if (argc == 4 || argc == 5) {
    std::ifstream file(argv[1], std::ios::binary);
    assert(file.good());
    const std::vector<uint8_t> bytes((std::istreambuf_iterator<char>(file)), {});
    intervaltrace_decoder decoder(std::stoul(argv[2]), std::stoull(argv[3]), argc == 5 ? std::stoll(argv[4]) : -1);
    for (size_t i = 0; i < bytes.size(); i += 13)
      decoder.push(bytes.data() + i, std::min<size_t>(13, bytes.size() - i));
    decoder.finish();
    std::cout << "PASS: decoded RTL stream, " << decoder.completed_intervals() << " intervals\n";
    return 0;
  }
  for (unsigned count : {1u, 18u, 19u, 20u}) {
    const auto snapshot = [count](uint64_t id, uint64_t end, uint64_t cycle, uint64_t physical, unsigned flags) {
      std::vector<uint64_t> p{id, end, cycle, physical};
      for (unsigned i = 0; i < count; ++i) p.push_back(std::numeric_limits<uint64_t>::max() - i);
      return packet(2, flags, p);
    };
    std::vector<uint8_t> stream;
    append(stream, packet(3, 0, {1, 2, count, 99}));
    append(stream, snapshot(0, 0, 0, 0, 8));
    // Two PMU snapshots precede both BBs and share the physical endpoint.
    append(stream, snapshot(1, 2, 5, 4, 0));
    append(stream, snapshot(2, 4, 5, 4, 0));
    append(stream, packet(1, 1, {0x1000, 0x1004, 2, 5}));
    append(stream, packet(1, 1, {0x1008, 0x100c, 2, 0}));
    append(stream, snapshot(3, 4, 5, 4, 16));
    append(stream, packet(4, 0, {1, 4, 5, 3}));
    for (unsigned chunk : {1u, 13u, 64u, 71u, 4096u}) {
      intervaltrace_decoder decoder(count, 99);
      for (size_t pos = 0; pos < stream.size(); pos += chunk)
        decoder.push(stream.data() + pos, std::min<size_t>(chunk, stream.size() - pos));
      decoder.finish();
      assert(decoder.completed_intervals() == 2);
      const auto &stats = decoder.stats();
      assert(stats.epochs == 1 && stats.basic_blocks == 2 && stats.partial_basic_blocks == 0);
      assert(stats.resets == 0 && stats.instructions == 4 && stats.cycles == 5);
    }
    auto bad = stream;
    bad[0] = 255;
    rejects([&] { intervaltrace_decoder d(count, 99); d.push(bad.data(), bad.size()); });
    rejects([&] { intervaltrace_decoder d(count, 99); d.push(stream.data(), stream.size() - 1); d.finish(); });
    rejects([&] { intervaltrace_decoder d(count, 98); d.push(stream.data(), stream.size()); });
    bad = stream;
    bad[63] = 1;
    rejects([&] { intervaltrace_decoder d(count, 99); d.push(bad.data(), bad.size()); });
    bad = stream;
    const size_t pmu_bytes = ((5 + count + 7) / 8) * 64;
    bad[64 + 3 * pmu_bytes + 2] = 0; // missing terminate_interval on first BB
    rejects([&] { intervaltrace_decoder d(count, 99); d.push(bad.data(), bad.size()); });
    bad = stream;
    // A smaller BB duration still fits under the final epoch total, but does
    // not agree with the PMU timestamp for the very same interval boundary.
    bad[64 + 3 * pmu_bytes + 32] = 4;
    rejects([&] { intervaltrace_decoder d(count, 99); d.push(bad.data(), bad.size()); });
  }
  // Partial-only captures and reset closures are valid even without intervals.
  std::vector<uint8_t> short_capture;
  append(short_capture, packet(3, 0, {1, 1000000, 1, 99}));
  append(short_capture, packet(2, 8, {0, 0, 0, 0, 0}));
  append(short_capture, packet(2, 16 | 32, {1, 3, 10, 3, 3}));
  append(short_capture, packet(1, 2 | 4 | 32, {0x1000, 0x1008, 3, 10}));
  append(short_capture, packet(4, 32, {1, 3, 10, 1}));
  append(short_capture, packet(3, 0, {2, 1000000, 1, 99}));
  append(short_capture, packet(2, 8, {0, 0, 0, 0, 0}));
  append(short_capture, packet(2, 16, {1, 2, 7, 2, 2}));
  append(short_capture, packet(1, 2 | 4, {0x2000, 0x2004, 2, 7}));
  append(short_capture, packet(4, 0, {2, 2, 7, 1}));
  intervaltrace_decoder short_decoder(1, 99);
  short_decoder.push(short_capture.data(), short_capture.size());
  short_decoder.finish();
  const auto &stats = short_decoder.stats();
  assert(stats.epochs == 2 && stats.intervals == 0 && stats.basic_blocks == 2);
  assert(stats.partial_basic_blocks == 2 && stats.resets == 1);
  assert(stats.instructions == 5 && stats.cycles == 17);
  intervaltrace_decoder empty(1, 99);
  empty.finish();
  assert(empty.stats().epochs == 0 && empty.stats().basic_blocks == 0);
  std::cout << "PASS: framing, fragmented pulls, padding, endpoints and malformed streams\n";
}
