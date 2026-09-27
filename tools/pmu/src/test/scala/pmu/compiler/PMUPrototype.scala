package pmu.compiler

import chisel3._
import chisel3.util.PopCount
import org.chipsalliance.cde.config.Parameters
import pmu._
import java.nio.file.{Files, Paths}

class RawProducer(registry: PMURegistry) extends RawModule {
  val clock = IO(Input(Clock()))
  val reset = IO(Input(Bool()))
  val bits = IO(Input(UInt(4.W)))
  val increment = WireInit(PopCount(bits))
  registry.register("retired", increment, clock, reset, "instructions", "PopCount of four lanes")
}

class Producer(explicit: Boolean)(implicit p: Parameters) extends Module {
  val bits = IO(Input(UInt(4.W)))
  val registry = p(PMUScopeKey).get
  val increment = WireInit(PopCount(bits))
  if (explicit) {
    val raw = Module(new RawProducer(registry))
    raw.clock := clock; raw.reset := reset.asBool; raw.bits := bits
  } else {
    val alias_clock = Wire(Clock())
    val alias_reset = Wire(Bool())
    alias_clock := clock
    alias_reset := reset.asBool
    withClockAndReset(alias_clock, alias_reset) {
      registry.register("retired", increment, "instructions", "PopCount of four lanes")
    }
  }
}

class PrototypeTile(source: String, explicit: Boolean) extends Module {
  val bits = IO(Input(UInt(4.W)))
  val enable = IO(Input(Bool()))
  val clear = IO(Input(Bool()))
  val snapshot = IO(Input(Bool()))
  val values = IO(Output(Vec(2, UInt(4.W))))
  val captured = IO(Output(Vec(2, UInt(4.W))))
  val captured_valid = IO(Output(Bool()))
  val registry = new PMURegistry(source)
  implicit val p: Parameters = Parameters.empty.alterPartial { case PMUScopeKey => Some(registry) }
  val producer = Module(new Producer(explicit))
  producer.bits := bits
  registry.register("cycles", WireInit(1.U), "cycles")
  val events = registry.build(4)
  val attached = TilePMU.build(events)
  assert(attached.manifest.events.map(_.incrementBits) == Vector(None, None))
  attached.enable := enable
  attached.clear := clear
  attached.snapshot := snapshot
  values := attached.values
  captured := attached.snapshot_values
  captured_valid := attached.snapshot_valid
}

class PMUPrototypeTop extends Module {
  val bits_a = IO(Input(UInt(4.W)))
  val bits_b = IO(Input(UInt(4.W)))
  val enable_a = IO(Input(Bool()))
  val enable_b = IO(Input(Bool()))
  val snapshot_a = IO(Input(Bool()))
  val snapshot_b = IO(Input(Bool()))
  val captured_a0 = IO(Output(UInt(4.W)))
  val captured_a1 = IO(Output(UInt(4.W)))
  val captured_b0 = IO(Output(UInt(4.W)))
  val captured_b1 = IO(Output(UInt(4.W)))
  val valid_a = IO(Output(Bool()))
  val valid_b = IO(Output(Bool()))
  val clear_a = IO(Input(Bool()))
  val clear_b = IO(Input(Bool()))
  val reset_a = IO(Input(Bool()))
  val reset_b = IO(Input(Bool()))
  val a0 = IO(Output(UInt(4.W)))
  val a1 = IO(Output(UInt(4.W)))
  val b0 = IO(Output(UInt(4.W)))
  val b1 = IO(Output(UInt(4.W)))
  val wide = IO(Output(UInt(64.W)))
  val a = Module(new PrototypeTile("tile_0", false))
  val b = Module(new PrototypeTile("tile_1", true))
  a.bits := bits_a; b.bits := bits_b
  a.enable := enable_a; b.enable := enable_b
  a.snapshot := snapshot_a; b.snapshot := snapshot_b
  a.clear := clear_a; b.clear := clear_b
  captured_a0 := a.captured(0); captured_a1 := a.captured(1)
  captured_b0 := b.captured(0); captured_b1 := b.captured(1)
  valid_a := a.captured_valid; valid_b := b.captured_valid
  a.reset := reset_a; b.reset := reset_b
  a0 := a.values(0); a1 := a.values(1)
  b0 := b.values(0); b1 := b.values(1)
  val registry = new PMURegistry("system")
  registry.register("cycles", WireInit(1.U))
  val events = registry.build()
  val attached = TilePMU.build(events)
  attached.enable := true.B
  attached.snapshot := false.B
  attached.clear := false.B
  wide := attached.values(0)
}

// Model Diplomacy's flipped clock/reset leaves and a reset synchronized outside
// the owner. All producers must resolve to the owner's exact generated domain.
class FlippedDomainTile extends RawModule {
  val auto = IO(new Bundle {
    val domain = Flipped(new Bundle {
      val clock = Clock()
      val reset = Bool()
    })
  })
  val tile = withClockAndReset(auto.domain.clock, auto.domain.reset) {
    Module(new PrototypeTile("flipped", true))
  }
  tile.bits := 0.U
  tile.enable := true.B
  tile.snapshot := false.B
  tile.clear := false.B
}
class GeneratedOwnerDomain extends Module {
  val delayed_reset = RegNext(reset.asBool, true.B)
  val tile = Module(new FlippedDomainTile)
  tile.auto.domain.clock := clock
  tile.auto.domain.reset := delayed_reset
}

class OptionalCaller(enabled: Boolean) extends RawModule {
  val p = Parameters.empty
  // The caller owns enablement. Missing scope is an error only when enabled.
  if (enabled) {
    val registry = p(PMUScopeKey).get
    registry.register("cycles", WireInit(1.U))
  }
}

class Negative(kind: String, withTilePMU: Boolean = true) extends Module {
  val input = IO(Input(UInt(8.W)))
  val other_clock = IO(Input(Clock()))
  val other_reset = IO(Input(Bool()))
  val enable = IO(Input(Bool()))
  val out = IO(Output(UInt(4.W)))
  val registry = new PMURegistry("negative")
  val increment = kind match {
    case "literal" => 1.U
    case "unbound" => UInt(1.W)
    case "port" => input
    case "register" => RegInit(0.U(1.W))
    case "expression" => input + 1.U
    case "known-wide" => WireInit(UInt(8.W), input)
    case "unknown-wide" => WireInit(input)
    case "zero-width" => WireInit(0.U(0.W))
    case _ => WireInit(1.U)
  }
  if (kind == "clock") registry.register("event", increment, other_clock, reset, "", "")
  else if (kind == "reset") registry.register("event", increment, clock, other_reset, "", "")
  else if (kind == "gated") {
    val gated = WireInit((clock.asUInt & enable.asUInt).asBool.asClock)
    withClockAndReset(gated, reset) { registry.register("event", increment) }
  } else if (kind == "local-reset") {
    val local = WireInit(reset.asBool || enable)
    registry.register("event", increment, clock, local, "", "")
  } else if (kind == "generated-clock") {
    val divided = RegInit(false.B)
    divided := !divided
    val generated = WireInit(divided.asClock)
    registry.register("event", increment, generated, reset, "", "")
  } else if (kind == "registered-reset") {
    val delayed = RegNext(reset.asBool, true.B)
    registry.register("event", increment, clock, delayed, "", "")
  } else if (kind == "literal-reset") registry.register("event", increment, clock, false.B, "", "")
  else if (kind != "empty") registry.register(if (kind == "empty-name") " " else "event", increment)
  if (kind == "duplicate") registry.register("event", increment)
  val events = registry.build(if (kind == "counter-width") 0 else 4)
  if (withTilePMU) {
    val attachment = TilePMU.build(events)
    attachment.enable := true.B
    attachment.snapshot := false.B
    attachment.clear := false.B
    out := attachment.values(0)
  } else {
    out := 0.U
  }
  if (kind == "late") registry.register("late", increment)
  if (kind == "twice") registry.build(4)
}

// Finalization is useful even when there is no accumulator or MMIO consumer.
class RegistryOnly extends Module {
  val bits = IO(Input(UInt(4.W)))
  val registry = new PMURegistry("events_only")
  implicit val p: Parameters = Parameters.empty.alterPartial { case PMUScopeKey => Some(registry) }
  val producer = Module(new Producer(true))
  producer.bits := bits
  registry.register("cycles", WireInit(1.U), "cycles")
  val events = registry.build()
}

class IdenticalTiles(duplicateSources: Boolean) extends Module {
  for (id <- 0 until 2) {
    val tile = Module(new PrototypeTile(if (duplicateSources) "same" else s"tile_$id", true))
    tile.bits := 0.U; tile.clear := false.B
    tile.enable := true.B; tile.snapshot := false.B
  }
}

object PMUPrototype {
  private var checks = 0
  private def check(name: String)(body: => Unit): Unit = {
    body; checks += 1; println(s"PASS: $name")
  }
  private def reject(name: String, message: String)(body: => Any): Unit = check(name) {
    val failure = try { body; None } catch { case e: Exception => Some(e) }
    assert(failure.nonEmpty, s"$name unexpectedly succeeded")
    def messages(e: Throwable): String = Option(e.getMessage).getOrElse("") +
      Option(e.getCause).map(messages).getOrElse("")
    assert(messages(failure.get).contains(message), s"$name failed for an unexpected reason: ${messages(failure.get)}")
  }
  def elaborate(gen: => RawModule): firrtl.AnnotationSeq = {
    val elaborated = new chisel3.stage.phases.Elaborate().transform(
      Seq(chisel3.stage.ChiselGeneratorAnnotation(() => gen)))
    new chisel3.stage.phases.Convert().transform(elaborated)
  }
  def main(args: Array[String]): Unit = {
    val out = Paths.get(args.head)
    Files.createDirectories(out)
    check("caller disables registration without a clock or registry") {
      val annos = elaborate(new OptionalCaller(false))
      assert(!annos.exists(a => a.isInstanceOf[PMUScopeAnnotation] || a.isInstanceOf[PMUEventAnnotation]))
    }
    reject("enabled caller without a registry", "None.get") {
      elaborate(new OptionalCaller(true))
    }
    reject("empty source", "source must be nonempty") { new PMURegistry(" ") }
    val failures = Vector(
      "literal" -> "bound wire", "unbound" -> "hardware", "port" -> "bound wire",
      "register" -> "bound wire", "expression" -> "bound wire", "known-wide" -> "increment width",
      "unknown-wide" -> "resolved increment width 8", "zero-width" -> "width",
      "clock" -> "declared clock differs", "reset" -> "declared reset differs",
      "gated" -> "declared clock differs", "local-reset" -> "declared reset differs",
      "generated-clock" -> "declared clock differs",
      "registered-reset" -> "declared reset differs",
      "literal-reset" -> "targetable", "empty" -> "empty registry", "empty-name" -> "name must be nonempty",
      "duplicate" -> "duplicate event", "counter-width" -> "counterBits must be positive",
      "late" -> "registration after build", "twice" -> "build called twice")
    failures.foreach { case (kind, message) =>
      reject(kind, message) { PMUCompiler.validate(PMUCompiler.fromChisel(elaborate(new Negative(kind)))) }
    }
    reject("duplicate scope identities", "Duplicate PMU source identities") {
      PMUCompiler.validate(PMUCompiler.fromChisel(elaborate(new IdenticalTiles(true))))
    }
    check("deduplicated producer hierarchies retain separate event instances") {
      val checked = PMUCompiler.validate(PMUCompiler.fromChisel(elaborate(new IdenticalTiles(false))))
      val retired = checked.annotations.collect { case e: EventAnnotation if e.id == 0 => e }
      assert(retired.size == 2)
      assert(retired.map(_.increment.path.last._2).distinct.size == 1)
      assert(retired.map(_.increment.path).distinct.size == 2)
      assert(PMUCompiler.manifests(checked).map(_.source) == Vector("tile_0", "tile_1"))
      val owners = checked.annotations.collect { case s: ScopeAnnotation => s.clock }
      assert(owners.map(_.path.last._2).distinct.size == 1)
      assert(owners.map(_.path).distinct.size == 2)
    }
    check("flipped domain bundle and shared generated owner reset") {
      val checked = PMUCompiler.validate(PMUCompiler.fromChisel(elaborate(new GeneratedOwnerDomain)))
      assert(PMUCompiler.manifests(checked).map(_.source) == Vector("flipped"))
    }
    check("registry-only finalization preserves narrow events without counters") {
      val annotations = elaborate(new RegistryOnly)
      val pmuAnnotations = annotations.filter(a => a.isInstanceOf[PMUScopeAnnotation] || a.isInstanceOf[PMUEventAnnotation])
      assert(pmuAnnotations.size == 3)
      assert(firrtl.annotations.JsonProtocol.deserialize(firrtl.annotations.JsonProtocol.serialize(pmuAnnotations)) == pmuAnnotations)
      val checked = PMUCompiler.validate(PMUCompiler.fromChisel(annotations))
      assert(PMUCompiler.manifests(checked).head.events.map(_.incrementBits) == Vector(Some(3), Some(1)))
      assert(!checked.circuit.modules.exists(_.name.startsWith("TilePMU")))
      def checkNoRegisters(statement: firrtl2.ir.Statement): Unit = {
        assert(!statement.isInstanceOf[firrtl2.ir.DefRegister])
        statement.foreachStmt(checkNoRegisters)
      }
      checked.circuit.modules.foreach {
        case m: firrtl2.ir.Module => checkNoRegisters(m.body)
        case _ => ()
      }
      val graph = new Connectivity(checked.circuit)
      checked.annotations.collect { case e: EventAnnotation => e }.foreach { event =>
        assert(graph.uintWidth(event.ownerIncrement) == graph.uintWidth(event.increment))
      }
      val serialized = firrtl2.annotations.JsonProtocol.serialize(checked.annotations.collect {
        case s: ScopeAnnotation => s
        case e: EventAnnotation => e
      })
      PMUCompiler.validate(checked.copy(annotations = firrtl2.annotations.JsonProtocol.deserialize(serialized)))
      Files.writeString(out.resolve("RegistryOnly.v"), PMUCompiler.emitNative(checked))
    }
    failures.filter { case (kind, _) => Set("unknown-wide", "clock", "reset", "late", "twice")(kind) }
      .foreach { case (kind, message) =>
        reject(s"registry-only $kind", message) {
          PMUCompiler.validate(PMUCompiler.fromChisel(elaborate(new Negative(kind, withTilePMU = false))))
        }
      }
    val nativeAnnotations = elaborate(new PMUPrototypeTop)
    val pmuAnnotations = nativeAnnotations.filter(a => a.isInstanceOf[PMUScopeAnnotation] || a.isInstanceOf[PMUEventAnnotation])
    check("Chisel annotation JSON round trip") {
      val json = firrtl.annotations.JsonProtocol.serialize(pmuAnnotations)
      Files.writeString(out.resolve("chisel-pmu.anno.json"), json)
      assert(firrtl.annotations.JsonProtocol.deserialize(json) == pmuAnnotations)
    }
    val raw = PMUCompiler.fromChisel(nativeAnnotations)
    check("explicit compiler transport") {
      assert(raw.annotations.count(_.isInstanceOf[EventAnnotation]) == 5)
      assert(raw.annotations.count(_.isInstanceOf[ScopeAnnotation]) == 3)
      assert(!raw.annotations.exists(_.isInstanceOf[firrtl2.bridge.UnsupportedAnnotation]))
    }
    val checked = PMUCompiler.validate(raw)
    val manifests = PMUCompiler.manifests(checked)
    check("nested aliases, both overloads, and resolved widths") {
      assert(manifests.map(_.source) == Vector("tile_0", "tile_1", "system"))
      assert(manifests.take(2).forall(_.events.map(_.incrementBits) == Vector(Some(3), Some(1))))
      assert(manifests.last.counterBits == 64)
      assert(manifests.forall(_.finalized))
    }
    check("FIRRTL2 annotation JSON round trip after deduplication") {
      val annotations = checked.annotations.filter(a => a.isInstanceOf[ScopeAnnotation] || a.isInstanceOf[EventAnnotation])
      val json = firrtl2.annotations.JsonProtocol.serialize(annotations)
      Files.writeString(out.resolve("firrtl2-pmu.anno.json"), json)
      val roundTrip = firrtl2.annotations.JsonProtocol.deserialize(json)
      assert(roundTrip == annotations)
      PMUCompiler.validate(checked.copy(annotations = roundTrip))
      val retired = annotations.collect { case e: EventAnnotation if e.id == 0 && e.source != "system" => e }
      assert(retired.map(_.increment.path).distinct.size == 2)
      // Owner targets stay instance-specific even when equal modules deduplicate.
      val owners = annotations.collect { case s: ScopeAnnotation if s.manifest.counterBits == 4 => s.clock }
      assert(owners.map(_.path).distinct.size == 2)
    }
    check("manifest JSON and repeated elaboration without leaked registry state") {
      manifests.foreach { m =>
        assert(PMUManifest.fromJson(m.toJson) == m)
        Files.writeString(out.resolve(s"${m.source}.json"), m.toJson)
      }
      assert(PMUCompiler.manifests(PMUCompiler.validate(PMUCompiler.fromChisel(elaborate(new PMUPrototypeTop)))) == manifests)
    }
    reject("unsupported manifest version", "Unsupported PMU schema version") {
      PMUManifest.fromJson(manifests.head.toJson.replace("\"schemaVersion\": 1", "\"schemaVersion\": 2"))
    }
    reject("native emission without validation", "not passed compiler validation") { PMUCompiler.nativeState(raw) }
    check("emit checked native RTL") {
      Files.writeString(out.resolve("checked.fir"), checked.circuit.serialize)
      Files.writeString(out.resolve("PMUPrototypeTop.v"), PMUCompiler.emitNative(checked))
    }
    println(s"$checks PMU prototype checks passed; RTL simulation runs next.")
  }
}
