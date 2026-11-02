"""``collect.forums`` — the LessWrong + Alignment Forum keyword sweep (§5.6 LW/AF row; v1
``search_forums.py`` + ``lib/search_engine.py`` LW/AF part).

Sweeps both ForumMagnum forums against their unauthenticated Algolia-shaped ``/api/search``
backend: one query per concept-group **term** (the taxonomy-joined recall strategy) plus one per
curated **tag** slug, per forum. Every hit clearing the karma + from-date gates is one ``sources``
row on ``sources.forums`` under the ``lesswrong`` / ``alignmentforum`` channel (external id the
post ``_id``, ``canonical_url = <base>/posts/<_id>``, key ``fm_post`` = the ``_id`` typed by
:mod:`stack.scholarly_records`), carrying ``metadata`` title / authors / ``published_at`` /
``doc_type = forum_post`` from the hit already held; every executed query writes ONE ``coverage``
row on ``coverage.forums`` (seen = gate-clearing hits, kept = hits new to the chain, the window,
an honest outcome). The two forums share one ForumMagnum DB, so a crosspost carries the SAME
``_id`` on both: two rows (one per channel) with one ``fm_post`` key, which ``gather`` makes one
document with two members (§6 forum group). ``collect.af_graphql`` writes AF posts with the same
key in its own chain.

**The AF host searches the LessWrong index.** ``alignmentforum.org/api/search`` serves the whole
shared index (every LessWrong post; live-verified 2026-10-12: a 7-day sweep returned the same 52
posts on both hosts, while AF's GraphQL listed one AF post). v3 registered every such hit under
``alignmentforum``, so every LessWrong hit became a false crosspost. The ``alignmentforum`` queries
therefore send the facet ``af:true`` and drop a hit whose ``af`` flag is false (backstop, counted
``hits_not_af``).

v3's coverage ``topic_keys`` (``group:<id>`` + ``technique:<slug>``) have no ``coverage`` column
(A.5); the query label ``kw:<term>`` / ``tag:<slug>`` is the coverage ``query`` and its concept
group is re-derived from ``channels.yaml`` by :func:`plan_queries`.

**Search-body in-band capture (§5.4, v3 D-INBAND1).** The ``/api/search`` response embeds each
post's body under ``h['body']``; it is minted through the harness as the source's in-band ``post``
entry (``blobs/``, ``media_type`` :data:`SEARCH_BODY_MEDIA_TYPE`). That body is an
Algolia-indexed copy of unknown fidelity (possibly truncated or stale), not the document of record:
there is still exactly ONE network call per query page and no per-item call.

**Incremental windows via ``publicDateMs`` (D-SWEEP1, 2026-07-24).** Each ``(channel, query)``
resumes from its watermark — the newest ``ok`` coverage row's ``window_end_at``
(:meth:`Collector.watermark`). When the effective start ``eff_from`` is past the channel's own
floor (a resume, or a ``window_days`` / ``from`` floor), it is sent to the backend as
``numericFilters = ["publicDateMs>=<ms>"]``. A COMPLETE windowed pass writes ``[eff_from, today]``
``ok``. A pass with no date filter (``eff_from`` at the channel floor) scans the whole index and
writes ``[floor_from, today]``.

**Server-side filters (D-SWEEP1, D642; live-checked 2026-10-18).** The backend is Elasticsearch
behind an Algolia-shaped API. It honours a lone lower bound ``publicDateMs>=<ms>`` and a lone upper
bound ``publicDateMs<<ms>``, each combined with ``baseScore>=<k>`` and with the facets, but it
*silently drops* a two-sided ``publicDateMs`` range (two clauses, a ``filters`` string) and returns
the whole index (``A TO B`` and a comma string are HTTP 400). So a pass sends at most one date bound
(:func:`numeric_filters`): a tail its lower bound, a hole its upper bound. Every pass also sends the
karma floor ``baseScore>=min_karma`` (about half the hits of a broad query are under 25); the
client-side karma gate stays.

**Karma lag (D406).** The karma gate reads ``baseScore`` at run time, and karma accrues over the
days after posting: a post under the floor at one run may clear it a week later. The tail is
therefore asked from ``eff_from = max(floor, watermark − karma_lag_days)`` (option, default 14, as
``collect.lw_graphql``'s ``tag_lag_days``) and its window written from there. A post dated before
the watermark and unchanged (``baseScore`` is outside the channel's ``record_projection``) writes no
row (``skip_unchanged``, counted ``sources_unchanged``). A post that clears the floor more than
``karma_lag_days`` after posting is still missed.

**Two honesty backstops, because a lower-bound filter is a silent no-op if a future deploy drops
it.** (1) The client-side floor gate: a hit dated before ``floor_from`` is never registered,
whatever the server returned. (2) Honor-detection: if any returned hit predates ``eff_from``
(beyond a 1-day timezone slack) the server ignored the filter, so the query's window falls back to
``[floor_from, today]`` (the whole index came back). Truncated passes claim nothing: results come
back in search-rank order, not date order, so a capped pass covered an unknown subset of its
window — written ``partial`` (never advances the watermark; v3 wrote ``from == to``, SHOULD-14).
A failed page writes the window ``failed_*``. A hole has the mirror backstop on its upper bound
(``upper_bound_ignored``, below).

**The first pass from a set floor is windowed** (round 5 (RUN) B7a): the lower bound is sent
whenever ``eff_from`` is past the channel's ``from_date`` (or past an earlier ``from``), so a plan
line ``from: 2025-10-01`` asks for that year, not the whole index (which, rank-ordered and capped,
could never complete).

**Server page cap (D642).** Elasticsearch's ``max_result_window`` is 10,000: page 99 at 100 hits
answers, page 100 is an HTTP 400 "Result window is too large, from + size must be less than or equal
to: [10000]" (plain text; live-verified 2026-10-18, also at 50 per page: page 199 ok, page 200 400).
A 400 on a page after the first (the same request answered on page 0, only the page number
differs) is that cap: the pass is ``truncated`` (``partial``), counted ``server_page_cap``, its
window ``windows_capped`` (build reason ``limit``, D492), and the hits that arrived are rows. A 400
on page 0 stays ``failed_transient[network]``. Backstop for a quieter cap: a pass whose ``nbPages``
ran out while its ``nbHits`` reports at least a page more than arrived is ``truncated`` the same
way. ``max_records`` above 10,000 is therefore moot.

**No date splitting; a per-query floor instead (D552).** ``collect.lw_graphql`` sweeps a broad tag
in date sub-windows (``subwindow_days``), each a bounded GraphQL range, so a cap binds per span.
This backend cannot do that: with one date bound per request every span is cumulative (``[s,
today]`` from a lower bound, everything before ``e`` from an upper one), so the 10,000-hit cap
binds per query and side, not per span, and a client-side cut does not lift it. What the operator
can do is narrow a broad query: option ``query_from`` maps query labels (``kw:<term>``,
``tag:<slug>``, both forums) to a later floor for that query alone (``max(floor, query_from)``:
the lower bound sent, the client gate, the start of its first window). Such a query covers only
from its own floor; moving that floor back later leaves a hole, asked as below. A capped window
stays visible as ``partial``, counted ``windows_capped`` with ``server_page_cap`` (build reason
``limit``, D492).

**Floor moved back** (round 5 (RUN) B7a; :meth:`stack.collect.base.Collector.holes`): a
``from`` earlier than a query's first window leaves a hole below its coverage, asked before the
usual tail. The hole is sent only its upper bound ``publicDateMs<<hole end>`` and the karma floor
(D642): every post before the hole's end comes back, rank-ordered, so hits dated before its start
are cut client-side (``hits_outside_hole``, which also counts hits on or after its end); the
window is written ``[hole start, hole end]``. The pages are those of the query's whole history
before the hole's end (karma-gated), not of the hole alone, nor (as before D642) of everything
from the hole's start to today. Honour-detection: a hit dated on or after the hole's end (beyond
the 1-day slack) means the server dropped the upper bound: counted ``upper_bound_ignored`` and
said as a warning; what came back is then the query's whole index, a superset of the hole, so a
complete pass still covers it and a capped one is ``partial`` as always. Coverage: only the gap is
claimed again.

**Deletion.** ``channels.yaml`` gives both forum channels no ``miss_means`` (default ``unknown``):
a keyword search not returning a post says nothing about the post, so this stage writes no miss
rows and does not detect deleted posts.

Pacing (D26): requests go through the harness transport (``HostPacer`` at ``1 / LW_DELAY_S`` per
host; LW and AF are two hosts); v3's explicit 0.3 s sleeps between pages and queries are that
pacer now. One attempt per page (v3: no retry ladder); a 429 does not halt the run, it fails the
query's window.

Options (plan stage line ``options:``, D72; the v3 env var wins when set):
  * ``max_records`` / ``STACK_COLLECT_LIMIT``       — per-query result cap (default: the channel's
                                                      ``breadth`` preset ``max_results_per_query``)
  * ``max_queries`` / ``STACK_COLLECT_MAX_QUERIES`` — cap on queries executed, per forum (0 = all)
  * ``fraction``    / ``STACK_COLLECT_FRACTION``    — stable hash-selected fraction of the plan (0,1]
  * ``min_karma``   / ``STACK_FORUMS_MIN_KARMA``    — baseScore floor (default 25, v1's value)
  * ``karma_lag_days``                              — days below the watermark the tail re-scans
                                                      (default 14; 0 = none; D406, above)
  * ``tags``        / ``STACK_FORUMS_TAGS``         — tag slugs (list, or comma-separated in the env;
                                                      default the v1-verified list; empty disables)
  * ``from``        / ``STACK_FORUMS_FROM``         — floor from-date (default channel ``from_date``)
  * ``window_days``                                 — floor = today − N days when ``from`` is unset
  * ``query_from``                                  — ``{label: YYYY-MM-DD}``: a later floor for these
                                                      queries alone (D552, above); a label not in
                                                      the plan or a date before the floor is refused
  * ``groups``                                      — only these concept-group ids' terms (no
                                                      extra keywords; tags still per ``tags``)
  * ``relist_after_h``                              — passes (D226, :meth:`stack.collect.base.
                                                      Collector.due`): a query covered through today
                                                      is skipped; a ``partial`` one is not asked
                                                      again within this many hours (12)

Cardinal rule: imports only ``stack.*`` and the stdlib; nothing from other workflows.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import time
from collections.abc import Callable
from typing import Any

from stack.collect.base import RELIST_OPTIONS, Collector, Pacing, Transport, Window, utc
from stack.collect.sampling import (
    ENV_FRACTION,
    ENV_MAX_QUERIES,
    SAMPLING_OPTIONS,
    select,
)
from stack.config import ChannelDef, StackConfig
from stack.scholarly_records import keys_for_source
from stack.stage_runner import StageContext, StageResult
from stack.store.job import say

#: The ``options`` keys this stage's code reads; any other is refused (D434)
OPTIONS: frozenset[str] = frozenset({"from", "groups", "karma_lag_days", "max_records", "min_karma", "query_from", "tags", "window_days"}) | SAMPLING_OPTIONS | RELIST_OPTIONS

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────
# The two forums share the ForumMagnum DB; a post cross-posted to both carries the SAME _id, so
# it gets a row under both channels with one external_id and one `fm_post` key (§6 forum group).
FORUM_CHANNELS = ("lesswrong", "alignmentforum")
LW_PAGE_SIZE = 100
LW_DELAY_S = 0.3  # LessWrong politeness: ~3 req/s
HTTP_TIMEOUT_S = 60.0
DEFAULT_FROM = "2020-01-01"
DEFAULT_MIN_KARMA = 25  # v1 search_config.yaml lesswrong.min_karma
# D406: karma accrues after posting, so the tail re-scans this many days below the watermark
# (collect.lw_graphql's tag_lag_days default).
DEFAULT_KARMA_LAG_DAYS = 14
CONTENT_ROLE = "post"
# The Algolia `body` is the index's text rendering of the post (not the HTML of record).
SEARCH_BODY_MEDIA_TYPE = "text/markdown"

# v1-verified LW tag slugs (search_config.yaml; all resolved 82–1006 hits). No channels.yaml
# schema slot exists for per-channel tag/karma params (channel objects are additionalProperties:
# false with no params field), so the LW-specific knobs live here as documented constants (the
# stage line's `options` override them); the taxonomy-joined TERM queries come from the channel's
# concept_groups (in config). v1's curated LessWrong keyword list is otherwise a subset of the
# concept-group terms (which this stage already queries one-by-one). These two are the exception
# — forum-native usages with no concept group of their own — so they are queried as bare
# keywords, carrying no topic keys because there is no group to join them to.
EXTRA_KEYWORDS = ("sandbagging", "debate")

DEFAULT_TAGS = (
    "ai-control",
    "interpretability-ml-and-ai",
    "ai-evaluations",
    "deceptive-alignment",
    "inner-alignment",
    "mesa-optimization",
    "rlhf",
)

BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0 Safari/537.36 stack-collect/1.0 (+research)"
)
PACING = Pacing(rps=1.0 / LW_DELAY_S, retry_delays_s=(), timeout_s=HTTP_TIMEOUT_S,
                headers={"User-Agent": BROWSER_UA})

_ENV_LIMIT = "STACK_COLLECT_LIMIT"
_ENV_MIN_KARMA = "STACK_FORUMS_MIN_KARMA"
_ENV_TAGS = "STACK_FORUMS_TAGS"
_ENV_FROM = "STACK_FORUMS_FROM"
_DEFAULT_LIMIT = 100


# ─────────────────────────────────────────────────────────────────────────────
# Query planning (concept-group terms + tags) — pure, unit-testable
# ─────────────────────────────────────────────────────────────────────────────
def _selected_group_ids(chan: ChannelDef, all_ids: list[str]) -> list[str]:
    """Resolve a channel's ``concept_groups`` selection ('all' | list | None) to ordered ids."""
    sel = chan.concept_groups
    if sel == "all" or sel is None:
        return list(all_ids)
    if isinstance(sel, list):
        wanted = set(sel)
        return [g for g in all_ids if g in wanted]
    return []


def plan_queries(config: StackConfig, chan: ChannelDef, tags: tuple[str, ...],
                 only_groups: list[str] | None = None) -> list[dict[str, Any]]:
    """The ordered query plan for one forum: concept-group TERM queries first (each carrying its
    ``group:``/``technique:`` topic keys), then TAG queries (facet filters, no concept topic).
    Deterministic: concept-group order, term order, then tag order. ``only_groups`` (the
    ``groups`` option) narrows the term queries to those groups and drops the extra keywords; an
    id that is not a concept group is a ``ValueError``. Terms go out as they are, also those of
    ``needs_ai_context`` groups: LessWrong and the Alignment Forum are AI venues. A group's ``forum_terms`` replace
    its ``terms`` (an empty list asks nothing for the group)."""
    groups = {g.id: g for g in config.channels.concept_groups}
    order = [g.id for g in config.channels.concept_groups]
    selected = _selected_group_ids(chan, order)
    if only_groups is not None:
        unknown = sorted(set(only_groups) - set(order))
        if unknown:
            raise ValueError(f"option groups: {unknown} are not concept groups")
        selected = [gid for gid in selected if gid in set(only_groups)]
    plan: list[dict[str, Any]] = []
    for gid in selected:
        g = groups[gid]
        topic_keys = [f"group:{gid}"] + [f"technique:{t}" for t in g.techniques]
        for term in g.terms_for_forums():  # `forum_terms` replaces `terms` here; empty = no query (the Chinese-coverage work)
            plan.append({"kind": "term", "term": term, "tag": None, "topic_keys": topic_keys})
    for kw in EXTRA_KEYWORDS if only_groups is None else ():
        plan.append({"kind": "term", "term": kw, "tag": None, "topic_keys": []})
    for tag in tags:
        plan.append({"kind": "tag", "term": "", "tag": tag, "topic_keys": []})
    return plan


def _query_label(item: dict[str, Any]) -> str:
    """The coverage ``query`` string for a plan item (stable watermark key)."""
    return f"tag:{item['tag']}" if item["kind"] == "tag" else f"kw:{item['term']}"


# ─────────────────────────────────────────────────────────────────────────────
# HTTP (the run's harness transport; module-level so offline tests monkeypatch it)
# ─────────────────────────────────────────────────────────────────────────────
_T: Transport | None = None


def http_post(url: str, payload: Any, *, timeout: float = HTTP_TIMEOUT_S) -> tuple[int, Any]:
    """One paced JSON POST through the run's transport; returns ``(status, parsed_json_or_None)``.
    A transport failure returns ``(0, None)`` so the caller records a failed window rather than
    crashing. ``timeout`` is kept for the seam's signature; ``PACING.timeout_s`` applies."""
    if _T is None:
        raise RuntimeError("forums.http_post: no transport outside run(ctx)")
    return _T.post_json(url, payload)


def _sleep(seconds: float) -> None:
    """Politeness delay (module-level so offline tests monkeypatch it to a no-op); the transport's
    pacer sleeps through it."""
    time.sleep(seconds)


def numeric_filters(*, since_ms: int | None = None, before_ms: int | None = None,
                    min_karma: int = 0) -> list[str] | None:
    """The ``numericFilters`` of one pass (module docstring, "Server-side filters"): a lower bound
    ``publicDateMs>=since_ms`` (a tail, D-SWEEP1) OR an upper bound ``publicDateMs<before_ms`` (a
    hole, D642), never both (the backend drops a two-sided range and returns the whole index), and
    the karma floor ``baseScore>=min_karma`` when it is positive; ``None`` when there is nothing to
    send."""
    if since_ms is not None and before_ms is not None:
        raise ValueError("numeric_filters: a two-sided publicDateMs range is dropped server-side")
    out = ([f"publicDateMs>={since_ms}"] if since_ms is not None else []) + (
        [f"publicDateMs<{before_ms}"] if before_ms is not None else []) + (
        [f"baseScore>={min_karma}"] if min_karma and min_karma > 0 else [])
    return out or None


def _search_page(
    base_url: str,
    query: str,
    tag: str | None,
    page: int,
    per_page: int,
    numeric: list[str] | None = None,
    *,
    af_only: bool = False,
) -> tuple[int, dict[str, Any] | None]:
    """One Algolia-shaped ``/api/search`` page; returns ``(status, result0_or_None)``. ``numeric``
    (from :func:`numeric_filters`) goes out as ``numericFilters``; it combines with
    ``facetFilters``. ``af_only`` adds the facet ``af:true`` (AND with a tag facet): the Alignment
    Forum host serves the whole shared LessWrong index (live-verified 2026-10-12)."""
    params: dict[str, Any] = {"query": query, "hitsPerPage": per_page, "page": page}
    facets = ([[f"tags.slug:{tag}"]] if tag else []) + ([["af:true"]] if af_only else [])
    if facets:
        params["facetFilters"] = facets
    if numeric:
        params["numericFilters"] = list(numeric)
    status, data = http_post(f"{base_url}/api/search", [{"indexName": "posts", "params": params}])
    if status != 200 or not isinstance(data, list) or not data:
        return status, None
    first = data[0]
    return status, first if isinstance(first, dict) else None


def fetch_forum(
    base_url: str,
    query: str,
    tag: str | None,
    limit: int,
    numeric: list[str] | None = None,
    *,
    on_page: Callable[[int], None] | None = None,
    stats: dict[str, Any] | None = None,
    af_only: bool = False,
) -> tuple[list[dict[str, Any]], str]:
    """Page through the forum search up to ``limit`` raw hits. Returns ``(hits, exhausted)`` with
    ``exhausted`` ∈ {complete, truncated, error} (``stats['status']`` = the failed page's HTTP
    status, ``0`` = transport failure). ``numeric`` (:func:`numeric_filters`) is threaded to every
    page; ``af_only`` the ``af:true`` facet. ``on_page`` (when given) is called after each page
    with the running hit total.

    The page size is CONSTANT (:data:`LW_PAGE_SIZE`) and ``limit`` is applied client-side: the
    backend pages by ``page × hitsPerPage``, so shrinking the last page (limit 150 → page 1 at size
    50) would re-read hits 50-99 and never reach 100-149. A page that carries more than the room
    left is ``truncated``, even when it is the last one. An HTTP 400 on a page after the first is
    the server's page cap (module docstring, "Server page cap"): ``truncated``, flagged
    ``stats['server_page_cap']``; a 400 on page 0 is a refused query (``error``)."""
    hits: list[dict[str, Any]] = []
    page = 0
    while True:
        remaining = limit - len(hits)
        if remaining <= 0:
            return hits, "truncated"
        status, result = _search_page(base_url, query, tag, page, LW_PAGE_SIZE, numeric,
                                      af_only=af_only)
        if status != 200 or result is None:
            if status == 400 and page > 0:
                # Elasticsearch's max_result_window: the same request answered on page 0
                if stats is not None:
                    stats["server_page_cap"] = True
                return hits, "truncated"
            if stats is not None:
                stats["status"] = status
            return hits, "error"
        batch = result.get("hits") or []
        if not batch:
            return hits, _ran_out(hits, result, stats)
        hits.extend(batch[:remaining])
        if on_page is not None:
            on_page(len(hits))
        if len(batch) > remaining:
            return hits, "truncated"
        nb_pages = int(result.get("nbPages") or 0)
        page += 1
        if page >= nb_pages:
            return hits, _ran_out(hits, result, stats)


def _ran_out(hits: list[dict[str, Any]], result: dict[str, Any],
             stats: dict[str, Any] | None) -> str:
    """The receipt of a pass whose pages ran out: ``complete``, unless the backend's ``nbHits``
    reports at least a page (:data:`LW_PAGE_SIZE`) more than arrived, i.e. the server stopped
    paging early (module docstring, "Server page cap"): ``truncated``, flagged
    ``stats['server_page_cap']``."""
    nb_hits = result.get("nbHits")
    if isinstance(nb_hits, int) and not isinstance(nb_hits, bool) and nb_hits - len(hits) >= LW_PAGE_SIZE:
        if stats is not None:
            stats["server_page_cap"] = True
        return "truncated"
    return "complete"


def error_class_of(status: int) -> tuple[str, bool]:
    """A failed page's HTTP status → ``(error_class, permanent)`` for its coverage window: ``0``
    transport → ``network``; 429 → ``rate_limit``; 5xx → ``http_5xx``; 401/403 → ``provider_auth``;
    a 200 whose body is not the expected shape → ``parse_error`` (permanent); else ``network``."""
    if status == 429:
        return "rate_limit", False
    if 500 <= status < 600:
        return "http_5xx", False
    if status in (401, 403):
        return "provider_auth", False
    if status == 200:
        return "parse_error", True
    return "network", False


# ─────────────────────────────────────────────────────────────────────────────
# Hit normalisation (title / date / authors — the body goes in-band)
# ─────────────────────────────────────────────────────────────────────────────
_ISO_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})")


def _posted_at(hit: dict[str, Any]) -> dt.datetime | None:
    """Best-effort posting time from a search hit (``postedAt`` / ``createdAt`` / ``publicDate``:
    an ISO string or an epoch in s or ms), as an aware UTC datetime; ``None`` when absent or
    malformed (a bad provider timestamp never fails the run)."""
    for field in ("postedAt", "createdAt", "publicDate"):
        val = hit.get(field)
        if val is None:
            continue
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            secs = val / 1000.0 if val > 1e11 else float(val)
            try:
                return dt.datetime.fromtimestamp(secs, dt.UTC)
            except (ValueError, OSError, OverflowError):
                return None
        s = str(val).strip()
        try:
            return utc(s)
        except ValueError:
            m = _ISO_DATE.match(s)
            if m:
                try:
                    return utc(m.group(1))
                except ValueError:
                    return None
    return None


def _posted_date(hit: dict[str, Any]) -> str | None:
    """Best-effort ISO ``YYYY-MM-DD`` posting date from a search hit (ISO string or epoch)."""
    at = _posted_at(hit)
    return at.date().isoformat() if at is not None else None


def _hit_authors(hit: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for key in ("authorDisplayName", "authorUserName"):
        v = hit.get(key)
        if isinstance(v, str) and v.strip():
            names.append(v.strip())
            break
    coauthors = hit.get("coauthors")
    if isinstance(coauthors, list):
        for c in coauthors:
            if isinstance(c, dict):
                nm = c.get("displayName") or c.get("username")
                if isinstance(nm, str) and nm.strip():
                    names.append(nm.strip())
    return names


def fm_keys(channel_group: str, channel: str, post_id: str) -> list[tuple[str, str]]:
    """The row's identity keys (``fm_post`` = the post ``_id``) as the harness's pairs."""
    return [(k["kind"], k["value"]) for k in keys_for_source(channel_group, channel, post_id)]


# ─────────────────────────────────────────────────────────────────────────────
# Coverage window helpers
# ─────────────────────────────────────────────────────────────────────────────
#: Algolia's per-query annotations of a hit (match highlights, snippets, ranking): not the forum's
#: record of the post, and they vary with the query that found it.
SEARCH_ANNOTATIONS = ("_highlightResult", "_snippetResult", "_rankingInfo")


def record_payload(hit: dict[str, Any]) -> dict[str, Any]:
    """The ``provider_record`` (D522): the search hit without ``body`` (the in-band ``post`` entry) and
    without Algolia's query annotations (:data:`SEARCH_ANNOTATIONS`), so re-finding a post by another
    query leaves its record unchanged."""
    return {k: v for k, v in hit.items() if k != "body" and k not in SEARCH_ANNOTATIONS}


def _as_of_date() -> str:
    """Today's date (ISO) — the upper bound a COMPLETE pass covers. Module-level so tests can
    monkeypatch a fixed 'now'."""
    return dt.datetime.now(dt.UTC).date().isoformat()


# One day of slack (ms) for the honor-detection: a hit dated within a day of `eff_from` is treated
# as on-window (timezone/`publicDateMs`-vs-`postedAt` edges), so only a genuinely older hit — proof
# the server returned the whole index — trips the fallback.
_FILTER_SLACK_MS = 24 * 60 * 60 * 1000


def _ms(iso_date: str) -> int:
    """Unix milliseconds at UTC midnight of an ISO ``YYYY-MM-DD`` date — the ``publicDateMs`` bound
    a windowed pass (lower) or a hole (upper) sends; never both in one request (module docstring,
    "Server-side filters")."""
    return int(dt.datetime.fromisoformat(iso_date).replace(tzinfo=dt.UTC).timestamp() * 1000)


def _public_date_ms(hit: dict[str, Any]) -> int | None:
    """The hit's ``publicDateMs`` (unix ms) as an int, or ``None`` if absent/non-numeric — the
    field the backend filters on and that honor-detection reads back."""
    v = hit.get("publicDateMs")
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return int(v)
    return None


def _filter_ignored(hits: list[dict[str, Any]], eff_from_ms: int) -> bool:
    """True if the server ignored the ``publicDateMs`` lower bound (D-SWEEP1 honesty backstop): any
    returned hit dated before ``eff_from`` beyond ``_FILTER_SLACK_MS`` means the whole index came
    back, so the caller must fall back to the floor-range window rather than falsely claim
    ``[eff_from, today]``."""
    threshold = eff_from_ms - _FILTER_SLACK_MS
    for h in hits:
        pd = _public_date_ms(h)
        if pd is not None and pd < threshold:
            return True
    return False


def _upper_bound_ignored(hits: list[dict[str, Any]], before_ms: int) -> bool:
    """True if the server ignored a hole's ``publicDateMs<`` upper bound (D642 honesty backstop):
    any returned hit dated on or after ``before_ms`` beyond ``_FILTER_SLACK_MS``."""
    threshold = before_ms + _FILTER_SLACK_MS
    return any((pd := _public_date_ms(h)) is not None and pd >= threshold for h in hits)


def floors(c: Collector, chan: ChannelDef, today: str) -> tuple[str, str]:
    """``(base_floor, floor_from)`` for one forum. ``base_floor`` = the earlier of ``from`` (env
    ``STACK_FORUMS_FROM`` wins) and the channel's ``from_date`` (else ``DEFAULT_FROM``): a pass
    starting there sends no date filter (v3's first pass over the whole index); a later ``from``
    is sent as the lower bound from the first pass on (module docstring). ``floor_from`` = ``from``
    when set, else the channel's floor, or today − ``window_days`` when that option is set and
    ``from`` is not: the client-side gate and the start of a query with no watermark."""
    explicit = c.option("from", None, env=_ENV_FROM, cast=str)
    chan_floor = dt.date.fromisoformat((chan.from_date or DEFAULT_FROM).strip()[:10]).isoformat()
    if explicit:
        floor = dt.date.fromisoformat(explicit.strip()[:10]).isoformat()
        return min(floor, chan_floor), floor
    base = chan_floor
    days = c.option("window_days", None, cast=int)
    if days:
        return base, max(base, (dt.date.fromisoformat(today) - dt.timedelta(days=days)).isoformat())
    return base, base


def query_floors(c: Collector, labels: set[str], floor: str) -> dict[str, str]:
    """The ``query_from`` option (D552): ``{query label: ISO date}``, each a later floor for that
    query on both forums. A label the plan does not ask, a malformed date or one before the
    stage's ``floor`` (the earlier of the two forums' floors) raises ``ValueError``."""
    raw = c.option("query_from", None)
    if not raw:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"option query_from: a mapping of query label to date, got {raw!r}")
    unknown = sorted(str(k) for k in raw if str(k) not in labels)
    if unknown:
        raise ValueError(f"option query_from: {unknown} are not query labels of this plan "
                         "(kw:<term> or tag:<slug>)")
    out: dict[str, str] = {}
    for label, value in raw.items():
        day = dt.date.fromisoformat(str(value).strip()[:10]).isoformat()
        if day < floor:
            raise ValueError(f"option query_from: {label} {day} is before the floor {floor}")
        out[str(label)] = day
    return out


def _unit_key(cid: str) -> Callable[[dict[str, Any]], str]:
    """The sampling key of a plan item on forum ``cid`` (stable across runs)."""
    return lambda item: f"{cid}:{_query_label(item)}"


def _tags(c: Collector) -> tuple[str, ...]:
    """Tag slugs: env ``STACK_FORUMS_TAGS`` (comma-separated; set but empty disables tag
    queries) wins over the ``tags`` option (a list), else :data:`DEFAULT_TAGS`."""
    raw = os.environ.get(_ENV_TAGS)
    if raw is not None:
        return tuple(t.strip() for t in raw.split(",") if t.strip())
    opt = c.option("tags", None)
    if opt is None:
        return DEFAULT_TAGS
    if isinstance(opt, str):
        return tuple(t.strip() for t in opt.split(",") if t.strip())
    return tuple(str(t).strip() for t in opt if str(t).strip())


# ─────────────────────────────────────────────────────────────────────────────
# The stage
# ─────────────────────────────────────────────────────────────────────────────
def run(ctx: StageContext) -> StageResult:
    global _T
    from stack.config import load

    c = Collector(ctx)
    config = load()
    # Depth: env override → stage option → the channel's breadth preset → floor. Both forums share
    # one preset (same ForumMagnum backend), so lesswrong's is authoritative.
    configured = config.channels.max_results_per_query(FORUM_CHANNELS[0], _DEFAULT_LIMIT)
    limit = max(1, c.option("max_records", configured, env=_ENV_LIMIT, cast=int))
    min_karma = c.option("min_karma", DEFAULT_MIN_KARMA, env=_ENV_MIN_KARMA, cast=int)
    lag = c.option("karma_lag_days", DEFAULT_KARMA_LAG_DAYS, cast=int)
    if lag < 0:
        raise ValueError(f"option karma_lag_days: {lag} < 0")
    tags = _tags(c)
    only = c.option("groups", None)
    only_groups = None if only is None else [str(g) for g in ([only] if isinstance(only, str) else only)]
    # Scale control: plan options feed sampling.select's env knobs (a set env var wins).
    env = dict(os.environ)
    for name, var in (("fraction", ENV_FRACTION), ("max_queries", ENV_MAX_QUERIES)):
        value = c.option(name, None, env=var, cast=str)
        if value is not None:
            env[var] = value
    today = _as_of_date()

    # Build the full ordered plan across both forums first. `planned` counts the FULL plan, not
    # the selected slice, so a fractional run reports its unrun queries (SHOULD-14). Scale
    # control applies PER FORUM, so a fractional run still exercises both backends.
    forums: list[tuple[str, ChannelDef, list[dict[str, Any]]]] = []
    planned = selected = 0
    labels: set[str] = set()
    for cid in FORUM_CHANNELS:
        chan = c.channel(cid)
        # `plan_queries` is a test seam (3 args); the 4th only when the option is set
        full = (plan_queries(config, chan, tags) if only_groups is None
                else plan_queries(config, chan, tags, only_groups))
        labels |= {_query_label(item) for item in full}
        plan, meta = select(full, _unit_key(cid), env=env)
        forums.append((cid, chan, plan))
        planned += int(meta["planned"])
        selected += int(meta["selected"])
    per_query = query_floors(c, labels, min(floors(c, chan, today)[1] for _, chan, _ in forums))

    _T = c.transport(PACING, sleep=lambda s: _sleep(s))
    relist = c.relist_after()
    tally = {"queries_complete": 0, "queries_truncated": 0, "queries_error": 0,
             "duplicate_queries": 0, "windowed": 0, "filter_ignored": 0,
             "hits_fetched": 0, "hits_seen": 0, "hits_new": 0, "hits_lesswrong": 0,
             "hits_alignmentforum": 0, "hits_not_af": 0, "server_page_cap": 0, "holes": 0,
             "hits_outside_hole": 0, "upper_bound_ignored": 0, "query_from_applied": 0}
    def take(w: Window, hits: list[dict[str, Any]], cid: str, chan: ChannelDef,
             item: dict[str, Any], label: str, base_url: str, floor_from: str, af_only: bool,
             observed: set[str], since: str | None = None, upto: str | None = None,
             mark: str | None = None) -> None:
        """The window's hits through the gates into rows; a hole's ``[since, upto)`` cuts a hit
        dated outside it (the server is sent only the upper bound, so everything before the hole
        comes back; a hit after its end belongs to the tail's window; counted
        ``hits_outside_hole``); a hit dated before ``mark`` (the karma-lag re-scan, D406) and
        unchanged writes no row."""
        for h in hits:
            post_id = str(h.get("_id") or h.get("objectID") or "")
            if not post_id:
                continue
            # v1's client-side gates: karma, then the date floor. The body is read
            # only as the in-band entry; it never affects the gates.
            if min_karma and (h.get("baseScore") or 0) < min_karma:
                continue
            if af_only and h.get("af") is False:
                tally["hits_not_af"] += 1
                continue
            pd = _posted_date(h)
            if pd is not None and ((since is not None and pd < since)
                                   or (upto is not None and pd >= upto)):
                tally["hits_outside_hole"] += 1
                continue
            if pd is not None and pd < floor_from:
                continue
            w.seen += 1
            term = item["tag"] if item["kind"] == "tag" else item["term"]
            if post_id in observed:
                # one row per source per run; another query re-found it (D228)
                c.attribute(cid, post_id, query=label, label=term)
                continue
            if c.prior(cid, post_id) is None:
                w.kept += 1
            observed.add(post_id)
            title = str(h.get("title") or "").strip()
            row = c.observe(
                cid, post_id, h,
                keys=fm_keys(chan.channel_group, cid, post_id),
                canonical_url=f"{base_url}/posts/{post_id}",
                metadata={"title": title or None, "authors": _hit_authors(h) or None,
                          "published_at": _posted_at(h), "doc_type": "forum_post"},
                skip_unchanged=mark is not None and pd is not None and pd < mark,
                query=label, label=term,
                provider_record=record_payload(h),  # D522
            )
            if row is None:
                continue  # re-seen in the karma-lag re-scan, unchanged: no row, no in-band offer
            body = h.get("body")
            c.add_inband(c.source_id(cid, post_id), CONTENT_ROLE,
                         text=body if isinstance(body, str) else None,
                         media_type=SEARCH_BODY_MEDIA_TYPE)

    def close(w: Window, exhausted: str, stats: dict[str, Any]) -> None:
        if exhausted == "error":
            error_class, permanent = error_class_of(int(stats.get("status") or 0))
            w.fail(error_class, permanent=permanent)
        else:
            w.close(exhausted)

    def count(exhausted: str, hits: list[dict[str, Any]], w: Window, cid: str,
              stats: dict[str, Any]) -> None:
        tally[f"queries_{exhausted}"] += 1
        tally["hits_fetched"] += len(hits)
        tally["hits_seen"] += w.seen
        tally["hits_new"] += w.kept
        tally[f"hits_{cid}"] += w.seen
        tally["server_page_cap"] += int(bool(stats.get("server_page_cap")))

    with c.halts():
        for cid, chan, plan in forums:
            base_url = (chan.location or "").rstrip("/")
            base_floor, stage_floor = floors(c, chan, today)
            # The AF host's search serves the whole shared LessWrong index: only `af` posts are
            # Alignment Forum posts (server facet, and the hit's `af` flag as a backstop).
            af_only = cid == "alignmentforum"
            observed: set[str] = set()
            done: set[str] = set()
            for item in plan:
                label = _query_label(item)
                if label in done:
                    # A term listed under two concept groups is one search and one coverage
                    # window (v3 wrote a receipt per group; the window key is per query).
                    tally["duplicate_queries"] += 1
                    continue
                done.add(label)
                # A per-query floor (D552): this query alone starts later.
                floor_from = max(stage_floor, per_query.get(label, stage_floor))
                tally["query_from_applied"] += int(floor_from > stage_floor)
                # Incremental window (D-SWEEP1): resume from the (channel, query) watermark. The
                # publicDateMs lower bound is sent only past the base floor — a first-ever pass
                # at the channel floor sends none and scans the whole index like v3's.
                # A complete pass already covered this query through today (v1's
                # `completed_today`): asking again returns nothing new and costs a request; a
                # cap-`partial` one truncates the same way within relist_after_h (D226). A hole
                # below the coverage (the floor moved back) keeps it due.
                if not c.due(cid, label, until=today, relist_after=relist, partial_due=False,
                             floor=floor_from):
                    continue
                # Floor moved back (module docstring): each hole first, sent only its upper bound
                # (D642: a lone `publicDateMs<` is honoured, a two-sided range is dropped); the
                # hole's start is cut client-side.
                for hs, he in c.holes(cid, label, floor_from):
                    tally["holes"] += 1
                    h_from, h_to = hs.date().isoformat(), he.date().isoformat()
                    before_ms = _ms(h_to)
                    stats: dict[str, Any] = {}
                    hits, exhausted = fetch_forum(
                        base_url, str(item["term"]), item["tag"], limit,
                        numeric_filters(before_ms=before_ms, min_karma=min_karma),
                        stats=stats, af_only=af_only)
                    # Honour-detection: a dropped upper bound returns the whole index, a superset
                    # of the hole, so a complete pass still covers it; counted and said.
                    if _upper_bound_ignored(hits, before_ms):
                        tally["upper_bound_ignored"] += 1
                        say(f"warning: {ctx.stage}: {cid} {label!r}: the server ignored the upper "
                            f"bound publicDateMs<{h_to}; hits after it are cut, and the hole "
                            f"[{h_from}, {h_to}] is claimed only by a complete pass")
                    with c.window(cid, label, h_from, h_to) as w:
                        take(w, hits, cid, chan, item, label, base_url, floor_from, af_only,
                             observed, since=h_from, upto=h_to)
                        close(w, exhausted, stats)
                    count(exhausted, hits, w, cid, stats)
                mark = c.watermark(cid, label)
                if mark is not None and mark.date().isoformat() >= today:
                    continue  # only a hole was due; the tail is covered through today
                # Karma lag (D406): re-scan `lag` days below the watermark (module docstring).
                eff_from = floor_from if mark is None else max(
                    floor_from, (mark.date() - dt.timedelta(days=lag)).isoformat())
                windowed = eff_from > base_floor
                eff_from_ms = _ms(eff_from)
                stats = {}
                hits, exhausted = fetch_forum(
                    base_url, str(item["term"]), item["tag"], limit,
                    numeric_filters(since_ms=eff_from_ms if windowed else None, min_karma=min_karma),
                    stats=stats, af_only=af_only)
                tally["windowed"] += int(windowed)
                # Honor-detection backstop (module docstring): a dropped filter degrades to the
                # floor-range window, never to a wrong coverage claim.
                filter_ignored = windowed and _filter_ignored(hits, eff_from_ms)
                tally["filter_ignored"] += int(filter_ignored)
                covered_from = eff_from if (windowed and not filter_ignored) else floor_from
                with c.window(cid, label, covered_from, today) as w:
                    take(w, hits, cid, chan, item, label, base_url, floor_from, af_only, observed,
                         mark=mark.date().isoformat() if mark is not None else None)
                    close(w, exhausted, stats)
                count(exhausted, hits, w, cid, stats)

    c.counts.update(tally, queries_planned=planned, queries_selected=selected,
                    min_karma=int(min_karma or 0), query_limit=limit, karma_lag_days=lag)
    return c.finish()
