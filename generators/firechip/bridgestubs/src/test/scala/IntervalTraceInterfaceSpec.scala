// See LICENSE for license details.

package firechip.bridgestubs

import chisel3._
import firechip.bridgeinterfaces._
import firesim.lib.bridgeutils.{BridgeAnnotation, PipeBridgeChannel}
import org.scalatest.flatspec.AnyFlatSpec
import pmu.{PMUEvent, PMUManifest, PMURegistry, PMUConsumerAnnotation, PMUMetadataConsumerAnnotation}
import chisel3.experimental.{annotate, ChiselAnnotation}
import pmu.compiler.PMUCompiler

import java.nio.file.{Files, Paths}

private class IntervalBridgeHarness(keys: Seq[IntervalTraceBridgeKey]) extends RawModule {
  val clock = IO(Input(Clock()))
  val reset = IO(Input(Bool()))
  keys.foreach { key =>
    val interval_bridge = Module(new IntervalTraceBridge(key))
    interval_bridge.io.tiletrace.clock := clock
    interval_bridge.io.tiletrace.reset := reset
    interval_bridge.io.tiletrace.trace := 0.U.asTypeOf(interval_bridge.io.tiletrace.trace)
    interval_bridge.io.increments.foreach(_ := 0.U)
    require(interval_bridge.io.increments.map(_.getWidth) == key.events.map(_.incrementBits))
  }
}

private class DeferredIntervalHarness(fault: String = "") extends Module {
  val retired_bits = IO(Input(UInt(4.W)))
  val other_bits = IO(Input(UInt(4.W)))
  val other_clock = IO(Input(Clock()))
  val other_reset = IO(Input(Bool()))
  val registry = new PMURegistry("deferred_tile")
  registry.register("retired", WireInit(chisel3.util.PopCount(retired_bits)), "instructions")
  registry.register("cycles", WireInit(1.U), "cycles")
  val events = registry.build()
  val interval_bridge = Module(new DeferredIntervalTraceBridge(events.manifest,
    TraceBundleWidths(4, 40, 32, None, 64, 40), retirementLatency = 1))
  interval_bridge.io.tiletrace.clock := (if (fault == "clock") other_clock else clock)
  interval_bridge.io.tiletrace.reset := (if (fault == "reset") other_reset else reset.asBool)
  interval_bridge.io.tiletrace.trace := 0.U.asTypeOf(interval_bridge.io.tiletrace.trace)
  interval_bridge.io.increments(1) := events.increments(1)
  if (fault == "wiring") {
    interval_bridge.io.increments(0) := chisel3.util.PopCount(other_bits)
  } else if (fault == "width" || fault == "truncation") {
    val truncated = Wire(UInt(2.W))
    truncated := events.increments(0)
    if (fault == "truncation") {
      val extended = Wire(UInt(3.W))
      extended := truncated
      interval_bridge.io.increments(0) := extended
    } else interval_bridge.io.increments(0) := truncated
  } else interval_bridge.io.increments(0) := events.increments(0)
  annotate(new ChiselAnnotation {
    def toFirrtl = PMUConsumerAnnotation(events.manifest.source,
      interval_bridge.io.increments.map(_.toAbsoluteTarget),
      interval_bridge.io.tiletrace.clock.toAbsoluteTarget, interval_bridge.io.tiletrace.reset.toAbsoluteTarget)
  })
}

class IntervalTraceInterfaceSpec extends AnyFlatSpec {
  private val events = Seq(
    IntervalPMUEvent(0, "rob.instructions_retired", "instructions", "Architectural retirements", 3),
    IntervalPMUEvent(1, "tile.cycles", "cycles", "Tile cycles", 1))
  private val traceWidths = TraceBundleWidths(4, 40, 32, None, 64, 40)
  private val key = IntervalTraceBridgeKey("boom_tile_0", traceWidths, events)

  private def elaborate(keys: Seq[IntervalTraceBridgeKey]): firrtl.AnnotationSeq = {
    val annotations = new chisel3.stage.phases.Elaborate().transform(
      Seq(chisel3.stage.ChiselGeneratorAnnotation(() => new IntervalBridgeHarness(keys))))
    new chisel3.stage.phases.Convert().transform(annotations)
  }

  private def deferred(fault: String = ""): firrtl.AnnotationSeq = {
    val annotations = new chisel3.stage.phases.Elaborate().transform(
      Seq(chisel3.stage.ChiselGeneratorAnnotation(() => new DeferredIntervalHarness(fault))))
    new chisel3.stage.phases.Convert().transform(annotations)
  }

  "Deferred PMU attachment" should "finalize inferred widths before serializing the bridge key" in {
    val annotations = deferred()
    assert(annotations.collect { case a: BridgeAnnotation => a }.head.widgetConstructorKey.isEmpty)
    val checked = PMUCompiler.validate(PMUCompiler.fromChisel(annotations))
    val finalized = PMUMetadataConsumerAnnotation.finalizeAll(PMUCompiler.manifests(checked), annotations)
    assert(!finalized.exists(_.isInstanceOf[PMUMetadataConsumerAnnotation]))
    val bridges = finalized.collect { case a: BridgeAnnotation => a }
    val resolved = bridges.head.widgetConstructorKey.get.asInstanceOf[IntervalTraceBridgeKey]
    assert(resolved.events.map(_.incrementBits) == Seq(3, 1))
    assert(resolved.retirementLatency == 1)
    assert(firrtl.annotations.JsonProtocol.deserialize(firrtl.annotations.JsonProtocol.serialize(bridges)) == bridges)
    assert(!checked.circuit.modules.exists(_.name.startsWith("TilePMU")))
  }

  it should "reject consumer domain, width and source mismatches" in {
    Seq("clock" -> "consumer clock differs", "reset" -> "consumer reset differs",
      "width" -> "consumer width differs", "truncation" -> "not wired to the registered increment", "wiring" -> "not wired to the registered increment").foreach {
      case (fault, message) => withClue(s"$fault: ") {
        val error = intercept[Exception] { PMUCompiler.validate(PMUCompiler.fromChisel(deferred(fault))) }
        def messages(t: Throwable): String = Option(t.getMessage).getOrElse("") +
          Option(t.getCause).map(messages).getOrElse("")
        assert(messages(error).contains(message), messages(error))
      }
    }
  }

  "Interval trace metadata" should "reject invalid catalogs and unresolved PMU manifests" in {
    intercept[IllegalArgumentException] { key.copy(events = events.reverse) }
    intercept[IllegalArgumentException] { key.copy(events = events.updated(1, events(1).copy(name = events.head.name))) }
    intercept[IllegalArgumentException] { key.copy(events = Seq.empty) }
    intercept[IllegalArgumentException] { events.head.copy(incrementBits = 0) }
    intercept[IllegalArgumentException] { events.head.copy(incrementBits = 65) }
    intercept[IllegalArgumentException] { key.copy(traceWidths = traceWidths.copy(retireWidth = 0)) }
    intercept[IllegalArgumentException] { key.copy(traceWidths = traceWidths.copy(iaddrWidth = 65)) }
    intercept[IllegalArgumentException] { key.copy(schemaVersion = 2) }
    intercept[IllegalArgumentException] { key.copy(traceWidths = traceWidths.copy(insnWidth = 16)) }
    intercept[IllegalArgumentException] { key.copy(traceWidths = traceWidths.copy(tvalWidth = 0)) }
    intercept[IllegalArgumentException] { key.copy(traceWidths = traceWidths.copy(wdataWidth = Some(0))) }
    val manifest = PMUManifest("boom_tile_0", 64,
      Vector(PMUEvent(0, "retired", "instructions", "Retired", None)))
    intercept[IllegalArgumentException] { IntervalTraceBridge.key(manifest, traceWidths) }
    val finalized = manifest.copy(events = manifest.events.map(_.copy(incrementBits = Some(3))), finalized = true)
    val converted = IntervalTraceBridge.key(finalized, traceWidths)
    assert(converted.events == Seq(IntervalPMUEvent(0, "retired", "instructions", "Retired", 3)))
    intercept[IllegalArgumentException] { IntervalTraceBridge.key(finalized.copy(counterBits = 32), traceWidths) }
  }

  "Interval trace stub" should "preserve narrow inputs and annotate every field in one clock domain" in {
    val annotations = elaborate(Seq(key))
    val bridges = annotations.collect { case a: BridgeAnnotation => a }
    assert(bridges.size == 1)
    val bridge = bridges.head
    assert(bridge.widgetClass == "firechip.goldengateimplementations.IntervalTraceBridgeModule")
    assert(bridge.widgetConstructorKey.contains(key))
    val channels = bridge.bridgeChannels.map {
      case channel: PipeBridgeChannel => channel
      case other => fail(s"Unexpected channel: $other")
    }
    // Reset, eight fields per slot, CSR time, and independently sized increments.
    assert(channels.size == 2 + 8 * key.traceWidths.retireWidth + key.events.size)
    assert(channels.forall(c => c.sources.size == 1 && c.sinks.isEmpty && c.latency == 1))
    assert(channels.map(_.clock).distinct.size == 1)
    assert(channels.map(_.name).distinct.size == channels.size)
    assert(!channels.exists(c => c.name.contains("ready") || c.name.contains("stall")))

    val encoded = firrtl.annotations.JsonProtocol.serialize(bridges)
    val decoded = firrtl.annotations.JsonProtocol.deserialize(encoded)
    assert(decoded == bridges)
    val out = Paths.get("generators/firechip/bridgestubs/target/interval-trace")
    Files.createDirectories(out)
    Files.writeString(out.resolve("bridge.anno.json"), encoded)
  }

  it should "keep heterogeneous port shapes and same-shape tile catalogs distinct" in {
    val keys = Seq(key, key.copy(source = "boom_tile_1"),
      key.copy(source = "boom_tile_2", traceWidths = traceWidths.copy(retireWidth = 2, iaddrWidth = 64),
        events = Seq(events.head.copy(incrementBits = 2))),
      key.copy(traceWidths = traceWidths.copy(wdataWidth = Some(64))),
      key.copy(traceWidths = traceWidths.copy(tvalWidth = 64)))
    val first = elaborate(keys).collect { case a: BridgeAnnotation => a }
    assert(first.size == keys.size)
    assert(first.map(_.target.module).distinct.size == keys.size)
    assert(first.map(_.widgetConstructorKey).toSet == keys.map(Some(_)).toSet)
    val second = elaborate(keys).collect { case a: BridgeAnnotation => a }
    assert(first.map(_.target.module) == second.map(_.target.module))
  }

}
