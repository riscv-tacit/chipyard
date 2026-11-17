# Interval trace tools

Scripts for running and validating the FireSim
[interval trace bridge](../../generators/firechip/bridgestubs/README.interval-trace.md).
Captures use [interval JSONL](../../generators/firechip/bridgestubs/README.interval-trace.md#interval-jsonl-format)
version 3, version 2 with `+interval-trace-wall-cycles`, or version 4 (no block entries and no block catalog)
with `+interval-trace-no-blocks`.
Run them from the Chipyard root.

| Script | Purpose |
|---|---|
| `run-interval-metasim.py` | Builds the FireMarshal smoke image and a Large or Mega BOOM interval-trace metasimulator, runs it, and validates the capture. Outputs go to `tools/interval-trace/target/interval-metasim/<core>/`. |
| `check-compact-capture.py JOB --interval N [--smoke]` | Validates a capture directory in version 2, 3 or 4: block catalog (none in version 4), interval continuity, totals, PMU retirement and cycle deltas, and the smoke result or the SPEC comparisons of an image staged by `prepare-spec-tests.py`. Writes `validation.json` into the directory. |
| `collect-spec-intervals.py prepare RUN ...`, `collect RUN` | Collects interval traces of specpack FireMarshal images on AWS F2, one FPGA per benchmark: `prepare` writes the run directory and the FireSim workload, `collect` (through `RUN/launch.sh`) runs the farm and validates every job. With `--no-blocks`, `prepare` sets `+interval-trace-no-blocks`, for captures that need each interval's totals and PMU deltas and not its code. |
| `prepare-spec-tests.py` | Stages SPEC CPU2017 test runsets as marker-enabled FireMarshal images for interval capture. |
| `check-spec-gcc.py JOB --spec-dir SPEC` | Validates a capture of the single 602.gcc_s test workload: SPEC `specdiff`, the interval capture, and, with record CSVs, the exact capture edges at the workload markers. |
