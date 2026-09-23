#!/usr/bin/env python3
"""TACIT artifact evaluation: the process-launch case study on MegaBoom v3.

A 10,000-iteration spawn-and-reap benchmark whose latency tail is set by RCU
callback batching. One FireMarshal workload, seven jobs in one FireSim launch:

    run-chores        driver map and jump-label patch map (decoder inputs)
    collect-10k       latency of every launch, control
    collect-10k-fix   the same with the RCU GP kthread moved to SCHED_BATCH at runtime
    trace-256         256 launches under TACIT, control        -> speedscope profile
    trace-256-fix     the same with the fix                     -> speedscope profile
    tp-full           collect-10k with the diagnosis tracepoints enabled
    tp-lite           the same minus the high-rate rcu_invoke_callback probe

    ./run.sh process_launch                  everything, resuming past steps whose outputs exist
    ./run.sh process_launch --list           the plan, no work done
    ./run.sh process_launch --force          redo every step
    ./run.sh process_launch --from analyse   redo this step and the ones after it

Steps, in order:  image -> fpga -> bundle -> decode -> analyse -> report
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent            # experiments/process_launch/
COMMON = HERE.parents[1]                          # bundle_run.py and the shared modules
sys.path.insert(0, str(COMMON))
import paths                                                          # noqa: E402
from firesim import dump_from_rootfs, newest_results_dir              # noqa: E402
from shell import (StageFailed, die, duration, have, ok, say, sh, skip,  # noqa: E402
                   step, warn)

# ------------------------------------------------------------------ the experiment
ANALYSIS = HERE / "analysis"
CONFIGS = HERE / "configs"
OUT = paths.OUT_ROOT / "process_launch"

WORKLOAD = "process-launch-f2"                    # in software/firemarshal/example-workloads
WORKLOAD_JSON = paths.FM / "example-workloads" / f"{WORKLOAD}.json"
GUEST_DIR = "/root/process-launch"
CHORES_JOB = f"{WORKLOAD}-run-chores"

MANAGER = ["firesim", "-c", "tacit-runtime/process-launch.yaml",
           "-a", "tacit-runtime/tacit-ae-hwdb.yaml",
           "-r", "tacit-runtime/tacit-ae-build-recipes.yaml"]
DECODE_GB_EACH = 4
SLOWEST = 5                       # per-iteration profiles: this many slowest launches + the median
ITERATIONS = 10_000
MIN_SAMPLES = 0.99 * ITERATIONS   # the console drops the odd line; a shortfall beyond
                                  # this means the run, not the console, was short
LATENCY = re.compile(r"^(\d+): (-?\d+)\r?$", re.M)


@dataclass(frozen=True)
class Job:
    name: str          # FireMarshal job name
    label: str         # for the tables
    @property
    def dir(self) -> str: return f"{WORKLOAD}-{self.name}"


# 10k-launch latency collections, in the order the tables print them
LATENCY_JOBS = (
    Job("collect-10k", "control"),
    Job("collect-10k-fix", "fix (rcu-batch)"),
    Job("tp-lite", "tracepoints, lite"),
    Job("tp-full", "tracepoints, full"),
)
# traced runs: bundled, decoded, and turned into speedscope profiles
TRACED = (
    Job("trace-256", "control"),
    Job("trace-256-fix", "fix"),
)
ALL_JOBS = (Job("run-chores", "chores"),) + LATENCY_JOBS + TRACED


def outdir(*parts) -> Path:
    d = OUT.joinpath(*parts)
    d.mkdir(parents=True, exist_ok=True)
    return d


def log_for(name: str) -> Path:
    return outdir("logs") / f"{name}.log"


def image_of(j: Job) -> Path:
    return paths.FM / "images" / "firechip" / j.dir / f"{j.dir}.img"


def bundle_of(j: Job) -> Path:
    return OUT / j.name / "bundle"


def fresh(output: Path, *inputs: Path) -> bool:
    if not output.exists() or not output.stat().st_size:
        return False
    t = output.stat().st_mtime
    return all(not i.exists() or i.stat().st_mtime <= t for i in inputs)


# ------------------------------------------------------------------------- image
def image(force: bool) -> None:
    """One marshal build: host-init compiles the four guest tools, one image per job."""
    say("image")
    step("firemarshal build")
    log = log_for("image")
    if all(image_of(j).exists() for j in ALL_JOBS) and not force:
        skip(f"{len(ALL_JOBS)} job images present")
    else:
        # marshal's dependency tracking does not cover the drivers baked into each
        # job's initramfs (or a changed kernel), so a forced rebuild must start clean:
        # a plain `marshal build` after a driver edit recompiles the module but keeps
        # the stale kernel binaries.
        if force:
            sh(["./marshal", "clean", WORKLOAD_JSON], log, cwd=paths.FM)
        sh(["./marshal", "-v", "build", WORKLOAD_JSON], log, cwd=paths.FM)
        ok()
    step("firemarshal install")
    sh(["./marshal", "install", WORKLOAD_JSON], log, cwd=paths.FM)
    ok()


# -------------------------------------------------------------------------- fpga
def results_dir() -> Path | None:
    try:
        r = newest_results_dir(WORKLOAD)
    except FileNotFoundError:
        return None
    need = [r / j.dir / "uartlog" for j in ALL_JOBS] + [r / CHORES_JOB / "jump_label_patch_map.txt"]
    return r if all(p.exists() for p in need) else None


def fpga(force: bool) -> Path:
    """One FireSim launch, seven slots. terminate_on_completion releases the run farm."""
    say("fpga")
    step(f"run farm: {len(ALL_JOBS)} slots")
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
        raise StageFailed("runworkload produced an incomplete results directory", 1, log)
    ok(r.name)
    return r


# ------------------------------------------------------------------------ bundle
def bundle(results: Path, force: bool) -> None:
    """Package the two traced captures with the binaries and patch map that explain them."""
    say("bundle")
    for j in TRACED:
        step(f"bundle capture: {j.name}")
        b = bundle_of(j)
        if (b / "config.json").exists() and not force:
            skip(j.name)
            continue
        if b.exists():
            shutil.rmtree(b)
        staged = outdir(j.name, ".binaries")
        apps = []
        for name in ("trace-submit", "dummy"):
            apps += ["--app", dump_from_rootfs(image_of(j), f"{GUEST_DIR}/{name}", staged / name)]
        sh([paths.PY, COMMON / "bundle_run.py",
            "--results", results / j.dir,
            "--template", CONFIGS / "decode.json",
            "--out", b,
            "--image", paths.FM / "images" / "firechip" / j.dir,
            "--jlmap", results / CHORES_JOB / "jump_label_patch_map.txt",
            *apps], log_for(f"bundle.{j.name}"))
        ok()


# ------------------------------------------------------------------------ decode
def decode(force: bool) -> None:
    """Decode both captures into speedscope profiles (plus the privilege and
    per-iteration breakdowns), in parallel."""
    say("decode")
    todo = [j for j in TRACED
            if force or not fresh(bundle_of(j) / "out" / "pl.speedscope.json", bundle_of(j) / "config.json")]
    for j in TRACED:
        if j not in todo:
            step(f"decode: {j.name}")
            skip("already decoded")
    if not todo:
        return
    step(f"decode: {', '.join(j.name for j in todo)}  ({len(todo)} in parallel)")
    for j in todo:
        (bundle_of(j) / "out").mkdir(parents=True, exist_ok=True)

    def one(j: Job) -> tuple[Job, int]:
        log = log_for(f"decode.{j.name}")
        with open(log, "ab") as f:
            f.write(f"\n$ {paths.DECODER} --config config.json   (cwd {bundle_of(j)})\n".encode())
            f.flush()
            rc = subprocess.run([str(paths.DECODER), "--config", "config.json"],
                                cwd=bundle_of(j), stdout=f, stderr=subprocess.STDOUT).returncode
        return j, rc

    t0 = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(todo)) as pool:
        for j, rc in pool.map(one, todo):
            if rc != 0:
                raise StageFailed(f"decode:{j.name}", rc, log_for(f"decode.{j.name}"))
    # the profiles are the deliverable: put them where a reviewer will find them
    prof = outdir("speedscope")
    for j in TRACED:
        shutil.copy(bundle_of(j) / "out" / "pl.speedscope.json", prof / f"{j.name}.speedscope.json")
    ok(f"{duration(time.monotonic() - t0)}; profiles in {prof}")


# ----------------------------------------------------------------------- analyse
def latencies(results: Path, j: Job) -> list[int]:
    """Per-launch latencies in ns from the job's uartlog; tv_nsec wraps (negative) dropped."""
    text = (results / j.dir / "uartlog").read_text(errors="replace")
    return [int(v) for _, v in LATENCY.findall(text) if int(v) >= 0]


def analyse(results: Path, force: bool) -> None:
    """The three latency figures, straight from the uartlogs."""
    say("analyse")
    figs = outdir("figures")
    uartlogs = [results / j.dir / "uartlog" for j in LATENCY_JOBS]
    log = log_for("analyse")
    jobs = [
        ("control distribution", figs / "control_distribution.png",
         [paths.PY, ANALYSIS / "plot_distribution.py",
          "--input", results / LATENCY_JOBS[0].dir / "uartlog",
          "--out", figs / "control_distribution.png"]),
        ("tracepoint overhead density", figs / "tracepoint_overhead_density.pdf",
         [paths.PY, ANALYSIS / "plot_tracepoint_overhead_density.py",
          "--results", results, "--out", figs / "tracepoint_overhead_density.pdf"]),
        ("fix tail (ccdf)", figs / "fix_ccdf.pdf",
         [paths.PY, ANALYSIS / "plot_fix_ccdf.py",
          "--results", results, "--out", figs / "fix_ccdf"]),
    ]
    for title, marker, cmd in jobs:
        step(title)
        if fresh(marker, *uartlogs) and not force:
            skip(marker.name)
            continue
        sh(cmd, log)
        ok(marker.name)

    # One file per iteration for the slowest launches and a median one, cut out of the
    # full profile: the launcher's root frame plus the child's that follows it. The
    # full 300 MB profile stays alongside; these open in seconds.
    for j in TRACED:
        step(f"per-iteration profiles: {j.name}")
        full = bundle_of(j) / "out" / "pl.speedscope.json"
        dest = outdir("speedscope", j.name)
        if any(dest.glob("*.slowest1.speedscope.json")) and fresh(next(dest.glob("*.slowest1.speedscope.json")), full) and not force:
            skip(str(dest))
            continue
        for old in dest.glob("*.speedscope.json"):
            old.unlink()
        sh([paths.PY, COMMON / "speedscope_iters.py", full, "--launcher", launcher_root(j),
            "--out", dest, "--stem", j.name, "--slowest", str(SLOWEST), "--median", "1"],
           log_for(f"iters.{j.name}"))
        ok(f"{len(list(dest.glob('*.speedscope.json')))} files in {dest}")


def launcher_root(j: Job) -> str:
    """The launcher's root frame name (u_<asid>), from the asid the bundler recorded for
    trace-submit. It is the process that spawns every child, so it has exactly one."""
    cfg = json.loads((bundle_of(j) / "config.json").read_text())
    for u in cfg["user_binaries"]:
        if Path(u["binary"]).name == "trace-submit":
            asids = u["asids"]
            if len(asids) != 1:
                raise StageFailed(f"trace-submit has {len(asids)} asids in {j.name}'s bundle, expected 1", 1, None)
            return f"u_{asids[0]}"
    raise StageFailed(f"no trace-submit entry in {j.name}'s bundle config", 1, None)


# ------------------------------------------------------------------------ report
def pct(xs: list[int], p: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, round((len(xs) - 1) * p / 100))] / 1000.0   # us


def report(results: Path) -> bool:
    say("results")
    passed = True
    print(f"  {'run':22} {'launches':>9} {'p50':>8} {'p90':>8} {'p99':>8} {'p99.9':>8} {'max':>9}   (us)")
    for j in LATENCY_JOBS:
        xs = latencies(results, j)
        if len(xs) < MIN_SAMPLES:
            passed = False
        flag = "" if len(xs) >= MIN_SAMPLES else "  SHORT -- INVESTIGATE"
        print(f"  {j.label:22} {len(xs):9,} {pct(xs, 50):8.0f} {pct(xs, 90):8.0f} "
              f"{pct(xs, 99):8.0f} {pct(xs, 99.9):8.0f} {max(xs) / 1000:9.0f}{flag}")
    for j in ALL_JOBS:
        text = (results / j.dir / "uartlog").read_text(errors="replace")
        if "*** PASSED ***" not in text:
            passed = False
            print(f"  {j.dir}: guest did not report PASSED -- INVESTIGATE")
    print(f"\n  every job passed and every collection is complete: {'YES' if passed else 'NO'}")
    print(f"\n  everything this run produced is under {OUT} :")
    print("    figures/control_distribution.png       launch latency distribution, control")
    print("    figures/tracepoint_overhead_density.*  control vs diagnosis tracepoints (lite, full)")
    print("    figures/fix_ccdf.*                     tail of control vs the RCU SCHED_BATCH fix")
    print("    speedscope/<run>/*.speedscope.json     one launch per file: the slowest and a median one")
    print("    speedscope/<run>.speedscope.json       the whole decoded trace (~300 MB; loads slowly)")
    print("                                           open either at https://www.speedscope.app")
    print("    <run>/bundle/                          the capture: trace, binaries, dwarf, patch map,")
    print("    <run>/bundle/out/                      and the decoder's outputs")
    print("    logs/<step>[.<job>].log                one transcript per step")
    return passed


# -------------------------------------------------------------------------- main
STEPS = ("image", "fpga", "bundle", "decode", "analyse")


def preflight() -> None:
    say("environment")
    paths.require("CY", "TD", "FM", "FS", "PY", "DECODER")
    for tool, fix in (("debugfs", "install e2fsprogs"),
                      ("firesim", "source sims/firesim/sourceme-manager.sh")):
        step(tool)
        ok() if have(tool) else die(f"{tool} not on PATH -- {fix}")
    step("python dependencies")
    try:
        import matplotlib, numpy  # noqa: F401
        ok()
    except ImportError as e:
        die(f"missing python package: {e.name} -- pip install matplotlib numpy")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force", action="store_true", help="redo steps whose outputs exist")
    ap.add_argument("--from", dest="from_step", choices=STEPS, metavar="STEP",
                    help=f"redo this step and the ones after it: {', '.join(STEPS)}")
    ap.add_argument("--list", action="store_true", help="show the plan and exit")
    args = ap.parse_args(argv)
    if args.list:
        print(f"workload : {WORKLOAD_JSON}\nslots    : {len(ALL_JOBS)} (one per job)")
        print(f"steps    : {', '.join(STEPS)}, report\nout      : {OUT}\n")
        for j in ALL_JOBS:
            print(f"  {j.name:16} {j.label}")
        return 0
    at = STEPS.index(args.from_step) if args.from_step else len(STEPS)
    redo = {s: args.force or i >= at for i, s in enumerate(STEPS)}
    started = time.monotonic()
    try:
        preflight()
        image(redo["image"])
        results = fpga(redo["fpga"])
        bundle(results, redo["bundle"])
        decode(redo["decode"])
        analyse(results, redo["analyse"])
    except StageFailed as e:
        die(e)
    passed = report(results)
    print(f"\n  total elapsed: {duration(time.monotonic() - started)}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
