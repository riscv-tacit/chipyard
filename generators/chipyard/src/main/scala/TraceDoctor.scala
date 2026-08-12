// See LICENSE for license details
// TraceDoctor oracle trace plumbing: collects per-BOOM-tile trace vectors
// and exposes them at the subsystem boundary (mirrors the pattern of
// tacit.CanHaveTraceSinkRawByte).

package chipyard

import chisel3._

import org.chipsalliance.cde.config.{Config}
import freechips.rocketchip.diplomacy._
import freechips.rocketchip.subsystem._

import boom.v3.common.{BoomTile, BoomTileAttachParams, BoomTraceDoctorIO}

// Enable the TraceDoctor oracle port on all BOOM v3 tiles
class WithBoomTraceDoctor(width: Int = 512) extends Config((site, here, up) => {
  case TilesLocated(InSubsystem) => up(TilesLocated(InSubsystem), site) map {
    case tp: BoomTileAttachParams =>
      tp.copy(tileParams = tp.tileParams.copy(
        core = tp.tileParams.core.copy(traceDoctorWidth = width)))
    case other => other
  }
})

trait CanHaveTraceDoctorIO { this: BaseSubsystem =>
  require(this.isInstanceOf[BaseSubsystem with InstantiatesHierarchicalElements])
  private val hierarchicalSubsystem =
    this.asInstanceOf[BaseSubsystem with InstantiatesHierarchicalElements]

  val traceDoctorTiles: Seq[BoomTile] =
    hierarchicalSubsystem.totalTiles.values.collect {
      case b: BoomTile if b.traceDoctorNode.isDefined => b
    }.toSeq

  // (traceWidth, per-tile module IO)
  val traceDoctorIOs = traceDoctorTiles.map { t =>
    val width = t.boomParams.core.traceDoctorWidth
    val sink = BundleBridgeSink[BoomTraceDoctorIO]()
    sink := t.traceDoctorNode.get
    val io = InModuleBody {
      val td = IO(Output(new BoomTraceDoctorIO(width))).suggestName(s"tracedoctor_${t.tileId}")
      td := sink.bundle
      td
    }
    (width, io)
  }
}
