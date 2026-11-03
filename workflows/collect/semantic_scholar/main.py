"""``collect.semantic_scholar`` — the PHRASE-MATCH academic-search axis (10-design §5.1; the S2 half
of v1 ``lib/search_engine.py``, run by v1's ``search-other-apis`` stage).

One bulk-search query per concept group selected by the channel's ``concept_groups``. Every hit is
one ``sources`` row on the chain ``sources.semantic_scholar`` (channel ``semantic_scholar``),
carrying what the search response already held: ``metadata`` title / abstract / authors /
``published_at`` / ``doc_type``, and the ``abstract`` as an in-band ``abstract`` entry (§5.4, v3
D-INBAND1; minted through the harness). Every executed query writes ONE ``coverage`` row on
``coverage.semantic_scholar``. No content fetch, no LLM. I/O is the collector harness
(:mod:`stack.collect.base`); this module keeps v3's provider logic.

**Why this channel exists: the misuse axis.** S2's query language takes quoted phrases and an
explicit ``+`` (AND), so a ``misuse: true`` concept group can be intersected with the
``ai_context`` disjunction from channels.yaml — ``("bioweapon" | "pathogen") + ("large language
model" | …)``. ``collect.openalex`` cannot express that (``title_and_abstract.search`` token-ANDs,
so the intersection floods) and therefore **skips** the misuse groups, exactly as v1 did. Non-misuse
groups run here too, as a phrase-match second opinion on the OpenAlex sweep; gather merges the
overlap on keys. A ``needs_ai_context`` group (broad words of another field, "decision theory",
"game theory"; SR scope note (d) E1/E2) is intersected with ``ai_context`` exactly as a misuse group.

**Identity.** The row's ``external_id`` keeps v3's precedence: the bare arXiv id (``externalIds.ArXiv``
or a ``10.48550/arXiv.<id>`` DOI), else the DOI, else ``pmid:<id>``, else the S2 ``paperId``, all
in :mod:`stack.scholarly_records` normal forms (D103). The **arXiv bridge now lives in the keys**:
:func:`paper_keys` types ``arxiv`` (+ the bridged DataCite ``doi``), ``doi``, ``pmid`` and ``s2``
(the 40-hex ``paperId``; corpus ids are not keys, D103), so gather merges an S2 hit on a preprint
with the ``collect.arxiv_sweep`` source inside the paper group. v3 re-registered such hits under
channel ``arxiv``; on the harness that would mint a second ``source_id`` for an ``(arxiv, id)``
pair the arXiv chain owns (C32 looks up the own chain only), apply the arXiv channel's projection
to an S2 record and mis-rank it in gather's provider order — so every row stays
``semantic_scholar`` (as ``collect.openalex`` does).

**The key.** ``SEMANTIC_SCHOLAR_API_KEY`` (the environment, else the ``$STACK_ENV_FILE`` file;
:func:`api_key`, :mod:`stack.collect.provider_keys`) is sent as the ``x-api-key`` header when set
(counted ``s2_keyed``); v3's second name ``STACK_S2_API_KEY`` and its 403 key-degrade stay gone (a
refused key fails the query ``provider_auth``). Without it the sweep runs on the shared key-less
pool. Either way at the S2 channel's rate (D550): the cross-process Gate's interval is
``1 / politeness.rate_rps`` of the ``semantic_scholar`` channel (2.5 s;
:func:`stack.provider_transport.gate_interval`), and the transport's ``HostPacer`` runs at the same
rate. A 429 is waited out by the transport (:meth:`stack.collect.base.Transport.fetch`, ``wait_429``
= :data:`stack.collect.base.BACKOFF_WAITS`): its ``Retry-After``, else the Gate's backoff (5 s,
doubling to 60 s, reset by a 200, shared by every process), and re-sent; a page still 429'd after
the waits fails its query's window (``rate_limit``) and the sweep goes on. A ``Retry-After`` above
60 s halts the run (the queries not reached stay due; ``scope_complete = false``).
:func:`_get_page`'s 2/8/20 s ladder is for 5xx and transport failures.

**Incrementality mirrors collect.forums (D-SWEEP1).** S2 exposes no "indexed at" axis: the only
date field is publication date. Each query resumes from its watermark (the end of the newest
``ok`` coverage row, :meth:`Collector.watermark`), else from the floor, and every request sends the
floor or watermark as ``publicationDateOrYear=<eff_from>:`` (an open-ended lower bound), a first
pass included (D801; before it, a first pass whose floor was at or below the channel's ``from_date``
was sent no filter and paged every year of the phrase's history, keeping only the floor's). A
complete pass covers ``[eff_from, today]``, contiguous with prior ``ok`` windows. Two honesty
backstops (a server-side date filter is a silent no-op if the API changes): (1) a client-side floor
gate — a paper published before the floor's year is never observed whatever the server returned;
(2) honor-detection — any returned paper dated before the sent lower bound beyond a one-day slack
proves the filter was ignored, so the span claims from the floor instead (the response held
everything older too).

**Year spans (D801).** A tail that crosses 1 January of a recent year (a first pass from the
2015 floor, or a tail resumed after a span that failed) is asked as consecutive date spans, oldest
first, split on 1 January of each of the last ``split_years`` calendar years (option, default
:data:`DEFAULT_SPLIT_YEARS`: in 2026 ``[floor, 2023-01-01]``, 2023, 2024, 2025, ``[2026-01-01,
today]``; :func:`year_spans`). Only a split point strictly inside the range cuts it, so the weekly
tail stays one span (two in the week that crosses 1 January). A span ``[s, e]``
other than the last sends the bounded ``publicationDateOrYear=<s>:<e − 1 day>`` and is written as
its own coverage window ``[s, e]``; the last stays open above, and the look-back below (D551)
applies to the tail's first span only. A query's sequence stops at the first span that is not
``ok``: a later ``ok`` span would carry the watermark over it (:func:`stack.collect.base.
date_windows`), and since the watermark is the end of the last ``ok`` span the next run resumes
there rather than from page 1 (the bulk token is not persisted). The split points fall on 1
January because S2 does not document where a year-only paper falls in a range that starts
mid-year. A hole (below) is split the same way. A third backstop: a paper dated after a bounded
span's last day beyond the one-day slack proves the upper bound was ignored (counted
``upper_bound_ignored``), so that answer claims nothing. Asking the span again would get the same
answer (one record whose date moved, or whose year and date disagree, is enough), so the span is
asked again open above from the same lower bound (D802, counted ``upper_bound_fallback``): an
open-ended request does not rely on the upper bound, and when complete it banks ``[s, today]``,
which covers the query's later spans, holes and tail for this run. A fallback that is not complete
writes its own outcome over ``[s, s]`` (``error`` names the broken bound) and the sequence stops; a
span whose bound broke with no page of the run's budget left is ``failed_permanent[query_invalid]``
and the run a budget stop (``scope_complete = false``: the next run asks the fallback).
The scheduled cycle WARNs on these counts (``tools/scheduled_run.py`` ``s2_collect_note``).
Memory: one request's papers are held at a time (a span's look-back and fallback re-asks replace
them); the rows observed, ``Collector``'s buffer, accumulate over the whole run until
:meth:`~stack.collect.base.Collector.finish` emits them.

**Index lag (D551).** S2 adds a paper to its index days or weeks after its publication date (journal
papers often appear well after they are dated), and the only axis is that date: a paper dated
before a run's watermark and indexed after it lies below the next run's window. The tail is
therefore asked from ``max(floor, watermark − lookback_days)`` (option, default 30, as arXiv's
``announce_lag_days``, D405), and its window is still written from the watermark, so coverage stays
contiguous. A re-seen paper whose projected record did not change writes no row (``skip_unchanged``,
counted ``sources_unchanged``), so the overlap is idempotent. When the cap fills inside the
look-back, those hits are kept and the query is asked again from the watermark (counted
``lookback_capped``), so the look-back cannot keep a query capped. A paper S2 indexes more than
``lookback_days`` after its date is still missed; holes are asked without a look-back.

**A truncated pass banks nothing.** Bulk search returns papers in ``paperId`` order, not date
order, so a capped pull (``max_records``, or the run's ``max_pages`` budget) covered an unknown
subset: it is written ``partial`` over ``[from, from]`` (SHOULD-14), as is a failed one
(``failed_*``); neither moves the watermark, and their ``attempts`` accumulate on one key. Two
known recall caveats: a paper with NO publication date is dropped by S2's own
``publicationDateOrYear`` filter, which every request carries since D801 (D272 accepted the loss
for holes), and S2 withholds abstracts for a licence-dependent slice of its index (those rows carry no in-band entry;
counted ``abstract_absent``).

**The coverage query** is the JCS of ``{"query": "group:<id>", "s2_query": <the query text>}``:
the watermark is per ``query_digest``, so a changed term list starts a new key instead of
inheriting a watermark the old query earned (as ``collect.openalex``). The floor is not in the key
(round 5 (RUN) B7a): a floor moved back (2025-10-01 → 2024-11-30) leaves a hole below the covered
range (:meth:`stack.collect.base.Collector.holes`), asked first as the bounded
``publicationDateOrYear=<start>:<end − 1 day>`` (in year spans) and written ``[start, end]``; then
the usual tail.
Only the gap is pulled again.

**Backfill cuts** (channels.yaml ``keyword_cuts`` on the channel; Tomáš 2026-10-13, docs/design/collection-yield.md
§10), as in ``collect.openalex``: a hit is dropped, before it is observed, when :func:`stack.rules.keyword_cut`
names a cut: ``publisher`` (a publisher DOI, or none, and no arXiv id or arXiv ``venue``), ``self_publication``
(Zenodo and preprint-server DOIs; SSRN kept), ``ai_term`` (a group in ``ai_term_groups``; none configured on S2).
Counted per run ``cut_<name>`` and ``cut_<name>:<group id>``; a dropped hit is not in the window's ``seen``. The cut
set is part of the coverage key (``"cuts"``), so switching a cut off re-sweeps from the floor. The dated exception of
D654 (``keyword_cuts.publisher_cut_floor``: a pre-floor hit with a pre-2020 safety stem bypasses
the source cuts, :func:`stack.rules.publisher_cut_exempt`) is wired here as on OpenAlex, dated by
:func:`paper_pub_date`, and goes into the key as ``"bypass"``; channels.yaml enables it for this channel
(2.3.0, ``publisher_cut_floor: "2020-01-01"``), so every S2 group key changed and each query re-sweeps from the floor
once. The venue allowlist (``keyword_cuts.keep_venues``) as on OpenAlex: a hit with an ACL Anthology or
AAAI DOI is not a ``publisher`` work, counted ``venue_kept``, and the key carries ``"venues"``. With
``keyword_cuts.venue_ai_term`` (set on this channel: its group queries have no field filter) the allowlist keeps
only a hit with an AI term in title or abstract; one without stays a ``publisher`` work (counted
``venue_no_ai_term``), and the key carries ``"venue_ai_terms"`` (the vocabulary's digest). Cut 5 (scheming,
specs, control off on S2) is ``s2_terms: []`` in channels.yaml: those queries are not asked.

Rows: written only when the record's stable projection moved (``skip_unchanged``; channels.yaml
``record_projection`` leaves the counter ``citationCount`` out, C23); ``kept`` = papers new to the
own chain.

Options (plan stage line ``options:``, D72; the v3 env var wins when set):
  * ``max_records`` / ``STACK_COLLECT_LIMIT``       — result cap of each request sequence, so of
                                                      each span, not of the query (D802): a first
                                                      pass in year spans may fetch ``split_years +
                                                      1`` times this, more with a look-back or a
                                                      fallback re-ask (default: the channel's
                                                      ``breadth`` preset ``max_results_per_query``)
  * ``max_pages``                                   — page budget for the whole run (0 = none). The
                                                      query that spends it is ``partial``; queries
                                                      not reached get no row and the run ends
                                                      ``scope_complete = false`` (the live smoke:
                                                      ``max_pages: 1``)
  * ``max_queries`` / ``STACK_COLLECT_MAX_QUERIES`` — cap on groups swept this run (0 = all)
  * ``queries``                                     — only these labels (``group:<id>``), before
                                                      sampling (as ``collect.openalex``)
  * ``fraction``    / ``STACK_COLLECT_FRACTION``    — stable hash-selected fraction of the groups
  * ``from``        / ``STACK_S2_FROM``             — floor from-date (default channel ``from_date``;
                                                      sent as the first pass's lower bound, D801)
  * ``full``        / ``STACK_S2_FULL``             — '1' ignores watermarks, re-sweeps from the floor
  * ``lookback_days``                               — days below the watermark the tail re-asks
                                                      (index lag, D551; default 30, 0 = none)
  * ``split_years``                                 — a tail or hole is split on 1 January of each
                                                      of the last N calendar years (D801; default
                                                      4, 0 = one span)
  * ``relist_after_h``                              — passes (D226, :meth:`stack.collect.base.
                                                      Collector.due`): a query covered through today
                                                      is skipped; a ``partial`` one is not asked
                                                      again within this many hours (12)

Not carried from v3's ``collect.meta`` payload (``sources.metadata`` has no slot and the raw record
is not stored): ``venue``, ``citations``, ``pmid``/``doi`` as fields (they are keys), ``oa_pdf``
(acquire resolves open access itself), v3 coverage ``topic_keys`` (derivable from the label and
channels.yaml).

Cardinal rule: imports only ``stack.*`` and the stdlib; nothing from other workflows.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import time
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from stack import rules
from stack.collect.base import BACKOFF_WAITS, RELIST_OPTIONS, Collector, Pacing, Transport
from stack.collect.provider_keys import s2_key
from stack.collect.sampling import (
    ENV_FRACTION,
    ENV_MAX_QUERIES,
    SAMPLING_OPTIONS,
    select,
)
from stack.config import ChannelDef, StackConfig
from stack.provider_transport import gate_interval
from stack.scholarly_records import (
    keys_for,
    normalize_arxiv,
    normalize_doi,
    normalize_pmid,
    normalize_s2_id,
)
from stack.stage_runner import StageContext, StageResult

#: The ``options`` keys this stage's code reads; any other is refused (D434)
OPTIONS: frozenset[str] = frozenset({"from", "full", "lookback_days", "max_pages", "max_records", "queries",
                                     "split_years"}) | SAMPLING_OPTIONS | RELIST_OPTIONS

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────
CHANNEL = "semantic_scholar"
DEFAULT_FROM = "2020-01-01"
API_BASE = "https://api.semanticscholar.org/graph/v1/paper/search/bulk"
# Bulk search returns up to 1000 papers per call and takes no page-size parameter; `token` carries
# the continuation. PAGE_MAX is therefore documentation of the server's behaviour, not a request.
PAGE_MAX = 1000
DELAY_S = 2.5                        # the channel's rate (D550); run() takes it from gate_interval("s2")
RETRY_DELAYS_S = (2.0, 8.0, 20.0)    # 5xx / transport-failure ladder before giving up on a page
HALT_429_AFTER_S = 60.0              # a 429 asking longer halts the run (as collect.s2-lookup)

# The fields the sweep actually reads. Every one is used:
#   externalIds     -> external id and keys (ArXiv / DOI / PubMed);
#   abstract        -> metadata.abstract and the in-band abstract entry (D-INBAND1);
#   authors         -> author display names (S2 carries NO affiliation data on this endpoint);
#   year/publicationDate -> published_at and the floor gate;
#   publicationTypes-> doc_type;
#   venue, citationCount, openAccessPdf -> not carried (module docstring), but `venue` and
#                      `openAccessPdf` are in channels.yaml's record_projection, so a change moves
#                      the record digest; kept in the request so the projection stays meaningful.
FIELDS = (
    "paperId,externalIds,title,abstract,authors,venue,year,publicationDate,"
    "citationCount,publicationTypes,openAccessPdf"
)

BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0 Safari/537.36 stack-collect/1.0 (+research)"
)

# The transport (D26, D550): the channel's interval is the pacer's rate (run() sets it); the transport
# waits 429s out (`wait_429`, the Gate's backoff) and halts on one asking > 60 s; the 5xx ladder stays
# in `_get_page` (over the `http_get_json` seam).
PACING = Pacing(rps=1.0 / DELAY_S, retry_delays_s=(), halt_429_after_s=HALT_429_AFTER_S,
                wait_429=BACKOFF_WAITS, headers={"User-Agent": BROWSER_UA})

_ENV_LIMIT = "STACK_COLLECT_LIMIT"
_ENV_FROM = "STACK_S2_FROM"
_ENV_FULL = "STACK_S2_FULL"
_DEFAULT_LIMIT = 1000
#: Default ``lookback_days`` (D551): S2 indexes journal papers weeks after their publication date.
DEFAULT_LOOKBACK_DAYS = 30
#: Default ``split_years`` (D801): a tail or hole is split on 1 January of each of the last this many
#: calendar years (module docstring, "Year spans"); the years before them are one span.
DEFAULT_SPLIT_YEARS = 4

_ISO_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})")

# One day of slack for honor-detection: a paper dated within a day of a sent bound is treated as
# on-window (date-granularity/timezone edges), so only a genuinely older (or, for a bounded span,
# newer) hit — proof the server returned unfiltered results — trips the backstop. Same rule as
# collect.forums.
_FILTER_SLACK_DAYS = 1

# S2 publicationTypes -> the doc_type vocabulary. Anything unlisted stays `other` for
# annotate.doc_class to settle rather than being guessed here.
_PAPER_TYPES = frozenset({"JournalArticle", "Conference", "Review", "Book", "BookSection"})


# ─────────────────────────────────────────────────────────────────────────────
# HTTP (module-level seams: tests monkeypatch `http_get_json` / `_sleep`)
# ─────────────────────────────────────────────────────────────────────────────
#: The run's paced transport (set by :func:`run`); ``http_get_json`` goes through it.
_T: Transport | None = None


def http_get_json(
    url: str, params: dict[str, Any], headers: dict[str, str], *, timeout: float = 60.0
) -> tuple[int, Any]:
    """One paced GET through the run's transport (429s waited out, D550), returning ``(status,
    decoded_json_or_None)``. A transport failure comes back as ``(0, None)`` and an unparseable
    body as ``(status, None)`` so the caller records an honest failed window instead of crashing
    the sweep. ``timeout`` is kept for the seam's signature; the transport's ``PACING.timeout_s``
    applies."""
    if _T is None:
        raise RuntimeError("semantic_scholar.http_get_json: no transport outside run(ctx)")
    return _T.fetch_json("GET", url, params=params, headers=headers)


def _sleep(seconds: float) -> None:
    """Retry delays (module-level so offline tests monkeypatch it to a no-op); the transport's
    pacer sleeps through it too."""
    time.sleep(seconds)


def error_class_of(status: int) -> tuple[str, bool]:
    """The coverage ``(error_class, permanent)`` of the status that failed a page: 429 →
    ``rate_limit``, 5xx → ``http_5xx``, 401/403 → ``provider_auth`` (key-less: S2 refusing the
    shared pool), 400 → ``query_invalid`` (permanent: S2 rejected the query), a transport failure
    or a 200 with an unparseable body → ``network``."""
    if status == 429:
        return "rate_limit", False
    if 500 <= status < 600:
        return "http_5xx", False
    if status in (401, 403):
        return "provider_auth", False
    if status == 400:
        return "query_invalid", True
    return "network", False


def _get_page(
    params: dict[str, Any], headers: dict[str, str], stats: dict[str, Any] | None = None
) -> Any | None:
    """One page WITH the ladder on 5xx and transport failures. Returns the decoded body, or
    ``None`` once the retries are spent — which the caller turns into a failed window rather than a
    silent short page; ``stats['last_status']`` then holds the status that ended it (``0`` =
    transport failure), see :func:`error_class_of`. A 429 is not laddered: the transport already
    waited it out (D550), so one that comes back ends the page (``rate_limit``).

    Passing ``stats`` makes the otherwise-silent failures visible — each failed response is
    classified (``http_429`` / ``http_5xx`` / ``timeouts``) and every actual retry bumps
    ``retries``. A 4xx is not retried.
    """
    for delay in (*RETRY_DELAYS_S, None):
        status, data = http_get_json(API_BASE, params, headers)
        if status == 200 and isinstance(data, dict):
            return data
        if stats is not None:
            stats["last_status"] = status
            if status == 429:
                stats["http_429"] = stats.get("http_429", 0) + 1
            elif 500 <= status < 600:
                stats["http_5xx"] = stats.get("http_5xx", 0) + 1
            elif status == 0:
                stats["timeouts"] = stats.get("timeouts", 0) + 1
        retryable = status == 0 or 500 <= status < 600  # D550: the transport waited the 429s
        if not retryable or delay is None:
            return None
        if stats is not None:
            stats["retries"] = stats.get("retries", 0) + 1
        _sleep(delay)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Query construction — pure, unit-testable
# ─────────────────────────────────────────────────────────────────────────────
def _phrase_or(terms: list[str]) -> str:
    """The terms as quoted phrases OR'd with S2's ``|``. A term containing a ``"`` would break out
    of the quoting and silently change the query's meaning, so such terms are dropped (they cannot
    be expressed on this backend) — the same call ``collect.openalex`` makes about commas."""
    safe = [t.strip() for t in terms if t.strip() and '"' not in t]
    return " | ".join(f'"{t}"' for t in safe)


def group_query(terms: list[str], ai_context: list[str], misuse: bool) -> str:
    """The S2 bulk-search ``query`` for one concept group (v1 ``_s2_query``).

    Quoted phrases OR'd with ``|``; a ``misuse`` group is additionally ANDed (``+``) with the
    ``ai_context`` disjunction, because a dangerous-capability term alone returns the whole
    biosecurity/chemistry/cyber literature.

    When ``misuse`` is set but ``ai_context`` is empty the group is returned UNINTERSECTED — v1's
    behaviour, kept so this function stays a pure rendering of its inputs. The plan-level guard in
    :func:`plan_groups` is what keeps an unconfigured ``ai_context`` from flooding the corpus.
    """
    joined = _phrase_or(terms)
    ctx = _phrase_or(ai_context)
    if misuse and ctx:
        return f"({joined}) + ({ctx})"
    return joined


def search_params(query: str, eff_from: str | None, to: str | None = None) -> dict[str, Any]:
    """The bulk-search query string params. ``eff_from``, when given, is sent as the
    ``publicationDateOrYear=<eff_from>:<to>`` range (inclusive; S2 accepts either end as empty):
    open-ended above for the tail's last span, ``to`` the last day of a bounded span. The run
    always gives ``eff_from`` (D801); ``None`` sends no date filter."""
    params: dict[str, Any] = {"query": query, "fields": FIELDS}
    if eff_from:
        params["publicationDateOrYear"] = f"{eff_from}:{to or ''}"  # `to`: a hole's last day
    return params


def fetch_group(
    query: str,
    eff_from: str | None,
    limit: int,
    headers: dict[str, str],
    stats: dict[str, Any] | None = None,
    *,
    on_page: Callable[[int], None] | None = None,
    max_pages: int | None = None,
    to: str | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Token-paginate one group query up to ``limit`` papers and at most ``max_pages`` pages.

    Returns ``(papers, exhausted)`` with ``exhausted`` ∈ {complete, truncated, error}. Like the
    OpenAlex sweep and unlike the arXiv one this cannot bank a partial window on truncation: bulk
    results come back in ``paperId`` order, so a capped pull covered an unknown subset. Each page
    that arrived bumps ``stats['pages_fetched']`` (the run's page budget).

    ``on_page`` (when given) is called after each page with the running paper total.
    """
    papers: list[dict[str, Any]] = []
    params = search_params(query, eff_from, to)
    pages = 0
    while True:
        if len(papers) >= limit:
            return papers[:limit], "truncated"
        if max_pages is not None and pages >= max_pages:
            return papers, "truncated"
        data = _get_page(params, headers, stats)
        if data is None:
            return papers, "error"
        pages += 1
        if stats is not None:
            stats["pages_fetched"] = stats.get("pages_fetched", 0) + 1
        batch = data.get("data") or []
        if not batch:
            return papers, "complete"
        papers.extend(p for p in batch if isinstance(p, dict))
        if on_page is not None:
            on_page(len(papers))
        token = str(data.get("token") or "")
        if not token:
            return papers[:limit], "complete"
        params["token"] = token


# ─────────────────────────────────────────────────────────────────────────────
# Paper normalisation + identity
# ─────────────────────────────────────────────────────────────────────────────
def _external_ids(paper: dict[str, Any]) -> dict[str, Any]:
    ids = paper.get("externalIds")
    return ids if isinstance(ids, dict) else {}


def arxiv_id_of(paper: dict[str, Any]) -> str:
    """The bare arXiv id (D103 normal form) this paper is an arXiv deposit of, else ''.

    Prefers ``externalIds.ArXiv`` (S2 states it directly) and falls back to an arXiv DataCite
    ``10.48550/arXiv.<id>`` DOI, so the bridge fires on either shape.
    """
    ext = _external_ids(paper)
    return normalize_arxiv(str(ext.get("ArXiv") or "")) or normalize_arxiv(str(ext.get("DOI") or ""))


def paper_doi(paper: dict[str, Any]) -> str:
    """The paper's DOI in the ``doi`` key's normal form (D103), '' when absent or malformed."""
    return normalize_doi(str(_external_ids(paper).get("DOI") or ""))


def _pubmed_id(paper: dict[str, Any]) -> str:
    """The PubMed id in normal form, '' when absent. S2 sometimes ships the literal ``"None"``
    (v1 had the same scar), which is not an id."""
    return normalize_pmid(str(_external_ids(paper).get("PubMed") or ""))


def paper_identity(paper: dict[str, Any]) -> tuple[str, str] | None:
    """The ``(channel, external_id)`` a paper is registered under, or ``None`` when unkeyable.

    Always channel ``semantic_scholar`` (module docstring); the external id keeps v3's precedence:
    the bare arXiv id, else the normalized DOI, else ``pmid:<id>`` (the fallback channels.yaml
    documents for this channel), else the S2 ``paperId``, so an otherwise-unkeyable hit is still
    discovered; ``None`` only when S2 returned no id at all.
    """
    arxiv = arxiv_id_of(paper)
    if arxiv:
        return CHANNEL, arxiv
    doi = paper_doi(paper)
    if doi:
        return CHANNEL, doi
    pmid = _pubmed_id(paper)
    if pmid:
        return CHANNEL, f"pmid:{pmid}"
    paper_id = str(paper.get("paperId") or "").strip()
    return (CHANNEL, paper_id) if paper_id else None


def paper_keys(paper: dict[str, Any]) -> list[tuple[str, str]]:
    """The paper's typed keys as ``(kind, value)`` pairs (:func:`stack.scholarly_records.keys_for`):
    ``arxiv`` (+ the bridged DataCite ``doi``), ``doi``, ``pmid`` and ``s2`` (the 40-hex
    ``paperId``). Junk values give no key; ``CorpusId`` is not a key (D103)."""
    ext = _external_ids(paper)
    pairs = [("arxiv", ext.get("ArXiv")), ("doi", ext.get("DOI")), ("pmid", ext.get("PubMed")),
             ("s2", paper.get("paperId"))]
    return [(k["kind"], k["value"]) for k in keys_for([(kind, str(v)) for kind, v in pairs if v])]


def paper_cut(paper: dict[str, Any], item: dict[str, Any]) -> str | None:
    """The backfill cut that drops this hit of plan entry ``item`` (module docstring), else ``None``."""
    if not item.get("cuts"):
        return None
    text = f"{paper.get('title') or ''} {paper.get('abstract') or ''}"
    venue = str(paper.get("venue") or "").strip().lower()
    return rules.keyword_cut(paper_keys(paper), text, drop_sources=item["drop_sources"], ai_term=item["ai_term"],
                             arxiv_located=venue in rules.S2_ARXIV_VENUES, published=paper_pub_date(paper),
                             publisher_cut_floor=item.get("publisher_cut_floor"),
                             keep_venues=bool(item.get("keep_venues")), venue_ai_term=bool(item.get("venue_ai_term")))


def paper_venue_kept(paper: dict[str, Any], item: dict[str, Any]) -> bool:
    """Whether the venue allowlist (the Chinese-coverage work) keeps this hit from the ``publisher`` cut (counted ``venue_kept``)."""
    if not item.get("keep_venues"):
        return False
    text = f"{paper.get('title') or ''} {paper.get('abstract') or ''}"
    venue = str(paper.get("venue") or "").strip().lower()
    return (rules.source_class(paper_keys(paper), arxiv_located=venue in rules.S2_ARXIV_VENUES,
                               keep_venues=True) == "venue"
            and rules.venue_allowlist_applies(text, keep_venues=True, venue_ai_term=bool(item.get("venue_ai_term"))))


def paper_venue_unguarded(paper: dict[str, Any], item: dict[str, Any]) -> bool:
    """Whether this is an allowlisted venue's hit that ``venue_ai_term`` leaves to the ``publisher`` cut (no AI
    term): counted ``venue_no_ai_term``."""
    if not (item.get("keep_venues") and item.get("venue_ai_term")):
        return False
    venue = str(paper.get("venue") or "").strip().lower()
    return (rules.source_class(paper_keys(paper), arxiv_located=venue in rules.S2_ARXIV_VENUES,
                               keep_venues=True) == "venue" and not paper_venue_kept(paper, item))


def paper_url(paper: dict[str, Any], external_id: str) -> str:
    """The item's canonical locator — arXiv abs page / doi.org resolver / S2 landing page."""
    arxiv = arxiv_id_of(paper)
    if arxiv:
        return f"https://arxiv.org/abs/{arxiv}"
    doi = paper_doi(paper)
    if doi:
        return f"https://doi.org/{doi}"
    paper_id = normalize_s2_id(str(paper.get("paperId") or "")) or str(paper.get("paperId") or "")
    return f"https://www.semanticscholar.org/paper/{paper_id or external_id}"


def doc_type_of(paper: dict[str, Any]) -> str:
    """``publicationTypes`` → the doc_type vocabulary.

    An arXiv deposit is a ``preprint`` whatever else S2 says (S2 frequently types an arXiv paper
    as ``JournalArticle`` once a venue is attached, but the thing this channel discovered and that
    acquire will fetch is the preprint). Otherwise the first recognised scholarly type wins, and
    anything unrecognised stays ``other`` for annotate.doc_class to settle.
    """
    types = paper.get("publicationTypes")
    listed = [str(t) for t in types] if isinstance(types, list) else []
    if "Preprint" in listed or arxiv_id_of(paper):
        return "preprint"
    if any(t in _PAPER_TYPES for t in listed):
        return "paper"
    if "Dataset" in listed:
        return "dataset"
    return "other"


def paper_authors(paper: dict[str, Any]) -> list[str]:
    """Author display names in S2's own order (authorship order is meaningful). S2 carries no
    affiliation data on this endpoint."""
    out: list[str] = []
    for a in paper.get("authors") or []:
        if not isinstance(a, dict):
            continue
        name = str(a.get("name") or "").strip()
        if name:
            out.append(name)
    return out


def paper_pub_date(paper: dict[str, Any]) -> str | None:
    """ISO ``YYYY-MM-DD`` publication date, else ``None``. ``publicationDate`` when S2 has it;
    otherwise a bare ``year`` is widened to ``<year>-01-01`` so a year-only paper still carries a
    usable date (and is still gated correctly by the floor, which compares on year)."""
    m = _ISO_DATE.match(str(paper.get("publicationDate") or "").strip())
    if m:
        return m.group(1)
    year = paper.get("year")
    if isinstance(year, int) and not isinstance(year, bool) and 1000 <= year <= 9999:
        return f"{year:04d}-01-01"
    return None


def paper_year(paper: dict[str, Any]) -> int | None:
    date = paper_pub_date(paper)
    return int(date[:4]) if date else None


# ─────────────────────────────────────────────────────────────────────────────
# Coverage window helpers
# ─────────────────────────────────────────────────────────────────────────────
def year_spans(start: dt.date, end: dt.date, split_years: int, this_year: int
               ) -> list[tuple[dt.date, dt.date]]:
    """``[start, end]`` as consecutive ascending spans split on 1 January of each of the last
    ``split_years`` calendar years up to ``this_year`` (module docstring, "Year spans"). Only a
    split point strictly inside the range cuts it, so a range inside one year is the one span
    ``[(start, end)]``, as is any range with ``split_years <= 0``. Fixed calendar dates, not a day
    count, so the boundaries do not drift with leap years."""
    if start > end:
        raise ValueError(f"date range {start}..{end}: start after end")
    cuts = [dt.date(y, 1, 1) for y in range(this_year - split_years + 1, this_year + 1)
            if start < dt.date(y, 1, 1) < end]
    bounds = [start, *cuts, end]
    return list(zip(bounds, bounds[1:]))


def _upper_ignored(papers: list[dict[str, Any]], to: str) -> str | None:
    """The newest publication date among ``papers`` that lies after a bounded span's last day
    ``to`` beyond ``_FILTER_SLACK_DAYS`` (proof the server ignored the upper bound; module
    docstring, "Year spans"), else ``None``. Undated papers prove nothing."""
    threshold = dt.date.fromisoformat(to).toordinal() + _FILTER_SLACK_DAYS
    late = [d for d in (paper_pub_date(p) for p in papers)
            if d and dt.date.fromisoformat(d).toordinal() > threshold]
    return max(late) if late else None


def _filter_ignored(papers: list[dict[str, Any]], eff_from: str) -> bool:
    """True if the server ignored the ``publicationDateOrYear`` lower bound (the honesty backstop):
    any returned paper dated before ``eff_from`` beyond ``_FILTER_SLACK_DAYS`` means unfiltered
    results came back (everything older included), so the caller claims the span from the floor
    rather than from ``eff_from``.

    Papers with no date at all are ignored here — an absent date is not evidence either way.
    """
    threshold = dt.date.fromisoformat(eff_from).toordinal() - _FILTER_SLACK_DAYS
    for p in papers:
        date = paper_pub_date(p)
        if date and dt.date.fromisoformat(date).toordinal() < threshold:
            return True
    return False


def coverage_query(item: dict[str, Any]) -> dict[str, Any]:
    """The coverage ``query`` (module docstring): label and query text, so a changed term list
    starts a new watermark key; the floor is not in it (a floor moved back is a hole)."""
    key = {"query": item["query"], "s2_query": item["s2_query"]}
    if item.get("cuts"):
        key["cuts"] = item["cuts"]  # a cut switched off is a new key (module docstring)
    if item.get("publisher_cut_floor"):  # D654 (a), as collect.openalex: the floor and the stem set
        key["bypass"] = {"floor": item["publisher_cut_floor"], "stems": rules.pre2020_stem_digest()}
    if item.get("keep_venues"):  # the Chinese-coverage work, as collect.openalex: the venue allowlist
        key["venues"] = rules.venue_allowlist_digest()
    if item.get("venue_ai_term"):  # the allowlist keeps only AI-term hits: the vocabulary is in the key
        key["venue_ai_terms"] = rules.ai_term_digest()
    if item.get("ai_term"):  # D751, as collect.openalex (no S2 group has the cut today)
        key["ai_terms"] = rules.ai_term_digest()
    return key


# ─────────────────────────────────────────────────────────────────────────────
# The plan
# ─────────────────────────────────────────────────────────────────────────────
def _selected_group_ids(chan: ChannelDef, all_ids: list[str]) -> list[str]:
    """Resolve a channel's ``concept_groups`` selection ('all' | list | None) to ordered ids.

    Deliberately a local copy of ``collect.forums``' helper: the cardinal rule forbids importing
    from another workflow, and this channel is the one that will carry v1's per-source group subset
    (``sources.europepmc``-style) when it lands on the channel entry.
    """
    sel = chan.concept_groups
    if sel == "all" or sel is None:
        return list(all_ids)
    if isinstance(sel, list):
        wanted = set(sel)
        return [g for g in all_ids if g in wanted]
    return []


def plan_groups(config: StackConfig) -> list[dict[str, Any]]:
    """The ordered sweep plan: one entry per selected concept group with a usable query.

    UNLIKE ``collect.openalex`` this keeps the ``misuse`` groups — intersecting them with
    ``ai_context`` is the whole reason this channel exists. The one case a misuse group IS skipped
    is an empty/unconfigured ``ai_context``: an unintersected ``"bioweapon" | "pathogen"`` query
    would pull the entire biosecurity literature into the corpus, which is worse than not running
    it, and the coverage row would claim a window this channel never meaningfully covered. A
    ``needs_ai_context`` group is treated the same way (intersected, or skipped without
    ``ai_context``). A group's ``s2_terms`` replace its ``terms`` here; an empty list skips it
    (S2 stems a quoted word: "steerability" returned "steer" papers, D229).
    """
    chan = config.channels.channel(CHANNEL)
    groups = {g.id: g for g in config.channels.concept_groups}
    order = [g.id for g in config.channels.concept_groups]
    ai_context = list(config.channels.ai_context)
    drop_sources = sorted(chan.keyword_cuts.drop_sources) if chan.keyword_cuts else []
    ai_term_groups = set(chan.keyword_cuts.ai_term_groups) if chan.keyword_cuts else set()
    cut_floor = chan.keyword_cuts.publisher_cut_floor if chan.keyword_cuts and drop_sources else None
    keep_venues = bool(chan.keyword_cuts and chan.keyword_cuts.keep_venues and "publisher" in drop_sources)
    venue_ai_term = bool(keep_venues and chan.keyword_cuts and chan.keyword_cuts.venue_ai_term)
    plan: list[dict[str, Any]] = []
    for gid in _selected_group_ids(chan, order):
        g = groups[gid]
        intersect = g.misuse or g.needs_ai_context
        if intersect and not ai_context:
            continue  # see docstring: unintersected misuse terms are a flood, not recall
        terms = g.terms_for_s2()  # `s2_terms` REPLACES `terms` here; empty = skip (D229)
        if not terms:
            continue
        query = group_query(terms, ai_context, intersect)
        if not query:
            continue
        ai_term = gid in ai_term_groups
        plan.append({"id": gid, "query": f"group:{gid}", "s2_query": query, "misuse": g.misuse,
                     "needs_ai_context": g.needs_ai_context, "drop_sources": drop_sources, "ai_term": ai_term,
                     "cuts": sorted([*drop_sources, *(["ai_term"] if ai_term else [])]),
                     "publisher_cut_floor": cut_floor, "keep_venues": keep_venues, "venue_ai_term": venue_ai_term})
    return plan


def only_queries(plan: list[dict[str, Any]], only: Any) -> list[dict[str, Any]]:
    """The ``queries`` option (as ``collect.openalex``): keep the plan entries whose label
    (``group:<id>``) is listed, in plan order; ``None``/empty keeps all. A label not in the plan
    (unknown group, or a group the plan skipped) is a ``ValueError``."""
    if not only:
        return plan
    wanted = [str(q) for q in ([only] if isinstance(only, str) else only)]
    unknown = sorted(set(wanted) - {item["query"] for item in plan})
    if unknown:
        raise ValueError(f"option queries: {unknown} are not configured query labels")
    return [item for item in plan if item["query"] in wanted]


# ─────────────────────────────────────────────────────────────────────────────
# The stage
# ─────────────────────────────────────────────────────────────────────────────
def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes")


def api_key() -> str | None:
    """``SEMANTIC_SCHOLAR_API_KEY`` (module docstring; also what ``tools/probe_*.py`` send)."""
    return s2_key()


def run(ctx: StageContext) -> StageResult:
    global _T
    from stack.config import load

    config = load()  # slow (seconds): loaded once and handed to the harness
    c = Collector(ctx, channels={ch.id: ch for ch in config.channels.channels})
    chan = c.channel(CHANNEL)
    today = c.now.date().isoformat()
    this_year = c.now.year

    floor_from = dt.date.fromisoformat(
        str(c.option("from", chan.from_date or DEFAULT_FROM, env=_ENV_FROM, cast=str)).strip()[:10]
    ).isoformat()
    floor_year = int(floor_from[:4])
    configured = config.channels.max_results_per_query(CHANNEL, _DEFAULT_LIMIT)
    limit = max(1, c.option("max_records", configured, env=_ENV_LIMIT, cast=int))
    page_budget = max(0, c.option("max_pages", 0, cast=int)) or None
    full_resweep = _truthy(c.option("full", "", env=_ENV_FULL, cast=str))
    lookback = c.option("lookback_days", DEFAULT_LOOKBACK_DAYS, cast=int)
    if lookback < 0:
        raise ValueError(f"option lookback_days: {lookback} < 0")
    split_years = c.option("split_years", DEFAULT_SPLIT_YEARS, cast=int)
    if split_years < 0:
        raise ValueError(f"option split_years: {split_years} < 0")

    full_plan = only_queries(plan_groups(config), c.option("queries", None))
    planned = len(full_plan)
    misuse_planned = sum(1 for item in full_plan if item["misuse"])
    # Scale control: plan options feed sampling.select's env knobs (a set env var wins).
    env = dict(os.environ)
    for name, var in (("fraction", ENV_FRACTION), ("max_queries", ENV_MAX_QUERIES)):
        value = c.option(name, None, env=var, cast=str)
        if value is not None:
            env[var] = value
    plan, sampling = select(full_plan, lambda item: f"{CHANNEL}:{item['query']}", env=env)

    interval = gate_interval("s2")  # the channel's rate (D550); raises on an unreadable channels.yaml
    assert interval is not None
    _T = c.transport(replace(PACING, rps=1.0 / interval), sleep=lambda s: _sleep(s))
    key = api_key()
    headers: dict[str, str] = {"x-api-key": key} if key else {}  # module docstring
    c.counts["s2_keyed"] = int(bool(key))
    api_stats: dict[str, Any] = {"retries": 0, "http_429": 0, "http_5xx": 0, "timeouts": 0,
                                 "pages_fetched": 0}
    tally = {"queries_complete": 0, "queries_truncated": 0, "queries_error": 0, "spans": 0,
             "filter_ignored": 0, "upper_bound_ignored": 0, "upper_bound_fallback": 0, "papers_fetched": 0,
             "papers_new": 0, "below_floor": 0,
             "unkeyable": 0, "arxiv_bridged": 0, "pmid_keyed": 0, "s2_id_keyed": 0,
             "abstract_absent": 0, "lookback_capped": 0}
    for name in rules.KEYWORD_CUTS:
        tally[f"cut_{name}"] = 0
    observed: set[str] = set()
    budget_stopped = False
    relist = c.relist_after()
    tally["holes"] = 0

    def take(item: dict[str, Any], qkey: dict[str, Any], papers: list[dict[str, Any]]
             ) -> tuple[int, int]:
        """The papers through the floor gate into rows; ``(seen, kept)``."""
        kept = 0
        seen = 0
        for p in papers:
            # Client-side floor gate (backstop 1): a paper published before the floor is never
            # observed, whatever the server returned. An undated paper cannot be proven sub-floor,
            # so it is kept — the same call collect.forums makes.
            year = paper_year(p)
            if year is not None and year < floor_year:
                tally["below_floor"] += 1
                continue
            cut = paper_cut(p, item)
            if paper_venue_kept(p, item):  # the Chinese-coverage work (counted also when `ai_term` then cuts it)
                tally["venue_kept"] = tally.get("venue_kept", 0) + 1
                tally[f"venue_kept:{item['id']}"] = tally.get(f"venue_kept:{item['id']}", 0) + 1
            elif paper_venue_unguarded(p, item):  # an allowlisted venue's hit without an AI term
                tally["venue_no_ai_term"] = tally.get("venue_no_ai_term", 0) + 1
                tally[f"venue_no_ai_term:{item['id']}"] = tally.get(f"venue_no_ai_term:{item['id']}", 0) + 1
            if cut is not None:  # a backfill cut (module docstring): not observed, not in `seen`
                tally[f"cut_{cut}"] += 1
                tally[f"cut_{cut}:{item['id']}"] = tally.get(f"cut_{cut}:{item['id']}", 0) + 1
                continue
            seen += 1
            ident = paper_identity(p)
            if ident is None:
                tally["unkeyable"] += 1
                continue  # no arXiv id, DOI, pmid or paperId: nothing to key on
            _, external_id = ident
            if arxiv_id_of(p):
                tally["arxiv_bridged"] += 1
            elif external_id.startswith("pmid:"):
                tally["pmid_keyed"] += 1
            elif not external_id.startswith("10."):
                tally["s2_id_keyed"] += 1
            if c.prior(CHANNEL, external_id) is None and external_id not in observed:
                kept += 1
            observed.add(external_id)
            title = str(p.get("title") or "").strip() or None
            abstract = str(p.get("abstract") or "").strip() or None
            row = c.observe(
                CHANNEL, external_id, p, keys=paper_keys(p),
                canonical_url=paper_url(p, external_id),
                metadata={"title": title, "abstract": abstract,
                          "authors": paper_authors(p) or None, "published_at": paper_pub_date(p),
                          "doc_type": doc_type_of(p)},
                skip_unchanged=True,
                query=qkey, query_id=item["query"], label=item["id"],  # matched_by (D228)
                provider_record=p,  # D522: the paper as S2 returned it
            )
            if row is None:
                continue
            if abstract:
                c.add_inband(row["source_id"], "abstract", text=abstract, media_type="text/plain")
            else:
                tally["abstract_absent"] += 1

        return seen, kept

    def close(w: Any, exhausted: str) -> None:
        if exhausted == "complete":
            w.complete()
        elif exhausted == "truncated":
            w.truncated()
        else:
            error_class, permanent = error_class_of(int(api_stats.get("last_status", 0)))
            w.fail(error_class, permanent=permanent)

    def pages_left() -> int | None:
        """The run's page budget left (``max_pages``), ``None`` without one."""
        return None if page_budget is None else max(0, page_budget - int(api_stats["pages_fetched"]))

    def pull(item: dict[str, Any], qkey: dict[str, Any], start: str, ask_from: str, to: str | None
             ) -> tuple[list[dict[str, Any]], str, int, int, int, str, str | None]:
        """One request sequence of a span from ``ask_from`` (to ``to``, else open above), its papers through
        :func:`take`. When the cap fills inside the look-back (``ask_from < start``) those hits are kept and the
        span is asked again from ``start`` (D551). Returns ``(the last request's papers, exhausted, seen, kept,
        fetched, the lower bound last asked, the newest date past ``to`` among every answer)``."""
        api_stats.pop("last_status", None)
        papers, exhausted = fetch_group(item["s2_query"], ask_from, limit, headers, api_stats,
                                        max_pages=pages_left(), to=to)
        seen, kept = take(item, qkey, papers)
        fetched = len(papers)
        late = _upper_ignored(papers, to) if to is not None else None
        if ask_from < start and exhausted == "truncated" and len(papers) >= limit:
            # The cap filled inside the look-back: keep those hits, then ask from the watermark
            # (else the query would come back capped every run).
            tally["lookback_capped"] += 1
            if pages_left() != 0:
                ask_from = start
                api_stats.pop("last_status", None)
                papers, exhausted = fetch_group(item["s2_query"], start, limit, headers, api_stats,
                                                max_pages=pages_left(), to=to)
                more_seen, more_kept = take(item, qkey, papers)
                seen, kept, fetched = seen + more_seen, kept + more_kept, fetched + len(papers)
                more_late = _upper_ignored(papers, to) if to is not None else None
                late = max(d for d in (late, more_late) if d) if (late or more_late) else None
        return papers, exhausted, seen, kept, fetched, ask_from, late

    def sweep_span(item: dict[str, Any], qkey: dict[str, Any], start: str, end: str, ask_from: str, *,
                   bounded: bool) -> tuple[str, bool]:
        """One span ``[start, end]`` of a query (module docstring, "Year spans"): asked from
        ``ask_from`` (``start``, or the tail's look-back below it) to ``end − 1 day`` when
        ``bounded``, else open above; its papers through :func:`take`; its coverage window written.
        A complete span whose bounds held is ``ok`` ``[start, end]`` (from the floor when the lower
        bound was ignored). A bounded span whose upper bound S2 broke is asked again open above from
        the same lower bound (D802, counted ``upper_bound_fallback``), which banks ``[start, today]``
        when complete. Anything else claims nothing (``[start, start]``). Returns ``(receipt,
        through_today)``: the receipt ``complete`` / ``truncated`` / ``error``, and whether the window
        banked reaches today (the fallback), so the query's later spans, holes and tail are covered."""
        nonlocal budget_stopped
        to = (dt.date.fromisoformat(end) - dt.timedelta(days=1)).isoformat() if bounded else None
        tally["spans"] += 1
        papers, exhausted, seen, kept, fetched, asked, late = pull(item, qkey, start, ask_from, to)
        tally["upper_bound_ignored"] += late is not None
        broken = late
        if late is not None and pages_left() != 0:
            # D802: the bounded answer cannot be banked, and asking the same span again gets the same
            # answer (one record dated past the bound is enough), so the rest of the query is asked
            # open above, which does not rely on the upper bound; its papers include the span's.
            tally["upper_bound_fallback"] += 1
            papers, exhausted, seen, more_kept, more_fetched, asked, _ = pull(item, qkey, start, ask_from, None)
            kept, fetched = kept + more_kept, fetched + more_fetched
            late, end = None, today
        # Honor-detection backstop (2): a date bound is a SILENT no-op if the API changes.
        filter_ignored = _filter_ignored(papers, asked)
        tally["filter_ignored"] += filter_ignored
        # Only a COMPLETE span whose upper bound held banks [start, end]; anything else banks NOTHING
        # ([start, start]): bulk results are paperId-ordered, so a capped pass covered an unknown
        # subset (SHOULD-14).
        ok = exhausted == "complete" and late is None
        covered_from = floor_from if (ok and filter_ignored) else start
        with c.window(CHANNEL, qkey, covered_from, end if ok else start) as w:
            w.seen, w.kept = seen, kept
            if late is not None:  # backstop (3) with no page left for the fallback: the next run asks it
                budget_stopped = True
                w.fail("query_invalid", permanent=True)
                w.error = f"publicationDateOrYear upper bound {to} ignored: a paper dated {late} came back"
                exhausted = "error"
            else:
                close(w, exhausted)
                if broken is not None and not ok:
                    w.error = (f"publicationDateOrYear upper bound {to} ignored (a paper dated {broken} came back); "
                               f"the open-ended request from {asked} ended {exhausted}")
        tally[f"queries_{exhausted}"] += 1
        tally["papers_fetched"] += fetched
        tally["papers_new"] += kept
        return exhausted, broken is not None and ok

    # A 429 asking more than 60 s halts the run (D550): the query it hit and those not reached get
    # no row and stay due; the run ends scope_complete = false (Collector.finish).
    with c.halts():
        for i, item in enumerate(plan):
            c.progress(i, len(plan))
            qkey = coverage_query(item)
            # Passes (D226): a query covered through today (and back to the floor) is not asked again
            # (no page of the run's budget is spent on it); a cap-`partial` one returns the same
            # paperId-ordered prefix within relist_after_h. `full` re-sweeps covered queries.
            if not full_resweep and not c.due(CHANNEL, qkey, until=today, relist_after=relist,
                                              partial_due=False, floor=floor_from):
                continue
            # Floor moved back (module docstring): each hole [s, e] first, in year spans, each asked as
            # the bounded range publicationDateOrYear=s:(e - 1 day) and written [s, e]; a hole's
            # sequence stops at its first span that is not ok (the rest stays a hole). A span whose
            # upper bound S2 broke is asked open above instead (D802); banked through today, it covers
            # the query's later holes and its tail too.
            through_today = False
            for hs, he in ([] if full_resweep else c.holes(CHANNEL, qkey, floor_from)):
                if pages_left() == 0 or through_today:
                    break
                tally["holes"] += 1
                for ss, se in year_spans(hs.date(), he.date(), split_years, this_year):
                    if pages_left() == 0:
                        break
                    receipt, through_today = sweep_span(item, qkey, ss.isoformat(), se.isoformat(),
                                                        ss.isoformat(), bounded=True)
                    if receipt != "complete" or through_today:
                        break
            if pages_left() == 0 and not through_today:
                budget_stopped = True  # the spans and queries not reached get no row; the next run picks them up
                break
            if through_today:
                continue
            # The tail: resume from the query's watermark, else the floor, always date-filtered (D801),
            # in year spans (module docstring); the sequence stops at the first span that is not ok.
            mark = None if full_resweep else c.watermark(CHANNEL, qkey)
            if mark is not None and mark.date().isoformat() >= today:
                continue  # only a hole was due; the tail is covered through today
            eff_from = max(floor_from, mark.date().isoformat()) if mark is not None else floor_from
            # Index lag (D551): the first span re-asks `lookback` days below the watermark; its window
            # is still written from the watermark (module docstring).
            ask_from = eff_from
            if mark is not None and lookback:
                ask_from = max(floor_from, (mark.date() - dt.timedelta(days=lookback)).isoformat())
            spans = year_spans(dt.date.fromisoformat(eff_from), c.now.date(), split_years, this_year)
            for j, (ss, se) in enumerate(spans):
                if j and pages_left() == 0:
                    budget_stopped = True
                    break
                receipt, through_today = sweep_span(item, qkey, ss.isoformat(), se.isoformat(),
                                                    ask_from if j == 0 else ss.isoformat(),
                                                    bounded=j < len(spans) - 1)
                if receipt != "complete" or through_today:
                    # not ok: a later ok span would carry the watermark over this one (module docstring);
                    # through today: the open-ended fallback (D802) covered the remaining spans
                    break
            if budget_stopped:
                break
        else:
            c.progress(len(plan), len(plan))

    c.counts.update(tally, groups_planned=planned, misuse_groups_planned=misuse_planned,
                    queries_selected=int(sampling["selected"]), query_limit=limit,
                    page_budget=page_budget or 0, budget_stopped=int(budget_stopped),
                    page_retries=int(api_stats["retries"]),
                    pages_fetched=int(api_stats["pages_fetched"]),
                    full_resweep=int(full_resweep), lookback_days=lookback, split_years=split_years)
    return c.finish(scope_complete=not budget_stopped)
