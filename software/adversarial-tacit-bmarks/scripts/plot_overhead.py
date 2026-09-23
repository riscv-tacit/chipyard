#!/usr/bin/env python3
"""Single-column bar chart of TACIT tracing overhead (traced/untraced - 1, %) for
the report benches, grouped by family, each bar annotated with the offered
packet rate and the packetizer cost that explain it.
Usage: plot_overhead.py <results-workload dir> <out prefix>"""
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path

import matplotlib as mpl
mpl.use("Agg")
import matplotlib.pyplot as plt

MM = 1 / 25.4
mpl.rcParams.update({
    "font.family": "sans-serif",
    "font.sans-serif": ["Arial", "Helvetica", "Liberation Sans", "DejaVu Sans"],
    "font.size": 8, "axes.labelsize": 8, "axes.titlesize": 8,
    "xtick.labelsize": 8, "ytick.labelsize": 7.5, "legend.fontsize": 7,
    "axes.linewidth": 0.6, "xtick.major.width": 0.5, "ytick.major.width": 0.5,
    "xtick.major.size": 2.0, "ytick.major.size": 2.0,
    "pdf.fonttype": 42, "ps.fonttype": 42, "savefig.dpi": 300,
})

# bench -> (family, display label, packetizer cycles per packet)
BENCHES = [
    ("nt_f0", "not-taken", "0", 1), ("nt_f1", "not-taken", "1", 1), ("nt_f3", "not-taken", "3", 1),
    ("tk_f0", "taken", "0", 1), ("tk_f1", "taken", "1", 1), ("tk_f3", "taken", "3", 1),
    ("tjr_near", "indirect", "near", 2), ("tjr_d13", "indirect", "8K", 2), ("tjr_d24", "indirect", "16M", 3),
]
FAMILY_TITLE = {"not-taken": "Not-taken, fillers", "taken": "Taken, fillers", "indirect": "Indirect, distance"}
COLORS = {"not-taken": "#0072B2", "taken": "#009E73", "indirect": "#D55E00"}  # Okabe-Ito

LINE = re.compile(r"RESULT (.*)")


def load(d):
    rows = defaultdict(lambda: defaultdict(dict))  # bench -> mode -> rep -> kv
    for f in Path(d).rglob("uartlog"):
        for line in f.read_text(errors="replace").splitlines():
            m = LINE.search(line)
            if not m:
                continue
            kv = dict(t.split("=", 1) for t in m.group(1).split())
            rows[kv["bench"]][kv["mode"]][int(kv["rep"])] = {k: int(v) for k, v in kv.items() if k not in ("bench", "mode")}
    return rows


def main():
    d, out = sys.argv[1], sys.argv[2]
    rows = load(d)
    labels, heights, lo, hi, notes, cols, fams = [], [], [], [], [], [], []
    for bench, fam, lab, cpp in BENCHES:
        u, t = rows[bench]["untraced"], rows[bench]["traced"]
        # interleaved reps: pair rep i untraced with rep i traced
        ratios = [t[r]["cycles"] / u[r]["cycles"] for r in sorted(t) if r in u]
        ov = [(x - 1) * 100 for x in ratios]
        med = statistics.median(ov)
        # offered packet rate: packets ~= bytes / mean packet size; use untraced cycles
        # compressed 1 B for branches, measured bytes/pkt for jumps (header+target+time)
        pkts = statistics.median(t[r]["bytes"] for r in t)
        if fam == "indirect":
            pkt_bytes = {"near": 4, "8K": 4, "16M": 6}[lab]
            pkts /= pkt_bytes
        offered = pkts / statistics.median(u[r]["cycles"] for r in u)
        labels.append(f"{lab}\n{offered:.2f}"); heights.append(med); lo.append(med - min(ov)); hi.append(max(ov) - med)
        notes.append(f"{cpp} cyc/pkt" if fam == "indirect" or bench == "nt_f0" else ""); cols.append(COLORS[fam]); fams.append(fam)

    # normalized runtime: traced / untraced, in percent (100 = no overhead)
    runtime = [h + 100 for h in heights]
    fig, ax = plt.subplots(figsize=(89 * MM, 40 * MM))
    x, pos, gap, prev = [], 0.0, 0.8, None
    for fam in fams:
        if prev is not None and fam != prev:
            pos += gap
        x.append(pos); pos += 1.0; prev = fam
    ax.bar(x, runtime, width=0.74, color=cols, edgecolor="none", zorder=2)
    ax.axhline(100, color="#555555", linewidth=0.5, linestyle=(0, (3, 2)), zorder=1)
    for xi, r in zip(x, runtime):
        ax.text(xi, r + 7, f"{r:.0f}", ha="center", va="bottom", fontsize=7, color="#222222")
    ax.set_xticks(x)
    ax.set_xticklabels([l.split("\n")[0] for l in labels])
    ax.set_ylabel("Runtime (% untraced)")
    ax.set_ylim(0, 365)
    ax.set_yticks([0, 100, 200, 300])
    ax.yaxis.grid(True, linewidth=0.3, color="#cccccc", zorder=0)
    ax.set_axisbelow(True)
    ax.spines[["top", "right"]].set_visible(False)
    ax.tick_params(axis="x", length=0, pad=1.5)
    # second axis layer: coloured rule + family/knob name under each group
    tr = ax.get_xaxis_transform()
    for fam in FAMILY_TITLE:
        xs = [xi for xi, f in zip(x, fams) if f == fam]
        ax.plot([xs[0] - 0.37, xs[-1] + 0.37], [-0.155, -0.155], color=COLORS[fam],
                linewidth=0.7, transform=tr, clip_on=False)
        ax.text(sum(xs) / len(xs), -0.185, FAMILY_TITLE[fam], ha="center", va="top",
                fontsize=7.5, color=COLORS[fam], transform=tr)
    ax.margins(x=0.02)
    fig.subplots_adjust(left=0.132, right=0.995, top=0.99, bottom=0.285)
    # exact canvas (no tight bbox) so the file is column-width and text is truly 8 pt
    for ext in ("pdf", "png"):
        fig.savefig(f"{out}.{ext}")
    for lab, h, n in zip(labels, heights, notes):
        print(f"{lab.split(chr(10))[0]:8s} runtime {h+100:6.1f}%")


if __name__ == "__main__":
    main()
