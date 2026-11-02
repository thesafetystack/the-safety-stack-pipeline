"""Uniform scale control for collect sweeps (10-design §5.1).

Every collect workflow plans a list of units up front — roster names, concept-group queries,
listing surfaces — and every one of them needs the same two knobs to be runnable at less than
full scale: a FRACTION (run a tenth of the plan) and a CAP (run at most N units). This is the one
implementation of both, so `STACK_COLLECT_FRACTION` means the same thing everywhere.

The fraction selects by a STABLE HASH of each unit's key, not by slicing or sampling randomly.
Two consequences that matter more than they look:

* **A repeated run covers the same slice.** A random or time-seeded 10% would walk over a
  different tenth each run, so no query would ever accumulate a complete coverage receipt and the
  incremental watermarks would never advance. Hash selection makes the sampled corpus a stable,
  growable subset — exactly what a scaled-down rehearsal of the full sweep should be.
* **It is machine- and process-independent.** Python's `hash()` is salted per process; sha256 is
  not, so the same fraction picks the same units on any machine, which is what makes a small run
  reproducible evidence rather than an anecdote.

Units NOT selected are still counted: the caller reports them as `skipped` in its RunCounts and
carries the selection in `runs.detail`, so a fractional run never reads as "that is all there
was" (SHOULD-14 — silence must never claim completeness).
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Mapping, Sequence
from typing import Any, TypeVar

T = TypeVar("T")

ENV_FRACTION = "STACK_COLLECT_FRACTION"
ENV_MAX_QUERIES = "STACK_COLLECT_MAX_QUERIES"
#: The ``options`` keys a collector feeds to :func:`select` (with the env knobs above; D434).
SAMPLING_OPTIONS: frozenset[str] = frozenset({"fraction", "max_queries"})

#: Name suffixes of the ``STACK_*`` env knobs that cap or sample a collector run
#: (``STACK_COLLECT_LIMIT``, ``STACK_COLLECT_FRACTION``, ``STACK_COLLECT_MAX_QUERIES``,
#: ``STACK_BLOGS_LIMIT``, ``STACK_CARDS_LIMIT``, ``STACK_X_LIMIT``, ``STACK_CARD_DATES_LIMIT``,
#: ``STACK_METADATA_LIMIT``). A set one wins over the plan's option (``Collector.option``, D72) and
#: truncates the run without changing its config digest, so the run counts it
#: (``env_override.<VAR>``, with a warning line) and ``stack build`` warns at its start (D445).
CAP_ENV_SUFFIXES: tuple[str, ...] = ("_LIMIT", "_FRACTION", "_MAX_QUERIES")


def is_cap_env(var: str) -> bool:
    """Whether env var ``var`` is a collector cap/sampling knob (:data:`CAP_ENV_SUFFIXES`)."""
    return var.startswith("STACK_") and var.endswith(CAP_ENV_SUFFIXES)


def cap_env_set(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """The cap/sampling knobs set (non-empty) in ``env`` (default ``os.environ``), sorted by name."""
    e = os.environ if env is None else env
    return {k: e[k].strip() for k in sorted(e) if is_cap_env(k) and e[k].strip()}


_RESOLUTION = 1_000_000  # hash buckets; a fraction is a cut on this scale


def _bucket(key: str) -> int:
    """The unit's stable bucket in ``[0, _RESOLUTION)`` — sha256, never ``hash()``."""
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % _RESOLUTION


def in_fraction(key: str, fraction: float) -> bool:
    """Whether ``key`` falls in the leading ``fraction`` of the hash space."""
    if fraction >= 1.0:
        return True
    if fraction <= 0.0:
        return False
    return _bucket(key) < int(fraction * _RESOLUTION)


def _read_fraction(env: Mapping[str, str]) -> float:
    raw = (env.get(ENV_FRACTION) or "").strip()
    if not raw:
        return 1.0
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"${ENV_FRACTION} must be a number in (0, 1], got {raw!r}") from exc
    if not 0.0 < value <= 1.0:
        raise ValueError(f"${ENV_FRACTION} must be in (0, 1], got {value}")
    return value


def _read_cap(env: Mapping[str, str]) -> int:
    raw = (env.get(ENV_MAX_QUERIES) or "").strip()
    if not raw:
        return 0
    try:
        return max(0, int(raw))
    except ValueError as exc:
        raise ValueError(f"${ENV_MAX_QUERIES} must be an integer, got {raw!r}") from exc


def select(
    units: Sequence[T],
    key_of: Callable[[T], str],
    *,
    env: Mapping[str, str] | None = None,
) -> tuple[list[T], dict[str, Any]]:
    """Apply ``$STACK_COLLECT_FRACTION`` then ``$STACK_COLLECT_MAX_QUERIES`` to a sweep plan.

    Returns ``(selected, meta)``. ``meta`` is the honest record of what was left out —
    ``{planned, selected, fraction, max_queries}`` — and belongs in ``runs.detail`` so a reader
    can tell a fractional rehearsal from a full sweep at a glance.

    Order is preserved: the fraction filters, the cap takes a prefix of what survives. With no
    env set this is the identity, so a full run is unaffected.
    """
    e = os.environ if env is None else env
    fraction = _read_fraction(e)
    cap = _read_cap(e)
    selected = [u for u in units if in_fraction(key_of(u), fraction)]
    floored = False
    if units and not selected:
        # A small plan (a scraper's 5 surfaces, a watcher's 13) rounds to zero under any modest
        # fraction, and a workflow that scans NOTHING is a useless rehearsal — the point of a
        # fractional run is to exercise the whole phase at reduced scale. So a non-empty plan
        # always keeps its lowest-bucket unit. Nesting survives: that unit is the first any
        # growing fraction admits, so it stays in every larger slice.
        selected = [min(units, key=lambda u: _bucket(key_of(u)))]
        floored = True
    if cap:
        selected = selected[:cap]
    meta: dict[str, Any] = {
        "planned": len(units),
        "selected": len(selected),
        "fraction": fraction,
        "max_queries": cap,
    }
    if floored:
        meta["floored"] = True  # the fraction selected nothing; one unit kept so the step runs
    return selected, meta
