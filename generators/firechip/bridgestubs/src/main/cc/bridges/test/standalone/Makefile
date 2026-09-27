# See LICENSE for license details
# Standalone (no simulator) unit tests for the TraceDoctor engine + workers.
#
#   make        build and run
#   make build  build only

BRIDGES_DIR := ../..

CXX ?= g++
CXXFLAGS ?= -std=c++17 -O2 -g -Wall -pthread
CXXFLAGS += -I$(BRIDGES_DIR)

SRCS := tracedoctor_engine_test.cc \
        $(BRIDGES_DIR)/tracedoctor_engine.cc \
        $(BRIDGES_DIR)/tracedoctor_worker.cc \
        $(BRIDGES_DIR)/tracedoctor_oracle.cc

HDRS := $(wildcard $(BRIDGES_DIR)/tracedoctor*.h)

tracedoctor_engine_test: $(SRCS) $(HDRS)
	$(CXX) $(CXXFLAGS) -o $@ $(SRCS)

intervaltrace_decoder_test: intervaltrace_decoder_test.cc $(BRIDGES_DIR)/intervaltrace_decoder.h
	$(CXX) $(CXXFLAGS) -o $@ $<

.PHONY: run build clean interval-test
interval-test: intervaltrace_decoder_test
	./intervaltrace_decoder_test
run: tracedoctor_engine_test
	./tracedoctor_engine_test

build: tracedoctor_engine_test

clean:
	rm -f tracedoctor_engine_test intervaltrace_decoder_test

.DEFAULT_GOAL := run
