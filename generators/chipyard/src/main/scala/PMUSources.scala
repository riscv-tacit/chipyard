package chipyard

import chisel3._
import chisel3.util.PopCount
import org.chipsalliance.cde.config.{Field, Parameters}
import freechips.rocketchip.diplomacy._
import freechips.rocketchip.subsystem.BaseSubsystem
import freechips.rocketchip.tilelink._
import freechips.rocketchip.util.ElaborationArtefacts
import pmu.{CanHavePMUEvents, PMUEvents, PMURegistry, PMUSystemIndex}

import scala.collection.mutable

/** PMU event sources outside the tiles (for example "l2"), in their declared order. Each
  * name must be registered with HasPMUSources.addPMUSource by the fragment that builds it.
  */
case object PMUSharedSourcesKey extends Field[Seq[String]](Nil)

/** Shared PMU event sources, registered by name while the subsystem builds its buses. */
trait HasPMUSources { this: BaseSubsystem =>
  // Lazy: coherence managers are built in BaseSubsystem's constructor, before this
  // trait's fields would otherwise be initialized.
  private lazy val registeredPMUSources = mutable.LinkedHashMap.empty[String, () => Option[PMUEvents]]

  /** Register a shared source. `events` is read after the source's module is elaborated. */
  def addPMUSource(name: String, events: () => Option[PMUEvents]): Unit = {
    require(!registeredPMUSources.contains(name), s"PMU source '$name' registered twice")
    registeredPMUSources(name) = events
  }

  /** The shared sources' events, in PMUSharedSourcesKey order. Call from module code. */
  def pmuSharedEvents: Seq[PMUEvents] = {
    val declared = p(PMUSharedSourcesKey)
    require(declared.distinct.size == declared.size, s"PMUSharedSourcesKey has duplicates: ${declared.mkString(", ")}")
    val registered = registeredPMUSources.keys.toSeq
    require(declared.toSet == registered.toSet,
      s"PMU shared sources declared (${declared.mkString(", ")}) differ from those built (${registered.mkString(", ")})")
    declared.map { name =>
      registeredPMUSources(name)().getOrElse(throw new IllegalArgumentException(s"PMU source '$name' has no events"))
    }
  }
}

/** Writes `<design>.pmu-sources.json`: every PMU source, tiles by tile ID then shared sources. */
trait HasPMUSourcesModuleImp { this: ChipyardSubsystemModuleImp[ChipyardSubsystem] =>
  // Runs after the subsystem's children (tiles, coherence managers) are elaborated.
  private val tileManifests = outer.totalTiles.toSeq.sortBy(_._1).flatMap { case (_, tile) =>
    tile.module match {
      case m: CanHavePMUEvents => m.pmuEvents.map(_.manifest)
      case _ => None
    }
  }
  private val sharedManifests = outer.pmuSharedEvents.map(_.manifest)
  if (tileManifests.nonEmpty || sharedManifests.nonEmpty) {
    ElaborationArtefacts.add("pmu-sources.json", PMUSystemIndex.toJson(tileManifests, sharedManifests))
  }
}

/** PMU events on the inclusive cache's outer TileLink port, between the L2 and the cache cork.
  * One edge per L2 bank; counts are summed over banks. Source name "l2".
  */
class InclusiveCacheOuterEvents(implicit p: Parameters) extends LazyModule {
  // An identity node connects out to in by itself; the module only observes.
  val node = TLIdentityNode()

  lazy val module = new Impl
  class Impl extends LazyModuleImp(this) with CanHavePMUEvents {
    private val ports = node.in

    // Only AcquireBlock reads the line from memory. AcquirePerm (the L2 asks for write
    // permission on a full-line write, e.g. block-device DMA) is granted by the cork without
    // a memory read, so it is not counted. Acquire carries no data: one beat per message.
    private def acquires(param: UInt): UInt = PopCount(ports.map { case (tl, _) =>
      tl.a.fire && tl.a.bits.opcode === TLMessages.AcquireBlock && tl.a.bits.param === param
    })
    // ReleaseData is multi-beat: count its first beat.
    private val release_data = PopCount(ports.map { case (tl, edge) =>
      tl.c.fire && edge.first(tl.c) && tl.c.bits.opcode === TLMessages.ReleaseData
    })

    private val registry = new PMURegistry("l2")
    registry.register("read_line_misses", acquires(TLPermissions.NtoB), unit = "lines",
      description = "Lines the L2 read from memory with read permission (outer AcquireBlock NtoB)")
    registry.register("write_line_misses", acquires(TLPermissions.NtoT), unit = "lines",
      description = "Lines the L2 read from memory with write permission (outer AcquireBlock NtoT); upgrades (BtoT) and full-line writes (AcquirePerm) excluded")
    registry.register("writebacks", release_data, unit = "lines",
      description = "Dirty lines the L2 evicted to memory (outer ReleaseData)")

    val pmuEvents = Some(registry.build())
    ElaborationArtefacts.add("l2.pmu.json", pmuEvents.get.manifest.toJson)
  }
}
