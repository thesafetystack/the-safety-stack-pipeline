"""Lane ``http`` (§3.1, §5.6 "Pages, Wayback"; D11, D22, D25, D26): one role's network episode for
the channel groups ``url`` and ``forum`` (:func:`workflows.acquire.lanes.lane_for`).

ADAPTed in place from v3's ``fetch.http`` (``workflows/fetch/http/main.py``): the episode logic
(URL choice, conditional headers, the live → headless → Wayback ladder, landing-page → PDF
resolution, media sniffing) and its pure helpers are kept. Dropped with the database: due
selection (the skeleton's scope), ``fetch_log``/``fetch_state`` writes (the skeleton's ``ROLE``
accounting, :mod:`stack.fetchcycle` ``role_*``), the crash-window idempotency check (a run that
dies publishes nothing, §7.9), the DB preloads, the worker pool and its host scheduler (the
skeleton calls lanes one role at a time), and the automatic content dedup (equal raw digests
across documents are the skeleton's ``same_raw_doc_ids[]``, C29: listed for a human merge, never
merged).

**Episode** (:meth:`HttpLane.fetch`). The URL is the member's ``canonical_url``; a PDF is expected
when it ends ``.pdf`` or the channel is a ``*_cards`` watcher (those serve a landing page that
links the PDF). The URL is refused before any request when its scheme is not http(s) or its host
is interior (:func:`stack.http.check_url_safety`, the one SSRF check, D680: redirect hops, a
landing page's PDF link and the browser's requests are not checked). Then the ladder:

* **live**: one GET under the channel's politeness (``channels.yaml`` ``rate_rps``,
  ``respect_robots``) with the typed retry loop (:class:`stack.http.PoliteFetcher`: up to 3
  retries with 2/4/8 s backoff on 5xx, timeouts and connection errors; a 429 halves the host's
  rate for the run and retries, no sooner than a stated ``Retry-After``; a Gate cooldown answer is
  waited out without spending a retry; past 120 s of such waits the 429 ends the rung at once,
  D588). An unasked gzip/deflate body is decoded (:func:`stack.http.decode_content`). Conditional headers (``If-None-Match``/``If-Modified-Since``)
  replay the role's ``etag``/``last_modified`` only when they belong to this URL
  (``validator_url``) and a live raw snapshot of the role is held (v3's "finding 8": a 304 must
  never assert "unchanged" about bytes we do not hold);
* **headless** (option ``headless``, default off): on a 403/451 or a transient failure, a
  Playwright render of the page (``via = 'headless'``) by the shared rung :mod:`stack.browser`
  (D590, Dan Elton's v3 module: bundled Chromium, else the system Chrome; at most 2 renders at
  once). A render failure degrades to the next rung; no browser at all is warned once per process
  and counted per role (``http.headless.unavailable``), so a dead rung shows in the run record;
* **wayback** (option ``wayback``, default on): the closest Internet Archive capture
  (``via = 'wayback'``, ``as_of`` = capture time), **only while the role holds no live or headless
  snapshot** ("archived-only: newest capture", §5.6), and then on ``blocked`` (403, 451) or
  ``dead`` (404, 410, other 4xx), or on ``error`` (5xx, 429, timeout, connection, DNS, after the
  retries) only while the role holds no raw bytes at all (D589, Dan Elton's v3
  rule: a transient failure says nothing new about a page whose archived copy we hold);
  ``paywalled`` (401, 402) never. The availability API names the capture and its **original
  bytes** are fetched (:data:`WAYBACK_RAW`, ``web.archive.org/web/<ts>id_/<url>``), never the
  replay page (toolbar, links rewritten to archive.org). Once a live body is held, a live
  failure is reported as itself (a 404 is ``dead`` and the pointer stays on the last good
  snapshot; a 5xx backs off) and archive.org is not asked. A capture never outranks a live
  candidate anyway (§5.5 orders ``via <> 'wayback'`` first), so a later live success becomes
  current.

A robots.txt Disallow is terminal (the site owner's wish; no headless, no Wayback).

**A pinned capture.** A ``canonical_url`` that names one capture's original bytes
(``web.archive.org/web/<timestamp>id_/<url>``, :func:`stack.urls.wayback_capture`; ``collect.manual`` keeps a
hand-added source's pin as its fetch URL) is fetched as written and its body is that capture: ``via = 'wayback'``,
``as_of`` the capture time (the archive's final URL, else the pin's), the archived ``Content-Type``, no validators,
no landing-page resolution and no availability lookup on a failure (the pin is the one copy asked for).

=========================================  ===============================================
episode                                    :class:`~workflows.acquire.lanes.FetchOutcome`
=========================================  ===============================================
200 (any rung)                             ``ok``, one raw snapshot (equal digest to the
                                           newest held → the skeleton mints nothing,
                                           ``last_outcome = 'unchanged'``)
304 Not Modified                           ``ok``, no snapshot, ``last_outcome = 'not_modified'``
no ``canonical_url``                       ``failed_permanent[no_fetchable_url]``
bad scheme / interior host                 ``failed_permanent[dead]`` / ``[blocked]``
404, 410, other 4xx                        ``failed_permanent[dead]``
HTTP 403 (live rung, nothing rescued)      ``failed_transient[forbidden]``; ``failed_permanent
                                           [blocked]`` on the attempt that reaches the cap
                                           (:func:`forbidden`)
451, robots Disallow, unsafe redirect      ``failed_permanent[blocked]``
401, 402                                   ``failed_permanent[paywalled]``
body over ``max_bytes``                    ``failed_permanent[too_large]``
5xx after the retries                      ``failed_transient[http_5xx]``
429 after the retries or the D588 cap      ``failed_transient[rate_limit]`` (``Retry-After``
                                           as ``retry_after_hint``); for this role only: a
                                           provider-level halt is the OA/arXiv lanes' business
timeout / connection, DNS, other transport ``failed_transient[timeout]`` / ``[network]``
=========================================  ===============================================

The skeleton turns these into ``attempts``, backoff, the cap of 4 and the weekly recheck of
``ok`` roles of channel group ``url`` (D22, D25).

**Landing page → PDF.** HTML where a PDF was expected: the page's ``citation_pdf_url``/``og:pdf``
declarations first, then ``.pdf`` hrefs scored by :func:`id_tokens` of the member's
``key_value`` (the v3 ``external_id``) and publication date; the first pick that is a real PDF
replaces the body (the snapshot's ``url``/``final_url`` are the PDF's; the role's validators stay
the page's, as in v3). Unresolved, the page itself is the snapshot.

**Snapshot** (one ``SNAPSHOT`` dict, the skeleton fills ids and conversion fields): ``digest``
(the bytes put into ``blobs/``), ``bytes``, ``media_type`` (:func:`stack.mediatype.media_type_for`:
magic bytes first, the live response's ``Content-Type``, or a capture's archived one, only as a
tie-breaker), ``url``, ``final_url`` (a capture's: the archive's ``id_`` URL, which names the
capture time), ``http_status``, ``via``, ``etag``/``last_modified`` (live only), ``as_of`` (archive
capture time, else the live ``Last-Modified``, else the skeleton's ``captured_at``). The role's
``etag``/``last_modified``/``validator_url`` are set from a live body and cleared by a headless or
archived one, so a later live request never replays validators that describe bytes we do not
hold (and conditional headers need a held live snapshot anyway, :func:`conditional_headers`).

**Transport** (D26). Every request (page, robots.txt, PDF pick, both Wayback calls) goes through
one run-scoped :class:`HttpSession` (a :class:`stack.http.PoliteFetcher` with its
:class:`~stack.http.HostPacer` and :class:`~stack.http.RobotsCache`, kept in
``env.lane_state['http']``) over the getter: ``env.transport`` when injected (an
:class:`stack.http.HttpGetter`, or an object exposing one as ``.get`` such as
``tests/fakes/http.FakeHttp``; then no DNS lookup is made either), else
:func:`stack.http.urllib_get`, which opens through :func:`stack.provider_transport.throttled_open`
(the cross-process ``Gate`` for provider hosts; a cross-origin redirect drops credential headers).
The headless rung is the one exception: Chromium makes its own requests (unpaced, unretried, no
robots check, no Gate). Waits (pacing, retry
backoff) use ``env.sleep``.

**Options** (the stage line's ``options``): ``wayback`` (bool, default true), ``headless`` (bool,
default false), ``http_timeout_s`` (default 30), ``max_bytes`` (default 64 MiB).

**Counts** (``env.counts``): ``http.requests``, ``http.via.<live|headless|wayback>``,
``http.not_modified``, ``http.media.<type>``, ``http.landing_resolved``,
``http.landing_unresolved``, ``http.headless.<rendered|failed|unavailable>`` (each headless try).
"""

from __future__ import annotations

import datetime as dt
import email.utils
import json
import re
import socket
import time
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from typing import Any, NamedTuple

from stack import browser
from stack.conversion.ladder import decode_text
from stack.core.hashes import utf8_safe
from stack.fetchcycle import FETCH_CAP
from stack.http import (
    _ROBOTS_BLOCKED,
    DEFAULT_HEADERS,
    DEFAULT_MAX_BYTES,
    HostPacer,
    HttpGetter,
    HttpResponse,
    PoliteFetcher,
    Resolver,
    RobotsCache,
    check_url_safety,
    looks_like_html,
    looks_like_pdf,
    urllib_get,
)
from stack.mediatype import media_type_for, sniff_kind
from stack.provider_transport import retry_seconds
from stack.urls import wayback_capture
from workflows.acquire.lanes import (
    FetchOutcome,
    FetchRequest,
    LaneEnv,
    channel_entry,
    host_group,
)

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────
PDF_HREF_RE = re.compile(
    r"""["'\(](?P<u>[^"'\(\)\s]+?\.pdf(?:\?[^"'\(\)\s]*)?)["'\)]""", re.I
)
# The Highwire/OpenGraph PDF declaration most publishers emit on landing pages (fetchpdf's
# `_collect_landing_page_candidates`); both attribute orders. Ranked ABOVE href scraping —
# it is the page's own statement of where its PDF lives, and need not end ".pdf".
CITATION_PDF_RE = re.compile(
    r"""<meta[^>]+(?:name|property)=["'](?:citation_pdf_url|og:pdf)["'][^>]+content=["'](?P<a>[^"']+)["']"""
    r"""|<meta[^>]+content=["'](?P<b>[^"']+)["'][^>]+(?:name|property)=["'](?:citation_pdf_url|og:pdf)["']""",
    re.I,
)
# The model-word allowlist that keeps otherwise-alphabetic slug tokens as scoring signals
# (v1 doc_fetch.id_tokens — preserved verbatim so its goldens still pass).
_MODEL_WORDS: frozenset[str] = frozenset(
    {
        "sonnet", "opus", "haiku", "mythos", "fable", "instant", "thinking",
        "codex", "rosalind", "images", "flash", "lite", "audio", "image",
        "omni", "premier", "spark",
    }
)

WAYBACK_API = "http://archive.org/wayback/available"
#: The ORIGINAL bytes of a capture (D589): ``id_`` after the timestamp asks the Wayback Machine for
#: the archived response as the site served it, not the replay page the availability API's ``url``
#: names (that page wraps HTML in the Wayback toolbar and rewrites every link to archive.org).
WAYBACK_RAW = "https://web.archive.org/web/{ts}id_/{url}"
_WAYBACK_RAW_TS = re.compile(r"^https?://web\.archive\.org/web/(\d{14})id_/", re.IGNORECASE)
DEFAULT_TIMEOUT_S = 30.0

#: Ladder outcome → the permanent ``error_class`` it maps to (§2.6); ``error`` is transient.
_PERMANENT = {"dead": "dead", "blocked": "blocked", "paywalled": "paywalled", "too_large": "too_large"}


# ─────────────────────────────────────────────────────────────────────────────
# Pure helpers (v3, kept)
# ─────────────────────────────────────────────────────────────────────────────
def id_tokens(slug: str, date: str = "") -> list[str]:
    """Tokens used to pick the right PDF link off a multi-link landing page (v1 doc_fetch,
    reimplemented). ``slug`` is the member's ``key_value`` (a ``scheme:slug`` form is split on
    ':'). Non-alphabetic slug parts stay; alphabetic parts stay only if they are known model
    words."""
    if ":" in slug:
        slug = slug.split(":", 1)[1]
    toks = [t for t in slug.split("-") if t and not t.isalpha() or t in _MODEL_WORDS]
    date = str(date or "")
    toks += [date, date.replace("-", "."), date.replace("-", "_"), date[:7]]
    m = re.search(r"v(\d+)-(\d+)$", slug)
    if m:
        toks += [
            f"{m.group(1)}.{m.group(2)}",
            f"{m.group(1)}-{m.group(2)}",
            f"v{m.group(1)}.{m.group(2)}",
        ]
    m = re.search(r"v(\d+)$", slug)
    if m:
        toks += [f"v{m.group(1)}.0", f"{m.group(1)}.0"]
    return [t.lower() for t in toks if t]


def citation_pdf_urls(html: str, base_url: str) -> list[str]:
    """Absolute URLs from ``citation_pdf_url``/``og:pdf`` meta declarations, de-duplicated,
    order preserved. The page's own statement of where its PDF lives — tried BEFORE the
    href scrape-and-score, and need not end ``.pdf``."""
    urls: list[str] = []
    for m in CITATION_PDF_RE.finditer(html):
        raw = (m.group("a") or m.group("b") or "").strip()
        u = urllib.parse.urljoin(base_url, raw) if raw else ""
        if u and u not in urls:
            urls.append(u)
    return urls


def pdf_candidates(html: str, base_url: str) -> list[str]:
    """Absolute PDF hrefs scraped from a landing page, de-duplicated, order preserved."""
    cands: list[str] = []
    for m in PDF_HREF_RE.finditer(html):
        u = urllib.parse.urljoin(base_url, m.group("u"))
        if u not in cands:
            cands.append(u)
    return cands


def pick_pdf(cands: list[str], tokens: list[str]) -> tuple[str | None, str]:
    """Pick the best PDF link (v1 scoring): a single candidate wins outright; else score each by
    id-token hits and require a strict, non-zero top score. Returns (url_or_None, reason)."""
    if not cands:
        return None, "no PDF links found on landing page"
    if len(cands) == 1:
        return cands[0], cands[0]
    scored = sorted(((sum(t in c.lower() for t in tokens), c) for c in cands), reverse=True)
    if scored[0][0] > 0 and (len(scored) == 1 or scored[0][0] > scored[1][0]):
        return scored[0][1], scored[0][1]
    return None, "AMBIGUOUS: " + " | ".join(cands[:8])


def choose_url(member: Mapping[str, Any]) -> tuple[str | None, bool]:
    """The content URL of a member and whether a PDF is expected: the ``canonical_url``,
    expecting a PDF only when the URL ends ``.pdf`` or the channel is a lab-card watcher
    (``*_cards``: a landing page that links the PDF). No usable URL: nothing to fetch."""
    url = (member.get("canonical_url") or "").strip() or None
    if url is None:
        return None, False
    expect_pdf = url.lower().endswith(".pdf") or str(member.get("channel") or "").endswith("_cards")
    return url, expect_pdf


def conditional_headers(role: Mapping[str, Any] | None, url: str, *, backed: bool) -> dict[str, str]:
    """The validators to replay for ``url``: the role's ``etag``/``last_modified``, only when they
    belong to this URL (``validator_url`` equal, or absent) and a live raw snapshot of the role is
    held (``backed``). Validators are URL-scoped: landing resolution and the Wayback rung fetch
    other URLs for the same role, and an ETag of one URL says nothing about another."""
    if role is None:
        return {}
    etag, last_mod = role.get("etag"), role.get("last_modified")
    if not (etag or last_mod) or not backed:
        return {}
    prev = role.get("validator_url")
    if prev is not None and str(prev) != url:
        return {}
    h: dict[str, str] = {}
    if etag:
        h["If-None-Match"] = str(etag)
    if last_mod:
        h["If-Modified-Since"] = str(last_mod)
    return h


def http_date(value: str | None) -> dt.datetime | None:
    """An HTTP date (``Last-Modified``) as an aware UTC datetime; ``None`` if absent/unparseable."""
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def _parse_wayback_timestamp(ts: str) -> dt.datetime | None:
    """Wayback ``YYYYMMDDHHMMSS`` → aware UTC datetime; None if unparseable."""
    if len(ts) < 14 or not ts[:14].isdigit():
        return None
    try:
        return dt.datetime(
            int(ts[0:4]), int(ts[4:6]), int(ts[6:8]),
            int(ts[8:10]), int(ts[10:12]), int(ts[12:14]), tzinfo=dt.UTC,
        )
    except ValueError:
        return None


def transient_class(resp: HttpResponse | None) -> str:
    """The transient ``error_class`` of an exhausted retry loop's last response."""
    if resp is None:
        return "network"
    if resp.status == 429:
        return "rate_limit"
    if resp.status is not None and 500 <= resp.status < 600:
        return "http_5xx"
    if resp.error == "timeout":
        return "timeout"
    return "network"


def forbidden(role: Mapping[str, Any] | None, error: str, patch: Mapping[str, Any]) -> FetchOutcome:
    """The outcome of an HTTP 403 (D150). On the wire a bot-block
    (Cloudflare and similar answer 403 to a client they distrust, and lift it later) looks the
    same as a real refusal, and v3 could not tell them apart either: :func:`stack.http.classify`
    maps 403 to ``blocked`` and v3's fetch cycle (``stack/fetchcycle.py`` ``failure_patch``)
    retried every failed outcome with backoff until the cap. So a 403 is
    ``failed_transient[forbidden]`` (the skeleton's backoff) until the attempt that reaches
    :data:`stack.fetchcycle.FETCH_CAP` (``attempts`` counts consecutive failures, D107), which
    records ``failed_permanent[blocked]``. 451 and a robots Disallow stay permanent at once."""
    return until_cap(role, "forbidden", "blocked", error, patch, what="HTTP 403")


def until_cap(role: Mapping[str, Any] | None, transient: str, permanent: str, error: str,
              patch: Mapping[str, Any], *, what: str) -> FetchOutcome:
    """``failed_transient[<transient>]`` until the attempt that reaches
    :data:`stack.fetchcycle.FETCH_CAP`, which records ``failed_permanent[<permanent>]`` (the 403
    rule of :func:`forbidden`; round 5, R4-2, also arXiv's ``atom_unknown`` and a 200 that is not a
    PDF: one such answer is not proof the paper is gone)."""
    attempt = int((role or {}).get("attempts") or 0) + 1
    if attempt >= FETCH_CAP:
        return FetchOutcome("failed_permanent", permanent, f"{error}; {what} at attempt {attempt} of {FETCH_CAP}",
                            role_patch=dict(patch))
    return FetchOutcome("failed_transient", transient, error, role_patch=dict(patch))


# ─────────────────────────────────────────────────────────────────────────────
# The run's HTTP session: pacer, robots, retry loop, the ladder
# ─────────────────────────────────────────────────────────────────────────────
class Ladder(NamedTuple):
    """One ladder run. ``outcome``: ``ok`` (``body`` set), ``unchanged`` (a 304), or a v3 failure
    outcome (``dead``, ``blocked``, ``paywalled``, ``error``). ``last`` is the live rung's last
    response (validators, the failure's status), ``captured`` the Wayback capture time,
    ``capture`` the archive's response with the original bytes (its ``Content-Type`` is the
    archived one; its other headers describe no live URL and are never stored as validators),
    ``headless`` the headless rung's result when it was tried (``rendered``, ``failed``, or
    ``unavailable``: no browser in this process, D590), else None."""

    outcome: str
    body: bytes | None
    via: str
    http_status: int | None
    final_url: str | None
    captured: dt.datetime | None
    last: HttpResponse | None
    history: list[dict[str, Any]]
    capture: HttpResponse | None = None
    headless: str | None = None


def _politeness(channel: str) -> dict[str, Any]:
    return dict(channel_entry(channel).get("politeness") or {})


class HttpSession:
    """One run's HTTP machinery over a getter (module docstring, Transport)."""

    def __init__(self, http_get: HttpGetter, *, sleep: Callable[[float], None],
                 monotonic: Callable[[], float] = time.monotonic, resolver: Resolver | None = None,
                 timeout: float = DEFAULT_TIMEOUT_S, max_bytes: int = DEFAULT_MAX_BYTES,
                 wayback: bool = True, headless: bool = False,
                 browser_render: Callable[[str], str | None] | None = None,
                 politeness: Callable[[str], Mapping[str, Any]] = _politeness) -> None:
        self.resolver = resolver
        self.max_bytes = max_bytes
        self.wayback_enabled = wayback
        self.headless_enabled = headless
        self._browser_render = browser_render or browser.render_html
        self._politeness = politeness
        self.pacer = HostPacer(monotonic=monotonic, sleep=sleep)
        self.robots = RobotsCache(http_get, DEFAULT_HEADERS["User-Agent"], timeout)
        self.http = PoliteFetcher(http_get, pacer=self.pacer, robots=self.robots, timeout=timeout,
                                  max_bytes=max_bytes, sleep=sleep)

    @classmethod
    def of(cls, env: LaneEnv) -> HttpSession:
        """The run's session, built on first use and kept in ``env.lane_state['http']`` (once,
        under the env's lock: every lane thread shares its one pacer)."""

        def make() -> HttpSession:
            transport = env.transport
            if transport is None:
                getter: HttpGetter = urllib_get
                resolver: Resolver | None = socket.getaddrinfo
            else:  # an injected transport defines the world: no real DNS lookup either
                getter, resolver = getattr(transport, "get", transport), None
            opts = env.options
            return cls(getter, sleep=env.sleep, resolver=resolver,
                       timeout=float(opts.get("http_timeout_s") or DEFAULT_TIMEOUT_S),
                       max_bytes=int(opts.get("max_bytes") or DEFAULT_MAX_BYTES),
                       wayback=bool(opts.get("wayback", True)), headless=bool(opts.get("headless", False)))

        session: HttpSession = env.state("http", make)
        return session

    def fetch_url(self, url: str, channel: str | None, *, conditional: Mapping[str, str] | None = None,
                  wait_limits: bool = True) -> tuple[str | None, HttpResponse | None, list[dict[str, Any]]]:
        """One host hit under the channel's politeness (robots when the channel opts in, pacing,
        the typed retry loop, 429 penalty, D588's limited answers; ``wait_limits=False`` returns a
        limited answer at once): :meth:`stack.http.PoliteFetcher.fetch`."""
        pol = self._politeness(channel) if channel else {}
        return self.http.fetch(url, rate_rps=pol.get("rate_rps"),
                               respect_robots=bool(pol.get("respect_robots", False)), conditional=conditional,
                               wait_limits=wait_limits)

    def ladder(self, url: str, channel: str | None, conditional: Mapping[str, str] | None, *,
               archive_ok: bool = True, backed: bool = False) -> Ladder:
        """live → headless → wayback (module docstring). ``archive_ok`` is false once the role
        holds a live or headless body (no Wayback at all, D124); ``backed``: the role holds raw
        bytes of any rung, an archived copy included, and then a transient live failure
        (``error``) does not ask the archive either (D589): a blip says nothing new about the page,
        and the copy held is the newest the archive had when it was taken."""
        outcome, resp, history = self.fetch_url(url, channel, conditional=conditional)
        if outcome is None:  # a 200 or 304
            assert resp is not None
            if resp.status == 304:
                return Ladder("unchanged", None, "live", 304, resp.final_url, None, resp, history)
            pin = _WAYBACK_RAW_TS.match(url) if wayback_capture(url) else None
            if pin is not None:  # a pinned capture (module docstring): the archive's copy, not a live page
                served = _WAYBACK_RAW_TS.match(resp.final_url or "")
                captured = _parse_wayback_timestamp((served or pin).group(1))
                return Ladder("ok", resp.body, "wayback", resp.status, resp.final_url or url, captured, resp, history,
                              resp)
            return Ladder("ok", resp.body, "live", resp.status, resp.final_url, None, resp, history)
        # Robots Disallow is TERMINAL — the site owner's wish; do NOT escalate to browser/Wayback.
        if outcome == _ROBOTS_BLOCKED:
            return Ladder("blocked", None, "live", None, None, None, resp, history)
        headless = None
        if outcome in ("blocked", "error") and self.headless_enabled:
            html, headless = self._safe_browser(url)
            if html is not None:
                return Ladder("ok", html.encode("utf-8"), "headless", 200, url, None, resp, history,
                              headless=headless)
        wayback = outcome in ("blocked", "dead") or (outcome == "error" and not backed)
        if wayback and self.wayback_enabled and archive_ok:
            way = self.wayback(url, history)
            if way is not None:
                capture, captured = way
                return Ladder("ok", capture.body, "wayback", 200, capture.final_url, captured, resp, history,
                              capture, headless)
        return Ladder(outcome, None, "live", resp.status if resp else None, None, None, resp, history,
                      headless=headless)

    def _safe_browser(self, url: str) -> tuple[str | None, str]:
        """The headless rung: ``(html, 'rendered')``, or ``(None, 'failed')`` on any failure (a
        browser failure must never crash the run), or ``(None, 'unavailable')`` when no browser can
        start in this process (:class:`stack.browser.BrowserUnavailable`; warned once there,
        counted per role by the lane, D590). The rendered DOM is a JS string: a lone surrogate in
        it becomes U+FFFD (:func:`~stack.core.hashes.utf8_safe`, D586), so the body encodes as UTF-8."""
        try:
            html = self._browser_render(url)
        except browser.BrowserUnavailable:
            return None, "unavailable"
        except Exception:  # noqa: BLE001 — typed degrade, never crash
            return None, "failed"
        return (utf8_safe(html), "rendered") if html is not None else (None, "failed")

    def wayback(self, url: str, history: list[dict[str, Any]]) -> tuple[HttpResponse, dt.datetime] | None:
        """The closest Wayback capture of ``url`` through the session (paced, retried, D26):
        the availability API names the capture, then its original bytes are fetched
        (:data:`WAYBACK_RAW`, D589), never the replay page. ``(the archive's response, capture
        time)``, or ``None`` when there is none. The capture time is the one in the archive's
        final URL (it redirects ``id_`` to the capture it serves), else the API's timestamp."""
        api = f"{WAYBACK_API}?{urllib.parse.urlencode({'url': url})}"
        outcome, resp, hist = self.fetch_url(api, None)
        history.extend(hist)
        if outcome is not None or resp is None or resp.status != 200 or not resp.body:
            return None
        try:
            snap = (json.loads(resp.body).get("archived_snapshots") or {}).get("closest") or {}
        except (ValueError, AttributeError):
            return None
        ts = str(snap.get("timestamp", ""))
        named = _parse_wayback_timestamp(ts)
        if str(snap.get("status")) != "200" or named is None:
            return None
        outcome, r2, hist = self.fetch_url(WAYBACK_RAW.format(ts=ts[:14], url=url), None)
        history.extend(hist)
        if outcome is not None or r2 is None or r2.status != 200 or not r2.body:
            return None
        served = _WAYBACK_RAW_TS.match(r2.final_url or "")
        return r2, (_parse_wayback_timestamp(served.group(1)) if served else None) or named

    def resolve_landing_pdf(self, base_url: str, html: bytes, channel: str | None, tokens: Sequence[str]
                            ) -> tuple[HttpResponse | None, str, list[str]]:
        """Resolve a landing page to its PDF: a ``citation_pdf_url``/``og:pdf`` declaration first
        (the page's own statement, no scoring), then the best-scoring ``.pdf`` href. Returns
        ``(the PDF response or None, the pick or the reason, the URLs requested)``."""
        text, _codec = decode_text(html)  # the page's charset (stack/conversion/ladder.py), as the converter reads it
        picks: list[str] = citation_pdf_urls(text, base_url)
        scored, why = pick_pdf(pdf_candidates(text, base_url), list(tokens))
        if scored is not None and scored not in picks:
            picks.append(scored)
        if not picks:
            return None, why, []
        reason, tried = why, []
        for pick in picks:
            tried.append(pick)
            outcome, resp, _hist = self.fetch_url(pick, channel)
            if outcome is None and resp is not None and looks_like_pdf(resp.body):
                return resp, pick, tried
            reason = f"picked {pick} but it is not a valid PDF"
        return None, reason, tried


# ─────────────────────────────────────────────────────────────────────────────
# The lane
# ─────────────────────────────────────────────────────────────────────────────
def _tokens(member: Mapping[str, Any], source_id: str) -> list[str]:
    published = member.get("published_at")
    date = published.date().isoformat() if isinstance(published, dt.datetime) else str(published or "")
    return id_tokens(str(member.get("key_value") or source_id), date)


def _failure(lad: Ladder, url: str, env: LaneEnv, tried: list[str],
             role: Mapping[str, Any] | None) -> FetchOutcome:
    last = lad.last
    what = (f"HTTP {last.status}" if last is not None and last.status is not None
            else f"transport {last.error}" if last is not None else lad.outcome)
    error = f"{url}: {what} ({len(lad.history)} request(s))"
    patch = {"tried_urls": tried}
    if lad.outcome == "blocked" and last is not None and last.status == 403:
        return forbidden(role, error, patch)
    if lad.outcome in _PERMANENT:
        return FetchOutcome("failed_permanent", _PERMANENT[lad.outcome], error, role_patch=patch)
    cls = transient_class(last)
    hint = None
    if cls == "rate_limit" and last is not None and "retry-after" in last.headers:
        hint = env.now + dt.timedelta(seconds=retry_seconds(last.headers, last.body))
    return FetchOutcome("failed_transient", cls, error, role_patch=patch, retry_after_hint=hint)


class HttpLane:
    """``LANE``: the episode of one ``url``/``forum`` role (module docstring)."""

    name = "http"

    @staticmethod
    def host_key(request: FetchRequest, env: LaneEnv) -> str:
        """The fetch group (``lanes.fetch_group``): the host of the URL this episode starts at."""
        return host_group(choose_url(request.member)[0], "lane:http")

    def fetch(self, request: FetchRequest, env: LaneEnv) -> FetchOutcome:
        session = HttpSession.of(env)
        url, expect_pdf = choose_url(request.member)
        if url is None:
            return FetchOutcome("failed_permanent", "no_fetchable_url",
                                f"member {request.source_id} has no canonical_url")
        unsafe = check_url_safety(url, resolver=session.resolver)
        if unsafe is not None:
            return FetchOutcome("failed_permanent", unsafe, f"{url}: refused (url_unsafe:{unsafe})",
                                role_patch={"tried_urls": [url]})
        raws = [s for s in request.snapshots if s.get("kind") == "raw"]
        held_live = any(s.get("via") in ("live", "headless") for s in raws)
        conditional = conditional_headers(request.role, url, backed=any(s.get("via") == "live" for s in raws))
        pinned = wayback_capture(url) is not None  # a pinned capture: no availability lookup (module docstring)
        lad = session.ladder(url, request.channel, conditional, archive_ok=not held_live and not pinned,
                             backed=bool(raws))
        env.count("http.requests", len(lad.history))
        if lad.headless is not None:
            env.count(f"http.headless.{lad.headless}")
        tried = [url]
        if lad.outcome == "unchanged":  # 304: validators refreshed, nothing minted
            env.count("http.not_modified")
            patch: dict[str, Any] = {"tried_urls": tried, "validator_url": url, "last_outcome": "not_modified"}
            headers = lad.last.headers if lad.last is not None else {}
            for key, header in (("etag", "etag"), ("last_modified", "last-modified")):
                if headers.get(header):
                    patch[key] = headers[header]
            return FetchOutcome("ok", role_patch=patch)
        if lad.outcome != "ok" or lad.body is None:
            return _failure(lad, url, env, tried, request.role)

        body, via = lad.body, lad.via
        live = via == "live" and lad.last is not None
        page_headers: Mapping[str, str] = lad.last.headers if live and lad.last is not None else {}
        body_headers, declared = page_headers, page_headers.get("content-type")
        if via == "wayback" and lad.capture is not None:  # the archived Content-Type, nothing else
            declared = lad.capture.headers.get("content-type")
        snap_url, final_url = url, lad.final_url  # a capture's final_url is its archive.org id_ URL
        if expect_pdf and live and looks_like_html(body) and not looks_like_pdf(body):
            pdf, why, picks = session.resolve_landing_pdf(url, body, request.channel,
                                                          _tokens(request.member, request.source_id))
            tried += picks
            env.count("http.requests", len(picks))
            if pdf is not None:
                env.count("http.landing_resolved")
                body, body_headers, declared = pdf.body, pdf.headers, None
                snap_url, final_url = why, pdf.final_url
            else:
                env.count("http.landing_unresolved")
        if len(body) > session.max_bytes:
            return FetchOutcome("failed_permanent", "too_large",
                                f"{snap_url}: {len(body)} bytes over the {session.max_bytes}-byte cap",
                                role_patch={"tried_urls": tried})
        digest = env.blobs.put(body)
        media_type = media_type_for(body, declared)
        env.count(f"http.via.{via}")
        env.count(f"http.media.{media_type}")
        as_of = lad.captured if via == "wayback" else http_date(body_headers.get("last-modified"))
        snap = {"digest": digest, "bytes": len(body), "media_type": media_type, "url": snap_url,
                "final_url": final_url, "http_status": lad.http_status or 200, "via": via,
                "etag": body_headers.get("etag"), "last_modified": body_headers.get("last-modified"),
                "as_of": as_of}
        # The role's validators are the page's (live) and describe what is now held; a headless
        # or archived body has none, and older ones no longer describe the newest body.
        patch = {"tried_urls": tried, "validator_url": url if live else None,
                 "etag": page_headers.get("etag") if live else None,
                 "last_modified": page_headers.get("last-modified") if live else None}
        return FetchOutcome("ok", snapshots=[snap], role_patch=patch)


LANE = HttpLane()

__all__ = [
    "CITATION_PDF_RE", "LANE", "PDF_HREF_RE", "WAYBACK_API", "WAYBACK_RAW", "HttpLane", "HttpSession",
    "Ladder",
    "choose_url", "citation_pdf_urls", "conditional_headers", "forbidden", "http_date", "id_tokens",
    "looks_like_html", "looks_like_pdf", "media_type_for", "pdf_candidates", "pick_pdf", "sniff_kind",
    "transient_class", "until_cap",
]
