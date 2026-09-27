#!/usr/bin/env python3
"""Exercise the real TileLink PMU adapter with byte masks and backpressure."""
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]
HERE = ROOT / 'tools/pmu'
OUT = HERE / 'target/mmio'
classes = OUT / 'classes'
classes.mkdir(parents=True, exist_ok=True)
export = ROOT / 'generators/chipyard/target/streams/compile/dependencyClasspath/_global/streams/export'
if not export.exists():
    raise SystemExit('Run sbt chipyard/compile first.')
cp = export.read_text().strip().split(os.pathsep)
scala = next(Path(p) for p in cp if '/scala-library/' in p)
version = scala.parent.name
maven = str(scala).split('/org/scala-lang/', 1)[0]
compiler = f'{maven}/org/scala-lang/scala-compiler/{version}/scala-compiler-{version}.jar'
plugin = f'{maven}/org/chipsalliance/chisel-plugin_{version}/6.7.0/chisel-plugin_{version}-6.7.0.jar'
for path in cp + [compiler, plugin]:
    if not Path(path).exists():
        raise SystemExit(f'Missing compiled dependency: {path}; run sbt chipyard/compile.')
java = str(ROOT / '.conda-env/bin/java')

def run(command, **kwargs):
    result = subprocess.run(command, cwd=ROOT, **kwargs)
    if result.returncode:
        raise SystemExit(f"PMU MMIO check failed (exit {result.returncode}); see diagnostics above.")

run([java, '-cp', os.pathsep.join([compiler] + cp), 'scala.tools.nsc.Main',
     '-classpath', os.pathsep.join(cp), f'-Xplugin:{plugin}', '-d', str(classes),
     str(HERE / 'mmio-tests/PMUMMIOTest.scala')])
run([java, '-cp', os.pathsep.join([str(classes)] + cp), 'pmu.tilelink.PMUMMIOTest', str(OUT)])
env = dict(os.environ, PATH=str(ROOT / '.conda-env/bin') + os.pathsep + os.environ['PATH'],
           VERILATOR_ROOT=str(ROOT / '.conda-env/share/verilator'))
run([str(ROOT / '.conda-env/bin/verilator_bin'), '--cc', '--exe', '--build', '-j', '2',
     '-MAKEFLAGS', 'CXX=g++ LINK=g++ AR=ar PERL=perl PYTHON3=python3', '--top-module', 'PMUMMIOTestTop',
     '--Mdir', str(OUT / 'obj_dir'), str(OUT / 'PMUMMIOTestTop.v'),
     str(HERE / 'mmio-tests/PMUMMIOTest.cpp')], env=env)
run([str(OUT / 'obj_dir/VPMUMMIOTestTop')])
