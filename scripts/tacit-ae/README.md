# TACIT artifact evaluation

Drivers for the experiments in the TACIT paper. Each experiment is one self-contained
script under `experiments/<name>/` that builds its workload, runs it on FireSim, decodes
the traces, and prints the numbers and figures the paper reports, over a small common
layer shared by all of them.

## `lua_fusion`

Profile-guided dispatch fusion in a Lua 5.4.7 interpreter on MegaBoom v3, traced with
TACIT on AWS F2. Three interpreters -- stock Lua and two with a hand-inserted
fused-dispatch guard -- run mandelbrot on four FPGA slots in a single FireSim launch (three
traced runs plus a chores job that exports the kernel's jump-label patch map). The three
captures are bundled, decoded in parallel, analysed, and compared. The pipeline is resumable:
every step checks for its own outputs first, so a failure late in the flow does not cost an
FPGA run.

## `process_launch`

The process-launch case study: a 10,000-iteration spawn-and-reap benchmark whose latency
tail is set by RCU callback batching. One workload
(`software/firemarshal/example-workloads/process-launch-f2.json`), seven jobs in one launch:
chores, two 10k latency collections (control, and the fix -- the RCU grace-period kthread
moved to SCHED_BATCH at runtime, so both share one kernel), two 256-launch runs traced by
TACIT (control, fix), and two 10k collections with the diagnosis tracepoints enabled (full,
and lite without the high-rate rcu_invoke_callback probe). It produces the control latency
distribution, the tracepoint-overhead density plot and the fix's tail CCDF straight from the
uartlogs, and decodes the two traces into speedscope profiles: the whole trace (about 300 MB,
slow to open) and, cut from it, one small file per launch for the five slowest launches and a
median one -- a launch is the launcher's root frame plus the child's that follows it, so the
tail can be inspected wherever it fell (`out/process_launch/speedscope/`, open at
https://www.speedscope.app).

```sh
./run.sh process_launch            # image -> fpga (7 slots) -> bundle -> decode -> analyse -> report
```

## Quick start

```sh
./run.sh lua_fusion --list        # the plan, no work done
./run.sh lua_fusion               # everything: build -> image -> fpga -> bundle -> decode -> analyse -> report
./run.sh lua_fusion --force       # redo every step
./run.sh lua_fusion --narrow      # decode without bb_pair_stats (faster, less memory)
./run.sh lua_fusion --from analyse   # redo one step and the ones after it (build, image, fpga, bundle, decode, analyse)
```

`run.sh` only sets up the environment -- it sources conda, `env.sh` and FireSim's
`sourceme-manager.sh`, which mutate the shell in ways Python cannot -- and hands off to
`experiments/<name>/<name>.py` (`lua_fusion` when no name is given). Everything else is
that experiment's one Python file.

## Requirements

| step | needs |
| --- | --- |
| `build` | the RISC-V toolchain from the chipyard conda env (`./build-setup.sh riscv-tools`) |
| `image` | `debugfs` (e2fsprogs), for verifying each binary inside the guest rootfs |
| `fpga` | AWS credentials configured for the FireSim manager (`aws configure`, or `firesim managerinit --platform f2`), permission to launch four `f2.6xlarge` on demand, and exclusive use of the run farm tag `tacit-ae-runfarm`. The bitstream is `control_f2_megaboom_tacit_pcim_sweep_progthresh` (MegaBoom v3 + TACIT); its AGFI and build recipe are in `sims/firesim/deploy/tacit-runtime/`. |
| `decode` | the decoder binary: `cargo build --release` in `software/tacit_decoder` (cargo from rustup, typically `~/.cargo/bin`). About 2.5 GB RAM per decode; the three run in parallel when memory allows. |
| all | `pandas`, `matplotlib`, `pyelftools` |

Rough wall-clock: build 1 min, image 3 min (kernel and rootfs build once, then cached), fpga
15 min including instance launch, bundle 1 min, decode 12 min for all three in parallel,
analyse and report 1 min. The script times each step, so the numbers it prints are measured.

## Layout

```
run.sh                              entry point: ./run.sh <experiment> [flags]
paths.py                            where everything lives, derived from this file's location
shell.py                            running commands, the console protocol, timing
uartlog.py                          parsing a FireSim uartlog
firesim.py                          verify_in_rootfs(), dump_from_rootfs(), newest_results_dir()
bundle_run.py                       packages a capture with the binaries and patch map that explain it
speedscope_iters.py                 cuts single iterations (launcher root + child root) out of a profile
experiments/lua_fusion/
  lua_fusion.py                     the arm table and the seven steps, in run order
  analysis/  configs/               the per-arm flow, the plotters, receiver templates, opcode tables
experiments/process_launch/
  process_launch.py                 the job table and the six steps, in run order
  analysis/  configs/               the three latency plotters, the decode template
out/<experiment>/                   everything it produced (gitignored; ~9 GB for lua_fusion)
```

The six modules at the top are the common layer: every experiment needs them and getting
them wrong is silent -- a stale rootfs, a `\r` in a parsed number, a glob that matches
nothing. Adding an experiment means adding `experiments/<name>/<name>.py` with its own
analysis and configs beside it; nothing shared needs to change.

The experiment's inputs live where they belong to: the interpreter trees and the FireMarshal
workload (`lua-fusion.json`, one overlay, four jobs) in `software/lua-dispatch`; the FireSim
runtime config, hardware database and build recipe in `sims/firesim/deploy/tacit-runtime/`
(named without the `config_` prefix, which FireSim's `.gitignore` drops at any depth).

## The arms

| arm | change |
| --- | --- |
| `base` | none -- stock Lua 5.4.7 |
| `mulmul` | one guard, MUL→MUL |
| `mmadd` | two guards, MUL→MUL and MUL→ADD |

Each arm replaces one `vmbreak;` in `lvm.c` with a guard that tests the next opcode against a
constant and jumps straight to its handler; `--list` prints the exact edit. Two canaries run
with the report: every arm executes the same bytecode, so the decoder must see the same number
of handler entries in each; and a guard is the only reason a handler is ever entered without
going through the indirect jump, so the number of such entry sites must equal the number of
guards declared. Either one failing means that arm's dispatch profile cannot be trusted.

## Where the output goes

```
out/lua_fusion/
  fig.runtime.pdf  fig.pred_grid.pdf     across-arm figures
  <arm>/bundle/                          the capture: trace, binaries, kernel dwarf,
  <arm>/bundle/out/                      jump-label patch map, and the decoder's tables
  <arm>/vbb.mandelbrot.txt               blocks ranked by variance-weighted cost
  <arm>/fig.bb_distributions.pdf         per-block latency distributions
  logs/<step>.<arm>.log                  one transcript per step
```

A bundle is self-contained and uses only bundle-relative paths, so it can be copied to another
machine and decoded there.
