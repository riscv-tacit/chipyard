#!/usr/bin/env python3
"""Build a FireMarshal smoke image and run the interval bridge in local Verilator metasim."""
import argparse
import csv
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import yaml

ROOT = Path(__file__).resolve().parents[2]
SIM = ROOT / 'sims/firesim/sim'
OUT = ROOT / 'tools/pmu/target/interval-metasim'
WORKLOAD = ROOT / 'software/firemarshal/example-workloads/interval-trace-smoke.json'
RECIPES = ROOT / 'sims/firesim-staging/sample_config_build_recipes.yaml'


def run(command, log, env, cwd=ROOT, timeout=None):
    print(shlex.join(map(str, command)), flush=True)
    print(f'  log: {log}', flush=True)
    with log.open('w') as stream:
        subprocess.run(list(map(str, command)), cwd=cwd, env=env,
                       stdout=stream, stderr=subprocess.STDOUT, check=True, timeout=timeout)


def check_pmu_endpoints(out):
    live = out / 'intervaltrace-0-intervals.jsonl'
    if live.exists():
        catalog = json.loads((out / 'intervaltrace-0.json').read_text())
        names = [event['name'] for event in catalog['events']]
        count = nominal = 0
        with live.open() as stream:
            for line in stream:
                row = json.loads(line)
                count += 1
                nominal += not row['final']
                for event, endpoint in [('rob.instructions_retired', 'physical_retirements'), ('tile.cycles', 'cycles')]:
                    if row['counter_deltas'][names.index(event)] != row[endpoint]:
                        raise RuntimeError(f'{event} disagrees with {endpoint} at snapshot {row["snapshot_id"]}')
        if not nominal or not count:
            raise RuntimeError('expected nominal and final intervals')
        subprocess.run([sys.executable, str(ROOT / 'tools/interval-trace/process.py'),
                        str(out / 'intervaltrace-0')], check=True)
        print(f'PASS: live interval features and PMU deltas agree at all {count} intervals')
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
    parser.add_argument('--jobs', type=int, default=8)
    parser.add_argument('--interval', type=int, default=1000)
    parser.add_argument('--max-cycles', type=int, default=2000000)
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
    run([marshal, 'build', WORKLOAD], out / 'marshal-build.log', env)
    run([marshal, 'install', WORKLOAD], out / 'marshal-install.log', env)
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
    elf = WORKLOAD.with_suffix('') / 'smoke.elf'
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
         *(['+interval-trace-raw'] if args.raw else []), '+permissive-off', elf], out / 'uartlog', env, cwd=out, timeout=1800)
    log = (out / 'uartlog').read_text(errors='replace')
    if 'PASS interval-trace-smoke checksum=40832' not in log or 'FAIL interval-trace-smoke' in log:
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
        run([decoder, out / 'intervaltrace-0.bin', len(catalog['events']), catalog['catalog_id']],
            out / 'decoder-check.log', env)
    check_pmu_endpoints(out)
    print(f'PASS: target workload, nonempty interval capture, and stream integrity; outputs in {out}')


if __name__ == '__main__':
    main()
