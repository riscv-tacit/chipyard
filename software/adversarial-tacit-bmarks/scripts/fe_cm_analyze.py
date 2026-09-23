#!/usr/bin/env python3
"""Analyze BOOM debug printf log (FE/CM lines from the temporary instrumentation)
over cycle windows. Usage: fe_cm_analyze.py <log> <start1>:<end1> [<start2>:<end2> ...]
Reports, per window: fetch stage activity, redirect sources (F1/F2/F3), fetch-buffer /
FTQ back-pressure, commit group sizes, ROB-empty fraction, trace stalls, and the
sequence of s0 fetch PCs modulo a handler ring (low 12 bits) for eyeballing."""
import re
import sys
from collections import Counter

FE = re.compile(r"FE\s+(\d+) s0v=(\d) s0=([0-9a-f]+) s1v=(\d) s1=([0-9a-f]+) f1r=(\d) f1t=([0-9a-f]+) s2v=(\d) s2=([0-9a-f]+) f2r=(\d) f2t=([0-9a-f]+) f2g=(\d) f3v=(\d) f3r=(\d) f4v=(\d) fbr=(\d) ftqr=(\d)")
CM = re.compile(r"CM\s+(\d+) n=(\d+) pc=([0-9a-f]+) ts=(\d) robempty=(\d)")

def main():
    log = sys.argv[1]
    wins = [tuple(int(x) for x in w.split(":")) for w in sys.argv[2:]]
    lo = min(w[0] for w in wins); hi = max(w[1] for w in wins)
    fe = {}; cm = {}
    with open(log, errors="replace") as f:
        for line in f:
            if line.startswith("FE"):
                m = FE.match(line)
                if not m: continue
                c = int(m.group(1))
                if lo <= c <= hi: fe[c] = m.groups()
            elif line.startswith("CM"):
                m = CM.match(line)
                if not m: continue
                c = int(m.group(1))
                if lo <= c <= hi: cm[c] = m.groups()
    for (a, b) in wins:
        n = b - a + 1
        F = [fe[c] for c in range(a, b + 1) if c in fe]
        C = [cm[c] for c in range(a, b + 1) if c in cm]
        f1r = sum(int(x[5]) for x in F); f2r = sum(int(x[9]) for x in F); f3r = sum(int(x[13]) for x in F)
        f2g = sum(int(x[11]) for x in F)
        s0v = sum(int(x[1]) for x in F); s1v = sum(int(x[3]) for x in F); s2v = sum(int(x[7]) for x in F)
        f3v = sum(int(x[12]) for x in F); f4v = sum(int(x[14]) for x in F)
        fb_full = sum(1 for x in F if x[15] == "0"); ftq_full = sum(1 for x in F if x[16] == "0")
        commits = sum(int(x[1]) for x in C); ts = sum(int(x[3]) for x in C)
        robempty = sum(1 for x in C if x[4] == "1")
        grp = Counter(int(x[1]) for x in C if int(x[1]) > 0)
        print(f"=== window {a}..{b} ({n} cycles): FE lines {len(F)}, CM lines {len(C)}")
        print(f"  fetch: s0v={s0v/n:.2f} s1v={s1v/n:.2f} s2v={s2v/n:.2f} f3v={f3v/n:.2f} f4v={f4v/n:.2f} per cycle")
        print(f"  redirects: F1={f1r} F2={f2r} F3={f3r} (per cycle F1={f1r/n:.3f} F2={f2r/n:.3f} F3={f3r/n:.3f}); f2_ghist_fix={f2g}")
        print(f"  backpressure: fb_full={fb_full/n:.3f} ftq_full={ftq_full/n:.3f}")
        print(f"  commit: insns={commits} ({commits/n:.2f} IPC) groups={dict(sorted(grp.items()))} trace_stall_cycles={ts} rob_empty_cycles={robempty}")
        # short fetch-PC trace (low 12 bits) for the first 24 cycles of the window
        seq = []
        for c in range(a, min(b, a + 30) + 1):
            if c in fe:
                x = fe[c]
                seq.append(f"{int(x[2],16)&0xfff:03x}{'*' if x[5]=='1' else ''}{'^' if x[9]=='1' else ''}{'!' if x[13]=='1' else ''}")
        print("  s0 pc trace (low12; *=F1 redirect ^=F2 redirect !=F3):", " ".join(seq))
SEQ = len(sys.argv) > 2 and sys.argv[2] == "seq"
if __name__ == "__main__" and not SEQ:
    main()


# ---- decode/dispatch summary (DS lines) -------------------------------------
DS = re.compile(r"DS\s+(\d+) dv=(\d) df=(\d) disv=(\d) disf=(\d) disrdy=(\d) robrdy=(\d) ren=(\d) iqfull=(\d) bmfull=(\d) mp=(\d) robocc=\s*(\d+)")

def ds_summary(log, wins):
    lo = min(w[0] for w in wins); hi = max(w[1] for w in wins)
    ds = {}
    with open(log, errors="replace") as f:
        for line in f:
            if not line.startswith("DS"):
                continue
            m = DS.match(line)
            if not m:
                continue
            c = int(m.group(1))
            if lo <= c <= hi:
                ds[c] = tuple(int(x) for x in m.groups()[1:])
    for (a, b) in wins:
        n = b - a + 1
        D = [ds[c] for c in range(a, b + 1) if c in ds]
        if not D:
            print(f"=== DS window {a}..{b}: no lines"); continue
        k = len(D)
        dv, df, disv, disf, disrdy, robrdy, ren, iq, bm, mp, occ = zip(*D)
        stalled = [i for i in range(k) if disf[i] < disv[i] or df[i] < dv[i]]
        occ_hist = Counter((o // 16) * 16 for o in occ)
        print(f"=== DS window {a}..{b} ({n} cycles): decode-active cycles {k} ({k/n:.2f})")
        print(f"  mean dec_valid={sum(dv)/k:.2f} dec_fire={sum(df)/k:.2f} dis_valid={sum(disv)/k:.2f} dis_fire={sum(disf)/k:.2f}  (dispatch/cycle over window {sum(disf)/n:.2f})")
        print(f"  cycles with !dis_ready={sum(1-x for x in disrdy)/k:.3f} !rob_ready={sum(1-x for x in robrdy)/k:.3f} ren_stall={sum(ren)/k:.3f} iq_full={sum(iq)/k:.3f} brmask_full={sum(bm)/k:.3f} mispred_flush={sum(mp)/k:.3f}")
        print(f"  rob occupancy: mean={sum(occ)/k:.1f} min={min(occ)} max={max(occ)} hist(by16)={dict(sorted(occ_hist.items()))}")

if __name__ == "__main__" and len(sys.argv) > 2 and not SEQ:
    ds_summary(sys.argv[1], [tuple(int(x) for x in w.split(":")) for w in sys.argv[2:]])


# ---- cycle-by-cycle commit / stall sequence --------------------------------
def cm_sequence(log, start, n):
    """Print n consecutive cycles from `start`: commits that cycle (n=), trace stall (S), fetch-buffer full (F)."""
    cm = {}; fe = {}
    with open(log, errors="replace") as f:
        for line in f:
            if line.startswith("CM"):
                m = CM.match(line)
                if m and start <= int(m.group(1)) < start + n:
                    cm[int(m.group(1))] = (int(m.group(2)), m.group(4) == "1")
            elif line.startswith("FE"):
                m = FE.match(line)
                if m and start <= int(m.group(1)) < start + n:
                    fe[int(m.group(1))] = m.group(16) == "0"
    out = []
    for c in range(start, start + n):
        k, ts = cm.get(c, (0, False))
        out.append(f"{k}{'S' if ts else ''}{'F' if fe.get(c) else ''}")
    print(f"cycles {start}..{start+n-1}: per-cycle commits (S = trace stall asserted, F = fetch buffer full)")
    print("  " + " ".join(out))

if __name__ == "__main__" and len(sys.argv) > 3 and sys.argv[2] == "seq":
    cm_sequence(sys.argv[1], int(sys.argv[3]), int(sys.argv[4]))
