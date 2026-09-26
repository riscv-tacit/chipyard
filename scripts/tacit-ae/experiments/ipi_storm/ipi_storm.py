#!/usr/bin/env python3
"""TACIT artifact evaluation: the IPI-storm case study on dual-core MegaBoom v3.

Two threads of one process (software/firemarshal/example-workloads/ipi-storm): a reader
pinned to CPU 0 spins over a shared page while a writer pinned to CPU 1 flips the page's
protection 200 times. Every flip makes Linux flush the reader's TLB through an OpenSBI
remote fence, so the reader's hart takes an inter-processor interrupt into machine mode
(_trap_handler -> tlb_process) and returns. trace-submit traces both harts; the reader's
trace is decoded with the func_path receiver, which records every _trap_handler invocation
during the process that handled a TLB request, block by block, and the first ten blocks
after each return. One FireMarshal workload, two jobs in one FireSim launch:

    run-chores   driver map and jump-label patch map (decoder inputs)
    ipi-storm    the traced run: tacit0.out and tacit1.out, one trace per hart

    ./run.sh ipi_storm                  everything, resuming past steps whose outputs exist
    ./run.sh ipi_storm --list           the plan, no work done
    ./run.sh ipi_storm --force          redo every step
    ./run.sh ipi_storm --from analyse --force   redo analysis and the report, keep the rest
    ./run.sh ipi_storm --to driver      host-only preparation, stop before the run farm

Steps, in order:  image -> driver -> fpga -> bundle -> decode -> analyse -> report
(the step contract shared by every experiment is in steps.py)

Which trace is the reader's: tacit<N>.out is hart N (the trace ports are ordered by tile
ID), but Linux's CPU numbers are not hart numbers -- CPU 0 is whichever hart won OpenSBI's
cold-boot lottery, a timing race. The reader is pinned to CPU 0, so its trace is
tacit<boot hart>.out, and the boot hart is read from the run's own OpenSBI banner. Hart 0
won on this bitstream (2026-09-24); the paper's capture, on an older bitstream, had hart 1.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent            # experiments/ipi_storm/
COMMON = HERE.parents[1]                          # bundle_run.py and the shared modules
sys.path.insert(0, str(COMMON))
import paths                                                          # noqa: E402
import steps                                                          # noqa: E402
from firesim import exclusive, dump_from_rootfs, newest_results_dir   # noqa: E402
from shell import (StageFailed, die, duration, have, ok, say, sh, skip,  # noqa: E402
                   step)

# ------------------------------------------------------------------ the experiment
ANALYSIS = HERE / "analysis"
CONFIGS = HERE / "configs"
OUT = paths.OUT_ROOT / "ipi_storm"

WORKLOAD = "ipi-storm"                            # in software/firemarshal/example-workloads
WORKLOAD_JSON = paths.FM / "example-workloads" / f"{WORKLOAD}.json"
GUEST_DIR = "/root/ipi-storm"
CHORES_JOB = f"{WORKLOAD}-run-chores"
TRACED_JOB = f"{WORKLOAD}-ipi-storm"
JOBS = (CHORES_JOB, TRACED_JOB)
HARTS = 2
APPS = ("trace-submit", "hello")                  # order matches configs/decode.json

MANAGER = ["firesim", "-c", "tacit-runtime/ipi-storm.yaml",
           "-a", "tacit-runtime/tacit-ae-hwdb.yaml",
           "-r", "tacit-runtime/tacit-ae-build-recipes.yaml"]

BOOT_HART = re.compile(r"Boot HART ID\s*:\s*(\d+)")
STALLS = re.compile(r"^stall count: (\d+) (\d+)", re.M)
FLIPS = 200                  # hello.c: CHANGE_ITER = 100 iterations, two mprotect calls each
MIN_INVOCATIONS = 190        # one TLB-flush IPI per flip, plus a few unrelated ones


def outdir(*parts) -> Path:
    d = OUT.joinpath(*parts)
    d.mkdir(parents=True, exist_ok=True)
    return d


def log_for(name: str) -> Path:
    return outdir("logs") / f"{name}.log"


def image_of(job: str) -> Path:
    return paths.FM / "images" / "firechip" / job / f"{job}.img"


BUNDLE = OUT / "bundle"
FP_BB = BUNDLE / "out" / "ipi.func_path.bb.csv"
FP_POST = BUNDLE / "out" / "ipi.func_path.post_exit.csv"
FIGS = ("func_path_pareto", "func_path_post_exit")


def fresh(output: Path, *inputs: Path) -> bool:
    if not output.exists() or not output.stat().st_size:
        return False
    t = output.stat().st_mtime
    return all(not i.exists() or i.stat().st_mtime <= t for i in inputs)


# ------------------------------------------------------------------------- image
def image(force: bool) -> None:
    """One marshal build: host-init compiles hello and trace-submit, one image per job."""
    say("image")
    step("firemarshal build")
    log = log_for("image")
    if all(image_of(j).exists() for j in JOBS) and not force:
        skip(f"{len(JOBS)} job images present")
    else:
        # marshal's dependency tracking does not cover the drivers baked into each
        # job's initramfs (or a changed kernel), so a forced rebuild must start clean
        if force:
            sh(["./marshal", "clean", WORKLOAD_JSON], log, cwd=paths.FM)
        sh(["./marshal", "-v", "build", WORKLOAD_JSON], log, cwd=paths.FM)
        ok()
    step("firemarshal install")
    sh(["./marshal", "install", WORKLOAD_JSON], log, cwd=paths.FM)
    ok()


# ------------------------------------------------------------------------ driver
def driver(force: bool) -> None:
    """Build the FireSim host driver for the dual-core bitstream, without a run farm."""
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
    need = [r / j / "uartlog" for j in JOBS] + [r / CHORES_JOB / "jump_label_patch_map.txt"]
    need += [r / TRACED_JOB / f"tacit{h}.out" for h in range(HARTS)]
    return r if all(p.exists() for p in need) else None


def fpga(force: bool) -> Path:
    """One FireSim launch, two slots. terminate_on_completion releases the run farm."""
    say("fpga")
    step(f"run farm: {len(JOBS)} slots (dual-core bitstream; a few minutes)")
    r = results_dir()
    if r and not force:
        skip(r.name)
        return r
    log = log_for("fpga")
    deploy = paths.FS / "deploy"
    sh(MANAGER + ["launchrunfarm"], log, cwd=deploy)
    with exclusive("infrasetup"):     # one at a time across experiments: shared driver bundle
        sh(MANAGER + ["infrasetup"], log, cwd=deploy)
    sh(MANAGER + ["runworkload"], log, cwd=deploy)
    r = results_dir()
    if r is None:
        raise StageFailed("runworkload produced an incomplete results directory", 1, log)
    ok(r.name)
    return r


# ------------------------------------------------------------------------ bundle
def boot_hart(results: Path) -> int:
    """The hart OpenSBI booted Linux on, which Linux numbers CPU 0."""
    m = BOOT_HART.search((results / TRACED_JOB / "uartlog").read_text(errors="replace"))
    if not m:
        raise StageFailed(f"no 'Boot HART ID' in {TRACED_JOB}'s uartlog", 1, None)
    return int(m.group(1))


def bundle(results: Path, force: bool) -> None:
    """Package both traces with the binaries and patch map that explain them; the decoder
    config reads the reader's trace and counts only invocations inside hello."""
    say("bundle")
    hart = boot_hart(results)
    trace = f"tacit{hart}.out"
    step(f"bundle capture: reader on CPU 0 = boot hart {hart} -> {trace}")
    if (BUNDLE / "config.json").exists() and not force:
        skip(str(BUNDLE))
        return
    if BUNDLE.exists():
        shutil.rmtree(BUNDLE)
    staged = outdir(".binaries")
    apps = []
    for name in APPS:
        apps += ["--app", dump_from_rootfs(image_of(TRACED_JOB), f"{GUEST_DIR}/{name}", staged / name)]
    sh([paths.PY, COMMON / "bundle_run.py",
        "--results", results / TRACED_JOB,
        "--template", CONFIGS / "decode.json",
        "--out", BUNDLE,
        "--trace", trace,
        "--image", paths.FM / "images" / "firechip" / TRACED_JOB,
        "--jlmap", results / CHORES_JOB / "jump_label_patch_map.txt",
        *apps], log_for("bundle"))
    # func_path's ctx is hello's address space: the handler invocations that matter are
    # the ones that interrupted the reader, not the kernel's or trace-submit's
    cfg_path = BUNDLE / "config.json"
    cfg = json.loads(cfg_path.read_text())
    hello = [u for u in cfg["user_binaries"] if Path(u["binary"]).name == "hello"]
    if len(hello) != 1 or len(hello[0]["asids"]) != 1:
        raise StageFailed(f"expected one asid for hello in {cfg_path}, got {hello}", 1, log_for("bundle"))
    cfg["receivers"]["func_path"]["ctx"] = hello[0]["asids"][0]
    cfg_path.write_text(json.dumps(cfg, indent=2) + "\n")
    ok(f"{trace}; hello asid {hello[0]['asids'][0]}")


# ------------------------------------------------------------------------ decode
def decode(force: bool) -> None:
    say("decode")
    step("decode: reader's trace, func_path under _trap_handler")
    if fresh(FP_BB, BUNDLE / "config.json") and not force:
        skip("already decoded")
        return
    (BUNDLE / "out").mkdir(parents=True, exist_ok=True)
    log = log_for("decode")
    with open(log, "ab") as f:
        f.write(f"\n$ {paths.DECODER} --config config.json   (cwd {BUNDLE})\n".encode())
        f.flush()
        rc = subprocess.run([str(paths.DECODER), "--config", "config.json"],
                            cwd=BUNDLE, stdout=f, stderr=subprocess.STDOUT).returncode
    if rc != 0 or not FP_BB.exists():
        raise StageFailed("decode", rc or 1, log)
    ok()


# ----------------------------------------------------------------------- analyse
def analyse(force: bool) -> None:
    """The paper's two figures, each as PDF and PNG: cycle attribution over the handler's
    basic blocks (top 12 + the rest, cumulative line), and the latency of the first ten
    blocks after each return to the reader."""
    say("analyse")
    figs = outdir("figures")
    log = log_for("analyse")
    for name, script, csv in (("func_path_pareto", "plot_func_path_pareto.py", FP_BB),
                              ("func_path_post_exit", "plot_post_exit.py", FP_POST)):
        step(name)
        if fresh(figs / f"{name}.pdf", csv) and fresh(figs / f"{name}.png", csv) and not force:
            skip(f"{name}.pdf/.png")
            continue
        for ext in ("pdf", "png"):
            sh([paths.PY, ANALYSIS / script, csv, figs / f"{name}.{ext}"], log)
        ok(f"{name}.pdf/.png")


# ------------------------------------------------------------------------ report
def report(results: Path) -> bool:
    import pandas as pd
    sys.path.insert(0, str(ANALYSIS))
    from plot_func_path_pareto import named_bbs                      # noqa: E402

    say("results")
    passed = True
    for j in JOBS:
        if "*** PASSED ***" not in (results / j / "uartlog").read_text(errors="replace"):
            passed = False
            print(f"  {j}: guest did not report PASSED -- INVESTIGATE")
    text = (results / TRACED_JOB / "uartlog").read_text(errors="replace")
    stalls = STALLS.search(text)
    cfg = json.loads((BUNDLE / "config.json").read_text())

    bb = pd.read_csv(FP_BB)
    bb.columns = bb.columns.str.strip()
    inv = bb["invocation"].nunique()
    agg = (bb.groupby(["bb_start", "bb_end"])["duration"].sum()
             .sort_values(ascending=False))
    total = agg.sum()
    top4, top12 = (100 * agg.head(n).sum() / total for n in (4, 12))
    named = sum(1 for (s, _) in agg.head(12).index if s in named_bbs)
    post = pd.read_csv(FP_POST)
    post.columns = post.columns.str.strip()
    means = post.groupby("bb_index")["duration"].mean()
    steady = means[means.index >= 3].mean()

    print(f"  run                     {results.name}")
    print(f"  reader's trace          {cfg['encoded_trace']}  (Linux CPU 0 = boot hart {boot_hart(results)})")
    if stalls:
        print(f"  encoder stalls          hart 0: {stalls.group(1)}, hart 1: {stalls.group(2)}")
    print()
    rows = (("IPI handler invocations", f"{inv}"),
            ("cycles per invocation, mean", f"{total / inv:.0f}"),
            ("top 4 blocks, share of cycles", f"{top4:.1f}%"),
            ("top 12 blocks, share of cycles", f"{top12:.1f}%"),
            *((f"block {i + 1} after return, mean cycles", f"{means.get(i, float('nan')):.1f}") for i in range(3)),
            ("blocks 4-10 after return, mean cycles", f"{steady:.1f}"))
    for label, value in rows:
        print(f"  {label:38} {value:>10}")
    print(f"  (durations in core cycles)")
    if inv < MIN_INVOCATIONS:
        passed = False
        print(f"\n  only {inv} invocations for {FLIPS} protection flips -- wrong trace or a broken decode; INVESTIGATE")
    if named < 12:
        print(f"\n  note: {12 - named} of the top 12 blocks have no name in plot_func_path_pareto.py "
              f"(OpenSBI laid out differently from the paper's build); their bars show the address")
    print(f"\n  guests passed and the reader's handler invocations are all there: {'YES' if passed else 'NO'}")
    print(f"\n  everything this run produced is under {OUT} :")
    print("    figures/func_path_pareto.{pdf,png}     cycle attribution over the IPI handler's basic blocks")
    print("    figures/func_path_post_exit.{pdf,png}  latency of the first ten blocks after each trap return")
    print("    bundle/                                the capture: both traces, binaries, dwarf, patch map,")
    print("    bundle/out/                            and the decoder's outputs (ipi.func_path*.csv)")
    print("    logs/<step>.log                        one transcript per step")
    return passed


# -------------------------------------------------------------------------- main
STEPS = ('image', 'driver', 'fpga', 'bundle', 'decode', 'analyse')


def preflight() -> None:
    say("environment")
    paths.require("CY", "TD", "FM", "FS", "PY", "DECODER")
    for tool, fix in (("debugfs", "install e2fsprogs"),
                      ("firesim", "source sims/firesim/sourceme-manager.sh")):
        step(tool)
        ok() if have(tool) else die(f"{tool} not on PATH -- {fix}")
    step("python dependencies")
    try:
        import matplotlib, pandas  # noqa: F401
        ok()
    except ImportError as e:
        die(f"missing python package: {e.name} -- pip install matplotlib pandas")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    steps.add_args(ap, STEPS)
    args = ap.parse_args(argv)
    if args.list:
        print(f"workload : {WORKLOAD_JSON}\nslots    : {len(JOBS)} (one per job)")
        print(f"steps    : {', '.join(STEPS)}, report\nout      : {OUT}\n")
        for j in JOBS:
            print(f"  {j}")
        return 0
    do = steps.plan(STEPS, args)          # step -> None (do not run), False (if needed), True (redo)
    started = time.monotonic()
    stopped = f"\n  stopped after {args.to_step}; total elapsed: "
    try:
        preflight()
        if do["image"] is not None: image(do["image"])
        if do["driver"] is not None: driver(do["driver"])
        if do["fpga"] is None:
            print(stopped + duration(time.monotonic() - started))
            return 0
        results = fpga(do["fpga"])
        if do["bundle"] is not None: bundle(results, do["bundle"])
        if do["decode"] is not None: decode(do["decode"])
        if do["analyse"] is not None: analyse(do["analyse"])
    except StageFailed as e:
        die(e)
    if not steps.reports(STEPS, args):
        print(stopped + duration(time.monotonic() - started))
        return 0
    passed = report(results)
    print(f"\n  total elapsed: {duration(time.monotonic() - started)}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
