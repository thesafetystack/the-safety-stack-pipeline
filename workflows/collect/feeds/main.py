"""``collect.feeds`` — every RSS/Atom feed configured as a ``kind: feed`` channel (round 4, track L).

**Why.** Newsletters and blogs (Zvi's Substack, Import AI, Paradigm 3's "Humans on AI") publish a
feed, and a Substack or WordPress feed carries each post's full HTML (``content:encoded``). A feed
is therefore both the listing and, for most items, the content: the body is minted **in-band** at
collect, as ``collect.lw_graphql`` mints a LessWrong ``htmlBody``, and ``acquire`` converts it with
no fetch (``link-crawling.md`` §7 phase 2). Adding a feed is one ``configs/channels.yaml`` entry::

    - id: <name>                 # one channel per feed: the channel is the provenance key
      kind: feed
      location: "<feed URL>"
      license_class: public_web
      channel_group: url
      politeness: { rate_rps: 0.5 }
      include_re: "<regex over the item link>"   # optional
      self_published_org: <org>                   # optional, an org of configs/registry.yaml

plus ``<name>: [post]`` (bodies in the feed) or ``[page]`` (listing only) under a plan's
``acquire.wants``, and the channel in a triage source rule if its posts are to be fetched by source.

**Rows.** One ``sources`` row per feed item on ``sources.feeds`` under the feed's channel:
``external_id`` = the item's ``guid`` (Atom ``id``) as the feed spells it, else its link (the
feed's own identity for the item, so a URL-rule change re-mints nothing); ``canonical_url`` = the
link's fetch form (:func:`stack.urls.fetch_url`); the one key ``url`` (:func:`stack.urls.url_key`
through :func:`stack.scholarly_records.keys_for_source`); ``metadata`` title, ``authors``
(``dc:creator``, RSS ``author``, Atom ``author/name``; several are kept in order),
``published_at`` (``pubDate``, Atom ``published`` else ``updated``), ``abstract`` = the item's
``description``/``summary`` as plain text (HTML stripped, at least :data:`MIN_ABSTRACT_CHARS`),
``doc_type = lab_blog`` (enums.yaml: "Lab or org blog / web article"; the vocabulary has no
newsletter type). The row's ``provider_record`` in-band entry (D522) = the item record without its
body (the ``post`` entry) and with the body's sha256.

**In-band body.** When the item carries ``content:encoded`` or an Atom ``content`` whose text
(tags stripped) is at least :data:`MIN_BODY_CHARS` long, the HTML is minted as role ``post``, raw
``text/html``, through :meth:`stack.collect.base.Collector.add_inband` (counted ``bodies_inband``);
otherwise the item is listing-only (``listing_only``) and ``acquire`` fetches the page when triage
says fetch. An item ``description`` is never taken as the body (it is a teaser on every feed
probed).

**Idempotent.** ``record_digest`` covers the item record and its body's sha256, and every item is
observed with ``skip_unchanged``: a re-run over an unchanged feed writes no ``sources`` row
(``sources_unchanged``); an edited body writes a new row and replaces the ``post`` entry.

**Coverage.** One ``coverage`` row per feed and run, ``query`` = ``{"feed": <location>, "since":
<since or null>}``, window ``[now, now]`` (a feed is a snapshot of its newest items, as
``collect.blogs``' surfaces): ``ok`` when read, ``failed_*`` when the feed could not be fetched
(``dead`` 404/410, ``blocked`` 401/403, ``rate_limit`` 429, ``http_5xx``, ``network``) or was not
a feed (``parse_error``: a 200 whose root element is not ``rss``/``feed``/``RDF``). A feed is not
re-read within ``relist_after_h`` of an ``ok`` read (D226, :meth:`Collector.due`), so a second pass
makes no request.

Options (plan stage line ``options:``, D72):
  * ``since``          — ``YYYY-MM-DD``: items published before it are not observed
    (``before_since``); an undated item is observed (``undated``)
  * ``only``           — channel ids (list, or comma-separated): the feeds read (default: every
    ``kind: feed`` channel, in ``channels.yaml`` order)
  * ``relist_after_h`` — passes (D226): default 12

Transport (D26): the harness transport at :data:`FEED_RPS` (the feed channels'
``politeness.rate_rps``), one attempt, 30 s timeout, a feed reader's headers (:data:`FEED_HEADERS`).

The item parser (:func:`parse_items`) lives here, not in ``stack.collect.listing``
(``parse_feed`` there returns ``(link, title, date)`` only); ``collect.blogs``' feed surfaces could
share it once it moves there. Imports only ``stack.*`` and the stdlib.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import html
import json
import re
import time
import xml.etree.ElementTree as ET
from typing import Any

from stack.collect.base import (
    RELIST_OPTIONS,
    CollectError,
    Collector,
    Pacing,
    Transport,
)
from stack.scholarly_records import keys_for_source
from stack.stage_runner import StageContext, StageResult
from stack.urls import fetch_url

#: The ``options`` keys this stage's code reads; any other is refused (D434)
OPTIONS: frozenset[str] = frozenset({"only", "since"}) | RELIST_OPTIONS

KIND = "feed"
DOC_TYPE = "lab_blog"
CONTENT_ROLE = "post"
BODY_MEDIA_TYPE = "text/html"
#: A body shorter than this (text with tags stripped) is a teaser, not the post: listing-only.
MIN_BODY_CHARS = 500
#: A description shorter than this (plain text) is not kept as the abstract.
MIN_ABSTRACT_CHARS = 30
FEED_RPS = 0.5
FEED_TIMEOUT_S = 30.0
#: A feed reader's headers, not ``stack.collect.listing.BROWSER_HEADERS``: jack-clark.net's nginx
#: answers 403 to that spoofed Chrome user agent and 200 to this one (live smoke 2026-10-12).
FEED_HEADERS = {"User-Agent": "stack-collect/1.0 (+research; feed reader)",
                "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8"}
PACING = Pacing(rps=FEED_RPS, retry_delays_s=(), timeout_s=FEED_TIMEOUT_S, headers=FEED_HEADERS)

_T: Transport | None = None


def _sleep(seconds: float) -> None:
    """Politeness delay (module-level so offline tests monkeypatch it)."""
    time.sleep(seconds)


# ─────────────────────────────────────────────────────────────────────────────
# Parsing
# ─────────────────────────────────────────────────────────────────────────────
_FEED_ROOTS = ("rss", "feed", "RDF")
_ATOM = "http://www.w3.org/2005/Atom"
_DC = "http://purl.org/dc/elements/1.1/"
_CONTENT = "http://purl.org/rss/1.0/modules/content/"
#: Namespaces whose item elements are read by name: RSS 2.0 (none), Atom, RSS 1.0.
_CORE_NS = ("", _ATOM, "http://purl.org/rss/1.0/")
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_RFC822_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_RFC822 = re.compile(r"(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?")


class NotAFeed(ValueError):
    """A body that is not XML, or XML whose root is not an RSS/Atom/RDF feed."""


class FeedItem:
    """One feed item as the feed gives it. (A plain class: a stage module loaded by path
    was not registered in ``sys.modules``, which ``@dataclass`` needs; the stage loader registers it
    since D435, ``tools/probe_collect.py`` and ``tools/probe_filters.py`` still do not.)"""

    FIELDS = ("link", "guid", "title", "published", "authors", "description")

    def __init__(self, link: str = "", guid: str = "", title: str = "", published: str | None = None,
                 authors: list[str] | None = None, description: str = "", body: str = "") -> None:
        self.link = link
        self.guid = guid
        self.title = title
        self.published = published          # ISO-8601 (UTC) when the feed dates the item
        self.authors = list(authors or [])
        self.description = description      # raw (may hold HTML)
        self.body = body                    # content:encoded / Atom content (HTML)

    def record(self) -> dict[str, Any]:
        """The item record (``record_digest`` input and ``provider_record``): every field but
        the body, plus the body's sha256 (so an edited body is a changed record)."""
        rec: dict[str, Any] = {k: getattr(self, k) for k in self.FIELDS}
        rec["body_sha256"] = hashlib.sha256(self.body.encode("utf-8")).hexdigest() if self.body else None
        return rec


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def plain_text(fragment: str) -> str:
    """HTML (or plain) text → whitespace-collapsed plain text, entities unescaped."""
    return _WS.sub(" ", html.unescape(_TAG.sub(" ", fragment or ""))).strip()


def feed_datetime(raw: str) -> str | None:
    """An RSS ``pubDate`` (RFC 822) or Atom ISO-8601 timestamp → ISO-8601 UTC, else ``None``.
    An RFC 822 offset other than ``GMT``/``UT``/``Z``/``+0000`` is applied when numeric."""
    text = (raw or "").strip()
    if not text:
        return None
    try:
        d = dt.datetime.fromisoformat(text)
        return (d if d.tzinfo else d.replace(tzinfo=dt.UTC)).astimezone(dt.UTC).isoformat()
    except ValueError:
        pass
    m = _RFC822.search(text)
    if not m or m.group(2).lower() not in _RFC822_MONTHS:
        return None
    try:
        d = dt.datetime(int(m.group(3)), _RFC822_MONTHS[m.group(2).lower()], int(m.group(1)),
                        int(m.group(4) or 0), int(m.group(5) or 0), int(m.group(6) or 0), tzinfo=dt.UTC)
    except ValueError:
        return None
    off = re.search(r"([+-])(\d{2}):?(\d{2})\s*$", text)
    if off:
        sign = 1 if off.group(1) == "+" else -1
        d -= sign * dt.timedelta(hours=int(off.group(2)), minutes=int(off.group(3)))
    return d.isoformat()


def _content_text(el: ET.Element) -> str:
    """An Atom ``content``/``summary``: its text, or its children serialised (``type="xhtml"``)."""
    if len(el):
        return "".join(ET.tostring(c, encoding="unicode") for c in el)
    return el.text or ""


def parse_items(text: str) -> list[FeedItem]:
    """An RSS 2.0, RSS 1.0 (RDF) or Atom feed → its items in feed order. Raises :class:`NotAFeed`
    when ``text`` is not XML or its root is not a feed (a 200 interstitial page is not an empty
    feed; ``stack.collect.listing._require_root``'s reasoning)."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise NotAFeed(f"not XML: {exc}") from None
    if _local(root.tag) not in _FEED_ROOTS:
        raise NotAFeed(f"root element <{_local(root.tag)}> is not one of {_FEED_ROOTS}")
    items: list[FeedItem] = []
    for el in root.iter():
        if _local(el.tag) not in ("item", "entry"):
            continue
        it = FeedItem()
        updated = None
        for sub in el:
            name = _local(sub.tag)
            ns = sub.tag[1:].split("}", 1)[0] if sub.tag.startswith("{") else ""
            value = (sub.text or "").strip()
            if ns == _DC:
                if name == "creator" and value:
                    it.authors.append(value)
                elif name == "date" and value:
                    it.published = it.published or feed_datetime(value)
                continue
            if ns == _CONTENT:
                if name == "encoded" and value and not it.body:
                    it.body = value
                continue
            if ns not in _CORE_NS:
                continue  # media:, itunes:, wfw:, slash:, … (media:content is not the body)
            if name == "link":
                href = (sub.get("href") or value).strip()
                if href and sub.get("rel", "alternate") == "alternate" and not it.link:
                    it.link = href
            elif name in ("guid", "id") and value:
                it.guid = value
            elif name == "title":
                it.title = plain_text(_content_text(sub))
            elif name in ("pubDate", "published") and value:
                it.published = it.published or feed_datetime(value)
            elif name == "updated" and value:
                updated = feed_datetime(value)
            elif name == "author":
                person = next((c.text for c in sub if _local(c.tag) == "name" and c.text), None)
                if person or value:
                    it.authors.append((person or value).strip())
            elif name in ("description", "summary"):
                it.description = _content_text(sub).strip()
            elif name == "content" and ns == _ATOM and not it.body:
                it.body = _content_text(sub).strip()
        it.published = it.published or updated
        if not it.link and it.guid.startswith(("http://", "https://")):
            it.link = it.guid  # RSS guid isPermaLink without a <link>
        if it.link:
            items.append(it)
    return items


def body_of(item: FeedItem) -> str | None:
    """The item's in-band body (HTML), or ``None`` when it has none worth minting
    (:data:`MIN_BODY_CHARS`)."""
    return item.body if item.body and len(plain_text(item.body)) >= MIN_BODY_CHARS else None


def abstract_of(item: FeedItem) -> str | None:
    text = plain_text(item.description)
    return text if len(text) >= MIN_ABSTRACT_CHARS else None


def error_class_of(status: int) -> tuple[str, bool]:
    """A failed feed read's HTTP status → ``(error_class, permanent)``; ``0`` = transport failure."""
    if status == 429:
        return "rate_limit", False
    if 500 <= status < 600:
        return "http_5xx", False
    if status in (401, 403):
        return "blocked", True
    if status in (404, 410):
        return "dead", True
    return "network", False


# ─────────────────────────────────────────────────────────────────────────────
# The stage
# ─────────────────────────────────────────────────────────────────────────────
def _id_list(value: Any) -> set[str]:
    items = value.split(",") if isinstance(value, str) else (value or [])
    return {str(v).strip() for v in items if str(v).strip()}


def _since(value: Any) -> str | None:
    if value in (None, ""):
        return None
    s = str(value).strip()
    try:
        return dt.date.fromisoformat(s).isoformat()
    except ValueError:
        raise CollectError(f"collect.feeds: option since={value!r} is not YYYY-MM-DD") from None


def feed_channels(c: Collector, only: set[str]) -> list[Any]:
    """The ``kind: feed`` channels to read, in ``channels.yaml`` order; a channel listed in
    ``only`` that is not a feed channel is refused, as is a feed without a ``location``, outside
    the ``url`` group or with an ``include_re`` that does not compile."""
    from stack.config import load

    chans = [ch for ch in load().channels.channels if ch.kind == KIND]
    unknown = sorted(only - {ch.id for ch in chans})
    if unknown:
        raise CollectError(f"collect.feeds: option only names non-feed channels {unknown}")
    out = []
    for ch in chans:
        if only and ch.id not in only:
            continue
        if not ch.location:
            raise CollectError(f"feed channel {ch.id!r} has no location (the feed URL)")
        if ch.channel_group != "url":
            raise CollectError(f"feed channel {ch.id!r}: channel_group {ch.channel_group!r}, feeds are `url`")
        if ch.include_re:
            try:
                re.compile(ch.include_re)
            except re.error as exc:
                raise CollectError(f"feed channel {ch.id!r}: include_re does not compile: {exc}") from None
        out.append(ch)
    return out


def feed_query(ch: Any, since: str | None) -> dict[str, Any]:
    """A feed's coverage query (its window key; also the rows' ``matched_by`` query, D228)."""
    return {"feed": ch.location, "since": since}


def observe_item(ctx: StageContext, c: Collector, ch: Any, item: FeedItem,
                 tally: dict[str, int], since: str | None = None) -> bool:
    """Observe one item (module docstring, "Rows"); returns whether its source is new."""
    ext = item.guid or item.link
    url = fetch_url(item.link) or item.link
    new = c.prior(ch.id, ext) is None
    record = item.record()
    row = c.observe(
        ch.id, ext, record,
        keys=[(k["kind"], k["value"]) for k in keys_for_source("url", ch.id, ext, url)],
        canonical_url=url,
        metadata={"title": item.title or None, "authors": item.authors or None,
                  "published_at": item.published, "abstract": abstract_of(item),
                  "doc_type": DOC_TYPE},
        provider_record=record,  # D522
        skip_unchanged=True,
        query=feed_query(ch, since), query_id=f"feed:{ch.id}", label=ch.location)  # D228
    if row is None:
        return False  # unchanged since the last observation: no row, no in-band offer
    body = body_of(item)
    if body is None:
        tally["listing_only"] += 1
    else:
        tally["bodies_inband"] += 1
        c.add_inband(row["source_id"], CONTENT_ROLE, raw=body.encode("utf-8"), media_type=BODY_MEDIA_TYPE)
    return new


def read_feed(ctx: StageContext, c: Collector, ch: Any, since: str | None,
              tally: dict[str, int]) -> None:
    """One feed: its coverage window, the request, the items."""
    assert _T is not None
    include = re.compile(ch.include_re) if ch.include_re else None
    with c.window(ch.id, feed_query(ch, since), c.now, c.now) as w:
        resp = _T.get(ch.location)
        status = resp.status or 0
        if status != 200 or not resp.body:
            error_class, permanent = error_class_of(status)
            w.fail(error_class, permanent=permanent)
            tally["feeds_failed"] += 1
            return
        try:
            items = parse_items(resp.body.decode("utf-8", errors="replace"))
        except NotAFeed:
            w.fail("parse_error", permanent=True)
            tally["feeds_failed"] += 1
            return
        tally["items_listed"] += len(items)
        seen_ext: set[str] = set()
        for item in items:
            if include is not None and not include.search(item.link):
                tally["items_excluded"] += 1
                continue
            if since is not None and item.published is not None and item.published[:10] < since:
                tally["before_since"] += 1
                continue
            ext = item.guid or item.link
            if ext in seen_ext:
                tally["items_repeat"] += 1
                continue  # one row per source per run
            seen_ext.add(ext)
            tally["undated"] += item.published is None
            w.seen += 1
            if observe_item(ctx, c, ch, item, tally, since):
                w.kept += 1
        w.complete()
        tally["feeds_ok"] += 1


def run(ctx: StageContext) -> StageResult:
    global _T
    c = Collector(ctx)
    since = _since(c.option("since", None))
    chans = feed_channels(c, _id_list(c.option("only", None)))
    relist = c.relist_after()
    tally = {"feeds_ok": 0, "feeds_failed": 0, "items_listed": 0, "items_excluded": 0,
             "before_since": 0, "items_repeat": 0, "undated": 0, "bodies_inband": 0,
             "listing_only": 0}
    _T = c.transport(PACING, sleep=lambda s: _sleep(s))
    try:
        with c.halts():
            for i, ch in enumerate(chans):
                c.progress(done=i, total=len(chans))
                if not c.due(ch.id, feed_query(ch, since), relist_after=relist):
                    continue  # read `ok` within relist_after_h (D226)
                read_feed(ctx, c, ch, since, tally)
            c.progress(done=len(chans), total=len(chans))
    finally:
        _T = None
    c.counts.update(tally, feeds=len(chans))
    return c.finish()
