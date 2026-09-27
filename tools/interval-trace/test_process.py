#!/usr/bin/env python3
import csv
import json
from pathlib import Path
import tempfile
import unittest

import process

P = 3 << 13
MAX = (1 << 64) - 1
CATALOG = dict(format_version=1, counter_bits=64, source='test_tile', catalog_id='99',
               events=[dict(id=0, name='test.requests', unit='requests'),
                       dict(id=1, name='rob.instructions_retired', unit='instructions')])


def fixture(prefix, records):
    Path(str(prefix) + '.json').write_text(json.dumps(CATALOG))
    rows = {'-bb.csv': [], '-pmu.csv': [], '-epochs.csv': []}
    epoch = 0
    for index, (kind, flags, w) in enumerate(records, 1):
        if kind == 3:
            epoch = w[0]
        row = dict(record=index, epoch=epoch, flags=flags)
        if kind == 1:
            row.update(start_pc=hex(w[0]), end_pc=hex(w[1]), instructions=w[2], elapsed_cycles=w[3],
                       terminate_interval=int(bool(flags & 1)), partial_start=int(bool(flags & 2)),
                       partial_end=int(bool(flags & 4)), privilege=flags >> 13)
            suffix = '-bb.csv'
        elif kind == 2:
            row.update(snapshot_id=w[0], kind='baseline' if flags & 8 else 'final' if flags & 16 else 'nominal',
                       logical_retired=w[1], cycle=w[2], physical_retired=w[3])
            row.update(zip([e['name'] for e in CATALOG['events']], w[4:]))
            suffix = '-pmu.csv'
        else:
            row.update(kind='start' if kind == 3 else 'end', nominal_interval='', counter_count='',
                       catalog_id='', retired='', cycles='', final_snapshot_id='')
            if kind == 3:
                row.update(nominal_interval=w[1], counter_count=w[2], catalog_id=w[3])
            else:
                row.update(retired=w[1], cycles=w[2], final_snapshot_id=w[3])
            suffix = '-epochs.csv'
        rows[suffix].append(row)
    fields = {'-bb.csv': process.BB_FIELDS, '-pmu.csv': process.PMU_FIELDS + [e['name'] for e in CATALOG['events']],
              '-epochs.csv': process.EPOCH_FIELDS}
    for suffix, values in rows.items():
        with Path(str(prefix) + suffix).open('w', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=fields[suffix])
            writer.writeheader()
            writer.writerows(values)


def records():
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


class AggregationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.prefix = self.root / 'capture'
        self.out = self.root / 'features'

    def read_csv(self, name):
        with (self.out / name).open(newline='') as stream:
            return list(csv.DictReader(stream))

    def test_pairing_wrap_reset_overshoot_and_idle_tail(self):
        fixture(self.prefix, records())
        totals = process.process(self.prefix, self.out)
        self.assertEqual((totals['instructions'], totals['physical_retirements'], totals['cycles']), (12, 12, 26))
        self.assertEqual((totals['nominal_intervals'], totals['exported_vectors'], totals['intervals']), (3, 2, 5))
        self.assertEqual((totals['attributed_cycles'], totals['unattributed_cycles']), (21, 5))
        results = [json.loads(line) for line in (self.out / 'intervals.jsonl').read_text().splitlines()]
        self.assertEqual([f['counter_deltas'] for f in results], [[5, 6], [0, 0], [2, 2], [2, 4], [1, 0]])
        self.assertEqual(results[0]['blocks'], [[0x100, 0x102, 3, 0, 5, 10, 2]])
        self.assertEqual(results[0]['ipc'], 0.5)
        self.assertIsNone(results[1]['ipc'])
        self.assertEqual(results[4]['ipc'], 0)
        self.assertEqual(results[1]['cycles'], 0)
        self.assertEqual(results[2]['partial_blocks'], 1)
        self.assertEqual(results[2]['blocks'][0][3], 6)
        self.assertEqual(results[4]['blocks'], [])
        self.assertEqual(results[4]['unattributed_cycles'], 3)
        summary = self.read_csv('intervals.csv')
        self.assertEqual([r['vector_row'] for r in summary], ['0', '', '', '1', ''])
        self.assertEqual(summary[1]['ipc'], '')
        self.assertEqual(summary[0]['ipc'], '0.5')
        rates = self.read_csv('rfv.csv')
        self.assertEqual(rates[0]['rob.instructions_retired.delta'], '6')
        self.assertEqual(rates[1]['test.requests.per_cycle'], '')
        self.assertEqual((self.out / 'bbv.txt').read_text().splitlines(), ['T:1:5 ', 'T:1:4 '])
        self.assertEqual((self.out / 'bbtv.txt').read_text().splitlines(), ['T:1:10 ', 'T:1:8 '])
        original = {p.name: p.read_bytes() for p in self.out.iterdir()}
        process.process(self.prefix, self.out)
        self.assertEqual(original, {p.name: p.read_bytes() for p in self.out.iterdir()})

    def test_live_export_without_record_csvs_and_incomplete_capture(self):
        fixture(self.prefix, records())
        expected = process.process(self.prefix, self.out)
        original = {p.name: p.read_bytes() for p in self.out.iterdir()}
        live = Path(str(self.prefix) + '-intervals.jsonl')
        live.write_bytes(original['intervals.jsonl'])
        for suffix in ('-bb.csv', '-pmu.csv', '-epochs.csv'):
            Path(str(self.prefix) + suffix).unlink()
        self.assertEqual(process.process(self.prefix, self.out), expected)
        self.assertEqual(original, {p.name: p.read_bytes() for p in self.out.iterdir()})
        partial = Path(str(live) + '.partial')
        partial.write_text('unfinished')
        with self.assertRaisesRegex(ValueError, 'incomplete live capture'):
            process.process(self.prefix, self.out)
        self.assertEqual(original, {p.name: p.read_bytes() for p in self.out.iterdir()})

    def test_corrupted_live_intervals_are_rejected(self):
        fixture(self.prefix, records())
        process.process(self.prefix, self.out)
        original = [json.loads(line) for line in (self.out / 'intervals.jsonl').read_text().splitlines()]
        live = Path(str(self.prefix) + '-intervals.jsonl')
        variants = [original[:-1]]
        for field, value in [('counter_deltas', []), ('start_retired', 1), ('snapshot_id', 10),
                             ('cycles', -1), ('nominal_interval', 0), ('flags', 8)]:
            bad = [dict(row) for row in original]
            bad[0][field] = value
            variants.append(bad)
        for variant in variants:
            live.write_text(''.join(json.dumps(row) + '\n' for row in variant))
            with self.assertRaises(ValueError):
                process.process(self.prefix, self.root / 'bad-output')
            self.assertFalse((self.root / 'bad-output').exists())

    def test_empty_capture_and_idle_only_epoch(self):
        fixture(self.prefix, [])
        self.assertEqual(process.process(self.prefix, self.out)['intervals'], 0)
        fixture(self.prefix, [(3, 0, [1, 4, 2, 99]), (2, 8, [0, 0, 0, 0, 0, 0]),
                              (2, 16, [1, 0, 7, 0, 3, 0]), (4, 0, [1, 0, 7, 1])])
        totals = process.process(self.prefix, self.out)
        self.assertEqual(totals['unattributed_cycles'], 7)
        self.assertEqual(totals['exported_vectors'], 0)
        self.assertEqual(self.read_csv('intervals.csv')[0]['cpi'], '')

    def test_partial_and_privilege_identities_do_not_alias(self):
        seq = [(3, 0, [1, 4, 2, 99]), (2, 8, [0, 0, 0, 0, 0, 0]),
               (2, 0, [1, 4, 4, 4, 0, 4]),
               (1, P, [0x100, 0x100, 1, 1]), (1, P | 2, [0x100, 0x100, 1, 1]),
               (1, 0, [0x100, 0x100, 1, 1]), (1, P | 1, [0x100, 0x100, 1, 1]),
               (2, 16, [2, 4, 4, 4, 0, 4]), (4, 0, [1, 4, 4, 2])]
        fixture(self.prefix, seq)
        self.assertEqual(process.process(self.prefix, self.out)['block_identities'], 3)

    def test_invalid_streams_do_not_publish_outputs(self):
        seq = records()
        variants = [seq[:-1], seq[:2] + seq[3:]]
        bad = records(); bad[4] = (1, P, [0x100, 0x102, 2, 3]); variants.append(bad)
        bad = records(); bad[0] = (3, 0, [1, 4, 2, 100]); variants.append(bad)
        for variant in variants:
            with self.subTest(variant=variant):
                fixture(self.prefix, variant)
                with self.assertRaises((RuntimeError, BrokenPipeError)):
                    process.process(self.prefix, self.out)
                self.assertFalse(self.out.exists())

    def test_csv_order_epoch_and_redundant_flags_are_checked(self):
        for old, new in [('5,1,', '50,1,'), ('5,1,', '5,2,'), (',0,0,0,3,', ',1,0,0,3,')]:
            fixture(self.prefix, records())
            file = Path(str(self.prefix) + '-bb.csv')
            data = file.read_text()
            self.assertIn(old, data)
            file.write_text(data.replace(old, new, 1))
            with self.assertRaises(ValueError):
                process.process(self.prefix, self.out)
            self.assertFalse(self.out.exists())


if __name__ == '__main__':
    unittest.main()
