#!/usr/bin/env python3
"""Run using this checkout's cached Scala/Chisel/FIRRTL2 build, offline."""
import os
from pathlib import Path
import subprocess

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
OUT = HERE / 'target'
OUT.mkdir(exist_ok=True)
classes = OUT / 'classes'
classes.mkdir(exist_ok=True)
export = ROOT / 'tools/firrtl2/bridge/src/target/streams/compile/dependencyClasspath/_global/streams/export'
if not export.exists():
    raise SystemExit('Missing cached build: build firrtl2_bridge with sbt first.')

def relocate(entry):
    # The workspace was copied from a different path; rebase project artifacts.
    for suffix in ['tools/firrtl2/', 'sims/firesim/', 'generators/']:
        if '/' + suffix in entry:
            return str(ROOT / (suffix + entry.split('/' + suffix, 1)[1]))
    return entry

cp = [relocate(p) for p in export.read_text().strip().split(os.pathsep)]
cp.append(str(ROOT / 'tools/firrtl2/bridge/src/target/scala-2.13/firrtl2_bridge_2.13-1.6.jar'))
scala_library = next(Path(p) for p in cp if '/scala-library/' in p)
version = scala_library.parent.name
maven = str(scala_library).split('/org/scala-lang/', 1)[0]
compiler = f'{maven}/org/scala-lang/scala-compiler/{version}/scala-compiler-{version}.jar'
plugin = f'{maven}/org/chipsalliance/chisel-plugin_{version}/6.7.0/chisel-plugin_{version}-6.7.0.jar'
for path in cp + [compiler, plugin]:
    if not Path(path).exists():
        raise SystemExit(f'Missing cached dependency: {path}')
java = str(ROOT / '.conda-env/bin/java')
classpath = os.pathsep.join(cp)
subprocess.run([java, '-cp', os.pathsep.join([compiler] + cp), 'scala.tools.nsc.Main',
                '-classpath', classpath, f'-Xplugin:{plugin}', '-deprecation',
                '-d', str(classes), str(HERE / 'RangeExperiment.scala')], check=True)
legacy_file = OUT / 'legacy-pre-gg.fir'
legacy_file.unlink(missing_ok=True)
subprocess.run([java, '-cp', os.pathsep.join([str(classes)] + cp),
                'pmurange.RangeExperiment', str(OUT), '--emit-only'], check=True)
legacy_export = ROOT / 'sims/firesim/sim/midas/target/streams/compile/dependencyClasspath/_global/streams/export'
legacy_cp = [relocate(p) for p in legacy_export.read_text().strip().split(os.pathsep)]
legacy_cp.append(str(ROOT / 'sims/firesim/sim/midas/target/scala-2.13/midas_2.13-1.0-SNAPSHOT.jar'))
for path in legacy_cp:
    if not Path(path).exists():
        raise SystemExit(f'Missing cached FireSim dependency: {path}')
legacy_library = next(Path(p) for p in legacy_cp if '/scala-library/' in p)
legacy_version = legacy_library.parent.name
legacy_compiler = f'{maven}/org/scala-lang/scala-compiler/{legacy_version}/scala-compiler-{legacy_version}.jar'
legacy_classes = OUT / 'legacy-classes'
legacy_classes.mkdir(exist_ok=True)
subprocess.run([java, '-cp', os.pathsep.join([legacy_compiler] + legacy_cp),
                'scala.tools.nsc.Main', '-classpath', os.pathsep.join(legacy_cp),
                '-d', str(legacy_classes), str(HERE / 'LegacyLowering.scala')], check=True)
subprocess.run([java, '-cp', os.pathsep.join([str(legacy_classes)] + legacy_cp),
                'pmurangelegacy.LegacyLowering', str(OUT)], check=True)
subprocess.run([java, '-cp', os.pathsep.join([str(classes)] + cp),
                'pmurange.RangeExperiment', str(OUT)], check=True)
