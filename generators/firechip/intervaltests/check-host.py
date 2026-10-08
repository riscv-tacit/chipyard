#!/usr/bin/env python3
"""Compile the stream driver and validate both synthetic and RTL-generated streams.
Run the Scala datapath/interface tests and check-legacy.py first.
"""
from pathlib import Path
import csv
import json
import sys
import struct
import os
import shlex
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "generators/firechip/intervaltests/target/host"
OUT.mkdir(parents=True, exist_ok=True)
bridges = ROOT / "generators/firechip/bridgestubs/src/main/cc/bridges"
midas = ROOT / "sims/firesim/sim/midas/src/main/cc"
legacy = ROOT / "generators/firechip/intervaltests/target/legacy"
cxx = shlex.split(os.environ.get("CXX", "g++"))
flags = ["-std=c++17", "-Wall", "-Wextra", "-Werror"]

def run(args):
    subprocess.run(args, cwd=ROOT, check=True)

exe = OUT / "intervaltrace_decoder_test"
run(cxx + flags + ["-I", str(bridges), str(bridges / "test/standalone/intervaltrace_decoder_test.cc"), "-o", str(exe)])
run([str(exe)])
for count in (1, 2, 18, 19):
    base = ROOT / f"generators/firechip/intervaltests/target/rtl-{count}"
    run([str(exe), str(base.with_suffix(".bin")), str(count), base.with_suffix(".catalog").read_text()])
includes = ["-I", str(midas), "-I", str(bridges.parent), "-I", str(legacy)]
run(cxx + flags + includes + ["-c", str(bridges / "intervaltrace.cc"), "-o", str(OUT / "intervaltrace.o")])
header_check = OUT / "generated-header-check.cc"
header_check.write_text('''#include "core/widget_registry.h"
#define GET_INCLUDES
#define GET_SUBSTRUCT_CHECKS
#include "intervaltrace_generated.h"
#undef GET_INCLUDES
#undef GET_SUBSTRUCT_CHECKS
void check_constructor(simif_t &simif, widget_registry_t &registry, const std::vector<std::string> &args) {
#define GET_BRIDGE_CONSTRUCTOR
#include "intervaltrace_generated.h"
}
''')
run(cxx + flags + includes + ["-fsyntax-only", str(header_check)])
driver_exe = OUT / "intervaltrace_driver_test"
run(cxx + flags + includes + ["-I", str(bridges),
    str(bridges / "test/standalone/intervaltrace_driver_test.cc"),
    str(bridges / "intervaltrace.cc"),
    *[str(midas / "core" / source) for source in
      ("simif.cc", "widget.cc", "bridge_driver.cc", "stream_engine.cc")], "-o", str(driver_exe)])
def check_csv(output, binary, counters):
    names = ['event,"zero"'] + [f'event_{i}' for i in range(1, counters)]
    rows = {}
    for suffix, kind in [('bb', 1), ('pmu', 2), ('epochs', 3)]:
        with (Path(output) / f'normal-7-{suffix}.csv').open(newline='') as stream:
            for row in csv.DictReader(stream):
                record = int(row.pop('record'))
                assert record not in rows and None not in row
                rows[record] = (kind, row)
        assert (Path(output) / f'normal-7-{suffix}.csv').read_bytes() == (Path(output) / f'raw-7-{suffix}.csv').read_bytes()
    offset = 0
    record = 0
    epoch = 0
    while offset < len(binary):
        header, = struct.unpack_from('<Q', binary, offset)
        kind, flags, count = header & 255, (header >> 16) & 65535, header >> 32
        w = struct.unpack_from('<' + 'Q' * count, binary, offset + 8)
        offset += ((count + 8) // 8) * 64
        record += 1
        if kind == 3:
            epoch = w[0]
        expected = dict(epoch=epoch, flags=flags)
        if kind == 1:
            expected.update(start_pc=hex(w[0]), end_pc=hex(w[1]), instructions=w[2], elapsed_cycles=w[3],
                            terminate_interval=int(bool(flags & 1)), partial_start=int(bool(flags & 2)),
                            partial_end=int(bool(flags & 4)), privilege=flags >> 13)
        elif kind == 2:
            expected.update(snapshot_id=w[0], kind='baseline' if flags & 8 else 'final' if flags & 16 else 'nominal',
                            logical_retired=w[1], cycle=w[2], physical_retired=w[3])
            expected.update(zip(names, w[4:]))
        else:
            expected.update(kind='start' if kind == 3 else 'end', nominal_interval='', counter_count='',
                            catalog_id='', retired='', cycles='', final_snapshot_id='')
            if kind == 3:
                expected.update(nominal_interval=w[1], counter_count=w[2], catalog_id=w[3])
            else:
                expected.update(retired=w[1], cycles=w[2], final_snapshot_id=w[3])
        csv_kind, actual = rows.pop(record)
        assert csv_kind == min(kind, 3)
        assert actual == {key: str(value) for key, value in expected.items()}, (record, actual, expected)
    assert not rows
    print(f'PASS: all {record} CSV records preserve packet fields and ordering ({counters} counters)')

P = 3 << 13  # Machine-mode privilege in BB flags bits 15:13.
MAX = (1 << 64) - 1


def records():
    """Synthetic edge cases: wraparound, reset epochs, repeated blocks, same-cycle
    snapshots, overshoot and an idle tail. Two counters; catalog ID 99."""
    return [
        (3, 0, [1, 4, 2, 99]),
        (2, 8, [0, 0, 0, 0, MAX - 2, 0]),
        # Two pending snapshots, including a duplicate physical/cycle endpoint.
        (2, 0, [1, 5, 10, 6, 2, 6]),
        (2, 0, [2, 6, 10, 6, 2, 6]),
        (1, P, [0x100, 0x102, 2, 4]),
        (1, P | 1, [0x100, 0x102, 3, 6]),
        (1, P | 1, [0x200, 0x200, 1, 0]),
        (2, 16 | 32, [3, 8, 15, 8, 4, 8]),
        (1, P | 2 | 4 | 32 | 64, [0x300, 0x304, 2, 3]),
        (4, 32, [1, 8, 15, 3]),
        # Reset establishes a new baseline, not a delta from the old epoch.
        (3, 0, [2, 4, 2, 99]),
        (2, 8, [0, 0, 0, 0, 7, 0]),
        (2, 0, [1, 4, 8, 4, 9, 4]),
        (1, P | 1, [0x100, 0x102, 4, 8]),
        (2, 16, [2, 4, 11, 4, 10, 4]),
        (4, 0, [2, 4, 11, 2]),
    ]


def encode(stream):
    encoded = bytearray()
    for kind, flags, words in stream:
        packet = struct.pack('<' + 'Q' * (len(words) + 1),
                             kind | (1 << 8) | (flags << 16) | (len(words) << 32), *words)
        encoded.extend(packet + bytes((-len(packet)) % 64))
    return bytes(encoded)


def decode(binary):
    offset = 0
    while offset < len(binary):
        header, = struct.unpack_from('<Q', binary, offset)
        kind, flags, count = header & 255, (header >> 16) & 65535, header >> 32
        yield kind, flags, struct.unpack_from('<' + 'Q' * count, binary, offset + 8)
        offset += ((count + 8) // 8) * 64


def write_stream(name, stream):
    base = OUT / name
    base.with_suffix('.bin').write_bytes(encode(stream))
    base.with_suffix('.catalog').write_text('99')
    return base


def check_intervals(output, binary):
    """Check the driver's interval JSONL version 2 output in every mode, and its
    aggregates against the raw packets. Returns (definitions, intervals)."""
    reference = None
    for name in ('default', 'raw-only', 'normal', 'raw'):
        catalog_text = (Path(output) / f'{name}-7-blocks.csv').read_text()
        lines = (Path(output) / f'{name}-7-intervals.jsonl').read_text().splitlines()
        if reference is None:
            reference = (catalog_text, lines)
        assert (catalog_text, lines) == reference, f'{name} output differs from default'
    with (Path(output) / 'default-7-blocks.csv').open() as stream:
        definitions = {int(row['id']): (int(row['start_pc'], 16), int(row['end_pc'], 16), int(row['privilege']),
                                        int(row['partial_start']), int(row['partial_end']))
                       for row in csv.DictReader(stream)}
    assert list(definitions) == list(range(1, len(definitions) + 1))
    intervals = [json.loads(line) for line in reference[1]]
    for f in intervals:
        assert f['interval_format_version'] == 2
        assert all(len(block) == 4 and block[0] in definitions for block in f['blocks'])
        assert sum(block[1] for block in f['blocks']) == f['instructions']
        assert sum(block[2] for block in f['blocks']) == f['attributed_cycles']
        assert f['attributed_cycles'] + f['unattributed_cycles'] == f['cycles']
    # Independent of the aggregator: per-epoch BB instructions, nominal snapshots
    # and distinct block identities, straight from the packets.
    bb_instructions, nominal, identities, epoch = {}, {}, set(), 0
    for kind, flags, w in decode(binary):
        if kind == 3:
            epoch = w[0]
        elif kind == 1:
            bb_instructions[epoch] = bb_instructions.get(epoch, 0) + w[2]
            identities.add((w[0], w[1], flags >> 13, int(bool(flags & 2)), int(bool(flags & 4))))
        elif kind == 2 and not flags & (8 | 16):
            nominal[epoch] = nominal.get(epoch, 0) + 1
    for e in set(bb_instructions) | {f['epoch'] for f in intervals}:
        assert sum(f['instructions'] for f in intervals if f['epoch'] == e) == bb_instructions.get(e, 0)
        assert sum(not f['final'] for f in intervals if f['epoch'] == e) == nominal.get(e, 0)
    assert set(definitions.values()) == identities
    return definitions, intervals


synthetic = write_stream('aggregate-edge-cases', records())
for count, base in [(n, ROOT / f"generators/firechip/intervaltests/target/rtl-{n}") for n in (1, 19)] + [(2, synthetic)]:
    with tempfile.TemporaryDirectory(prefix="driver-", dir=OUT) as output:
        run([str(driver_exe), output, str(base.with_suffix(".bin")), str(count),
             base.with_suffix(".catalog").read_text().strip()])
        check_csv(output, base.with_suffix(".bin").read_bytes(), count)
        definitions, intervals = check_intervals(output, base.with_suffix(".bin").read_bytes())
        if base == synthetic:
            # Expected aggregation results for the synthetic edge cases.
            assert len(intervals) == 5 and sum(not f['final'] for f in intervals) == 3
            assert [f['counter_deltas'] for f in intervals] == [[5, 6], [0, 0], [2, 2], [2, 4], [1, 0]]
            assert tuple(sum(f[k] for f in intervals) for k in
                         ('instructions', 'physical_retirements', 'cycles', 'attributed_cycles',
                          'unattributed_cycles')) == (12, 12, 26, 21, 5)
            assert [[definitions[b[0]]] + b[1:] for b in intervals[0]['blocks']] == [[(0x100, 0x102, 3, 0, 0), 5, 10, 2]]
            assert [f['ipc'] for f in (intervals[0], intervals[1], intervals[4])] == [0.5, None, 0]
            assert intervals[1]['cycles'] == 0 and intervals[2]['partial_blocks'] == 1
            assert definitions[intervals[2]['blocks'][0][0]][3:] == (1, 1)
            assert intervals[4]['blocks'] == [] and intervals[4]['unattributed_cycles'] == 3
        print(f'PASS: interval JSONL version 2 agrees across output modes and with the raw packets ({count} counters)')

# The same PC range with a different privilege or partial flag is a different block.
aliasing = write_stream('aggregate-identities', [
    (3, 0, [1, 4, 2, 99]), (2, 8, [0, 0, 0, 0, 0, 0]), (2, 0, [1, 4, 4, 4, 0, 4]),
    (1, P, [0x100, 0x100, 1, 1]), (1, P | 2, [0x100, 0x100, 1, 1]),
    (1, 0, [0x100, 0x100, 1, 1]), (1, P | 1, [0x100, 0x100, 1, 1]),
    (2, 16, [2, 4, 4, 4, 0, 4]), (4, 0, [1, 4, 4, 2])])
with tempfile.TemporaryDirectory(prefix="driver-", dir=OUT) as output:
    run([str(driver_exe), output, str(aliasing.with_suffix(".bin")), "2", "99"])
    definitions, _ = check_intervals(output, aliasing.with_suffix(".bin").read_bytes())
    assert len(definitions) == 3
print('PASS: block identities include privilege and partial flags')

# The stream decoder rejects invalid streams: a missing epoch end, a dropped
# snapshot, BB cycles that disagree with the snapshots, and a catalog mismatch.
# (The driver test above checks that rejected streams publish no intervals.)
invalid = [records()[:-1], records()[:2] + records()[3:]]
bad = records(); bad[4] = (1, P, [0x100, 0x102, 2, 3]); invalid.append(bad)
bad = records(); bad[0] = (3, 0, [1, 4, 2, 100]); invalid.append(bad)
for index, stream in enumerate(invalid):
    base = write_stream(f'aggregate-invalid-{index}', stream)
    result = subprocess.run([str(exe), str(base.with_suffix('.bin')), '2', '99'], cwd=ROOT, timeout=60,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert result.returncode != 0, f'invalid stream {index} was accepted'
print(f'PASS: {len(invalid)} invalid streams rejected')
print("PASS: driver compilation and generated constructor/MMIO layout")
