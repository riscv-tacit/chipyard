package pmurange

import chisel3._
import chisel3.util._
import chisel3.util.experimental.BoringUtils
import firrtl2.{CircuitState, ir}
import firrtl2.stage.{Forms, TransformManager}
import scala.collection.mutable
import java.nio.file.{Files, Paths}

final class Observations {
  val sources = mutable.ArrayBuffer.empty[(String, UInt)]
  val rows = mutable.ArrayBuffer.empty[ujson.Value]
  def inspect(phase: String, name: String, value: UInt): Unit = {
    rows += ujson.Obj("stage" -> phase, "name" -> name,
      "width" -> value.widthOption.map(w => ujson.Num(w)).getOrElse(ujson.Null),
      "literal" -> value.litOption.map(v => ujson.Str(v.toString)).getOrElse(ujson.Null))
  }
  def register(name: String, wire: UInt): Unit = {
    sources += name -> wire
    inspect("registration", name, wire)
  }
}

class NestedProducer extends Module {
  val bits = IO(Input(UInt(4.W)))
  val increment = WireInit(PopCount(bits))
}

class RangeDUT(obs: Observations) extends Module {
  val bits = IO(Input(UInt(4.W)))
  val raw = IO(Input(UInt(8.W)))
  val sel = IO(Input(Bool()))

  def publish(name: String, wire: UInt): Unit = {
    wire.suggestName(name)
    obs.register(name, wire)
    val out = IO(Output(UInt())).suggestName(s"out_$name")
    out := wire
  }
  obs.inspect("expression", "constant_literal", 1.U)
  publish("constant", WireInit(1.U))
  publish("explicit_constant", WireInit(UInt(1.W), 1.U))
  publish("boolean", WireInit(sel.asUInt))
  val popExpr = PopCount(bits)
  obs.inspect("expression", "pop4_expression", popExpr)
  val pop = WireInit(popExpr)
  publish("pop4", pop)
  publish("explicit_pop", WireInit(UInt(3.W), popExpr))
  val narrowed = Wire(UInt(2.W)); narrowed := pop
  publish("narrowed_pop", narrowed)
  publish("sum4", WireInit(bits.asBools.map(_.asUInt).reduce(_ +& _)))
  val padded = Wire(UInt(8.W)); padded := pop
  publish("padded_pop", padded)
  publish("mux_constants", WireInit(Mux(sel, 2.U, 5.U)))
  publish("masked", WireInit(raw & 15.U))
  publish("trunc_sum", WireInit(raw(2, 0) + 3.U(3.W)))
  publish("masked_slice", WireInit((raw & 15.U)(7, 4)))
  publish("correlated", WireInit(sel.asUInt +& (!sel).asUInt))
  val state = RegInit(0.U(8.W)); state := state + 1.U
  publish("state", WireInit(state))
  publish("registered_pop", WireInit(RegNext(pop, 0.U)))
  val deferred = Wire(UInt())
  publish("deferred", deferred) // Width inspection intentionally precedes assignment.
  deferred := pop
  val last = WireDefault(15.U(4.W)); last := 1.U
  publish("last_connect", last)
  val conditional = WireDefault(0.U(3.W))
  when(sel) { conditional := 4.U }
  publish("conditional", conditional)
  publish("unsupported_div", WireInit(raw / 3.U))
  val child = Module(new NestedProducer)
  child.bits := bits
  publish("bored_pop", WireInit(BoringUtils.bore(child.increment)))
  obs.sources.foreach { case (name, wire) => obs.inspect("owner_end", name, wire) }
}

// Deliberately small unsigned interval analysis, not a production compiler pass.
// Exact evaluation below independently checks the transfer rules on this DUT.
final case class Bounds(lo: BigInt, hi: BigInt, reasons: Set[String] = Set.empty) {
  def json: ujson.Value = ujson.Obj("min" -> lo.toString, "max" -> hi.toString,
    "reasons" -> ujson.Arr.from(reasons.toSeq.sorted))
}
final class Graph(circuit: ir.Circuit) {
  final class ModuleGraph(val module: ir.Module) {
    val types = mutable.Map.empty[String, ir.Type]
    val drivers = mutable.Map.empty[String, ir.Expression]
    val registers = mutable.Set.empty[String]
    val conditional = mutable.Set.empty[String]
    val instances = mutable.Map.empty[String, String]
    module.ports.foreach(p => types(p.name) = p.tpe)
    def scan(s: ir.Statement, guarded: Boolean = false): Unit = s match {
      case ir.Block(stmts) => stmts.foreach(scan(_, guarded))
      case ir.Conditionally(_, _, yes, no) => scan(yes, true); scan(no, true)
      case ir.DefWire(_, name, tpe) => types(name) = tpe
      case ir.DefNode(_, name, expr) => types(name) = expr.tpe; drivers(name) = expr
      case ir.DefRegister(_, name, tpe, _, _, _) => types(name) = tpe; registers += name
      case ir.DefInstance(_, name, mod, _) => instances(name) = mod
      case ir.Connect(_, loc, expr) =>
        drivers(loc.serialize) = expr
        if (guarded) conditional += loc.serialize
      case ir.EmptyStmt => ()
      case other => other.foreachStmt(scan(_, guarded))
    }
    scan(module.body)
  }
  val modules = circuit.modules.collect { case m: ir.Module => m.name -> new ModuleGraph(m) }.toMap
  def width(t: ir.Type): Option[Int] = t match {
    case ir.UIntType(ir.IntWidth(w)) => Some(w.toInt)
    case _ => None
  }
  def full(t: ir.Type, reason: String): Option[Bounds] =
    width(t).map(w => Bounds(0, (BigInt(1) << w) - 1, Set(reason)))
  def narrow(b: Bounds, t: ir.Type): Option[Bounds] = width(t).map { w =>
    val modulus = BigInt(1) << w
    if (b.hi < modulus) b
    else if (b.lo / modulus == b.hi / modulus)
      Bounds(b.lo % modulus, b.hi % modulus, b.reasons)
    else Bounds(0, modulus - 1, b.reasons + "modular-wrap")
  }.orElse(Some(b))

  // Follow an instance output into its defining module. Inputs conservatively use
  // their declared width; this experiment does not specialize input ranges per instance.
  def bound(module: String, expr: ir.Expression, seen: Set[(String, String)] = Set.empty): Option[Bounds] = {
    val g = modules(module)
    def recur(e: ir.Expression) = bound(module, e, seen)
    def named(name: String, tpe: ir.Type): Option[Bounds] = {
      val t = g.types.getOrElse(name, tpe)
      if (g.registers(name)) full(t, "register-boundary")
      else if (g.conditional(name)) full(t, "conditional-driver-before-expand-whens")
      else if (seen((module, name))) full(t, "cycle")
      else g.drivers.get(name).flatMap(e => bound(module, e, seen + ((module, name))))
        .flatMap(narrow(_, t)).orElse(full(t, "declared-width"))
    }
    expr match {
      case ir.UIntLiteral(value, _) => Some(Bounds(value, value))
      case ir.Reference(name, tpe, _, _) => named(name, tpe)
      case ir.SubField(ir.Reference(inst, _, _, _), port, _, _) if g.instances.contains(inst) =>
        val child = g.instances(inst)
        bound(child, ir.Reference(port, modules(child).types(port)), seen)
      case ir.Mux(_, a, b, t) => for (x <- recur(a); y <- recur(b); r <- narrow(
        Bounds(x.lo.min(y.lo), x.hi.max(y.hi), x.reasons ++ y.reasons), t)) yield r
      case ir.DoPrim(op, args, consts, t) =>
        val values = args.map(recur)
        val result = if (values.forall(_.nonEmpty)) {
          val v = values.map(_.get)
          val reasons = v.flatMap(_.reasons).toSet
          def make(lo: BigInt, hi: BigInt) = Some(Bounds(lo, hi, reasons))
          op.toString match {
            case "add" => make(v(0).lo + v(1).lo, v(0).hi + v(1).hi)
            case "pad" | "asUInt" => Some(v.head)
            case "bits" =>
              val low = consts(1).toInt
              val w = (consts(0) - consts(1) + 1).toInt
              narrow(Bounds(v.head.lo >> low, v.head.hi >> low, reasons), ir.UIntType(ir.IntWidth(w)))
            case "tail" =>
              width(args.head.tpe).flatMap(w => narrow(v.head, ir.UIntType(ir.IntWidth(w - consts.head.toInt)))).orElse(full(t, "unknown-tail-width"))
            case "shr" => make(v.head.lo >> consts.head.toInt, v.head.hi >> consts.head.toInt)
            case "shl" => make(v.head.lo << consts.head.toInt, v.head.hi << consts.head.toInt)
            case "and" => make(0, v(0).hi.min(v(1).hi))
            case "not" => width(args.head.tpe).map { w =>
              val mask = (BigInt(1) << w) - 1
              Bounds(mask - v.head.hi, mask - v.head.lo, reasons)
            }
            case "eq" | "neq" | "lt" | "leq" | "gt" | "geq" | "andr" | "orr" | "xorr" => make(0, 1)
            case "cat" => width(args(1).tpe).map(w =>
              Bounds((v(0).lo << w) + v(1).lo, (v(0).hi << w) + v(1).hi, reasons))
            case _ => full(t, s"unsupported:$op")
          }
        } else None
        result.flatMap(narrow(_, t)).orElse(full(t, s"unknown-operands:$op"))
      case _ => full(expr.tpe, "unsupported-expression")
    }
  }

  // Concrete bit-vector evaluator: all top inputs AND register states are supplied.
  // It is intentionally separate from the interval transfer functions.
  def exact(module: String, expr: ir.Expression, inputs: Map[String, BigInt], states: Map[String, BigInt]): BigInt = {
    val g = modules(module)
    def mask(v: BigInt, t: ir.Type): BigInt = width(t).map(w => v & ((BigInt(1) << w) - 1)).getOrElse(v)
    def ev(e: ir.Expression): BigInt = exact(module, e, inputs, states)
    expr match {
      case ir.UIntLiteral(v, _) => v
      case ir.Reference(n, t, _, _) =>
        if (g.registers(n)) states(n)
        else inputs.get(n).getOrElse(mask(ev(g.drivers(n)), g.types.getOrElse(n, t)))
      case ir.SubField(ir.Reference(inst, _, _, _), port, _, _) =>
        val child = g.instances(inst)
        val childInputs = modules(child).module.ports.filter(_.direction == ir.Input).flatMap { p =>
          g.drivers.get(s"$inst.${p.name}").map(e => p.name -> ev(e))
        }.toMap
        exact(child, ir.Reference(port, modules(child).types(port)), childInputs, Map.empty)
      case ir.Mux(c, a, b, t) => mask(if (ev(c) != 0) ev(a) else ev(b), t)
      case ir.DoPrim(op, args, c, t) =>
        val a = args.map(ev)
        val v = op.toString match {
          case "add" => a(0) + a(1)
          case "pad" | "asUInt" => a.head
          case "bits" => (a.head >> c(1).toInt) & ((BigInt(1) << (c(0) - c(1) + 1).toInt) - 1)
          case "tail" => a.head & ((BigInt(1) << (width(args.head.tpe).get - c.head.toInt)) - 1)
          case "shr" => a.head >> c.head.toInt
          case "shl" => a.head << c.head.toInt
          case "and" => a(0) & a(1)
          case "or" => a(0) | a(1)
          case "xor" => a(0) ^ a(1)
          case "not" => ~a.head
          case "cat" => (a(0) << width(args(1).tpe).get) | a(1)
          case "eq" => if (a(0) == a(1)) BigInt(1) else BigInt(0)
          case "neq" => if (a(0) != a(1)) BigInt(1) else BigInt(0)
          case "div" => a(0) / a(1)
          case other => sys.error(s"Concrete evaluator missing $other")
        }
        mask(v, t)
      case other => sys.error(s"Concrete evaluator missing ${other.serialize}")
    }
  }
}

object RangeExperiment {
  def main(args: Array[String]): Unit = {
    val out = Paths.get(args(0)); Files.createDirectories(out)
    def write(name: String, value: String): Unit = Files.writeString(out.resolve(name), value)
    val obs = new Observations
    val elaborated = new chisel3.stage.phases.Elaborate().transform(
      Seq(chisel3.stage.ChiselGeneratorAnnotation(() => new RangeDUT(obs))))
    val converted = new chisel3.stage.phases.Convert().transform(elaborated)
    val native = converted.collectFirst { case firrtl.stage.FirrtlCircuitAnnotation(c) => c }.get
    write("native-chisel.fir", native.serialize)
    val raw = firrtl2.bridge.ChiselBridge.annosToState(converted)
    obs.sources.foreach { case (n, w) => obs.inspect("after_elaboration", n, w) }
    write("chisel-observations.json", ujson.write(ujson.Arr.from(obs.rows), indent = 2))
    if (args.contains("--emit-only")) {
      write("raw-chirrtl.fir", raw.circuit.serialize)
      return
    }
    val legacyPath = out.resolve("legacy-pre-gg.fir")
    // FIRRTL text omits expression types; restore them after the cross-JVM parse.
    val legacy = if (Files.exists(legacyPath)) Seq("legacy-pre-gg" ->
      new TransformManager(Forms.Resolved).transform(
        CircuitState(firrtl2.Parser.parse(Files.readString(legacyPath))))) else Seq.empty
    val stages = Seq("raw-chirrtl" -> raw,
      "resolved-high" -> new TransformManager(Forms.Resolved).transform(raw),
      "middle" -> new TransformManager(Forms.MidForm).transform(raw),
      "low-optimized" -> new TransformManager(Forms.LowFormOptimized).transform(raw)) ++ legacy
    val rows = mutable.ArrayBuffer.empty[ujson.Value]
    stages.foreach { case (stage, state) =>
      write(s"$stage.fir", state.circuit.serialize)
      val graph = new Graph(state.circuit)
      val top = graph.modules(state.circuit.main)
      top.module.ports.filter(_.name.startsWith("out_")).foreach { p =>
        val b = graph.bound(state.circuit.main, ir.Reference(p.name, p.tpe))
        rows += ujson.Obj("stage" -> stage, "name" -> p.name.stripPrefix("out_"),
          "width" -> graph.width(p.tpe).map(w => ujson.Num(w)).getOrElse(ujson.Null),
          "range" -> b.map(_.json).getOrElse(ujson.Null))
      }
    }
    write("ranges.json", ujson.write(ujson.Arr.from(rows), indent = 2))
    val summary = new StringBuilder("stage,event,width,min,max,reasons\n")
    rows.foreach { row =>
      val range = row("range")
      val lo = if (range == ujson.Null) "?" else range("min").str
      val hi = if (range == ujson.Null) "?" else range("max").str
      val why = if (range == ujson.Null) "unresolved" else range("reasons").arr.map(_.str).mkString(";")
      summary.append(s"${row("stage").str},${row("name").str},${row("width")},$lo,$hi,$why\n")
    }
    write("ranges.csv", summary.toString)
    // Check expression semantics against a hand-written oracle for every top input.
    // Registers are cut points: enumerate their full representable states separately.
    val validation = mutable.ArrayBuffer.empty[ujson.Value]
    stages.filter(x => Set("middle", "low-optimized", "legacy-pre-gg")(x._1)).foreach { case (stage, state) =>
      val graph = new Graph(state.circuit)
      val top = graph.modules(state.circuit.main)
      val outputs = top.module.ports.filter(_.name.startsWith("out_"))
      val ranges = outputs.map(p => p.name -> graph.bound(state.circuit.main, ir.Reference(p.name, p.tpe)).get).toMap
      val observed = mutable.Map.empty[String, (BigInt, BigInt)]
      val registers = top.registers.toSeq.sorted
      require(registers.size == 2, s"Unexpected registers: $registers")
      val accumulator = registers.find(n => graph.width(top.types(n)).contains(8)).get
      val pipeline = registers.find(n => graph.width(top.types(n)).contains(3)).get
      var checks = 0L
      def check(bits: Int, raw: Int, sel: Int, acc: Int, piped: Int): Unit = {
        val pop = Integer.bitCount(bits)
        val oracle = Map[String, BigInt](
          "constant" -> 1, "explicit_constant" -> 1, "boolean" -> sel,
          "pop4" -> pop, "explicit_pop" -> pop, "narrowed_pop" -> (pop & 3),
          "sum4" -> pop, "padded_pop" -> pop, "mux_constants" -> (if (sel == 1) 2 else 5),
          "masked" -> (raw & 15), "trunc_sum" -> (((raw & 7) + 3) & 7),
          "masked_slice" -> 0, "correlated" -> 1, "state" -> acc,
          "registered_pop" -> piped, "deferred" -> pop, "last_connect" -> 1,
          "conditional" -> (if (sel == 1) 4 else 0), "unsupported_div" -> (raw / 3),
          "bored_pop" -> pop)
        val inputs = Map[String, BigInt]("bits" -> bits, "raw" -> raw, "sel" -> sel, "clock" -> 0, "reset" -> 0)
        val states = Map[String, BigInt](accumulator -> acc, pipeline -> piped)
        outputs.foreach { p =>
          val actual = graph.exact(state.circuit.main, ir.Reference(p.name, p.tpe), inputs, states)
          require(actual == oracle(p.name.stripPrefix("out_")), s"$stage ${p.name}: $actual != oracle")
          val b = ranges(p.name)
          require(b.lo <= actual && actual <= b.hi, s"UNSOUND $stage ${p.name}: $actual outside $b")
          val (lo, hi) = observed.getOrElse(p.name, (actual, actual))
          observed(p.name) = (lo.min(actual), hi.max(actual))
          checks += 1
        }
      }
      for (bits <- 0 until 16; raw <- 0 until 256; sel <- 0 until 2) check(bits, raw, sel, 0, 0)
      for (acc <- 0 until 256; piped <- 0 until 8) check(0, 0, 0, acc, piped)
      // These cones do not mix state and primary inputs. The two sweeps therefore
      // enumerate each output's complete input/state domain without a huge Cartesian product.
      outputs.foreach { p =>
        val (lo, hi) = observed(p.name)
        validation += ujson.Obj("stage" -> stage, "name" -> p.name.stripPrefix("out_"),
          "enumeratedMin" -> lo.toString, "enumeratedMax" -> hi.toString,
          "inferred" -> ranges(p.name).json)
      }
      println(s"PASS $stage: $checks output checks against independent formulas; every value inside its inferred interval.")
    }
    // Bounds inferred at earlier stages must also contain every concrete value
    // seen after lowering; an unknown range is explicitly left unresolved.
    val concrete = validation.filter(_("stage").str == "middle").map(v => v("name").str -> v).toMap
    rows.filter(_("range") != ujson.Null).foreach { row =>
      val actual = concrete(row("name").str)
      require(BigInt(row("range")("min").str) <= BigInt(actual("enumeratedMin").str))
      require(BigInt(row("range")("max").str) >= BigInt(actual("enumeratedMax").str))
    }
    write("validation.json", ujson.write(ujson.Arr.from(validation), indent = 2))
    obs.sources.foreach { case (n, w) => obs.inspect("after_firrtl_compilation", n, w) }
    write("chisel-observations.json", ujson.write(ujson.Arr.from(obs.rows), indent = 2))
    println(summary.toString)
  }
}
