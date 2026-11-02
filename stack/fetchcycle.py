"""Per-role fetch accounting for the ``acquire`` stage: attempts, backoff, the cap.

There is more than one way to acquire bytes for a source (the lanes of
``workflows/acquire/lanes/``: arXiv, HTTP, open access, in-band), but only one correct way to
*account* for an attempt. Getting it subtly different in a second lane would not fail a test;
it would show up months later as one lane quarantining a source another keeps hammering. So
the accounting lives here, once.

Accounting is the per-role ``ROLE`` struct of ``documents.content.roles[]`` (keyed
``(source_id, content_role)``, §6; cap 4 per role, D25). The pure builders below take a role
dict and return the updated copy; ``workflows/acquire/main.py`` is their one caller, so lanes
never touch attempts, timestamps, backoff or the cap:

* :func:`backoff` — :data:`FIRST_RETRY` (due at once) after the first consecutive failure, so
  the next ``acquire`` run (the next pass of ``stack build --passes``) retries it; later
  ``1h·2^(attempts-1)`` capped at 7 d (§3.1; v3's ``failure_patch`` formula, which kept it for
  every attempt);
* :func:`role_settled` — an ``ok``/``empty`` episode: consecutive-failure count reset (v3's
  ``settle_patch``; X24 allows the decrease on ``ok``), ``retry_after`` = the recheck when the
  role is rechecked (D22: ``ok`` roles of channel group ``url``), else NULL;
* :func:`role_failed` — a ``failed_transient`` episode bumps ``attempts``, backs off and
  quarantines at the cap (``quarantined[cap_reached]``); a ``failed_permanent`` one bumps
  ``attempts`` and is terminal. **A rate limit is not counted** (D484; class in :data:`UNCOUNTED`:
  ``failed_transient[rate_limit]``, whatever answered it: a host's 429 in the ``http`` lane, an
  OA host's or S2's limit, D402/D413, an arXiv Atom 429/503 that left the paper unanswered, D411):
  the episode is recorded (``status``, ``error_class``, ``last_attempt_at``, ``last_outcome``) and
  backs off, ``retry_after = max(now + backoff(max(attempts, 1)), the provider's Retry-After)`` (so
  without a hint and without earlier failures the next run), but ``attempts`` does not move and the
  role is never quarantined by it: the provider was busy, the source was not tried. Other failures
  count as before, so a role quarantines at its fourth non-rate-limit failure since its last ``ok``. A terminal state is not forever (round 5, R4-2): ``cap_reached``
  and ``failed_permanent[dead|blocked|no_fetchable_url]`` get the long recheck
  :data:`TERMINAL_RECHECK` (``retry_after = now + 90 d``; :func:`workflows.acquire.scope.is_due`
  reads it), so a host down for a day, an embargo that ends or a block that is lifted is seen
  again; other permanent classes (``paywalled``, ``too_large``, …) keep ``retry_after`` NULL. A
  rechecked role that fails again re-enters its terminal state at once (``attempts`` stays over
  the cap) with a fresh 90 d; ``stack acquire reopen`` makes such roles due now for a known
  outage;
* :func:`role_skipped` — a lane's skip (``skipped[withdrawn]`` …): never charged (v3's
  ``skip_patch`` rule), ``retry_after`` NULL;
* :class:`Reopen` — the operator's "that outage is over" (``stack acquire reopen``, R4-2): a failed
  role (``failed_transient``, ``failed_permanent``, ``quarantined``) of one lane, optionally of one
  error class and last attempted on or after a date, gets ``retry_after = now`` (due at once);
  nothing else changes (``attempts`` stays, X24).

v3's DB scheduling half (``FetchCycle``, ``select_due``, ``ownership_sql``,
``due_predicate_sql``, ``SourceRow``, ``schedule_for``) was deleted in the 2026-10-13 cleanup.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

# Next-due backoff after a FAILED episode: retry_after = now + min(base * 2**attempts, cap).
FAIL_BACKOFF_BASE = timedelta(hours=1)
FAIL_BACKOFF_CAP = timedelta(days=7)
#: The delay after a role's first consecutive failure (round 4, passes: due at the next run).
FIRST_RETRY = timedelta(0)
#: Fetch attempts per role before ``quarantined[cap_reached]`` (§3.1 caps, D25).
FETCH_CAP = 4
#: The long recheck of a terminal role (R4-2): ``quarantined[cap_reached]`` and these
#: ``failed_permanent`` classes are tried once more after this long.
TERMINAL_RECHECK = timedelta(days=90)
#: ``failed_transient`` classes that back off but do not count toward :data:`FETCH_CAP` (D484): a
#: provider's rate limit says nothing about the source.
UNCOUNTED: frozenset[str] = frozenset({"rate_limit"})
RECHECKED_PERMANENT: frozenset[str] = frozenset({"dead", "blocked", "no_fetchable_url"})


# ---------------------------------------------------------------- per-role accounting (pure)
def backoff(attempts: int) -> timedelta:
    """Next-due delay after the ``attempts``-th consecutive failure: :data:`FIRST_RETRY` after
    the first, else ``1h·2^(attempts-1)`` capped at 7 d (attempts 1 → 0, 2 → 2 h, 3 → 4 h; §3.1
    as amended for passes)."""
    if attempts < 1:
        raise ValueError(f"backoff after {attempts} attempts: count from 1")
    if attempts == 1:
        return FIRST_RETRY
    if attempts - 1 >= 8:  # 2^8 h > 7 d already
        return FAIL_BACKOFF_CAP
    return min(FAIL_BACKOFF_BASE * (2 ** (attempts - 1)), FAIL_BACKOFF_CAP)


def _attempted(role: Mapping[str, Any], now: datetime, outcome: str) -> dict[str, Any]:
    """The role after an episode: a lane's outcome, so no acquire rule set its status (``rule`` NULL, D690)."""
    out = dict(role)
    out["rule"] = None
    out["first_attempt_at"] = role.get("first_attempt_at") or now
    out["last_attempt_at"] = now
    out["last_outcome"] = outcome
    return out


def role_settled(role: Mapping[str, Any], now: datetime, *, status: str = "ok",
                 outcome: str = "ok", recheck: timedelta | None = None) -> dict[str, Any]:
    """The role after a settled episode (``ok``, or ``empty``: examined, nothing found):
    ``attempts`` reset to 0 (consecutive failures, as v3's ``settle_patch``), error cleared,
    ``retry_after = now + recheck`` when the role is rechecked, else NULL."""
    if status not in ("ok", "empty"):
        raise ValueError(f"role_settled: status {status!r} is not settled")
    out = _attempted(role, now, outcome)
    out.update(status=status, error_class=None, error=None, attempts=0,
               retry_after=now + recheck if recheck is not None else None)
    return out


def role_failed(role: Mapping[str, Any], now: datetime, *, status: str, error_class: str,
                error: str | None = None, cap: int = FETCH_CAP,
                retry_after_hint: datetime | None = None) -> dict[str, Any]:
    """The role after a failed episode. ``attempts`` + 1 (never decreases, X24), except for a
    ``failed_transient`` of a class in :data:`UNCOUNTED` (a rate limit, D484: ``attempts`` unchanged,
    never quarantined). ``failed_transient``: at ``attempts >= cap`` → ``quarantined[cap_reached]``
    with ``retry_after = now +`` :data:`TERMINAL_RECHECK`, else ``retry_after = now +
    backoff(max(attempts, 1))`` (or the provider's later hint). ``failed_permanent``: terminal,
    ``retry_after = now +`` :data:`TERMINAL_RECHECK` for the classes of :data:`RECHECKED_PERMANENT`,
    else NULL."""
    if status not in ("failed_transient", "failed_permanent"):
        raise ValueError(f"role_failed: status {status!r} is not a failure")
    out = _attempted(role, now, status)
    counted = not (status == "failed_transient" and error_class in UNCOUNTED)
    attempts = int(role.get("attempts") or 0) + (1 if counted else 0)
    out.update(attempts=attempts, error=error)
    if status == "failed_permanent":
        out.update(status=status, error_class=error_class,
                   retry_after=now + TERMINAL_RECHECK if error_class in RECHECKED_PERMANENT else None)
    elif counted and attempts >= cap:
        out.update(status="quarantined", error_class="cap_reached",
                   error=f"{error_class}: {error}" if error else error_class, retry_after=now + TERMINAL_RECHECK)
    else:
        due = now + backoff(max(attempts, 1))
        if retry_after_hint is not None and retry_after_hint > due:
            due = retry_after_hint
        out.update(status=status, error_class=error_class, retry_after=due)
    return out


def role_skipped(role: Mapping[str, Any], now: datetime, *, error_class: str,
                 error: str | None = None) -> dict[str, Any]:
    """The role after a lane skip (``skipped[<error_class>]``): never charged (``attempts``
    unchanged; v3's ``skip_patch`` rule), ``retry_after`` NULL."""
    out = _attempted(role, now, "skipped")
    out.update(status="skipped", error_class=error_class, error=error, retry_after=None)
    return out


#: Role statuses :class:`Reopen` may make due (a failure of some kind).
REOPENABLE: frozenset[str] = frozenset({"failed_transient", "failed_permanent", "quarantined"})


@dataclass(frozen=True)
class Reopen:
    """Which failed roles an operator re-opens (module docstring). ``error_class`` matches the
    role's class, or for ``quarantined[cap_reached]`` the transient class the quarantine came from
    (the ``<class>: …`` prefix of its ``error``); ``since`` is a UTC date or instant compared with
    ``last_attempt_at``."""

    lane: str
    error_class: str | None = None
    since: datetime | None = None

    @classmethod
    def parse(cls, spec: Mapping[str, Any]) -> Reopen:
        """From the ``options.reopen`` mapping of an ``acquire`` line (``lane`` required;
        ``error_class``, ``since`` as ``YYYY-MM-DD`` or ISO instant optional). ``ValueError``
        names what is wrong."""
        if not isinstance(spec, Mapping):
            raise ValueError(f"options.reopen must be a mapping, got {spec!r}")  # noqa: TRY004 - one error type for the caller
        unknown = set(spec) - {"lane", "error_class", "since"}
        if unknown:
            raise ValueError(f"options.reopen: unknown keys {sorted(unknown)}")
        lane = spec.get("lane")
        if not isinstance(lane, str) or not lane:
            raise ValueError("options.reopen.lane is required (a lane name such as arxiv, oa, http)")
        since = spec.get("since")
        if since is not None and not isinstance(since, datetime):
            if isinstance(since, date):
                since = datetime.combine(since, time(), UTC)
            else:
                try:
                    since = datetime.fromisoformat(str(since))
                except ValueError:
                    raise ValueError(f"options.reopen.since {since!r} is not a date or ISO instant") from None
        if since is not None and since.tzinfo is None:
            since = since.replace(tzinfo=UTC)
        err = spec.get("error_class")
        return cls(lane=lane, error_class=str(err) if err else None, since=since)

    def as_option(self) -> dict[str, Any]:
        """The ``options.reopen`` mapping that :meth:`parse` reads back (JSON-safe)."""
        out: dict[str, Any] = {"lane": self.lane}
        if self.error_class:
            out["error_class"] = self.error_class
        if self.since is not None:
            out["since"] = self.since.isoformat()
        return out

    def matches(self, role: Mapping[str, Any]) -> bool:
        if role.get("lane") != self.lane or role.get("status") not in REOPENABLE:
            return False
        if self.error_class is not None:
            cls = role.get("error_class")
            if role.get("status") == "quarantined" and cls == "cap_reached":
                cls = str(role.get("error") or "").partition(":")[0].strip() or cls
            if cls != self.error_class and role.get("error_class") != self.error_class:
                return False
        if self.since is not None:
            last = role.get("last_attempt_at")
            if last is None or last < self.since:
                return False
        return True


def role_reopened(role: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    """The role made due at ``now`` (:class:`Reopen`): only ``retry_after`` changes."""
    return {**role, "retry_after": now}


__all__ = [
    "FAIL_BACKOFF_BASE",
    "FAIL_BACKOFF_CAP",
    "FETCH_CAP",
    "FIRST_RETRY",
    "RECHECKED_PERMANENT",
    "REOPENABLE",
    "TERMINAL_RECHECK",
    "Reopen",
    "backoff",
    "role_failed",
    "role_reopened",
    "role_settled",
    "role_skipped",
]
