"""Two helpers for FireSim/FireMarshal artifacts.

Not a wrapper layer -- `marshal build`, `firesim runworkload` and the rest are
single subprocess calls that read better inline, where the order of operations is
visible. These two are here because one is cryptic and the other is repeated, and
both fail silently in shell.
"""
from __future__ import annotations

import contextlib
import fcntl
import subprocess
import tempfile
from pathlib import Path

import paths
from shell import md5


class RootfsMismatch(RuntimeError):
    """The guest image does not contain the binary we think it does."""


def dump_from_rootfs(img: Path | str, guest_path: str, dest: Path | str) -> Path:
    """Extract one file from an ext4 image without mounting it.

    debugfs reports a missing file on stderr but still exits 0, so the extracted
    file -- not the return code -- is what says whether it worked.
    """
    img, dest = Path(img), Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.unlink(missing_ok=True)
    subprocess.run(["debugfs", "-R", f"dump {guest_path} {dest}", str(img)],
                   capture_output=True, text=True)
    if not dest.exists() or dest.stat().st_size == 0:
        raise RootfsMismatch(
            f"{guest_path} is absent (or empty) in {img.name}.\n"
            f"    The overlay copy did not reach the image; rebuild with --force."
        )
    return dest


def verify_in_rootfs(img: Path | str, guest_path: str, local_file: Path | str,
                     digits: int = 12) -> str:
    """Check that `guest_path` inside ext4 image `img` is byte-identical to `local_file`.

    Returns the shared md5 prefix; raises RootfsMismatch otherwise.

    This exists because FireMarshal caches images. If an overlay copy did not take,
    or a stale image got reused, the guest boots the *previous* binary: the run
    completes, passes, and prints entirely plausible cycle counts while measuring
    the wrong thing. Comparing a guarded arm against itself that way yields a
    real-looking number, which is worse than a crash.

    debugfs extracts a single file from the image without mounting it, so this
    needs no root. It is also not something anyone reconstructs from memory, which
    is the other reason it lives in a function.
    """
    img, local_file = Path(img), Path(local_file)
    if not img.exists():
        raise RootfsMismatch(f"image does not exist: {img}")
    if not local_file.exists():
        raise RootfsMismatch(f"local file does not exist: {local_file}")

    with tempfile.TemporaryDirectory() as td:
        dumped = dump_from_rootfs(img, guest_path, Path(td) / "extracted")
        got, want = md5(dumped), md5(local_file)

    if got != want:
        raise RootfsMismatch(
            f"{guest_path} in {img.name} is not the binary that was just built.\n"
            f"    image has {got[:digits]}, expected {want[:digits]}\n"
            f"    FireMarshal served a cached image; rebuild the image stage with --force."
        )
    return want[:digits]


def newest_results_dir(suffix: str, root: Path | None = None) -> Path:
    """Most recent FireSim run directory whose name ends in `suffix`.

    FireSim writes each run to results-workload/<timestamp>-<workload>/, with the
    per-job output one level below:

        2026-09-03--23-37-18-lua-fuse-mmadd/
          lua-fuse-mmadd-mmadd-chores/
          lua-fuse-mmadd-mmadd-mandelbrot-traced/uartlog

    so locating a run means matching the workload as a *suffix* -- the timestamp is
    the prefix. In shell this is `ls -dt …/*name | head -1`, which has three ways to
    mislead: the glob reads backwards from what you expect, an unmatched glob
    silently becomes a literal string that flows on as a bogus path, and -t sorts by
    mtime rather than by the timestamp in the name. Raising here converts all three
    into one sentence naming what was looked for.
    """
    root = Path(root) if root else paths.FS / "deploy" / "results-workload"
    if not root.is_dir():
        raise FileNotFoundError(f"no FireSim results directory at {root}")
    runs = [p for p in root.glob(f"*{suffix}") if p.is_dir()]
    if not runs:
        raise FileNotFoundError(
            f"no FireSim run matching *{suffix} in {root}\n"
            f"    Has the run stage completed for this arm? Existing runs:\n" +
            "\n".join(f"      {p.name}" for p in
                      sorted(root.iterdir(), key=lambda p: p.stat().st_mtime,
                             reverse=True)[:5])
        )
    # mtime, not name: a re-run can touch an older directory.
    return max(runs, key=lambda p: p.stat().st_mtime)


if __name__ == "__main__":
    import sys
    print(newest_results_dir(sys.argv[1] if len(sys.argv) > 1 else "lua-fuse-mmadd"))


@contextlib.contextmanager
def exclusive(name: str):
    """Serialise a host-side manager step across experiments running at the same time.

    `firesim infrasetup` repacks the driver bundle for the hardware config into one shared
    file under sims/firesim and copies it to the run hosts; six experiments on the same
    bitstream doing that at once copied each other's half-written tarballs (tar exit 2 on
    every host, 2026-09-24). The lock lives under out/, so every experiment on this host
    contends for the same file, and it is released when the block ends or the process dies.
    """
    lock = paths.OUT_ROOT / f".{name}.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
