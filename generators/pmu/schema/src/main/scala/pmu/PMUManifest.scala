package pmu

/** Compiler-independent catalog. No Chisel objects or compiler targets cross this boundary. */
final case class PMUEvent(
  id: Int,
  name: String,
  unit: String,
  description: String,
  incrementBits: Option[Int]
) {
  require(id >= 0, "PMU event ID must be nonnegative")
  require(name.trim.nonEmpty, "PMU event name must be nonempty")
  require(incrementBits.forall(_ > 0), "PMU increment width must be positive")
}

final case class PMUManifest(
  source: String,
  counterBits: Int,
  events: Vector[PMUEvent],
  finalized: Boolean = false,
  schemaVersion: Int = PMUManifest.schemaVersion
) {
  require(schemaVersion == PMUManifest.schemaVersion, s"Unsupported PMU schema version: $schemaVersion")
  require(source.trim.nonEmpty, "PMU source must be nonempty")
  require(counterBits > 0, "PMU counterBits must be positive")
  require(events.nonEmpty, "PMU catalog must not be empty")
  require(events.map(_.id) == events.indices.toVector, "PMU IDs must be dense and ordered")
  require(events.map(_.name).distinct.size == events.size, "Duplicate PMU event names")
  require(events.forall(_.incrementBits.forall(_ <= counterBits)), "PMU increment exceeds counterBits")
  require(!finalized || events.forall(_.incrementBits.nonEmpty), "Finalized PMU widths must be resolved")

  def toJson: String = ujson.write(ujson.Obj(
    "schemaVersion" -> schemaVersion, "source" -> source, "counterBits" -> counterBits,
    "finalized" -> finalized,
    "events" -> ujson.Arr.from(events.map(e => ujson.Obj(
      "id" -> e.id, "name" -> e.name, "unit" -> e.unit, "description" -> e.description,
      "incrementBits" -> e.incrementBits.map(ujson.Num(_)).getOrElse(ujson.Null)
    )))
  ), indent = 2)
}

object PMUManifest {
  val schemaVersion = 1

  def fromJson(json: String): PMUManifest = {
    val obj = ujson.read(json)
    def int(v: ujson.Value): Int = {
      val n = v.num
      require(n.isValidInt && n == n.toInt.toDouble, s"Expected an integer, got $n")
      n.toInt
    }
    PMUManifest(obj("source").str, int(obj("counterBits")), obj("events").arr.map { e =>
      PMUEvent(int(e("id")), e("name").str, e("unit").str, e("description").str,
        if (e("incrementBits") == ujson.Null) None else Some(int(e("incrementBits"))))
    }.toVector, obj("finalized").bool, int(obj("schemaVersion")))
  }
}
