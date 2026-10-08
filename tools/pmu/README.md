# PMU integration tests

Chipyard-level tests for the [PMU library](../../generators/pmu/README.md) and its
BOOM v3 integration. The library's own unit tests run with `sbt pmu/test`.

Run these from the Chipyard root. Outputs go to `tools/pmu/target/`.

| Command | Checks |
|---|---|
| `python3 tools/pmu/test-platform-header.py` | The software platform-header generator: device matching by source, counter width from the device tree, and rejection of invalid inputs. |
| `python3 tools/pmu/test-boom.py` | Elaborates `DualSmallBoomV3PMUConfig`, `MediumBoomV3PMUConfig` and the default `SmallBoomV3Config` through the Chipyard generator. Checks each tile's `<config>.boom_tile_N.pmu.json` (19 events in name order), the tile PMU device-tree nodes and register maps, and that the default configuration has no PMU. Run `sbt chipyard/compile` first. |
| `python3 tools/pmu/test-software.py` | Builds the `SmallBoomV3PMUConfig` Verilator simulator, generates the platform header from its event list and device tree, and runs Baremetal-IDE's `tile-pmu-test` on it. `--seed` sets the Verilator initialization seed (default 1); `--skip-simulator-build` reuses an existing simulator. |
