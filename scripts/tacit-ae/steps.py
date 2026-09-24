"""The step contract every experiment follows, so one orchestrator can drive them all.

An experiment is an ordered tuple of step names. Two are fixed by contract:

    PREPARE_END  the last host-only step. Everything up to and including it needs no
                 run farm and, because the steps before it share FireMarshal's kernel
                 tree and mount point, must not overlap with another experiment's.
    RUN_START    the first step that needs the run farm. From here on experiments are
                 independent and can run concurrently (distinct run_farm_tag each).

Each experiment accepts the same flags (add_args) and turns them into the same plan
(plan). --from and --to bound the window; steps in it run skip-if-done, steps before it
run skip-if-done too (later steps need what they produce, and they are expected to be
done already), steps after --to do not run at all. Only --force makes a step redo work
whose outputs exist -- so `--from analyse --force` redoes analysis onward, `--from
fpga` resumes the run-farm half without touching what prepare built. The report runs
only when the window reaches the last step.
"""
from __future__ import annotations

import argparse

PREPARE_END = "driver"
RUN_START = "fpga"


def add_args(ap: argparse.ArgumentParser, steps: tuple[str, ...]) -> None:
    ap.add_argument("--force", action="store_true", help="redo every step whose outputs exist")
    ap.add_argument("--from", dest="from_step", choices=steps, metavar="STEP",
                    help=f"start the window here (with --force: redo from here): {', '.join(steps)}")
    ap.add_argument("--to", dest="to_step", choices=steps, metavar="STEP",
                    help="stop after this step (the report runs only if the window reaches the end)")
    ap.add_argument("--list", action="store_true", help="show the plan and exit")


def check(steps: tuple[str, ...]) -> None:
    """Every experiment must have both contract steps, in order."""
    assert PREPARE_END in steps and RUN_START in steps, f"steps {steps} lack the contract steps"
    assert steps.index(PREPARE_END) + 1 == steps.index(RUN_START), \
        f"{RUN_START} must directly follow {PREPARE_END} in {steps}"


def plan(steps: tuple[str, ...], args: argparse.Namespace) -> dict[str, bool | None]:
    """step -> None (do not run), False (run, skip if done), True (run, redo)."""
    check(steps)
    lo = steps.index(args.from_step) if args.from_step else None
    hi = steps.index(args.to_step) if args.to_step else len(steps) - 1
    if lo is not None and lo > hi:
        raise SystemExit(f"--from {args.from_step} is after --to {args.to_step}")
    out: dict[str, bool | None] = {}
    for i, s in enumerate(steps):
        if i > hi:
            out[s] = None
        elif args.force and (lo is None or i >= lo):
            out[s] = True
        else:
            out[s] = False
    return out


def reports(steps: tuple[str, ...], args: argparse.Namespace) -> bool:
    return args.to_step is None or args.to_step == steps[-1]
