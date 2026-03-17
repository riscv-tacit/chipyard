package firechip.bridgeinterfaces

import chisel3._
import chisel3.util._

object TraceEgressRawByteConstants {
  val numLanes = 4
}

class TraceEgressRawByteInterface extends Bundle {
  val bits = Output(Vec(TraceEgressRawByteConstants.numLanes, UInt(8.W)))
  val mask = Output(Vec(TraceEgressRawByteConstants.numLanes, Bool()))
  val valid = Output(Bool())
  val ready = Input(Bool())
  def fire = valid && ready
}

class TraceRawBytePortIO extends Bundle {
  val out = new TraceEgressRawByteInterface
}

class TraceRawByteBridgeTargetIO extends Bundle {
  val byte = Flipped(new TraceRawBytePortIO)
  val reset = Input(Bool())
  val clock = Input(Clock())
}

case class TraceRawByteKey() // intentionally empty