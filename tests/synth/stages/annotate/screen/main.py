"""TEST-ONLY stand-in ``annotate.screen`` (T5.1a build scenario) on the annotate framework
(``stack.annotate``, T4.3a): a computed workflow (no model, ``origin = 'computed'``) that decides
every document of the pinned ``corpus`` (the build's ``fold.corpus`` file) ``relevant`` on its
current text (``content.current_text_sha256``). The framework does the rest: prior evidence
(the ``screen_evidence`` series read as ``--in``) on the same work key is carried, at most
``options.max_rows`` documents are decided per run (the rest ``not_attempted[limit]``,
``scope_complete = false``; the next build's run does them), and ``screen`` + ``screen_evidence`` are written (full; chains of one, D14) so
that V7, V8, X7, X8 hold. It never reads the text blob."""

from __future__ import annotations

from stack.annotate import (
    PURE_SUBSTAGES,
    SUBSTAGES,
    Answer,
    AskContext,
    AskItem,
    Workflow,
    run_annotate,
)
from stack.stage_runner import StageContext, StageResult

__all__ = ["PURE_SUBSTAGES", "SUBSTAGES", "WORKFLOW", "run"]


def _ask(item: AskItem, actx: AskContext) -> Answer:
    return Answer({"verdict": "relevant", "basis_role": (item.row.get("content") or {}).get("current_role"),
                   "buckets": ["evals"], "quotes": [{"text": "we evaluate", "offset": 10}],
                   "confidence": 0.8, "reasoning": "about evals", "adjudication_tier": 1})


WORKFLOW = Workflow("screen", ask=_ask)


def run(ctx: StageContext) -> StageResult:
    return run_annotate(ctx, WORKFLOW)
