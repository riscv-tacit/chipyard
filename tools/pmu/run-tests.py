#!/usr/bin/env python3
"""Offline prototype checks using this checkout's cached Scala/Chisel/FIRRTL2 artifacts."""
import os
from pathlib import Path
import shutil
import subprocess

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
OUT = HERE / 'target' / 'prototype'
OUT.mkdir(parents=True, exist_ok=True)
classes = OUT / 'classes'
shutil.rmtree(classes, ignore_errors=True)
classes.mkdir()
export = ROOT / 'tools/firrtl2/bridge/src/target/streams/compile/dependencyClasspath/_global/streams/export'
if not export.exists():
    raise SystemExit('Missing cached build; run sbt firrtl2_bridge/compile cde/compile first.')


def relocate(entry):
    for suffix in ['tools/firrtl2/', 'tools/cde/', 'sims/firesim/', 'generators/']:
        if '/' + suffix in entry:
            return str(ROOT / (suffix + entry.split('/' + suffix, 1)[1]))
    return entry


cp = [relocate(p) for p in export.read_text().strip().split(os.pathsep)]
cp += [str(ROOT / p) for p in [
    'tools/firrtl2/bridge/src/target/scala-2.13/firrtl2_bridge_2.13-1.6.jar',
    'tools/cde/target/scala-2.13/cde_2.13-1.6.jar']]
scala_library = next(Path(p) for p in cp if '/scala-library/' in p)
version = scala_library.parent.name
maven = str(scala_library).split('/org/scala-lang/', 1)[0]
compiler = f'{maven}/org/scala-lang/scala-compiler/{version}/scala-compiler-{version}.jar'
plugin = f'{maven}/org/chipsalliance/chisel-plugin_{version}/6.7.0/chisel-plugin_{version}-6.7.0.jar'
for path in cp + [compiler, plugin]:
    if not Path(path).exists():
        raise SystemExit(f'Missing cached dependency: {path}')


def run(command, **kwargs):
    result = subprocess.run(command, **kwargs)
    if result.returncode:
        raise SystemExit(f"PMU check failed (exit {result.returncode}); see diagnostics above.")


java = str(ROOT / '.conda-env/bin/java')
sources = sorted(str(p) for base in [ROOT / 'generators/pmu/src', ROOT / 'generators/pmu/schema/src', HERE / 'src']
                 for p in base.rglob('*.scala'))
sources += [str(ROOT / 'generators/boom/src/main/scala/v3/common/pmu-event-logic.scala'),
            str(HERE / 'boom-tests/PMUEventLogicTest.scala')]
run([java, '-cp', os.pathsep.join([compiler] + cp), 'scala.tools.nsc.Main',
                '-classpath', os.pathsep.join(cp), f'-Xplugin:{plugin}', '-deprecation',
                '-d', str(classes), *sources])
run([java, '-cp', os.pathsep.join([str(classes)] + cp),
                'pmu.compiler.PMUPrototype', str(OUT)])
run([java, '-cp', os.pathsep.join([str(classes)] + cp),
                'pmu.compiler.PMUEventLogicTest', str(OUT)])
for top, driver in [('PMUPrototypeTop', HERE / 'src/test/cpp/PMUPrototype.cpp'),
                    ('PMUEventLogicTop', HERE / 'boom-tests/PMUEventLogicTest.cpp')]:
    env = dict(os.environ, PATH=str(ROOT / '.conda-env/bin') + os.pathsep + os.environ['PATH'],
               VERILATOR_ROOT=str(ROOT / '.conda-env/share/verilator'))
    run([str(ROOT / '.conda-env/bin/verilator_bin'), '--cc', '--exe', '--build', '-j', '2',
                    '-MAKEFLAGS', 'CXX=g++ LINK=g++ AR=ar PERL=perl PYTHON3=python3', '--top-module', top,
                    '--Mdir', str(OUT / f'obj_{top}'), str(OUT / f'{top}.v'), str(driver)], env=env)
    run([str(OUT / f'obj_{top}/V{top}')])
