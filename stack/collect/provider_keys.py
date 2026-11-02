"""Scholarly-provider keys and the OpenAlex spend meter (T-RUN item 6; R6-9, R8-1).

**Keys.** Two provider keys, each read by ONE name through :func:`stack.llm.provider_key` (the
process environment, else the file ``$STACK_ENV_FILE`` names; nothing is put into ``os.environ``,
no message carries a value):

* ``SEMANTIC_SCHOLAR_API_KEY`` (:func:`s2_key`): sent as the ``x-api-key`` header by
  ``collect.semantic_scholar``, ``collect.s2-lookup`` and acquire's OA lane gate (to
  ``api.semanticscholar.org`` only, :class:`stack.oa_adapter.RequestGate`, D592; fetchpdf never sees
  it). Free; not metered. Without it every S2 user is keyless as before.
* ``OPENALEX_API_KEY`` (:func:`openalex_key`): sent ONLY by an :class:`OpenAlexMeter` (as
  ``Authorization: Bearer``, never in a URL), so every keyed OpenAlex request is reserved in the
  OpenAlex ledger and counted against the run's caps. The older name ``OPENALEXAPIKEY`` is read by
  nothing in ``stack``/``workflows``; fetchpdf's own reads are neutralised in
  :mod:`stack.oa_adapter`.

**How the key is sent** (help.openalex.org/how-to-use-the-api/rate-limits-and-authentication,
read 2026-10-13): "Add it as an ``api_key`` query parameter … or send it as a bearer token in the
``Authorization`` header … Both work identically." The header is used, so the key never enters a
URL, and with it no ``rec.error``, coverage row, warning or journal fingerprint.

**Where a key may go** (:func:`key_url`): a request for exactly the provider's API host, over ``https``,
on the default port. The OpenAlex meter and the OA lane's S2 key use it; a plain-``http`` (or other-port)
request to that host is sent keyless, so a key never travels in the clear.

**Prices** (help.openalex.org/access/example-costs/, read 2026-10-13): per 1,000 calls "Get single
entity: Free", "List + filter: $0.10", "Search: $1"; one credit = $0.0001 ("adds 10 credits
($0.001)"); "A response reports what the query cost in ``meta.cost`` and in the
``X-RateLimit-Credits-Used`` header". A ``title_and_abstract.search`` filter is not named there;
the 2026-10 RUNLOG headers showed ~10-22 credits per such request, so a request whose filter or
params contain a search is priced as a search. **Reservation** = :data:`RESERVE_FACTOR` × the
documented price (covers the 22 credits seen); **reconciliation** = ``x-ratelimit-cost-usd`` when
the answer carries it (not in the docs; seen on every keyed answer 2026-10-13, with
``x-ratelimit-limit-usd``, ``x-ratelimit-prepaid-remaining-usd``, ``x-ratelimit-onetime-remaining``),
else ``x-ratelimit-credits-used`` × :data:`CREDIT_USD`, else the drop of ``x-ratelimit-remaining-usd``
since this run's previous answer (when it is in ``[0, 20 × reserved]``), else the reserved amount;
**release** on a transport failure (no answer). The ledger counts the free daily allowance ($1 a
day keyed, drawn before prepaid credit) as spend too, so its total overstates the prepaid draw.

**The ledger** is the run's OpenAlex ledger (:func:`stack.llm.ledger.run_ledger`, provider
``openalex``, in memory; the run record's ``spend.usd_micro.openalex``), capped per run at
:data:`CAP_USD` (the key's credit, Tomáš 2026-10-13), lowered (never raised) by
``$STACK_OPENALEX_CAP_USD`` — the X ledger's shape (:func:`stack.collect.xapi.x_ledger`). Keyless
requests are free and unmetered.

**Per-run caps** (:meth:`OpenAlexMeter.for_run`): stage options ``max_requests`` (keyed requests,
0 = none) and ``max_usd`` (default :data:`DEFAULT_RUN_MAX_USD` when keyed, so a forgotten option
cannot spend the credit; the stage line's ``budget.max_usd``, when set, lowers it). A request past
either cap is not sent: the meter raises :class:`OpenAlexBudgetStop`, the
user ends the run with the in-flight window/batch unwritten (its queries or keys stay due) and
``scope_complete = false``. Counts: ``openalex_keyed_requests``, ``openalex_cost_micro_usd``,
``openalex_reconciled_<cost_usd|credits|remaining_usd|reserved>``, ``openalex_budget_stop`` and
``openalex_budget_stop_<reason>``; keyless: ``openalex_keyless = 1`` and one stderr line per run.
``http_requests`` (the transport's) counts a request the cap refused too.
"""

from __future__ import annotations

import os
import sys
import urllib.parse
from collections.abc import Mapping
from typing import Any

from stack.collect import base
from stack.collect.base import CollectError, Collector, Sender
from stack.http import HttpResponse
from stack.llm.errors import BudgetExceeded
from stack.llm.ledger import Ledger, Reservation, run_ledger

S2_KEY_ENV = "SEMANTIC_SCHOLAR_API_KEY"
OPENALEX_KEY_ENV = "OPENALEX_API_KEY"
CAP_ENV = "STACK_OPENALEX_CAP_USD"
#: Tomáš 2026-10-13: the key carries $10 of credit. A run's ledger refuses any request past it.
CAP_USD = 10.00
#: Per keyed run when the stage line sets no ``max_usd`` (brief B4, decided).
DEFAULT_RUN_MAX_USD = 0.50
#: The ``options`` keys :meth:`OpenAlexMeter.for_run` reads (D434).
METER_OPTIONS: frozenset[str] = frozenset({"max_requests", "max_usd"})
#: Documented prices (module docstring).
CREDIT_USD = 0.0001
PRICE_LIST_USD = 0.10 / 1000
PRICE_SEARCH_USD = 1.00 / 1000
RESERVE_FACTOR = 2.5
HOST = "api.openalex.org"
_COST_USD = "x-ratelimit-cost-usd"
_CREDITS = "x-ratelimit-credits-used"
_REMAINING_USD = "x-ratelimit-remaining-usd"
_AUTH_HEADERS = ("authorization", "x-api-key")


def key_url(url: str, host: str) -> bool:
    """Whether a provider key for ``host`` may be attached to a request for ``url``: scheme ``https``,
    hostname exactly ``host`` (case-insensitive), no port other than 443 (module docstring)."""
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    return parts.scheme.lower() == "https" and (parts.hostname or "").lower() == host and port in (None, 443)


def _provider_key(name: str) -> str | None:
    from stack.llm import provider_key

    return provider_key(name)


def s2_key() -> str | None:
    """``SEMANTIC_SCHOLAR_API_KEY`` (module docstring), or ``None``."""
    return _provider_key(S2_KEY_ENV)


def s2_headers() -> dict[str, str]:
    """``{"x-api-key": <key>}`` when keyed, else ``{}``."""
    key = s2_key()
    return {"x-api-key": key} if key else {}


def openalex_key() -> str | None:
    """``OPENALEX_API_KEY`` (module docstring), or ``None``. Only :class:`OpenAlexMeter` sends it."""
    return _provider_key(OPENALEX_KEY_ENV)


def openalex_ledger(run_id: str) -> Ledger:
    """The run's OpenAlex ledger (:func:`stack.llm.ledger.run_ledger`, provider ``openalex``);
    cap :data:`CAP_USD`, lowered (never raised) by ``$STACK_OPENALEX_CAP_USD``."""
    cap = CAP_USD
    raw = os.environ.get(CAP_ENV, "").strip()
    if raw:
        try:
            cap = float(raw)
        except ValueError:
            raise CollectError(f"${CAP_ENV}={raw!r} is not a number") from None
        if not 0 < cap <= CAP_USD:
            raise CollectError(f"${CAP_ENV}={raw}: must be in (0, {CAP_USD}] (it may only lower the cap)")
    return run_ledger(run_id, "openalex", cap_usd=cap)


def request_kind(url: str) -> str:
    """``search`` (a ``search`` parameter, or a ``.search`` filter), ``single`` (``/works/W1``:
    free per the docs, reserved as a list call) or ``list``."""
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qs(parts.query)
    if any(k == "search" or k.startswith("search") for k in query) or any(
            ".search" in v for v in query.get("filter", [])):
        return "search"
    return "single" if len([p for p in parts.path.split("/") if p]) >= 2 else "list"


def reserve_usd(kind: str) -> float:
    return RESERVE_FACTOR * (PRICE_SEARCH_USD if kind == "search" else PRICE_LIST_USD)


class OpenAlexBudgetStop(Exception):
    """A keyed OpenAlex request was not sent: ``reason`` ∈ ``max_requests`` | ``max_usd``
    (module docstring). Not a :class:`stack.collect.base.RunHalted`: no provider
    ``retry_after``, so nothing is held; the caller ends the run ``scope_complete = false``."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"OpenAlex spend stop: {reason}")
        self.reason = reason


def strip_auth(headers: Mapping[str, str]) -> dict[str, str]:
    """``headers`` without credential headers (a caller's own must never ride along)."""
    return {k: v for k, v in headers.items() if k.lower() not in _AUTH_HEADERS}


class OpenAlexMeter:
    """Reserve → send → reconcile per keyed OpenAlex request (module docstring). ``key`` ``None``
    = keyless: :meth:`wrap` passes requests through unmetered (credential headers still stripped)."""

    def __init__(self, c: Collector, *, key: str | None, ledger: Ledger | None = None,
                 max_requests: int = 0, max_usd: float = DEFAULT_RUN_MAX_USD) -> None:
        if key and ledger is None:
            ledger = openalex_ledger(c.ctx.run_id)
        self.c, self._key, self.ledger = c, key or None, ledger
        self.max_requests, self.max_usd = int(max_requests), float(max_usd)
        self.requests = 0
        self._remaining_usd: float | None = None

    @property
    def keyed(self) -> bool:
        return self._key is not None

    @classmethod
    def for_run(cls, c: Collector) -> OpenAlexMeter:
        """The run's meter: the key, the stage options ``max_requests``/``max_usd`` (the line's
        ``budget.max_usd`` lowers the latter), the ledger. Keyless: ``openalex_keyless = 1`` and
        one stderr line."""
        key = openalex_key()
        if not key:
            c.counts["openalex_keyless"] = 1
            print(f"warning: {c.ctx.stage}: no {OPENALEX_KEY_ENV} (environment or $STACK_ENV_FILE); "
                  f"OpenAlex runs keyless on the free daily budget", file=sys.stderr)
            return cls(c, key=None)
        max_requests = int(c.option("max_requests", 0, cast=int) or 0)
        max_usd = float(c.option("max_usd", DEFAULT_RUN_MAX_USD, cast=float))
        budget = getattr(c.ctx.plan_stage, "budget", None)
        if budget is not None and budget.max_usd is not None:
            max_usd = min(max_usd, float(budget.max_usd))
        if max_requests < 0 or max_usd < 0:
            raise CollectError(f"{c.ctx.stage}: options max_requests/max_usd must be >= 0")
        meter = cls(c, key=key, max_requests=max_requests, max_usd=max_usd)
        c.counts["openalex_max_requests"] = max_requests
        c.counts["openalex_max_micro_usd"] = round(max_usd * 1e6)
        return meter

    def _bump(self, name: str, n: int = 1) -> None:
        self.c.counts[name] = self.c.counts.get(name, 0) + n

    def _stop(self, reason: str) -> OpenAlexBudgetStop:
        self.c.counts["openalex_budget_stop"] = 1
        self.c.counts[f"openalex_budget_stop_{reason}"] = 1
        return OpenAlexBudgetStop(reason)

    def reserve(self, url: str) -> Reservation:
        """Check the run caps and reserve one request; raises :class:`OpenAlexBudgetStop`."""
        assert self.ledger is not None
        if self.max_requests and self.requests >= self.max_requests:
            raise self._stop("max_requests")
        kind = request_kind(url)
        try:
            res = self.ledger.reserve(run_id=self.c.ctx.run_id, stage=self.c.ctx.stage,
                                      model=f"openalex/{kind}", attempt="first",
                                      amount_usd=reserve_usd(kind), tokens_in=0, tokens_out=0,
                                      run_budget_usd=self.max_usd)
        except BudgetExceeded:
            raise self._stop("max_usd") from None
        self.requests += 1
        self._bump("openalex_keyed_requests")
        return res

    def actual_usd(self, res: Reservation, headers: Mapping[str, str]) -> tuple[float, str]:
        """``(actual, source)`` from the answer's headers (module docstring)."""
        h = {str(k).lower(): v for k, v in headers.items()}
        remaining = _float(h.get(_REMAINING_USD))
        previous, self._remaining_usd = self._remaining_usd, remaining
        cost = _float(h.get(_COST_USD))
        if cost is not None and cost >= 0:
            return cost, "cost_usd"
        credits = _float(h.get(_CREDITS))
        if credits is not None and credits >= 0:
            return credits * CREDIT_USD, "credits"
        if remaining is not None and previous is not None and 0 <= previous - remaining <= 20 * res.reserved_usd:
            return previous - remaining, "remaining_usd"
        return res.reserved_usd, "reserved"

    def settle(self, res: Reservation, resp: HttpResponse | None) -> None:
        assert self.ledger is not None
        if resp is None or resp.status is None:
            self.ledger.release(res)
            self._bump("openalex_released")
            return
        actual, source = self.actual_usd(res, resp.headers or {})
        self.ledger.reconcile(res, actual_usd=actual, tokens_in=0, tokens_out=0)
        self._bump(f"openalex_reconciled_{source}")
        self._bump("openalex_cost_micro_usd", round(actual * 1e6))

    def wrap(self, send: Sender | None = None) -> Sender:
        """A :class:`stack.collect.base.Sender` metering every request to :data:`HOST` (others
        pass through). ``None`` = the harness's default sender, looked up at call time."""

        def inner(method: str, url: str, *, headers: Mapping[str, str], body: bytes | None,
                  timeout: float, max_bytes: int) -> HttpResponse:
            fn = send if send is not None else base.urllib_send
            return fn(method, url, headers=headers, body=body, timeout=timeout, max_bytes=max_bytes)

        def metered(method: str, url: str, *, headers: Mapping[str, str], body: bytes | None,
                    timeout: float, max_bytes: int) -> HttpResponse:
            if (urllib.parse.urlsplit(url).hostname or "").lower() != HOST:
                return inner(method, url, headers=headers, body=body, timeout=timeout, max_bytes=max_bytes)
            clean = strip_auth(headers)
            if self._key is None or not key_url(url, HOST):  # keyless; plain http never carries the key
                return inner(method, url, headers=clean, body=body, timeout=timeout, max_bytes=max_bytes)
            res = self.reserve(url)
            resp: HttpResponse | None = None
            try:
                resp = inner(method, url, headers={**clean, "Authorization": f"Bearer {self._key}"},
                             body=body, timeout=timeout, max_bytes=max_bytes)
            finally:
                self.settle(res, resp)
            return resp

        return metered


#: The stage option that restores an OpenAlex field list (D525: by default no ``select``, full works).
SELECT_OPTION = "select"


def openalex_select(value: Any, required: str) -> str | None:
    """The ``select`` parameter of an OpenAlex request from a stage's ``select:`` option (D525): ``None``
    (absent: no ``select``, full work objects, which become the rows' ``provider_record``), else the fields
    as a comma list (a string ``"a,b"`` or a YAML list). It must name every field of ``required`` (the
    stage's former list, whose fields the stage reads and digests), else ``ValueError``."""
    if value is None:
        return None
    parts = value.split(",") if isinstance(value, str) else value
    if not isinstance(parts, list | tuple) or not all(isinstance(f, str) for f in parts):
        raise ValueError(f"option select: a comma list or a list of field names, got {value!r}")
    fields = list(dict.fromkeys(f.strip() for f in parts if f.strip()))
    missing = [f for f in required.split(",") if f not in fields]
    if missing:
        raise ValueError(f"option select: {missing} missing; the stage reads them (required: {required})")
    return ",".join(fields)


def _float(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


__all__ = ["CAP_USD", "DEFAULT_RUN_MAX_USD", "METER_OPTIONS", "OPENALEX_KEY_ENV", "S2_KEY_ENV", "OpenAlexBudgetStop",
           "OpenAlexMeter", "openalex_key", "openalex_ledger", "request_kind", "s2_headers", "s2_key"]
