"""``collect.s2-lookup``: Semantic Scholar records for the ``documents`` head's arXiv ids, DOIs and
PMIDs (D92). The shared lookup code is ``workflows/fetch/metadata/main.py`` (its module docstring)."""

from __future__ import annotations

from stack.stage_runner import StageContext, StageResult
from workflows.fetch.metadata import main as lookup

OPTIONS: frozenset[str] = lookup.LOOKUP_OPTIONS["s2"]  # D434


def run(ctx: StageContext) -> StageResult:
    return lookup.run_lookup(ctx, "s2")
