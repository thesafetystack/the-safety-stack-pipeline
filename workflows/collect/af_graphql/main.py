"""``collect.af_graphql`` — the COMPLETE Alignment Forum corpus via ForumMagnum's GraphQL API
(§5.6 LW/AF row; a completeness axis the keyword ``collect.forums`` sweep cannot give).

The Alignment Forum is small — ~4,752 posts *ever* (live-verified 2026-07-23). Rather than lean
on keyword recall for the one forum that matters most, this stage pages the WHOLE forum through
ForumMagnum's public GraphQL endpoint (``<base>/graphql``): ``posts(input:{terms:{view:"new", …}})``
with an ``after`` date term (and a ``before`` term for a bounded span) and ``limit``/``offset``
pagination, at 200 per page, so keyword recall never has to be the completeness story for this
channel. One listing reaches at most ~2,200 posts (the server's skip limit, below), so the forum is
listed in date spans, not in one pull.

**Rows.** Every post is one ``sources`` row on ``sources.af_graphql`` (channel ``alignmentforum``,
external id the post ``_id``, ``canonical_url = <base>/posts/<_id>``, key ``fm_post`` = the ``_id``
typed by :mod:`stack.scholarly_records`), carrying ``metadata`` title / authors (user, then
coauthors) / ``published_at`` (``postedAt``) / ``doc_type = forum_post`` and the ``htmlBody`` as an
in-band ``post`` entry (§5.4; the raw HTML minted through the harness into ``blobs/``,
``media_type text/html``; acquire converts it). ``collect.forums`` writes the same post under the
``lesswrong`` and/or ``alignmentforum`` channel in its own chain with the same ``fm_post`` key, so
``gather`` makes an LW/AF crosspost one document with several members (§6 forum group). The one
sweep writes ONE ``coverage`` row on ``coverage.af_graphql`` (query ``af:all``).

**No karma gate (unlike ``collect.forums``).** The Alignment Forum is an edited, curated venue
and the point here is COMPLETENESS, so every post is registered regardless of ``baseScore``. The
only client-side gate is a date floor backstop (below).

**Incremental descending sweep (coverage query ``af:all``).** Its watermark is the newest ``ok``
coverage row's ``window_end_at`` for ``(alignmentforum, af:all)`` (:meth:`Collector.watermark`).
``after = max(floor_from, watermark)`` and pagination walks ``view:"new"``, which returns posts
**newest-first (date-DESCENDING)**. Descending order is why a cap-truncated pass can claim NOTHING:
it covered the newest unknown *prefix* of ``[after, today]``, so its window is written ``partial``
(never advances the watermark; v3 wrote the empty window ``from == to``, SHOULD-14). A
FULLY-PAGINATED pass (a page returns fewer than ``limit``, or empty) has covered every post in
``[after, today]`` and writes that window ``ok``. A failed page writes it ``failed_*``.

The GraphQL ``after`` term is server-honored (live-verified 2026-07-24), so no honor-detection is
needed (unlike ``collect.forums``' Algolia filter). The date-floor gate is the belt-and-braces
backstop: a post dated before ``floor_from`` is never registered whatever the server returned.

**Promotion lag (D551).** A LessWrong post can be moved to the Alignment Forum days after it was
posted, and ``view:"new"``'s ``after`` term is on ``postedAt``: a post promoted after a run lies
below the next run's watermark. The tail is therefore asked from ``max(floor, watermark −
lookback_days)`` (option, default 14, as ``collect.forums``' ``karma_lag_days``, D406), and its
window is still written from the watermark. A post dated before the watermark whose projected
record did not change writes no row (``skip_unchanged`` over the channel's ``record_projection``,
which leaves ``baseScore`` out; counted ``sources_unchanged``), so the overlap is idempotent. When
the cap stops a pull that started in the look-back, the posts are kept and the window is asked
again from the watermark (counted ``lookback_capped``). A post promoted more than ``lookback_days``
after it was posted is still missed.

**Floor moved back** (round 5 (RUN) B7a; :meth:`stack.collect.base.Collector.holes`; D645): a
``from`` earlier than the first window leaves a hole below the coverage, asked before the tail as
bounded spans of ``subwindow_days`` (default :data:`DEFAULT_SUBWINDOW_DAYS`; ``0`` = one span per
hole), oldest first, each its own coverage window ``[s, e]``; a hole's sequence stops at the first
span that is not ``ok`` and the rest stays a hole for the next run. As ``collect.lw_graphql``'s
spans, both bounds go out one day wider (``after`` s − 1, ``before`` e + 1: ForumMagnum's day
boundaries and time zone are not pinned) and posts dated outside ``s <= date < e`` are cut
client-side (``posts_outside_span``; one dated after the ``before`` sent counts ``before_ignored``:
the server dropped the bound). A ``failed_*`` window covers nothing (holes and the watermark read
``ok`` rows only), so a hole whose window failed in an earlier run is asked again by the next one.

**Skip limit and adaptive split** (D645; :mod:`stack.collect.forummagnum`, "Skip limit"). The
GraphQL refuses an ``offset`` past ~2,000 ("Exceeded maximum value for skip"), so one listing
reaches ~2,200 posts. Before D645 the hole was one listing unbounded above, newest-first: a hole
older than ~2,200 posts back from today was refused mid-pull and filed
``failed_permanent[parse_error]`` in every build (the 2020-2023 hole of 2026-10). Now a span the
server refuses is split in two halves at its middle day (the older half bounded above, the newer
one keeps the span's upper bound or open end), asked oldest first, recursively; the refused span
writes no coverage row (counted ``spans_split``). A span under :data:`MIN_SPLIT_DAYS` days that is
still refused cannot be split: it is written ``partial`` (a server cap: ``windows_capped``, build
reason ``limit``, counted ``skip_capped``) with the posts it reached as rows.

**Tail.** The tail ``[watermark, today]`` is one listing open above, newest-first, as before (the
weekly case: a few dozen posts); only a refused tail (a first pass from a far floor) is split as
above, its newest half still open above.

**Deletion.** ``channels.yaml`` gives ``alignmentforum`` no ``miss_means`` (default ``unknown``)
and ``view:"new"`` with an ``after`` bound never re-lists an old post, so this stage writes no miss
rows and does not detect deleted posts (§5.6's "``withdrawn_at`` on that member" needs a per-id
re-check this stage does not make).

Pacing (D26): requests go through the harness transport (``HostPacer`` at ``1 / AF_DELAY_S``); v3's
explicit 1 s sleep between pages is that pacer now. One attempt per page (v3: no retry ladder); a
429 does not halt the run, it fails the window.

Options (plan stage line ``options:``, D72; the v3 env var wins when set):
  * ``max_records`` / ``STACK_COLLECT_LIMIT`` — result cap for the single sweep (default: the
    channel's ``breadth`` preset ``max_results_per_query``; keep it above the corpus size for a
    completing first pass — see the descending-order note)
  * ``from``        / ``STACK_AF_FROM``       — floor from-date (default channel ``from_date``)
  * ``window_days``                           — floor = today − N days when ``from`` is unset
  * ``lookback_days``                         — days below the watermark the tail re-asks
    (promotion lag, D551; default 14, 0 = none)
  * ``subwindow_days``                        — a hole is listed in bounded spans of this many
    days (D645; default :data:`DEFAULT_SUBWINDOW_DAYS`, 0 = one bounded span per hole); a span the
    server's skip limit refuses is split further either way
  * ``relist_after_h``                        — passes (D226, :meth:`stack.collect.base.Collector.
    due`): a sweep covered through today is skipped; a ``partial`` one is not asked again within
    this many hours (12)

Cardinal rule: imports only ``stack.*`` and the stdlib; nothing from other workflows.
"""

from __future__ import annotations

import datetime as dt
import time
from collections.abc import Callable
from typing import Any, NamedTuple

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
OPTIONS: frozenset[str] = frozenset({"from", "lookback_days", "max_records", "subwindow_days",
                                     "window_days"}) | RELIST_OPTIONS

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────
CHANNEL = "alignmentforum"
QUERY_KEY = "af:all"  # the single coverage query key (the whole-forum sweep)
DEFAULT_BASE = "https://www.alignmentforum.org"
PAGE_SIZE = 200        # ForumMagnum serves up to 200 posts/page
AF_DELAY_S = 1.0       # politeness: ~1 request/s (live-verified friendly)
HTTP_TIMEOUT_S = 60.0
DEFAULT_FROM = "2020-01-01"
CONTENT_ROLE = "post"
BODY_MEDIA_TYPE = "text/html"  # GraphQL htmlBody: the canonical post HTML

BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0 Safari/537.36 stack-collect/1.0 (+research)"
)
PACING = Pacing(rps=1.0 / AF_DELAY_S, retry_delays_s=(), timeout_s=HTTP_TIMEOUT_S,
                headers={"User-Agent": BROWSER_UA})

_ENV_LIMIT = "STACK_COLLECT_LIMIT"
_ENV_FROM = "STACK_AF_FROM"
_DEFAULT_LIMIT = 200
#: Default ``lookback_days`` (D551): posts moved to the Alignment Forum after they were posted.
DEFAULT_LOOKBACK_DAYS = 14
#: Default ``subwindow_days`` (D645): a hole's span length. The Alignment Forum has had a few
#: hundred to ~900 posts a year (2015-2026), so 180 days stay far under the ~2,200 posts one
#: listing reaches; a denser span is split anyway.
DEFAULT_SUBWINDOW_DAYS = 180
#: A span shorter than this many days is not split on a skip refusal (written ``partial``, D645).
MIN_SPLIT_DAYS = 2


# ─────────────────────────────────────────────────────────────────────────────
# GraphQL query construction (pure — unit-testable; live-verified 2026-07-24)
# ─────────────────────────────────────────────────────────────────────────────
# The strict-date guard, the failed-page error mapping and the post normalisation are shared with
# collect.lw_graphql (stack.collect.forummagnum); the names below are this module's (tests use them).
_as_query_date = fm.as_query_date
error_class_of = fm.error_class_of
_posted_date = fm.posted_date
_posted_at = fm.posted_at
_post_authors = fm.post_authors
fm_keys = fm.fm_keys


def build_posts_query(after: str, offset: int, limit: int, before: str | None = None) -> str:
    """The ForumMagnum GraphQL ``posts`` query for one page (the exact shape live-verified against
    ``https://www.alignmentforum.org/graphql`` on 2026-07-24; ``before`` as ``collect.lw_graphql``
    sends it, D645).

    ``view:"new"`` returns posts newest-first (date-DESCENDING); ``after`` is the (server-honored)
    lower date bound, ``before`` (when given) the upper one of a bounded span; ``limit``/``offset``
    page the result. Selected fields are exactly what the
    identity + metadata + in-band body need: ``_id`` (external id), ``title``, ``postedAt`` (pub
    date), ``baseScore`` (recorded, never gated on), ``htmlBody`` (the in-band body), ``slug``, and
    author/coauthor display names. ``after`` and ``before`` are strict ``YYYY-MM-DD`` (validated by
    ``_as_query_date`` at the call site) and ``offset``/``limit`` are coerced to ``int`` here, so
    the inline interpolation cannot inject."""
    upper = f', before: "{before}"' if before is not None else ""
    return (
        '{ posts(input: {terms: {view: "new", '
        f'limit: {int(limit)}, offset: {int(offset)}, after: "{after}"{upper}'
        "}}) { results { "
        "_id title postedAt baseScore htmlBody slug "
        "user { displayName username } coauthors { displayName username } "
        "} } }"
    )


# ─────────────────────────────────────────────────────────────────────────────
# HTTP (the run's harness transport; module-level so offline tests monkeypatch it)
# ─────────────────────────────────────────────────────────────────────────────
_T: Transport | None = None


def http_post(url: str, payload: Any, *, timeout: float = HTTP_TIMEOUT_S) -> tuple[int, Any]:
    """One paced JSON POST through the run's transport; returns ``(status, parsed_json_or_None)``.
    A transport failure returns ``(0, None)`` so the caller records a failed window rather than
    crashing. ``timeout`` is kept for the seam's signature; ``PACING.timeout_s`` applies."""
    if _T is None:
        raise RuntimeError("af_graphql.http_post: no transport outside run(ctx)")
    return _T.post_json(url, payload)


def _sleep(seconds: float) -> None:
    """Politeness delay (module-level so offline tests monkeypatch it to a no-op); the transport's
    pacer sleeps through it."""
    time.sleep(seconds)


def _posts_page(
    base_url: str, after: str, offset: int, limit: int, before: str | None = None
) -> tuple[int, Any]:
    """One GraphQL ``posts`` page; returns ``(status, results)`` as
    :func:`stack.collect.forummagnum.posts_results`: the list, ``SKIP_REFUSED`` for the skip-limit
    refusal (D645), ``None`` for a non-200, another GraphQL error envelope or a malformed shape (the
    caller folds those into a single ``error`` outcome)."""
    query = build_posts_query(after, offset, limit, before)
    status, data = http_post(f"{base_url}/graphql", {"query": query})
    return status, fm.posts_results(status, data)


def fetch_af(
    base_url: str,
    after: str,
    limit: int,
    *,
    before: str | None = None,
    on_page: Callable[[], None] | None = None,
    stats: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Page ``view:"new"`` posts newest-first from ``after`` (up to ``before`` when given) up to
    ``limit``. Returns ``(posts, exhausted)`` with ``exhausted`` ∈ {complete, truncated, skip_cap,
    error}: ``complete`` = the API ran out of results before the cap; ``truncated`` = the cap
    stopped a DESCENDING pull, so an unknown newest prefix was covered and the caller claims
    NOTHING (SHOULD-14); ``skip_cap`` = the server refused the next offset (its skip limit, D645),
    likewise a newest prefix; ``error`` = a page failed/garbled mid-pull (``stats['status']`` = its
    HTTP status, ``0`` = transport failure). ``on_page`` (when given) is called once per fetched
    page for progress. The loop is :func:`stack.collect.forummagnum.page_descending`;
    ``PAGE_SIZE`` and ``http_post`` are this module's, read at call time (the test seams)."""
    return fm.page_descending(lambda offset, size: _posts_page(base_url, after, offset, size, before),
                              limit, PAGE_SIZE, on_page=on_page, stats=stats)


# ─────────────────────────────────────────────────────────────────────────────
# Coverage window helpers
# ─────────────────────────────────────────────────────────────────────────────
class Span(NamedTuple):
    """One coverage window ``[start, end]`` and how it is listed (module docstring, D645): ``ask``
    is the ``after`` term sent, ``before`` the ``before`` term (``None`` = open above, the
    newest-first tail); ``cut_lo`` / ``cut_hi``: a post dated before ``start`` / on or after
    ``end`` belongs to a neighbouring span and is cut client-side."""

    start: dt.date
    end: dt.date
    ask: dt.date
    before: dt.date | None
    cut_lo: bool
    cut_hi: bool


_DAY = dt.timedelta(days=1)


def hole_span(start: dt.date, end: dt.date) -> Span:
    """A bounded span of a hole: bounds one day wider, posts cut to ``start <= date < end``."""
    return Span(start, end, start - _DAY, end + _DAY, True, True)


def tail_span(start: dt.date, end: dt.date, ask: dt.date) -> Span:
    """The tail ``[start, end]``: asked from ``ask`` (the look-back, D551), open above, no cut."""
    return Span(start, end, ask, None, False, False)


def split_span(span: Span) -> tuple[Span, Span] | None:
    """``span`` halved at its middle day (D645), or ``None`` under :data:`MIN_SPLIT_DAYS`: the older
    half keeps ``ask`` and the lower cut, is bounded at the middle day + 1 and cut there; the newer
    half is asked from the middle day − 1, cut below it, and keeps ``before`` and the upper cut."""
    days = (span.end - span.start).days
    if days < MIN_SPLIT_DAYS:
        return None
    mid = span.start + dt.timedelta(days=days // 2)
    return (Span(span.start, mid, span.ask, mid + _DAY, span.cut_lo, True),
            Span(mid, span.end, mid - _DAY, span.before, True, span.cut_hi))


class _SplitSpan(Exception):
    """Raised inside a coverage window whose listing the skip limit refused and that can be split:
    :meth:`stack.collect.base.Collector.window` writes no row for an exception that is not a halt,
    and the caller lists the two halves instead."""


def record_payload(post: dict[str, Any]) -> dict[str, Any]:
    """The ``provider_record`` (D522): the GraphQL post without ``htmlBody`` (the in-band ``post``
    entry), as ``collect.lw_graphql``'s."""
    return {k: v for k, v in post.items() if k != "htmlBody"}


def _as_of_date() -> str:
    """Today's date (ISO) — the upper bound a COMPLETE sweep covers. Module-level so tests can
    monkeypatch a fixed 'now'."""
    return dt.datetime.now(dt.UTC).date().isoformat()


def floor_date(c: Collector, chan_from: str | None, today: str) -> str:
    """The sweep's floor (never registers a post before it): ``from`` (env ``STACK_AF_FROM``
    wins), else today − ``window_days``, else the channel's ``from_date``, else ``DEFAULT_FROM``;
    validated as a strict query date (it is interpolated into the GraphQL text)."""
    explicit = c.option("from", None, env=_ENV_FROM, cast=str)
    if explicit:
        return _as_query_date(explicit.strip(), source=f"AF floor date ({_ENV_FROM} / option from)")
    days = c.option("window_days", None, cast=int)
    if days:
        return (dt.date.fromisoformat(today) - dt.timedelta(days=days)).isoformat()
    return _as_query_date((chan_from or DEFAULT_FROM).strip(),
                          source="AF floor date (channels.yaml from_date)")


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
    # Strict-date guard on every value interpolated inline into the query (injection): the floor
    # here, and the watermark-derived date below — fail fast, naming the source.
    floor_from = floor_date(c, chan.from_date, today)
    configured = load().channels.max_results_per_query(CHANNEL, _DEFAULT_LIMIT)
    limit = max(1, c.option("max_records", configured, env=_ENV_LIMIT, cast=int))
    lag = c.option("lookback_days", DEFAULT_LOOKBACK_DAYS, cast=int)
    if lag < 0:
        raise ValueError(f"option lookback_days: {lag} < 0")
    span_days = c.option("subwindow_days", DEFAULT_SUBWINDOW_DAYS, cast=int)
    if span_days < 0:
        raise ValueError(f"option subwindow_days: {span_days} < 0")
    _T = c.transport(PACING, sleep=lambda s: _sleep(s))
    # Passes (D226): a sweep covered through today is not asked again; a cap-`partial` one
    # returns the same newest prefix within relist_after_h. A hole below the coverage (the floor
    # moved back) keeps it due.
    if not c.due(CHANNEL, QUERY_KEY, until=today, relist_after=c.relist_after(),
                 partial_due=False, floor=floor_from):
        c.counts.update({"query_limit": limit, "lookback_days": lag})
        return c.finish()

    observed: set[str] = set()
    totals = {"fetched": 0, "seen": 0, "kept": 0, "holes": 0, "outside_span": 0, "lookback_capped": 0,
              "spans": 0, "spans_split": 0, "skip_capped": 0, "before_ignored": 0}

    def query_date(d: dt.date | None, what: str) -> str | None:
        return None if d is None else _as_query_date(d.isoformat(), source=f"{what} ({QUERY_KEY})")

    def ask_span(span: Span, *, counted: bool = True) -> str:
        """List one span (:func:`sweep`); a span the skip limit refused is listed as its two
        halves instead, oldest first, the newer one only when the older one completed (a later
        `ok` window must not carry the watermark over a gap)."""
        totals["spans"] += int(counted)
        try:
            return sweep(span)
        except _SplitSpan:
            halves = split_span(span)
            assert halves is not None  # sweep raises only for a splittable span
            totals["spans_split"] += 1
            got = ask_span(halves[0])
            return ask_span(halves[1]) if got == "complete" else got

    def sweep(span: Span) -> str:
        """One coverage window ``[span.start, span.end]``: page ``view:"new"`` from ``span.ask``
        (below ``start``: the tail's look-back, D551) up to ``span.before``; posts outside the
        span's cuts are left to its neighbours (module docstring)."""
        stats: dict[str, Any] = {}
        got = "error"  # stays so if a run-level halt ends the window before the pull returns
        after = query_date(span.ask, "span after")
        before = query_date(span.before, "span before")
        start = query_date(span.start, "span start")
        assert after is not None and start is not None
        with c.window(CHANNEL, QUERY_KEY, span.start.isoformat(), span.end.isoformat()) as w:
            posts, got = fetch_af(base_url, after, limit, before=before, stats=stats)
            if got == "skip_cap" and split_span(span) is not None:
                raise _SplitSpan  # no row for this window: its halves are listed instead
            if after < start and got == "truncated":
                # The cap stopped a pull that started in the look-back: keep its posts, then ask
                # from the window start (D551; else the window would stay capped every run).
                totals["lookback_capped"] += 1
                record(w, posts, span)
                stats = {}
                posts, got = fetch_af(base_url, start, limit, before=before, stats=stats)
                if got == "skip_cap" and split_span(span) is not None:
                    raise _SplitSpan
            record(w, posts, span)
            totals["seen"] += w.seen
            totals["kept"] += w.kept
            if got == "error":
                error_class, permanent = error_class_of(int(stats.get("status") or 0))
                w.fail(error_class, permanent=permanent)
            elif got == "skip_cap":
                totals["skip_capped"] += 1
                w.truncated()  # a server cap on a span too short to split (D645, D492)
            else:
                w.close(got)
        return got

    def record(w: Any, posts: list[dict[str, Any]], span: Span) -> None:
        """Observe ``posts`` into window ``w``; a post dated before the window start (the
        look-back) and unchanged writes no row (D551)."""
        start, end = span.start.isoformat(), span.end.isoformat()
        before = None if span.before is None else span.before.isoformat()
        totals["fetched"] += len(posts)
        for p in posts:
            post_id = str(p.get("_id") or "")
            if not post_id:
                continue
            # NO karma gate (AF is curated; completeness is the goal). The only client-side
            # filters are the date-floor backstop (a post dated before floor_from is never
            # registered, whatever the server returned) and the span cuts.
            pd = _posted_date(p)
            if pd is not None and pd < floor_from:
                continue
            if pd is not None and ((span.cut_hi and pd >= end) or (span.cut_lo and pd < start)):
                totals["outside_span"] += 1
                if before is not None and pd > before:
                    totals["before_ignored"] += 1  # the server dropped `before`
                continue
            w.seen += 1
            if c.prior(CHANNEL, post_id) is None and post_id not in observed:
                w.kept += 1
            observed.add(post_id)
            title = str(p.get("title") or "").strip()
            row = c.observe(
                CHANNEL, post_id, p,
                keys=fm_keys(chan.channel_group, CHANNEL, post_id),
                canonical_url=f"{base_url}/posts/{post_id}",
                metadata={"title": title or None, "authors": _post_authors(p) or None,
                          "published_at": _posted_at(p), "doc_type": "forum_post"},
                query=QUERY_KEY,  # matched_by (D228): the one sweep query, no label
                provider_record=record_payload(p),  # D522
                skip_unchanged=pd is not None and pd < start,
            )
            if row is None:
                continue  # re-seen in the look-back, unchanged: no row, no in-band offer
            body = p.get("htmlBody")
            c.add_inband(c.source_id(CHANNEL, post_id), CONTENT_ROLE,
                         raw=body.encode("utf-8") if isinstance(body, str) and body else None,
                         media_type=BODY_MEDIA_TYPE)

    exhausted = "error"  # stays so if a run-level halt ends a window before its pull returns
    with c.halts():
        worst = "complete"
        for hs, he in c.holes(CHANNEL, QUERY_KEY, floor_from):
            totals["holes"] += 1
            for s, e in date_windows(hs.date(), he.date(), span_days):
                got = ask_span(hole_span(s, e))
                if got != "complete":
                    worst = got
                    break  # the rest of this hole stays a hole for the next run
        mark = c.watermark(CHANNEL, QUERY_KEY)
        if mark is None or mark.date().isoformat() < today:
            eff_from = ask_from = floor_from
            if mark is not None:
                eff_from = max(floor_from, _as_query_date(
                    mark.date().isoformat(), source=f"coverage watermark ({CHANNEL}/{QUERY_KEY})"))
                # Promotion lag (D551): re-ask `lag` days below the watermark.
                ask_from = max(floor_from, (mark.date() - dt.timedelta(days=lag)).isoformat())
            got = ask_span(tail_span(dt.date.fromisoformat(eff_from), dt.date.fromisoformat(today),
                                     dt.date.fromisoformat(ask_from)), counted=False)
            worst = worst if got == "complete" else got
        exhausted = worst

    c.counts.update({"posts_fetched": totals["fetched"], "posts_seen": totals["seen"],
                     "posts_new": totals["kept"], "holes": totals["holes"],
                     "posts_outside_span": totals["outside_span"],
                     "lookback_capped": totals["lookback_capped"], "spans": totals["spans"],
                     "spans_split": totals["spans_split"], "skip_capped": totals["skip_capped"],
                     "before_ignored": totals["before_ignored"]})
    c.counts.update({f"queries_{exhausted}": 1, "query_limit": limit, "lookback_days": lag,
                     "subwindow_days": span_days})
    return c.finish()
