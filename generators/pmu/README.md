# Standalone PMU

Initial native implementation of [PMU API v1](../../reference-notes/boom-pmu-api.md).
Each registration supplies one local `UInt` wire carrying the complete increment.
The producer computes any OR, PopCount, or sum.

## Ownership

- `schema/`: immutable catalog and versioned JSON; Scala and ujson only.
- [`PMU.scala`](src/main/scala/pmu/PMU.scala): scoped registration, finalization,
  and width-preserving event routing, with no counters.
- [`TilePMU.scala`](src/main/scala/pmu/TilePMU.scala): optional native counter bank
  and its `PMUAttachment` controls/outputs.
- `src/main/scala/pmu/PMUAnnotations.scala`: backend-neutral scope/event annotations.
  The library depends on Chisel, CDE, and the schema.
- [`../../tools/pmu/`](../../tools/pmu/README.md): FIRRTL2 annotation adapter,
  mandatory native checks, and the executable prototype.

Rocket Chip has no dependency on these projects. BOOM v3 creates its scope before
constructing children when `WithBoomPMUEvents` is enabled. `WithBoomPMU` also
instantiates the native bank and MMIO adapter in `BoomTileModuleImp`. The optional `tilelink/` adapter depends on Rocket Chip
and PMU; Rocket Chip has no reverse dependency. See the
[MMIO ABI](../../reference-notes/boom-pmu-mmio.md).

## Example: one registry per tile

The integrator owns the registry's lifetime: allocate one per tile construction,
supply it through that tile's parameters, and call `build()` after all producers
exist.
This example transports the registry through `Parameters`; passing the registry
directly is equally valid. Producers explicitly obtain it and call `register`.

```scala
import chisel3._
import org.chipsalliance.cde.config.Parameters
import pmu._

class ExampleROB(implicit p: Parameters) extends Module {
  val completed = IO(Input(UInt(3.W)))
  val increment = WireInit(completed)
  val registry = p(PMUScopeKey).get
  registry.register("rob.completed", increment, unit = "operations")
}

class ExampleTile()(implicit p: Parameters) extends Module {
  val completed = IO(Input(UInt(3.W)))
  val clear = IO(Input(Bool()))
  val count = IO(Output(UInt(64.W)))

  // Scala supplies this tile's existing p; there is no registry argument.
  val rob = Module(new ExampleROB)
  rob.completed := completed

  // All producers have now been constructed. This example requires an enabled scope.
  val events = p(PMUScopeKey).get.build()
  val attachment = TilePMU.build(events)
  attachment.enable := true.B // This direct-hardware example counts continuously.
  attachment.clear := clear
  attachment.snapshot := false.B
  count := attachment.values(0)
}

class ExampleSystem extends Module {
  val completed = IO(Input(Vec(2, UInt(3.W))))
  val clear = IO(Input(Vec(2, Bool())))
  val counts = IO(Output(Vec(2, UInt(64.W))))
  val parentP = Parameters.empty

  for (id <- 0 until 2) {
    // Allocate BEFORE constructing this tile. Each iteration gets a fresh registry.
    val registry = new PMURegistry(s"tile_$id")
    val tileP = parentP.alterPartial {
      case PMUScopeKey => Some(registry)
    }
    val tile = Module(new ExampleTile()(tileP))
    tile.completed := completed(id)
    tile.clear := clear(id)
    counts(id) := tile.count
  }
}
```

Every producer within a tile must resolve `PMUScopeKey` to the same registry.
Children can derive parameter views for other settings while preserving that key.
The next tile, and the next elaboration, get fresh registries. Allocate outside
the lookup case body: CDE evaluates that body on every lookup, so putting
`new PMURegistry(...)` inside it would create a different registry each time.

For a Diplomacy tile, bind the scope before the tile's constructor runs, including
its superclass constructors. Call `build()` at the end of its hardware
implementation body. `InModuleBody` callbacks run before the subclass body and
are too early for scopes with ordinary children constructed there.

The library checks duplicate names, late registration, and repeated `build()` calls.
Supplying the intended scope and calling `build()` are the integrator's
responsibility; these checks do not detect an omitted `build()` call or a producer given
the wrong registry. An enabled caller uses `.get` so a missing scope fails during
elaboration. Optional instrumentation must be guarded by the caller:

```scala
if (p(EnableBoomPMUEvents)) {
  val registry = p(PMUScopeKey).get
  val retired = WireInit(PopCount(io.commit.arch_valids))
  registry.register("rob.instructions_retired", retired, unit = "instructions")
}
```

`register` itself does not inspect `Parameters` or decide whether PMU is enabled.

## Finalization and optional native hardware

```scala
val events = registry.build(counterBits = 64) // Seal once; emit event annotations.
val nativePMU = TilePMU.build(events)         // Optional: instantiate native counters.
```

`build()` returns `PMUEvents`: an immutable catalog plus owner-local increment
wires and clock/reset handles. Each increment retains its own inferred width;
finalization does not create counters, snapshots, control inputs, or MMIO.
Omit `TilePMU.build(events)` for a registry-only design or a bridge consumer.
Mandatory compiler validation still checks the catalog, widths, and domains.
`counterBits` remains the catalog's accumulator-width contract (64 by default),
not the width of the routed event wires.

Call `TilePMU.build(events)` in the owner module after finalization. It uses the
clock/reset recorded in `events`, and does not register events or emit a second
catalog. `PMUEvents` contains elaboration-local Chisel handles; only its manifest
and compiler annotations are serialized for later consumers. Registry sealing
and compiler width finalization are separate: `manifest.finalized` becomes true
only in the compiler-produced manifest.

Hardware reset initializes running and captured counters to zero and invalidates
the snapshot. `enable` gates accumulation; synchronous clear zeros the running
bank. Snapshot includes the enabled increment on that edge, before clear.
Addition wraps at the configured width (64 by default).
The returned manifest is provisional: a `WireInit` width can remain unknown in
Scala even after compilation.

**Compile through the PMU adapter and validator.** An ordinary Chisel elaboration
alone cannot check inferred widths or parent/child clock aliases. See the compiler
README for the supported native path. Chipyard's Chisel 6 generator invokes these
checks automatically for enabled PMUs and writes `<name>.pmu.json`. Standalone
users must invoke the compiler adapter explicitly.

## BOOM v3 examples

Enable `boom.v3.common.WithBoomPMU` in a configuration, or use these Chipyard configs:

- `SmallBoomV3PMUConfig`: one small BOOM v3 tile.
- `DualSmallBoomV3PMUConfig`: two independently elaborated small BOOM v3 tiles.
- `MediumBoomV3PMUConfig`: one two-wide BOOM v3 tile.

Each tile registers the complete [initial catalog](../../reference-notes/boom-pmu-counter-catalog.md):
**19 native counters**, comprising cycles, architectural retirement, branch
recovery, I/D-cache line misses, I/D-side walker requests, dispatch throughput,
frontend/backend/recovery indicators, and resource/ROB stall details.

Cycle and event predicates contribute 0/1; retirement and dispatch contribute a
PopCount up to the configured core width. Resource stall counters observe the
first waiting lane, including partial dispatch, and can overlap. The catalog
records exact qualification and counting semantics.

The native counters are 64-bit cumulative counters. They are independent of the
CSR counters and their inhibition controls. Retirement is sampled directly at
ROB's commit edge; the existing CSR retirement input has an additional register.
`BoomTileModuleImp.nativePMU` holds the optional attachment. Each tile's MMIO
adapter controls enable/clear/snapshot and exposes coherent captured values.
Counting starts **disabled** after reset. The default region is
`0x40000000 + tileId * 0x1000`; use `WithBoomPMU(baseAddress = ...)` to relocate it.
The device tree and register-map JSON describe each region. Read the
[MMIO ABI](../../reference-notes/boom-pmu-mmio.md) for software sequencing and
[register constants](software/pmu_mmio.h). The
[software interface](software/README.md) provides ordered accessors and
manifest-derived Baremetal-IDE platform bindings. Run
`python3 tools/pmu/test-software.py` for the full-core native MMIO test.
`BoomTileModuleImp.pmuEvents` exposes raw events independently of the native bank.
The [interval bridge](../firechip/bridgestubs/README.interval-trace.md) routes them
alongside the existing architectural trace and accumulates its own FPGA-host bank.
Use `WithBoomPMUEvents` alone when only that event surface is needed.

For example, with the usual Chipyard tool environment, from the repository root:

```sh
sbt 'chipyard/runMain chipyard.Generator --target-dir tools/pmu/target/example --name boom-pmu --top-module chipyard.harness.TestHarness --legacy-configs chipyard:SmallBoomV3PMUConfig'
```

The generated `boom-pmu.pmu.json` maps event IDs to names and resolved increment
widths. Sources use each tile's `uniqueName` (normally `boom_tile_0`, etc.). Existing
configs keep PMU disabled by default. This example supports the Chisel 6 build;
Chisel 7 PMU generation and `WithCloneBoomTiles` with PMU enabled are rejected.

## Bridge metadata

`PMUConsumerAnnotation` lets the compiler check the raw event wiring of a consumer.
`PMUMetadataConsumerAnnotation` supports deferred metadata finalization without
introducing a FireSim dependency into the PMU library. The interval bridge uses
this hook to publish its concrete key after width inference and domain checks.
The previous range-analysis experiment remains in `tools/pmu-range-experiment`;
this library does not finalize ranges or packed trace layouts.
