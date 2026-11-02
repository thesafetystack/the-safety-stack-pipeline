"""TEST-ONLY stand-in ``collect.arxiv`` (T5.1a build scenario): re-observes the first source of
its own pinned chain ``sources.arxiv`` (the runner pins it from the stage line's ``prior:``, D92)
as a ``sources`` part file, ``observed_at`` and ``run_id`` of this run. Writes no coverage."""

from __future__ import annotations

import datetime as dt

import pyarrow as pa

from stack.stage_runner import StageContext, StageError, StageResult


def run(ctx: StageContext) -> StageResult:
    if "sources" not in ctx.prior:
        raise StageError("stand-in collect.arxiv extends its own sources.arxiv chain; it has no files")
    view = ctx.view("sources")
    row = min(view.to_pylist(), key=lambda r: r["source_id"])
    now = dt.datetime.now(dt.UTC)
    row.update(observed_at=now, run_id=ctx.run_id)
    row["inband"].update(run_id=ctx.run_id, updated_at=now)
    ctx.emit_part("sources", pa.Table.from_pylist([row], schema=view.schema), "*")
    ctx.counts["observed"] = 1
    return StageResult(scope_complete=True, rows_touched=1)
