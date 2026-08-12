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
                     std::vector<std::pair<uint64_t, unsigned>> slots = {}) {
  oracleToken t = {};
  t.word0 = (tsc & ((1ULL << 48) - 1)) | ((uint64_t)state << 48);
  unsigned s = 0;
  for (auto &[pc, flags] : slots) {
    t.flags |= (uint64_t)(flags | 1) << (16 * s); // |1 = arch_valid
    t.pc[s] = pc;
    s++;
  }
  return t;
}

std::map<uint64_t, std::pair<uint64_t, uint64_t>> read_oracle_csv(
    const std::string &path) {
  std::map<uint64_t, std::pair<uint64_t, uint64_t>> m;
  std::ifstream f(path);
  std::string line;
  std::getline(f, line); // header
  while (std::getline(f, line)) {
    uint64_t pc, retired, cx12;
    if (sscanf(line.c_str(), "%lx,%lu,%lu", &pc, &retired, &cx12) == 3)
      m[pc] = {retired, cx12};
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
  auto expect = [&](uint64_t pc, uint64_t retired, uint64_t cx12) {
    if (m[pc] != std::make_pair(retired, cx12)) {
      fprintf(stderr, "FAIL: %s pc=%lx got (%lu,%lu) want (%lu,%lu)\n",
              __func__, pc, m[pc].first, m[pc].second, retired, cx12);
      g_failures++;
    }
  };
  expect(0x1000, 1, 12);
  expect(0x2000, 1, 120);  // 12 + 9 stalled
  expect(0x3000, 1, 228);  // 12 + 9 stalled + (1+9) flushed cycles
  expect(0x4000, 1, 12);
  expect(0x5000, 1, 120);
  expect(0x6000, 1, 120);  // 12 + (1+9) drained forward
  expect(0x7000, 1, 6);    // half a cycle each
  expect(0x7004, 1, 6);
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
    retired += st.first;
    cx12 += st.second;
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
  test_oracle_replay_real();

  if (g_failures) {
    fprintf(stderr, "%d test(s) FAILED\n", g_failures);
    return 1;
  }
  printf("All tests passed.\n");
  return 0;
}
