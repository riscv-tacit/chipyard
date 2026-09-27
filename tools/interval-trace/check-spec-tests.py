#!/usr/bin/env python3
"""Validate an interval-only CPU2017 test capture staged by prepare-spec-tests.py."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


def check(job):
    output = job / 'output'
    metadata = json.loads((output / 'benchmark.json').read_text())
    benchmark = metadata['benchmark']
    invocations = metadata['invocations']
    uart = (job / 'uartlog').read_text(errors='replace')
    assert f'PASS spec-interval {benchmark} invocations={len(invocations)}' in uart
    assert 'FAIL spec-interval' not in uart and '*** PASSED ***' in uart
    assert 'IntervalTrace intervaltrace-0: error:' not in uart
    assert (output / 'exitcode').read_text().strip() == '0'
    assert len(list((output / 'status').iterdir())) == len(invocations)
    for index in range(len(invocations)):
        assert (output / 'status' / f'{index:04d}').read_text().strip() == '0'
    # These are the trusted, local SPEC runsetup comparison commands, preserving
    # per-benchmark tolerances and auxiliary validators. No saved host environment
    # rows from compare.cmd are executed.
    for index, command in enumerate(metadata['comparisons']):
        with (job / f'spec-comparison-{index:04d}.log').open('w') as log:
            subprocess.run(['bash', '-c', command], cwd=output, check=True,
                           stdout=log, stderr=subprocess.STDOUT,
                           env=dict(os.environ, SPEC=metadata['spec_dir']))
    prefix = job / 'intervaltrace-0'
    catalog = json.loads(prefix.with_suffix('.json').read_text())
    names = [event['name'] for event in catalog['events']]
    subprocess.run([sys.executable, str(Path(__file__).with_name('process.py')), str(prefix)], check=True)
    features = job / 'intervaltrace-0-features'
    epochs = {}
    previous_epoch = 0
    with (features / 'intervals.jsonl').open() as stream:
        for line in stream:
            interval = json.loads(line)
            epoch = interval['epoch']
            assert 1 <= epoch <= len(invocations)
            assert epoch in (previous_epoch, previous_epoch + 1)
            if epoch != previous_epoch and previous_epoch:
                assert epochs[previous_epoch]['final']
            previous_epoch = epoch
            assert interval['nominal_interval'] == 1000000
            assert interval['flags'] == (16 if interval['final'] else 0), 'reset or invalid capture flags'
            for event, field in [('rob.instructions_retired', 'physical_retirements'), ('tile.cycles', 'cycles')]:
                assert interval['counter_deltas'][names.index(event)] == interval[field]
            total = epochs.setdefault(epoch, dict(instructions=0, cycles=0, nominal_intervals=0, final=False))
            assert not total['final'], 'records after final interval'
            total['instructions'] += interval['instructions']
            total['cycles'] += interval['cycles']
            total['nominal_intervals'] += not interval['final']
            total['final'] = interval['final']
    assert len(epochs) == len(invocations), 'missing invocation capture'
    for epoch, total in epochs.items():
        assert total['final'] and total['instructions'] > 0 and total['cycles'] > 0
        total['ipc'] = total['instructions'] / total['cycles']
        total['invocation'] = invocations[epoch - 1]
    manifest = json.loads((features / 'manifest.json').read_text())
    summary = dict(passed=True, benchmark=benchmark, suite=metadata['suite'], input='test',
                   capture_scope='launcher-markers', capture_edge_check='not-retained',
                   nominal_interval=1000000, epochs=epochs, totals=manifest['totals'],
                   features=str(features), comparisons=len(metadata['comparisons']))
    (job / 'validation.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('results_job', type=Path)
    check(parser.parse_args().results_job.resolve())
