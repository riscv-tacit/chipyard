#!/usr/bin/env python3
"""TACIT artifact evaluation: profile-guided dispatch fusion in Lua 5.4.7 on MegaBoom v3.

Three interpreters -- stock Lua and two with a hand-inserted fused-dispatch guard --
run mandelbrot under TACIT, on four FPGA slots in one FireSim launch: three traced
runs plus a chores job that exports the kernel's jump-label patch map. The three
captures are then bundled, decoded in parallel, analysed, and compared.

    ./run.sh lua_fusion              everything, resuming past steps whose outputs exist
    ./run.sh lua_fusion --list       the plan, no work done
    ./run.sh lua_fusion --force      redo every step
    ./run.sh lua_fusion --narrow     decode without bb_pair_stats (faster, less memory)
    ./run.sh lua_fusion --from analyse --force   redo analysis and the report, keep the rest
    ./run.sh lua_fusion --to driver      host-only preparation, stop before the run farm

Steps, in order:  build -> image -> driver -> fpga -> bundle -> decode -> analyse -> report
(the step contract shared by every experiment is in steps.py)
Each step checks for its own outputs first, so a failure late in the pipeline does
not cost an FPGA run.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import csv
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent            # experiments/lua_fusion/
sys.path.insert(0, str(HERE.parents[1]))        # the common layer: paths, shell, uartlog, firesim
import paths                                                          # noqa: E402
import steps                                                          # noqa: E402
from firesim import exclusive, dump_from_rootfs, newest_results_dir, verify_in_rootfs  # noqa: E402
from shell import (StageFailed, die, duration, have, md5, ok, out, say, sh,  # noqa: E402
                   skip, step, warn)
from uartlog import parse as parse_uartlog                            # noqa: E402

# ------------------------------------------------------------------ the experiment
COMMON = HERE.parents[1]              # bundle_run.py and the shared modules
ANALYSIS = HERE / "analysis"          # plotters and the per-arm flow
CONFIGS = HERE / "configs"            # optabs and decoder receiver templates
OUT = paths.OUT_ROOT / "lua_fusion"   # everything this produces, captures included

LUA_DISPATCH = paths.SOFTWARE / "lua-dispatch"
WORKLOAD = "lua-fusion"               # one FireMarshal workload, four jobs
WORKLOAD_JSON = LUA_DISPATCH / "workloads" / f"{WORKLOAD}.json"
OVERLAY = LUA_DISPATCH / "workloads" / WORKLOAD / "overlay" / "root" / "lua-dispatch"
CHORES_JOB = f"{WORKLOAD}-chores"
BENCH = "mandelbrot"

# FireSim's deploy/.gitignore drops every config_*.yaml at any depth, so the AE's
# manager inputs live in tacit-runtime/ under other names.
MANAGER = ["firesim", "-c", "tacit-runtime/lua-fusion.yaml",
           "-a", "tacit-runtime/tacit-ae-hwdb.yaml",
           "-r", "tacit-runtime/tacit-ae-build-recipes.yaml"]

CROSS = "riscv64-unknown-linux-gnu"
CFLAGS = "-O2 -g -static -fno-crossjumping"   # -fno-crossjumping keeps each handler's
                                              # dispatch tail its own jr site
# mandelbrot is the traced workload; the others only guard against a guard that
# corrupts semantics in a way mandelbrot happens not to exercise.
GATE_BENCHMARKS = (("mandelbrot.lua", "900"), ("spectralnorm.lua", "200"),
                   ("fannkuch.lua", "8"), ("sieve.lua", "200000"))
DECODE_GB_EACH = 4                    # measured peak is ~2.5 GB; headroom for the pool


@dataclass(frozen=True)
class Arm:
    name: str          # also the binary suffix (lua-<name>) and the job prefix
    tree: str          # source tree under software/lua-dispatch
    optab: str         # opcode -> handler address, for THIS build's addresses
    label: str         # short, for figures
    table_label: str   # long, for the results table
    edit: str          # the provenance claim: what changed from baseline
    guards: tuple      # opcodes given a guard; () is the baseline
    decode: bool = True  # False: run it for its runtime only (no capture, bundle or decode)

    @property
    def tree_dir(self) -> Path: return LUA_DISPATCH / self.tree
    @property
    def binary(self) -> Path: return self.tree_dir / "src" / "lua"
    @property
    def app(self) -> str: return f"lua-{self.name}"
    @property
    def job(self) -> str: return f"{WORKLOAD}-{self.name}-{BENCH}-traced"
    @property
    def image(self) -> Path: return paths.FM / "images" / "firechip" / self.job / f"{self.job}.img"
    @property
    def out(self) -> Path: return OUT / self.name
    @property
    def bundle(self) -> Path: return self.out / "bundle"
    @property
    def optab_path(self) -> Path: return CONFIGS / self.optab


# The baseline is stock Lua 5.4.7. Every other arm is that source with one `vmbreak;`
# replaced by a guard, by hand, and nothing else: the interpreter tests the next
# opcode against a constant and jumps straight to its handler when it matches. The
# first arm is the baseline the report measures the others against.
ARMS = (
    Arm("base", "lua-5.4.7", "optab_base.json", "baseline", "unfused baseline",
        "none -- stock lua 5.4.7, byte-identical in every loaded section", ()),
    Arm("mulmul", "lua-fuse-mulmul", "optab_mulmul.json", "+MUL→MUL", "1 guard  MUL->MUL",
        "lvm.c:1478  vmbreak -> vmbreak_fused(OP_MUL)", ("OP_MUL",)),
    Arm("mmadd", "lua-fuse-mulmul-muladd", "optab_mmadd.json", "+MUL→ADD",
        "2 guards MUL->MUL,MUL->ADD",
        "lvm.c:1478  vmbreak -> vmbreak_fused2(OP_MUL, OP_ADD)", ("OP_MUL", "OP_ADD")),
    # The control for the profile itself. Ranking edges by the whole handler span puts
    # LEI->MUL among the top few; ranking by the entry block says its arrival is already
    # predicted. This arm guards LEI->MUL and is run for its runtime alone: if the entry
    # block is right, the guard buys nothing (fig.span_vs_entry.pdf shows the two rankings).
    Arm("leimul", "lua-fuse-leimul", "optab_leimul.json", "+LEI→MUL (span-ranked)",
        "1 guard  LEI->MUL, runtime only",
        "lvm.c  OP_LEI vmbreak -> vmbreak_fused(OP_MUL)", ("OP_MUL",), decode=False),
)
DECODED = tuple(a for a in ARMS if a.decode)   # the arms whose captures are bundled and decoded
BASE = ARMS[0]


def outdir(*parts) -> Path:
    d = OUT.joinpath(*parts)
    d.mkdir(parents=True, exist_ok=True)
    return d


def log_for(name: str) -> Path:
    return outdir("logs") / f"{name}.log"


def fresh(output: Path, *inputs: Path) -> bool:
    """Does `output` exist and post-date every input it was derived from?"""
    if not output.exists() or not output.stat().st_size:
        return False
    t = output.stat().st_mtime
    return all(not i.exists() or i.stat().st_mtime <= t for i in inputs)


def dirsize(p: Path) -> str:
    return out(["du", "-sh", p]).split()[0] if p.exists() else "?"


# ------------------------------------------------------------------------- build
def build(force: bool) -> None:
    """Cross-compile each interpreter, then prove the guards did not change semantics."""
    say("build")
    for a in ARMS:
        step(f"riscv interpreter: {a.name}")
        log = log_for(f"build.{a.name}")
        if a.binary.exists() and not force:
            skip(f"md5 {md5(a.binary, 12)}")
            continue
        make = ["make", "-C", a.tree_dir / "src"]
        sh(make + ["clean"], log)
        sh(make + ["posix", f"-j{os.cpu_count()}", f"CC={CROSS}-gcc -std=gnu99",
                   f"AR={CROSS}-ar rcu", f"RANLIB={CROSS}-ranlib",
                   f"MYCFLAGS={CFLAGS}", "MYLDFLAGS=-static"], log)
        ok(f"md5 {md5(a.binary, 12)}")

    # A host build of the UNMODIFIED interpreter is the oracle the gate compares to.
    step("host reference interpreter")
    host = LUA_DISPATCH / "lua-host" / "src"
    if (host / "lua").exists() and not force:
        skip("already built")
    else:
        log = log_for("build.host")
        sh(["make", "-C", host, "clean"], log)
        sh(["make", "-C", host, "posix", f"-j{os.cpu_count()}", "CC=gcc -std=gnu99",
            "MYCFLAGS=-O2"], log)
        ok()

    # A guard that jumps into the wrong handler is silent on small inputs -- the
    # interpreter keeps running and produces plausible output. Run the real workloads
    # on a host build of each arm and diff against stock before spending FPGA time.
    for a in ARMS:
        step(f"host correctness gate: {a.name}")
        log = log_for(f"gate.{a.name}")
        work = outdir(".scratch", "hostchk") / a.name
        if work.exists():
            shutil.rmtree(work)
        shutil.copytree(a.tree_dir, work)
        sh(["make", "-C", work / "src", "clean"], log)
        sh(["make", "-C", work / "src", "posix", f"-j{os.cpu_count()}",
            "CC=gcc -std=gnu99", "MYCFLAGS=-O2 -fno-crossjumping"], log)
        for bench, arg in GATE_BENCHMARKS:
            got = out([work / "src" / "lua", f"bench/{bench}", arg], cwd=LUA_DISPATCH)
            want = out([host / "lua", f"bench/{bench}", arg], cwd=LUA_DISPATCH)
            if got != want:
                log.write_text(f"MISMATCH on {bench} {arg}:\n  got  {got!r}\n  want {want!r}\n")
                raise StageFailed(f"gate:{a.name}:{bench}", 1, log)
        shutil.rmtree(work, ignore_errors=True)   # kept only when the gate fails
        ok(f"{len(GATE_BENCHMARKS)} benchmarks match")


# ------------------------------------------------------------------------- image
def image(force: bool) -> None:
    """Install every interpreter into the one overlay and build the guest images."""
    say("image")
    step("firemarshal build")
    log = log_for("image")
    if all(a.image.exists() for a in ARMS) and not force:
        skip(f"{len(ARMS)} job images present")
    else:
        for a in ARMS:
            shutil.copy(a.binary, OVERLAY / a.app)
            (OVERLAY / a.app).chmod(0o755)
        # rebuilt in-guest by host-init, so a stale cross-built copy must not linger
        (OVERLAY / "trace-run").unlink(missing_ok=True)
        # marshal's dependency tracking does not cover the drivers baked into each
        # job's initramfs (or a changed kernel), so a forced rebuild must start clean:
        # a plain `marshal build` after a driver edit recompiles the module but keeps
        # the stale kernel binaries.
        if force:
            sh(["./marshal", "clean", WORKLOAD_JSON], log, cwd=paths.FM)
        sh(["./marshal", "-v", "build", WORKLOAD_JSON], log, cwd=paths.FM)
        ok()
    # The guest must run the interpreters we just built; see verify_in_rootfs.
    for a in ARMS:
        step(f"verify rootfs interpreter: {a.name}")
        ok(verify_in_rootfs(a.image, f"/root/lua-dispatch/{a.app}", a.binary))
    step("firemarshal install")
    sh(["./marshal", "install", WORKLOAD_JSON], log, cwd=paths.FM)
    ok()


# ------------------------------------------------------------------------ driver
def driver(force: bool) -> None:
    """Build the FireSim host driver for the bitstream, without a run farm. infrasetup
    would do it, but building it here keeps the run phase free of host-side builds --
    two experiments' infrasetups would otherwise race on the same make tree."""
    say("driver")
    step("firesim builddriver")
    sh(MANAGER + ["builddriver"], log_for("driver"), cwd=paths.FS / "deploy")
    ok()


# -------------------------------------------------------------------------- fpga
def results_dir() -> Path | None:
    """The newest complete run of the workload: every traced job and the chores job."""
    try:
        r = newest_results_dir(WORKLOAD)
    except FileNotFoundError:
        return None
    need = [r / a.job / "uartlog" for a in ARMS] + [r / CHORES_JOB / "jump_label_patch_map.txt"]
    return r if all(p.exists() for p in need) else None


def fpga(force: bool) -> Path:
    """One FireSim launch: four slots, one per job. The config's terminate_on_completion
    releases the run farm when runworkload finishes."""
    say("fpga")
    step(f"run farm: {len(ARMS) + 1} slots")
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
def bundle(results: Path, force: bool, narrow: bool) -> None:
    """Package each capture so it decodes identically later, on any machine."""
    say("bundle")
    template = CONFIGS / ("decode_narrow.json" if narrow else "decode_full.json")
    for a in DECODED:
        step(f"bundle capture: {a.name}")
        if (a.bundle / "config.json").exists() and not force:
            skip(dirsize(a.bundle))
            continue
        # A bundle dir without config.json is debris from an interrupted attempt;
        # bundle_run refuses a non-empty destination, correctly.
        if a.bundle.exists():
            shutil.rmtree(a.bundle)
        # Take the binaries from the guest image, not the overlay: the image is what
        # the guest actually executed, the overlay drifts after the build.
        staged = outdir(a.name, ".binaries")
        apps = []
        for name in (a.app, "trace-run"):
            apps += ["--app", dump_from_rootfs(a.image, f"/root/lua-dispatch/{name}", staged / name)]
        sh([paths.PY, COMMON / "bundle_run.py",
            "--results", results / a.job,
            "--template", template,
            "--out", a.bundle,
            "--image", paths.FM / "images" / "firechip" / a.job,
            "--jlmap", results / CHORES_JOB / "jump_label_patch_map.txt",
            *apps], log_for(f"bundle.{a.name}"))
        ok(dirsize(a.bundle))


# ------------------------------------------------------------------------ decode
def decode(force: bool) -> None:
    """Turn each packet stream into per-block statistics, as many at once as memory allows."""
    say("decode")
    todo = [a for a in DECODED
            if force or not fresh(a.bundle / "out" / "lua.bb_stats.csv", a.bundle / "config.json")]
    done = [a for a in DECODED if a not in todo]
    for a in done:
        step(f"decode: {a.name}")
        skip("already decoded")
    if not todo:
        return
    workers = max(1, min(len(todo), _available_gb() // DECODE_GB_EACH))
    step(f"decode: {', '.join(a.name for a in todo)}  ({workers} in parallel)")
    for a in todo:
        (a.bundle / "out").mkdir(parents=True, exist_ok=True)

    def one(a: Arm) -> tuple[Arm, int]:
        log = log_for(f"decode.{a.name}")
        with open(log, "ab") as f:
            f.write(f"\n$ {paths.DECODER} --config config.json   (cwd {a.bundle})\n".encode())
            f.flush()
            rc = subprocess.run([str(paths.DECODER), "--config", "config.json"],
                                cwd=a.bundle, stdout=f, stderr=subprocess.STDOUT).returncode
        return a, rc

    t0 = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(one, todo))
    failed = [(a, rc) for a, rc in results if rc != 0]
    if failed:
        a, rc = failed[0]
        raise StageFailed(f"decode:{a.name}", rc, log_for(f"decode.{a.name}"))
    blocks = ", ".join(
        f"{a.name} {sum(1 for _ in open(a.bundle / 'out' / 'lua.bb_stats.csv')) - 1:,}"
        for a in todo)
    ok(f"blocks: {blocks}  {duration(time.monotonic() - t0)}")
    decode_span(force)


def decode_span(force: bool) -> None:
    """A second, narrow decode of the baseline capture with the dispatch_stats receiver
    timing the WHOLE handler (entry to the next handler's entry) instead of the entry
    block: the other half of the span-vs-entry comparison. Same capture, same handler
    table, one receiver option changed; the decoder allows one dispatch_stats receiver
    per run, hence the second pass."""
    step("decode: base, handler-span unit")
    cfg_in, cfg_out = BASE.bundle / "config.json", BASE.bundle / "config_span.json"
    out_csv = BASE.bundle / "out-span" / "lua.dispatch_stats.csv"
    if fresh(out_csv, cfg_in) and not force:
        return skip("already decoded")
    c = json.loads(cfg_in.read_text())
    ds = dict(c["receivers"]["dispatch_stats"])
    ds.update({"span": "handler", "path": "out-span/lua.dispatch_stats.csv",
               "hist_path": "out-span/lua.dispatch_hist.csv"})
    c["receivers"] = {"prv_breakdown": {"enabled": True}, "dispatch_stats": ds}
    c["emulations"] = []
    cfg_out.write_text(json.dumps(c, indent=2) + "\n")
    (BASE.bundle / "out-span").mkdir(exist_ok=True)
    t0 = time.monotonic()
    sh([paths.DECODER, "--config", "config_span.json"], log_for("decode.base.span"), cwd=BASE.bundle)
    ok(duration(time.monotonic() - t0))


def _available_gb() -> int:
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // (1024 * 1024)
    return DECODE_GB_EACH


# ----------------------------------------------------------------------- analyse
def analyse(force: bool) -> None:
    """Rank blocks by variance-weighted cost and draw each arm's own figures."""
    say("analyse")
    for a in DECODED:
        step(f"analysis flow: {a.name}")
        tables = a.bundle / "out" / "lua"          # the decoder's lua.<report>.csv, as a prefix
        if fresh(a.out / f"vbb.{BENCH}.txt", a.bundle / "out" / "lua.bb_stats.csv") and not force:
            skip(str(a.out))
            continue
        window = parse_uartlog(a.bundle / "uartlog").window_cycles
        sh([ANALYSIS / "flow.sh", a.optab_path, tables, f"{window / 1e9:.6f}", a.out],
           log_for(f"analyse.{a.name}"))          # window in Gcycles
        ok(str(a.out))
    step("entry block vs handler span (baseline, MUL)")
    entry_h, span_h = BASE.bundle / "out" / "lua.dispatch_hist.csv", BASE.bundle / "out-span" / "lua.dispatch_hist.csv"
    if fresh(OUT / "fig.unit_mul.pdf", entry_h, span_h) and not force:
        return skip("fig.unit_mul.pdf")
    sh([paths.PY, ANALYSIS / "plot_smear_effects.py", "unit", "--optab", BASE.optab_path, "--target", "MUL",
        "--entry-hist", entry_h, "--span-hist", span_h, "--xmax-entry", "22", "--xmax-span", "36",
        "--out", OUT / "fig.unit_mul"], log_for("analyse.unit_mul"))
    # the same comparison as a ranking of every hot edge, for the log
    window = parse_uartlog(BASE.bundle / "uartlog").window_cycles
    sh([paths.PY, ANALYSIS / "plot_span_vs_entry.py", "--optab", BASE.optab_path,
        "--entry", BASE.bundle / "out" / "lua.dispatch_stats.csv",
        "--span", BASE.bundle / "out-span" / "lua.dispatch_stats.csv",
        "--window", f"{window / 1e9:.6f}", "--out", OUT / "logs" / "span_vs_entry"],
       log_for("analyse.unit_mul"))
    ok("fig.unit_mul.pdf  (edge ranking table in logs/analyse.unit_mul.log)")


# ------------------------------------------------------------------------ report
CHECKSUM = r"(inset \d+ checksum \d+)"   # what mandelbrot prints; not a platform fact
MIN_GUARD_TRAFFIC = 10_000               # below this a non-jr site is noise, not a guard
ENTRY_TOLERANCE = 1e-4                   # handler-entry counts must agree this closely


@dataclass
class Row:
    arm: Arm
    src: Path          # where its uartlog lives: the bundle, or the job's results dir
    window: int
    total: int
    checksum: str
    stall: int
    entries: int | None
    fallthrough: list
    guard_sites: list


def collect(results: Path) -> list[Row]:
    rows = []
    for a in ARMS:
        src = a.bundle if a.decode else results / a.job
        uartlog = src / "uartlog"
        if not uartlog.exists():
            continue
        log = parse_uartlog(uartlog)
        entries, fallthrough, guard_sites = None, [], []
        summary = a.bundle / "out" / "lua.dispatch_stats.summary.json"
        if summary.exists():
            j = json.loads(summary.read_text())
            entries, fallthrough = j["entries_total"], j["fallthrough_entries"]
        # A guard is any handler entry NOT made by the indirect jr: a branch onto the
        # stub in front of the entry, or a direct jump landing on it.
        sites = a.bundle / "out" / "lua.dispatch_stats.sites.csv"
        if sites.exists():
            with open(sites) as f:
                guard_sites = sorted({
                    (r["site_pc"].strip(), r["site_kind"].strip(), r["to_handler"].strip())
                    for r in csv.DictReader(f, skipinitialspace=True)
                    if r["site_kind"].strip() != "jr" and int(r["count"]) >= MIN_GUARD_TRAFFIC})
        rows.append(Row(a, src, log.window_cycles or 0, log.total_cycles or 0,
                        log.search(CHECKSUM) or "?", log.stall_cycles,
                        entries, fallthrough, guard_sites))
    return rows


def figures(rows: list[Row]) -> None:
    log = log_for("report")
    step("runtime figure")
    try:
        sh([paths.PY, ANALYSIS / "plot_runtime_bars.py", "--out", OUT / "fig.runtime",
            *[f"{r.arm.label}={r.src}" for r in rows]], log)
        ok(str(OUT / "fig.runtime.pdf"))
    except Exception:
        warn(f"see {log}")
    # The grid needs each arm's own optab: handlers move between builds, so an
    # address means nothing without the build that produced it.
    step("per-predecessor grid")
    hists = [(r, r.arm.bundle / "out" / "lua.dispatch_hist.csv") for r in rows if r.arm.decode]
    if not all(h.exists() for _, h in hists):
        return warn("needs the decode step for every arm")
    try:
        sh([paths.PY, ANALYSIS / "plot_pred_grid.py", "--targets", "MUL,ADD",
            "--out", OUT / "fig.pred_grid",
            *[f"{r.arm.label}={h}:{r.arm.optab_path}" for r, h in hists]], log)
        ok(str(OUT / "fig.pred_grid.pdf"))
    except Exception:
        warn(f"see {log}")


def results_table(rows: list[Row]) -> None:
    base = rows[0].window
    print(f"  {'arm':28} {'window cycles':>15} {'vs baseline':>12} {'total cycles':>15}  correctness")
    for r in rows:
        delta = f"{100 * (r.window - base) / base:+.2f}%" if base and r.window else "—"
        print(f"  {r.arm.table_label:28} {r.window:15,} {delta:>12} {r.total:15,}  {r.checksum}")
    same = len({r.checksum for r in rows}) == 1
    print(f"\n  program output identical across arms: {'YES' if same else 'NO -- INVESTIGATE'}")
    print(f"  max trace-unit stall: {max(r.stall for r in rows)} cycles "
          f"(perturbation is nil if this is small vs the window)")


def canaries(rows: list[Row]) -> bool:
    """(1) Every arm executes the same bytecode, so the decoder must see the same
    number of handler entries in each. (2) A guard is the only reason a handler is
    ever entered without going through the indirect jump, so the number of such
    entry sites must equal the number of guards the arm declares."""
    have_ = [r for r in rows if r.entries is not None]
    if not have_:
        return True
    print(f"\n  {'arm':28} {'handler entries':>16} {'vs baseline':>12} "
          f"{'non-jr entry sites':>19} {'declared guards':>16}  check")
    ref, all_ok = have_[0].entries, True
    for r in have_:
        good = abs(r.entries - ref) / ref < ENTRY_TOLERANCE and len(r.guard_sites) == len(r.arm.guards)
        all_ok &= good
        print(f"  {r.arm.table_label:28} {r.entries:16,} {100 * (r.entries - ref) / ref:+11.4f}% "
              f"{len(r.guard_sites):19d} {len(r.arm.guards):>16}  {'ok' if good else 'MISMATCH'}")
        for pc, kind, to in r.guard_sites:
            print(f"  {'':28} guard site {pc} ({kind}) -> handler {to}")
        for e in r.fallthrough:
            print(f"  {'':28} fall-through {e['block_start']} -> handler {e['handler']}  x{e['count']:,}")
    print(f"  instrument canaries: {'PASS' if all_ok else 'FAIL -- a MISMATCH arm is not trustworthy'}")
    return all_ok


def report(results: Path) -> bool:
    say("results")
    rows = collect(results)
    if len(rows) < len(ARMS):
        print(f"  only {len(rows)} of {len(ARMS)} arms have a uartlog -- run the fpga and bundle steps first")
        return False
    figures(rows)
    results_table(rows)
    passed = canaries(rows)
    print(f"\n  everything this run produced is under {OUT} :")
    print("    fig.runtime.pdf, fig.pred_grid.pdf     across-arm figures")
    print("    fig.unit_mul.pdf                       MUL's arrivals by predecessor: entry block vs whole handler span")
    print("                                           (after-LEI sits on the floor in (a), off it in (b); the leimul arm tests it)")
    print("    <arm>/vbb.mandelbrot.txt               blocks ranked by variance-weighted cost")
    print("    <arm>/fig.bb_distributions.pdf         per-block latency distributions")
    print("    <arm>/bundle/                          the capture: trace, binaries, dwarf,")
    print("    <arm>/bundle/out/                      patch map, and the decoder's tables")
    print("    logs/<step>.<arm>.log                  one transcript per step")
    return passed


# -------------------------------------------------------------------------- main
STEPS = ('build', 'image', 'driver', 'fpga', 'bundle', 'decode', 'analyse')


def preflight() -> None:
    say("environment")
    paths.require("CY", "TD", "FM", "FS", "PY", "DECODER")
    for tool, fix in (("riscv64-unknown-linux-gnu-gcc", "source env.sh"),
                      ("debugfs", "install e2fsprogs"),
                      ("firesim", "source sims/firesim/sourceme-manager.sh")):
        step(tool)
        ok() if have(tool) else die(f"{tool} not on PATH -- {fix}")
    step("python dependencies")
    try:
        import elftools, matplotlib, pandas  # noqa: F401
        ok()
    except ImportError as e:
        die(f"missing python package: {e.name} -- pip install pandas matplotlib pyelftools")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    steps.add_args(ap, STEPS)
    ap.add_argument("--narrow", action="store_true", help="skip bb_pair_stats: less time and memory")
    args = ap.parse_args(argv)
    if args.list:
        print(f"workload : {WORKLOAD_JSON}\nslots    : {len(ARMS) + 1} (one per arm + chores; {len(DECODED)} arms decoded)")
        print(f"steps    : {', '.join(STEPS)}, report\nout      : {OUT}\n")
        for a in ARMS:
            print(f"  {a.name:8} {a.tree:24} {a.edit}")
        return 0
    do = steps.plan(STEPS, args)          # step -> None (do not run), False (if needed), True (redo)
    started = time.monotonic()
    stopped = f"\n  stopped after {args.to_step}; total elapsed: "
    try:
        preflight()
        if do["build"] is not None: build(do["build"])
        if do["image"] is not None: image(do["image"])
        if do["driver"] is not None: driver(do["driver"])
        if do["fpga"] is None:
            print(stopped + duration(time.monotonic() - started))
            return 0
        results = fpga(do["fpga"])
        if do["bundle"] is not None: bundle(results, do["bundle"], args.narrow)
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
