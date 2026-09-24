#!/usr/bin/env python3
"""Lossy coverage-loss figure: lost cycles per million traced cycles, one dot per
benchmark, log x-axis. Ported from the paper's figure script (software/spec2017/
paper-figure-lossy.py) minus the depth-sweep table.

    lollipop.py <lossy-coverage.csv> <out-stem>

Log x-axis: the data spans well under 1 ppm to hundreds (or, on some inputs, tens of
thousands), and on a linear axis most benchmarks collapse to an invisible sliver. A dot
plot rather than bars: bar length on a log axis is not proportional to the value it
encodes. Suite order rather than sorted by value, so the figure cross-references the
table; numeric prefixes retained for the same reason.
"""
import csv
import math
import sys

import matplotlib
matplotlib.use("Agg")
from matplotlib import pyplot as plt
from matplotlib.ticker import FixedLocator, LogLocator

MM = 1 / 25.4
SINGLE_COL = 89 * MM
FIG_H = 48 * MM
BLUE, GREY = "#0072B2", "#555555"   # Okabe-Ito
RC = {
    "font.family": "sans-serif",
    "font.sans-serif": ["Liberation Sans", "Arial", "Helvetica", "DejaVu Sans"],
    "font.size": 8, "axes.labelsize": 8, "xtick.labelsize": 7, "ytick.labelsize": 7,
    "axes.linewidth": 0.6, "xtick.major.width": 0.5, "ytick.major.width": 0.5,
    "xtick.major.size": 2.5, "ytick.major.size": 2.5,
    "xtick.minor.width": 0.4, "xtick.minor.size": 1.5,
    "pdf.fonttype": 42, "ps.fonttype": 42,
}
SHORT = {
    "600.perlbench_s": "600.perlbench", "602.gcc_s": "602.gcc", "605.mcf_s": "605.mcf",
    "620.omnetpp_s": "620.omnetpp", "623.xalancbmk_s": "623.xalancbmk", "625.x264_s": "625.x264",
    "631.deepsjeng_s": "631.deepsjeng", "641.leela_s": "641.leela", "648.exchange2_s": "648.exchange2",
    "657.xz_s-cpu2006docs": "657.xz·docs", "657.xz_s-cld": "657.xz·cld",
}
ORDER = list(SHORT)


def sig3(v: float) -> str:
    """Three significant figures, never scientific notation (the paper's labels)."""
    if v >= 100:
        return f"{v:.0f}"
    if v >= 10:
        return f"{v:.1f}"
    return f"{v:.2f}"


def main() -> int:
    src, stem = sys.argv[1], sys.argv[2]
    rows = {r["benchmark"]: r for r in csv.DictReader(open(src))}
    data = [(SHORT[b], float(rows[b]["gap_fraction"]) * 1e6) for b in ORDER if b in rows]
    if not data:
        raise SystemExit(f"no known benchmarks in {src}")
    names = [n for n, _ in data]
    vals = [max(v, 0.0) for _, v in data]
    # Axis bounds from the data, in whole decades, with a floor at 0.5 ppm so a zero
    # (no gap at all) still shows as a dot at the left edge rather than vanishing.
    floor = 0.5
    shown = [max(v, floor) for v in vals]
    hi = 10 ** math.ceil(math.log10(max(shown) * 1.5))
    ys = list(range(len(data) - 1, -1, -1))   # index 0 at the top
    with plt.rc_context(RC):
        fig, ax = plt.subplots(figsize=(SINGLE_COL, FIG_H))
        ax.hlines(ys, floor * 0.8, shown, color=BLUE, linewidth=0.7, alpha=0.55, zorder=2)
        ax.plot(shown, ys, "o", color=BLUE, markersize=3.2, zorder=3)
        for y, v, s in zip(ys, vals, shown):
            ax.annotate("0" if v == 0 else sig3(v), (s, y), textcoords="offset points",
                        xytext=(4, 0), va="center", fontsize=6, color=GREY)
        ax.set_xscale("log")
        ax.set_xlim(floor, hi * 3)          # room on the right for the value labels
        ax.spines["bottom"].set_bounds(floor, hi)
        ax.xaxis.set_major_locator(LogLocator(base=10))
        ax.xaxis.set_minor_locator(FixedLocator(
            [v * d for d in (0.1, 1, 10, 100, 1000, 10000) for v in range(2, 10) if floor <= v * d <= hi]))
        ax.set_ylim(-0.7, len(data) - 0.3)
        ax.set_yticks(ys)
        ax.set_yticklabels(names)
        ax.set_xlabel("Lost Cycles per Million Traced Cycles (log)", labelpad=1.5)
        ax.spines[["top", "right"]].set_visible(False)
        ax.tick_params(axis="y", length=0)
        for ext in ("pdf", "png"):
            fig.savefig(f"{stem}.{ext}", bbox_inches="tight", pad_inches=0.02, dpi=400)
        plt.close(fig)
    pooled = sum(float(r["gap_cycles"]) for r in rows.values()) / sum(float(r["window_cycles"]) for r in rows.values())
    print(f"wrote {stem}.pdf/.png; pooled loss {pooled * 1e6:.1f} ppm (coverage {100 - pooled * 100:.4f}%)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
