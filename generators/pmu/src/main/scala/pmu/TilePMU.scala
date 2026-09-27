package pmu

import chisel3._
import chisel3.reflect.DataMirror

/** Native controls and outputs; metadata and wiring handles are Scala values. */
final case class PMUAttachment(
  enable: Bool, clear: Bool, snapshot: Bool,
  values: Vec[UInt], snapshot_values: Vec[UInt], snapshot_valid: Bool,
  manifest: PMUManifest
)

object TilePMU {
  /** Instantiate a native bank from sealed events in the registry owner's module.
    * The bank always observes the domain recorded by registry.build(), even if the
    * caller has since entered a different withClockAndReset context.
    */
  def build(events: PMUEvents): PMUAttachment = {
    require((events.increments ++ Vector(events.clock, events.reset)).forall(DataMirror.isVisible),
      "TilePMU.build must be called where the finalized event wires are visible")
    val native_pmu = withClockAndReset(events.clock, events.reset) {
      Module(new TilePMU(events.manifest.events.size, events.manifest.counterBits))
    }
    events.increments.zipWithIndex.foreach { case (increment, id) =>
      native_pmu.increments(id) := increment
    }
    PMUAttachment(native_pmu.enable, native_pmu.clear, native_pmu.snapshot,
      native_pmu.values, native_pmu.snapshot_values, native_pmu.snapshot_valid, events.manifest)
  }
}

/** Native cumulative counters and a coherent read snapshot. No simulator dependency. */
private[pmu] final class TilePMU(count: Int, counterBits: Int) extends Module {
  val increments = IO(Input(Vec(count, UInt(counterBits.W))))
  val enable = IO(Input(Bool()))
  val clear = IO(Input(Bool()))
  val snapshot = IO(Input(Bool()))
  val values = IO(Output(Vec(count, UInt(counterBits.W))))
  val snapshot_values = IO(Output(Vec(count, UInt(counterBits.W))))
  val snapshot_valid = IO(Output(Bool()))
  val counters = RegInit(VecInit(Seq.fill(count)(0.U(counterBits.W))))
  val captured = RegInit(VecInit(Seq.fill(count)(0.U(counterBits.W))))
  val captured_valid = RegInit(false.B)
  for (id <- 0 until count) {
    val next = counters(id) + Mux(enable, increments(id), 0.U)
    // Capture before clear, including this edge's increment when enabled.
    when(snapshot) { captured(id) := next }
    when(clear) { counters(id) := 0.U }
      .elsewhen(enable) { counters(id) := next }
  }
  when(snapshot) { captured_valid := true.B }
  values := counters
  snapshot_values := captured
  snapshot_valid := captured_valid
}
