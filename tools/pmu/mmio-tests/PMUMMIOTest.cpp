#include "VPMUMMIOTestTop.h"
#include "verilated.h"
#include <array>
#include <cstdint>
#include <cstdlib>
#include <deque>
#include <iostream>

#define GET(b, name) ((b) == 0 ? dut.io32_##name : dut.io64_##name)
#define SET(b, name, value) do { if ((b) == 0) dut.io32_##name = (value); else dut.io64_##name = (value); } while (0)
static unsigned checks = 0;
static void expect(bool ok, const char* msg) {
  ++checks;
  if (!ok) { std::cerr << "FAIL: " << msg << std::endl; std::exit(1); }
}
struct Request {
  uint32_t offset;
  bool write;
  uint64_t data;
  unsigned mask, size, source = 0;
};
struct Bank {
  bool enabled = false, valid = false;
  uint64_t live[2] = {}, captured[2] = {};
  std::deque<Request> pending;
};
struct Sim {
  VPMUMMIOTestTop dut;
  std::array<Bank, 2> banks;
  bool accepted[2] = {}, responded[2] = {};
  uint64_t response[2] = {};
  unsigned cycles = 0, commands[2] = {};
  unsigned bytes(int b) { return b == 0 ? 4 : 8; }
  Sim() {
    dut.reset = 1;
    for (int b = 0; b < 2; ++b) {
      SET(b, increment, b ? UINT64_C(0x8000000000000001) : UINT64_C(0x100000003));
      SET(b, a_valid, 0); SET(b, d_ready, 1);
      SET(b, a_write, 0); SET(b, a_source, 0); SET(b, a_address, 0x40000000);
      SET(b, a_size, 2); SET(b, a_data, 0); SET(b, a_mask, 15);
    }
    tick(); tick(); tick(); dut.reset = 0; tick();
  }
  uint64_t readWord(int b, uint32_t offset) {
    auto& m = banks[b];
    const uint32_t word = offset & ~(bytes(b) - 1);
    if (word == 0) return m.enabled;
    if (word == 0x10) return unsigned(m.enabled) | (unsigned(m.valid) << 1);
    if (word == 0x18) return UINT64_C(0x20001) | (b ? (UINT64_C(64) << 32) : 0);
    if (word == 0x1c) return 64;
    if (word >= 0x100 && word < 0x110) {
      const auto value = m.captured[(word - 0x100) / 8];
      return b ? value : uint32_t(value >> ((word & 4) * 8));
    }
    return 0;
  }
  void tick() {
    dut.clock = 0; dut.eval();
    for (int b = 0; b < 2; ++b) {
      auto& m = banks[b];
      accepted[b] = GET(b, a_valid) && GET(b, a_ready);
      responded[b] = GET(b, d_valid) && GET(b, d_ready);
      if (dut.reset) { m = Bank{}; continue; }
      expect(bool(GET(b, enabled)) == m.enabled, "enable before edge");
      if (accepted[b]) m.pending.push_back({uint32_t(GET(b, a_address) - 0x40000000),
        bool(GET(b, a_write)), GET(b, a_data), unsigned(GET(b, a_mask)),
        unsigned(GET(b, a_size)), unsigned(GET(b, a_source))});
      bool clear = false, snapshot = false, next_enable = m.enabled;
      if (GET(b, d_valid)) {
        expect(!m.pending.empty(), "response without an accepted request");
        const auto req = m.pending.front();
        expect(GET(b, d_source) == req.source, "response source ordering");
        expect(!GET(b, d_denied) && !GET(b, d_corrupt), "unexpected TileLink error");
        if (!req.write) expect(GET(b, d_data) == readWord(b, req.offset), "MMIO read data while stalled or accepted");
        if (responded[b]) {
          response[b] = GET(b, d_data);
          const unsigned word = req.offset & ~(bytes(b) - 1);
          if (req.write && (req.mask & 1)) {
            if (word == 0) next_enable = req.data & 1;
            if (word == 8) {
              clear = req.data & 1; snapshot = req.data & 2;
              if (clear || snapshot) ++commands[b];
            }
          }
          m.pending.pop_front();
        }
      }
      expect(bool(GET(b, clear)) == clear, "clear must pulse exactly once on masked command completion");
      expect(bool(GET(b, snapshot)) == snapshot, "snapshot must pulse exactly once on masked command completion");
      const uint64_t next[2] = {m.live[0] + (m.enabled ? GET(b, increment) : 0),
                               m.live[1] + unsigned(m.enabled)};
      if (snapshot) { m.captured[0] = next[0]; m.captured[1] = next[1]; m.valid = true; }
      for (int i = 0; i < 2; ++i) m.live[i] = clear ? 0 : next[i];
      m.enabled = next_enable;
    }
    dut.clock = 1; dut.eval(); ++cycles;
    for (int b = 0; b < 2; ++b) {
      const auto& m = banks[b];
      expect(GET(b, live0) == m.live[0] && GET(b, live1) == m.live[1], "running counters / enable / clear ordering");
      expect(GET(b, captured0) == m.captured[0] && GET(b, captured1) == m.captured[1], "inclusive coherent snapshot and preservation across clear");
      expect(bool(GET(b, enabled)) == m.enabled && bool(GET(b, captured_valid)) == m.valid, "reset and command status");
    }
  }
  void drive(int b, const Request& r) {
    SET(b, a_valid, 1); SET(b, a_write, r.write); SET(b, a_source, r.source);
    SET(b, a_address, 0x40000000 + r.offset); SET(b, a_size, r.size);
    SET(b, a_data, r.data); SET(b, a_mask, r.mask);
  }
  uint64_t access(int b, Request r, unsigned stall = 0) {
    drive(b, r); SET(b, d_ready, 0);
    unsigned timeout = 0;
    do { tick(); expect(++timeout < 100, "request timeout"); } while (!accepted[b]);
    SET(b, a_valid, 0);
    for (unsigned i = 0; i < stall; ++i) tick();
    SET(b, d_ready, 1);
    do { tick(); expect(++timeout < 200, "response timeout"); } while (!responded[b]);
    return response[b];
  }
  void write32(int b, unsigned offset, uint32_t value, unsigned stall = 0) {
    unsigned lane = offset % bytes(b);
    access(b, {offset, true, uint64_t(value) << (8 * lane), 15u << lane, 2}, stall);
  }
  uint32_t read32(int b, unsigned offset, unsigned stall = 0) {
    unsigned lane = offset % bytes(b);
    return access(b, {offset, false, 0, 15u << lane, 2}, stall) >> (8 * lane);
  }
  void exercise(int b) {
    expect(read32(b, 0) == 0 && read32(b, 0x10) == 0, "reset-disabled / snapshot invalid");
    expect(read32(b, 0x18) == 0x20001 && read32(b, 0x1c) == 64, "version / count / width discovery");
    expect(read32(b, 0x100) == 0 && read32(b, 0x108) == 0, "snapshots zero before capture");
    access(b, {0, true, 1, bytes(b) == 8 ? 0xfeu : 0xeu, b ? 3u : 2u}, 3);
    expect(!banks[b].enabled, "masked-off enable write ignored");
    access(b, {8, true, 3, bytes(b) == 8 ? 0xfeu : 0xeu, b ? 3u : 2u}, 5);
    expect(!banks[b].valid, "masked-off command ignored");
    write32(b, 0, 1);
    for (int i = 0; i < 20; ++i) tick();
    write32(b, 8, 2, 9);
    const uint64_t snapshot = banks[b].captured[0];
    const uint32_t lo = read32(b, 0x100, 4);
    for (int i = 0; i < 17; ++i) tick();
    const uint32_t hi = read32(b, 0x104, 4);
    expect((uint64_t(hi) << 32 | lo) == snapshot, "split 64-bit reads remain coherent while accumulating");
    if (b) expect(access(b, {0x100, false, 0, 0xff, 3}, 4) == snapshot, "native 64-bit read");
    write32(b, 8, 3, 7);
    expect(banks[b].live[0] == 0 && banks[b].live[1] == 0 && banks[b].valid, "atomic snapshot-and-clear");
    const uint64_t preserved = banks[b].captured[0];
    write32(b, 8, 1);
    expect(banks[b].captured[0] == preserved && banks[b].valid, "clear preserves the last captured bank");
    write32(b, 0, 0);
    const uint64_t paused = banks[b].live[0];
    for (int i = 0; i < 10; ++i) tick();
    expect(banks[b].live[0] == paused, "disabled accumulation holds");
    write32(b, 8, 2);
    expect(banks[b].captured[0] == paused, "snapshot while disabled excludes increment");
    write32(b, 0x100, 0xffffffff); write32(b, 0x18, 0xffffffff);
    expect(banks[b].captured[0] == paused, "counter window is read-only");
    expect(read32(b, 0x18) == 0x20001, "INFO is read-only");
    write32(b, 8, 0xfffffffc); expect(read32(b, 8) == 0, "reserved command bits ignored and readback zero");
    expect(read32(b, 0x80) == 0, "unimplemented offsets read zero without aliasing");
    // Byte write to COMMAND's upper byte cannot assert either low-byte command.
    const unsigned lane = 9 % bytes(b);
    access(b, {9, true, uint64_t(3) << (8 * lane), 1u << lane, 0});
    expect(banks[b].captured[0] == paused, "upper-byte command write ignored");
    // Two sources: hold the second A request against a full register request queue.
    write32(b, 0, 1);
    unsigned before = commands[b];
    drive(b, {8, true, 2, 15, 2, 0}); SET(b, d_ready, 0);
    do { tick(); } while (!accepted[b]);
    drive(b, {8, true, 3, 15, 2, 1});
    for (int i = 0; i < 6; ++i) { tick(); expect(!accepted[b], "full queue backpressures A"); }
    expect(commands[b] == before, "D backpressure does not execute commands");
    SET(b, d_ready, 1);
    unsigned timeout = 0;
    do { tick(); expect(++timeout < 100, "backpressured A timeout"); } while (!accepted[b]);
    SET(b, a_valid, 0);
    while (!banks[b].pending.empty()) { tick(); expect(++timeout < 200, "queued D timeout"); }
    expect(commands[b] == before + 2, "held A and D requests each execute once");
    uint32_t random = 0x5567123 + b;
    for (unsigned i = 0; i < 128; ++i) {
      random ^= random << 13; random ^= random >> 17; random ^= random << 5;
      if (i % 5 == 0) write32(b, 0, random & 1, random % 4);
      write32(b, 8, random & 3, random % 7);
      read32(b, 0x100 + 4 * (random % 4), random % 5);
    }
  }
};

int main(int argc, char** argv) {
  Verilated::commandArgs(argc, argv);
  Sim sim;
  sim.exercise(0); sim.exercise(1);
  // A hardware reset invalidates snapshots and disables both independently controlled banks.
  sim.dut.reset = 1; sim.tick(); sim.dut.reset = 0; sim.tick();
  sim.dut.final();
  std::cout << "PASS: real 32/64-bit TileLink PMU MMIO, " << sim.cycles << " cycles, " << checks
    << " checks: masks, A/D backpressure, snapshot stability, atomic capture+clear, enable, reset, and 64-bit wrap" << std::endl;
}
