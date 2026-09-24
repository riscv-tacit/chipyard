#!/usr/bin/env python3
"""TACIT artifact evaluation: timing error against the TraceDoctor oracle, SPEC CPU2017 intspeed.

Each benchmark runs its ref input for three seconds untraced, then a five-second
window is traced by TACIT while the TraceDoctor oracle records every basic-block
boundary with its true cycle count, in two attribution conventions: cycles charged at the
block boundary, and split 1/n across the block's instructions. One FireSim launch, twelve slots: the eleven
windows and a chores job that exports the kernel's jump-label patch map. Each capture
is bundled and decoded once, with the oracle as reference clock, for TACIT itself and
for three emulated trace formats (TNT+CYC without and with return compression, and
timestamp counters), at basic-block and at function granularity; a second, block-only decode
against the 1/n oracle separates TACIT's real attribution error from the part the
fractional reference manufactures (the stacked figure).

    ./run.sh spec_oracle                  everything, resuming past steps whose outputs exist
    ./run.sh spec_oracle --list           the plan, no work done
    ./run.sh spec_oracle --to driver      host-only preparation (builds the SPEC overlay: needs $SPEC_DIR)
    ./run.sh spec_oracle --from analyse --force   redo the tables and figure

Steps, in order:  image -> driver -> fpga -> bundle -> decode -> analyse -> report
"""
from __future__ import annotations

import argparse
import concurrent.futures
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
COMMON = HERE.parents[1]
sys.path.insert(0, str(COMMON))
import paths                                                          # noqa: E402
import spec                                                           # noqa: E402
import steps                                                          # noqa: E402
from firesim import dump_from_rootfs, newest_results_dir              # noqa: E402
from shell import (StageFailed, die, duration, have, ok, say, sh, skip,  # noqa: E402
                   step, warn)

ANALYSIS = HERE / "analysis"
CONFIGS = HERE / "configs"
OUT = paths.OUT_ROOT / "spec_oracle"
WORKLOAD = "spec17-intspeed-ref-oracle"
WORKLOAD_JSON = paths.SOFTWARE / "spec2017" / "marshal-configs" / f"{WORKLOAD}.json"
CHORES_JOB = f"{WORKLOAD}-run-chores"
ORACLE = "oracle/bboracle_boundary.csv.zst"     # inside a bundle: cycles charged at the boundary
ORACLE_INST = "oracle/bboracle_inst.csv.zst"    # the same run, cycles split 1/n over the block
MANAGER = ["firesim", "-c", "tacit-runtime/spec-oracle.yaml",
           "-a", "tacit-runtime/tacit-ae-hwdb.yaml",
           "-r", "tacit-runtime/tacit-ae-build-recipes.yaml"]
THREADS_PER_DECODE = 8                          # frontend + receivers; sizes the pool

# The estimators, in the order the paper's figure shows them.
ESTIMATORS = (("tacit", "TACIT"), ("nret", "tnt_cyc_nret"),
              ("retc", "tnt_cyc_retcompressed"), ("tc", "tc"))
# "oracle_bb/nret/oracle_bb: epsilon(BB) = 100.47%   (...)" -- and for tacit itself
# "oracle_bb/tacit: epsilon(BB) = 9.51%"; function granularity reports epsilon(self).
EPS = re.compile(r"^oracle_\w+/([\w.]+)(?:/oracle_\w+)?: epsilon\((BB|self)\) = ([0-9.]+)%", re.M)


def outdir(*parts) -> Path:
    d = OUT.joinpath(*parts); d.mkdir(parents=True, exist_ok=True); return d


def log_for(name: str) -> Path:
    return outdir("logs") / f"{name}.log"


def job(b: spec.Bench) -> str: return f"{WORKLOAD}-{b.name}-traced"
def image_of(b: spec.Bench) -> Path: return paths.FM / "images" / "firechip" / job(b) / f"{job(b)}.img"
def bundle_of(b: spec.Bench) -> Path: return OUT / b.name / "bundle"


def fresh(output: Path, *inputs: Path) -> bool:
    if not output.exists() or not output.stat().st_size:
        return False
    t = output.stat().st_mtime
    return all(not i.exists() or i.stat().st_mtime <= t for i in inputs)


# ------------------------------------------------------------------------- image
def image(force: bool) -> None:
    """One marshal build: host-init compiles SPEC (ref inputs) from $SPEC_DIR into the
    overlay, then one rootfs per job."""
    say("image")
    step("firemarshal build (compiles SPEC ref: slow the first time)")
    log = log_for("image")
    imgs = [image_of(b) for b in spec.BENCHMARKS] + [paths.FM / "images" / "firechip" / CHORES_JOB / f"{CHORES_JOB}.img"]
    if all(p.exists() for p in imgs) and not force:
        skip(f"{len(imgs)} job images present")
    else:
        if force:
            sh(["./marshal", "clean", WORKLOAD_JSON], log, cwd=paths.FM)
        sh(["./marshal", "-v", "build", WORKLOAD_JSON], log, cwd=paths.FM)
        ok()
    step("firemarshal install")
    sh(["./marshal", "install", WORKLOAD_JSON], log, cwd=paths.FM)
    ok()


# ------------------------------------------------------------------------ driver
def driver(force: bool) -> None:
    say("driver")
    step("firesim builddriver")
    sh(MANAGER + ["builddriver"], log_for("driver"), cwd=paths.FS / "deploy")
    ok()


# -------------------------------------------------------------------------- fpga
def results_dir() -> Path | None:
    try:
        r = newest_results_dir(WORKLOAD)
    except FileNotFoundError:
        return None
    need = [r / CHORES_JOB / "jump_label_patch_map.txt"]
    for b in spec.BENCHMARKS:
        need += [r / job(b) / "uartlog", r / job(b) / "tacit0.out",
                 r / job(b) / "bboracle_boundary.csv.zst", r / job(b) / "bboracle_inst.csv.zst"]
    return r if all(p.exists() for p in need) else None


def fpga(force: bool) -> Path:
    """One launch, twelve slots. The runtime config's plusarg_passthrough turns on the
    TraceDoctor boundary oracle, triggered by the same window markers TACIT uses."""
    say("fpga")
    step(f"run farm: {len(spec.BENCHMARKS) + 1} slots")
    r = results_dir()
    if r and not force:
        skip(r.name)
        return r
    log = log_for("fpga")
    deploy = paths.FS / "deploy"
    sh(MANAGER + ["launchrunfarm"], log, cwd=deploy)
    sh(MANAGER + ["infrasetup"], log, cwd=deploy)
    sh(MANAGER + ["runworkload"], log, cwd=deploy)
    r = results_dir()
    if r is None:
        raise StageFailed("runworkload produced an incomplete results directory (trace, oracle or patch map missing)", 1, log)
    ok(r.name)
    return r


# ------------------------------------------------------------------------ bundle
def bundle(results: Path, force: bool) -> None:
    """Package each capture with its oracle, binaries and patch map, then point the
    decoder at the oracle: TACIT and the three emulated formats, all measured against
    the same boundary oracle rows, at block and function granularity in one pass."""
    say("bundle")
    for b in spec.BENCHMARKS:
        step(f"bundle capture: {b.name}")
        bd = bundle_of(b)
        if (bd / "config_inst.json").exists() and (bd / ORACLE).exists() and (bd / ORACLE_INST).exists() and not force:
            skip(b.name)
            continue
        if bd.exists():
            shutil.rmtree(bd)
        staged = outdir(b.name, ".binaries")
        app = dump_from_rootfs(image_of(b), f"{spec.GUEST_ROOT}/{b.dir}/{b.elf}", staged / b.elf)
        sh([paths.PY, COMMON / "bundle_run.py",
            "--results", results / job(b), "--template", CONFIGS / "decode.json", "--out", bd,
            "--image", paths.FM / "images" / "firechip" / job(b),
            "--jlmap", results / CHORES_JOB / "jump_label_patch_map.txt",
            "--app", app], log_for(f"bundle.{b.name}"))
        for o in (ORACLE, ORACLE_INST):
            if not (bd / o).exists():
                raise StageFailed(f"bundle for {b.name} has no {o}", 1, log_for(f"bundle.{b.name}"))
        ok(f"asids {inject_oracle(bd)}")


def inject_oracle(bd: Path) -> list[int]:
    """Point the decoder at the oracle: TACIT's own receivers and, for each emulated
    format, an error receiver against the same oracle rows. Returns the asids traced."""
    cfg = json.loads((bd / "config.json").read_text())
    asids = cfg["user_binaries"][0]["asids"]
    err = {"oracle_bb": {"path": ORACLE, "asids": asids},
           "oracle_func": {"path": ORACLE, "asids": asids, "top_n": 12}}
    cfg["receivers"] = {"oracle_bb": {"name": "tacit", "path": ORACLE, "asids": asids},
                        "oracle_func": {"name": "tacit", "path": ORACLE, "asids": asids, "top_n": 12}}
    cfg["emulations"] = [
        {"name": "nret", "format": "tnt_cyc_nret", "lim_tnt": 6, "error": err},
        {"name": "retc", "format": "tnt_cyc_retcompressed", "lim_tnt": 6, "error": err},
        {"name": "tc", "format": "tc", "interval": 1000, "error": err},
    ]
    (bd / "config.json").write_text(json.dumps(cfg, indent=2) + "\n")
    # The 1/n sweep: identical except for the oracle file and block granularity only,
    # so any difference is attributable to the attribution convention alone.
    err = {"oracle_bb": {"path": ORACLE_INST, "asids": asids}}
    cfg["receivers"] = {"oracle_bb": {"name": "tacit", "path": ORACLE_INST, "asids": asids}}
    cfg["emulations"] = [
        {"name": "nret", "format": "tnt_cyc_nret", "lim_tnt": 6, "error": err},
        {"name": "retc", "format": "tnt_cyc_retcompressed", "lim_tnt": 6, "error": err},
        {"name": "tc", "format": "tc", "interval": 1000, "error": err},
    ]
    (bd / "config_inst.json").write_text(json.dumps(cfg, indent=2) + "\n")
    return asids


# ------------------------------------------------------------------------ decode
def decode_log(b: spec.Bench) -> Path:
    return bundle_of(b) / "out" / "decode.log"      # the epsilon lines are on stdout


def decode_inst_log(b: spec.Bench) -> Path:
    return bundle_of(b) / "out" / "decode_inst.log"


def decoded(b: spec.Bench) -> bool:
    return all(fresh(log, bundle_of(b) / cfg) and EPS.search(log.read_text())
               for log, cfg in ((decode_log(b), "config.json"), (decode_inst_log(b), "config_inst.json")))


def decode(force: bool) -> None:
    say("decode")
    todo = [b for b in spec.BENCHMARKS if force or not decoded(b)]
    for b in spec.BENCHMARKS:
        if b not in todo:
            step(f"decode: {b.name}"); skip("already decoded")
    if not todo:
        return
    workers = max(1, min(len(todo), (os.cpu_count() or 8) // THREADS_PER_DECODE))
    step(f"decode: {len(todo)} captures x 2 oracles  ({workers} in parallel; 15-35 min each)")
    for b in todo:
        (bundle_of(b) / "out").mkdir(parents=True, exist_ok=True)

    def one(b: spec.Bench) -> tuple[spec.Bench, int]:
        # boundary oracle, then the 1/n oracle: sequential per capture, so the two never
        # share the bundle's out/ directory at the same time
        for log, cfg in ((decode_log(b), "config.json"), (decode_inst_log(b), "config_inst.json")):
            with open(log, "wb") as f:
                rc = subprocess.run([str(paths.DECODER), "--config", cfg], cwd=bundle_of(b),
                                    stdout=f, stderr=subprocess.STDOUT).returncode
            if rc != 0:
                return b, rc
        return b, 0

    t0 = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for b, rc in pool.map(one, todo):
            if rc != 0:
                raise StageFailed(f"decode:{b.name}", rc, decode_log(b))
    ok(duration(time.monotonic() - t0))


# ----------------------------------------------------------------------- analyse
def epsilons() -> dict[str, dict[str, dict[str, float]]]:
    """{benchmark: {"BB": {estimator: eps%}, "self": {...}, "BB_inst": {...}}} from the decode logs."""
    out: dict[str, dict[str, dict[str, float]]] = {}
    for b in spec.BENCHMARKS:
        if not decode_log(b).exists():
            continue
        d: dict[str, dict[str, float]] = {"BB": {}, "self": {}, "BB_inst": {}}
        for est, metric, val in EPS.findall(decode_log(b).read_text()):
            d[metric][est] = float(val)
        if decode_inst_log(b).exists():
            for est, metric, val in EPS.findall(decode_inst_log(b).read_text()):
                if metric == "BB":
                    d["BB_inst"][est] = float(val)
        out[b.name] = d
    return out


def analyse(force: bool) -> None:
    """Long-format tables of epsilon per benchmark and estimator, and the figure."""
    say("analyse")
    step("epsilon tables")
    logs = [decode_log(b) for b in spec.BENCHMARKS] + [decode_inst_log(b) for b in spec.BENCHMARKS]
    tables = (("BB", "eps_bb.csv"), ("self", "eps_func.csv"), ("BB_inst", "eps_bb_inst.csv"))
    if all(fresh(OUT / name, *logs) for _, name in tables) and not force:
        skip(", ".join(name for _, name in tables))
    else:
        eps = epsilons()
        for metric, name in tables:
            with open(OUT / name, "w", newline="") as fh:
                w = csv.writer(fh); w.writerow(["benchmark", "estimator", "estimator_label", "eps_pct"])
                for bench in sorted(eps):
                    for est, label in ESTIMATORS:
                        if est in eps[bench][metric]:
                            w.writerow([bench, est, label, f"{eps[bench][metric][est]:.2f}"])
        ok(", ".join(name for _, name in tables))
    step("figure: epsilon vs oracle, TACIT's BB bar split into semantic and structural error")
    csvs = [OUT / name for _, name in tables]
    if fresh(OUT / "eps_vs_oracle_stacked.pdf", *csvs) and not force:
        skip("eps_vs_oracle_stacked.pdf")
    else:
        sh([paths.PY, ANALYSIS / "plot_eps_stacked.py", OUT, OUT / "eps_bb_inst.csv"], log_for("analyse"), cwd=OUT)
        ok("eps_vs_oracle_stacked.pdf")


# ------------------------------------------------------------------------ report
def report() -> bool:
    say("results")
    eps = epsilons()
    passed = True
    for metric, title in (("BB", "epsilon at basic-block granularity (%), boundary oracle"),
                          ("BB_inst", "epsilon at basic-block granularity (%), 1/n oracle"),
                          ("self", "epsilon at function granularity, self time (%)")):
        print(f"\n  {title}")
        print(f"  {'benchmark':24}" + "".join(f"{label:>22}" for _, label in ESTIMATORS))
        for b in spec.BENCHMARKS:
            row = eps.get(b.name, {}).get(metric, {})
            cells = []
            for est, _ in ESTIMATORS:
                if est in row:
                    cells.append(f"{row[est]:22.2f}")
                else:
                    cells.append(f"{'MISSING':>22}"); passed = False
            print(f"  {b.name:24}" + "".join(cells))
    print(f"\n  every benchmark decoded against both oracles with all four estimators: {'YES' if passed else 'NO -- INVESTIGATE'}")
    print(f"\n  everything this run produced is under {OUT} :")
    print("    eps_bb.csv, eps_func.csv               epsilon per (benchmark, estimator), long format; eps_bb_inst.csv the 1/n oracle")
    print("    eps_vs_oracle_stacked.pdf/.png         the figure: epsilon per benchmark and estimator, TACIT's BB bar")
    print("                                           split into semantic (boundary oracle) and structural (1/n) error")
    print("    <benchmark>/bundle/                    trace, both oracles, binaries, dwarf, patch map; out/decode*.log")
    print("    logs/<step>[.<benchmark>].log          one transcript per step")
    return passed


# -------------------------------------------------------------------------- main
STEPS = ("image", "driver", "fpga", "bundle", "decode", "analyse")


def preflight(do) -> None:
    say("environment")
    paths.require("CY", "TD", "FM", "FS", "PY", "DECODER")
    if do["image"] is not None:
        spec.check_spec_dir()
    for tool, fix in (("debugfs", "install e2fsprogs"), ("firesim", "source sims/firesim/sourceme-manager.sh")):
        step(tool); ok() if have(tool) else die(f"{tool} not on PATH -- {fix}")
    step("python dependencies")
    try:
        import matplotlib, pandas  # noqa: F401
        ok()
    except ImportError as e:
        die(f"missing python package: {e.name} -- pip install matplotlib pandas")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    steps.add_args(ap, STEPS)
    args = ap.parse_args(argv)
    if args.list:
        print(f"workload : {WORKLOAD_JSON}\nslots    : {len(spec.BENCHMARKS) + 1} (one per benchmark window + chores)")
        print(f"steps    : {', '.join(STEPS)}, report\nout      : {OUT}\n")
        for b in spec.BENCHMARKS:
            print(f"  {b.name:24} {b.dir}/{b.elf}{b.args}")
        return 0
    do = steps.plan(STEPS, args)
    started = time.monotonic()
    stopped = f"\n  stopped after {args.to_step}; total elapsed: "
    try:
        preflight(do)
        if do["image"] is not None: image(do["image"])
        if do["driver"] is not None: driver(do["driver"])
        if do["fpga"] is None:
            print(stopped + duration(time.monotonic() - started)); return 0
        results = fpga(do["fpga"])
        if do["bundle"] is not None: bundle(results, do["bundle"])
        if do["decode"] is not None: decode(do["decode"])
        if do["analyse"] is not None: analyse(do["analyse"])
    except StageFailed as e:
        die(e)
    if not steps.reports(STEPS, args):
        print(stopped + duration(time.monotonic() - started)); return 0
    passed = report()
    print(f"\n  total elapsed: {duration(time.monotonic() - started)}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
