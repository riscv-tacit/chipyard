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

// ------------------------------------------------------------------- oracle

tracedoctor_oracle::tracedoctor_oracle(std::vector<std::string> const &args,
                                       struct traceInfo const &info)
    : tracedoctor_worker("Oracle", args, info, 1) {}

void tracedoctor_oracle::creditForward(uint64_t pc) {
  if (pendingForwardX12) {
    stats[pc].cyclesX12 += pendingForwardX12;
    pendingForwardX12 = 0;
  }
}

// Attribute the quiet cycles after `prev` according to prev's steady state.
void tracedoctor_oracle::accountGap(uint64_t cyclesX12) {
  if (cyclesX12 == 0)
    return;
  if (prev.robEmpty()) {
    if (drainTargetPc) { // Flushed: backward to the flush causer
      stats[drainTargetPc].cyclesX12 += cyclesX12;
      cyclesFlushedX12 += cyclesX12;
    } else { // Drained: forward to the refill instruction
      pendingForwardX12 += cyclesX12;
      cyclesDrainedX12 += cyclesX12;
    }
  } else { // Stalled: forward to the blocking ROB head (next commit)
    pendingForwardX12 += cyclesX12;
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
      st.cyclesX12 += 12 / n; // Computing: 1/n each (n <= 4 divides 12)
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
      stats[drainTargetPc].cyclesX12 += 12;
      cyclesFlushedX12 += 12;
    } else {
      pendingForwardX12 += 12;
      cyclesDrainedX12 += 12;
    }
  } else {
    pendingForwardX12 += 12;
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
  fprintf(f, "pc,retired,cycles_x12\n");
  std::vector<std::pair<uint64_t, pcStats>> sorted(stats.begin(), stats.end());
  std::sort(sorted.begin(), sorted.end(), [](auto &a, auto &b) {
    return a.second.cyclesX12 > b.second.cyclesX12;
  });
  for (auto &[pc, st] : sorted) {
    fprintf(f, "0x%" PRIx64 ",%" PRIu64 ",%" PRIu64 "\n", pc, st.retired,
            st.cyclesX12);
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
          12 * (lastTsc - firstTsc + 1), pendingForwardX12);
  fprintf(stdout,
          "%s: CHECK mispredict_events(%" PRIu64 ") bmiss_commits(%" PRIu64
          ") bad_bmiss_type(%" PRIu64 ") tsc_violations(%" PRIu64 ")\n",
          tracerName.c_str(), mispredictEvents, bmissCommits, badBmissType,
          tscViolations);
}
