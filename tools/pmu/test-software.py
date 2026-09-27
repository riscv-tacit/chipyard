#!/usr/bin/env python3
"""Build and run Baremetal-IDE's native TilePMU test on a complete BOOM core."""
import argparse
import os
from pathlib import Path
import shlex
import subprocess

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'tools/pmu/target/software'
CONFIG = 'SmallBoomV3PMUConfig'


def run(command, log, env, timeout=None):
    print(f'{shlex.join(map(str, command))}\n  log: {log}', flush=True)
    with log.open('w') as stream:
        stream.write(shlex.join(map(str, command)) + '\n')
        stream.flush()
        try:
            subprocess.run(list(map(str, command)), cwd=ROOT, env=env,
                           stdout=stream, stderr=subprocess.STDOUT,
                           check=True, timeout=timeout)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            print('\n'.join(log.read_text(errors='replace').splitlines()[-40:]))
            raise SystemExit(f'Failed; see {log}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--jobs', type=int, default=8)
    parser.add_argument('--seed', type=int, default=1, help='Verilator initialization seed')
    parser.add_argument('--skip-simulator-build', action='store_true',
                        help='use the existing simulator and its matching metadata')
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    conda = ROOT / '.conda-env'
    riscv = Path(env.get('RISCV', conda / 'riscv-tools'))
    env['RISCV'] = str(riscv)
    env['PATH'] = os.pathsep.join(map(str, [conda / 'bin', riscv / 'bin', env['PATH']]))
    env.setdefault('JAVA_TOOL_OPTIONS', '-Xmx12G -Xss8M -XX:ActiveProcessorCount=8')
    make = ['make', '-C', ROOT / 'sims/verilator', 'default', f'CONFIG={CONFIG}',
            f'-j{args.jobs}', 'CXX=g++', 'LINK=g++', 'AR=ar', 'PERL=perl', 'PYTHON3=python3']
    # This checkout's conda environment was relocated. Bypass the wrapper's old
    # prefix and the compiled-in paths to host tools without modifying conda.
    if (conda / 'bin/verilator_bin').exists():
        env['VERILATOR_ROOT'] = str(conda / 'share/verilator')
        # Make also calls `verilator --version` separately from VERILATOR.
        host_bin = OUT / 'host-bin'
        host_bin.mkdir(exist_ok=True)
        launcher = host_bin / 'verilator'
        launcher.write_text('#!/bin/sh\nexec ' +
                            shlex.quote(str(conda / 'bin/verilator_bin')) + ' "$@"\n')
        launcher.chmod(0o755)
        env['PATH'] = str(host_bin) + os.pathsep + env['PATH']
        make.append(f'VERILATOR={conda}/bin/verilator_bin --main --timing --cc --exe')
    if not args.skip_simulator_build:
        run(make, OUT / 'simulator-build.log', env)
    prefix = f'chipyard.harness.TestHarness.{CONFIG}'
    elaboration = ROOT / 'sims/verilator/generated-src' / prefix / prefix
    ide = ROOT / 'software/baremetal-ide'
    build = OUT / 'baremetal'
    run(['cmake', '-S', ide, '-B', build,
         f'-DCMAKE_TOOLCHAIN_FILE={ide}/riscv-gcc.cmake',
         f'-DTILE_PMU_MANIFEST={elaboration}.pmu.json',
         f'-DTILE_PMU_PLATFORM_JSON={elaboration}.json',
         f'-DTILE_PMU_SOFTWARE_DIR={ROOT}/generators/pmu/software'], OUT / 'software-configure.log', env)
    run(['cmake', '--build', build, '--target', 'tile-pmu-test', '-j', args.jobs],
        OUT / 'software-build.log', env)
    elf = build / 'examples/pmu-tests/tile-pmu-test.elf'
    simulator = ROOT / f'sims/verilator/simulator-chipyard.harness-{CONFIG}'
    log = OUT / 'tile-pmu-test.log'
    run([simulator, '+permissive', f'+verilator+seed+{args.seed}', '+dramsim',
         f'+dramsim_ini_dir={ROOT}/generators/testchipip/src/main/resources/dramsim2_ini',
         '+max-cycles=10000000', f'+loadmem={elf}', '+permissive-off', elf],
        log, env, timeout=600)
    output = log.read_text()
    if 'PASS TilePMU software test (' not in output or 'FAIL' in output:
        raise SystemExit(f'Missing successful target test result; see {log}')
    print(output)


if __name__ == '__main__':
    main()
