"""TEST-ONLY stand-in ``gather`` (T5.1a build scenario): a no-op gather. Reads every pinned
``sources`` chain (counts them) and writes the ``documents`` view back as a **full** file on the
pinned head, rows unchanged (the synthetic world's rows; nothing to merge), ``blocks_written`` =
gather's blocks. A real gather re-derives ``identity``/``metadata``/``inband`` (track A)."""

from __future__ import annotations

from stack.stage_runner import StageContext, StageError, StageResult

GATHER_BLOCKS = ["identity", "metadata", "inband"]


def run(ctx: StageContext) -> StageResult:
    src = [k for k, ch in ctx.inputs.items() if ch.family == "sources"]
    if "documents" not in ctx.inputs or not src:
        raise StageError(f"stand-in gather needs sources and documents inputs, got {sorted(ctx.inputs)}")
    ctx.counts["sources_rows"] = sum(ctx.view(k).num_rows for k in src)
    ctx.counts["sources_chains"] = len(src)
    view = ctx.view("documents")
    ctx.emit_full("documents", view, GATHER_BLOCKS)
    return StageResult(scope_complete=True, rows_touched=0)
