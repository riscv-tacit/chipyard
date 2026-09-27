// Numeric event adapter for the offline CSV reader. The reusable library lives
// with the driver and consumes the decoder's callback directly.
#include "intervaltrace_json.h"
#include "intervaltrace_decoder.h"
#include <charconv>
#include <iostream>
#include <iomanip>
#include <limits>
#include <sstream>
#include <string>

static uint64_t number(const std::string &s) {
  uint64_t value = 0;
  const auto result = std::from_chars(s.data(), s.data() + s.size(), value);
  if (result.ec != std::errc{} || result.ptr != s.data() + s.size())
    throw std::runtime_error("invalid unsigned integer: " + s);
  return value;
}
int main(int argc, char **argv) {
  try {
    if (argc != 3) throw std::runtime_error("usage: aggregate COUNTER_COUNT CATALOG_ID");
    const auto counters = number(argv[1]);
    if (counters == 0 || counters > 1000000) throw std::runtime_error("invalid counter count");
    intervaltrace_aggregator aggregate([](const intervaltrace_features &f) {
      intervaltrace_write_features(std::cout, f);
    });
    intervaltrace_decoder decoder(counters, number(argv[2]),
      [&](unsigned kind, unsigned flags, uint64_t epoch, const std::vector<uint64_t> &w) {
        aggregate.consume(kind, flags, epoch, w);
      });
    for (std::string line; std::getline(std::cin, line);) {
      std::istringstream input(line);
      std::vector<uint64_t> fields;
      for (std::string token; input >> token;) fields.push_back(number(token));
      if (fields.size() < 2 || fields[0] > 4 || fields[1] > 65535)
        throw std::runtime_error("invalid event record");
      decoder.push_record(fields[0], fields[1], {fields.begin() + 2, fields.end()});
    }
    if (std::cin.bad()) throw std::runtime_error("cannot read event stream");
    decoder.finish();
    std::cout.flush();
    if (!std::cout) throw std::runtime_error("cannot write aggregates");
  } catch (const std::exception &e) {
    std::cerr << "interval aggregation: " << e.what() << '\n';
    return 1;
  }
}
