# Interval trace tools

Scripts for running and validating the FireSim
[interval trace bridge](../../generators/firechip/bridgestubs/README.interval-trace.md).
Captures use [interval JSONL version 2](../../generators/firechip/bridgestubs/README.interval-trace.md#interval-jsonl-format).
Run them from the Chipyard root.

| Script | Purpose |
|---|---|
| `run-interval-metasim.py` | Builds the FireMarshal smoke image and a Large or Mega BOOM interval-trace metasimulator, runs it, and validates the capture. Outputs go to `tools/interval-trace/target/interval-metasim/<core>/`. |
| `check-compact-capture.py JOB --interval N [--smoke]` | Validates a capture directory: block catalog, interval continuity, totals, PMU retirement and cycle deltas, and the smoke result or the SPEC comparisons of an image staged by `prepare-spec-tests.py`. Writes `validation.json` into the directory. |
| `prepare-spec-tests.py` | Stages SPEC CPU2017 test runsets as marker-enabled FireMarshal images for interval capture. |
| `check-spec-gcc.py JOB --spec-dir SPEC` | Validates a capture of the single 602.gcc_s test workload: SPEC `specdiff`, the interval capture, and, with record CSVs, the exact capture edges at the workload markers. |
