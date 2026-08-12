// See LICENSE for license details.

#include "bridges/peek_poke.h"
#include "bridges/tracedoctor.h"
#include "core/bridge_driver.h"
#include "core/simif.h"
#include "core/simulation.h"

#include <cstring>
#include <fstream>
#include <iostream>
#include <random>
#include <string_view>
#include <vector>

/**
 * Metasim unit test for the TraceDoctor bridge.
 *
 * Pokes random trace vectors (64-bit lanes) with random valids into the
 * TraceDoctorDUT, one target cycle at a time, and records the vectors of
 * valid cycles. The raw 'filer' worker output must contain exactly those
 * vectors, one stream token per valid cycle, and is diffed against the
 * expected file written here (see TraceDoctorSuite.scala).
 */
class TraceDoctorModule : public simulation_t {
private:
  peek_poke_t &peek_poke;
  tracedoctor_t &tracedoctor;

  uint64_t random_seed = 0;
  std::mt19937_64 gen;

  std::string expected_fname;
  std::vector<char> expected;

public:
  TraceDoctorModule(widget_registry_t &registry,
                    const std::vector<std::string> &args,
                    std::string_view target_name)
      : simulation_t(registry, args),
        peek_poke(registry.get_widget<peek_poke_t>()),
        tracedoctor(registry.get_widget<tracedoctor_t>()) {

    for (auto &arg : args) {
      if (arg.find("+seed=") == 0) {
        random_seed = strtoll(arg.c_str() + 6, nullptr, 10);
        fprintf(stderr, "Using custom SEED: %ld\n", random_seed);
      }

      if (arg.find("+tracedoctor-expected-output=") == 0) {
        expected_fname = arg.substr(strlen("+tracedoctor-expected-output="));
      }
    }
    gen.seed(random_seed);
  }

  ~TraceDoctorModule() override = default;

  bool steps(const unsigned s) {
    peek_poke.step(s, /*blocking=*/false);
    const unsigned timeout = 10000 + s;
    bool was_done = false;
    for (unsigned i = 0; i < timeout; i++) {
      for (auto *bridge : registry.get_all_bridges()) {
        bridge->tick();
      }

      if (peek_poke.is_done()) {
        was_done = true;
        break;
      }
    }

    if (!was_done) {
      std::cout << "Hit timeout of " << timeout
                << " tick loops after a requested " << s << " steps"
                << std::endl;
    }

    return was_done;
  }

  int simulation_run() override {
    const auto &info = tracedoctor.trace_info();
    const unsigned lanes = info.traceBits / 64;
    const unsigned padBytes = info.tokenBytes - lanes * 8;

    // Reset the DUT with the trace held invalid.
    peek_poke.poke("io_traceValid", 0, /*blocking=*/true);
    peek_poke.poke("reset", 1, /*blocking=*/true);
    peek_poke.step(1, /*blocking=*/true);
    peek_poke.poke("reset", 0, /*blocking=*/true);
    peek_poke.step(1, /*blocking=*/true);

    std::vector<uint64_t> data(lanes);
    for (unsigned t = 0, total = 256; t < total; ++t) {
      const bool valid = (gen() % 10) < 7;

      for (unsigned i = 0; i < lanes; i++) {
        // peek_poke.poke takes uint32_t; the upper half of each 64b lane
        // stays zero in the DUT, so only generate 32b of random data.
        data[i] = (uint32_t)gen();
        peek_poke.poke("io_traceData_" + std::to_string(i),
                       (uint32_t)data[i],
                       /*blocking=*/true);
      }
      peek_poke.poke("io_traceValid", valid, /*blocking=*/true);
      steps(1);

      if (valid) {
        // One token per valid cycle: lanes little-endian, padded to a beat.
        for (unsigned i = 0; i < lanes; i++) {
          const char *p = reinterpret_cast<const char *>(&data[i]);
          expected.insert(expected.end(), p, p + 8);
        }
        expected.insert(expected.end(), padBytes, 0);
      }
    }

    // Stop producing tokens and let everything propagate, then flush.
    peek_poke.poke("io_traceValid", 0, /*blocking=*/true);
    steps(10);
    tracedoctor.finish();

    std::ofstream f(expected_fname, std::ios::binary);
    f.write(expected.data(), expected.size());

    return EXIT_SUCCESS;
  }
};

std::unique_ptr<simulation_t>
create_simulation(simif_t &simif,
                  widget_registry_t &registry,
                  const std::vector<std::string> &args) {
  return std::make_unique<TraceDoctorModule>(registry, args, "TraceDoctorModule");
}
