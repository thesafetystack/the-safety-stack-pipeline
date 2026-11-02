"""``collect.x-lookup``: X posts for the ``documents`` head's ``x_post`` keys (and the ``ids``
option), with the self-thread of each linked post, through the X API v2 under the X ledger
(round 4, track X). The code is ``stack/collect/xapi.py`` (its module docstring)."""

from __future__ import annotations

from stack.collect import xapi
from stack.stage_runner import StageContext, StageResult

OPTIONS: frozenset[str] = xapi.OPTIONS  # D434


def run(ctx: StageContext) -> StageResult:
    return xapi.run(ctx)
