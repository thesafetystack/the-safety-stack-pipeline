"""``collect.card-dates-lookup``: publication dates for card documents that have none, from their
stored content and official pages (v3 ``fetch.card_dates``). The code is
:mod:`stack.lookup.card_date_backfill` (its module docstring). It writes no ``provider_record`` (D526): the
dates come from pages, not an API record."""

from __future__ import annotations

from stack.lookup import card_date_backfill
from stack.stage_runner import StageContext, StageResult

OPTIONS: frozenset[str] = card_date_backfill.OPTIONS  # D434


def run(ctx: StageContext) -> StageResult:
    return card_date_backfill.run_lookup(ctx)
