"""``collect.blogs`` — the lab-blog / research-site scrape (§5.6 url row).

Covers the ``kind: scrape`` channels in ``channels.yaml`` that have a :data:`SITE_SPECS` entry
(v1's four near-identical scrape stages, the 2026-08 lab additions and the 2026-10 safety-org
and research-org blogs of the Shallow Review scope). They are SPEC-TABLE ENTRIES
over the shared listing engine :mod:`stack.collect.listing` (also run by ``collect.cards``): read
the site's index / sitemap / feed, filter links by regex, write one ``sources`` row per post on
``sources.blogs`` under the post's channel. **No post content is fetched** (so no in-band entry):
acquisition is ``acquire``'s job.

ONE STAGE, SEVERAL CHANNELS — deliberately not one channel. ``channel`` is the provenance and
policy key (``license_class``, ``self_published_org``, per-channel coverage, release filters);
collapsing them into one ``lab_blog`` channel would erase which lab published a post.

**Identity matches v1 exactly** (v1-import continuity; an id drift re-mints every post):

| channel                    | v1 stem                        | external_id     |
|----------------------------|--------------------------------|-----------------|
| anthropic_alignment_blog   | ``alignment:<y>-<slug>``       | ``<y>-<slug>``  |
| transformer_circuits       | ``circuits:<y>-<slug>``        | ``<y>-<slug>``  |
| openai_alignment_blog      | ``openai-alignment:<y>-<slug>``| ``<y>-<slug>``  |
| openai_blog                | ``openai:<slug>``              | ``<slug>``      |

The year comes from the URL path where the site puts it there (both Anthropic sites), and from
the feed's own date otherwise (alignment.openai.com). A post on a year-qualified site whose year
cannot be established is first matched against ids already known for its slug on the channel —
the own ``sources.blogs`` chain plus this run's rows, so the archive index finds a post the feed
dated earlier in the same run (v3 read the same over SQL ``LIKE '%-<slug>'``; here the match is
exact, ``<YYYY>-<slug>``). Otherwise it is NOT observed under a guessed year: it is counted
``unyeared`` and left for a later pass (a wrong year is a permanent duplicate; a skip is
recoverable).

Rows: ``identity.canonical_url`` = the post URL's fetch form (:func:`stack.urls.fetch_url`: fragment
and tracking parameters dropped, url-normalisation.md §2), the one key ``url`` (its :func:`stack.urls.url_key`, typed
by :mod:`stack.scholarly_records`), ``metadata`` title (listing anchor / feed title, else the slug),
``published_at`` (the listing date when a day is known, else a ``/YYYY/MM/DD/`` permalink date:
:func:`path_date`), ``doc_type = lab_blog``. A site spec's optional ``title_strip_re`` removes a
prefix the site's anchors glue onto the title (a date, on cooperativeai.com). A site spec's
optional ``title_include_re`` (case-insensitive) keeps only the posts whose listing title (after
``title_strip_re``; the slug title when the listing gave none) matches it; the others are not
observed and are counted ``posts_title_filtered`` (``google_research``: the safety stems,
collection-yield.md C9, Tomáš 2026-10-13). No publication-date floor is applied: every listed
post is collected, however old (C10, 2026-10-13: old posts included). Run counts
``titles_from_slug`` and ``undated`` count the observed posts that fell back (no column records a
title's provenance). Every listed
post is observed each run with ``skip_unchanged`` (an unchanged listing record writes no row);
for a known post a slug-derived title and a missing date are carried from its prior row
(:func:`carried`): an archive index without dates must not blank a date the feed gave. No
``provider_record`` (D526): a scraped listing has no API record of the post.
Within a channel the surfaces overlap by design (feed ∪ archive index): one row per post per run,
the first surface's wins.

**Coverage.** One ``coverage`` row per surface, ``query`` = the surface key, window ``[now, now]``
(a listing is a snapshot; v3 wrote ``[channel from_date, today]`` on a complete scan). Outcomes as
``collect.cards`` (its module docstring): ``ok``; ``partial`` when the new-post cap bit; a listing
that could not be read → ``failed_*``.

**Transport** (D26): the harness transport at the channels' ``politeness.rate_rps`` (0.5 req/s
per host), one attempt, 30 s timeout, v1's browser headers; ``respect_robots`` is not applied to
listing reads (v3 did not either). ``STACK_BLOGS_FIXTURES`` still swaps in v3's offline
``ReplayFetcher``.

Options (plan stage line ``options:``, D72; the v3 env var wins when set):
  * ``max_channels``              — the first N eligible channels in ``channels.yaml`` order (0 = all)
  * ``limit`` / ``STACK_BLOGS_LIMIT`` — cap on NEW posts this run (0 = none)
  * ``only``  / ``STACK_BLOGS_ONLY``  — channel ids (list, or comma-separated in the env)
  * ``fraction`` / ``max_queries`` (``STACK_COLLECT_FRACTION`` / ``_MAX_QUERIES``) — the uniform
    scale knobs over the surface list (:mod:`stack.collect.sampling`)
  * ``relist_after_h`` — passes (D226, :meth:`stack.collect.base.Collector.due`): a surface listed
    ``ok`` within this many hours (default 12) is not listed again; ``partial`` (the new-post cap)
    and failed surfaces are, since a re-list makes progress

Dropped from v3 (no column in A.5): ``venue_type = blog``, ``self_published`` and the
``discovery`` provenance blob.

Cardinal rule: imports only ``stack.*`` and the stdlib; nothing from other workflows.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import time
import urllib.parse
from collections.abc import Callable
from functools import partial
from typing import Any

from stack.collect.base import RELIST_OPTIONS, Collector, Pacing, Transport, Window
from stack.collect.listing import (
    BROWSER_HEADERS,
    GENERIC_TEXT_RE,
    Candidate,
    FetchResult,
    HttpFetcher,
    ReplayFetcher,
    Surface,
    SurfaceScan,
    resolve_fetcher,
    scan_surface,
    slugify,
)
from stack.collect.sampling import (
    ENV_FRACTION,
    ENV_MAX_QUERIES,
    SAMPLING_OPTIONS,
    select,
)
from stack.rules import safety_stem_regex
from stack.scholarly_records import keys_for_source
from stack.stage_runner import StageContext, StageResult
from stack.urls import fetch_url

#: The ``options`` keys this stage's code reads; any other is refused (D434)
OPTIONS: frozenset[str] = frozenset({"limit", "max_channels", "only"}) | SAMPLING_OPTIONS | RELIST_OPTIONS

_ENV_LIMIT = "STACK_BLOGS_LIMIT"
_ENV_ONLY = "STACK_BLOGS_ONLY"
_ENV_FIXTURES = "STACK_BLOGS_FIXTURES"

#: The channels' ``politeness.rate_rps`` (0.5 for every blog channel); one attempt, 30 s timeout
#: as v3's ``listing.HttpFetcher``.
LISTING_RPS = 0.5
LISTING_TIMEOUT_S = 30.0
PACING = Pacing(rps=LISTING_RPS, retry_delays_s=(), timeout_s=LISTING_TIMEOUT_S,
                headers=BROWSER_HEADERS)

_T: Transport | None = None


def _sleep(seconds: float) -> None:
    """Politeness delay (module-level so offline tests monkeypatch it); the pacer sleeps here."""
    time.sleep(seconds)


class TransportFetcher:
    """``listing``'s fetcher protocol (``get(url, headers=, params=) -> FetchResult``) over the
    harness transport, so listing reads are paced, gated and counted. Remembers the last failed
    status for :func:`scan_error_class`. (Duplicated in ``collect.cards``: workflows import no
    workflow; its home is ``stack.collect.listing`` next to ``HttpFetcher``.)"""

    def __init__(self, transport: Transport) -> None:
        self.transport = transport
        self.last_bad: FetchResult | None = None

    def get(self, url: str, *, headers: dict[str, str] | None = None,
            params: dict[str, Any] | None = None) -> FetchResult:
        # listing's ReplayFetcher keys sort the params; keep the same URL spelling.
        resp = self.transport.get(url, dict(sorted((params or {}).items())) or None,
                                  headers=headers)
        text = resp.body.decode("utf-8", errors="replace") if resp.body else None
        status = resp.status or 0
        rl = status == 403 and "rate limit" in (text or "").lower()
        res = FetchResult(status=status, text=text, rate_limited=rl)
        if status != 200 or not text:
            self.last_bad = res
        return res


def scan_error_class(scan: SurfaceScan, fetcher: Any) -> tuple[str, bool]:
    """A failed surface scan → ``(error_class, permanent)`` for its coverage window (module
    docstring)."""
    last = getattr(fetcher, "last_bad", None)
    if scan.note.get("github_rate_limited") or (last is not None and last.rate_limited):
        return "rate_limit", False
    if last is None:
        return "parse_error", True  # every read was a 200: the body was not a listing
    s = last.status
    if s == 429:
        return "rate_limit", False
    if 500 <= s < 600:
        return "http_5xx", False
    if s in (401, 403):
        return "blocked", True
    if s in (404, 410):
        return "dead", True
    if s == 200:
        return "parse_error", True
    return "network", False


def close_window(w: Window, scan: SurfaceScan, fetcher: Any, *, capped: bool) -> str:
    """Write the surface's outcome on its window; returns v3's ``exhausted`` string."""
    exhausted = scan.exhausted
    if capped and exhausted == "complete":
        exhausted = "truncated"  # a cap that bit this surface
    if exhausted == "error":
        error_class, permanent = scan_error_class(scan, fetcher)
        w.fail(error_class, permanent=permanent)
    else:
        w.close(exhausted)
    return exhausted


# ─────────────────────────────────────────────────────────────────────────────
# Site specs and identity
# ─────────────────────────────────────────────────────────────────────────────
DOC_TYPE = "lab_blog"

# Slug rules. 'url' = the URL's last path segment (the common case). 'path-after-year' = every
# segment after the /<YYYY>/ marker joined with '-' — transformer-circuits.pub nests papers
# ("2025/attribution-graphs/biology.html" → "attribution-graphs-biology"), and the last segment
# alone ("biology") would collide across papers.
_SLUG_URL = "url"
_SLUG_PATH_AFTER_YEAR = "path-after-year"

_YEAR_IN_PATH = re.compile(r"/(\d{4})(?:/|$)")
_ISO_DATE = re.compile(r"^(\d{4})-\d{2}-\d{2}$")
_DATE_IN_PATH = re.compile(r"/(\d{4})/(\d{2})/(\d{2})/")


# Keyed by channels.yaml channel id. `id_template` and `slug_mode` encode the per-site identity
# rules v1 hardcoded in four separate stage files; the surfaces are what its `_fetch_index` did.
SITE_SPECS: dict[str, dict[str, Any]] = {
    "anthropic_alignment_blog": {
        "id_template": "{year}-{slug}",
        "slug_mode": _SLUG_URL,
        "surfaces": [
            Surface(
                key="anthropic-alignment-index", method="html-index", doc_type=DOC_TYPE,
                urls=["https://alignment.anthropic.com"],
                # /YYYY/<slug>[/][index.html] — v1 scrape_anthropic_alignment's index regex.
                include_re=(
                    r"^https://alignment\.anthropic\.com/\d{4}/[^/?#]+"
                    r"(?:/(?:index\.html)?)?$"
                ),
            ),
        ],
    },
    "transformer_circuits": {
        "id_template": "{year}-{slug}",
        "slug_mode": _SLUG_PATH_AFTER_YEAR,
        "surfaces": [
            # The site declares a real Atom feed (<link rel="alternate" type="application/
            # atom+xml" href="/feed.xml">, 55 entries, served as application/atom+xml). Listed
            # FIRST because a feed is a fraction of the index's bytes and carries real dates;
            # the html-index below stays as the archive fallback (the feed is a recent window).
            Surface(
                key="transformer-circuits-feed", method="feed", doc_type=DOC_TYPE,
                urls=["https://transformer-circuits.pub/feed.xml"],
                include_re=r"^https://transformer-circuits\.pub/\d{4}/.+",
                exclude_re=r"(?i)-updates?(?:\.html|/index\.html|/)?$",
            ),
            Surface(
                key="transformer-circuits-index", method="html-index", doc_type=DOC_TYPE,
                urls=["https://transformer-circuits.pub"],
                include_re=r"^https://transformer-circuits\.pub/\d{4}/.+",
                # v1's UPDATE_RE: "…-update(s)" pages are running notes on an existing paper,
                # not standalone publications. v1 matched the SLUG; here it has to tolerate every
                # way the site spells the same page's URL — the directory form's `/index.html`
                # tail defeated a bare `-updates?$` anchor and let 22 of them through.
                exclude_re=r"(?i)-updates?(?:\.html|/index\.html|/)?$",
            ),
        ],
    },
    "openai_alignment_blog": {
        "id_template": "{year}-{slug}",
        "slug_mode": _SLUG_URL,
        "surfaces": [
            # The feed carries real dates, which this site's URLs do not — and the v1 id form is
            # year-qualified, so the feed is the PRIMARY surface here, not a convenience.
            Surface(
                key="openai-alignment-feed", method="feed", doc_type=DOC_TYPE,
                urls=["https://alignment.openai.com/rss.xml"],
                include_re=r"^https://alignment\.openai\.com/[^/?#]+/?$",
            ),
            # v1's HTML fallback: the feed is a recent window, the index is the archive. Posts it
            # finds beyond the feed's reach have no date and are reported as unyeared.
            Surface(
                key="openai-alignment-index", method="html-index", doc_type=DOC_TYPE,
                urls=["https://alignment.openai.com"],
                include_re=r"^https://alignment\.openai\.com/[^/?#]+/?$",
            ),
        ],
    },
    "openai_blog": {
        "id_template": "{slug}",
        "slug_mode": _SLUG_URL,
        "surfaces": [
            # The news feed FIRST: it carries each post's title and pubDate (2026-10-12: 1,287
            # items, 1,191 under /index/, i.e. the archive, not a recent window). The sitemap has
            # neither: <loc> plus <lastmod>, a modification time, so a sitemap-only listing (v1-v3)
            # gave slug titles and no dates. Feed links drop the trailing slash; the slug makes
            # them the same source as the sitemap's (and the url key drops it too).
            Surface(
                key="openai-news-feed", method="feed", doc_type=DOC_TYPE,
                urls=["https://openai.com/news/rss.xml"],
                include_re=r"^https://openai\.com/index/[^/?#]+/?$",
                exclude_re=r"(?i)(system-card|model-card|preparedness-framework)",
            ),
            # The archive completeness surface: posts the feed does not list keep slug titles.
            Surface(
                key="openai-index-posts", method="sitemap", doc_type=DOC_TYPE,
                urls=["https://openai.com/sitemap.xml"],
                # v1 scrape_openai_blog's INDEX_RE.
                include_re=r"^https://openai\.com/index/[^/?#]+/?$",
                # Card + framework pages under /index/ are collect.cards' territory (it mints them
                # as <org>-<slug> on openai_cards). Minting them here too would produce a second
                # source per card under a different key, and v3 has no cross-source title dedup
                # yet to merge them — so they are excluded at discovery rather than deduped later.
                exclude_re=r"(?i)(system-card|model-card|preparedness-framework)",
            ),
        ],
    },
    # ── added 2026-08-10 (coverage audit: blogs were collected from 2 of 37 tracked orgs) ──
    # Surface choice per site was MEASURED, not assumed: a declared feed wins (small, dated,
    # cheap to re-walk), sitemap next, html-index last. None of these sites put a year in the
    # post path, so identity is `{slug}` — the bare last path segment — as with openai_blog.
    "google_research": {
        "id_template": "{slug}",
        "slug_mode": _SLUG_URL,
        # C9 (collection-yield.md §9.3, Tomáš 2026-10-13): only posts with a safety stem in the
        # title (96 of 100 dropped on the 2026-10 backlog; 1 labelled relevant among them).
        "title_include_re": safety_stem_regex(),
        "surfaces": [
            # RSS linked from the blog index (101 items). research.google/blog/<slug>/
            Surface(
                key="google-research-feed", method="feed", doc_type=DOC_TYPE,
                urls=["https://research.google/blog/rss/"],
                include_re=r"^https://research\.google/blog/[^/?#]+/?$",
            ),
        ],
    },
    "mistral_blog": {
        "id_template": "{slug}",
        "slug_mode": _SLUG_URL,
        "surfaces": [
            # Declared via <link rel="alternate" type="application/rss+xml"> (80 items).
            Surface(
                key="mistral-news-feed", method="feed", doc_type=DOC_TYPE,
                urls=["https://mistral.ai/news/rss"],
                include_re=r"^https://mistral\.ai/news/[^/?#]+/?$",
            ),
        ],
    },
    "deepmind_blog": {
        "id_template": "{slug}",
        "slug_mode": _SLUG_URL,
        "surfaces": [
            # No feed, and the /discover/blog/ index server-renders zero post links (fully JS-
            # hydrated). The robots.txt-declared sitemap does carry them: 343 posts under
            # /blog/ (NOT the /discover/blog/ path the site links to), 722 URLs total. It
            # carries no publication date (its lastmod is the 2026-07 site migration's for most
            # posts), so these posts are undated here; collect.card-dates-lookup dates them from
            # the post page's article:published_time (UNDATED_BLOGS, D662).
            Surface(
                key="deepmind-blog-sitemap", method="sitemap", doc_type=DOC_TYPE,
                urls=["https://deepmind.google/sitemap.xml"],
                include_re=r"^https://deepmind\.google/blog/[^/?#]+/?$",
                # Model/framework pages are collect.cards' territory (deepmind_cards), same
                # reasoning as openai_blog's exclusion: one source per item, not two.
                exclude_re=r"(?i)(model-card|frontier-safety-framework)",
            ),
        ],
    },
    # ── safety-org and research-org blogs (SR scope, 2026-10-12; docs/scope/shallow-review.md
    # (e)). Surfaces measured 2026-10-12 (robots.txt, blog index, declared feed, sitemap; ≤ 4
    # requests per site). Identity `{slug}` (the URL's last path segment; new channels, no v1
    # continuity). Where a site has no feed, a server-rendered index with titled or dated anchors
    # is listed BEFORE the sitemap: the first surface's row wins within a run (one row per post)
    # and a sitemap carries neither titles nor publication dates (D155), so sitemap-first would
    # pin slug titles on every post the index also lists. A post the site files under two
    # sections with the same slug (e.g. far.ai /blog/x and /research/x) is one source.
    "miri_blog": {
        "id_template": "{slug}",
        "slug_mode": _SLUG_URL,
        "surfaces": [
            # WordPress RSS, 6 items, dated (a recent window).
            Surface(
                key="miri-feed", method="feed", doc_type=DOC_TYPE,
                urls=["https://intelligence.org/feed/"],
                include_re=r"^https://intelligence\.org/\d{4}/\d{2}/\d{2}/[^/?#]+/?$",
            ),
            # The archive: robots.txt-declared Yoast index (post-sitemap1-3 + page/faq/team);
            # /YYYY/MM/DD/ in the post path is the publication date (path_date).
            Surface(
                key="miri-sitemap", method="sitemap", doc_type=DOC_TYPE,
                urls=["https://intelligence.org/sitemap_index.xml"],
                include_re=r"^https://intelligence\.org/\d{4}/\d{2}/\d{2}/[^/?#]+/?$",
            ),
        ],
    },
    "metr_blog": {
        "id_template": "{slug}",
        "slug_mode": _SLUG_URL,
        "surfaces": [
            # RSS (<link rel=alternate>), 104 items, dated, back to 2023: the archive. /blog/ and
            # /notes/ posts and /evaluations/ reports; translations (/zh-Hans/, /es/), PDFs, arXiv
            # and regulations.gov links are not matched. The slug's YYYY-MM-DD- prefix is the
            # date and is stripped from the id (listing.slug_from_url).
            Surface(
                key="metr-feed", method="feed", doc_type=DOC_TYPE,
                urls=["https://metr.org/feed.xml"],
                include_re=r"^https://metr\.org/(?:blog|notes|evaluations)/[^/?#]+/?$",
            ),
        ],
    },
    "transluce_blog": {
        "id_template": "{slug}",
        "slug_mode": _SLUG_URL,
        "surfaces": [
            # No feed; the home page's cards concatenate kind+date+title+teaser into one anchor.
            # The sitemap (58 URLs, lastmod only) puts posts at top-level slugs, so the
            # non-post pages are excluded by name (the 2026-10-12 set; a new top-level page
            # would be listed as a post) plus /docent/blog/<slug>.
            Surface(
                key="transluce-sitemap", method="sitemap", doc_type=DOC_TYPE,
                urls=["https://transluce.org/sitemap.xml"],
                include_re=r"^https://transluce\.org/(?:docent/blog/)?[^/?#]+/?$",
                exclude_re=(
                    r"^https://transluce\.org/(?:about|apply|bookshelf|docent|donate|"
                    r"investigations|news|tools|2025-fundraiser|independence-and-transparency-"
                    r"policy|responsible-disclosure-policy)/?$"
                ),
            ),
        ],
    },
    "goodfire_blog": {
        "id_template": "{slug}",
        "slug_mode": _SLUG_URL,
        "surfaces": [
            # goodfire.ai redirects to goodfire.com. No feed; the /research index's anchors carry
            # no text. Sitemap (robots.txt): 42 /research/ + 21 /blog/, lastmod only.
            Surface(
                key="goodfire-sitemap", method="sitemap", doc_type=DOC_TYPE,
                urls=["https://www.goodfire.com/sitemap.xml"],
                include_re=r"^https://www\.goodfire\.com/(?:research|blog)/[^/?#]+/?$",
            ),
        ],
    },
    "uk_aisi_blog": {
        "id_template": "{slug}",
        "slug_mode": _SLUG_URL,
        "surfaces": [
            # No feed. /blog index: 100 posts with title anchors, undated (the sitemap's 100).
            Surface(
                key="uk-aisi-blog-index", method="html-index", doc_type=DOC_TYPE,
                urls=["https://www.aisi.gov.uk/blog"],
                include_re=r"^https://www\.aisi\.gov\.uk/blog/[^/?#]+/?$",
            ),
            # /research index: 32 titled papers (of the sitemap's 65).
            Surface(
                key="uk-aisi-research-index", method="html-index", doc_type=DOC_TYPE,
                urls=["https://www.aisi.gov.uk/research"],
                include_re=r"^https://www\.aisi\.gov\.uk/research/[^/?#]+/?$",
            ),
            # Completeness: sitemap (robots.txt) /blog/ 100 + /research/ 65, lastmod only.
            Surface(
                key="uk-aisi-sitemap", method="sitemap", doc_type=DOC_TYPE,
                urls=["https://www.aisi.gov.uk/sitemap.xml"],
                include_re=r"^https://www\.aisi\.gov\.uk/(?:blog|research)/[^/?#]+/?$",
            ),
        ],
    },
    "redwood_blog": {
        "id_template": "{slug}",
        "slug_mode": _SLUG_URL,
        "surfaces": [
            # Substack RSS, 20 items, dated (a recent window).
            Surface(
                key="redwood-feed", method="feed", doc_type=DOC_TYPE,
                urls=["https://blog.redwoodresearch.org/feed"],
                include_re=r"^https://blog\.redwoodresearch\.org/p/[^/?#]+/?$",
            ),
            # The archive: Substack sitemap, 138 /p/ posts, lastmod only.
            Surface(
                key="redwood-sitemap", method="sitemap", doc_type=DOC_TYPE,
                urls=["https://blog.redwoodresearch.org/sitemap.xml"],
                include_re=r"^https://blog\.redwoodresearch\.org/p/[^/?#]+/?$",
            ),
        ],
    },
    "apollo_blog": {
        "id_template": "{slug}",
        "slug_mode": _SLUG_URL,
        "surfaces": [
            # No feed. /blog index: ~15 posts, dated, title anchors (some posts' first anchor is
            # an empty image link: slug title). Index links are www., sitemap links are not.
            Surface(
                key="apollo-blog-index", method="html-index", doc_type=DOC_TYPE,
                urls=["https://www.apolloresearch.ai/blog"],
                include_re=r"^https://(?:www\.)?apolloresearch\.ai/blog/[^/?#]+/?$",
            ),
            # /science index: 11 papers, dated, "Read more" anchors (slug titles).
            Surface(
                key="apollo-science-index", method="html-index", doc_type=DOC_TYPE,
                urls=["https://www.apolloresearch.ai/science"],
                include_re=r"^https://(?:www\.)?apolloresearch\.ai/science/[^/?#]+/?$",
            ),
            # Completeness: sitemap (robots.txt) /science/ 27, /governance/ 17, /blog/ 14,
            # /monitoring/ 7; lastmod only. /press/ and /team/ are not posts.
            Surface(
                key="apollo-sitemap", method="sitemap", doc_type=DOC_TYPE,
                urls=["https://www.apolloresearch.ai/sitemap.xml"],
                include_re=(
                    r"^https://(?:www\.)?apolloresearch\.ai/"
                    r"(?:blog|science|governance|monitoring)/[^/?#]+/?$"
                ),
            ),
        ],
    },
    "arc_blog": {
        "id_template": "{slug}",
        "slug_mode": _SLUG_URL,
        "surfaces": [
            # Ghost RSS (<link rel=alternate>), 15 items (Ghost's page size), dated, to 2023-12.
            Surface(
                key="arc-feed", method="feed", doc_type=DOC_TYPE,
                urls=["https://www.alignment.org/blog/rss/"],
                include_re=r"^https://www\.alignment\.org/blog/[^/?#]+/?$",
            ),
            # The archive: Ghost's posts sitemap, 28 posts, lastmod only.
            Surface(
                key="arc-sitemap", method="sitemap", doc_type=DOC_TYPE,
                urls=["https://www.alignment.org/sitemap-posts.xml"],
                include_re=r"^https://www\.alignment\.org/blog/[^/?#]+/?$",
            ),
        ],
    },
    "epoch_blog": {
        "id_template": "{slug}",
        "slug_mode": _SLUG_URL,
        "surfaces": [
            # Atom (<link rel=alternate>), 50 items, dated, back to 2026-05 (a recent window):
            # /publications/ and /gradient-updates/ (the newsletter essays). Not /data-insights/
            # (short chart notes, 19 of 50), /latest/ (announcements) or project pages.
            Surface(
                key="epoch-feed", method="feed", doc_type=DOC_TYPE,
                urls=["https://epoch.ai/atom.xml"],
                include_re=r"^https://epoch\.ai/(?:publications|gradient-updates)/[^/?#]+/?$",
            ),
            # The archive: the publications child of the robots.txt sitemap index (the index's
            # other six children are data, models, topics, benchmarks and pages).
            Surface(
                key="epoch-sitemap", method="sitemap", doc_type=DOC_TYPE,
                urls=["https://epoch.ai/sitemap-publications-0.xml"],
                include_re=r"^https://epoch\.ai/(?:publications|gradient-updates)/[^/?#]+/?$",
            ),
        ],
    },
    "forethought_blog": {
        "id_template": "{slug}",
        "slug_mode": _SLUG_URL,
        "surfaces": [
            # RSS (<link rel=alternate>), 78 items, dated, back to 2025: the whole archive.
            Surface(
                key="forethought-feed", method="feed", doc_type=DOC_TYPE,
                urls=["https://www.forethought.org/feed"],
                include_re=r"^https://www\.forethought\.org/research/[^/?#]+/?$",
            ),
        ],
    },
    "far_ai_blog": {
        "id_template": "{slug}",
        "slug_mode": _SLUG_URL,
        "surfaces": [
            # No feed. /blog index: 55 posts, dated, but every anchor is empty (slug titles).
            Surface(
                key="far-ai-blog-index", method="html-index", doc_type=DOC_TYPE,
                urls=["https://www.far.ai/blog"],
                include_re=r"^https://www\.far\.ai/blog/[^/?#]+/?$",
            ),
            # Completeness: sitemap (robots.txt) /research/ 60 + /blog/ 56, lastmod only.
            # /author/ (597) and /event-recordings/ (232) are not posts.
            Surface(
                key="far-ai-sitemap", method="sitemap", doc_type=DOC_TYPE,
                urls=["https://www.far.ai/sitemap.xml"],
                include_re=r"^https://www\.far\.ai/(?:blog|research)/[^/?#]+/?$",
            ),
        ],
    },
    "timaeus_blog": {
        "id_template": "{slug}",
        "slug_mode": _SLUG_URL,
        "surfaces": [
            # No feed, /blog index 404s. robots.txt sitemap index → sitemap-0.xml: /research/
            # 31 + /blog/ 29, lastmod only; most slugs carry a YYYY-MM-DD- prefix (the date).
            # Blog posts nest one level under a series (/blog/slt/2023-02-06-gradient-surfing/).
            # /blog/updates/ is excluded: hiring and org notes, and their date-stripped slugs
            # collide ("hiring" x5). /projects/ and /events/ are not posts.
            Surface(
                key="timaeus-sitemap", method="sitemap", doc_type=DOC_TYPE,
                urls=["https://timaeus.co/sitemap-index.xml"],
                include_re=r"^https://timaeus\.co/(?:research|blog)/(?:[^/?#]+/)?[^/?#]+/?$",
                exclude_re=r"^https://timaeus\.co/blog/updates/",
            ),
        ],
    },
    "clr_blog": {
        "id_template": "{slug}",
        "slug_mode": _SLUG_URL,
        "surfaces": [
            # No feed. The /research/ index lists ~140 titled pieces (year-only dates, not
            # taken). A sitemap exists (sitemap-index.xml → sitemap-0.xml), not used: the
            # index is the archive and carries titles.
            Surface(
                key="clr-research-index", method="html-index", doc_type=DOC_TYPE,
                urls=["https://longtermrisk.org/research/"],
                include_re=r"^https://longtermrisk\.org/research/[^/?#]+/?$",
            ),
        ],
    },
    "softmax_blog": {
        "id_template": "{slug}",
        "slug_mode": _SLUG_URL,
        "surfaces": [
            # No feed. /blog index: 5 posts, anchors concatenate title, subtitle, author and
            # date ("Red Button, Blue ButtonTeaching AI to supercooperate…"). The sitemap index
            # expands into paginated forum sitemaps (gods-of-the-arena), so not used.
            Surface(
                key="softmax-blog-index", method="html-index", doc_type=DOC_TYPE,
                urls=["https://softmax.com/blog"],
                include_re=r"^https://softmax\.com/blog/[^/?#]+/?$",
            ),
        ],
    },
    "meaning_alignment_blog": {
        "id_template": "{slug}",
        "slug_mode": _SLUG_URL,
        "surfaces": [
            # meaningalignment.org's sitemap has 8 top-level pages, no posts; the posts are on
            # its Substack (linked from the home page). Substack RSS, 20 items, dated, to 2022.
            Surface(
                key="meaning-alignment-feed", method="feed", doc_type=DOC_TYPE,
                urls=["https://meaningalignment.substack.com/feed"],
                include_re=r"^https://meaningalignment\.substack\.com/p/[^/?#]+/?$",
            ),
        ],
    },
    "cooperative_ai_blog": {
        "id_template": "{slug}",
        "slug_mode": _SLUG_URL,
        "title_strip_re": r"^[A-Z][a-z]+ \d{1,2}, \d{4}",
        "surfaces": [
            # No feed. /blog index: 29 /post/ links (the sitemap's 25 are a subset), dated;
            # anchors prefix the date to the title ("September 9, 2026A Science of …"), which
            # title_strip_re removes.
            Surface(
                key="cooperative-ai-blog-index", method="html-index", doc_type=DOC_TYPE,
                urls=["https://www.cooperativeai.com/blog"],
                include_re=r"^https://www\.cooperativeai\.com/post/[^/?#]+/?$",
            ),
        ],
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# Identity
# ─────────────────────────────────────────────────────────────────────────────
def year_of(cand: Candidate) -> str:
    """The post's year: from a ``/YYYY/`` path segment, else the listing's date hint, else ''."""
    m = _YEAR_IN_PATH.search(cand.url)
    if m:
        return m.group(1)
    m = _ISO_DATE.match(cand.date_hint or "")
    return m.group(1) if m else ""


def post_slug(cand: Candidate, slug_mode: str) -> str:
    """The slug half of the external_id, per the channel's ``slug_mode``."""
    if slug_mode != _SLUG_PATH_AFTER_YEAR:
        return cand.slug
    m = _YEAR_IN_PATH.search(cand.url)
    if not m:
        return cand.slug
    tail = cand.url[m.end():]
    tail = re.sub(r"\.(html?|pdf|md)$", "", tail.split("?")[0].split("#")[0].rstrip("/"), flags=re.IGNORECASE)
    tail = re.sub(r"/index$", "", tail)
    return slugify(tail.replace("/", "-")) or cand.slug




def known_ids(c: Collector, ctx: StageContext) -> dict[tuple[str, str], str]:
    """``(channel, slug) → <YYYY>-<slug>`` for the year-qualified ids already in the own
    ``sources`` chain (the newest year wins, as v3's ``ORDER BY external_id DESC``)."""
    out: dict[tuple[str, str], str] = {}
    if ctx.prior.get("sources") is None and ctx.inputs.get("sources") is None:
        return out  # first run: no own chain yet (the harness starts it)
    for ident in ctx.view("sources").column("identity").to_pylist():
        _remember(out, ident["channel"], ident["external_id"] or "")
    return out


def _remember(known: dict[tuple[str, str], str], channel: str, external_id: str) -> None:
    m = _YEARED_ID.match(external_id)
    if m:
        key = (channel, m.group(2))
        if external_id > known.get(key, ""):
            known[key] = external_id


def _known_id(known: dict[tuple[str, str], str], channel: str, slug: str) -> str | None:
    return known.get((channel, slug))


_YEARED_ID = re.compile(r"^(\d{4})-(.+)$")


def external_id_for(
    cand: Candidate, spec: dict[str, Any], known: Callable[[str], str | None] | None = None,
) -> str | None:
    """The channel's ``external_id`` for a candidate, or ``None`` when it cannot be established
    (a year-qualified site whose post carries no year and whose slug ``known`` does not resolve —
    see the module docstring on why guessing is worse than skipping)."""
    slug = post_slug(cand, str(spec["slug_mode"]))
    if not slug:
        return None
    template = str(spec["id_template"])
    if "{year}" not in template:
        return template.format(slug=slug)
    year = year_of(cand)
    if year:
        return template.format(year=year, slug=slug)
    return known(slug) if known is not None else None


def path_date(url: str) -> str | None:
    """``YYYY-MM-DD`` from a ``/YYYY/MM/DD/`` run of path segments (WordPress permalinks, e.g.
    intelligence.org/2026/09/30/<slug>/), else ``None``. A publication date, unlike a sitemap's
    ``lastmod``; used only when the listing gave no day-precision date."""
    m = _DATE_IN_PATH.search(urllib.parse.urlsplit(url).path)
    if not m:
        return None
    try:
        return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
    except ValueError:
        return None


def slug_title(cand: Candidate) -> str | None:
    """The fallback title: the slug title-cased (no listing text to take it from)."""
    return cand.slug.replace("-", " ").title() or None


def title_included(cand: Candidate, site: dict[str, Any]) -> bool:
    """Whether the site's ``title_include_re`` (case-insensitive; none = every post) matches the
    post's listing title: the anchor / feed text after ``title_strip_re``, else :func:`slug_title`."""
    pattern = site.get("title_include_re")
    if not pattern:
        return True
    text = cand.text or ""
    if site.get("title_strip_re"):
        text = re.sub(site["title_strip_re"], "", text, count=1).strip()
    title = text if text and not GENERIC_TEXT_RE.match(text) else slug_title(cand)
    return re.search(pattern, title or "", re.IGNORECASE) is not None


def has_text_title(cand: Candidate) -> bool:
    """Whether the listing gave a meaningful anchor / feed title for this post."""
    return bool(cand.text) and not GENERIC_TEXT_RE.match(cand.text)


def title_hint(cand: Candidate) -> str | None:
    """A listing-derived title (never a content fetch): the anchor / feed title when meaningful,
    else :func:`slug_title`."""
    return cand.text if has_text_title(cand) else slug_title(cand)


# ─────────────────────────────────────────────────────────────────────────────
# The stage
# ─────────────────────────────────────────────────────────────────────────────
_ISO_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def url_keys(channel_group: str, channel: str, external_id: str,
             canonical_url: str) -> list[tuple[str, str]]:
    """The row's identity keys (``url`` = the canonical URL, typed by
    :mod:`stack.scholarly_records`) as the harness's pairs."""
    return [(k["kind"], k["value"])
            for k in keys_for_source(channel_group, channel, external_id, canonical_url)]


def carried(c: Collector, channel: str, external_id: str, cand: Candidate, title: str | None,
            published: str | None) -> tuple[str | None, str | None]:
    """``(title, published)`` for an already-known source, filling what this listing lacks from
    its prior row: a surface that carries less (an archive index without dates, an anchor without
    text) must not overwrite what an earlier sighting established (v3 wrote listing metadata only
    for a NEW source)."""
    prior = c.prior(channel, external_id)
    if prior is None or prior["state"] != "observed":
        return title, published
    md = prior["metadata"]
    if published is None and md["published_at"] is not None:
        published = md["published_at"].date().isoformat()
    if md["title"] and not has_text_title(cand):
        title = md["title"]  # this listing had no usable anchor text: keep the earlier title
    return title, published


def _listing_fetcher() -> TransportFetcher | HttpFetcher | ReplayFetcher:
    """The fetcher for one surface: v3's replay seam when ``STACK_BLOGS_FIXTURES`` is set, else a
    fresh :class:`TransportFetcher` over this run's transport."""
    if os.environ.get(_ENV_FIXTURES):
        return resolve_fetcher(_ENV_FIXTURES)
    assert _T is not None
    return TransportFetcher(_T)


def _scale_env(c: Collector) -> dict[str, str]:
    """``os.environ`` with the plan's ``fraction`` / ``max_queries`` options for
    :func:`stack.collect.sampling.select` (a set env var wins)."""
    env = dict(os.environ)
    for name, var in (("fraction", ENV_FRACTION), ("max_queries", ENV_MAX_QUERIES)):
        value = c.option(name, None, env=var, cast=str)
        if value is not None:
            env[var] = value
    return env


def _id_list(value: Any) -> set[str]:
    """A channel-id option: a list, or a comma-separated string (the env form)."""
    items = value.split(",") if isinstance(value, str) else (value or [])
    return {str(v).strip() for v in items if str(v).strip()}


def run(ctx: StageContext) -> StageResult:
    global _T
    from stack.config import load

    c = Collector(ctx)
    config = load()
    limit = c.option("limit", 0, env=_ENV_LIMIT, cast=int) or 0
    only = _id_list(c.option("only", None, env=_ENV_ONLY))
    max_channels = c.option("max_channels", 0, cast=int) or 0

    # Config-driven: every kind=scrape channel with a ported SITE_SPECS entry, in config order.
    channel_ids = [ch.id for ch in config.channels.channels
                   if ch.kind == "scrape" and ch.id in SITE_SPECS and (not only or ch.id in only)]
    if max_channels:
        channel_ids = channel_ids[:max_channels]
    # Scale control selects SURFACES (the unit a scrape plans in); the full count is reported.
    all_surfaces = [(cid, sp) for cid in channel_ids for sp in SITE_SPECS[cid]["surfaces"]]
    selected, sampling = select(all_surfaces, lambda cs: f"{cs[0]}:{cs[1].key}",
                                env=_scale_env(c))

    known = known_ids(c, ctx)
    _T = c.transport(PACING, sleep=lambda s: _sleep(s))
    tally = {"surfaces_complete": 0, "surfaces_truncated": 0, "surfaces_error": 0,
             "posts_seen": 0, "posts_new": 0, "posts_capped": 0, "unyeared": 0,
             "titles_from_slug": 0, "undated": 0, "posts_title_filtered": 0}
    minted = 0
    observed: set[tuple[str, str]] = set()
    relist = c.relist_after()
    try:
        with c.halts():
            for i, (channel, spec) in enumerate(selected):
                c.progress(done=i, total=len(selected))
                if not c.due(channel, spec.key, relist_after=relist):
                    continue  # listed `ok` within relist_after_h (D226)
                chan = c.channel(channel)
                site = SITE_SPECS[channel]
                fetcher = _listing_fetcher()
                # listing types the fetcher as its two classes, not a protocol (reported)
                scan = scan_surface(spec, channel, c, fetcher, full=False)  # type: ignore[arg-type]
                capped = False
                with c.window(channel, spec.key, c.now, c.now) as w:
                    for cand in scan.candidates:
                        ext = external_id_for(cand, site, partial(_known_id, known, channel))
                        if ext is None:
                            tally["unyeared"] += 1
                            continue
                        if (channel, ext) in observed:
                            # feed ∪ index overlap: one row per post per run (D228 attribution)
                            c.attribute(channel, ext, query=spec.key, label=channel)
                            continue
                        if not title_included(cand, site):
                            tally["posts_title_filtered"] += 1
                            continue  # C9: not observed, not in the window's `seen`
                        w.seen += 1
                        new = c.prior(channel, ext) is None
                        if new and limit and minted >= limit:
                            capped = True
                            tally["posts_capped"] += 1
                            continue  # not observed; a capped surface is partial
                        url = fetch_url(cand.url) or cand.url
                        if site.get("title_strip_re"):
                            cand.text = re.sub(site["title_strip_re"], "", cand.text, count=1).strip()
                        title = title_hint(cand)
                        published = (cand.date_hint if _ISO_DAY.match(cand.date_hint or "")
                                     else path_date(cand.url))
                        title, published = carried(c, channel, ext, cand, title, published)
                        # the fallbacks, counted per observed post (metadata has no field for a
                        # title's provenance): a slug title (none from this listing and none
                        # better carried), no publication date
                        tally["titles_from_slug"] += not has_text_title(cand) and title == slug_title(cand)
                        tally["undated"] += published is None
                        record = {"url": url, "title": title, "published": published,
                                  "doc_type": DOC_TYPE}
                        c.observe(channel, ext, record,
                                  keys=url_keys(chan.channel_group, channel, ext, url),
                                  canonical_url=url, skip_unchanged=True,
                                  metadata={"title": title, "published_at": published,
                                            "doc_type": DOC_TYPE},
                                  query=spec.key, label=channel)  # matched_by (D228)
                        observed.add((channel, ext))
                        _remember(known, channel, ext)
                        if new:
                            w.kept += 1
                            minted += 1
                    exhausted = close_window(w, scan, fetcher, capped=capped)
                tally[f"surfaces_{exhausted}"] += 1
                tally["posts_seen"] += w.seen
                tally["posts_new"] += w.kept
            c.progress(done=len(selected), total=len(selected))
    finally:
        _T = None
    c.counts.update(tally, channels=len(channel_ids), surfaces_planned=int(sampling["planned"]),
                    surfaces_selected=int(sampling["selected"]), new_cap=limit)
    return c.finish()
