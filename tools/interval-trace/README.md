# Interval features

This exporter supports the earlier verbose interval JSONL and optional record
CSVs. It does **not** consume the current compact `interval_format_version: 2`
JSONL/block-catalog pair. Current collection stops at those driver outputs; use
`tools/pmu/check-compact-capture.py` to validate a smoke or suite capture without
generating feature exports. The instructions below describe the legacy exporter.

From the Chipyard root:

```sh
python3 tools/interval-trace/process.py \
  tools/pmu/target/interval-metasim/large/intervaltrace-0
```

The argument is the capture prefix, without `.json` or `-intervals.jsonl`.
The earlier host driver aggregates during capture and writes `PREFIX-intervals.jsonl`
plus `PREFIX.json` (the event catalog). Repeated executions of a block are combined
within each interval; the driver holds only the current interval's sparse block
map and pending PMU snapshots. It does not retain every dynamic BB record.

The processor exports this JSONL directly to `PREFIX-features/`; use `--output DIR`
to override. No C++ compiler is needed for live aggregates. Legacy captures containing
only record CSVs still work: a small C++17 adapter is built on first use (compiler
overridable with `CXX`). `--from-csv` explicitly recomputes from record CSVs when both
representations exist. Outputs are replaced only after the complete input validates.

Use `+interval-trace-csv` to additionally record BB, PMU, and epoch CSVs for debugging;
`+interval-trace-raw` independently enables the padded binary stream. Both are off
by default. When using FireSim Manager, also add those optional files to the workload's
`simulation_outputs` if they should be copied back. Default workload lists collect
only the interval JSONL and catalog, alongside the UART log.

During capture, the JSONL has a `.partial` suffix. The driver renames it only after
stream validation, draining, and successful file closes. Failed runs retain that
partial file for diagnosis; the exporter rejects it as an incomplete capture.
An empty but successfully completed capture produces an empty JSONL.

Interval aggregates preserve block weights, counter deltas, and IPC, but intentionally
lose dynamic BB order. The SPEC validator checks exact capture-edge marker placement
only when record CSVs are available; its `capture_edge_check` field reports whether
that check ran. Workload comparison, interval continuity, vector totals, and PMU
delta checks also run for aggregate-only captures.

## Outputs

| File | Contents |
| --- | --- |
| `bbv.txt` | SimPoint sparse vectors: instruction contributions per block |
| `bbtv.txt` | Same dimensions and interval rows, weighted by elapsed cycles |
| `blocks.csv` | One-based vector dimension IDs mapped to block identity |
| `intervals.csv` | Actual endpoints, counts, logical IPC/CPI, physical IPC, partial/reset flags, and vector row mapping |
| `rfv.csv` | Exact event deltas and deltas per elapsed cycle |
| `intervals.jsonl` | Exact integer aggregates plus logical IPC for every interval, including zero-cycle intervals and final tails |
| `manifest.json` | Input catalog, policies, source scope, and reconciled totals |

Sparse lines use `T:ID:WEIGHT :ID:WEIGHT ...`, matching NEMU's SimPoint convention.
Weights are **raw integers**, with no unit-sum normalization or learned scaling.
`intervals.csv.vector_row` is the zero-based line index shared by both vector
files. Both exports select nominal intervals with positive instruction and cycle
counts. A blank row index means the interval remains in the detailed outputs but
is omitted from both clustering exports. In particular:

- Final tails are always retained for accounting, even when empty or idle-only.
- Same-cycle nominal intervals remain visible with zero cycles. Their undefined
  IPC and per-cycle rates are blank, rather than infinity or a fabricated zero.
- Partial BBs inside an otherwise eligible nominal interval remain included and
  flagged; their counts are not silently dropped.

`rfv.csv` has one row per detailed interval, including tails. Each registered event
has `.delta` and `.per_cycle` columns. Raw deltas and the explicit
`physical_retirements` column remain available for computing selected per-instruction
metrics downstream. Raw PMU snapshots include the whole retirement cycle, so those
endpoints can differ from logical BB boundaries by younger retiring slots. Logical IPC is `instructions / cycles`;
physical IPC is reported separately. Zero denominators produce blank CSV rate fields.
Each `intervals.jsonl` entry also contains numeric `ipc = instructions / cycles`,
or JSON `null` when cycles are zero.
These are rates for the available core/tile catalog, not a claim to reproduce the
paper's complete RFV (uncore events and feature scaling are separate work).

## Accounting and identities

The adapter merges the CSVs by their shared `record` numbers, rejecting gaps,
duplicates, incorrect epochs, malformed columns, and contradictory flag fields.
The existing C++ stream decoder validates the reconstructed records using the
same checks as the driver: catalogs, baselines, snapshot sequencing, retirement
and cycle boundaries, and complete epoch shutdown.

A PMU snapshot may precede several BB records. The aggregator queues snapshots
and closes a nominal interval only when its terminating BB arrives. The final
snapshot is consumed at epoch end, after any final partial BB. It uses recorded
boundaries directly, retaining overshoot compensation rather than recalculating
fixed instruction windows.

- **BBV:** sum the actual instruction counts of all occurrences of a block.
- **BBTV:** sum each occurrence's recorded elapsed cycles, from its preceding BB
  boundary to its own boundary. Later BBs retiring in the same cycle get zero.
- **RFV:** subtract consecutive cumulative PMU snapshots modulo 2^64 within an
  epoch. A new baseline resets the subtraction history.
- **Tail idle time:** cycles between the final BB boundary and capture end are
  `unattributed_cycles`. No synthetic BB is created and no existing BB absorbs
  that time. `sum(BBTV) + unattributed_cycles == cycles` for every detailed interval.

A block key is `(start_pc, end_pc, privilege, partial_start, partial_end)`.
Partial fragments are kept distinct from complete blocks, even if their ranges
coincide. Trap, discontinuity, and reset flags are retained in interval `bb_flags`.
Within `intervals.jsonl`, each block entry is
`[start_pc, end_pc, privilege, partial_flags, instructions, cycles, executions]`;
`partial_flags` uses the wire bits 1 and 2. The two PCs are JSON integers; the
block mapping CSV renders them in hex.

Block IDs are deterministic for this ordered input and shared by BBV/BBTV. They
are local to the capture/source. The trace has no ASID or code build identity;
do not equate encounter IDs across captures or assume it distinguishes different
address spaces or code versions at the same virtual addresses.

Total IPC uses summed instructions divided by summed cycles, not a mean of
interval IPCs. The exported vectors omit final tails, so use the row mapping when
joining them with RFV or computing weights from actual interval lengths.

## Reusing the core in the driver

[`intervaltrace_aggregate.h`](../../generators/firechip/bridgestubs/src/main/cc/bridges/intervaltrace_aggregate.h)
has no simulator, DMA, CSV, or Python dependency. An `intervaltrace_aggregator`
accepts validated records through `consume(kind, flags, epoch, words)` and emits
an `intervaltrace_features` object through a synchronous callback. The driver and
offline adapter both feed it from the same validated decoder callback and share
JSON serialization through `intervaltrace_json.h`.

The offline `aggregate.cc` adapter consumes decimal event records from the Python
CSV reader and emits JSONL. CSV input, sparse-vector export, and rate formatting
are outside the aggregation core. Input is streamed, with only an interval's
sparse map and pending snapshot batch in the core. The exporter retains a global
block dictionary, not every dynamic BB record.

## Validation

```sh
python3 -m unittest discover -s tools/interval-trace -p 'test_*.py' -v
```

Tests cover PMU-before-BB ordering, repeated block accumulation, carried overshoot,
same-cycle snapshots, 64-bit counter wrap, reset baselines, partial/privilege
identities, final idle cycles, empty captures, deterministic reprocessing, zero
rate denominators, and invalid/truncated inputs without publishing new outputs.

The Large BOOM smoke capture produces 61 common vector rows, 190 block identities,
and 62 detailed intervals including the final tail. Totals reconcile to 61,631
instructions and 30,156 cycles (logical IPC about 2.04374).
