"""``collect.openalex-lookup``: OpenAlex records for the ``documents`` head's DOIs, PMIDs and arXiv ids
(D92). The shared lookup code is ``workflows/fetch/metadata/main.py`` (its module docstring)."""

from __future__ import annotations

from stack.stage_runner import StageContext, StageResult
from workflows.fetch.metadata import main as lookup

OPTIONS: frozenset[str] = lookup.LOOKUP_OPTIONS["openalex"]  # D434


def run(ctx: StageContext) -> StageResult:
    return lookup.run_lookup(ctx, "openalex")
