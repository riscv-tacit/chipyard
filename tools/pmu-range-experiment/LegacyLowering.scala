package pmurangelegacy

import java.nio.file.{Files, Paths}
import firrtl.{ChirrtlForm, CircuitState, Parser}
import firrtl.options.Dependency
import firrtl.stage.Forms
import firrtl.stage.transforms.Compiler

object LegacyLowering {
  def main(args: Array[String]): Unit = {
    val out = Paths.get(args(0))
    val circuit = Parser.parse(Files.readString(out.resolve("raw-chirrtl.fir")))
    // GoldenGateCompilerPhase's pre-GG target lowering, with an empty user
    // TargetTransforms list. This does not run MidasTransforms or bridge extraction.
    val compiler = new Compiler(Seq(
      Dependency[midas.passes.RunConvertAssertsEarly],
      Dependency(firrtl.transforms.formal.ConvertAsserts),
      Dependency[firrtl.passes.memlib.InferReadWrite],
      Dependency[firrtl.transforms.SimplifyMems]) ++ Forms.LowForm)
    val state = CircuitState(circuit, ChirrtlForm, Seq(
      firrtl.passes.memlib.InferReadWriteAnnotation,
      firrtl.passes.memlib.DefaultReadFirstAnnotation,
      firrtl.passes.memlib.PassthroughSimpleSyncReadMemsAnnotation))
    Files.writeString(out.resolve("legacy-pass-order.txt"), compiler.prettyPrint("  "))
    val result = compiler.execute(state)
    Files.writeString(out.resolve("legacy-pre-gg.fir"), result.circuit.serialize)
    println("PASS legacy FIRRTL 1.6.0: Golden Gate pre-target lowering accepted the Chisel 6 / FIRRTL2 circuit.")
  }
}
