# Native TilePMU software

`pmu_mmio.h` defines the v1 register ABI. `tile_pmu.h` adds ordered RISC-V
MMIO accessors, enable/disable, command writes, and stable split counter reads.
Reads return the captured bank, not live counters. A single software owner must
serialize snapshots; `STATUS.snapshot_valid` is sticky, not a completion toggle.

## Platform metadata

Use the **same elaboration's** finalized `<name>.pmu.json` event catalog and
`<name>.json` Chipyard JSON device tree. The header generator selects a source,
matches its `pmu,source` MMIO device, and emits:

- `TILE_PMU_BASE`, source, counter count, and width;
- `TILE_PMU_EVENT_*` symbolic IDs;
- `TILE_PMU_EVENTS(X)`, with `(symbol, id, key, unit)` for every counter.

```sh
python3 generators/pmu/software/generate_platform_header.py \
  --manifest /path/to/design.pmu.json --platform /path/to/design.json \
  --source boom_tile_0 --output /path/to/include/tile_pmu_platform.h
```

The JSON device tree supplies the address; the PMU manifest supplies IDs and
semantics. The current generator handles Chipyard's directly mapped `soc` bus
and native 64-bit v1 bank. It rejects missing/ambiguous sources, provisional
catalogs, invalid IDs, and C-name collisions. Runtime INFO validates version,
count, and width; it cannot detect a same-size catalog from a different build.
Keep both metadata files with the matching hardware artifact.

An application can derive both reads and key/value output without another map:

```c
#include <inttypes.h>
#include <stdio.h>
#include "tile_pmu_platform.h"

uint64_t values[TILE_PMU_COUNTER_COUNT];
tile_pmu_command(TILE_PMU_BASE, PMU_COMMAND_SNAPSHOT);
#define READ(symbol, id, key, unit) \
  values[id] = tile_pmu_read_counter(TILE_PMU_BASE, id);
TILE_PMU_EVENTS(READ)
#undef READ
#define PRINT(symbol, id, key, unit) \
  printf("%s=%" PRIu64 " %s\n", key, values[id], unit);
TILE_PMU_EVENTS(PRINT)
#undef PRINT
```

Counting starts disabled. Clear while disabled, enable around the workload,
then disable and snapshot for a fixed window. MMIO control and software readout
instructions affect a running measurement; the counter window is not identical
to two `minstret` reads around the workload.

## Baremetal-IDE integration

The optional `platform/tile_pmu.cmake` attaches generated metadata to the selected
Baremetal-IDE `chip-config` target. Pass `TILE_PMU_MANIFEST` and
`TILE_PMU_PLATFORM_JSON` to CMake, together with `TILE_PMU_SOFTWARE_DIR` pointing
to this directory. `TILE_PMU_SOURCE` defaults to `boom_tile_0`. Baremetal-IDE does
not assume a surrounding Chipyard checkout; the Chipyard test runner supplies
all three paths explicitly. Other platforms/examples continue to build without
supplying PMU metadata.

The ownership is intentional: this package owns the PMU ABI/accessors and JSON
schema-to-C generator; Baremetal-IDE owns the platform attachment and target test;
`tools/pmu/test-software.py` owns building and running the Chipyard simulator.
Generated headers belong in the selected CMake build directory, alongside the
ELF. They are build outputs, not a second source of platform metadata.

From the Chipyard root:

```sh
python3 tools/pmu/test-platform-header.py
python3 tools/pmu/test-software.py
```

The second command builds `SmallBoomV3PMUConfig`, generates its platform header,
builds `tile-pmu-test.elf` using Baremetal-IDE's default HTIF platform, and runs
it on Verilator with DRAMSim. Logs, ELF, and header are retained in
`tools/pmu/target/software/`. `--skip-simulator-build` reuses an existing simulator
and its matching metadata; the default builds/checks simulator prerequisites.
The runner uses initialization seed 1 by default (`--seed` overrides it) and
the local conda/RISC-V toolchain and overrides stale Verilator
installation paths without editing the environment.
