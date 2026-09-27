# PMU API v1

Date: 2026-09-15\
Status: standalone native library and FIRRTL2 validation prototype implemented in
[generators/pmu](../generators/pmu/README.md) and [tools/pmu](../tools/pmu/README.md).
The BOOM v3 native counter catalog and Chipyard Chisel 6 validation are
integrated. Legacy FIRRTL annotation transport for bridges and bridge construction
remain next steps.\
Related: [registration plan](boom-pmu-registration-plan.md),
[counter catalog and planned packing policy](boom-pmu-counter-catalog.md),
[range experiment](boom-pmu-range-experiment.md),
[trace contract](boom-pmu-sim-trace-plan.md), [native MMIO ABI](boom-pmu-mmio.md).

## Architecture

One producer registration can feed either or both independent consumers:

```text
producer increment wires ──→ native TilePMU: uniform cumulative counters
           │                            └──→ hardware / CSR / MMIO consumer
           └──→ FireSim target-cycle channels
                            └──→ bridge: 64-bit cumulative accumulation
                                           └──→ padded snapshot packets to host
```

Registry finalization creates no counter hardware. The optional TilePMU is ordinary
synthesizable Chisel hardware, usable without FireSim.
The FireSim backend uses the same event definitions and increment wires. Its
cumulative accumulators live in the bridge and advance only on accepted target
cycles. It does not copy full-width native counter values across the boundary.
The target trace stub supplies capture control and instruction/block-boundary
markers. A target-cycle token is distinct from a packed host output record.
The [2026-09-21 packet plan](firesim-bb-pmu-packet-plan.md) supersedes the earlier
compact saturating-delta backend: uniform 64-bit cumulative snapshots, software
deltas, and multi-beat PMU packets with continuous-valid serialization and beat priority.

The public PMU API has no dependency on MIDAS AutoCounter. AutoCounter's annotation,
clock analysis, routing, and bridge-constructor machinery are implementation
references for the optional FireSim backend.

## Public signatures

These declarations summarize the API implemented in `generators/pmu`.
`PMUEvents`, `PMUAttachment`, and `PMUManifest` are Scala holders, not Chisel Bundles.

```scala
case object PMUScopeKey extends Field[Option[PMURegistry]](None)

final class PMURegistry(source: String) {
  // Uses the current Chisel clock/reset context, captured when called.
  def register(
    name: String,
    increment: UInt,
    unit: String = "events",
    description: String = ""
  ): Unit

  // Explicit-context variant, useful from RawModule/helper code.
  // Metadata is explicit here; only the short overload has default arguments.
  def register(
    name: String,
    increment: UInt,
    clock: Clock,
    reset: Reset,
    unit: String,
    description: String
  ): Unit

  def build(counterBits: Int = 64): PMUEvents
}

// Constructed only by registry.build(); handles belong to the owner's module.
final class PMUEvents private[pmu] (
  val increments: Vector[UInt], // Each wire retains its own inferred width.
  val clock: Clock,
  val reset: Reset,
  val manifest: PMUManifest     // Sealed IDs; widths may still be provisional.
)

object TilePMU {
  def build(events: PMUEvents): PMUAttachment
}

final case class PMUAttachment(
  enable: Bool,              // Level input: accumulate while high.
  clear: Bool,               // Pulse input: clear running counters.
  snapshot: Bool,            // Pulse input: capture inclusive totals before clear.
  values: Vec[UInt],         // Live cumulative outputs.
  snapshot_values: Vec[UInt], // Stable captured outputs.
  snapshot_valid: Bool,      // Capture performed since hardware reset.
  manifest: PMUManifest      // Immutable provisional catalog, in output/ID order.
)
```

`register` is a public method on the registry instance. It does not require
`Parameters`; the caller chooses how to pass the registry to producers.
`PMUScopeKey` is an optional transport convenience. Consumers do not inspect
the registry while it is open.

### Producer contract

- `increment` is a producer-local bound UInt wire. Constants use a wire too.
  Reject bare literals and unbound types. Boolean conditions are converted with
  `.asUInt` and assigned to a wire.
- The wire carries the complete nonnegative increment on every observation edge;
  idle contributes zero. The producer computes every OR, PopCount, and sum.
- One producer per counter name per scope. Names are nonempty and unique;
  duplicate names fail even if their metadata matches. No implicit reduction,
  event-selection mask, identity/gauge operation, or returned counter value.
- Unit defaults to `events`; description defaults to empty. These are metadata,
  not hardware controls or numeric range assertions.
- The caller decides enablement and supplies the intended registry. When using
  the optional key, an enabled caller uses `p(PMUScopeKey).get`; a missing scope
  fails during elaboration. Guard optional instrumentation before obtaining the
  registry or constructing increment wires. The short overload always obtains
  Module.clock and Module.reset when called.
- Source widths can be unknown during Scala elaboration. Validate known widths
  early and validate all resolved widths in the compiler before bank-input
  truncation is legalized. Reject a width above counterBits, or a width that
  remains unresolved. Producers retain responsibility for arithmetic truncation
  that occurs before the registered wire.

Example:

```scala
val registry = p(PMUScopeKey).get // This example assumes PMU is enabled.
val retired = WireInit(PopCount(io.commit.arch_valids))
registry.register(
  "rob.instructions_retired", retired,
  unit = "instructions", description = "Architecturally retired instructions"
)

// Explicit context. This still must match the owning scope's domain.
registry.register(
  name = "rob.instructions_retired", increment = retired,
  clock = tile_clock, reset = tile_reset,
  unit = "instructions", description = "Architecturally retired instructions"
)
```

These are alternative spellings of one registration, not two registrations of
one counter. A `withClockAndReset` block also selects the short form's context.

### Scope and native attachment contract

Create a fresh registry before any instrumented lazy/base-class children are
constructed. Supply it directly or bind it into tile-local Parameters. `source`
is a nonempty capture identity, unique among scopes in one design. Compiler validation rejects
duplicate source identities; it requires no global mutable registry.

The tile integrator owns this lifecycle: supply the same registry to the intended
producers and call `build()` once. `Parameters` transports the reference; it does
not allocate or finalize the scope automatically. The library's misuse checks do not
detect omitted `build()` calls or producers given the wrong registry. Enabled
callers using `.get` fail immediately if the key has no registry. See the
[complete owner/producer example](../generators/pmu/README.md#example-one-registry-per-tile).

Call `build()` once in the owning Module's active clock/reset context after all
producers exist. It seals the registry, assigns dense IDs in registration order,
routes sources without padding them to counterBits, and emits backend-neutral
scope/event annotations. It returns `PMUEvents` and creates no counters or control
ports. A registry-only design is valid and still requires compiler validation.
It fails for an empty catalog, nonpositive counterBits, repeated `build()`, or late
registration. The scope's observation clock/reset are those active at `build()`.

```scala
// Before tile construction:
val registry = new PMURegistry(source = s"tile_$tileId")
val tileP = p.alterPartial { case PMUScopeKey => Some(registry) }
// Construct the tile and its children with tileP.
```

```scala
// At the end of its Module body, using that tile-local Parameters:
val registry = p(PMUScopeKey).get
val cycles = WireInit(1.U)
registry.register("tile.cycles", cycles, unit = "cycles")
val events = registry.build(counterBits = 64)
val pmu = TilePMU.build(events) // Optional native consumer.
pmu.enable := true.B // Direct hardware consumer; MMIO instead stores enable, reset false.
pmu.clear := false.B
pmu.snapshot := false.B
// Connect pmu.values to the desired native consumer.
```

`TilePMU.build(events)` is a separate native attachment step, called in the owner
module. It instantiates the bank using the clock/reset recorded in `events`, even
if the active Chisel context has since changed. The bank implementation lives in
[`TilePMU.scala`](../generators/pmu/src/main/scala/pmu/TilePMU.scala); registration
and finalization live in [`PMU.scala`](../generators/pmu/src/main/scala/pmu/PMU.scala).
Omitting this step leaves the catalog and narrow event wires available without
native counter/snapshot storage or MMIO. The existing `WithBoomPMU` configuration
explicitly performs both steps; the bridge backend and its selection remain future
integration work.

The bank starts at zero on owner reset and wraps modulo 2^counterBits while
`enable` is high. `clear` zeros the running counters. `snapshot` captures all
counters together, including that edge's increment when enabled, before any
simultaneous clear. Clear preserves the captured values and valid flag; hardware
reset zeros both banks and invalidates the snapshot. See the
[native MMIO ABI](boom-pmu-mmio.md) for enable-write timing and command handshakes.

Native enable, clear, and snapshot do not control the bridge's independent cumulative
bank or change registered source increments. Comparisons require matching windows
and reset semantics; the bridge supplies separate trace controls.

The manifest carries schema version, source, counterBits, and ordered entries
with IDs, names, units, descriptions, and optional Scala-known increment widths.
Compiler targets live in annotations, not as serialized Chisel objects in JSON.
The compiler produces a new finalized manifest with resolved widths and, when
tracing is enabled, ranges, packed field layout, and capture manifest identity.

The [implemented MMIO interface](boom-pmu-mmio.md) and future trace record schema
are separate consumers. Neither adds operations to `registry.register` or makes
native TilePMU depend on FireSim.

## Clock and reset rules

### What AutoCounter's explicit clock actually means

Its full API records `(signal, clock, reset)` as compiler targets. The transform
masks the event to zero under the associated reset. BridgeTopWiring resolves the
clock through the elaborated hierarchy to its source clock, and AutoCounter groups
events by that source. Golden Gate associates their channels with the corresponding
target clock. A bridge counter adds an increment only when a token for that target
cycle is accepted; the bridge hardware itself operates on the FPGA host clock.

For example, if one target domain advances twice as often as another, their
counters see their respective numbers of target edges. Five host cycles spent
waiting to accept one target token do not cause five increments.

The clock argument declares when to observe the signal. It neither creates a
synchronizer nor proves that the signal is valid in that domain. Passing the wrong
clock can produce a plausible but incorrect count. AutoCounter's default overload
uses the current Chisel Module.clock and Module.reset.

### Our v1 restriction: one domain per scope

- Every producer's declared clock/reset must match the owner's observation
  clock/reset. The explicit overload does not authorize a different domain.
- Compare resolved connectivity, not Scala object identity: a child's clock port
  and its parent's clock can be different objects wired to the same source.
- Accept direct clock/reset aliases and demonstrably equivalent simple casts.
  Reject an unrelated clock, added producer-local division/gating, independently
  generated local reset, or a domain relationship the checker cannot establish.
  The owner's domain may itself come from a clock generator or reset synchronizer;
  both sides must resolve to that same source. Stop at generated logic/registers
  rather than collapsing them to their upstream inputs and claiming equivalence.
- A same-frequency clock is not automatically the same clock. A diagnostic source
  string is not a clock-domain identifier. Clock/reset annotation sources must
  be bound targetable signals; represent a reset tied low with a wire.
- Compiler checks verify the declared domains, not arbitrary functional CDC
  correctness inside an increment expression. The producer must guarantee that
  its increment is valid on the declared edge. Explicit clock arguments must not
  be used to disguise a held event from a gated producer as a new event each cycle.
- Additional clock domains require separate registries and, when selected, native banks. Their
  FireSim support, especially alignment between observations, is later work.

Owner reset resets the native bank and invalidates an in-progress trace epoch;
trace collection must restart under the defined capture-start contract. It does
not manufacture a completed interval. Local independently reset producers are
outside v1, rather than silently adopting AutoCounter's per-event reset masks.

## Registry, annotations, and bridge construction

There is one source of event metadata. Registration stores it in memory; `build()`
emits the corresponding annotations without a second producer registration.

The implementation needs these compiler descriptions (internal class names are
not part of the public API):

1. Scope: source identity, owner-local clock/reset targets, and the catalog with counterBits.
2. Event: scope association, dense ID, metadata, increment target, and declared
   clock/reset targets. Use instance-aware targets with rename handling, including
   owner-local routed increment targets used to verify width preservation.
3. Optional trace attachment: scope association and target-stub control/marker
   targets. Trace selection belongs to configuration; do not include every
   existing AutoCounter or cover annotation automatically.

All builds, including registry-only designs, run the mandatory domain/width checks
and finalize the manifest. They do not require Golden Gate. Emulation builds additionally emit bridge channels plus an immutable serializable
constructor key. Range inference is optional metadata for future compact encoding.
The key contains no Data handles, mutable registry, closures, or compiler objects
whose identities depend on a previous JVM.

Use a stable serialization representation for arbitrary-precision values, such
as decimal strings, and version the schema. Check the Chisel 6 → FIRRTL2 → legacy
FIRRTL conversion explicitly; custom annotations do not survive by assumption.

Stage ordering must follow the actual toolchain. Range/domain analysis can run
after width inference and conditional expansion. AutoCounter's BridgeTopWiring
runs after the ClockBridge has been extracted and emits BridgeIOAnnotation and
channel annotations directly. Our annotation/routing prototype must distinguish
that phase from the earlier analysis and the later bridge Chisel elaboration.
No custom pass is required to emit accumulator RTL if bridge Chisel receives the
final ranges and field layout as constructor parameters.

The bridge accepts aligned increments and boundary/control tokens. It updates
uniform 64-bit cumulative counters only on target-token acceptance and captures
`counter + currentIncrement` at a boundary without clearing running counters.
The host differences complete snapshots within an epoch. Backpressure retains
all records without target-visible retirement stalls. The serializer retains a
complete wide PMU packet and keeps valid high through its last beat; ordinary
PMU beat priority preserves contiguity. Endpoint metadata associates
snapshots with BBs even when a PMU packet arrives first. Several logical
boundaries may refer to the same physical counter state.

## Native prototype and remaining integration

The standalone native prototype implements both overloads, registry lifecycle
checks, hierarchy routing, cumulative counters, and an immutable JSON catalog.
Its FIRRTL2 path validates widths/domains before input truncation and updates
absolute annotation targets through normal module deduplication. Native RTL is
emitted without Golden Gate. See [the runner and coverage](../tools/pmu/README.md).

Standalone users invoke `PMUCompiler` explicitly. Chipyard's Chisel 6 generator
now invokes the same checks for enabled PMUs and exports finalized native
manifests. BOOM v3 provides an opt-in 19-counter native catalog. The Chisel 6/FIRRTL2
annotation path is implemented; legacy FIRRTL bridge transport and trace
annotations are not.

The complete native/emulation acceptance checklist remains:

- Compile both registration overloads and verify clock/reset capture under normal
  Module, withClockAndReset, and explicit-context calls; verify caller-side enablement and missing-registry failure.
- Accept parent/child clock/reset aliases; reject different clocks, gated clocks,
  different resets, and unresolved declared domains in native and emulation builds.
- Transport scope/event/trace annotations through serialization and compiler
  conversion; verify instance identity with two tiles and normal deduplication.
- Resolve initially unknown widths, reject oversized native inputs before they
  truncate, and preserve the PopCount maximum of 4.
- Construct a bridge from the finalized catalog using uniform 64-bit cumulative
  counters. Verify inclusive snapshots, repeated boundaries, continuous-valid serialization and PMU
  beat priority, and paused target-token acceptance without duplicate increments.
- Elaborate the native bank independently of Golden Gate. The full bridge test
  is additional to the already executed expression-range experiment.

## Implementation references

- [AutoCounter API and annotations](../sims/firesim/sim/midas/targetutils/src/main/scala/midas/annotations.scala).
- [Clock-source resolution and routing](../sims/firesim/sim/midas/src/main/scala/midas/passes/BridgeTopWiring.scala).
- [Reset masking and per-clock bridge grouping](../sims/firesim/sim/midas/src/main/scala/midas/passes/AutoCounterTransform.scala).
- [Bridge accumulation on targetFire](../sims/firesim/sim/midas/src/main/scala/midas/widgets/AutoCounterBridge.scala).
