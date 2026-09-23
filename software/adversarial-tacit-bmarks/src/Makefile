CC   = riscv64-unknown-elf-gcc
DUMP = riscv64-unknown-elf-objdump
CFLAGS  = -std=gnu99 -O2 -Wall -Wextra -fno-common -fno-builtin-printf \
          -march=rv64imafd -mabi=lp64d -mcmodel=medany -mno-relax \
          -specs=htif_nano.specs -Icommon
LDFLAGS = -static

FAMILIES = branch-dense indirect-dense mem-burst jr-diag jr-diag2 indirect-table
ELFS = $(addprefix build/,$(addsuffix .elf,$(FAMILIES)))

REPORT_ELFS = build/report-branch.elf build/report-indirect.elf build/report-mem.elf
all: $(ELFS) $(REPORT_ELFS)

# final report ELFs reuse the family kernel files
build/report-branch.elf: report-branch.c branch-dense-kernels.S common/harness.c common/harness.h common/tacit.h
	@mkdir -p build
	$(CC) $(CFLAGS) $(LDFLAGS) -o $@ report-branch.c branch-dense-kernels.S common/harness.c
build/report-indirect.elf: report-indirect.c indirect-table-kernels.S indirect-dense-kernels.S common/harness.c common/harness.h common/tacit.h
	@mkdir -p build
	$(CC) $(CFLAGS) $(LDFLAGS) -o $@ report-indirect.c indirect-table-kernels.S indirect-dense-kernels.S common/harness.c
build/report-mem.elf: report-mem.c mem-burst-kernels.S common/harness.c common/harness.h common/tacit.h
	@mkdir -p build
	$(CC) $(CFLAGS) $(LDFLAGS) -o $@ report-mem.c mem-burst-kernels.S common/harness.c

# FPGA-sized jr diagnostic: same rounds as indirect-dense (1M jumps per run)
build/jr-diag.elf: CFLAGS += -DROUNDS=65536

build/%.elf: %.c %-kernels.S common/harness.c common/harness.h common/tacit.h
	@mkdir -p build
	$(CC) $(CFLAGS) $(LDFLAGS) -o $@ $*.c $*-kernels.S common/harness.c

# RTL-simulation build of the jr diagnostic: short, 1 rep, DMA buffer placed
# inside the simulated DRAM (override RTL_DMA_ADDRESS / RTL_ROUNDS as needed)
RTL_DMA_ADDRESS ?= 0x88000000
RTL_DMA_MAX     ?= 0x04000000
RTL_ROUNDS      ?= 1024
RTL2_ROUNDS     ?= 8192
rtl: build-rtl/jr-diag.elf build-rtl/jr-rtl.elf build-rtl/jr-rtl-bp.elf build-rtl/jr-rings.elf build-rtl/nt-rtl.elf build-rtl/tjr-rtl.elf
build-rtl/tjr-rtl.elf: tjr-rtl.c indirect-table-kernels.S common/harness.c common/harness.h common/tacit.h
	@mkdir -p build-rtl
	$(CC) $(CFLAGS) $(LDFLAGS) -DREPS=1 -DDMA_ADDRESS=$(RTL_DMA_ADDRESS)ULL -DDMA_MAX_SIZE=$(RTL_DMA_MAX)ULL -o $@ tjr-rtl.c indirect-table-kernels.S common/harness.c
build-rtl/nt-rtl.elf: nt-rtl.c branch-dense-kernels.S common/harness.c common/harness.h common/tacit.h
	@mkdir -p build-rtl
	$(CC) $(CFLAGS) $(LDFLAGS) -DREPS=1 -DDMA_ADDRESS=$(RTL_DMA_ADDRESS)ULL -DDMA_MAX_SIZE=$(RTL_DMA_MAX)ULL -o $@ nt-rtl.c branch-dense-kernels.S common/harness.c
build-rtl/jr-rings.elf: jr-rings.c jr-rings-kernels.S common/harness.c common/harness.h common/tacit.h
	@mkdir -p build-rtl
	$(CC) $(CFLAGS) $(LDFLAGS) -DREPS=1 -DDMA_ADDRESS=$(RTL_DMA_ADDRESS)ULL -DDMA_MAX_SIZE=$(RTL_DMA_MAX)ULL -o $@ jr-rings.c jr-rings-kernels.S common/harness.c
build-rtl/jr-rtl-bp.elf: jr-rtl-bp.c jr-diag2-kernels.S common/harness.c common/harness.h common/tacit.h
	@mkdir -p build-rtl
	$(CC) $(CFLAGS) $(LDFLAGS) -DREPS=1 -DROUNDS=$(RTL2_ROUNDS) -DDMA_ADDRESS=$(RTL_DMA_ADDRESS)ULL -DDMA_MAX_SIZE=$(RTL_DMA_MAX)ULL -o $@ jr-rtl-bp.c jr-diag2-kernels.S common/harness.c
build-rtl/jr-rtl.elf: jr-rtl.c jr-diag2-kernels.S common/harness.c common/harness.h common/tacit.h
	@mkdir -p build-rtl
	$(CC) $(CFLAGS) $(LDFLAGS) -DREPS=1 -DROUNDS=$(RTL2_ROUNDS) -DDMA_ADDRESS=$(RTL_DMA_ADDRESS)ULL -DDMA_MAX_SIZE=$(RTL_DMA_MAX)ULL -o $@ jr-rtl.c jr-diag2-kernels.S common/harness.c
build-rtl/jr-diag.elf: jr-diag.c jr-diag-kernels.S common/harness.c common/harness.h common/tacit.h
	@mkdir -p build-rtl
	$(CC) $(CFLAGS) $(LDFLAGS) -DREPS=1 -DROUNDS=$(RTL_ROUNDS) -DDMA_ADDRESS=$(RTL_DMA_ADDRESS)ULL -DDMA_MAX_SIZE=$(RTL_DMA_MAX)ULL -o $@ jr-diag.c jr-diag-kernels.S common/harness.c

# functional check on plain spike: MMIO stubbed out, cycle numbers meaningless
SIM_ELFS = $(addprefix build-sim/,$(addsuffix .elf,$(FAMILIES)))
sim: $(SIM_ELFS)
build-sim/%.elf: %.c %-kernels.S common/harness.c common/harness.h common/tacit.h
	@mkdir -p build-sim
	$(CC) $(CFLAGS) $(LDFLAGS) -DHARNESS_NO_MMIO -DREPS=1 -o $@ $*.c $*-kernels.S common/harness.c

dump: $(ELFS)
	for e in $(ELFS); do $(DUMP) -d $$e > $${e%.elf}.dump; done

clean:
	rm -rf build build-sim build-rtl

.PHONY: all sim rtl dump clean
