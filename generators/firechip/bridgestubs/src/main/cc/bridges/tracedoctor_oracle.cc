// See LICENSE for license details

#include "tracedoctor_oracle.h"

#include <algorithm>
#include <cinttypes>
#include <vector>

// ---------------------------------------------------------------- insttrace

tracedoctor_insttrace::tracedoctor_insttrace(
    std::vector<std::string> const &args, struct traceInfo const &info)
    : tracedoctor_worker("InstTrace", args, info, 1) {
  for (auto &a : args) {
    if (a == "csv")
      csv = true;
  }
  if (csv) {
    fprintf(std::get<freg_descriptor>(fileRegister[0]), "tsc,pc,flags\n");
  }
}

tracedoctor_insttrace::~tracedoctor_insttrace() {
  fprintf(stdout, "%s: records(%" PRIu64 ")\n", tracerName.c_str(), records);
}

void tracedoctor_insttrace::tick(char const *data, unsigned int tokens) {
  auto *toks = reinterpret_cast<oracleToken const *>(data);
  FILE *f = std::get<freg_descriptor>(fileRegister[0]);
  for (unsigned i = 0; i < tokens; i++) {
    oracleToken const &t = toks[i];
    if (!t.committing())
      continue;
    for (unsigned s = 0; s < 4; s++) {
      if (!t.archValid(s))
        continue;
      records++;
      if (csv) {
        fprintf(f, "%" PRIu64 ",0x%" PRIx64 ",0x%x\n", t.tsc(), t.pc[s],
                t.slotFlags(s));
      } else {
        uint64_t rec[3] = {t.tsc(), t.pc[s], t.slotFlags(s)};
        fwrite(rec, sizeof(uint64_t), 3, f);
      }
    }
  }
}

// ----------------------------------------------------------------- bboracle

tracedoctor_bboracle::tracedoctor_bboracle(std::vector<std::string> const &args,
                                           struct traceInfo const &info)
    : tracedoctor_worker("BBOracle", args, info, 1) {
  for (auto &a : args) {
    if (a == "boundary")
      boundaryWhole = true;
  }
  fprintf(std::get<freg_descriptor>(fileRegister[0]),
          "entry_tsc,bb_pc,retired,computing_x12,stalled_x12,flushed_x12,"
          "drained_x12\n");
}

void tracedoctor_bboracle::emitInstance(bbInstance const &inst) {
  if (!inst.valid)
    return;
  fprintf(std::get<freg_descriptor>(fileRegister[0]),
          "%" PRIu64 ",0x%" PRIx64 ",%" PRIu64 ",%" PRIu64 ",%" PRIu64
          ",%" PRIu64 ",%" PRIu64 "\n",
          inst.entryTsc, inst.pc, inst.retired, inst.computingX12,
          inst.stalledX12, inst.flushedX12, inst.drainedX12);
  instances++;
}

void tracedoctor_bboracle::closeInstance(uint64_t entryTsc, uint64_t pc) {
  emitInstance(open);
  open = bbInstance{true, entryTsc, pc, 0, 0, 0, 0, 0};
}

void tracedoctor_bboracle::chargeFlushed(uint64_t cyclesX12) {
  open.flushedX12 += cyclesX12;
  totalX12 += cyclesX12;
}

// Quiet/non-commit cycles, attributed per the state after `prev`.
void tracedoctor_bboracle::gapCycles(uint64_t cyclesX12) {
  if (cyclesX12 == 0)
    return;
  if (prev.robEmpty() && drainActive) { // Flushed
    chargeFlushed(cyclesX12);
  } else if (prev.robEmpty()) { // Drained: forward to the refill instance
    pendingDrainedX12 += cyclesX12;
  } else { // Stalled: forward to the next commit's instance
    pendingStalledX12 += cyclesX12;
  }
}

void tracedoctor_bboracle::processToken(oracleToken const &t) {
  constexpr unsigned F_CFI = 0x02 | 0x04 | 0x08; // br | jal | jalr
  if (havePrev) {
    gapCycles(12 * (t.tsc() - prev.tsc() - 1));
  } else {
    firstTsc = t.tsc();
  }
  lastTsc = t.tsc();

  if (t.robEmpty() && !inDrain) {
    inDrain = true;
    drainActive = lastCommitFlushes;
    lastCommitFlushes = false; // consumed: a further empty period is Drained
  } else if (!t.robEmpty()) {
    inDrain = false;
    drainActive = false;
  }

  if (t.committing()) {
    unsigned n = 0;
    for (unsigned s = 0; s < 4; s++)
      n += t.archValid(s);
    bool first = true;
    for (unsigned s = 0; s < 4; s++) {
      if (!t.archValid(s))
        continue;
      if (bbBoundary) { // this commit opens a new instance
        closeInstance(t.tsc(), t.pc[s]);
        bbBoundary = false;
      }
      if (first) { // forward-pending resolves to the newest open instance
        open.stalledX12 += pendingStalledX12;
        open.drainedX12 += pendingDrainedX12;
        totalX12 += pendingStalledX12 + pendingDrainedX12;
        pendingStalledX12 = pendingDrainedX12 = 0;
        if (boundaryWhole) { // whole group cycle to the oldest slot's instance
          open.computingX12 += 12;
          totalX12 += 12;
        }
        first = false;
      }
      open.retired++;
      if (!boundaryWhole) {
        open.computingX12 += 12 / n;
        totalX12 += 12 / n;
      }
      totalCommits++;
      unsigned const f = t.slotFlags(s);
      if (f & (0x20 /*bmiss*/ | 0x10 /*flush_on_commit*/)) {
        lastCommitFlushes = true;
      } else if (f & F_CFI) {
        lastCommitFlushes = false;
      }
      if (f & F_CFI) // CFI terminates the instance
        bbBoundary = true;
    }
  } else if (t.robEmpty() && drainActive) {
    chargeFlushed(12);
  } else if (t.robEmpty()) {
    pendingDrainedX12 += 12;
  } else {
    pendingStalledX12 += 12;
  }

  prev = t;
  havePrev = true;
}

void tracedoctor_bboracle::tick(char const *data, unsigned int tokens) {
  auto *toks = reinterpret_cast<oracleToken const *>(data);
  for (unsigned i = 0; i < tokens; i++)
    processToken(toks[i]);
}

tracedoctor_bboracle::~tracedoctor_bboracle() {
  emitInstance(open);
  fprintf(stdout,
          "%s: instances(%" PRIu64 "), commits(%" PRIu64
          "), attributed_x12(%" PRIu64 ") span_x12(%" PRIu64
          ") unresolved_fwd(%" PRIu64 ")\n",
          tracerName.c_str(), instances, totalCommits, totalX12,
          12 * (lastTsc - firstTsc + 1), pendingStalledX12 + pendingDrainedX12);
}

// ------------------------------------------------------------------ bbtrace

tracedoctor_bbtrace::tracedoctor_bbtrace(std::vector<std::string> const &args,
                                         struct traceInfo const &info)
    : tracedoctor_worker("BBTrace", args, info, 1) {}

tracedoctor_bbtrace::~tracedoctor_bbtrace() {
  fprintf(stdout, "%s: events(%" PRIu64 ")\n", tracerName.c_str(), events);
}

void tracedoctor_bbtrace::nextCommit(uint64_t tsc, uint64_t pc,
                                     unsigned flags) {
  constexpr unsigned F_BR = 0x02, F_JAL = 0x04, F_JALR = 0x08;
  if (havePending) {
    FILE *f = std::get<freg_descriptor>(fileRegister[0]);
    char const *kind;
    if (pendingFlags & F_JALR) {
      kind = "UninferableJump";
    } else if (pendingFlags & F_JAL) {
      kind = "InferrableJump";
    } else if (pc == pendingPc + 4 || pc == pendingPc + 2) {
      kind = "NonTakenBranch";
    } else {
      kind = "TakenBranch";
    }
    fprintf(f, "[timestamp: %" PRIu64 "] %s: 0x%" PRIx64 " -> 0x%" PRIx64 "\n",
            pendingTsc, kind, pendingPc, pc);
    events++;
    havePending = false;
  }
  if (flags & (F_BR | F_JAL | F_JALR)) {
    havePending = true;
    pendingPc = pc;
    pendingTsc = tsc;
    pendingFlags = flags;
  }
}

void tracedoctor_bbtrace::tick(char const *data, unsigned int tokens) {
  auto *toks = reinterpret_cast<oracleToken const *>(data);
  for (unsigned i = 0; i < tokens; i++) {
    oracleToken const &t = toks[i];
    if (!t.committing())
      continue;
    for (unsigned s = 0; s < 4; s++) {
      if (t.archValid(s))
        nextCommit(t.tsc(), t.pc[s], t.slotFlags(s));
    }
  }
}

// ------------------------------------------------------------------- oracle

tracedoctor_oracle::tracedoctor_oracle(std::vector<std::string> const &args,
                                       struct traceInfo const &info)
    : tracedoctor_worker("Oracle", args, info, 1) {}

void tracedoctor_oracle::creditForward(uint64_t pc) {
  if (pendingStalledX12) {
    stats[pc].stalledX12 += pendingStalledX12;
    pendingStalledX12 = 0;
  }
  if (pendingDrainedX12) {
    stats[pc].drainedX12 += pendingDrainedX12;
    pendingDrainedX12 = 0;
  }
}

// Attribute the quiet cycles after `prev` according to prev's steady state.
void tracedoctor_oracle::accountGap(uint64_t cyclesX12) {
  if (cyclesX12 == 0)
    return;
  if (prev.robEmpty()) {
    if (drainTargetPc) { // Flushed: backward to the flush causer
      stats[drainTargetPc].flushedX12 += cyclesX12;
      cyclesFlushedX12 += cyclesX12;
    } else { // Drained: forward to the refill instruction
      pendingDrainedX12 += cyclesX12;
      cyclesDrainedX12 += cyclesX12;
    }
  } else { // Stalled: forward to the blocking ROB head (next commit)
    pendingStalledX12 += cyclesX12;
    cyclesStalledX12 += cyclesX12;
  }
}

void tracedoctor_oracle::processToken(oracleToken const &t) {
  if (havePrev) {
    if (t.tsc() <= prev.tsc())
      tscViolations++;
    // quiet cycles strictly between prev and t
    accountGap(12 * (t.tsc() - prev.tsc() - 1));
  } else {
    firstTsc = t.tsc();
  }
  lastTsc = t.tsc();
  totalTokens++;

  if (t.mispredictEv())
    mispredictEvents++;

  // Drain lifecycle: classify at onset using the youngest previous commit.
  if (t.robEmpty() && !inDrain) {
    inDrain = true;
    drainTargetPc = lastCommitFlushes ? lastCommitPc : 0;
  } else if (!t.robEmpty()) {
    inDrain = false;
    drainTargetPc = 0;
  }

  // The token's own cycle.
  if (t.committing()) {
    unsigned n = 0;
    for (unsigned s = 0; s < 4; s++)
      n += t.archValid(s);
    uint64_t firstPc = 0;   // oldest commit this cycle (slot 0 is oldest)
    uint64_t flusherPc = 0; // youngest committing bmiss/flush_on_commit slot
    for (unsigned s = 0; s < 4; s++) {
      if (!t.archValid(s))
        continue;
      auto &st = stats[t.pc[s]];
      st.retired++;
      st.computingX12 += 12 / n; // Computing: 1/n each (n <= 4 divides 12)
      totalCommits++;
      if (t.bmiss(s)) {
        bmissCommits++;
        if (!t.isBr(s) && !t.isJalr(s))
          badBmissType++;
      }
      if (!firstPc)
        firstPc = t.pc[s];
      if (t.bmiss(s) || t.flushOnCommit(s))
        flusherPc = t.pc[s];
    }
    cyclesComputingX12 += 12;
    // Stalled/Drained cycles pending forward resolve to the oldest commit.
    creditForward(firstPc);
    lastCommitFlushes = flusherPc != 0;
    lastCommitPc = flusherPc;
  } else if (t.robEmpty()) {
    if (drainTargetPc) {
      stats[drainTargetPc].flushedX12 += 12;
      cyclesFlushedX12 += 12;
    } else {
      pendingDrainedX12 += 12;
      cyclesDrainedX12 += 12;
    }
  } else {
    pendingStalledX12 += 12;
    cyclesStalledX12 += 12;
  }

  prev = t;
  havePrev = true;
}

void tracedoctor_oracle::tick(char const *data, unsigned int tokens) {
  auto *toks = reinterpret_cast<oracleToken const *>(data);
  for (unsigned i = 0; i < tokens; i++)
    processToken(toks[i]);
}

tracedoctor_oracle::~tracedoctor_oracle() {
  FILE *f = std::get<freg_descriptor>(fileRegister[0]);
  fprintf(f, "pc,retired,computing_x12,stalled_x12,flushed_x12,drained_x12\n");
  auto total = [](pcStats const &s) {
    return s.computingX12 + s.stalledX12 + s.flushedX12 + s.drainedX12;
  };
  std::vector<std::pair<uint64_t, pcStats>> sorted(stats.begin(), stats.end());
  std::sort(sorted.begin(), sorted.end(), [&](auto &a, auto &b) {
    return total(a.second) > total(b.second);
  });
  for (auto &[pc, st] : sorted) {
    fprintf(f,
            "0x%" PRIx64 ",%" PRIu64 ",%" PRIu64 ",%" PRIu64 ",%" PRIu64
            ",%" PRIu64 "\n",
            pc, st.retired, st.computingX12, st.stalledX12, st.flushedX12,
            st.drainedX12);
  }

  uint64_t attributedX12 = cyclesComputingX12 + cyclesStalledX12 +
                           cyclesFlushedX12 + cyclesDrainedX12;
  fprintf(stdout,
          "%s: tokens(%" PRIu64 "), commits(%" PRIu64 "), pcs(%zu), "
          "tsc[%" PRIu64 "..%" PRIu64 "]\n",
          tracerName.c_str(), totalTokens, totalCommits, stats.size(),
          firstTsc, lastTsc);
  fprintf(stdout,
          "%s: cycles_x12 computing(%" PRIu64 ") stalled(%" PRIu64
          ") flushed(%" PRIu64 ") drained(%" PRIu64 ") attributed(%" PRIu64
          ") span_x12(%" PRIu64 ") unresolved_fwd(%" PRIu64 ")\n",
          tracerName.c_str(), cyclesComputingX12, cyclesStalledX12,
          cyclesFlushedX12, cyclesDrainedX12, attributedX12,
          12 * (lastTsc - firstTsc + 1), pendingStalledX12 + pendingDrainedX12);
  fprintf(stdout,
          "%s: CHECK mispredict_events(%" PRIu64 ") bmiss_commits(%" PRIu64
          ") bad_bmiss_type(%" PRIu64 ") tsc_violations(%" PRIu64 ")\n",
          tracerName.c_str(), mispredictEvents, bmissCommits, badBmissType,
          tscViolations);
}
