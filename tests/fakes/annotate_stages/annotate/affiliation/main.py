"""TEST-ONLY ``annotate.affiliation`` stage: the fake workflow of ``tests/fakes/annotate.py`` on the annotate
framework (selected by ``STACK_WORKFLOWS_ROOT=tests/fakes/annotate_stages``)."""

from __future__ import annotations

from stack.annotate import PURE_SUBSTAGES, SUBSTAGES, run_annotate
from stack.stage_runner import StageContext, StageResult
from tests.fakes.annotate import AFFILIATION as WORKFLOW

__all__ = ["PURE_SUBSTAGES", "SUBSTAGES", "WORKFLOW", "run"]


def run(ctx: StageContext) -> StageResult:
    return run_annotate(ctx, WORKFLOW)
