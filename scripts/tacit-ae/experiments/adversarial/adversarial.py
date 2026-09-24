#!/usr/bin/env python3
"""TACIT artifact evaluation: worst-case encoder overhead on adversarial micro-benchmarks.

Bare-metal kernels built to drive the TACIT encoder toward its worst case on the 4-wide
MegaBoom, with the trace going to the DMA sink: not-taken branches with 0/1/3 ALU fillers
between them, taken branches likewise, table-driven indirect jumps whose targets sit near,
8 KiB and 16 MiB away (1, 2 and 3 packetizer cycles per packet), call/return storms and
branch bursts queued behind DRAM misses. Each kernel runs a warm-up, then interleaved
untraced and traced repetitions of identical code, printing one RESULT line per run with
cycles, instructions, encoder stall cycles, trace bytes and DMA source-ready stalls.
Three ELFs, three FPGA slots, one launch; no Linux, no decode.

    ./run.sh adversarial                  everything, resuming past steps whose outputs exist
    ./run.sh adversarial --list           the plan, no work done
    ./run.sh adversarial --to driver      host-only preparation (cross-compiles the ELFs)
    ./run.sh adversarial --from analyse --force   redo the table and figure

Steps, in order:  image -> driver -> fpga -> analyse -> report
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
COMMON = HERE.parents[1]
sys.path.insert(0, str(COMMON))
import paths                                                          # noqa: E402
import steps                                                          # noqa: E402
from firesim import newest_results_dir                                # noqa: E402
from shell import (StageFailed, die, duration, have, ok, out, say, sh,  # noqa: E402
                   skip, step)

ANALYSIS = HERE / "analysis"
OUT = paths.OUT_ROOT / "adversarial"
SUITE = paths.SOFTWARE / "adversarial-tacit-bmarks"
WORKLOAD = "tacit-overhead-report"
WORKLOAD_JSON = SUITE / f"{WORKLOAD}.json"
ELFS = {"branch": "report-branch.elf", "indirect": "report-indirect.elf", "mem": "report-mem.elf"}
JOBS = [f"{WORKLOAD}-{j}" for j in ELFS]
MANAGER = ["firesim", "-c", "tacit-runtime/adversarial.yaml",
           "-a", "tacit-runtime/tacit-ae-hwdb.yaml",
           "-r", "tacit-runtime/tacit-ae-build-recipes.yaml"]
RESULT = re.compile(r"^RESULT bench=(\S+) mode=(\S+) rep=(\d+) .*cycles=(\d+)", re.M)


def outdir(*parts) -> Path:
    d = OUT.joinpath(*parts); d.mkdir(parents=True, exist_ok=True); return d


def log_for(name: str) -> Path:
    return outdir("logs") / f"{name}.log"


def fresh(output: Path, *inputs: Path) -> bool:
    if not output.exists() or not output.stat().st_size:
        return False
    t = output.stat().st_mtime
    return all(not i.exists() or i.stat().st_mtime <= t for i in inputs)


# ------------------------------------------------------------------------- image
def image(force: bool) -> None:
    """FireMarshal's host-init cross-compiles the three ELFs (`make all` in src/); the
    workload is bare metal, so `build -d` skips the rootfs and `install` (without -d,
    which marshal does not support for nodisk) writes the FireSim workload file."""
    say("image")
    step("firemarshal build -d (cross-compiles the kernels)")
    log = log_for("image")
    elfs = [SUITE / "src" / "build" / e for e in ELFS.values()]
    if all(p.exists() for p in elfs) and not force:
        skip(f"{len(elfs)} ELFs present")
    else:
        if force:
            sh(["make", "-C", SUITE / "src", "clean"], log)
        sh(["./marshal", "-d", "-v", "build", WORKLOAD_JSON], log, cwd=paths.FM)
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
def complete(r: Path) -> bool:
    """Every job's uartlog carries traced and untraced RESULT lines."""
    for j in JOBS:
        u = r / j / "uartlog"
        if not u.exists():
            return False
        modes = {m.group(2) for m in RESULT.finditer(u.read_text(errors="replace"))}
        if not {"untraced", "traced"} <= modes:
            return False
    return True


def results_dir() -> Path | None:
    try:
        r = newest_results_dir(WORKLOAD)
    except FileNotFoundError:
        return None
    return r if complete(r) else None


def fpga(force: bool) -> Path:
    say("fpga")
    step(f"run farm: {len(JOBS)} slots (bare metal; a few minutes each)")
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
        raise StageFailed("runworkload produced an incomplete results directory (a job lacks RESULT lines)", 1, log)
    ok(r.name)
    return r


# ----------------------------------------------------------------------- analyse
def analyse(results: Path, force: bool) -> None:
    say("analyse")
    step("per-benchmark table")
    logs = [results / j / "uartlog" for j in JOBS]
    if fresh(OUT / "overhead.txt", *logs) and not force:
        skip("overhead.txt")
    else:
        (OUT / "overhead.txt").write_text(out([paths.PY, ANALYSIS / "parse_results.py", results]) + "\n")
        ok("overhead.txt")
    step("figure: runtime, % of untraced")
    if fresh(OUT / "fig.overhead.pdf", *logs) and not force:
        skip("fig.overhead.pdf")
    else:
        sh([paths.PY, ANALYSIS / "plot_overhead.py", results, OUT / "fig.overhead"], log_for("analyse"))
        ok("fig.overhead.pdf, fig.overhead.png")


# ------------------------------------------------------------------------ report
EXPECT = {"nt_f0": 3.21, "tjr_near": 1.78, "tjr_d24": 1.44}   # the paper's headline slowdowns (d64)


def report() -> bool:
    say("results")
    table = (OUT / "overhead.txt").read_text()
    print()
    for line in table.splitlines():
        print("  " + line)
    slow = {}
    for line in table.splitlines()[1:]:
        f = line.split()
        if len(f) > 3:
            try:
                slow[f[0]] = float(f[3])
            except ValueError:
                pass
    passed = all(b in slow for b in EXPECT) and slow.get("nt_f0", 0) > 2.5
    print("\n  headline slowdowns (traced / untraced cycles), paper value in brackets:")
    for b, want in EXPECT.items():
        print(f"    {b:10} {slow.get(b, float('nan')):5.2f}x   [{want:.2f}x]")
    print(f"\n  every family reported and nt_f0 saturates the 1 packet/cycle drain: {'YES' if passed else 'NO -- INVESTIGATE'}")
    print(f"\n  everything this run produced is under {OUT} :")
    print("    overhead.txt                per benchmark: cycles, slowdown, stall fraction, bytes/insn, DMA source stalls")
    print("    fig.overhead.pdf/.png       the figure: runtime as % of untraced, by family and knob")
    print("    logs/<step>.log             one transcript per step")
    return passed


# -------------------------------------------------------------------------- main
STEPS = ("image", "driver", "fpga", "analyse")


def preflight() -> None:
    say("environment")
    paths.require("CY", "FM", "FS", "PY")
    for tool, fix in (("riscv64-unknown-elf-gcc", "source env.sh (chipyard conda toolchain)"),
                      ("firesim", "source sims/firesim/sourceme-manager.sh")):
        step(tool); ok() if have(tool) else die(f"{tool} not on PATH -- {fix}")
    step("python dependencies")
    try:
        import matplotlib  # noqa: F401
        ok()
    except ImportError as e:
        die(f"missing python package: {e.name} -- pip install matplotlib")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    steps.add_args(ap, STEPS)
    args = ap.parse_args(argv)
    if args.list:
        print(f"workload : {WORKLOAD_JSON}\nslots    : {len(JOBS)} (one bare-metal ELF each)")
        print(f"steps    : {', '.join(STEPS)}, report\nout      : {OUT}\n")
        for j, e in ELFS.items():
            print(f"  {j:10} src/build/{e}")
        return 0
    do = steps.plan(STEPS, args)
    started = time.monotonic()
    stopped = f"\n  stopped after {args.to_step}; total elapsed: "
    try:
        preflight()
        if do["image"] is not None: image(do["image"])
        if do["driver"] is not None: driver(do["driver"])
        if do["fpga"] is None:
            print(stopped + duration(time.monotonic() - started)); return 0
        results = fpga(do["fpga"])
        if do["analyse"] is not None: analyse(results, do["analyse"])
    except StageFailed as e:
        die(e)
    if not steps.reports(STEPS, args):
        print(stopped + duration(time.monotonic() - started)); return 0
    passed = report()
    print(f"\n  total elapsed: {duration(time.monotonic() - started)}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
