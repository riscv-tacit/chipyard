package chipyard.stage.phases

import firrtl.AnnotationSeq
import firrtl.options.{Dependency, Phase, StageOptions}
import firrtl.options.Viewer.view
import firrtl.stage.FirrtlCircuitAnnotation
import chipyard.stage._
import pmu.{PMUConsumerAnnotation, PMUEventAnnotation, PMUMetadataConsumerAnnotation, PMUScopeAnnotation}
import pmu.compiler.PMUCompiler

/** Validate PMU events, with or without native banks, before downstream compilation. */
final class ValidatePMU extends Phase with PreservesAll with HasChipyardStageUtils {
  override val prerequisites = Seq(Dependency[ChipyardChiselStage])
  override val optionalPrerequisiteOf = Seq(
    Dependency[GenerateFirrtlAnnos], Dependency[LegacyFirrtl2Emission])

  override def transform(annotations: AnnotationSeq): AnnotationSeq = {
    def isPMU(a: firrtl.annotations.Annotation): Boolean = a match {
      case _: PMUScopeAnnotation | _: PMUEventAnnotation | _: PMUConsumerAnnotation => true
      case _ => false
    }
    if (!annotations.exists(isPMU)) {
      require(!annotations.exists(_.isInstanceOf[PMUMetadataConsumerAnnotation]), "PMU metadata request has no registry")
      return annotations
    }

    // Analyze a copy of the circuit. Keep the normal Chipyard lowering path intact.
    // Other annotations configure downstream compilation, not PMU domain/width checks.
    val inputs = annotations.filter(a => isPMU(a) || a.isInstanceOf[FirrtlCircuitAnnotation])
    val checked = PMUCompiler.validate(PMUCompiler.fromChisel(inputs))
    val manifests = PMUCompiler.manifests(checked)
    val json = ujson.Arr.from(manifests.map(m => ujson.read(m.toJson)))
    val targetDir = view[StageOptions](annotations).targetDir
    val baseName = view[ChipyardOptions](annotations).longName.get
    writeOutputFile(targetDir, s"$baseName.pmu.json", ujson.write(json, indent = 2))

    // Consumers replace deferred keys using the validated catalog. All routing
    // checks run before native/FireSim lowering; no PMU compiler classes escape.
    PMUMetadataConsumerAnnotation.finalizeAll(manifests, annotations).filterNot(isPMU)
  }
}
