#include "VPMUPrototypeTop.h"
#include "verilated.h"
#include <cstdint>
#include <iostream>

int main(int argc, char** argv) {
  Verilated::commandArgs(argc, argv);
  VPMUPrototypeTop dut;
  unsigned a0 = 0, a1 = 0, b0 = 0, b1 = 0;
  unsigned captured_a0 = 0, captured_a1 = 0, captured_b0 = 0, captured_b1 = 0;
  bool valid_a = false, valid_b = false;
  uint64_t wide = 0;
  uint32_t random = 0x713fab;
  for (unsigned cycle = 0; cycle < 4096; ++cycle) {
    random ^= random << 13; random ^= random >> 17; random ^= random << 5;
    dut.bits_a = random & 15; dut.bits_b = (random >> 4) & 15;
    dut.enable_a = (cycle % 7 != 0); dut.enable_b = (cycle % 5 != 0);
    dut.snapshot_a = (cycle % 3 == 0); dut.snapshot_b = (cycle % 4 == 0);
    dut.clear_a = (cycle % 31 == 7); dut.clear_b = (cycle % 23 == 3);
    dut.reset_a = (cycle == 0 || cycle % 109 == 0);
    dut.reset_b = (cycle == 0 || cycle % 131 == 0);
    dut.reset = (cycle == 0 || cycle % 257 == 0);
    dut.clock = 0; dut.eval();
    // Capture inclusive totals first, then clear the independent running banks.
    unsigned next_a0 = (a0 + (dut.enable_a ? __builtin_popcount(dut.bits_a) : 0)) % 16;
    unsigned next_a1 = (a1 + (dut.enable_a ? 1 : 0)) % 16;
    unsigned next_b0 = (b0 + (dut.enable_b ? __builtin_popcount(dut.bits_b) : 0)) % 16;
    unsigned next_b1 = (b1 + (dut.enable_b ? 1 : 0)) % 16;
    if (dut.reset_a) { captured_a0 = captured_a1 = 0; valid_a = false; }
    else if (dut.snapshot_a) { captured_a0 = next_a0; captured_a1 = next_a1; valid_a = true; }
    if (dut.reset_b) { captured_b0 = captured_b1 = 0; valid_b = false; }
    else if (dut.snapshot_b) { captured_b0 = next_b0; captured_b1 = next_b1; valid_b = true; }
    a0 = (dut.reset_a || dut.clear_a) ? 0 : next_a0;
    a1 = (dut.reset_a || dut.clear_a) ? 0 : next_a1;
    b0 = (dut.reset_b || dut.clear_b) ? 0 : next_b0;
    b1 = (dut.reset_b || dut.clear_b) ? 0 : next_b1;
    wide = dut.reset ? 0 : wide + 1;
    dut.clock = 1; dut.eval();
    if (dut.a0 != a0 || dut.a1 != a1 || dut.b0 != b0 || dut.b1 != b1 || dut.wide != wide) {
      std::cerr << "Native PMU mismatch at cycle " << cycle << std::endl;
      return 1;
    }
    if (dut.captured_a0 != captured_a0 || dut.captured_a1 != captured_a1 ||
        dut.captured_b0 != captured_b0 || dut.captured_b1 != captured_b1 ||
        bool(dut.valid_a) != valid_a || bool(dut.valid_b) != valid_b) {
      std::cerr << "Snapshot mismatch at cycle " << cycle << std::endl;
      return 1;
    }
    // An extra evaluation without a target clock edge must not add again.
    dut.eval();
    if (dut.a0 != a0 || dut.b0 != b0 || dut.wide != wide) return 1;
  }
  dut.final();
  std::cout << "PASS: 4096 RTL cycles, nested routing, independent enable/clear/reset, inclusive snapshots, atomic capture+clear, wrapping, and 64-bit default" << std::endl;
}
