"""The collector harness (§3.1 collect row, §5.2, §5.4, §5.6, §6 C32, A.5; D8, D13, D26, D54,
D72, D92): what every ``collect.<suffix>`` stage builds on.

How a collector uses this
=========================

A collector stage ``collect.<suffix>`` lives in ``workflows/collect/<suffix>/main.py`` and
exposes ``run(ctx) -> StageResult`` (:mod:`stack.stage_runner`). Its plan line carries
``prior: [sources, coverage]`` and its ``options:`` (D72), e.g.::

    - {stage: collect.arxiv_sweep, prior: [sources, coverage], options: {window_days: 21}}

A run reads only its ``--in`` paths (D373): ``stack run collect.arxiv_sweep --in
data/sources.arxiv_sweep/<series>/ --in data/coverage.arxiv_sweep/<series>/`` pins the stage's own
series directories (a build passes the same paths). On the first run they are empty and the harness
starts them (a full file, base ``none``). A run that pins no own ``sources``/``coverage`` while the
destination directory has files is refused by the runner's restart guard without ``--restart`` (D377;
:func:`stack.stage_runner.restart_guarded`; before the run for an explicit ``--out``, else when it
emits): reading no prior would re-mint every ``source_id``.
A pinned chain that is not the collector's own is a :class:`CollectError`. A run with no own
``sources`` prior into a series with no ``documents`` while another series has some is refused by
the series guard unless ``--new-series`` (D491; :func:`stack.stage_runner.new_series_refusal`).

The skeleton (names are this module's)::

    from stack.collect.base import Collector, Pacing, RunHalted

    PACING = Pacing(rps=1 / 3.0, retry_delays_s=(2.0, 8.0, 20.0))  # the provider's v3 constants
    PAGE_SIZE = 100                                                # module-level test seams:
    def _sleep(s: float) -> None: time.sleep(s)                    # tests monkeypatch these
    def http_get_json(url, params): return _T.get_json(url, params)

    def run(ctx):
        global _T
        c = Collector(ctx)
        days = c.option("window_days", 7, cast=int)       # an env= name would win over options
        _T = c.transport(PACING, sleep=lambda s: _sleep(s))
        with c.halts():                     # a run-level 429 (RunHalted) ends the loop here
            for q in queries:
                since = c.watermark("arxiv", q)          # newest end of an `ok` window, or None
                with c.window("arxiv", q, start, end) as w:
                    for page in pages:
                        status, data = http_get_json(url, params)
                        for item in data:
                            w.seen += 1
                            c.observe("arxiv", ext_id, item, keys=[("arxiv", ext_id)],
                                      metadata={"title": ..., "published_at": ...})
                            c.add_inband(c.source_id("arxiv", ext_id), "abstract",
                                         text=item["summary"], media_type="text/plain")
                            w.kept += 1
                    w.close("complete")      # v3 receipts: complete | truncated | error
        return c.finish()

**Pieces.**

* Config: :meth:`Collector.channel` (``configs/channels.yaml`` via :mod:`stack.config`, or the
  ``channels=`` given), :meth:`Collector.option` (plan ``options``; a set env var wins).
* Identity (C32): :meth:`Collector.source_id` looks ``(channel, external_id)`` up in the own
  ``sources`` view and mints (:func:`stack.core.ids.mint`) only when absent; re-runs mint none.
  :meth:`Collector.prior` is the latest row of that source in the own chain.
* Rows: :meth:`Collector.observe` (``state observed``, ``record_format`` default ``record``,
  ``status ok``, ``record_digest`` over the channel's ``record_projection``; ``skip_unchanged``
  writes nothing when the digest equals the prior observation's, §5.6 OpenAlex) and
  :meth:`Collector.miss` (``state missing``, or ``retracted`` after the plan line's ``miss_cap``
  consecutive misses under ``miss_means: withdrawn``, D13; ``record_format miss``, ``status
  empty``; V1). One row per ``source_id`` per run: a second observation of a
  source in the same run is counted (``sources_repeat``) and its record dropped (the first stands).
* Attribution (round 4, D228): ``observe``/``miss`` take the coverage ``query`` that returned the
  source (the string or mapping given to :meth:`Collector.window`), a readable ``query_id``
  (default: :func:`query_text`, the string itself or a mapping's JCS) and a human ``label`` (term,
  author, feed). Each becomes one ``metadata.matched_by`` entry ``{query_id, query_digest,
  label}``; a repeated observation in the run adds its entry to the standing row (deduplicated,
  sorted), and :meth:`Collector.attribute` adds one without observing again (a collector that
  skips its own repeats). ``metadata.query_digest`` is the first entry's digest.
* In-band (§5.4, D8): :meth:`Collector.add_inband` mints through :class:`InbandSink` (the mint
  rule, :mod:`stack.collect.inband`) into ``ctx.blobs`` and puts the ``INBAND_ENTRY`` on the
  source's row; roles not offered are carried from the prior row.
* Provider records (D522): :meth:`Collector.observe` with ``provider_record=`` (or
  :meth:`Collector.add_record`) mints the source's record of the item as received as the row's
  ``provider_record`` in-band entry (canonical JSON, or raw bytes with their media type), on a sink
  of its own (``record_*`` counts, ``record_bytes``). It is not content: ``acquire`` never mints it.
  A row skipped as unchanged writes no record, so a record dates from the last change of the
  projected fields (:mod:`stack.collect.inband`, "Provider records").
* Coverage: :meth:`Collector.window` yields a :class:`Window` (``seen``, ``kept``; one outcome:
  :meth:`Window.complete` → ``ok``/exhausted, :meth:`Window.truncated` → ``partial``,
  :meth:`Window.fail` → ``failed_*``; :meth:`Window.close` maps v3's ``exhausted`` strings).
  ``attempts`` count over runs per window key; a window left without an outcome is a
  :class:`CollectError`. :meth:`Collector.watermark` = max ``window_end_at`` of the own chain's
  ``ok`` rows per ``(channel, query_digest)``: only ``ok`` rows advance it.
* Date sub-windows (round 5 (RUN) B7a): a first window too large for one pull (a backfill from a
  far floor) is split by :func:`date_windows` into consecutive ``[s, e]`` spans of a fixed number of
  days, asked in ascending order, each written as its own coverage window. The collector stops a
  query's sequence at the first span that is not ``ok``: a later ``ok`` span would carry the
  watermark (the newest ``ok`` end) over the hole. Since the watermark is the end of the last
  ``ok`` span, the next run or pass resumes at the next span. Used by ``collect.lw_graphql``
  (option ``subwindow_days``) and ``collect.af_graphql`` (holes); ``collect.semantic_scholar``
  splits the same way on 1 January of recent years (option ``split_years``, D801), so a failed page
  costs one span, not the whole pass (its bulk token is not persisted). OpenAlex (cursor) raises
  the per-query cap instead (option ``max_records``), and arXiv's ascending pull banks its own
  prefix.
* Floor moved back (round 5 (RUN) B7a): :meth:`Collector.holes` lists the spans of ``[floor,
  watermark]`` no ``ok`` window of the query covers (the union of the own chain's ``ok`` windows).
  When a plan's ``from`` moves earlier than the first window (2025-10-01 → 2024-11-30), the hole
  is ``[new floor, old floor]``; a collector asks each hole as a bounded window before its usual
  tail ``[watermark, today]``, so only the gap is swept, not the whole range again. :meth:`due`
  with ``floor`` treats a query with a hole as due even when its watermark reaches ``until``.
  A window whose ``start`` the floor passes (``window_days`` moving forward) leaves no hole.
* Passes (round 4, D226): :meth:`Collector.due` is the one test of whether a unit (a ``(channel,
  query)``) needs asking this run, so a second pass minutes after the first repeats no completed
  work. Not due: the channel is held by a provider ``retry_after`` still in the future
  (:meth:`Collector.held_until`; counted ``held_retry_after``, the run ends ``scope_complete =
  false`` as the halt did); the ``ok`` watermark already reaches ``until`` (``already_covered``);
  with ``relist_after``, the unit's newest attempt (:meth:`Collector.last_attempt`) is younger than
  that and was ``ok`` (a listing snapshot) or ``partial`` with ``partial_due=False`` (a cap that
  would truncate the same way again) — counted ``not_due``. Failed units are always due.
  :meth:`Collector.relist_after` reads the plan line's ``relist_after_h`` (default
  :data:`RELIST_AFTER_H`). :data:`SKIP_COUNTS` names the skip counters; ``http_requests`` starts
  at 0, so a pass-2 record reads "0 requests, N covered".
* Transport (D26): :meth:`Collector.transport` gives a :class:`Transport` over
  :func:`stack.http.urllib_get` (which goes through ``provider_transport.throttled_open``, the
  cross-process ``Gate``) paced by a :class:`stack.http.HostPacer` at **the provider's**
  :class:`Pacing` (no global default: ``rps`` and the retry ladder are required from the
  collector). ``get``/``post`` = one request; ``fetch`` = with the provider's retry ladder;
  ``get_json``/``post_json`` = v3's ``(status, data)`` seam (``0`` = transport failure).
* Run-level 429 (§5.6, OpenAlex ``:113-118``): a 429 whose retry-after exceeds
  ``Pacing.halt_429_after_s`` raises :class:`RunHalted`. The harness then makes no further
  request (every later call raises at once), the open window is written
  ``failed_transient[rate_limit]`` with ``retry_after`` and **no attempt charged** (its
  ``attempts`` stay the prior count, 0 on a first try), it is never ``ok`` so the watermark
  does not move, and :meth:`Collector.halts` swallows the halt so the observations already made
  are emitted; the run ends ``ok`` with ``scope_complete = false``.
* Drain (D570; :mod:`stack.stage_runner`, "Drain"): once the process drains, the harness starts no
  window (:meth:`Collector.window` raises :class:`RunStopped` on entry) and sends no request
  (:meth:`Transport.request` raises it before sending, so no new page or batch). :class:`RunStopped`
  is a :class:`RunHalted` (``c.halted`` is set, so a collector's own halt handling applies: no misses
  after an incomplete listing), but the window it interrupts writes **no** coverage row (abandoned as
  not attempted: no attempt charged, no watermark move, due again next run) and ``halted`` is not
  counted. :meth:`Collector.halts` swallows it; a collector without ``halts()`` lets it reach the
  runner, which calls the harness's :meth:`Collector.finish` (``ctx.on_stopped``). Either way the
  observations already made and the windows already closed are emitted, and the run ends ``ok``,
  ``scope_complete = false``, ``stopped.drain``. A request in flight ends on its own timeout.
* Gate cooldown (D401): a provider 429 sets the ``Gate`` cooldown (60 s without retry-after; S2:
  a backoff of 5 s doubling to 60 s, reset by a 2xx, D550),
  during which ``throttled_open`` answers a synthetic 429 without sending. ``Transport.request``
  waits a cooldown of at most ``halt_429_after_s`` (:data:`COOLDOWN_WAIT_MAX_S` when unset) and
  re-sends, counting ``http_cooldown`` (not ``http_requests``/``http_429``, no penalty); a longer
  one halts like a real 429 when the pacing halts, else comes back as the 429. A real 429 on a
  gate-paced family halves the pacer no lower than the Gate's interval. D588: :meth:`Transport.fetch`
  returns a cooldown answer that ``request`` gave back at once (never a ladder step: it would only meet
  our own cooldown again), retries a 429/503 no sooner than its stated wait (one over the threshold is
  returned at once), and never retries a plan refusal (D583).
* 429 waits (D521, D550): a pacing with ``wait_429`` (``collect.semantic_scholar``,
  ``collect.s2-lookup``: :data:`BACKOFF_WAITS`) has :meth:`Transport.fetch` wait a real 429 out (its
  retry-after, else the Gate's backoff, at most the halt threshold) and re-send, up to ``wait_429``
  times, instead of spending a ladder step on it; counted ``http_429_waits``.
* Live progress: ``Collector.counts`` *is* ``ctx.counts``, so the provisional run record shows
  the run's counters while it runs: ``http_requests``, ``http_retries``, ``http_429``,
  ``http_5xx``, ``http_transport_errors``, ``pages`` (2xx responses), ``windows_<status>``,
  ``rows_so_far`` (observations buffered) and, when the collector calls
  :meth:`Collector.progress`, ``queries_done``/``queries_total``. The harness calls
  ``ctx.heartbeat()`` from the request path at most every :data:`HEARTBEAT_S` seconds (the first
  request beats at once), from :meth:`Collector.progress` under the same throttle, and at every
  window boundary.
* :meth:`Collector.finish` emits the run's ``sources`` and ``coverage`` rows, each as one part
  on its own chain (a full file when the chain is new), ``blocks_written='*'`` (D53), fills
  ``ctx.counts`` and returns the :class:`StageResult`. A family with no rows this run writes no
  file. A window of this run that ended ``failed_transient`` makes the run ``scope_complete =
  false`` (R3-2; the next run or pass retries it); ``failed_permanent`` windows do not (counted
  ``windows_failed_permanent.<error_class>``, a build warning).
* Caps (A5, D492; S25-21): a window a cap stopped short (:meth:`Window.truncated`: ``max_records``, a
  provider's own cap such as forums' 10,000-hit result window (D642) or GitHub's truncated tree, a ``max_urls``/``max_repos``
  or page cap) is ``partial`` and counted ``windows_capped``; a unit :meth:`Collector.due` skips because its
  newest attempt was such a ``partial`` (``partial_due=False``: it would truncate the same way) is counted
  ``capped_not_due``. Either makes the run ``scope_complete = false``, build reason ``limit``
  (:data:`stack.build.INCOMPLETE_REASONS`), so the build ends ``incomplete``, not ``ok``, until the cap is
  raised or the window split. ``truncated(cap=False)`` is a ``partial`` that is not a cap (v3_import's next
  slice, links' undecided candidates, x-lookup's budget stop): those runs say why they are short themselves.

Key typing is not done here: ``keys`` are taken as given ``(kind, value)`` pairs; every collector types its keys
through :mod:`stack.scholarly_records` (``normalize_*``) and :mod:`stack.urls` before calling :meth:`Collector.observe`.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

import pyarrow as pa

from stack import identity
from stack.collect.inband import JSON_MEDIA_TYPE, RECORD_ROLE, Entry, InbandSink, record_bytes
from stack.collect.sampling import is_cap_env
from stack.config import ChannelDef
from stack.core import ids
from stack.core.hashes import jcs
from stack.http import HostPacer, HttpResponse, decode_content, urllib_get
from stack.provider_transport import COOLDOWN_WAITS, family, gate_interval, is_cooldown, limited_delay, retry_seconds
from stack.schemas.families import family as family_spec
from stack.schemas.vocab import ERROR_CLASSES, SOURCE_ORIGINS
from stack.stage_runner import StageContext, StageResult, StageStopped
from stack.store.job import say

Row = dict[str, Any]
T = TypeVar("T")

PHASE = "collect"
RECORD = "record"
MISS = "miss"
#: ``record_digest`` of a miss row: there is no record (sha256 of JCS ``null``).
MISS_DIGEST = hashlib.sha256(jcs(None).encode()).hexdigest()
#: v3 coverage receipts (``exhausted``) → :class:`Window` outcomes.
EXHAUSTED = ("complete", "truncated", "error")
#: Minimum seconds between two progress heartbeats from the request path / :meth:`Collector.progress`.
HEARTBEAT_S = 15.0
#: Default ``relist_after_h`` (D226): a unit attempted this recently is not asked again in a later
#: pass when its newest attempt was a listing ``ok`` or a deterministic ``partial``; also the
#: lookups' default wait before a missed key is asked again.
RELIST_AFTER_H = 12.0
#: The ``options`` key :meth:`Collector.relist_after` reads (a stage calling it declares it, D434).
RELIST_OPTIONS: frozenset[str] = frozenset({"relist_after_h"})
#: The run counters of units :meth:`Collector.due` skipped (D226).
SKIP_COUNTS = ("already_covered", "not_due", "held_retry_after")
_METADATA_FIELDS = tuple(f.name for f in family_spec("sources").schema.field("metadata").type)


class CollectError(Exception):
    """A collector broke the harness protocol (unknown channel, a window without an outcome, a
    row for an unobserved source, an unpinned own chain with files, …)."""


class RunHalted(Exception):
    """A run-level rate limit (§5.6): no further request this run; :meth:`Collector.halts`
    catches it. ``retry_after_s`` = the provider's retry-after in seconds."""

    def __init__(self, retry_after_s: float, reason: str) -> None:
        super().__init__(reason)
        self.retry_after_s = retry_after_s
        self.reason = reason


class RunRefused(RunHalted):
    """A provider refused the run's account, not a request: no retry or wait fixes it (OpenAlex's
    "Plan upgrade required" for a filter the plan does not include, a rejected key). A
    :class:`RunHalted` (no further request this run, :meth:`Collector.halts` swallows it), but the
    window it interrupts is written ``failed_permanent[<error_class>]`` (charged: the query asked for
    what the account cannot have) or, for a ``failed_transient`` class (``provider_auth``),
    ``failed_transient[<error_class>]`` uncharged, never ``rate_limit``; no ``retry_after``, no hold.
    ``reason`` (credential-free) goes into that row's ``error`` and the run record's ``error``
    (:meth:`Collector.finish`)."""

    def __init__(self, error_class: str, reason: str, *, permanent: bool) -> None:
        status = "failed_permanent" if permanent else "failed_transient"
        if error_class not in ERROR_CLASSES[status]:
            raise CollectError(f"{status}[{error_class}]: not in {sorted(ERROR_CLASSES[status])}")
        super().__init__(0.0, reason)
        self.error_class = error_class
        self.permanent = permanent


class RunStopped(RunHalted, StageStopped):
    """A drain (module docstring, Drain): no further window or request this run; the interrupted
    window writes no row. A :class:`RunHalted` for the collectors' halt handling, a
    :class:`~stack.stage_runner.StageStopped` for the runner."""

    def __init__(self) -> None:
        super().__init__(0.0, "drain: the run stops admitting work")


# ---------------------------------------------------------------- small helpers
def utcnow() -> dt.datetime:
    """The harness clock (:attr:`Collector.now` when no ``now`` is given); a test seam."""
    return dt.datetime.now(dt.UTC)


def utc(value: str | dt.datetime | dt.date | None) -> dt.datetime | None:
    """An aware UTC datetime from an ISO string (``Z`` accepted), a date or a datetime; naive =
    UTC; ``None`` passes. Raises ``ValueError`` on a malformed string."""
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        d = value
    elif isinstance(value, dt.date):
        d = dt.datetime(value.year, value.month, value.day)
    else:
        d = dt.datetime.fromisoformat(str(value).strip())
    return d.astimezone(dt.UTC) if d.tzinfo is not None else d.replace(tzinfo=dt.UTC)


def query_text(query: str | Mapping[str, Any]) -> str:
    """The ``coverage.query`` string: a string as is, a mapping as its JCS."""
    return query if isinstance(query, str) else jcs(_jsonable(query))


def query_digest(query: str | Mapping[str, Any]) -> str:
    """``coverage.query_digest`` = sha256 hex of :func:`query_text`."""
    return hashlib.sha256(query_text(query).encode("utf-8")).hexdigest()


def _jsonable(value: Any) -> Any:
    """JSON-safe copy (datetimes and other non-JSON leaves rendered with ``str``)."""
    return json.loads(json.dumps(value, default=str, ensure_ascii=False))


def record_digest(record: Mapping[str, Any], projection: Sequence[str] = ()) -> str:
    """``sources.record_digest`` (C23): sha256 hex of JCS over the record's ``projection`` fields
    in order (``{field: value}``, an absent field as ``null``), or over the whole record when the
    projection is empty. Counters and volatile fields stay out by listing only stable fields in
    the channel's ``record_projection``."""
    proj = {f: record.get(f) for f in projection} if projection else dict(record)
    return hashlib.sha256(jcs(_jsonable(proj)).encode("utf-8")).hexdigest()


def _authors(value: Any) -> list[Row] | None:
    if value is None:
        return None
    out = []
    for a in value:
        if isinstance(a, str):
            out.append({"name": a, "orcid": None, "affiliation_raw": None})
        else:
            out.append({"name": a["name"], "orcid": a.get("orcid"),
                        "affiliation_raw": a.get("affiliation_raw")})
    return out


def _match(query: str | Mapping[str, Any] | None, query_id: str | None,
           label: str | None) -> Row | None:
    """The ``metadata.matched_by`` entry of one query (module docstring, "Attribution"), or
    ``None`` when no query is given."""
    if query is None:
        if query_id is not None or label is not None:
            raise CollectError("query_id/label given without the query they belong to")
        return None
    if query_id is None:
        query_id = query_text(query)  # a mapping as its JCS (callers that predate query_id)
    return {"query_id": str(query_id), "query_digest": query_digest(query),
            "label": None if label is None else str(label)}


def _add_match(row: Row, match: Row | None) -> Row:
    """Add ``match`` to the row's ``matched_by`` (deduplicated, sorted); the first match sets
    ``metadata.query_digest``. Returns the row."""
    if match is None:
        return row
    md = row["metadata"]
    found = md["matched_by"] if md["matched_by"] is not None else []
    if match not in found:
        found = sorted([*found, match], key=lambda m: (m["query_id"], m["label"] or "",
                                                       m["query_digest"]))
    md["matched_by"] = found
    if md["query_digest"] is None:
        md["query_digest"] = match["query_digest"]
    return row


# ---------------------------------------------------------------- transport
#: Longest Gate cooldown :meth:`Transport.request` waits out when the pacing sets no
#: ``halt_429_after_s`` (D401). How many cooldowns one request waits at most is
#: ``provider_transport.COOLDOWN_WAITS`` (3; re-exported here), shared with the acquire loops (D588).
COOLDOWN_WAIT_MAX_S = 60.0
#: ``Pacing.wait_429`` for a family whose Gate backs off (D550, ``provider_transport.BACKOFF_FAMILIES``):
#: 5 + 10 + 20 + 40 + 60 + 60 = 195 s of waits for one request at most, about D521's 3 x 60 s.
BACKOFF_WAITS = 6


def _is_cooldown(resp: HttpResponse) -> bool:
    """The synthetic 429 ``throttled_open`` answers during a Gate cooldown (nothing was sent)."""
    return resp.status == 429 and is_cooldown(resp.headers)


@dataclass(frozen=True)
class Pacing:
    """A provider's pacing and retry constants, **from its v3 collector** (D26; no harness
    default stands in for them). ``rps`` = requests per second per host (``1 / delay_s``);
    ``retry_delays_s`` = the backoff ladder :meth:`Transport.fetch` sleeps between attempts
    (``()`` = one attempt); ``retry_statuses`` = what is retried (``0`` = transport failure);
    ``halt_429_after_s`` = a 429 whose retry-after (header, or body ``retryAfter``,
    :func:`stack.provider_transport.retry_seconds`; 60 s when neither says) is **above** this
    halts the run (:class:`RunHalted`); ``None`` = a 429 never halts, it is retried.
    ``wait_429`` (D521): :meth:`Transport.fetch` waits a real 429 out (its retry-after; when unsaid
    the Gate's backoff for S2, D550, else 60 s; at most ``halt_429_after_s`` or
    :data:`COOLDOWN_WAIT_MAX_S`) and re-sends, at most this many times per call, instead of taking
    a ladder step (``0`` = the ladder handles 429)."""

    rps: float
    retry_delays_s: tuple[float, ...]
    retry_statuses: frozenset[int] = frozenset({0, 429, 500, 502, 503, 504})
    halt_429_after_s: float | None = None
    wait_429: int = 0
    timeout_s: float = 60.0
    max_bytes: int = 64 * 1024 * 1024
    headers: Mapping[str, str] = field(default_factory=dict)


class Sender(Protocol):
    """The transport seam (``tests/fakes/http.py`` implements it): one request, never raises
    for an HTTP or transport failure (status ``None`` + ``error`` instead)."""

    def __call__(self, method: str, url: str, *, headers: Mapping[str, str], body: bytes | None,
                 timeout: float, max_bytes: int) -> HttpResponse: ...


def urllib_send(method: str, url: str, *, headers: Mapping[str, str], body: bytes | None,
                timeout: float, max_bytes: int) -> HttpResponse:
    """The default :class:`Sender`: GET through :func:`stack.http.urllib_get` (restricted opener,
    ``throttled_open`` Gate); POST through ``throttled_open`` with the stdlib opener (the forum
    GraphQL/search endpoints, as v3 posted them). Total: never raises. Both decode a gzip/deflate
    body the server sent unasked, capped at ``max_bytes + 1`` (:func:`stack.http.decode_content`)."""
    if method == "GET" and body is None:
        return urllib_get(url, headers=headers, timeout=timeout, max_bytes=max_bytes)
    from stack.provider_transport import throttled_open

    req = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
    try:
        with throttled_open(req, opener=urllib.request.urlopen, timeout=timeout) as resp:
            data, hdrs = decode_content(resp.read(max_bytes + 1), resp.headers, max_bytes)
            return HttpResponse(int(resp.status), hdrs, data, resp.geturl())
    except urllib.error.HTTPError as exc:
        try:
            data = exc.read(max_bytes + 1)
        except Exception:  # noqa: BLE001 — the status is what matters
            data = b""
        data, hdrs = decode_content(data, exc.headers or {}, max_bytes)
        return HttpResponse(int(exc.code), hdrs, data, url)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return HttpResponse(None, {}, b"", url, error=type(exc).__name__)
    except Exception:  # noqa: BLE001 — total, like urllib_get
        return HttpResponse(None, {}, b"", url, error="other")


def full_url(url: str, params: Mapping[str, Any] | None) -> str:
    """``url`` with ``params`` urlencoded in the given order (``doseq``), as v3 built them."""
    if not params:
        return url
    sep = "&" if urllib.parse.urlsplit(url).query else "?"
    return f"{url}{sep}{urllib.parse.urlencode(dict(params), doseq=True)}"


class Transport:
    """Paced provider HTTP for one run (module docstring). Counts go to the collector's
    ``counts`` (``http_requests``, ``http_retries``, ``http_429``, ``http_5xx``,
    ``http_transport_errors``, ``pages`` = 2xx responses, ``http_cooldown`` = synthetic Gate
    cooldown answers, not sent and not in ``http_requests``; ``http_bytes`` and ``http_ms`` = the
    sent requests' response bytes and wall milliseconds, D523), live (module docstring)."""

    def __init__(self, collector: Collector, pacing: Pacing, *, send: Sender | None = None,
                 sleep: Callable[[float], None] | None = None,
                 monotonic: Callable[[], float] | None = None) -> None:
        if pacing.rps <= 0:
            raise CollectError(f"Pacing.rps must be > 0, got {pacing.rps}")
        self.collector = collector
        self.pacing = pacing
        self._send = send or urllib_send
        self._sleep = sleep or time.sleep
        self._clock = monotonic or time.monotonic
        self.pacer = HostPacer(monotonic=monotonic or time.monotonic,
                               sleep=lambda s: self._sleep(s), default_rps=pacing.rps)

    def _count(self, name: str, n: int = 1) -> None:
        self.collector.counts[name] = self.collector.counts.get(name, 0) + n

    def request(self, method: str, url: str, *, params: Mapping[str, Any] | None = None,
                headers: Mapping[str, str] | None = None,
                body: bytes | None = None) -> HttpResponse:
        """One paced request. Raises :class:`RunHalted` without sending once the run is halted,
        and on a halting 429 (which halts the run); :class:`RunStopped` once the process drains."""
        stopping = getattr(self.collector, "stopping", None)  # a stand-in collector may have no drain
        if self.collector.halted is None and stopping is not None and stopping():
            raise self.collector.stop()
        if self.collector.halted is not None:
            raise self.collector.halted
        target = full_url(url, params)
        host = urllib.parse.urlsplit(target).hostname or ""
        self.pacer.acquire(host)
        # A Gate cooldown (another request's 429, any process) answers a synthetic 429 without
        # sending: wait it out when it is short (<= the halt threshold), up to COOLDOWN_WAITS
        # times; it is counted as `http_cooldown`, never as a request or `http_429` (D401).
        limit = (self.pacing.halt_429_after_s if self.pacing.halt_429_after_s is not None
                 else COOLDOWN_WAIT_MAX_S)
        for waits in range(COOLDOWN_WAITS + 1):
            self._count("http_requests")  # before sending: the live record shows it in flight
            self.collector.beat()
            t0 = self._clock()
            resp = self._send(method, target, headers={**self.pacing.headers, **(headers or {})},
                              body=body, timeout=self.pacing.timeout_s,
                              max_bytes=self.pacing.max_bytes)
            if not _is_cooldown(resp):
                # D523: response size and time, so a run's page latency and size are measurable
                self._count("http_bytes", len(resp.body or b""))
                self._count("http_ms", int(round((self._clock() - t0) * 1000)))
                break
            self._count("http_requests", -1)  # nothing was sent
            self._count("http_cooldown")
            wait = retry_seconds(dict(resp.headers), resp.body)
            if wait > limit or waits == COOLDOWN_WAITS:
                break
            self._sleep(wait)
            if self.collector.halted is not None:
                raise self.collector.halted
        if _is_cooldown(resp):
            if self.pacing.halt_429_after_s is not None and wait > self.pacing.halt_429_after_s:
                raise self.collector.halt(wait, f"{host}: provider cooldown, {wait:.0f}s left")
            self.collector.beat()
            return resp
        if resp.status is None:
            self._count("http_transport_errors")
        elif resp.status == 429:
            self._count("http_429")
            # The Gate already holds a gate-paced family to its interval and sets the cooldown:
            # the pacer is not halved below that interval (D401); other hosts halve as before.
            interval = gate_interval(family(target))
            if interval is None:
                self.pacer.penalize(host)
            else:
                self.pacer.penalize(host, floor_rps=1.0 / interval)
            wait = retry_seconds(dict(resp.headers), resp.body)
            if self.pacing.halt_429_after_s is not None and wait > self.pacing.halt_429_after_s:
                raise self.collector.halt(wait, f"{host}: 429, retry after {wait:.0f}s")
        elif 500 <= resp.status < 600:
            self._count("http_5xx")
        elif 200 <= resp.status < 300:
            self._count("pages")
        self.collector.beat()
        return resp

    def get(self, url: str, params: Mapping[str, Any] | None = None, *,
            headers: Mapping[str, str] | None = None) -> HttpResponse:
        return self.request("GET", url, params=params, headers=headers)

    def post(self, url: str, payload: Any, *, params: Mapping[str, Any] | None = None,
             headers: Mapping[str, str] | None = None) -> HttpResponse:
        """POST ``payload`` as JSON (bytes pass as is)."""
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        h = {"Content-Type": "application/json", **(headers or {})}
        return self.request("POST", url, params=params, headers=h, body=data)

    def fetch(self, method: str, url: str, *, params: Mapping[str, Any] | None = None,
              headers: Mapping[str, str] | None = None, body: bytes | None = None) -> HttpResponse:
        """:meth:`request` with the provider's retry ladder: a status in ``retry_statuses``
        (``0`` = transport failure) sleeps the next ``retry_delays_s`` and retries; returns the
        last response (the caller turns a failed one into an ``error`` window). With
        ``Pacing.wait_429`` a real 429 is waited out instead (its retry-after, capped at the halt
        threshold; counted ``http_429_waits``), at most ``wait_429`` times, without using a ladder
        step; a 429 after the last wait is returned (D521). D588: a Gate cooldown answer that
        :meth:`request` returned (it did not wait it out: too long, or its waits were spent) is
        returned at once, never laddered (a step would only meet the cooldown again); a 429 or 503
        that states a wait is retried no sooner than that wait, and returned at once when the wait
        is over the halt threshold (:func:`stack.provider_transport.limited_delay`); a plan refusal
        (D583) is never retried."""
        limit = (self.pacing.halt_429_after_s if self.pacing.halt_429_after_s is not None
                 else COOLDOWN_WAIT_MAX_S)
        waits = 0
        ladder = iter((*self.pacing.retry_delays_s, None))
        while True:
            resp = self.request(method, url, params=params, headers=headers, body=body)
            if resp.status == 429 and self.pacing.wait_429 and not _is_cooldown(resp):
                if waits >= self.pacing.wait_429:
                    return resp
                waits += 1
                self._count("http_429_waits")
                self._sleep(min(retry_seconds(dict(resp.headers), resp.body), limit))
                continue
            if _is_cooldown(resp):
                return resp
            delay = next(ladder)
            if (resp.status or 0) not in self.pacing.retry_statuses or delay is None:
                return resp
            delay = limited_delay(resp.status, resp.headers, resp.body, delay, cap=limit)
            if delay is None:
                return resp
            self._count("http_retries")
            self._sleep(delay)

    @staticmethod
    def _decode(resp: HttpResponse) -> tuple[int, Any]:
        if resp.status is None:
            return 0, None
        try:
            return resp.status, (json.loads(resp.body) if resp.body else None)
        except (ValueError, UnicodeDecodeError):
            return resp.status, None

    def fetch_json(self, method: str, url: str, *, params: Mapping[str, Any] | None = None,
                   headers: Mapping[str, str] | None = None,
                   body: bytes | None = None) -> tuple[int, Any]:
        """:meth:`fetch` decoded as :meth:`get_json`: ``(status, decoded JSON or None)``."""
        return self._decode(self.fetch(method, url, params=params, headers=headers, body=body))

    def get_json(self, url: str, params: Mapping[str, Any] | None = None, *,
                 headers: Mapping[str, str] | None = None) -> tuple[int, Any]:
        """v3's ``http_get_json`` seam: ``(status, decoded JSON or None)``, ``0`` on transport
        failure. One request (no retries: v3 collectors run their own ladders over this)."""
        return self._decode(self.get(url, params, headers=headers))

    def post_json(self, url: str, payload: Any, *,
                  headers: Mapping[str, str] | None = None) -> tuple[int, Any]:
        """v3's ``http_post`` seam (forums, AF GraphQL)."""
        return self._decode(self.post(url, payload, headers=headers))


# ---------------------------------------------------------------- coverage windows
def date_windows(start: dt.date, end: dt.date, days: int) -> list[tuple[dt.date, dt.date]]:
    """``[start, end]`` as consecutive ascending spans of ``days`` days, the last one ending at
    ``end`` (module docstring, "Date sub-windows"): ``[(start, start + days), (start + days, …),
    …, (…, end)]``. ``days <= 0`` or a span of at most ``days`` gives the one window ``[(start,
    end)]``; ``start > end`` is a :class:`CollectError`."""
    if start > end:
        raise CollectError(f"date window {start}..{end}: start after end")
    if days <= 0 or (end - start).days <= days:
        return [(start, end)]
    out: list[tuple[dt.date, dt.date]] = []
    s = start
    while (end - s).days > days:
        e = s + dt.timedelta(days=days)
        out.append((s, e))
        s = e
    out.append((s, end))
    return out


@dataclass
class Window:
    """One query window or lookup batch of this run (a ``coverage`` row on exit)."""

    channel: str
    query: str
    query_digest: str
    start: dt.datetime
    end: dt.datetime
    seen: int = 0
    kept: int = 0
    gate_digest: str | None = None
    outcome: tuple[str, str | None, bool, str | None, dt.datetime | None] | None = None
    capped: bool = False
    error: str | None = None

    def complete(self) -> None:
        """Every result of the window arrived: ``ok``, ``is_exhausted``; advances the watermark."""
        self.outcome = ("ok", None, True, None, None)

    def truncated(self, next_cursor: str | None = None, error_class: str | None = None, *,
                  cap: bool = True) -> None:
        """Stopped early on purpose (a cap, ``max_records``): ``partial``, not exhausted, never
        advances the watermark; ``next_cursor`` where the provider gives one. ``cap`` (default): a cap
        cut it, so the run ends ``scope_complete = false``, reason ``limit`` (D492); ``cap=False`` for a
        planned ``partial`` whose run reports its own shortfall (module docstring, "Caps")."""
        self.outcome = ("partial", error_class, False, next_cursor, None)
        self.capped = cap

    def fail(self, error_class: str, *, permanent: bool = False,
             retry_after: dt.datetime | None = None) -> None:
        """A page failed for good: ``failed_transient`` (``rate_limit``, ``timeout``,
        ``network``, ``http_5xx``, …) or ``failed_permanent`` (``query_invalid``, …)."""
        status = "failed_permanent" if permanent else "failed_transient"
        if error_class not in ERROR_CLASSES[status]:
            raise CollectError(f"{status}[{error_class}]: not in {sorted(ERROR_CLASSES[status])}")
        self.outcome = (status, error_class, False, None, retry_after)

    def close(self, exhausted: str, *, error_class: str = "network",
              next_cursor: str | None = None) -> None:
        """v3's receipt string → outcome: ``complete`` → :meth:`complete`, ``truncated`` →
        :meth:`truncated`, ``error`` → :meth:`fail` (``error_class``)."""
        if exhausted == "complete":
            self.complete()
        elif exhausted == "truncated":
            self.truncated(next_cursor)
        elif exhausted == "error":
            self.fail(error_class)
        else:
            raise CollectError(f"exhausted {exhausted!r} not in {EXHAUSTED}")


# ---------------------------------------------------------------- the harness
class Collector:
    """One collector run's harness (module docstring). ``channels`` replaces the
    ``configs/channels.yaml`` lookup (tests, a toy channel); ``now`` pins the clock."""

    def __init__(self, ctx: StageContext, *, channels: Mapping[str, ChannelDef] | None = None,
                 now: Callable[[], dt.datetime] | None = None,
                 monotonic: Callable[[], float] | None = None) -> None:
        phase, _, suffix = ctx.stage.partition(".")
        if phase != PHASE or not suffix:
            raise CollectError(f"{ctx.stage!r}: a collector stage is `collect.<suffix>` (D92)")
        self.ctx = ctx
        self.suffix = suffix
        self.sources_chain = f"sources.{suffix}"
        self.coverage_chain = f"coverage.{suffix}"
        self._channels = dict(channels) if channels is not None else None
        self._now = now or (lambda: utcnow())
        self.now = self._now()
        # Live (module docstring): the harness's counters are the run's, the heartbeat copies them.
        self.counts: dict[str, int] = ctx.counts
        self.counts.setdefault("http_requests", 0)
        self._monotonic = monotonic or time.monotonic
        self._beat_at: float | None = None
        self.halted: RunHalted | None = None
        self.held: dt.datetime | None = None
        self.sink = InbandSink(ctx.blobs, now=lambda: self.now)
        self.records = InbandSink(ctx.blobs, now=lambda: self.now)  # D522: its own tallies/breaker
        self._record_bytes = 0
        self._pinned = {fam: self._own_pin(fam) for fam in ("sources", "coverage")}
        self._sources_view = self._view("sources")
        self._coverage_view = self._view("coverage")
        self._by_ext: dict[tuple[str, str], Row] = {}
        for r in self._sources_view:
            ident = r["identity"]
            self._by_ext[(ident["channel"], ident["external_id"])] = r
        self._cov_prior: dict[tuple[str, str, dt.datetime, dt.datetime], Row] = {
            (r["channel"], r["query_digest"], r["window_start_at"], r["window_end_at"]): r
            for r in self._coverage_view}
        self._minted: dict[tuple[str, str], str] = {}
        self._rows: dict[str, Row] = {}
        self._cov_rows: dict[tuple[str, str, dt.datetime, dt.datetime], Row] = {}
        ctx.on_stopped = self.finish  # D570: a RunStopped the collector does not catch ends here

    # ------------------------------------------------------------ chains
    def _own_pin(self, fam: str) -> bool:
        own = f"{fam}.{self.suffix}"
        pinned = self.ctx.prior.get(fam) or self.ctx.inputs.get(fam)
        if pinned is not None:
            if pinned.chain_id != own:
                raise CollectError(f"{self.ctx.stage}: pinned {fam} chain {pinned.chain_id!r} is "
                                   f"not the collector's own {own!r} (C32)")
            return True
        return False  # a non-empty own directory not read: the runner's restart guard refuses (D377)

    def _view(self, fam: str) -> list[Row]:
        return self.ctx.view(fam).to_pylist() if self._pinned[fam] else []

    # ------------------------------------------------------------ config
    def channel(self, channel_id: str) -> ChannelDef:
        """The channel's config (``configs/channels.yaml``, or the ``channels=`` given)."""
        if self._channels is None:
            from stack.config import load

            self._channels = {c.id: c for c in load().channels.channels}
        try:
            return self._channels[channel_id]
        except KeyError:
            raise CollectError(f"channel {channel_id!r} not in channels.yaml "
                               f"({len(self._channels)} channels)") from None

    def option(self, name: str, default: T, *, env: str | None = None,
               cast: Callable[[Any], T] | None = None) -> T:
        """A stage option (D72: the plan line's ``options``); a set, non-empty ``env`` var wins
        (v3's overrides keep working). ``cast`` converts the chosen value (env values are
        strings). A cap/sampling knob that wins (``*_LIMIT``, ``*_FRACTION``, ``*_MAX_QUERIES``)
        is counted ``env_override.<VAR>`` and warned (D445)."""
        raw: Any = default
        if name in self.ctx.plan_stage.options:
            raw = self.ctx.plan_stage.options[name]
        if env is not None and os.environ.get(env, "").strip():
            raw = os.environ[env].strip()
            if is_cap_env(env):
                self._cap_env_override(env, name, raw)
        return cast(raw) if cast is not None and raw is not None else raw

    def _cap_env_override(self, env: str, name: str, value: str) -> None:
        """A cap/sampling env knob beat option ``name`` (D445): counted ``env_override.<VAR>`` = 1
        and one warning line per run and variable (the run's digest does not see it)."""
        key = f"env_override.{env}"
        if key in self.counts:
            return
        self.counts[key] = 1
        planned = self.ctx.plan_stage.options.get(name, "<default>")
        say(f"warning: {self.ctx.stage}: ${env}={value} overrides option {name!r} "
            f"(plan: {planned!r}); the run is capped/sampled by the environment")

    # ------------------------------------------------------------ identity (C32)
    def prior(self, channel: str, external_id: str) -> Row | None:
        """The latest row of ``(channel, external_id)`` in the own chain (before this run)."""
        return self._by_ext.get((channel, external_id))

    def source_id(self, channel: str, external_id: str) -> str:
        """The source's id: the own chain's, else this run's mint, else a new mint (counted
        ``minted_ids``)."""
        key = (channel, external_id)
        prior = self._by_ext.get(key)
        if prior is not None:
            return str(prior["source_id"])
        if key not in self._minted:
            self._minted[key] = ids.mint("source")
        return self._minted[key]

    # ------------------------------------------------------------ sources rows
    def observe(self, channel: str, external_id: str, record: Mapping[str, Any], *,
                keys: Iterable[tuple[str, str]] = (), canonical_url: str | None = None,
                metadata: Mapping[str, Any] | None = None, record_format: str = RECORD,
                skip_unchanged: bool = False,
                query: str | Mapping[str, Any] | None = None, query_id: str | None = None,
                label: str | None = None, provider_record: Any = None,
                record_media_type: str | None = None) -> Row | None:
        """One observed record → this run's ``sources`` row for the source (returned). ``keys``
        are ``(kind, value)`` pairs, typed by the caller (``scholarly_records``, ``stack.urls``);
        ``metadata`` holds A.5 ``metadata`` fields (``origin`` defaults to ``provider``,
        ``license_class`` to the channel's). ``skip_unchanged``: write nothing when the prior
        row is an observation with the same ``record_digest`` (counted ``sources_unchanged``).
        ``query``/``query_id``/``label``: the query that returned it (module docstring,
        "Attribution"); a source already observed this run keeps its row and gains the entry.
        ``provider_record`` (D522): the source's record of the item as received, minted as the
        row's ``provider_record`` in-band entry (:meth:`add_record`) when the row is written (not
        on a repeat, not when skipped as unchanged).
        The key adders of :mod:`stack.identity` add their keys from ``record`` after ``keys``."""
        ch = self.channel(channel)
        keys = list(keys)
        keys += identity.added_keys(channel, ch.channel_group, record, keys)
        match = _match(query, query_id, label)
        digest = record_digest(record, ch.record_projection)
        prior = self.prior(channel, external_id)
        sid = self.source_id(channel, external_id)
        if sid in self._rows:
            self._bump("sources_repeat")
            return _add_match(self._rows[sid], match)
        if skip_unchanged and prior is not None and prior["state"] == "observed" \
                and prior["record_digest"] == digest:
            self._bump("sources_unchanged")
            return None
        md = self._metadata(ch, metadata or {}, miss_count=0)
        row = self._row(sid, channel, external_id, state="observed", digest=digest,
                        record_format=record_format, status="ok", canonical_url=canonical_url,
                        keys=keys, metadata=md, prior=prior)
        self._rows[sid] = _add_match(row, match)
        if provider_record is not None:
            self.add_record(sid, provider_record, media_type=record_media_type)
        return row

    def attribute(self, channel: str, external_id: str, *, query: str | Mapping[str, Any],
                  query_id: str | None = None, label: str | None = None) -> bool:
        """Add a ``matched_by`` entry (module docstring, "Attribution") to the source's row of this
        run without observing it again; ``False`` (nothing recorded) when the run wrote no row
        for it (not observed, or skipped as unchanged)."""
        sid = self._by_ext.get((channel, external_id), {}).get("source_id") \
            or self._minted.get((channel, external_id))
        row = self._rows.get(str(sid)) if sid else None
        if row is None:
            return False
        _add_match(row, _match(query, query_id, label))
        return True

    def miss(self, channel: str, external_id: str, *, query: str | Mapping[str, Any] | None = None,
             keys: Iterable[tuple[str, str]] = (), query_id: str | None = None,
             label: str | None = None) -> Row:
        """A provider-side miss → a ``missing`` row (``retracted`` once ``miss_count`` reaches the
        plan line's ``miss_cap`` under ``miss_means: withdrawn``, D13/D22). ``miss_count``
        counts consecutive misses over the own chain; in-band entries are carried (text kept).
        ``query`` (+ ``query_id``, ``label``) is attributed as in :meth:`observe`."""
        ch = self.channel(channel)
        match = _match(query, query_id, label)
        prior = self.prior(channel, external_id)
        sid = self.source_id(channel, external_id)
        if sid in self._rows:
            self._bump("sources_repeat")
            return _add_match(self._rows[sid], match)
        before = prior["metadata"]["miss_count"] if prior is not None and prior["state"] != "observed" else 0
        count = int(before or 0) + 1
        state = "missing"
        if ch.miss_means == "withdrawn":
            cap = self.ctx.plan_stage.miss_cap
            if cap is None:
                raise CollectError(f"channel {channel!r} has miss_means: withdrawn but stage line "
                                   f"{self.ctx.stage!r} sets no miss_cap (D22)")
            if count >= cap:
                state = "retracted"
        md = self._metadata(ch, {}, miss_count=count)
        row = self._row(sid, channel, external_id, state=state, digest=MISS_DIGEST,
                        record_format=MISS, status="empty", canonical_url=None, keys=keys,
                        metadata=md, prior=prior)
        self._rows[sid] = _add_match(row, match)
        return row

    def _metadata(self, ch: ChannelDef, given: Mapping[str, Any], *, miss_count: int) -> Row:
        bad = sorted(set(given) - set(_METADATA_FIELDS))
        if bad:
            raise CollectError(f"metadata fields {bad} not in sources.metadata {_METADATA_FIELDS}")
        owned = sorted(set(given) & {"query_digest", "matched_by"})
        if owned:
            raise CollectError(f"metadata {owned}: pass the query to observe()/miss() (query=, "
                               f"query_id=, label=), not the field")
        md: Row = dict.fromkeys(_METADATA_FIELDS)
        md.update(sensitivity=[], is_withdrawn=False, origin="provider",
                  license_class=ch.license_class)
        md.update(given)
        md.update(miss_count=miss_count)
        if md["origin"] not in SOURCE_ORIGINS:
            raise CollectError(f"metadata.origin {md['origin']!r} not in {SOURCE_ORIGINS}")
        md["authors"] = _authors(md["authors"])
        for k in ("published_at", "provider_updated_at"):
            md[k] = utc(md[k])
        return md

    def _row(self, sid: str, channel: str, external_id: str, *, state: str, digest: str,
             record_format: str, status: str, canonical_url: str | None,
             keys: Iterable[tuple[str, str]], metadata: Row, prior: Row | None) -> Row:
        carried = {e["content_role"]: e for e in (prior["inband"]["entries"] if prior else [])}
        return {
            "source_id": sid, "state": state, "record_digest": digest,
            "record_format": record_format, "observed_at": self.now,
            "status": status, "run_id": self.ctx.run_id, "error_class": None, "error": None,
            "identity": {"source_id": sid, "channel": channel, "external_id": external_id,
                         "canonical_url": canonical_url,
                         "keys": [{"kind": k, "value": v} for k, v in keys]},
            "metadata": metadata,
            "inband": {"entries": list(carried.values()), "run_id": self.ctx.run_id,
                       "updated_at": self.now},
            "_carried": carried,
            "_known": prior is not None,
        }

    # ------------------------------------------------------------ in-band (§5.4)
    def add_inband(self, source_id: str, content_role: str, *, text: str | None = None,
                   raw: bytes | None = None, media_type: str, text_media_type: str | None = None,
                   captured_at: str | dt.datetime | None = None,
                   backfill: bool | None = None) -> Entry | None:
        """Offer an in-band body for a source observed this run; on a mint (or an unchanged
        carried entry) the row's ``inband.entries[]`` holds it for the role. ``text_media_type``: the text's
        media type when both ``raw`` and ``text`` are given (D690). ``backfill``
        defaults to "the source existed before this run". Returns the entry or ``None``."""
        row = self._rows.get(source_id)
        if row is None:
            raise CollectError(f"add_inband: {source_id} was not observed this run")
        entries: dict[str, Entry] = {e["content_role"]: e for e in row["inband"]["entries"]}
        entry = self.sink.offer(source_id, content_role, text=text, raw=raw, media_type=media_type,
                                text_media_type=text_media_type, carried=entries, captured_at=captured_at,
                                backfill=row["_known"] if backfill is None else backfill)
        if entry is not None:
            entries[content_role] = entry
            row["inband"]["entries"] = list(entries.values())
        return entry

    def add_record(self, source_id: str, record: Any, *,
                   media_type: str | None = None) -> Entry | None:
        """The source's ``provider_record`` in-band entry (D522; :mod:`stack.collect.inband`,
        "Provider records") for a source observed this run. ``record``: a JSON item (canonical
        :func:`~stack.collect.inband.record_bytes`, media type ``application/json``) or the bytes
        as received (``media_type`` required). The mint rule holds (an unchanged record keeps its
        entry and first ``captured_at``); a source already recorded this run keeps its first record.
        Counted ``record_minted``/``record_skipped``/``record_errors`` and ``record_bytes`` (the
        offered size, minted or not)."""
        row = self._rows.get(source_id)
        if row is None:
            raise CollectError(f"add_record: {source_id} was not observed this run")
        if isinstance(record, bytes | bytearray):
            if media_type is None:
                raise CollectError("add_record: raw record bytes need their media_type")
            raw = bytes(record)
        else:
            raw, media_type = record_bytes(record), media_type or JSON_MEDIA_TYPE
        entries: dict[str, Entry] = {e["content_role"]: e for e in row["inband"]["entries"]}
        prior = entries.get(RECORD_ROLE)
        if prior is not None and row.get("_recorded"):
            return prior  # the first observation of the run stands (``observe``: repeats)
        self._record_bytes += len(raw)
        entry = self.records.offer(source_id, RECORD_ROLE, raw=raw, media_type=media_type,
                                   carried=entries, backfill=False)
        if entry is not None:
            entries[RECORD_ROLE] = entry
            row["inband"]["entries"] = list(entries.values())
            row["_recorded"] = True
        return entry

    @property
    def record_counts(self) -> dict[str, int]:
        """The ``record_*`` run counts (D522, D523)."""
        r = self.records
        return {"record_minted": r.minted, "record_skipped": r.skipped, "record_errors": r.errors,
                "record_disabled": int(r.disabled), "record_bytes": self._record_bytes}

    # ------------------------------------------------------------ coverage
    def watermark(self, channel: str, query: str | Mapping[str, Any]) -> dt.datetime | None:
        """Newest ``window_end_at`` among the own chain's ``ok`` rows for ``(channel,
        query_digest)``, or ``None``. Only ``ok`` rows advance it (this run's rows do not count)."""
        qd = query_digest(query)
        ends = [r["window_end_at"] for r in self._coverage_view
                if r["channel"] == channel and r["query_digest"] == qd and r["status"] == "ok"]
        return max(ends) if ends else None

    def holes(self, channel: str, query: str | Mapping[str, Any],
              floor: str | dt.datetime | dt.date) -> list[tuple[dt.datetime, dt.datetime]]:
        """The spans of ``[floor, watermark]`` that no ``ok`` window of ``(channel,
        query_digest)`` covers, ascending (module docstring, "Floor moved back"); ``[]`` without
        a watermark or when the ``ok`` windows reach back to the floor without a gap. Windows
        that touch (one ends where the next starts) are contiguous. Only the own chain's prior
        rows count, as for :meth:`watermark`."""
        qd = query_digest(query)
        spans = sorted((r["window_start_at"], r["window_end_at"]) for r in self._coverage_view
                       if r["channel"] == channel and r["query_digest"] == qd
                       and r["status"] == "ok" and r["window_end_at"] > r["window_start_at"])
        pos = utc(floor)
        if not spans or pos is None:
            return []
        out: list[tuple[dt.datetime, dt.datetime]] = []
        for start, end in spans:
            if end <= pos:
                continue
            if start > pos:
                out.append((pos, start))
            pos = end
        return out

    def last_attempt(self, channel: str, query: str | Mapping[str, Any]) -> Row | None:
        """The own chain's newest charged coverage row for ``(channel, query_digest)`` (max
        ``last_attempt_at``; an uncharged halt row has none of its own), or ``None``."""
        qd = query_digest(query)
        rows = [r for r in self._coverage_view if r["channel"] == channel
                and r["query_digest"] == qd and r["last_attempt_at"] is not None]
        return max(rows, key=lambda r: r["last_attempt_at"]) if rows else None

    def held_until(self, channel: str) -> dt.datetime | None:
        """The latest provider ``retry_after`` still in the future among the own chain's
        ``failed_transient`` rows of ``channel`` (a run-level 429 wrote it), or ``None``."""
        held = [r["retry_after"] for r in self._coverage_view
                if r["channel"] == channel and r["status"] == "failed_transient"
                and r["retry_after"] is not None and r["retry_after"] > self.now]
        return max(held) if held else None

    def relist_after(self, default_h: float = RELIST_AFTER_H) -> dt.timedelta:
        """The plan line's ``relist_after_h`` option (hours, ``0`` = always due) as a timedelta."""
        hours = self.option("relist_after_h", default_h, cast=float)
        if hours < 0:
            raise CollectError(f"{self.ctx.stage}: relist_after_h {hours} < 0")
        return dt.timedelta(hours=hours)

    def hold(self, channel: str, n: int = 1) -> bool:
        """``True`` (and ``n`` units counted ``held_retry_after``) when ``channel`` is held by a
        provider ``retry_after`` (:meth:`held_until`); the run then ends ``scope_complete =
        false``."""
        until = self.held_until(channel)
        if until is None:
            return False
        self.held = max(self.held or until, until)
        self._bump("held_retry_after", n)
        return True

    def due(self, channel: str, query: str | Mapping[str, Any], *,
            until: str | dt.datetime | dt.date | None = None,
            relist_after: dt.timedelta | None = None, partial_due: bool = True,
            floor: str | dt.datetime | dt.date | None = None) -> bool:
        """Whether ``(channel, query)`` needs asking this run (module docstring, "Passes");
        a unit that is not due is counted under its reason (:data:`SKIP_COUNTS`).

        ``until``: the end this run would ask up to (window collectors; the provider's
        granularity, a day for most); covered when the ``ok`` watermark reaches it and, with
        ``floor``, no :meth:`holes` lie between the floor and the watermark.
        ``relist_after``: a unit whose newest attempt is younger is not due when that attempt
        was ``ok`` and no ``until`` is given (a listing snapshot), or ``partial`` and not
        ``partial_due`` (a cap that truncates the same way when asked again; also counted
        ``capped_not_due``, which leaves the run ``scope_complete = false``, D492)."""
        if self.hold(channel):
            return False
        if until is not None:
            mark = self.watermark(channel, query)
            end = utc(until)
            if mark is not None and end is not None and mark >= end and (
                    floor is None or not self.holes(channel, query, floor)):
                self._bump("already_covered")
                return False
        if relist_after is not None:
            last = self.last_attempt(channel, query)
            if last is not None and self.now - last["last_attempt_at"] < relist_after and (
                    (last["status"] == "ok" and until is None)
                    or (last["status"] == "partial" and not partial_due)):
                self._bump("not_due")
                if last["status"] == "partial":  # D492: the cap still stands; the scope is not complete
                    self._bump("capped_not_due")
                return False
        return True

    @contextmanager
    def window(self, channel: str, query: str | Mapping[str, Any],
               start: str | dt.datetime | dt.date, end: str | dt.datetime | dt.date, *,
               gate_digest: str | None = None) -> Iterator[Window]:
        """One coverage window (module docstring). Writes its row on exit; a :class:`RunHalted`
        inside writes ``failed_transient[rate_limit]`` uncharged and re-raises; a :class:`RunStopped`
        (a drain, also raised here on entry) writes no row and re-raises; any other
        exception writes nothing and propagates (the run fails)."""
        if self.halted is None and self.stopping():
            raise self.stop()
        if isinstance(self.halted, RunStopped):
            raise self.halted
        self.channel(channel)
        s, e = utc(start), utc(end)
        if s is None or e is None:
            raise CollectError("a coverage window needs start and end")
        w = Window(channel, query_text(query), query_digest(query), s, e, gate_digest=gate_digest)
        key = (channel, w.query_digest, s, e)
        if key in self._cov_rows:
            raise CollectError(f"coverage window {key} written twice in one run")
        try:
            yield w
        except RunStopped:
            raise  # abandoned as not attempted: no row, no charge (D570)
        except RunRefused as r:
            w.fail(r.error_class, permanent=r.permanent)
            w.error = r.reason
            assert w.outcome is not None
            self._cov_rows[key] = self._coverage_row(w, key, charge=r.permanent)
            self._bump(f"windows_{w.outcome[0]}")
            raise
        except RunHalted as h:
            w.outcome = ("failed_transient", "rate_limit", False, None,
                         self.now + dt.timedelta(seconds=h.retry_after_s))
            self._cov_rows[key] = self._coverage_row(w, key, charge=False)
            raise
        if w.outcome is None:
            raise CollectError(f"coverage window {channel}/{w.query!r} closed without an outcome "
                               f"(call complete(), truncated() or fail())")
        self._cov_rows[key] = self._coverage_row(w, key, charge=True)
        self._bump(f"windows_{w.outcome[0]}")
        if w.outcome[0] == "partial" and w.capped:
            self._bump("windows_capped")  # D492
        self.beat(force=True)

    def _coverage_row(self, w: Window, key: tuple[str, str, dt.datetime, dt.datetime], *,
                      charge: bool) -> Row:
        assert w.outcome is not None
        status, error_class, exhausted, cursor, retry_after = w.outcome
        prior = self._cov_prior.get(key)
        attempts = int(prior["attempts"]) if prior is not None else 0
        first = prior["first_attempt_at"] if prior is not None else None
        if charge:
            attempts += 1
            first = first or self.now
        return {"channel": w.channel, "query_digest": w.query_digest, "window_start_at": w.start,
                "window_end_at": w.end, "query": w.query, "seen": w.seen, "kept": w.kept,
                "is_exhausted": exhausted, "next_cursor": cursor, "gate_digest": w.gate_digest,
                "status": status, "run_id": self.ctx.run_id, "error_class": error_class,
                "error": w.error, "attempts": attempts, "first_attempt_at": first,
                "last_attempt_at": self.now if charge else (prior or {}).get("last_attempt_at"),
                "retry_after": retry_after}

    # ------------------------------------------------------------ transport, halt
    def transport(self, pacing: Pacing, *, send: Sender | None = None,
                  sleep: Callable[[float], None] | None = None,
                  monotonic: Callable[[], float] | None = None) -> Transport:
        """A :class:`Transport` at the provider's ``pacing`` (module docstring)."""
        return Transport(self, pacing, send=send, sleep=sleep, monotonic=monotonic)

    def halt(self, retry_after_s: float, reason: str) -> RunHalted:
        """Halt the run (no further request); returns the :class:`RunHalted` to raise. A
        collector with its own 429 classification (v3 OpenAlex's budget test) calls this."""
        if self.halted is None:
            self.halted = RunHalted(retry_after_s, reason)
            self._bump("halted")
        return self.halted

    def stopping(self) -> bool:
        """The process drains (module docstring, Drain); ``False`` under a context without one."""
        drain = getattr(self.ctx, "drain", None)
        return drain is not None and drain.stopping()

    def stop(self) -> RunStopped:
        """Stop the run for a drain (no further window or request); returns the
        :class:`RunStopped` to raise. A run halted already keeps its halt."""
        if self.halted is None:
            self.halted = RunStopped()
        return self.halted if isinstance(self.halted, RunStopped) else RunStopped()

    @contextmanager
    def halts(self) -> Iterator[None]:
        """Swallow :class:`RunHalted` (the run continues to :meth:`finish`, scope incomplete), a
        :class:`RunStopped` too. A :class:`RunRefused` is kept as the run's halt (its class and
        reason reach the run record) and said on stderr."""
        try:
            yield
        except RunStopped:
            self.stop()
        except RunRefused as r:
            if self.halted is None:
                self.halted = r
                self._bump("halted")
                self._bump(f"refused.{r.error_class}")
                say(f"{self.ctx.stage} run {self.ctx.run_id}: halted, the provider refused "
                    f"({r.error_class}): {r.reason}")
        except RunHalted as h:
            self.halt(h.retry_after_s, h.reason)

    # ------------------------------------------------------------ progress
    def progress(self, done: int | None = None, total: int | None = None) -> None:
        """Report query progress: ``queries_done`` / ``queries_total`` in the live counts (either
        may be omitted), then a throttled heartbeat (:meth:`beat`). Under a drain, before the last
        unit (``done < total``), raises :class:`RunStopped` (a unit boundary: module docstring, Drain)."""
        if done is not None and total is not None and done < total and self.stopping():
            raise self.stop()
        if done is not None:
            self.counts["queries_done"] = int(done)
        if total is not None:
            self.counts["queries_total"] = int(total)
        self.beat()

    def beat(self, *, force: bool = False) -> None:
        """Refresh ``rows_so_far`` and the in-band counts, then ``ctx.heartbeat()`` (the
        provisional record shows the live counts); at most every :data:`HEARTBEAT_S` seconds
        unless ``force``."""
        t = self._monotonic()
        if not force and self._beat_at is not None and t - self._beat_at < HEARTBEAT_S:
            return
        self._beat_at = t
        self.counts.update(self.sink.counts)
        self.counts.update(self.record_counts)
        self.counts["rows_so_far"] = len(self._rows)
        self.ctx.heartbeat()

    # ------------------------------------------------------------ finish
    def _bump(self, name: str, n: int = 1) -> None:
        self.counts[name] = self.counts.get(name, 0) + n

    def rows(self) -> list[Row]:
        """This run's ``sources`` rows (without the harness's private keys)."""
        return [{k: v for k, v in r.items() if not k.startswith("_")} for r in self._rows.values()]

    def coverage_rows(self) -> list[Row]:
        return list(self._cov_rows.values())

    def finish(self, scope_complete: bool = True) -> StageResult:
        """Emit the run's ``sources`` and ``coverage`` rows (one file each on the own chain; a
        family with no rows writes none), fill ``ctx.counts``; ``scope_complete`` is false
        after a halt, when a provider ``retry_after`` held a unit (:meth:`hold`) and when any
        window of this run ended ``failed_transient`` (R3-2: the next run or pass retries it, so
        the build says it is not done), and when a cap stopped a window short or kept a capped unit
        from being asked (``windows_capped``, ``capped_not_due``; D492, reason ``limit``). ``failed_permanent`` windows keep the scope complete
        (the next run asks them again, :meth:`due`, and fails the same way until the query or the
        channel is fixed); they are counted per error class
        (``windows_failed_permanent.<error_class>``) and the build warns about them."""
        for fam, rows, pinned, cid in (("sources", self.rows(), self._pinned["sources"],
                                        self.sources_chain),
                                       ("coverage", self.coverage_rows(), self._pinned["coverage"],
                                        self.coverage_chain)):
            if not rows:
                continue
            table = pa.Table.from_pylist(rows, schema=family_spec(fam).schema)
            if pinned:
                self.ctx.emit_part(fam, table, "*", chain_id=cid)
            else:
                self.ctx.emit_full(fam, table, "*", chain_id=cid)
        perm: dict[str, int] = {}
        transient = False
        for r in self._cov_rows.values():
            if r["status"] == "failed_permanent":
                key = f"windows_failed_permanent.{r['error_class']}"
                perm[key] = perm.get(key, 0) + 1
            transient = transient or r["status"] == "failed_transient"
        counts = {**self.counts, **self.sink.counts, **self.record_counts, **perm,
                  "rows_so_far": len(self._rows),
                  "sources_written": len(self._rows),
                  "minted_ids": len(self._minted), "coverage_written": len(self._cov_rows)}
        self.ctx.counts.update(counts)
        capped = int(self.counts.get("windows_capped", 0)) + int(self.counts.get("capped_not_due", 0)) > 0
        refused = self.halted if isinstance(self.halted, RunRefused) else None
        return StageResult(scope_complete=scope_complete and self.halted is None
                           and self.held is None and not transient and not capped, counts=counts,
                           error=None if refused is None else f"{refused.error_class}: {refused.reason}")


__all__ = ["EXHAUSTED", "HEARTBEAT_S", "MISS_DIGEST", "RELIST_AFTER_H", "RELIST_OPTIONS", "SKIP_COUNTS", "CollectError",
           "Collector", "Pacing", "RunHalted", "RunRefused", "RunStopped", "Sender",
           "Transport", "Window", "date_windows", "full_url", "query_digest", "query_text", "record_digest",
           "urllib_send", "utc", "utcnow"]
