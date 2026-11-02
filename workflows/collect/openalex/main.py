"""``collect.openalex`` — the OpenAlex concept-group sweep + institution-id sweep (§5.6 OpenAlex
row; v1 ``search_openalex.py`` + ``search_openalex_using_institution_ids.py`` + the OpenAlex half
of ``lib/search_engine.py``).

The broad academic-search axis, in TWO query families over one loop (three with the option
``author_ids``):

* **Concept groups** — one ``title_and_abstract.search`` query per non-misuse concept group in
  channels.yaml (the group's synonym terms OR'd), query label ``group:<id>``. A
  ``needs_ai_context`` group is sent as a boolean phrase query ``("t1" OR "t2") AND ("<ai_context
  phrase>" OR …)`` (:func:`phrase_query`): live probe 2026-10-12 (count-only), "decision theory"
  19,194 works = 617 ANDed with the five ``ai_context`` phrases + 18,577 ``NOT`` them, so the
  backend honours the intersection; with an empty ``ai_context`` such a group is skipped.
* **Institution ids** — one ``institutions.id:<Iid>`` query per configured institution
  (channels.yaml ``institutions:``, v1 ``orgs.yaml``), query label ``inst:<Iid>``. NO keyword
  filter — the point is recall of org-affiliated work that matches no safety keyword and no
  rostered author (see ``institution_filter``).

* **Author ids** (option ``author_ids``, default off; C4, round 5 (RUN) B7b) — the roster's
  resolved OpenAlex author ids (``configs/rosters/researchers.yaml`` ``openalex_ids``, written by
  ``tools/resolve_openalex_authors.py``), batched into :data:`AUTHOR_BUCKETS` fixed hash buckets
  (option ``author_buckets``), one ``authorships.author.id:A1|A2|…`` query per non-empty bucket,
  query label ``authors:bNN``. A bucket is chosen by the id's hash, so a newly resolved id changes
  one bucket's key (a new key sweeps from the floor) and leaves the others' watermarks. A list call
  ($0.0001 a page), no keyword filter: a roster author's work is in scope by author. A bucket over
  :data:`MAX_IDS_PER_FILTER` ids (OpenAlex's OR limit) fails the run before any request.

**Backfill cuts** (channels.yaml ``keyword_cuts`` on the channel; Tomáš 2026-10-13, docs/design/collection-yield.md
§10). A concept-group hit is dropped, before it is observed, when :func:`stack.rules.keyword_cut` names a cut:
``publisher`` (a publisher DOI, or no DOI, and no arXiv location: the key, an arXiv URL among ``locations`` or the
arXiv repository's source id, :func:`work_arxiv_located`), ``self_publication`` (Zenodo and preprint-server DOIs;
SSRN kept), ``ai_term`` (a group in ``ai_term_groups`` whose hit has no AI term in title or abstract). The
institution and author-id families are never cut, so a work they also return in the run is observed under them.
Counted per run ``cut_<name>`` and ``cut_<name>:<group id>``; a dropped hit is not in the window's ``seen``. The
group's cut set is part of its coverage key (``"cuts"``), so switching a cut off starts a new key that re-sweeps
from the floor instead of inheriting a watermark earned with the cut on.

**The venue allowlist** (channels.yaml ``keyword_cuts.keep_venues``): a hit with a DOI of
:data:`stack.rules.VENUE_DOI_PREFIXES` (ACL Anthology, AAAI) is not a ``publisher`` work (source class ``venue``);
``ai_term`` still applies. Counted ``venue_kept`` (+ ``:<group id>``). The key carries ``"venues"``
(:func:`stack.rules.venue_allowlist_digest`), so switching it on re-sweeps every cut group from the floor once.

**The pre-2020 exceptions** (D654; channels.yaml ``keyword_cuts``). (a) ``publisher_cut_floor``: a
concept-group hit published before it (``publication_date``, else ``publication_year``) whose title or abstract
carries a pre-2020 safety stem (:func:`stack.rules.publisher_cut_exempt`) bypasses the source cuts, no-DOI works
included; ``ai_term`` still applies. Counted ``publisher_cut_bypassed`` (+ ``:<group id>``). The key carries
``"bypass": {floor, stems}`` (:func:`stack.rules.pre2020_stem_digest`). (b) ``field_filter_floor`` with
``field_filter_groups`` (broad groups): such a group is swept as every broad group and once more as a second plan
entry with the same label and terms, without the CS-field filter and bounded by ``to`` = the day before the floor
(:func:`plan_groups`). Its filter differs, so it is its own coverage key with its own windows, holes and watermark;
the loop treats it as any entry with a ``to`` (a bounded window, no look-back: :func:`entry_window`). The first
entry's hits before the floor are a subset of the second's (a few pages asked twice, once).

All three are filtered to scholarly work types and the publication floor. Every hit is one ``sources``
row on the chain ``sources.openalex`` (channel ``openalex``; external id = the normalized DOI,
else the OpenAlex work id ``W…``, §5.6), written only when the record's stable projection moved
(``skip_unchanged``; channels.yaml ``record_projection`` leaves the counter ``cited_by_count``
out, C23). ``metadata`` carries title / rebuilt abstract / authors (ORCID and the affiliation
line per author: the institutions OpenAlex resolved, else its raw affiliation strings) /
``published_at`` / ``doc_type``; ``license_class`` is the channel's, per member. The
``abstract_inverted_index`` each work carries is de-inverted and minted as an in-band
``abstract`` entry (§5.4) through the harness. Every executed query writes ONE ``coverage`` row
on ``coverage.openalex``. No content fetch, no LLM. I/O is the collector harness
(:mod:`stack.collect.base`); this module keeps v3's provider logic.

**The arXiv identity bridge** now lives in the keys (D103): a work's keys are typed by
:func:`stack.scholarly_records.keys_for` from its OpenAlex id, DOI and PMID, and an arXiv DataCite
DOI (``10.48550/arXiv.<id>``) also yields the ``arxiv`` key, so gather merges the work with the
``collect.arxiv_sweep`` source inside the paper group. v3 re-registered such works under channel
``arxiv``; on the harness that would mint a second ``source_id`` for an ``(arxiv, id)`` pair the
arXiv chain already owns (C32 looks up the own chain only), apply the arXiv channel's projection
to an OpenAlex record and mis-rank it in gather's provider order — so the row stays ``openalex``.

**Type filter (do NOT port v1 verbatim).** v1 filtered ``type:article``. OpenAlex has since
retyped arXiv works as ``preprint``: on a live probe of one concept group, ``type:article``
returned 1,536 works where ``article|preprint`` returned 5,210 — a verbatim port would silently
drop ~70% of hits, every arXiv preprint among them. The filter is ``ARTICLE_TYPES`` below. It also keeps ``conference-paper``
(D653): OpenAlex's proceedings type, which the list lacked until 2026-10 (every coverage key changed
with it, so every query re-swept from the floor once).

**Incrementality runs on the publication date with a look-back (D582).** OpenAlex indexes
retroactively (a work can be added months after its publication date), so a resumed query cannot
just ask from its watermark. Until 2026-10 it added ``from_created_date`` = the watermark; OpenAlex
now refuses ``from_created_date`` and ``from_updated_date`` below a Premium, Institutional or
Partner plan (a 429 "Plan upgrade required", probed 2026-10-15; our keyed plan has neither). A
resumed open-ended query therefore asks ``from_publication_date`` = ``max(floor, watermark −
lookback_days)`` (option, default :data:`DEFAULT_LOOKBACK_DAYS` = 60) and writes its window from
the watermark, so coverage stays contiguous (as arXiv's ``announce_lag_days``, D405, and S2's
``lookback_days``, D551). A re-seen work whose projected record did not change writes no row
(``skip_unchanged``), so the overlap is idempotent. When the cap fills inside the look-back (cursor
order is not date order), the hits are kept and the query is asked again from the watermark in
the same window (counted ``lookback_capped``), so the look-back cannot keep a query capped.
**Known gap:** a work OpenAlex adds more than ``lookback_days`` after its publication date is
missed by every later run; in the 2026-10-14 sweep about 11% of the works published since
2026-01-01 had a ``created_date`` more than 60 days after their ``publication_date``. Only a full
re-sweep catches them: ``full: true`` (or ``STACK_OPENALEX_FULL=1``) on one run asks every query
from the floor (the 2026-10-14 sweep from 2025-10-01: 845 pages, 39.7 min, $0.73); a ``from``
moved back asks only the hole below the old floor (D272), not the covered range.
A bounded window (``to`` / ``window_end_days_ago``) covers ``[floor, to]`` and has no look-back.

**The coverage query** is the JCS of ``{"query": <label>, "filter": <the filter, the floor
written as KEY_FLOOR>}`` (unchanged by D582: the created axis was never in it, so the
look-back keeps every key and watermark): the watermark is per ``query_digest``, so
a changed term list or end starts a new key instead of inheriting a watermark the old query earned
(a carried watermark would be a false completeness claim for the new terms' back catalogue). The
floor is not in the key (round 5 (RUN) B7a): a floor moved back (2025-10-01 → 2024-11-30) leaves
a hole below the covered range (:meth:`stack.collect.base.Collector.holes`), asked first as a
bounded window (``from_publication_date`` the hole's start, ``to_publication_date`` the day before
its end, no look-back) and written ``[hole start, hole end]``; then the usual tail. Only the gap
is pulled again. A tail pass that is not complete (cap ``partial``, failed page ``failed_*``, halt)
is written over ``[floor, floor]`` (a hole's over ``[start, end]``) so its ``attempts``
accumulate on one key; cursor order is not date order, so a capped pull covered an unknown subset
and claims nothing. While a hole is not filled, the tail claims only from the hole's end.

**Run-level 429** (§5.6; D105a): OpenAlex meters requests (a search $0.001, a list $0.0001; a
free daily budget of $0.10 keyless, $1 keyed; :mod:`stack.collect.provider_keys`) and answers a spent budget with a 429 whose ``retryAfter`` is hours. The transport halts
the run on a 429 whose retry-after exceeds ``BUDGET_RETRY_AFTER_S`` (and :func:`_get_page` does
the same on a decoded body, for the seam): the in-flight query's window is
``failed_transient[rate_limit]`` with ``retry_after``, no attempt charged, no further request,
the rows already observed are emitted, the run ends ``ok`` with ``scope_complete = false``. A
short 429 (a burst) keeps v3's 2/8/20 s ladder; each step's re-send first waits out the Gate
cooldown that 429 set (the transport, D401). A Gate cooldown answer the transport returns (its
three waits spent) fails the page at once, not a ladder step (D588: a step would only meet our own
cooldown again).

**A refusal halts (D583).** An answer whose body's ``error`` says "Plan upgrade required" (a filter
the account's plan does not include; OpenAlex sends it as a 429 without ``Retry-After``) is not a
rate limit: :func:`_get_page` raises :class:`stack.collect.base.RunRefused` at once (no ladder; the
provider Gate sets no cooldown for it, ``stack.provider_transport.plan_refusal``), the in-flight
window is written ``failed_permanent[provider_plan]`` with the provider's message in its ``error``,
no further request is sent, and the run ends ``ok``, ``scope_complete = false`` (``halted``,
``refused.provider_plan``) with the message in the run record's ``error``. A 401 or 403 halts the
same way as ``failed_transient[provider_auth]`` (uncharged: the key, not the query). Before, each
such 429 ran the ladder against the 60 s Gate cooldown, about 4 min per query, and failed every
query ``failed_transient[rate_limit]``.

**The key and the spend caps** (:mod:`stack.collect.provider_keys`): with ``OPENALEX_API_KEY``
(environment or ``$STACK_ENV_FILE``) every request goes through an :class:`~stack.collect.
provider_keys.OpenAlexMeter`: the key as ``Authorization: Bearer`` (never in a URL), each request
reserved in the OpenAlex ledger and reconciled from the answer's cost headers, the run capped by
the options ``max_requests``/``max_usd`` (default $0.50 a keyed run). A request past a cap is not
sent: the in-flight query gets no coverage row (its pages so far are dropped), the queries not
reached get none either, and the run ends ``ok`` with ``scope_complete = false``
(``openalex_budget_stop``). Without the key the sweep runs keyless (free, unmetered) and says so
(``openalex_keyless``, one stderr line).

**Failed-only retry** (``retry_failed_run`` / ``STACK_OPENALEX_RETRY_FAILED_RUN``): the queries
whose coverage row from that run in ``coverage.openalex`` is ``failed_*`` are re-run (all of
them; fraction and query caps ignored) with the current configuration and normal watermarks.
A run id with no coverage rows on the chain, or a failed query no longer configured, fails closed.

Options (plan stage line ``options:``, D72; the v3 env var wins when set):
  * ``max_records`` / ``STACK_COLLECT_LIMIT``       — per-query result cap (default: the channel's
                                                      ``breadth`` preset ``max_results_per_query``)
  * ``max_queries`` / ``STACK_COLLECT_MAX_QUERIES`` — cap on queries swept (0 = all)
  * ``fraction``    / ``STACK_COLLECT_FRACTION``    — stable hash-selected query fraction (0, 1]
  * ``queries``                                     — only these labels (``group:<id>``,
                                                      ``inst:<Iid>``), before sampling
  * ``from``        / ``STACK_OPENALEX_FROM``       — publication floor (default: ``window_days``
                                                      before the end, else the channel's ``from_date``)
  * ``window_days``                                 — floor = end − N days when ``from`` is unset
  * ``to``, ``window_end_days_ago``                 — bounded window end (a date; today − N days)
  * ``full``        / ``STACK_OPENALEX_FULL``       — '1' ignores watermarks (re-sweep from the floor;
                                                      catches works indexed past the look-back)
  * ``lookback_days``                               — days below the watermark a resumed open-ended
                                                      query re-asks (D582; default 60, 0 = none)
  * ``retry_failed_run`` / ``STACK_OPENALEX_RETRY_FAILED_RUN`` — see above
  * ``relist_after_h``                              — passes (D226, :meth:`stack.collect.base.
                                                      Collector.due`): a cap-``partial`` query is
                                                      not asked again within this many hours (12)
  * ``max_requests``, ``max_usd``                   — keyed runs only: the run's request and USD
                                                      caps (above; ``max_usd`` default 0.50)
  * ``select``                                      — an OpenAlex field list (D525; default none:
                                                      full works, each the row's ``provider_record``,
                                                      D522); must include :data:`SELECT_FIELDS`
  * ``author_ids``                                  — add the author-id family (default false)
  * ``author_buckets``                              — its bucket count (default 64)
  * ``roster`` / ``STACK_ARXIV_ROSTER``             — the roster it reads (default
                                                      ``configs/rosters/researchers.yaml``)
  * ``STACK_MAILTO`` (env only)                     — contact for OpenAlex's polite pool
  * ``OPENALEX_API_KEY`` (env or ``STACK_ENV_FILE``) — the key (above); v3's ``OPENALEXAPIKEY`` is
                                                      not read.

Not carried from v3's ``collect.meta`` payload (``sources.metadata`` has no slot; the full work is
the row's ``provider_record``, D522): ``venue``, ``venue_issn_l``, ``citations``, ``oa_pdf`` (the OA PDF URL;
acquire resolves open access itself), v3 coverage ``topic_keys`` (derivable from the label and
channels.yaml).

Cardinal rule: imports only ``stack.*`` and the stdlib; nothing from other workflows.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from stack import rules
from stack.collect import roster as roster_mod
from stack.collect.base import (
    RELIST_OPTIONS,
    CollectError,
    Collector,
    Pacing,
    RunHalted,
    RunRefused,
    RunStopped,
    Sender,
    Transport,
    full_url,
    urllib_send,
)
from stack.collect.provider_keys import (
    METER_OPTIONS,
    SELECT_OPTION,
    OpenAlexBudgetStop,
    OpenAlexMeter,
    openalex_key,
    openalex_select,
)
from stack.collect.sampling import (
    ENV_FRACTION,
    ENV_MAX_QUERIES,
    SAMPLING_OPTIONS,
    select,
)
from stack.config import StackConfig
from stack.identity import added_keys
from stack.provider_transport import COOLDOWN_MESSAGE, plan_refusal
from stack.scholarly_records import keys_for, normalize_doi, normalize_openalex_id
from stack.stage_runner import StageContext, StageResult

#: The ``options`` keys this stage's code reads; any other is refused (D434)
OPTIONS: frozenset[str] = frozenset({"author_buckets", "author_ids", "from", "full", "lookback_days", "max_records",
                                     "queries", "retry_failed_run", "roster", "to", "window_days",
                                     "window_end_days_ago", SELECT_OPTION}) | (
    SAMPLING_OPTIONS | METER_OPTIONS | RELIST_OPTIONS)

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────
CHANNEL = "openalex"
DEFAULT_FROM = "2020-01-01"
API_BASE = "https://api.openalex.org/works"
PAGE_SIZE = 200                      # OpenAlex per_page maximum
DELAY_S = 0.15                       # polite pool allows ~10 req/s; stay well under (the pacing)
RETRY_DELAYS_S = (2.0, 8.0, 20.0)    # 429/5xx backoff before giving up on a page
# A 429 whose body says to come back later than this is a BUDGET response, not a burst limit.
# OpenAlex meters requests since 2026 (a search $0.001, $0.10/day free keyless, $1 keyed, reset at
# midnight UTC; stack.collect.provider_keys) and
# answers an exhausted budget with `retryAfter` in the tens of thousands of seconds; retrying
# that on the 2/8/20 s ladder — and then moving on to the next query to do it again — spent 59
# queries × 4 requests on 2026-09-07 learning one fact. Anything above this halts the run.
BUDGET_RETRY_AFTER_S = 60.0
#: Default ``lookback_days`` (D582): a resumed query re-asks this many days of publication dates below
#: its watermark. In the 2026-10-14 sweep, of the works published since 2026-06-01 (whose lag can be
#: seen up to ~4 months) 2.2% were created more than 30 days after publication and 0.7% more than
#: 60; the wider cohort since 2026-01-01 has ~11% above 60 days, spread to ~265, which no look-back
#: of this order reaches (``full``, module docstring). 30 -> 60 days costs about a month of the
#: queries' volume more per run (~70 of the full year's 845 pages).
DEFAULT_LOOKBACK_DAYS = 60

# The rate-limit headers OpenAlex sends on every answer (200 and 429 alike). Captured by
# `http_get_json` as a side channel so the run's counts can report the day's remaining budget
# without changing the (status, body) seam the tests monkeypatch.
_LAST_RATELIMIT: dict[str, str] = {}
_RATELIMIT_HEADERS = ("x-ratelimit-remaining", "x-ratelimit-remaining-usd", "x-ratelimit-reset")


class OpenAlexBudgetExhausted(RunHalted):
    """OpenAlex answered 429 with a long ``retryAfter``: the day's request budget is spent.

    Raised out of :func:`_get_page` instead of the backoff ladder; a :class:`RunHalted`, so the
    harness writes the in-flight window ``failed_transient[rate_limit]`` uncharged and
    :meth:`Collector.halts` ends the sweep (module docstring)."""

    def __init__(self, retry_after_s: float, message: str) -> None:
        super().__init__(retry_after_s, message)
        self.message = message


# Scholarly work types kept. `preprint` is load-bearing (see module docstring); `review` and
# `book-chapter` carry real safety-relevant surveys; `conference-paper` (D653) holds 22-33 % of
# every family's hits, and a proceedings paper whose arXiv copy OpenAlex merged into the proceedings record is
# reachable only under it (Corrupted Reward Channel, IJCAI 2017). Everything else (dataset, paratext,
# editorial, erratum, report, dissertation …) is noise at this stage. The list is in every family's filter and so
# in every coverage key: editing it re-sweeps every query from the floor once.
ARTICLE_TYPES = ("article", "preprint", "review", "book-chapter", "conference-paper")

# OpenAlex `primary_topic.field.id` for Computer Science — the flood tamer v1 applied to `broad`
# concept groups, whose common tokens otherwise drown the sweep in unrelated literature.
CS_FIELD_ID = "17"

# The fields the sweep reads (D525: no `select` by default, full works become the `provider_record`; the
# `select:` option restores a field list, which must include these). They kept responses small:
#   abstract_inverted_index -> the in-band abstract (§5.4);
#   authorships             -> author names AND the per-author affiliation/ORCID detail;
#   ids                     -> the PMID key;
#   primary_location        -> the landing URL;
#   cited_by_count, best_oa_location -> part of the record (v3 carried them into collect.meta;
#                              `sources.metadata` has no slot, so they are only in the digest's
#                              record — `cited_by_count` not even there, C23);
#   locations               -> arXiv locations as extra `arxiv` keys (stack/identity.py key adder
#                              `openalex_arxiv_location`; round 5, T-ID).
SELECT_FIELDS = (
    "id,doi,title,ids,type,publication_date,authorships,primary_location,cited_by_count,"
    "best_oa_location,abstract_inverted_index,locations"
)

BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0 Safari/537.36 stack-collect/1.0 (+research)"
)

#: v3's constants as the harness transport's pacing: one request per DELAY_S per host (v3 slept
#: DELAY_S between pages and queries); no transport-level ladder (_get_page's 2/8/20 s ladder is
#: v3's); a 429 whose retry-after exceeds BUDGET_RETRY_AFTER_S halts the run.
PACING = Pacing(rps=1.0 / DELAY_S, retry_delays_s=(), halt_429_after_s=BUDGET_RETRY_AFTER_S,
                headers={"User-Agent": BROWSER_UA})

#: The author-id family (module docstring): fixed hash buckets, and OpenAlex's OR limit per filter.
AUTHOR_BUCKETS = 64
MAX_IDS_PER_FILTER = 100

_ENV_LIMIT = "STACK_COLLECT_LIMIT"
_ENV_FROM = "STACK_OPENALEX_FROM"
_ENV_FULL = "STACK_OPENALEX_FULL"
_ENV_RETRY = "STACK_OPENALEX_RETRY_FAILED_RUN"
_ENV_MAILTO = "STACK_MAILTO"
_DEFAULT_LIMIT = 200
#: The publication floor as written into the coverage key (round 5 (RUN) B7a): the key names the
#: query, not its floor, so a floor moved back leaves a hole of the same key (module docstring).
KEY_FLOOR = "*"

_ISO_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})")
# OpenAlex gives ORCID as a full URL (https://orcid.org/0000-0002-1825-0097); the bare id is the
# 16-digit-plus-checksum form (the last char may be X). Extract it wherever it sits in the string.
_ORCID_RE = re.compile(r"(\d{4}-\d{4}-\d{4}-\d{3}[\dxX])")


# ─────────────────────────────────────────────────────────────────────────────
# HTTP (module-level seams: tests monkeypatch ``http_get_json`` / ``_sleep`` offline)
# ─────────────────────────────────────────────────────────────────────────────
#: The run's paced transport (set by :func:`run`); ``http_get_json`` goes through it.
_T: Transport | None = None
#: The run transport's sender under the OpenAlex meter (``None`` = the harness default); a test
#: seam for keyed runs (tests install a ``tests/fakes/http.FakeHttp``).
SEND: Sender | None = None


def http_get_json(url: str, params: dict[str, Any], *, timeout: float = 60.0) -> tuple[int, Any]:
    """One paced GET through the run's transport returning ``(status, decoded_json_or_None)``.
    A transport failure comes back as ``(0, None)`` and an unparseable body as ``(status, None)``
    so the caller records an honest failed window instead of crashing the sweep; a non-2xx body
    is decoded too (OpenAlex's 429 body is the only place ``retryAfter`` lives). Raises
    :class:`RunHalted` on a budget 429 (the transport's halt). ``timeout`` is kept for the
    seam's signature; ``PACING.timeout_s`` applies. Outside a run (``tools/probe_filters.py``
    imports this seam) it sends one request through the harness's default sender, paced only by
    the cross-process provider ``Gate``."""
    if _T is None:
        resp = urllib_send("GET", full_url(url, params), headers=dict(PACING.headers), body=None,
                           timeout=PACING.timeout_s, max_bytes=PACING.max_bytes)
    else:
        resp = _T.get(url, params)
    _remember_ratelimit(resp.headers)
    if resp.status is None:
        return 0, None
    try:
        return resp.status, (json.loads(resp.body) if resp.body else None)
    except (ValueError, UnicodeDecodeError):
        return resp.status, None


def _remember_ratelimit(headers: Mapping[str, str]) -> None:
    """Keep the latest rate-limit headers (see ``_LAST_RATELIMIT``)."""
    for name in _RATELIMIT_HEADERS:
        value = headers.get(name)
        if value is not None:
            _LAST_RATELIMIT[name] = str(value)


def _budget_retry_after(status: int, data: Any) -> float | None:
    """The ``retryAfter`` of a budget-exhausted 429, else ``None``.

    Only a 429 whose decoded body carries ``retryAfter`` above ``BUDGET_RETRY_AFTER_S`` counts.
    A bare ``(429, None)`` — what a burst limit looks like — keeps the backoff ladder.
    """
    if status != 429 or not isinstance(data, dict):
        return None
    try:
        retry_after = float(data.get("retryAfter") or 0)
    except (TypeError, ValueError):
        return None
    return retry_after if retry_after > BUDGET_RETRY_AFTER_S else None


def _sleep(seconds: float) -> None:
    """Retry delays (module-level so offline tests monkeypatch it to a no-op); the transport's
    pacer sleeps through it too."""
    time.sleep(seconds)


def _ratelimit_detail() -> dict[str, Any]:
    """``{remaining, remaining_usd, reset_s}`` from the last OpenAlex answer, when seen."""
    out: dict[str, Any] = {}
    if "x-ratelimit-remaining" in _LAST_RATELIMIT:
        out["remaining"] = _LAST_RATELIMIT["x-ratelimit-remaining"]
    if "x-ratelimit-remaining-usd" in _LAST_RATELIMIT:
        out["remaining_usd"] = _LAST_RATELIMIT["x-ratelimit-remaining-usd"]
    if "x-ratelimit-reset" in _LAST_RATELIMIT:
        out["reset_s"] = _LAST_RATELIMIT["x-ratelimit-reset"]
    return out


def _credential_free(text: str) -> str:
    """``text`` with the OpenAlex key (when one is configured) and any ``api_key=`` value blanked,
    at most 500 characters: a provider message that lands in a row or a run record."""
    key = openalex_key()
    if key:
        text = text.replace(key, "<redacted>")
    return re.sub(r"(api_key=)[^&\s\"']+", r"\1<redacted>", text)[:500]


def _refusal(status: int, data: Any) -> RunRefused | None:
    """The :class:`RunRefused` for an answer no retry fixes (module docstring, D583), else ``None``: a
    plan refusal (:func:`stack.provider_transport.plan_refusal`, any status) is
    ``failed_permanent[provider_plan]``; a 401/403 ``failed_transient[provider_auth]``."""
    if status == 200:
        return None
    message = plan_refusal(data)
    if message is not None:
        return RunRefused("provider_plan", _credential_free(f"HTTP {status}: {message}"), permanent=True)
    if status in (401, 403):
        detail = str((data or {}).get("message") or (data or {}).get("error") or "") if isinstance(data, dict) else ""
        return RunRefused("provider_auth", _credential_free(f"HTTP {status}: {detail or 'key refused'}"),
                          permanent=False)
    return None


def _get_page(params: dict[str, Any], stats: dict[str, Any] | None = None) -> Any | None:
    """One page WITH backoff on the two retryable conditions (429 rate-limit, 5xx) plus transport
    failures. Returns the decoded body, or ``None`` once the retries are spent — which the caller
    turns into a failed window rather than a silent short page; ``stats['last_status']`` then
    holds the status that ended it (``0`` = transport failure), see :func:`error_class_of`.

    Passing a ``stats`` dict makes the otherwise-silent backoffs visible — each retryable response
    is classified (``http_429`` / ``http_5xx`` / ``timeouts``) and every actual retry (sleep +
    re-request) bumps ``retries`` (the missing-mailto debugging incident). A budget 429 raises
    :class:`OpenAlexBudgetExhausted` at once (no ladder); a plan refusal or a 401/403 raises
    :class:`RunRefused` at once (:func:`_refusal`, D583); a Gate cooldown answer (the transport
    returns one only once its own cooldown waits are spent) fails the page at once (D588)."""
    for delay in (*RETRY_DELAYS_S, None):
        status, data = http_get_json(API_BASE, params)
        if status == 200 and isinstance(data, dict):
            return data
        refused = _refusal(status, data)
        if refused is not None:
            if stats is not None:
                stats["last_status"] = status
                if status == 429:
                    stats["http_429"] = stats.get("http_429", 0) + 1
            raise refused
        budget_wait = _budget_retry_after(status, data)
        if budget_wait is not None:
            if stats is not None:
                stats["http_429"] = stats.get("http_429", 0) + 1
            message = str((data or {}).get("message") or "OpenAlex request budget exhausted")
            raise OpenAlexBudgetExhausted(budget_wait, message)
        if stats is not None:
            stats["last_status"] = status
            if status == 429:
                stats["http_429"] = stats.get("http_429", 0) + 1
            elif 500 <= status < 600:
                stats["http_5xx"] = stats.get("http_5xx", 0) + 1
            elif status == 0:
                stats["timeouts"] = stats.get("timeouts", 0) + 1
        retryable = status == 429 or status == 0 or 500 <= status < 600
        if not retryable or delay is None or _is_cooldown_answer(status, data):
            return None
        if stats is not None:
            stats["retries"] = stats.get("retries", 0) + 1
        _sleep(delay)
    return None


def _is_cooldown_answer(status: int, data: Any) -> bool:
    """Whether a decoded answer is the provider Gate's synthetic cooldown 429 (nothing was sent;
    ``stack.provider_transport.COOLDOWN_MESSAGE``)."""
    return status == 429 and isinstance(data, dict) and data.get("message") == COOLDOWN_MESSAGE


def error_class_of(status: int) -> tuple[str, bool]:
    """The coverage ``(error_class, permanent)`` of the status that failed a page: 429 →
    ``rate_limit``, 5xx → ``http_5xx``, 401/403 → ``provider_auth``, 400 → ``query_invalid``
    (permanent: OpenAlex rejected the filter), a transport failure or a 200 with an unparseable
    body → ``network``."""
    if status == 429:
        return "rate_limit", False
    if 500 <= status < 600:
        return "http_5xx", False
    if status in (401, 403):
        return "provider_auth", False
    if status == 400:
        return "query_invalid", True
    return "network", False


# ─────────────────────────────────────────────────────────────────────────────
# Query construction
# ─────────────────────────────────────────────────────────────────────────────
def _phrase_or(terms: list[str]) -> str:
    """``"t1" OR "t2"`` over the usable terms: surrounding quotes stripped (a quoted
    ``openalex_terms`` entry is the same phrase), a term with an inner quote or a comma dropped."""
    safe = [t.strip().strip('"').strip() for t in terms]
    return " OR ".join(f'"{t}"' for t in safe if t and '"' not in t and "," not in t)


def phrase_query(terms: list[str], ai_context: list[str]) -> str:
    """The ``title_and_abstract.search`` value of a ``needs_ai_context`` group: the group's
    phrases OR'd, ANDed with the ``ai_context`` phrases OR'd (OpenAlex's boolean search; quoted =
    phrase match). '' when either side has no usable term."""
    left, right = _phrase_or(terms), _phrase_or(ai_context)
    return f"({left}) AND ({right})" if left and right else ""


def group_filter(terms: list[str], from_date: str, broad: bool, *,
                 to_date: str | None = None, ai_context: list[str] | None = None) -> str:
    """The OpenAlex ``filter`` expression for one concept group.

    ``title_and_abstract.search`` takes the group's synonym terms OR'd with ``|``. Commas
    separate ANDed filters, so a term containing one would silently split the expression into
    garbage — such terms are dropped (they cannot be expressed on this backend). ``to_date``
    bounds the publication window (``to_publication_date``). A non-empty ``ai_context`` (a
    ``needs_ai_context`` group) renders the search value as :func:`phrase_query` instead.
    ``from_date`` is the floor, or a resumed query's look-back start (D582); there is no created or
    updated axis (OpenAlex refuses both below a Premium plan).
    """
    if ai_context:
        search = phrase_query(terms, ai_context)
    else:
        search = "|".join(t.strip() for t in terms if t.strip() and "," not in t)
    parts = [
        f"type:{'|'.join(ARTICLE_TYPES)}",
        f"from_publication_date:{from_date}",
        f"title_and_abstract.search:{search}",
    ]
    if to_date:
        parts.append(f"to_publication_date:{to_date}")
    if broad:
        parts.append(f"primary_topic.field.id:{CS_FIELD_ID}")
    return ",".join(parts)


def institution_filter(iid: str, from_date: str, fields: list[int], *, to_date: str | None = None) -> str:
    """The OpenAlex ``filter`` for one institution-id sweep query (v1
    ``search_openalex_using_institution_ids.py``): ``institutions.id:<iid>`` from the floor, plus
    the ``primary_topic.field.id`` volume guard when the org configured ``fields`` (v1's generic
    big-company guard).

    **v3 departure from v1.** v1 ran this family with NO type filter, deliberately letting the
    downstream affil-filter → screen gates decide relevance. There is no downstream relevance gate
    at collect time, so an unbounded institution pull would flood the corpus with every dataset/
    erratum/paratext a lab ever deposited. We therefore apply the SAME ``ARTICLE_TYPES`` filter the
    concept groups use. (Same incrementality as the group queries: a resumed pass asks from its
    look-back start, D582.)
    """
    safe_id = iid.strip()
    parts = [
        f"type:{'|'.join(ARTICLE_TYPES)}",   # v3 volume bound (see docstring); v1 had no type filter
        f"institutions.id:{safe_id}",
        f"from_publication_date:{from_date}",
    ]
    if to_date:
        parts.append(f"to_publication_date:{to_date}")
    if fields:
        parts.append("primary_topic.field.id:" + "|".join(str(f) for f in fields))
    return ",".join(parts)


def author_filter(ids: list[str], from_date: str, *, to_date: str | None = None) -> str:
    """The OpenAlex ``filter`` for one author-id bucket: ``authorships.author.id`` over ``ids``
    (OR'd with ``|``), the scholarly types, the publication floor or look-back start (and end) —
    the institution family's shape with an author filter."""
    bad = [i for i in ids if not re.fullmatch(r"A\d+", i)]
    if bad or not ids:
        raise ValueError(f"author_filter: {bad or 'no ids'} are not OpenAlex author ids")
    parts = [
        f"type:{'|'.join(ARTICLE_TYPES)}",
        f"authorships.author.id:{'|'.join(ids)}",
        f"from_publication_date:{from_date}",
    ]
    if to_date:
        parts.append(f"to_publication_date:{to_date}")
    return ",".join(parts)


def _base_params(filt: str, select: str | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {"filter": filt, "per_page": PAGE_SIZE}
    if select is not None:
        params["select"] = select  # the `select:` option (D525); none = full works
    params["cursor"] = "*"
    mailto = (os.environ.get(_ENV_MAILTO) or "").strip()
    if mailto:
        params["mailto"] = mailto  # polite pool: markedly higher rate limits
    return params


def fetch_group(
    filt: str,
    limit: int,
    stats: dict[str, Any] | None = None,
    *,
    on_page: Callable[[int], None] | None = None,
    select: str | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Cursor-paginate one group query up to ``limit`` works.

    Returns ``(works, exhausted)`` with ``exhausted`` ∈ {complete, truncated, error}
    (:data:`stack.collect.base.EXHAUSTED`). Unlike the arXiv sweep this cannot bank a partial
    window on truncation: OpenAlex cursor order is not date order, so a capped pull covered an
    unknown subset and must claim nothing. Pacing between pages is the transport's (``PACING``).

    An optional ``stats`` dict is threaded to :func:`_get_page` so backoff/429 activity across the
    whole run is surfaced. ``on_page`` (when given) is called after each page with the running
    work total. A budget 429 propagates as :class:`OpenAlexBudgetExhausted` / :class:`RunHalted`.
    ``select``: the ``select`` parameter (D525; ``None`` = full works).
    """
    works: list[dict[str, Any]] = []
    params = _base_params(filt, select)
    while True:
        if len(works) >= limit:
            return works[:limit], "truncated"
        data = _get_page(params, stats)
        if data is None:
            return works, "error"
        batch = data.get("results") or []
        if not batch:
            return works, "complete"
        works.extend(batch)
        if on_page is not None:
            on_page(len(works))
        cursor = ((data.get("meta") or {}).get("next_cursor")) or ""
        if not cursor:
            return works[:limit], "complete"
        params["cursor"] = cursor


# ─────────────────────────────────────────────────────────────────────────────
# Work normalisation + identity
# ─────────────────────────────────────────────────────────────────────────────
def work_arxiv_located(work: dict[str, Any]) -> bool:
    """Whether any of the work's locations (``primary_location``, ``locations``, ``best_oa_location``) is the arXiv
    repository by OpenAlex source id (:data:`stack.rules.OPENALEX_ARXIV_SOURCE_IDS`). An arXiv URL among
    ``locations`` gives an ``arxiv`` key instead (:func:`stack.identity.added_keys`)."""
    for loc in [work.get("primary_location"), work.get("best_oa_location"), *(work.get("locations") or [])]:
        src = loc.get("source") if isinstance(loc, dict) else None
        sid = str(src.get("id") or "").rstrip("/").rsplit("/", 1)[-1].upper() if isinstance(src, dict) else ""
        if sid in rules.OPENALEX_ARXIV_SOURCE_IDS:
            return True
    return False


def work_cut(work: dict[str, Any], keys: list[tuple[str, str]], item: dict[str, Any]) -> str | None:
    """The backfill cut that drops this hit of plan entry ``item`` (module docstring), else ``None``: only a
    concept-group entry with cuts; the keys with the identity key adders' (an arXiv location's ``arxiv`` key)."""
    if item["kind"] != "group" or not item.get("cuts"):
        return None
    allk, text, located = _cut_inputs(work, keys)
    return rules.keyword_cut(allk, text, drop_sources=item["drop_sources"], ai_term=item["ai_term"],
                             arxiv_located=located, published=work_published(work),
                             publisher_cut_floor=item.get("publisher_cut_floor"),
                             keep_venues=bool(item.get("keep_venues")), venue_ai_term=bool(item.get("venue_ai_term")))


def work_cut_bypassed(work: dict[str, Any], keys: list[tuple[str, str]], item: dict[str, Any]) -> bool:
    """Whether a source cut would drop this hit and the dated exception (D654) let it through (it may still be
    cut ``ai_term``): counted ``publisher_cut_bypassed``."""
    if item["kind"] != "group" or not item.get("publisher_cut_floor"):
        return False
    allk, text, located = _cut_inputs(work, keys)
    keep = rules.venue_allowlist_applies(text, keep_venues=bool(item.get("keep_venues")),
                                         venue_ai_term=bool(item.get("venue_ai_term")))
    return (rules.source_cut(allk, drop_sources=item["drop_sources"], arxiv_located=located,
                             keep_venues=keep) is not None
            and rules.publisher_cut_exempt(text, work_published(work), item["publisher_cut_floor"]))


def work_venue_kept(work: dict[str, Any], keys: list[tuple[str, str]], item: dict[str, Any]) -> bool:
    """Whether the venue allowlist (the Chinese-coverage work) is what keeps this hit from the ``publisher`` cut: counted
    ``venue_kept`` (it may still be cut ``ai_term``)."""
    if item["kind"] != "group" or not item.get("keep_venues"):
        return False
    allk, text, located = _cut_inputs(work, keys)
    return (rules.source_class(allk, arxiv_located=located, keep_venues=True) == "venue"
            and rules.venue_allowlist_applies(text, keep_venues=True, venue_ai_term=bool(item.get("venue_ai_term"))))


def _cut_inputs(work: dict[str, Any], keys: list[tuple[str, str]]) -> tuple[list[tuple[str, str]], str, bool]:
    """The keys with the identity key adders' (an arXiv location's ``arxiv`` key), title + abstract, arXiv-located."""
    abstract = deinvert_abstract(work.get("abstract_inverted_index")) or ""
    return (keys + added_keys(CHANNEL, "paper", work, keys), f"{work.get('title') or ''} {abstract}",
            work_arxiv_located(work))


def work_published(work: dict[str, Any]) -> str | None:
    """The work's publication date (:func:`work_pub_date`), else its ``publication_year`` as ``YYYY``."""
    year = work.get("publication_year")
    return work_pub_date(work) or (str(year) if isinstance(year, int) and not isinstance(year, bool) else None)


def openalex_short_id(work_id: str) -> str:
    """``https://openalex.org/W123`` → ``W123`` (the ``openalex`` key's normal form,
    :func:`stack.scholarly_records.normalize_openalex_id`); '' when it is not a work id."""
    return normalize_openalex_id(work_id or "")


def work_doi(work: dict[str, Any]) -> str:
    """The work's DOI in the ``doi`` key's normal form (D103: lower case, resolver prefix
    stripped, ``10.NNNN…/suffix``; an arXiv DataCite DOI as ``10.48550/arxiv.<id>``); ''."""
    return normalize_doi(str(work.get("doi") or ""))


def work_identity(work: dict[str, Any]) -> tuple[str, str] | None:
    """The ``(channel, external_id)`` a work is registered under, or ``None`` when unkeyable:
    ``('openalex', <normalized DOI>)``, else ``('openalex', <OpenAlex work id>)`` (§5.6 "DOI else
    W…"), so a work without a DOI is still discovered rather than dropped. An arXiv DOI is no
    longer re-keyed to channel ``arxiv``: the bridge is the ``arxiv`` key :func:`work_keys` adds
    (module docstring)."""
    doi = work_doi(work)
    if doi:
        return CHANNEL, doi
    short = openalex_short_id(str(work.get("id") or ""))
    return (CHANNEL, short) if short else None


def work_keys(work: dict[str, Any]) -> list[tuple[str, str]]:
    """The work's typed keys as ``(kind, value)`` pairs (:func:`stack.scholarly_records.keys_for`):
    ``openalex`` from the work id, ``doi`` (plus the bridged ``arxiv`` for an arXiv DOI), ``pmid``
    from ``ids.pmid``. Junk values give no key."""
    ids: dict[str, Any] = work["ids"] if isinstance(work.get("ids"), dict) else {}
    pairs = [("openalex", work.get("id")), ("doi", work.get("doi")), ("pmid", ids.get("pmid"))]
    return [(k["kind"], k["value"]) for k in keys_for([(kind, v) for kind, v in pairs if v])]


def work_url(work: dict[str, Any], external_id: str) -> str:
    """The item's canonical locator — arXiv abs page for an arXiv DOI / doi.org resolver /
    OpenAlex landing page / the OpenAlex work page."""
    for kind, value in work_keys(work):
        if kind == "arxiv":
            return f"https://arxiv.org/abs/{value}"
    doi = work_doi(work)
    if doi:
        return f"https://doi.org/{doi}"
    landing = ((work.get("primary_location") or {}).get("landing_page_url")) or ""
    return str(landing) or f"https://openalex.org/{external_id}"


def doc_type_of(work: dict[str, Any]) -> str:
    """OpenAlex ``type`` → the doc_type vocabulary. Anything unrecognised stays ``other`` for
    annotate.doc_class to settle rather than being guessed here."""
    t = str(work.get("type") or "").strip().lower()
    if t == "preprint":
        return "preprint"
    if t in ("article", "review", "book-chapter", "conference-paper"):
        return "paper"  # a proceedings paper is a peer-reviewed paper (enums.yaml doc_type has no `conference`)
    return "other"


def work_authors(work: dict[str, Any]) -> list[str]:
    """Author display names in OpenAlex's own order (authorship order is meaningful)."""
    return [a["name"] for a in work_authors_detail(work)]


def normalize_orcid(raw: Any) -> str | None:
    """The bare ORCID id from whatever OpenAlex hands back (a full ``orcid.org`` URL or a bare id),
    uppercased so the checksum X is canonical. ``None`` when absent/unparseable."""
    m = _ORCID_RE.search(str(raw or ""))
    return m.group(1).upper() if m else None


def work_authors_detail(work: dict[str, Any]) -> list[dict[str, Any]]:
    """Per-authorship ``{name, orcid?, institutions: [display names]}`` in OpenAlex's own order.

    ``institutions`` is the authorship's resolved institution display names; when OpenAlex resolved
    none it falls back to ``raw_affiliation_strings`` (the unparsed affiliation lines), so an
    author with an affiliation the graph could not disambiguate is not silently affiliation-less."""
    out: list[dict[str, Any]] = []
    for a in work.get("authorships") or []:
        if not isinstance(a, dict):
            continue
        author = a.get("author") or {}
        name = str((author.get("display_name") if isinstance(author, dict) else "") or "").strip()
        if not name:
            continue
        entry: dict[str, Any] = {"name": name}
        orcid = normalize_orcid(author.get("orcid") if isinstance(author, dict) else None)
        if orcid:
            entry["orcid"] = orcid
        institutions = [
            n
            for i in (a.get("institutions") or [])
            if isinstance(i, dict) and (n := str(i.get("display_name") or "").strip())
        ]
        if not institutions:
            institutions = [
                s for raw in (a.get("raw_affiliation_strings") or []) if (s := str(raw).strip())
            ]
        entry["institutions"] = institutions
        out.append(entry)
    return out


def metadata_authors(work: dict[str, Any]) -> list[dict[str, Any]] | None:
    """``sources.metadata.authors`` (A.5 ``AUTHOR``): ``{name, orcid, affiliation_raw}`` per
    authorship, ``affiliation_raw`` = the author's institutions joined with ``"; "`` (the
    per-lab attribution detail v3 kept as ``authors_detail``); ``None`` when there is none."""
    out = [{"name": a["name"], "orcid": a.get("orcid"),
            "affiliation_raw": "; ".join(a["institutions"]) or None}
           for a in work_authors_detail(work)]
    return out or None


def work_pub_date(work: dict[str, Any]) -> str | None:
    m = _ISO_DATE.match(str(work.get("publication_date") or "").strip())
    return m.group(1) if m else None


def deinvert_abstract(idx: Any) -> str | None:
    """Rebuild an OpenAlex abstract from its ``abstract_inverted_index`` (D-INBAND1).

    OpenAlex ships abstracts as a word→positions inverted index
    (``{"token": [pos, ...], ...}``); reconstruct by placing each token at each of its positions,
    sorting by position, and joining with single spaces. Returns ``None`` for a None/empty/malformed
    index and is defensive about non-int positions (a stray string/float/bool is skipped rather than
    crashing the sweep) — a work with no usable positions yields ``None``. Verified live to
    reconstruct the full abstract exactly; ~96% of works carry the field."""
    if not isinstance(idx, dict) or not idx:
        return None
    placed: list[tuple[int, str]] = []
    for token, positions in idx.items():
        if not isinstance(positions, (list, tuple)):
            continue
        for pos in positions:
            # bool is an int subclass — exclude it so a stray True/False is not read as 0/1.
            if isinstance(pos, bool) or not isinstance(pos, int):
                continue
            placed.append((pos, str(token)))
    if not placed:
        return None
    placed.sort(key=lambda p: p[0])
    return " ".join(tok for _, tok in placed)


# ─────────────────────────────────────────────────────────────────────────────
# The sweep plan
# ─────────────────────────────────────────────────────────────────────────────
def plan_groups(config: StackConfig) -> list[dict[str, Any]]:
    """The ordered sweep plan: one entry per non-misuse concept group with usable terms.

    ``misuse`` groups are skipped exactly as in v1 — their AI-context intersection cannot be
    expressed as an OR of terms, so the query would return the whole unrelated literature.
    (The 2026-10-12 probe shows the boolean phrase form can express it; ``needs_ai_context``
    groups use it, the misuse rule is left as v1 had it.) A ``needs_ai_context`` group carries
    the ``ai_context`` list for :func:`group_filter` and is skipped when that list is empty. Each entry carries the
    channel's ``keyword_cuts`` that apply to it (``drop_sources``, ``ai_term``, and their names as ``cuts``) and the
    dated exception's ``publisher_cut_floor`` (``None`` without source cuts). A group in ``field_filter_groups`` is
    followed by a second entry, the same without the CS-field filter (``broad`` false) and with ``to`` = the day
    before ``field_filter_floor`` (D654 (b); module docstring).
    """
    ai_context = list(config.channels.ai_context)
    cuts = config.channels.channel(CHANNEL).keyword_cuts
    drop_sources = sorted(cuts.drop_sources) if cuts else []
    ai_term_groups = set(cuts.ai_term_groups) if cuts else set()
    cut_floor = (cuts.publisher_cut_floor if cuts else None) if drop_sources else None
    field_floor = cuts.field_filter_floor if cuts else None
    field_groups = set(cuts.field_filter_groups) if cuts else set()
    keep_venues = bool(cuts and cuts.keep_venues and "publisher" in drop_sources)
    venue_ai_term = bool(keep_venues and cuts and cuts.venue_ai_term)
    plan: list[dict[str, Any]] = []
    for g in config.channels.concept_groups:
        if g.misuse:
            continue
        if g.needs_ai_context and not ai_context:
            continue  # unintersected, the group is another field's literature
        # `openalex_terms` REPLACES `terms` here (v1 parity): a phrase that is precise under
        # phrase-matching can flood token-AND search, and an EMPTY override means the group has
        # no OpenAlex-viable term at all and belongs only to the phrase-match APIs.
        terms = g.terms_for_openalex()
        if not terms:
            continue
        topic_keys = [f"group:{g.id}"] + [f"technique:{t}" for t in g.techniques]
        entry: dict[str, Any] = {
            "kind": "group",
            "id": g.id,
            "query": f"group:{g.id}",
            "terms": list(terms),
            "broad": g.broad,
            "ai_context": list(ai_context) if g.needs_ai_context else [],
            "topic_keys": topic_keys,
            # the backfill cuts on this group's hits (module docstring); `cuts` goes into the coverage key
            "drop_sources": drop_sources,
            "ai_term": g.id in ai_term_groups,
            "cuts": sorted([*drop_sources, *(["ai_term"] if g.id in ai_term_groups else [])]),
            # D654 (a): the dated publisher-cut exception (in the coverage key)
            "publisher_cut_floor": cut_floor,
            # The Chinese-coverage work: the venue allowlist through the publisher cut (its digest in the coverage key)
            "keep_venues": keep_venues,
            "venue_ai_term": venue_ai_term,
        }
        plan.append(entry)
        if field_floor and g.broad and g.id in field_groups:
            # D654 (b): before the floor once more without the CS-field filter, as its own entry and key
            to = (dt.date.fromisoformat(field_floor) - dt.timedelta(days=1)).isoformat()
            plan.append({**entry, "broad": False, "to": to})
    return plan


def plan_institutions(config: StackConfig) -> list[dict[str, Any]]:
    """The institution-id sweep plan: one entry per configured OpenAlex institution
    (channels.yaml ``institutions:``, v1 ``orgs.yaml openalex_institutions``).

    Each entry carries the query label ``inst:<Iid>`` (also the sampling key) and a single
    ``org:<slug>`` topic key. No keyword filter (that is the whole point of this family — recall
    of org-affiliated work matching no safety keyword); the optional ``fields`` volume guard rides
    along for the filter builder.
    """
    plan: list[dict[str, Any]] = []
    for inst in config.channels.institutions():
        plan.append(
            {
                "kind": "institution",
                "id": inst.id,
                "query": f"inst:{inst.id}",
                "org": inst.org,
                "fields": list(inst.fields),
                "topic_keys": [f"org:{inst.org}"],
            }
        )
    return plan


def plan_authors(roster: roster_mod.Roster, buckets: int = 0) -> list[dict[str, Any]]:
    """The author-id family's plan: the roster's OpenAlex ids in ``buckets`` (default
    :data:`AUTHOR_BUCKETS`) fixed buckets by id hash, one entry per non-empty bucket in bucket
    order: ``{kind: authors, id: bNN, query: authors:bNN, ids, topic_keys: []}``. A bucket over
    :data:`MAX_IDS_PER_FILTER` ids is a ``ValueError`` (raise ``author_buckets``)."""
    n = buckets or AUTHOR_BUCKETS
    if n < 1:
        raise ValueError(f"option author_buckets: {n} < 1")
    by: dict[int, list[str]] = {}
    for aid in sorted({i for e in roster.entries for i in e.openalex_ids}):
        by.setdefault(int(hashlib.sha256(aid.encode()).hexdigest(), 16) % n, []).append(aid)
    width = len(str(n - 1))
    plan: list[dict[str, Any]] = []
    for k in sorted(by):
        if len(by[k]) > MAX_IDS_PER_FILTER:
            raise ValueError(f"author bucket {k}: {len(by[k])} ids > {MAX_IDS_PER_FILTER} "
                             f"(OpenAlex's OR limit); raise option author_buckets above {n}")
        bid = f"b{k:0{width}d}"
        plan.append({"kind": "authors", "id": bid, "query": f"authors:{bid}", "ids": by[k],
                     "topic_keys": []})
    return plan


def query_label(coverage_query: str) -> str:
    """The query label (``group:<id>`` / ``inst:<Iid>``) of a ``coverage.query`` this collector
    wrote (the JCS of ``{"query": label, "filter": …}``, module docstring)."""
    try:
        data = json.loads(coverage_query)
    except ValueError:
        return coverage_query
    return str(data.get("query", coverage_query)) if isinstance(data, dict) else coverage_query


def failed_only_plan(coverage: list[dict[str, Any]], combined: list[dict[str, Any]],
                     retry_run: str) -> list[dict[str, Any]]:
    """The plan entries whose coverage row from run ``retry_run`` is ``failed_*``, in plan order.

    ``coverage`` is the own ``coverage.openalex`` view (current row per window, so a query re-run
    since ``retry_run`` shows the later run's row instead). This is a new attempt using the
    current configuration, not cursor continuation. Fails closed (``ValueError``) when no current
    row belongs to ``retry_run`` (an unknown or unfinished run: rows are emitted at a run's end),
    or when a failed query is no longer configured. ``partial`` (capped) rows are not retried."""
    mine = [r for r in coverage if r["run_id"] == retry_run]
    if not mine:
        raise ValueError(
            f"retry_failed_run {retry_run!r} must identify a finished collect.openalex run with "
            f"current rows in coverage.openalex (none found: unknown or unfinished run, or every "
            f"query it ran was re-run since)")
    failed = {query_label(r["query"]) for r in mine if str(r["status"]).startswith("failed_")}
    missing = failed - {item["query"] for item in combined}
    if missing:
        raise ValueError(f"Failed queries no longer configured: {sorted(missing, key=str)}")
    return [item for item in combined if item["query"] in failed]


# ─────────────────────────────────────────────────────────────────────────────
# The stage
# ─────────────────────────────────────────────────────────────────────────────
def _iso_date(raw: Any) -> str:
    return dt.date.fromisoformat(str(raw).strip()[:10]).isoformat()


def publication_window(c: Collector, chan_from: str | None,
                       today: dt.date) -> tuple[str, str | None]:
    """``(floor, to)`` of this run's publication window: ``to`` = option ``to``, else today −
    ``window_end_days_ago``, else ``None`` (open-ended: covered through the day of the pass);
    ``floor`` = ``from`` (env ``STACK_OPENALEX_FROM`` wins), else ``window_days`` before the end,
    else the channel's ``from_date``, else ``DEFAULT_FROM``."""
    to_raw = c.option("to", None, cast=str)
    ago = c.option("window_end_days_ago", None, cast=int)
    to_date = _iso_date(to_raw) if to_raw else (
        (today - dt.timedelta(days=ago)).isoformat() if ago is not None else None)
    explicit = c.option("from", None, env=_ENV_FROM, cast=str)
    if explicit:
        return _iso_date(explicit), to_date
    days = c.option("window_days", None, cast=int)
    if days:
        end = dt.date.fromisoformat(to_date) if to_date else today
        return (end - dt.timedelta(days=days)).isoformat(), to_date
    return _iso_date(chan_from or DEFAULT_FROM), to_date


def lookback_start(floor: str, claim_from: str, mark: dt.date, lookback_days: int) -> str:
    """Where a resumed open-ended tail asks from (D582): ``max(floor, mark − lookback_days)``, never
    above ``claim_from`` (where its window starts: the watermark, or a failed hole's end above it)."""
    return min(claim_from, max(floor, (mark - dt.timedelta(days=lookback_days)).isoformat()))


def _filter_for(item: dict[str, Any], from_date: str, to_date: str | None) -> str:
    """The plan entry's filter from ``from_date`` (the floor, :data:`KEY_FLOOR` for the coverage key,
    a hole's start, or a look-back start) to ``to_date``; every family alike (D582)."""
    if item["kind"] == "institution":
        return institution_filter(item["id"], from_date, item["fields"], to_date=to_date)
    if item["kind"] == "authors":
        return author_filter(item["ids"], from_date, to_date=to_date)
    return group_filter(item["terms"], from_date, item["broad"], to_date=to_date,
                        ai_context=item.get("ai_context") or None)


def entry_window(item: dict[str, Any], to_date: str | None, today: dt.date) -> tuple[str | None, str]:
    """``(to, end)`` of plan entry ``item`` in this run: ``to`` = the earlier of the run's ``to_date`` and the
    entry's own ``to`` (None: open-ended), ``end`` = ``to`` or today (the coverage window's end)."""
    own = item.get("to")
    to = min(to_date, own) if to_date and own else (to_date or own)
    return to, to or today.isoformat()


def coverage_query(item: dict[str, Any], to_date: str | None) -> dict[str, Any]:
    """The plan entry's coverage key (module docstring): ``{"query": <label>, "filter": <the filter from
    :data:`KEY_FLOOR` to ``to_date``, the entry's window end>}``, plus ``"cuts"`` when the entry has any (a cut
    switched off is a new key), ``"bypass"`` (the publisher-cut floor and :func:`stack.rules.pre2020_stem_digest`,
    D654), ``"venues"`` (:func:`stack.rules.venue_allowlist_digest`) and, for an ``ai_term`` group, ``"ai_terms"``
    (:func:`stack.rules.ai_term_digest`, D751). The key leaves the publication floor out: a floor moved back is a hole of the same key, not a new
    key that re-sweeps the covered range."""
    qkey: dict[str, Any] = {"query": item["query"], "filter": _filter_for(item, KEY_FLOOR, to_date)}
    if item.get("cuts"):
        qkey["cuts"] = item["cuts"]
    if item.get("publisher_cut_floor"):  # D654 (a): the floor and the stem set
        qkey["bypass"] = {"floor": item["publisher_cut_floor"], "stems": rules.pre2020_stem_digest()}
    if item.get("keep_venues"):  # The Chinese-coverage work: the venue allowlist
        qkey["venues"] = rules.venue_allowlist_digest()
    if item.get("venue_ai_term"):  # the allowlist keeps only AI-term hits: the vocabulary is in the key
        qkey["venue_ai_terms"] = rules.ai_term_digest()
    if item.get("ai_term"):  # D751: the AI-term vocabulary of the `ai_term` cut
        qkey["ai_terms"] = rules.ai_term_digest()
    return qkey


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes")


def run(ctx: StageContext) -> StageResult:
    global _T
    from stack.config import load

    config = load()  # slow (seconds): loaded once and handed to the harness
    c = Collector(ctx, channels={ch.id: ch for ch in config.channels.channels})
    chan = c.channel(CHANNEL)
    today = c.now.date()
    floor, to_date = publication_window(c, chan.from_date, today)
    configured = config.channels.max_results_per_query(CHANNEL, _DEFAULT_LIMIT)
    limit = max(1, c.option("max_records", configured, env=_ENV_LIMIT, cast=int))
    try:
        select_fields = openalex_select(c.option(SELECT_OPTION, None), SELECT_FIELDS)
    except ValueError as exc:
        raise CollectError(f"{ctx.stage}: {exc}") from None
    full_resweep = _truthy(c.option("full", "", env=_ENV_FULL, cast=str))
    lookback = c.option("lookback_days", DEFAULT_LOOKBACK_DAYS, cast=int)
    if lookback < 0:
        raise ValueError(f"option lookback_days: {lookback} < 0")

    # Two query families over ONE loop: the concept-group sweep (broad academic axis) and the
    # institution-id sweep (org-affiliated recall). They share identity, incrementality, in-band
    # capture and coverage; only the filter differs. Sampling runs over the COMBINED plan.
    group_plan = plan_groups(config)
    inst_plan = plan_institutions(config)
    author_plan: list[dict[str, Any]] = []
    if _truthy(c.option("author_ids", False, cast=str)):
        roster_opt = c.option("roster", None, env=roster_mod.ENV_ROSTER, cast=str)
        roster = roster_mod.load_roster_file(
            roster_mod.default_path(config.config_dir) if not roster_opt else
            Path(roster_opt))
        author_plan = plan_authors(roster, c.option("author_buckets", 0, cast=int))
    combined = group_plan + inst_plan + author_plan
    only = c.option("queries", None)
    if only:
        wanted = [str(q) for q in ([only] if isinstance(only, str) else only)]
        unknown = sorted(set(wanted) - {item["query"] for item in combined})
        if unknown:
            raise ValueError(f"option queries: {unknown} are not configured query labels")
        combined = [item for item in combined if item["query"] in wanted]
    retry_run = (c.option("retry_failed_run", "", env=_ENV_RETRY, cast=str) or "").strip()
    if retry_run:
        own = ctx.view("coverage").to_pylist() if ("coverage" in ctx.prior or "coverage" in ctx.inputs) else []
        combined = failed_only_plan(own, combined, retry_run)
    planned = len(combined)
    # Scale control: plan options feed sampling.select's env knobs (a set env var wins). An
    # explicit retry always selects ALL failed queries, regardless of sampling.
    env = dict(os.environ)
    for name, var in (("fraction", ENV_FRACTION), ("max_queries", ENV_MAX_QUERIES)):
        value = c.option(name, None, env=var, cast=str)
        if value is not None:
            env[var] = value
    plan, sampling = select(combined, lambda item: f"{CHANNEL}:{item['query']}",
                            env={} if retry_run else env)

    meter = OpenAlexMeter.for_run(c)
    _T = c.transport(PACING, send=meter.wrap(SEND), sleep=lambda s: _sleep(s))
    _LAST_RATELIMIT.clear()
    api_stats: dict[str, Any] = {"retries": 0, "http_429": 0, "http_5xx": 0, "timeouts": 0}
    tally = {"queries_complete": 0, "queries_truncated": 0, "queries_error": 0,
             "works_fetched": 0, "works_new": 0, "works_unkeyable": 0, "arxiv_doi": 0,
             "no_doi": 0, "abstracts_rebuilt": 0}
    for name in rules.KEYWORD_CUTS:
        tally[f"cut_{name}"] = 0
    tally["publisher_cut_bypassed"] = 0
    tally["venue_kept"] = 0
    observed: set[str] = set()
    relist = c.relist_after()
    budget_stop = False
    tally["holes"] = 0
    tally["lookback_capped"] = 0

    def take(item: dict[str, Any], qkey: dict[str, Any], works: list[dict[str, Any]]) -> tuple[int, int]:
        """Observe the query's ``works`` (cuts, identity, in-band abstract); ``(seen, kept)``."""
        label = item["query"]
        kept = 0
        dropped = 0
        for w in works:
            ident = work_identity(w)
            if ident is None:
                tally["works_unkeyable"] += 1
                continue  # neither DOI nor id: nothing to key on
            _, external_id = ident
            keys = work_keys(w)
            cut = work_cut(w, keys, item)
            if work_cut_bypassed(w, keys, item):  # D654 (counted also when `ai_term` then cuts it)
                tally["publisher_cut_bypassed"] += 1
                tally[f"publisher_cut_bypassed:{item['id']}"] = tally.get(f"publisher_cut_bypassed:{item['id']}", 0) + 1
            if work_venue_kept(w, keys, item):  # the Chinese-coverage work (counted also when `ai_term` then cuts it)
                tally["venue_kept"] += 1
                tally[f"venue_kept:{item['id']}"] = tally.get(f"venue_kept:{item['id']}", 0) + 1
            if cut is not None:  # a backfill cut (module docstring): not observed, not in `seen`
                dropped += 1
                tally[f"cut_{cut}"] += 1
                tally[f"cut_{cut}:{item['id']}"] = tally.get(f"cut_{cut}:{item['id']}", 0) + 1
                continue
            if c.prior(CHANNEL, external_id) is None and external_id not in observed:
                kept += 1
            observed.add(external_id)
            tally["arxiv_doi"] += any(k == "arxiv" for k, _ in keys)
            tally["no_doi"] += not work_doi(w)
            abstract = deinvert_abstract(w.get("abstract_inverted_index"))
            title = str(w.get("title") or "").strip() or None
            row = c.observe(
                CHANNEL, external_id, w, keys=keys,
                canonical_url=work_url(w, external_id),
                metadata={"title": title, "abstract": abstract,
                          "authors": metadata_authors(w), "published_at": work_pub_date(w),
                          "doc_type": doc_type_of(w)},
                skip_unchanged=True,
                # matched_by (D228): group id, or the institution's org slug
                query=qkey, query_id=label, label=item.get("org", item["id"]),
                provider_record=w,  # D522: the work as OpenAlex returned it
            )
            if row is not None and abstract:
                tally["abstracts_rebuilt"] += 1
                c.add_inband(row["source_id"], "abstract", text=abstract,
                             media_type="text/plain")
        return len(works) - dropped, kept

    def pull(item: dict[str, Any], qkey: dict[str, Any], filt: str, ok: tuple[str, str],
             fail: tuple[str, str], then: str | None = None) -> str:
        """One window of the query: cursor-pull ``filt``, observe, write ``ok`` (``[from, to]``)
        when complete, else ``fail`` (claims nothing). ``then`` (a look-back's, D582): the filter
        asked next in the same window when ``filt`` comes back capped (its hits are kept; counted
        ``lookback_capped``). Raises :class:`OpenAlexBudgetStop` (no row), :class:`RunHalted` (an
        uncharged row) and :class:`RunRefused` (a ``failed_permanent[provider_plan]`` row or an
        uncharged ``failed_transient[provider_auth]`` one)."""
        api_stats.pop("last_status", None)
        seen = kept = fetched = 0
        try:
            works, exhausted = fetch_group(filt, limit, api_stats, select=select_fields)
            if exhausted == "truncated" and then is not None:
                tally["lookback_capped"] += 1
                seen, kept = take(item, qkey, works)
                fetched = len(works)
                api_stats.pop("last_status", None)
                works, exhausted = fetch_group(then, limit, api_stats, select=select_fields)
        except RunHalted:
            # The day's budget is spent (or the provider refused, D583): bank THIS window
            # non-advancing (the harness writes failed_transient[rate_limit] with retry_after
            # uncharged, or the refusal's class) and stop; the queries not reached get no row,
            # so the next run picks them up.
            with c.window(CHANNEL, qkey, *fail):
                raise
        more_seen, more_kept = take(item, qkey, works)
        seen, kept, fetched = seen + more_seen, kept + more_kept, fetched + len(works)
        # A COMPLETE pass covered the window it claims (from the watermark, or the floor, to its
        # end; a look-back below the watermark is re-asked, not claimed); anything else claims
        # nothing, because cursor order is not date order.
        with c.window(CHANNEL, qkey, *(ok if exhausted == "complete" else fail)) as win:
            win.seen, win.kept = seen, kept
            if exhausted == "complete":
                win.complete()
            elif exhausted == "truncated":
                win.truncated()
            else:
                error_class, permanent = error_class_of(int(api_stats.get("last_status", 0)))
                win.fail(error_class, permanent=permanent)
        tally[f"queries_{exhausted}"] += 1
        tally["works_fetched"] += fetched
        tally["works_new"] += kept
        return exhausted

    with c.halts():
        for i, item in enumerate(plan):
            c.progress(i, len(plan))
            item_to, item_end = entry_window(item, to_date, today)
            if item_to is not None and item_to < floor:
                continue  # the entry's window ends before this run's floor (D654 (b)'s entry under a later `from`)
            qkey = coverage_query(item, item_to)
            # Passes (D226): a query whose `ok` window already reaches the window end (today for
            # an open-ended window: a pass is a day) and reaches back to the floor is
            # not asked again; a cap-`partial` one returns the same cursor prefix within
            # relist_after_h; a budget 429's retry_after holds the channel. `full` re-sweeps
            # covered queries (the hold stands).
            if full_resweep:
                if c.hold(CHANNEL):
                    continue
            elif not c.due(CHANNEL, qkey, until=item_end, relist_after=relist,
                           partial_due=False, floor=floor):
                continue
            tail_from = floor
            try:
                # Floor moved back: each hole [s, e] as a bounded publication window
                # (to_publication_date e − 1 day: e is covered), no look-back.
                for hs, he in ([] if full_resweep else c.holes(CHANNEL, qkey, floor)):
                    tally["holes"] += 1
                    h_from, h_to = hs.date().isoformat(), he.date().isoformat()
                    last_day = (he.date() - dt.timedelta(days=1)).isoformat()
                    got = pull(item, qkey, _filter_for(item, h_from, last_day), (h_from, h_to), (h_from, h_to))
                    if got != "complete":
                        tail_from = h_to  # the tail claims only from the covered range up
                # The tail. Resumed and open-ended (D582): asked from the look-back start
                # max(floor, watermark − lookback_days), written from the watermark; a capped
                # look-back is asked again from the watermark. Else from the floor (first pass,
                # `full`, a bounded window). A failed tail is written [tail_from, tail_from].
                ask_from, claim_from, then = tail_from, tail_from, None
                if item_to is None and not full_resweep:
                    mark = c.watermark(CHANNEL, qkey)
                    if mark is not None and mark.date().isoformat() >= item_end:
                        continue  # only a hole was due; the tail is covered through today
                    if mark is not None:
                        claim_from = max(tail_from, mark.date().isoformat())
                        ask_from = lookback_start(floor, claim_from, mark.date(), lookback)
                        if ask_from < claim_from:
                            then = _filter_for(item, claim_from, item_to)
                pull(item, qkey, _filter_for(item, ask_from, item_to), (claim_from, item_end),
                     (tail_from, tail_from), then)
            except OpenAlexBudgetStop:
                # A spend cap refused the next request: this window and the rest get no row and
                # are due next run (module docstring).
                budget_stop = True
                break
        if not budget_stop:
            c.progress(len(plan), len(plan))  # not reached after a halt

    remaining = _ratelimit_detail().get("remaining")
    if remaining is not None and str(remaining).isdigit():
        tally["ratelimit_remaining"] = int(remaining)
    c.counts.update(tally, groups_planned=len(group_plan), institutions_planned=len(inst_plan),
                    author_buckets_planned=len(author_plan),
                    author_ids_planned=sum(len(i["ids"]) for i in author_plan),
                    queries_planned=planned, queries_selected=int(sampling["selected"]),
                    query_limit=limit, page_retries=int(api_stats["retries"]),
                    full_resweep=int(full_resweep), lookback_days=lookback,
                    budget_exhausted=int(c.halted is not None
                                         and not isinstance(c.halted, (RunStopped, RunRefused))))
    return c.finish(scope_complete=not budget_stop)
