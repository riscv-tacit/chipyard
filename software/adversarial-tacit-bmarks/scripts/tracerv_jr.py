#!/usr/bin/env python3
"""Analyze a FireSim TracerV human-readable trace (TRACEFILE-C0) captured around
a bench_run_marked() window: an untraced pass and a traced pass of one kernel.

Token layout (TracerVBridge.scala): bits [63:0] = cycle, then one 64-bit word
per commit slot, slot 0 lowest: bit 63 = valid, bits [62:0] = iaddr. The
human-readable writer prints the 512-bit token as 8 x 16 hex chars, most
significant word first, so the LAST 16 chars are the cycle.

Usage: tracerv_jr.py TRACEFILE-C0 <objdump -d file> <kernel symbol> [max_lines]
"""
import re
import sys
from collections import Counter, defaultdict


def kernel_pcs(dump, ksym):
    """Return (lo, hi, jr_pcs, pc->mnemonic) for the kernel: from k_sym up to
    the next global k_ symbol (or harness code)."""
    lines = open(dump).read().splitlines()
    in_k = False
    lo = hi = None
    jr = set()
    mn = {}
    for l in lines:
        m = re.match(r"^([0-9a-f]+) <([^>]+)>:", l)
        if m:
            name = m.group(2)
            if name == ksym:
                in_k = True
            elif in_k and (name.startswith("k_") or name in ("run_untraced", "harness_init", "bench_run_cfg", "run_traced")):
                break
            continue
        m = re.match(r"^\s*([0-9a-f]+):\s+[0-9a-f]+\s+(\S+)", l)
        if m and in_k:
            pc = int(m.group(1), 16)
            lo = pc if lo is None else lo
            hi = pc
            mn[pc] = m.group(2)
            if m.group(2) in ("jr", "jalr", "j"):
                jr.add(pc)
    return lo, hi, jr, mn


def parse(path, max_lines=None):
    """Yield (cycle, [pc, ...]) per cycle from the human-readable format:
    one line per valid commit slot, `Cycle: <hex16> I<slot>: <hex16 pc>`."""
    pat = re.compile(r"Cycle:\s*([0-9a-f]+)\s+I(\d+):\s*([0-9a-f]+)")
    cur_cyc = None
    cur = []
    with open(path) as f:
        for n, line in enumerate(f):
            if max_lines and n >= max_lines:
                break
            m = pat.search(line)
            if not m:
                continue
            cyc = int(m.group(1), 16)
            pc = int(m.group(3), 16)
            if cyc != cur_cyc:
                if cur_cyc is not None:
                    yield cur_cyc, cur
                cur_cyc, cur = cyc, []
            cur.append(pc)
    if cur_cyc is not None:
        yield cur_cyc, cur


def segments(tokens, lo, hi):
    """Split into runs of consecutive kernel-PC commits (a run of the kernel)."""
    segs = []
    cur = []
    gap = 0
    for cyc, pcs in tokens:
        kp = [p for p in pcs if lo <= p <= hi]
        if kp:
            cur.append((cyc, kp))
            gap = 0
        else:
            gap += 1
            if cur and gap > 50:  # left the kernel for good
                segs.append(cur)
                cur = []
    if cur:
        segs.append(cur)
    return [s for s in segs if len(s) > 100]


def analyze(seg, jr, mn, label):
    first, last = seg[0][0], seg[-1][0]
    ncyc = last - first + 1
    ninsn = sum(len(k) for _, k in seg)
    jr_cycles = [c for c, k in seg for p in k if p in jr]
    deltas = [b - a for a, b in zip(jr_cycles, jr_cycles[1:])]
    hist = Counter(deltas)
    print(f"--- {label}: cycles={ncyc} insns={ninsn} ipc={ninsn/ncyc:.2f} jumps={len(jr_cycles)} "
          f"cyc/jump={ncyc/max(1,len(jr_cycles)):.3f}")
    print("    inter-jump delta histogram (delta: count):",
          ", ".join(f"{d}:{c}" for d, c in sorted(hist.items())[:14]))
    # which jr precedes long gaps, and the group shapes
    long_gap_src = Counter()
    for (a, b), c in zip(zip(jr_cycles, jr_cycles[1:]), range(len(deltas))):
        pass
    # bubbles: consecutive commit cycles with no commits in between
    commit_cycles = [c for c, _ in seg]
    bub = Counter(b - a for a, b in zip(commit_cycles, commit_cycles[1:]))
    print("    commit-cycle gap histogram:", ", ".join(f"{d}:{c}" for d, c in sorted(bub.items())[:12]))
    big = sorted(((d * c, d, c) for d, c in bub.items()), reverse=True)[:8]
    print("    gaps by total cycles (total, gap, count):", big)
    grp = Counter(len(k) for _, k in seg)
    print("    commit group sizes:", dict(sorted(grp.items())))
    # per-jump-PC mean latency to the next jump
    per_pc = defaultdict(list)
    prev = None
    for c, k in seg:
        for p in k:
            if p in jr:
                if prev is not None:
                    per_pc[prev[1]].append(c - prev[0])
                prev = (c, p)
    worst = sorted(((sum(v) / len(v), hex(p)) for p, v in per_pc.items()), reverse=True)[:4]
    print("    slowest jump PCs (mean cycles to next jump):", worst)
    # periodicity of long deltas
    long_idx = [i for i, d in enumerate(deltas) if d >= 6]
    if long_idx:
        per = Counter(b - a for a, b in zip(long_idx, long_idx[1:]))
        print(f"    long deltas (>=6): {len(long_idx)}, spacing histogram: {dict(sorted(per.items())[:8])}")


def stream_segments(tokens, lo, hi, want=2, min_len=1000):
    """Streaming version of segments(): stop after `want` kernel runs."""
    cur, gap, found = [], 0, []
    for cyc, pcs in tokens:
        kp = [p for p in pcs if lo <= p <= hi]
        if kp:
            cur.append((cyc, kp)); gap = 0
        else:
            gap += 1
            if cur and gap > 50:
                if len(cur) > min_len:
                    found.append(cur)
                    if len(found) >= want:
                        return found
                cur = []
    if len(cur) > min_len:
        found.append(cur)
    return found


def main():
    tf, dump, ksym = sys.argv[1:4]
    max_lines = int(sys.argv[4]) if len(sys.argv) > 4 else None
    lo, hi, jr, mn = kernel_pcs(dump, ksym)
    print(f"kernel {ksym}: pcs 0x{lo:x}..0x{hi:x}, {len(jr)} jump pcs")
    segs = stream_segments(parse(tf, max_lines), lo, hi)
    print(f"kernel segments found: {len(segs)} (expect 2: untraced, traced)")
    for i, s in enumerate(segs):
        analyze(s, jr, mn, ["untraced", "traced"][i] if i < 2 else f"seg{i}")


if __name__ == "__main__":
    main()
