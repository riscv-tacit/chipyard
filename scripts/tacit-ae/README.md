# TACIT artifact evaluation

Drivers for the experiments in the TACIT paper. Each experiment is one self-contained
script under `experiments/<name>/` that builds its workload, runs it on FireSim, decodes
the traces, and prints the numbers and figures the paper reports, over a small common
layer shared by all of them.

## `lua_fusion`

Profile-guided dispatch fusion in a Lua 5.4.7 interpreter on MegaBoom v3, traced with
TACIT on AWS F2. Four interpreters -- stock Lua, two with a hand-inserted fused-dispatch guard, and a
span-ranked control guard -- run mandelbrot on five FPGA slots in a single FireSim launch
(four runs plus a chores job that exports the kernel's jump-label patch map; three are decoded). The three
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
./run.sh process_launch            # image -> driver -> fpga (7 slots) -> bundle -> decode -> analyse -> report
```

## `adversarial`

Worst-case encoder overhead on bare-metal micro-benchmarks (`software/adversarial-tacit-bmarks`),
trace to the DMA sink. Kernels drive the 4-wide MegaBoom toward the encoder's limits: not-taken
and taken branches with 0, 1 or 3 fillers between them, table-driven indirect jumps whose
targets are near, 8 KiB or 16 MiB away (1, 2 or 3 packetizer cycles per packet), call/return
storms, and branch bursts behind DRAM misses. Each kernel interleaves untraced and traced
repetitions of identical code and prints a RESULT line per run. Three ELFs, three slots, one
launch, no Linux and no decode; the analysis is the RESULT-line table
(`out/adversarial/overhead.txt`) and the paper's runtime figure (`fig.overhead.pdf`). Headline:
back-to-back not-taken branches run at 3.2x, the 1 packet per cycle drain; one filler between
them brings it to 1.07x; near indirect jumps 1.78x, far ones 1.44x.

```sh
./run.sh adversarial               # image (cross-compile) -> driver -> fpga (3 slots) -> analyse -> report
```

## `ipi_storm`

The IPI-storm case study, on the one dual-core bitstream
(`control_f2_dualmegaboom_tacit_pcim_sramq_d64_trapprv`: two MegaBoom v3 harts, each with its own
TACIT encoder). One process, two threads (`software/firemarshal/example-workloads/ipi-storm`): a
reader pinned to CPU 0 spins over a shared page while a writer pinned to CPU 1 flips the page's
protection 200 times. Each flip makes Linux flush the reader's TLB through an OpenSBI remote
fence, so the reader's hart takes an inter-processor interrupt into machine mode and returns.
`trace-submit` traces both harts (`tacit0.out`, `tacit1.out`); the reader's trace is decoded with
the `func_path` receiver, which follows every `_trap_handler` invocation during the process that
handled a TLB request. Two figures: cycle attribution over the handler's basic blocks (the top 12
and the rest, with the cumulative share) and the latency of the first ten blocks after each return
to the reader. Two slots in one launch (chores and the traced run), a few minutes on the farm.

The reader's trace is the one for the hart Linux calls CPU 0, which is whichever hart won
OpenSBI's boot lottery; the script reads it from the run's OpenSBI banner (`Boot HART ID`)
instead of assuming it. Hart 0 wins on this bitstream.

```sh
./run.sh ipi_storm                 # image -> driver -> fpga (2 slots) -> bundle -> decode -> analyse -> report
```

## `spec_overhead`, `spec_lossy`, `spec_oracle`

SPEC CPU2017 intspeed, eleven benchmarks (xz counts twice, one per workload), three
questions, three launches. SPEC is licensed: the `image` step compiles the benchmarks from the
reviewer's own installation, pointed to by `$SPEC_DIR`, into a FireMarshal overlay
(`software/spec2017/speckle/gen_binaries.sh`, an hour or so the first time per input set);
nothing SPEC-derived is in the repository. The three run on distinct run farms
(`tacit-ae-spec-overhead-<input>`, `tacit-ae-spec-lossy-<input>`, `tacit-ae-spec-oracle`) once their
images and driver exist; the per-input tag lets a ref run share the farm with a train run.

| experiment | input | slots | measures |
| --- | --- | --- | --- |
| `spec_overhead` | train (`--input ref` for the paper's) | 22 | each benchmark untraced and traced lossless over DMA: runtime and CPI overhead, host DMA bandwidth, trace bits per instruction, geomeans (`out/spec_overhead/<input>/overhead.csv`, `overhead.pdf`) |
| `spec_lossy` | train (as in the paper) | 11 | each benchmark traced in lossy mode: the share of the run the encoder spent paused instead of stalling the core (`out/spec_lossy/<input>/lossy-coverage.csv`, `lossy-coverage-lollipop.pdf`) |
| `spec_oracle` | ref (as in the paper) | 12 | a five-second window per benchmark after three seconds of warm-up, traced by TACIT while TraceDoctor's basic-block oracle records true cycle counts, charged at the block boundary and, separately, split 1/n over the block; each capture is bundled and decoded against both for TACIT and three emulated formats (TNT+CYC, with return compression, timestamp counters) at block and function granularity (`out/spec_oracle/eps_bb.csv`, `eps_bb_inst.csv`, `eps_func.csv`, `eps_vs_oracle_stacked.pdf`) |

```sh
# SPEC at ~/spec2017/cpu2017 (setup-ae.sh) or export SPEC_DIR=/path/to/cpu2017
./run.sh spec_overhead --to driver        # image steps must run one at a time (shared Linux tree, one mount point)
./run.sh spec_lossy --to driver
./run.sh spec_oracle --to driver
./run.sh spec_overhead --from fpga &      # then the run-farm halves in parallel
./run.sh spec_lossy --from fpga &
./run.sh spec_oracle --from fpga &
./run.sh spec_overhead --input ref        # the paper's overhead numbers: ref inputs, hours on the farm
```

`spec_overhead` and `spec_lossy` take `--input test|train|ref` (default train, which is what
`./run.sh all` runs). Each input set is its own workload JSON, its own SPEC compile and set of
images, and its own output tree (`out/spec_overhead/<input>/`), so a ref run sits beside the
train one. The paper reports overhead on ref and lossy coverage on train; ref overhead runs
are long (the slowest jobs, xz and mcf with tracing, take up to two days on the farm, against
about four hours for train) and `all` does not run them. `spec_oracle` is ref only: its five-second window needs a run that lasts
longer than the test inputs do.

The workloads are `software/spec2017/marshal-configs/spec17-intspeed-{test,train,ref}-overhead.json` and `-{test,train,ref}-lossy-full.json` (whole-run lossy; the `train-lossy` one without `-full` is an older windowed measurement) and `spec17-intspeed-ref-oracle.json`;
the runtime configs `sims/firesim/deploy/tacit-runtime/spec-{overhead,lossy,oracle}.yaml`
(the oracle one carries the TraceDoctor `plusarg_passthrough`; the overhead and lossy ones are
templates from which the scripts derive a `config_spec-<x>-<input>.yaml` per input). `spec_oracle` needs the decoder
and about 2.5 GB per decode; it decodes as many captures in parallel as the host has cores for.

## Quick start

```sh
./run.sh all                      # every experiment: shared preparation, then all in parallel (SPEC: train, train, ref)
```

`all` runs every directory under `experiments/` and first does the host-only preparation serially -- the `br-base` rootfs and kernel, then
each experiment through its `driver` step -- because FireMarshal builds every kernel in one
Linux tree and modifies images through one mount point. From the `fpga` step on the
experiments touch only their own run farm (distinct `run_farm_tag`), results directory and
`out/` tree, so they run at the same time, one tmux window each in a session named `tacit-ae`:
`tmux attach -t tacit-ae` shows them live (Ctrl-b n / p moves between experiments, Ctrl-b d
detaches), a finished window stays open until Enter is pressed, and each console is also kept
in `out/<experiment>/logs/run.log`. Without tmux installed, or with `--no-tmux`, they run in the
calling console with the experiment's name in front of every line (and `all` says so). Re-running resumes.

### One experiment

```sh
./run.sh lua_fusion --list        # the plan, no work done
./run.sh lua_fusion               # everything: build -> image -> driver -> fpga -> bundle -> decode -> analyse -> report
./run.sh lua_fusion --force       # redo every step
./run.sh lua_fusion --narrow      # decode without bb_pair_stats (faster, less memory)
./run.sh lua_fusion --from analyse --force   # redo analysis onward, keep the rest
./run.sh lua_fusion --to driver      # stop after the host-only steps
```

Every experiment has the same step contract (`steps.py`): the host-only steps end with
`driver`, the run-farm steps start with `fpga`, and `--force`, `--from`, `--to` and `--list`
mean the same thing everywhere. That contract is what `all` relies on.

`run.sh` only sets up the environment -- it sources conda, `env.sh` and FireSim's
`sourceme-manager.sh`, which mutate the shell in ways Python cannot -- and hands off to
`experiments/<name>/<name>.py` (`lua_fusion` when no name is given). Everything else is
that experiment's one Python file.

## From a fresh FireSim manager instance

Assumes an AWS F2 FireSim manager instance as the FireSim tutorial tooling allocates it: the
account's credentials in `~/.aws/`, the run-farm key in `~/firesim.pem`, and the instance
tagged `firesim-tutorial-username`, which the manager reads to pick the key pair, VPC and
security groups (nothing in this tree needs editing for that). `conda` and `cargo` must be on
the PATH; everything else is built below. Run the long steps inside `tmux` or `screen`: the
setup takes about an hour and `run.sh all` about five hours, most of it the train runs (it opens its own
tmux session for the parallel phase; the preparation before it runs in the calling shell).

```sh
git clone -b tacit-asplos-2027-ae https://github.com/riscv-tacit/chipyard.git tacit-chipyard
cd tacit-chipyard
scripts/tacit-ae/setup-ae.sh            # about an hour
cd scripts/tacit-ae
./run.sh lua_fusion                     # one experiment end to end, about 30 min
./run.sh all                            # everything (SPEC on train, train, ref); tmux attach -t tacit-ae
```

`setup-ae.sh` installs `tmux` and `e2fsprogs` if the instance lacks them, runs chipyard's
`build-setup.sh riscv-tools` (conda env, RISC-V toolchain, FireSim, FireMarshal), initialises the SPEC submodule that chipyard's setup skips, builds the
trace decoder, checks the AWS identity, key file and instance tag, and installs SPEC CPU2017
from the artifact's ISO bucket (`s3://tacit-ae-spec2017-696925255345`, readable by every
reviewer's IAM user) into `~/spec2017/cpu2017`, which is where the `spec_*` experiments look.
It is idempotent: rerunning skips what is already there. With your own SPEC installation
already in place, `export SPEC_DIR=/path/to/cpu2017` before running it and the download is
skipped.

Every experiment prints its report at the end and leaves its tables and figures under
`out/<experiment>/`; `./run.sh <experiment>` again resumes past anything already done, and
`--force` redoes it. The farms use `f2.6xlarge` on demand, 57 at once for `all`; if the
account's F2 quota is smaller, run the experiments one at a time (`./run.sh <experiment>`) and
they will queue on their own tags. Every run farm terminates itself when its workload
completes; after an interrupted run, `firesim -c tacit-runtime/<experiment>.yaml -a
tacit-runtime/tacit-ae-hwdb.yaml -r tacit-runtime/tacit-ae-build-recipes.yaml terminaterunfarm`
from `sims/firesim/deploy` (with `sims/firesim/sourceme-manager.sh` sourced) cleans up.

## Requirements

| step | needs |
| --- | --- |
| `build` | the RISC-V toolchain from the chipyard conda env (`./build-setup.sh riscv-tools`) |
| `image` | `debugfs` (e2fsprogs), for verifying each binary inside the guest rootfs |
| `fpga` | AWS credentials configured for the FireSim manager (`aws configure`, or `firesim managerinit --platform f2`), permission to launch four `f2.6xlarge` on demand, and exclusive use of the run farm tag `tacit-ae-runfarm`. The bitstream is `control_f2_megaboom_tacit_pcim_sramq_oracle_asserts_d64` (MegaBoom v3 + TACIT with its 64-entry SRAM packet queue and the TraceDoctor oracle, the design the paper's numbers come from); its AGFI and build recipe are in `sims/firesim/deploy/tacit-runtime/`. `process_launch` runs on `control_f2_megaboom_tacit_pcim_sramq_oracle_asserts_d64_trapprv` (the same design with the encoder's trap-privilege fix) and `ipi_storm` on its dual-core counterpart `control_f2_dualmegaboom_tacit_pcim_sramq_d64_trapprv`. |
| `decode` | the decoder binary: `cargo build --release` in `software/tacit_decoder` (cargo from rustup, typically `~/.cargo/bin`). About 2.5 GB RAM per decode; the three run in parallel when memory allows. |
| all | `pandas`, `matplotlib`, `pyelftools` |
| `spec_*` `image` | a licensed SPEC CPU2017 installation at `~/spec2017/cpu2017` (what `setup-ae.sh` installs) or at `$SPEC_DIR`, compiled with the chipyard RISC-V Linux toolchain |

Rough wall-clock: build 1 min, image 3 min (kernel and rootfs build once, then cached), fpga
15 min including instance launch, bundle 1 min, decode 12 min for all three in parallel,
analyse and report 1 min. The script times each step, so the numbers it prints are measured.

## Layout

```
setup-ae.sh                         one-shot setup on a fresh manager instance (toolchain, FireSim, decoder, SPEC)
run.sh                              entry point: ./run.sh all | ./run.sh <experiment> [flags]
all.py                              shared preparation, then every experiment in parallel
steps.py                            the step contract experiments share (prepare ends at driver, run starts at fpga)
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
spec.py                             the SPEC benchmark table, $SPEC_DIR check, counter-log parser
experiments/spec_overhead/          image -> driver -> fpga -> analyse (analysis/overhead.py)
experiments/spec_lossy/             image -> driver -> fpga -> analyse (analysis/compare_lossy.py)
experiments/spec_oracle/            image -> driver -> fpga -> bundle -> decode -> analyse (analysis/plot_eps*.py, configs/decode.json)
experiments/adversarial/            image -> driver -> fpga -> analyse (analysis/parse_results.py, plot_overhead.py)
experiments/ipi_storm/              image -> driver -> fpga -> bundle -> decode -> analyse (analysis/plot_func_path_pareto.py, plot_post_exit.py, configs/decode.json)
out/<experiment>/                   everything it produced (gitignored; ~9 GB for lua_fusion)
```

The eight modules at the top are the common layer: every experiment needs them and getting
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
| `leimul` | one guard, LEI→MUL: the edge the whole-handler-span ranking promotes; run for its runtime only |

Each arm replaces one `vmbreak;` in `lvm.c` with a guard that tests the next opcode against a
constant and jumps straight to its handler; `--list` prints the exact edit.

The fourth arm is the control for the profile itself. A tracer that stamps every `jr` can
time a whole handler exactly, and ranking edges by that span puts LEI→MUL among the top
few; ranking by the entry block alone, the dispatch cost, says LEI→MUL's arrival is already
predicted. The baseline capture is decoded a second time with the receiver timing whole
handlers; `fig.unit_mul.pdf` shows MUL's arrivals by predecessor under both units, after-LEI
on the floor in the entry panel and well off it in the span panel; and the `leimul` arm
shows what the span-ranked guard is worth: its runtime bar sits on the baseline's. Two canaries run
with the report: every arm executes the same bytecode, so the decoder must see the same number
of handler entries in each; and a guard is the only reason a handler is ever entered without
going through the indirect jump, so the number of such entry sites must equal the number of
guards declared. Either one failing means that arm's dispatch profile cannot be trusted.

## Where the output goes

```
out/lua_fusion/
  fig.runtime.pdf  fig.pred_grid.pdf     across-arm figures
  fig.unit_mul.pdf                       MUL's arrivals by predecessor: entry block vs handler span
  <arm>/bundle/                          the capture: trace, binaries, kernel dwarf,
  <arm>/bundle/out/                      jump-label patch map, and the decoder's tables
  <arm>/vbb.mandelbrot.txt               blocks ranked by variance-weighted cost
  <arm>/fig.bb_distributions.pdf         per-block latency distributions
  logs/<step>.<arm>.log                  one transcript per step
```

A bundle is self-contained and uses only bundle-relative paths, so it can be copied to another
machine and decoded there.
