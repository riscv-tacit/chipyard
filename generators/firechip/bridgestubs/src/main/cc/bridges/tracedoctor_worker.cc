// See LICENSE for license details
// Ported from firesim/firesim#1501 (TraceDoctor, EECS-NTNU).

#include "tracedoctor_worker.h"

#include <cassert>
#include <stdexcept>

void strReplaceAll(std::string &str,
                   std::string const &from,
                   std::string const &to) {
  if (from.empty())
    return;
  size_t start_pos = 0;
  while ((start_pos = str.find(from, start_pos)) != std::string::npos) {
    str.replace(start_pos, from.length(), to);
    start_pos += to.length();
  }
}

std::vector<std::string> strSplit(std::string const &str,
                                  std::string const &sep) {
  std::vector<std::string> arr;
  size_t pos = 0;
  while (pos < str.size()) {
    size_t next = str.find_first_of(sep, pos);
    if (next == std::string::npos) {
      arr.push_back(str.substr(pos));
      break;
    }
    if (next > pos) {
      arr.push_back(str.substr(pos, next - pos));
    }
    pos = next + 1;
  }
  return arr;
}

tracedoctor_worker::tracedoctor_worker(std::string const &name,
                                       std::vector<std::string> const &args,
                                       struct traceInfo const &info,
                                       int const requiredFiles)
    : name(name), tracerName(name + "@" + std::to_string(info.tracerId)),
      info(info) {
  if (requiredFiles != TDWORKER_NO_FILES) {
    unsigned int localRequiredFiles = requiredFiles;
    std::string compressionCmd = "";
    unsigned int compressionLevel = 1;
    unsigned int compressionThreads = 1;

    std::vector<std::string> filesToOpen;

    for (auto &a : args) {
      std::vector<std::string> c = strSplit(a, ":");
      if (c[0].compare("file") == 0 && c.size() > 1 && localRequiredFiles > 0) {
        filesToOpen.push_back(c[1]);
        localRequiredFiles--;
      } else if (c[0].compare("compressionThreads") == 0 && c.size() > 1) {
        compressionThreads = std::stoul(c[1], nullptr, 0);
      } else if (c[0].compare("compressionLevel") == 0 && c.size() > 1) {
        compressionLevel = std::stoul(c[1], nullptr, 0);
      } else if (c[0].compare("compressionCmd") == 0 && c.size() > 1) {
        compressionCmd = c[1];
      }
    }

    if (localRequiredFiles != 0) {
      throw std::invalid_argument(
          "TraceDoctor Worker " + tracerName + " requires " +
          std::to_string(requiredFiles) +
          " file(s), provide via e.g. 'file:output.csv'");
    }

    for (auto &a : filesToOpen) {
      openFile(a, compressionCmd, compressionLevel, compressionThreads);
    }
  }
}

void tracedoctor_worker::tick(char const *const data, unsigned int tokens) {
  (void)data;
  (void)tokens;
}

FILE *tracedoctor_worker::openFile(std::string const &fileName,
                                   std::string const &compressionCmd,
                                   unsigned int const compressionLevel,
                                   unsigned int const compressionThreads) {
  std::string localFileName = fileName;
  std::string localCompressionCmd = compressionCmd;
  strReplaceAll(localFileName, std::string("%id"),
                std::to_string(info.tracerId));

  FILE *fileDescriptor;

  // (extension -> (command, supports -T thread count))
  std::map<std::string, std::pair<std::string, bool>> const compressionMap = {
      {".gz", {"gzip", false}},
      {".bz2", {"bzip2", false}},
      {".xz", {"xz -T", true}},
      {".zst", {"zstd -T", true}},
  };

  if (localCompressionCmd.empty()) {
    for (const auto &c : compressionMap) {
      if (c.first.size() <= localFileName.size() &&
          std::equal(c.first.rbegin(), c.first.rend(),
                     localFileName.rbegin())) {
        localCompressionCmd = c.second.first;
        if (c.second.second)
          localCompressionCmd += std::to_string(compressionThreads);
        localCompressionCmd += std::string(" -") +
                               std::to_string(compressionLevel) +
                               std::string(" - >") + localFileName;
        break;
      }
    }
  } else {
    localCompressionCmd += std::string(" - >") + localFileName;
  }

  if (!localCompressionCmd.empty()) {
    fileDescriptor = popen(localCompressionCmd.c_str(), "w");
    if (fileDescriptor == NULL)
      throw std::invalid_argument("Could not execute " + localCompressionCmd);
    fileRegister.emplace_back(localFileName, fileDescriptor, false);
  } else {
    fileDescriptor = fopen(localFileName.c_str(), "w");
    if (fileDescriptor == NULL)
      throw std::invalid_argument("Could not open " + localFileName);
    fileRegister.emplace_back(localFileName, fileDescriptor, true);
  }

  return fileDescriptor;
}

void tracedoctor_worker::closeFile(FILE *const fileDescriptor) {
  auto it = fileRegister.begin();
  while (it != fileRegister.end()) {
    if (std::get<freg_descriptor>(*it) == fileDescriptor) {
      if (std::get<freg_file>(*it)) {
        fclose(std::get<freg_descriptor>(*it));
      } else {
        pclose(std::get<freg_descriptor>(*it));
      }
      it = fileRegister.erase(it);
      break;
    }
    ++it;
  }
}

void tracedoctor_worker::closeFiles() {
  auto it = fileRegister.begin();
  while (it != fileRegister.end()) {
    if (std::get<freg_file>(*it)) {
      fclose(std::get<freg_descriptor>(*it));
    } else {
      pclose(std::get<freg_descriptor>(*it));
    }
    it = fileRegister.erase(it);
  }
}

tracedoctor_worker::~tracedoctor_worker() { closeFiles(); }

tracedoctor_dummy::tracedoctor_dummy(std::vector<std::string> const &args,
                                     struct traceInfo const &info)
    : tracedoctor_worker("Dummy", args, info, TDWORKER_NO_FILES){};

tracedoctor_filer::tracedoctor_filer(std::vector<std::string> const &args,
                                     struct traceInfo const &info)
    : tracedoctor_worker("Filer", args, info, 1), byteCount(0), raw(false) {
  for (auto &a : args) {
    if (a == "raw") {
      raw = true;
      continue;
    }
  }

  if (info.traceBytes == info.tokenBytes) {
    raw = true;
  }

  // Non-raw mode strips per-token padding and hence only works when a
  // trace fits into a single token.
  assert(raw || info.traceBytes <= info.tokenBytes);

  fprintf(stdout, "%s: file(%s), raw(%d)\n", tracerName.c_str(),
          std::get<freg_name>(fileRegister[0]).c_str(), raw);
}

tracedoctor_filer::~tracedoctor_filer() {
  fprintf(stdout, "%s: file(%s), bytes_stored(%ld)\n", tracerName.c_str(),
          std::get<freg_name>(fileRegister[0]).c_str(), byteCount);
}

void tracedoctor_filer::tick(char const *const data, unsigned int tokens) {
  if (raw) {
    fwrite(data, 1, (size_t)tokens * info.tokenBytes,
           std::get<freg_descriptor>(fileRegister[0]));
    byteCount += (size_t)tokens * info.tokenBytes;
  } else {
    for (unsigned int i = 0; i < tokens; i++) {
      fwrite(&data[(size_t)i * info.tokenBytes], 1, info.traceBytes,
             std::get<freg_descriptor>(fileRegister[0]));
    }
    byteCount += (size_t)tokens * info.traceBytes;
  }
}
