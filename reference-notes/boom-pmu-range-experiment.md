# PMU increment range experiment

Date: 2026-09-15\
Status: executed; all checks passed. This is a range-information experiment,
not the complete registry/compact-observation/Golden-Gate prototype.

## Main result

**The expression graph contains enough information to infer a four-input
PopCount's range of [0, 4] immediately after Chisel elaboration. A registered
UInt wire does not expose that maximum directly.**

Width inference provides widths, not the tighter event range. For general
increment expressions, the practical initial analysis point is after width
inference and conditional-assignment expansion. The relevant expression structure
survived both optimized FIRRTL2 lowering and FireSim's legacy pre-Golden-Gate
lowering in this experiment.

The tighter ranges below were computed by the experiment's custom interval
analyzer. They are not a built-in compiler range report.

## Setup and reproduction

Run from the repository root:

```sh
python3 tools/pmu-range-experiment/run.py
```

Sources: [runner](../tools/pmu-range-experiment/run.py),
[Chisel circuit, analyzer, and checks](../tools/pmu-range-experiment/RangeExperiment.scala),
[legacy compiler handoff](../tools/pmu-range-experiment/LegacyLowering.scala).
The [README](../tools/pmu-range-experiment/README.md) describes the cached-build
requirements and generated artifacts.

The experiment used the installed Chisel 6.7.0 / Scala 2.13.16 artifacts and local
FIRRTL2 bridge. A separate JVM used FireSim's FIRRTL 1.6.0 / Scala 2.13.10 artifacts.
The test circuit publishes 20 increment wires, including a bored child-module
PopCount, without modifying BOOM or the existing build definitions.

## 1. What Scala can see

These are measured direct Chisel API results:

| Value inspected | `widthOption` | `litOption` |
| --- | --- | --- |
| Literal `1.U`, before making a wire | 1 | 1 |
| `WireInit(1.U)` | Unknown | None |
| `WireInit(UInt(1.W), 1.U)` | 1 | None |
| Four-input `PopCount(bits)`, before making a wire | 3 | None |
| `WireInit(PopCount(bits))` | Unknown | None |
| Explicit three-bit wire driven by PopCount | 3 | None |
| Explicit eight-bit wire driven by PopCount | 8 | None |
| `Wire(UInt())`, registered before assignment to PopCount | Unknown | None |

For the registered wires, these observations stayed the same at the end of the
owner module, after elaboration, and even after FIRRTL compilation. Compiler
width inference does not update the original Scala Data handles.

Consequences:

- Keeping the wire object until `build()` does not make its width become known.
- Even an explicitly sized three-bit wire only gives a representable maximum of
  7. Its driver must be analyzed to establish the tighter maximum of 4.
- `litOption` on a constant-driven wire does not reveal its constant driver.
- The original plan's requirement to know every increment width at Scala-time
  finalization is too restrictive for the proposed `WireInit` examples. Check
  known widths early and validate inferred widths in the compiler.

## 2. What each compiler stage makes available

| Stage | Available information | Experiment result |
| --- | --- | --- |
| During registration / owner `build()` | Wire handle; sometimes a width; no driver range through the inspected UInt APIs | Insufficient for general automatic sizing |
| Native Chisel FIRRTL / raw CHIRRTL after elaboration | Complete driver graph; some unresolved types and widths | Constants, PopCount, sums, muxes, and masks can already yield useful bounds |
| Resolved high FIRRTL | Inferred widths and typed arithmetic | Truncating addition and register fallback now have definite bit-vector bounds |
| Middle FIRRTL, after ExpandWhens | Conditional assignments represented as muxes; resolved widths | Simple driver traversal also handles conditional increments precisely |
| Optimized low FIRRTL | Lowered and optimized expression graph | All tested bounds retained; named wires may have become nodes or aliases |
| FireSim legacy pre-GG target lowering | Low FIRRTL before MidasTransforms | All tested bounds retained, including the bored child PopCount |

PopCount is already a tree of additions and bit slices in the earliest emitted
IR. There is no PopCount primitive for this analyzer to recognize. Propagating
[0, 1] through the leaves and respecting every slice recovers [0, 4].

## 3. Measured ranges

Representative results from middle FIRRTL; optimized low FIRRTL and legacy
pre-GG lowering produced the same bounds for these cases:

| Increment | Signal width | Width-only range | Inferred range | Interpretation |
| --- | ---: | --- | --- | --- |
| Constant-driven wire | 1 | [0, 1] | [1, 1] | Exact constant |
| Boolean wire | 1 | [0, 1] | [0, 1] | Exact bounds |
| PopCount of four bits | 3 | [0, 7] | [0, 4] | Exact bounds |
| Expanding sum of four Boolean values | 4 | [0, 15] | [0, 4] | Extra storage bits do not enlarge the value range |
| PopCount padded to eight bits | 8 | [0, 255] | [0, 4] | Follows the driver through the wire |
| PopCount assigned to a two-bit wire | 2 | [0, 3] | [0, 3] | Actual truncation respected; 4 becomes 0 |
| `Mux(sel, 2, 5)` | 3 | [0, 7] | [2, 5] | Interval bounds; not a claim that 3 and 4 occur |
| Eight-bit input AND 15 | 8 | [0, 255] | [0, 15] | Mask tightens the upper bound |
| Three-bit input plus 3, truncating | 3 | [0, 7] | [0, 7] | Wrap is accounted for |
| Upper four bits of `(input AND 15)` | 4 | [0, 15] | [0, 0] | Provably zero |
| `sel +& !sel` | 2 | [0, 3] | [0, 2] | Conservative; actual value is always 1 |
| Eight-bit running register | 8 | [0, 255] | [0, 255] | No lifetime bound inferred from an update |
| Registered PopCount | 3 | [0, 7] | [0, 7] | Stops at register; tighter reachable range needs a sequential argument |
| Default 15, then unconditional assignment of 1 | 4 | [0, 15] | [1, 1] | Last connection honored |
| Default 0, conditionally assigned 4 | 3 | [0, 7] | [0, 4] | Before ExpandWhens this prototype falls back to [0, 7] |
| Eight-bit input divided by 3 | 8 | [0, 255] | [0, 255] | Unsupported-analysis fallback; concrete maximum is 85 |
| Child-module PopCount routed with `BoringUtils.bore` | 3 | [0, 7] | [0, 4] | Routing preserves the analyzable expression chain |

At a nominal 1,000,000 cycles, PopCount's maximum of 4 gives a 22-bit delta field.
Using its three-bit representation's maximum of 7 would require 23 bits; using
the padded eight-bit representation's maximum of 255 would require 28 bits.

## 4. Validation and limits

The run passed **614,400 output checks**: 204,800 each for middle FIRRTL,
optimized low FIRRTL, and the legacy pre-GG circuit. A concrete bit-vector
interpreter evaluated the emitted expressions and compared them with independent
hand-written formulas, then checked containment in the inferred intervals.
Every known earlier-stage interval was also checked against the enumerated extrema.

Each stage swept all 8,192 combinations of the circuit's primary inputs and all
2,048 combinations of its register states separately. None of the tested output
cones mixes these domains, so each cone's full combinational domain is covered.
Register states deliberately include all representable values, including values
that may be unreachable after reset. These checks establish neither sequential
reachability nor correctness of an analyzer for arbitrary circuits.

The analyzer is a limited unsigned prototype. Missing pieces include production
annotation/rename tracking, arbitrary aggregates, memories, black boxes, signed
arithmetic, per-instance input specialization, and relational/sequential proofs.
Unknown widths remain unknown; unsupported operations use a conservative
representable range once their widths are known.

The legacy experiment runs the same pre-GG target-lowering pass list as
GoldenGateCompilerPhase with an empty user TargetTransforms list. It does not run
bridge extraction, FAME transforms, RTL simulation, or FPGA synthesis. No claim
is made yet about generating compact storage/ports or preserving their annotations
through those later stages. CIRCT/firtool and Chisel 7 were not exercised.

## 5. Recommendation for the PMU plan

1. Keep the producer API as one explicit increment wire per counter.
2. At Scala-time `build()`, seal names/IDs, route sources, and record compiler targets.
   Validate widths that are already known; defer unknown widths to compiler checks
   before bank-input truncation is legalized. The provisional catalog must allow
   unresolved increment widths.
3. Prototype the production range pass after InferWidths and ExpandWhens, before
   observation interface construction and before optimization removes source
   identities. Use module/instance connectivity when tracing bored signals.
4. Following the subsequent [API v1 decision](boom-pmu-api.md), pass the derived
   ranges and layout as serializable bridge constructor parameters. Later bridge
   Chisel generates compact interval storage. Do not expect inferred widths/ranges
   to return to the completed target Scala module body.
5. Next prove annotation transport, narrow increment/control channel routing, and
   construction of one bridge-side saturating field with target boundary capture
   through the full Golden Gate flow. That remains the next integration experiment.
