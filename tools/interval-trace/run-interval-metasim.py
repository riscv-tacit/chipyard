#!/usr/bin/env python3
"""Build a FireMarshal bare-metal image and run the interval bridge in local Verilator metasim.

Workloads: smoke (a checksum loop) and wfi (three timer sleeps in WFI, which
checks stall accounting against the program's own mcycle and minstret).
"""
import argparse
import csv
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import yaml

ROOT = Path(__file__).resolve().parents[2]
SIM = ROOT / 'sims/firesim/sim'
OUT = ROOT / 'tools/interval-trace/target/interval-metasim'
WORKLOADS = ROOT / 'software/firemarshal/example-workloads'
# Workload: (FireMarshal JSON, ELF, PASS line prefix).
WORKLOAD = {'smoke': ('interval-trace-smoke.json', 'smoke.elf', 'PASS interval-trace-smoke checksum=40832'),
            'wfi': ('interval-trace-wfi.json', 'wfi.elf', 'PASS interval-trace-wfi '),
            'l2': ('interval-trace-l2.json', 'l2.elf', 'PASS interval-trace-l2 ')}
RECIPES = ROOT / 'sims/firesim-staging/sample_config_build_recipes.yaml'


def run(command, log, env, cwd=ROOT, timeout=None):
    print(shlex.join(map(str, command)), flush=True)
    print(f'  log: {log}', flush=True)
    with log.open('w') as stream:
        subprocess.run(list(map(str, command)), cwd=cwd, env=env,
                       stdout=stream, stderr=subprocess.STDOUT, check=True, timeout=timeout)


def check_wfi(out, elf, env):
    """The wfi workload under the marker trigger: one epoch spanning the program's
    measured region. Interval cycles exclude stall cycles, so they must equal the
    program's mcycle difference up to the few instructions between its counter
    reads and the markers; stall cycles must cover its timer waits; and every
    interval with stall cycles must contain a block holding a WFI."""
    log = (out / 'uartlog').read_text(errors='replace')
    fields = dict(re.findall(r'(\w+)=(\d+)', re.search(r'PASS interval-trace-wfi (.*)', log).group(1)))
    mcycle, minstret, mtime_us, waits_us = (int(fields[k]) for k in ('mcycle', 'minstret', 'mtime_us', 'waits_us'))
    catalog = json.loads((out / 'intervaltrace-0.json').read_text())
    names = [e['name'] for e in catalog['events']]
    stall = names.index(catalog['stall_event'])
    tile = names.index('tile.cycles')
    with (out / 'intervaltrace-0-blocks.csv').open() as stream:
        blocks = {int(r['id']): (int(r['start_pc'], 16), int(r['end_pc'], 16)) for r in csv.DictReader(stream)}
    disassembly = subprocess.run(['riscv64-unknown-elf-objdump', '-d', elf], env=env, check=True,
                                 capture_output=True, text=True).stdout
    wfi_pcs = [int(m, 16) for m in re.findall(r'^\s*([0-9a-f]+):\s+10500073\s+wfi', disassembly, re.M)]
    intervals = [json.loads(line) for line in (out / 'intervaltrace-0-intervals.jsonl').read_text().splitlines()]
    if {f['epoch'] for f in intervals} != {1}:
        raise RuntimeError('expected one marker-triggered epoch')
    busy = sum(f['cycles'] for f in intervals)
    stalled = sum(f['counter_deltas'][stall] for f in intervals)
    wall = sum(f['counter_deltas'][tile] for f in intervals)
    instructions = sum(f['instructions'] for f in intervals)
    for f in intervals:
        if f['counter_deltas'][stall] and not any(
                blocks[b[0]][0] <= pc <= blocks[b[0]][1] for b in f['blocks'] for pc in wfi_pcs):
            raise RuntimeError(f'stall cycles in snapshot {f["snapshot_id"]} without a WFI block')
    summary = dict(mcycle=mcycle, busy_cycles=busy, stall_cycles=stalled, wall_cycles=wall,
                   minstret=minstret, instructions=instructions, mtime_us=mtime_us, waits_us=waits_us,
                   stalled_intervals=sum(bool(f['counter_deltas'][stall]) for f in intervals))
    print(json.dumps(summary, indent=2))
    (out / 'wfi-check.json').write_text(json.dumps(summary, indent=2) + '\n')
    # The capture spans the markers; the program reads its counters just inside them,
    # next to an uncached mtime load. mtime ticks once per microsecond (1,000 cycles),
    # so each of the three waits can be up to one tick shorter than requested.
    if not 0 <= busy - mcycle <= 1000 or not 0 <= instructions - minstret <= 50:
        raise RuntimeError('busy cycles or instructions disagree with mcycle/minstret')
    if wall != busy + stalled or not (waits_us - 3) * 1000 <= stalled <= (mtime_us + 1) * 1000:
        raise RuntimeError('stall cycles do not cover the timer waits')
    print('PASS: interval cycles match mcycle; stall cycles cover the WFI waits and lie in WFI blocks')


def check_l2(out, dts):
    """The l2 workload under the marker trigger: it reads R new lines, then writes W new
    lines, one access per 64-byte line, in memory nothing touched before. Each line
    crosses the L2's outer port once (demand miss or L1 next-line prefetch), so L2 read
    and write line misses are R and W plus a few instruction-fetch and prefetch lines.
    At most an L2-full of the W dirty lines can stay in the L2, and at most an L1-full
    in the L1, so the writebacks are bounded below. Cache sizes come from the target DTS."""
    log = (out / 'uartlog').read_text(errors='replace')
    fields = dict(re.findall(r'(\w+)=(\d+)', re.search(r'PASS interval-trace-l2 (.*)', log).group(1)))
    reads, writes = int(fields['read_lines']), int(fields['write_lines'])
    tree = dts.read_text()
    l1_lines = int(re.search(r'd-cache-size = <(\d+)>', tree).group(1)) // 64
    l2_lines = int(re.search(r'cache-level = <2>;\s*cache-sets = <\d+>;\s*cache-size = <(\d+)>', tree).group(1)) // 64
    catalog = json.loads((out / 'intervaltrace-0.json').read_text())
    if catalog.get('format_version') != 2 or [s['name'] for s in catalog['sources'] if s['shared']] != ['l2']:
        raise RuntimeError('expected catalog version 2 with the shared source l2')
    names = [e['name'] for e in catalog['events']]
    intervals = [json.loads(line) for line in (out / 'intervaltrace-0-intervals.jsonl').read_text().splitlines()]
    if {f['epoch'] for f in intervals} != {1}:
        raise RuntimeError('expected one marker-triggered epoch')
    total = {n: sum(f['counter_deltas'][i] for f in intervals) for i, n in enumerate(names)}
    summary = dict(read_lines=reads, write_lines=writes, l1_lines=l1_lines, l2_lines=l2_lines,
                   **{n: total[n] for n in names if n.startswith('l2.') or n.startswith('lsu.dcache')})
    print(json.dumps(summary, indent=2))
    (out / 'l2-check.json').write_text(json.dumps(summary, indent=2) + '\n')
    # Instruction-fetch lines of the loops, next-line prefetches past each region, and
    # lines dirtied before the region (stack, data) that are evicted inside it.
    slack = 64
    checks = [
        ('l2.read_line_misses', reads <= total['l2.read_line_misses'] <= reads + slack),
        ('l2.write_line_misses', writes <= total['l2.write_line_misses'] <= writes + slack),
        ('l2.writebacks', writes - l2_lines <= total['l2.writebacks'] <= writes + slack),
        ('lsu.dcache_line_misses', reads + writes <= total['lsu.dcache_line_misses'] <= reads + writes + slack),
        ('lsu.dcache_load_line_misses + store_line_misses <= line_misses',
         total['lsu.dcache_load_line_misses'] + total['lsu.dcache_store_line_misses'] <= total['lsu.dcache_line_misses']),
        ('lsu.dcache_writebacks + probe_writebacks',
         writes - l1_lines <= total['lsu.dcache_writebacks'] + total['lsu.dcache_probe_writebacks'] <= writes + slack),
    ]
    failed = [name for name, ok in checks if not ok]
    if failed:
        raise RuntimeError(f'L1/L2 event totals out of bounds: {", ".join(failed)}')
    print('PASS: L2 and L1 data-cache line misses and writebacks match the access pattern')


def check_capture(out, interval, workload, elf, env, dts):
    # Interval JSONL: block catalog, interval continuity, totals, PMU endpoints
    # against retirement and cycles, and the workload result.
    spec = importlib.util.spec_from_file_location(
        'check_compact_capture', Path(__file__).with_name('check-compact-capture.py'))
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)
    checker.check(out, interval, smoke=workload == 'smoke', expected_pass=WORKLOAD[workload][2])
    if workload == 'wfi':
        check_wfi(out, elf, env)
    if workload == 'l2':
        check_l2(out, dts)
    if not (out / 'intervaltrace-0-pmu.csv').exists():
        return
    # The driver validates framing/integrity before emitting CSV. These two architectural
    # events independently check real BOOM routing and retirement alignment.
    catalog = json.loads((out / 'intervaltrace-0.json').read_text())
    names = [e['name'] for e in catalog['events']]
    with (out / 'intervaltrace-0-pmu.csv').open(newline='') as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames[8:] != names:
            raise RuntimeError('PMU CSV columns disagree with event catalog')
        snapshots = list(reader)
    for row in snapshots:
        for event, endpoint in [('rob.instructions_retired', 'physical_retired'), ('tile.cycles', 'cycle')]:
            if int(row[event]) != int(row[endpoint]):
                raise RuntimeError(f'{event} disagrees with {endpoint} at snapshot {row["snapshot_id"]}')
    if len(snapshots) < 3 or snapshots[0]['kind'] != 'baseline' or snapshots[-1]['kind'] != 'final':
        raise RuntimeError('expected baseline, nominal, and final PMU snapshots')
    with (out / 'intervaltrace-0-bb.csv').open(newline='') as stream:
        blocks = list(csv.DictReader(stream))
    with (out / 'intervaltrace-0-epochs.csv').open(newline='') as stream:
        epochs = list(csv.DictReader(stream))
    if not blocks or not epochs or epochs[-1]['kind'] != 'end':
        raise RuntimeError('missing BB records or final epoch end')
    if sum(int(b['terminate_interval']) for b in blocks) != sum(r['kind'] == 'nominal' for r in snapshots):
        raise RuntimeError('CSV interval boundary/snapshot counts disagree')
    if sum(int(b['instructions']) for b in blocks) != sum(int(e['retired']) for e in epochs if e['kind'] == 'end'):
        raise RuntimeError('CSV BB/epoch instruction counts disagree')
    records = sorted(int(row['record']) for row in blocks + snapshots + epochs)
    if records != list(range(1, len(records) + 1)):
        raise RuntimeError('CSV packet ordering has gaps or duplicates')
    print(f'PASS: CSV records and PMU endpoints agree at all {len(snapshots)} snapshots')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--core', choices=('large', 'mega'), default='large')
    parser.add_argument('--workload', choices=sorted(WORKLOAD), default='smoke')
    parser.add_argument('--jobs', type=int, default=8)
    parser.add_argument('--interval', type=int, default=1000)
    parser.add_argument('--max-cycles', type=int, default=2000000)
    parser.add_argument('--sim-timeout', type=int, default=1800, help='wall-clock limit of the simulation run, seconds')
    parser.add_argument('--output', type=Path, help='results directory; default: target/interval-metasim/CORE')
    parser.add_argument('--trigger-select', type=int, choices=(0, 1, 2, 3), help='always/cycle/PC/instruction selector')
    parser.add_argument('--trigger-start', help='decimal cycle, hex PC, or hex mask+instruction')
    parser.add_argument('--trigger-end', help='decimal cycle, hex PC, or hex mask+instruction')
    parser.add_argument('--trigger', action='store_true', help='capture only between workload instruction markers')
    parser.add_argument('--csv', action='store_true', help='also retain and validate per-record CSVs')
    parser.add_argument('--raw', action='store_true', help='also retain and independently decode raw packets')
    parser.add_argument('--skip-build', action='store_true', help='rerun an already built metasimulator')
    parser.add_argument('--skip-target-elaboration', action='store_true',
                        help='reuse target FIRRTL/annotations already in the metasim generated directory')
    args = parser.parse_args()
    if min(args.jobs, args.interval, args.max_cycles) <= 0:
        parser.error('jobs, interval and max-cycles must be positive')
    recipe = yaml.safe_load(RECIPES.read_text())[f'alveo_u250_firesim_{args.core}_boom_v3_interval_trace']
    name = recipe['TARGET_CONFIG']
    quintuplet = '-'.join(recipe[x] for x in ('PLATFORM', 'TARGET_PROJECT', 'DESIGN', 'TARGET_CONFIG', 'PLATFORM_CONFIG'))
    generated = SIM / 'generated-src' / recipe['PLATFORM'] / quintuplet
    generated.mkdir(parents=True, exist_ok=True)
    out = args.output.resolve() if args.output else OUT / args.core
    out.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    conda = ROOT / '.conda-env'
    riscv = Path(env.get('RISCV', conda / 'riscv-tools'))
    env.update(RISCV=str(riscv), FIRESIM_ENV_SOURCED='1')
    env['PATH'] = os.pathsep.join(map(str, [conda / 'bin', riscv / 'bin', env['PATH']]))
    env.setdefault('JAVA_TOOL_OPTIONS', '-Xmx24G -Xss8M -XX:ActiveProcessorCount=8')
    # Scope relocated-toolchain workarounds to this process; do not source the
    # workspace's generated env.sh, which may still name its previous location.
    env['VERILATOR_ROOT'] = str(conda / 'share/verilator')
    env['CPATH'] = str(conda / 'include') + os.pathsep + env.get('CPATH', '')
    env['LIBRARY_PATH'] = str(conda / 'lib') + os.pathsep + env.get('LIBRARY_PATH', '')
    env['LD_LIBRARY_PATH'] = str(conda / 'lib') + os.pathsep + env.get('LD_LIBRARY_PATH', '')
    marshal = ROOT / 'software/firemarshal/marshal'
    workload = WORKLOADS / WORKLOAD[args.workload][0]
    run([marshal, 'build', workload], out / 'marshal-build.log', env)
    run([marshal, 'install', workload], out / 'marshal-install.log', env)
    base = 'firechip.chip.FireSim.' + name
    firrtl = generated / (base + '.fir')
    annotations = generated / (base + '.anno.json')
    if not args.skip_build:
        if not args.skip_target_elaboration:
            # Use the normal Chipyard stage explicitly, then pass its legacy
            # output to Golden Gate. This avoids sourcing stale generated env.sh.
            target = out / 'target'
            command = (f'firechip/runMain chipyard.Generator --target-dir {target} --name {base} '
                       f'--top-module firechip.chip.FireSim --configs firechip.chip.{name} --emit-legacy-sfc')
            run(['java', '-jar', ROOT / 'scripts/sbt-launch.jar', '-Dsbt.supershell=false',
                 '-Dsbt.server.forcestart=true', command], out / 'target-build.log', env)
            shutil.copyfile(target / (base + '.sfc.fir'), firrtl)
            shutil.copyfile(target / (base + '.anno.json'), annotations)
        if not firrtl.exists() or not annotations.exists():
            parser.error('target FIRRTL/annotations missing; omit --skip-target-elaboration')
        make = ['make', '-C', SIM, 'TARGET_PROJECT=firesim',
                f'TARGET_PROJECT_MAKEFRAG={ROOT}/generators/firechip/chip/src/main/makefrag/firesim',
                f'PLATFORM={recipe["PLATFORM"]}', f'TARGET_CONFIG={name}',
                f'PLATFORM_CONFIG={recipe["PLATFORM_CONFIG"]}']
        run(make + ['firesim_target_symlink_hook', 'firesim-main-classpath'], out / 'host-build.log', env)
        # These two inputs have just been built above (or explicitly reused).
        # Suppress only their make recipes, not Golden Gate or Verilator checks.
        run(make + ['-o', firrtl, '-o', annotations,
                    f'VERILATOR={conda}/bin/verilator_bin --main --timing --cc --exe',
                    f'VERILATOR_MAKEFLAGS=-j{args.jobs} VM_PARALLEL_BUILDS=1',
                    'CXX=g++', 'LINK=g++', 'AR=ar', 'PERL=perl', 'PYTHON3=python3', 'verilator'],
            out / 'metasim-build.log', env)
    binary = generated / 'VFireSim'
    if not binary.exists():
        parser.error('metasimulator missing; omit --skip-build')
    elf = workload.with_suffix('') / WORKLOAD[args.workload][1]
    # Remove this runner's previous outputs so an optional dump cannot look current.
    for suffix in ('.bin', '.json', '-bb.csv', '-pmu.csv', '-epochs.csv', '-intervals.jsonl', '-intervals.jsonl.partial'):
        (out / ('intervaltrace-0' + suffix)).unlink(missing_ok=True)
    run([binary, '+permissive', '+verilator+seed+1', '+fesvr-step-size=128',
         f'+max-cycles={args.max_cycles}', '+mm_useHardwareDefaultRuntimeSettings_0',
         '+blkdev-in-mem0=128', '+blkdev-log0=blkdev-log0',
         f'+interval-trace-interval={args.interval}', '+interval-trace-file=intervaltrace',
         *(['+interval-trace-trigger'] if args.trigger else []),
         *([f'+interval-trace-select={args.trigger_select}'] if args.trigger_select is not None else []),
         *([f'+interval-trace-start={args.trigger_start}'] if args.trigger_start is not None else []),
         *([f'+interval-trace-end={args.trigger_end}'] if args.trigger_end is not None else []),
         *(['+interval-trace-csv'] if args.csv else []),
         *(['+interval-trace-raw'] if args.raw else []), '+permissive-off', elf], out / 'uartlog', env, cwd=out, timeout=args.sim_timeout)
    log = (out / 'uartlog').read_text(errors='replace')
    if WORKLOAD[args.workload][2] not in log or 'FAIL interval-trace' in log:
        raise RuntimeError('target did not report PASS; see uartlog')
    if not re.search(r'IntervalTrace .*: epochs=\d+, intervals=[1-9]\d*, BBs=[1-9]\d*', log):
        raise RuntimeError('missing nonempty interval capture summary; see uartlog')
    if args.raw:
        catalog = json.loads((out / 'intervaltrace-0.json').read_text())
        bridges = ROOT / 'generators/firechip/bridgestubs/src/main/cc/bridges'
        decoder = out / 'check-stream'
        run(['g++', '-std=c++17', '-O2', '-Wall', '-Wextra', '-Werror', '-I', bridges,
             bridges / 'test/standalone/intervaltrace_decoder_test.cc', '-o', decoder],
            out / 'decoder-build.log', env)
        stall = catalog.get('stall_event')
        run([decoder, out / 'intervaltrace-0.bin', len(catalog['events']), catalog['catalog_id']] +
            ([[e['name'] for e in catalog['events']].index(stall)] if stall else []),
            out / 'decoder-check.log', env)
    check_capture(out, args.interval, args.workload, elf, env, out / 'target' / (base + '.dts'))
    print(f'PASS: target workload, nonempty interval capture, and stream integrity; outputs in {out}')


if __name__ == '__main__':
    main()
