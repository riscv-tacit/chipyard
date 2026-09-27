package pmu.tilelink

import chisel3._
import org.chipsalliance.cde.config.Parameters
import freechips.rocketchip.diplomacy._
import freechips.rocketchip.tilelink._
import pmu._
import pmu.compiler.PMUCompiler
import java.nio.file.{Files, Paths}

class PMUMMIOTestIO(dataBits: Int) extends Bundle {
  val increment = Input(UInt(64.W))
  val a_valid = Input(Bool())
  val a_ready = Output(Bool())
  val a_write = Input(Bool())
  val a_source = Input(UInt(1.W))
  val a_address = Input(UInt(32.W))
  val a_size = Input(UInt(3.W))
  val a_data = Input(UInt(dataBits.W))
  val a_mask = Input(UInt((dataBits / 8).W))
  val d_valid = Output(Bool())
  val d_ready = Input(Bool())
  val d_source = Output(UInt(1.W))
  val d_data = Output(UInt(dataBits.W))
  val d_denied = Output(Bool())
  val d_corrupt = Output(Bool())
  val enabled = Output(Bool())
  val clear = Output(Bool())
  val snapshot = Output(Bool())
  val live0 = Output(UInt(64.W))
  val live1 = Output(UInt(64.W))
  val captured0 = Output(UInt(64.W))
  val captured1 = Output(UInt(64.W))
  val captured_valid = Output(Bool())
}

class PMUMMIODut(val beatBytes: Int)(implicit p: Parameters) extends LazyModule {
  val client = TLClientNode(Seq(TLMasterPortParameters.v1(Seq(
    TLMasterParameters.v1(name = "pmu-test", sourceId = IdRange(0, 2))))))
  val source = s"bus_${beatBytes * 8}"
  val adapter = new PMUTileLink(BigInt("40000000", 16), beatBytes, source)
  adapter.node := client
  lazy val module = new PMUMMIODutModule(this)
}

class PMUMMIODutModule(outer: PMUMMIODut) extends LazyModuleImp(outer) {
    val beatBytes = outer.beatBytes
    val io = IO(new PMUMMIOTestIO(beatBytes * 8))
    val registry = new PMURegistry(outer.source)
    val increment = WireInit(io.increment)
    val cycles = WireInit(1.U)
    registry.register("events", increment)
    registry.register("cycles", cycles, unit = "cycles")
    val events = registry.build()
    val bank = TilePMU.build(events)
    outer.adapter.attach(bank)
    io.enabled := bank.enable
    io.clear := bank.clear
    io.snapshot := bank.snapshot
    io.live0 := bank.values(0); io.live1 := bank.values(1)
    io.captured0 := bank.snapshot_values(0); io.captured1 := bank.snapshot_values(1)
    io.captured_valid := bank.snapshot_valid

    val (tl, edge) = outer.client.out(0)
    tl.a.valid := io.a_valid
    tl.a.bits := 0.U.asTypeOf(tl.a.bits)
    tl.a.bits.opcode := Mux(io.a_write, TLMessages.PutPartialData, TLMessages.Get)
    tl.a.bits.source := io.a_source
    tl.a.bits.address := io.a_address
    tl.a.bits.size := io.a_size
    tl.a.bits.mask := io.a_mask
    tl.a.bits.data := io.a_data
    io.a_ready := tl.a.ready
    tl.d.ready := io.d_ready
    io.d_valid := tl.d.valid
    io.d_source := tl.d.bits.source
    io.d_data := tl.d.bits.data
    io.d_denied := tl.d.bits.denied
    io.d_corrupt := tl.d.bits.corrupt
    tl.b.ready := true.B
    tl.c.valid := false.B; tl.c.bits := 0.U.asTypeOf(tl.c.bits)
    tl.e.valid := false.B; tl.e.bits := 0.U.asTypeOf(tl.e.bits)
}

class PMUMMIOTestTop extends Module {
  implicit val p: Parameters = Parameters.empty
  val dut32 = Module(LazyModule(new PMUMMIODut(4)).module)
  val dut64 = Module(LazyModule(new PMUMMIODut(8)).module)
  val io32 = IO(new PMUMMIOTestIO(32))
  val io64 = IO(new PMUMMIOTestIO(64))
  io32 <> dut32.io
  io64 <> dut64.io
}

object PMUMMIOTest {
  def main(args: Array[String]): Unit = {
    val elaborated = new chisel3.stage.phases.Elaborate().transform(
      Seq(chisel3.stage.ChiselGeneratorAnnotation(() => new PMUMMIOTestTop)))
    val annotations = new chisel3.stage.phases.Convert().transform(elaborated)
    val checked = PMUCompiler.validate(PMUCompiler.fromChisel(annotations))
    Files.writeString(Paths.get(args.head).resolve("PMUMMIOTestTop.v"), PMUCompiler.emitNative(checked))
    println("Emitted validated PMU banks with real 32- and 64-bit TileLink register adapters")
  }
}
