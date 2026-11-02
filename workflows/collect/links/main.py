"""``collect.links``: the outbound links of linking documents (newsletters, flagged manual
sources) as candidate sources, chosen by a context-aware LLM selector (round 4, track L2). The
code is ``stack/collect/links.py`` (its module docstring); where things are stored: README here."""

from __future__ import annotations

from stack.collect import links
from stack.stage_runner import StageContext, StageResult

OPTIONS: frozenset[str] = links.OPTIONS  # D434


def run(ctx: StageContext) -> StageResult:
    return links.run(ctx)
