// See LICENSE for license details
// Ported from firesim/firesim#1501 (TraceDoctor, EECS-NTNU) and modernized
// for the current bridge driver layout. This file is simulator-independent:
// workers consume raw token buffers and know nothing about DMA or MMIO.

#ifndef __TRACEDOCTOR_WORKER_H_
#define __TRACEDOCTOR_WORKER_H_

#include <cstdint>
#include <cstdio>
#include <map>
#include <string>
#include <tuple>
#include <vector>

struct traceInfo {
  unsigned int tracerId;
  unsigned int tokenBits;  // width of one DMA stream beat
  unsigned int tokenBytes;
  unsigned int traceBits;  // width of the target-side trace payload
  unsigned int traceBytes;
};

void strReplaceAll(std::string &str,
                   std::string const &from,
                   std::string const &to);

std::vector<std::string> strSplit(std::string const &str,
                                  std::string const &sep);

enum fileRegisterFields { freg_name = 0, freg_descriptor = 1, freg_file = 2 };

#define TDWORKER_NO_FILES 0

class tracedoctor_worker {
protected:
  std::string const name;
  std::string const tracerName;
  struct traceInfo const info;
  // (fileName, descriptor, isPlainFile). isPlainFile == false means the
  // descriptor is a popen'd compression pipe and must be pclose'd.
  std::vector<std::tuple<std::string, FILE *, bool>> fileRegister;

  FILE *openFile(std::string const &fileName,
                 std::string const &compressionCmd = "",
                 unsigned int compressionLevel = 1,
                 unsigned int compressionThreads = 1);
  void closeFile(FILE *fileDescriptor);
  void closeFiles();

public:
  tracedoctor_worker(std::string const &name,
                     std::vector<std::string> const &args,
                     struct traceInfo const &info,
                     int requiredFiles = TDWORKER_NO_FILES);
  virtual void tick(char const *data, unsigned int tokens);
  virtual ~tracedoctor_worker();
};

class tracedoctor_dummy : public tracedoctor_worker {
public:
  tracedoctor_dummy(std::vector<std::string> const &args,
                    struct traceInfo const &info);
};

class tracedoctor_filer : public tracedoctor_worker {
private:
  uint64_t byteCount;
  bool raw;

public:
  tracedoctor_filer(std::vector<std::string> const &args,
                    struct traceInfo const &info);
  ~tracedoctor_filer() override;
  void tick(char const *data, unsigned int tokens) override;
};

#endif // __TRACEDOCTOR_WORKER_H_
