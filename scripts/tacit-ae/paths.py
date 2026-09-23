"""Where everything lives.

Derived from this file's own location, so a clone anywhere works: this module sits
at <chipyard>/scripts/tacit-ae/paths.py, hence CY is two levels up. $CHIPYARD
overrides that for the odd case of driving a tree from outside it.

Only locations shared by *every* AE experiment belong here. An experiment's own
material -- its source trees, its FireMarshal workload definitions, its configs --
is that experiment's business and stays in its own module. `software/lua-dispatch`
is a good example of what does NOT go here.
"""
from __future__ import annotations

import os
from pathlib import Path

CY = Path(os.environ["CHIPYARD"]).resolve() if os.environ.get("CHIPYARD") \
     else Path(__file__).resolve().parents[2]

# A wrong CY produces confusing failures three stages later, so say so now. env.sh
# is the cheapest unambiguous marker of a chipyard root.
if not (CY / "env.sh").exists():
    raise RuntimeError(
        f"{CY} does not look like a chipyard tree (no env.sh).\n"
        f"    Either run these helpers from within the tree, or set $CHIPYARD."
    )

# ---------------------------------------------------------------- chipyard roots
SOFTWARE = CY / "software"
SIMS     = CY / "sims"
FM       = SOFTWARE / "firemarshal"
FS       = SIMS / "firesim"
TD       = SOFTWARE / "tacit_decoder"

# ----------------------------------------------------------------- the decoder
# Treated as a given binary with a published output format. Its analysis scripts
# and configs are NOT used from here: each experiment carries its own, so an AE
# tree contains everything needed to interpret its own results.
DECODER  = TD / "target" / "release" / "tacit-decoder"

# ------------------------------------------------- toolchain from the conda env
CONDA    = CY / ".conda-env"
PY       = CONDA / "bin" / "python3"
OBJDUMP  = CONDA / "riscv-tools" / "bin" / "riscv64-unknown-elf-objdump"

# Experiment output. $TACIT_AE_OUT relocates the lot, e.g. onto a bigger filesystem.
OUT_ROOT = Path(os.environ["TACIT_AE_OUT"]).resolve() if os.environ.get("TACIT_AE_OUT") \
           else Path(__file__).resolve().parent / "out"


class MissingPath(RuntimeError):
    """A path an experiment needs is absent, with the command that would create it."""


# What to tell someone when a path is missing. Anything not listed gets a bare
# "does not exist", which is still better than a stack trace from three calls deep.
_FIX = {
    "FM":      "git submodule update --init software/firemarshal",
    "FS":      "git submodule update --init sims/firesim",
    "TD":      "git submodule update --init software/tacit_decoder",
    "DECODER": "cd software/tacit_decoder && cargo build --release",
    "PY":      "the chipyard conda env is missing -- run ./build-setup.sh",
    "OBJDUMP": "the riscv toolchain is missing -- run ./build-setup.sh",
}


def require(*names: str) -> None:
    """Assert that the named module-level paths exist, reporting all failures at once.

    Checking up front beats failing mid-pipeline: a reviewer who is missing a
    submodule should learn that before an eight-minute FPGA run, not after.
    """
    missing = []
    for n in names:
        p = globals().get(n)
        if p is None:
            raise KeyError(f"no such path: {n}")
        if not Path(p).exists():
            missing.append(f"  {n:9} {p}" + (f"\n    fix: {_FIX[n]}" if n in _FIX else ""))
    if missing:
        raise MissingPath("required paths are missing:\n" + "\n".join(missing))


def out_dir(experiment: str, *parts: str) -> Path:
    """Output directory for one experiment, created on demand.

    Namespacing by experiment is what keeps two of them from overwriting each
    other's figures when a reviewer runs both.
    """
    d = OUT_ROOT.joinpath(experiment, *parts)
    d.mkdir(parents=True, exist_ok=True)
    return d


def describe() -> str:
    """One line per location -- what `--list` should print when something looks wrong."""
    names = ("CY", "SOFTWARE", "FM", "FS", "TD", "DECODER", "PY", "OBJDUMP", "OUT_ROOT")
    return "\n".join(f"  {n:9} {globals()[n]}"
                     f"{'' if Path(globals()[n]).exists() else '   (MISSING)'}"
                     for n in names)


if __name__ == "__main__":
    print(describe())
