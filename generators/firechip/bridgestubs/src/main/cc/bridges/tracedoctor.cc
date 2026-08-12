// See LICENSE for license details
// Ported from firesim/firesim#1501 (TraceDoctor, EECS-NTNU).

#include "tracedoctor.h"
#include "core/simif.h"

#include <cstdio>
#include <stdexcept>

#include "tracedoctor_register.h"

char tracedoctor_t::KIND;

tracedoctor_t::tracedoctor_t(simif_t &simif,
                             StreamEngine &stream,
                             const TRACEDOCTORBRIDGEMODULE_struct &mmio_addrs,
                             int tracerno,
                             const std::vector<std::string> &args,
                             int stream_idx,
                             int stream_depth,
                             unsigned int token_width,
                             unsigned int trace_width,
                             const ClockInfo &clock_info)
    : streaming_bridge_driver_t(simif, stream, &KIND), mmio_addrs(mmio_addrs),
      stream_idx(stream_idx), stream_depth(stream_depth),
      clock_info(clock_info) {
  info.tracerId = tracerno;
  info.tokenBits = token_width;
  info.traceBits = trace_width;
  info.tokenBytes = (token_width + 7) / 8;
  info.traceBytes = (trace_width + 7) / 8;

  unsigned int bufferDepth = 64;
  unsigned int bufferGrouping = 1;
  int traceThreads = -1;

  const std::string tracetrigger_arg = "+tracedoctor-trigger=";
  const std::string tracethreads_arg = "+tracedoctor-threads=";
  const std::string tracebuffers_arg = "+tracedoctor-buffers=";
  const std::string traceworker_arg = "+tracedoctor-worker=";

  for (auto &arg : args) {
    if (arg.find(tracetrigger_arg) == 0) {
      std::string const sarg = arg.substr(tracetrigger_arg.length());
      if (sarg.compare("none") == 0) {
        this->traceTrigger = 0;
      } else if (sarg.compare("tracerv") == 0) {
        this->traceTrigger = 1;
      } else {
        throw std::invalid_argument(
            "TraceDoctor@" + std::to_string(info.tracerId) + " '" + sarg +
            "' invalid trigger argument, choose 'none' or 'tracerv'");
      }
    }
    if (arg.find(tracebuffers_arg) == 0) {
      auto bufferargs = strSplit(arg.substr(tracebuffers_arg.length()), ",");
      long tmp = std::stol(bufferargs[0]);
      bufferDepth = (tmp <= 0) ? 1 : tmp;
      if (bufferargs.size() >= 2) {
        tmp = std::stol(bufferargs[1]);
        bufferGrouping = (tmp <= 0) ? 1 : tmp;
      }
    }
    if (arg.find(tracethreads_arg) == 0) {
      traceThreads = std::stol(arg.substr(tracethreads_arg.length()));
    }
  }

  std::vector<std::unique_ptr<tracedoctor_worker>> workers;
  for (auto &arg : args) {
    if (arg.find(traceworker_arg) == 0) {
      auto workerargs = strSplit(arg.substr(traceworker_arg.length()), ",");
      if (workerargs.empty()) {
        throw std::invalid_argument("TraceDoctor@" +
                                    std::to_string(info.tracerId) +
                                    " invalid worker argument");
      }
      std::string const workername = workerargs.front();
      workerargs.erase(workerargs.begin());

      auto const &reg = tracedoctor_register.find(workername);
      if (reg == tracedoctor_register.end()) {
        throw std::invalid_argument("TraceDoctor@" +
                                    std::to_string(info.tracerId) +
                                    " unknown worker '" + workername + "'");
      }
      fprintf(stdout, "TraceDoctor@%d: adding worker '%s' with args '",
              info.tracerId, workername.c_str());
      for (auto &a : workerargs) {
        fprintf(stdout, "%s%s", a.c_str(),
                (&a == &workerargs.back()) ? "" : ", ");
      }
      fprintf(stdout, "'\n");

      workers.push_back(reg->second(workerargs, info));
    }
  }

  if (workers.empty()) {
    fprintf(stdout, "TraceDoctor@%d: no workers selected, disable tracing\n",
            info.tracerId);
    traceEnabled = false;
  } else {
    traceEnabled = true;
    engine = std::make_unique<tracedoctor_engine_t>(info,
                                                    std::move(workers),
                                                    stream_depth,
                                                    bufferDepth,
                                                    bufferGrouping,
                                                    traceThreads);
    pull_fn = [this](char *dest, size_t max_bytes, size_t min_bytes) {
      return this->pull(this->stream_idx, dest, max_bytes, min_bytes);
    };
  }
}

tracedoctor_t::~tracedoctor_t() = default;

void tracedoctor_t::init() {
  if (!traceEnabled) {
    write(mmio_addrs.traceEnable, 0);
    write(mmio_addrs.triggerSelector, 0);
    fprintf(stdout, "TraceDoctor@%d: collection disabled\n", info.tracerId);
  } else {
    write(mmio_addrs.traceEnable, 1);
    write(mmio_addrs.triggerSelector, traceTrigger);
    fprintf(stdout,
            "TraceDoctor@%d: trigger(%s), stream_depth(%d), token_width(%d), "
            "trace_width(%d)\n",
            info.tracerId, (traceTrigger == 0) ? "none" : "tracerv",
            stream_depth, info.tokenBits, info.traceBits);
    fprintf(stdout,
            "TraceDoctor@%d: buffer_depth(%u), buffer_grouping(%u), "
            "workers(%zu), threads(%d)\n",
            info.tracerId, engine->buffer_depth(), engine->buffer_grouping(),
            engine->num_workers(), engine->num_threads());
  }
  write(mmio_addrs.initDone, 1);
}

void tracedoctor_t::tick() {
  if (traceEnabled) {
    engine->drain(pull_fn, false);
  }
}

void tracedoctor_t::finish() {
  if (!traceEnabled) {
    return;
  }
  pull_flush(stream_idx);
  while (engine->drain(pull_fn, true))
    ;
  // The FireSim driver does not run bridge destructors at exit; tear the
  // engine down here so worker threads join and workers emit their outputs.
  engine.reset();
  traceEnabled = false;
}
