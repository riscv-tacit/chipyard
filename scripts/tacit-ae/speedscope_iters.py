#!/usr/bin/env python3
"""Cut single iterations out of a decoded speedscope profile, one file each.

The decoder's profile has one root frame per address space, named by asid (u_<asid>),
and a spawn-and-reap benchmark alternates them: the launcher's root, then one child's
root, then the launcher again. One iteration is therefore one launcher root plus the
child root that follows it -- the spawn and the reap live in the launcher's span, the
child's own execution in the other, and the tail latency can fall in either. Ranking
iterations by that combined span reproduces the latency the benchmark itself reports.

    speedscope_iters.py PROFILE --launcher u_114 --out DIR [--slowest 5] [--median 1]

Writes DIR/<stem>.iter<N>.<tag>.speedscope.json, one iteration per file, each a valid
self-contained profile: events of exactly those two roots, frames renumbered, and a
name carrying the rank and duration. Nothing here depends on marker functions or on
the decoder: the roots are the iteration boundaries by construction.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def root_spans(events):
    """(first_event_index, last_event_index, frame) of every depth-0 span, in order."""
    spans, depth, start = [], 0, None
    for i, e in enumerate(events):
        if e["type"] == "O":
            if depth == 0:
                start = i
            depth += 1
        else:
            depth -= 1
            if depth == 0:
                spans.append((start, i, events[start]["frame"]))
    if depth != 0:
        sys.exit(f"profile does not close its last frame (depth {depth} at end)")
    return spans


def iterations(events, frames, launcher):
    """Pair each launcher root with the child root that follows it."""
    spans = root_spans(events)
    its = []
    for k in range(len(spans) - 1):
        a, b = spans[k], spans[k + 1]
        if frames[a[2]]["name"] == launcher and frames[b[2]]["name"] != launcher:
            first, last = a[0], b[1]
            its.append({"n": len(its), "child": frames[b[2]]["name"],
                        "first": first, "last": last,
                        "cycles": events[last]["at"] - events[first]["at"]})
    return its


def write_one(d, it, label, path):
    ev = d["profiles"][0]["events"][it["first"]:it["last"] + 1]
    used = sorted({e["frame"] for e in ev})
    remap = {f: i for i, f in enumerate(used)}
    out = {
        "$schema": d["$schema"],
        "shared": {"frames": [d["shared"]["frames"][f] for f in used]},
        "profiles": [{
            "type": "evented", "unit": "none",
            "name": f"{label}: iteration {it['n']} ({it['child']}), {it['cycles']:,} cycles",
            "startValue": ev[0]["at"], "endValue": ev[-1]["at"],
            "events": [{"type": e["type"], "frame": remap[e["frame"]], "at": e["at"]} for e in ev],
        }],
        "activeProfileIndex": 0,
        "exporter": "tacit-ae speedscope_iters",
    }
    path.write_text(json.dumps(out, separators=(",", ":")))
    return len(ev), len(used)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("profile", type=Path)
    ap.add_argument("--launcher", required=True, help="root frame name of the launcher, e.g. u_114")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--slowest", type=int, default=5, help="how many of the slowest iterations")
    ap.add_argument("--median", type=int, default=1, help="how many iterations around the median")
    ap.add_argument("--stem", default=None, help="output file stem (default: the profile's)")
    args = ap.parse_args(argv)

    d = json.loads(args.profile.read_bytes())
    frames, events = d["shared"]["frames"], d["profiles"][0]["events"]
    its = iterations(events, frames, args.launcher)
    if not its:
        sys.exit(f"no launcher/child root pairs for launcher {args.launcher!r}; roots seen: "
                 + ", ".join(sorted({frames[s[2]]['name'] for s in root_spans(events)})[:8]))
    ranked = sorted(its, key=lambda it: -it["cycles"])
    picks = [(it, f"slowest{r + 1}") for r, it in enumerate(ranked[:args.slowest])]
    mid = len(ranked) // 2
    lo = max(0, mid - args.median // 2)
    picks += [(it, "median") for it in ranked[lo:lo + args.median]]

    args.out.mkdir(parents=True, exist_ok=True)
    stem = args.stem or args.profile.name.replace(".speedscope.json", "")
    print(f"{len(its)} iterations; cycles: min {ranked[-1]['cycles']:,} median "
          f"{ranked[mid]['cycles']:,} max {ranked[0]['cycles']:,}")
    for it, tag in picks:
        p = args.out / f"{stem}.iter{it['n']:03d}.{tag}.speedscope.json"
        nev, nfr = write_one(d, it, tag, p)
        print(f"  {tag:9} iteration {it['n']:3d} {it['child']:8} {it['cycles']:>10,} cycles "
              f"{nev:>7,} events {nfr:>5,} frames  {p.stat().st_size / 1e6:5.1f} MB  {p.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
