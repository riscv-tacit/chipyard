# Native PMU MMIO ABI v1

Date: 2026-09-16\
Status: implemented for BOOM v3 in the Chipyard Chisel 6 build.\
Related: [producer API](boom-pmu-api.md), [counter catalog](boom-pmu-counter-catalog.md).

## Placement and address discovery

`WithBoomPMU` enables the native bank and its MMIO interface. Each tile owns a
4 KiB region at `BoomPMUBaseAddress + tileId * 0x1000`; the default base is
`0x40000000`. `new WithBoomPMU(baseAddress = ...)` changes the base. Addresses must
be 4 KiB aligned and fit the platform map; normal Diplomacy address-overlap checks
apply. PMU-disabled configurations contain neither a bank nor an MMIO node.

The device tree advertises `compatible = "ucbbar,pmu-v1"`, the region, and
`pmu,source = "boom_tile_N"`. Match that source to the generated `.pmu.json`
manifest for event IDs, names, units, and definitions. IDs are registration-order
indices and may change with the design. Generated `.regmap.json` artifacts also
name every counter field.

The optional `pmu_tilelink` project depends on Rocket Chip and the standalone
PMU library. Rocket Chip does not depend on PMU. Create `PMUTileLink` in the owning
LazyModule and negotiate its node. In the owner's module body after all producers:

```scala
val events = registry.build()
val nativePMU = TilePMU.build(events)
mmio.attach(nativePMU)
```

Finalization creates no counters. The separate native attachment supplies the
MMIO-visible hardware; the adapter does not finalize an early lazy child.

BOOM connects the node through its existing local slave bus. The register mapper
and bank share the tile clock/reset; the existing TileLink crossing transports
transactions into that domain. No control pulses or snapshot buses independently
cross clock domains. There is no separate asynchronous attachment API in v1.

## Register map

All offsets are relative to the region base. Register slots and counter entries
are 8 bytes apart. The adapter supports both 32- and 64-bit TileLink register buses.

| Offset | Register | Access | Contents |
| --- | --- | --- | --- |
| `0x000` | CONTROL | RW | Bit 0: enable. Other bits read zero and ignore writes. |
| `0x008` | COMMAND | W1 command / R0 | Bit 0: clear; bit 1: snapshot. Writing both captures then clears. Zero and reserved bits have no effect. |
| `0x010` | STATUS | RO | Bit 0: enabled; bit 1: snapshot valid. |
| `0x018` | INFO | RO | Bits 15:0: ABI version (1); 31:16: counter count; 47:32: counter width; 63:48: zero. |
| `0x100 + 8*i` | COUNTER[i] | RO | Captured value for manifest event ID i; low 32 bits at +0, high 32 bits at +4. |

Undefined offsets read zero and ignore writes; writes to read-only fields are
ignored. Byte enables qualify each field. Only a write covering COMMAND's low
byte can issue either command. The counter window exposes the snapshot, not live
values. Counters narrower than 64 bits are zero-extended; this adapter requires
width <= 64 and at most 480 counters. BOOM uses 19 counters of 64 bits each.

C constants are in [pmu_mmio.h](../generators/pmu/software/pmu_mmio.h).

## Controls and exact edge semantics

The native hardware interface has independent level `enable` and pulse `clear`
and `snapshot` inputs. MMIO stores enable in a register initialized to false and
converts command writes into one-cycle pulses. COMMAND contains no persistent bits.

For a command executed at observation edge E, let:

```text
next[i] = (running[i] + (enabled ? increment[i] : 0)) modulo 2^counterBits
```

At that edge:

- If snapshot is requested, capture all `next[i]` values together and set valid.
- If clear is requested, zero all running counters; otherwise store `next[i]`.
- Clear never changes enable or the previously captured values/valid flag.
- Without snapshot, captured values remain stable while running counters change.
- Hardware reset takes priority: zero both banks, invalidate the snapshot, and
  reset the MMIO enable register to false.

Thus snapshot-and-clear includes E's enabled increment in the closing window and
starts the next window after E. Clear-only discards that edge's increment.
Snapshot while disabled captures the paused totals without an increment.

An enable write updates the stored level on its execution edge. The counters use
the previous level on that edge and the new level on following edges. Enabling
does not clear; disabling does not take a snapshot. All counters, including
cycles, share enable so their native measurement windows agree.

The adapter queues requests. Commands execute once on the local register mapper's
write-response handshake, not merely while request valid is asserted. Request or
response backpressure cannot repeat a command. Effects are visible immediately
after that edge; subsequent ordered reads see the completed snapshot. No software
busy polling is required. STATUS.valid remains set until hardware reset; it is
not a new-snapshot completion toggle.

## Software sequence

Use the platform's ordered MMIO accessors and I/O barriers. On RISC-V bare metal,
`fence iorw, iorw` can enforce ordering between the command and subsequent reads;
volatile alone is not a processor I/O-ordering guarantee.

1. Read INFO and check the ABI version, count, and width.
2. While disabled, write `CLEAR` to start from zero.
3. Write `CONTROL = ENABLE` to start accumulating.
4. At an observation boundary, write `COMMAND = SNAPSHOT`, or
   `SNAPSHOT | CLEAR` for consecutive delta windows.
5. Order subsequent reads after the command, then read COUNTER[0..count-1].
6. Write `CONTROL = 0` to pause. Snapshot separately if a final sample is needed.

A 32-bit reader may read each value as two words because snapshot storage is
stable. Software must serialize commands from multiple readers of the same bank;
a concurrent new snapshot or hardware reset can replace the image during a read.
A 64-bit register bus also permits aligned 64-bit reads. Counters wrap; v1 has no
native overflow interrupt or saturation flag.

## Relationship to tracing

These controls affect only the native bank. Registered increment wires and event
annotations remain unchanged by native enable/clear/snapshot. The future FireSim
bridge will own its capture controls and independent cumulative counter state. Native
snapshots are not the bridge's packed 512-bit records.

## Validation

- `python3 tools/pmu/run-tests.py`: compiler checks, 4,096 native RTL cycles covering
  independent enable/reset/clear, inclusive snapshots, atomic capture-and-clear,
  snapshot retention, and wrapping; also the BOOM event predicate tests.
- After `sbt 'chipyard/compile'`, `python3 tools/pmu/test-mmio.py`: actual TileLink
  adapters at both bus widths, with an independent software oracle. Checks byte
  masks, zero/reserved writes, read-only fields, request and response backpressure,
  queued commands, coherent split reads, inclusive snapshots, enable, reset, and
  64-bit wrapping.
- `python3 tools/pmu/test-boom.py`: enabled one-wide/two-tile and two-wide designs,
  plus disabled BOOM; checks catalogs and per-tile MMIO discovery artifacts.

- `python3 tools/pmu/test-software.py`: complete one-wide BOOM running
  Baremetal-IDE's native MMIO test. Verified 2026-09-21: 207 target-side checks
  pass with normal simulator seeds 1 and 2; a debug run also passes. Includes
  INFO/catalog checks, fixed-loop retirement accounting, enable/disable,
  stable captured values, clear/capture ordering, and split/native 64-bit reads.
  Platform addresses and event IDs come from the matching elaboration's JSON.

See [software integration](../generators/pmu/software/README.md) for the generated
X-macro API and [test results](../tools/pmu/README.md#full-core-native-software-check)
for limitations, including an unresolved early hang in an unseeded simulation.
No FireSim trace implementation is exercised by this software test.
