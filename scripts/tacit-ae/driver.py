#!/usr/bin/env python3
"""Run one AE experiment: every selected stage, for every selected arm, in order.

Resumable by design. A stage whose output is already present is skipped unless
--force, because the expensive stages are the FPGA ones and a reviewer who hits a
failure in `analyse` should not have to re-simulate to get back to it.
"""
from __future__ import annotations

import argparse
import importlib
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import paths                                          # noqa: E402
from shell import StageFailed, die, duration, have, ok, say, skip, step  # noqa: E402

DEFAULT_EXPERIMENT = "lua_fusion"


def load(name: str):
    """Import an experiment's modules. They import each other by plain name, so the
    experiment directory goes on the path rather than being treated as a package."""
    d = HERE / "experiments" / name
    if not d.is_dir():
        avail = ", ".join(p.name for p in (HERE / "experiments").iterdir() if p.is_dir())
        raise SystemExit(f"no experiment '{name}'. Available: {avail}")
    sys.path.insert(0, str(d))
    return (importlib.import_module("variants"), importlib.import_module("stages"),
            importlib.import_module("report"))


def preflight(selected: set[str]) -> None:
    """Check only what the selected stages actually need, before anything expensive."""
    say("environment")
    paths.require("CY", "TD", "FM", "FS", "PY")
    if "build" in selected:
        step("riscv toolchain")
        ok() if have("riscv64-unknown-linux-gnu-gcc") else die("riscv gcc not on PATH -- source env.sh")
    if "image" in selected:
        step("debugfs (rootfs verification)")
        ok() if have("debugfs") else die("debugfs not on PATH -- install e2fsprogs")
    if "run" in selected:
        step("firesim")
        ok() if have("firesim") else die("firesim not on PATH -- source sourceme-manager.sh")
    step("python dependencies")
    try:
        import elftools, matplotlib, pandas  # noqa: F401
        ok()
    except ImportError as e:
        die(f"missing python package: {e.name} -- pip install pandas matplotlib pyelftools")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("experiment", nargs="?", default=DEFAULT_EXPERIMENT)
    ap.add_argument("--arms", help="comma-separated, in order; the first is the baseline")
    ap.add_argument("--stages", help="comma-separated subset; they always run in pipeline order")
    ap.add_argument("--force", action="store_true", help="redo stages whose output exists")
    ap.add_argument("--narrow", action="store_true", help="skip bb_pair_stats: half the time and memory")
    ap.add_argument("--list", action="store_true", help="show the plan and exit")
    args = ap.parse_args(argv)

    V, S, R = load(args.experiment)
    arms = V.select(args.arms.split(",") if args.arms else V.DEFAULT)
    names = [s.__name__ for s in S.STAGES] + ["report"]
    selected = set(args.stages.split(",")) if args.stages else set(names)
    if unknown := selected - set(names):
        raise SystemExit(f"unknown stage(s): {', '.join(sorted(unknown))}. Known: {', '.join(names)}")
    out = S.outdir()

    if args.list:
        print(f"experiment : {args.experiment}\narms       : {', '.join(a.key for a in arms)}")
        print(f"stages     : {', '.join(n for n in names if n in selected)}\nout        : {out}\n")
        for a in arms:
            print(f"  {a.key:11} {a.tree:24} {a.edit}")
        return 0

    preflight(selected)
    started = time.monotonic()
    per_arm = [s for s in S.STAGES if s.__name__ in selected]
    for v in arms if per_arm else []:
        say(f"arm: {v.key}   ({v.tree})")
        for stage in per_arm:
            step(S.TITLE.get(stage.__name__, stage.__name__))
            try:
                kw = {"narrow": args.narrow} if stage.__name__ == "bundle" else {}
                stage(v, S.logfile(v, stage.__name__), args.force, **kw)
            except StageFailed as e:
                die(e)
            except Exception as e:                    # a helper's own diagnosis
                die(str(e))

    passed = True
    if "report" in selected:
        passed = R.main(arms, out)
    print(f"\n  total elapsed: {duration(time.monotonic() - started)}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
