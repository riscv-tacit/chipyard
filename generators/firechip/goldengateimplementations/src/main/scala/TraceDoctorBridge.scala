// See LICENSE for license details
// Ported from firesim/firesim#1501 (TraceDoctor, EECS-NTNU).

package firechip.goldengateimplementations

import chisel3._
import chisel3.util._

import org.chipsalliance.cde.config.Parameters
import freechips.rocketchip.util._

import midas.widgets._
import firesim.lib.bridgeutils._

import firechip.bridgeinterfaces._

class TraceDoctorBridgeModule(key: TraceDoctorKey)(implicit p: Parameters)
    extends BridgeModule[HostPortIO[TraceDoctorBridgeTargetIO]]()(p)
    with StreamToHostCPU {

  // Match TracerV's stream depth.
  val toHostCPUQueueDepth = 6144

  lazy val module = new BridgeModuleImp(this) {
    val io    = IO(new WidgetIO)
    val hPort = IO(HostPort(new TraceDoctorBridgeTargetIO(key.traceWidth)))

    // One stream beat per trace token. Multi-beat tokens were prototyped in
    // the original PR but showed FPGA timing problems; widen here only with
    // care.
    require(
      key.traceWidth <= BridgeStreamConstants.streamWidthBits,
      s"TraceDoctor trace width (${key.traceWidth}) must fit a single " +
        s"${BridgeStreamConstants.streamWidthBits}b stream beat",
    )

    // Register order defines the TRACEDOCTORBRIDGEMODULE_struct layout on
    // the driver side; keep in sync with tracedoctor.h.
    val initDone    = genWORegInit(Wire(Bool()), "initDone", false.B)
    val traceEnable = genWORegInit(Wire(Bool()), "traceEnable", false.B)

    val triggerSelector = RegInit(0.U((p(CtrlNastiKey).dataBits).W))
    attach(triggerSelector, "triggerSelector", WriteOnly)

    // Mask off tokens produced while the target is under reset.
    val traceValid = hPort.hBits.trace.valid && !hPort.hBits.reset

    val trigger = MuxLookup(
      triggerSelector,
      false.B,
      Seq(
        0.U -> true.B,
        1.U -> hPort.hBits.tracerVTrigger,
      ),
    )

    val traceOut = traceEnable && traceValid && trigger

    // Accept one target token per host transaction; enqueue a stream beat
    // only on cycles carrying a valid, triggered trace vector.
    val commonPredicates = Seq(hPort.toHost.hValid, hPort.fromHost.hReady, streamEnq.ready, initDone)
    val do_fire_helper   = DecoupledHelper(commonPredicates: _*)

    hPort.toHost.hReady   := do_fire_helper.fire(hPort.toHost.hValid)
    hPort.fromHost.hValid := do_fire_helper.fire(hPort.fromHost.hReady)

    streamEnq.valid := do_fire_helper.fire(streamEnq.ready) && traceOut
    streamEnq.bits  := hPort.hBits.trace.bits.pad(BridgeStreamConstants.streamWidthBits)

    genCRFile()

    override def genHeader(base: BigInt, memoryRegions: Map[String, BigInt], sb: StringBuilder): Unit = {
      genConstructor(
        base,
        sb,
        "tracedoctor_t",
        "tracedoctor",
        Seq(
          UInt32(toHostStreamIdx),
          UInt32(toHostCPUQueueDepth),
          UInt32(BridgeStreamConstants.streamWidthBits),
          UInt32(key.traceWidth),
          Verbatim(clockDomainInfo.toC),
        ),
        hasStreams = true,
      )
    }
  }
}
