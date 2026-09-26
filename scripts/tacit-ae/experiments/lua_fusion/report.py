"""What the run concluded: the comparison table, the instrument canaries, the caveat.

This was a heredoc inside the shell driver, carrying its own copies of the arm
table that could drift from the shell's. It now reads the same `variants` module
everything else does, and can be run on its own against existing bundles.
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path

import paths
from shell import ok, say, sh, step, warn
from uartlog import parse as parse_uartlog
from variants import ANALYSIS, Variant

CHECKSUM = r"(inset \d+ checksum \d+)"   # what mandelbrot prints; not a platform fact
MIN_GUARD_TRAFFIC = 10_000               # below this a non-jr site is noise, not a guard
ENTRY_TOLERANCE = 1e-4                   # handler-entry counts must agree this closely


@dataclass
class Row:
    v: Variant
    window: int = 0
    total: int = 0
    checksum: str = "?"
    stall: int = 0
    entries: int | None = None
    fallthrough: list = field(default_factory=list)
    guard_sites: list = field(default_factory=list)

    @property
    def declared(self) -> int:
        return len(self.v.guards)


def collect(vs: list[Variant]) -> list[Row]:
    rows = []
    for v in vs:
        if not v.uartlog.exists():
            continue
        log = parse_uartlog(v.uartlog)
        r = Row(v, window=log.window_cycles or 0, total=log.total_cycles or 0,
                checksum=log.search(CHECKSUM) or "?", stall=log.stall_cycles)

        # The decoder's own account of what it saw.
        summary = v.bundle_dir / "out" / "lua.dispatch_stats.summary.json"
        if summary.exists():
            j = json.loads(summary.read_text())
            r.entries = j["entries_total"]
            r.fallthrough = j["fallthrough_entries"]

        # A guard is any handler entry NOT made by the indirect jr: a branch onto the
        # stub in front of the entry, or a direct jump landing on it. Counting distinct
        # non-jr sites is what a declared guard must produce, however GCC laid it out.
        sites = v.bundle_dir / "out" / "lua.dispatch_stats.sites.csv"
        if sites.exists():
            with open(sites) as f:
                r.guard_sites = sorted({
                    (row["site_pc"].strip(), row["site_kind"].strip(), row["to_handler"].strip())
                    for row in csv.DictReader(f, skipinitialspace=True)
                    if row["site_kind"].strip() != "jr" and int(row["count"]) >= MIN_GUARD_TRAFFIC})
        rows.append(r)
    return rows


def results_table(rows: list[Row]) -> None:
    base = rows[0].window
    print(f"  {'arm':28} {'window cycles':>15} {'vs baseline':>12} {'total cycles':>15}  correctness")
    for r in rows:
        delta = f"{100 * (r.window - base) / base:+.2f}%" if base and r.window else "—"
        print(f"  {r.v.table_label:28} {r.window:15,} {delta:>12} {r.total:15,}  {r.checksum}")

    same = len({r.checksum for r in rows}) == 1
    print(f"\n  program output identical across arms: {'YES' if same else 'NO -- INVESTIGATE'}")
    print(f"  max trace-unit stall: {max(r.stall for r in rows)} cycles "
          f"(perturbation is nil if this is small vs the window)")


def canaries(rows: list[Row]) -> bool:
    """Two checks that say whether the dispatch profile can be trusted at all.

    (1) Every arm executes the same bytecode, so the decoder must see the same
        number of handler entries in each. A shortfall means arrivals the
        instrument cannot see -- exactly the failure that once hid guarded ones.
    (2) A guard is the only reason a handler is ever entered without going through
        the indirect jump, so the number of such entry sites must equal the number
        of guards the arm declares.
    """
    have = [r for r in rows if r.entries is not None]
    if not have:
        return True
    print(f"\n  {'arm':28} {'handler entries':>16} {'vs baseline':>12} "
          f"{'non-jr entry sites':>19} {'declared guards':>16}  check")
    ref, all_ok = have[0].entries, True
    for r in have:
        drift = abs(r.entries - ref) / ref
        good = drift < ENTRY_TOLERANCE and len(r.guard_sites) == r.declared
        all_ok &= good
        print(f"  {r.v.table_label:28} {r.entries:16,} {100 * (r.entries - ref) / ref:+11.4f}% "
              f"{len(r.guard_sites):19d} {r.declared:>16}  {'ok' if good else 'MISMATCH'}")
        for pc, kind, to in r.guard_sites:
            print(f"  {'':28} guard site {pc} ({kind}) -> handler {to}")
        for e in r.fallthrough:
            print(f"  {'':28} fall-through {e['block_start']} -> handler "
                  f"{e['handler']}  x{e['count']:,}")
    verdict = "PASS" if all_ok else \
        "FAIL -- a MISMATCH arm's dispatch profile is not trustworthy"
    print(f"  instrument canaries: {verdict}")
    return all_ok


def figures(rows: list[Row], out: Path) -> None:
    log = out / "logs" / "report.log"

    step("runtime figure")
    bars = [f"{r.v.label}={r.v.bundle_dir}" for r in rows]
    try:
        sh([paths.PY, ANALYSIS / "plot_runtime_bars.py",
            "--out", out / "fig.runtime", *bars], log)
        ok(str(out / "fig.runtime.pdf"))
    except Exception:
        warn(f"see {log}")

    # The grid needs each arm's own optab: handlers move between builds, so an
    # address means nothing without the build that produced it.
    step("per-predecessor grid")
    hists = [(r, r.v.bundle_dir / "out" / "lua.dispatch_hist.csv") for r in rows]
    if not all(h.exists() for _, h in hists):
        return warn("needs the decode stage for every arm")
    grid = [f"{r.v.label}={h}:{r.v.optab_path}" for r, h in hists]
    try:
        sh([paths.PY, ANALYSIS / "plot_pred_grid.py", "--targets", "MUL,ADD",
            "--out", out / "fig.pred_grid", *grid], log)
        ok(str(out / "fig.pred_grid.pdf"))
    except Exception:
        warn(f"see {log}")


def caveat(out: Path) -> None:
    """Where the outputs are, and what the headline numbers do not account for."""
    print(f"\n  everything this run produced is under {out} :")
    print("    fig.runtime.pdf, fig.pred_grid.pdf     across-arm figures")
    print("    <arm>/vbb.mandelbrot.txt               blocks ranked by variance-weighted cost")
    print("    <arm>/fig.bb_distributions.pdf         per-block latency distributions")
    print("    <arm>/trace.<arm>.*.csv                the decoder's tables, copied here")
    print("    logs/<arm>.<stage>.log                 one transcript per stage")
    print("    <arm>/bundle/                          the capture: trace, binaries, dwarf,")
    print("                                           patch map, and the decoder's tables")


def main(vs: list[Variant], out: Path) -> bool:
    say("results")
    rows = collect(vs)
    if not rows:
        print("  no uartlogs found -- run the `run` and `bundle` stages first")
        return False
    figures(rows, out)
    results_table(rows)
    passed = canaries(rows)
    caveat(out)
    return passed
