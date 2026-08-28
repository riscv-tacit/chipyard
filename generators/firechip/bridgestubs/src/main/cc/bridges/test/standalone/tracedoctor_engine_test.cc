// See LICENSE for license details
// Standalone unit test for the TraceDoctor engine + worker framework.
// No simulator involved: a fake pull function feeds synthetic token streams.
//
// Build & run: make -C this directory (see Makefile).

#include <cassert>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <map>
#include <random>
#include <sstream>
#include <string>
#include <vector>

#include <unistd.h>

#include "tracedoctor_engine.h"
#include "tracedoctor_register.h"

namespace {

int g_failures = 0;

#define CHECK(cond, msg)                                                       \
  do {                                                                         \
    if (!(cond)) {                                                             \
      fprintf(stderr, "FAIL: %s (%s:%d): %s\n", __func__, __FILE__, __LINE__, \
              msg);                                                            \
      g_failures++;                                                            \
      return;                                                                  \
    }                                                                          \
  } while (0)

std::string tmpfile_name(const std::string &tag) {
  static int counter = 0;
  std::ostringstream ss;
  const char *dir = getenv("TMPDIR");
  ss << (dir ? dir : "/tmp") << "/td_test_" << getpid() << "_" << counter++
     << "_" << tag;
  return ss.str();
}

std::vector<char> read_file(const std::string &path) {
  std::ifstream f(path, std::ios::binary);
  return std::vector<char>((std::istreambuf_iterator<char>(f)),
                           std::istreambuf_iterator<char>());
}

// Deterministic pseudo-random token stream.
std::vector<char> make_source(size_t bytes, uint64_t seed) {
  std::mt19937_64 gen(seed);
  std::vector<char> src(bytes);
  for (size_t i = 0; i + 8 <= bytes; i += 8) {
    uint64_t v = gen();
    memcpy(&src[i], &v, 8);
  }
  return src;
}

// A pull function serving from `src`, honoring pull semantics: deliver up to
// max_bytes; if fewer than min_bytes are available, deliver nothing.
tracedoctor_pull_fn make_pull(const std::vector<char> &src, size_t &cursor) {
  return [&src, &cursor](char *dest, size_t max_bytes, size_t min_bytes) {
    size_t const available = src.size() - cursor;
    if (available < min_bytes || available == 0) {
      return (size_t)0;
    }
    size_t const n = std::min(available, max_bytes);
    memcpy(dest, &src[cursor], n);
    cursor += n;
    return n;
  };
}

std::vector<std::unique_ptr<tracedoctor_worker>>
make_filer(const traceInfo &info, const std::string &path, bool raw = true) {
  std::vector<std::string> args = {"file:" + path};
  if (raw)
    args.push_back("raw");
  std::vector<std::unique_ptr<tracedoctor_worker>> ws;
  ws.push_back(std::make_unique<tracedoctor_filer>(args, info));
  return ws;
}

constexpr unsigned kTokenBits = 512;
constexpr unsigned kTokenBytes = kTokenBits / 8;

traceInfo make_info(unsigned traceBits = kTokenBits) {
  traceInfo info = {};
  info.tracerId = 0;
  info.tokenBits = kTokenBits;
  info.tokenBytes = kTokenBytes;
  info.traceBits = traceBits;
  info.traceBytes = (traceBits + 7) / 8;
  return info;
}

// Full drains followed by a flush of the residue, inline (threads = 0).
void test_filer_raw_inline() {
  const auto info = make_info();
  const unsigned streamDepth = 16; // tokens per drain
  const size_t totalTokens = streamDepth * 7 + 5;
  const auto src = make_source(totalTokens * kTokenBytes, 1);
  size_t cursor = 0;
  const auto out = tmpfile_name("raw_inline.bin");

  {
    tracedoctor_engine_t engine(
        info, make_filer(info, out), streamDepth,
        /*bufferDepth=*/1, /*bufferGrouping=*/4, /*traceThreads=*/0);
    auto pull = make_pull(src, cursor);
    while (engine.drain(pull, false))
      ;
    while (engine.drain(pull, true))
      ;
    CHECK(engine.total_tokens() == totalTokens, "token count mismatch");
  }

  const auto got = read_file(out);
  CHECK(got == src, "inline filer output != input");
  remove(out.c_str());
  printf("PASS: %s\n", __func__);
}

// Threaded engine with several buffers and grouping; ordered byte-exactness.
void test_filer_raw_threaded() {
  const auto info = make_info();
  const unsigned streamDepth = 32;
  const size_t totalTokens = streamDepth * 129 + 17;
  const auto src = make_source(totalTokens * kTokenBytes, 2);
  size_t cursor = 0;
  const auto out = tmpfile_name("raw_threaded.bin");

  {
    tracedoctor_engine_t engine(
        info, make_filer(info, out), streamDepth,
        /*bufferDepth=*/4, /*bufferGrouping=*/2, /*traceThreads=*/-1);
    auto pull = make_pull(src, cursor);
    while (engine.drain(pull, false))
      ;
    while (engine.drain(pull, true))
      ;
    CHECK(engine.total_tokens() == totalTokens, "token count mismatch");
  }

  const auto got = read_file(out);
  CHECK(got == src, "threaded filer output != input");
  remove(out.c_str());
  printf("PASS: %s\n", __func__);
}

// Two workers must both observe the complete stream.
void test_two_workers() {
  const auto info = make_info();
  const unsigned streamDepth = 8;
  const size_t totalTokens = streamDepth * 33;
  const auto src = make_source(totalTokens * kTokenBytes, 3);
  size_t cursor = 0;
  const auto outA = tmpfile_name("two_a.bin");
  const auto outB = tmpfile_name("two_b.bin");

  {
    std::vector<std::unique_ptr<tracedoctor_worker>> ws;
    ws.push_back(std::make_unique<tracedoctor_filer>(
        std::vector<std::string>{"file:" + outA, "raw"}, info));
    ws.push_back(std::make_unique<tracedoctor_filer>(
        std::vector<std::string>{"file:" + outB, "raw"}, info));
    tracedoctor_engine_t engine(info, std::move(ws), streamDepth,
                                /*bufferDepth=*/4, /*bufferGrouping=*/2,
                                /*traceThreads=*/-1);
    auto pull = make_pull(src, cursor);
    while (engine.drain(pull, false))
      ;
    while (engine.drain(pull, true))
      ;
  }

  CHECK(read_file(outA) == src, "worker A output != input");
  CHECK(read_file(outB) == src, "worker B output != input");
  remove(outA.c_str());
  remove(outB.c_str());
  printf("PASS: %s\n", __func__);
}

// Regression for the dispatched-buffer fix: flush a partial buffer, then
// continue draining. The flushed tokens must not be emitted twice.
void test_flush_then_more_data() {
  const auto info = make_info();
  const unsigned streamDepth = 16;
  const size_t phase1Tokens = 5;             // partial, forced out via flush
  const size_t phase2Tokens = streamDepth * 3; // continues afterwards
  const auto src = make_source((phase1Tokens + phase2Tokens) * kTokenBytes, 4);
  const auto out = tmpfile_name("flush_more.bin");

  {
    std::vector<char> phase1(src.begin(),
                             src.begin() + phase1Tokens * kTokenBytes);
    std::vector<char> phase2(src.begin() + phase1Tokens * kTokenBytes,
                             src.end());
    size_t cursor1 = 0, cursor2 = 0;

    tracedoctor_engine_t engine(
        info, make_filer(info, out), streamDepth,
        /*bufferDepth=*/2, /*bufferGrouping=*/4, /*traceThreads=*/-1);

    auto pull1 = make_pull(phase1, cursor1);
    while (engine.drain(pull1, true)) // mid-run flush (partial buffer)
      ;
    auto pull2 = make_pull(phase2, cursor2);
    while (engine.drain(pull2, false))
      ;
    while (engine.drain(pull2, true))
      ;
    CHECK(engine.total_tokens() == phase1Tokens + phase2Tokens,
          "token count mismatch");
  }

  const auto got = read_file(out);
  CHECK(got == src, "flush-then-drain produced wrong bytes (duplication?)");
  remove(out.c_str());
  printf("PASS: %s\n", __func__);
}

// Non-raw filer strips per-token padding down to traceBytes.
void test_filer_nonraw_strip() {
  const auto info = make_info(/*traceBits=*/256);
  const unsigned streamDepth = 8;
  const size_t totalTokens = streamDepth * 5 + 3;
  const auto src = make_source(totalTokens * kTokenBytes, 5);
  size_t cursor = 0;
  const auto out = tmpfile_name("nonraw.bin");

  {
    tracedoctor_engine_t engine(
        info, make_filer(info, out, /*raw=*/false), streamDepth,
        /*bufferDepth=*/2, /*bufferGrouping=*/2, /*traceThreads=*/-1);
    auto pull = make_pull(src, cursor);
    while (engine.drain(pull, false))
      ;
    while (engine.drain(pull, true))
      ;
  }

  std::vector<char> expected;
  for (size_t t = 0; t < totalTokens; t++) {
    expected.insert(expected.end(), src.begin() + t * kTokenBytes,
                    src.begin() + t * kTokenBytes + info.traceBytes);
  }
  const auto got = read_file(out);
  CHECK(got == expected, "non-raw filer did not strip token padding");
  remove(out.c_str());
  printf("PASS: %s\n", __func__);
}

// Compression pipe: write .zst through the worker, decompress, compare.
void test_compression_zst() {
  if (system("command -v zstd > /dev/null 2>&1") != 0) {
    printf("SKIP: %s (zstd not installed)\n", __func__);
    return;
  }

  const auto info = make_info();
  const unsigned streamDepth = 16;
  const size_t totalTokens = streamDepth * 20;
  const auto src = make_source(totalTokens * kTokenBytes, 6);
  size_t cursor = 0;
  const auto out = tmpfile_name("comp.bin.zst");

  {
    tracedoctor_engine_t engine(
        info, make_filer(info, out), streamDepth,
        /*bufferDepth=*/2, /*bufferGrouping=*/2, /*traceThreads=*/-1);
    auto pull = make_pull(src, cursor);
    while (engine.drain(pull, false))
      ;
    while (engine.drain(pull, true))
      ;
  }

  const auto decomp = out + ".decomp";
  const auto cmd = "zstd -d -q -c " + out + " > " + decomp;
  CHECK(system(cmd.c_str()) == 0, "zstd decompression failed");
  const auto got = read_file(decomp);
  CHECK(got == src, "decompressed output != input");
  remove(out.c_str());
  remove(decomp.c_str());
  printf("PASS: %s\n", __func__);
}

// Round-robin thread pool (fewer threads than workers).
void test_round_robin_pool() {
  const auto info = make_info();
  const unsigned streamDepth = 8;
  const size_t totalTokens = streamDepth * 65;
  const auto src = make_source(totalTokens * kTokenBytes, 7);
  size_t cursor = 0;
  const auto outA = tmpfile_name("rr_a.bin");
  const auto outB = tmpfile_name("rr_b.bin");
  const auto outC = tmpfile_name("rr_c.bin");

  {
    std::vector<std::unique_ptr<tracedoctor_worker>> ws;
    for (const auto &o : {outA, outB, outC}) {
      ws.push_back(std::make_unique<tracedoctor_filer>(
          std::vector<std::string>{"file:" + o, "raw"}, info));
    }
    tracedoctor_engine_t engine(info, std::move(ws), streamDepth,
                                /*bufferDepth=*/4, /*bufferGrouping=*/2,
                                /*traceThreads=*/2);
    auto pull = make_pull(src, cursor);
    while (engine.drain(pull, false))
      ;
    while (engine.drain(pull, true))
      ;
  }

  CHECK(read_file(outA) == src, "rr worker A output != input");
  CHECK(read_file(outB) == src, "rr worker B output != input");
  CHECK(read_file(outC) == src, "rr worker C output != input");
  remove(outA.c_str());
  remove(outB.c_str());
  remove(outC.c_str());
  printf("PASS: %s\n", __func__);
}

// ------------------------------------------------------------------ oracle

#include "tracedoctor_oracle.h"

oracleToken mk_token(uint64_t tsc, unsigned state,
                     std::vector<std::pair<uint64_t, unsigned>> slots = {},
                     unsigned prv = 3, unsigned asid = 0) {
  oracleToken t = {};
  t.word0 = (tsc & ((1ULL << 48) - 1)) | ((uint64_t)state << 48);
  t.ctx = ((uint64_t)oracleToken::FORMAT_TAG << 56) |
          ((uint64_t)(prv & 0x3) << 16) | (asid & 0xffff);
  unsigned s = 0;
  for (auto &[pc, flags] : slots) {
    t.flags |= (uint64_t)(flags | 1) << (16 * s); // |1 = arch_valid
    t.pc[s] = pc;
    s++;
  }
  return t;
}

struct oracleRow {
  uint64_t retired = 0, comp = 0, stall = 0, flush = 0, drain = 0;
  bool operator==(oracleRow const &o) const {
    return retired == o.retired && comp == o.comp && stall == o.stall &&
           flush == o.flush && drain == o.drain;
  }
  uint64_t total() const { return comp + stall + flush + drain; }
};

std::map<uint64_t, oracleRow> read_oracle_csv(const std::string &path) {
  std::map<uint64_t, oracleRow> m;
  std::ifstream f(path);
  std::string line;
  std::getline(f, line); // header
  while (std::getline(f, line)) {
    uint64_t pc;
    unsigned asid;
    oracleRow r;
    if (sscanf(line.c_str(), "%lx,%u,%lu,%lu,%lu,%lu,%lu", &pc, &asid,
               &r.retired, &r.comp, &r.stall, &r.flush, &r.drain) == 7)
      m[pc] = r;
  }
  return m;
}

// Hand-built stream covering Computing, Stalled, Flushed, Drained and the
// 1/n split, with exactly computable attribution.
void test_oracle_ground_truth() {
  constexpr unsigned COMMIT = 0x01, EMPTY = 0x04;
  constexpr unsigned BR = 0x02, BMISS = 0x20;
  std::vector<oracleToken> toks = {
      mk_token(10, COMMIT, {{0x1000, 0}}),          // computing
      mk_token(20, COMMIT, {{0x2000, 0}}),          // 9 stalled -> 0x2000
      mk_token(30, COMMIT, {{0x3000, BR | BMISS}}), // 9 stalled -> 0x3000
      mk_token(31, EMPTY),                          // flushed drain onset
      mk_token(40, COMMIT, {{0x4000, 0}}),          // 31..39 flushed -> 0x3000
      mk_token(50, COMMIT, {{0x5000, 0}}),          // 9 stalled -> 0x5000
      mk_token(51, EMPTY),                          // drained onset (no flush)
      mk_token(60, COMMIT, {{0x6000, 0}}),          // 51..59 drained -> 0x6000
      mk_token(61, COMMIT, {{0x7000, 0}, {0x7004, 0}}),  // 1/2 split
  };

  const auto out = tmpfile_name("oracle_gt.csv");
  const auto info = make_info();
  {
    tracedoctor_oracle w({"file:" + out}, info);
    w.tick(reinterpret_cast<char *>(toks.data()), toks.size());
  }
  auto m = read_oracle_csv(out);
  auto expect = [&](uint64_t pc, oracleRow want) {
    if (!(m[pc] == want)) {
      fprintf(stderr,
              "FAIL: %s pc=%lx got (r%lu c%lu s%lu f%lu d%lu) want (r%lu c%lu "
              "s%lu f%lu d%lu)\n",
              __func__, pc, m[pc].retired, m[pc].comp, m[pc].stall,
              m[pc].flush, m[pc].drain, want.retired, want.comp, want.stall,
              want.flush, want.drain);
      g_failures++;
    }
  };
  //                 retired comp stall flush drain
  expect(0x1000, {1, 12, 0, 0, 0});
  expect(0x2000, {1, 12, 108, 0, 0});   // 9 stalled cycles forward
  expect(0x3000, {1, 12, 108, 108, 0}); // + (1+9) flushed cycles backward
  expect(0x4000, {1, 12, 0, 0, 0});
  expect(0x5000, {1, 12, 108, 0, 0});
  expect(0x6000, {1, 12, 0, 0, 108});   // (1+9) drained cycles forward
  expect(0x7000, {1, 6, 0, 0, 0});      // half a cycle each
  expect(0x7004, {1, 6, 0, 0, 0});
  remove(out.c_str());
  if (!g_failures)
    printf("PASS: %s\n", __func__);
}

struct bbRow {
  uint64_t pc = 0, retired = 0, comp = 0, stall = 0, flush = 0, drain = 0;
  uint64_t exit = 0;
  unsigned prv = 0, asid = 0;
};

std::vector<bbRow> read_bb_csv(const std::string &path) {
  std::vector<bbRow> v;
  std::ifstream f(path);
  std::string line;
  std::getline(f, line); // header
  while (std::getline(f, line)) {
    bbRow r;
    uint64_t tsc, xtsc;
    if (sscanf(line.c_str(), "%lu,%lu,0x%lx,%u,%u,%lu,%lu,%lu,%lu,%lu", &tsc,
               &xtsc, &r.pc, &r.prv, &r.asid, &r.retired, &r.comp, &r.stall,
               &r.flush, &r.drain) == 10) {
      r.exit = xtsc;
      v.push_back(r);
    }
  }
  return v;
}

// Trap edges must terminate BB instances: a flush_on_commit commit
// (sret/ecall) ends its own BB, and a non-committing exception means the
// next commit opens the handler's instance. Regression for the 602.gcc_s
// finding where post-trap entry commits were absorbed into the pre-trap
// instance (kernel<->user entry instructions credited to the wrong BB).
void test_bboracle_trap_boundaries() {
  constexpr unsigned COMMIT = 0x01, EMPTY = 0x04, EXC = 0x10;
  constexpr unsigned BR = 0x02, JAL = 0x04, FLUSH = 0x10;
  const auto info = make_info();

  { // sret-like: flush_on_commit commit closes its instance
    std::vector<oracleToken> toks = {
        mk_token(10, COMMIT, {{0x100, 0}, {0x104, BR}}),
        mk_token(11, COMMIT, {{0x200, FLUSH}}),
        mk_token(12, EMPTY), // squash refill: Flushed, charged to 0x200
        mk_token(20, COMMIT, {{0x300, 0}, {0x304, JAL}}, /*prv=*/0,
                 /*asid=*/42), // user-mode resume: tags must stick
        mk_token(21, COMMIT, {{0x400, 0}}, 0, 42),
    };
    const auto out = tmpfile_name("bb_trap_sret.csv");
    {
      tracedoctor_bboracle w({"file:" + out}, info);
      w.tick(reinterpret_cast<char *>(toks.data()), toks.size());
    }
    auto v = read_bb_csv(out);
    CHECK(v.size() == 4, "sret: expected 4 instances");
    if (v.size() == 4) {
      CHECK(v[0].pc == 0x100 && v[0].retired == 2, "sret: pre-trap inst");
      CHECK(v[0].exit == 10, "sret: exit tsc = last commit cycle");
      CHECK(v[1].pc == 0x200 && v[1].retired == 1, "sret: flusher inst");
      CHECK(v[1].exit == 11, "sret: flusher exit tsc");
      CHECK(v[1].flush == 96, "sret: refill flushed to flusher");
      CHECK(v[2].pc == 0x300 && v[2].retired == 2, "sret: post-trap entry");
      CHECK(v[2].prv == 0 && v[2].asid == 42, "sret: user tags on entry inst");
      CHECK(v[1].prv == 3, "sret: kernel tag on flusher inst");
      CHECK(v[3].pc == 0x400 && v[3].retired == 1, "sret: tail inst");
    }
    remove(out.c_str());
  }

  { // fence.i/CSR-write-like: flushing commit with SEQUENTIAL resume must
    // NOT split the instance (it is a timing event, not a control arc)
    std::vector<oracleToken> toks = {
        mk_token(10, COMMIT, {{0x100, 0}, {0x104, BR}}),
        mk_token(11, COMMIT, {{0x200, FLUSH}}), // fence.i at 0x200
        mk_token(12, EMPTY),                    // refetch
        mk_token(20, COMMIT, {{0x204, 0}}),     // sequential resume
        mk_token(21, COMMIT, {{0x208, JAL}}),
        mk_token(22, COMMIT, {{0x900, 0}}),
    };
    const auto out = tmpfile_name("bb_trap_fencei.csv");
    {
      tracedoctor_bboracle w({"file:" + out}, info);
      w.tick(reinterpret_cast<char *>(toks.data()), toks.size());
    }
    auto v = read_bb_csv(out);
    CHECK(v.size() == 3, "fencei: expected 3 instances");
    if (v.size() == 3) {
      CHECK(v[0].pc == 0x100 && v[0].retired == 2, "fencei: pre inst");
      CHECK(v[1].pc == 0x200 && v[1].retired == 3,
            "fencei: one instance across the sequential flush");
      CHECK(v[2].pc == 0x900 && v[2].retired == 1, "fencei: tail inst");
    }
    remove(out.c_str());
  }

  { // exception: non-committing squash; handler commit opens fresh
    std::vector<oracleToken> toks = {
        mk_token(10, COMMIT, {{0x100, 0}}),
        mk_token(11, EXC),
        mk_token(12, EMPTY), // refill reads Drained (no flushing commit)
        mk_token(20, COMMIT, {{0x500, 0}, {0x504, BR}}),
        mk_token(21, COMMIT, {{0x600, 0}}),
    };
    const auto out = tmpfile_name("bb_trap_exc.csv");
    {
      tracedoctor_bboracle w({"file:" + out}, info);
      w.tick(reinterpret_cast<char *>(toks.data()), toks.size());
    }
    auto v = read_bb_csv(out);
    CHECK(v.size() == 3, "exc: expected 3 instances");
    if (v.size() == 3) {
      CHECK(v[0].pc == 0x100 && v[0].retired == 1, "exc: pre-trap inst");
      CHECK(v[1].pc == 0x500 && v[1].retired == 2, "exc: handler entry inst");
      CHECK(v[1].drain == 96, "exc: refill drained to handler");
      CHECK(v[1].stall == 12, "exc: exception cycle forward to handler");
      CHECK(v[2].pc == 0x600 && v[2].retired == 1, "exc: tail inst");
    }
    remove(out.c_str());
  }
  if (!g_failures)
    printf("PASS: %s\n", __func__);
}

// Workers must hard-reject tokens without the current format tag (captures
// from bitstreams predating prv/asid) instead of silently mis-decoding.
void test_reject_old_format() {
  auto t = mk_token(10, 0x01, {{0x100, 0}});
  t.ctx = 0; // old bitstream: reserved word was zero
  const auto info = make_info();
  const auto out = tmpfile_name("reject_old.csv");
  bool threw = false;
  try {
    tracedoctor_bboracle w({"file:" + out}, info);
    w.tick(reinterpret_cast<char *>(&t), 1);
  } catch (std::exception const &) {
    threw = true;
  }
  CHECK(threw, "old-format token must throw");
  remove(out.c_str());
  if (!g_failures)
    printf("PASS: %s\n", __func__);
}

// bbtrace must emit Trap arcs at trap edges like the tacit decoder does:
// (sret pc -> next commit) exactly; for a non-committing exception the
// from-pc is approximated by the last committed pc.
void test_bbtrace_trap_arcs() {
  constexpr unsigned COMMIT = 0x01, EMPTY = 0x04, EXC = 0x10;
  constexpr unsigned BR = 0x02, FLUSH = 0x10;
  std::vector<oracleToken> toks = {
      mk_token(10, COMMIT, {{0x100, 0}, {0x104, BR}}),
      mk_token(11, COMMIT, {{0x200, FLUSH}}),
      mk_token(12, EMPTY),
      mk_token(20, COMMIT, {{0x300, 0}}),
      mk_token(21, EXC),
      mk_token(30, COMMIT, {{0x700, FLUSH}}), // fence.i-like: sequential
      mk_token(31, COMMIT, {{0x704, 0}}),     // resume, must emit NO arc
  };
  const auto out = tmpfile_name("bbtrace_trap.txt");
  const auto info = make_info();
  {
    tracedoctor_bbtrace w({"file:" + out}, info);
    w.tick(reinterpret_cast<char *>(toks.data()), toks.size());
  }
  std::ifstream f(out);
  std::vector<std::string> lines;
  std::string line;
  while (std::getline(f, line))
    lines.push_back(line);
  CHECK(lines.size() == 3, "bbtrace: expected 3 arcs");
  if (lines.size() == 3) {
    CHECK(lines[0] == "[timestamp: 10] TakenBranch: 0x104 -> 0x200",
          "bbtrace: br arc");
    CHECK(lines[1] == "[timestamp: 11] Trap: 0x200 -> 0x300",
          "bbtrace: sret trap arc");
    CHECK(lines[2] == "[timestamp: 21] Trap: 0x300 -> 0x700",
          "bbtrace: exception trap arc");
  }
  remove(out.c_str());
  if (!g_failures)
    printf("PASS: %s\n", __func__);
}

// Replay the real metasim capture if present: totals must reconcile.
void test_oracle_replay_real() {
  const char *cap = getenv("ORACLE_TOKENS");
  if (!cap) {
    printf("SKIP: %s (set ORACLE_TOKENS=path to a capture)\n", __func__);
    return;
  }
  auto data = read_file(cap);
  const auto out = tmpfile_name("oracle_real.csv");
  const auto info = make_info();
  {
    tracedoctor_oracle w({"file:" + out}, info);
    w.tick(data.data(), data.size() / 64);
  }
  auto m = read_oracle_csv(out);
  uint64_t retired = 0, cx12 = 0;
  for (auto &[pc, st] : m) {
    retired += st.retired;
    cx12 += st.total();
  }
  printf("%s: pcs=%zu retired=%lu cycles=%.1f\n", __func__, m.size(), retired,
         cx12 / 12.0);
  CHECK(retired > 0, "no commits attributed");
  remove(out.c_str());
  printf("PASS: %s\n", __func__);
}

} // namespace

int main() {
  test_filer_raw_inline();
  test_filer_raw_threaded();
  test_two_workers();
  test_flush_then_more_data();
  test_filer_nonraw_strip();
  test_compression_zst();
  test_round_robin_pool();
  test_oracle_ground_truth();
  test_bboracle_trap_boundaries();
  test_bbtrace_trap_arcs();
  test_reject_old_format();
  test_oracle_replay_real();

  if (g_failures) {
    fprintf(stderr, "%d test(s) FAILED\n", g_failures);
    return 1;
  }
  printf("All tests passed.\n");
  return 0;
}
