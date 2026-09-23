"""Running commands, and saying what happened.

The console protocol is one line per action, written in two halves so a long
command can print its result when it finishes:

    step("build riscv interpreter")      ->  "   build riscv interpreter        "
    ok("a1b2c3de")                       ->  "ok  (a1b2c3de)  2m04s"

step() starts a clock, so ok()/skip()/warn() report elapsed time without the
caller threading a timer through. Anything under a second is not worth printing
and is omitted.

Command output never goes to the terminal; it goes to a per-stage log, and a
failure prints the tail of that log. A wall of make output would bury the one
line that says what broke.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Sequence

WIDTH = 46          # column at which a step's result is printed
TAIL_LINES = 12     # how much of a failed log to show inline

_use_colour = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
_BOLD, _RESET = ("\033[1m", "\033[0m") if _use_colour else ("", "")
_started: float | None = None


class StageFailed(RuntimeError):
    """A command exited non-zero. Carries the log so the driver can report it."""

    def __init__(self, cmd: str, returncode: int, log: Path | None):
        super().__init__(f"{cmd} exited {returncode}")
        self.cmd, self.returncode, self.log = cmd, returncode, log


# ---------------------------------------------------------------------- console
def say(title: str) -> None:
    print(f"\n{_BOLD}== {title}{_RESET}", flush=True)


def step(text: str) -> None:
    global _started
    _started = time.monotonic()
    print(f"   {text:<{WIDTH}}", end="", flush=True)


def ok(detail: str = "") -> None:
    print(f"ok{f'  ({detail})' if detail else ''}{_elapsed()}", flush=True)


def skip(reason: str) -> None:
    print(f"skip  ({reason})", flush=True)


def warn(reason: str) -> None:
    """A step failed in a way that does not invalidate the run -- a figure, say."""
    print(f"warn  ({reason}){_elapsed()}", flush=True)


def die(exc: StageFailed | str) -> "NoReturn":  # type: ignore[valid-type]
    print("FAILED", flush=True)
    log = getattr(exc, "log", None)
    if log and Path(log).exists():
        print(f"   see {log}", file=sys.stderr)
        for line in tail(log):
            print(f"   | {line}", file=sys.stderr)
    else:
        print(f"   {exc}", file=sys.stderr)
    raise SystemExit(1)


def _elapsed() -> str:
    if _started is None:
        return ""
    s = time.monotonic() - _started
    return f"  {duration(s)}" if s >= 1.0 else ""


def duration(s: float) -> str:
    if s < 60:
        return f"{s:.1f}s"
    if s < 3600:
        return f"{int(s // 60)}m{int(s % 60):02d}s"
    return f"{int(s // 3600)}h{int(s % 3600 // 60):02d}m"


def tail(log: Path | str, n: int = TAIL_LINES) -> list[str]:
    """Last n non-empty lines of a log, for a failure message."""
    try:
        lines = Path(log).read_bytes().decode("utf8", "replace").splitlines()
    except OSError:
        return []
    return [ln.rstrip() for ln in lines if ln.strip()][-n:]


# ---------------------------------------------------------------------- running
def sh(cmd: Sequence[str | Path], log: Path | str | None = None,
       cwd: Path | str | None = None, env: dict | None = None,
       check: bool = True) -> int:
    """Run a command, appending its output to `log`.

    cwd is an argument rather than a surrounding `cd` because several of these
    tools are position-sensitive -- marshal wants the FireMarshal root, the
    decoder wants the bundle directory -- and an argument cannot be forgotten
    the way a stray `cd` can.

    Logs are appended, so a stage that runs several commands leaves one readable
    transcript; each is preceded by the command line that produced it.
    """
    argv = [str(c) for c in cmd]
    if log is None:
        p = subprocess.run(argv, cwd=cwd, env=env)
    else:
        Path(log).parent.mkdir(parents=True, exist_ok=True)
        with open(log, "ab") as f:
            f.write(f"\n$ {' '.join(argv)}\n".encode())
            f.flush()
            p = subprocess.run(argv, cwd=cwd, env=env, stdout=f, stderr=subprocess.STDOUT)
    if check and p.returncode:
        raise StageFailed(argv[0], p.returncode, Path(log) if log else None)
    return p.returncode


def out(cmd: Sequence[str | Path], cwd: Path | str | None = None,
        check: bool = True) -> str:
    """Run a command and return its stdout, stripped. For md5s, globs, versions."""
    argv = [str(c) for c in cmd]
    p = subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
    if check and p.returncode:
        raise StageFailed(argv[0], p.returncode, None)
    return p.stdout.strip()


def have(program: str) -> bool:
    """Is this on PATH? Used by preflight, which runs before anything expensive."""
    from shutil import which
    return which(program) is not None


def md5(path: Path | str, n: int = 32) -> str:
    """First n hex digits of a file's md5 -- the identity check used throughout."""
    import hashlib
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:n]
