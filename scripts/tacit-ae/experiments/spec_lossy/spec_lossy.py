#!/usr/bin/env python3
"""TACIT artifact evaluation: trace coverage in lossy mode on SPEC CPU2017 intspeed.

Every intspeed benchmark runs its test input once, traced by TACIT in lossy mode with the
trace streamed over DMA: when the trace buffer fills, the encoder pauses instead of
stalling the core and resumes with a synchronisation packet. The counters at exit say how
much of each run fell into such gaps, i.e. how much of the program a stall-free tracer
would have missed. One FireSim launch, eleven slots.

    ./run.sh spec_lossy                   everything, resuming past steps whose outputs exist
    ./run.sh spec_lossy --list            the plan, no work done
    ./run.sh spec_lossy --to driver       host-only preparation (builds the SPEC overlay: needs $SPEC_DIR)
    ./run.sh spec_lossy --from analyse --force   redo the table and figure

Steps, in order:  image -> driver -> fpga -> analyse -> report
"""
from __future__ import annotations

import argparse
import re
import csv
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
COMMON = HERE.parents[1]
sys.path.insert(0, str(COMMON))
import paths                                                          # noqa: E402
import spec                                                           # noqa: E402
import steps                                                          # noqa: E402
from firesim import exclusive, newest_results_dir                                # noqa: E402
from shell import (StageFailed, die, duration, have, ok, say, sh, skip,  # noqa: E402
                   step)

ANALYSIS = HERE / "analysis"
INPUTS = ("test", "train", "ref")
DEFAULT_INPUT = "train"      # what `./run.sh all` runs; the paper's lossy numbers are train

# Set by configure(): everything that depends on the SPEC input set. One workload JSON
# per input (software/spec2017/marshal-configs/spec17-intspeed-<input>-lossy.json); the
# runtime config is derived per input from the tracked one (see runtime_config). "-full" is the
# whole-run measurement (spec17-intspeed-train-lossy, without it, is an older windowed one).
INPUT = DEFAULT_INPUT
OUT = paths.OUT_ROOT / "spec_lossy" / INPUT
WORKLOAD = f"spec17-intspeed-{INPUT}-lossy-full"
WORKLOAD_JSON = paths.SOFTWARE / "spec2017" / "marshal-configs" / f"{WORKLOAD}.json"
MANAGER: list[str] = []
JOBS: list[str] = []


RUNTIME_TEMPLATE = paths.FS / "deploy" / "tacit-runtime" / "spec-lossy.yaml"


def runtime_config(inp: str) -> str:
    """The manager config for one input set, derived from the tracked spec-lossy.yaml: the
    workload for that input and a run farm tag suffixed with it, so two inputs can be on
    the farm at the same time (FireSim scopes a farm, and terminates it, by tag). Written
    beside the template as config_spec-lossy-<input>.yaml: FireSim's deploy/.gitignore drops
    config_*.yaml at any depth, so the generated file never shows up in git."""
    name = f"config_spec-lossy-{inp}.yaml"
    text = RUNTIME_TEMPLATE.read_text()
    text, n_tag = re.subn(r"^(\s*run_farm_tag:\s*)\S+", rf"\g<1>tacit-ae-spec-lossy-{inp}", text, flags=re.M)
    text, n_wl = re.subn(r"^(\s*workload_name:\s*)\S+", rf"\g<1>spec17-intspeed-{inp}-lossy-full.json", text, flags=re.M)
    assert n_tag == 1 and n_wl == 1, f"{RUNTIME_TEMPLATE}: expected one run_farm_tag and one workload_name"
    (RUNTIME_TEMPLATE.parent / name).write_text(text)
    return name


def configure(inp: str) -> None:
    global INPUT, OUT, WORKLOAD, WORKLOAD_JSON, MANAGER, JOBS
    INPUT = inp
    OUT = paths.OUT_ROOT / "spec_lossy" / inp
    WORKLOAD = f"spec17-intspeed-{inp}-lossy-full"
    WORKLOAD_JSON = paths.SOFTWARE / "spec2017" / "marshal-configs" / f"{WORKLOAD}.json"
    MANAGER = ["firesim", "-c", f"tacit-runtime/{runtime_config(inp)}",
               "-a", "tacit-runtime/tacit-ae-hwdb.yaml",
               "-r", "tacit-runtime/tacit-ae-build-recipes.yaml"]
    JOBS = [f"{WORKLOAD}-{b.name}" for b in spec.BENCHMARKS]


configure(DEFAULT_INPUT)
STEM = "lossy-coverage"


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
    say("image")
    step(f"firemarshal build (compiles SPEC {INPUT} inputs: slow the first time)")
    log = log_for("image")
    imgs = [paths.FM / "images" / "firechip" / j / f"{j}.img" for j in JOBS]
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
    return r if all((r / j / "uartlog").exists() and list((r / j / "output").glob("*.out")) for j in JOBS) else None


def fpga(force: bool) -> Path:
    say("fpga")
    step(f"run farm: {len(JOBS)} slots")
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
        raise StageFailed("runworkload produced an incomplete results directory (a job has no output/*.out)", 1, log)
    ok(r.name)
    return r


# ----------------------------------------------------------------------- analyse
def analyse(results: Path, force: bool) -> None:
    say("analyse")
    step("coverage table and figure")
    outs = [results / j / "output" for j in JOBS]
    if fresh(OUT / f"{STEM}.csv", *outs) and not force:
        skip(f"{STEM}.csv")
        return
    sh([paths.PY, ANALYSIS / "compare_lossy.py", results, "-o", OUT / STEM], log_for("analyse"), cwd=OUT)
    ok(f"{STEM}.csv, {STEM}.pdf")
    step("figure: lost cycles per million, log scale (the paper's)")
    sh([paths.PY, ANALYSIS / "lollipop.py", OUT / f"{STEM}.csv", OUT / f"{STEM}-lollipop"], log_for("analyse"), cwd=OUT)
    ok(f"{STEM}-lollipop.pdf")


# ------------------------------------------------------------------------ report
def report() -> bool:
    say("results")
    with open(OUT / f"{STEM}.csv") as fh:
        rs = list(csv.DictReader(fh))
    show = ("window_cycles", "gap_cycles", "gap_fraction", "pauses", "stall_fraction", "bits_per_insn")
    print(f"\n  {'benchmark':24}" + "".join(f"{c:>16}" for c in show))
    for r in rs:
        cells = []
        for c in show:
            v = float(r[c])
            cells.append(f"{v:16.4f}" if "fraction" in c or c == "bits_per_insn" else f"{v:16.0f}")
        print(f"  {r['benchmark']:24}" + "".join(cells))
    names = {r["benchmark"] for r in rs}
    log = log_for("analyse").read_text(errors="replace") if log_for("analyse").exists() else ""
    confirmed = "LOSSLESS run" not in log and "cannot confirm lossy" not in log
    passed = all(b.name in names for b in spec.BENCHMARKS) and confirmed
    print(f"\n  all {len(spec.BENCHMARKS)} benchmarks reported, every run confirmed in lossy mode: "
          f"{'YES' if passed else 'NO -- INVESTIGATE'}")
    print(f"\n  everything this run produced is under {OUT} :")
    print(f"    {STEM}.csv               per benchmark: traced cycles, gap cycles and fraction, pauses, stalls, DMA bytes")
    print(f"    {STEM}-lollipop.pdf/.png the figure: lost cycles per million traced cycles, log scale")
    print("    logs/<step>.log             one transcript per step (analyse.log carries the per-run sanity warnings)")
    return passed


# -------------------------------------------------------------------------- main
STEPS = ("image", "driver", "fpga", "analyse")


def preflight(do) -> None:
    say("environment")
    paths.require("CY", "FM", "FS", "PY")
    if do["image"] is not None:
        spec.check_spec_dir()
    step("firesim"); ok() if have("firesim") else die("firesim not on PATH -- source sims/firesim/sourceme-manager.sh")
    step("python dependencies")
    try:
        import matplotlib  # noqa: F401
        ok()
    except ImportError as e:
        die(f"missing python package: {e.name} -- pip install matplotlib")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", choices=INPUTS, default=DEFAULT_INPUT,
                    help=f"SPEC input set (default {DEFAULT_INPUT}; ref is what the paper reports, hours on the farm)")
    steps.add_args(ap, STEPS)
    args = ap.parse_args(argv)
    configure(args.input)
    if args.list:
        print(f"input    : {INPUT}\nfarm tag : tacit-ae-spec-lossy-{INPUT}\nworkload : {WORKLOAD_JSON}\nslots    : {len(JOBS)} (one lossy traced run per benchmark)")
        print(f"steps    : {', '.join(STEPS)}, report\nout      : {OUT}\n")
        for b in spec.BENCHMARKS:
            print(f"  {b.name:24} ./intspeed.sh {b.dir} --threads 1{b.args} --trace --dma --lossy")
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
