"""A stage line's ``llm_mode`` (``sync`` | ``batch``, default ``sync``) for the annotate stages and
triage's asker: the stage half of :mod:`stack.llm.batch`.

In batch mode a stage builds its admitted work list as in sync mode (carry/ask, ``max_rows`` in
queue order), runs :func:`prefetch` over thunks that call each item's **normal** ask against the
run's client (the collect passes and the batches), then runs its unchanged sync loop: the *replay*
pass, in which every ``complete()`` is served from the batch outcomes or the response cache, so the
evidence rows are written by the sync code. A :class:`stack.llm.BatchUnanswered` out of an item's
ask makes that item ``not_attempted[unanswered]`` (no evidence; the next run asks it again, the
response cache serving whatever was answered) and the run's scope incomplete. Halt and budget come
out of the replay as in sync mode. Options (stage line ``options:``, steering how a run asks, not
what): ``llm_mode`` and :meth:`stack.llm.batch.BatchOptions.from_options`'s ``batch_*`` keys.

**Seam** (test-only): :data:`CLOCK` replaces the driver's monotonic clock; its sleep is the
annotate framework's ``SLEEP`` (:mod:`stack.annotate.stage`), passed in by the caller.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping, MutableMapping
from typing import Any

from stack.llm import LlmClient
from stack.llm.batch import BatchOptions, BatchReport, prefetch

LLM_MODES = ("sync", "batch")
#: The ``options`` keys :func:`llm_mode` reads (with :meth:`BatchOptions.from_options`'s), D434.
OPTIONS: frozenset[str] = frozenset({"llm_mode", "batch_max_requests", "batch_max_mb", "batch_min_requests",
                                     "batch_wait_minutes"})
#: ``() -> float`` (monotonic seconds) for the batch driver; None = ``time.monotonic``.
CLOCK: Callable[[], float] | None = None


def llm_mode(options: Mapping[str, Any], error: type[Exception], where: str) -> BatchOptions | None:
    """The stage line's batch options in batch mode, None in sync mode. A bad ``llm_mode`` or
    ``batch_*`` value raises ``error`` (the stage's own: ``StageError``, ``GateError``) before any call."""
    mode = options.get("llm_mode", "sync")
    if mode not in LLM_MODES:
        raise error(f"{where}: options.llm_mode must be one of {list(LLM_MODES)}, got {mode!r}")
    try:
        opts = BatchOptions.from_options(options)
    except ValueError as exc:
        raise error(f"{where}: {exc}") from exc
    return opts if mode == "batch" else None


def run_batches(client: LlmClient, asks: Callable[[], Iterable[Callable[[], object]]], *,
                options: BatchOptions, label: str, heartbeat: Callable[[], None],
                sleep: Callable[[float], None], counts: MutableMapping[str, int]) -> BatchReport:
    """:func:`stack.llm.batch.prefetch` for one stage run; its report goes into ``counts``
    (``batch.*``). The client is then in the replay pass."""
    report = prefetch(client, asks, options=options, label=label, heartbeat=heartbeat,
                      clock=CLOCK or time.monotonic, sleep=sleep)
    counts.update(report.as_counts())
    return report


def replay_counts(client: LlmClient | None, counts: MutableMapping[str, int]) -> None:
    """After the replay pass: ``batch.sync_fallback``, the replay's calls that no collect pass had
    collected and were asked sync (e.g. a screen tier 2 admitted only in the replay)."""
    if client is not None and client.batch_pass:
        counts["batch.sync_fallback"] = client.batch_sync_fallback


__all__ = ["CLOCK", "LLM_MODES", "OPTIONS", "llm_mode", "replay_counts", "run_batches"]
