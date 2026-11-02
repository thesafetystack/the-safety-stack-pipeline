"""Per-run cost ledgers, in memory (D36; the host-wide ledger files of D44/D242 retired by T-STORE).

Each run holds one :class:`Ledger` per provider in this process (:func:`run_ledger`, keyed by run
id and provider): ``openrouter`` (the LLM client, :func:`stack.llm.open_client`), ``x``
(:mod:`stack.collect.xapi`), ``openalex`` (:mod:`stack.collect.provider_keys`). Nothing is written
to disk: a run's spend lives in its record (``RunRecord.spend``, :func:`run_spend`), and two runs
never share a cap. Every outbound paid attempt is **reserved** before the request and
**reconciled** after the response:

``reserve``    the conservative amount (for the LLM client: input-token estimate over the full
               serialized request (+64, ×1.25) × input price + ``max_tokens`` × output price,
               ``llm.yaml`` ``pricing``), refused with :class:`BudgetExceeded` before anything is
               recorded when the run's committed + reserved + amount would pass the ledger's
               ``cap_usd`` or the call's ``run_budget_usd``.
``reconcile``  closes a reservation at its actual cost (OpenRouter ``usage.cost`` when present,
               else tokens × price). A reconcile of a reservation already released charges the
               whole actual; a second reconcile of one reservation is refused.
``release``    closes a reservation at $0: an attempt that ended in a transport error (no answer,
               nothing billed).

A **batched** reservation (OpenRouter's batch API, :mod:`stack.llm.batch`) is never released at
$0 once it may have been billed: the batch driver settles or reconciles every one at the run's
actual/reserved ratio (D308), a lost batch's (a 404/410 poll) at the full reserved amount (D370).
:func:`close_run` (the stage runner, before the final record) is the backstop: a batched
reservation still open then is reconciled at reserved × the ratio of this ledger's reconciled
batched reservations (1.0 before any), every other open one is released.

The journal (:meth:`Ledger.lines`, the dicts the old ledger file held, kept in memory for the run's
lifetime, ~0.4 KB per line) is for tests and debugging; nothing persists it.
"""

from __future__ import annotations

import datetime as dt
import secrets
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal

from stack.llm.errors import BudgetExceeded

#: Floating-point slack on the limit comparisons (sums of many small floats).
_EPS = 1e-9

AttemptKind = Literal["first", "retry", "adjudicate", "fallback"]
LineKind = Literal["reserve", "reconcile", "release"]

#: The LLM client's provider key (spend keys ``openrouter/<model>``).
LLM_PROVIDER = "openrouter"


def _now() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass(frozen=True)
class Reservation:
    """One reserved outbound attempt; :meth:`Ledger.reconcile` closes it."""

    reservation_id: str
    run_id: str
    stage: str
    model: str
    attempt: str
    reserved_usd: float
    batched: bool = False


class Ledger:
    """One run's ledger for one provider, in memory and thread-safe. ``cap_usd`` (None: no cap of
    its own) bounds the run's committed + reserved total; a reserve's ``run_budget_usd`` bounds it
    too (the lower wins). Both refusals are :class:`BudgetExceeded`."""

    def __init__(self, *, cap_usd: float | None = None, provider: str = LLM_PROVIDER) -> None:
        self.cap_usd = None if cap_usd is None else float(cap_usd)
        self.provider = provider
        self._lock = threading.Lock()
        self._total = 0.0
        self._by_run: dict[str, float] = {}
        self._open: dict[str, dict[str, Any]] = {}
        self._released: set[str] = set()
        self._lines: list[dict[str, Any]] = []
        #: model → settled actual USD (reconciled reservations only).
        self._settled: dict[str, float] = {}
        self._batch_actual = 0.0
        self._batch_reserved = 0.0
        self._batches: list[str] = []
        self._abandoned: list[str] = []

    # ── reading ───────────────────────────────────────────────────────────────

    def total(self) -> float:
        """Committed + outstanding reserved USD."""
        with self._lock:
            return self._total

    def run_total(self, run_id: str) -> float:
        """Committed + outstanding reserved USD of ``run_id`` (one run per ledger)."""
        with self._lock:
            return self._by_run.get(run_id, 0.0)

    def outstanding(self, run_ids: Iterable[str] | None = None) -> list[dict[str, Any]]:
        """Reserve lines with no reconcile/release yet (optionally only of ``run_ids``)."""
        wanted = None if run_ids is None else set(run_ids)
        with self._lock:
            return [dict(r) for r in self._open.values() if wanted is None or r.get("run_id") in wanted]

    def lines(self) -> list[dict[str, Any]]:
        """Every journal line, in order (module docstring)."""
        with self._lock:
            return [dict(x) for x in self._lines]

    def settled(self) -> dict[str, float]:
        """model → settled actual USD."""
        with self._lock:
            return dict(self._settled)

    def open_usd(self) -> float:
        """USD reserved by reservations still open (in-flight calls and batches)."""
        with self._lock:
            return sum(float(r["cost_usd"]) for r in self._open.values())

    def batch_ids(self) -> list[str]:
        """Ids of the batches in flight (:meth:`batch_submitted`, not yet :meth:`batch_ended`)."""
        with self._lock:
            return list(self._batches)

    def abandoned_batch_ids(self) -> list[str]:
        """Ids of the batches abandoned (``batch_ended(..., abandoned=True)``, or still in flight at
        :meth:`close`): they keep running at the provider and are billed at the ratio (D308). A batch
        whose poll answers 404/410 (lost) is listed here too (D366); its calls are charged at their
        full reservation (D370)."""
        with self._lock:
            return list(self._abandoned)

    # ── writing ───────────────────────────────────────────────────────────────

    def reserve(
        self,
        *,
        run_id: str,
        stage: str,
        model: str,
        attempt: AttemptKind,
        amount_usd: float,
        tokens_in: int,
        tokens_out: int,
        run_budget_usd: float | None = None,
        batched: bool = False,
    ) -> Reservation:
        """Check and reserve ``amount_usd``; :class:`BudgetExceeded` (nothing recorded) when the
        committed + reserved + amount would pass ``cap_usd`` or ``run_budget_usd``.
        ``tokens_in``/``tokens_out`` are the estimate the amount was computed from."""
        if amount_usd < 0:
            raise ValueError(f"negative reservation {amount_usd}")
        with self._lock:
            for name, cap, spent in (("budget", run_budget_usd, self._by_run.get(run_id, 0.0)),
                                     ("cap", self.cap_usd, self._total)):
                if cap is not None and spent + amount_usd - cap > _EPS:
                    raise BudgetExceeded(
                        f"run {run_id} ({stage}, {self.provider}): committed+reserved ${spent:.6f} + "
                        f"reservation ${amount_usd:.6f} > {name} ${cap:.4f}; not sent")
            res = Reservation(f"v{secrets.token_hex(8)}", run_id, stage, model, attempt, float(amount_usd),
                              batched)
            line = {"kind": "reserve", "reservation_id": res.reservation_id, "run_id": run_id, "stage": stage,
                    "model": model, "attempt": attempt, "tokens_in": int(tokens_in),
                    "tokens_out": int(tokens_out), "cost_usd": res.reserved_usd, "at": _now()}
            if batched:
                line["batched"] = True
            self._append(line)
            self._open[res.reservation_id] = line
            return res

    def reconcile(self, res: Reservation, *, actual_usd: float, tokens_in: int, tokens_out: int) -> None:
        """Close ``res`` at ``actual_usd`` (the whole actual when ``res`` was released before).
        Refuses (``ValueError``, nothing recorded) a reservation that is neither open nor
        released: reconciled already, or not in this ledger."""
        with self._lock:
            if self._open.pop(res.reservation_id, None) is not None:
                delta = float(actual_usd) - res.reserved_usd
            elif res.reservation_id in self._released:
                self._released.discard(res.reservation_id)
                delta = float(actual_usd)
            else:
                raise ValueError(f"ledger {self.provider}: reservation {res.reservation_id} is not open "
                                 f"(reconciled already, or not in this ledger); not reconciled")
            self._append({
                "kind": "reconcile", "reservation_id": res.reservation_id, "run_id": res.run_id,
                "stage": res.stage, "model": res.model, "attempt": res.attempt,
                "tokens_in": int(tokens_in), "tokens_out": int(tokens_out),
                "cost_usd": delta, "actual_usd": float(actual_usd), "at": _now(),
            })
            self._settled[res.model] = self._settled.get(res.model, 0.0) + float(actual_usd)
            if res.batched:
                self._batch_actual += float(actual_usd)
                self._batch_reserved += res.reserved_usd

    def release(self, res: Reservation) -> bool:
        """Close ``res`` at $0 when it is still open (False, nothing recorded, when it is not)."""
        with self._lock:
            return self._release_locked(res.reservation_id)

    def batch_submitted(self, batch_id: str) -> None:
        """A batch was accepted by the provider: in flight until :meth:`batch_ended`."""
        with self._lock:
            if batch_id not in self._batches:
                self._batches.append(batch_id)

    def batch_ended(self, batch_id: str, *, abandoned: bool = False) -> None:
        """A batch settled or was abandoned (its reservations are closed by then); an abandoned
        one's id moves to :meth:`abandoned_batch_ids`."""
        with self._lock:
            if batch_id in self._batches:
                self._batches.remove(batch_id)
                if abandoned:
                    self._abandoned.append(batch_id)

    def close(self) -> int:
        """End of the run (:func:`close_run`): every open batched reservation is reconciled at
        reserved × the batched actual/reserved ratio (1.0 before any), every other open one is
        released; a batch still in flight counts as abandoned from here on. Returns how many were
        closed."""
        with self._lock:
            ratio = self._batch_actual / self._batch_reserved if self._batch_reserved > 0 else 1.0
            batched = [r for r in self._open.values() if r.get("batched")]
            n = 0
            for r in list(self._open.values()):
                if r.get("batched"):
                    continue
                self._release_locked(r["reservation_id"])
                n += 1
            self._abandoned += self._batches
            self._batches.clear()
        for r in batched:
            res = Reservation(r["reservation_id"], r["run_id"], r["stage"], r["model"], r["attempt"],
                              float(r["cost_usd"]), True)
            self.reconcile(res, actual_usd=res.reserved_usd * ratio, tokens_in=0, tokens_out=0)
            n += 1
        return n

    def _release_locked(self, reservation_id: str) -> bool:
        r = self._open.pop(reservation_id, None)
        if r is None:
            return False
        self._released.add(reservation_id)
        self._append({
            "kind": "release", "reservation_id": reservation_id, "run_id": r["run_id"],
            "stage": r.get("stage"), "model": r["model"], "attempt": r.get("attempt"),
            "tokens_in": 0, "tokens_out": 0, "cost_usd": -float(r["cost_usd"]), "at": _now(),
        })
        return True

    def _append(self, line: dict[str, Any]) -> None:
        self._lines.append(line)
        self._total += float(line["cost_usd"])
        self._by_run[line["run_id"]] = self._by_run.get(line["run_id"], 0.0) + float(line["cost_usd"])


# ─────────────────────────────────────────────────────────────────────────────
# The per-run registry
# ─────────────────────────────────────────────────────────────────────────────

#: run_id → provider → that run's ledger; :func:`close_run` closes an entry, :func:`forget_run`
#: drops it.
_RUNS: dict[str, dict[str, Ledger]] = {}
_RUNS_LOCK = threading.Lock()


def run_ledger(run_id: str, provider: str = LLM_PROVIDER, *, cap_usd: float | None = None) -> Ledger:
    """The ledger of ``run_id`` for ``provider`` in this process, created on first use with
    ``cap_usd``. A second call with a different ``cap_usd`` raises (one run, one cap)."""
    with _RUNS_LOCK:
        led = _RUNS.setdefault(run_id, {}).get(provider)
        if led is None:
            led = _RUNS[run_id][provider] = Ledger(cap_usd=cap_usd, provider=provider)
        elif cap_usd is not None and led.cap_usd != float(cap_usd):
            raise ValueError(f"run {run_id}: {provider} ledger already capped at ${led.cap_usd}, not ${cap_usd}")
        return led


def _spend_key(provider: str, model: str) -> str:
    """``openrouter/<model>`` for the LLM client; the provider alone for the others."""
    return f"{provider}/{model}" if provider == LLM_PROVIDER else provider


def run_spend(run_id: str) -> dict[str, Any]:
    """The run record's ``spend`` (T-STORE, design §6.7): ``usd_micro`` = settled micro-USD per
    ``openrouter/<model>``, ``x`` and ``openalex``; ``reserved_open_usd_micro`` = the open
    reservations (in-flight calls and batches); ``batch_ids`` = batches in flight;
    ``abandoned_batch_ids`` = batches abandoned (still running at the provider, billed at the
    ratio) or lost (a 404/410 poll, charged at the full reservation; D366, D370); kept after the
    run ends, so the final record names them. A run that opened no ledger gets zeros."""
    with _RUNS_LOCK:
        ledgers = list(_RUNS.get(run_id, {}).values())
    usd: dict[str, float] = {}
    open_usd = 0.0
    batch_ids: list[str] = []
    abandoned: list[str] = []
    for led in ledgers:
        for model, v in led.settled().items():
            key = _spend_key(led.provider, model)
            usd[key] = usd.get(key, 0.0) + v
        open_usd += led.open_usd()
        batch_ids += led.batch_ids()
        abandoned += led.abandoned_batch_ids()
    return {"usd_micro": {k: round(v * 1e6) for k, v in sorted(usd.items())},
            "reserved_open_usd_micro": round(open_usd * 1e6), "batch_ids": batch_ids,
            "abandoned_batch_ids": abandoned}


def close_run(run_id: str) -> dict[str, Any]:
    """Close every ledger of ``run_id`` (:meth:`Ledger.close`) and return its final
    :func:`run_spend`. The stage runner calls it before writing the final record; no call of the
    run is in flight then. Idempotent."""
    with _RUNS_LOCK:
        ledgers = list(_RUNS.get(run_id, {}).values())
    for led in ledgers:
        led.close()
    return run_spend(run_id)


def forget_run(run_id: str) -> None:
    """Drop the run's ledgers (after its final record)."""
    with _RUNS_LOCK:
        _RUNS.pop(run_id, None)


__all__ = ["LLM_PROVIDER", "AttemptKind", "Ledger", "LineKind", "Reservation", "close_run", "forget_run",
           "run_ledger", "run_spend"]
