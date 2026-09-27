#!/usr/bin/env python3
"""Validate a completed SPEC 602.gcc_s test capture and export interval features.

Usage: check-spec-gcc.py RESULTS_JOB --spec-dir CPU2017_INSTALL
RESULTS_JOB contains uartlog, intervaltrace-0 capture files, and collected output/.
"""
import argparse
from collections import deque
import csv
import json
import os
from pathlib import Path
import subprocess
import sys


def check(job, spec, capture_from_boot=False):
    uart = (job / 'uartlog').read_text(errors='replace')
    assert 'PASS spec2017-gcc-test exit_code=0' in uart, 'target did not report PASS'
    assert 'FAIL spec2017-gcc-test' not in uart and '*** PASSED ***' in uart
    assert 'IntervalTrace intervaltrace-0: error:' not in uart
    output = job / 'output'
    assert (output / 'exitcode').read_text().strip() == '0'
    assert 'interval-submit: exit_code=0' in (output / 'gcc.out').read_text()
    # Use the comparison flags recorded by SPEC's runsetup for this input.
    assembly = 't1.opts-O3_-finline-limit_50000.s'
    with (job / 'specdiff.log').open('w') as log:
        subprocess.run([str(spec / 'bin/specperl'), str(spec / 'bin/harness/specdiff'),
                        '-m', '-l', '10', '--floatcompare', '--nonansupport',
                        str(spec / 'benchspec/CPU/502.gcc_r/data/test/output' / assembly),
                        str(output / assembly)], stdout=log, stderr=subprocess.STDOUT, check=True,
                        env=dict(os.environ, SPEC=str(spec)))
    prefix = job / 'intervaltrace-0'
    catalog = json.loads(prefix.with_suffix('.json').read_text())
    names = [e['name'] for e in catalog['events']]
    detailed = all((job / ('intervaltrace-0-' + kind + '.csv')).exists()
                   for kind in ('epochs', 'pmu', 'bb'))
    count = nominal = 0
    if detailed:
        with (job / 'intervaltrace-0-epochs.csv').open() as f:
            epochs = list(csv.DictReader(f))
        assert len(epochs) == 2 and [e['kind'] for e in epochs] == ['start', 'end'], epochs
        assert epochs[0]['nominal_interval'] == '1000000'
        assert all(e['epoch'] == '1' and e['flags'] == '0' for e in epochs)
        count = nominal = 0
        previous_record = 0
        with (job / 'intervaltrace-0-pmu.csv').open() as f:
            rows = csv.DictReader(f)
            assert rows.fieldnames[8:] == names
            for row in rows:
                count += 1
                assert row['epoch'] == '1'
                assert int(row['record']) > previous_record
                previous_record = int(row['record'])
                assert row['rob.instructions_retired'] == row['physical_retired']
                assert row['tile.cycles'] == row['cycle']
                if count == 1:
                    assert row['kind'] == 'baseline'
                nominal += row['kind'] == 'nominal'
            assert count >= 2 and row['kind'] == 'final'
    # The streaming processor/decoder checks global record sequence, endpoint
    # matching, BB/PMU totals, final records and vector alignment without loading
    # the entire raw BB capture into memory.
    processor = Path(__file__).with_name('process.py')
    subprocess.run([sys.executable, str(processor), str(prefix)], check=True)
    features = job / 'intervaltrace-0-features'
    totals = json.loads((features / 'manifest.json').read_text())['totals']
    assert totals['instructions'] > 0
    if detailed:
        assert totals['nominal_intervals'] == nominal
        assert totals['instructions'] == int(epochs[-1]['retired'])
    if detailed:
        # A marker may share its cycle with a preceding/following branch, so it
        # need not belong to the literal first/last BB. At most retire_width - 1
        # neighboring instructions may lie outside the marker on either edge.
        markers = json.loads((output / 'marker-pcs.json').read_text())
        width = catalog['retire_width']
        first = []
        last = deque(maxlen=width)
        blocks = 0
        with (job / 'intervaltrace-0-bb.csv').open() as f:
            for row in csv.DictReader(f):
                if len(first) < width:
                    first.append(row)
                last.append(row)
                blocks += 1
        if capture_from_boot:
            assert first and first[0]['privilege'] == '3', 'capture did not start in machine-mode boot'
        else:
            for edge, kind in [(first, 'start'), (reversed(last), 'end')]:
                outside = 0
                found = False
                for block in edge:
                    if outside >= width:
                        break
                    if block['privilege'] == '0' and any(
                            int(block['start_pc'], 16) <= pc <= int(block['end_pc'], 16)
                            for pc in markers[kind]):
                        found = True
                        break
                    outside += int(block['instructions'])
                assert found, ('marker missing at capture edge', kind, markers)
    else:
        # The host decoder checked packet order and complete epoch shutdown
        # before publishing this file. Dynamic edge order is deliberately not
        # retained in interval-only captures; exact marker-edge validation is
        # available with the opt-in record CSVs.
        blocks = nominal = 0
        with (features / 'intervals.jsonl').open() as stream:
            for line in stream:
                interval = json.loads(line)
                assert interval['epoch'] == 1 and interval['nominal_interval'] == 1000000
                assert interval['flags'] == (16 if interval['final'] else 0)
                nominal += not interval['final']
                blocks += sum(block[6] for block in interval['blocks'])
                for event, endpoint in [('rob.instructions_retired', 'physical_retirements'),
                                        ('tile.cycles', 'cycles')]:
                    assert interval['counter_deltas'][names.index(event)] == interval[endpoint]
        assert interval['final'] and totals['nominal_intervals'] == nominal
        count = totals['intervals'] + 1  # Add the epoch baseline snapshot.
    summary = dict(passed=True, benchmark='602.gcc_s', input='test', nominal_interval=1000000,
                   capture_scope='boot-through-shutdown' if capture_from_boot else 'launcher-markers',
                   capture_edge_check='exact-records' if detailed else 'not-retained',
                   pmu_snapshots=count, bb_records=blocks, capture=str(job),
                   features=str(features), **totals)
    (job / 'validation.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('results_job', type=Path)
    parser.add_argument('--spec-dir', required=True, type=Path)
    parser.add_argument('--capture-from-boot', action='store_true',
                        help='validate ungated capture, including Linux boot and shutdown')
    args = parser.parse_args()
    check(args.results_job.resolve(), args.spec_dir.resolve(), args.capture_from_boot)
