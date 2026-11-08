// See LICENSE for license details.
package firechip.goldengateimplementations

import chisel3._
import chiseltest._
import firechip.bridgeinterfaces._
import org.scalatest.flatspec.AnyFlatSpec
import scala.collection.mutable.ArrayBuffer
import scala.util.Random
import java.nio.file.{Files, Paths}

class IntervalTraceDatapathSpec extends AnyFlatSpec with ChiselScalatestTester {
  import IntervalTraceFormat._
  private val mask64 = (BigInt(1) << 64) - 1
  private case class Slot(pc: BigInt, insn: BigInt = 0x13, priv: Int = 3)
  private case class Cycle(slots: Seq[Option[Slot]], increments: Seq[BigInt], trap: Boolean = false, reset: Boolean = false)
  private case class Record(kind: Int, flags: Int, words: Vector[BigInt])
  // stall: event ID of a cycle event named as the key's stall event.
  private def key(lanes: Int, counters: Int, stall: Option[Int] = None): IntervalTraceBridgeKey = IntervalTraceBridgeKey("test_tile",
    TraceBundleWidths(lanes, 40, 32, None, 64, 40),
    (0 until counters).map(i => IntervalPMUEvent(i, s"event_$i", if (stall.contains(i)) "cycles" else "events", "Test event",
      "test_tile", s"event_$i")),
    stallEvent = stall.map(i => s"event_$i"))

  private def decode(beats: Seq[BigInt], n: Int, bbCount: Int = 4): Vector[Record] = {
    val words = beats.flatMap(b => (0 until 8).map(i => (b >> (64 * i)) & mask64))
    val records = ArrayBuffer.empty[Record]
    var offset = 0
    while (offset < words.size) {
      val h = words(offset)
      val kind = (h & 255).toInt
      assert(((h >> 8) & 255) == version)
      val count = (h >> 32).toInt
      assert(count == (if (kind == pmu) n + 4 else if (kind == bb) bbCount else 4))
      val padded = ((1 + count + 7) / 8) * 8
      assert(offset + padded <= words.size, "truncated record")
      assert(words.slice(offset + 1 + count, offset + padded).forall(_ == 0), "nonzero padding")
      records += Record(kind, ((h >> 16) & 65535).toInt, words.slice(offset + 1, offset + 1 + count).toVector)
      offset += padded
    }
    records.toVector
  }

  private class Driver(dut: IntervalTraceDatapath, bridgeKey: IntervalTraceBridgeKey, stallSeed: Int) {
    val beats = ArrayBuffer.empty[BigInt]
    private val random = new Random(stallSeed)
    private var stalled: Option[BigInt] = None
    private var steps = 0
    dut.io.capture_enable.poke(false.B)
    dut.io.trigger_selector.poke(0.U)
    dut.io.trigger_start.poke("hffffffff00008013".U)
    dut.io.trigger_end.poke("hffffffff00010013".U)
    dut.io.nominal_interval.poke(1.U)
    dut.io.target.valid.poke(false.B)
    dut.io.target.bits.target_reset.poke(false.B)
    dut.io.target.bits.trace.time.poke(0.U)
    dut.io.target.bits.increments.foreach(_.poke(0.U))
    dut.io.target.bits.trace.retiredinsns.foreach { t =>
      t.valid.poke(false.B); t.iaddr.poke(0.U); t.insn.poke(0.U); t.priv.poke(0.U)
      t.exception.poke(false.B); t.interrupt.poke(false.B); t.cause.poke(0.U); t.tval.poke(0.U)
    }
    def tick(forceReady: Option[Boolean] = None): Unit = {
      steps += 1
      assert(steps < 30000, "datapath stopped making progress")
      val ready = forceReady.getOrElse(random.nextInt(4) == 0)
      dut.io.stream.ready.poke(ready.B)
      val valid = dut.io.stream.valid.peek().litToBoolean
      val bits = dut.io.stream.bits.peek().litValue
      stalled.foreach { previous => assert(valid && bits == previous, "stalled beat changed") }
      stalled = if (valid && !ready) Some(bits) else None
      if (valid && ready) beats += bits
      dut.clock.step()
    }
    def start(length: Int): Unit = {
      dut.io.nominal_interval.poke(length.U)
      dut.io.capture_enable.poke(true.B)
    }
    def send(c: Cycle): Unit = {
      dut.io.target.bits.target_reset.poke(c.reset.B)
      dut.io.target.bits.trace.time.poke(999.U) // deliberately unrelated to elapsed target cycles
      dut.io.target.bits.trace.retiredinsns.zipWithIndex.foreach { case (t, i) =>
        val s = c.slots.lift(i).flatten
        t.valid.poke(s.nonEmpty.B)
        t.iaddr.poke((s.map(_.pc).getOrElse(BigInt(0)) & ((BigInt(1) << 40) - 1)).U)
        t.insn.poke(s.map(_.insn).getOrElse(BigInt(0)).U)
        t.priv.poke(s.map(_.priv).getOrElse(3).U)
        t.exception.poke((c.trap && i == 0).B)
      }
      dut.io.target.bits.increments.zip(c.increments).foreach { case (wire, value) => wire.poke(value.U) }
      dut.io.target.valid.poke(true.B)
      while (!dut.io.target.ready.peek().litToBoolean) tick()
      tick()
      dut.io.target.valid.poke(false.B)
    }
    def stop(fixture: String = s"rtl-${bridgeKey.events.size}"): Unit = {
      dut.io.capture_enable.poke(false.B)
      dut.io.target.valid.poke(false.B)
      while (!dut.io.drained.peek().litToBoolean) tick()
      // Catch an incorrectly early drained indication.
      tick(Some(true)); tick(Some(true))
      val out = Paths.get("generators/firechip/intervaltests/target")
      Files.createDirectories(out)
      val bytes = beats.flatMap(b => (0 until 64).map(i => ((b >> (8 * i)) & 255).toByte)).toArray
      Files.write(out.resolve(s"$fixture.bin"), bytes)
      Files.writeString(out.resolve(s"$fixture.catalog"), catalogId(bridgeKey).toString)
      bridgeKey.stallIndex.foreach(i => Files.writeString(out.resolve(s"$fixture.stall"), i.toString))
    }
  }

  // Software uses absolute nominal endpoints and a list of pending instructions,
  // independently of the RTL's prefix/debt state and sparse batch storage.
  private def reference(input: Seq[Cycle], k: IntervalTraceBridgeKey, length: Int): Vector[Record] = {
    val out = ArrayBuffer(Record(epochStart, 0, Vector(BigInt(1), BigInt(length), BigInt(k.events.size), catalogId(k))))
    var counters = Vector.fill(k.events.size)(BigInt(0))
    def snapshot(id: Int, retired: Int, cycle: Int, groupEnd: Int, flags: Int): Record =
      Record(pmu, flags, Vector(BigInt(id), BigInt(retired), BigInt(cycle), BigInt(groupEnd)) ++ counters)
    out += snapshot(0, 0, 0, 0, baseline)
    var pending = Vector.empty[Slot]
    var partial = true
    var previousPriv: Option[Int] = None
    var retired = 0
    var cycle = 0
    var lastBoundaryCycle = 0
    var lastBoundaryStall = BigInt(0)
    var nextThreshold = length
    var id = 0
    def sext(pc: BigInt): BigInt = if (pc.testBit(39)) pc | (mask64 ^ ((BigInt(1) << 40) - 1)) else pc
    def block(extra: Int): Record = {
      // Stall cycles since the previous boundary, from the cumulative stall counter.
      val stall = k.stallIndex.map(i => (counters(i) - lastBoundaryStall) & mask64)
      val r = Record(bb, extra | (if (partial) partialStart else 0) | (pending.head.priv << privilegeShift),
        Vector(sext(pending.head.pc), sext(pending.last.pc), BigInt(pending.size), BigInt(cycle - lastBoundaryCycle)) ++ stall)
      pending = Vector.empty
      lastBoundaryCycle = cycle
      k.stallIndex.foreach(i => lastBoundaryStall = counters(i))
      partial = false
      r
    }
    def terminates(word: BigInt): Boolean = {
      val x = word.toLong
      if ((x & 3) != 3) {
        ((x & 3) == 1 && Set(5L, 6L, 7L)((x >> 13) & 7)) ||
          ((x & 3) == 2 && ((x >> 13) & 7) == 4 && ((x >> 7) & 31) != 0 && ((x >> 2) & 31) == 0)
      } else Set(0x63L, 0x6fL, 0x67L)(x & 127) || Set(0x30200073L, 0x10200073L)(x)
    }
    input.foreach { c =>
      assert(!c.reset)
      cycle += 1
      counters = counters.zip(c.increments).map { case (a, b) => (a + b) & mask64 }
      val blocks = ArrayBuffer.empty[Record]
      val snapshots = ArrayBuffer.empty[Record]
      val groupEnd = retired + c.slots.flatten.size
      c.slots.flatten.foreach { s =>
        val privChange = previousPriv.exists(_ != s.priv)
        val gap = pending.lastOption.exists(p => p.pc + (if ((p.insn & 3) == 3) 4 else 2) != s.pc)
        if (pending.nonEmpty && (gap || privChange)) {
          blocks += block(partialEnd | (if (privChange) privilegeChange else discontinuity))
          partial = true
        }
        if (privChange) partial = true
        previousPriv = Some(s.priv)
        pending :+= s
        retired += 1
        if (terminates(s.insn)) {
          val boundary = retired >= nextThreshold
          if (boundary) {
            nextThreshold += length // exactly one adjustment, even for a very long block
            id += 1
            snapshots += snapshot(id, retired, cycle, groupEnd, 0)
          }
          blocks += block(if (boundary) terminateInterval else 0)
        }
      }
      if (c.trap) {
        if (pending.nonEmpty) blocks += block(partialEnd | trap)
        partial = true
      }
      out ++= snapshots
      out ++= blocks
    }
    id += 1
    out += snapshot(id, retired, cycle, retired, finalSnapshot)
    if (pending.nonEmpty) out += block(partialEnd)
    out += Record(epochEnd, 0, Vector(1, retired, cycle, id).map(BigInt(_)))
    out.toVector
  }

  "Instruction-triggered capture" should "exclude boot and shutdown, preserve marker cycles and restart under backpressure" in {
    val k = key(4, 3)
    def cycle(slots: Option[Slot]*): Cycle = Cycle(slots.toSeq, Seq(BigInt(slots.flatten.size), 1, 7))
    val boot = cycle(Some(Slot(0x1000)), Some(Slot(0x1004, 0x10013)))
    val begin = cycle(Some(Slot(0x2000)), None, Some(Slot(0x2004, 0x8013)), Some(Slot(0x2008)))
    val work = cycle(Some(Slot(0x200c, 0x63)), Some(Slot(0x2010)))
    val end = cycle(None, Some(Slot(0x2014, 0x10013)), Some(Slot(0x2018, 0x63)))
    val captured = Seq(begin, work, end)
    test(new IntervalTraceDatapath(k, incrementBits = Some(64))) { dut =>
      val driver = new Driver(dut, k, 73)
      dut.io.trigger_selector.poke(3.U)
      driver.start(4)
      driver.send(boot)
      assert(!dut.io.active.peek().litToBoolean)
      captured.foreach(driver.send)
      driver.send(boot) // waits for end records to drain, then consumes without capture
      assert(!dut.io.active.peek().litToBoolean)
      // A reset token containing a marker must not arm capture.
      driver.send(begin.copy(reset = true))
      assert(!dut.io.active.peek().litToBoolean)
      captured.foreach(driver.send)
      driver.send(boot)
      driver.stop()
      val expected = reference(captured, k, 4)
      val second = expected.map(r => if (r.kind == epochStart || r.kind == epochEnd)
        r.copy(words = r.words.updated(0, BigInt(2))) else r)
      assert(decode(driver.beats.toSeq, 3) == expected ++ second)
    }
  }

  it should "honor retirement order when both markers share a cycle and permit host stop" in {
    val k = key(4, 3)
    val startThenEnd = Cycle(Seq(Some(Slot(0x1000, 0x8013)), None,
      Some(Slot(0x1004, 0x10013))), Seq(2, 1, 0).map(BigInt(_)))
    val endThenStart = Cycle(Seq(Some(Slot(0x2000, 0x10013)), None,
      Some(Slot(0x2004, 0x8013))), Seq(2, 1, 0).map(BigInt(_)))
    test(new IntervalTraceDatapath(k, incrementBits = Some(64))) { dut =>
      val driver = new Driver(dut, k, 89)
      dut.io.trigger_selector.poke(3.U)
      driver.start(1000000)
      driver.send(startThenEnd)
      driver.send(Cycle(Seq.empty, Seq(0, 1, 0).map(BigInt(_))))
      assert(!dut.io.active.peek().litToBoolean)
      driver.send(endThenStart)
      assert(dut.io.active.peek().litToBoolean)
      driver.stop()
      val first = reference(Seq(startThenEnd), k, 1000000)
      val second = reference(Seq(endThenStart), k, 1000000).map(r =>
        if (r.kind == epochStart || r.kind == epochEnd) r.copy(words = r.words.updated(0, BigInt(2))) else r)
      assert(decode(driver.beats.toSeq, 3) == first ++ second)
    }
  }

  "Configurable triggers" should "capture inclusive target-cycle windows independently of host stalls" in {
    val k = key(2, 3)
    val input = (0 until 7).map(i => Cycle(
      if (i == 3) Seq.empty else Seq(Some(Slot(0x1000 + 4 * i))),
      Seq(BigInt(if (i == 3) 0 else 1), 1, i), reset = i == 0))
    test(new IntervalTraceDatapath(k, incrementBits = Some(64))) { dut =>
      val driver = new Driver(dut, k, 103)
      dut.io.trigger_selector.poke(1.U)
      dut.io.trigger_start.poke(2.U)
      dut.io.trigger_end.poke(4.U)
      driver.start(100)
      input.foreach { cycle =>
        driver.send(cycle)
        (0 until 10).foreach(_ => driver.tick(Some(false)))
      }
      driver.stop()
      assert(decode(driver.beats.toSeq, 3) == reference(input.slice(2, 5), k, 100))
    }
  }

  it should "keep the cycle window absolute across target reset and start fresh PMU epochs" in {
    val k = key(1, 3)
    val a = Cycle(Seq(Some(Slot(0x1000))), Seq(1, 1, 5).map(BigInt(_)))
    val b = Cycle(Seq(Some(Slot(0x2000))), Seq(1, 1, 7).map(BigInt(_)))
    test(new IntervalTraceDatapath(k, incrementBits = Some(64))) { dut =>
      val driver = new Driver(dut, k, 105)
      dut.io.trigger_selector.poke(1.U)
      dut.io.trigger_start.poke(1.U)
      dut.io.trigger_end.poke(3.U)
      driver.start(100)
      driver.send(a) // cycle 0: outside the window
      driver.send(a) // cycle 1: first epoch
      driver.send(a.copy(reset = true)) // cycle 2: closes epoch, contributes no counters/time
      driver.send(b) // cycle 3: second epoch, end of window
      driver.send(b) // cycle 4: excluded
      driver.stop()
      val first = reference(Seq(a), k, 100).map(r =>
        if (r.kind == bb || r.kind == epochEnd || (r.kind == pmu && (r.flags & finalSnapshot) != 0))
          r.copy(flags = r.flags | targetReset) else r)
      val second = reference(Seq(b), k, 100).map(r => if (r.kind == epochStart || r.kind == epochEnd)
        r.copy(words = r.words.updated(0, BigInt(2))) else r)
      assert(decode(driver.beats.toSeq, 3) == first ++ second)
    }
  }

  it should "capture a one-cycle window at zero and include an idle endpoint" in {
    val k = key(1, 3)
    val idle = Cycle(Seq.empty, Seq(0, 1, 5).map(BigInt(_)))
    test(new IntervalTraceDatapath(k, incrementBits = Some(64))) { dut =>
      val driver = new Driver(dut, k, 107)
      dut.io.trigger_selector.poke(1.U)
      dut.io.trigger_start.poke(0.U)
      dut.io.trigger_end.poke(0.U)
      driver.start(100)
      driver.send(idle)
      driver.send(idle)
      driver.stop()
      assert(decode(driver.beats.toSeq, 3) == reference(Seq(idle), k, 100))
    }
  }

  it should "match sign-extended PCs across different slots and rearm after stop" in {
    val k = key(4, 3)
    val pc = BigInt("ffffff8000001000", 16)
    val begin = Cycle(Seq(None, None, Some(Slot(pc))), Seq(1, 1, 5).map(BigInt(_)))
    val end = Cycle(Seq(Some(Slot(pc + 4))), Seq(1, 1, 7).map(BigInt(_)))
    val outside = Cycle(Seq(Some(Slot(0x2000, 0x8013))), Seq(1, 1, 11).map(BigInt(_)))
    test(new IntervalTraceDatapath(k, incrementBits = Some(64))) { dut =>
      val driver = new Driver(dut, k, 109)
      dut.io.trigger_selector.poke(2.U)
      dut.io.trigger_start.poke(pc.U)
      dut.io.trigger_end.poke((pc + 4).U)
      driver.start(100)
      driver.send(outside)
      driver.send(begin); driver.send(end); driver.send(outside)
      driver.send(begin); driver.send(end); driver.send(outside)
      driver.stop()
      // The software reference expects producer-width addresses before sign extension.
      val expected = reference(Seq(begin, end).map(c => c.copy(slots = c.slots.map(_.map(
        slot => slot.copy(pc = slot.pc & ((BigInt(1) << 40) - 1)))))), k, 100)
      val second = expected.map(r => if (r.kind == epochStart || r.kind == epochEnd)
        r.copy(words = r.words.updated(0, BigInt(2))) else r)
      assert(decode(driver.beats.toSeq, 3) == expected ++ second)
    }
  }

  it should "honor instruction masks and ignore invalid slots and reset matches" in {
    val k = key(2, 3)
    val begin = Cycle(Seq(None, Some(Slot(0x1000, 0xabcd0013L))), Seq(1, 1, 5).map(BigInt(_)))
    val end = Cycle(Seq(Some(Slot(0x1004, 0xef120013L))), Seq(1, 1, 7).map(BigInt(_)))
    test(new IntervalTraceDatapath(k, incrementBits = Some(64))) { dut =>
      val driver = new Driver(dut, k, 113)
      dut.io.trigger_selector.poke(3.U)
      dut.io.trigger_start.poke("hffff0000abcd0000".U)
      dut.io.trigger_end.poke("hffff0000ef120000".U)
      driver.start(100)
      // Matching bits with valid low cannot start an epoch, even on accepted cycles.
      dut.io.target.bits.trace.retiredinsns(0).insn.poke("habcd0013".U)
      dut.io.target.valid.poke(true.B)
      driver.tick(Some(true))
      dut.io.target.valid.poke(false.B)
      assert(!dut.io.active.peek().litToBoolean)
      driver.send(begin.copy(reset = true))
      assert(!dut.io.active.peek().litToBoolean)
      driver.send(begin); driver.send(end)
      driver.stop()
      assert(decode(driver.beats.toSeq, 3) == reference(Seq(begin, end), k, 100))
    }
  }

  it should "give end priority when both patterns match the same retirement" in {
    val k = key(1, 3)
    val cycle = Cycle(Seq(Some(Slot(0x1000))), Seq(1, 1, 5).map(BigInt(_)))
    test(new IntervalTraceDatapath(k, incrementBits = Some(64))) { dut =>
      val driver = new Driver(dut, k, 127)
      dut.io.trigger_selector.poke(3.U)
      // A zero mask deliberately matches any retired instruction.
      dut.io.trigger_start.poke(0.U)
      dut.io.trigger_end.poke(0.U)
      driver.start(100)
      driver.send(cycle)
      driver.send(Cycle(Seq.empty, Seq(0, 1, 0).map(BigInt(_))))
      assert(!dut.io.active.peek().litToBoolean)
      driver.stop()
      assert(decode(driver.beats.toSeq, 3) == reference(Seq(cycle), k, 100))
    }
  }

  it should "reject invalid selectors and reversed cycle windows" in {
    test(new IntervalTraceDatapath(key(1, 3), incrementBits = Some(64))) { dut =>
      val driver = new Driver(dut, key(1, 3), 131)
      driver.start(100)
      dut.io.trigger_selector.poke(4.U)
      dut.io.config_error.expect(true.B)
      dut.io.trigger_selector.poke(1.U)
      dut.io.trigger_start.poke(10.U)
      dut.io.trigger_end.poke(9.U)
      dut.io.config_error.expect(true.B)
      dut.io.trigger_end.poke(10.U)
      dut.io.config_error.expect(false.B)
      driver.stop()
    }
  }

  "Interval trace datapath" should "preserve prefixes, carried overshoot, holes, empty cycles and same-cycle snapshots under stalls" in {
    val k = key(4, 19)
    val zeros = Vector.fill(19)(BigInt(0))
    val inc = Vector.tabulate(19)(i => BigInt(i + 1))
    var pc = BigInt(0x80000000L)
    def slot(word: Int = 0x13): Option[Slot] = { val s = Some(Slot(pc, word)); pc += (if ((word & 3) == 3) 4 else 2); s }
    // The first block is longer than several intervals. Later branches repay
    // the debt, including two logical endpoints sharing one physical snapshot.
    val input = Seq.fill(3)(Cycle(Seq.fill(4)(slot()), inc)) ++ Seq(
      Cycle(Seq(None, slot(0x63), None, slot(0xa001)), inc),
      Cycle(Seq(slot(0xc001), slot(0x8067), slot(0x9002), None), zeros),
      Cycle(Seq.empty, inc),
      Cycle(Seq(slot(0x13)), inc),
      Cycle(Seq.empty, inc, trap = true),
      Cycle(Seq(slot(0x13)), inc))
    test(new IntervalTraceDatapath(k, incrementBits = Some(64))).withAnnotations(Seq(firrtl2.options.TargetDirAnnotation(
      "generators/firechip/intervaltests/target/prefixes"))) { dut =>
      val driver = new Driver(dut, k, 31)
      driver.start(4)
      input.foreach(driver.send)
      driver.stop()
      assert(decode(driver.beats.toSeq, 19) == reference(input, k, 4))
      val snapshots = decode(driver.beats.toSeq, 19).filter(r => r.kind == pmu && r.flags == 0)
      assert(snapshots.sliding(2).exists(p => p.size == 2 && p(0).words(2) == p(1).words(2)))
      assert(decode(driver.beats.toSeq, 19).exists(r => r.kind == bb && r.words(3) == 0))
    }
  }

  it should "match a software reference with partial PMU padding, counter wrap, traps, privilege changes and PC discontinuities" in {
    val k = key(4, 18)
    val random = new Random(47)
    var pc = BigInt("fffffff000", 16)
    val input = (0 until 80).map { cycle =>
      val slots = (0 until 4).map { lane =>
        if (random.nextBoolean()) None else {
          if (cycle % 9 == 0 && lane == 2) pc += 0x100
          val word = Vector(0x13, 0x1, 0x63, 0x6f, 0xa001, 0xc001, 0x8082)(random.nextInt(7))
          val s = Slot(pc, word, if (cycle % 13 < 6) 3 else 0)
          pc += (if ((word & 3) == 3) 4 else 2)
          Some(s)
        }
      }
      Cycle(slots, Vector.tabulate(18)(i => if (cycle == 0) mask64 - i else BigInt(i + 3)), trap = cycle % 11 == 0)
    }
    test(new IntervalTraceDatapath(k, incrementBits = Some(64))).withAnnotations(Seq(firrtl2.options.TargetDirAnnotation(
      "generators/firechip/intervaltests/target/random"))) { dut =>
      val driver = new Driver(dut, k, 17)
      driver.start(7)
      input.foreach(driver.send)
      driver.stop()
      assert(decode(driver.beats.toSeq, 18) == reference(input, k, 7))
    }
  }

  it should "flush before target reset, establish a new baseline, and stop without another target token" in {
    val k = key(1, 1)
    test(new IntervalTraceDatapath(k, incrementBits = Some(64))).withAnnotations(Seq(firrtl2.options.TargetDirAnnotation(
      "generators/firechip/intervaltests/target/reset"))) { dut =>
      val driver = new Driver(dut, k, 89)
      driver.start(10)
      driver.send(Cycle(Seq(Some(Slot(0x1000))), Seq(9)))
      driver.send(Cycle(Seq(Some(Slot(0x1004))), Seq(999), reset = true))
      driver.send(Cycle(Seq.empty, Seq(999), reset = true))
      driver.send(Cycle(Seq(Some(Slot(0x2000, 0x63))), Seq(2)))
      driver.stop()
      val records = decode(driver.beats.toSeq, 1)
      assert(records.filter(_.kind == epochStart).map(_.words.head) == Vector(1, 2).map(BigInt(_)))
      assert(records.filter(r => r.kind == pmu && (r.flags & finalSnapshot) != 0).map(_.words.last) == Vector(9, 2).map(BigInt(_)))
      assert(records.filter(_.kind == bb).map(_.words(2)) == Vector(BigInt(1), BigInt(1)))
      assert(records.count(r => r.kind == pmu && r.flags == baseline) == 2)
      assert(records.filter(_.kind == epochEnd).head.flags == targetReset)
      assert(records.last.kind == epochEnd)
    }
  }

  it should "ignore disabled observations, reject a zero start length and latch configuration for the epoch" in {
    val k = key(1, 2)
    test(new IntervalTraceDatapath(k, incrementBits = Some(64))).withAnnotations(Seq(firrtl2.options.TargetDirAnnotation(
      "generators/firechip/intervaltests/target/controls"))) { dut =>
      val driver = new Driver(dut, k, 13)
      driver.send(Cycle(Seq(Some(Slot(0x1000, 0x63))), Seq(99, 99)))
      assert(driver.beats.isEmpty)
      // Offer a token while the host configuration is invalid: it must remain
      // unconsumed until the host supplies a positive interval length.
      driver.start(0)
      dut.io.target.valid.poke(true.B)
      for (_ <- 0 until 5) {
        dut.io.config_error.expect(true.B)
        dut.io.target.ready.expect(false.B)
        driver.tick(Some(true))
      }
      driver.start(2)
      while (!dut.io.target.ready.peek().litToBoolean) driver.tick()
      driver.tick()
      dut.io.target.valid.poke(false.B)
      // The first valid epoch token is now included. Editing host configuration
      // while active must not change its latched interval length.
      dut.io.nominal_interval.poke(0.U)
      dut.io.config_error.expect(false.B)
      driver.send(Cycle(Seq(Some(Slot(0x2000, 0x63))), Seq(3, 4)))
      driver.stop()
      val records = decode(driver.beats.toSeq, 2)
      val boundaries = records.filter(r => r.kind == pmu && r.flags == 0)
      assert(boundaries.size == 1 && boundaries.head.words(1) == 2)
      assert(boundaries.head.words.drop(4) == Vector(BigInt(102), BigInt(103)))
      assert(records.head.words(1) == 2)
    }
  }

  "Stall accounting" should "give each block the stall cycles since its previous boundary, for several stalled blocks per interval" in {
    val k = key(2, 3, Some(1))
    val wfi = 0x10500073
    def cycle(slots: Seq[Option[Slot]], stall: Int, trap: Boolean = false): Cycle =
      Cycle(slots, Seq(BigInt(slots.flatten.size), BigInt(stall), BigInt(1)), trap)
    val input = Seq(
      cycle(Seq(Some(Slot(0x1000))), 0),
      cycle(Seq.empty, 1), cycle(Seq.empty, 1), // first sleep
      cycle(Seq(Some(Slot(0x1004, wfi))), 0, trap = true), // WFI retires; the wake-up trap ends its block
      cycle(Seq(Some(Slot(0x3000, 0x63))), 0),
      cycle(Seq.empty, 1), // second sleep: a different block in the same interval
      cycle(Seq(Some(Slot(0x2000, wfi))), 0, trap = true),
      // Two blocks end in one stalled cycle: the first takes it, as for elapsed cycles.
      cycle(Seq(Some(Slot(0x3000, 0x63)), Some(Slot(0x3004, 0x63))), 1),
      cycle(Seq(Some(Slot(0x4000))), 0),
      cycle(Seq.empty, 1), cycle(Seq.empty, 1)) // stall in a block still open at host stop
    test(new IntervalTraceDatapath(k, incrementBits = Some(64))).withAnnotations(Seq(firrtl2.options.TargetDirAnnotation(
      "generators/firechip/intervaltests/target/stall"))) { dut =>
      val driver = new Driver(dut, k, 137)
      driver.start(6)
      input.foreach(driver.send)
      driver.stop("rtl-stall-cases")
      val records = decode(driver.beats.toSeq, 3, 5)
      assert(records == reference(input, k, 6))
      val blocks = records.filter(_.kind == bb)
      assert(blocks.map(_.words(4)) == Vector(2, 0, 1, 1, 0, 2).map(BigInt(_)))
      // Both stalled blocks fall in the first interval, which the second branch at 0x3004 closes.
      assert(blocks.indexWhere(r => (r.flags & terminateInterval) != 0) == 4)
      assert(blocks.map(_.words(4)).sum == records.filter(r => r.kind == pmu && (r.flags & finalSnapshot) != 0).head.words(4 + 1))
    }
  }

  it should "match the software reference with random stalls, traps, privilege changes and PC discontinuities" in {
    val k = key(4, 18, Some(5))
    val random = new Random(53)
    var pc = BigInt("fffffff000", 16)
    val input = (0 until 120).map { cycle =>
      val slots = (0 until 4).map { lane =>
        if (random.nextInt(3) != 0) None else {
          if (cycle % 9 == 0 && lane == 2) pc += 0x100
          val word = Vector(0x13, 0x1, 0x63, 0x6f, 0xa001, 0xc001, 0x8082, 0x10500073)(random.nextInt(8))
          val s = Slot(pc, word, if (cycle % 13 < 6) 3 else 0)
          pc += (if ((word & 3) == 3) 4 else 2)
          Some(s)
        }
      }
      val stall = if (slots.flatten.isEmpty && random.nextBoolean()) 1 else 0
      Cycle(slots, Vector.tabulate(18)(i => if (i == 5) BigInt(stall) else BigInt(i + 3)), trap = cycle % 11 == 0)
    }
    test(new IntervalTraceDatapath(k, incrementBits = Some(64))).withAnnotations(Seq(firrtl2.options.TargetDirAnnotation(
      "generators/firechip/intervaltests/target/stall-random"))) { dut =>
      val driver = new Driver(dut, k, 19)
      driver.start(7)
      input.foreach(driver.send)
      driver.stop("rtl-stall")
      val records = decode(driver.beats.toSeq, 18, 5)
      assert(records == reference(input, k, 7))
      assert(records.exists(r => r.kind == bb && r.words(4) > 0))
    }
  }

  "Interval PMU serializer" should "hold every beat stable and continuously valid across first, middle and final stalls" in {
    test(new IntervalPacketSerializer(1536)).withAnnotations(Seq(firrtl2.options.TargetDirAnnotation(
      "generators/firechip/intervaltests/target/serializer"))) { dut =>
      val words = (0 until 24).map(i => BigInt(0x100 + i))
      val packet = words.zipWithIndex.map { case (w, i) => w << (64 * i) }.reduce(_ | _)
      dut.io.packet.valid.poke(true.B)
      dut.io.packet.bits.poke(packet.U)
      dut.io.beat.ready.poke(false.B)
      dut.clock.step()
      dut.io.packet.valid.poke(false.B)
      for (beat <- 0 until 3) {
        val expected = (packet >> (512 * beat)) & ((BigInt(1) << 512) - 1)
        for (_ <- 0 until 5) {
          dut.io.beat.valid.expect(true.B)
          dut.io.beat.bits.expect(expected.U)
          dut.io.packet.ready.expect(false.B)
          dut.clock.step()
        }
        dut.io.beat.ready.poke(true.B)
        dut.clock.step()
        dut.io.beat.ready.poke(false.B)
      }
      dut.io.beat.valid.expect(false.B)
      dut.io.packet.ready.expect(true.B)
    }
  }
}
