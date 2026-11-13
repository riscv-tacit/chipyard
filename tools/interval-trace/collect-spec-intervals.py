#!/usr/bin/env python3
"""Collect interval traces of specpack FireMarshal images on AWS F2, one FPGA per benchmark.

  prepare  build and install each image with FireMarshal, then write into RUN:
           runtime.yaml (one no_net_config node per benchmark on as many f2.6xlarge),
           hwdb.yaml, items.json and launch.sh, plus one FireSim workload that lists
           every benchmark as a job (sims/firesim/deploy/workloads/<RUN name>.json).
  collect  launchrunfarm, infrasetup, runworkload (all jobs at once; with
           terminate_on_completion each instance terminates once its job's
           results are copied back), then terminaterunfarm (always, also on
           failure, for instances FireSim left running), then
           check-compact-capture.py on every job.
           Progress is in RUN/steps.json and RUN/summary.csv.

Run collect through RUN/launch.sh, which sources the FireSim manager environment.
"""
import argparse
import csv
import fcntl
import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / 'sims/firesim/deploy'
RECIPES = ROOT / 'sims/firesim-staging/sample_config_build_recipes.yaml'
CHECKER = ROOT / 'tools/interval-trace/check-compact-capture.py'
OUTPUTS = ['uartlog', 'intervaltrace-0.json', 'intervaltrace-0-blocks.csv', 'intervaltrace-0-intervals.jsonl']

RUNTIME = """\
run_farm:
  base_recipe: run-farm-recipes/aws_ec2.yaml
  recipe_arg_overrides:
    run_farm_tag: {session}
    always_expand_run_farm: true
    launch_instances_timeout_minutes: 30
    run_instance_market: ondemand
    spot_interruption_behavior: terminate
    spot_max_price: ondemand
    default_simulation_dir: /home/ubuntu/{session}
    run_farm_hosts_to_use:
    - f2.6xlarge: {nodes}
    - f2.12xlarge: 0
    - f2.48xlarge: 0
    - m4.16xlarge: 0
    - z1d.3xlarge: 0
    - z1d.6xlarge: 0
    - z1d.12xlarge: 0
metasimulation:
  metasimulation_enabled: false
  metasimulation_host_simulator: verilator
  metasimulation_only_plusargs: +fesvr-step-size=128 +max-cycles=2000000 +verilator+seed+1
  metasimulation_only_vcs_plusargs: +vcs+initreg+0 +vcs+initmem+0
target_config:
  topology: no_net_config
  no_net_num_nodes: {nodes}
  link_latency: 6405
  switching_latency: 10
  net_bandwidth: 200
  profile_interval: -1
  default_hw_config: {hw_config}
  plusarg_passthrough: +interval-trace-trigger +interval-trace-interval={interval} +interval-trace-file=intervaltrace
tracing:
  enable: false
  output_format: 0
  selector: 1
  start: 0
  end: -1
autocounter:
  read_rate: 0
workload:
  workload_name: {session}.json
  terminate_on_completion: true
  suffix_tag: null
host_debug:
  zero_out_dram: false
  disable_synth_asserts: false
synth_print:
  start: 0
  end: -1
  cycle_prefix: true
"""

LAUNCH = """\
#!/usr/bin/env bash
set -eo pipefail
cd {root}/sims/firesim
source ./sourceme-manager.sh --skip-ssh-setup
export FIRESIM_KEYPAIR={keypair} FIRESIM_SSH_KEY={ssh_key}
exec {python} {script} collect {run}
"""


def now():
    return datetime.now(timezone.utc).isoformat()


def tool_environment():
    """Chipyard's conda toolchain, without sourcing a generated env.sh (as run-interval-metasim.py)."""
    env = dict(os.environ)
    conda = ROOT / '.conda-env'
    riscv = Path(env.get('RISCV', conda / 'riscv-tools'))
    env.update(RISCV=str(riscv))
    env['PATH'] = os.pathsep.join(map(str, [conda / 'bin', riscv / 'bin', env['PATH']]))
    return env


def prepare(args):
    run = args.run.resolve()
    session = run.name
    if run.exists():
        raise SystemExit(f'{run} exists; choose a new run directory')
    index = json.loads((args.images / 'images.json').read_text())
    prepared = {i['benchmark']: i for i in index['prepared']}
    benchmarks = args.benchmarks or sorted(prepared)
    missing = sorted(set(benchmarks) - set(prepared))
    if missing:
        raise SystemExit(f'not in {args.images}/images.json: {", ".join(missing)}')
    for benchmark in benchmarks:
        # A host wrapper in the target's command lines (SPEC records submit commands in its
        # command files) cannot run in the image; reject it before any FPGA time is spent.
        script = Path(prepared[benchmark]['config']).parent / 'overlay/run-interval.sh'
        if 'rv-exec' in script.read_text():
            raise SystemExit(f'{script} runs the host wrapper rv-exec; re-export the bundle')
    run.mkdir(parents=True)
    env = tool_environment()
    marshal = ROOT / 'software/firemarshal/marshal'
    items, jobs = [], []
    for benchmark in benchmarks:
        image = prepared[benchmark]
        folder = run / benchmark
        folder.mkdir()
        for step in ('build', 'install'):
            with (folder / f'marshal-{step}.log').open('w') as log:
                subprocess.run([marshal, step, image['config']], cwd=ROOT, env=env, stdout=log,
                               stderr=subprocess.STDOUT, check=True)
        installed = json.loads((DEPLOY / 'workloads' / image['workload']).read_text())
        name = Path(image['workload']).stem
        jobs.append(dict(name=name, bootbinary=installed['common_bootbinary'], rootfs=installed['common_rootfs'],
                         outputs=installed['common_outputs']))
        items.append(dict(benchmark=benchmark, job=name, invocations=image['invocations'], config=image['config'],
                          metadata=image['metadata'], rootfs_size=image['rootfs_size'], folder=str(folder)))
        print(f'{benchmark}: built and installed', flush=True)
    workload = DEPLOY / 'workloads' / f'{session}.json'
    # FireSim resolves each job's ../ image paths from workloads/<benchmark_name>/.
    (DEPLOY / 'workloads' / session).mkdir()
    workload.write_text(json.dumps(dict(benchmark_name=session, common_simulation_outputs=OUTPUTS, workloads=jobs),
                                   indent=2) + '\n')
    (run / 'runtime.yaml').write_text(RUNTIME.format(session=session, nodes=len(jobs), hw_config=args.hw_config,
                                                     interval=args.interval))
    (run / 'hwdb.yaml').write_text(f'{args.hw_config}:\n    agfi: {args.agfi}\n    deploy_quintuplet_override: null\n'
                                   '    custom_runtime_config: null\n')
    (run / 'items.json').write_text(json.dumps(dict(
        images=str(args.images.resolve()), kit=index['kit'], label=index['label'], size=index['size'],
        hw_config=args.hw_config, agfi=args.agfi, interval=args.interval, timeout=args.timeout,
        min_free_gb=args.min_free_gb, workload=str(workload), items=items), indent=2) + '\n')
    launch = run / 'launch.sh'
    launch.write_text(LAUNCH.format(root=ROOT, keypair=args.keypair, ssh_key=args.ssh_key, python=sys.executable,
                                    script=Path(__file__).resolve(), run=run))
    launch.chmod(0o755)
    print(f'{len(jobs)} jobs; start with: tmux new-session -d -s {session} {launch}')


class Collection:
    def __init__(self, run):
        self.run = run
        self.plan = json.loads((run / 'items.json').read_text())
        self.steps = [dict(benchmark=i['benchmark'], phase='pending') for i in self.plan['items']]

    def save(self):
        (self.run / 'steps.json').write_text(json.dumps(self.steps, indent=2) + '\n')
        with (self.run / 'summary.csv').open('w', newline='') as output:
            writer = csv.DictWriter(output, fieldnames=['benchmark', 'phase', 'instructions', 'cycles',
                                                        'nominal_intervals', 'block_definitions', 'capture'],
                                    extrasaction='ignore')
            writer.writeheader()
            writer.writerows(self.steps)

    def phase(self, phase):
        for step in self.steps:
            step['phase'] = phase
        self.save()

    def firesim(self, step, timeout=None):
        args = ['firesim', step, '-c', str(self.run / 'runtime.yaml'), '-a', str(self.run / 'hwdb.yaml'),
                '-r', str(RECIPES)]
        if step == 'terminaterunfarm':
            args += ['-q']
        if timeout:
            args = ['timeout', '--signal=INT', '--kill-after=120s', timeout] + args
        with (self.run / f'{step}.log').open('w') as log:
            return subprocess.run(args, cwd=DEPLOY, stdout=log, stderr=subprocess.STDOUT).returncode

    def validate(self, item, step, results):
        job = results / item['job']
        step.update(capture=str(job))
        args = [sys.executable, str(CHECKER), str(job), '--interval', str(self.plan['interval'])]
        with (Path(item['folder']) / 'validation.log').open('w') as log:
            code = subprocess.run(args, stdout=log, stderr=subprocess.STDOUT).returncode
        if code != 0:
            step['phase'] = 'failed validation'
            return False
        v = json.loads((job / 'validation.json').read_text())
        (Path(item['folder']) / 'validation.json').write_text(json.dumps(v, indent=2) + '\n')
        step.update({k: v[k] for k in ['instructions', 'cycles', 'nominal_intervals', 'block_definitions']})
        if len(v['epochs']) != item['invocations']:
            step['phase'] = f'failed: {len(v["epochs"])} epochs, {item["invocations"]} invocations'
            return False
        step.update(phase='passed', finished_at=now())
        return True

    def simulate(self):
        """launchrunfarm, infrasetup and runworkload; returns the results directory."""
        free = shutil.disk_usage(ROOT).free / 1024**3
        assert free > self.plan['min_free_gb'], f'{free:.0f} GiB free, need {self.plan["min_free_gb"]}'
        self.phase('launchrunfarm')
        assert self.firesim('launchrunfarm') == 0, 'launchrunfarm failed'
        self.phase('infrasetup')
        assert self.firesim('infrasetup') == 0, 'infrasetup failed'
        self.phase('runworkload')
        (self.run / 'runworkload.started').write_text(now() + '\n')
        code = self.firesim('runworkload', self.plan['timeout'])
        (self.run / 'runworkload.exit').write_text(f'{code}\n')
        assert code == 0, 'runworkload failed or timed out'
        results = list((DEPLOY / 'results-workload').glob(f'*-{self.run.name}'))
        assert len(results) == 1, results
        return results[0]

    def collect(self):
        # The run farm is terminated as soon as runworkload has copied the results
        # back (or failed), before validation, so no instance idles during it.
        status = 0
        try:
            try:
                results = self.simulate()
            finally:
                self.save()
                cleanup = self.firesim('terminaterunfarm')
                (self.run / 'cleanup.exit').write_text(f'{cleanup}\n')
            self.phase('validation')
            with ThreadPoolExecutor(max_workers=4) as pool:
                passed = list(pool.map(lambda pair: self.validate(*pair, results),
                                       zip(self.plan['items'], self.steps)))
            self.save()
            assert all(passed), 'validation failed: ' + ', '.join(
                s['benchmark'] for s, ok in zip(self.steps, passed) if not ok)
        except BaseException as error:
            status = 1
            (self.run / 'failure.txt').write_text(f'{error!r}\n')
            print(repr(error), flush=True)
        finally:
            self.save()
        cleanup_file = self.run / 'cleanup.exit'
        cleanup = int(cleanup_file.read_text()) if cleanup_file.exists() else 1
        status = status or cleanup
        (self.run / 'manager.exit').write_text(f'{status}\n')
        return status

def collect(args):
    run = args.run.resolve()
    lock = (run / 'collection.lock').open('w')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    raise SystemExit(Collection(run).collect())


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('prepare', help='build images and write the run directory')
    p.add_argument('run', type=Path, help='new run directory; its name tags the run farm, workload and results')
    p.add_argument('--images', type=Path, required=True, help='specpack FireMarshal directory (holds images.json)')
    p.add_argument('--hw-config', required=True, help='hwdb entry name, for example firesim_mega_boom_v3_pmu_v2_interval_trace')
    p.add_argument('--agfi', required=True)
    p.add_argument('--interval', type=int, default=1000000, help='nominal interval length, instructions')
    p.add_argument('--timeout', required=True, help='runworkload wall-clock limit, timeout(1) syntax, for example 10h')
    p.add_argument('--min-free-gb', type=int, required=True, help='local free space required before launching')
    p.add_argument('--keypair', required=True, help='FIRESIM_KEYPAIR')
    p.add_argument('--ssh-key', required=True, help='FIRESIM_SSH_KEY')
    p.add_argument('--benchmarks', nargs='+', help='default: every benchmark in images.json')
    p.set_defaults(func=prepare)
    c = sub.add_parser('collect', help='run a prepared directory (through its launch.sh)')
    c.add_argument('run', type=Path)
    c.set_defaults(func=collect)
    args = parser.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
