// See LICENSE for license details.

package firechip.bridgestubs

import chisel3._
import firesim.lib.bridgeutils._
import firechip.bridgeinterfaces._
import pmu.PMUManifest

/** Target-only attachment data. Increments must be visible at the attachment,
  * ordered by event ID, and already aligned to the tile trace's clock/reset and
  * observation latency. This holder neither routes nor delays producer wires.
  */
case class IntervalPMUInputs(manifest: PMUManifest, increments: Seq[UInt])

/** Target endpoint for a producer with a compiler-finalized event catalog.
  * Live BOOM registries use DeferredIntervalTraceBridge for late finalization.
  */
class IntervalTraceBridge(key: IntervalTraceBridgeKey) extends BlackBox
    with Bridge[HostPortIO[IntervalTraceBridgeTargetIO]] {
  val moduleName = "firechip.goldengateimplementations.IntervalTraceBridgeModule"
  val io = IO(new IntervalTraceBridgeTargetIO(key))
  val bridgeIO = HostPort(io)
  val constructorArg = Some(key)
  generateAnnotations()

  // Different port shapes must not share an extmodule defname. Include source
  // and catalog identity too, so distinct tile catalogs retain their annotation.
  private val identityFields = Seq(key.schemaVersion.toString, key.source, key.retirementLatency.toString,
    key.traceWidths.retireWidth.toString, key.traceWidths.iaddrWidth.toString,
    key.traceWidths.insnWidth.toString, key.traceWidths.wdataWidth.toString,
    key.traceWidths.causeWidth.toString, key.traceWidths.tvalWidth.toString) ++ key.events.flatMap { event =>
    Seq(event.id.toString, event.name, event.unit, event.description, event.incrementBits.toString)
  }
  private val identity = identityFields.map(s => s"${s.length}:$s").mkString
  private val digest = java.security.MessageDigest.getInstance("SHA-256")
    .digest(identity.getBytes(java.nio.charset.StandardCharsets.UTF_8))
    .map(b => f"${b & 0xff}%02x").mkString
  override def desiredName: String = s"IntervalTraceBridge_$digest"
}

object IntervalTraceBridge {
  /** Copy validated metadata only; provisional Scala widths are never a key. */
  def key(manifest: PMUManifest, traceWidths: TraceBundleWidths, retirementLatency: Int = 0): IntervalTraceBridgeKey = {
    require(manifest.finalized, "Interval bridge requires a compiler-finalized PMU manifest")
    require(manifest.counterBits == 64, "Interval bridge initially uses 64-bit cumulative counters")
    IntervalTraceBridgeKey(manifest.source, traceWidths,
      manifest.events.map(e => IntervalPMUEvent(e.id, e.name, e.unit, e.description, e.incrementBits.get)),
      retirementLatency = retirementLatency)
  }

  /** Attach to the existing exported tile trace without instantiating TracerV.
    * BOOM delays its retirement trace by one cycle; its PMU attachment must
    * supply increments aligned to that observation, including reset handling.
    */
  def apply(tile_trace: testchipip.cosim.TileTraceIO, pmuInputs: IntervalPMUInputs): IntervalTraceBridge = {
    val bridgeKey = key(pmuInputs.manifest, ConvertTraceBundleWidths(tile_trace.traceBundleWidths))
    require(pmuInputs.increments.size == bridgeKey.events.size, "Interval PMU input count mismatch")
    pmuInputs.increments.zip(bridgeKey.events).foreach { case (increment, event) =>
      require(increment.widthOption.contains(event.incrementBits),
        s"Interval PMU input width mismatch for ${event.name}")
    }
    val interval_bridge = withClockAndReset(tile_trace.clock, tile_trace.reset) {
      Module(new IntervalTraceBridge(bridgeKey))
    }
    interval_bridge.io.tiletrace := ConvertTileTraceIO(tile_trace)
    interval_bridge.io.increments.zip(pmuInputs.increments).foreach { case (sink, source) => sink := source }
    interval_bridge
  }
}
