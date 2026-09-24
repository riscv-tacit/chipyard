#!/usr/bin/env python3
"""Run every experiment: shared preparation first, then the experiments in parallel.

    ./run.sh all [--force] [--experiments lua_fusion,process_launch] [--list]

Preparation is serial because the experiments share host-side state that FireMarshal
does not protect: the kernel is built in one Linux tree and images are modified
through one mount point. Nothing in it needs the run farm.

    1. firemarshal base    br-base rootfs, kernel and drivers, built once
    2. <experiment> --to driver, one after the other: guest binaries, job images, the
                           FireSim host driver (a no-op after the first experiment)

Then every experiment runs `--from fpga` at the same time, resuming past anything
already done. From that step on they touch only their own run farm (distinct
run_farm_tag), results directory and out/ tree, so they cannot interfere. Each one runs
in its own window of a tmux session (`tmux attach -t tacit-ae`; Ctrl-b n / p to move
between windows, Ctrl-b d to detach) so the runs can be watched live; without tmux, or
with --no-tmux, they run here with the experiment's name in front of every line. Either
way each console is kept in out/<experiment>/logs/run.log. A failure in one does not
stop the others; the exit status is non-zero if any failed.

Re-running resumes: every step skips work whose outputs exist.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import paths                                          # noqa: E402
import steps                                          # noqa: E402
from shell import StageFailed, die, duration, ok, say, sh, step  # noqa: E402

EXPERIMENTS_DIR = HERE / "experiments"
BR_BASE = paths.FM / "boards" / "firechip" / "base-workloads" / "br-base.json"


def discover() -> list[str]:
    return sorted(d.name for d in EXPERIMENTS_DIR.iterdir()
                  if d.is_dir() and (d / f"{d.name}.py").exists())


def script(name: str) -> Path:
    p = EXPERIMENTS_DIR / name / f"{name}.py"
    if not p.exists():
        die(f"no experiment '{name}'. Available: {', '.join(discover())}")
    return p


def run_experiment(name: str, flags: list[str], prefix: bool) -> int:
    """Run one experiment's script, streaming its console with the name in front and
    a copy into out/<name>/logs/run.log."""
    logdir = paths.OUT_ROOT / name / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    log = logdir / "run.log"
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    with open(log, "a") as lf:
        lf.write(f"\n$ {name} {' '.join(flags)}\n")
        proc = subprocess.Popen([str(paths.PY), str(script(name)), *flags],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, env=env, cwd=HERE)
        assert proc.stdout is not None
        tag = f"[{name}] " if prefix else ""
        for line in proc.stdout:
            lf.write(line)
            lf.flush()                       # run.log must be readable while the run is live
            sys.stdout.write(tag + line if line.strip() else line)
            sys.stdout.flush()
        return proc.wait()


def prepare(names: list[str], force: bool) -> None:
    say("prepare (serial: shared FireMarshal state)")
    step("firemarshal base: br-base")
    sh(["./marshal", "-v", "build", BR_BASE], paths.OUT_ROOT / "prepare.br-base.log", cwd=paths.FM)
    ok()
    for name in names:
        print(f"\n== prepare: {name}")
        rc = run_experiment(name, ["--to", steps.PREPARE_END] + (["--force"] if force else []), prefix=True)
        if rc != 0:
            raise StageFailed(f"{name} --to {steps.PREPARE_END}", rc, paths.OUT_ROOT / name / "logs" / "run.log")


SESSION = "tacit-ae"


def run_all_tmux(names: list[str], force: bool) -> dict[str, int]:
    """One tmux window per experiment. Each window runs `./run.sh <experiment> --from fpga`
    itself (run.sh sets up the environment, so it does not matter what the tmux server
    inherited), tees the console into out/<experiment>/logs/run.log, writes its exit
    status to run.rc, and then stays open for reading until Enter is pressed."""
    session = SESSION
    if subprocess.run(["tmux", "has-session", "-t", session], capture_output=True).returncode == 0:
        session = f"{SESSION}-{time.strftime('%H%M%S')}"     # an older session is still around
    say(f"run (parallel, in tmux session '{session}': {', '.join(names)})")
    rcs: dict[str, Path] = {}
    for i, name in enumerate(names):
        logdir = paths.OUT_ROOT / name / "logs"
        logdir.mkdir(parents=True, exist_ok=True)
        rc = logdir / "run.rc"
        rc.unlink(missing_ok=True)
        rcs[name] = rc
        flags = f"--from {steps.RUN_START}" + (" --force" if force else "")
        cmd = (f"cd {HERE} && echo '$ {name} {flags}' >> {logdir / 'run.log'} && "
               f"./run.sh {name} {flags} 2>&1 | tee -a {logdir / 'run.log'}; "
               f"echo ${{PIPESTATUS[0]}} > {rc}; "
               f"echo; echo '[{name}] finished (exit '$(cat {rc})'). Log: {logdir / 'run.log'}. Press Enter to close this window.'; read")
        if i == 0:
            sh(["tmux", "new-session", "-d", "-s", session, "-n", name, "-x", "200", "-y", "50", cmd])
        else:
            sh(["tmux", "new-window", "-t", session, "-n", name, cmd])
    print(f"\n  watch:   tmux attach -t {session}      (Ctrl-b n / p: next / previous experiment, Ctrl-b d: detach)")
    print(f"  logs:    {paths.OUT_ROOT}/<experiment>/logs/run.log\n")
    results: dict[str, int] = {}
    t0 = time.monotonic()
    try:
        while len(results) < len(names):
            for name, rc in rcs.items():
                if name not in results and rc.exists() and rc.read_text().strip():
                    results[name] = int(rc.read_text().strip())
                    print(f"  [{name}] finished: {'ok' if results[name] == 0 else f'FAILED (exit {results[name]})'}"
                          f"   after {duration(time.monotonic() - t0)}")
            if len(results) < len(names):
                time.sleep(5)
    except KeyboardInterrupt:
        print(f"\n  interrupted here; the experiments keep running in tmux session '{session}'.")
        raise SystemExit(130)
    return results


def run_all(names: list[str], force: bool) -> dict[str, int]:
    say(f"run (parallel, in this console: {', '.join(names)})")
    results: dict[str, int] = {}
    lock = threading.Lock()

    def one(name: str) -> None:
        rc = run_experiment(name, ["--from", steps.RUN_START] + (["--force"] if force else []), prefix=True)
        with lock:
            results[name] = rc

    threads = [threading.Thread(target=one, args=(n,), name=n) for n in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force", action="store_true", help="redo every step of every experiment")
    ap.add_argument("--experiments", default=None, help="comma-separated subset (default: all)")
    ap.add_argument("--list", action="store_true", help="show what would run and exit")
    ap.add_argument("--no-tmux", action="store_true",
                    help="run the experiments in this console (prefixed lines) instead of one tmux window each")
    args = ap.parse_args(argv)
    use_tmux = not args.no_tmux and shutil.which("tmux") is not None
    if not args.no_tmux and not use_tmux and not args.list:
        print("  note: tmux is not installed, so the experiments will run in this console with their\n"
              "  names in front of every line (sudo yum/apt install tmux, or setup-ae.sh, gives each\n"
              "  one its own window next time). Their consoles are kept in out/<experiment>/logs/run.log.")
    names = args.experiments.split(",") if args.experiments else discover()
    for n in names:
        script(n)
    if args.list:
        print(f"experiments : {', '.join(names)}")
        print(f"prepare     : br-base, then each experiment --to {steps.PREPARE_END}, one at a time")
        print(f"run         : each experiment --from {steps.RUN_START}, all at once"
              + (f", one tmux window each (session '{SESSION}')" if use_tmux else ", prefixed lines in this console"))
        print(f"out         : {paths.OUT_ROOT}")
        return 0
    started = time.monotonic()
    paths.OUT_ROOT.mkdir(parents=True, exist_ok=True)
    try:
        prepare(names, args.force)
    except StageFailed as e:
        die(e)
    results = run_all_tmux(names, args.force) if use_tmux else run_all(names, args.force)
    say("summary")
    for n in names:
        print(f"  {n:16} {'ok' if results.get(n) == 0 else f'FAILED (exit {results.get(n)})'}"
              f"   report and outputs under {paths.OUT_ROOT / n}")
    print(f"\n  total elapsed: {duration(time.monotonic() - started)}")
    return 0 if all(rc == 0 for rc in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
