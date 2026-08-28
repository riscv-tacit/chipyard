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
  uint64_t ctx;      // asid[15:0] | prv[17:16] | format tag[63:56]
  uint64_t resv;

  // Bumped whenever the token layout changes; workers hard-reject captures
  // from bitstreams emitting anything else.
  static constexpr unsigned FORMAT_TAG = 0xD1;

  uint64_t tsc() const { return word0 & ((1ULL << 48) - 1); }
  unsigned state() const { return (word0 >> 48) & 0xff; }
  bool committing() const { return state() & 0x01; }
  bool dispatching() const { return state() & 0x02; }
  bool robEmpty() const { return state() & 0x04; }
  bool robPopulated() const { return state() & 0x08; }
  bool exception() const { return state() & 0x10; }
  bool mispredictEv() const { return state() & 0x20; }

  unsigned fmt() const { return (ctx >> 56) & 0xff; }
  unsigned prv() const { return (ctx >> 16) & 0x3; }
  unsigned asid() const { return ctx & 0xffff; }

  unsigned slotFlags(unsigned s) const { return (flags >> (16 * s)) & 0xffff; }
  bool archValid(unsigned s) const { return slotFlags(s) & 0x01; }
  bool isBr(unsigned s) const { return slotFlags(s) & 0x02; }
  bool isJal(unsigned s) const { return slotFlags(s) & 0x04; }
  bool isJalr(unsigned s) const { return slotFlags(s) & 0x08; }
  bool flushOnCommit(unsigned s) const { return slotFlags(s) & 0x10; }
  bool bmiss(unsigned s) const { return slotFlags(s) & 0x20; }
};
static_assert(sizeof(oracleToken) == 64, "oracle token must be one beat");

// Reject old-format captures loudly rather than mis-decoding them.
void requireOracleFormat(oracleToken const &t, char const *who);

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
    // per-state cycles * 12 (exact for 1/n splits, n <= 4)
    uint64_t computingX12 = 0;
    uint64_t stalledX12 = 0;
    uint64_t flushedX12 = 0;
    uint64_t drainedX12 = 0;
  };
  // keyed by (pc, asid): user VAs collide across address spaces
  using pcKey = std::pair<uint64_t, unsigned>;
  struct pcKeyHash {
    size_t operator()(pcKey const &k) const {
      return std::hash<uint64_t>()(k.first ^ ((uint64_t)k.second << 48));
    }
  };
  std::unordered_map<pcKey, pcStats, pcKeyHash> stats;

  bool havePrev = false;
  oracleToken prev = {};

  // cycles awaiting forward attribution, split by state at accrual time
  uint64_t pendingStalledX12 = 0;
  uint64_t pendingDrainedX12 = 0;
  // backward-attribution target for the current drain (pc 0 = none -> Drained)
  pcKey drainTarget = {0, 0};
  bool inDrain = false;
  // youngest commit of the last committing token (drain classification)
  pcKey lastCommit = {0, 0};
  bool lastCommitFlushes = false;

  // consistency counters
  uint64_t mispredictEvents = 0, bmissCommits = 0, badBmissType = 0;
  uint64_t tscViolations = 0, totalCommits = 0, totalTokens = 0;
  uint64_t cyclesComputingX12 = 0, cyclesStalledX12 = 0;
  uint64_t cyclesFlushedX12 = 0, cyclesDrainedX12 = 0;
  uint64_t firstTsc = 0, lastTsc = 0;

  void creditForward(pcKey const &k);
  void accountGap(uint64_t cyclesX12);
  void processToken(oracleToken const &t);

public:
  tracedoctor_oracle(std::vector<std::string> const &args,
                     struct traceInfo const &info);
  ~tracedoctor_oracle() override;
  void tick(char const *data, unsigned int tokens) override;
};

// BB-level control-flow trace in tacit_decoder txt-receiver event format:
//   [timestamp: N] TakenBranch: 0x<from> -> 0x<to>
// One line per committed CFI, arc = (cfi pc, next committed pc). Kind:
// is_jal -> InferrableJump, is_jalr -> UninferableJump, is_br -> Taken/
// NonTakenBranch by fallthrough test (next == pc+2 or pc+4; a taken branch
// targeting its own fallthrough is indistinguishable and reads non-taken).
// Trap arcs: a flush_on_commit commit (sret/ecall) pends an exact Trap arc;
// a non-committing exception pends one with from = last committed pc (the
// faulting pc never commits, so it is not in the token stream). An
// exception while a CFI arc is pending emits that CFI arc toward the
// handler entry instead (its true target was squashed).
class tracedoctor_bbtrace : public tracedoctor_worker {
private:
  bool havePending = false;
  bool pendingTrap = false;
  uint64_t pendingPc = 0;
  uint64_t pendingTsc = 0;
  unsigned pendingFlags = 0;
  bool haveLastCommit = false;
  uint64_t lastCommitPc = 0;
  uint64_t events = 0;

  void nextCommit(uint64_t tsc, uint64_t pc, unsigned flags);

public:
  tracedoctor_bbtrace(std::vector<std::string> const &args,
                      struct traceInfo const &info);
  ~tracedoctor_bbtrace() override;
  void tick(char const *data, unsigned int tokens) override;
};

// Per-BB-instance oracle attribution: runs the same TIP 4-state DFA but
// accounts into dynamic basic-block instances (opened by the first commit
// after a CFI, closed by the next CFI). Computing/Stalled cycles land on
// the executing instance, Drained cycles on the refilling instance, and
// Flushed cycles on the instance containing the mispredicted/flushing
// commit — the "oracle sense", vs TACIT's delta-to-successor accounting.
// Emits one record per instance: entry_tsc,bb_pc,retired,cycles_x12.
// Instances close lazily (at the next commit), so all Flushed charges
// target the still-open flusher instance; lastCommitFlushes is consumed
// at drain onset so a subsequent refill-then-empty reads Drained.
class tracedoctor_bboracle : public tracedoctor_worker {
private:
  // 'boundary' arg: charge a commit group's whole cycle to the instance of
  // its oldest slot instead of splitting 1/n across a BB boundary. Matches
  // TACIT's integer-delta convention (the boundary cycle belongs to the
  // closing BB), removing the fractional-attribution floor from epsilon;
  // sub-BB placement is sanctioned by TIP's granularity rule. Default off
  // (TIP-faithful 1/n).
  bool boundaryWhole = false;
  struct bbInstance {
    bool valid = false;
    uint64_t entryTsc = 0;
    uint64_t exitTsc = 0;  // tsc of the instance's last commit: TACIT stamps
                           // events at CFI retirement, so exit-to-exit deltas
                           // are its estimate -- needed to emulate it from
                           // the oracle stream alone (entry deltas are a
                           // different slicing convention)
    uint64_t pc = 0;
    unsigned prv = 0;  // sampled at the opening commit's token
    unsigned asid = 0;
    uint64_t retired = 0;
    uint64_t computingX12 = 0;
    uint64_t stalledX12 = 0;
    uint64_t flushedX12 = 0;
    uint64_t drainedX12 = 0;
  };
  bbInstance open; // currently executing instance (closes lazily at the
                   // next commit, so a terminating CFI's flush cycles land
                   // here during the drain)

  bool havePrev = false;
  oracleToken prev = {};

  bool bbBoundary = true; // next commit opens a new instance
  // flush_on_commit covers both redirecting traps (sret/ecall) and
  // non-redirecting flushes (fence.i, CSR writes) which resume at pc+4;
  // only a discontinuous resume is a BB boundary, decided at next commit
  bool flushPending = false;
  uint64_t flushPc = 0;
  uint64_t pendingStalledX12 = 0;
  uint64_t pendingDrainedX12 = 0;
  bool drainActive = false; // drain classified Flushed
  bool lastCommitFlushes = false;
  bool inDrain = false;

  uint64_t totalX12 = 0, firstTsc = 0, lastTsc = 0, totalCommits = 0;
  uint64_t instances = 0;

  void emitInstance(bbInstance const &inst);
  void closeInstance(uint64_t entryTsc, uint64_t pc, unsigned prv,
                     unsigned asid);
  void chargeFlushed(uint64_t cyclesX12);
  void gapCycles(uint64_t cyclesX12);
  void processToken(oracleToken const &t);

public:
  tracedoctor_bboracle(std::vector<std::string> const &args,
                       struct traceInfo const &info);
  ~tracedoctor_bboracle() override;
  void tick(char const *data, unsigned int tokens) override;
};

#endif // __TRACEDOCTOR_ORACLE_H_
