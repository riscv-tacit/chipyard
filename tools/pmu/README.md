# PMU compiler checks and native prototype

This project depends on `pmu` and the Chisel 6 → FIRRTL2 bridge. It has no MIDAS
or Rocket Chip dependency. The library and catalog live in
[`generators/pmu`](../../generators/pmu/README.md).

## Supported native flow

Given the annotations returned by Chisel's `Elaborate` and `Convert` phases:

```scala
val raw = PMUCompiler.fromChisel(chiselAnnotations)
val checked = PMUCompiler.validate(raw)
val manifests = PMUCompiler.manifests(checked)
val verilog = PMUCompiler.emitNative(checked)
```

`fromChisel` translates scope/event/consumer annotations explicitly. The generic FIRRTL2
bridge does not know these annotations. `validate` runs through middle FIRRTL,
including width inference, reset inference, module deduplication, and conditional
expansion. It applies every pass's annotation renames using `runTransform`.
The checks therefore run **before `LegalizeConnects` inserts input truncation**.

Validation:

- Rejects duplicate scope identities or missing/duplicate event annotations.
- Resolves source widths, rejects zero/oversized/unresolved inputs, and returns
  finalized catalogs in registration order.
- Checks owner/producer domains through instance-specific wiring. Direct aliases
  and one-bit casts are accepted, including flipped leaves in Diplomacy bundles.
  Registers, generated clocks, and blackbox outputs are opaque domain sources:
  producer and owner must share the exact source. Additional producer-local
  gating, division, or reset delay fails; no clock-domain crossing is inferred.
- Checks consumer count, per-event wiring identity, exact widths, and clock/reset.
  Intermediate truncation or extension is not accepted as a transparent wire.
- Preserves absolute instance targets through deduplication and JSON round trips.
  Deleted or replicated targets fail explicitly instead of silently selecting one.

The checker verifies declared domains, not arbitrary functional CDC inside an
increment expression. `emitNative` requires finalized manifests and consumes
PMU annotations before optimization removes their source wires.

Standalone callers use this explicit entry point. Chipyard's Chisel 6
`ValidatePMU` generation phase also invokes it automatically when PMU annotations
are present, analyzing a copy of the circuit and exporting `<name>.pmu.json`.
It consumes PMU annotations from the exported annotation JSON before downstream
native compilation. Chisel's emitted `.fir` can still contain embedded descriptors;
the standard Chipyard firtool flags ignore unknown annotations. The existing
CIRCT/legacy target-lowering paths otherwise remain intact.

After validation, the phase finalizes registered metadata consumers before
exporting bridge annotations. The [interval bridge](../../generators/firechip/bridgestubs/README.interval-trace.md)
uses this to supply its resolved constructor key to Golden Gate. Chisel 7 PMU
generation is rejected until its validation path is implemented.
Definition/Instance hierarchy drilling has not been validated.

## Run checks

From the repository root, using this checkout's cached build and conda tools:

```sh
python3 tools/pmu/run-tests.py
```

The runner compiles the actual schema, library, compiler, and prototype sources,
then executes the checks and simulates emitted RTL with Verilator. It needs the
cached `firrtl2_bridge` and `cde` artifacts, Scala 2.13.16, and Chisel 6.7.0. It
reports missing dependencies rather than downloading them. Outputs go to the
ignored `tools/pmu/target/prototype/` directory, including manifests, annotations,
middle FIRRTL, and RTL.

Build projects are also available through sbt:

```sh
sbt 'pmu/compile' 'pmu_compiler/Test/compile'
sbt 'pmu_compiler/Test/runMain pmu.compiler.PMUPrototype tools/pmu/target/prototype'
```

`runMain` runs the Scala checks and emits RTL. Use the Python runner for the full
Verilator simulation; the prototype is an executable test, not a ScalaTest suite.

Coverage includes caller-side disablement without a clock or registry, failure
when an enabled caller has no registry, direct registry use without Parameters,
RawModule producers, nested producers, both overloads, clock/reset aliases, invalid registration
and domain cases, inferred oversized inputs, two independent scopes, deduplicated
owners, JSON serialization, and repeated elaboration in one JVM. Registry-only
coverage verifies that finalization creates no registers or native bank, keeps
individual increment widths, survives annotation serialization, and still rejects
oversized inputs, mismatched domains, late registration, and repeated build. The RTL oracle
checks independent enable/reset/clear, inclusive snapshots and atomic capture+clear,
simultaneous nonzero increments, wrapping small
counters, the default 64-bit bank, and no accumulation without a clock edge.

Range inference remains separate work. The native prototype does not establish
FireSim token or boundary semantics; the interval bridge has its own RTL tests.

## Verified result (2026-09-16)

The standalone prototype now has **41 Scala/compiler checks**, including a
Diplomacy-style flipped domain bundle with a shared generated owner reset.
The native RTL oracle covers **4,096 Verilator cycles**. The runner also compiles
BOOM's parameter-independent `PMUEventLogic` source and the harness under
`boom-tests/`, then checks its emitted RTL: directed corner cases and **8,192
randomized dispatch/ROB observations** (16,399 assertions total). This verifies
partial groups, simultaneous causes, control-stall exclusions, ROB head holes,
and miss-allocation qualification. The PMU library/compiler itself still has no
BOOM or Rocket Chip dependency. The additional BOOM harness runs through the
Python runner; the sbt prototype entry point runs the standalone checks only.
The native projects also compile through the root sbt build, and the executable
Scala prototype runs there. The inferred PopCount input resolves to 3 bits; a
separate oversized `WireInit` input is rejected before bank-input truncation.
Two identical producer hierarchies retain distinct scope/event instances after
deduplication. Native reset/clear and wrapping agree with the RTL oracle.

## BOOM integration check

After `sbt 'chipyard/compile'`, run:

```sh
python3 tools/pmu/test-boom.py
```

This elaborates `DualSmallBoomV3PMUConfig`, the two-wide
`MediumBoomV3PMUConfig`, and the unmodified/default `SmallBoomV3Config` through
the actual Chipyard generator in separate JVMs. It checks per-tile finalized
manifests, all 19 counter names, units, descriptions, and widths,
native bank instances, exported annotation consumption, and absence of banks and
manifests when disabled. Artifacts and logs go under `tools/pmu/target/boom/`.
This is a real BOOM elaboration check, not a software workload simulation.

Verified BOOM result: the enabled one-wide/two-tile and two-wide configurations,
and the default disabled configuration pass the integration check. Each enabled
tile has 19 native 64-bit counters. Event increments are one bit except dispatch
and retirement, which resolve to two bits in the two-wide configuration. CIRCT
firtool emits final Verilog for the dual-tile design with pass verification
enabled; each native bank now has 19 running and 19 captured 64-bit registers.
These elaboration checks are separate from the software workload check below.
The interval bridge separately checks event-only and native-bank coexistence configurations.

## Native MMIO check

After `sbt 'chipyard/compile'`, run `python3 tools/pmu/test-mmio.py`. It compiles
`mmio-tests/PMUMMIOTest.scala` against the actual `pmu_tilelink` adapter, emits
validated RTL, and drives real TileLink A/D requests on both 32- and 64-bit buses.
The independent C++ oracle checks 2,829 cycles, including byte masks, reserved and
read-only registers, request/response backpressure, queued commands, enable/reset,
inclusive capture-and-clear, stable split reads, and 64-bit wrapping. Artifacts
are in `tools/pmu/target/mmio/`. See the [MMIO ABI](../../reference-notes/boom-pmu-mmio.md).

## Full-core native software check

Run `python3 tools/pmu/test-software.py` from the repository root. It builds
`SmallBoomV3PMUConfig`, derives platform bindings from its `.pmu.json` and JSON
device tree, builds Baremetal-IDE's `tile-pmu-test.elf`, and runs it on Verilator
with DRAMSim and initialization seed 1 (`--seed` overrides it). Use
`--skip-simulator-build` only when retaining the matching simulator and metadata.
All outputs are under `tools/pmu/target/software/`. See the
[software interface and X-macro example](../../generators/pmu/software/README.md).
`python3 tools/pmu/test-platform-header.py` checks source/address selection and
rejection of malformed or ambiguous bindings.

Verified 2026-09-21: **207 target-side checks pass** on the one-wide BOOM, with
normal simulator seeds 1 and 2; a waveform-enabled run also passed. The fixed
1,024-iteration loop retires exactly 4,097 instructions between CSR reads and
4,107 in the wider MMIO-controlled PMU window. The test checks all 19 captured
counters for initial zero, disabled counting, stable snapshots while running,
freeze while disabled, capture-before-clear, and clear retaining the previous
snapshot. It compares RV64 loads with split 32-bit reads and prints manifest
keys, values, and units. Retirement and cycles advance; the memory workload
produces data-cache misses. Eight platform-generator checks also pass.

An initial normal run without an explicit initialization seed hit BOOM's
`Pipeline has hung` assertion before software output (and reproduced once).
Explicit seeds 1/2 and the debug run pass; the unseeded failure has **not** been
root-caused or fixed. Its log is retained as `unseeded-hang.log`. The explicit
seed makes this test reproducible; it is not a claim to fix the boot issue.
These results do not cover every event predicate, multi-tile/superscalar software
execution, long-run 64-bit wrap, or end-to-end FireSim execution.
