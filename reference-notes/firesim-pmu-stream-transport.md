# FireSim PMU stream transport: independent bridge and multi-beat records

Date: 2026-09-21\
Status: repository inspection and revised design recommendation; no new bridge implemented.

## Finding

TraceDoctor is not a dependency of the PMU/retirement measurement path. Its
one-target-payload-to-one-stream-beat mapping is an implementation restriction,
not a FireSim/XDMA record-size limit. Build a dedicated bridge and driver on the
shared FireSim bridge-stream interfaces. The 512-bit limit applies to each beat;
a logical observation record can occupy several beats.

The [2026-09-21 packet plan](firesim-bb-pmu-packet-plan.md) now specifies the
initial format: one padded 512-bit BB packet, a PMU packet padded to a whole
number of beats, and ordinary PMU beat priority after a continuously valid
wide-to-512 serializer. Counters are uniform 64-bit cumulative values; the host computes
deltas. Earlier compact saturating fields are deferred, not required for v1.

## Existing examples in this checkout

- [PrintBridge](../sims/firesim/sim/midas/src/main/scala/midas/widgets/PrintBridge.scala)
  lines 153-174 use `junctions.MultiWidthFifo` to pack narrow tokens or serialize
  tokens wider than the DMA beat. Record construction and width adaptation are
  in the FPGA bridge, not the target producer. Its particular format rounds
  record widths to a power of two; our packet protocol need not copy that choice.
- [MultiWidthFifo](../sims/firesim/sim/midas/src/main/scala/junctions/MultiWidthFifo.scala)
  lines 20-54 implement wide-to-narrow storage and dequeue. Widths must divide
  evenly and storage depth must be compatible. Padding to a supported width or
  a simple beat-index serializer are possible implementation choices; inspect
  pointer/depth constraints before reusing it for arbitrary beat counts.
- [CospikeBridge](../generators/firechip/goldengateimplementations/src/main/scala/CospikeBridge.scala)
  lines 95-140 divide a retirement group into stream-sized groups and emit them
  over multiple FPGA cycles, consuming the target token only when finished.
  Individual instruction records must fit its chosen slots; this is an example
  of one target observation producing multiple beats, not an arbitrary packet
  library.
- [TracerVBridge](../generators/firechip/goldengateimplementations/src/main/scala/TracerVBridge.scala)
  lines 154-214 similarly group retirement traces and sequence multiple beats.
- [TraceDoctorBridge](../generators/firechip/goldengateimplementations/src/main/scala/TraceDoctorBridge.scala)
  explicitly requires the trace payload to fit one stream beat. Its comment
  reports timing problems with an earlier multi-beat prototype; that is not a
  transport prohibition, nor a measurement of our proposed bridge.

These are source-level findings. They do not validate timing, resource usage,
or FPGA operation of a new PMU bridge.

## Shared transport and XDMA

[StreamToHostCPU](../sims/firesim/sim/midas/src/main/scala/midas/widgets/UsesBridgeStreams.scala)
provides a backpressured 512-bit stream. The interface is a Decoupled UInt, with
no generic packet-end field. Bridges/drivers own width adaptation, record
framing, and multiplexing of logical record types onto their stream.

The shared [CPU-managed stream driver](../sims/firesim/sim/midas/src/main/cc/bridges/cpu_managed_stream.cc)
reads batches of available beats. Its pull interface requires beat-aligned read
sizes, not one read per logical observation. A DMA read may contain several
records or only part of one; the decoder must preserve partial-record state.

For a concrete XDMA-backed path, the
[Alveo U250 backend](../sims/firesim/sim/midas/src/main/cc/simif_xilinx_alveo_u250.cc)
opens `/dev/xdma*_c2h_0` and implements `cpu_managed_axi4_read` using `pread` on
that descriptor. The bridge uses the common stream API rather than implementing
XDMA transactions itself. The physical backend depends on the selected platform;
not every platform in this checkout uses XDMA.

## Selected bridge protocol

The [BB/PMU packet plan](firesim-bb-pmu-packet-plan.md) is the authoritative
framing and arbitration proposal. The target still supplies narrow increments,
retirement observations, and boundary/reset/epoch controls. The independent
bridge bank now uses uniform 64-bit cumulative counters and coherent snapshots;
software computes deltas. There is no mandatory compact-field range analysis
or native full-counter export for this format.

The bridge has two output paths: a BB padder producing one beat, and a complete
elaboration-fixed wide PMU packet with static padding and wide-to-512 serialization.
The serializer holds valid continuously through the last beat's transfer, so
ordinary PMU beat priority preserves packet contiguity. No narrow-word emitter
or additional multi-beat arbitration lock is required.

Use endpoint metadata to associate snapshots with BBs: priority can put a PMU
packet before the BB that closes the interval. Correct timing comes from atomic
target-cycle capture rather than output arbitration. The initial scheduler
finishes each finite target-cycle output batch before accepting the next one.

## Implications and remaining checks

- Report total bits, beat count, padding, and estimated traffic. Each BB is one
  beat; PMU records can exceed one beat. Any resource-based maximum record size
  must be an explicit bridge parameter.
- Host packetization and serialization stay in the bridge. No TraceDoctor token
  producer, wire format, worker registration, or decoder is required. Its existing
  code remains a comparison/reference only.
- Multi-beat output does not eliminate target-to-bridge wiring costs. Narrow
  increment channels still need physical routing, and bridge accumulators,
  snapshot storage, and serializer muxes still consume FPGA resources.
- Validate a record wider than one beat, stalled output in the middle of a
  record, consecutive boundaries, simultaneous record types, split DMA reads,
  and final draining. Compare target measurements under different host stalls.
- Confirm actual Golden Gate channel alignment and the chosen platform's stream
  engine in a prototype before drawing throughput or timing conclusions.
