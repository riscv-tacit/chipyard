# PMU increment range experiment

This standalone experiment measures what information is available from ordinary
Chisel increment wires and from their compiler expression graphs. It does not
implement the PMU registry or trace bridge.

## Run

From the repository root:

```sh
python3 tools/pmu-range-experiment/run.py
```

The runner uses this checkout's existing build exports and cached dependencies:
Chisel 6.7.0 with Scala 2.13.16, local FIRRTL2 and its Chisel bridge, and FireSim's
FIRRTL 1.6.0/MIDAS build with Scala 2.13.10. It compiles only the experiment and
runs offline. It rebases cached workspace paths to the current checkout. This is
a harness for the provisioned checkout, not a standalone dependency installer;
another checkout needs those build artifacts first. No root build changes are
required. All generated files go into the ignored `target/` directory.

## What runs

1. Inspect `widthOption` and `litOption` at registration, end of the owner module,
   after elaboration, and after compiler processing. Inspect the original literal
   and PopCount expression as controls; every published increment is a wire.
2. Save native Chisel FIRRTL and convert it through the same
   `ChiselBridge.annosToState` used by Chipyard's legacy FIRRTL emission.
3. Inspect raw CHIRRTL, resolved high FIRRTL, middle FIRRTL, and optimized low
   FIRRTL using a small custom unsigned interval analyzer.
4. In a separate JVM, parse the raw circuit with FireSim's legacy FIRRTL 1.6.0 and
   run the pre-Golden-Gate target-lowering pass list from
   `GoldenGateCompilerPhase`, with no user TargetTransforms. Parse and restore
   expression types for range analysis of that output.
5. Check middle, optimized-low, and legacy-lowered expressions against independent
   arithmetic formulas. Sweep all 8,192 combinations of primary inputs and all
   2,048 combinations of the two register states separately. No output in this
   DUT mixes state with primary inputs, so these sweeps cover each output's full
   combinational domain. Check earlier-stage intervals against the resulting
   extrema as well.

The register sweep includes arbitrary representable states, not just states
reachable after reset. It validates the intentional register-boundary fallback;
it does not prove sequential invariants.

## Scope of the analyzer

The tighter ranges in the results come from **our custom analysis**, not a Chisel
or FIRRTL max-value getter. The analysis follows named drivers and hierarchical
output connections, propagates unsigned intervals, honors destination narrowing,
and stops at registers. Before ExpandWhens, conditionally driven wires use a
conservative width bound. Unsupported operations use their width bound when
known; unresolved cases remain unknown. The concrete evaluator supports division
to check the deliberately conservative division-analysis fallback.

This is an experimental subset for the included circuits. It does not handle
arbitrary aggregates, memories, black boxes, per-instance input specialization,
signed arithmetic, relational proofs, sequential reachability, or production
annotation/rename tracking. The test outputs keep observation points alive.

**This does not run full Golden Gate bridge extraction, FAME transformation,
FPGA synthesis, or RTL simulation.** The evidence covers expression availability,
range inference, concrete expression semantics, and the pre-GG compiler handoff.

## Artifacts

- `target/chisel-observations.json`: direct Chisel observations at each stage.
- `target/native-chisel.fir`: native emitted Chisel expression graph.
- `target/{raw-chirrtl,resolved-high,middle,low-optimized,legacy-pre-gg}.fir`:
  inspected circuits.
- `target/ranges.csv` and `target/ranges.json`: inferred intervals and reasons.
- `target/validation.json`: enumerated extrema versus inferred intervals.
- `target/legacy-pass-order.txt`: actual legacy pre-GG lowering order.

See [the experiment report](../../reference-notes/boom-pmu-range-experiment.md)
for measured results and the implications for registration/finalization.
