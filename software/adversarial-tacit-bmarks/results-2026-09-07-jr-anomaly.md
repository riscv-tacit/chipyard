# jr ring slowdown: root cause notes (2026-09-06/07)

## Symptom
On F2 (MegaBoom, SRAM-queue encoder, DMA sink, lossless), indirect-jump rings
with 20-32 byte handlers (jr_f3, jr_p2, jr_p3, two-island jr_near) run 33%
slower traced than untraced, with ~0-900 encoder stall cycles out of 3.1M.
12/16-byte rings stall 78K-90K cycles yet lose nothing. Direct-jump twin,
always-ready sink, and lossy mode show no slowdown.

## Mechanism
1. The DMA sink is latency-bound: 8-byte Puts (mbus beat), 16 source ids =
   128 B in flight; at ~1.4 B/cycle any write-ack latency > ~90 cycles empties
   the pool (srcstall 1.4-1.8% of cycles on F2, 3.7% in RTL/DRAMSim).
2. Those refusals fill the 64-entry queue past the 48 watermark for a few
   cycles at a time: brief ROB commit gates.
3. The ring has two stable commit schedules in BOOM. Fast: rows commit 4-wide,
   2.25 cycles/handler. Slow: every row splits right after `auipc` because the
   dependent `addi t1` is still busy at the head (1+3 / 3+1 groups), 3.0
   cycles/handler. `auipc` and `jr` share the single jump unit. A brief gate
   moves the ring from fast to slow and it stays there. TracerV commit groups:
   untraced {4: 83919, 3: 16364, 1: 16401}; traced {4: 34808, 3: 65499, 1: 65542}.
4. 12/16 B handlers fit one ROB row (`auipc, addi, [nop], jr`); the jr depends
   on the addi so the row can never split: single schedule, immune.

## Evidence
- F2 jr-diag2 (results-workload/2026-09-06--23-50-47-jr-diag2): table in
  uartlog; TracerV analysis in tracerv-jr_f3-analysis.txt.
- Untraced runs land in the slow schedule too (jr_f3 1024-round untraced pass
  49153 cyc = 3.0/handler; j_f3 both passes 3.0).
- RTL (Verilator, TacitMegaBoomV3BackpressureConfig, jr-rtl-bp.elf, 4096 rounds):
  always sink 147470; +tacit_bp_mode=every +tacit_bp_beats=1500 +tacit_bp_off=150
  -> 192416 cycles with 1302 stall cycles (2.94/handler, flipped);
  off=250 -> 174748 / 7349 stall (mixed); off=400 -> 161478 / 4177 (recovers);
  off=60 -> no stall, no effect.

## Implications for the overhead study
- Encoder-attributable cost on these kernels is the stall cycles (tens to a
  few thousand). The 33% is a BOOM pipeline bistability triggered by any brief
  commit gate; report it as such, not as trace bandwidth.
- Sink fixes remove the trigger: 64 B burst Puts, nSource 32-64, a byte FIFO in
  the sink. Deepening the encoder queue helps only against transient refusals.
