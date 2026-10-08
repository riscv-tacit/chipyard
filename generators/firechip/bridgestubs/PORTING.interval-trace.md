# Interval trace dependencies and porting

The validated integration is based on Chipyard `dbc29c40`, BOOM `53b0d34c`,
and the Chisel 6 / FIRRTL2 build in this checkout. The interval bridge and driver
do not import TACIT or TraceDoctor types, consume their tokens, or instantiate
those bridges. This is not a claim that the patches apply or compile unchanged
on an arbitrary upstream Chipyard release; that needs a separate baseline test.

## Components

- `generators/pmu` is the PMU library: the per-tile event registry, the event
  list, `CanHavePMUEvents`/`PMUEvents.bore()` for consumers outside the tile, and
  the optional tile PMU with its TileLink register interface.
- The BOOM commit registers events; it needs the PMU projects on its classpath.
  The accompanying Chipyard commit adds that dependency.
- The bridge's increment ports are unsized; FIRRTL width inference sizes them
  from the tile's increments, and the FPGA-side widget from the target channels.
- The interval interface uses the existing architectural `TileTraceIO` and its
  conversion helpers. It does not require a TracerV bridge instance. The widget
  uses FireSim `HostPort`, `BridgeModule`, and `StreamToHostCPU`; the C++ driver
  uses the standard bridge/stream interfaces.
- Chipyard IO/harness binders connect retirement and raw PMU increments. The tile
  PMU is optional. BOOM retirement is observed one accepted cycle after the
  PMU events, so the bridge alignment is part of the integration contract.
- Baremetal-IDE bindings and the FireMarshal/Speckle marker workloads are optional
  software consumers, separate from interval hardware and host accounting.

## BOOM fork adaptation

Two newly added PMU predicates observe pre-existing TACIT backpressure:
`io.traceStall` in `core.scala` and `io.trace_stall` in `rob.scala`. A baseline
without that facility should omit those terms (equivalent to an inactive stall),
while retaining CSR-stall, reset, flush and recovery qualification. Do not bring
in the TACIT encoder solely to satisfy these references. Keep the terms in this
fork: removing them here would change counter semantics when tracing stalls BOOM.

Some patch context in `tile.scala` is adjacent to the existing trace encoders.
Build the registry after all producer modules elaborate and before the tile PMU;
the adjacent encoder code is not a PMU dependency.
Verify the selected BOOM revision's architectural retirement, original instruction
bits, PC and exception semantics before reusing the binder's one-cycle latency.

## Repository and platform boundaries

Cherry-pick the BOOM, Baremetal-IDE, Speckle and FireMarshal changes in their own
repositories as needed. A parent gitlink update does not transfer submodule
commits. Apply root feature commits in order and resolve gitlinks against the
chosen recipient baseline, rather than replacing unrelated upstream revisions.
Publish the referenced submodule objects before publishing a parent revision.

The separate FireSim baseline pin includes upstream merges and TACIT-related
history. It records the tested checkout; it is not an interval dependency bundle.
The additional manager-key configuration commit is independent of the bridge.
For AWS F2, use a functioning stream transport: the validated recipe uses PCIM,
PCIe bus mastering and hugepage setup. Those platform facilities need to exist
on the recipient baseline, but do not belong in the interval packet implementation.
Other FireSim platforms can use their own supported stream transport.

## Validation boundary

The PMU unit, BOOM elaboration and tile PMU software tests, interval RTL/driver
tests, and metasim exercise this integrated tree.
Porting to a clean upstream tree still requires compiling its target and widget,
checking retirement/PMU alignment, and running smoke before collecting workloads.
The driver emits interval JSONL version 2 and an append/flush block catalog.
