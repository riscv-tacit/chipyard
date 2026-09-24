#!/usr/bin/env python3
"""Grouped bar chart of eps against the TraceDoctor oracle, BB and function,
with TACIT's BB bar split into its two error components.

    semantic    eps against the whole-cycle (boundary) oracle -- real
                disagreement about which block a cycle belongs to
    structural  the extra error the 1/n-split oracle manufactures, which a
                lossless tracer would also incur: it is a property of the
                reference's fractional representation, not of the tracer

Only TACIT is split. The emulated formats' 1/n increment is NEGATIVE
(-0.05 to -1.37 pp): their error is large enough that the representational
component partly cancels against it rather than adding, so there is no
meaningful stacked segment to draw.

Sized for a single paper column (3.3 in) at 8 pt. Two subplots rather than one
dual-axis chart: BB spans ~1-133% and function ~0.02-69%, and a shared scale
would flatten the function panel into invisibility. Same x categories and the
same fixed estimator order in both, so a reader compares down a column.

Colors are the validated categorical slots 1-4 in fixed order (never cycled),
so an estimator keeps its hue across both panels.
"""
import csv, sys, pathlib
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.ticker import MultipleLocator

PT = 8
LEG = 7          # legend only: buys room for five entries in three columns
matplotlib.rcParams.update({
    "font.size": PT, "axes.titlesize": PT, "axes.labelsize": PT,
    "xtick.labelsize": PT, "ytick.labelsize": PT, "legend.fontsize": PT,
    "pdf.fonttype": 42, "ps.fonttype": 42,   # embed TrueType, not Type-3
})

D = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")

ORDER = ["tacit", "nret", "retc", "tc"]
# "+ret" would read as "adds RET packets"; retcompressed does the opposite --
# it elides them, predicting the target from a 64-deep return-address stack and
# spending one TNT bit instead of an address packet.
LABEL = {"tacit": "ATT (semantic)", "nret": "TNT+CYC",
         "retc": "TNT+CYC+RAS", "tc": "TC"}
COLOR = {"tacit": "#2a78d6", "nret": "#eb6834",
         "retc": "#1baf7a", "tc": "#eda100"}
INK, MUTED, GRID = "#1a1a19", "#6b6a63", "#b8b7b0"

def load(p):
    rows = {}
    for r in csv.DictReader(open(p)):
        rows.setdefault(r["benchmark"], {})[r["estimator"]] = float(r["eps_pct"])
    return rows

bb, fn = load(D / "eps_bb.csv"), load(D / "eps_func.csv")
inst = load(pathlib.Path(sys.argv[2]))          # 1/n BB sweep
STRUCT = "#a9c9f0"                              # lighter step of the TACIT hue
benches = sorted(bb)
# keep the SPEC number; drop only the _s suffix and the long xz input name
short = [b.replace("_s", "").replace("-cpu2006docs", "-docs") for b in benches]

fig, axes = plt.subplots(2, 1, figsize=(3.3, 3.9), sharex=True)
x = np.arange(len(benches))
w = 0.225                       # groups span 0.90 of the unit: tight gaps

for ax, data, ylab, ticks, minor, top in (
    (axes[0], bb, r"$\epsilon$(BB)  [%]",   [0, 50, 100, 150], 25, 150),
    (axes[1], fn, r"$\epsilon$(func)  [%]", [0, 20, 40, 60],    10,  75),
):
    for i, est in enumerate(ORDER):
        base = [data[b][est] for b in benches]
        ax.bar(x + (i - 1.5) * w, base, w * 0.92,
               label=LABEL[est], color=COLOR[est], linewidth=0, zorder=3)
        # the 1/n increment exists only for the BB sweep, and only TACIT's is
        # positive; a negative segment cannot be stacked
        if data is bb and est == "tacit":
            extra = [max(0.0, inst[b][est] - data[b][est]) for b in benches]
            ax.bar(x + (i - 1.5) * w, extra, w * 0.92, bottom=base,
                   label="ATT (structural)", color=STRUCT, linewidth=0, zorder=3)
    ax.set_ylabel(ylab, fontsize=PT, color=INK, labelpad=1)
    ax.set_xticks(x)
    ax.tick_params(axis="y", which="major", colors=INK, length=2.5,
                   width=0.6, direction="out", pad=1.5)
    ax.tick_params(axis="y", which="minor", length=1.5, width=0.5,
                   direction="out", color=INK)
    ax.tick_params(axis="x", colors=INK, length=2.5, width=0.6,
                   direction="out", pad=1.5)
    # one grey, one style: majors and minors differ only in whether the
    # value beside them is printed
    ax.grid(axis="y", which="both", color=GRID, linewidth=0.5,
            linestyle=(0, (1, 2)), zorder=0)
    ax.set_axisbelow(True)
    ax.set_xlim(-0.6, len(benches) - 0.4)
    ax.set_yticks(ticks)
    ax.yaxis.set_minor_locator(MultipleLocator(minor))
    ax.set_ylim(0, top)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_visible(True)
        ax.spines[s].set_color(INK)
        ax.spines[s].set_linewidth(0.6)


axes[1].set_xticklabels(short, rotation=45, ha="right",
                        fontsize=PT, color=INK)
axes[0].legend(frameon=False, ncol=3, loc="lower center", fontsize=LEG,
               bbox_to_anchor=(0.5, 1.06), handlelength=0.9,
               handletextpad=0.35, columnspacing=0.8, borderaxespad=0,
               labelcolor=INK)
fig.tight_layout(rect=(0, 0, 1, 0.94))
fig.subplots_adjust(hspace=0.11)
for ext in ("png", "pdf"):
    out = D / f"eps_vs_oracle_stacked.{ext}"
    fig.savefig(out, dpi=400, bbox_inches="tight", pad_inches=0.01,
                facecolor="white")
    print(f"  wrote {out}")
