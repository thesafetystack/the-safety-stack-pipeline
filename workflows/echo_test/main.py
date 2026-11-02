"""TEST-ONLY toy stage ``echo_test`` (not in any plan's stage list; the runner runs it with a
bare stage line). Exercises the stage protocol of ``stack/stage_runner.py`` end to end.

Reads the ``documents`` input view and emits a ``documents`` **part file** re-emitting its first
``n`` rows (stage-line ``options.n``, default 10) unchanged, so the published chain's view is
byte-identical to the input's (X15/X17 hold). Under substage ``echo``; ``--only noop`` skips it
and emits nothing (a run with no outputs). The inflight keys are the echoed ``doc_id``s.

``blocks_written`` is ``'*'``: S7 (D53) requires ``'*'`` on a file whose stage is not one of
``gather``/``triage``/``acquire``, and the rows are unchanged anyway. The crash points of the
crash test (env ``STACK_CRASH_AT``) live in the runner's publish path, not here.

v3 ancestor: ``workflows/annotate/echo_test/main.py`` (one receipt per pending unit).
"""

from __future__ import annotations

from stack.stage_runner import StageContext, StageResult

SUBSTAGES = ("echo", "noop")
OPTIONS: frozenset[str] = frozenset({"n"})  # D434
DEFAULT_N = 10


def run(ctx: StageContext) -> StageResult:
    n = int(ctx.plan_stage.options.get("n", DEFAULT_N))
    with ctx.substage("echo") as sub:
        if sub.active:
            view = ctx.view("documents")
            rows = view.slice(0, min(n, view.num_rows))
            ctx.heartbeat(inflight_keys=rows.column("doc_id").to_pylist())
            ctx.emit_part("documents", rows, "*")
            ctx.counts["echoed"] = rows.num_rows
            ctx.heartbeat(inflight_keys=[])
    with ctx.substage("noop"):
        pass
    return StageResult(scope_complete=True, rows_touched=0)
