package pmu

import firrtl.RenameMap
import firrtl.annotations.{Annotation, ReferenceTarget}

/** Fail closed if a compiler stage deletes or replicates a target before consumption. */
private[pmu] object PMURename {
  def apply(target: ReferenceTarget, renames: RenameMap): ReferenceTarget =
    renames.get(target) match {
      case None => target
      case Some(Seq(next: ReferenceTarget)) => next
      case other => throw new IllegalArgumentException(s"PMU target must have one reference after rename: $target -> $other")
    }
}

final case class PMUScopeAnnotation(
  manifest: PMUManifest,
  clock: ReferenceTarget,
  reset: ReferenceTarget
) extends Annotation {
  def update(renames: RenameMap): Seq[Annotation] = Seq(copy(
    clock = PMURename(clock, renames), reset = PMURename(reset, renames)))
}

final case class PMUEventAnnotation(
  source: String,
  id: Int,
  increment: ReferenceTarget,
  clock: ReferenceTarget,
  reset: ReferenceTarget,
  ownerIncrement: ReferenceTarget
) extends Annotation {
  def update(renames: RenameMap): Seq[Annotation] = Seq(copy(
    increment = PMURename(increment, renames), clock = PMURename(clock, renames),
    reset = PMURename(reset, renames), ownerIncrement = PMURename(ownerIncrement, renames)))
}

/** A raw event consumer. Compiler checks verify source identity, widths and the
  * observation domain before any downstream lowering can truncate a connection.
  */
final case class PMUConsumerAnnotation(
  source: String,
  increments: Seq[ReferenceTarget],
  clock: ReferenceTarget,
  reset: ReferenceTarget
) extends Annotation {
  def update(renames: RenameMap): Seq[Annotation] = Seq(copy(
    increments = increments.map(PMURename(_, renames)),
    clock = PMURename(clock, renames), reset = PMURename(reset, renames)))
}

/** Target-side metadata consumers finalize after PMU validation, before annotation
  * export. Implementations can live in the consuming project (e.g. FireChip), so
  * the PMU library/compiler do not depend on a particular simulation framework.
  */
trait PMUMetadataConsumerAnnotation extends Annotation with firrtl.options.Unserializable {
  def finalizePMU(manifests: Map[String, PMUManifest], annotations: firrtl.AnnotationSeq): firrtl.AnnotationSeq
}

object PMUMetadataConsumerAnnotation {
  def finalizeAll(manifests: Seq[PMUManifest], annotations: firrtl.AnnotationSeq): firrtl.AnnotationSeq = {
    require(manifests.forall(_.finalized), "PMU metadata requires compiler-finalized catalogs")
    val bySource = manifests.map(m => m.source -> m).toMap
    val consumers = annotations.collect { case a: PMUMetadataConsumerAnnotation => a }
    val result = consumers.foldLeft(annotations)((annos, consumer) => consumer.finalizePMU(bySource, annos))
    require(!result.exists(_.isInstanceOf[PMUMetadataConsumerAnnotation]), "Unconsumed PMU metadata request")
    result
  }
}
