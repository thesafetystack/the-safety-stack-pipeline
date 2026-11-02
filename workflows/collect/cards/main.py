"""``collect.cards`` — the per-lab model-card / policy-doc watcher (§5.6 url row; v1
``pipeline/config/doc_watch.yaml`` + ``pipeline/stages/lib/doc_watch.py``).

Per configured ``kind: watch`` ``*_cards`` channel it reads ONLY the channel's listing/seed
surface(s) — a sitemap XML, an HTML card index, a GitHub API tree/org listing, or a pinned PDF URL
— parses the listing in code (the shared engine :mod:`stack.collect.listing`, also run by
``collect.blogs``), applies the include/exclude/policy regexes, and writes one ``sources`` row per
listed item on ``sources.cards`` under the item's ``*_cards`` channel. **No item content is
fetched here** (so no in-band entry): acquisition is ``acquire``'s job. A pinned-pdf source is
observed from its configured URL without downloading it.

**Identity** (C32, §6 url group). ``external_id`` is the v1 ``make_doc_id`` org-slug form
(``<org>-<slug>``, the ``systemcard:``/``policy:`` prefix stripped; v1-import continuity, pinned
in ``tests/test_collect_cards.py``); ``identity.canonical_url`` = the item URL as listed in its fetch
form (:func:`stack.urls.fetch_url`, url-normalisation.md §2); the one key is ``url`` = its :func:`stack.urls.url_key`
(raw.githubusercontent refs collapse to ``/-/``), typed by :mod:`stack.scholarly_records`.
``metadata``: ``title`` from the listing (an anchor's title block when the anchor holds several text blocks,
e.g. dateline + title + description on deploymentsafety.openai.com; else its text; else :func:`slug_title`, version
dots restored), ``published_at`` the
listing's resolved publication date (:mod:`stack.lookup.card_dates` evidence: URL date prefix,
listing datelines, never a sitemap ``lastmod``), ``doc_type`` ∈ {system_card, model_card,
framework_policy} per the ``policy_re`` split, an item of a ``system_card`` surface that says "model card" in its URL
or text being a ``model_card``. Every listed item is observed each run with
``skip_unchanged``: an item whose listing record (url, title, date, doc_type) is unchanged writes
no row; a new or changed one does. For a known source, a title derived from the slug and a
missing date are carried from its prior row (:func:`carried`), so a surface that carries less
does not overwrite what an earlier sighting established; so are a title and doc_type the code before D759 derived
from the same listing (a changed field would re-triage the document; the dataset release corrects those at export).

**Coverage.** One ``coverage`` row per listing surface scanned, ``query`` = the surface key.
A listing is a snapshot, so its window is ``[now, now]`` — except ``github-org``, whose window is
``[prior watermark, new watermark]`` over repo ``created_at`` (a first ``full`` sweep: from its oldest
repo): :func:`stack.collect.listing.prior_watermark` reads it back from the newest ``ok`` window (only
``ok`` advances it, so a rate-limited scan rescans its repos next run; v3 kept the old watermark the
same way). **History (D555):** without ``full`` the first contact bootstraps the watermark to the
newest repo (no history); with ``full`` it sweeps the org's repos oldest first, ``max_repos`` (25) per
run. A scan the cap stopped is a banked prefix: its window is ``ok`` up to the newest repo it read, so
the next run resumes after it, and the run ends ``scope_complete = false`` (``surfaces_capped``, build
reason ``limit``) until the sweep has caught up. An existing watermark is resumed with or without
``full``; a banked window is written ``ok`` but not exhausted (``is_exhausted`` false), and such a
surface is asked again at once, not held by ``relist_after_h``. Only the API's first page (100 repos,
newest first) is read. Outcomes:
``complete`` → ``ok``; a GitHub-truncated tree or the new-source cap → ``partial``; a listing that
could not be read → ``failed_*`` (:func:`scan_error_class`: GitHub 403 rate limit / 429 →
``rate_limit``, 5xx → ``http_5xx``, transport → ``network``, 401/403 → ``blocked``, 404/410 →
``dead``, a 200 that is not a listing → ``parse_error``).

**Transport** (D26). Listing reads go through the harness transport (``HostPacer`` at the
channels' ``politeness.rate_rps`` = 0.5 req/s per host, ``throttled_open`` Gate), one attempt per
read and a 30 s timeout as v3's ``HttpFetcher``, with v1's browser headers. ``respect_robots`` in
``channels.yaml`` is not applied to listing reads (v3 did not either). ``STACK_CARDS_FIXTURES``
still swaps in v3's offline ``ReplayFetcher`` (bypasses the transport).

Options (plan stage line ``options:``, D72; the v3 env var wins when set):
  * ``max_urls`` / ``STACK_CARDS_LIMIT`` — cap on NEW sources this run (0 = none); a capped
    surface is ``partial``.
  * ``full``     / ``STACK_CARDS_FULL``  — github-org watchers sweep repo history from first contact,
    25 repos per run (default: bootstrap the watermark on first contact, no historical sweep; D555).
  * ``only``     / ``STACK_CARDS_ONLY``  — channel ids (list, or comma-separated in the env).
  * ``fraction`` / ``max_queries`` (``STACK_COLLECT_FRACTION`` / ``_MAX_QUERIES``) — the uniform
    scale knobs over the surface list (:mod:`stack.collect.sampling`).
  * ``relist_after_h`` — passes (D226, :meth:`stack.collect.base.Collector.due`): a surface listed
    ``ok`` within this many hours (default 12) is not listed again; ``partial`` (the new-source
    cap, a truncated tree) and failed surfaces are.

Dropped from v3 (no column in A.5): ``self_published`` (derivable from the channel's
``self_published_org``), the ``discovery`` provenance blob (surface, method, listing URL) and the
``card_date_evidence`` list (its resolved date is ``published_at``). No ``provider_record`` (D526): a
scraped surface has no API record of the card.

Why the watch specs live HERE and not in ``channels.yaml``: ``ChannelDef`` is ``extra="forbid"``
with no ``params``/``watch`` field, so the v1 spec shapes cannot be channel config; they are
:data:`WATCH_SPECS` below, keyed by channel id (see the ``[FLAG]`` in ``channels.yaml``).

Cardinal rule: imports only ``stack.*`` and the stdlib; nothing from other workflows.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import time
from typing import Any

from stack.collect.base import RELIST_OPTIONS, Collector, Pacing, Transport, Window
from stack.collect.listing import (
    BROWSER_HEADERS,
    GENERIC_TEXT_RE,
    Candidate,
    ChannelWatch,
    FetchResult,
    HttpFetcher,
    ReplayFetcher,
    Surface,
    SurfaceScan,
    external_id_of,
    make_doc_id,
    resolve_fetcher,
    scan_surface,
)
from stack.collect.sampling import (
    ENV_FRACTION,
    ENV_MAX_QUERIES,
    SAMPLING_OPTIONS,
    select,
)
from stack.scholarly_records import keys_for_source
from stack.stage_runner import StageContext, StageResult
from stack.urls import fetch_url

#: The ``options`` keys this stage's code reads; any other is refused (D434)
OPTIONS: frozenset[str] = frozenset({"full", "max_urls", "only"}) | SAMPLING_OPTIONS | RELIST_OPTIONS

# ─────────────────────────────────────────────────────────────────────────────
# Env-var interface (v3) and transport seams
# ─────────────────────────────────────────────────────────────────────────────
_ENV_LIMIT = "STACK_CARDS_LIMIT"
_ENV_FULL = "STACK_CARDS_FULL"
_ENV_ONLY = "STACK_CARDS_ONLY"
_ENV_FIXTURES = "STACK_CARDS_FIXTURES"

#: The channels' ``politeness.rate_rps`` (0.5 for every ``*_cards`` channel); v3's listing reads
#: were one attempt with a 30 s timeout (``listing.HttpFetcher``).
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
    status for :func:`scan_error_class`. (Duplicated in ``collect.blogs``: workflows import no
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
# Watch specs (v1 doc_watch.yaml data, ported)
# ─────────────────────────────────────────────────────────────────────────────
# Keyed by channels.yaml channel id. org_slug MUST match v1's doc_watch.yaml org_slug (it is the
# id root, so it decides external_id compatibility with collect.v1_import). NOTE deepmind embeds
# "google-deepmind" (not registry's "deepmind"); qwen embeds "alibaba".
WATCH_SPECS: dict[str, ChannelWatch] = {
    "anthropic_cards": ChannelWatch(
        org_slug="anthropic",
        surfaces=[
            Surface(
                key="anthropic-systemcards", method="html-index", doc_type="system_card",
                urls=["https://www.anthropic.com/system-cards"],
                include_re=r"anthropic\.com/[a-z0-9-]*-(?:system|model)-card$",
            ),
            Surface(
                key="anthropic-rsp", method="html-index", kind="policy",
                doc_type="framework_policy",
                urls=["https://www.anthropic.com/rsp-updates"],
                include_re=r"\.pdf",
                exclude_text_re=r"(?i)redline",
                text_slug_re=r"(?i)version\s+(\d+)\.(\d+)",
                text_slug_template="rsp-v{0}-{1}",
                title_prefix="Anthropic's Responsible Scaling Policy",
            ),
        ],
    ),
    "openai_cards": ChannelWatch(
        org_slug="openai",
        surfaces=[
            Surface(
                key="openai-systemcards-ds", method="html-index", doc_type="system_card",
                urls=["https://deploymentsafety.openai.com/"],
                include_re=r"^https://deploymentsafety\.openai\.com/[a-z0-9][a-z0-9-]*$",
                policy_re=r"(?i)preparedness|framework",
            ),
            Surface(
                key="openai-index", method="sitemap", doc_type="system_card",
                urls=["https://openai.com/sitemap.xml"],
                include_re=(
                    r"openai\.com/index/[a-z0-9-]*"
                    r"(?:system-card|model-card|preparedness-framework)[a-z0-9-]*/?$"
                ),
                policy_re=r"(?i)preparedness",
            ),
        ],
    ),
    "deepmind_cards": ChannelWatch(
        org_slug="google-deepmind",
        surfaces=[
            Surface(
                key="deepmind-modelcards", method="html-index", doc_type="model_card",
                urls=["https://deepmind.google/models/model-cards/"],
                include_re=r"deepmind-media/.*\.pdf",
                exclude_re=r"#page=",
                policy_re=r"(?i)frontier.?safety|fsf",
            ),
            Surface(
                key="deepmind-modelcard-pages", method="html-index", doc_type="model_card",
                urls=["https://deepmind.google/models/model-cards/"],
                include_re=r"deepmind\.google/models/(?:model-cards|fsf-reports)/[a-z0-9-]+/?$",
                policy_re=r"(?i)frontier.?safety|fsf",
            ),
            Surface(
                key="deepmind-fsf", method="sitemap", kind="policy",
                doc_type="framework_policy",
                urls=["https://deepmind.google/sitemap.xml"],
                include_re=r"deepmind\.google/blog/[a-z0-9-]*frontier-safety-framework[a-z0-9-]*/?$",
            ),
        ],
    ),
    "meta_cards": ChannelWatch(
        org_slug="meta",
        surfaces=[
            Surface(
                key="meta-modelcards", method="github-tree", doc_type="model_card",
                github=[
                    {"owner": "meta-llama", "repo": "llama-models",
                     "path_re": r"MODEL_CARD[A-Z_]*\.md$"},
                    {"owner": "meta-llama", "repo": "PurpleLlama",
                     "path_re": r"MODEL_CARD[A-Z_]*\.md$"},
                ],
            ),
            Surface(
                key="meta-frontier", method="pinned-pdf", kind="policy",
                doc_type="framework_policy",
                urls=["https://ai.meta.com/static-resource/meta-frontier-ai-framework/"],
                pinned_id="policy:meta-frontier-ai-framework",
                title_prefix="Meta Frontier AI Framework",
            ),
        ],
    ),
    "xai_cards": ChannelWatch(
        org_slug="xai",
        surfaces=[
            Surface(
                key="xai-docs", method="html-index", doc_type="system_card",
                urls=["https://x.ai/safety", "https://x.ai/news"],
                include_re=r"(?:[\w-]+\.)?x\.ai/[^#\s]+\.pdf",
                policy_re=r"(?i)framework|risk-management|rmf",
            ),
            # Delisted-but-live PDFs. data.x.ai has no directory listing; these are
            # curated. Do not pin anything still on /safety (would double-identity).
            Surface(
                key="xai-archive-cards", method="pinned-pdf", doc_type="system_card",
                pins=[
                    {"url": "https://data.x.ai/2025-11-17-grok-4-1-model-card.pdf",
                     "pinned_id": "systemcard:xai-grok-4-1",
                     "title": "Grok 4.1 Model Card", "date": "2025-11-17"},
                    {"url": "https://data.x.ai/2026-04-07-grok-4-20-model-card.pdf",
                     "pinned_id": "systemcard:xai-grok-4-20",
                     "title": "Grok 4.20 Model Card", "date": "2026-04-07"},
                ],
            ),
            Surface(
                key="xai-archive-policies", method="pinned-pdf", kind="policy",
                doc_type="framework_policy",
                pins=[
                    {"url": "https://data.x.ai/2025.02.20-RMF-Draft.pdf",
                     "pinned_id": "policy:xai-rmf-draft-2025",
                     "title": "Draft Risk Management Framework", "date": "2025-02-20"},
                    {"url": "https://data.x.ai/2025-08-20-xai-risk-management-framework.pdf",
                     "pinned_id": "policy:xai-rmf-2025",
                     "title": "Risk Management Framework", "date": "2025-08-20"},
                    {"url": "https://data.x.ai/2025-12-31-xai-frontier-artificial-intelligence-framework.pdf",
                     "pinned_id": "policy:xai-frontier-ai-framework-2025",
                     "title": "Frontier AI Framework", "date": "2025-12-31"},
                ],
            ),
        ],
    ),
    "deepseek_cards": ChannelWatch(
        org_slug="deepseek",
        surfaces=[
            Surface(
                key="deepseek-github", method="github-org", doc_type="system_card",
                github_org="deepseek-ai", path_re=r"(?i)^[^/]*\.pdf$", max_repos=25,
            ),
        ],
    ),
    "qwen_cards": ChannelWatch(
        org_slug="alibaba",
        surfaces=[
            Surface(
                key="qwen-github", method="github-org", doc_type="system_card",
                github_org="QwenLM", path_re=r"(?i)^[^/]*\.pdf$", max_repos=25,
            ),
        ],
    ),
}


# ─────────────────────────────────────────────────────────────────────────────
# Candidate typing + classification
# ─────────────────────────────────────────────────────────────────────────────
def _classify_kind(spec: Surface, url: str, text: str) -> str:
    """systemcard | policy — policy_re over the raw URL or link text upgrades to policy."""
    if spec.policy_re:
        pr = re.compile(spec.policy_re)
        if pr.search(url) or pr.search(text or ""):
            return "policy"
    return spec.kind


def _doc_type_for(spec: Surface, kind: str) -> str:
    """system_card | model_card | framework_policy by the surface alone. A policy classification always wins."""
    if kind == "policy":
        return "framework_policy"
    return spec.doc_type


#: An item that names itself a model card (``...-model-card.pdf``, "Grok 4.1 Model Card").
_MODEL_CARD_RE = re.compile(r"(?i)model[\s_-]*card")


def _doc_type_of(spec: Surface, kind: str, cand: Candidate) -> str:
    """:func:`_doc_type_for`, except that an item of a ``system_card`` surface whose URL or listing text says "model
    card" is a ``model_card`` (xAI and OpenAI list their model cards beside their system cards)."""
    doc_type = _doc_type_for(spec, kind)
    if doc_type == "system_card" and (_MODEL_CARD_RE.search(cand.url) or _MODEL_CARD_RE.search(cand.text)):
        return "model_card"
    return doc_type


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


#: Card words: a slug title holding one gets no doc word appended (``gpt-oss-model-card`` on a system-card surface is
#: not "... Model Card System Card").
_CARD_WORD_RE = re.compile(r"(?i)\bcard\b")
#: A run of 1-2 digit numbers in a title-cased slug: the dots of a version the slug lost (``Claude Opus 4 8``).
_VERSION_RUN_RE = re.compile(r"\b\d{1,2}(?: \d{1,2})+\b")


def slug_title(slug: str, doc_word: str) -> str:
    """The title a slug gives when the listing gives none: its words title-cased, a run of 1-2 digit numbers joined
    with dots (``claude-opus-4-8-system-card`` → ``Claude Opus 4.8 System Card``; ``llama-guard-3-11b`` keeps
    ``3 11B``) and ``doc_word`` appended unless a word is already "card"."""
    title = _VERSION_RUN_RE.sub(lambda m: m.group(0).replace(" ", "."), slug.replace("-", " ").title())
    if not _CARD_WORD_RE.search(title):
        title = f"{title} {doc_word}"
    return title


def _title_hint(cand: Candidate, spec: Surface, doc_type: str) -> str | None:
    """A listing-derived title hint (never a content fetch): the anchor's title block (:attr:`Candidate.title_text`,
    a multi-block anchor without its dateline and description), else the anchor text when meaningful, else
    :func:`slug_title`; ``title_prefix`` is prepended when set."""
    doc_word = {"framework_policy": "Policy", "model_card": "Model Card"}.get(doc_type, "System Card")
    if cand.text and not GENERIC_TEXT_RE.match(cand.text):
        title = cand.title_text or cand.text
    else:
        title = slug_title(cand.slug, doc_word)
    if spec.title_prefix and spec.title_prefix not in title:
        title = f"{spec.title_prefix} — {title}"
    return title or None


def _legacy_title_hint(cand: Candidate, spec: Surface, doc_type: str) -> str | None:
    """The title hint before the 2026-10-30 title fixes (D759): the anchor text run together, or the slug title-cased
    without dots and with the doc word appended unless it ends the title. :func:`carried` keeps a known source's
    stored title when it is this (a title change re-triages the document)."""
    doc_word = {"framework_policy": "Policy", "model_card": "Model Card"}.get(doc_type, "System Card")
    if cand.text and not GENERIC_TEXT_RE.match(cand.text):
        title = cand.text
    else:
        title = cand.slug.replace("-", " ").title()
        if not title.lower().endswith(doc_word.lower()):
            title = f"{title} {doc_word}"  # v3 appended it to "...-system-card" slugs too
    if spec.title_prefix and spec.title_prefix not in title:
        title = f"{spec.title_prefix} — {title}"
    return title or None


def carried(c: Collector, channel: str, external_id: str, cand: Candidate, title: str | None,
            published: str | None, doc_type: str, *, legacy_title: str | None = None,
            legacy_doc_type: str | None = None) -> tuple[str | None, str | None, str]:
    """``(title, published, doc_type)`` for an already-known source, filling what this listing lacks from its prior
    row: a surface that carries less (an archive index without dates, an anchor without text) must not overwrite
    what an earlier sighting established (v3 wrote listing metadata only for a NEW source). And a stored title or
    doc_type that is what the code before D759 derived from this same listing (``legacy_title``,
    ``legacy_doc_type``) is kept: the fixes apply to new sources only, since a changed field changes the document's
    ``fields_digest`` and sends it back through triage (the release corrects the stored ones at export instead)."""
    prior = c.prior(channel, external_id)
    if prior is None or prior["state"] != "observed":
        return title, published, doc_type
    md = prior["metadata"]
    if published is None and md["published_at"] is not None:
        published = md["published_at"].date().isoformat()
    if md["title"] and not (cand.text and not GENERIC_TEXT_RE.match(cand.text)):
        title = md["title"]  # this listing had no usable anchor text: keep the earlier title
    elif md["title"] and legacy_title is not None and md["title"] == legacy_title:
        title = md["title"]  # stored before D759 from this same anchor text: keep it
    if legacy_doc_type is not None and md.get("doc_type") == legacy_doc_type:
        doc_type = legacy_doc_type
    return title, published, doc_type


def _listing_fetcher() -> TransportFetcher | HttpFetcher | ReplayFetcher:
    """The fetcher for one surface: v3's replay seam when ``STACK_CARDS_FIXTURES`` is set, else a
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


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes") if value is not None else False


def _github_window(c: Collector, channel: str, spec: Surface,
                   scan: SurfaceScan) -> tuple[dt.datetime, dt.datetime]:
    """``[prior watermark, new watermark]`` for a github-org surface; a first ``full`` sweep starts at
    its oldest repo (module docstring)."""
    ts = lambda v: dt.datetime.strptime(v, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.UTC)
    prior = c.watermark(channel, spec.key)
    new = ts(scan.watermark) if scan.watermark else None
    oldest = ts(scan.note["oldest"]) if scan.note.get("oldest") else None
    start = prior or oldest or new or c.now
    return start, max(start, new or start)


def run(ctx: StageContext) -> StageResult:
    global _T
    from stack.config import load

    c = Collector(ctx)
    config = load()
    limit = c.option("max_urls", 0, env=_ENV_LIMIT, cast=int) or 0
    full = _truthy(c.option("full", False, env=_ENV_FULL))
    only = _id_list(c.option("only", None, env=_ENV_ONLY))

    # Config-driven: every kind=watch channel that has a ported WATCH_SPECS entry.
    channel_ids = [ch.id for ch in config.channels.channels
                   if ch.kind == "watch" and ch.id in WATCH_SPECS and (not only or ch.id in only)]
    # Scale control selects SURFACES (the unit a watch plans in); the full count is reported.
    all_surfaces = [(cid, sp) for cid in channel_ids for sp in WATCH_SPECS[cid].surfaces]
    selected, sampling = select(all_surfaces, lambda cs: f"{cs[0]}:{cs[1].key}",
                                env=_scale_env(c))

    _T = c.transport(PACING, sleep=lambda s: _sleep(s))
    tally = {"surfaces_complete": 0, "surfaces_truncated": 0, "surfaces_error": 0,
             "items_seen": 0, "items_new": 0, "items_capped": 0, "surfaces_capped": 0}
    minted = 0
    observed: set[tuple[str, str]] = set()
    relist = c.relist_after()
    try:
        with c.halts():
            for i, (channel, spec) in enumerate(selected):
                c.progress(done=i, total=len(selected))
                # listed `ok` within relist_after_h (D226); a github-org history sweep the cap stopped
                # (an `ok` window not exhausted, D555) is asked again at once
                last = c.last_attempt(channel, spec.key) if spec.method == "github-org" else None
                banked = last is not None and last["status"] == "ok" and not last["is_exhausted"]
                if not c.due(channel, spec.key, relist_after=None if banked else relist):
                    continue
                chan = c.channel(channel)
                watch = WATCH_SPECS[channel]
                fetcher = _listing_fetcher()
                # listing types the fetcher as its two classes, not a protocol (reported)
                scan = scan_surface(spec, channel, c, fetcher, full=full)  # type: ignore[arg-type]
                if spec.method == "github-org":
                    start, end = _github_window(c, channel, spec, scan)
                else:
                    start = end = c.now  # a listing is a snapshot (module docstring)
                capped = False
                with c.window(channel, spec.key, start, end) as w:
                    for cand in scan.candidates:
                        # Identity from the LISTING only (no item fetch).
                        if spec.method == "pinned-pdf":
                            # slug is already external_id_of(pinned_id), per candidate.
                            ext = cand.slug
                            kind = spec.kind
                        else:
                            kind = _classify_kind(spec, cand.url, cand.text)
                            ext = external_id_of(make_doc_id(kind, watch.org_slug, cand.slug))
                        doc_type = _doc_type_of(spec, kind, cand)
                        w.seen += 1
                        new = c.prior(channel, ext) is None and (channel, ext) not in observed
                        if new and limit and minted >= limit:
                            capped = True
                            tally["items_capped"] += 1
                            continue  # not observed; a capped surface is partial
                        url = fetch_url(cand.url) or cand.url  # fetchable as listed; the key is its url_key
                        legacy_doc_type = _doc_type_for(spec, kind)
                        title = _title_hint(cand, spec, doc_type)
                        published = cand.date_hint if _ISO_DAY.match(cand.date_hint or "") else None
                        title, published, doc_type = carried(
                            c, channel, ext, cand, title, published, doc_type,
                            legacy_title=_legacy_title_hint(cand, spec, legacy_doc_type),
                            legacy_doc_type=legacy_doc_type)
                        record = {"url": url, "title": title, "published": published,
                                  "doc_type": doc_type}
                        c.observe(channel, ext, record,
                                        keys=url_keys(chan.channel_group, channel, ext, url),
                                        canonical_url=url, skip_unchanged=True,
                                        metadata={"title": title, "published_at": published,
                                                  "doc_type": doc_type},
                                        query=spec.key, label=channel)  # matched_by (D228)
                        observed.add((channel, ext))
                        if new:
                            w.kept += 1
                            minted += 1
                    exhausted = close_window(w, scan, fetcher, capped=capped)
                    if exhausted == "complete" and scan.note.get("repos_capped"):
                        # D555: a banked prefix: `ok` (advances the watermark) but not exhausted, so
                        # the next run or pass resumes it at once
                        w.outcome = ("ok", None, False, None, None)
                        tally["surfaces_capped"] += 1
                tally[f"surfaces_{exhausted}"] += 1
                tally["items_seen"] += w.seen
                tally["items_new"] += w.kept
            c.progress(done=len(selected), total=len(selected))
    finally:
        _T = None
    c.counts.update(tally, surfaces_planned=int(sampling["planned"]),
                    surfaces_selected=int(sampling["selected"]), new_cap=limit)
    return c.finish(scope_complete=tally["surfaces_capped"] == 0)  # D555: a capped history sweep
