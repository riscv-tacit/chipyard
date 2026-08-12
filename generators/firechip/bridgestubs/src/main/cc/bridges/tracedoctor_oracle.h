// See LICENSE for license details
// Instruction-level oracle workers for the BOOM TraceDoctor token stream.
// Token layout contract: see BOOM v3 core.scala TraceDoctor packing block.

#ifndef __TRACEDOCTOR_ORACLE_H_
#define __TRACEDOCTOR_ORACLE_H_

#include <cstdint>
#include <unordered_map>

#include "tracedoctor_worker.h"

// 512b / 64B token, little-endian 64b words.
struct oracleToken {
  uint64_t word0;    // tsc[47:0] | state[55:48]
  uint64_t flags;    // 16b per commit slot
  uint64_t pc[4];    // sign-extended commit PCs
  uint64_t resv[2];

  uint64_t tsc() const { return word0 & ((1ULL << 48) - 1); }
  unsigned state() const { return (word0 >> 48) & 0xff; }
  bool committing() const { return state() & 0x01; }
  bool dispatching() const { return state() & 0x02; }
  bool robEmpty() const { return state() & 0x04; }
  bool robPopulated() const { return state() & 0x08; }
  bool exception() const { return state() & 0x10; }
  bool mispredictEv() const { return state() & 0x20; }

  unsigned slotFlags(unsigned s) const { return (flags >> (16 * s)) & 0xffff; }
  bool archValid(unsigned s) const { return slotFlags(s) & 0x01; }
  bool isBr(unsigned s) const { return slotFlags(s) & 0x02; }
  bool isJal(unsigned s) const { return slotFlags(s) & 0x04; }
  bool isJalr(unsigned s) const { return slotFlags(s) & 0x08; }
  bool flushOnCommit(unsigned s) const { return slotFlags(s) & 0x10; }
  bool bmiss(unsigned s) const { return slotFlags(s) & 0x20; }
};
static_assert(sizeof(oracleToken) == 64, "oracle token must be one beat");

// Per-committed-instruction records: '(tsc, pc, flags)'; binary 24B records
// by default, 'csv' for text. TracerV-equivalent view for cross-validation.
class tracedoctor_insttrace : public tracedoctor_worker {
private:
  bool csv = false;
  uint64_t records = 0;

public:
  tracedoctor_insttrace(std::vector<std::string> const &args,
                        struct traceInfo const &info);
  ~tracedoctor_insttrace() override;
  void tick(char const *data, unsigned int tokens) override;
};

// TIP 4-state oracle: attributes every cycle to an instruction address.
// Computing: 1/n to each committing instruction (exact, x12 fixed point).
// Stalled:   forward to the next committed instruction (ROB head).
// Flushed:   backward to the flush-causing commit (bmiss / flush_on_commit).
// Drained:   forward to the first instruction committed after refill.
class tracedoctor_oracle : public tracedoctor_worker {
private:
  struct pcStats {
    uint64_t retired = 0;
    uint64_t cyclesX12 = 0; // cycles * 12, exact for 1/n splits, n <= 4
  };
  std::unordered_map<uint64_t, pcStats> stats;

  bool havePrev = false;
  oracleToken prev = {};

  // cycles awaiting forward attribution (Stalled/Drained), x12
  uint64_t pendingForwardX12 = 0;
  // backward-attribution target for the current drain (0 = none -> Drained)
  uint64_t drainTargetPc = 0;
  bool inDrain = false;
  // youngest commit of the last committing token (drain classification)
  uint64_t lastCommitPc = 0;
  bool lastCommitFlushes = false;

  // consistency counters
  uint64_t mispredictEvents = 0, bmissCommits = 0, badBmissType = 0;
  uint64_t tscViolations = 0, totalCommits = 0, totalTokens = 0;
  uint64_t cyclesComputingX12 = 0, cyclesStalledX12 = 0;
  uint64_t cyclesFlushedX12 = 0, cyclesDrainedX12 = 0;
  uint64_t firstTsc = 0, lastTsc = 0;

  void creditForward(uint64_t pc);
  void accountGap(uint64_t cyclesX12);
  void processToken(oracleToken const &t);

public:
  tracedoctor_oracle(std::vector<std::string> const &args,
                     struct traceInfo const &info);
  ~tracedoctor_oracle() override;
  void tick(char const *data, unsigned int tokens) override;
};

#endif // __TRACEDOCTOR_ORACLE_H_
