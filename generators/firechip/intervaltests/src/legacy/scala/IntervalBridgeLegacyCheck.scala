import chisel3._
import org.chipsalliance.cde.config.Parameters
import freechips.rocketchip.diplomacy.LazyModule
import firesim.lib.bridgeutils.{BridgeAnnotation, RationalClock}
import firesim.lib.nasti.NastiParameters
import midas.widgets.{CtrlNastiKey, TargetClockInfo}
import firechip.bridgeinterfaces._
import firechip.goldengateimplementations._
import java.nio.file.{Files, Paths}

object IntervalBridgeLegacyCheck {
  def main(args: Array[String]): Unit = {
    implicit val p: Parameters = Parameters.empty.alterPartial {
      case CtrlNastiKey => NastiParameters(32, 32, 1)
      case TargetClockInfo => Some(RationalClock("tile", 1, 1))
    }
    // Import a real target-side annotation, then elaborate its actual host class.
    val annotations = firrtl.annotations.JsonProtocol.deserialize(Files.readString(Paths.get(args(0))))
    val key = annotations.collect { case a: BridgeAnnotation if a.widgetClass == "firechip.goldengateimplementations.IntervalTraceBridgeModule" =>
      a.widgetConstructorKey.get.asInstanceOf[IntervalTraceBridgeKey] }.head
    val widget = LazyModule(new IntervalTraceBridgeModule(key))
    new chisel3.stage.ChiselStage().emitVerilog(widget.module, Array("--target-dir", args(1)))
    val header = new StringBuilder
    widget.module.genHeader(0, Map.empty, header)
    Files.writeString(Paths.get(args(1), "intervaltrace_generated.h"), header.toString)
    println("PASS: Chisel 3 full widget elaboration, Verilog and driver header generation from Chisel 6 annotation")
  }
}
