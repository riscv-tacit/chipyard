#include "VPMUEventLogicTop.h"
#include "verilated.h"
#include <cstdint>
#include <cstdlib>
#include <iostream>

static unsigned checks = 0;
static void expect(bool condition, const char* name) {
  ++checks;
  if (!condition) {
    std::cerr << "FAIL: " << name << std::endl;
    std::exit(1);
  }
}

int main(int argc, char** argv) {
  Verilated::commandArgs(argc, argv);
  VPMUEventLogicTop d;
  auto clear = [&]() {
    d.valids = d.fires = 0;
    d.rob_blocked = d.rename_blocked = d.issue_queue_blocked = 0;
    d.ldq_blocked = d.stq_blocked = d.serialization_blocked = d.other_backend_blocked = 0;
    d.redirect = d.control_stall = 0; d.empty_capacity = 1;
    d.head_valids = d.head_busy = d.head_loads = d.head_exceptions = 0;
    d.commit_enabled = 1;
    d.accepted = d.existing_mshr = d.tag_match = 0; d.cacheable = 1;
  };
  clear();
  // Invalid lanes may retain arbitrary stale resource hazards.
  d.rob_blocked = d.rename_blocked = d.ldq_blocked = 15;
  d.eval(); expect(d.event_mask == 1, "empty dispatch ignores stale per-uop hazards");
  d.empty_capacity = 0; d.eval(); expect(d.event_mask == 0, "empty with no proven capacity is not frontend starvation");
  clear(); d.valids = 15; d.fires = 1; d.ldq_blocked = 2; d.issue_queue_blocked = 8;
  d.eval(); expect(d.dispatched == 1 && d.event_mask == ((1 << 1) | (1 << 6)),
    "partial dispatch attributes the first waiting lane, not a younger queue hazard");
  d.rename_blocked = 2; d.eval();
  expect(d.event_mask == ((1 << 1) | (1 << 4) | (1 << 6)), "simultaneous causes on the same lane are retained");
  d.redirect = 1; d.eval(); expect(d.event_mask == (1 << 2), "redirect suppresses resource attribution");
  d.control_stall = 1; d.eval(); expect(d.event_mask == 0, "CSR/trace stalls excluded from dispatch classification");
  clear(); d.valids = 10; d.fires = 2; d.serialization_blocked = 8;
  d.eval(); expect(d.event_mask == (1 << 8), "serialization after a dispatched lane is separate from backend capacity");
  clear(); d.valids = d.fires = 15; d.rob_blocked = 15;
  d.eval(); expect(d.dispatched == 4 && d.event_mask == 0, "full-width dispatch counts every accepted uop");
  clear(); d.valids = 8; d.other_backend_blocked = 8;
  d.eval(); expect(d.event_mask == (1 << 1), "RoCC allocation contributes to broad backend blocking");

  // Head holes, ready older instructions and exceptional/rollback states.
  clear(); d.head_valids = 12; d.head_loads = d.head_busy = 8;
  d.eval(); expect(!d.head_load_wait, "younger busy load is not the oldest instruction");
  d.head_valids = 8; d.eval(); expect(d.head_load_wait, "head selection skips invalid banks");
  d.head_exceptions = 8; d.eval(); expect(!d.head_load_wait, "exceptional load is excluded");
  d.head_exceptions = 0; d.commit_enabled = 0;
  d.eval(); expect(!d.head_load_wait, "rollback or external commit blocking is excluded");
  d.commit_enabled = 1; d.head_valids = 0;
  d.eval(); expect(!d.head_load_wait, "empty head row ignores stale load and busy bits");

  // A held request, a primary allocation, then secondary merges, an upgrade and MMIO.
  unsigned misses = 0;
  clear();
  for (unsigned cycle = 0; cycle < 12; ++cycle) {
    d.accepted = cycle >= 3;
    d.existing_mshr = cycle >= 4 && cycle < 8;
    d.tag_match = cycle >= 8 && cycle < 10;
    d.cacheable = cycle < 10;
    d.eval(); misses += d.line_miss;
  }
  expect(misses == 1, "backpressure, merged requests, upgrades and MMIO count only one line miss");

  // Independent iterative oracle for arbitrary partial groups and overlapping causes.
  uint32_t state = 0x518dad;
  auto random = [&]() { state ^= state << 13; state ^= state >> 17; state ^= state << 5; return state; };
  for (unsigned trial = 0; trial < 8192; ++trial) {
    clear();
    d.valids = random() & 15;
    unsigned causes[7];
    for (auto& c : causes) c = random() & 15;
    d.rob_blocked = causes[0]; d.rename_blocked = causes[1]; d.issue_queue_blocked = causes[2];
    d.ldq_blocked = causes[3]; d.stq_blocked = causes[4];
    d.serialization_blocked = causes[5]; d.other_backend_blocked = causes[6];
    d.redirect = (random() % 11 == 0); d.control_stall = (random() % 17 == 0);
    d.empty_capacity = random() & 1;
    unsigned expected_count = 0, first_waiting = 4;
    bool stopped = d.redirect || d.control_stall;
    for (unsigned lane = 0; lane < 4; ++lane) {
      if (!(d.valids & (1 << lane))) continue;
      for (auto c : causes) if (c & (1 << lane)) stopped = true;
      if (!stopped) { d.fires |= 1 << lane; ++expected_count; }
      else if (first_waiting == 4) first_waiting = lane;
    }
    unsigned expected_mask = 0;
    if (!d.control_stall) {
      if (d.redirect) expected_mask = 1 << 2;
      else if (!d.valids) expected_mask = d.empty_capacity ? 1 : 0;
      else if (first_waiting < 4) {
        for (unsigned c = 0; c < 7; ++c) {
          if (!(causes[c] & (1 << first_waiting))) continue;
          if (c < 6) expected_mask |= 1 << (3 + c);
          if (c != 5) expected_mask |= 1 << 1;
        }
      }
    }
    d.head_valids = random() & 15; d.head_busy = random() & 15;
    d.head_loads = random() & 15; d.head_exceptions = random() & 15;
    d.commit_enabled = random() & 1;
    bool expected_head = false;
    for (unsigned lane = 0; lane < 4; ++lane) {
      if (!(d.head_valids & (1 << lane))) continue;
      expected_head = d.commit_enabled && (d.head_busy & (1 << lane)) &&
        (d.head_loads & (1 << lane)) && !(d.head_exceptions & (1 << lane));
      break;
    }
    d.eval();
    expect(d.event_mask == expected_mask && d.dispatched == expected_count, "random dispatch oracle");
    expect(bool(d.head_load_wait) == expected_head, "random ROB head oracle");
  }
  d.final();
  std::cout << "PASS: BOOM event predicates: " << checks
    << " RTL checks (directed cases and 8192 randomized dispatch/ROB observations)" << std::endl;
}
