# Adversarial TACIT micro-benchmarks

Bare-metal (M-mode, htif) micro-benchmarks that drive the TACIT parallel
encoder on the 4-wide MegaBoom F2 design toward its worst case, with the trace
going to the **DMA sink** (the realistic deployment path) rather than the
FireSim raw-byte bridge.

## What bounds the overhead

Lossless overhead is exactly the cycles the ROB is commit-gated by the
encoder's `stall`, exposed as `TR_TE_STALL_COUNT`. On this design:

| Path                                   | Cost                                      |
|----------------------------------------|-------------------------------------------|
| Enqueue                                | up to 4 packets / cycle (one per slot)    |
| Compressed TB / NT / IJ (1 byte)       | 1 packetizer cycle per packet             |
| Full packet of N bytes                 | ceil(N/4) + 1 packetizer cycles           |
| UJ (`jalr`, `ret`): 3..7 bytes         | 2..3 cycles, never compressed             |
| Stall watermark                        | occupancy >= 48 of 64 (16-entry reserve)  |
| DMA sink                               | one TL Put per bus word, <= 16 in flight  |

So the adversary is control-flow events per commit cycle, weighted by packet
cost, plus bursts of more than ~48 packets. Branch *mis*prediction is not
adversarial: it lowers commit rate and relieves the queue.

## Families

| ELF                   | Bench          | Knob                                  | Expectation                                  |
|-----------------------|----------------|---------------------------------------|----------------------------------------------|
| `branch-dense.elf`    | `nt_f{0,1,3,7,15}` | ALU fillers per not-taken branch  | f0: ~4 pkt/cyc vs 1/cyc drain, core throttled to ~1 branch/cycle |
|                       | `tk_f{0,1,3,7}`    | fillers per taken branch          | ~1 taken/cycle (fetch bundle), near break-even |
| `indirect-dense.elf`  | `call_f{0,1,3,7}`  | fillers per `jal`+`ret` pair      | IJ 1 B + UJ 3 B per pair, 1 + 2 cycles       |
|                       | `jr_{near,d13,d20,d24}` | XOR distance of jr target    | target varint 1/2/3/4 bytes, 2..3 cyc/packet |
| `mem-burst.elf`       | `chase_b{0,8,32,64,120}` | branches queued behind a DRAM miss | b0 = floor; b64+ bursts past the 48 watermark |
|                       | `seq_b{2,8}`       | L1-resident sequential control    | plain loop, low pressure                     |

Each kernel runs a warmup, then `REPS` untraced and `REPS` traced repetitions
of identical code, printing one `RESULT` line per run with cycles, instret,
encoder stall cycles, DMA bytes and DMA source-ready stall cycles (deltas
across the traced window).

## Build and run

```
# from chipyard root, env sourced
cd software/firemarshal
./marshal -d build   ../adversarial-tacit-bmarks/adversarial-tacit-bmarks.json
./marshal -d install ../adversarial-tacit-bmarks/adversarial-tacit-bmarks.json
# then in sims/firesim/deploy/config_runtime.yaml:
#   workload_name: adversarial-tacit-bmarks.json   (3 slots)
#   default_hw_config: control_f2_megaboom_tacit_pcim_sramq_asserts_hellafix
```

Summarize a results directory:

```
software/adversarial-tacit-bmarks/scripts/parse_results.py sims/firesim/deploy/results-workload/<run>
```

## Notes

* Sources live in `src/` (marshal `workdir`); the JSON sits beside it.
* The DMA buffer is at physical 0x1_0000_0000, 1 GiB, overflow mode; the
  programs occupy < 100 MiB from 0x8000_0000. Counts are read as deltas, so
  the sink is never reset.
* `indirect-dense.elf` is ~17 MiB because the far jr islands are made with
  `.skip`; that is intentional.
* `-mno-relax` keeps the `auipc`/`addi`/`jr` dispatch from being relaxed.

## Diagnostic families and the jr ring bistability (2026-09-06/07)

`jr-diag`, `jr-diag2` (F2) and `jr-rtl*` (Verilator) chase the 31-33 % slowdown
first seen on `jr_near` / `jr_f3` with near-zero encoder stall. Result files:
`results-2026-09-06-jr-diag*.txt`, `results-2026-09-07-rtl-flip.txt`,
`tracerv-jr_f3-analysis.txt`.

Findings:

* The slowdown is **not encoder bandwidth**. The 16-handler `auipc / addi / jr`
  ring is bistable on MegaBoom: 2.25 or 3.0 cycles per handler (`auipc` and
  `jr` share the single jump unit). A brief commit gate flips it into the slow
  state and it stays there. TracerV shows the slow state as every ROB row
  splitting after the `auipc` (its dependent `addi t1` still busy), i.e. zero
  commit slack.
* On F2 the trigger is the DMA sink's first backpressure (~55 stall cycles in a
  3.1 M-cycle run). Always-ready sink and lossy mode never gate commit, so no
  flip. Untraced runs can sit in the slow state too (`jr_f3.dma-short`
  untraced, `j_f3`).
* Reproduced in Verilator with `TacitMegaBoomV3BackpressureConfig`: a single
  400-cycle sink outage (`+tacit_bp_mode=burst +tacit_bp_period=600000
  +tacit_bp_off=400`) gates commit for 314 cycles and costs 46 K cycles
  (147469 -> 193459 for 4096 rounds). Waveform of the flip:
  `sims/verilator/output/jr-flip/jr_f3_flip.fst` (cycles 596000-616000).
* Handler spacing matters: 12/16-byte rings stall often but never flip;
  20/24-byte and two-island layouts flip; 32-byte flips intermittently;
  64-byte does not.

Implication for the overhead numbers: report the encoder stall cycles and the
runtime delta separately. When they disagree by orders of magnitude the gap is
core fragility amplifying a tiny gate, not trace-encoder cost.

RTL flow (no FireSim): `make rtl` in `src/` builds `build-rtl/jr-rtl-bp.elf`
(4096 rounds, DMA buffer inside the 256 MiB simulated DRAM); run the debug
simulator directly with the plusargs above plus `+loadmem=<elf>`. TracerV
stamps on FireSim are host-inflated; trust group shapes and small deltas only.

### Rerun with rotated single fillers (2026-09-07)

`results-2026-09-07-hellafix-asserts-rotfill.txt`. The f1 variants now rotate the
filler destination over a1..a4 (four independent chains). Untraced nt_f1 moved
only from IPC 2.03 to 2.10 and its slowdown from 1.03x to 1.06x, so the
one-branch-per-cycle rate of the interleaved ring is a BOOM limit, not the
filler chain; nt_f1 remains the "production at drain rate" point (stall
fraction 0.5, slowdown 6 %). Everything else reproduced within noise. jr_near
flipped late this time (1.08x) and jr_d20's untraced pass sat in the slow state
(IPC 1.04), both consistent with the bistability above.

### Mechanism of the jr-ring bistability (RTL instrumentation, 2026-09-07)

Per-cycle printf instrumentation of BOOM (frontend stages and redirects,
decode/dispatch hazards, ROB occupancy, jump-unit issue; patch in
`scripts/boom-debug-printf.patch`, applied to `TacitMegaBoomV3BackpressurePrintfConfig`,
results in `results-2026-09-07-rtl-instrumented.txt`) around a single 314-cycle
commit gate:

| | fast (2.25 cyc/handler) | slow (3.0 cyc/handler) |
|---|---|---|
| F1/F2/F3 redirects per cycle | 0.47 / 0.47 / 0.44 | 0.48 / 0.48 / 0.33 |
| fetch buffer full | 0 % | 23 % |
| issue queue full (dispatch stall) | 32 % | 49 % |
| ROB occupancy | 80-92 / 128 | 79-100 / 128 |
| ROB full, rename stall, br-mask full, mispredict | none | none |
| jump-unit issues per cycle | 0.889 | 0.667 |
| commit rows | mostly 4-wide | split 1+3 after each `auipc` |

The predictor and frontend are identical in both states. The difference is the
scheduler: `auipc` and `jr` both need MegaBoom's single jump unit, and with
oldest-ready-first issue the relative phase of consecutive handlers' chains
either keeps the jump unit busy (2 issues per 2.25 cycles) or leaves it idle
one cycle in three. The phase is set by how early the next `auipc` reaches the
issue queue, which depends on issue-queue occupancy, which depends on the
phase: two self-consistent fixed points. A commit gate fills the ROB, halts
dispatch, drains the issue queue, and the refill lands in the other phase.
The micro-BTB capacity hypothesis was tested and refuted by a ring-size sweep
(`results-2026-09-07-rtl-ringsweep.txt`): all rings of 8-32 handlers run fast
untraced, and flip susceptibility is layout-dependent, not monotonic in size.
Waveform of the flip (core cycles 596000-616000, harness cycles are 2x):
`sims/verilator/output/jr-flip/jr_f3_flip.fst`.

## Final report workload (2026-09-07)

`tacit-overhead-report.json`: three bare-metal jobs, reps interleaved
(untraced, traced, untraced, traced, ...), DMA sink, run with a dedicated
manager config so the shared `config_runtime.yaml` is untouched:

```
cd sims/firesim && source sourceme-manager.sh && cd deploy
firesim -c tacit-runtime/tacit-overhead-report.yaml launchrunfarm
firesim -c tacit-runtime/tacit-overhead-report.yaml infrasetup
firesim -c tacit-runtime/tacit-overhead-report.yaml runworkload
firesim -c tacit-runtime/tacit-overhead-report.yaml terminaterunfarm --forceterminate
```

| Job | Benches | What it shows |
|---|---|---|
| branch | nt_f0, nt_f1, nt_f3, nt_f7, tk_f0, tk_f1, tk_f3 | compressed-packet ceiling (4:1), production at drain rate, taken branches free |
| indirect | tjr_near, tjr_d13, tjr_d20, tjr_d24, call_f0 | UJ reach sweep: 3/4 B packets fit, 5/6 B packets (3 packetizer cycles) do not; bytes are not the cost |
| mem | chase_b0, chase_b64, chase_b120 | pointer-chase floor and the ROB-full burst that exceeds the 48-entry watermark |

`tjr_*` = table-driven jr ring (`ld/addi/jr`, no auipc, hence no scheduler
bistability); "island" = one of the two contiguous handler blocks whose
separation sets the target XOR distance. The auipc-based `jr_*` rings are
excluded from the report (see the bistability sections above).

### Final report numbers (2026-09-07 22:27 run, `results-2026-09-07-report.txt`)

| Bench | Untraced IPC | Offered pkts/cycle | Packet | Packetizer cyc/pkt | Slowdown | Stall frac |
|---|---|---|---|---|---|---|
| nt_f0 | 3.22 | 3.2 | 1 B | 1 | **3.21x** | 0.75 |
| nt_f1 | 2.10 | 1.05 | 1 B | 1 | 1.07x | 0.51 |
| nt_f3 / nt_f7 | 2.03 / 2.69 | 0.5 / 0.34 | 1 B | 1 | 1.00x | 0 |
| tk_f0 / f1 / f3 | 0.57 / 1.73 / 2.35 | 0.56 / 0.87 / 0.6 | 1 B | 1 | 1.00x (±4 % noise) | 0 |
| tjr_near | 2.68 | 0.84 | 3-4 B | 2 | **1.78x** | 0.48 |
| tjr_d13 | 1.46 | 0.46 | 4 B | 2 | 1.00x | 0.03 |
| tjr_d20 / d24 | 1.46 | 0.46 | 5 / 6 B | 3 | **1.43x** | 0.64 |
| call_f0 | 0.50 | 0.5 (IJ 1 B + UJ 3 B) | mixed | 1 + 2 | 1.00x | 0.01 |
| chase_b0 / b64 / b120 | 0.06 / 0.58 / 1.03 | bursty | 1-2 B | 1 | 1.02 / 1.06 / 1.18x | 0 / 0.01 / 0.33 |

Model check: traced cycles ~= max(untraced cycles, packets x packetizer cycles
per packet), within 5 % on every row (nt_f0 1.065M pkts -> 1.079M cycles;
tjr_near 1.05M x 2 = 2.10M -> 2.21M; tjr_d20/d24 1.05M x 3 = 3.15M -> 3.29M;
tjr_d13 2.10M < 2.29M untraced -> no slowdown; call_f0 3.15M < 4.23M -> none).

### Why nt_f1 stalls half the time but loses 7 % (RTL instrumentation, 2026-09-07)

`results-2026-09-07-rtl-nt-instrumented.txt`. Untraced, nt_f1's frontend delivers
one 4-instruction bundle every other cycle (F2 corrects F1's speculative global
history on ~70 % of cycles and re-fetches), so the core commits 2 instructions
(one branch) per cycle with the ROB nearly empty (~23 entries). Traced, commit
runs `4 4 4 4 4 0S 0S 0S 0S 0S`: five 4-wide cycles enqueue 10 packets while 5
drain, then five stall cycles drain the other 5. The frontend never notices:
during the 5-cycle stall it keeps fetching into the ROB (occupancy 0 -> 124),
and the 4-wide bursts empty it again. Throughput = min(frontend 1.04, drain
1.0) branches/cycle -> 5 % loss; stall fraction = 1 - frontend/commit-burst rate
= 50 % regardless. nt_f0 runs `0S x9, 4 4 4`: the frontend (3.2 branches/cycle)
fills the ROB and FTQ (FTQ full 69 %) during each stall, so there the stall is
real loss. Rule: encoder stall cycles cost runtime only when the ROB/FTQ fill
during them and block the frontend.

### Mechanism of the indirect-jump results (RTL instrumentation, 2026-09-08)

`results-2026-09-08-rtl-tjr-instrumented.txt`. The instrumented sim reproduces
F2 (near 1.77x vs 1.78x, d13 1.00x, d24 1.44x), so the printf log explains both
halves of the curve.

**Core side — why the far rings offer half the jump rate.** The micro-BTB stores
targets as a 13-bit signed offset (`faubtb.scala`, `offsetSz = 13`, ±4 KiB).
`near`'s islands are 0x80 apart, so F1 predicts every target and the fetch
pipeline never stalls (`s2_valid` = 1.00/cycle, 0.842 jumps/cycle, 1.19 cyc/jump).
At 8 KiB and beyond F1's offset cannot reach; F2's BTB overrides with the
correct target, which asserts `f1_clear` and re-steers `s0_vpc`
(frontend.scala:496), costing exactly one fetch bubble per jump
(`s2_valid` = 0.54/cycle, i.e. `f1_clear` once per jump; 0.457 jumps/cycle,
2.19 cyc/jump). d13 and d24 are identical in the core; only the packet differs.

**Encoder side — one rule covers every bench.**
`traced_time = untraced_time x max(1, offered_rate / drain_rate)`, where the
drain is 1/(packetizer cycles per packet):

| Bench | Offered (pkt/cyc) | Packet | Drain (pkt/cyc) | Predicted | Measured |
|---|---|---|---|---|---|
| nt_f0 | 3.20 | 1 B | 1.0 | 3.20x | 3.21x |
| nt_f1 | 1.055 | 1 B | 1.0 | 1.05x | 1.07x |
| tk_* | <= 0.87 | 1 B | 1.0 | 1.00x | 1.00x |
| tjr_near | 0.842 | 4 B | 0.5 | 1.68x | 1.77x |
| tjr_d13 | 0.457 | 4 B | 0.5 | 1.00x | 1.00x |
| tjr_d24 | 0.457 | 6 B | 0.333 | 1.37x | 1.44x |

Measured runs ~5 % above the prediction; the DMA sink's source-ready stalls
(2-2.5 % of cycles) and the packetizer's one-cycle turnaround between full
packets account for it. Stall *fraction* is the duty cycle of the gate, not the
loss: nt_f1 stalls 50 % of cycles for a 5 % loss because the frontend only
supplies 1.05 branches/cycle and the ROB absorbs the 5-cycle bursts
(FTQ full 11 % of cycles), whereas tjr_near stalls 47 % and loses 43 % because
its frontend supplies 1.68x the drain, so the ROB saturates and the FTQ backs
up (full 38 % of cycles) and blocks fetch.

### Figure

`figures/tacit-overhead.pdf` (and `.png`, 300 dpi), regenerated by
`scripts/plot_overhead.py <results dir> figures/tacit-overhead`.

Spec: exactly 89 x 40 mm, i.e. one column of a two-column layout, exported at
final size with **no** tight bbox so `\includegraphics[width=\columnwidth]`
applies no scaling and the type really is 8 pt. Liberation Sans (Arial
metrics), embedded and searchable; Okabe-Ito colours; nine bars of traced
runtime as a percentage of untraced, value on each bar, dashed baseline at 100,
two-layer x axis (knob value on the ticks, family + knob name underneath).

The caption must define "filler": an independent ALU instruction placed between
consecutive branches. Also worth one clause: reps are deterministic, so the
run-to-run spread is under 1 % and error bars are omitted (the 97 % and 99 %
taken-branch bars are that noise, not a speed-up).
