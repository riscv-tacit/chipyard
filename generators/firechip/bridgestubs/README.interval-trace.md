# Interval trace bridge

See [dependencies and porting](PORTING.interval-trace.md) for compiler, BOOM,
submodule and FPGA-platform prerequisites when applying this outside the fork.

The target interface, BOOM/Chipyard routing, independent harness binder, FPGA host
widget, and interval-aggregating host driver are implemented. The driver writes
per-interval basic-block and PMU aggregates in interval JSONL version 2.
Validation covers target elaboration, RTL unit tests, the Chisel 6-to-Chisel 3
bridge key handoff, and a complete Large BOOM FireSim Verilator metasimulation.
MegaBOOM metasimulation and AWS F2 runs of this version remain unvalidated.

## Attachment

Use one of the configs in
[`TargetConfigs.scala`](../chip/src/main/scala/TargetConfigs.scala):

- `firechip.chip.FireSimSmallBoomV3IntervalTraceConfig`
- `firechip.chip.FireSimDualSmallBoomV3IntervalTraceConfig`
- `firechip.chip.FireSimMediumBoomV3IntervalTraceConfig`
- `firechip.chip.FireSimLargeBoomV3IntervalTraceConfig`
- `firechip.chip.FireSimMegaBoomV3IntervalTraceConfig`

[`WithBoomIntervalTrace`](../chip/src/main/scala/BridgeBinders.scala) composes the
interval harness binder, PMU sideband export, BOOM event registration, and existing
`WithTraceIO`. These configs use the MMIO-only FireSim bridge set, which has no
TracerV consumer. The interval bridge reuses `TracePort`/`TileTraceIO`; it adds no
second retirement output and does not use TraceDoctor or TACIT encoding.

BOOM's `WithPMUEvents` gives each independently elaborated BOOM v3 tile a PMU
registry, and the tile exposes its events through the PMU library's
`CanHavePMUEvents`. `WithPMUTraceIO(retirementLatency)` routes each tile's raw
increments through Chipyard with `PMUEvents.bore()`, paired with each trace entry
by tile identity. The default interval configs add no tile PMU. Compose BOOM's
`WithTilePMU` as well to add one; its enable/clear/snapshot controls do not affect
tracing.

`WithPMUTraceIO(retirementLatency, stallEvent)` can also name a **stall event**: a
one-bit cycle event during which the core is stalled without advancing its own
cycle counter. `WithBoomIntervalTrace` names BOOM's `csr.stall_cycles`, which
counts cycles with `csr_stall` asserted: from a WFI until an interrupt is pending
(or after CEASE). Rocket's CSR file, which BOOM v3 uses, holds `mcycle` and commit
during exactly these cycles; the tile clock keeps running. The bridge key carries
the event's name; it must be in the catalog with unit `cycles`, and the target-side
`IntervalTraceBridge.apply` checks that its increment is one bit wide.

The bridge key carries each tile's event list (ID, name, unit and description).
The bridge's increment ports are unsized on both sides. On the target side, FIRRTL
width inference sizes them from the tile's increments while Golden Gate lowers the
target; Golden Gate then builds each channel from those port widths, and the
FPGA-side widget's increment inputs are inferred from the channels they connect to.

Harness binders use first-match selection. Put `WithBoomIntervalTrace` before a
fallback `TracePort` binder. Stacking separate TracePort binders does not enable
multiple consumers. BOOM v4, cloned tiles, and counters outside the tile domain
are not supported by this attachment.

BOOM's architectural trace is delayed one target cycle relative to raw PMU events.
`IntervalTraceAligner` in the FPGA widget delays increments and reset by one
**accepted target token**, leaving the existing delayed trace unchanged. The
first real target token seeds that stage without accounting an undefined prior
observation. Before alignment, the widget discards the synthetic zero token that
FireSim's latency-one `HostPort` channel inserts at FPGA initialization.
Backpressure freezes the alignment state. At reset assertion, the last pre-reset
retirement still pairs with its pre-reset PMU sample; the following observation
carries reset and is excluded. No alignment registers are added to the DUT.
Capture stop includes all matched observations; the newest raw PMU sample still
waiting for its retirement trace is outside that capture. The catalog records
`retirement_latency` (one for BOOM v3, set by `WithBoomIntervalTrace`; zero for an
already aligned producer).

[`IntervalTraceBridge`](src/main/scala/interval/IntervalTraceBridge.scala) attaches
the bridge to a tile's trace and event-ID-ordered increments.

## Input and accounting

[`IntervalTrace.scala`](../bridgeinterfaces/src/main/scala/IntervalTrace.scala)
reuses the shared trace bundle:

- Per-slot `valid` is architectural retirement. Lane order and holes are retained.
  Original instruction bits identify RV64 conditional branches, JAL/JALR, their
  compressed forms, and trap returns. Taken and not-taken branches both end BBs.
- PC and instruction size identify unexpected sequential-PC discontinuities.
  Narrow PCs are sign-extended into 64-bit records. Counts come from valid slots,
  never PC subtraction.
- Privilege and trap fields are preserved at the interface. The current widget
  requires an RV64 producer (`causeWidth == 64`). It does not consume writeback
  data or CSR time.
- Clock/reset accompany the trace; narrow per-event increments accompany it.
  Tile PMU enable/clear controls do not affect this bank.

[`IntervalTraceDatapath`](../goldengateimplementations/src/main/scala/interval/IntervalTraceDatapath.scala)
advances time and modulo-2^64 PMU counters only on accepted non-reset target tokens
within a capture epoch. Cycles without retirement still count. A snapshot includes
the entire current cycle's increments. Several logical endpoints in one cycle
share that physical vector and timestamp, but have different snapshot IDs and
logical retirement positions. Software uses modular differences within an epoch.

A BB's elapsed cycles run from the preceding BB boundary through its own boundary.
The first BB ending in a target cycle receives elapsed time; later BBs get zero.
Instruction counts stop at the actual retirement slot. This is elapsed-time
accounting, not causal attribution of individual pipeline events.

With a stall event, each BB also carries its stall cycles: the stall counter's
increase from the preceding BB boundary through its own, under the same
first-BB-per-cycle convention. Because a stall holds commit, a WFI's stall cycles
fall in the BB containing that WFI, however many stalled BBs an interval holds.

Interval arithmetic retains `count + drift`, incrementing it per retired
instruction and subtracting the nominal length exactly once at each eligible
completed BB. Long blocks retain their debt. Younger slots in the same group can
close subsequent intervals. The nominal grid therefore does not drift with BB
overshoot. Logical instruction/time positions use 64-bit fields.

## Capture controls and discontinuities

[`IntervalTraceBridgeModule`](../goldengateimplementations/src/main/scala/IntervalTraceBridge.scala)
provides these host MMIO registers, in order:

| Register | Behavior |
| --- | --- |
| `init_done` | Holds target acceptance off until driver initialization finishes |
| `capture_enable` | Arms capture; clearing it requests an ordered stop |
| `interval_low`, `interval_high` | 64-bit nominal instruction interval; default 1,000,000 |
| `active` | Capture epoch is active |
| `drained` | Epoch closed and widget-local records have entered the stream engine |
| `config_error` | Inactive, armed capture has a zero interval, invalid selector, or reversed cycle window |
| `trigger_selector` | 0: always on; 1: cycle window; 2: PC endpoints; 3: masked instruction endpoints |
| `trigger_start_low`, `trigger_start_high` | Low/high halves of the selected start endpoint |
| `trigger_end_low`, `trigger_end_high` | Low/high halves of the selected end endpoint |

A start waits for a non-reset observation to be available. The widget latches the
nominal length, emits epoch start and a zero baseline, then accepts that first
observation. Host configuration writes during an active epoch do not change its
latched length. The first BB is marked partial-start because capture can begin
inside a dynamic block.

Trigger selection follows TracerV's four modes, with separate interval-bridge
plusargs and MMIO. It does not require a TracerV bridge or `tracing.enable`.
Program the selector/endpoints before arming and keep them fixed until shutdown.
The driver validates the options and writes all controls before `init_done`.

| Selector | `+interval-trace-start` / `+interval-trace-end` interpretation |
| --- | --- |
| `0` (default) | Always on; endpoints are not accepted |
| `1` | Unsigned decimal, inclusive cycle window; defaults 0 through 2^64-1 |
| `2` | Hexadecimal start/end PCs, both required; compare sign-extended 64-bit PCs |
| `3` | Hexadecimal `MMMMMMMMIIIIIIII`: upper 32 bits are mask, lower 32 bits are instruction value |

Hex values may have a `0x` prefix. Instruction matching is
`((retired_instruction ^ value) & mask) == 0`; a zero mask matches any valid
retirement. Mode 3 defaults to exact FireMarshal markers: start `ffffffff00008013`
and end `ffffffff00010013`. `+interval-trace-trigger` remains shorthand for mode 3;
it may be combined with endpoint overrides, but not a different explicit selector.

Cycle numbers are zero-based **aligned tile observations** after the aligner's
transport initialization/latency handling. Idle and target-reset observations
advance this counter; FPGA stalls and periods without an accepted target token do
not. Target reset does not restart the cycle window. Reset observations are still
excluded from PMU/time accounting. A window overlapping reset can therefore produce
multiple capture epochs. The window closes after its inclusive end observation,
even if no subsequent target observation arrives. These are local tile cycles,
not TracerV's driver conversion from a reference clock.

PC/instruction modes match only valid retirements. Both complete endpoint cycles
are included, including neighboring retirement slots, so PMU increments and
retirement counts cover the same cycles. Matching occurs after retirement/PMU
alignment. If several endpoints match in a cycle, the last match in retirement
order determines whether capture remains active; if start and end both match the
same slot, end wins. An unmatched end while inactive is ignored. Later start
matches create fresh epochs. Target reset closes capture and requires a fresh
start match in these modes; host stop overrides every trigger mode.

FireMarshal markers are `addi x0,x1,0` (`0x00008013`) and `addi x0,x2,0`
(`0x00010013`). Emit them as `.4byte` in inline assembly to prevent compression.
For example:

```text
+interval-trace-trigger
+interval-trace-select=1 +interval-trace-start=100000 +interval-trace-end=199999
+interval-trace-select=2 +interval-trace-start=80001000 +interval-trace-end=80002000
+interval-trace-select=3 +interval-trace-start=ffffffff00008013 +interval-trace-end=ffffffff00010013
```

These are alternative configurations. The configurable trigger adds MMIO registers
and replaces the earlier `trigger_enable` register with `trigger_selector`. It
requires a matching newly generated driver and bitstream; do not pair this driver
with the previous fixed-marker bitstream. The streamed record format is unchanged.

Validated with 18 RTL tests, the generated Golden Gate MMIO header, driver parsing
and MMIO tests, and Large BOOM metasim. PC endpoints `80000234`/`800002a6` and exact
FireMarshal instruction markers produce byte-identical features: 52,494 retired
instructions over 17,696 cycles. The inclusive cycle window 10,000–19,999 captures
exactly 10,000 cycles. The local validation report and per-mode captures are under
`tools/pmu/target/interval-metasim/triggers/`.

A host stop needs no further target token. After draining the current batch, it
emits a final PMU snapshot, any open partial BB, and epoch end. Stop includes all
previously accepted observations and excludes later ones. A final PMU snapshot is
emitted even if the last nominal interval ended exactly at the stop position.
Trailing idle cycles with no open BB are represented by the final PMU/epoch cycle
endpoint, without inventing a zero-instruction BB.

An accepted target-reset token closes the current epoch with reset flags; its
retirements/increments are ignored. Already frozen output finishes first. Reset
cycles are excluded, and a new baseline starts when reset deasserts if capture
remains enabled in the default, untriggered mode. Host reset/abrupt simulator abort can still truncate a stream;
the decoder rejects missing epoch end or an incomplete packet.

Unexpected PC discontinuities or privilege changes split an open BB into flagged
partial blocks. Trap flags are interpreted as cycle-level observations: process
that cycle's architectural retirements first, then flush its remaining partial
block and mark the next start partial. Partial-end records do not close a nominal
interval; their instructions still contribute to its progress. The next completed
control-flow block can close it.

BOOM explicitly notes that some CSR fields do not exactly align with retirement.
The above is an observable grouping convention, not a correction to the producer.
The interface also lacks ASID/context-switch identity. The initial output cannot
disambiguate identical PCs from different address spaces; such experiments need a
separate context contract.

## Stream format v1

[`IntervalTraceFormat`](../bridgeinterfaces/src/main/scala/IntervalTraceFormat.scala)
defines numeric assignments. Words are little-endian unsigned 64-bit values.
Header is word 0: bits 7:0 type, 15:8 version, 31:16 flags, 63:32 payload word count
(excluding header/padding). All padding is zero.

| Type | Payload words, in order | Size |
| --- | --- | --- |
| 1: BB | start PC, end PC, instruction count, elapsed cycles, and stall cycles when the key names a stall event | One 512-bit beat |
| 2: PMU | snapshot ID, logical boundary retirement count, cycle end, whole-cycle retirement count, counters in catalog order | `ceil((5 + counterCount) / 8)` beats |
| 3: Epoch start | epoch ID, nominal interval, counter count, catalog ID | One beat |
| 4: Epoch end | epoch ID, total retired, elapsed cycles, final snapshot ID | One beat |

Flag bits within the 16-bit flags field:

| Bits | Meaning |
| --- | --- |
| 0 | BB terminates a nominal interval |
| 1, 2 | BB partial start / partial end |
| 3 | PMU capture baseline |
| 4 | PMU final snapshot |
| 5 | Target-reset closure |
| 6 | Trap caused partial BB closure |
| 7 | Unexpected PC discontinuity caused partial closure |
| 8 | Privilege change caused partial closure |
| 15:13 | BB privilege/debug value from the original three-bit trace field |

The catalog ID and the companion JSON's `stall_event` identify whether BBs carry
the fifth word; the header's payload count must agree.

Epoch IDs begin at 1; baseline snapshot ID is 0. Nominal snapshots increment IDs;
the final snapshot consumes the next ID. Baselines and final snapshots are not
nominal interval snapshots. Catalog ID is a deterministic 64-bit digest recorded
in the companion JSON alongside the full event catalog/source identity.

One accepted target cycle produces one bounded batch. Storage covers a partial
split before and a completed BB after each retirement slot, plus a final trap
split, and up to one nominal PMU boundary per slot. All PMU records in the batch
are emitted before its BB records. The complete PMU packet is statically padded
and serialized with valid continuously asserted. A PMU-first arbiter feeds a
common two-entry output queue, which keeps stalled stream beats stable. Subsequent
batches enter only after current records reach that queue. Host-clock serialization
continues while target-token acceptance is paused; no DUT retirement stall is used.
Epoch start precedes baseline; epoch end follows the final PMU and partial BB.

## Driver and validation

[`intervaltrace_t`](src/main/cc/bridges/intervaltrace.cc) validates incoming packets
and aggregates each interval directly. Its default outputs are:

- `intervaltrace-0.json`: the existing PMU/wire catalog (wire format version 1).
- `intervaltrace-0-blocks.csv`: capture-local, one-based block IDs and definitions:
  `id,start_pc,end_pc,privilege,partial_start,partial_end`. PCs are hexadecimal.
- `intervaltrace-0-intervals.jsonl`: `interval_format_version: 3` records containing
  `[block_id,instructions,cycles,executions]` block entries, PMU deltas, endpoints,
  and IPC. IDs remain stable across intervals and epochs in the same capture.
  See [Interval JSONL format](#interval-jsonl-format).

New definitions are appended and flushed before their referencing interval is
written and flushed. The driver retains an identity-to-ID lookup plus the current
interval's block measurements; the catalog is not deferred until completion.
These flushes empty userspace buffers, not a power-loss durability guarantee.
JSONL retains the existing `.partial` naming until complete stream validation and
successful output closes. No interrupted-capture recovery mode is added.

`tools/interval-trace/check-compact-capture.py` validates a version 2 capture:
block catalog references, interval continuity, measurement totals, PMU endpoints,
and the smoke or SPEC workload results.

### Interval JSONL format

#### Version 3

Version 3 has exactly the fields of version 2. When the catalog
(`intervaltrace-0.json`) names a `stall_event`, every cycle field excludes stall
cycles: a block's `cycles` are its elapsed cycles minus its stall cycles, and
`start_cycle`/`end_cycle` are snapshot cycles minus the cumulative stall counter, so
`cycles`, `attributed_cycles`, `unattributed_cycles` and `ipc` all count busy
cycles and every identity below still holds. Elapsed (wall) time stays available
per interval in `counter_deltas`: `tile.cycles`' delta is `cycles` plus the stall
event's delta. Before subtracting, the driver checks that the BBs' stall cycles
equal the stall counter at every interval snapshot. Without a stall event,
version 3 is numerically identical to version 2.

#### Version 2

`intervaltrace-0-intervals.jsonl` holds one JSON object per line, one per interval,
in capture order. Every record starts with `"interval_format_version": 2` (3 in
current captures).

| Field | Meaning |
|---|---|
| `interval_format_version` | `2`. |
| `epoch` | Capture epoch ID, starting at 1. Target reset and each new trigger window start a new epoch. |
| `snapshot_id` | ID of the PMU snapshot that closes this interval (see [Stream format v1](#stream-format-v1)). |
| `nominal_interval` | Nominal interval length in retired instructions, fixed for the epoch. |
| `start_retired`, `end_retired` | Logical retirement counts at the interval's opening and closing boundaries, from the epoch baseline. |
| `start_cycle`, `end_cycle` | Target cycle counts at the opening and closing snapshots. |
| `start_physical_retired`, `end_physical_retired` | Retirement counts of the complete target cycles at the opening and closing snapshots. |
| `instructions` | `end_retired - start_retired`; equal to the sum of the block entries' instructions. |
| `cycles` | `end_cycle - start_cycle`. |
| `attributed_cycles` | Sum of the block entries' cycles. Equal to `cycles` except in a final interval. |
| `unattributed_cycles` | `cycles - attributed_cycles`: cycles after the last block of a final interval. |
| `physical_retirements` | `end_physical_retired - start_physical_retired`: retirements over the same whole cycles as `counter_deltas`. |
| `partial_blocks` | Number of block instances in the interval that start or end mid-block. |
| `flags` | Flags of the closing PMU snapshot, using the stream flag bits: `0` nominal; bit 4 final snapshot; bit 5 target-reset closure. |
| `bb_flags` | Bitwise OR of the flags of every block instance in the interval, using the stream flag bits. |
| `ipc` | `instructions / cycles`; `null` when `cycles` is 0. |
| `final` | `true` for the last interval of an epoch, closed by the final snapshot. It can be shorter than `nominal_interval`. |
| `counter_deltas` | For each PMU event, the counter difference between the opening and closing snapshots, modulo 2^64, in event-ID order of `intervaltrace-0.json`. |
| `blocks` | One `[block_id, instructions, cycles, executions]` entry per distinct block in the interval: retired instructions and attributed cycles within this interval, and the number of times the block executed. |

A block's cycles run from the preceding block boundary through its own; when
several blocks end in the same target cycle, the first receives that cycle's
elapsed time and the others zero.

`intervaltrace-0-blocks.csv` defines the block IDs:

| Column | Meaning |
|---|---|
| `id` | Block ID, from 1, in order of first appearance. Stable across intervals and epochs of one capture. |
| `start_pc`, `end_pc` | Hexadecimal PCs of the block's first and last retired instructions. |
| `privilege` | Privilege/debug value from the trace's three-bit field (`0` user, `1` supervisor, `3` machine). |
| `partial_start`, `partial_end` | `1` if the block's instances start (end) mid-block: at capture start or stop, a trap, a PC discontinuity, or a privilege change. |

All five columns together identify a block: the same PC range with a different
privilege or partial flag has its own ID. Each definition is written to
`blocks.csv` before the first interval that refers to it. While a capture runs,
the interval file is named `intervaltrace-0-intervals.jsonl.partial`; it receives
its final name after the stream validates and the outputs close.

#### Version 1 (deprecated)

Captures written before version 2 have no `interval_format_version` field and no
`blocks.csv`. Each block entry carries its identity inline:
`[start_pc, end_pc, privilege, partial_flags, instructions, cycles, executions]`,
with PCs as integers and `partial_flags` using stream flag bit 1 (value 2) for a
partial start and bit 2 (value 4) for a partial end. All other fields match
version 2.

The driver no longer writes version 1. Read it only to process existing captures:
numbering the distinct `(start_pc, end_pc, privilege, partial_flags)` entries in
order of first appearance gives the version 2 block IDs.

With `+interval-trace-csv`, the additional files are:

- `intervaltrace-0-bb.csv`: start/end PCs (hex), instruction count, elapsed cycles,
  interval/partial flags, privilege, the complete numeric flag word, and stall cycles
  (empty without a stall event). These are the received records: elapsed cycles
  include stall cycles.
- `intervaltrace-0-pmu.csv`: baseline, nominal, and final snapshots, with logical
  retirement/cycle/physical retirement endpoints and named event columns. Counters
  are **cumulative 64-bit values**, exactly as received. Compute modular deltas
  between successive snapshots within an epoch; do not subtract across epochs.
- `intervaltrace-0-epochs.csv`: start/end records retaining nominal interval,
  catalog identity, final totals, and reset flags. Fields inapplicable to a record
  are empty.

Every CSV row carries its `epoch` and a shared, one-based `record` sequence number.
Sorting all three files by `record` reconstructs original packet order, including
PMU-before-BB ordering. CSV contains no 512-bit padding. These are decoded records.

Options:

- `+interval-trace-file=PREFIX`: set the filename prefix before `-<bridge-index>`
  (default `intervaltrace`).
- `+interval-trace-csv`: additionally save the per-BB, PMU, and epoch CSVs described
  above. Disabled by default; long runs can produce very large BB CSVs.
- `+interval-trace-raw`: additionally save `PREFIX-<bridge-index>.bin`, preserving
  received bytes before validation for debugging/replay. Binary dumping is off by
  default; the bridge wire format remains unchanged.
- `+interval-trace-interval=N`: positive 64-bit nominal instruction length.
- `+interval-trace-trigger`: shorthand for the FireMarshal instruction trigger.
- `+interval-trace-select=0|1|2|3`: select always-on, cycle, PC, or instruction triggering.
- `+interval-trace-start=VALUE`, `+interval-trace-end=VALUE`: endpoints as described above.
- `+interval-trace-disable`: consume observations without recording a capture.

The driver initializes/arms through host MMIO and stops during `finish()`, draining
widget output and flushing the stream engine. It does not depend on TracerV.

The incremental C++ decoder retains partial packets across DMA pulls and checks
version, lengths, padding, catalog identity, epoch/snapshot ordering, and endpoints.
It queues PMU endpoints ahead of BBs and requires exactly one matching snapshot
for every `terminate_interval` BB, with matching retirement and cycle endpoints.
Epoch end checks final counts and unmatched
snapshots. Validation/I/O failures fail the run, including failures during finish.
A successful enabled capture prints one finish summary: completed epochs and
nominal intervals, BB records (including the number with either partial flag),
reset closures, retired instructions, target cycles, and `stream_bytes` received
(the transport byte count, including padding, not CSV file size). With a stall
event it also prints `stall_cycles`; its `cycles` remain elapsed target cycles.
Partial BBs carrying both partial flags count once. Counts span all capture epochs.
Reset counts represent reset-closed epochs, not cycles spent in reset.

An enabled capture with no retired instructions warns once at finish. A short
capture with BB records but no completed nominal interval is valid and does not
warn. Disabled capture produces neither files nor diagnostics. Normal overshoot,
same-cycle boundaries, zero-cycle BBs, resets, and partial BBs do not warn.
Framing/catalog errors, snapshot/boundary mismatches, invalid totals, incomplete
normal shutdown, and file I/O failures remain fatal. Failed captures print errors
without a successful-capture summary. Repeated `finish()` calls do not repeat
warnings or summaries.

Run from the Chipyard root with its Java/Scala tool environment:

```sh
java -jar scripts/sbt-launch.jar \
  'firechip_intervaltests/test' \
  'firechip_bridgestubs/testOnly firechip.bridgestubs.IntervalTraceInterfaceSpec'
python3 generators/firechip/intervaltests/check-legacy.py
python3 generators/firechip/intervaltests/check-host.py
```

The legacy check uses FireSim's cached dependency classpath to compile the actual
widget with Chisel 3.6.1, import a Chisel 6.7.0 annotation, and elaborate the widget,
emitting CHIRRTL plus the driver header. Standalone, the widget's increment inputs
are unsized and undriven, so it is not lowered to Verilog here; Golden Gate sizes
them from the target channels in a real build. The host check compiles that generated constructor/MMIO layout and
the driver, tests malformed/fragmented packets, and decodes RTL-produced streams.
It also runs the actual driver against mocked MMIO/stream endpoints using those
RTL streams, checking every CSV field against the original packets, quoted event
names, shared record ordering, optional byte-for-byte raw dumps, metadata files,
stop/flush ordering, empty/disabled captures, idempotent finish, and fatal
decode/write/close failures. It checks the interval JSONL version 2 output across
output modes and against per-epoch totals and block identities taken directly
from the packets, the exact aggregation of a synthetic stream covering counter
wraparound, reset epochs, same-cycle snapshots, overshoot and an idle tail, and
the rejection of invalid streams.

RTL tests cover holes, idle cycles, overshoot debt, same-cycle boundaries, coherent
PMU capture, 18/19-counter padding, wraparound, compressed instructions, traps,
privilege/PC discontinuities, reset/restart, host-only tail flushing, capture
configuration, and stalled-beat stability against a software reference.

To elaborate a connected target (without building a metasimulator):

```sh
java -jar scripts/sbt-launch.jar \
  'firechip/runMain chipyard.Generator --target-dir tools/pmu/target/interval-bridge/small --name interval-small --top-module firechip.chip.FireSim --configs firechip.chip.FireSimSmallBoomV3IntervalTraceConfig --emit-legacy-sfc'
python3 generators/firechip/intervaltests/check-legacy.py \
  tools/pmu/target/interval-bridge/small/interval-small.anno.json
```

The optional annotation argument elaborates the widget from an actual target's
bridge key; that key's widths are filled by Golden Gate, so pass an annotation
file whose key already has widths, such as the one the interface test writes.
The interface tests cover key validation, conversion from a tile's event list,
unresolved keys, and per-tile port shapes. Aligner tests cover startup, token
stalls, and reset assertion/deassertion, including removal of the synthetic
HostPort startup token. End-to-end metasim execution covers Large.

## FireMarshal metasim smoke test

The [build recipes](../../../sims/firesim-staging/sample_config_build_recipes.yaml)
include `alveo_u250_firesim_large_boom_v3_interval_trace` and
`alveo_u250_firesim_mega_boom_v3_interval_trace`. Both can build Verilator metasims
or later U250 bitstreams. The [runtime config](../../../sims/firesim-staging/interval-trace-runtime.yaml)
selects one local Large BOOM metasim, a 1,000-instruction interval, and a
2,000,000-target-cycle timeout. The hardware default remains 1,000,000 instructions.

The [FireMarshal workload](../../../software/firemarshal/example-workloads/interval-trace-smoke.json)
uses `bare-base.json` and the existing HTIF baremetal runtime. It updates an array
through a small branch/memory loop, checks a checksum, prints
`PASS interval-trace-smoke checksum=40832`, and returns zero. It does not program
trace hardware or the tile PMU. FireMarshal installs a baremetal boot ELF and
a dummy rootfs; no Linux image is built. Collected outputs include `uartlog`,
`intervaltrace-0-intervals.jsonl`, `intervaltrace-0-blocks.csv`, and `intervaltrace-0.json` by default.
Use `--csv` or `--raw` on the smoke runner to retain the optional detailed outputs.

From the Chipyard root, with the checkout's conda toolchain installed:

```sh
.conda-env/bin/python3 tools/interval-trace/run-interval-metasim.py --core large
# Reuse the built metasimulator for another run:
.conda-env/bin/python3 tools/interval-trace/run-interval-metasim.py --core large --skip-build
# Build and run Mega instead:
.conda-env/bin/python3 tools/interval-trace/run-interval-metasim.py --core mega
# Stall accounting: three timer sleeps in WFI between the markers
.conda-env/bin/python3 tools/interval-trace/run-interval-metasim.py --core mega --workload wfi --trigger --csv
```

The `wfi` workload (`interval-trace-wfi`) runs in M-mode with interrupts globally
disabled, so WFI wakes on the pending timer interrupt without a trap. It prints its
own `mcycle`, `minstret` and `mtime` differences over the marked region. The runner
checks that the capture's interval cycles match `mcycle`, that the stall cycles
cover the requested waits, and that every interval with stall cycles contains a
block holding a WFI.

The local runner builds and installs the image through FireMarshal, invokes the
Chipyard generator, Golden Gate and Verilator, runs the workload, and requires a
nonempty interval capture plus successful in-driver decoder checks. It checks
`rob.instructions_retired` and `tile.cycles` deltas against each interval's physical
retirement and cycle counts and validates the capture with `check-compact-capture.py`.
With `--csv`, it also checks the detailed snapshot endpoints and record ordering.
Results and stage logs are under `tools/interval-trace/target/interval-metasim/<core>/`. It sets its own local tool
paths so a relocated checkout does not depend on a stale generated `env.sh`.
`--skip-target-elaboration` is only for explicitly reusing target FIRRTL and
annotations already in that recipe's FireSim generated directory. Add `--raw` to
retain the optional binary dump and run the standalone decoder on it as well.
Use `--trigger` for FireMarshal markers or `--trigger-select`, `--trigger-start`,
and `--trigger-end` for the configurable modes. `--output DIR` keeps results from
different trigger configurations in separate directories.
The FireMarshal output list collects the default interval JSONL and catalog; add
the CSV or `.bin` filenames to that list when collecting optional diagnostic dumps
through the manager. Reinstall the workload after changing its output list.

For the FireSim manager workflow, first build/install the FireMarshal workload,
then run from `sims/firesim/deploy` in the usual sourced manager environment:

```sh
firesim infrasetup -c ../../firesim-staging/interval-trace-runtime.yaml \
  -r ../../firesim-staging/sample_config_build_recipes.yaml \
  -a ../../firesim-staging/sample_config_hwdb.yaml
firesim runworkload -c ../../firesim-staging/interval-trace-runtime.yaml \
  -r ../../firesim-staging/sample_config_build_recipes.yaml \
  -a ../../firesim-staging/sample_config_hwdb.yaml
```

That route requires the normal local SSH/run-farm setup. `tracing.enable` is false
because it controls TracerV; interval capture is enabled through its own driver.
To use Mega, change `target_config.default_hw_config` to its recipe name. The
local runner does not invoke FPGA provisioning or bitstream generation.

The earlier CSV-default driver was validated on Large BOOM with the smoke-run settings: workload checksum
40832, 30,210 simulated target cycles, one capture epoch, 61 completed intervals,
12,090 BB records, and 785,984 stream bytes. The three driver-written CSVs total
576,919 bytes (about 27% less than the padded binary dump); default capture creates
no `.bin` file. All 63 PMU snapshots matched both
independent endpoints, and the C++ decoder accepted the complete stream including
shutdown records. Those detailed checks remain available with `--csv`; raw packet
validation always runs inside the driver.
This integration test exposed and now covers the HostPort
initialization token that previously created a spurious one-cycle epoch.

## AWS F2 MegaBOOM bitstream build

The recipe `firesim_mega_boom_v3_interval_trace` in
[`sample_config_build_recipes.yaml`](../../../sims/firesim-staging/sample_config_build_recipes.yaml)
uses `FireSimMegaBoomV3IntervalTraceConfig`,
`FRFCFS16GBQuadRank_BaseF2PCIMConfig`, a 50 MHz host clock, and `RUNTIMEOPTIMIZED`.
That is the FireSim enum spelling of Vivado's `RuntimeOptimized` synthesis
directive. The existing F2 build scripts forward it to `synth_design`.
The PCIM configuration sends the interval stream directly into host memory using
`FPGAManagedStreamEngine`, matching the established F2 trace builds. The interval
bridge and its driver use the shared stream interface. The updated F2 runtime
enables PCIe bus mastering, and the manager reserves hugepages during run-farm
setup for the DMA buffers.

[`interval-trace-build.yaml`](../../../sims/firesim-staging/interval-trace-build.yaml)
selects this one build and an AWS EC2 build farm with on-demand `z1d.12xlarge`
builders, matching the established F2 MegaBOOM build setup. The farm has its own
`interval-trace-buildfarm` tag. The manager generates RTL and compiles the driver
locally, provisions the build host, copies the design, runs Vivado, collects
results, creates the AFI, and releases the build host.

From the Chipyard root, using the normal FireSim manager environment:

```sh
cd sims/firesim
source ./sourceme-manager.sh --skip-ssh-setup
cd deploy
firesim buildbitstream \
  -b ../../firesim-staging/interval-trace-build.yaml \
  -r ../../firesim-staging/sample_config_build_recipes.yaml \
  -a ../../firesim-staging/sample_config_hwdb.yaml -q
```

Use a persistent terminal such as tmux for the long-running manager command.
The checkout's generated `env.sh` files must point at this checkout, and the
standard F2 SDK/HDK setup must be complete. The manager accepts `FIRESIM_KEYPAIR`
for the EC2 keypair name and `FIRESIM_SSH_KEY` for the local private-key path,
defaulting to `firesim` and `~/firesim.pem`. This workspace's local environment
selects the existing `firesim-lux` keypair. SSH keepalives cover long silent Vivado
phases. The recipe uses the usual F2
bit-builder S3 configuration and does not request sharing with other accounts.
Manager logs are in `sims/firesim/deploy/logs/`; collected build artifacts go to
`results-build/`, and successful builds write a corresponding entry under
`built-hwdb-entries/`. This is a manager build, not the local checkpoint-only path.

## AWS F2 smoke workload

[`interval-trace-f2-runtime.yaml`](../../../sims/firesim-staging/interval-trace-f2-runtime.yaml)
runs the same `interval-trace-smoke` baremetal ELF on one on-demand `f2.6xlarge`,
using the MegaBOOM PCIM image in
[`interval-trace-hwdb.yaml`](../../../sims/firesim-staging/interval-trace-hwdb.yaml).
It captures 1,000-instruction intervals, with a two-million-target-cycle limit.
The expected workload output is `PASS interval-trace-smoke checksum=40832`.

Build and install the workload from the Chipyard root in the sourced manager
environment:

```sh
software/firemarshal/marshal build software/firemarshal/example-workloads/interval-trace-smoke.json
software/firemarshal/marshal install software/firemarshal/example-workloads/interval-trace-smoke.json
```

Then, from `sims/firesim/deploy`, in a dedicated shell:

```sh
runtime=../../firesim-staging/interval-trace-f2-runtime.yaml
hwdb=../../firesim-staging/interval-trace-hwdb.yaml
recipes=../../firesim-staging/sample_config_build_recipes.yaml
trap 'firesim terminaterunfarm -c "$runtime" -a "$hwdb" -r "$recipes" -q' EXIT
firesim launchrunfarm -c "$runtime" -a "$hwdb" -r "$recipes" &&
firesim infrasetup -c "$runtime" -a "$hwdb" -r "$recipes" &&
firesim runworkload -c "$runtime" -a "$hwdb" -r "$recipes"
```

The runtime requests termination on completion; the shell exit trap also covers
setup or workload failures when that shell exits. Use a distinct `run_farm_tag`
and `suffix_tag` for concurrent runs. Results under `results-workload/` include
`uartlog`, interval JSONL, and the counter catalog with the updated host driver.
Archived driver bundles from earlier builds still produce CSVs and require their
matching workload output lists. Validate a collected capture with
`tools/interval-trace/check-compact-capture.py`. Hardware and metasim runs should satisfy the same
trace and PMU invariants; cycle counts depend on the core configuration.

Validated on AWS F2 with MegaBOOM and `agfi-05e72126d1d207bad` on
2026-09-26: checksum 40832, 33,282 simulated target cycles, one capture epoch,
66 completed nominal intervals plus the final tail, 14,599 BB records, and 68
PMU snapshots. The capture contains 66,678 instructions over 33,228 cycles with
zero unattributed cycles. All PMU retirement/cycle endpoints, record ordering,
and BB/epoch totals passed the same checks used by metasim. Offline processing
produced 66 aligned BBV/BBTV rows and RFV/IPC summaries. The simulation exited
successfully, and the manager requested termination of its dedicated instance.
