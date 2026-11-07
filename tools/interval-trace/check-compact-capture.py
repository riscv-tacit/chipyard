#!/usr/bin/env python3
"""Validate an interval capture in JSONL version 2 or 3 and its workload results.

Version 3 equals version 2, except that with a stall event in the catalog every
cycle field excludes stall cycles; tile.cycles' delta is then cycles plus the
stall event's delta.
"""
import argparse
import csv
import json
import math
import os
from pathlib import Path
import re
import subprocess


def check_capture(prefix, interval):
    """Validate one capture's catalog, block definitions and intervals (version 2 or 3).

    Returns (names, definitions, epochs, totals). Requires a reset-free capture
    whose intervals all have the given nominal length.
    """
    assert not Path(str(prefix) + '-intervals.jsonl.partial').exists(), 'incomplete capture'
    catalog = json.loads(prefix.with_suffix('.json').read_text())
    names = [event['name'] for event in catalog['events']]
    stall = catalog.get('stall_event')
    assert stall is None or stall in names, 'stall event outside the catalog'
    with Path(str(prefix) + '-blocks.csv').open() as stream:
        rows = csv.DictReader(stream)
        assert rows.fieldnames == ['id', 'start_pc', 'end_pc', 'privilege', 'partial_start', 'partial_end']
        definitions = {}
        for row in rows:
            index = int(row['id'])
            assert index == len(definitions) + 1
            key = (int(row['start_pc'], 16), int(row['end_pc'], 16), int(row['privilege']),
                   int(row['partial_start']), int(row['partial_end']))
            assert 0 <= key[0] <= key[1] < 2**64 and key[2] in (0, 1, 3)
            assert key[3] in (0, 1) and key[4] in (0, 1)
            definitions[index] = key
    assert len(set(definitions.values())) == len(definitions), 'duplicate block identities'
    totals = dict(intervals=0, nominal_intervals=0, instructions=0, cycles=0, stall_cycles=0, bb_records=0)
    version = None
    previous = None
    epochs = {}
    referenced = set()
    with Path(str(prefix) + '-intervals.jsonl').open() as stream:
        for line in stream:
            f = json.loads(line)
            assert f['interval_format_version'] in (2, 3) and f['nominal_interval'] == interval
            assert version in (None, f['interval_format_version']), 'mixed interval format versions'
            version = f['interval_format_version']
            assert stall is None or version == 3, 'a stall event requires interval format version 3'
            stalled = f['counter_deltas'][names.index(stall)] if stall is not None else 0
            assert len(f['counter_deltas']) == len(names)
            new_epoch = previous is None or previous['final']
            if new_epoch:
                assert f['epoch'] == (previous['epoch'] + 1 if previous else 1)
                assert f['snapshot_id'] == 1
                assert all(f['start_' + name] == 0 for name in ('retired', 'cycle', 'physical_retired'))
                epochs[f['epoch']] = dict(instructions=0, cycles=0, intervals=0)
            else:
                assert f['epoch'] == previous['epoch'] and f['snapshot_id'] == previous['snapshot_id'] + 1
                assert all(f['start_' + name] == previous['end_' + name]
                           for name in ('retired', 'cycle', 'physical_retired'))
            for endpoint, count in [('retired', 'instructions'), ('cycle', 'cycles'),
                                    ('physical_retired', 'physical_retirements')]:
                assert f[count] >= 0 and f['end_' + endpoint] - f['start_' + endpoint] == f[count]
            assert f['flags'] == (16 if f['final'] else 0), 'reset or unexpected interval flags'
            assert f['end_physical_retired'] >= f['end_retired']
            if f['final']:
                assert f['end_physical_retired'] == f['end_retired']
            assert f['attributed_cycles'] + f['unattributed_cycles'] == f['cycles']
            if f['cycles']:
                assert math.isclose(f['ipc'], f['instructions'] / f['cycles'], rel_tol=1e-14)
            else:
                assert f['ipc'] is None
            assert f['counter_deltas'][names.index('rob.instructions_retired')] == f['physical_retirements']
            assert f['counter_deltas'][names.index('tile.cycles')] - stalled == f['cycles']
            ids = set()
            instructions = cycles = executions = 0
            for block in f['blocks']:
                assert len(block) == 4 and all(type(x) is int and 0 <= x < 2**64 for x in block)
                index, insns, elapsed, count = block
                assert index in definitions and index not in ids and insns > 0 and count > 0
                ids.add(index)
                instructions += insns; cycles += elapsed; executions += count
            referenced.update(ids)
            assert instructions == f['instructions'] and cycles == f['attributed_cycles']
            totals['intervals'] += 1; totals['nominal_intervals'] += not f['final']
            totals['instructions'] += instructions; totals['cycles'] += f['cycles']; totals['bb_records'] += executions
            totals['stall_cycles'] += stalled
            for name in ('instructions', 'cycles'):
                epochs[f['epoch']][name] += f[name]
            epochs[f['epoch']]['intervals'] += 1
            previous = f
    assert previous and previous['final'] and totals['instructions'] > 0
    assert referenced == set(definitions), 'unused catalog definitions'
    totals['interval_format_version'] = version
    return names, definitions, epochs, totals


def check(job, interval, smoke=False, expected_pass=None):
    """smoke: the interval-trace-smoke workload; expected_pass: the PASS line prefix
    of another bare-metal workload. Otherwise a SPEC workload."""
    prefix = job / 'intervaltrace-0'
    names, definitions, epochs, totals = check_capture(prefix, interval)
    uart = (job / 'uartlog').read_text(errors='replace')
    assert '*** PASSED ***' in uart and 'IntervalTrace intervaltrace-0: error:' not in uart
    summaries = re.findall(r'IntervalTrace intervaltrace-0: epochs=(\d+), intervals=(\d+), BBs=(\d+) .*?resets=(\d+), '
                           r'instructions=(\d+), cycles=(\d+), stream_bytes=\d+(?:, stall_cycles=(\d+))?', uart)
    assert len(summaries) == 1, 'missing or ambiguous driver summary'
    # The driver summary reports elapsed cycles; interval cycles exclude stall cycles.
    summary = tuple(int(x or 0) for x in summaries[0])
    assert summary == (len(epochs), totals['nominal_intervals'], totals['bb_records'], 0, totals['instructions'],
                       totals['cycles'] + totals['stall_cycles'], totals['stall_cycles'])
    comparisons = 0
    if smoke:
        assert 'PASS interval-trace-smoke checksum=40832' in uart and 'FAIL interval-trace-smoke' not in uart
        benchmark = 'interval-trace-smoke'
    elif expected_pass:
        assert expected_pass in uart and 'FAIL ' not in uart
        benchmark = expected_pass.split()[1]
    else:
        output = job / 'output'
        metadata = json.loads((output / 'benchmark.json').read_text())
        benchmark = metadata['benchmark']
        assert f'PASS spec-interval {benchmark} invocations={len(epochs)}' in uart
        assert 'FAIL spec-interval' not in uart and (output / 'exitcode').read_text().strip() == '0'
        assert len(epochs) == len(metadata['invocations'])
        assert len(list((output / 'status').iterdir())) == len(epochs)
        for index in range(len(epochs)):
            assert (output / 'status' / f'{index:04d}').read_text().strip() == '0'
        for index, command in enumerate(metadata['comparisons']):
            with (job / f'spec-comparison-{index:04d}.log').open('w') as log:
                subprocess.run(['bash', '-c', command], cwd=output, check=True,
                               stdout=log, stderr=subprocess.STDOUT,
                               env=dict(os.environ, SPEC=metadata['spec_dir']))
            comparisons += 1
    result = dict(passed=True, benchmark=benchmark, nominal_interval=interval,
                  block_definitions=len(definitions), epochs=epochs, comparisons=comparisons, capture=str(job), **totals)
    (job / 'validation.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('job', type=Path)
    parser.add_argument('--interval', type=int, default=1000000)
    parser.add_argument('--smoke', action='store_true')
    args = parser.parse_args()
    check(args.job.resolve(), args.interval, args.smoke)
