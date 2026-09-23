#!/usr/bin/env python3
"""Summarize RESULT lines from adversarial-tacit-bmarks uartlogs.

Usage: parse_results.py <uartlog-or-results-dir>...
For each bench: median untraced/traced cycles, slowdown, encoder stall
fraction, trace bytes per retired instruction and per traced cycle, and the
DMA sink's source-ready stall cycles.
"""
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path

LINE = re.compile(r"RESULT (.*)")


def parse_file(path):
    rows = []
    for line in Path(path).read_text(errors="replace").splitlines():
        m = LINE.search(line)
        if not m:
            continue
        kv = dict(tok.split("=", 1) for tok in m.group(1).split())
        for k, v in list(kv.items()):
            if k not in ("bench", "mode"):
                kv[k] = int(v)
        kv.setdefault("gap", 0)
        kv.setdefault("dropped", 0)
        rows.append(kv)
    return rows


def med(xs):
    return statistics.median(xs) if xs else float("nan")


def summarize(rows):
    by = defaultdict(lambda: defaultdict(list))
    order = []
    for r in rows:
        if r["bench"] not in order:
            order.append(r["bench"])
        by[r["bench"]][r["mode"]].append(r)
    hdr = ("bench", "untraced_cyc", "traced_cyc", "slowdown", "stall_frac",
           "ipc_untraced", "B/insn", "B/cyc", "srcstall_frac", "gap_frac", "dropped")
    print("{:<18} {:>13} {:>13} {:>9} {:>10} {:>12} {:>7} {:>6} {:>13} {:>8} {:>8}".format(*hdr))
    for b in order:
        u = by[b].get("untraced", [])
        t = by[b].get("traced", [])
        if not u or not t:
            continue
        uc = med([r["cycles"] for r in u])
        tc = med([r["cycles"] for r in t])
        ti = med([r["instret"] for r in t])
        st = med([r["stall"] for r in t])
        by_ = med([r["bytes"] for r in t])
        ss = med([r["srcstall"] for r in t])
        ui = med([r["instret"] for r in u])
        gp = med([r["gap"] for r in t])
        dr = med([r["dropped"] for r in t])
        print("{:<18} {:>13.0f} {:>13.0f} {:>9.3f} {:>10.3f} {:>12.2f} {:>7.3f} {:>6.3f} {:>13.3f} {:>8.3f} {:>8.0f}".format(
            b, uc, tc, tc / uc, st / tc, ui / uc, by_ / ti, by_ / tc, ss / tc, gp / tc, dr))


def main(args):
    rows = []
    for a in args:
        p = Path(a)
        files = [p] if p.is_file() else sorted(p.rglob("uartlog"))
        for f in files:
            rows.extend(parse_file(f))
    if not rows:
        sys.exit("no RESULT lines found")
    summarize(rows)


if __name__ == "__main__":
    main(sys.argv[1:] or ["."])
