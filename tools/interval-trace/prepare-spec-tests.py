#!/usr/bin/env python3
"""Stage existing CPU2017 test runsets as isolated, marker-enabled FireMarshal images.

Inputs are read-only. Saved SPEC host environment settings are never copied into
images or executed. Every speccmds invocation is retained, including all XZ cases.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]
SUITES = ROOT / 'software/spec2017/speckle/commands'
LAUNCHER = ROOT / 'software/spec2017/speckle/submit/interval-submit'


def commands(path):
    """Keep executable command rows, discarding SPEC's captured environment."""
    result = []
    for line in path.read_text().splitlines():
        if not line or line.startswith(('-E ', '-C ', '#')):
            continue
        match = re.search(r'(?:^|\s)(\S+_base\.host-m64|\S+/bin/specperl)\s+(.*)', line)
        if match:
            result.append((match.group(1), match.group(2)))
        elif line.strip() not in ('-r', '-N C'):
            raise ValueError(f'{path}: unsupported command row (not printed; may contain host settings)')
    if not result:
        raise ValueError(f'{path}: no executable command rows')
    return result


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def comparison(spec, benchmark, program, args):
    if Path(program).name == 'specperl':
        executable = spec / 'bin/specperl'
    else:
        executable = spec / 'benchspec/CPU' / benchmark / 'exe' / Path(program).name
    if not executable.exists():
        raise FileNotFoundError(executable)
    return shlex.quote(str(executable)) + ' ' + args


def local_files(args):
    # Comparison inputs and shell redirections are copied back if they exist.
    # Numeric/options tokens do not name files; checking them is harmless.
    return {token for token in shlex.split(args)
            if token and not token.startswith(('/', '-')) and token not in ('>', '>>', '<', '2>', '2>>')
            and '..' not in Path(token).parts and not token.isdigit()}


def stage(spec, suite, benchmark, output):
    source = spec / 'benchspec/CPU' / benchmark
    runs = sorted((source / 'run').glob('run_base_test_host-m64.*'))
    if not runs:
        raise FileNotFoundError(f'{benchmark}: no host test runsetup directory')
    run = runs[0]
    invocations = commands(run / 'speccmds.cmd')
    checks = commands(run / 'compare.cmd')
    binaries = {}
    for program, _ in invocations:
        target_name = Path(program).name.replace('.host-m64', '.riscv-64')
        binary = source / 'exe' / target_name
        if not binary.exists():
            raise FileNotFoundError(binary)
        binaries[target_name] = binary
    dest = output / benchmark
    overlay = dest / 'overlay'
    inputs = overlay / benchmark
    inputs.mkdir(parents=True, exist_ok=True)
    # Only runset inputs are copied: never the saved environment/command files,
    # host executables, SPEC control files, or logs left by host input generation.
    excluded = {'speccmds.cmd', 'compare.cmd', 'control', 'inputgen.cmd'}
    for path in run.iterdir():
        if path.name in excluded or '_base.' in path.name or path.name.startswith('inputgen') or path.name == 'log.dec':
            continue
        if path.is_dir():
            shutil.copytree(path, inputs / path.name, dirs_exist_ok=True)
        else:
            shutil.copy2(path, inputs / path.name)
    for name, path in binaries.items():
        shutil.copy2(path, inputs / name)
    shutil.copy2(LAUNCHER, overlay / 'interval-submit')
    marker_text = subprocess.check_output([str(ROOT / '.conda-env/riscv-tools/bin/riscv64-unknown-linux-gnu-objdump'),
                                           '-d', str(LAUNCHER)], text=True)
    markers = {kind: [int(pc, 16) for pc in re.findall(r'^\s*([0-9a-f]+):\s+' + word + r'\s', marker_text, re.M)]
               for kind, word in [('start', '00008013'), ('end', '00010013')]}
    assert all(markers.values())
    targets = []
    outputs = set()
    for index, (program, args) in enumerate(invocations):
        name = Path(program).name.replace('.host-m64', '.riscv-64')
        targets.append(dict(epoch=index + 1, executable=name, arguments_shell=args))
        # stdout/stderr redirection filenames only (not all benchmark inputs).
        outputs.update(re.findall(r'(?:^|\s)(?:2)?>>?\s*(\S+)', args))
    for _, args in checks:
        outputs.update(local_files(args))
    metadata = dict(benchmark=benchmark, suite=suite, input='test', threads=1,
                    invocations=targets, marker_pcs=markers,
                    comparisons=[comparison(spec, benchmark, program, args) for program, args in checks],
                    spec_dir=str(spec), binaries={name: dict(source=str(path), sha256=digest(path)) for name, path in binaries.items()},
                    launcher_sha256=digest(LAUNCHER), source_runset=str(run))
    (dest / 'benchmark.json').write_text(json.dumps(metadata, indent=2) + '\n')
    (overlay / 'benchmark.json').write_text(json.dumps(metadata, indent=2) + '\n')
    lines = ['#!/bin/sh', 'export OMP_NUM_THREADS=1', 'export OMP_STACKSIZE=120M',
             'ulimit -s unlimited', 'mkdir -p /output/status',
             'cp /benchmark.json /output/benchmark.json', 'cd /' + shlex.quote(benchmark) + ' || exit 1',
             'overall=0']
    for i, command in enumerate(targets):
        lines += [f"echo 'BEGIN spec-interval {benchmark} invocation={i}'",
                  '/interval-submit --status-file /output/status/' + f'{i:04d}' + ' ./' + shlex.quote(command['executable']) + ' ' + command['arguments_shell'],
                  'status=$?', 'if [ "$status" -ne 0 ]; then overall=$status; fi',
                  f"echo \"END spec-interval {benchmark} invocation={i} status=$status\""]
    for name in sorted(outputs):
        # No host absolute paths or parent traversals can become target output paths.
        if name.startswith('/') or '..' in Path(name).parts:
            raise ValueError(f'unsafe result path: {name}')
        lines += ['if [ -f ' + shlex.quote(name) + ' ]; then',
                  '  mkdir -p ' + shlex.quote('/output/' + str(Path(name).parent)),
                  '  cp ' + shlex.quote(name) + ' ' + shlex.quote('/output/' + name), 'fi']
    lines += ['echo "$overall" > /output/exitcode',
              'if [ "$overall" -eq 0 ]; then', f"  echo 'PASS spec-interval {benchmark} invocations={len(targets)}'",
              'else', f'  echo "FAIL spec-interval {benchmark} status=$overall"', 'fi', 'exit "$overall"']
    script = overlay / 'run-interval-test.sh'
    script.write_text('\n'.join(lines) + '\n'); script.chmod(0o755)
    subprocess.run(['sh', '-n', str(script)], check=True)
    name = 'spec17-interval-test-' + benchmark.replace('.', '-')
    config = dict(name=name, base='br-base.json', workdir=str(dest), overlay=str(overlay),
                  **{'rootfs-size': '2GiB', 'command': './run-interval-test.sh', 'outputs': ['/output'],
                     'firesim': {'simulation_outputs': ['uartlog', 'intervaltrace-0-blocks.csv', 'intervaltrace-0-intervals.jsonl', 'intervaltrace-0.json']}})
    config_path = dest / (name + '.json')
    config_path.write_text(json.dumps(config, indent=2) + '\n')
    return dict(benchmark=benchmark, suite=suite, invocations=len(targets), config=str(config_path),
                workload=name + '.json', metadata=str(dest / 'benchmark.json'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--spec-dir', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--suites', nargs='+', choices=('intspeed', 'intrate', 'fpspeed', 'fprate'), default=['intspeed'])
    args = parser.parse_args()
    args.output = args.output.resolve(); args.output.mkdir(parents=True, exist_ok=True)
    prepared, missing = [], []
    for suite in args.suites:
        for command_file in sorted((SUITES / suite).glob('*.test.cmd')):
            benchmark = command_file.name.removesuffix('.test.cmd')
            try:
                prepared.append(stage(args.spec_dir.resolve(), suite, benchmark, args.output))
            except FileNotFoundError as error:
                missing.append(dict(benchmark=benchmark, suite=suite, reason=str(error)))
    manifest = dict(prepared=prepared, missing=missing, suites=args.suites)
    (args.output / 'images.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(json.dumps(dict(prepared=len(prepared), invocations=sum(b['invocations'] for b in prepared), missing=missing), indent=2))


if __name__ == '__main__':
    main()
