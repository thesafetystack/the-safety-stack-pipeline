"""TEST-ONLY stand-in ``acquire`` (T5.1a build scenario): writes a ``documents`` part
re-emitting the first ``acquired`` row of the pinned head unchanged (``blocks_written =
['content']``), and is ``scope_complete`` only from its ``options.complete_after``-th
consecutive run (default 1): it counts the ``acquire`` files at the head of the pinned chain, so
the build's ``repeat:`` is exercised. From its ``options.halt_from``-th consecutive run it counts a
halted lane as the real acquire does (``halted``, ``halted.oa``, ``stopped.halted``) with
``options.halted_ok`` (default 0) roles ``outcome.ok``. ``options.fail: true`` raises (a stage
failing mid-build)."""

from __future__ import annotations

from stack.stage_runner import StageContext, StageError, StageResult


def run(ctx: StageContext) -> StageResult:
    if ctx.plan_stage.options.get("fail"):
        raise RuntimeError("stand-in acquire: injected failure (options.fail)")
    if "documents" not in ctx.inputs:
        raise StageError("stand-in acquire needs --input documents")
    chain = ctx.inputs["documents"]
    trailing = 0
    for f in reversed(chain.files):
        if f.header.get("stage") != "acquire":
            break
        trailing += 1
    view = ctx.view("documents")
    rows = [r for r in view.to_pylist() if r["state"] == "acquired"][:1]
    ctx.emit_part("documents", view.from_pylist(rows, schema=view.schema), ["content"])
    this_run = trailing + 1
    ctx.counts["consecutive_run"] = this_run
    halt_from = ctx.plan_stage.options.get("halt_from")
    if halt_from is not None and this_run >= int(halt_from):
        ctx.counts.update({"halted": 1, "halted.oa": 1, "stopped.halted": 1,
                           "outcome.ok": int(ctx.plan_stage.options.get("halted_ok", 0))})
    return StageResult(scope_complete=this_run >= int(ctx.plan_stage.options.get("complete_after", 1)),
                       rows_touched=0)
