#!/usr/bin/env python3
"""Coverage analysis for lossy-mode TACIT runs (spec17-intspeed-*-lossy).

Parses the per-job counter logs written by trace-start/trace-stop --
<results-dir>/<job>/output/<bmark>-lossy.txt -- and reports, per benchmark, how
much of the traced window the encoder was inside a gap.

Headline metric:

    gap fraction = gap_cycles / window_cycles

i.e. the share of traced cycles spent inside a Pause..Resume gap, which is
execution the trace does not cover. Coverage is 1 - gap fraction.

The window is bracketed by rdcycle/rdinstret sampled just after enable
(trace-start) and just before disable (trace-stop), so it excludes the
untraced warm-up.

These numbers are only meaningful under sustained sink backpressure. Load the
tacit driver in ring mode (the default since the DMA sink's overflow mode stops
backpressuring once the buffer fills, which flatters the encoder arbitrarily).
The script warns when a log looks like it was taken without real backpressure.

Usage:
    ./compare-lossy.py RESULTS_DIR [RESULTS_DIR ...]

Multiple directories are treated as repetitions of the same workload and
averaged per benchmark.
"""
import argparse
import pathlib
import re
import sys

import numpy as np
import matplotlib
matplotlib.use("Agg")
from matplotlib import pyplot as plt

# Typography, colour and sizing matched to compare-overhead-ref.py's combined():
# built at final print size, sans-serif metric-compatible with Arial, units in
# the axis label, Okabe-Ito colorblind-safe fill, no top/right spines, no title
# (the caption carries it), vector PDF with editable text.
MM = 1 / 25.4
DOUBLE_COL = 183 * MM     # full width (ACM/IEEE 2-col is ~178 mm)
FIG_H = 80 * MM           # one panel; combined() spends 170 mm on three
BLUE = "#0072B2"          # Okabe-Ito
RED = "#D55E00"           # Okabe-Ito vermillion (not pure red)

RC = {
    "font.family": "sans-serif",
    "font.sans-serif": ["Liberation Sans", "Arial", "Helvetica", "DejaVu Sans"],
    # Nature-spec 5-6 pt is too small at this size; sized for a systems-paper
    # double-column figure placed at 1:1 (rescaling would drag it off spec).
    "font.size": 11.5, "axes.labelsize": 11.5,
    "xtick.labelsize": 11.5, "ytick.labelsize": 11.5, "legend.fontsize": 8.5,
    "axes.linewidth": 0.8,
    "xtick.major.width": 0.7, "ytick.major.width": 0.7,
    "xtick.major.size": 3.0, "ytick.major.size": 3.0,
    "pdf.fonttype": 42, "ps.fonttype": 42,
}

# Must match the driver's dma_size_mb (tacit_dma.c), as the DMA byte count
# register holds the within-buffer offset and wraps are counted separately.
DMA_BUF_SIZE = 4194304  # 4 MiB

# A lossy run should replace stalls with pauses; more than this fraction of the
# window spent stalled means the encoder was not actually in lossy mode.
STALL_WARN_FRACTION = 0.001

_SCALAR = re.compile(
    r"^(window_start_cycle|window_start_instret|window_end_cycle|"
    r"window_end_instret|stall count|dma count|dma wrap count|"
    r"dma src rdy stall count|cycles|instret|"
    r"elapsed_ns):\s*(\d+)\s*$")
# Its own pattern: trace-submit appends " (requested N)" after the value, while
# trace-stop prints it bare. Anchoring to end-of-line silently lost the former.
_WM = re.compile(r"^resume watermark:\s*(\d+)")
_GAPS = re.compile(
    r"^gap cycles:\s*(\d+)\s+dropped:\s*(\d+)\s+pauses:\s*(\d+)\s*$")
_LOSSY = re.compile(r"^tacit lossy mode:\s*(\d+)\s*$")

# Two shapes of counter log:
#   windowed  (trace-start/trace-stop): absolute window_start_*/window_end_* pairs
#   whole-run (trace-submit)          : cycles/instret already differenced
WINDOWED = ("window_start_cycle", "window_end_cycle",
            "window_start_instret", "window_end_instret")
WHOLE_RUN = ("cycles", "instret")

# A benchmark's run script may invoke trace-submit several times (perlbench and
# gcc do). Aggregation differs by counter origin:
#   ENCODER counters are cleared at each enable (window-scoped, by design), so
#     each invocation reports only its own -- they must be SUMMED.
#   DMA SINK counters are not touched by encoder enable and stay cumulative
#     across invocations -- take the LAST.
#   cycles/instret/elapsed_ns are per-invocation deltas -- SUM.
# Taking the last value for everything silently reports only the final
# invocation: perlbench's 5 invocations would read 0.0005% instead of 0.0113%.
SUM_KEYS = {"cycles", "instret", "elapsed_ns", "stall_count", "gap_cycles",
            "dropped_packets", "pauses", "dropped_insns"}

# Canonical suite order, matching compare-overhead-ref.py's BENCHMARKS and the
# marshal-config job order. Plain alphabetical sorting puts 657.xz_s-cld before
# 657.xz_s-cpu2006docs, which flips them relative to every other figure.
# intspeed.sh names split-workload outputs <bmark>_<n>, losing the input's
# identity. Map them back so ordering is deterministic and labels are meaningful.
ALIASES = {"657.xz_s_0": "657.xz_s-cpu2006docs", "657.xz_s_1": "657.xz_s-cld"}

BENCHMARK_ORDER = ["600.perlbench_s", "602.gcc_s", "605.mcf_s", "620.omnetpp_s",
                   "623.xalancbmk_s", "625.x264_s", "631.deepsjeng_s",
                   "641.leela_s", "648.exchange2_s", "657.xz_s-cpu2006docs",
                   "657.xz_s-cld"]


def _order_key(name):
    """Canonical position, with anything unrecognised sorted to the end."""
    try:
        return (0, BENCHMARK_ORDER.index(name))
    except ValueError:
        return (1, name)


def parse_lossy_log(path):
    """Parse one trace-start/trace-stop counter log into a dict of raw counters."""
    raw = {}
    for line in path.read_text(errors="replace").splitlines():
        m = _SCALAR.match(line)
        if m:
            k, v = m.group(1).replace(" ", "_"), int(m.group(2))
            raw[k] = raw.get(k, 0) + v if k in SUM_KEYS else v
            if k == "cycles":
                raw["invocations"] = raw.get("invocations", 0) + 1
            continue
        m = _GAPS.match(line)
        if m:
            for k, v in zip(("gap_cycles", "dropped_packets", "pauses"),
                            (int(g) for g in m.groups())):
                raw[k] = raw.get(k, 0) + v      # encoder counters: per-invocation
            continue
        m = _WM.match(line)
        if m:
            raw["resume_watermark"] = int(m.group(1))
            continue
        m = _LOSSY.match(line)
        if m:
            raw["lossy"] = int(m.group(1))
    return raw


def derive(raw):
    """Turn raw counters into the per-benchmark metrics, or None if unusable."""
    if all(k in raw for k in WINDOWED):
        cycles = raw["window_end_cycle"] - raw["window_start_cycle"]
        instret = raw["window_end_instret"] - raw["window_start_instret"]
    elif all(k in raw for k in WHOLE_RUN):
        cycles, instret = raw["cycles"], raw["instret"]
    else:
        missing = [k for k in WINDOWED if k not in raw]
        return None, f"missing counters: {', '.join(missing)}"
    if cycles <= 0:
        return None, f"non-positive window ({cycles} cycles); start/stop mispaired?"

    gap = raw.get("gap_cycles", 0)
    pauses = raw.get("pauses", 0)
    stall = raw.get("stall_count", 0)
    wraps = raw.get("dma_wrap_count", 0)
    dma_bytes = wraps * DMA_BUF_SIZE + raw.get("dma_count", 0)

    return {
        "window_cycles": cycles,
        "window_instret": instret,
        "ipc": instret / cycles,
        "gap_cycles": gap,
        "gap_fraction": gap / cycles,
        "pauses": pauses,
        "mean_gap_cycles": (gap / pauses) if pauses else 0.0,
        "dropped_packets": raw.get("dropped_packets", 0),
        "stall_cycles": stall,
        "stall_fraction": stall / cycles,
        "dma_bytes": dma_bytes,
        # Cycles the DMA sink had no free TL source ID. Non-zero means the sink
        # was genuinely backpressuring; zero means every gap came from the
        # encoder's own queues, not from trace-port congestion.
        # None (not merely 0) when the log predates the counter -- absent must
        # never be reported as "the sink never stalled".
        "dma_src_rdy_stall": raw.get("dma_src_rdy_stall_count"),
        "sink_stall_fraction": (None if "dma_src_rdy_stall_count" not in raw
                                else raw["dma_src_rdy_stall_count"] / cycles),
        "bytes_per_cycle": dma_bytes / cycles,
        "bits_per_insn": (dma_bytes * 8 / instret) if instret else 0.0,
        "lossy": raw.get("lossy", None),
        # None when the log predates the register; 0 means the encoder default.
        "resume_watermark": raw.get("resume_watermark"),
        "elapsed_ns": raw.get("elapsed_ns"),
        "invocations": raw.get("invocations", 1),
    }, None


def check(bmark, m):
    """Warnings that say when a coverage number should not be trusted."""
    warns = []
    if m["lossy"] == 0:
        warns.append("log reports lossy mode 0 -- this is a LOSSLESS run")
    elif m["lossy"] is None:
        warns.append("no 'tacit lossy mode' line; cannot confirm lossy was on")
    if m["pauses"] == 0:
        warns.append("no pauses: the sink never backpressured, so the gap "
                     "fraction does not represent a stressed encoder")
    if m["stall_fraction"] > STALL_WARN_FRACTION:
        warns.append(f"core stalled for {m['stall_fraction'] * 100:.2f}% of the "
                     "window; lossy mode should pause rather than stall")
    if m["dma_bytes"] and m["dma_bytes"] < DMA_BUF_SIZE:
        warns.append("DMA ring never wrapped; the trace fit in one buffer, so "
                     "the sink was likely not saturated")
    return [f"{bmark}: {w}" for w in warns]


def load_run(run_dir):
    """Load every per-benchmark lossy log under one results-workload directory."""
    logs = sorted(run_dir.glob("*/output/*-lossy.txt")) + \
           sorted(run_dir.glob("*/output/*.out"))
    if not logs:
        raise SystemExit(f"no */output/*-lossy.txt or *.out under {run_dir}")

    results, problems = {}, []
    for log in logs:
        bmark = (log.name[:-len("-lossy.txt")] if log.name.endswith("-lossy.txt")
                 else log.stem)
        bmark = ALIASES.get(bmark, bmark)
        metrics, err = derive(parse_lossy_log(log))
        if err:
            problems.append(f"{bmark}: {err} ({log})")
            continue
        results[bmark] = metrics
    return results, problems


def load_runs(run_dirs):
    """Average metrics per benchmark across repetitions."""
    runs, problems = [], []
    for d in run_dirs:
        r, p = load_run(d)
        runs.append(r)
        problems.extend(p)

    common = set.intersection(*(set(r) for r in runs)) if runs else set()

    # Only a conflict when the SAME benchmark is averaged across runs at different
    # watermarks. Distinct benchmarks at different watermarks in one directory are
    # a deliberate sweep (e.g. one job per depth), not a mistake.
    for b in sorted(common):
        wms = {r[b].get("resume_watermark") for r in runs if b in r}
        wms.discard(None)
        if len(wms) > 1:
            raise SystemExit(
                f"{b} appears at different resume watermarks {sorted(wms)}; "
                "averaging would mix sweep points. Analyse them separately.")
    for r in runs:
        for extra in sorted(set(r) - common):
            problems.append(f"{extra}: present in only some runs, dropped")

    keys = [k for k in next(iter(runs[0].values())) if k != "lossy"]
    averaged = {}
    for bmark in sorted(common, key=_order_key):
        averaged[bmark] = {
            k: (None if any(r[bmark][k] is None for r in runs)
                else float(np.mean([r[bmark][k] for r in runs])))
            for k in keys}
        # lossy is a flag, not a quantity: keep it only if every run agrees.
        flags = {r[bmark]["lossy"] for r in runs}
        averaged[bmark]["lossy"] = flags.pop() if len(flags) == 1 else None
    return averaged, problems


def print_table(results):
    hdr = (f"{'Benchmark':<24} {'n':>2} {'Window (Mcyc)':>14} {'IPC':>6} "
           f"{'Gap':>8} {'Pauses':>10} {'Mean gap':>10} "
           f"{'Dropped':>10} {'B/cyc':>7} {'BPI':>7}")
    print(hdr)
    print("-" * len(hdr))
    for bmark, m in results.items():
        print(f"{bmark:<24} {m['invocations']:>2.0f} {m['window_cycles'] / 1e6:>14,.1f} "
              f"{m['ipc']:>6.2f} {m['gap_fraction'] * 100:>7.3f}% "
              f"{m['pauses']:>10,.0f} {m['mean_gap_cycles']:>10,.1f} "
              f"{m['dropped_packets']:>10,.0f} {m['bytes_per_cycle']:>7.3f} "
              f"{m['bits_per_insn']:>7.2f}")


def _check_overlap(fig, ax, hline_y):
    """Warn if a value label's white box straddles the total-gap-fraction line.
    Ported from compare-overhead-ref.py, which guards the same failure."""
    fig.canvas.draw()
    r = fig.canvas.get_renderer()
    y_disp = ax.transData.transform((0, hline_y))[1]
    bad = []
    for t in ax.texts:
        bb = t.get_window_extent(renderer=r)
        if bb.y0 <= y_disp <= bb.y1:
            bad.append(t.get_text())
    if bad:
        print(f"  NOTE: {len(bad)} label(s) overlap the total-gap-fraction line "
              f"(masked by their white box): {bad}")
    else:
        print("  overlap check: no label intersects the total-gap-fraction line")


def plot(results, total_gap_frac, out_stem):
    """Single panel, styled after compare-overhead-ref.py's combined()."""
    # Drop the "_s" suffix to buy horizontal room, as combined() does.
    names = [b.replace("_s-", "\n").replace("_s", "") for b in results]
    # Parts per million, not percent: the suite spans 0.9-335 ppm, and in percent
    # four of eleven benchmarks round to 0.000 and the figure says nothing.
    vals = [results[b]["gap_fraction"] * 1e6 for b in results]
    x = np.arange(len(names))

    with plt.rc_context(RC):
        fig, ax = plt.subplots(figsize=(DOUBLE_COL, FIG_H))
        ax.bar(x, vals, color=BLUE, width=0.72, zorder=2)
        ax.axhline(y=total_gap_frac * 1e6, color=RED, linestyle="--", linewidth=1.1,
                   zorder=1, label=f"total {total_gap_frac * 1e6:.0f} ppm "
                                   f"({(1 - total_gap_frac) * 100:.3f}% coverage)")
        ax.legend(frameon=False, loc="upper right", handlelength=2.0)
        for xi, v in zip(x, vals):
            ax.annotate(f"{v:.3g}", (xi, v), textcoords="offset points",
                        xytext=(0, 1.5), ha="center", va="bottom",
                        fontsize=8.5, zorder=6,
                        bbox=dict(facecolor="white", edgecolor="none",
                                  pad=0.6, alpha=1.0))
        ax.axhline(y=0, color="black", linewidth=0.5, zorder=3)
        ax.margins(y=0.22)
        ax.set_ylabel("Gap cycles per million traced cycles")
        ax.spines[["top", "right"]].set_visible(False)
        ax.set_xticks(x)
        ax.set_xticklabels(names, rotation=45, ha="right")
        for ext in ("pdf", "png"):
            fig.savefig(f"{out_stem}.{ext}", bbox_inches="tight",
                        pad_inches=0.03, dpi=300)
        _check_overlap(fig, ax, total_gap_frac * 1e6)
        plt.close(fig)
    print(f"Wrote {out_stem}.png and {out_stem}.pdf")


def write_csv(results, path):
    cols = ["window_cycles", "window_instret", "ipc", "gap_cycles",
            "gap_fraction", "pauses", "mean_gap_cycles", "dropped_packets",
            "stall_cycles", "stall_fraction", "dma_bytes", "bytes_per_cycle",
            "bits_per_insn", "dma_src_rdy_stall", "sink_stall_fraction",
            "invocations"]
    with open(path, "w") as f:
        f.write("benchmark," + ",".join(cols) + "\n")
        for bmark, m in results.items():
            f.write(bmark + "," + ",".join(f"{m[c]}" for c in cols) + "\n")
    print(f"Wrote {path}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Report lossy-mode trace coverage from TACIT counter logs.")
    ap.add_argument("results", nargs="+", type=pathlib.Path,
                    help="results-workload directories (repetitions are averaged)")
    ap.add_argument("-o", "--out-stem", default="lossy-coverage-results",
                    help="stem for the .png/.pdf/.csv outputs")
    args = ap.parse_args()

    results, problems = load_runs(args.results)
    if not results:
        raise SystemExit("no usable lossy logs found")

    print_table(results)

    # A ratio of sums, not a mean of ratios: benchmarks contribute unequal
    # numbers of cycles, and this is the only form that stays correct when the
    # windows differ. Geomean is wrong for a part-over-whole -- it is pulled to
    # the smallest values (0.0004% here, 11x low) and collapses to 0 on any
    # benchmark with no gaps at all.
    total_gap = sum(m["gap_cycles"] for m in results.values())
    total_cycles = sum(m["window_cycles"] for m in results.values())
    total_gap_frac = total_gap / total_cycles
    per_bmark = [m["gap_fraction"] for m in results.values()]
    print(f"\nTotal gap fraction:    {total_gap_frac * 100:.3f}%  "
          f"(coverage {100 - total_gap_frac * 100:.3f}%)")
    print(f"Mean of per-benchmark: {np.mean(per_bmark) * 100:.3f}%  "
          f"(max {np.max(per_bmark) * 100:.3f}%)")

    have = {b: m for b, m in results.items() if m["dma_src_rdy_stall"] is not None}
    missing = [b for b in results if b not in have]
    if have:
        stalled = {b: m for b, m in have.items() if m["dma_src_rdy_stall"] > 0}
        if stalled:
            print("\nDMA sink stalled (no free TL source ID):")
            for b, m in stalled.items():
                print(f"  {b:<24} {m['dma_src_rdy_stall']:>14,.0f} cycles  "
                      f"({m['sink_stall_fraction'] * 100:.3f}% of window)")
        else:
            print("\nDMA sink never stalled in any measured run: every gap came "
                  "from the encoder's own queues, not from sink backpressure.")
    if missing:
        print(f"\nDMA sink stall not recorded for {len(missing)} benchmark(s) "
              "(log predates the counter); sink pressure is UNKNOWN for them, "
              "not zero.")

    warnings = [w for b, m in results.items() for w in check(b, m)]
    if problems or warnings:
        # stdout is block-buffered when piped; flush so the warnings do not
        # overtake the table they refer to.
        sys.stdout.flush()
        print("\nWarnings:", file=sys.stderr)
        for w in problems + warnings:
            print(f"  ! {w}", file=sys.stderr)

    write_csv(results, f"{args.out_stem}.csv")
    plot(results, total_gap_frac, args.out_stem)
