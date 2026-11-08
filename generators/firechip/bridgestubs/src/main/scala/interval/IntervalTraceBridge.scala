// See LICENSE for license details.

package firechip.bridgestubs

import chisel3._
import firesim.lib.bridgeutils._
import firechip.bridgeinterfaces._
import pmu.PMUCatalog

/** Target endpoint for one tile's trace and PMU increments. Increment ports are
  * unsized; width inference sizes them from the connected increments.
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
    Seq(event.id.toString, event.name, event.unit, event.description, event.source, event.sourceEvent)
  } ++ key.stallEvent.map("stall:" + _) ++ key.sharedSources.map("shared:" + _)
  private val identity = identityFields.map(s => s"${s.length}:$s").mkString
  private val digest = java.security.MessageDigest.getInstance("SHA-256")
    .digest(identity.getBytes(java.nio.charset.StandardCharsets.UTF_8))
    .map(b => f"${b & 0xff}%02x").mkString
  override def desiredName: String = s"IntervalTraceBridge_$digest"
}

object IntervalTraceBridge {
  /** Key for a tile's merged event list (PMUCatalog.merge). stallEvent names the
    * event whose cycles are reported per block as stall cycles (see IntervalTraceBridgeKey).
    */
  def key(catalog: PMUCatalog, traceWidths: TraceBundleWidths, retirementLatency: Int = 0,
          stallEvent: Option[String] = None): IntervalTraceBridgeKey =
    IntervalTraceBridgeKey(catalog.sources.head.name, traceWidths,
      catalog.events.map(e => IntervalPMUEvent(e.id, e.name, e.unit, e.description, e.source, e.sourceEvent)),
      retirementLatency = retirementLatency, stallEvent = stallEvent,
      sharedSources = catalog.sources.tail.map(_.name))

  /** Attach to the existing exported tile trace without instantiating TracerV.
    * Increments are in catalog-ID order, in the tile trace's clock domain.
    * retirementLatency is the number of cycles by which the trace's retirement
    * lags the PMU increments; the FPGA widget aligns them. stallEvent must name
    * a one-bit event of unit cycles.
    */
  def apply(tile_trace: testchipip.cosim.TileTraceIO, catalog: PMUCatalog, increments: Seq[UInt],
            retirementLatency: Int, stallEvent: Option[String] = None): IntervalTraceBridge = {
    val bridgeKey = key(catalog, ConvertTraceBundleWidths(tile_trace.traceBundleWidths), retirementLatency, stallEvent)
    require(increments.size == bridgeKey.events.size, "Interval PMU input count mismatch")
    // Widths are known here, before the bridge's unsized ports hide them.
    bridgeKey.stallIndex.foreach { index =>
      require(increments(index).widthOption.contains(1),
        s"Interval stall event '${stallEvent.get}' must be one bit wide, not ${increments(index).widthOption.getOrElse("unknown")}")
    }
    val interval_bridge = withClockAndReset(tile_trace.clock, tile_trace.reset) {
      Module(new IntervalTraceBridge(bridgeKey))
    }
    interval_bridge.io.tiletrace := ConvertTileTraceIO(tile_trace)
    interval_bridge.io.increments.zip(increments).foreach { case (sink, source) => sink := source }
    interval_bridge
  }
}
