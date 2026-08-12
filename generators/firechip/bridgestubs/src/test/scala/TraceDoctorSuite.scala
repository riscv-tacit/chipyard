// See LICENSE for license details.

package firechip.bridgestubs

import java.io._

import org.scalatest.Suites
import org.scalatest.matchers.should._

import firesim.BasePlatformConfig

abstract class TraceDoctorTestBase(
  platformConfig: BasePlatformConfig,
  width:          Int,
) extends BridgeSuite("TraceDoctorModule", s"TraceDoctorModuleTraceWidth${width}", platformConfig) {
  override def defineTests(backend: String, debug: Boolean) {
    it should "dump raw trace tokens matching the poked vectors" in {
      // TestSuiteCommon.run() silently skips backends whose command is not
      // on PATH (notably 'vcs-post-synth', which is a make target, not a
      // command); cancel instead of vacuously comparing two empty files.
      assume(isCmdAvailable(backend), s"'$backend' command not available to run the simulation")

      // The C++ test harness (bridges/test/TraceDoctorModule.cc) writes the
      // tokens it expects here.
      val expected = File.createTempFile("expected", ".bin")
      expected.deleteOnExit()

      val output = File.createTempFile("output", ".bin")
      output.deleteOnExit()

      val args = Seq(
        s"+tracedoctor-worker=filer,raw,file:${output.getPath}",
        s"+tracedoctor-expected-output=${expected.getPath}",
      )

      val runResult = run(backend, false, args = args)
      assert(runResult == 0)

      val expectedBytes = java.nio.file.Files.readAllBytes(expected.toPath)
      val outputBytes   = java.nio.file.Files.readAllBytes(output.toPath)
      assert(expectedBytes.nonEmpty, "test produced an empty expected token stream")
      outputBytes should equal(expectedBytes)
    }
  }
}

class TraceDoctorF1TestW512 extends TraceDoctorTestBase(BaseConfigs.F1, 512)
class TraceDoctorF1TestW256 extends TraceDoctorTestBase(BaseConfigs.F1, 256)
