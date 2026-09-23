# TACIT artifact evaluation

Drivers for the experiments in the TACIT paper. Each experiment builds its workload,
runs it on FireSim, decodes the trace, and prints the numbers and figures the paper
reports — resumably, so a failure late in the pipeline does not cost an FPGA run.

## Quick start

```sh
./run.sh lua_fusion --list                      # the plan, no work done
./run.sh lua_fusion                             # everything, all three arms
./run.sh lua_fusion --stages decode,analyse,report   # re-analyse existing captures
./run.sh lua_fusion --arms base,mulmul --force  # redo two arms from scratch
```

`run.sh` exists only to set up the environment — it sources conda, `env.sh`,
FireSim's `sourceme-manager.sh` and the Xilinx settings, which mutate the shell in
ways Python cannot — and then hands off to `driver.py`. Everything else is Python.

## Requirements

| stage | needs |
| --- | --- |
| `build` | the RISC-V toolchain from the chipyard conda env |
| `image` | `debugfs` (e2fsprogs), for verifying the binary inside the guest rootfs |
| `run` | an AWS F2 run farm (one f2.6xlarge) with the `control_f2_megaboom_tacit_pcim_sweep_progthresh` bitstream — MegaBoom v3 + TACIT, AGFI recorded in `sims/firesim/deploy/tacit-runtime/tacit-ae-hwdb.yaml` — and exclusive use of the run farm; the runs are serialised for that reason |
| `decode` | ~32 GB RAM per decode (`bb_pair_stats` dominates; `--narrow` drops it and roughly halves both time and memory) |
| all | `pandas`, `matplotlib`, `pyelftools` |

Rough wall-clock per arm: build 2 min, image 4 min, run 8 min, bundle 2 min,
decode 15 min, analyse 1 min. The driver times each stage, so the numbers it
prints are measured rather than these estimates.

## Layout

```
run.sh  driver.py            entry point and the variant x stage loop
paths.py                     where everything lives, derived from this file's location
shell.py                     running commands, the console protocol, timing
uartlog.py                   parsing a FireSim uartlog
firesim.py                   verify_in_rootfs(), newest_results_dir()
experiments/<name>/          one experiment: variants, stages, report, analysis, configs
out/<name>/                  everything it produced
```

The four modules at the top are shared because they are needed by every experiment
and because getting them wrong is silent — a stale rootfs, a `\r` in a parsed
number, a glob that matches nothing. Anything specific to one study, including its
plotting scripts and decoder configs, lives in that experiment's directory. The
decoder itself is treated as a given binary with a published output format; its own
`scripts/analysis/` tree is not used from here.

Adding an experiment means adding a directory under `experiments/` with a variants
table, a `STAGES` list, and a `report.main()`. No shared code needs to change.

## Where the output goes

Everything an experiment produces is under `out/<experiment>/`, including the
capture itself:

```
out/lua_fusion/
  fig.runtime.pdf  fig.pred_grid.pdf     across-arm figures
  <arm>/bundle/                          the capture: trace, binaries, kernel dwarf,
                                         jump-label patch map, decoder output
  <arm>/vbb.mandelbrot.txt               blocks ranked by variance-weighted cost
  <arm>/fig.bb_distributions.pdf         per-block latency distributions
  <arm>/trace.<arm>.*.csv                the decoder's tables
  logs/<arm>.<stage>.log                 one transcript per stage
```

A bundle is self-contained and uses only bundle-relative paths, so it can be copied
to another machine and decoded there. They are large — about 9 GB each — so `out/`
is gitignored.

## Experiments

### `lua_fusion`

Profile-guided dispatch fusion in a Lua 5.4.7 interpreter on BOOM v3. TACIT's block
profile identifies the interpreter's dispatch site as the dominant source of
variance-weighted cost; each arm replaces one `vmbreak;` with a guard that tests the
next opcode against a constant and jumps straight to its handler.

| arm | change |
| --- | --- |
| `base` | none — stock Lua 5.4.7 |
| `mulmul` | one guard, MUL→MUL |
| `mmadd` | two guards, MUL→MUL and MUL→ADD |
| `leimul` | one guard, LEI→MUL — a site the span profile ranks highly and the entry-block profile says is already predicted |
| `leimulctl` | the same guard with a target that never matches: the code-layout change without the guard |

`--list` prints each arm's exact source edit.

Two canaries run with the report. Every arm executes the same bytecode, so the
decoder must see the same number of handler entries in each; and a guard is the only
reason a handler is ever entered without going through the indirect jump, so the
number of such entry sites must equal the number of guards declared. Either one
failing means the dispatch profile for that arm cannot be trusted.
