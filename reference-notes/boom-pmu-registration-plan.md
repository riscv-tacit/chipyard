# TilePMU registration and elaboration plan

Date: 2026-09-15\
Status: [PMU API v1](boom-pmu-api.md) decided for the initial implementation; standalone native PMU and FIRRTL2 validation prototype implemented ([code and checks](../tools/pmu/README.md)); The BOOM v3 native counter catalog and Chipyard Chisel 6 validation are integrated. The [range-information experiment](boom-pmu-range-experiment.md) has run successfully.\
Parent: [BOOM PMU and simulation tracer plan](boom-pmu-sim-trace-plan.md).
Counter selection: [native counter catalog and planned packing policy](boom-pmu-counter-catalog.md)
(2026-09-16; includes the two-million-cycle sizing target and reserved cycle field).

## Objective

Pipeline modules publish performance events through a PMU hook without manually
adding ports to every intermediate module. The PMU owns catalog validation,
automatic routing and accumulation, with values exposed to architectural/research
consumers. This is
an elaboration-time mechanism that generates hardware; it does not eliminate
physical wiring or its timing/resource cost.

## Three distinct components

1. Counter descriptions with unique names, units, and definitions. Dense numeric IDs are assigned when registration is sealed;
   a companion JSON manifest records the mapping for that build.
2. A fresh registration scope per tile and per elaboration. Producers register
   bound hardware sources and metadata while their modules are constructed.
3. Optional `TilePMU` hardware, instantiated separately after registration closes, consuming the collected
   increments and exposing current counter values.

Registration is an instance method on the caller-supplied registry. Do not use
a global mutable registry or mutable singleton CDE default. A tile factory or constructor boundary must create the scope
before any instrumented lazy or ordinary child is built, including base-class
children. Pass the scope explicitly or through a tile-local Parameters binding.

## Single-domain API v1

The authoritative signatures and clock/reset rules are in [PMU API v1](boom-pmu-api.md).
The sketches below describe that contract; implementation lives in `generators/pmu`.
The operations are create a scope, publish increments, finalize the catalog, and
optionally instantiate the native PMU.
The bank exposes live values, level enable, pulse clear/snapshot, and a coherent
native snapshot. The [MMIO adapter](boom-pmu-mmio.md) controls that interface;
trace intervals and tags belong to the downstream trace layer.

The hardware module is named `TilePMU`. `PMURegistry` is the elaboration-side API. Accumulator width is one bank-wide
configuration parameter, defaulting to 64 bits, with no per-counter override.
The [2026-09-21 packet plan](firesim-bb-pmu-packet-plan.md) now uses uniform
64-bit cumulative bridge counters and host-side deltas. The older nonuniform
saturating format is retained below as a deferred optimization.

### Local counter declarations

Each declaration supplies one producer-local UInt wire carrying the complete
per-cycle increment for one counter. There is one producer per name in a tile
registry. The producer computes any OR, PopCount, sum, or other reduction and
drives the resulting wire. Neither PMURegistry nor TilePMU infers or performs
reductions across event sources.

```scala
// API v1; see generators/pmu for the implementation.
case object PMUScopeKey extends Field[Option[PMURegistry]](None)

final class PMURegistry(source: String) {
  // Captures the caller's active Chisel clock/reset context.
  def register(
    name: String, increment: UInt,
    unit: String = "events", description: String = ""
  ): Unit

  // Explicit-context overload: metadata arguments have no defaults here.
  def register(
    name: String, increment: UInt, clock: Clock, reset: Reset,
    unit: String, description: String
  ): Unit

  def build(counterBits: Int = 64): PMUEvents
}
```

The caller supplies the same registry instance to every producer in one scope.
Passing it directly or through Parameters is the caller's choice. With Parameters,
an enabled caller uses `p(PMUScopeKey).get`, so missing setup fails during elaboration.
Optional instrumentation has an explicit caller-side guard:

```scala
if (p(EnableBoomPMU)) {
  val registry = p(PMUScopeKey).get
  val retired = WireInit(PopCount(io.commit.arch_valids))
  registry.register("rob.instructions_retired", retired, unit = "instructions")
}
```

Registration stores an already bound producer-local wire plus immutable metadata,
not a deferred hardware-generating closure. Constants also require a wire, such
as `WireInit(1.U)`; a bare literal has no owning module for hierarchical routing.
The caller supplies the wire; the registry validates it and rejects bare literals
or unbound types. A wire introduces no register or observation latency.
Registration records the current or explicitly supplied clock/reset with the wire.
The short overload always looks up the active Chisel context when called.
Registration neither allocates the running counter in the producer nor returns
its current value, and it introduces no producer ready/stall interface.

For example, these declarations live near the relevant existing pipeline signals.
Each assumes the caller has obtained its enabled scope as `registry`:

```scala
// ROB:
val retired_increment = WireInit(PopCount(io.commit.arch_valids))
registry.register(
  name = "rob.instructions_retired",
  increment = retired_increment,
  unit = "instructions",
  description = "Architecturally retired instructions"
)

// In each load pipeline. request is illustrative; choose the actual interface.
val accepted_increment = WireInit(request.fire.asUInt)
registry.register(
  name = s"lsu.load_pipe_$lane.requests_accepted",
  increment = accepted_increment,
  unit = "requests",
  description = "Accepted load requests, including accepted replays"
)

// At a module that already has all lane-stall signals:
val any_stalled_increment = WireInit(lane_stalled.asUInt.orR.asUInt)
registry.register(
  name = "lsu.any_lane_stalled_cycles",
  increment = any_stalled_increment,
  unit = "cycles",
  description = "Cycles with at least one stalled load lane"
)
```

Each name identifies a counter within its tile. Duplicate names fail, even if
metadata matches; no name implicitly merges registrations. Pipeline instances
use distinct prefixes. JSON records names, definitions, units, widths, and IDs;
no event enumeration or manually assigned ID is required.

The single-domain contract is an unsigned increment on each observation edge.
Idle contributes zero; Boolean events use `.asUInt`. Increment widths must fit
the configured counter width. Check known widths at Scala-time finalization;
record unresolved source targets for mandatory compiler validation after width
inference and before bank-input truncation is legalized. The range experiment
showed that ordinary `WireInit` sources can retain unknown widths throughout
Scala elaboration. If the producer sums multiple signals, the producer must
preserve the required arithmetic width.
Sampled gauges and histograms are outside this initial API.

The host can sum per-lane request deltas to recover total requests. It cannot
recover cycles with any lane stalled by summing per-lane stall-cycle counters,
because simultaneous stalls would be counted twice. Such a metric must be
computed where the Boolean signals meet, as above. If cross-module aggregation
is needed, a producer must explicitly obtain the inputs, compute the reduction,
and publish one resulting increment wire. Shared names never request a reduction.

### Scope ownership and finalization

Create one registry before constructing any instrumented tile children, including
lazy/base-class children. In the BOOM integration, descendants inherit it through
Parameters; other callers may pass the registry directly. The registry
is ephemeral elaboration state; it needs no domain string. The source name is
a required unique capture identity, not a clock-domain selector.

```scala
// Tile construction boundary, before instrumented children:
val registry = new PMURegistry(source = s"tile_$tileId")
val tileP = p.alterPartial { case PMUScopeKey => Some(registry) }
// Construct the tile with tileP.
```

```scala
// Near the end of that tile's Module body, after all producers exist:
val registry = p(PMUScopeKey).get
val cycle_increment = WireInit(1.U)
registry.register("tile.cycles", cycle_increment, unit = "cycles")
val events = registry.build(counterBits = 64)
val pmu = TilePMU.build(events) // Optional native consumer.

pmu.enable := true.B // Always-on direct hardware example, without the MMIO adapter.
pmu.snapshot := false.B
pmu.clear := false.B                 // Or an explicit measurement-reset control.
// Current values remain available to ordinary PMU consumers.
// Attach cumulative trace observation and padded packet transport separately.
// Emit pmu.manifest through the elaboration artifact writer.
```

Conceptual finalization interface:

```scala
def build(counterBits: Int = 64): PMUEvents

// PMUEvents holds the sealed catalog, width-preserving increment wires, and domain.
// Native construction is separate:
def build(events: PMUEvents): PMUAttachment // On the TilePMU companion object.

// PMUAttachment is a Scala holder:
// enable:   Bool                   // Input: level accumulation enable.
// clear:    Bool                   // Input: synchronous running-bank clear.
// snapshot: Bool                   // Input: coherent capture before clear.
// snapshot_values: Vec[UInt]       // Output: stable captured values.
// snapshot_valid: Bool            // Output: captured since hardware reset.
// values:   Vec[UInt]              // Output: current accumulated values.
// manifest: PMUManifest            // Immutable metadata, serialized as JSON.
```

`registry.build()` seals registration, assigns IDs, routes width-preserving source
wires, and emits backend-neutral annotations. It creates no counters. It runs in
the owner's active Chisel context after all intended producers have elaborated.
`TilePMU.build(events)` separately instantiates the optional native bank under
the recorded owner clock/reset. Event order equals
manifest ID order. Late registration and repeated `build()` calls fail. Compiler-
derived observation ranges, widths, and offsets are appended by a later layout
stage; `build()` must not pretend these are available before that analysis runs.
The Scala-time catalog may also contain unresolved increment widths. The compiler
must resolve and validate those widths and produce final metadata; it does not
update the original Scala wire handles. Source-target annotations and their
preservation through these stages remain part of the implementation prototype.

The scope's domain is the clock/reset active at `build()`. Every producer's captured
or explicitly supplied domain must resolve to that same domain. Compare actual
connectivity, not Scala object identity; direct parent/child aliases are valid.
Reject different, derived/gated, or unresolved clock/reset domains in v1. The
producer still guarantees that its increment is valid on the declared observation
edge; annotating a clock supplies no CDC or clock-enable correction.

### End-to-end attachment sketch

These integration names remain schematic, not existing BOOM APIs:

```scala
// In the tile Module body, with the tile-local Parameters and PMU enabled:
val registry = p(PMUScopeKey).get
val core = Module(new Core) // ROB registers during child construction.
val lsu  = Module(new LSU)  // Each load pipeline registers its own counter.
val stub = Module(new SimTraceStub)
// Connect existing pipeline interfaces and structured retirement observation.

val cycle_increment = WireInit(1.U)
registry.register("tile.cycles", cycle_increment, unit = "cycles")
val events = registry.build(counterBits = 64)
val pmu = TilePMU.build(events) // Optional native consumer.
pmu.enable := true.B // Always-on direct hardware example, without the MMIO adapter.
pmu.snapshot := false.B
pmu.clear := false.B

// Associate this scope with the stub's trace-control/marker targets.
// Compiler annotations preserve source identities, widths, and declared domains.
// The compiler analyzes ranges and routes narrow increments plus control to
// FireSim channels. Serialized layout parameters drive later bridge Chisel,
// which builds cumulative counters, coherent snapshots, and padded packets.
```

The observation attachment is separate from TilePMU's ordinary accumulator API.
The target stub owns interval boundaries and control. The bridge owns cumulative
counter state, capture buffers, padded packets, and transport. Increment wires and
aligned boundary/control signals cross the target-to-bridge boundary; padded
BB and cumulative PMU packets cross the bridge-to-host boundary. Native 64-bit counter values
remain available to native consumers. The stub does not need the counter list.
See [PMU API v1](boom-pmu-api.md) for registry, annotation, and serialized-catalog roles.

The implemented separation is:

```scala
val events = registry.build()          // Seal, route narrow wires, emit annotations.
val nativePMU = TilePMU.build(events)   // Optional native accumulator and snapshot bank.
// Drive nativePMU.enable, clear, and snapshot; connect its outputs or attach MMIO.
```

Registry-only designs omit the second step. Scope identity and event validation
are independent of any native bank instance. Routing adds no pipeline latency;
the registry still requires at least one event. Compiler validation remains
mandatory even when no native bank is instantiated. See the implementations in
[`PMU.scala`](../generators/pmu/src/main/scala/pmu/PMU.scala) and
[`TilePMU.scala`](../generators/pmu/src/main/scala/pmu/TilePMU.scala).

A condensed catalog for a two-lane configuration is:

```json
{
  "schema_version": 1,
  "source": "tile_0",
  "counter_bits": 64,
  "counters": [
    {"id": 0, "name": "rob.instructions_retired", "unit": "instructions"},
    {"id": 1, "name": "lsu.load_pipe_0.requests_accepted", "unit": "requests"},
    {"id": 2, "name": "lsu.load_pipe_1.requests_accepted", "unit": "requests"},
    {"id": 3, "name": "lsu.any_lane_stalled_cycles", "unit": "cycles"},
    {"id": 4, "name": "tile.cycles", "unit": "cycles"}
  ]
}
```

Actual IDs follow registration order. Full metadata also includes descriptions,
increment widths, and manifest identity. Source identity remains useful when
merging captures from several tiles; the literal domain label "tile" adds no
information and is omitted.

### Accumulation, clear, and observation semantics

TilePMU exposes enable, clear, live values, and an explicit coherent snapshot for
native MMIO. It has no interval tags or delta computation. The
[MMIO ABI](boom-pmu-mmio.md) defines the implemented control/read protocol.
Conceptually, each observation edge performs:

```text
next = counters + (enable ? increments : 0)   // modulo counter width
if snapshot:
    captured = next
    captured_valid = true
counters = clear ? 0 : next
```

Owner reset zeros both banks and invalidates captured data; MMIO enable resets
false. Clear takes priority over updating running counters but does not erase
the captured bank. Snapshot-and-clear includes the edge's enabled increment in
the captured result. Clear-only discards that increment. Reads never clear or
capture implicitly. Guest architectural HPM controls remain independent.

Ordinary native consumers and trace software can read cumulative values and
compute their own deltas. The selected bridge format exports independent 64-bit
cumulative snapshots, with a baseline per epoch and no per-boundary clear.
Native clear remains separate and does not reset bridge measurement state.

Native consumers must respect register timing: immediately after edge E, `values`
includes increments sampled at E; a register sampling `values` at E sees its
pre-E state. The trace bridge independently observes the increment and boundary
for that same target edge. On accepted tokens it captures the modulo-64-bit sum
of its cumulative value and the current increment. It does not snapshot native `values`.
Consecutive boundaries use the same rule. Owner reset invalidates the trace epoch;
native clear affects only the native bank unless trace control separately starts
a new epoch.

The downstream layer owns retaining each coherent observation while transport
proceeds, and FireSim backpressure protects its buffers. Multiple logical
boundaries in one physical cycle may reference a shared physical observation;
the host differences successive cumulative endpoints rather than adding a
shared observation more than once. Within-cycle attribution
still follows the parent plan.

## Historical compact observation proposal (deferred)

This section preserves the 2026-09-15 compact-format proposal and its analysis
work. It is superseded as the initial wire format by the
[2026-09-21 packet plan](firesim-bb-pmu-packet-plan.md). The initial bridge uses
64-bit cumulative snapshots; the rules below apply only if a future compact
encoding is explicitly selected. Existing analysis experiments remain useful.

Historical design point: automatically derive combinational value ranges,
size compact delta fields for a nominal observation window, and saturate at all
ones if a field's capacity is reached. Nominal length is a sizing parameter, not
a guaranteed bound on workload behavior. No heartbeat sampling, timeout-driven
interval splitting, overflow interrupts, or adaptive overflow records are required
for this initial design. BB-boundary/debt semantics remain unchanged.

### Automatic analysis versus annotations

Use value-range propagation over the actual registered increment expression,
not just its storage width:

| Expression | Range rule for unsigned values |
| --- | --- |
| Boolean converted to UInt | [0, 1] |
| Constant k | [k, k] |
| PopCount of N bits | [0, N] |
| Mux(c, a, b) | Conservative union of the two value ranges |
| Expanding addition | Sum of operand minima and maxima |
| Masks, comparisons, slices | Sound transfer functions respecting bit-vector semantics |
| Unsupported expression / unknown state | Conservative representable range |

A four-wide retirement PopCount has range [0, 4], even though its three-bit
representation permits 7. Recognize the high-level operation where available;
range propagation through a lowered sum of four [0, 1] operands also gives
[0, 4]. No producer-supplied range annotation is needed for this case. Respect truncating
addition, modular arithmetic, and register boundaries; do not apply unbounded
integer rules to finite-width operations or infer a register's lifetime range
from one update expression.

Correlated signals can require more analysis: a sum of eight Booleans is bounded
by 8 unless mutual exclusion or a tighter invariant is established. Optional
annotations may express architectural invariants that the analysis cannot infer;
keep their provenance in metadata and validate them. They are not mandatory
for ordinary registrations, and the initial producer API need not grow a bound
argument. Fall back conservatively when analysis cannot establish a tighter
range. Saturation must still use the actual increment, not a truncated value
based on an annotated maximum.

Chisel/FIRRTL width inference alone is not this range analysis. The compiler
analyzes the increment expressions and emits resolved widths, ranges, and layout
as serializable bridge constructor parameters. Later bridge Chisel elaboration
builds the compact observation storage and packed host-record interface using
those parameters. The target-to-bridge channels carry increments at their resolved
source widths, independent of compact interval field widths. Compiler results do
not retroactively update Scala-time source handles or Vec lengths. Keep registry
finalization, compiler analysis/routing, and bridge elaboration separate.

### Initial range-information experiment

The [executed experiment](boom-pmu-range-experiment.md) recovered [0, 4] from
four-input PopCount expressions in raw emitted IR, after ordinary lowering,
and after FireSim's legacy pre-GG target lowering. These are custom-analysis
results, not built-in UInt range metadata. Plain `WireInit(PopCount(...))` had
unknown width in the Scala registry; compiler width inference resolved it later.
Conditional drivers became straightforward to analyze after ExpandWhens.

The recommended next prototype places range analysis after width inference and
conditional expansion, transports annotations through the compiler handoff, then
constructs bridge-side compact state from serialized catalog/layout parameters.
It must verify the full Golden Gate channel and bridge-construction path. The first experiment did not run bridge
extraction or generate observation hardware; the two initial milestones below
are still required.

### Nominal sizing and stream record layout

Use an explicitly cycle-valued nominal sizing parameter, for example
`nominalObservationCycles = 1_000_000`. This is independent of the SimPoint
instruction-count interval. If a configuration derives it from a nominal
instruction count and an expected CPI, that conversion is a sizing estimate,
not a proof of execution time.

For inferred maximum per-cycle increment r_i and nominal cycle length L:

```text
nominalMaximum[i] = r_i * L
fieldBits[i]      = max(1, ceil(log2(nominalMaximum[i] + 2)))
fieldMaximum[i]   = 2^fieldBits[i] - 1
```

Reserve all ones for the lower-bound sentinel, including when the nominal maximum
is exactly `2^k - 1`. Compute these values with arbitrary-precision
elaboration/compiler arithmetic. The
[initial catalog](boom-pmu-counter-catalog.md#packing-policy) proposes two million
nominal cycles and a 64-bit cycle-field override; the following is a sizing example.
At L = 1,000,000 cycles, the four-wide retirement event uses a nominal maximum
of 4,000,000 and a 22-bit delta field; a 0/1 event uses 20 bits. Inferring 7 from
the retirement signal width would waste a bit here and is not the preferred
analysis. No hard cycle bound or proof of BB termination is needed for sizing.

Assign fixed field offsets from the manifest, then report total header/payload
bits, `ceil(recordBits / 512)` stream beats, and final padding. The
[2026-09-17 transport reassessment](firesim-pmu-stream-transport.md) removes the
mandatory one-beat limit: a dedicated bridge serializes larger records using
FireSim's shared stream transport. The initial catalog can still target one beat
for efficiency. Do not reduce counter coverage or nominal capacity merely to
satisfy a TraceDoctor-specific restriction. Saturation does not change record
length; multi-beat serialization is normal framing, not an overflow fallback.

### Saturation and reset semantics

Each bridge observation field represents an interval delta with this behavior.
The entire transition runs only on acceptance of a target-cycle token:

```text
S = 2^fieldBits - 1
next = min(S, delta + actualIncrement)    // Compute before narrowing.

if a boundary is captured:
    emit next                           // Includes the boundary edge's increment.
    delta <- 0                          // Next interval starts on the next edge.
else:
    delta <- next
```

The sum/comparison must have enough width for both operands and carry, or use an
equivalent overflow-safe comparison. Saturation is sticky until this delta is
emitted/reset. A long or nonterminating interval leaves the affected field at all
ones; it never wraps back to a plausible small count. Other fields continue
counting independently. No emergency observation is required. An explicit stop
may emit the pending partial interval; saturation does not manufacture a BB
boundary or imply that the logical interval completed.

All ones means **at least S**. It intentionally does not distinguish an exact
value of S from a larger value clipped to S, so no separate overflow bit is
needed. All other field values are exact. No per-counter delta history or modular
subtraction is needed in the host decoder. Clear/reset/capture-start semantics
must initialize the observation state and prevent mixing measurement epochs.

This requires saturation-aware state before values are narrowed for transport.
Taking low bits of a cumulative counter and subtracting them downstream cannot
implement this contract. A small saturating delta accumulator observing effective
increments is a direct implementation; an alternative must preserve the same
sticky saturation behavior, even when an interval does not terminate. This does
not change the uniform cumulative accumulator API of TilePMU. Observation state
is implemented by bridge Chisel; the target stub supplies its boundary/control
signals. The native bank and bridge bank share event definitions but have
independent accumulation state. Physical routing and channel alignment still
need validation in the prototype.

### Manifest and host interpretation

Extend the final capture manifest with:

- Encoding identifier, e.g. `saturating_delta_v1`.
- Nominal sizing length and its units (cycles).
- Per-event inferred increment range and inference/annotation provenance.
- Per-field nominal maximum, bit width, bit offset, and all-ones sentinel.
- Header width, total record width, 512-bit beat width, beat count, and padding.

The host preserves a per-field saturated/lower-bound flag with the raw value.
It must not silently treat all ones as an exact RFV feature or exact IPC operand.
Derived sums/rates inherit that qualification; choosing to filter or otherwise
handle saturated samples belongs to downstream analysis. Exact conservation
checks apply to unsaturated fields; saturated fields provide lower bounds.
Ordinary full-width timestamp/retirement metadata, where available, remains
separate from compact PMU fields.

### Commercial PMU comparison

Commercial PMUs commonly expose accumulated counter values with separate
configuration, enable, and reset/write operations. Intel's SDM describes reading
performance counters while running or stopped using RDPMC/RDMSR; these reads
return counts rather than automatically computing interval deltas. Arm's PMU
reset controls are separate writes; for example the Cortex-A78 PMCR documented
by AMD has distinct event-counter and cycle-counter reset bits.

This supports a small accumulator/current-value interface for TilePMU. It does
not require implementing the broader commercial facilities such as event
selection, privilege filtering, overflow interrupts, or sampling engines here.
Those are separate features. The initial MMIO frontend is documented in the
[native MMIO ABI](boom-pmu-mmio.md); architectural CSR integration remains future work.

Sources:
- [Intel SDM Volume 3B, performance monitoring](https://www.intel.com/content/dam/www/public/us/en/documents/manuals/64-ia-32-architectures-software-developer-vol-3b-part-2-manual.pdf).
- [AMD Cortex-A78 PMCR register documentation](https://docs.amd.com/r/en-US/am028-versal-gen2-register-reference/PMCR_EL0-DBG_A78_PMU-Register?contentId=szPrW9smmpgrxzkitiotzg).

## Explicit lifecycle

Externally, the lifecycle is Open -> Built; internal implementation can retain
separate sealing and routing stages.

- Create the scope before producer construction.
- Producers publish while Open.
- Call `build()` once, after all intended producers have elaborated.
- Reject publication after `build()` and repeated `build()` calls. Do not expose an
  early hardware vector or cache a view of an unfinished registry.
- Connect enable/clear/snapshot and observation endpoints and emit the manifest after `build()`.

Registration order may determine numeric IDs. Single-threaded, deterministic
elaboration makes this reproducible for the same construction/configuration;
IDs may legitimately change across builds. The companion JSON is authoritative.
The required ordering is all producers before finalization. Adding pipeline
registers to event routes later would change measurement alignment; do not do
that silently to fix timing.

### Diplomacy ordering trap

In this checkout, LazyModuleImp elaborates lazy children and executes registered
InModuleBody callbacks during base-class construction. BoomTileModuleImp then
constructs ordinary core and LSU Modules in its own body. An ordinary lazy PMU
child or early InModuleBody callback can consequently finalize too soon.

Create the scope early, but seal and collect near the end of the tile module
body, after all intended producers exist. A plain PMU Module instantiated there
is a reasonable initial backend. Lazy wrappers remain useful for attachment;
a lazy val alone does not establish the required ordering.

## Routing backend

Use automatic hierarchical routing behind the hook. The first candidate is
BoringUtils.bore(source), which creates intermediate connections without manual
Scala IO changes at each hierarchy level. The project defaults to Chisel 6.7.0
and has a Chisel 7 option. The older string-keyed addSource/addSink API is
already deprecated in the 6.7.0 source; avoid it for the new public abstraction.

This backend needs a small actual-toolchain prototype before BOOM integration:

- Collect before relevant modules are fully closed; require bound producer wires
  and a common hierarchy. Verify constant-driven wires and reject bare literals.
- Verify generated routing through nested ordinary modules and lazy children.
- Check Definition/Instance and clone restrictions explicitly. A cloned module
  may not rerun registration side effects.
- Elaborate two identical tiles and repeat elaboration in one JVM, with normal
  deduplication, to detect retained state and cross-tile connections.
- Run through the actual Golden Gate flow, not just standalone Chisel emission.

If necessary, replace native hierarchical routing with a transform while
preserving the producer API. Compiler annotations are already required for
domain/width validation and the FireSim backend; that does not require replacing
BoringUtils for native connections before testing the supported hierarchical API.

## Catalog and availability

- Assign dense IDs at finalization, allowing deterministic registration order.
  Emit a companion JSON containing the complete ID-to-counter mapping. Captures
  identify their manifest by version/hash; numeric IDs need not match across
  configurations. Use names and definitions for cross-build analysis.
- Each counter name has exactly one registered increment source within a tile.
  Duplicate names fail. Replicated producers use distinct names.
- Validate known increment widths at `build()` and unresolved widths after compiler
  width inference, before bank-input truncation is legalized. Reject oversized or
  still-unresolved sources. Producers are responsible for correct arithmetic
  before registration.
- Missing required counters fail. Absent optional counters appear as unavailable,
  rather than as measured zeros. Required/optional expectations belong to the
  measurement configuration and do not require a closed event enumeration.
- Emit schema version, names, definitions, widths, units, availability, and
  diagnostic producer paths into JSON, alongside tile/source metadata.
- Collect all configured research counters concurrently, independently of guest
  architectural counter selection.

## Architectural HPM integration

The PMU contract is one explicit increment wire per counter. PMURegistry routes
that wire and TilePMU accumulates its value. Neither component selects event
sets, interprets masks, ORs hits, or sums separate registered sources. A counter
that measures an OR or a sum receives that already-computed result from its
producer. Architectural CSR selector behavior does not define this API.

The existing CSR HPM path is a separate compatibility concern. It can remain
in place while the registry, bank, and observation prototypes are established.
Any later adapter that preserves legacy selectors must explicitly implement their
existing event-combination rules outside PMURegistry and TilePMU, and supply one
final increment wire per architectural counter. Preserve selector encodings and
timing, including the existing registered event path, when retaining compatibility.
Validate each mapped increment against the CSR input width; widen that interface
or reject an unsupported mapping rather than silently truncating it.

CSRFile is constructed inside BoomCore before the tile has finished elaborating
all producers, including the LSU. A future adapter must therefore define its
architectural interface early and connect it after sealing, without querying an
unfinished registry. Trace IDs need not equal architectural selector encodings;
record any implemented mapping separately in build metadata.

The research bank has independent accumulation. Guest counter writes, selector
changes, and inhibition must not silently corrupt RFV deltas. Architectural and
research totals are comparable only for equivalent events, measurement windows,
and enable/reset semantics. Legacy CSR integration is follow-on work and is not
a prerequisite for validating the PMU API or cumulative observation path.

## Clocking and snapshots

The first PMU assumes the owning tile observation clock/reset domain.
In the supported BOOM configuration, collect only producers verified to run in
that domain. Event publication inherits its PMU scope; it must not silently bore
raw increments across an unrelated clock. This keeps the registration/finalization
API simple without pretending it implements CDC.

BOOM v3's core and LSU are ordinary tile Modules, and the inspected v3 pipeline
sources do not introduce explicit withClock/ClockGate domains. Verify the actual
selected frontend/cache configuration too. If local clock gating is introduced,
a held event signal needs an explicit validity/clock-enable contract; it must
not become repeated events in an ungated collector.

Keep event latency explicit and associate event, boundary, and reset/control
channels with the same target clock. The bridge accumulates only on accepted
target tokens, captures through the boundary edge, and retains a coherent copy
while transport drains it. TilePMU separately exposes native cumulative values.
The explicit registration overload records a domain; it does not route CDC or
change the one-domain-per-scope rule. Clock/reset validation is required in native
builds as well as FireSim builds.

### L2 is a separate PMU scope, not another tile registration

Inclusive L2 is instantiated by the subsystem coherence-manager configuration.
Its clock can differ from the tile's; it can also be shared by multiple harts.
Future L2 instrumentation should therefore create its own registration scope,
local event accumulation, source identity, and manifest. Do not route its raw
pulses into the tile PMU or identify the source only by hart ID.

Two different problems must be solved before combining L2 and core RFVs:

1. Transport correctness: transfer a frozen multi-bit snapshot using a proper
   request/response handshake or asynchronous queue. Independent synchronizers
   on the counter bits are not sufficient.
2. Measurement alignment: a core interval request observed later in the L2
   domain captures a later state. A matching interval ID does not remove this
   skew. Record the actual local capture time and define a common-time mapping,
   or use a FireSim-aware coordinated collection mechanism. Do not label delayed
   snapshots as exactly simultaneous with the core boundary.

Shared-L2 events also need a declared scope: whole-cache activity versus
requester-attributed activity. Core-visible cache-interface events may be useful
initial features, but are not replacements for internal L2 misses/occupancy.

Initial scope: one tile clock domain and core/tile RFV events only. The manifest
records tile/source identity. Future domain-local PMUs will need explicit clock
and alignment metadata when that support is designed. Multidomain synchronization/alignment remains
separate work. Within-commit-group timing conventions follow the parent plan.

## Implementation milestones

1. Registry/routing prototype: nested producers, lazy and ordinary children,
   stable manifest, automatic collection, and a tiny counter bank. Prove the
   compiler and evaluation-order assumptions, including constant-driven wires.
2. Observation/compiler prototype: use a tiny producer with a wire driven by
   a four-bit PopCount and a boundary trigger. Validate domain/width annotations,
   route increment/control channels, and serialize the catalog/layout across the compiler
   handoff. Use later bridge Chisel to generate cumulative capture and padded
   BB/PMU packets; derive multi-beat layout and emit the final manifest. Distinguish early
   analysis, clock-bridge extraction, event-channel construction, and widget
   elaboration. Run through Golden Gate and verify layout agreement and capture
   under consecutive boundaries and backpressure. Both prototypes must pass
   before tile attachment.
3. Tile attachment: create a scope early, publish actual retirements and a few
   prediction/LSU events as explicit increment wires, collect after all producers,
   and instantiate the PMU.
4. Research capture integration: current-value output and explicit clear;
   downstream coherent capture on the stub trigger and the padded packet output
   with continuous-valid wide-packet serialization and PMU beat priority.
5. Event expansion: audit frontend, resource-pressure, and memory semantics
   before adding them to the catalog.
6. Architectural compatibility, as follow-on work: keep any legacy selector
   adapter separate from the PMU API. Define and test its explicit combinations,
   increment widths, selector timing, and inhibition behavior. Broader OS support
   is later work.

Required checks: reversed producer order; duplicate/late registration; required
versus optional missing events; distinct replicated counter names; constant-driven
wires and bare-literal rejection; producer-computed OR and sum increments; multi-bit
increments; snapshot with concurrent increment/reset; range inference for
four-bit PopCount and lowered sums; sound fallback for unknown correlations;
cumulative snapshot baselines and modular software deltas; boundary-inclusive
capture without per-boundary clear; continuous PMU validity through all serialized beats;
stable stalled outputs; fixed layout, multi-beat framing, and resource limits; two tiles; repeated elaboration; disabled PMU;
clock/reset alias acceptance and mismatched/gated/unresolved-domain rejection;
explicit and implicit registration contexts; annotation serialization and rename
handling; target-token stalls without duplicate increments; and no combinational
measurement feedback into event producers.

## References

- [BOOM tile](../generators/boom/src/main/scala/v3/common/tile.scala).
- [BOOM event definitions and CSR construction](../generators/boom/src/main/scala/v3/exu/core.scala).
- [Current event selection](../generators/rocket-chip/src/main/scala/rocket/Events.scala).
- [CSR counters](../generators/rocket-chip/src/main/scala/rocket/CSR.scala).
- [Diplomacy elaboration](../generators/diplomacy/diplomacy/src/diplomacy/lazymodule/LazyModuleImp.scala).
- [Chisel version selection](../build.sbt).
- [Chisel 6.7.0 BoringUtils](https://github.com/chipsalliance/chisel/blob/v6.7.0/src/main/scala/chisel3/util/experimental/BoringUtils.scala).

- [Chisel width inference](https://www.chisel-lang.org/docs/explanations/width-inference): distinguishes ordinary width inference from the proposed value-range analysis.
