"""``collect.lw_graphql`` — every LessWrong post under a list of tags (default: the AI tag,
lesswrong.com/w/ai) via ForumMagnum's GraphQL API, whatever its karma.

**Why.** Shallow Review 2025 took "LessWrong posts under the AI tag" as a source
(``docs/scope/shallow-review.md`` (e)). ``collect.forums`` runs a keyword and tag search with a
karma >= 25 gate, which misses exactly the low-karma and off-keyword posts on decision theory,
anthropics, acausal trade, abstractions, brain-like AGI and field critiques the SR scope adds.
This stage lists the tag instead, with no karma gate; karma is recorded, not filtered on.

**Tags beyond AI** (option ``tags``, slugs). The AI tag alone misses SR-extension posts (the first
real window: "Social Theory and Agent Foundations" carried only World Modeling), so the stage
takes a list of tag slugs, each its own coverage query ``tag:<slug>`` with its own window,
watermark and overlap re-scan. :data:`SR_EXTENSION_TAGS` lists the SR-scope tags (decision
theory, anthropics, acausal trade, agent foundations, …); every one of them has posts without
the AI tag (probe 2026-10-12, below). A post under several queried tags is one source: the first
tag in the run observes it, later tags count it in their window's ``seen`` and in
``posts_multi_tag`` and do not observe it again.

**Query** (live-settled 2026-10-12 against ``https://www.lesswrong.com/graphql``, 5 probe
requests, plus 2 for the extension tags): a tag's ``_id`` comes from
``tags(input:{terms:{view:"tagBySlug", slug:…}})`` (``tag(input:{selector:{slug:…}})`` is
rejected: ``SelectorInput`` has no ``slug``); the ids are pinned in :data:`TAG_IDS` by slug
(option ``tag_ids: {slug: id}`` adds or overrides one; an unknown slug fails the run before any
request). The posts query is ``af_graphql``'s
``posts(input:{terms:{view:"new", after, limit, offset}})`` plus ``filterSettings:{tags:[{tagId,
filterMode:"Required"}]}`` (``tagName`` is not needed). The probes showed the filter honoured:
50 of 50 filtered posts carried the tag in ``tagRelevance``, against 2 of 50 in the unfiltered
``view:"new"`` page of the same day; offset paging over the filtered list is stable.
**Client-side backstop:** every post's ``tagRelevance`` is requested and a post whose entry for the
queried tag is absent or not positive is dropped from that tag's query, counted ``hits_not_tagged`` (as ``collect.forums``
counts ``hits_not_af``); so a server that stopped honouring the filter would show as a large
count, not as a flood of untagged rows.

**Rows.** One ``sources`` row per tagged post on ``sources.lw_graphql``: channel ``lesswrong``
(the same channel ``collect.forums`` writes for its LW hits), external id the post ``_id``,
``canonical_url = <base>/posts/<_id>``, key ``fm_post`` = the ``_id`` (so gather merges it with
the same post from ``collect.forums`` and, for a crosspost, ``collect.af_graphql``); ``metadata``
title, authors (user, then coauthors), ``published_at`` (``postedAt``), ``doc_type =
forum_post``; the ``htmlBody`` as an in-band ``post`` entry (``text/html`` raw, as
``af_graphql``). One ``coverage`` row per tag per run on ``coverage.lw_graphql``, query
``tag:<slug>`` (``tag:ai`` for the AI tag).

**Karma is recorded, not gated on.** ``baseScore`` is a counter, so it stays out of
``record_digest`` (C23; the channel's ``record_projection``). The GraphQL post record **without**
``htmlBody`` (JSON, sorted keys: ``_id``, ``title``, ``postedAt``, ``baseScore``, ``voteCount``,
``commentCount``, ``af``, ``url`` (a link post's target), ``slug``, ``tagRelevance`` (tag id →
relevance score), ``user``, ``coauthors``) is the row's ``provider_record`` in-band entry (D522;
canonical JSON in ``blobs/``).
The payload is the record as first seen or last changed: an unchanged re-seen post writes no row,
so its karma there is not refreshed.

**Late tagging and the overlap re-scan.** LessWrong tags posts after publication (authors,
moderators, an auto-tagger), so a watermark at the newest ``ok`` window end would miss a post
tagged after the window passed it. Each run therefore re-scans an overlap: ``eff_from =
max(floor, watermark − tag_lag_days)`` (option ``tag_lag_days``, default 14). A re-seen post
whose projected record is unchanged writes no row (``skip_unchanged``, counted
``sources_unchanged``); a changed one (title, date or body edited) writes a new row. A post tagged
more than ``tag_lag_days`` after its ``postedAt`` is still missed; a run with a lower ``from``
(the floor) picks it up.

**Paging and coverage** are ``af_graphql``'s: ``view:"new"`` is newest-first, so a
cap-truncated pass covered an unknown newest prefix and writes its window ``partial`` (never
advances the watermark, SHOULD-14); a fully paged pass writes ``[eff_from, today]`` ``ok``; a
failed page writes ``failed_*``.

**Date sub-windows** (option ``subwindow_days``, round 5 (RUN) B7a; :func:`stack.collect.base.
date_windows`). A backfill from a far floor holds more tagged posts than ``max_records`` (the AI
tag: ~370 a month, so ~4,600 from 2025-10-01 and ~8,000 from 2024-11-30) and, newest-first, a
capped pass banks nothing. With ``subwindow_days`` N > 0 a span ``[eff_from, today]`` longer than
N days is asked as consecutive N-day spans, oldest first, each its own coverage window ``[s, e]``
on the tag's query; the tag's sequence stops at the first span that is not ``ok``, and the next run
resumes after the last ``ok`` one (its end is the watermark). A span other than the last sends the
GraphQL ``before`` term as well; both bounds go out one day wider than the span (``after`` s − 1,
``before`` e + 1: ForumMagnum's day boundaries and inclusivity are not pinned), and the span is
cut client-side to ``s <= postedAt date < e`` (the last span: ``>= s``); a post outside it is
counted ``posts_outside_span`` and belongs to its neighbour. A post dated after the ``before``
sent (e + 1) means the server ignored ``before`` (counted ``before_ignored``): the span then pages through
every newer post first and is ``partial`` at the cap rather than wrong. Default 0: one window, as
before.

**Skip limit** (D645; :mod:`stack.collect.forummagnum`, "Skip limit"): one listing reaches ~2,200
posts; a later offset is refused ("Exceeded maximum value for skip"). Such a window is written
``partial`` (a server cap: ``windows_capped``, build reason ``limit``, counted ``skip_capped``) with
the posts reached as rows, never ``failed_permanent[parse_error]``. This stage does not split on it
(``collect.af_graphql`` does): the plan's 30-day spans hold far fewer posts per tag; lower
``subwindow_days`` if it shows.

**Floor moved back** (round 5 (RUN) B7a; :meth:`stack.collect.base.Collector.holes`): a ``from``
earlier than the tag's first window leaves a hole below its coverage; the hole is asked first as
bounded spans (as above: ``before`` sent, posts cut to it; split by ``subwindow_days``), then the
usual tail, so only the gap is listed again (counted ``holes``). A post dated before the floor is never registered, whatever the
server returned (date-floor backstop). No miss rows: ``lesswrong`` has ``miss_means: unknown``
and an ``after``-bounded listing never re-lists old posts, so deletion and un-tagging are not
detected.

Pacing (D26): ``1 / LW_DELAY_S`` through the harness transport (the ``af_graphql`` constant; LW
and AF are one backend), one attempt per page; a 429 fails the window, it does not halt the run.

Options (plan stage line ``options:``, D72):
  * ``window_days``  — floor = today − N days when ``from`` is unset
  * ``from``         — floor date (default: ``window_days``, else the channel's ``from_date``)
  * ``max_records``  / ``STACK_COLLECT_LIMIT`` — result cap per tag query (default: the
    channel's ``breadth`` preset ``max_results_per_query``)
  * ``tag_lag_days`` — overlap re-scanned below the watermark (default 14)
  * ``tags``         — tag slugs, one coverage query each (default ``[ai]``); ``max_records`` is per tag
  * ``subwindow_days`` — split a longer first window into spans of this many days (default 0 = no
    split; above)
  * ``tag_ids``      — ``{slug: ForumMagnum tag _id}`` added to or overriding :data:`TAG_IDS`
  * ``relist_after_h`` — passes (D226, :meth:`stack.collect.base.Collector.due`): a tag covered
    through today is skipped; a ``partial`` one is not asked again within this many hours (12)

The shared ForumMagnum pieces (date guard, response parse, paging loop, error mapping, post
normalisation) live in :mod:`stack.collect.forummagnum`; the query, the seams ``http_post``,
``_sleep``, ``PAGE_SIZE`` and the stage are this module's. Imports only ``stack.*`` and the
stdlib.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import time
from collections.abc import Callable
from typing import Any

from stack.collect import forummagnum as fm
from stack.collect.base import (
    RELIST_OPTIONS,
    Collector,
    Pacing,
    Transport,
    date_windows,
)
from stack.stage_runner import StageContext, StageResult

#: The ``options`` keys this stage's code reads; any other is refused (D434)
OPTIONS: frozenset[str] = frozenset({"from", "max_records", "subwindow_days", "tag_ids", "tag_lag_days", "tags",
                                     "window_days"}) | RELIST_OPTIONS

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────
CHANNEL = "lesswrong"
DEFAULT_BASE = "https://www.lesswrong.com"
#: The AI tag (slug ``ai``, name "AI", postCount 21192 on 2026-10-12), from
#: ``tags(input:{terms:{view:"tagBySlug", slug:"ai"}})``.
AI_TAG_SLUG = "ai"
AI_TAG_ID = "sYm3HiWcfZvrGu3ui"
DEFAULT_TAGS = (AI_TAG_SLUG,)
#: Tag slug → ForumMagnum ``_id`` (``tagBySlug`` probe 2026-10-12). Comment: postCount, and of
#: the tag's newest <= 100 posts (``view:"new"``) how many lack the AI tag. None is a subtag of AI
#: (no ``parentTag``) and none always co-occurs with it.
TAG_IDS: dict[str, str] = {
    AI_TAG_SLUG: AI_TAG_ID,
    "decision-theory": "X8JsWEnBRPvs5Y99i",             # 600; 65 of 100 lack AI
    "anthropics": "PbShukhzpLsWpGXkM",                  # 310; 83 of 100
    "acausal-trade": "tgJoX7PGDDh2vJNqT",               # 86; 44 of 72
    "agent-foundations": "RyNWXFjKNcafRKvPh",           # 361; 12 of 100 (all since 2025-08)
    "embedded-agency": "MP8NqPNATMqPrij4n",             # 151; 33 of 100
    "logical-induction": "8ckoduMw3gvCMJGSB",           # 48; 24 of 37
    "updateless-decision-theory": "5f5c37ee1b5cdee568cfb1dc",  # 52; 34 of 37 (an old 24-hex id)
    "functional-decision-theory": "fM6pmeSEncbzxoGpr",  # 61; 36 of 46
    "natural-abstraction": "9WnadaJsqsrKggBwn",         # 117; 17 of 94
    "infra-bayesianism": "wdLqQnzdgiYpDXEWH",           # 74; 14 of 69
    "game-theory": "b8FHrKqyXuYGWc6vn",                 # 431; 62 of 100
    "logical-uncertainty": "ye2H85NHoDLomm6BS",         # 85; 53 of 67
}
#: The Shallow Review scope extension (``docs/scope/shallow-review.md`` (c), (d) I1-I4, I6):
#: decision theory, anthropics, acausal trade, MIRI-style agent foundations, abstractions, game
#: theory, as LessWrong tags. Posts here often lack the AI tag, so the AI-tag query misses them.
#: ``game-theory`` also lists non-AI game theory (E1/E2 territory); triage filters those.
SR_EXTENSION_TAGS: tuple[str, ...] = tuple(s for s in TAG_IDS if s != AI_TAG_SLUG)
PAGE_SIZE = 100        # LW bodies are long; ForumMagnum serves up to 200 posts/page
LW_DELAY_S = 1.0       # politeness: ~1 request/s (af_graphql's constant; one backend)
HTTP_TIMEOUT_S = 60.0
DEFAULT_FROM = "2020-01-01"
DEFAULT_TAG_LAG_DAYS = 14
CONTENT_ROLE = "post"
BODY_MEDIA_TYPE = "text/html"  # GraphQL htmlBody: the canonical post HTML

BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0 Safari/537.36 stack-collect/1.0 (+research)"
)
PACING = Pacing(rps=1.0 / LW_DELAY_S, retry_delays_s=(), timeout_s=HTTP_TIMEOUT_S,
                headers={"User-Agent": BROWSER_UA})

_ENV_LIMIT = "STACK_COLLECT_LIMIT"
_DEFAULT_LIMIT = 200
_TAG_ID = re.compile(r"^[A-Za-z0-9]{1,40}$")

#: The post fields requested; everything but ``htmlBody`` goes into the ``provider_record``.
POST_FIELDS = (
    "_id title postedAt baseScore voteCount commentCount af url slug tagRelevance htmlBody "
    "user { displayName username } coauthors { displayName username }"
)


# ─────────────────────────────────────────────────────────────────────────────
# GraphQL query construction (pure; shape live-settled 2026-10-12)
# ─────────────────────────────────────────────────────────────────────────────
def as_tag_id(value: str, *, source: str) -> str:
    """Guard a tag id before it is interpolated inline into the query text: alphanumeric only
    (ForumMagnum ids are 17 such characters); raise ``ValueError`` naming ``source``."""
    if not _TAG_ID.match(value):
        raise ValueError(f"{source}: {value!r} is not a ForumMagnum tag id (alphanumeric)")
    return value


def build_posts_query(tag_id: str, after: str, offset: int, limit: int,
                      before: str | None = None) -> str:
    """The ``posts`` query for one page: ``view:"new"`` (newest-first) from ``after`` (and up to
    ``before`` when given: a date sub-window), only posts carrying ``tag_id`` (``filterSettings``
    ``Required``). ``after``, ``before`` and ``tag_id`` are validated at the call site
    (:func:`stack.collect.forummagnum.as_query_date`, :func:`as_tag_id`); ``offset``/``limit`` are
    coerced to ``int`` here, so the inline interpolation cannot inject."""
    upper = f'before: "{before}", ' if before is not None else ""
    return (
        '{ posts(input: {terms: {view: "new", '
        f'limit: {int(limit)}, offset: {int(offset)}, after: "{after}", {upper}'
        f'filterSettings: {{tags: [{{tagId: "{tag_id}", filterMode: "Required"}}]}}'
        f"}}}}) {{ results {{ {POST_FIELDS} }} }} }}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# HTTP (the run's harness transport; module-level so offline tests monkeypatch it)
# ─────────────────────────────────────────────────────────────────────────────
_T: Transport | None = None


def http_post(url: str, payload: Any, *, timeout: float = HTTP_TIMEOUT_S) -> tuple[int, Any]:
    """One paced JSON POST through the run's transport; ``(status, parsed_json_or_None)``, ``0``
    = transport failure. ``timeout`` is kept for the seam's signature; ``PACING.timeout_s``
    applies."""
    if _T is None:
        raise RuntimeError("lw_graphql.http_post: no transport outside run(ctx)")
    return _T.post_json(url, payload)


def _sleep(seconds: float) -> None:
    """Politeness delay (module-level so offline tests monkeypatch it to a no-op); the transport's
    pacer sleeps through it."""
    time.sleep(seconds)


def fetch_tagged(
    base_url: str,
    tag_id: str,
    after: str,
    limit: int,
    *,
    before: str | None = None,
    on_page: Callable[[], None] | None = None,
    stats: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Page the tag's posts newest-first from ``after`` (up to ``before`` when given) up to
    ``limit``; ``(posts, exhausted)`` as :func:`stack.collect.forummagnum.page_descending`
    (``PAGE_SIZE`` and ``http_post`` read at call time, the test seams)."""
    def page(offset: int, size: int) -> tuple[int, list[dict[str, Any]] | None]:
        query = build_posts_query(tag_id, after, offset, size, before)
        status, data = http_post(f"{base_url}/graphql", {"query": query})
        return status, fm.posts_results(status, data)

    return fm.page_descending(page, limit, PAGE_SIZE, on_page=on_page, stats=stats)


# ─────────────────────────────────────────────────────────────────────────────
# Post handling
# ─────────────────────────────────────────────────────────────────────────────
def has_tag(post: dict[str, Any], tag_id: str) -> bool:
    """The backstop: the post's ``tagRelevance`` gives ``tag_id`` a positive score."""
    rel = post.get("tagRelevance")
    if not isinstance(rel, dict):
        return False
    score = rel.get(tag_id)
    return isinstance(score, (int, float)) and not isinstance(score, bool) and score > 0


def record_payload(post: dict[str, Any]) -> dict[str, Any]:
    """The ``provider_record`` (D522): the GraphQL post record without ``htmlBody`` (the body is the
    in-band ``post`` entry)."""
    return {k: v for k, v in post.items() if k != "htmlBody"}


def _as_of_date() -> str:
    """Today's date (ISO), the upper bound a complete sweep covers. Module-level so tests can
    monkeypatch a fixed 'now'."""
    return dt.datetime.now(dt.UTC).date().isoformat()


def floor_date(c: Collector, chan_from: str | None, today: str) -> str:
    """The sweep's floor (never registers a post before it): option ``from``, else today −
    ``window_days``, else the channel's ``from_date``, else ``DEFAULT_FROM``; a strict query
    date (it is interpolated into the GraphQL text)."""
    explicit = c.option("from", None, cast=str)
    if explicit:
        return fm.as_query_date(explicit.strip(), source="LW floor date (option from)")
    days = c.option("window_days", None, cast=int)
    if days:
        return (dt.date.fromisoformat(today) - dt.timedelta(days=days)).isoformat()
    return fm.as_query_date((chan_from or DEFAULT_FROM).strip(),
                            source="LW floor date (channels.yaml from_date)")


# ─────────────────────────────────────────────────────────────────────────────
# The stage
# ─────────────────────────────────────────────────────────────────────────────
def run(ctx: StageContext) -> StageResult:
    global _T
    from stack.config import load

    c = Collector(ctx)
    chan = c.channel(CHANNEL)
    base_url = (chan.location or DEFAULT_BASE).rstrip("/")
    today = _as_of_date()
    floor_from = floor_date(c, chan.from_date, today)
    tags = resolve_tags(c.option("tags", list(DEFAULT_TAGS)), c.option("tag_ids", {}))
    lag = c.option("tag_lag_days", DEFAULT_TAG_LAG_DAYS, cast=int)
    if lag < 0:
        raise ValueError(f"option tag_lag_days: {lag} < 0")
    span_days = c.option("subwindow_days", 0, cast=int)
    if span_days < 0:
        raise ValueError(f"option subwindow_days: {span_days} < 0")
    configured = load().channels.max_results_per_query(CHANNEL, _DEFAULT_LIMIT)
    limit = max(1, c.option("max_records", configured, env=_ENV_LIMIT, cast=int))
    _T = c.transport(PACING, sleep=lambda s: _sleep(s))

    observed: set[str] = set()  # post ids handled this run, across tags
    tally = {"seen": 0, "new": 0, "not_tagged": 0, "before_floor": 0, "multi_tag": 0,
             "fetched": 0, "outside_span": 0, "before_ignored": 0, "spans": 0, "holes": 0,
             "skip_capped": 0}
    relist = c.relist_after()
    with c.halts():
        for done, (slug, tag_id) in enumerate(tags):
            c.progress(done=done, total=len(tags))
            # Passes (D226): a tag covered through today is not re-scanned (its tag_lag_days
            # overlap is re-read on the next day's run); a cap-`partial` one returns the same
            # newest prefix within relist_after_h.
            if not c.due(CHANNEL, tag_query(slug), until=today, relist_after=relist,
                         partial_due=False, floor=floor_from):
                continue
            exhausted = sweep_tag(ctx, c, chan, base_url, slug, tag_id, floor_from, today, lag,
                                  limit, observed, tally, span_days=span_days)
            c.counts[f"queries_{exhausted}"] = c.counts.get(f"queries_{exhausted}", 0) + 1
    c.progress(done=len(tags), total=len(tags))

    c.counts.update({"posts_fetched": tally["fetched"], "posts_seen": tally["seen"],
                     "posts_new": tally["new"], "hits_not_tagged": tally["not_tagged"],
                     "posts_before_floor": tally["before_floor"],
                     "posts_multi_tag": tally["multi_tag"], "query_limit": limit,
                     "tag_lag_days": lag, "tags": len(tags), "subwindow_days": span_days,
                     "spans": tally["spans"], "posts_outside_span": tally["outside_span"],
                     "before_ignored": tally["before_ignored"], "holes": tally["holes"],
                     "skip_capped": tally["skip_capped"]})
    return c.finish()


def resolve_tags(slugs: Any, extra_ids: Any) -> list[tuple[str, str]]:
    """The run's ``(slug, tag_id)`` list from option ``tags`` (slugs, order kept, repeats
    dropped) over :data:`TAG_IDS` updated with option ``tag_ids``; an unknown slug, a malformed
    id or an empty list raises ``ValueError`` (before any request)."""
    if isinstance(slugs, str):
        slugs = [slugs]
    if not isinstance(slugs, (list, tuple)) or not slugs:
        raise ValueError(f"option tags: {slugs!r} is not a non-empty list of tag slugs")
    if not isinstance(extra_ids, dict):
        raise TypeError(f"option tag_ids: {extra_ids!r} is not a {{slug: id}} mapping")
    ids = {**TAG_IDS, **{str(k): str(v) for k, v in extra_ids.items()}}
    out: list[tuple[str, str]] = []
    for slug in dict.fromkeys(str(s).strip() for s in slugs):
        if slug not in ids:
            raise ValueError(f"option tags: unknown tag slug {slug!r} (pinned: {sorted(ids)}; "
                             f"add its id under option tag_ids)")
        out.append((slug, as_tag_id(ids[slug], source=f"tag id for {slug!r}")))
    return out


def tag_query(slug: str) -> str:
    """A tag's coverage query (its watermark key)."""
    return f"tag:{slug}"


def sweep_tag(ctx: StageContext, c: Collector, chan: Any, base_url: str, slug: str, tag_id: str,
              floor_from: str, today: str, lag: int, limit: int, observed: set[str],
              tally: dict[str, int], *, span_days: int = 0) -> str:
    """One tag's coverage query ``tag:<slug>``: its window ``[eff_from, today]``, as consecutive
    spans when ``span_days`` splits it (module docstring), each fetched and written by
    :func:`sweep_span`; the sequence stops at the first span that is not ``ok``. Returns that
    span's ``exhausted`` receipt (``complete`` when every span was). A post already handled this run
    under another tag counts in the span's ``seen`` and in ``multi_tag`` but is not observed
    again."""
    query_key = tag_query(slug)
    this_query: set[str] = set()
    worst = "complete"
    # A floor moved back (base "Floor moved back"): each hole below the coverage first, as
    # bounded spans (`before` sent, posts cut to the hole), split by `span_days` like the tail.
    for hs, he in c.holes(CHANNEL, query_key, floor_from):
        tally["holes"] += 1
        for s, e in date_windows(hs.date(), he.date(), span_days):
            tally["spans"] += 1
            got = sweep_span(ctx, c, chan, base_url, slug, tag_id, floor_from, s.isoformat(),
                             e.isoformat(), False, True, limit, observed, this_query, tally)
            if got != "complete":
                worst = got
                break  # the rest of this hole stays a hole for the next run
    # Late tagging: re-scan `lag` days below the watermark (module docstring).
    mark = c.watermark(CHANNEL, query_key)
    if mark is not None and mark.date().isoformat() >= today:
        return worst  # only a hole was due; the tail is covered through today
    eff_from = floor_from
    if mark is not None:
        rescan = (mark.date() - dt.timedelta(days=lag)).isoformat()
        eff_from = max(floor_from, fm.as_query_date(
            rescan, source=f"coverage watermark − tag_lag_days ({CHANNEL}/{query_key})"))
    spans = date_windows(dt.date.fromisoformat(eff_from), dt.date.fromisoformat(today), span_days)
    exhausted = "complete"
    for i, (s, e) in enumerate(spans):
        last = i == len(spans) - 1
        tally["spans"] += 1
        exhausted = sweep_span(ctx, c, chan, base_url, slug, tag_id, floor_from, s.isoformat(),
                               e.isoformat(), last, len(spans) > 1, limit, observed, this_query,
                               tally)
        if exhausted != "complete":
            break  # a later `ok` span would carry the watermark over this one (base.date_windows)
    return exhausted if exhausted != "complete" else worst


def sweep_span(ctx: StageContext, c: Collector, chan: Any, base_url: str, slug: str, tag_id: str,
               floor_from: str, start: str, end: str, last: bool, split: bool, limit: int,
               observed: set[str], this_query: set[str], tally: dict[str, int]) -> str:
    """One coverage window ``[start, end]`` of the tag's query: fetch, backstops, rows. ``split``:
    the window is one of several spans (slack bounds, the client-side span cut; module
    docstring), else the whole window as before (``after`` = ``start``, no ``before``)."""
    query_key = tag_query(slug)
    src = f"date span ({CHANNEL}/{query_key})"
    after = start
    before = None
    if split:
        after = fm.as_query_date((dt.date.fromisoformat(start) - dt.timedelta(days=1)).isoformat(),
                                 source=src)
        if not last:
            before = fm.as_query_date(
                (dt.date.fromisoformat(end) + dt.timedelta(days=1)).isoformat(), source=src)
    stats: dict[str, Any] = {}
    exhausted = "error"  # stays so if a run-level halt ends the window before the pull returns
    seen = kept = 0
    with c.window(CHANNEL, query_key, start, end) as w:
        posts, exhausted = fetch_tagged(base_url, tag_id, after, limit, before=before, stats=stats)
        tally["fetched"] += len(posts)
        for p in posts:
            post_id = str(p.get("_id") or "")
            if not post_id or post_id in this_query:
                continue
            # NO karma gate. Two client-side backstops: the tag filter and the date floor.
            if not has_tag(p, tag_id):
                tally["not_tagged"] += 1
                continue
            pd = fm.posted_date(p)
            if pd is not None and pd < floor_from:
                tally["before_floor"] += 1
                continue
            if split and pd is not None and (pd < start or (not last and pd >= end)):
                tally["outside_span"] += 1
                if before is not None and pd > before:
                    tally["before_ignored"] += 1
                continue  # a neighbouring span's post
            this_query.add(post_id)
            seen += 1
            if post_id in observed:
                tally["multi_tag"] += 1
                c.attribute(CHANNEL, post_id, query=query_key, label=slug)  # D228
                continue  # one source: observed under an earlier tag this run
            observed.add(post_id)
            tally["seen"] += 1
            if c.prior(CHANNEL, post_id) is None:
                kept += 1
                tally["new"] += 1
            title = str(p.get("title") or "").strip()
            row = c.observe(
                CHANNEL, post_id, p,
                keys=fm.fm_keys(chan.channel_group, CHANNEL, post_id),
                canonical_url=f"{base_url}/posts/{post_id}",
                metadata={"title": title or None, "authors": fm.post_authors(p) or None,
                          "published_at": fm.posted_at(p), "doc_type": "forum_post"},
                provider_record=record_payload(p),
                skip_unchanged=True,
                query=query_key, label=slug,  # matched_by (D228)
            )
            if row is None:
                continue  # re-seen in the overlap, unchanged: no row, no in-band offer
            body = p.get("htmlBody")
            c.add_inband(row["source_id"], CONTENT_ROLE,
                         raw=body.encode("utf-8") if isinstance(body, str) and body else None,
                         media_type=BODY_MEDIA_TYPE)
        w.seen, w.kept = seen, kept
        if exhausted == "error":
            error_class, permanent = fm.error_class_of(int(stats.get("status") or 0))
            w.fail(error_class, permanent=permanent)
        elif exhausted == "skip_cap":
            tally["skip_capped"] += 1
            w.truncated()  # the server's skip limit (D645): a cap, the posts so far are rows
        else:
            w.close(exhausted)
    return exhausted
