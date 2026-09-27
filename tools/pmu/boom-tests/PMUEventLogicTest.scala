package pmu.compiler

import chisel3._
import chisel3.util._
import boom.v3.common.PMUEventLogic
import java.nio.file.{Files, Paths}

/** Exercise the same combinational predicates used by real BOOM producers. */
class PMUEventLogicTop extends RawModule {
  val valids = IO(Input(UInt(4.W)))
  val fires = IO(Input(UInt(4.W)))
  val rob_blocked = IO(Input(UInt(4.W)))
  val rename_blocked = IO(Input(UInt(4.W)))
  val issue_queue_blocked = IO(Input(UInt(4.W)))
  val ldq_blocked = IO(Input(UInt(4.W)))
  val stq_blocked = IO(Input(UInt(4.W)))
  val serialization_blocked = IO(Input(UInt(4.W)))
  val other_backend_blocked = IO(Input(UInt(4.W)))
  val redirect = IO(Input(Bool()))
  val control_stall = IO(Input(Bool()))
  val empty_capacity = IO(Input(Bool()))
  val dispatched = IO(Output(UInt(3.W)))
  val event_mask = IO(Output(UInt(9.W)))
  val events = PMUEventLogic.dispatch(valids.asBools, fires.asBools,
    rob_blocked.asBools, rename_blocked.asBools, issue_queue_blocked.asBools,
    ldq_blocked.asBools, stq_blocked.asBools, serialization_blocked.asBools,
    other_backend_blocked.asBools, redirect, control_stall, empty_capacity)
  dispatched := events.dispatched
  event_mask := VecInit(Seq(events.frontend_starved, events.backend_blocked,
    events.redirect_blocked, events.rob_blocked, events.rename_blocked,
    events.issue_queue_blocked, events.ldq_blocked, events.stq_blocked,
    events.serialization_blocked)).asUInt

  val head_valids = IO(Input(UInt(4.W)))
  val head_busy = IO(Input(UInt(4.W)))
  val head_loads = IO(Input(UInt(4.W)))
  val head_exceptions = IO(Input(UInt(4.W)))
  val commit_enabled = IO(Input(Bool()))
  val head_load_wait = IO(Output(Bool()))
  head_load_wait := PMUEventLogic.headLoadWaiting(head_valids.asBools,
    head_busy.asBools, head_loads.asBools, head_exceptions.asBools, commit_enabled)

  val accepted = IO(Input(Bool()))
  val cacheable = IO(Input(Bool()))
  val existing_mshr = IO(Input(Bool()))
  val tag_match = IO(Input(Bool()))
  val line_miss = IO(Output(Bool()))
  line_miss := PMUEventLogic.lineMissAllocation(accepted, cacheable, existing_mshr, tag_match)
}

object PMUEventLogicTest {
  def main(args: Array[String]): Unit = {
    val annotations = PMUPrototype.elaborate(new PMUEventLogicTop)
    val checked = PMUCompiler.validate(PMUCompiler.fromChisel(annotations))
    Files.writeString(Paths.get(args.head).resolve("PMUEventLogicTop.v"), PMUCompiler.emitNative(checked))
    println("Emitted real BOOM PMU observation predicates for RTL validation")
  }
}
