"""The arms of the dispatch-fusion experiment.

This table is the experiment's identity: what is compared against what, and what
single source change distinguishes them. In the shell driver the same facts were
spread over seven `case` statements and three Python dicts that could -- and did --
drift apart. Here each arm is declared once, and everything derivable is derived.

The baseline is stock Lua 5.4.7, unmodified. Every other arm is that source with
one `vmbreak;` replaced by a guard, by hand, and nothing else: the interpreter
tests the next opcode against a constant and jumps straight to its handler when it
matches, instead of going through the indirect dispatch table.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import paths

# The interpreter trees live with the experiment, not in the shared path module:
# software/lua-dispatch belongs to this study and nothing else uses it.
EXPERIMENT = "lua_fusion"
HERE = Path(__file__).resolve().parent
ANALYSIS = HERE / "analysis"      # this study's plotters and flow
CONFIGS = HERE / "configs"        # its optabs and decoder receiver templates
OUT = paths.OUT_ROOT / EXPERIMENT # captures included: everything lands under out/

LUA_DISPATCH = paths.SOFTWARE / "lua-dispatch"
WORKLOADS = LUA_DISPATCH / "workloads"
BENCH = "mandelbrot"
CFLAGS = "-O2 -g -static -fno-crossjumping"   # -fno-crossjumping keeps each handler's
                                              # dispatch tail its own jr site


@dataclass(frozen=True)
class Variant:
    key: str                 # what --arms takes
    tree: str                # source tree under software/lua-dispatch
    workload: str            # FireMarshal workload (also the results-dir suffix)
    tag: str                 # this arm's job prefix inside the workload
    config: str              # FireSim runtime config in sims/firesim/deploy
    optab: str               # opcode -> handler address, for THIS build's addresses
    label: str               # short, for figures
    table_label: str         # long, for the results table
    edit: str                # the provenance claim: what changed from baseline
    guards: tuple = ()       # opcodes given a guard; () is the baseline
    app: str = "lua"         # binary name inside the overlay
    workload_tag: str = ""   # chores belong to the WORKLOAD, not the arm; see below

    # ---- everything below is derived, so it cannot disagree with the above ----
    @property
    def job(self) -> str:
        """FireSim job directory for the traced run, inside the results directory."""
        return f"{self.workload}-{self.tag}-{BENCH}-traced"

    @property
    def chores(self) -> str:
        """The companion job. Two arms can share one workload -- the layout control
        rides along with the arm it controls for -- so chores keys off the workload's
        own tag, not the arm's."""
        return f"{self.workload}-{self.workload_tag or self.tag}-chores"

    @property
    def tree_dir(self) -> Path:
        return LUA_DISPATCH / self.tree

    @property
    def binary(self) -> Path:
        return self.tree_dir / "src" / "lua"

    @property
    def workload_json(self) -> Path:
        return WORKLOADS / f"{self.workload}.json"

    @property
    def overlay(self) -> Path:
        return WORKLOADS / self.workload / "overlay" / "root" / "lua-dispatch"

    @property
    def guest_path(self) -> str:
        return f"/root/lua-dispatch/{self.app}"

    @property
    def image(self) -> Path:
        return paths.FM / "images" / "firechip" / self.job / f"{self.job}.img"

    @property
    def arm_out(self) -> Path:
        return OUT / self.key

    @property
    def bundle_dir(self) -> Path:
        """The capture lives with the results it produced, not in the decoder's tree,
        so one output directory holds the trace, the binaries that explain it, and
        every number derived from them."""
        return OUT / self.key / "bundle"

    @property
    def optab_path(self) -> Path:
        return CONFIGS / self.optab

    @property
    def uartlog(self) -> Path:
        return self.bundle_dir / "uartlog"

    def __str__(self) -> str:
        return f"{self.key} ({self.tree})"


# ---------------------------------------------------------------------- the arms
BASE = Variant(
    key="base", tree="lua-5.4.7", workload="lua-fuse-base-mb", tag="base",
    config="config_runtime_lua_base.yaml", optab="optab_base.json",
    label="baseline", table_label="unfused baseline",
    edit="none -- stock lua 5.4.7, byte-identical in every loaded section",
)

MULMUL = Variant(
    key="mulmul", tree="lua-fuse-mulmul", workload="lua-fuse-mulmul", tag="mulmul",
    config="config_runtime_lua_mulmul.yaml", optab="optab_mulmul.json",
    label="+MUL→MUL", table_label="1 guard  MUL->MUL",
    edit="lvm.c:1478  vmbreak -> vmbreak_fused(OP_MUL)",
    guards=("OP_MUL",),
)

MMADD = Variant(
    key="mmadd", tree="lua-fuse-mulmul-muladd", workload="lua-fuse-mmadd", tag="mmadd",
    config="config_runtime_lua_mmadd.yaml", optab="optab_mmadd.json",
    label="+MUL→ADD", table_label="2 guards MUL->MUL,MUL->ADD",
    edit="lvm.c:1478  vmbreak -> vmbreak_fused2(OP_MUL, OP_ADD)",
    guards=("OP_MUL", "OP_ADD"),
)

# The span-vs-entry test: a guard that the span profile ranks 4th but the
# entry-block profile says is already predicted. Its layout control rides in the
# same workload, which is why workload_tag is pinned below.
LEIMUL = Variant(
    key="leimul", tree="lua-fuse-leimul", workload="lua-fuse-leimul", tag="leimul",
    config="config_runtime_lua_leimul.yaml", optab="optab_leimul.json",
    label="+LEI→MUL", table_label="1 guard  LEI->MUL (span pick)",
    edit="the LEI dispatch site -> vmbreak_fused(OP_MUL)",
    guards=("OP_MUL",), app="lua-leimul",
)

LEIMULCTL = Variant(
    key="leimulctl", tree="lua-leimul-gt127", workload="lua-fuse-leimul", tag="leimul-gt127",
    config="config_runtime_lua_leimul.yaml", optab="optab_leimul.json",
    label="LEI→MUL control", table_label="LEI->MUL layout control",
    edit="same guard with GUARD_TARGET=127, which never matches: layout without the guard",
    guards=(), app="lua-leimul-gt127", workload_tag="leimul",
)

ALL = {v.key: v for v in (BASE, MULMUL, MMADD, LEIMUL, LEIMULCTL)}
DEFAULT = ("base", "mulmul", "mmadd")


def get(key: str) -> Variant:
    if key not in ALL:
        raise KeyError(f"unknown arm '{key}'; known arms: {', '.join(ALL)}")
    return ALL[key]


def select(keys) -> list[Variant]:
    """Resolve --arms into variants, preserving the caller's order.

    Order matters: the report takes the first as the baseline everything else is
    measured against.
    """
    return [get(k) for k in keys]


def outdir(*parts) -> Path:
    """A directory under this experiment's output root, created on demand."""
    d = OUT.joinpath(*parts)
    d.mkdir(parents=True, exist_ok=True)
    return d
