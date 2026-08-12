// See LICENSE for license details
// Ported from firesim/firesim#1501 (TraceDoctor, EECS-NTNU).

package firechip.bridgestubs

import chisel3._

import org.chipsalliance.cde.config.Parameters

import firesim.lib.bridgeutils._

import firechip.bridgeinterfaces._

class TraceDoctorBridge(traceWidth: Int) extends BlackBox
    with Bridge[HostPortIO[TraceDoctorBridgeTargetIO]] {
  require(traceWidth > 0, "TraceDoctorBridge: trace width must be larger than 0")

  val moduleName = "firechip.goldengateimplementations.TraceDoctorBridgeModule"

  val io = IO(new TraceDoctorBridgeTargetIO(traceWidth))
  val bridgeIO = HostPort(io)

  val constructorArg = Some(TraceDoctorKey(traceWidth))

  generateAnnotations()

  // Uniquify blackbox defnames by width so differently-sized instances in a
  // heterogeneous config do not collide (see TracerVBridge for background).
  override def desiredName = super.desiredName + s"_w${traceWidth}"
}

object TraceDoctorBridge {
  def apply(clock: Clock, trace: TraceDoctorTraceIO, reset: Bool, tracerVTrigger: Bool)(implicit
    p: Parameters
  ): TraceDoctorBridge = {
    val ep = Module(new TraceDoctorBridge(trace.traceWidth))
    ep.io.trace          := trace
    ep.io.clock          := clock
    ep.io.reset          := reset
    ep.io.tracerVTrigger := tracerVTrigger
    ep
  }
}
