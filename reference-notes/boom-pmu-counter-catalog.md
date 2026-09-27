# BOOM PMU initial counter catalog

Date: 2026-09-16\
Status: the complete native catalog is implemented in BOOM v3: 19 counters per
scope, including retired instructions. The bridge, optional packed-field
selection and packetization below remain planned. Native counters
are 64-bit wrapping cumulative counters. The [MMIO interface](boom-pmu-mmio.md)
controls native enable, clear, and coherent snapshots; counting starts disabled
after reset and all native counters share the enable window.\
Related: [PMU API](boom-pmu-api.md),
[registration and packing plan](boom-pmu-registration-plan.md),
[trace contract](boom-pmu-sim-trace-plan.md).

## Scope

The packet proposal can carry all **19 counters per tile** as 64-bit values;
omitting the retired-instruction field remains an optional catalog selection. Combine event counts with indicators of lost progress: miss counts
alone do not reveal whether latency was hidden by other work.

Retain `rob.instructions_retired` in the native PMU. Its packed field may be
omitted when the trace supplies the corresponding actual retirement count.
Verify observation windows agree: a logical instruction boundary inside a commit
group can differ from the whole-cycle PMU boundary. Do not substitute the nominal
instruction interval length for an actual count.

This catalog covers one BOOM v3 tile observation domain. Shared L2 events remain
a separate scope. Each producer supplies one complete increment wire, computing
any OR, PopCount, or other reduction before `registry.register(...)`. The owner
calls `registry.build()` after all producers have registered, then explicitly
instantiates native counters with `TilePMU.build(events)`.

## Basic events: 6 counters

| Counter | Unit | Implemented definition |
| --- | --- | --- |
| `tile.cycles` | cycles | Every observation edge outside reset, including stalls and idle cycles. Reserve 64 packed bits. |
| `branch.mispredict_redirects` | redirects | Resolved branch mispredictions that request recovery. Count the selected recovery request, not every resolving branch lane. |
| `frontend.icache_line_misses` | requests | Accepted requests to fetch missing instruction-cache lines; one per transaction, not per waiting cycle. |
| `frontend.itlb_walk_requests` | requests | Accepted instruction-side page-table-walker requests. |
| `lsu.dcache_line_misses` | requests | Accepted primary cacheable line-miss MSHR allocations, including prefetches. Exclude secondary merges, permission-only upgrades and uncached traffic. |
| `lsu.dtlb_walk_requests` | requests | Accepted data-side page-table-walker requests. |

Count activity when it occurs, including speculative activity, unless explicitly
stated otherwise. A walk request is not every TLB lookup miss, and a line-miss
transaction is not every load that misses or joins an outstanding miss. Put these
distinctions in the manifest descriptions.

### Producer signals

- [Core performance events](../generators/boom/src/main/scala/v3/exu/core.scala)
  expose legacy branch and cache/TLB events. The new branch counter is driven
  inside the selected branch-redirect arm, so a higher-priority delayed ROB flush
  suppresses it.
- [Frontend](../generators/boom/src/main/scala/v3/ifu/frontend.scala) and
  [LSU](../generators/boom/src/main/scala/v3/lsu/lsu.scala) use `io.ptw.req.fire`
  for their existing TLB performance events.
- [I-cache](../generators/boom/src/main/scala/v3/ifu/icache.scala) reports
  `tl_out.a.fire` as its acquisition event.
- [D-cache](../generators/boom/src/main/scala/v3/lsu/dcache.scala) reports
  `edge.done(tl_out.a)`. The
  [MSHR machinery](../generators/boom/src/main/scala/v3/lsu/mshrs.scala) also
  handles permission upgrades. The new event uses `req.fire && cacheable &&
  !idx_match(req_idx) && !req.bits.tag_match` in the MSHR file. Count accepted
  allocations, including speculative/prefetch allocations later canceled, rather
  than waiting for an external memory request or completion.

## Broad pipeline indicators: 4 counters

| Counter | Unit | Implemented definition |
| --- | --- | --- |
| `pipeline.dispatched_uops` | uops | Number of uops accepted into the backend this cycle, including speculative work. |
| `pipeline.frontend_starved_cycles` | cycles | Empty dispatch with conservative raw capacity available; excludes explicit recovery/control blocking. See qualification rules below. |
| `pipeline.backend_blocked_cycles` | cycles | First waiting dispatch lane is blocked by ROB, rename, dispatcher, LDQ/STQ or RoCC allocation capacity. |
| `pipeline.redirect_blocked_cycles` | cycles | Explicit redirect or rollback control is active, excluding CSR/trace stalls. Counts even if dispatch is empty. |

These are initial top-down diagnostic indicators. They do not yet implement a
mutually exclusive slot partition into retiring, bad speculation, frontend-bound,
and backend-bound categories. That needs additional accounting for partially
utilized cycles, squashed work, and recovery.

Dispatched minus retired uops is not an exact per-interval wrong-path count:
work can cross interval boundaries. Redirect-blocked cycles also do not
necessarily include subsequent frontend refill delay. Neither quantity alone
measures the complete misprediction penalty.

## Stall details: 8 counters

| Counter | Unit | Implemented definition |
| --- | --- | --- |
| `pipeline.rob_blocked_cycles` | cycles | The first waiting dispatch lane encounters a full ROB; serialization draining is separate. |
| `pipeline.rename_blocked_cycles` | cycles | The first waiting dispatch lane has an INT/FP/predicate rename stall. |
| `pipeline.issue_queue_blocked_cycles` | cycles | Dispatcher readiness blocks the first waiting lane. BasicDispatcher requires every issue queue ready, including queues unused by that uop. |
| `lsu.ldq_blocked_cycles` | cycles | The first waiting lane uses LDQ and cannot allocate an entry. |
| `lsu.stq_blocked_cycles` | cycles | The first waiting lane uses STQ and cannot allocate an entry. |
| `pipeline.serialization_blocked_cycles` | cycles | The first waiting lane encounters ROB draining, unique-instruction ordering, pipeline draining, or RoCC fence conditions. |
| `rob.head_load_wait_cycles` | cycles | The oldest valid entry in the current head row is a busy load without an exception, while commit is otherwise enabled. |
| `rob.nonempty_no_retire_cycles` | cycles | Nonempty ROB with no architectural retirement; includes recovery, external stalls and predicated-only commit. |

Each cycle counter adds at most one per cycle, even if several lanes qualify.
Resource counters require relevant waiting work: an unused full queue alone
must not increment a blocked-work counter.

Detail counters may overlap with one another and with the broad indicators.
Preserve those overlaps rather than arbitrarily assigning one cause; their sum
is not a cycle total or percentage partition. An incomplete load can await
translation, dependencies, forwarding, or memory, so it does not prove a cache miss.

The [dispatch hazard logic](../generators/boom/src/main/scala/v3/exu/core.scala)
provides the resource predicates. The following qualification is implemented
before registration.

## Packing policy

The [2026-09-21 packet plan](firesim-bb-pmu-packet-plan.md) selects uniform 64-bit
cumulative bridge counters, coherent snapshots, and host-side differences. Each
PMU packet has one header and is padded to the next 512-bit beat boundary. It
is captured as one complete, elaboration-fixed wide vector. Static padding and
continuous-valid wide-to-512 serialization feed an ordinary PMU-priority arbiter.
BB packets are padded to one beat and bypass the serializer.

The proposed PMU record contains one header word, four endpoint/identity metadata
words, and N counter words. Thus 19 counters use 24 words (three beats) exactly;
18 counters use 23 words plus one padding word, also three beats. There is no
per-counter ID on the wire: the manifest determines order. No native counter
API or event definition changes are required.

All counter fields wrap at 64 bits and are differenced in software within the
same capture epoch. All ones is a normal value. The previous nominal-cycle sizing
and saturating-field proposal is retained below only as a deferred optimization.

### Historical compact-field proposal (deferred)

Target a fixed-size PMU record of **512 bits including its header** for the
initial catalog as a bandwidth optimization, with saturating delta fields.
The [2026-09-17 transport reassessment](firesim-pmu-stream-transport.md) removes
this as a correctness limit: larger records may span multiple beats in a dedicated
FireSim bridge. Saturation does not change instruction/block boundaries or the
record's fixed length.

The proposed starting capacity target is:

```text
nominal instruction interval = 1,000,000 instructions
sizing IPC                  = 0.5 instructions/cycle
nominalObservationCycles    = 2,000,000 cycles
```

The IPC assumption is a sizing estimate, not a minimum IPC guarantee. Long stalls
and block-alignment overshoot can produce longer intervals. A proven maximum
increment per cycle does not establish a maximum total for an instruction interval.

For maximum per-cycle increment `r` and nominal cycles `C`, reserve all ones for
the lower-bound sentinel while allowing an exact nominal maximum:

```text
nominalMaximum = r * C
fieldBits      = max(1, ceil(log2(nominalMaximum + 2)))
sentinel       = 2^fieldBits - 1
```

The `+2` also reserves the sentinel when the nominal maximum equals `2^k - 1`.
Compute using arbitrary-precision compiler arithmetic. At two million cycles,
a 0/1 event needs 21 bits; a maximum increment of four needs 23 bits.

Allow explicit widths for important fields, initially:

```scala
nominalObservationCycles = 2_000_000
fieldBitsOverrides = Map("tile.cycles" -> 64)
```

These are proposed bridge packing settings, not current API declarations. Keep
this policy outside producer registration. A 64-bit cycle field provides ample
practical capacity for the timing denominator, but remains finite and retains
the same saturation semantics.

For illustration, 64 cycle bits plus 17 fields of 21 bits consumes 421 bits,
leaving 91 for headers and wider fields. Actual event bounds, optional retirement,
and header contents determine the final layout. Report every field's cost and
report the resulting beat count and padding. A layout exceeding 512 bits uses
multiple beats rather than silently reducing field capacity or counter coverage.

Values below the sentinel are exact; all ones means **at least the sentinel**.
Saturate before narrowing and hold saturation until capture/reset. The host must
preserve lower-bound status and must not use saturated values as exact features
or IPC operands. Saturation prevents misleading wraparound; it does not preserve
exact totals beyond capacity.

Record event definitions, widths, offsets, overrides, and nominal sizing cycles
in the manifest. Native 64-bit cumulative counters remain independent of bridge
interval accumulation and packing.

## Implemented qualification rules

- **Partial dispatch:** find the first valid lane that did not fire. Count all
  active causes on that lane, once per cycle. Older accepted lanes still increment
  dispatched uops; hazards in younger lanes are not attributed yet. This counts
  partial-bandwidth loss as well as cycles with no dispatch.
- **Recovery and external stalls:** suppress frontend/backend/resource and
  serialization classification during redirect, rollback, CSR stall (including
  WFI), or trace stall. The redirect counter records explicit recovery control
  unless CSR/trace stall is active. Dispatched uops always counts actual fires.
- **Frontend capacity:** require `rob.io.ready`, an available INT register and
  FP register when enabled, first-lane BasicDispatcher readiness, free LDQ and
  STQ entries, an available branch tag, and no decode-exception stall. Read
  free-list allocation-valid signals and the raw branch mask; invalid input uop
  bits cannot establish capacity. This conservative test may leave empty cycles
  unclassified even when a particular instruction type could have progressed.
  Normal pipeline supply bubbles qualify; no claim of exhaustive cycle attribution
  is made.
- **ROB readiness:** `!rob.io.ready` includes draining and exception state as
  well as fullness. Use the actual `full` signal for ROB capacity; attribute
  `s_wait_till_empty` to serialization.
- **Dispatcher:** BOOM v3 uses BasicDispatcher, which requires all issue queues
  ready. The issue-queue counter reports that actual restriction, including an
  unrelated full queue. Revisit this definition if the dispatcher changes.
- **ROB head:** select the first valid bank in the current head row; ignore stale
  busy/load bits in invalid banks. Do not count a younger busy load while an older
  valid entry exists. An empty head row contributes zero while the ROB advances.
  Use head state, never rollback's tail-selected commit uops. Exclude per-entry
  exceptions, the ROB's global commit-inhibit state (including post-exception
  delay), CSR/trace stalls, and flushes from head-load waiting.
- **No-retirement:** this is deliberately broader than head-load waiting. It
  includes any nonempty cycle without architectural retirement, including rollback
  and commits consisting only of predicated-away uops.
- **Misses:** frontend and PTW events count accepted handshakes. D-cache counts
  primary allocations, including prefetches and speculation. Held requests count
  when accepted, secondary merges do not add a new line miss, and upgrades/MMIO
  are excluded. D-cache request qualification already rejects killed requests
  before MSHR allocation; later cancellation does not undo the event.

## Validation and remaining work

`python3 tools/pmu/run-tests.py` exercises the shared observation predicates in
RTL, including four-wide partial dispatch, overlapping causes, stale invalid
lanes, recovery/CSR suppression, oldest-head selection, exceptional loads, and a
held miss request followed by merging/upgrades/MMIO. It includes 8,192 randomized
dispatch/ROB observations and the existing native counter-bank oracle.

After `sbt 'chipyard/compile'`, `python3 tools/pmu/test-boom.py` elaborates two
independent one-wide tiles, a two-wide tile, and a disabled configuration. It
checks all 19 events, dense IDs, names, units, descriptions, resolved widths,
per-tile isolation, and the absence of a bank/manifest when disabled. This is
elaboration and focused RTL validation, not a complete BOOM software workload.

Remaining bridge work: boundary transport, catalog selection, 64-bit cumulative
capture, padded BB/PMU packets, continuous-valid wide-to-512 serialization, PMU beat
priority, host reassembly/association, and software deltas. Validate baselines,
long intervals, same-cycle boundaries, and target-token backpressure. Compact
range-derived fields remain a deferred encoding rather than the v1 backend.
