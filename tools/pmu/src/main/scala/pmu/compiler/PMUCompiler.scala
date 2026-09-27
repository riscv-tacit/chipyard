package pmu.compiler

import firrtl2.{CircuitState, RenameMap, Transform, ir}
import firrtl2.annotations.{Annotation, ReferenceTarget, TargetToken}
import firrtl2.options.Dependency
import firrtl2.stage.{Forms, TransformManager}
import pmu._
import scala.collection.mutable

private object Rename {
  def apply(t: ReferenceTarget, r: RenameMap): ReferenceTarget = r.get(t) match {
    case None => t
    case Some(Seq(next: ReferenceTarget)) => next
    case other => throw new IllegalArgumentException(s"PMU target must have one reference after rename: $t -> $other")
  }
}

final case class ScopeAnnotation(
  manifest: PMUManifest, clock: ReferenceTarget, reset: ReferenceTarget
) extends Annotation {
  def update(r: RenameMap): Seq[Annotation] = Seq(copy(
    clock = Rename(clock, r), reset = Rename(reset, r)))
}

final case class EventAnnotation(
  source: String, id: Int, increment: ReferenceTarget, clock: ReferenceTarget,
  reset: ReferenceTarget, ownerIncrement: ReferenceTarget
) extends Annotation {
  def update(r: RenameMap): Seq[Annotation] = Seq(copy(increment = Rename(increment, r),
    clock = Rename(clock, r), reset = Rename(reset, r), ownerIncrement = Rename(ownerIncrement, r)))
}

final case class ConsumerAnnotation(
  source: String, increments: Seq[ReferenceTarget], clock: ReferenceTarget, reset: ReferenceTarget
) extends Annotation {
  def update(r: RenameMap): Seq[Annotation] = Seq(copy(
    increments = increments.map(Rename(_, r)), clock = Rename(clock, r), reset = Rename(reset, r)))
}

/** Explicit Chisel 6 -> FIRRTL2 transport. The generic bridge drops unknown annotations. */
object PMUCompiler {
  def fromChisel(annotations: firrtl.AnnotationSeq): CircuitState = {
    val convert = firrtl2.bridge.convertTargetToFirrtl2
    val translated = annotations.map {
      case PMUScopeAnnotation(m, c, r) =>
        firrtl2.bridge.Firrtl2AnnotationWrapper(ScopeAnnotation(m, convert(c), convert(r)))
      case PMUEventAnnotation(s, i, v, c, r, b) =>
        firrtl2.bridge.Firrtl2AnnotationWrapper(EventAnnotation(s, i, convert(v), convert(c), convert(r), convert(b)))
      case PMUConsumerAnnotation(s, values, c, r) =>
        firrtl2.bridge.Firrtl2AnnotationWrapper(ConsumerAnnotation(s, values.map(value => convert(value)), convert(c), convert(r)))
      case other => other
    }
    firrtl2.bridge.ChiselBridge.annosToState(translated)
  }

  /** Checked native entry point: resolve widths/domains before LegalizeConnects can truncate. */
  def validate(raw: CircuitState): CircuitState =
    new TransformManager(Seq(Dependency[ValidatePMU])).flattenedTransformOrder
      .foldLeft(raw)((state, transform) => transform.runTransform(state))

  def manifests(checked: CircuitState): Vector[PMUManifest] =
    checked.annotations.collect { case a: ScopeAnnotation =>
      require(a.manifest.finalized, "PMU manifest has not passed compiler validation")
      a.manifest
    }.toVector

  /** Emit a previously checked native circuit, scheduling the emitter's lowering prerequisites. */
  def emitNative(checked: CircuitState): String = {
    val native = nativeState(checked)
    val requested = native.copy(annotations = native.annotations :+
      firrtl2.EmitCircuitAnnotation(classOf[firrtl2.VerilogEmitter]))
    new TransformManager(Seq(Dependency[firrtl2.VerilogEmitter]), Forms.MidForm)
      .flattenedTransformOrder.foldLeft(requested)((s, t) => t.runTransform(s))
      .getEmittedCircuit.value
  }

  /** Consume annotations only after validation, before native optimization removes their wires. */
  def nativeState(checked: CircuitState): CircuitState = {
    manifests(checked)
    checked.copy(annotations = checked.annotations.filterNot(a =>
      a.isInstanceOf[ScopeAnnotation] || a.isInstanceOf[EventAnnotation] || a.isInstanceOf[ConsumerAnnotation]))
  }
}

/** V1 compares wiring aliases and one-bit casts; it never infers equivalence through clock/reset logic. */
final class ValidatePMU extends Transform {
  override def prerequisites = Forms.MidForm
  override def optionalPrerequisiteOf = Seq(Dependency(firrtl2.passes.LegalizeConnects))
  override def invalidates(t: Transform): Boolean = false

  def execute(state: CircuitState): CircuitState = {
    val scopes = state.annotations.collect { case a: ScopeAnnotation => a }
    val events = state.annotations.collect { case a: EventAnnotation => a }
    require(scopes.map(_.manifest.source).distinct.size == scopes.size, "Duplicate PMU source identities")
    val bySource = scopes.map(s => s.manifest.source -> s).toMap
    require(events.forall(e => bySource.contains(e.source)), "PMU event has no owning scope")
    val consumers = state.annotations.collect { case a: ConsumerAnnotation => a }
    require(consumers.forall(c => bySource.contains(c.source)), "PMU consumer has no owning scope")
    val graph = new Connectivity(state.circuit)
    val manifests = scopes.map { scope =>
      val source = scope.manifest.source
      val members = events.filter(_.source == source).sortBy(_.id)
      require(members.map(_.id) == scope.manifest.events.map(_.id), s"PMU $source: missing or duplicate event annotations")
      val owner = graph.location(scope.clock).path
      val clock = graph.domain(scope.clock)
      val reset = graph.domain(scope.reset)
      val finalized = scope.manifest.events.zip(members).map { case (entry, event) =>
        val label = s"PMU $source/${entry.name}"
        require(graph.location(event.increment).path.startsWith(owner), s"$label: producer is outside scope owner")
        require(graph.location(event.ownerIncrement).path == owner,
          s"$label: routed increment is outside scope owner")
        require(graph.domain(event.clock) == clock, s"$label: declared clock differs from owner clock")
        require(graph.domain(event.reset) == reset, s"$label: declared reset differs from owner reset")
        val width = graph.uintWidth(event.increment)
        require(width > 0 && width <= scope.manifest.counterBits,
          s"$label: resolved increment width $width exceeds counterBits=${scope.manifest.counterBits} or is zero")
        require(graph.uintWidth(event.ownerIncrement) == width, s"$label: routed increment width differs from source")
        entry.copy(incrementBits = Some(width))
      }
      source -> scope.manifest.copy(events = finalized, finalized = true)
    }.toMap
    consumers.foreach { consumer =>
      val source = consumer.source
      val scope = bySource(source)
      val members = events.filter(_.source == source).sortBy(_.id)
      require(consumer.increments.size == members.size, s"PMU $source: consumer event count mismatch")
      require(graph.domain(consumer.clock) == graph.domain(scope.clock), s"PMU $source: consumer clock differs from owner")
      require(graph.domain(consumer.reset) == graph.domain(scope.reset), s"PMU $source: consumer reset differs from owner")
      consumer.increments.zip(members).foreach { case (sink, event) =>
        require(graph.uintWidth(sink) == graph.uintWidth(event.ownerIncrement),
          s"PMU $source/${event.id}: consumer width differs from source")
        require(graph.signal(sink) == graph.signal(event.ownerIncrement),
          s"PMU $source/${event.id}: consumer is not wired to the registered increment")
      }
    }
    state.copy(annotations = state.annotations.map {
      case s: ScopeAnnotation => s.copy(manifest = manifests(s.manifest.source))
      case other => other
    })
  }
}

/** Connectivity at middle FIRRTL: aggregate types and last-connect/when expansion are resolved. */
private object Connectivity {
  final case class Location(path: Vector[String], ref: String)
}
private final class Connectivity(circuit: ir.Circuit) {
  import Connectivity.Location
  private final class ModuleInfo(val module: ir.DefModule) {
    val types = mutable.Map.empty[String, ir.Type]
    val drivers = mutable.Map.empty[String, ir.Expression]
    val instances = mutable.Map.empty[String, String]
    val registers = mutable.Set.empty[String]
    // Diplomacy's output AutoBundles contain flipped input clock/reset leaves.
    val inputs = module.ports.flatMap(p => inputLeaves(p.tpe, p.name, p.direction == ir.Input)).toSet
    module.ports.foreach(p => types(p.name) = p.tpe)
    def scan(s: ir.Statement): Unit = s match {
      case ir.DefWire(_, n, t) => types(n) = t
      case ir.DefNode(_, n, e) => types(n) = e.tpe; drivers(n) = e
      case ir.DefRegister(_, n, t, _, _, _) => types(n) = t; registers += n
      case ir.DefInstance(_, n, m, _) => instances(n) = m
      case ir.Connect(_, loc, expr) => drivers(loc.serialize) = expr
      case _: ir.Conditionally => throw new IllegalArgumentException("PMU validation requires expanded conditionals")
      case other => other.foreachStmt(scan)
    }
    module match { case m: ir.Module => scan(m.body); case _ => () }
    lazy val leaves = types.toVector.flatMap { case (n, t) => leafTypes(t, n) }.toMap
  }
  private val modules = circuit.modules.map(m => m.name -> new ModuleInfo(m)).toMap
  private def moduleAt(path: Vector[String]): ModuleInfo =
    modules(path.foldLeft(circuit.main)((m, i) => modules(m).instances(i)))

  def location(t: ReferenceTarget): Location = {
    require(t.circuit == circuit.main && t.module == circuit.main, s"PMU requires an absolute instance target: $t")
    var module = circuit.main
    val path = t.path.map { case (inst, ofModule) =>
      require(modules(module).instances.get(inst.value).contains(ofModule.value), s"PMU target has a stale instance path: $t")
      module = ofModule.value
      inst.value
    }.toVector
    val ref = t.ref + t.component.map {
      case TargetToken.Field(n) => s".$n"
      case TargetToken.Index(n) => s"[$n]"
      case other => throw new IllegalArgumentException(s"Unsupported PMU target selector: $other")
    }.mkString
    Location(path, ref)
  }

  private def inputLeaves(t: ir.Type, name: String, input: Boolean): Vector[String] = t match {
    case ir.BundleType(fields) => fields.toVector.flatMap(f =>
      inputLeaves(f.tpe, s"$name.${f.name}", input != (f.flip == ir.Flip)))
    case ir.VectorType(elem, count) => (0 until count).toVector.flatMap(i => inputLeaves(elem, s"$name[$i]", input))
    case _ => if (input) Vector(name) else Vector.empty
  }

  // Expand aggregate declarations into leaf names without relying on flattened-name conventions.
  private def leafTypes(t: ir.Type, name: String): Vector[(String, ir.Type)] = t match {
    case ir.BundleType(fields) => fields.toVector.flatMap(f => leafTypes(f.tpe, s"$name.${f.name}"))
    case ir.VectorType(elem, count) => (0 until count).toVector.flatMap(i => leafTypes(elem, s"$name[$i]"))
    case _ => Vector(name -> t)
  }
  private def tpe(loc: Location): ir.Type = {
    val leaves = moduleAt(loc.path).leaves
    leaves.getOrElse(loc.ref, throw new IllegalArgumentException(s"Unresolved PMU reference: $loc"))
  }
  def uintWidth(t: ReferenceTarget): Int = tpe(location(t)) match {
    case ir.UIntType(ir.IntWidth(n)) if n.isValidInt => n.toInt
    case other => throw new IllegalArgumentException(s"PMU increment width is unresolved or not UInt: $t ($other)")
  }

  private def oneBit(t: ir.Type): Boolean = t match {
    case ir.ClockType | ir.ResetType | ir.AsyncResetType => true
    case ir.UIntType(ir.IntWidth(n)) => n == 1
    case _ => false
  }
  private def ref(path: Vector[String], e: ir.Expression): Location = {
    val name = e.serialize
    val module = moduleAt(path)
    val instance = name.takeWhile(_ != '.')
    if (module.instances.contains(instance) && name.contains('.'))
      Location(path :+ instance, name.drop(instance.length + 1))
    else Location(path, name)
  }
  /** Canonicalize wiring, not logic. A register, generated-clock node, or blackbox
    * output is an opaque source: aliases of that exact source match, independently
    * generated signals do not. This permits a tile's own reset synchronizer without
    * treating a producer-local delayed reset as equivalent to its input.
    */
  def domain(target: ReferenceTarget): String = canonical(target, requireOneBit = true)
  def signal(target: ReferenceTarget): String = canonical(target, requireOneBit = false)

  private def canonical(target: ReferenceTarget, requireOneBit: Boolean): String = {
    def resolve(loc: Location, seen: Set[Location]): String = {
      require(!seen(loc), s"PMU domain has a wiring cycle at $loc")
      require(!requireOneBit || oneBit(tpe(loc)), s"PMU domain is not a single-bit clock/reset: $loc")
      val module = moduleAt(loc.path)
      if (module.registers(loc.ref.takeWhile(c => c != '.' && c != '['))) return loc.toString
      module.drivers.get(loc.ref) match {
        case Some(e) => expression(loc.path, e, seen + loc, loc)
        case None if module.inputs(loc.ref) && loc.path.nonEmpty =>
          val parent = loc.path.init
          val connection = s"${loc.path.last}.${loc.ref}"
          val driver = moduleAt(parent).drivers.getOrElse(connection,
            throw new IllegalArgumentException(s"Unresolved PMU domain input: $loc"))
          expression(parent, driver, seen + loc, loc)
        case None if module.inputs(loc.ref) && loc.path.isEmpty => loc.toString
        case None if module.module.isInstanceOf[ir.ExtModule] => loc.toString
        case _ => throw new IllegalArgumentException(s"Unresolved PMU domain: $loc")
      }
    }
    def expression(path: Vector[String], e: ir.Expression, seen: Set[Location], origin: Location): String = e match {
      // A width-changing connection is not a transparent wiring alias. In
      // particular, do not hide intermediate truncation followed by extension.
      case _ if !requireOneBit && tpe(origin) != e.tpe => s"converted:$origin"
      case _: ir.Reference | _: ir.SubField | _: ir.SubIndex => resolve(ref(path, e), seen)
      case ir.DoPrim(op, Seq(arg), _, t) if Set("asClock", "asAsyncReset", "asUInt")(op.toString) && oneBit(arg.tpe) && oneBit(t) =>
        expression(path, arg, seen, origin)
      case ir.UIntLiteral(n, _) if !requireOneBit || n == 0 || n == 1 => s"constant:$n"
      case _ => origin.toString
    }
    resolve(location(target), Set.empty)
  }
}
