// See LICENSE for license details
// Ported from firesim/firesim#1501 (TraceDoctor, EECS-NTNU).

#ifndef __TRACEDOCTOR_REGISTER_H_
#define __TRACEDOCTOR_REGISTER_H_

#include <functional>
#include <map>
#include <memory>
#include <string>

#include "tracedoctor_oracle.h"
#include "tracedoctor_worker.h"

#define REGISTER_TRACEDOCTOR_WORKER(__name, __class)                          \
  {                                                                            \
    __name, [](std::vector<std::string> const &args,                          \
               struct traceInfo const &info) {                                \
      return std::unique_ptr<tracedoctor_worker>(new __class(args, info));    \
    }                                                                          \
  }

typedef std::map<std::string,
                 std::function<std::unique_ptr<tracedoctor_worker>(
                     std::vector<std::string> const &,
                     struct traceInfo const &)>>
    tracedoctor_register_t;

// The worker registry. Add an entry here to register a new worker.
static tracedoctor_register_t const tracedoctor_register = {
    REGISTER_TRACEDOCTOR_WORKER("dummy", tracedoctor_dummy),
    REGISTER_TRACEDOCTOR_WORKER("filer", tracedoctor_filer),
    REGISTER_TRACEDOCTOR_WORKER("insttrace", tracedoctor_insttrace),
    REGISTER_TRACEDOCTOR_WORKER("oracle", tracedoctor_oracle),
};

#endif // __TRACEDOCTOR_REGISTER_H_
