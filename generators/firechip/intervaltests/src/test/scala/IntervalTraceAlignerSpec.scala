// See LICENSE for license details.
package firechip.goldengateimplementations

import chisel3._
import chiseltest._
import firechip.bridgeinterfaces._
import org.scalatest.flatspec.AnyFlatSpec

class IntervalTraceAlignerSpec extends AnyFlatSpec with ChiselScalatestTester {
  private def key(latency: Int) = IntervalTraceBridgeKey("tile",
    TraceBundleWidths(1, 40, 32, None, 64, 40),
    Seq(IntervalPMUEvent(0, "retired", "instructions", "Retirements")), retirementLatency = latency)

  private def drive(dut: IntervalTraceAligner, increment: Int, targetReset: Boolean, pc: Int): Unit = {
    dut.io.in.valid.poke(true.B)
    dut.io.in.bits.increments(0).poke(increment.U)
    dut.io.in.bits.target_reset.poke(targetReset.B)
    dut.io.in.bits.trace.time.poke(0.U)
    val slot = dut.io.in.bits.trace.retiredinsns.head
    slot.valid.poke(true.B); slot.iaddr.poke(pc.U); slot.insn.poke(0x13.U); slot.priv.poke(3.U)
    slot.exception.poke(false.B); slot.interrupt.poke(false.B); slot.cause.poke(0.U); slot.tval.poke(0.U)
  }

  "Retirement alignment" should "advance on accepted target cycles and preserve the pre-reset retirement" in {
    test(new IntervalTraceAligner(key(1), incrementBits = Some(3))) { dut =>
      // First token has no prior PMU sample; it only seeds the alignment stage.
      dut.io.out.ready.poke(false.B)
      drive(dut, 2, false, 0)
      dut.io.in.ready.expect(true.B); dut.io.out.valid.expect(false.B)
      dut.clock.step()
      // Raw reset asserts, but this trace is the final pre-reset retirement.
      drive(dut, 7, true, 0x100)
      for (_ <- 0 until 5) {
        dut.io.in.ready.expect(false.B); dut.io.out.valid.expect(true.B)
        dut.io.out.bits.increments(0).expect(2.U)
        dut.io.out.bits.target_reset.expect(false.B)
        dut.io.out.bits.trace.retiredinsns.head.iaddr.expect(0x100.U)
        dut.clock.step()
      }
      dut.io.out.ready.poke(true.B); dut.clock.step()
      // Deassertion still carries a reset-cycle trace and must be excluded.
      drive(dut, 3, false, 0x104)
      dut.io.out.bits.increments(0).expect(7.U)
      dut.io.out.bits.target_reset.expect(true.B)
      dut.clock.step()
      // No valid target token: neither reset nor increments may advance.
      drive(dut, 6, true, 0x108)
      dut.io.in.valid.poke(false.B)
      dut.io.out.valid.expect(false.B)
      dut.clock.step(3)
      drive(dut, 1, false, 0x10c)
      dut.io.out.bits.increments(0).expect(3.U)
      dut.io.out.bits.target_reset.expect(false.B)
      dut.io.out.bits.trace.retiredinsns.head.iaddr.expect(0x10c.U)
      dut.clock.step()
    }
  }

  it should "pass through a producer with no retirement latency" in {
    test(new IntervalTraceAligner(key(0), incrementBits = Some(3))) { dut =>
      drive(dut, 4, true, 0x100)
      dut.io.out.ready.poke(false.B)
      dut.io.in.ready.expect(false.B); dut.io.out.valid.expect(true.B)
      dut.io.out.bits.increments(0).expect(4.U); dut.io.out.bits.target_reset.expect(true.B)
      dut.io.out.ready.poke(true.B)
      dut.io.in.ready.expect(true.B)
      dut.clock.step()
    }
  }

  for (latency <- Seq(0, 1)) {
    it should s"discard HostPort initialization before aligning latency $latency" in {
      test(new IntervalTraceAligner(key(latency), discardInitialToken = true, incrementBits = Some(3))) { dut =>
        dut.io.out.ready.poke(false.B)
        drive(dut, 0, false, 0)
        dut.io.in.valid.poke(false.B)
        dut.clock.step(3) // Idle FPGA cycles must not consume the initial token.
        dut.io.out.valid.expect(false.B)
        dut.io.in.valid.poke(true.B)
        dut.io.in.ready.expect(true.B); dut.io.out.valid.expect(false.B)
        dut.clock.step() // Drop the synthetic all-zero channel token.
        drive(dut, 1, true, 0x100)
        if (latency == 1) {
          dut.io.in.ready.expect(true.B); dut.io.out.valid.expect(false.B)
          dut.clock.step() // Seed PMU/reset history with the first real sample.
          drive(dut, 2, false, 0x104)
        }
        for (_ <- 0 until 3) {
          dut.io.in.ready.expect(false.B); dut.io.out.valid.expect(true.B)
          dut.io.out.bits.target_reset.expect(true.B)
          dut.io.out.bits.increments(0).expect(1.U)
          dut.clock.step()
        }
        dut.io.out.ready.poke(true.B)
        dut.clock.step()
        drive(dut, 3, false, 0x108)
        dut.io.out.valid.expect(true.B)
        dut.io.out.bits.target_reset.expect(false.B)
        dut.io.out.bits.increments(0).expect((if (latency == 1) 2 else 3).U)
      }
    }
  }

}
