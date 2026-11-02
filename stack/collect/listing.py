"""Listing discovery for collect workflows — the shared scan engine (10-design §5.1).

ONE implementation of "read a configured listing surface, parse it in code, and hand back typed
candidates". Two workflows use it: ``collect.cards`` (per-lab model-card / policy watchers) and
``collect.blogs`` (lab blog / research-site scrapes). They differ only in their spec tables and
in how they mint identities — the sitemap/HTML-index/GitHub crawling, the include/exclude regex
filtering, the slug rules, and the honest per-surface ``exhausted`` accounting are identical, and
a second copy of them would drift.

Everything here is LISTING-only. No item content is ever fetched: that is the fetch phase's job
(10-design §5.1), and the phase invariant governs network calls, not persistence.

The slug/id helpers are BYTE-FOR-BYTE ports of v1 ``doc_watch.py`` — they ARE the dedup keys, so
any drift re-mints every previously seen doc. They are golden-tested in
``tests/test_collect_cards.py`` against v1's ``test_watch_docs_ids.py`` fixtures.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Protocol

from stack.http import DEFAULT_MAX_BYTES, decode_content
from stack.lookup.card_dates import evidence, listing_dates, resolve

# Cloudflare / bot-gated listings (x.ai, anthropic.com) serve non-browser clients a block page;
# the browser UA is v1's stages/lib/corpus.DEFAULT_HEADERS (ported verbatim).
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
GITHUB_API = "https://api.github.com"
GITHUB_HEADERS = {"Accept": "application/vnd.github+json"}


# ─────────────────────────────────────────────────────────────────────────────
# Ported id/slug helpers (BYTE-FOR-BYTE from v1 doc_watch.py — these ARE the dedup
# keys; any drift re-mints every previously seen doc. Golden-tested in
# tests/test_collect_cards.py against v1's test_watch_docs_ids.py fixtures.)
# ─────────────────────────────────────────────────────────────────────────────
GENERIC_TEXT_RE = re.compile(
    r"(?i)^\s*(view|read|see|download|open|get|the)?\s*"
    r"(the\s+)?(model\s+card|system\s+card|pdf|report|paper|more|here|link)?\s*$"
)
_NOISE_TAIL_TOKENS = ("card", "system", "model")
_DATE_PREFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-(.+)$")
_HEX_SLUG_RE = re.compile(r"^[0-9a-f]{16,}$")
# Trailing CDN cache-buster (media.x.ai/…/card-4p6-4cd2dc57.pdf). 8 hex is the usual fingerprint;
# stripping it lets a re-upload keep the same identity when the rest of the slug is readable.
_CDN_HASH_TAIL_RE = re.compile(r"-([0-9a-f]{8,})$")
_WORD_RE = re.compile(r"[a-z]{3,}")


def _strip_cdn_hash(slug: str) -> str:
    """Drop a trailing ``-<8+ hex>`` fingerprint. No match → the slug unchanged."""
    return _CDN_HASH_TAIL_RE.sub("", slug)


def _is_opaque_slug(slug: str) -> bool:
    """True when the URL slug cannot be an identity: all-hex, or (after stripping a CDN
    hash tail) no real word remains. ``card``/``system``/``model`` are noise, so
    ``card-4p6-4cd2dc57`` is opaque and must take its name from the listing anchor."""
    if not slug or _HEX_SLUG_RE.match(slug):
        return True
    head = _strip_cdn_hash(slug)
    words = [w for w in _WORD_RE.findall(head) if w not in _NOISE_TAIL_TOKENS]
    return not words


def slugify(s: str) -> str:
    """v1 corpus.slugify with the doc-slug ``fallback=""`` contract (empty stays empty — an
    ``org``-fallback would make ``systemcard:org`` nonsense)."""
    out = re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")
    return out


def slug_from_url(url: str) -> tuple[str, str]:
    """``(slug, date_hint)`` from a URL's last path segment. Strips file extensions and
    ``data.x.ai``-style ``YYYY-MM-DD-`` filename prefixes (which become the date_hint)."""
    seg = urllib.parse.urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
    seg = re.sub(r"\.(pdf|md|html?)$", "", seg, flags=re.IGNORECASE)
    slug = slugify(seg)
    m = _DATE_PREFIX_RE.match(slug)
    if m:
        return m.group(2), m.group(1)
    return slug, ""


def slug_from_repo_path(path: str) -> str:
    """``models/llama3_2/MODEL_CARD_VISION.md`` -> ``llama-3-2-vision``. ``MODEL_CARD*`` files are
    named by their directory path (last two components, dropping a leading ``models``); a
    letter->digit boundary gets a dash to match catalog id style."""
    p = Path(path)
    stem = p.stem
    if stem.upper().startswith("MODEL_CARD"):
        variant = stem[len("MODEL_CARD"):].strip("_").lower()
        dirs = [d for d in p.parent.parts if d.lower() != "models"][-2:]
        base = "-".join(dirs) or stem
        slug = f"{base}-{variant}" if variant else base
    else:
        slug = stem
    slug = slugify(slug)
    return re.sub(r"(?<=[a-z])(?=\d)", "-", slug)


def make_doc_id(kind: str, org_slug: str, slug: str) -> str:
    """``{systemcard|policy}:{org_slug}-{slug}``, dropping duplicated org tokens and trailing
    system/model/card noise. Never contains ``--``."""
    toks = [t for t in slug.split("-") if t]
    org_toks = org_slug.split("-")
    while toks and toks[0] in org_toks:
        toks.pop(0)
    while toks and toks[-1] in _NOISE_TAIL_TOKENS:
        toks.pop()
    prefix = "systemcard" if kind == "systemcard" else "policy"
    return f"{prefix}:{org_slug}-{'-'.join(toks) or slug}"


def external_id_of(doc_id: str) -> str:
    """The v3 ``external_id`` for a v1 doc id: the ``<org>-<slug>`` body (prefix stripped). Keeps
    ``collect.cards`` and ``collect.v1_import`` minting the IDENTICAL ``(channel, external_id)`` for
    the same doc — v1_import's ``parse_stem('systemcard:<org>-<slug>')`` returns ``<org>-<slug>``."""
    return doc_id.split(":", 1)[1]


# ─────────────────────────────────────────────────────────────────────────────
# Watch-source spec (the v1 doc_watch.yaml DATA, ported into code — see module docstring for
# WHY it cannot live in channels.yaml). LISTING-only fields; the v1 `fetch`/`render` knobs are
# dropped (item content + JS rendering are fetch-phase concerns, 10-design §5.1).
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Surface:
    key: str
    method: str                                # html-index | sitemap | feed | github-tree |
    #                                            github-org | pinned-pdf
    doc_type: str = "system_card"              # default doc genre; policy_re match -> framework_policy
    kind: str = "systemcard"                   # v1 id-prefix kind: systemcard | policy
    urls: list[str] = field(default_factory=list)
    include_re: str | None = None
    exclude_re: str | None = None
    text_re: str | None = None
    exclude_text_re: str | None = None
    text_slug_re: str | None = None
    text_slug_template: str = ""
    policy_re: str | None = None
    title_prefix: str = ""
    github: list[dict[str, str]] = field(default_factory=list)
    github_org: str = ""
    path_re: str | None = None
    max_repos: int = 25
    pinned_id: str = ""
    # Multi-item pinned-pdf archive: [{url, pinned_id, title?, date?}]. When non-empty,
    # _scan_pinned emits one candidate per entry; urls/pinned_id stay the single-item Meta path.
    pins: list[dict[str, str]] = field(default_factory=list)


@dataclass
class ChannelWatch:
    """One channel's listing surfaces plus the org slug its identities are rooted in."""

    org_slug: str
    surfaces: list[Surface]


# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Candidate:
    url: str                 # the item's canonical locator (fetch will acquire from here)
    slug: str
    text: str = ""           # listing anchor text (title hint)
    date_hint: str = ""      # YYYY-MM-DD parsed from the listing, or ""
    #: the title block of a multi-block anchor (:func:`title_from_blocks`), "" for a single-block one; ``text`` keeps
    #: the blocks run together, as the identity rules have always read it
    title_text: str = ""
    date_evidence: list[dict[str, Any]] = field(default_factory=list)
    modified_hint: str = ""


def url_passes(spec: Surface, raw_url: str, text: str) -> bool:
    if spec.include_re and not re.search(spec.include_re, raw_url):
        return False
    if spec.exclude_re and re.search(spec.exclude_re, raw_url):
        return False
    if spec.text_re and not re.search(spec.text_re, text or ""):
        return False
    return not (spec.exclude_text_re and re.search(spec.exclude_text_re, text or ""))


def candidate_of(spec: Surface, raw_url: str, text: str = "", lastmod: str = "",
                 blocks: list[str] | None = None) -> Candidate | None:
    """Filter + slug a raw listing link into a Candidate (None = filtered out). Ports v1
    ``candidate_of`` — the text_slug_re / opaque-CDN-slug identity rules included.

    Opaque CDN slugs (pure hex, or a short code plus a hash tail like ``4p5-5184fdf9``)
    take their identity from non-generic listing-anchor text. Readable slugs keep the
    URL form, with only a trailing hash fingerprint stripped so a re-upload does not remint.
    ``blocks`` (an HTML index anchor's text blocks, :func:`parse_anchors`) set only ``title_text``.
    """
    if not url_passes(spec, raw_url, text):
        return None
    text = re.sub(r"\s+", " ", text or "").strip()
    slug, date_hint = slug_from_url(raw_url)
    if spec.text_slug_re:
        m = re.search(spec.text_slug_re, text)
        if not m:
            return None  # slug-from-text sources: the anchor text IS the identity
        slug = spec.text_slug_template.format(*m.groups())
    elif _is_opaque_slug(slug):
        if text and not GENERIC_TEXT_RE.match(text):
            slug = slugify(text)
        else:
            return None  # opaque CDN hash with no usable link text
    else:
        slug = _strip_cdn_hash(slug) or slug
    if not slug:
        return None
    hints = [e] if date_hint and (e := evidence(date_hint, 'url_filename')) else []
    if lastmod and (e := evidence(lastmod, 'sitemap.lastmod', 'modification')):
        hints.append(e)
    return Candidate(url=raw_url, slug=slug, text=text, date_hint=resolve(hints)['pub_date'] or '',
                     date_evidence=hints, modified_hint=lastmod, title_text=title_from_blocks(blocks or []))


# ─────────────────────────────────────────────────────────────────────────────
# HTTP fetch layer (stdlib only; a replay seam for offline tests)
# ─────────────────────────────────────────────────────────────────────────────
@dataclass

class FetchResult:
    status: int                 # HTTP status; 0 = transport error
    text: str | None = None
    rate_limited: bool = False  # GitHub 403 API-rate-limit


def _url_with_params(url: str, params: dict[str, Any] | None) -> str:
    if not params:
        return url
    qs = urllib.parse.urlencode(sorted((k, str(v)) for k, v in params.items()))
    sep = "&" if urllib.parse.urlsplit(url).query else "?"
    return f"{url}{sep}{qs}"


class HttpFetcher:
    """Live listing reads via stdlib ``urllib`` with the browser UA. GETs ONLY the configured
    listing/seed surfaces (10-design §5.1) — item content is never fetched here."""

    def get(
        self, url: str, *, headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> FetchResult:
        full = _url_with_params(url, params)
        req = urllib.request.Request(full, headers={**BROWSER_HEADERS, **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw, _ = decode_content(resp.read(), resp.headers, DEFAULT_MAX_BYTES)  # D588
                return FetchResult(status=resp.status, text=raw.decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                raw, _ = decode_content(exc.read(), exc.headers or {}, DEFAULT_MAX_BYTES)
                body = raw.decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001, S110 — the status is the answer; the body is a bonus
                pass
            rl = exc.code == 403 and ("rate limit" in body.lower() or "api rate limit" in body.lower())
            return FetchResult(status=exc.code, text=body or None, rate_limited=rl)
        except (urllib.error.URLError, OSError, ValueError):
            return FetchResult(status=0, text=None)


class ReplayFetcher:
    """Offline test seam: serves canned responses from ``<dir>/responses.json`` — a mapping of the
    full (params-folded) URL to ``{"status": int, "text": str, "rate_limited": bool}``. A miss
    raises (fixtures must be exhaustive) so a test-authoring gap is loud, never a silent network
    read."""

    def __init__(self, path: Path) -> None:
        self._responses: dict[str, dict[str, Any]] = json.loads(
            (path / "responses.json").read_text(encoding="utf-8")
        )

    def get(
        self, url: str, *, headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> FetchResult:
        full = _url_with_params(url, params)
        if full not in self._responses:
            raise LookupError(f"ReplayFetcher: no canned response for {full!r}")
        r = self._responses[full]
        return FetchResult(
            status=int(r.get("status", 200)),
            text=r.get("text"),
            rate_limited=bool(r.get("rate_limited", False)),
        )


def resolve_fetcher(fixtures_env: str) -> HttpFetcher | ReplayFetcher:
    """The live fetcher, or a :class:`ReplayFetcher` when ``$<fixtures_env>`` names a fixture dir.

    The env var name is a parameter because each workflow owns its own (``STACK_CARDS_FIXTURES``,
    ``STACK_BLOGS_FIXTURES``): one shared var would make a cards fixture dir silently serve blog
    scans too.
    """
    fixtures = os.environ.get(fixtures_env)
    if fixtures:
        return ReplayFetcher(Path(fixtures))
    return HttpFetcher()


# ─────────────────────────────────────────────────────────────────────────────
# Listing parsers (stdlib)
# ─────────────────────────────────────────────────────────────────────────────
#: Tags that end one text block of an anchor and start the next (:func:`parse_anchors`); inline tags (``span``,
#: ``em``, ``strong``, ...) do not split a block.
BLOCK_TAGS = frozenset({"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "time", "section", "article", "header",
                        "footer", "br", "td", "th", "dt", "dd", "figcaption", "blockquote"})
#: A text block that is only a date (a listing's dateline inside the anchor): ISO, ``Oct 07, 2026``, ``7 October 2026``.
_DATE_BLOCK_RE = re.compile(r"(?i)^(?:\d{4}-\d{2}-\d{2}|[a-z]{3,9}\.?\s+\d{1,2},?\s+\d{4}|\d{1,2}\s+[a-z]{3,9}\.?,?\s+\d{4})$")


class _LinkParser(HTMLParser):
    """Collect ``(href, anchor_text, blocks)`` from an HTML index page: ``anchor_text`` is the anchor's text nodes run
    together (no separator, the form the identity rules read); ``blocks`` the same text split at :data:`BLOCK_TAGS`,
    each block's whitespace collapsed, empty blocks dropped."""

    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self.blocks: list[list[str]] = []
        self._href: str | None = None
        self._text: list[str] = []
        self._segs: list[list[str]] = [[]]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            if self._href is not None:
                self._flush()
            self._href = dict(attrs).get("href")
            self._text = []
            self._segs = [[]]
        elif tag in BLOCK_TAGS and self._href is not None:
            self._segs.append([])

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)
            self._segs[-1].append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href is not None:
            self._flush()
        elif tag in BLOCK_TAGS and self._href is not None:
            self._segs.append([])

    def _flush(self) -> None:
        if self._href:
            self.links.append((self._href, " ".join("".join(self._text).split())))
            self.blocks.append([b for b in (" ".join("".join(seg).split()) for seg in self._segs) if b])
        self._href = None
        self._text = []
        self._segs = [[]]


def parse_anchors(html: str, base_url: str) -> list[tuple[str, str, list[str]]]:
    """``(absolute url, anchor text, text blocks)`` of every anchor of an HTML page (:class:`_LinkParser`)."""
    parser = _LinkParser()
    parser.feed(html)
    parser.close()
    return [(urllib.parse.urljoin(base_url, href.strip()), text, blocks)
            for (href, text), blocks in zip(parser.links, parser.blocks, strict=True)]


def parse_links(html: str, base_url: str) -> list[tuple[str, str]]:
    return [(url, text) for url, text, _blocks in parse_anchors(html, base_url)]


def title_from_blocks(blocks: list[str]) -> str:
    """The title block of a multi-block anchor: the first block after any leading date blocks (a card listing's anchor
    holds a dateline, the title and a description, which the run-together anchor text glues into one string:
    ``Oct 07, 2026GPT-6 Sol and ...``). "" for an anchor of one block (its text is the title) or of dates only."""
    if len(blocks) < 2:
        return ""
    return next((b for b in blocks if not _DATE_BLOCK_RE.match(b)), "")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _require_root(text: str, allowed: tuple[str, ...], kind: str) -> ET.Element:
    """Parse ``text`` and assert its ROOT ELEMENT is one of ``allowed``, else raise ParseError.

    Well-formedness is not the same as being the right document, and the gap between them is a
    false completeness claim. A CDN interstitial or proxy block page served with **status 200** —
    ``<html><body>403 Forbidden</body></html>`` — is perfectly valid XML that simply contains no
    ``<url>`` and no ``<item>``. Without this check the scanners read that as "I fetched the
    listing and it was empty", set no error, and bank ``exhausted='complete'`` plus a full
    coverage window for a surface they never actually read (SHOULD-14). The next run then starts
    after that window. A genuine 403/500 is caught by the status check instead; this is
    specifically the 200-with-an-error-body case, which is the normal shape for the
    Cloudflare-fronted surfaces here (channels.yaml notes ai.meta.com's sitemap doing exactly
    this to non-browser clients).

    ``ET.ParseError`` is deliberately reused rather than a new exception type: ``_scan_sitemap``
    and ``_scan_feed`` already wrap every parse in ``except ET.ParseError`` and turn it into
    ``ok = False`` → an ``error`` receipt, which is precisely the outcome wanted here. Returning
    empty instead would be wrong — empty is indistinguishable from a valid-but-empty listing,
    which must keep reporting ``complete``.

    This is the same fix, and the same reasoning, as the root check in
    ``workflows/collect/arxiv_sweep/main.py::parse_entries``.
    """
    root = ET.fromstring(text)
    name = _local(root.tag)
    if name not in allowed:
        raise ET.ParseError(
            f"not a {kind}: root element is <{name}>, expected one of "
            f"{', '.join(f'<{a}>' for a in allowed)}"
        )
    return root


# Atom is <feed>, RSS 2.0 is <rss>, RSS 1.0 is <RDF> (still served by some research blogs, and
# `parse_feed`'s iter() walk reads its <item> elements correctly).
_FEED_ROOTS = ("feed", "rss", "RDF")
_SITEMAP_ROOTS = ("urlset", "sitemapindex")


def parse_sitemap(text: str) -> tuple[list[str], list[tuple[str, str]]]:
    """Return ``(child_sitemap_urls, [(loc, lastmod)])`` for a sitemap or sitemapindex XML.

    Raises ``ET.ParseError`` when the body is not XML, or is XML that is not a sitemap at all
    (see :func:`_require_root`) — every caller already treats that as an ``error`` surface.
    """
    root = _require_root(text, _SITEMAP_ROOTS, "sitemap")
    children: list[str] = []
    entries: list[tuple[str, str]] = []
    for el in root:
        name = _local(el.tag)
        loc = lastmod = ""
        for sub in el:
            if _local(sub.tag) == "loc" and sub.text:
                loc = sub.text.strip()
            elif _local(sub.tag) == "lastmod" and sub.text:
                lastmod = sub.text.strip()
        if not loc:
            continue
        if name == "sitemap":
            children.append(loc)
        elif name == "url":
            entries.append((loc, lastmod))
    return children, entries


_RSS_DATE_ISO = re.compile(r"^(\d{4}-\d{2}-\d{2})")
_RSS_DATE_RFC822 = re.compile(r"(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})")
_RFC822_MONTHS = {
    m: f"{i:02d}"
    for i, m in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1
    )
}


def _feed_date(raw: str) -> str:
    """A feed date (RSS ``pubDate`` RFC-822 or Atom ``published`` ISO-8601) → ``YYYY-MM-DD``, or
    ``""`` when unparseable. Feeds are the one listing surface that reliably carries real dates,
    which is why blog identities can be year-qualified without fetching any post."""
    text = (raw or "").strip()
    m = _RSS_DATE_ISO.match(text)
    if m:
        return m.group(1)
    m = _RSS_DATE_RFC822.search(text)
    if m:
        month = _RFC822_MONTHS.get(m.group(2).lower())
        if month:
            return f"{m.group(3)}-{month}-{int(m.group(1)):02d}"
    return ""


def parse_feed(text: str) -> list[tuple[str, str, str]]:
    """Parse an RSS 2.0 or Atom feed → ``[(link, title, date)]``.

    Both shapes in one reader because a site's feed format is not something the spec table should
    have to know: RSS puts the URL in ``<item><link>`` text, Atom in ``<entry><link href=…>``.

    Raises ``ET.ParseError`` when the body is not XML, or is XML that is not a feed at all (see
    :func:`_require_root`) — every caller already treats that as an ``error`` surface.
    """
    root = _require_root(text, _FEED_ROOTS, "feed")
    items: list[tuple[str, str, str]] = []
    for el in root.iter():
        name = _local(el.tag)
        if name not in ("item", "entry"):
            continue
        link = title = date = ""
        for sub in el:
            sub_name = _local(sub.tag)
            if sub_name == "link":
                link = (sub.get("href") or sub.text or "").strip()
            elif sub_name == "title" and sub.text:
                title = " ".join(sub.text.split())
            elif sub_name in ("pubDate", "published", "updated", "date") and sub.text:
                date = date or _feed_date(sub.text)
        if link:
            items.append((link, title, date))
    return items


# ─────────────────────────────────────────────────────────────────────────────
# Per-method discovery → list[Candidate] + a typed surface outcome
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class SurfaceScan:
    candidates: list[Candidate]
    exhausted: str                       # complete | truncated | error
    watermark: str | None = None         # github-org: newest created_at accounted for this run
    note: dict[str, Any] = field(default_factory=dict)


def _scan_html_index(spec: Surface, fetcher: HttpFetcher | ReplayFetcher) -> SurfaceScan:
    cands: list[Candidate] = []
    seen_raw: set[str] = set()
    ok = True
    for index_url in spec.urls:
        res = fetcher.get(index_url, headers=BROWSER_HEADERS)
        if res.status != 200 or not res.text:
            ok = False
            continue
        dates = listing_dates(res.text, index_url)
        for raw, text, blocks in parse_anchors(res.text, index_url):
            if raw in seen_raw:
                continue
            c = candidate_of(spec, raw, text, blocks=blocks)
            if c:
                c.date_evidence.extend({**e, 'url': index_url} for e in dates.get(raw, []))
                c.date_hint = resolve(c.date_evidence)['pub_date'] or ''
                seen_raw.add(raw)
                cands.append(c)
    return SurfaceScan(cands, "complete" if ok else "error")


def _scan_sitemap(spec: Surface, fetcher: HttpFetcher | ReplayFetcher) -> SurfaceScan:
    cands: list[Candidate] = []
    ok = True
    include = re.compile(spec.include_re) if spec.include_re else None
    for sm_url in spec.urls:
        res = fetcher.get(sm_url, headers=BROWSER_HEADERS)
        if res.status != 200 or not res.text:
            ok = False
            continue
        try:
            children, entries = parse_sitemap(res.text)
        except ET.ParseError:
            ok = False
            continue
        # One level of sitemap-index expansion (bounded to the configured surface).
        for child in children:
            cres = fetcher.get(child, headers=BROWSER_HEADERS)
            if cres.status != 200 or not cres.text:
                ok = False
                continue
            try:
                _, child_entries = parse_sitemap(cres.text)
            except ET.ParseError:
                ok = False
                continue
            entries.extend(child_entries)
        for loc, lastmod in entries:
            if include and not include.search(loc):
                continue
            c = candidate_of(spec, loc, "", lastmod)
            if c:
                cands.append(c)
    return SurfaceScan(cands, "complete" if ok else "error")


def _scan_feed(spec: Surface, fetcher: HttpFetcher | ReplayFetcher) -> SurfaceScan:
    """RSS/Atom feed surface. A feed is a complete listing of what it advertises — but it is
    usually the site's RECENT window, not its archive, so a feed-only site pairs this with an
    html-index surface rather than relying on it alone."""
    cands: list[Candidate] = []
    ok = True
    for feed_url in spec.urls:
        res = fetcher.get(feed_url, headers=BROWSER_HEADERS)
        if res.status != 200 or not res.text:
            ok = False
            continue
        try:
            items = parse_feed(res.text)
        except ET.ParseError:
            ok = False
            continue
        for link, title, date in items:
            c = candidate_of(spec, urllib.parse.urljoin(feed_url, link), title)
            if c and (e := evidence(date, 'feed_date')):
                c.date_hint = e['value'] if e['precision'] == 'day' else ''
                c.date_evidence.append(e)
            if c:
                cands.append(c)
    return SurfaceScan(cands, "complete" if ok else "error")


def _scan_github_tree(spec: Surface, fetcher: HttpFetcher | ReplayFetcher) -> SurfaceScan:
    cands: list[Candidate] = []
    exhausted = "complete"
    for repo_spec in spec.github:
        owner, repo = repo_spec["owner"], repo_spec["repo"]
        path_re = re.compile(repo_spec["path_re"])
        res = fetcher.get(
            f"{GITHUB_API}/repos/{owner}/{repo}/git/trees/HEAD",
            headers=GITHUB_HEADERS, params={"recursive": "1"},
        )
        if res.rate_limited:
            return SurfaceScan([], "error", note={"github_rate_limited": True})
        if res.status != 200 or not res.text:
            exhausted = "error"
            continue
        try:
            tree = json.loads(res.text)
        except json.JSONDecodeError:
            exhausted = "error"
            continue
        if tree.get("truncated"):
            exhausted = "truncated"  # GitHub capped the tree — coverage partial
        for t in tree.get("tree", []):
            if t.get("type") != "blob" or not path_re.search(t.get("path", "")):
                continue
            raw = f"https://raw.githubusercontent.com/{owner}/{repo}/HEAD/{t['path']}"
            if not url_passes(spec, raw, ""):
                continue
            cands.append(Candidate(url=raw, slug=slug_from_repo_path(t["path"])))
    return SurfaceScan(cands, exhausted)


def _scan_github_org(
    spec: Surface, fetcher: HttpFetcher | ReplayFetcher, *, prior_watermark: str | None, full: bool,
) -> SurfaceScan:
    res = fetcher.get(
        f"{GITHUB_API}/orgs/{spec.github_org}/repos",
        headers=GITHUB_HEADERS, params={"sort": "created", "per_page": "100"},
    )
    if res.rate_limited:
        return SurfaceScan([], "error", note={"github_rate_limited": True})
    if res.status != 200 or not res.text:
        return SurfaceScan([], "error")
    try:
        repos = json.loads(res.text)
    except json.JSONDecodeError:
        return SurfaceScan([], "error")
    if not isinstance(repos, list):
        return SurfaceScan([], "error")

    def created(r: dict[str, Any]) -> str:
        return str(r.get("created_at") or "")

    watermark = prior_watermark
    if watermark is None and not full:
        # First contact: bootstrap the watermark to newest instead of sweeping history — old repo
        # PDFs are tech reports the corpus already holds via arXiv (v1 rationale). --full opts in.
        newest = max((created(r) for r in repos), default="")
        return SurfaceScan(
            [], "complete", watermark=newest,
            note={"bootstrap": True, "repos_seen": len(repos)},
        )
    # D555: `full` replaces only the first-contact bootstrap (history swept from the oldest repo
    # on); an existing watermark is resumed with or without it.
    fresh = [r for r in repos if created(r) > (watermark or "")]
    # Scan OLDEST-first so a max_repos cap advances the watermark only over repos actually
    # scanned; newer capped-out repos stay above it for next run (v1's stranding fix). Such a
    # capped scan is a banked prefix (D555): `complete` up to its watermark, noted `repos_capped`
    # (the repos left), so the collector writes it `ok` and ends scope-incomplete.
    fresh.sort(key=created)
    capped = len(fresh) > spec.max_repos
    note: dict[str, Any] = {}
    if capped:
        note["repos_capped"] = len(fresh) - spec.max_repos
        fresh = fresh[: spec.max_repos]
    if fresh and watermark is None:
        note["oldest"] = created(fresh[0])  # D555: a first sweep's window starts at its oldest repo
    cands: list[Candidate] = []
    new_watermark = watermark or ""
    exhausted = "complete"
    for r in fresh:
        owner, repo = spec.github_org, str(r["name"])
        tres = fetcher.get(
            f"{GITHUB_API}/repos/{owner}/{repo}/git/trees/HEAD",
            headers=GITHUB_HEADERS, params={"recursive": "1"},
        )
        if tres.rate_limited:
            # Stop advancing the watermark past this repo; report unknown coverage.
            return SurfaceScan(cands, "error", watermark=new_watermark,
                               note={**note, "github_rate_limited": True})
        if tres.status != 200 or not tres.text:
            exhausted = "error"
            continue
        try:
            tree = json.loads(tres.text)
        except json.JSONDecodeError:
            exhausted = "error"
            continue
        for t in tree.get("tree", []):
            if t.get("type") != "blob":
                continue
            if spec.path_re and not re.search(spec.path_re, t.get("path", "")):
                continue
            raw = f"https://raw.githubusercontent.com/{owner}/{repo}/HEAD/{t['path']}"
            cands.append(Candidate(url=raw, slug=slug_from_repo_path(t["path"])))
        new_watermark = max(new_watermark, created(r))
    return SurfaceScan(cands, exhausted, watermark=new_watermark or None, note=note)


def _scan_pinned(spec: Surface) -> SurfaceScan:
    """Mint pinned-pdf sources from configured URLs WITHOUT downloading (10-design §5.1).

    ``spec.pins`` (an archive of known items) wins when non-empty: one candidate per entry,
    ``slug`` = ``external_id_of(pinned_id)``. Otherwise the single-item Meta path
    (``urls[0]`` + ``pinned_id``). The listing *is* the configured URL list, so the
    surface is always ``complete``.
    """
    items = list(spec.pins) if spec.pins else (
        [{"url": spec.urls[0], "pinned_id": spec.pinned_id, "title": spec.title_prefix}]
        if spec.urls and spec.pinned_id else []
    )
    cands: list[Candidate] = []
    for item in items:
        ext = external_id_of(item["pinned_id"])
        cands.append(Candidate(
            url=item["url"], slug=ext,
            text=item.get("title") or spec.title_prefix,
            date_hint=item.get("date") or "",
        ))
    note: dict[str, Any] = {"pinned": len(cands)}
    if len(cands) == 1:
        note["pinned_id"] = items[0]["pinned_id"]
        note["external_id"] = cands[0].slug
    return SurfaceScan(cands, "complete", note=note)


class WatermarkSource(Protocol):
    """What :func:`prior_watermark` reads: :class:`stack.collect.base.Collector` (its own
    ``coverage`` chain view)."""

    def watermark(self, channel: str, query: str) -> dt.datetime | None: ...


#: GitHub's ``created_at`` rendering, which the github-org scan compares as strings.
GITHUB_TS = "%Y-%m-%dT%H:%M:%SZ"


def prior_watermark(coverage: WatermarkSource | None, channel: str, surface_key: str) -> str | None:
    """The github-org incremental watermark: the end of the newest ``ok`` window of the collector's
    own ``coverage`` chain for ``(channel, query = surface_key)``, rendered as GitHub's
    ``created_at`` (v3 read the newest coverage row's ``window_.from`` over SQL; the collector
    writes the surface's window as ``[prior watermark, new watermark]``). ``coverage = None`` =
    no prior (a first scan, or a test without a store). Only an ``ok`` window advances it, so a
    rate-limited partial scan rescans its repos next run (v3 kept the partial watermark)."""
    if coverage is None:
        return None
    end = coverage.watermark(channel, surface_key)
    return end.astimezone(dt.UTC).strftime(GITHUB_TS) if end is not None else None


def scan_surface(
    spec: Surface, channel: str, coverage: WatermarkSource | None,
    fetcher: HttpFetcher | ReplayFetcher, *, full: bool,
) -> SurfaceScan:
    if spec.method == "html-index":
        return _scan_html_index(spec, fetcher)
    if spec.method == "sitemap":
        return _scan_sitemap(spec, fetcher)
    if spec.method == "feed":
        return _scan_feed(spec, fetcher)
    if spec.method == "github-tree":
        return _scan_github_tree(spec, fetcher)
    if spec.method == "github-org":
        return _scan_github_org(
            spec, fetcher, prior_watermark=prior_watermark(coverage, channel, spec.key), full=full,
        )
    if spec.method == "pinned-pdf":
        return _scan_pinned(spec)
    return SurfaceScan([], "error", note={"unknown_method": spec.method})

