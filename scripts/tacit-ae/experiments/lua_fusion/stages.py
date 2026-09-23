"""The six per-arm stages, in the order they run.

Each stage is a plain function taking (variant, log, force). It decides for itself
whether its work is already done and says so; the driver only prints the heading
and handles failure. Keeping the skip test inside the stage means the condition
sits next to the work it guards, rather than in a table somewhere else.

Commands are carried over from the shell driver unchanged -- what moved is the
arrangement, not the recipe.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import paths
from firesim import dump_from_rootfs, newest_results_dir, verify_in_rootfs
from shell import ok, out, sh, skip
from uartlog import parse as parse_uartlog
from variants import ANALYSIS, CFLAGS, CONFIGS, LUA_DISPATCH, Variant, outdir

CROSS = "riscv64-unknown-linux-gnu"
# mandelbrot is the traced workload; the others only guard against a guard that
# corrupts semantics in a way mandelbrot happens not to exercise.
GATE_BENCHMARKS = (("mandelbrot.lua", "900"), ("spectralnorm.lua", "200"),
                   ("fannkuch.lua", "8"), ("sieve.lua", "200000"))


def logfile(v: Variant, stage: str) -> Path:
    return outdir("logs") / f"{v.key}.{stage}.log"


# Stages run in separate processes when a reviewer resumes, so anything one stage
# learns and a later one needs has to survive on disk.
def _state(v: Variant) -> Path:
    return outdir(".state") / f"{v.key}.json"


def remember(v: Variant, **kw) -> None:
    s = recall(v)
    s.update({k: str(x) for k, x in kw.items()})
    _state(v).write_text(json.dumps(s, indent=1))


def recall(v: Variant) -> dict:
    p = _state(v)
    return json.loads(p.read_text()) if p.exists() else {}


# ------------------------------------------------------------------------ build
def build(v: Variant, log: Path, force: bool) -> None:
    """Cross-compile the interpreter, then prove the guard did not change semantics."""
    if v.binary.exists() and not force:
        skip(f"md5 {_md5(v.binary)}")
    else:
        make = ["make", "-C", v.tree_dir / "src"]
        sh(make + ["clean"], log)
        sh(make + ["posix", f"-j{os.cpu_count()}",
                   f"CC={CROSS}-gcc -std=gnu99", f"AR={CROSS}-ar rcu",
                   f"RANLIB={CROSS}-ranlib", f"MYCFLAGS={CFLAGS}",
                   "MYLDFLAGS=-static"], log)
        ok(f"md5 {_md5(v.binary)}")
    _host_reference(log, force)
    _gate(v, log)


def _host_reference(log: Path, force: bool) -> None:
    """A host build of the UNMODIFIED interpreter -- the oracle the gate compares to.
    A clean clone has its source but not the binary, so build it once."""
    from shell import step
    host = LUA_DISPATCH / "lua-host" / "src"
    step("host reference interpreter")
    if (host / "lua").exists() and not force:
        return skip("already built")
    sh(["make", "-C", host, "clean"], log)
    sh(["make", "-C", host, "posix", f"-j{os.cpu_count()}",
        "CC=gcc -std=gnu99", "MYCFLAGS=-O2"], log)
    ok()


def _gate(v: Variant, log: Path) -> None:
    """Run the real workloads on a host build of THIS arm and diff against stock.

    A guard that jumps into the wrong handler is silent on small inputs -- the
    interpreter keeps running and produces plausible output. Catching that before
    an FPGA run is worth the two minutes it costs.
    """
    from shell import StageFailed, step
    step("host correctness gate")
    work = outdir(".scratch") / "hostchk" / v.key
    if work.exists():
        shutil.rmtree(work)
    shutil.copytree(v.tree_dir, work)
    sh(["make", "-C", work / "src", "clean"], log)
    sh(["make", "-C", work / "src", "posix", f"-j{os.cpu_count()}",
        "CC=gcc -std=gnu99", "MYCFLAGS=-O2 -fno-crossjumping"], log)

    oracle = LUA_DISPATCH / "lua-host" / "src" / "lua"
    for bench, arg in GATE_BENCHMARKS:
        got = out([work / "src" / "lua", f"bench/{bench}", arg], cwd=LUA_DISPATCH)
        want = out([oracle, f"bench/{bench}", arg], cwd=LUA_DISPATCH)
        if got != want:
            log.write_text(f"MISMATCH on {bench} {arg}:\n  got  {got!r}\n  want {want!r}\n")
            raise StageFailed(f"gate:{bench}", 1, log)
    shutil.rmtree(work, ignore_errors=True)   # kept only when the gate fails
    ok(f"{len(GATE_BENCHMARKS)} benchmarks match")


# ------------------------------------------------------------------------ image
def image(v: Variant, log: Path, force: bool) -> None:
    """Install the binary into the workload overlay and build the guest image."""
    from shell import step
    if v.image.exists() and not force:
        skip(v.image.name)
    else:
        shutil.copy(v.binary, v.overlay / v.app)
        (v.overlay / v.app).chmod(0o755)
        # rebuilt in-guest by host-init, so a stale cross-built copy must not linger
        (v.overlay / "trace-run").unlink(missing_ok=True)
        sh(["./marshal", "-v", "build", v.workload_json], log, cwd=paths.FM)
        ok()
    # The guest must run the interpreter we just built; see verify_in_rootfs.
    step("verify rootfs interpreter")
    ok(verify_in_rootfs(v.image, v.guest_path, v.binary))
    sh(["./marshal", "install", v.workload_json], log, cwd=paths.FM)


# -------------------------------------------------------------------------- run
def run(v: Variant, log: Path, force: bool) -> None:
    """One FPGA simulation. Serialised: the run farm is used exclusively."""
    try:
        r = newest_results_dir(v.workload)
        if (r / v.job / "uartlog").exists() and not force:
            remember(v, results=r)
            return skip(r.name)
    except FileNotFoundError:
        pass
    deploy = paths.FS / "deploy"
    sh(["firesim", "-c", v.config, "infrasetup"], log, cwd=deploy)
    sh(["firesim", "-c", v.config, "runworkload"], log, cwd=deploy)
    r = newest_results_dir(v.workload)
    remember(v, results=r)
    ok(r.name)


# ----------------------------------------------------------------------- bundle
def bundle(v: Variant, log: Path, force: bool, narrow: bool = False) -> None:
    """Package the capture so it decodes identically later, on any machine."""
    if (v.bundle_dir / "config.json").exists() and not force:
        return skip(_dirsize(v.bundle_dir))
    # A bundle dir without config.json is debris from an interrupted attempt.
    # bundle_run refuses a non-empty destination -- correctly, since silently
    # writing into one would mix two captures -- so clear it here instead of
    # leaving a reviewer to work out what to delete.
    if v.bundle_dir.exists():
        shutil.rmtree(v.bundle_dir)
    r = Path(recall(v).get("results") or newest_results_dir(v.workload))
    template = "decode_narrow.json" if narrow else "decode_full.json"
    # Take the app binaries from the guest image, not from the overlay. The overlay
    # is an input to image construction and drifts afterwards -- host-init rebuilds
    # trace-run into it, and an earlier run may have deleted it -- so bundling from
    # there can embed a binary that is not the one the trace was produced by. The
    # image is what the guest actually executed.
    staged = outdir(v.key, ".binaries")
    apps = []
    for name in (v.app, "trace-run"):
        apps += ["--app", dump_from_rootfs(v.image, f"/root/lua-dispatch/{name}",
                                           staged / name)]
    sh([paths.PY, ANALYSIS / "bundle_run.py",
        "--results", r / v.job,
        "--template", CONFIGS / template,
        "--out", v.bundle_dir,
        "--image", paths.FM / "images" / "firechip" / v.job,
        "--jlmap", r / v.chores / "jump_label_patch_map.txt",
        *apps], log)
    ok(_dirsize(v.bundle_dir))


# ----------------------------------------------------------------------- decode
def decode(v: Variant, log: Path, force: bool) -> None:
    """Turn the packet stream into per-block statistics."""
    stats = v.bundle_dir / "out" / "lua.bb_stats.csv"
    if fresh(stats, v.bundle_dir / "config.json") and not force:
        return skip("already decoded")
    (v.bundle_dir / "out").mkdir(parents=True, exist_ok=True)
    sh([paths.DECODER, "--config", "config.json"], log, cwd=v.bundle_dir)
    ok(f"{sum(1 for _ in open(stats)):,} blocks")


# ---------------------------------------------------------------------- analyse
def analyse(v: Variant, log: Path, force: bool) -> None:
    """Rank blocks by variance-weighted cost and draw this arm's own figures."""
    a = outdir(v.key)   # bundle/ is a sibling; both live under out/
    if fresh(a / "vbb.mandelbrot.txt", v.bundle_dir / "out" / "lua.bb_stats.csv") and not force:
        return skip(str(a))
    for report in ("bb_stats", "bb_hist", "bb_pair_stats", "dispatch_stats", "dispatch_hist"):
        src = v.bundle_dir / "out" / f"lua.{report}.csv"
        if src.exists():
            shutil.copy(src, a / f"trace.{v.key}.{report}.csv")
    window = parse_uartlog(v.uartlog).window_cycles
    # run_mandelbrot_flow.sh wants the window in billions of cycles
    sh([ANALYSIS / "flow.sh", v.optab_path,
        a / f"trace.{v.key}", f"{window / 1e9:.6f}", a], log)
    ok(str(a))


# ---------------------------------------------------------------------- helpers
def fresh(output: Path, *inputs: Path) -> bool:
    """Does `output` exist and post-date every input it was derived from?

    Existence alone is not enough. Re-decoding a capture leaves the previous
    stage's output in place, so a stage that only checks for a file happily
    reuses results computed from data that has since been replaced -- and says
    "skip", which reads like everything is fine.
    """
    if not output.exists() or not output.stat().st_size:
        return False
    t = output.stat().st_mtime
    return all(not i.exists() or i.stat().st_mtime <= t for i in inputs)


def _md5(p: Path, n: int = 12) -> str:
    from shell import md5
    return md5(p, n)


def _dirsize(p: Path) -> str:
    return out(["du", "-sh", p]).split()[0] if p.exists() else "?"


STAGES = [build, image, run, bundle, decode, analyse]

# Short column headings. The docstrings explain *why* a stage exists and are too
# long to print; these say what is happening right now.
TITLE = {"build": "build riscv interpreter", "image": "firemarshal image",
         "run": "FPGA run (serialised)", "bundle": "bundle capture",
         "decode": "decode from bundle", "analyse": "analysis flow"}
