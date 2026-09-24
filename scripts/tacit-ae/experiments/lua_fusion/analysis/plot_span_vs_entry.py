#!/usr/bin/env python3
"""Does ranking dispatch edges by the whole handler span pick the same edges as ranking
them by the entry block? One figure and one table, from two decodes of the SAME baseline
capture: the dispatch_stats receiver timing the entry block (dispatch cost alone) and the
same receiver with "span": "handler" (entry + handler body).

On an unfused computed-goto interpreter every op boundary is a `jr`, so a jalr-stamped
tracer measures the whole-handler span exactly: span is the best signal such a tracer can
have. The two rankings agree only where the body cost does not depend on the predecessor.
Where they disagree, the span view is being driven by body variance -- cache and data
effects inside the handler -- that a guard cannot remove. LEI->MUL is the case in point:
the span ranks it among the top edges; the entry block says its arrival is already
predicted; and the LEI->MUL guard, built and run, changes the runtime by nothing.

Excess = n*(mean - floor), floor = min-of-p50 into that handler over hot predecessors,
computed on each unit separately (as the selection tool does).

  plot_span_vs_entry.py --optab configs/optab_base.json \
      --entry base/bundle/out/lua.dispatch_stats.csv --span base/bundle/out-span/lua.dispatch_stats.csv \
      --window 6.548791 --out fig.span_vs_entry [--top 6]
"""
import argparse
import json
import pathlib
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from paper_palette import NAVY, CORAL, INK, GRID, NEUTRAL  # noqa: E402

MIN_N = 10_000
MM = 1 / 25.4


def load(csv, optab):
    d = pd.read_csv(csv, skipinitialspace=True)
    for c in ("from_handler", "to_handler"):
        d[c] = d[c].astype(str).str.strip()
    d["f"] = d["from_handler"].map(optab)
    d["t"] = d["to_handler"].map(optab)
    d = d.dropna(subset=["f", "t"]).copy()
    d["sum"] = d["count"] * d["mean"]
    g = d.groupby(["f", "t"]).agg(n=("count", "sum"), sum=("sum", "sum"),
                                  p50=("p50", "median"), p90=("p90", "median"))
    g["mu"] = g["sum"] / g["n"]
    hot = g[g.n >= MIN_N]
    floor = hot.groupby("t")["p50"].min()
    g["floor"] = g.index.get_level_values("t").map(floor).to_numpy()
    g["excess"] = (g["n"] * (g["mu"] - g["floor"])).clip(lower=0)
    return g


def verdict(r) -> str:
    if r.rank_e <= 3 and r.rank_s > 5:
        return "span buries a real dispatch edge"
    if r.rank_s <= 3 and r.rank_e > 5:
        return "span promotes a body-variance edge"
    if abs(r.rank_e - r.rank_s) >= 3:
        return "moved"
    return ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--optab", required=True)
    ap.add_argument("--entry", required=True)
    ap.add_argument("--span", required=True)
    ap.add_argument("--window", type=float, required=True, help="traced window in Gcycles")
    ap.add_argument("--out", required=True, help="output stem (.pdf and .png)")
    ap.add_argument("--top", type=int, default=6, help="rows: the union of the top N under either unit")
    a = ap.parse_args()
    optab = json.load(open(a.optab))
    win = a.window * 1e9
    E, S = load(a.entry, optab), load(a.span, optab)
    J = E.join(S, lsuffix="_e", rsuffix="_s", how="inner")
    J = J[J.n_e >= MIN_N].copy()
    J["body"] = J.mu_s - J.mu_e
    J["rank_e"] = J.excess_e.rank(ascending=False).astype(int)
    J["rank_s"] = J.excess_s.rank(ascending=False).astype(int)
    sp = float(J.excess_e.rank().corr(J.excess_s.rank()))
    tops = {k: len(set(J.excess_e.nlargest(k).index) & set(J.excess_s.nlargest(k).index)) for k in (3, 5, 10)}
    top = J[(J.rank_e <= a.top) | (J.rank_s <= a.top)].sort_values("excess_e", ascending=False)

    # ---- table --------------------------------------------------------------------
    print(f"{len(J)} hot edges   entry-unit excess {J.excess_e.sum() / 1e9:.3f} G ({100 * J.excess_e.sum() / win:.1f}% of window)"
          f"   span-unit excess {J.excess_s.sum() / 1e9:.3f} G ({100 * J.excess_s.sum() / win:.1f}%)")
    print(f"Spearman(entry vs span excess) {sp:.3f}; top-3/5/10 overlap {tops[3]}/3 {tops[5]}/5 {tops[10]}/10\n")
    print(f"{'edge':14s} {'n':>8s} | {'entry':>6s} {'floor':>5s} {'excess M':>9s} {'rank':>4s} | "
          f"{'span':>6s} {'floor':>5s} {'excess M':>9s} {'rank':>4s} | {'body':>5s}")
    for (f, t), r in top.iterrows():
        print(f"{f + '->' + t:14s} {r.n_e / 1e6:7.1f}M | {r.mu_e:6.2f} {r.floor_e:5.0f} {r.excess_e / 1e6:9.1f} {int(r.rank_e):4d} | "
              f"{r.mu_s:6.2f} {r.floor_s:5.0f} {r.excess_s / 1e6:9.1f} {int(r.rank_s):4d} | {r.body:5.1f}  {verdict(r)}")

    # ---- figure: one row per edge, two dots (entry-unit vs span-unit excess) ----------
    rows = list(top.iterrows())
    names = [f"{f}→{t}" for (f, t), _ in rows]
    ys = list(range(len(rows) - 1, -1, -1))
    ex_e = [r.excess_e / 1e6 for _, r in rows]
    ex_s = [r.excess_s / 1e6 for _, r in rows]
    rc = {"font.family": "sans-serif", "font.size": 8, "axes.labelsize": 8, "xtick.labelsize": 7,
          "ytick.labelsize": 7, "legend.fontsize": 7, "axes.linewidth": 0.6, "pdf.fonttype": 42, "ps.fonttype": 42}
    with plt.rc_context(rc):
        fig, ax = plt.subplots(figsize=(89 * MM, (18 + 7 * len(rows)) * MM))
        for y, e, s, (_, r) in zip(ys, ex_e, ex_s, rows):
            ax.plot([e, s], [y, y], color=NEUTRAL, linewidth=2.2, solid_capstyle="round", zorder=1)
            ax.plot(e, y, "o", color=NAVY, markersize=4.2, zorder=3)
            ax.plot(s, y, "o", markerfacecolor="white", markeredgecolor=CORAL, markeredgewidth=1.1, markersize=4.2, zorder=3)
            ax.annotate(f"#{int(r.rank_e)}", (e, y), textcoords="offset points", xytext=(0, 4.5),
                        ha="center", va="bottom", fontsize=5.5, color=NAVY)
            ax.annotate(f"#{int(r.rank_s)}", (s, y), textcoords="offset points", xytext=(0, -4.5),
                        ha="center", va="top", fontsize=5.5, color=CORAL)
            v = verdict(r)
            if v:
                ax.annotate(v, (max(e, s), y), textcoords="offset points", xytext=(6, 0), va="center",
                            fontsize=5.5, color=INK, style="italic")
        ax.plot([], [], "o", color=NAVY, markersize=4.2, label="entry block (dispatch cost alone)")
        ax.plot([], [], "o", markerfacecolor="white", markeredgecolor=CORAL, markeredgewidth=1.1, markersize=4.2,
                label="whole handler span")
        ax.set_yticks(ys)
        ax.set_yticklabels(names)
        ax.set_ylim(-0.7, len(rows) - 0.3)
        ax.set_xlim(0, max(ex_e + ex_s) * 1.55)
        ax.set_xlabel("recoverable excess, n·(mean − floor)  [Mcycles]", labelpad=2)
        ax.grid(axis="x", color=GRID, linewidth=0.5, zorder=0)
        ax.set_axisbelow(True)
        ax.spines[["top", "right"]].set_visible(False)
        ax.tick_params(axis="y", length=0)
        ax.legend(loc="lower right", frameon=False, handletextpad=0.4)
        fig.tight_layout(pad=0.3)
        for ext in ("pdf", "png"):
            fig.savefig(f"{a.out}.{ext}", dpi=300, bbox_inches="tight", pad_inches=0.02)
        plt.close(fig)
    print(f"\nwrote {a.out}.pdf/.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
