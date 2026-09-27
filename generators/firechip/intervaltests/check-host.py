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

# Exercise wraparound, reset epochs, repeated blocks, same-cycle snapshots,
# overshoot and idle tails through the actual driver as well as the offline core.
sys.path.insert(0, str(ROOT / 'tools/interval-trace'))
from test_process import records
synthetic = OUT / 'aggregate-edge-cases'
encoded = bytearray()
for kind, flags, words in records():
    packet = struct.pack('<' + 'Q' * (len(words) + 1),
                         kind | (1 << 8) | (flags << 16) | (len(words) << 32), *words)
    encoded.extend(packet + bytes((-len(packet)) % 64))
synthetic.with_suffix('.bin').write_bytes(encoded)
synthetic.with_suffix('.catalog').write_text('99')
for count, base in [(n, ROOT / f"generators/firechip/intervaltests/target/rtl-{n}") for n in (1, 19)] + [(2, synthetic)]:
    with tempfile.TemporaryDirectory(prefix="driver-", dir=OUT) as output:
        run([str(driver_exe), output, str(base.with_suffix(".bin")), str(count),
             base.with_suffix(".catalog").read_text().strip()])
        check_csv(output, base.with_suffix(".bin").read_bytes(), count)
        sys.path.insert(0, str(ROOT / 'tools/interval-trace'))
        import process
        catalog = dict(format_version=1, counter_bits=64,
                       catalog_id=base.with_suffix('.catalog').read_text().strip(),
                       events=[dict(id=i, name='event,"zero"' if i == 0 else f'event_{i}')
                               for i in range(count)])
        captures = Path(output)
        prefix = captures / 'normal-7'
        prefix.with_suffix('.json').write_text(json.dumps(catalog))
        process.process(prefix, captures / 'offline-features', from_csv=True)
        expected = [json.loads(line) for line in
                    (captures / 'offline-features/intervals.jsonl').read_text().splitlines()]
        expected_catalog = (captures / 'offline-features/blocks.csv').read_text()
        for name in ('default', 'raw-only', 'normal', 'raw'):
            definition_file = captures / f'{name}-7-blocks.csv'
            assert definition_file.read_text() == expected_catalog
            with definition_file.open() as stream:
                definitions = list(csv.DictReader(stream))
            assert [int(row['id']) for row in definitions] == list(range(1, len(definitions) + 1))
            keys = {int(row['id']): [int(row['start_pc'], 16), int(row['end_pc'], 16),
                    int(row['privilege']), 2 * int(row['partial_start']) + 4 * int(row['partial_end'])]
                    for row in definitions}
            actual = []
            for line in (captures / f'{name}-7-intervals.jsonl').read_text().splitlines():
                interval = json.loads(line)
                assert interval.pop('interval_format_version') == 2
                assert all(len(block) == 4 for block in interval['blocks'])
                interval['blocks'] = [keys[block[0]] + block[1:] for block in interval['blocks']]
                actual.append(interval)
            assert actual == expected, name
        print(f'PASS: compact driver output reconstructs legacy measurements exactly; catalog IDs stable across epochs ({count} counters)')
print("PASS: driver compilation and generated constructor/MMIO layout")
