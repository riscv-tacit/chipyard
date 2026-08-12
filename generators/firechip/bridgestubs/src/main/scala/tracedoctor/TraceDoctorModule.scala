// See LICENSE for license details.

package firechip.bridgestubs

import chisel3._

import org.chipsalliance.cde.config.{Config, Field, Parameters}

import firechip.bridgeinterfaces._

case object TraceDoctorModuleTraceWidth extends Field[Int]

class TraceDoctorModuleTraceWidthConfig(w: Int) extends Config((site, here, up) => {
  case TraceDoctorModuleTraceWidth => w
})

class TraceDoctorModuleTraceWidth512 extends TraceDoctorModuleTraceWidthConfig(512)
class TraceDoctorModuleTraceWidth256 extends TraceDoctorModuleTraceWidthConfig(256)

// The trace vector is poked from the metasim test driver as 64-bit lanes.
class TraceDoctorDUTIO(val traceWidth: Int) extends Bundle {
  val traceData  = Input(Vec(traceWidth / 64, UInt(64.W)))
  val traceValid = Input(Bool())
}

class TraceDoctorDUT(implicit val p: Parameters) extends Module {
  val traceWidth = p(TraceDoctorModuleTraceWidth)
  require(traceWidth % 64 == 0, "test DUT pokes the trace vector as 64b lanes")

  val io = IO(new TraceDoctorDUTIO(traceWidth))

  val trace = Wire(new TraceDoctorTraceIO(traceWidth))
  trace.valid := io.traceValid
  trace.bits  := io.traceData.asUInt

  val bridge = TraceDoctorBridge(clock, trace, reset.asBool, true.B)
}

class TraceDoctorModule(implicit p: Parameters)
    extends firesim.lib.testutils.PeekPokeHarness(() => new TraceDoctorDUT)
