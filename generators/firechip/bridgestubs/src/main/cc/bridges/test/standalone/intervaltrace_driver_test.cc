// See LICENSE for license details.
#include "intervaltrace.h"
#include "core/config.h"
#include "core/simulation.h"
#include <algorithm>
#include <cassert>
#include <filesystem>
#include <fstream>
#include <functional>
#include <iostream>
#include <iterator>
#include <unistd.h>

// Link the real simif, bridge_driver, widget and StreamEngine implementations.
// This test invokes the driver lifecycle directly, without a simulation loop.
int simulation_t::execute_simulation_flow() { throw std::logic_error("unused simulation loop"); }

static const TargetConfig target_config{{0, 0, 0}, {0, 0, 0}, 0, {}, {}, {0, 0}, "interval-driver-test"};
static const INTERVALTRACEBRIDGEMODULE_struct addrs{0, 4, 8, 12, 16, 20, 24, 28, 32, 36, 40, 44};

class MockSim : public simif_t {
public:
  MockSim() : simif_t(target_config) {}
  std::vector<std::pair<size_t, uint32_t>> writes;
  bool stopped = false;
  unsigned stop_polls = 0;
  void write(size_t addr, uint32_t value) override {
    writes.emplace_back(addr, value);
    if (addr == addrs.capture_enable && value == 0) stopped = true;
  }
  uint32_t read(size_t addr) override {
    if (addr == addrs.config_error) return 0;
    assert(addr == addrs.drained && stopped);
    return ++stop_polls >= 3;
  }
};

class MockStream : public FPGAToCPUStreamDriver {
public:
  MockStream(const std::vector<uint8_t> &bytes, MockSim &sim) : bytes(bytes), sim(sim) {}
  const std::vector<uint8_t> bytes;
  MockSim &sim;
  size_t position = 0;
  bool flushed = false;
  void init() override {}
  size_t pull(void *dest, size_t capacity, size_t required) override {
    // Hold the final beat until the driver flushes the stream engine.
    const size_t visible = flushed ? bytes.size() : (bytes.size() >= 64 ? bytes.size() - 64 : 0);
    const size_t count = std::min({capacity, visible - position, size_t(128)});
    if (count < required) return 0;
    if (count) std::memcpy(dest, bytes.data() + position, count);
    position += count;
    return count;
  }
  void flush() override {
    assert(sim.stopped && sim.stop_polls >= 3);
    flushed = true;
  }
};

class MockEngine : public StreamEngine {
public:
  MockEngine(const std::vector<uint8_t> &bytes, MockSim &sim) {
    fpga_to_cpu_streams.push_back(std::make_unique<MockStream>(bytes, sim));
  }
  MockStream &input() { return static_cast<MockStream &>(*fpga_to_cpu_streams.front()); }
};

static std::vector<uint8_t> read_file(const std::filesystem::path &path) {
  std::ifstream file(path, std::ios::binary);
  assert(file.good());
  return {(std::istreambuf_iterator<char>(file)), {}};
}
static void rejects(const std::function<void()> &fn) {
  bool threw = false;
  try { fn(); } catch (const std::runtime_error &) { threw = true; }
  assert(threw);
}

// Capture only diagnostics; assertions and failures still terminate the test.
class Diagnostics {
public:
  Diagnostics() : saved(dup(STDERR_FILENO)), file(std::tmpfile()) {
    assert(saved >= 0 && file);
    std::fflush(stderr);
    assert(dup2(fileno(file), STDERR_FILENO) >= 0);
  }
  ~Diagnostics() {
    std::fflush(stderr);
    assert(dup2(saved, STDERR_FILENO) >= 0);
    close(saved);
    std::fclose(file);
  }
  std::string text() {
    std::fflush(stderr);
    std::rewind(file);
    std::string result;
    for (int c; (c = std::fgetc(file)) != EOF;) result += char(c);
    std::fseek(file, 0, SEEK_END);
    return result;
  }
private:
  int saved;
  FILE *file;
};

int main(int argc, char **argv) {
  // Arguments: output directory, stream, counter count, catalog ID and, optionally, the stall event ID.
  assert(argc == 5 || argc == 6);
  const std::filesystem::path out(argv[1]);
  const auto bytes = read_file(argv[2]);
  const unsigned counters = std::stoul(argv[3]);
  const uint64_t catalog = std::stoull(argv[4]);
  const int64_t stall = argc == 6 ? std::stoll(argv[5]) : -1;
  const std::string manifest = "{\"test_catalog\":true}";
  std::vector<std::string> event_names;
  for (unsigned i = 0; i < counters; ++i) event_names.push_back("event_" + std::to_string(i));
  event_names[0] = "event,\"zero\""; // Exercise CSV header escaping.
  auto args = [&](const char *name) {
    return std::vector<std::string>{"+interval-trace-file=" + (out / name).string(),
                                   "+interval-trace-interval=4294967298"};
  };
  for (const std::string name : {"default", "raw-only", "normal", "raw"}) {
    const bool raw = name == "raw" || name == "raw-only";
    const bool csv = name == "normal" || name == "raw";
    auto options = args(name.c_str());
    if (raw) options.push_back("+interval-trace-raw");
    if (csv) options.push_back("+interval-trace-csv");
    if (raw) options.push_back("+interval-trace-trigger");
    MockSim sim;
    MockEngine stream(bytes, sim);
    intervaltrace_t driver(sim, stream, addrs, 7, options, 0, 16, counters, catalog, manifest.c_str(), event_names, stall);
    Diagnostics messages;
    // Reusing a prefix must not leave an old successful output on failure.
    { std::ofstream previous(out / (name + "-7-intervals.jsonl")); previous << "old"; }
    driver.init();
    assert(!std::filesystem::exists(out / (name + "-7-intervals.jsonl")));
    const std::vector<std::pair<size_t, uint32_t>> expected{{8, 2}, {12, 1}, {28, raw ? 3u : 0u},
      {32, raw ? 0x8013u : 0u}, {36, raw ? UINT32_MAX : 0u},
      {40, raw ? 0x10013u : UINT32_MAX}, {44, UINT32_MAX}, {4, 1}, {0, 1}};
    assert(sim.writes == expected);
    while (stream.input().position < bytes.size() - 64) driver.tick();
    // Both outputs must already be readable while the driver is alive and before finish().
    const auto live_catalog = read_file(out / (name + "-7-blocks.csv"));
    const auto live_intervals = read_file(out / (name + "-7-intervals.jsonl.partial"));
    assert(std::count(live_catalog.begin(), live_catalog.end(), '\n') > 1);
    assert(!live_intervals.empty() && live_intervals.back() == '\n');
    const std::string live_text(live_intervals.begin(), live_intervals.end());
    assert(live_text.find("\"interval_format_version\":3") != std::string::npos);
    driver.finish();
    assert(!driver.terminate() && driver.exit_code() == 0);
    assert(stream.input().flushed && stream.input().position == bytes.size());
    if (raw) assert(read_file(out / (name + "-7.bin")) == bytes);
    else assert(!std::filesystem::exists(out / (name + "-7.bin")));
    for (const char *suffix : {"-bb.csv", "-pmu.csv", "-epochs.csv"})
      if (csv) assert(!read_file(out / (name + "-7" + suffix)).empty());
      else assert(!std::filesystem::exists(out / (name + "-7" + suffix)));
    assert(!read_file(out / (name + "-7-intervals.jsonl")).empty());
    assert(!std::filesystem::exists(out / (name + "-7-intervals.jsonl.partial")));
    const auto json = read_file(out / (name + "-7.json"));
    assert(std::string(json.begin(), json.end()) == manifest);
    const auto summary = messages.text();
    assert(summary.find("warning:") == std::string::npos);
    assert(summary.find("epochs=") != std::string::npos && summary.find("partial=") != std::string::npos);
    assert(summary.find("bytes=" + std::to_string(bytes.size())) != std::string::npos);
    assert((summary.find("stall_cycles=") != std::string::npos) == (stall >= 0));
    const auto write_count = sim.writes.size();
    driver.finish(); driver.tick();
    assert(messages.text() == summary && sim.writes.size() == write_count);
  }
  {
    MockSim sim;
    MockEngine stream({}, sim);
    intervaltrace_t driver(sim, stream, addrs, 0, args("empty"), 0, 16, counters, catalog, manifest.c_str(), event_names, stall);
    Diagnostics messages;
    driver.init(); driver.finish(); driver.finish();
    const auto text = messages.text();
    const auto warning = text.find("warning: empty capture");
    assert(warning != std::string::npos && text.find("warning:", warning + 1) == std::string::npos);
    assert(text.find("BBs=0") != std::string::npos && driver.exit_code() == 0);
  }
  {
    MockSim sim;
    MockEngine stream({}, sim);
    auto disabled_args = args("disabled");
    disabled_args.push_back("+interval-trace-disable");
    intervaltrace_t driver(sim, stream, addrs, 0, disabled_args, 0, 16, counters, catalog, manifest.c_str(), event_names, stall);
    Diagnostics messages;
    driver.init(); driver.tick(); driver.finish();
    assert(messages.text().empty());
    for (const char *suffix : {".bin", ".json", "-blocks.csv", "-bb.csv", "-pmu.csv", "-epochs.csv", "-intervals.jsonl", "-intervals.jsonl.partial"})
      assert(!std::filesystem::exists(out / (std::string("disabled-0") + suffix)));
  }
  for (bool late : {false, true}) {
    auto bad = bytes;
    if (late) bad.resize(bad.size() - 1); else bad[0] = 255;
    const char *name = late ? "truncated" : "malformed";
    MockSim sim;
    MockEngine stream(bad, sim);
    auto options = args(name);
    options.push_back("+interval-trace-raw");
    options.push_back("+interval-trace-csv");
    intervaltrace_t driver(sim, stream, addrs, 0, options, 0, 16, counters, catalog, manifest.c_str(), event_names, stall);
    Diagnostics messages;
    driver.init(); driver.tick();
    if (!late) assert(driver.terminate() && driver.exit_code() == 1);
    rejects([&] { driver.finish(); });
    assert(driver.terminate() && driver.exit_code() == 1);
    assert(!std::filesystem::exists(out / (std::string(name) + "-0-intervals.jsonl")));
    assert(std::filesystem::exists(out / (std::string(name) + "-0-intervals.jsonl.partial")));
    const auto raw = read_file(out / (std::string(name) + "-0.bin"));
    assert(!raw.empty() && std::equal(raw.begin(), raw.end(), bad.begin()));
    if (late) assert(raw == bad);
    const auto text = messages.text();
    assert(text.find("error:") != std::string::npos);
    assert(text.find("epochs=") == std::string::npos && text.find("warning:") == std::string::npos);
  }
  // Trigger parsing is independent of option order and programs 64-bit endpoints
  // before capture_enable/init_done. Reuse the real driver MMIO lifecycle.
  const std::vector<std::vector<std::string>> trigger_options{
    {"+interval-trace-select=1", "+interval-trace-start=4294967298", "+interval-trace-end=8589934595"},
    {"+interval-trace-end=ffffff8000001004", "+interval-trace-start=0xffffff8000001000", "+interval-trace-select=2"},
    {"+interval-trace-select=3", "+interval-trace-start=ffff0000abcd0000", "+interval-trace-end=ffff0000ef120000"},
    {"+interval-trace-trigger", "+interval-trace-select=3"},
    {"+interval-trace-select=1"}};
  const std::vector<std::vector<uint32_t>> trigger_values{
    {1, 2, 1, 3, 2}, {2, 0x1000, 0xffffff80, 0x1004, 0xffffff80},
    {3, 0xabcd0000, 0xffff0000, 0xef120000, 0xffff0000},
    {3, 0x8013, UINT32_MAX, 0x10013, UINT32_MAX}, {1, 0, 0, UINT32_MAX, UINT32_MAX}};
  for (size_t i = 0; i < trigger_options.size(); ++i) {
    MockSim sim;
    MockEngine stream({}, sim);
    auto options = args(("trigger-" + std::to_string(i)).c_str());
    options.insert(options.end(), trigger_options[i].begin(), trigger_options[i].end());
    intervaltrace_t driver(sim, stream, addrs, 0, options, 0, 16, counters, catalog, manifest.c_str(), event_names, stall);
    Diagnostics messages;
    driver.init();
    for (size_t j = 0; j < 5; ++j)
      assert(sim.writes[j + 2] == std::make_pair(size_t(28 + 4 * j), trigger_values[i][j]));
    assert(sim.writes[7] == std::make_pair(size_t(4), uint32_t(1)));
    assert(sim.writes[8] == std::make_pair(size_t(0), uint32_t(1)));
    driver.finish();
  }
  const std::vector<std::vector<std::string>> invalid_options{
    {"+interval-trace-select=4"}, {"+interval-trace-select=-1"}, {"+interval-trace-select=1junk"},
    {"+interval-trace-select=2", "+interval-trace-start=1000"},
    {"+interval-trace-start=1000"}, {"+interval-trace-trigger", "+interval-trace-select=1"},
    {"+interval-trace-select=1", "+interval-trace-start=20", "+interval-trace-end=10"},
    {"+interval-trace-select=1", "+interval-trace-start=18446744073709551616"},
    {"+interval-trace-select=3", "+interval-trace-start=0x"},
    {"+interval-trace-select=3", "+interval-trace-end="},
    {"+interval-trace-select=1", "+interval-trace-start=0x10"}};
  for (const auto &extra : invalid_options) {
    MockSim sim;
    MockEngine stream({}, sim);
    auto options = args("bad-trigger");
    options.insert(options.end(), extra.begin(), extra.end());
    rejects([&] { intervaltrace_t driver(sim, stream, addrs, 0, options, 0, 16, counters, catalog, manifest.c_str(), event_names, stall); });
    assert(sim.writes.empty());
  }
  {
    std::filesystem::create_symlink("/dev/full", out / "full-catalog-0-blocks.csv");
    MockSim sim;
    MockEngine stream(bytes, sim);
    intervaltrace_t driver(sim, stream, addrs, 0, args("full-catalog"), 0, 16, counters, catalog,
                          manifest.c_str(), event_names, stall);
    rejects([&] { driver.init(); });
    assert(sim.writes.empty());
  }
  // A buffered write can fail only on close; that must still fail the run.
  for (const char *suffix : {".bin", "-bb.csv", "-pmu.csv", "-epochs.csv", "-intervals.jsonl.partial"}) {
    const std::string name = std::string("full") + suffix;
    std::filesystem::create_symlink("/dev/full", out / (name + "-0" + suffix));
    MockSim sim;
    MockEngine stream(bytes, sim);
    auto options = args(name.c_str());
    options.push_back("+interval-trace-raw");
    options.push_back("+interval-trace-csv");
    intervaltrace_t driver(sim, stream, addrs, 0, options, 0, 16, counters, catalog, manifest.c_str(), event_names, stall);
    Diagnostics messages;
    driver.init(); driver.tick();
    rejects([&] { driver.finish(); });
    assert(driver.exit_code() == 1 && messages.text().find("error:") != std::string::npos);
  }
  std::cout << "PASS: driver MMIO, default interval aggregation, optional CSV/raw dumps, stop/flush, diagnostics, disabled capture and fatal failures\n";
}
