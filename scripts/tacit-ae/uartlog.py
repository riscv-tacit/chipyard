r"""Reading a FireSim uartlog.

Two traps, both learned the hard way and both silent when you get them wrong:

  - The log is not clean UTF-8. The guest emits control bytes during boot, so it
    must be read as bytes and decoded with errors="replace" -- which is the same
    reason `grep` needs -a on these files.
  - Every line ends in one or more carriage returns ("...cycles\r\r"). A regex
    anchored with $, or a naive split, carries the \r into the captured value, so
    int() blows up or a string compare fails for no visible reason.

What is parsed here is what the platform emits: the tracer's own summary line and
the pass/fail line. A correctness marker like "inset 321907 checksum 232664538"
belongs to whichever workload printed it, so experiments pull those out with
search() rather than this module growing a field per benchmark.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# "tacit: stall_cycles=20 gap_cycles=0 ... window_cycles~=6548791000 perturbation=0.0000%"
# window_cycles uses ~= because it is a count of sampled cycles, not an exact total.
_PAIR = re.compile(r"(\w+)\s*~?=\s*(\S+)")
_DONE = re.compile(r"\*\*\* (PASSED|FAILED) \*\*\* after (\d+) cycles")


def _coerce(v: str):
    """int where integral, float for percentages, str otherwise."""
    v = v.rstrip("%")
    for cast in (int, float):
        try:
            return cast(v)
        except ValueError:
            pass
    return v


@dataclass
class Uartlog:
    path: Path
    text: str
    tacit: dict = field(default_factory=dict)       # the tracer's summary line
    processes: list = field(default_factory=list)   # asid/pid/comm lines
    passed: bool | None = None
    total_cycles: int | None = None

    # -- the tracer's own account of the run ---------------------------------
    @property
    def window_cycles(self) -> int | None:
        """Cycles inside the traced region. This is the figure experiments compare."""
        return self.tacit.get("window_cycles")

    @property
    def stall_cycles(self) -> int:
        """Cycles the core was stalled by the trace unit -- the perturbation budget."""
        return self.tacit.get("stall_cycles", 0)

    @property
    def gap_cycles(self) -> int:
        return self.tacit.get("gap_cycles", 0)

    @property
    def dropped_packets(self) -> int:
        """Non-zero means the trace is incomplete and any block profile from it is a lie."""
        return self.tacit.get("dropped_packets", 0)

    @property
    def pause_count(self) -> int:
        return self.tacit.get("pause_count", 0)

    @property
    def perturbation(self) -> float:
        return self.tacit.get("perturbation", 0.0)

    @property
    def lossless(self) -> bool:
        """Did the capture lose nothing? The precondition for trusting a decode."""
        return self.dropped_packets == 0 and self.gap_cycles == 0

    # -- experiment-specific extraction --------------------------------------
    def search(self, pattern: str, group: int = 1, cast=None, default=None):
        """First match of `pattern`, e.g. r'checksum (\\d+)'. None if absent."""
        m = re.search(pattern, self.text)
        if not m:
            return default
        v = m.group(group)
        return cast(v) if cast else v

    def search_all(self, pattern: str, group: int = 1) -> list[str]:
        return [m.group(group) for m in re.finditer(pattern, self.text)]

    def __str__(self) -> str:
        w = f"{self.window_cycles:,}" if self.window_cycles else "?"
        # every one of these files is named "uartlog"; the parent names the run
        return (f"{self.path.parent.name}: window={w} total={self.total_cycles:,} "
                f"passed={self.passed} stall={self.stall_cycles} "
                f"dropped={self.dropped_packets}")


def parse(path: Path | str) -> Uartlog:
    """Read and parse a uartlog. Raises FileNotFoundError if it is not there."""
    path = Path(path)
    # bytes, not text: see the module docstring. \r is stripped globally rather
    # than per-match so that every regex below can be written the obvious way.
    text = path.read_bytes().decode("utf8", "replace").replace("\r", "")
    log = Uartlog(path=path, text=text)

    for line in text.splitlines():
        if not line.startswith("tacit:"):
            continue
        pairs = {k: _coerce(v) for k, v in _PAIR.findall(line[len("tacit:"):])}
        # Several lines share the prefix; the summary is the one carrying the
        # window, the others announce processes the tracer saw.
        if "window_cycles" in pairs:
            log.tacit.update(pairs)
        elif "comm" in pairs:
            log.processes.append(pairs)

    if m := _DONE.search(text):
        log.passed = m.group(1) == "PASSED"
        log.total_cycles = int(m.group(2))
    return log


if __name__ == "__main__":
    import sys
    for p in sys.argv[1:]:
        log = parse(p)
        print(log)
        print("   processes:", ", ".join(f"{x['comm']}(asid={x['asid']})"
                                         for x in log.processes) or "none")
        print("   lossless :", log.lossless, " perturbation:", log.perturbation, "%")
