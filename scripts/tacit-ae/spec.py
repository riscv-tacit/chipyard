"""What the SPEC CPU2017 intspeed experiments share: the benchmark table, the licensed
SPEC installation they build from, and the counter logs the run scripts leave behind.

SPEC is licensed software: the interpreter overlays are compiled from the reviewer's
own installation, pointed to by $SPEC_DIR, by speckle/gen_binaries.sh during the image
step (FireMarshal host-init `build-intspeed.sh <test|train|ref>`). Nothing SPEC-derived
is in the repository.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from shell import die, ok, step


@dataclass(frozen=True)
class Bench:
    name: str        # job / results label, e.g. 657.xz_s-cld
    dir: str         # directory in the overlay and in the guest's /root
    elf: str         # the benchmark binary inside that directory
    args: str = ""   # extra intspeed.sh arguments (xz has two workloads)


BENCHMARKS = (
    Bench("600.perlbench_s", "600.perlbench_s", "perlbench_s_base.riscv-64"),
    Bench("602.gcc_s", "602.gcc_s", "sgcc_base.riscv-64"),
    Bench("605.mcf_s", "605.mcf_s", "mcf_s_base.riscv-64"),
    Bench("620.omnetpp_s", "620.omnetpp_s", "omnetpp_s_base.riscv-64"),
    Bench("623.xalancbmk_s", "623.xalancbmk_s", "xalancbmk_s_base.riscv-64"),
    Bench("625.x264_s", "625.x264_s", "x264_s_base.riscv-64"),
    Bench("631.deepsjeng_s", "631.deepsjeng_s", "deepsjeng_s_base.riscv-64"),
    Bench("641.leela_s", "641.leela_s", "leela_s_base.riscv-64"),
    Bench("648.exchange2_s", "648.exchange2_s", "exchange2_s_base.riscv-64"),
    Bench("657.xz_s-cpu2006docs", "657.xz_s", "xz_s_base.riscv-64", " --workload 0"),
    Bench("657.xz_s-cld", "657.xz_s", "xz_s_base.riscv-64", " --workload 1"),
)
GUEST_ROOT = ""   # FireMarshal copies the overlay onto the rootfs root: /605.mcf_s/..., and
                  # firemarshal.sh runs each job command from / (so "./intspeed.sh" resolves)


DEFAULT_SPEC_DIR = Path.home() / "spec2017" / "cpu2017"   # where setup-ae.sh installs the ISO


def spec_dir() -> Path:
    """$SPEC_DIR if set, else the place setup-ae.sh installs to."""
    return Path(os.environ.get("SPEC_DIR") or DEFAULT_SPEC_DIR).expanduser()


def check_spec_dir() -> None:
    """The image step compiles SPEC through the installation; fail before anything expensive.
    speckle's gen_binaries.sh reads $SPEC_DIR, so the resolved path is exported for it."""
    step("SPEC CPU2017 installation (SPEC_DIR)")
    d = spec_dir()
    if not (d / "shrc").exists():
        die(f"{d} is not a SPEC CPU2017 installation (no shrc) -- run scripts/tacit-ae/setup-ae.sh --spec "
            f"to install the artifact's ISO there, or export SPEC_DIR=/path/to/your/cpu2017")
    os.environ["SPEC_DIR"] = str(d)
    ok(str(d))


# ---- counter logs -------------------------------------------------------------------
# trace-submit / trace-stop print one "key: value" line per counter into the job's
# output/*.out, once per INVOCATION of the benchmark binary (perlbench, gcc and x264 run
# it two or three times per input set), so a job's total is an aggregate:
#   * cycles / instret / elapsed_ns are per invocation           -> summed
#   * the encoder's counters (stall, gap, dropped, pauses) are window-scoped on the
#     2026-09-07 RTL: cleared when tracing is enabled, frozen when it is disabled, so
#     each printed value covers one invocation                    -> summed
#     (on the older bitstreams they accumulated from reset, and the last value was the
#     total; software/spec2017/compare-overhead-ref.py took LAST for that reason)
#   * the DMA sink's counters (dma count, wrap count, source-ready stalls) still
#     accumulate from reset                                       -> the last value wins
# Reading only the final invocation, as the first compare-overhead.py did, undercounts
# perlbench/gcc/x264 by 2-5x at ref and inflates their bits/instruction by the same.
_LINE = re.compile(r"^(stall count|dma count|dma wrap count|dma src rdy stall count|"
                   r"gap cycles|dropped|pauses|elapsed_ns|cycles|instret|window_start_cycle|"
                   r"window_end_cycle|window_start_instret|window_end_instret)\s*:\s*(\d+)", re.M)
_GAPS = re.compile(r"^gap cycles: (\d+) dropped: (\d+) pauses: (\d+)", re.M)
SUMMED = {"cycles", "instret", "elapsed_ns", "stall count", "gap cycles", "dropped", "pauses"}
DMA_BUF_BYTES = 4 * 1024 * 1024   # the sink's ring; a wrap is one buffer of traffic


def counters(job_dir: Path) -> dict[str, int]:
    """Aggregate every counter across the job's output/*.out files (see the note above)."""
    agg: dict[str, int] = {}
    for f in sorted((job_dir / "output").glob("*.out")):
        text = f.read_text(errors="replace").replace("\r", "")
        seen: dict[str, int] = {}
        for k, v in _LINE.findall(text):
            seen[k] = seen.get(k, 0) + int(v) if k in SUMMED else int(v)
        for g, d, p in _GAPS.findall(text):
            for k, v in (("gap cycles", g), ("dropped", d), ("pauses", p)):
                seen[k] = seen.get(k, 0) + int(v)
        for k, v in seen.items():
            agg[k] = agg.get(k, 0) + v if k in SUMMED else v
    return agg


def dma_bytes(c: dict[str, int]) -> int:
    return c.get("dma wrap count", 0) * DMA_BUF_BYTES + c.get("dma count", 0)
