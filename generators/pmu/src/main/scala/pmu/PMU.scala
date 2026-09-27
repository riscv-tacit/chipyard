package pmu

import chisel3._
import chisel3.experimental.{annotate, requireIsHardware, ChiselAnnotation}
import chisel3.reflect.DataMirror
import chisel3.util.experimental.BoringUtils
import org.chipsalliance.cde.config.Field
import scala.collection.mutable.ArrayBuffer

/** Optional transport through CDE; callers may also pass a registry directly. */
case object PMUScopeKey extends Field[Option[PMURegistry]](None)

/** A sealed event catalog and its observation wires, with no counter hardware.
  * Hardware handles are local to the module that called registry.build().
  * Widths in the manifest remain provisional until compiler validation.
  */
final class PMUEvents private[pmu] (
  val increments: Vector[UInt], val clock: Clock, val reset: Reset,
  val manifest: PMUManifest
)

/** One registry per observation scope. The caller supplies the same instance to its producers. */
final class PMURegistry(val source: String) {
  require(source.trim.nonEmpty, "PMU source must be nonempty")
  private case class Entry(name: String, increment: UInt, clock: Clock, reset: Reset, unit: String, description: String)
  private val entries = ArrayBuffer.empty[Entry]
  private var built = false

  /** Observe the increment in the caller's active Chisel clock/reset context. */
  def register(name: String, increment: UInt, unit: String = "events", description: String = ""): Unit =
    register(name, increment, Module.clock, Module.reset, unit, description)

  /** Explicit-context registration, including from RawModule producers. */
  def register(
    name: String, increment: UInt, clock: Clock, reset: Reset, unit: String, description: String
  ): Unit = {
    require(!built, s"PMU $source: registration after build")
    require(name.trim.nonEmpty, s"PMU $source: event name must be nonempty")
    require(!entries.exists(_.name == name), s"PMU $source: duplicate event '$name'")
    requireIsHardware(increment, s"PMU $source/$name increment")
    require(DataMirror.isWire(increment), s"PMU $source/$name: increment must be a bound wire (including constants)")
    require(DataMirror.isVisible(increment), s"PMU $source/$name: register the wire in its producer context")
    checkDomainSignal(clock, "clock")
    checkDomainSignal(reset, "reset")
    entries += Entry(name, increment, clock, reset, unit, description)
  }

  private def checkDomainSignal(signal: Data, kind: String): Unit = {
    requireIsHardware(signal, s"PMU $source $kind")
    require(!signal.isLit, s"PMU $source: $kind must be targetable; put constants in a wire")
  }

  /** Seal registration, route width-preserving event wires, and emit annotations.
    * Call once in the owning module after its producers. This creates no counters.
    * Compiler validation is mandatory even without a native consumer.
    */
  def build(counterBits: Int = 64): PMUEvents = {
    require(!built, s"PMU $source: build called twice")
    require(counterBits > 0, s"PMU $source: counterBits must be positive")
    require(entries.nonEmpty, s"PMU $source: cannot build an empty registry")
    // Local aliases anchor the scope even when the active domain comes from a parent.
    val owner_clock = WireInit(Module.clock)
    val owner_reset = WireInit(Module.reset)
    checkDomainSignal(owner_clock, "owner clock")
    checkDomainSignal(owner_reset, "owner reset")
    val manifest = PMUManifest(source, counterBits, entries.zipWithIndex.map { case (e, id) =>
      require(e.increment.widthOption.forall(w => w > 0 && w <= counterBits),
        s"PMU $source/${e.name}: increment width exceeds counterBits=$counterBits or is zero")
      PMUEvent(id, e.name, e.unit, e.description, e.increment.widthOption)
    }.toVector)
    built = true

    // Each wire keeps its own inferred width; no padding to the accumulator width.
    val increments = entries.map(e => WireInit(BoringUtils.bore(e.increment))).toVector
    annotate(new ChiselAnnotation {
      def toFirrtl = PMUScopeAnnotation(manifest,
        owner_clock.toAbsoluteTarget, owner_reset.toAbsoluteTarget)
    })
    entries.zipWithIndex.foreach { case (e, id) =>
      annotate(new ChiselAnnotation {
        def toFirrtl = PMUEventAnnotation(source, id, e.increment.toAbsoluteTarget,
          e.clock.toAbsoluteTarget, e.reset.toAbsoluteTarget, increments(id).toAbsoluteTarget)
      })
    }
    new PMUEvents(increments, owner_clock, owner_reset, manifest)
  }
}

/** Raw, same-cycle increments. Unknown Chisel widths remain unknown until the
  * compiler resolves them; there is no accumulator-width padding at this port.
  */
class PMUEventBundle(incrementWidths: Seq[Option[Int]]) extends Bundle {
  val increments = chisel3.util.MixedVec(incrementWidths.map {
    case Some(bits) => UInt(bits.W)
    case None => UInt()
  })
}
