package chipyard.config

import org.chipsalliance.cde.config._
import freechips.rocketchip.diplomacy._
import freechips.rocketchip.subsystem._
import freechips.rocketchip.tilelink._
import freechips.rocketchip.devices.tilelink.PhysicalFilter
import freechips.rocketchip.util.ElaborationArtefacts
import sifive.blocks.inclusivecache._
import chipyard.{HasPMUSources, InclusiveCacheOuterEvents, PMUSharedSourcesKey}

/** WithInclusiveCache with InclusiveCacheOuterEvents on the L2's outer port (PMU source "l2"). Place it ABOVE
  * WithInclusiveCache: it reads the same InclusiveCacheKey and replaces only the coherence manager.
  *
  * The coherence-manager body is copied from rocket-chip-inclusive-cache
  * design/craft/inclusivecache/src/Configs.scala (WithInclusiveCache, submodule commit 8e157c8).
  * Lines marked [PMU] are the only differences. Compare again when that submodule is bumped:
  * the cork turns Acquire into Get with param 0, so the counting node cannot sit after it and the
  * upstream function cannot be wrapped.
  */
class WithInclusiveCachePMUEvents extends Config((site, here, up) => {
  case SubsystemBankedCoherenceKey => up(SubsystemBankedCoherenceKey, site).copy(coherenceManager = { context =>
    implicit val p = context.p
    val sbus = context.tlBusWrapperLocationMap(SBUS)
    val cbus = context.tlBusWrapperLocationMap.lift(CBUS).getOrElse(sbus)
    val InclusiveCacheParams(
      ways,
      sets,
      writeBytes,
      portFactor,
      memCycles,
      physicalFilter,
      hintsSkipProbe,
      bankedControl,
      ctrlAddr,
      bufInnerInterior,
      bufInnerExterior,
      bufOuterInterior,
      bufOuterExterior) = p(InclusiveCacheKey)

    val l2Ctrl = ctrlAddr.map { addr =>
      InclusiveCacheControlParameters(
        address = addr,
        beatBytes = cbus.beatBytes,
        bankedControl = bankedControl)
    }
    val l2 = LazyModule(new InclusiveCache(
      CacheParameters(
        level = 2,
        ways = ways,
        sets = sets,
        blockBytes = sbus.blockBytes,
        beatBytes = sbus.beatBytes,
        hintsSkipProbe = hintsSkipProbe),
      InclusiveCacheMicroParameters(
        writeBytes = writeBytes,
        portFactor = portFactor,
        memCycles = memCycles,
        innerBuf = bufInnerInterior,
        outerBuf = bufOuterInterior),
      l2Ctrl))

    def skipMMIO(x: TLClientParameters) = {
      val dcacheMMIO =
        x.requestFifo &&
        x.sourceId.start % 2 == 1 && // 1 => dcache issues acquires from another master
        x.nodePath.last.name == "dcache.node"
      if (dcacheMMIO) None else Some(x)
    }

    val filter = LazyModule(new TLFilter(cfilter = skipMMIO))
    val l2_inner_buffer = bufInnerExterior()
    val l2_outer_buffer = bufOuterExterior()
    val l2_outer_events = LazyModule(new InclusiveCacheOuterEvents)  // [PMU]
    val cork = LazyModule(new TLCacheCork)
    val lastLevelNode = cork.node

    l2_inner_buffer.suggestName("InclusiveCache_inner_TLBuffer")
    l2_outer_buffer.suggestName("InclusiveCache_outer_TLBuffer")
    l2_outer_events.suggestName("InclusiveCache_outer_events")  // [PMU]

    l2_inner_buffer.node :*= filter.node
    l2.node :*= l2_inner_buffer.node
    l2_outer_buffer.node :*= l2.node
    l2_outer_events.node :*= l2_outer_buffer.node  // [PMU]

    /* PhysicalFilters need to be on the TL-C side of a CacheCork to prevent Acquire.NtoB -> Grant.toT */
    physicalFilter match {
      case None => lastLevelNode :*= l2_outer_events.node  // [PMU] was l2_outer_buffer.node
      case Some(fp) => {
        val physicalFilter = LazyModule(new PhysicalFilter(fp.copy(controlBeatBytes = cbus.beatBytes)))
        lastLevelNode :*= physicalFilter.node :*= l2_outer_events.node  // [PMU] was l2_outer_buffer.node
        physicalFilter.controlNode := cbus.coupleTo("physical_filter") {
          TLBuffer(1) := TLFragmenter(cbus, Some("LLCPhysicalFilter")) := _
        }
      }
    }

    l2.ctrls.foreach {
      _.ctrlnode := cbus.coupleTo("l2_ctrl") { TLBuffer(1) := TLFragmenter(cbus, Some("LLCCtrl")) := _ }
    }

  // [PMU] Register the events with the subsystem under their declared name.
    context match {
      case s: HasPMUSources => s.addPMUSource("l2", () => l2_outer_events.module.pmuEvents)
      case other => throw new IllegalArgumentException(
        s"WithInclusiveCachePMUEvents needs a subsystem with HasPMUSources, got ${other.getClass.getName}")
    }

    ElaborationArtefacts.add("l2.json", l2.module.json)
    (filter.node, lastLevelNode, None)
  })
  // [PMU] Idempotent, so the fragment may appear twice in a stack (e.g. reapplied above a
  // later WithInclusiveCache); only the topmost coherence manager is built.
  case PMUSharedSourcesKey => (up(PMUSharedSourcesKey, site) :+ "l2").distinct
})
