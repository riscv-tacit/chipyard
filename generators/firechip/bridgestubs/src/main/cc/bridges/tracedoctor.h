// See LICENSE for license details
// Ported from firesim/firesim#1501 (TraceDoctor, EECS-NTNU) onto the modern
// KIND-registered streaming bridge driver API.

#ifndef __TRACEDOCTOR_H_
#define __TRACEDOCTOR_H_

#include <memory>
#include <string>
#include <vector>

#include "core/bridge_driver.h"
#include "core/clock_info.h"

#include "tracedoctor_engine.h"

class StreamEngine;

/**
 * Structure carrying the addresses of all fixed MMIO ports.
 * Field order must match the register declaration order in
 * TraceDoctorBridgeModule (goldengateimplementations).
 */
struct TRACEDOCTORBRIDGEMODULE_struct {
  uint64_t initDone;
  uint64_t traceEnable;
  uint64_t triggerSelector;
};

class tracedoctor_t final : public streaming_bridge_driver_t {
public:
  /// The identifier for the bridge type used for casts.
  static char KIND;

  tracedoctor_t(simif_t &simif,
                StreamEngine &stream,
                const TRACEDOCTORBRIDGEMODULE_struct &mmio_addrs,
                int tracerno,
                const std::vector<std::string> &args,
                int stream_idx,
                int stream_depth,
                unsigned int token_width,
                unsigned int trace_width,
                const ClockInfo &clock_info);
  ~tracedoctor_t() override;

  void init() override;
  void tick() override;
  void finish() override;

  bool trace_enabled() const { return traceEnabled; }
  const traceInfo &trace_info() const { return info; }

private:
  const TRACEDOCTORBRIDGEMODULE_struct mmio_addrs;
  const int stream_idx;
  const int stream_depth;

  ClockInfo clock_info;
  struct traceInfo info = {};

  bool traceEnabled = false;
  unsigned int traceTrigger = 0;

  std::unique_ptr<tracedoctor_engine_t> engine;
  tracedoctor_pull_fn pull_fn;
};

#endif // __TRACEDOCTOR_H_
