#!/usr/bin/env python3
"""Check the top-down PMU events in a metasim capture's per-snapshot PMU CSV (intervaltrace-0-pmu.csv).

Per pair of consecutive snapshots (counters are cumulative), with W the dispatch width:
  direction + JALR mispredict redirects <= branch.mispredict_redirects (the rest are other CFI types)
  instructions_retired <= retired_uops (both counted at commit)
  W * frontend_starved_cycles <= frontend_bubble_slots
  dispatched_uops + frontend_bubble_slots + W * redirect_blocked_cycles <= W * tile.cycles
Over the whole capture also retired_uops <= dispatched_uops (between two snapshots, uops in flight at the
first can retire after it). The slot inequality holds cycle by cycle: an unstalled cycle fills exactly W slots with dispatched uops and bubbles,
a redirect cycle dispatches nothing and adds no bubbles, a stalled cycle adds no bubbles.
Prints totals and the slot-based level 1 over the whole capture.

Usage: check-topdown-events.py OUTPUT_DIR [--width 4]
"""
import argparse
import csv
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('output', type=Path)
parser.add_argument('--width', type=int, default=4)
args = parser.parse_args()
W = args.width

with (args.output / 'intervaltrace-0-pmu.csv').open(newline='') as stream:
    rows = list(csv.DictReader(stream))
need = ['branch.mispredict_redirects', 'branch.direction_mispredict_redirects', 'branch.jalr_mispredict_redirects',
        'rob.instructions_retired', 'rob.retired_uops', 'pipeline.dispatched_uops', 'rob.flushes',
        'pipeline.frontend_bubble_slots', 'pipeline.frontend_starved_cycles', 'pipeline.redirect_blocked_cycles',
        'tile.cycles', 'csr.stall_cycles']
missing = [n for n in need if n not in rows[0]]
if missing:
    raise SystemExit(f'events missing from the capture: {missing}')
failures = 0
total = {n: 0 for n in need}
for a, b in zip(rows, rows[1:]):
    d = {n: int(b[n]) - int(a[n]) for n in need}
    for n in need:
        total[n] += d[n]
    checks = [
        ('direction + JALR <= all redirects',
         d['branch.direction_mispredict_redirects'] + d['branch.jalr_mispredict_redirects'] <= d['branch.mispredict_redirects']),
        ('instructions <= retired uops', d['rob.instructions_retired'] <= d['rob.retired_uops']),
        ('W * starved <= bubble slots', W * d['pipeline.frontend_starved_cycles'] <= d['pipeline.frontend_bubble_slots']),
        ('dispatched + bubbles + W * redirect <= W * cycles',
         d['pipeline.dispatched_uops'] + d['pipeline.frontend_bubble_slots'] + W * d['pipeline.redirect_blocked_cycles']
         <= W * d['tile.cycles']),
    ]
    for name, ok in checks:
        if not ok:
            failures += 1
            print(f'FAIL snapshot {b["snapshot_id"]}: {name}: {d}')
if total['rob.retired_uops'] > total['pipeline.dispatched_uops']:
    failures += 1
    print('FAIL whole capture: retired uops > dispatched uops')
slots = W * (total['tile.cycles'] - total['csr.stall_cycles'])
retiring = total['rob.retired_uops'] / slots
bad = (total['pipeline.dispatched_uops'] - total['rob.retired_uops'] + W * total['pipeline.redirect_blocked_cycles']) / slots
front = total['pipeline.frontend_bubble_slots'] / slots
print(f'{len(rows) - 1} snapshot intervals; totals: ' + ', '.join(f'{n}={v}' for n, v in total.items()))
print(f'redirects not BR or JALR: {total["branch.mispredict_redirects"] - total["branch.direction_mispredict_redirects"] - total["branch.jalr_mispredict_redirects"]}')
print(f'slot level 1: retiring {retiring:.3f}, bad speculation {bad:.3f}, front end {front:.3f}, '
      f'back end (rest) {1 - retiring - bad - front:.3f}')
print('PASS' if failures == 0 else f'FAIL: {failures} checks failed')
raise SystemExit(1 if failures else 0)
