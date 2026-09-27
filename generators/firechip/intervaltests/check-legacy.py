#!/usr/bin/env python3
"""Compile and elaborate the actual widget with FireSim's cached Scala/Chisel 3 dependencies.
Run the interface test first to produce the Chisel 6 bridge annotation.
"""
from pathlib import Path
import json
import os
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "generators/firechip/intervaltests/target/legacy"
OUT.mkdir(parents=True, exist_ok=True)
cache = Path.home() / ".cache/coursier/v1/https/repo1.maven.org/maven2"
compiler = [cache / f"org/scala-lang/{name}/2.13.10/{name}-2.13.10.jar"
            for name in ("scala-compiler", "scala-library", "scala-reflect")]
plugin = cache / "edu/berkeley/cs/chisel3-plugin_2.13.10/3.6.1/chisel3-plugin_2.13.10-3.6.1.jar"
export = ROOT / "sims/firesim/sim/target/streams/compile/dependencyClasspath/_global/streams/export"
# Cached build products may have been relocated with this workspace.
paths = []
for path in export.read_text().strip().split(os.pathsep):
    if "/sims/firesim/sim/" in path:
        path = str(ROOT / "sims/firesim/sim" / path.split("/sims/firesim/sim/", 1)[1])
    if not Path(path).exists():
        raise SystemExit(f"Missing cached dependency: {path}; build FireSim first")
    paths.append(path)
classpath = os.pathsep.join(paths)
java = shutil.which("java") or str(ROOT / ".conda-env/bin/java")
sources = [
    "generators/firechip/bridgeinterfaces/src/main/scala/TileTraceIO.scala",
    "generators/firechip/bridgeinterfaces/src/main/scala/IntervalTrace.scala",
    "generators/firechip/bridgeinterfaces/src/main/scala/IntervalTraceFormat.scala",
    "generators/firechip/goldengateimplementations/src/main/scala/interval/IntervalTraceDatapath.scala",
    "generators/firechip/goldengateimplementations/src/main/scala/IntervalTraceBridge.scala",
    "generators/firechip/intervaltests/src/legacy/scala/IntervalBridgeLegacyCheck.scala",
]
subprocess.run([java, "-cp", os.pathsep.join(map(str, compiler)), "scala.tools.nsc.Main",
                "-classpath", classpath, "-Xplugin:" + str(plugin), "-d", str(OUT)] + sources,
               cwd=ROOT, check=True)
# Import only interval annotations: target-only Chisel annotations are unrelated
# to this widget check and need not exist in the older host compiler.
annotation_input = ROOT / (sys.argv[1] if len(sys.argv) > 1 else
                          "generators/firechip/bridgestubs/target/interval-trace/bridge.anno.json")
annotations = [a for a in json.loads(annotation_input.read_text())
               if a.get("widgetClass") == "firechip.goldengateimplementations.IntervalTraceBridgeModule"]
assert annotations, "No interval bridge found"
annotation_file = OUT / "interval-only.anno.json"
annotation_file.write_text(json.dumps(annotations))
subprocess.run([java, "-cp", str(OUT) + os.pathsep + classpath, "IntervalBridgeLegacyCheck",
                str(annotation_file), str(OUT)], cwd=ROOT, check=True)
