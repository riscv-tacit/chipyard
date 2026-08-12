// See LICENSE for license details.

package firechip.bridgeinterfaces

import chisel3._

// A single wide trace vector sampled every target cycle. What the bits mean
// is a contract between the target-side producer (e.g. a core's TraceDoctor
// port) and the host-side worker that parses the tokens; the bridge itself
// is payload-agnostic.
class TraceDoctorTraceIO(val traceWidth: Int) extends Bundle {
  val valid = Bool()
  val bits  = UInt(traceWidth.W)
}

class TraceDoctorBridgeTargetIO(val traceWidth: Int) extends Bundle {
  val clock          = Input(Clock())
  val reset          = Input(Bool())
  val trace          = Input(new TraceDoctorTraceIO(traceWidth))
  // Global trigger state, wired via midas TriggerSink on the target side.
  // Consulted only when the driver selects the 'tracerv' trigger mode.
  val tracerVTrigger = Input(Bool())
}

case class TraceDoctorKey(traceWidth: Int)
