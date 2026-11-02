"""``fold.<config>`` as a stage under the runner (D73; T1.6): ``stack run fold.corpus --in
data/documents/<series>/`` goes through :func:`stack.stage_runner.run_stage` like any stage.

The adapter runs the fold's pure steps over the runner's pinned inputs (``ctx.view`` = step 1,
the union; for ``build`` the ``corpus`` file and each ``<wf>`` file, chains of one), then hands
the table to ``ctx.emit_full`` so **the runner owns publishing** (staging, the publish rule
:func:`stack.store.chain.claim`, final record in the publish hook). It does not call
:func:`stack.fold.run_fold`, which publishes itself; ``stack fold`` (the verb) wraps that one in :func:`stack.stage_runner.run_recorded`. Both
paths produce the same file: ``writer = fold``, the fold header keys, ``config_digest`` =
:func:`stack.fold.fold_config_digest` (D65).

``fold.build`` takes ``--in <fold.corpus file>`` and one ``--in <annotate.<wf> file>`` per
workflow to join (registered workflows only: the runner pins ``data/`` files of registered
families; an unregistered workflow is joined by path through ``stack fold build --join``).
``fold.release`` is not a stage (it writes ``releases/<tag>/``); the runner refuses it before
dispatch.

Filter precedence: ``--filter`` (or the stage line's ``filter:``, which the runner passes as
``ctx.filter``), then the plan's ``fold.<config>.filter`` (:class:`stack.fold.configs.FoldPlan`,
D66), then the config's default.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from stack.fold import WRITER, filter_rows, fold_config_digest, join
from stack.fold.configs import fold_config
from stack.query import resolve_filter
from stack.runs import FoldInfo
from stack.store.layout import NONE

if TYPE_CHECKING:
    from stack.stage_runner import StageContext, StageResult

#: A fold line reads no ``options``: any key on it is refused (D434)
OPTIONS: frozenset[str] = frozenset()


def run(ctx: StageContext) -> StageResult:
    from stack.stage_runner import StageError, StageResult

    config = ctx.stage.removeprefix("fold.")
    spec = fold_config(config)
    if spec.output == "release_dir":  # the runner refuses it before dispatch; kept as a guard
        raise StageError(f"{ctx.stage} is not a stage: use `stack fold release` or `stack release`")
    extra = set(ctx.inputs) - {spec.input_kind}
    if spec.input_kind not in ctx.inputs or ctx.prior or (extra and not spec.joins):
        raise StageError(f"{ctx.stage} takes one --in {spec.input_kind} (data/{spec.input_kind}/<series>/ or a file)"
                         f"{' and --in <wf> files' if spec.joins else ''} and nothing in its output family "
                         f"(got inputs {sorted(ctx.inputs)}, prior {sorted(ctx.prior)})")
    if spec.input_kind == "corpus":
        h = ctx.inputs["corpus"].files[-1].header
        if h.get("fold_config") != "corpus":
            raise StageError(f"{ctx.stage}: input corpus {ctx.inputs['corpus'].head} has fold_config "
                             f"{h.get('fold_config')!r}; fold.build reads a fold.corpus file")
    table = ctx.view(spec.input_kind)  # 1. union (a corpus file: read as is)
    rows_in = table.num_rows
    wf_tables = {wf: ctx.view(wf) for wf in sorted(extra)} if spec.joins else None
    counts: dict[str, int] = {}
    table, folded = join(table, wf_tables, counts)  # 2. join (build only)
    predicate: str | None = None
    if spec.default_filter is not None:  # 3. filter
        predicate = resolve_filter(ctx.filter or ctx.plan.fold.filter_for(config) or spec.default_filter)
        table = filter_rows(table, predicate)
    elif ctx.filter is not None:
        raise StageError(f"{ctx.stage} has no filter step (got {ctx.filter!r})")
    fold: FoldInfo = {"config": config, "filter": predicate if predicate is not None else NONE,
                      "joined": {wf: ctx.inputs[wf].head for wf in folded},
                      "annotations_folded": folded, "release_config": NONE}
    ctx.fold, ctx.writer = fold, WRITER
    ctx.config_digest = fold_config_digest(fold)
    if spec.output == "supersede_head" and table.num_rows != rows_in:
        raise StageError(f"{ctx.stage} must keep every row ({table.num_rows} of {rows_in})")
    ctx.emit_full(spec.output_family, table)  # 5. write: the runner stages and publishes
    ctx.counts.update({"rows_in": rows_in, "rows_out": table.num_rows, **counts})
    return StageResult(scope_complete=True, rows_touched=table.num_rows)


__all__ = ["OPTIONS", "run"]
