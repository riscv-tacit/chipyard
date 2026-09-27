package pmu.tilelink

import chisel3._
import chisel3.util.Cat
import org.chipsalliance.cde.config.Parameters
import freechips.rocketchip.diplomacy.{AddressSet, Description, ResourceBindings, ResourceString, SimpleDevice}
import freechips.rocketchip.regmapper.{RegField, RegFieldDesc, RegWriteFn}
import freechips.rocketchip.tilelink.TLRegisterNode
import pmu.PMUAttachment

/** Construct in the LazyModule that owns the PMU; attach in its module body after
  * TilePMU.build(events). The register mapper and bank must share clock and reset.
  * This avoids finalizing a registry during early lazy-child elaboration.
  */
final class PMUTileLink(address: BigInt, beatBytes: Int, source: String)(implicit p: Parameters) {
  require(address >= 0 && address % 4096 == 0, "PMU MMIO address must be nonnegative and 4 KiB aligned")
  require(beatBytes == 4 || beatBytes == 8, "PMU MMIO supports 32- or 64-bit register buses")
  private var attached = false
  val device = new SimpleDevice("pmu", Seq("ucbbar,pmu-v1")) {
    override def describe(resources: ResourceBindings): Description = {
      val Description(name, mapping) = super.describe(resources)
      Description(name, mapping + ("pmu,source" -> Seq(ResourceString(source))))
    }
  }
  val node = TLRegisterNode(Seq(AddressSet(address, 4095)), device,
    beatBytes = beatBytes, concurrency = 1)

  def attach(bank: PMUAttachment): Unit = {
    require(!attached, "PMU MMIO may be attached only once")
    require(bank.manifest.source == source, "PMU MMIO source does not match its bank")
    require(bank.manifest.counterBits <= 64, "PMU MMIO v1 supports counters up to 64 bits")
    val count = bank.manifest.events.size
    require(0x100 + 8 * count <= 4096, "PMU counter array exceeds its MMIO aperture")
    attached = true
    val enabled = RegInit(false.B)
    bank.enable := enabled
    bank.clear := false.B
    bank.snapshot := false.B
    val command = RegWriteFn((valid: Bool, data: UInt) => {
      // RegMapper gates valid by the addressed byte mask and response handshake.
      // A queued or backpressured request cannot repeat these one-cycle commands.
      bank.clear := valid && data(0)
      bank.snapshot := valid && data(1)
      true.B
    })
    val control = Seq(RegField(1, enabled,
      RegFieldDesc("enable", "Accumulate while set; reset disables counting")))
    val commands = Seq(RegField.w(2, command,
      RegFieldDesc("command", "Write one: bit 0 clear, bit 1 snapshot; both capture then clear; reads zero")))
    val status = Seq(RegField.r(2, Cat(bank.snapshot_valid, enabled),
      RegFieldDesc("status", "Bit 0 enabled; bit 1 snapshot valid")))
    val info = Seq(
      RegField.r(16, 1.U(16.W), RegFieldDesc("version", "PMU MMIO ABI version")),
      RegField.r(16, count.U(16.W), RegFieldDesc("count", "Number of counters")),
      RegField.r(16, bank.manifest.counterBits.U(16.W), RegFieldDesc("width", "Counter width in bits")))
    // Split each value into 32-bit fields so both register bus widths work.
    // Snapshot storage remains stable until another snapshot or hardware reset.
    val counters = bank.manifest.events.map { e =>
      val value = bank.snapshot_values(e.id).pad(64)
      (0x100 + 8 * e.id) -> Seq(
        RegField.r(32, value(31, 0), RegFieldDesc(s"counter_${e.id}_lo", s"${e.name}: low word; ${e.description}")),
        RegField.r(32, value(63, 32), RegFieldDesc(s"counter_${e.id}_hi", s"${e.name}: high word")))
    }
    node.regmap((Seq(0x00 -> control, 0x08 -> commands, 0x10 -> status, 0x18 -> info) ++ counters): _*)
  }
}
