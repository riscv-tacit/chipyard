// See LICENSE for license details.

package firechip.bridgestubs

import chisel3._
import firechip.bridgeinterfaces._
import firesim.lib.bridgeutils.{BridgeAnnotation, PipeBridgeChannel}
import org.scalatest.flatspec.AnyFlatSpec
import pmu.{PMUCatalog, PMUEvent, PMUManifest}

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
    // Unsized: width inference sizes each increment from what drives it.
    require(interval_bridge.io.increments.size == key.events.size && interval_bridge.io.increments.forall(_.widthOption.isEmpty))
  }
}

class IntervalTraceInterfaceSpec extends AnyFlatSpec {
  private val events = Seq(
    IntervalPMUEvent(0, "rob.instructions_retired", "instructions", "Architectural retirements",
      "boom_tile_0", "rob.instructions_retired"),
    IntervalPMUEvent(1, "tile.cycles", "cycles", "Tile cycles", "boom_tile_0", "tile.cycles"))
  private val traceWidths = TraceBundleWidths(4, 40, 32, None, 64, 40)
  private val key = IntervalTraceBridgeKey("boom_tile_0", traceWidths, events)

  private def elaborate(keys: Seq[IntervalTraceBridgeKey]): firrtl.AnnotationSeq = {
    val annotations = new chisel3.stage.phases.Elaborate().transform(
      Seq(chisel3.stage.ChiselGeneratorAnnotation(() => new IntervalBridgeHarness(keys))))
    new chisel3.stage.phases.Convert().transform(annotations)
  }

  "Interval trace metadata" should "reject invalid catalogs" in {
    intercept[IllegalArgumentException] { key.copy(events = events.reverse) }
    intercept[IllegalArgumentException] { key.copy(events = events.updated(1, events(1).copy(name = events.head.name, sourceEvent = events.head.name))) }
    intercept[IllegalArgumentException] { key.copy(events = Seq.empty) }
    intercept[IllegalArgumentException] { key.copy(traceWidths = traceWidths.copy(retireWidth = 0)) }
    intercept[IllegalArgumentException] { key.copy(traceWidths = traceWidths.copy(iaddrWidth = 65)) }
    intercept[IllegalArgumentException] { key.copy(schemaVersion = 2) }
    intercept[IllegalArgumentException] { key.copy(traceWidths = traceWidths.copy(insnWidth = 16)) }
    intercept[IllegalArgumentException] { key.copy(traceWidths = traceWidths.copy(tvalWidth = 0)) }
    intercept[IllegalArgumentException] { key.copy(traceWidths = traceWidths.copy(wdataWidth = Some(0))) }
  }

  it should "accept only a cataloged cycle event as the stall event, by name" in {
    val stalled = key.copy(stallEvent = Some("tile.cycles"))
    assert(stalled.stallIndex.contains(1) && key.stallIndex.isEmpty)
    intercept[IllegalArgumentException] { key.copy(stallEvent = Some("csr.stall_cycles")) }
    intercept[IllegalArgumentException] { key.copy(stallEvent = Some("rob.instructions_retired")) }
    assert(IntervalTraceFormat.manifestJson(stalled).contains("\"stall_event\":\"tile.cycles\""))
    assert(IntervalTraceFormat.manifestJson(key).contains("\"stall_event\":null"))
    assert(IntervalTraceFormat.catalogId(stalled) != IntervalTraceFormat.catalogId(key))
    assert(IntervalTraceFormat.bbWords(stalled) == 5 && IntervalTraceFormat.bbWords(key) == 4)
    // The stall event changes the BB record layout, so the stub must not share a defname.
    val bridges = elaborate(Seq(key, stalled)).collect { case a: BridgeAnnotation => a }
    assert(bridges.map(_.target.module).distinct.size == 2)
    assert(bridges.map(_.widgetConstructorKey).toSet == Set(Some(key), Some(stalled)))
    val decoded = firrtl.annotations.JsonProtocol.deserialize(firrtl.annotations.JsonProtocol.serialize(bridges))
    assert(decoded == bridges)
  }

  it should "convert a tile's event list into a bridge key" in {
    val manifest = PMUManifest("boom_tile_0", Vector(PMUEvent(0, "retired", "instructions", "Retired")))
    val converted = IntervalTraceBridge.key(PMUCatalog.merge(manifest), traceWidths, retirementLatency = 1)
    assert(converted.source == "boom_tile_0" && converted.retirementLatency == 1 && converted.sharedSources.isEmpty)
    assert(converted.events == Seq(IntervalPMUEvent(0, "retired", "instructions", "Retired", "boom_tile_0", "retired")))
    assert(IntervalTraceFormat.manifestJson(converted).contains("\"name\":\"retired\""))
    val stallManifest = PMUManifest("boom_tile_0", Vector(PMUEvent(0, "csr.stall_cycles", "cycles", "Stall")))
    assert(IntervalTraceBridge.key(PMUCatalog.merge(stallManifest), traceWidths, 1, Some("csr.stall_cycles"))
      .stallIndex.contains(0))
  }

  it should "merge shared sources after the tile's events, with prefixed names and catalog version 2" in {
    val tile = PMUManifest("boom_tile_0", Vector(PMUEvent(0, "csr.stall_cycles", "cycles", "Stall"),
      PMUEvent(1, "tile.cycles", "cycles", "Cycles")))
    val l2 = PMUManifest("l2", Vector(PMUEvent(0, "read_line_misses", "lines", "Reads"),
      PMUEvent(1, "writebacks", "lines", "Writebacks")))
    val merged = IntervalTraceBridge.key(PMUCatalog.merge(tile, Seq(l2)), traceWidths, 1, Some("csr.stall_cycles"))
    assert(merged.sharedSources == Seq("l2"))
    assert(merged.events.map(e => (e.id, e.name, e.source, e.sourceEvent)) == Seq(
      (0, "csr.stall_cycles", "boom_tile_0", "csr.stall_cycles"), (1, "tile.cycles", "boom_tile_0", "tile.cycles"),
      (2, "l2.read_line_misses", "l2", "read_line_misses"), (3, "l2.writebacks", "l2", "writebacks")))
    val json = ujson.read(IntervalTraceFormat.manifestJson(merged))
    assert(json("format_version").num == 2)
    assert(json("sources").arr.map(s => (s("name").str, s("shared").bool)) == Seq(("boom_tile_0", false), ("l2", true)))
    assert(json("events")(2)("source").str == "l2" && json("events")(2)("source_event").str == "read_line_misses")
    val unshared = IntervalTraceBridge.key(PMUCatalog.merge(tile), traceWidths, 1, Some("csr.stall_cycles"))
    assert(IntervalTraceFormat.catalogId(merged) != IntervalTraceFormat.catalogId(unshared))
    // Distinct catalogs must not share a stub defname.
    assert(elaborate(Seq(merged, unshared)).collect { case a: BridgeAnnotation => a.target.module }.distinct.size == 2)
    // A shared event cannot be the stall event; names must follow the source rule; sources must be in order.
    intercept[IllegalArgumentException] { merged.copy(stallEvent = Some("l2.writebacks")) }
    intercept[IllegalArgumentException] { merged.copy(events = merged.events.updated(2, merged.events(2).copy(name = "read_line_misses"))) }
    intercept[IllegalArgumentException] { merged.copy(sharedSources = Nil) }
    intercept[IllegalArgumentException] { merged.copy(sharedSources = Seq("l2", "boom_tile_0")) }
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
    // The events name their source, so moving a catalog to another tile renames it there too.
    def onTile(tile: String, tileEvents: Seq[IntervalPMUEvent] = events) =
      key.copy(source = tile, events = tileEvents.map(_.copy(source = tile)))
    val keys = Seq(key, onTile("boom_tile_1"),
      onTile("boom_tile_2", Seq(events.head)).copy(traceWidths = traceWidths.copy(retireWidth = 2, iaddrWidth = 64)),
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
