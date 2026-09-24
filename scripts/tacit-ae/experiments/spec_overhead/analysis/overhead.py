#!/usr/bin/env python3
"""Tracing overhead of TACIT on SPEC CPU2017 intspeed: one results directory holding an
untraced and a traced+DMA job per benchmark.

    overhead.py --results RESULTS_DIR --prefix <workload>- --out OUT_DIR

For each benchmark: cycle inflation (traced/untraced elapsed - 1), CPI overhead
(CPI_traced/CPI_untraced - 1; separates the tracer's own slowdown from the work done),
DMA bandwidth into the host (MB/s over the traced run) and trace bits per retired
instruction. Writes overhead.csv and overhead.pdf/.png (three panels) and prints the table
with geometric means.
"""
from __future__ import annotations

import argparse
import csv
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))   # scripts/tacit-ae
import spec  # noqa: E402

COLUMNS = ("benchmark", "base_ns", "traced_ns", "overhead_pct", "cpi_overhead_pct",
           "stall_pct", "dma_bytes", "dma_mb_s", "instret", "bits_per_insn")


def geomean_pct(pcts: list[float]) -> float:
    return (math.exp(sum(math.log(1 + p / 100) for p in pcts) / len(pcts)) - 1) * 100 if pcts else float("nan")


def rows(results: Path, prefix: str, missing: list[str]) -> list[dict]:
    out = []
    for b in spec.BENCHMARKS:
        base = spec.counters(results / f"{prefix}{b.name}")
        traced = spec.counters(results / f"{prefix}{b.name}-traced-dma")
        if not all(k in base for k in ("elapsed_ns", "cycles", "instret")) or \
           not all(k in traced for k in ("elapsed_ns", "cycles", "instret", "dma count")):
            missing.append(b.name)
            continue
        dma = spec.dma_bytes(traced)
        cpi_b = base["cycles"] / base["instret"]
        cpi_t = traced["cycles"] / traced["instret"]
        out.append({
            "benchmark": b.name,
            "base_ns": base["elapsed_ns"],
            "traced_ns": traced["elapsed_ns"],
            "overhead_pct": (traced["elapsed_ns"] - base["elapsed_ns"]) / base["elapsed_ns"] * 100,
            "cpi_overhead_pct": (cpi_t / cpi_b - 1) * 100,
            "stall_pct": traced.get("stall count", 0) / traced["cycles"] * 100,
            "dma_bytes": dma,
            "dma_mb_s": dma / (traced["elapsed_ns"] / 1e9) / 1e6,
            "instret": traced["instret"],
            "bits_per_insn": dma * 8 / traced["instret"],
        })
    return out


def table(rs: list[dict]) -> str:
    lines = [f"  {'benchmark':24}{'base s':>9}{'traced s':>10}{'ovh %':>8}{'cpi ovh %':>11}{'stall %':>9}{'DMA MB/s':>10}{'bits/insn':>11}"]
    for r in rs:
        lines.append(f"  {r['benchmark']:24}{r['base_ns']/1e9:9.2f}{r['traced_ns']/1e9:10.2f}{r['overhead_pct']:8.2f}"
                     f"{r['cpi_overhead_pct']:11.2f}{r['stall_pct']:9.2f}{r['dma_mb_s']:10.1f}{r['bits_per_insn']:11.3f}")
    lines.append(f"  {'GEOMEAN':24}{'':>9}{'':>10}{geomean_pct([r['overhead_pct'] for r in rs]):8.2f}"
                 f"{geomean_pct([r['cpi_overhead_pct'] for r in rs]):11.2f}{'':>9}"
                 f"{sum(r['dma_mb_s'] for r in rs)/len(rs):10.1f}{sum(r['bits_per_insn'] for r in rs)/len(rs):11.3f}")
    return "\n".join(lines)


def plot(rs: list[dict], out: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    names = [r["benchmark"] for r in rs]
    x = range(len(rs))
    fig, (a1, a2, a3) = plt.subplots(3, 1, figsize=(10, 10), sharex=True)
    ovh = [r["overhead_pct"] for r in rs]
    bars = a1.bar(x, ovh, color="#4c72b0")
    for bar, v in zip(bars, ovh):
        a1.text(bar.get_x() + bar.get_width() / 2, bar.get_height(), f"{v:.1f}%", ha="center",
                va="bottom" if v >= 0 else "top", fontsize=9)
    g = geomean_pct(ovh)
    a1.axhline(g, color="red", ls="--", label=f"geomean {g:.2f}%")
    a1.axhline(0, color="black", lw=0.8)
    a1.set_ylabel("runtime overhead (%)"); a1.legend()
    bw = [r["dma_mb_s"] for r in rs]
    bars = a2.bar(x, bw, color="#55a868")
    for bar, v in zip(bars, bw):
        a2.text(bar.get_x() + bar.get_width() / 2, bar.get_height(), f"{v:.0f}", ha="center", va="bottom", fontsize=9)
    a2.set_ylabel("DMA bandwidth (MB/s)")
    bpi = [r["bits_per_insn"] for r in rs]
    bars = a3.bar(x, bpi, color="#c44e52")
    for bar, v in zip(bars, bpi):
        a3.text(bar.get_x() + bar.get_width() / 2, bar.get_height(), f"{v:.2f}", ha="center", va="bottom", fontsize=9)
    a3.set_ylabel("trace bits / instruction")
    a3.set_xticks(list(x)); a3.set_xticklabels(names, rotation=45, ha="right")
    fig.tight_layout()
    for ext in ("pdf", "png"):
        fig.savefig(out / f"overhead.{ext}", dpi=200, bbox_inches="tight")
    plt.close(fig)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--results", required=True, type=Path, help="FireSim results-workload/<run> directory")
    ap.add_argument("--prefix", required=True, help="job-directory prefix, e.g. spec17-intspeed-test-overhead-")
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()
    missing: list[str] = []
    rs = rows(args.results, args.prefix, missing)
    if not rs:
        raise SystemExit(f"no complete benchmark pairs under {args.results}")
    args.out.mkdir(parents=True, exist_ok=True)
    with open(args.out / "overhead.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS); w.writeheader()
        for r in rs:
            w.writerow({k: (f"{v:.4f}" if isinstance(v, float) else v) for k, v in r.items()})
    plot(rs, args.out)
    print(table(rs))
    if missing:
        print(f"  MISSING (no counters in both jobs): {', '.join(missing)}")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
