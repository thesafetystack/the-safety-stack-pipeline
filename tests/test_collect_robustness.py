"""Collect under adverse conditions — the pre-sweep confidence suite.

The existing collect tests answer "does this work when the upstream behaves?". A full sweep is
6-7 hours against five upstreams that rate-limit us, so the interesting question before spending
it is the other one: **when a page fails mid-sweep, does the receipt still tell the truth?**

Three properties, in descending order of how much damage getting them wrong does:

I. **No false completeness.** ``coverage.exhausted='complete'`` advances a watermark past the
   window just covered. If it is banked for a query whose pages did not all arrive, the missed
   work falls below the watermark and is never collected again — the failure is silent and
   permanent, unlike every other collect error, which is merely a retry. ``writer.write_coverage``
   checks the value is in the enum; only these tests check it is TRUE.

II. **A bad body is never mistaken for an empty one, and never invents identity.** The listing
    scanners are hand-rolled over stdlib parsers and are fed whatever the open web serves. This is
    invariant I again, reached by the other route: ``collect.blogs`` / ``collect.cards`` write a
    coverage receipt per SURFACE, so a block page read as "this index is empty" is the same
    permanent watermark lie an API sweep can tell. The second half is identity — a garbage
    ``external_id`` is a permanently mis-keyed source, un-mergeable with the real document later.

    Deliberately NOT exhaustive. An earlier draft ran a 12-input x 3-method x 4-parser matrix; the
    inputs collapsed onto three distinct branches and it found nothing in the other 60-odd
    combinations, so it is now three representative inputs, chosen one per branch (see
    ``NOT_A_LISTING``).

III. **One run at a time.** The advisory lock is what stops two shells sweeping the same channel
     into duplicate work. It is untestable on PGlite (single-connection), so it runs only on a
     real ``STACK_TEST_DB_URL`` (WEAKSPOTS W38).

Fixtures and wrappers live in ``tests/collect_faults.py``.
"""

from __future__ import annotations

import importlib.util
import pathlib
from typing import Any

import pytest

from stack.collect.listing import FetchResult, Surface, scan_surface


class _CannedFetcher:
    """Serves one canned body for every URL — the fetcher seam ``scan_surface`` already takes,
    so the scanners run exactly as they do in a sweep."""

    def __init__(self, text: str, status: int = 200) -> None:
        self.text = text
        self.status = status

    def get(self, url: str, *, headers: Any = None, params: Any = None) -> FetchResult:
        return FetchResult(status=self.status, text=self.text)

from collect_faults import (  # tests/ is on sys.path (pytest prepend import mode)
    MODES,
    FaultyCall,
    af_page,
    atom_feed,
    forum_page,
    openalex_pages,
    s2_pages,
)

REPO = pathlib.Path(__file__).resolve().parent.parent
WORKFLOWS_DIR = REPO / "workflows"


def _load_wf(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(
        f"_robust_{name}", WORKFLOWS_DIR / "collect" / name / "main.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


oa = _load_wf("openalex")
s2 = _load_wf("semantic_scholar")
arxiv = _load_wf("arxiv_sweep")
forums = _load_wf("forums")
afg = _load_wf("af_graphql")
lwg = _load_wf("lw_graphql")

_WORK = {"id": "https://openalex.org/W1", "doi": "https://doi.org/10.1/a", "title": "T",
         "type": "article", "publication_date": "2024-01-02"}
_PAPER = {"paperId": "p1", "title": "T", "externalIds": {"DOI": "10.1/a"},
          "publicationDate": "2024-01-02"}
_HIT = {"_id": "h1", "title": "T", "postedAt": "2024-01-02T00:00:00Z", "baseScore": 99}
_POST = {"_id": "a1", "title": "T", "postedAt": "2024-01-02T00:00:00Z"}


@pytest.fixture(autouse=True)
def _fast_and_small(monkeypatch: Any) -> None:
    """Strip the pacing, and shrink every page size to 2.

    The delays (arXiv's 3s ToS pause, the 2/8/20s retry ladders) are behaviour under test
    elsewhere; left real they would put this file in the minutes.

    The page sizes matter more subtly. Each loop decides it is finished when a SHORT page comes
    back — ``len(batch) < page`` means the upstream had no more to give. At the real sizes (100
    for arXiv and LessWrong, 200 for AF) a stub would have to return 100 identical rows just to
    earn a second request, and getting that wrong yields ``complete`` on page one: a test that
    passes without ever reaching the injected fault. Two-row pages make "there is a page 2"
    explicit and cheap."""
    for mod in (oa, s2, arxiv, forums, afg):
        if hasattr(mod, "_sleep"):
            monkeypatch.setattr(mod, "_sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(arxiv, "ARXIV_PAGE_SIZE", 2)
    monkeypatch.setattr(forums, "LW_PAGE_SIZE", 2)
    monkeypatch.setattr(afg, "PAGE_SIZE", 2)
    monkeypatch.setattr(lwg, "_sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(lwg, "PAGE_SIZE", 2)


# ─────────────────────────────────────────────────────────────────────────────
# I. No false completeness
#
# Each family gets: a HEALTHY baseline that must reach `complete` (otherwise the fault case
# proves nothing), then the same plan with the transport broken from page 2 — mid-sweep, after
# real results are already in hand, which is precisely when a loop is tempted to call it a day.
# ─────────────────────────────────────────────────────────────────────────────
def _oa_fetch(monkeypatch: Any, pages: list[list[dict[str, Any]]], **fault: Any) -> tuple[Any, str]:
    sp = openalex_pages(pages)
    call: Any = sp.json_page
    if fault:
        call = FaultyCall(sp.json_page, kind="json", **fault)
    monkeypatch.setattr(oa, "http_get_json", call)
    return oa.fetch_group("f", 1000)


def _s2_fetch(monkeypatch: Any, pages: list[list[dict[str, Any]]], **fault: Any) -> tuple[Any, str]:
    sp = s2_pages(pages)
    call: Any = sp.json_page
    if fault:
        call = FaultyCall(sp.json_page, kind="json", **fault)
    monkeypatch.setattr(s2, "http_get_json", call)
    return s2.fetch_group("q", None, 1000, {})


def _arxiv_fetch(monkeypatch: Any, feeds: list[str], **fault: Any) -> tuple[Any, str]:
    state = {"i": 0}

    def serve(_url: str, _params: dict[str, Any], **_kw: Any) -> tuple[int, str]:
        i = state["i"]
        state["i"] = i + 1
        return 200, (feeds[i] if i < len(feeds) else atom_feed([], 0))

    call: Any = serve
    if fault:
        call = FaultyCall(serve, kind="text", **fault)
    monkeypatch.setattr(arxiv, "http_get", call)
    return arxiv.fetch_author("au:x", "20200101", 1000)


def _forums_fetch(monkeypatch: Any, pages: list[list[dict[str, Any]]], **fault: Any) -> tuple[Any, str]:
    state = {"i": 0}

    def serve(_url: str, _payload: Any, **_kw: Any) -> tuple[int, Any]:
        i = state["i"]
        state["i"] = i + 1
        # nbPages is what the forum loop paginates on; without it page 0 is always the last page.
        return forum_page(pages[i] if i < len(pages) else [], nb_pages=len(pages))

    call: Any = serve
    if fault:
        call = FaultyCall(serve, kind="json", **fault)
    monkeypatch.setattr(forums, "http_post", call)
    return forums.fetch_forum("https://lw.test", "q", None, 1000)


def _af_fetch(monkeypatch: Any, pages: list[list[dict[str, Any]]], **fault: Any) -> tuple[Any, str]:
    state = {"i": 0}

    def serve(_url: str, _payload: Any, **_kw: Any) -> tuple[int, Any]:
        i = state["i"]
        state["i"] = i + 1
        return af_page(pages[i] if i < len(pages) else [])

    call: Any = serve
    if fault:
        call = FaultyCall(serve, kind="json", **fault)
    monkeypatch.setattr(afg, "http_post", call)
    return afg.fetch_af("https://af.test", "2020-01-01", 1000)


def _lw_fetch(monkeypatch: Any, pages: list[list[dict[str, Any]]], **fault: Any) -> tuple[Any, str]:
    state = {"i": 0}

    def serve(_url: str, _payload: Any, **_kw: Any) -> tuple[int, Any]:
        i = state["i"]
        state["i"] = i + 1
        return af_page(pages[i] if i < len(pages) else [])  # same ForumMagnum response shape

    call: Any = serve
    if fault:
        call = FaultyCall(serve, kind="json", **fault)
    monkeypatch.setattr(lwg, "http_post", call)
    return lwg.fetch_tagged("https://lw.test", lwg.AI_TAG_ID, "2020-01-01", 1000)


FAMILIES = {
    "openalex": (_oa_fetch, [[_WORK, _WORK], [_WORK]]),
    "semantic_scholar": (_s2_fetch, [[_PAPER, _PAPER], [_PAPER]]),
    "arxiv": (_arxiv_fetch, [
        atom_feed([{"id": "2401.00001"}, {"id": "2401.00002"}], 3),
        atom_feed([{"id": "2401.00003"}], 3),
    ]),
    "forums": (_forums_fetch, [[_HIT, _HIT], [_HIT]]),
    "af_graphql": (_af_fetch, [[_POST, _POST], [_POST]]),
    "lw_graphql": (_lw_fetch, [[_POST, _POST], [_POST]]),
}


@pytest.mark.parametrize("family", sorted(FAMILIES))
def test_healthy_multipage_sweep_reaches_complete(family: str, monkeypatch: Any) -> None:
    """The baseline. If a family cannot reach ``complete`` when every page arrives, its fault
    case below is vacuous — so this test is what gives the next one its meaning."""
    fetch, pages = FAMILIES[family]
    items, exhausted = fetch(monkeypatch, pages)
    assert exhausted == "complete", f"{family}: healthy sweep did not complete"
    assert len(items) > 0, f"{family}: healthy sweep returned nothing"


@pytest.mark.parametrize("family", sorted(FAMILIES))
@pytest.mark.parametrize("mode", MODES)
def test_midsweep_failure_never_claims_complete(family: str, mode: str, monkeypatch: Any) -> None:
    """(I) THE invariant. Page 1 lands, page 2 is unavailable for good — the receipt must say
    ``truncated`` or ``error``, never ``complete``.

    ``complete`` here would bank a window covering work the sweep never saw, and the watermark
    would then skip it on every future run. That is the one collect failure with no second
    chance, which is why this is parametrized over every family and every failure mode rather
    than spot-checked on one."""
    fetch, pages = FAMILIES[family]
    _items, exhausted = fetch(monkeypatch, pages, fail_at=2, mode=mode)
    assert exhausted in ("truncated", "error"), (
        f"{family}/{mode}: banked {exhausted!r} after a mid-sweep failure — "
        f"a false 'complete' advances the watermark past work that was never collected"
    )


@pytest.mark.parametrize("family", sorted(FAMILIES))
@pytest.mark.parametrize("mode", MODES)
def test_first_page_failure_claims_nothing(family: str, mode: str, monkeypatch: Any) -> None:
    """The degenerate case: nothing ever arrives. A zero-result ``complete`` is the most dangerous
    receipt of all — indistinguishable from "this query genuinely has no matches", and it banks a
    full window on the strength of an outage."""
    fetch, pages = FAMILIES[family]
    items, exhausted = fetch(monkeypatch, pages, fail_at=1, mode=mode)
    assert exhausted != "complete", (
        f"{family}/{mode}: claimed 'complete' having fetched nothing — "
        f"an outage would be recorded as an exhaustive, empty search"
    )
    assert not items, f"{family}/{mode}: returned items despite a failing first page"


# ─────────────────────────────────────────────────────────────────────────────
# II. Hostile listing bodies: no crash, no false completeness, no invented identity
# ─────────────────────────────────────────────────────────────────────────────
# Two distinct kinds of bad body, because they warrant different assertions.
#
# NOT_A_LISTING is content that carries no extractable item at all — an outage page, a truncated
# document, a wall of prose. Scanning one must never end in a completeness claim WITH items,
# because there are none to find.
# Three inputs, not seven: one per DISTINCT branch the scanners take. The other four
# ("whitespace", "truncated tag", "unclosed", "declaration only") all land on the same
# ``ET.ParseError`` arm as "not markup", so they were matrix padding rather than coverage.
NOT_A_LISTING = {
    # (a) a 200 with an empty body — caught by `not res.text`, BEFORE any parser runs. The only
    #     input here that never reaches a parser at all, and not covered by the dead-surface test
    #     below (which only sends non-200 statuses).
    "empty": "",
    # (b) well-formed, parses cleanly, wrong document entirely. THE dangerous shape: this is the
    #     class of body that produced the arXiv false-`complete` bug, where "parsed fine, contained
    #     nothing" was read as "there is nothing".
    "html in xml slot": "<html><body>403 Forbidden</body></html>",
    # (c) not markup at all — `ET.ParseError` for sitemap/feed, zero links for html-index.
    "not markup": "just some prose, no tags at all",
}

# GNARLY is a body that IS a valid listing and does contain a real link, but is shaped to break a
# hand-rolled parser: pathological nesting, a 20 KB attribute, entity soup, an embedded NUL.
# Finding the link here is CORRECT — ``complete`` with candidates is the honest outcome — so the
# assertion is about identity hygiene, not about the verdict.
GNARLY = {
    "deep nesting": "<div>" * 300 + "<a href='/x'>t</a>" + "</div>" * 300,
    "huge attr": f"<html><a href='/a?{'x' * 20000}'>t</a></html>",
    "entity soup": "<html><a href='/a'>&amp;&lt;&#x26;&nosuch;</a></html>",
    "nul byte": "<html><a href='/a'>\x00title</a></html>",
}


@pytest.mark.parametrize("name", sorted(NOT_A_LISTING))
@pytest.mark.parametrize("method", ["html-index", "sitemap", "feed"])
def test_unlistable_body_never_scans_as_a_populated_complete(
    name: str, method: str,
) -> None:
    """(II+I) The listing families' half of the completeness invariant.

    A surface served a block page, a truncated document or a wall of prose must not come back
    claiming a complete listing of items it invented. ``collect.blogs`` and ``collect.cards``
    write one coverage receipt per surface keyed on ``spec.key``, so a false ``complete`` here is
    the same permanent watermark lie the API sweeps can tell, reached by a different route.

    ``complete`` with ZERO candidates is permitted for **html-index only**, and deliberately so:
    ``HTMLParser`` never raises and a block page is valid HTML, so an html-index with no links is
    genuinely indistinguishable from an index that has been emptied. "I read it, there was
    nothing" is the honest reading, and there is no structural signal that would justify better.

    ``sitemap`` and ``feed`` get no such licence, because there IS a signal: the root element.
    Both must report ``error``. This is the check that was missing — a 200 carrying
    ``<html>403 Forbidden</html>`` used to parse cleanly, yield no ``<url>``/``<item>``, and bank
    ``complete`` plus a full coverage window for a surface never actually read."""
    spec = Surface(key="k", method=method, urls=["https://e.test/x"])
    scan = scan_surface(spec, "c", None, _CannedFetcher(NOT_A_LISTING[name]), full=True)
    assert not scan.candidates, (
        f"{method}/{name}: produced {len(scan.candidates)} candidate(s) from a body that "
        f"contains no listing at all"
    )
    if method == "html-index":
        assert scan.exhausted in ("complete", "error")
    else:
        assert scan.exhausted == "error", (
            f"{method}/{name}: banked {scan.exhausted!r} for a body whose root element is not a "
            f"{method} — that claims a window over a surface that was never read"
        )


@pytest.mark.parametrize(
    "method, body",
    [
        ("sitemap", '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/'
                    'sitemap/0.9"></urlset>'),
        ("feed", '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"></feed>'),
        ("feed", '<?xml version="1.0"?><rss><channel></channel></rss>'),
    ],
    ids=["empty urlset", "empty atom", "empty rss"],
)
def test_a_genuinely_empty_listing_still_reports_complete(method: str, body: str) -> None:
    """The other side of the root check, and the reason it is a root check rather than an
    is-it-empty check.

    A real sitemap or feed that happens to contain no items HAS been read exhaustively, and must
    keep saying so — otherwise a quiet week on a blog would look like an outage, the surface would
    never bank a window, and its watermark would never advance. Tightening ``error`` to cover this
    case would swap a false ``complete`` for a false ``error``, which stalls collection instead of
    skipping it. Only the ROOT ELEMENT separates the two."""
    spec = Surface(key="k", method=method, urls=["https://e.test/x"])
    scan = scan_surface(spec, "c", None, _CannedFetcher(body), full=True)
    assert scan.exhausted == "complete", (
        f"{method}: a valid but empty listing reported {scan.exhausted!r} — a quiet period would "
        f"stall the surface instead of banking a window"
    )
    assert not scan.candidates


@pytest.mark.parametrize("name", sorted(GNARLY))
def test_gnarly_body_never_mints_a_malformed_identity(name: str) -> None:
    """(II) These bodies DO contain a real link, so extracting it is correct. What must not
    survive is a slug or URL that is blank, whitespace-padded, or carrying a control character:
    ``external_id`` is the permanent dedup key, so a source minted under a mangled one can never
    be merged with the real document later — re-collecting it just adds a second row.

    html-index only. These are all HTML, so the sitemap and feed scanners reject them at the parse
    step and the assertion loop runs over an empty candidate list — three methods looked like
    three times the coverage and was one third of it."""
    spec = Surface(key="k", method="html-index", urls=["https://e.test/x"])
    scan = scan_surface(spec, "c", None, _CannedFetcher(GNARLY[name]), full=True)
    assert scan.candidates, f"{name}: expected the real link to be found — nothing to assert on"
    for cand in scan.candidates:
        assert cand.url and cand.url.strip() == cand.url, f"{name}: blank/padded url"
        assert "\x00" not in cand.url, f"{name}: NUL survived into a url"
        assert "\x00" not in cand.slug, f"{name}: NUL survived into a slug"
        assert cand.slug.strip() == cand.slug, f"{name}: padded slug {cand.slug!r}"


def test_dead_surface_is_an_error_not_an_empty_success(monkeypatch: Any) -> None:
    """(II+I) A non-200 must never read as "this surface has nothing". ``_scan_*`` distinguishes
    them; this pins it, because the two are one ``or`` away from each other in every scanner."""
    for status in (403, 404, 429, 500, 0):
        for method in ("html-index", "sitemap", "feed"):
            spec = Surface(key="k", method=method, urls=["https://e.test/x"])
            scan = scan_surface(spec, "c", None, _CannedFetcher("", status=status), full=True)
            assert scan.exhausted == "error", (
                f"{method}: HTTP {status} scanned as {scan.exhausted!r} — a dead surface would "
                f"bank a window it never read"
            )
            assert not scan.candidates


def test_deinvert_abstract_survives_hostile_indices() -> None:
    """(II) OpenAlex ships abstracts as an inverted index, and ``collect.openalex`` writes the
    reconstruction straight into a corpus snapshot (D-INBAND1). A malformed index must yield
    nothing — never a crash, and never a partially-garbled body presented as an abstract."""
    for bad in (
        {}, {"a": None}, {"a": "notalist"}, {"a": [-1]}, {"a": [1.5]}, {"a": ["x"]},
        {"a": [10**9]}, {"a": [0], "b": [0]},
    ):
        out = oa.deinvert_abstract(bad)
        assert out is None or isinstance(out, str)
