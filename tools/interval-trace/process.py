#!/usr/bin/env python3
"""Export live interval aggregates, or aggregate legacy record CSV captures."""
import argparse
import csv
import heapq
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
BRIDGES = ROOT / 'generators/firechip/bridgestubs/src/main/cc/bridges'
BB_FIELDS = 'record epoch start_pc end_pc instructions elapsed_cycles terminate_interval partial_start partial_end privilege flags'.split()
PMU_FIELDS = 'record epoch snapshot_id kind logical_retired cycle physical_retired flags'.split()
EPOCH_FIELDS = 'record epoch kind nominal_interval counter_count catalog_id retired cycles final_snapshot_id flags'.split()


def uint(value, base=10):
    result = int(value, base)
    if not 0 <= result < 2**64:
        raise ValueError(f'unsigned 64-bit value out of range: {value}')
    return result


def build():
    target = HERE / 'target/aggregate'
    sources = [HERE / 'aggregate.cc', BRIDGES / 'intervaltrace_aggregate.h', BRIDGES / 'intervaltrace_decoder.h', BRIDGES / 'intervaltrace_json.h']
    if not target.exists() or any(p.stat().st_mtime > target.stat().st_mtime for p in sources):
        target.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(shlex.split(os.environ.get('CXX', 'g++')) +
                       ['-std=c++17', '-O2', '-Wall', '-Wextra', '-Werror', '-I', str(BRIDGES),
                        str(sources[0]), '-o', str(target)], check=True)
    return target


def csv_rows(prefix, suffix, fields):
    path = Path(str(prefix) + suffix)
    with path.open(newline='') as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != fields or len(set(fields)) != len(fields):
            raise ValueError(f'{path}: unexpected or duplicate column names')
        previous = 0
        for row in reader:
            if None in row or None in row.values():
                raise ValueError(f'{path}: malformed CSV row')
            record = uint(row['record'])
            if record <= previous:
                raise ValueError(f'{path}: record numbers are not strictly increasing')
            previous = record
            yield record, suffix, row


def events(prefix, catalog):
    names = [e['name'] for e in catalog['events']]
    readers = [csv_rows(prefix, '-bb.csv', BB_FIELDS),
               csv_rows(prefix, '-pmu.csv', PMU_FIELDS + names),
               csv_rows(prefix, '-epochs.csv', EPOCH_FIELDS)]
    epoch = 0
    for expected, (record, suffix, row) in enumerate(heapq.merge(*readers, key=lambda r: r[0]), 1):
        if record != expected:
            raise ValueError(f'expected record {expected}, received {record}')
        flags = uint(row['flags'])
        if suffix == '-epochs.csv':
            if row['kind'] == 'start':
                epoch = uint(row['epoch'])
                kind, fields, empty = 3, ['epoch', 'nominal_interval', 'counter_count', 'catalog_id'], ['retired', 'cycles', 'final_snapshot_id']
            elif row['kind'] == 'end':
                kind, fields, empty = 4, ['epoch', 'retired', 'cycles', 'final_snapshot_id'], ['nominal_interval', 'counter_count', 'catalog_id']
            else:
                raise ValueError('unknown epoch record kind')
            if any(row[key] != '' for key in empty):
                raise ValueError('unexpected values in unused epoch fields')
            words = [uint(row[key]) for key in fields]
        elif suffix == '-bb.csv':
            kind = 1
            words = [uint(row[key], 16) for key in ('start_pc', 'end_pc')]
            words += [uint(row[key]) for key in ('instructions', 'elapsed_cycles')]
            for key, value in [('terminate_interval', bool(flags & 1)), ('partial_start', bool(flags & 2)),
                               ('partial_end', bool(flags & 4)), ('privilege', flags >> 13)]:
                if uint(row[key]) != value:
                    raise ValueError(f'BB {key} disagrees with flags at record {record}')
        else:
            kind = 2
            if row['kind'] != ('baseline' if flags & 8 else 'final' if flags & 16 else 'nominal'):
                raise ValueError('PMU kind disagrees with flags')
            words = [uint(row[key]) for key in ['snapshot_id', 'logical_retired', 'cycle', 'physical_retired'] + names]
        if uint(row['epoch']) != epoch:
            raise ValueError(f'wrong epoch at record {record}')
        yield ' '.join(map(str, [kind, flags, *words])) + '\n'


def aggregate(prefix, catalog, output, executable):
    # stdout goes to a file, so the producer cannot deadlock on a full output pipe.
    with output.open('w') as stream:
        worker = subprocess.Popen([str(executable), str(len(catalog['events'])), str(catalog['catalog_id'])],
                                  stdin=subprocess.PIPE, stdout=stream, text=True)
        try:
            with worker.stdin:
                for event in events(prefix, catalog):
                    worker.stdin.write(event)
            if worker.wait() != 0:
                raise RuntimeError('aggregation failed; see decoder diagnostic')
        except BaseException:
            worker.terminate()
            worker.wait()
            raise


def ratio(numerator, denominator):
    return format(numerator / denominator, '.17g') if denominator else ''


def validated_intervals(stream, catalog):
    """Check aggregate continuity and coverage without retaining dynamic records."""
    previous = None
    for line in stream:
        f = json.loads(line)
        if len(f['counter_deltas']) != len(catalog['events']):
            raise ValueError('interval counter count disagrees with catalog')
        new_epoch = previous is None or previous['final']
        if new_epoch:
            if (f['epoch'] != (previous['epoch'] + 1 if previous else 1) or
                    f['snapshot_id'] != 1 or any(f['start_' + field] != 0
                                               for field in ('retired', 'cycle', 'physical_retired'))):
                raise ValueError('invalid aggregate epoch start')
        elif (f['epoch'] != previous['epoch'] or f['snapshot_id'] != previous['snapshot_id'] + 1 or
              f['nominal_interval'] != previous['nominal_interval'] or
              any(f['start_' + field] != previous['end_' + field]
                  for field in ('retired', 'cycle', 'physical_retired'))):
            raise ValueError('nonconsecutive aggregate endpoints')
        for endpoint, count in (('retired', 'instructions'), ('cycle', 'cycles'),
                                ('physical_retired', 'physical_retirements')):
            if f[count] < 0 or f['end_' + endpoint] - f['start_' + endpoint] != f[count]:
                raise ValueError('aggregate counts disagree with endpoints')
        if (f['nominal_interval'] <= 0 or f['end_physical_retired'] < f['end_retired'] or
                f['flags'] not in ((16, 48) if f['final'] else (0,)) or
                (f['final'] and f['end_physical_retired'] != f['end_retired'])):
            raise ValueError('invalid aggregate boundary')
        previous = f
        yield f
    if previous is not None and not previous['final']:
        raise ValueError('aggregate capture missing final interval')


def export(directory, catalog):
    names = [e['name'] for e in catalog['events']]
    columns = ('epoch snapshot_id vector_row final nominal_interval start_retired end_retired start_cycle end_cycle '
               'start_physical_retired end_physical_retired instructions cycles attributed_cycles unattributed_cycles '
               'physical_retirements partial_blocks flags bb_flags ipc cpi physical_ipc').split()
    feature_columns = [f'{name}.{measure}' for name in names for measure in ('delta', 'per_cycle')]
    blocks = {}
    totals = dict(intervals=0, nominal_intervals=0, exported_vectors=0, instructions=0, cycles=0,
                  attributed_cycles=0, unattributed_cycles=0, physical_retirements=0)
    with (directory / 'intervals.csv').open('w', newline='') as summary, \
         (directory / 'rfv.csv').open('w', newline='') as rfv_file, \
         (directory / 'bbv.txt').open('w') as bbv, (directory / 'bbtv.txt').open('w') as bbtv, \
         (directory / 'blocks.csv').open('w', newline='') as block_file, \
         (directory / 'intervals.jsonl').open() as intervals:
        writer = csv.DictWriter(summary, fieldnames=columns)
        writer.writeheader()
        rfv = csv.writer(rfv_file)
        rfv.writerow(['epoch', 'snapshot_id', 'vector_row', 'cycles', 'physical_retirements'] + feature_columns)
        mapping = csv.writer(block_file)
        mapping.writerow(['id', 'start_pc', 'end_pc', 'privilege', 'partial_start', 'partial_end'])
        for f in validated_intervals(intervals, catalog):
            # A common row selection preserves BBV/BBTV comparison alignment.
            selected = not f['final'] and f['instructions'] > 0 and f['cycles'] > 0
            vector_row = totals['exported_vectors'] if selected else ''
            iv, tv = {}, {}
            for start, end, privilege, partial, instructions, cycles, executions in f['blocks']:
                key = start, end, privilege, partial
                if key not in blocks:
                    blocks[key] = len(blocks) + 1
                    mapping.writerow([blocks[key], hex(start), hex(end), privilege, int(bool(partial & 2)), int(bool(partial & 4))])
                index = blocks[key]
                iv[index], tv[index] = instructions, cycles
            if sum(iv.values()) != f['instructions'] or sum(tv.values()) != f['attributed_cycles']:
                raise ValueError('feature vector totals disagree with interval')
            if f['attributed_cycles'] + f['unattributed_cycles'] != f['cycles']:
                raise ValueError('attributed and unattributed cycles do not cover interval')
            if selected:
                for stream, vector in ((bbv, iv), (bbtv, tv)):
                    stream.write('T' + ''.join(f':{index}:{value} ' for index, value in sorted(vector.items()) if value) + '\n')
            row = {key: f[key] for key in columns if key in f}
            row.update(vector_row=vector_row, final=int(f['final']), ipc=ratio(f['instructions'], f['cycles']),
                       cpi=ratio(f['cycles'], f['instructions']), physical_ipc=ratio(f['physical_retirements'], f['cycles']))
            writer.writerow(row)
            rates = []
            for delta in f['counter_deltas']:
                rates += [delta, ratio(delta, f['cycles'])]
            rfv.writerow([f['epoch'], f['snapshot_id'], vector_row, f['cycles'], f['physical_retirements']] + rates)
            totals['intervals'] += 1
            totals['nominal_intervals'] += not f['final']
            totals['exported_vectors'] += selected
            for key in ('instructions', 'cycles', 'attributed_cycles', 'unattributed_cycles', 'physical_retirements'):
                totals[key] += f[key]
    totals['block_identities'] = len(blocks)
    totals['ipc'] = ratio(totals['instructions'], totals['cycles'])
    return totals


def process(prefix, output, from_csv=False):
    catalog = json.loads(Path(str(prefix) + '.json').read_text())
    if catalog['format_version'] != 1 or catalog['counter_bits'] != 64:
        raise ValueError('requires interval format 1 with 64-bit cumulative counters')
    if [e['id'] for e in catalog['events']] != list(range(len(catalog['events']))):
        raise ValueError('counter IDs must be contiguous and in catalog order')
    live = Path(str(prefix) + '-intervals.jsonl')
    if not from_csv and Path(str(prefix) + '-intervals.jsonl.partial').exists():
        raise ValueError('incomplete live capture; inspect driver diagnostics (or explicitly use --from-csv)')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.interval-features-', dir=output.parent) as temporary:
        stage = Path(temporary)
        if live.exists() and not from_csv:
            shutil.copyfile(live, stage / 'intervals.jsonl')
        else:
            aggregate(prefix, catalog, stage / 'intervals.jsonl', build())
        totals = export(stage, catalog)
        manifest = dict(feature_format_version=1, input_prefix=str(prefix.resolve()), catalog=catalog,
                        block_identity=['start_pc', 'end_pc', 'privilege', 'partial_start', 'partial_end'],
                        identity_scope='this capture/source; no ASID or code build identity is present',
                        bbtv_policy='preceding-BB-boundary-to-current-boundary; final idle time unattributed',
                        vectors='raw integer weights; common nominal, nonzero-instruction, nonzero-cycle rows only',
                        vector_row='zero-based line index shared by bbv.txt and bbtv.txt; blank means not exported',
                        rfv='all registered events; modulo-2^64 snapshot deltas; rates per elapsed cycle',
                        zero_denominator='empty CSV rate; no infinity or fabricated zero',
                        totals=totals)
        (stage / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
        output.mkdir(parents=True, exist_ok=True)
        # Publish only after the complete stream and every output has validated.
        for file in sorted(stage.iterdir(), key=lambda p: p.name == 'manifest.json'):
            os.replace(file, output / file.name)
    return totals


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('prefix', type=Path, help='capture/catalog prefix, e.g. .../intervaltrace-0')
    parser.add_argument('--output', type=Path, help='default: PREFIX-features')
    parser.add_argument('--from-csv', action='store_true', help='recompute from opt-in record CSVs, even if live aggregates exist')
    args = parser.parse_args()
    output = args.output or Path(str(args.prefix) + '-features')
    totals = process(args.prefix, output, args.from_csv)
    print(f"PASS: {totals['nominal_intervals']} nominal intervals, {totals['exported_vectors']} vector rows, "
          f"{totals['instructions']} instructions, {totals['cycles']} cycles; {output}")


if __name__ == '__main__':
    main()
