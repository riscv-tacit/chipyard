// See LICENSE for license details.
package firechip.bridgestubs

import chisel3._
import chisel3.util.MixedVec
import chisel3.experimental.{annotate, ChiselAnnotation}
import firrtl.annotations.{ModuleTarget, SingleTargetAnnotation}
import firesim.lib.bridgeutils._
import firechip.bridgeinterfaces._
import pmu.{PMUConsumerAnnotation, PMUManifest, PMUMetadataConsumerAnnotation}

/** Same port structure as the final bridge, with inferred increment widths.
  * Only the target compiler sees this type; Golden Gate receives the final key.
  */
class DeferredIntervalTraceTargetIO(widths: TraceBundleWidths, manifest: PMUManifest) extends Bundle {
  val tiletrace = Input(new TileTraceIO(widths))
  val increments = Input(MixedVec(manifest.events.map(_.incrementBits match {
    case Some(bits) => UInt(bits.W)
    case None => UInt()
  })))
}

final case class FinalizeIntervalPMUAnnotation(
  target: ModuleTarget, source: String, widths: TraceBundleWidths, retirementLatency: Int
) extends SingleTargetAnnotation[ModuleTarget] with PMUMetadataConsumerAnnotation {
  def duplicate(next: ModuleTarget): FinalizeIntervalPMUAnnotation = copy(target = next)
  def finalizePMU(manifests: Map[String, PMUManifest], annotations: firrtl.AnnotationSeq): firrtl.AnnotationSeq = {
    val manifest = manifests.getOrElse(source,
      throw new IllegalArgumentException(s"Interval trace has no finalized PMU catalog for $source"))
    val key = IntervalTraceBridge.key(manifest, widths, retirementLatency)
    val matches = annotations.collect { case a: BridgeAnnotation if a.target == target => a }
    require(matches.size == 1, s"Interval trace $source requires exactly one bridge annotation")
    require(matches.head.widgetConstructorKey.isEmpty, s"Interval trace $source was already finalized")
    annotations.filterNot(_ == this).map {
      case a: BridgeAnnotation if a.target == target => a.copy(widgetConstructorKey = Some(key))
      case other => other
    }
  }
}

class DeferredIntervalTraceBridge(manifest: PMUManifest, widths: TraceBundleWidths, retirementLatency: Int)
    extends BlackBox with Bridge[HostPortIO[DeferredIntervalTraceTargetIO]] {
  require(!manifest.finalized, "Deferred interval attachment expects the live registry catalog")
  require(retirementLatency == 0 || retirementLatency == 1, "Unsupported retirement observation latency")
  val moduleName = "firechip.goldengateimplementations.IntervalTraceBridgeModule"
  val io = IO(new DeferredIntervalTraceTargetIO(widths, manifest))
  val bridgeIO = HostPort(io)
  val constructorArg = None
  generateAnnotations()
  annotate(new ChiselAnnotation {
    def toFirrtl = FinalizeIntervalPMUAnnotation(DeferredIntervalTraceBridge.this.toNamed.toTarget,
      manifest.source, widths, retirementLatency)
  })
  private val identity = s"${manifest.toJson}:$widths:$retirementLatency"
  private val digest = java.security.MessageDigest.getInstance("SHA-256")
    .digest(identity.getBytes(java.nio.charset.StandardCharsets.UTF_8)).map(b => f"${b & 255}%02x").mkString
  override def desiredName: String = s"DeferredIntervalTraceBridge_$digest"
}

object DeferredIntervalTraceBridge {
  def attach(tile_trace: testchipip.cosim.TileTraceIO, manifest: PMUManifest,
             increments: Seq[UInt], retirementLatency: Int): Unit = {
    require(increments.size == manifest.events.size, "Interval PMU input count mismatch")
    val interval_bridge = withClockAndReset(tile_trace.clock, tile_trace.reset) {
      Module(new DeferredIntervalTraceBridge(manifest,
        ConvertTraceBundleWidths(tile_trace.traceBundleWidths), retirementLatency))
    }
    interval_bridge.io.tiletrace := ConvertTileTraceIO(tile_trace)
    interval_bridge.io.increments.zip(increments).foreach { case (sink, source) => sink := source }
    annotate(new ChiselAnnotation {
      def toFirrtl = PMUConsumerAnnotation(manifest.source,
        interval_bridge.io.increments.map(_.toAbsoluteTarget),
        interval_bridge.io.tiletrace.clock.toAbsoluteTarget,
        interval_bridge.io.tiletrace.reset.toAbsoluteTarget)
    })
  }
}
