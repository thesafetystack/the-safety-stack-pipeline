"""Two passes of the collect phase and ``gather`` (round 4, D226: the phases are idempotent on successful
entries).

Every collector of ``configs/plans/dev-v2.yaml`` (``collect.arxiv_sweep``, ``collect.openalex``,
``collect.semantic_scholar``, ``collect.forums``, ``collect.af_graphql``, ``collect.lw_graphql``,
``collect.blogs``, ``collect.cards``), the lookups (``collect.openalex-lookup``,
``collect.s2-lookup``, ``collect.card-dates-lookup``) and ``gather`` run through the real runner on a
temporary store, each stage with the inputs a build gives it (:func:`stack.build.stage_inputs`; a
collector pins its own chains from the line's ``prior:``), over :class:`World`: one fake that
answers every provider those stages talk to at the existing seams
(``stack.collect.base.urllib_send``, the lookups' ``SEND``, the card-date audit's ``GET``) and
counts requests. A pass is the plan's collect phase in plan order with a ``gather`` before the
lookups (they read the ``documents`` head, which a fresh store does not have) and one after.

(a) Pass 2 minutes after pass 1 makes no request for what pass 1 completed and writes no rows
(``test_second_pass_repeats_no_completed_work``; it prints the per-stage table). (b) A unit that
failed in pass 1 is retried in pass 2, and only that unit (``test_failed_unit_is_retried_*``).
(c) After the re-list interval a listing surface is due again (``test_listing_due_after_*``).
(d) A ``failed_transient`` window carrying the provider's ``retry_after`` holds the channel until
then (``test_retry_after_holds_*``). (e) Every row every collector and lookup writes carries its
query attribution (``test_every_collector_attributes_its_rows``, D228).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import time
import urllib.parse
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from stack.collect import base as collect_base
from stack.http import HttpResponse
from stack.lookup import card_date_backfill
from stack.plan import Plan
from stack.runs import RunRecord
from stack.store.chain import head_of, read_view, resolve
from tests.synth.records import run_line, sdir
from workflows.fetch.metadata import main as lookup

pytestmark = pytest.mark.e2e  # area marker(s), not a tier (pyproject markers; docs/SETUP.md §7)

TODAY = dt.datetime.now(dt.UTC).date()


def _h(text: str, n: int = 5) -> int:
    return int(hashlib.sha256(text.encode()).hexdigest()[:8], 16) % (10 ** n)


def _iso(d: dt.date) -> str:
    return d.isoformat()


# ─────────────────────────────────────────────────────────────────────────────
# the world: every provider of the collect phase, one counting fake
# ─────────────────────────────────────────────────────────────────────────────
class World:
    """Answers the collect phase's providers (no network) and logs every request. ``fail`` holds
    substrings: a request whose URL or body contains one answers ``503`` (every attempt);
    ``limit`` maps a substring to a ``429`` with that ``Retry-After`` (seconds); ``s2_miss`` holds
    substrings: an S2 batch id containing one answers ``null`` (a missed key), beside the ids the
    hash misses."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.fail: list[str] = []
        self.limit: dict[str, float] = {}
        self.s2_miss: list[str] = []

    # the Sender seam (stack.collect.base.Sender) and the lookups' SEND
    def __call__(self, method: str, url: str, *, headers: Mapping[str, str], body: bytes | None,
                 timeout: float, max_bytes: int) -> HttpResponse:
        text = urllib.parse.unquote_plus(url) + (body.decode("utf-8", "replace") if body else "")
        self.calls.append((method, url, text))
        if any(f in text for f in self.fail):
            return HttpResponse(503, {}, b"", url)
        for sub, after in self.limit.items():
            if sub in text:
                return HttpResponse(429, {"retry-after": str(int(after))}, b"", url)
        parts = urllib.parse.urlsplit(url)
        q = dict(urllib.parse.parse_qsl(parts.query))
        host, path = parts.hostname or "", parts.path
        payload: Any = json.loads(body) if body else None
        if host == "export.arxiv.org":
            return self._text(url, _arxiv_feed(q))
        if host == "api.openalex.org":
            return self._json(url, _openalex(q))
        if host == "api.semanticscholar.org" and path.endswith("/batch"):
            return self._json(url, [_s2_paper(i) if _h(i) % 2 == 0 and not any(m in i for m in self.s2_miss)
                                    else None for i in payload["ids"]])
        if host == "api.semanticscholar.org":
            return self._json(url, {"total": 1, "token": None,
                                    "data": [_s2_paper(f"q:{q.get('query', '')}")]})
        if path.endswith("/api/search"):
            return self._json(url, [{"hits": [_forum_hit(host, payload)], "nbPages": 1}])
        if path.endswith("/graphql"):
            return self._json(url, {"data": {"posts": {"results": _fm_posts(host, payload)}}})
        if host == "api.github.com" and "/orgs/" in path:
            return self._json(url, [{"name": "Model-V1", "created_at": "2026-01-02T00:00:00Z"}])
        if host == "api.github.com":
            return self._json(url, {"tree": [], "truncated": False})
        if path.endswith("robots.txt"):
            return self._text(url, "User-agent: *\nAllow: /\n")
        if "sitemap" in path:
            return self._text(url, '<?xml version="1.0"?><urlset xmlns='
                                   '"http://www.sitemaps.org/schemas/sitemap/0.9"></urlset>')
        if any(s in path for s in ("rss", "feed", "atom")):
            return self._text(url, _rss(host))
        return self._text(url, _index(host), "text/html")

    # the HttpGetter seam (card-date audit)
    def get(self, url: str, *, headers: Mapping[str, str], timeout: float,
            max_bytes: int) -> HttpResponse:
        return self("GET", url, headers=headers, body=None, timeout=timeout, max_bytes=max_bytes)

    @staticmethod
    def _json(url: str, data: Any) -> HttpResponse:
        return HttpResponse(200, {"content-type": "application/json"}, json.dumps(data).encode(),
                            url)

    @staticmethod
    def _text(url: str, text: str, media: str = "application/xml") -> HttpResponse:
        return HttpResponse(200, {"content-type": media}, text.encode(), url)


def _arxiv_feed(q: Mapping[str, str]) -> str:
    """One entry per query (first page), submitted on the query's from-date."""
    search = q.get("search_query", "")
    if int(q.get("start", "0")) > 0:
        return '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"></feed>'
    m = re.search(r"submittedDate:\[(\d{8})", search)
    day = dt.datetime.strptime(m.group(1), "%Y%m%d").date() if m else TODAY
    aid = f"2609.{_h(search.split(' AND submittedDate')[0]):05d}"
    return ('<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry>'
            f"<id>http://arxiv.org/abs/{aid}v1</id><title>Paper {aid}</title>"
            "<summary>We study the alignment of language models with oversight.</summary>"
            f"<published>{_iso(day)}T00:00:00Z</published>"
            "<author><name>Ethan Perez</name></author></entry></feed>")


_ABSTRACT = ("We study scalable oversight of language models with debate between two copies, and "
             "find that judges agree with experts more often when the debaters are stronger.")


def _work(wid: str, doi: str) -> dict[str, Any]:
    words = _ABSTRACT.split()
    return {"id": f"https://openalex.org/{wid}", "doi": f"https://doi.org/{doi}",
            "ids": {"openalex": f"https://openalex.org/{wid}"}, "title": f"Work {wid}",
            "publication_year": TODAY.year, "publication_date": _iso(TODAY), "type": "article",
            "cited_by_count": 3,
            "authorships": [{"author": {"display_name": "Ada Lovelace", "orcid": None},
                             "institutions": []}],
            "abstract_inverted_index": {w: [i for i, x in enumerate(words) if x == w]
                                        for w in words}}


def _openalex(q: Mapping[str, str]) -> dict[str, Any]:
    filt = q.get("filter", "")
    m = re.match(r"^(doi|pmid|openalex):(.*)$", filt)
    if m is None:  # the collector's sweep: one work per query, no next cursor
        # the window's dates, the look-back start and the lagged end (today minus a lag), do not pick the fake
        # DOIs: with the end in the hash, which DOIs the S2 batch misses changed from day to day
        base = re.sub(r"(from|to)_publication_date:[0-9-]+", r"\1_publication_date:*", filt)
        base = re.sub(r"^type:[^,]*,", "", base)  # the work-type list (D653) does not pick the fake DOIs
        n = _h(base)
        return {"results": [_work(f"W{n}", f"10.5555/oa{n}")], "meta": {"next_cursor": None}}
    if m.group(1) != "doi":  # a lookup by pmid / openalex id: no hit
        return {"results": [], "meta": {}}
    return {"results": [_work(f"W9{_h(v)}", v) for v in m.group(2).split("|") if _h(v) % 2 == 0],
            "meta": {}}


def _s2_paper(form: str) -> dict[str, Any]:
    ext: dict[str, Any] = {}
    if form.startswith("ARXIV:"):
        ext["ArXiv"] = form.removeprefix("ARXIV:")
    elif form.startswith("DOI:"):
        ext["DOI"] = form.removeprefix("DOI:")
    else:
        ext["DOI"] = f"10.5555/s2{_h(form)}"
    return {"paperId": f"{_h(form, 8):040d}", "externalIds": ext, "title": f"S2 paper {form}",
            "year": TODAY.year, "publicationDate": _iso(TODAY), "abstract": _ABSTRACT,
            "venue": "", "publicationTypes": ["JournalArticle"], "authors": [{"name": "Bea"}],
            "openAccessPdf": None}


def _forum_hit(host: str, payload: Any) -> dict[str, Any]:
    params = payload[0]["params"]
    pid = f"fm{_h(host + json.dumps(params.get('query')) + json.dumps(params.get('facetFilters')))}"
    posted = TODAY - dt.timedelta(days=1)
    return {"_id": pid, "objectID": pid, "title": f"Post {pid}", "baseScore": 100, "af": True,
            "postedAt": f"{_iso(posted)}T00:00:00.000Z", "body": "A forum post body.",
            "authorDisplayName": "Alice", "publicDateMs": int(time.mktime(posted.timetuple())) * 1000}


def _fm_posts(host: str, payload: Any) -> list[dict[str, Any]]:
    query = str(payload["query"])
    if int(re.search(r"offset:\s*(\d+)", query).group(1)) > 0:  # type: ignore[union-attr]
        return []
    tag = re.search(r'tagId:\s*"([^"]+)"', query)
    tag_id = tag.group(1) if tag else ""
    pid = f"gq{_h(host + tag_id)}"
    return [{"_id": pid, "title": f"Post {pid}", "slug": pid, "baseScore": 50,
             "postedAt": f"{_iso(TODAY - dt.timedelta(days=1))}T00:00:00.000Z",
             "user": {"displayName": "Alice", "username": "alice"}, "coauthors": [],
             "tagRelevance": {tag_id: 1} if tag_id else {},
             "htmlBody": "<p>A post body long enough to keep.</p>"}]


def _rss(host: str) -> str:
    day = TODAY - dt.timedelta(days=2)
    stamp = day.strftime("%a, %d %b %Y 09:00:00 GMT")
    return ('<?xml version="1.0"?><rss version="2.0"><channel><item><title>A post</title>'
            f"<link>https://{host}/{day.year}/a-post/</link><pubDate>{stamp}</pubDate></item>"
            "</channel></rss>")


def _index(host: str) -> str:
    return (f'<html><body><a href="https://{host}/{TODAY.year}/an-indexed-post/">An indexed post'
            '</a><a href="/claude-test-system-card">Claude Test System Card</a></body></html>')


# ─────────────────────────────────────────────────────────────────────────────
# the plan (dev-v2's collect lines, options narrowed to the world) and the passes
# ─────────────────────────────────────────────────────────────────────────────
COLLECTORS = ("collect.arxiv_sweep", "collect.openalex", "collect.semantic_scholar",
              "collect.forums", "collect.af_graphql", "collect.lw_graphql", "collect.blogs",
              "collect.cards")
LOOKUPS = ("collect.openalex-lookup", "collect.s2-lookup", "collect.card-dates-lookup")
#: A pass: the collectors, a gather (the lookups read the documents head), the lookups, a gather.
PASS = (*COLLECTORS, "gather", *LOOKUPS, "gather")


def plan_for(roster: Path, extra: Mapping[str, Mapping[str, Any]] | None = None) -> Plan:
    """dev-v2's collect phase (``configs/plans/dev-v2.yaml``) with options narrowed to
    :class:`World`; ``extra`` ``{stage: options}`` merges into a stage's options."""
    opts: dict[str, dict[str, Any]] = {
        "collect.arxiv_sweep": {"window_days": 21, "roster": str(roster),
                                "keyword_groups": ["umbrella"], "scoped_categories": ["cs.GT"]},
        "collect.openalex": {"window_days": 3, "window_end_days_ago": 14, "max_records": 300,
                             "queries": ["group:umbrella", "group:superalignment"]},
        # one span per query (D801's year spans have their own tests in test_collect_s2.py): the world's S2
        # search answers one paper dated today whatever the date range, and these tests count one request a pass
        "collect.semantic_scholar": {"max_pages": 1, "queries": ["group:umbrella"], "split_years": 0},
        "collect.forums": {"window_days": 7, "max_records": 200, "groups": ["specs"],
                           "tags": []},
        "collect.af_graphql": {"window_days": 7, "max_records": 200},
        "collect.lw_graphql": {"window_days": 7, "max_records": 500, "tags": ["ai"]},
        "collect.blogs": {"only": ["openai_alignment_blog", "metr_blog"]},
        "collect.cards": {"only": ["anthropic_cards", "deepseek_cards"], "max_urls": 20},
        "collect.openalex-lookup": {"max_records": 200},
        "collect.s2-lookup": {"max_records": 50},
        "collect.card-dates-lookup": {},
    }
    for stage, more in (extra or {}).items():
        opts[stage].update(more)
    lines: list[dict[str, Any]] = []
    for stage in (*COLLECTORS, *LOOKUPS):
        line: dict[str, Any] = {"stage": stage, "prior": ["sources", "coverage"],
                                "options": opts[stage]}
        if stage == "collect.openalex-lookup":
            line.update(keys=["doi", "pmid", "arxiv"], miss_cap=3)
        if stage == "collect.s2-lookup":
            line.update(keys=["doi", "arxiv"], miss_cap=3)
        lines.append(line)
    lines.append({"stage": "gather"})
    return Plan.model_validate({"plan": "passes", "schema_version": 2, "series": "main", "stages": lines})


def run_one(store: Path, plan: Plan, stage: str) -> RunRecord:
    """One run of ``stage`` with the inputs a build gives it."""
    rec = run_line(store, plan, stage)
    assert rec is not None, stage
    return rec


def run_pass(store: Path, plan: Plan, world: World,
             stages: tuple[str, ...] = PASS) -> dict[str, dict[str, Any]]:
    """One pass; per stage (first occurrence; a second gather is ``gather#2``) the world's request
    count and the run's record."""
    out: dict[str, dict[str, Any]] = {}
    for stage in stages:
        if stage.endswith("-lookup") and head_of(store, "data/documents/main") == "":
            continue
        n = len(world.calls)
        rec = run_one(store, plan, stage)
        assert rec.status == "ok", (stage, rec.error)
        key = stage if stage not in out else f"{stage}#2"
        out[key] = {"requests": len(world.calls) - n, "rec": rec}
    return out


def _rows(rec: RunRecord) -> int:
    return int(rec.counts.get("sources_written", 0)) + int(rec.counts.get("coverage_written", 0))


def _touched(rec: RunRecord) -> int:
    """A gather run's ``rows_touched`` (rows that differ from the head's)."""
    return int((rec.version or {}).get("rows_touched", 0))


def _skipped(rec: RunRecord) -> int:
    return sum(int(rec.counts.get(k, 0)) for k in collect_base.SKIP_COUNTS)


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> World:
    for var in ("STACK_ARXIV_ROSTER", "STACK_COLLECT_LIMIT", "STACK_COLLECT_FRACTION",
                "STACK_COLLECT_MAX_QUERIES", "STACK_ARXIV_FROM", "STACK_OPENALEX_FROM",
                "STACK_OPENALEX_FULL", "STACK_OPENALEX_RETRY_FAILED_RUN", "STACK_S2_FROM",
                "STACK_S2_FULL", "STACK_FORUMS_MIN_KARMA", "STACK_AF_FROM", "STACK_BLOGS_LIMIT",
                "STACK_BLOGS_ONLY", "STACK_BLOGS_FIXTURES", "STACK_CARDS_LIMIT", "STACK_CARDS_FULL",
                "STACK_CARDS_ONLY", "STACK_CARDS_FIXTURES", "STACK_MAILTO", "STACK_METADATA_LIMIT",
                "STACK_CARD_DATES_LIMIT", "STACK_CARD_DATES_CHANNELS", "STACK_CARD_DATES_REFRESH"):
        monkeypatch.delenv(var, raising=False)
    w = World()
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr("stack.collect.base.urllib_send", w)
    monkeypatch.setattr(lookup, "SEND", w)
    monkeypatch.setattr(card_date_backfill, "GET", w.get)
    return w


@pytest.fixture
def roster(tmp_path: Path) -> Path:
    p = tmp_path / "roster.yaml"
    p.write_text('names:\n  - "Ethan Perez"\n', encoding="utf-8")
    return p


def _table(store: Path, p1: dict[str, dict[str, Any]], p2: dict[str, dict[str, Any]]) -> str:
    lines = [(f"{'stage':28} {'req1':>5} {'req2':>5} {'rows2':>5} {'skip2':>5} {'http2':>5}  "
              "files2 (rows, bytes)")]
    for stage, a in p1.items():
        b = p2.get(stage)
        if b is None:
            continue
        rec = b["rec"]
        rows = _touched(rec) if stage.startswith("gather") else _rows(rec)
        files = ", ".join(f"{k} ({o['row_count']}, {(store / o['path']).stat().st_size})"
                          for k, o in rec.outputs.items())
        lines.append(f"{stage:28} {a['requests']:>5} {b['requests']:>5} {rows:>5} "
                     f"{_skipped(rec):>5} {rec.counts.get('http_requests', '-'):>5}  {files}")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# (a) pass 2 repeats no completed work
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.slow  # real store + runner, ~20 runs
def test_second_pass_repeats_no_completed_work(tmp_path: Path, world: World, roster: Path,
                                               capsys: pytest.CaptureFixture[str]) -> None:
    store, plan = tmp_path / "store", plan_for(roster)
    p1 = run_pass(store, plan, world)
    p2 = run_pass(store, plan, world)
    with capsys.disabled():
        print("\n" + _table(store, p1, p2))
    for stage in COLLECTORS + LOOKUPS:
        assert p1[stage]["requests"] > 0, f"{stage}: pass 1 made no request (the world is wrong)"
    for stage, r in p2.items():
        rec = r["rec"]
        if stage.startswith("gather"):
            assert _touched(rec) == 0, (stage, rec.version)
            continue
        assert r["requests"] == 0, (stage, r["requests"], rec.counts)
        assert rec.counts.get("http_requests", 0) == 0, (stage, rec.counts)
        assert _rows(rec) == 0, (stage, rec.counts)
        assert rec.outputs == {}, (stage, sorted(rec.outputs))
        if stage in COLLECTORS:
            assert _skipped(rec) > 0, (stage, rec.counts)


# ─────────────────────────────────────────────────────────────────────────────
# (b) a unit that failed is retried in the next pass, and only that unit
# ─────────────────────────────────────────────────────────────────────────────
#: (stage, a substring of the failing unit's requests, plan option overrides). Every case has a
#: unit that succeeds beside the failing one, except af_graphql (one sweep) and the lookups (one
#: batch per axis).
FAILS = [
    ("collect.arxiv_sweep", "cat:cs.GT", {}),
    ("collect.openalex", "superalignment", {}),
    ("collect.semantic_scholar", "superalignment",
     {"collect.semantic_scholar": {"max_pages": 0,
                                   "queries": ["group:umbrella", "group:superalignment"]}}),
    ("collect.forums", "alignmentforum.org", {}),
    ("collect.af_graphql", "alignmentforum.org/graphql", {}),
    ("collect.lw_graphql", "decision-theory",
     {"collect.lw_graphql": {"tags": ["ai", "decision-theory"]}}),
    ("collect.blogs", "metr.org", {}),
    ("collect.cards", "api.github.com", {}),
    ("collect.openalex-lookup", "filter=doi:", {}),
    ("collect.s2-lookup", "paper/batch", {}),
    ("collect.card-dates-lookup", "www.anthropic.com", {}),
]


@pytest.mark.slow  # real store + runner
@pytest.mark.parametrize("stage, fail, extra", FAILS, ids=[f[0] for f in FAILS])
def test_failed_unit_is_retried_in_the_next_pass(tmp_path: Path, world: World, roster: Path,
                                                stage: str, fail: str, extra: dict[str, Any]) -> None:
    """Pass 1 fails one unit (``503`` on every attempt); pass 2 minutes later asks again for that
    unit only (every request it makes is the failed unit's); pass 3 asks nothing."""
    if stage == "collect.lw_graphql":
        fail = lwg_tag_id(fail)
    store, plan = tmp_path / "store", plan_for(roster, extra)
    if stage in LOOKUPS:  # the lookups read the documents head
        run_pass(store, plan, world, (*COLLECTORS, "gather"))
    world.fail = [fail]
    n = len(world.calls)
    p1 = run_pass(store, plan, world, (stage,))
    assert any(fail in t for *_, t in world.calls[n:]), "the failing unit was not asked"
    world.fail = []
    n = len(world.calls)
    p2 = run_pass(store, plan, world, (stage,))
    retried = world.calls[n:]
    assert retried and all(fail in t for *_, t in retried), [u for _, u, _ in retried]
    assert p2[stage]["rec"].counts["http_requests"] == len(retried)
    if stage not in ("collect.af_graphql", *LOOKUPS):  # the unit that succeeded is skipped
        assert sum(p2[stage]["rec"].counts.get(k, 0) for k in collect_base.SKIP_COUNTS) > 0
    p3 = run_pass(store, plan, world, (stage,))
    assert p3[stage]["requests"] == 0 and p3[stage]["rec"].outputs == {}
    assert p1[stage]["rec"].status == "ok"


def lwg_tag_id(slug: str) -> str:
    """The ForumMagnum tag id of an LW tag slug (the GraphQL query carries the id)."""
    from stack.stage_runner import load_stage_module

    return str(load_stage_module("collect.lw_graphql").TAG_IDS[slug])


# ─────────────────────────────────────────────────────────────────────────────
# (c) re-list interval, partial windows, a page budget
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.slow  # real store + runner
@pytest.mark.usefixtures("no_keyword_cuts")  # harness fixtures are publisher / non-AI works
def test_listing_and_missed_keys_due_after_relist_interval(
        tmp_path: Path, world: World, roster: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Listing surfaces (blogs, cards) and missed lookup keys are not asked again 1 h after a pass
    and are asked again 13 h after it (``relist_after_h`` default 12). S2 at full scope: the world's
    documents all carry an abstract, which S2's default scope skips (D520)."""
    store, plan = tmp_path / "store", plan_for(roster, {"collect.s2-lookup": {"only_missing_abstract": False}})
    world.s2_miss = ["DOI:10.5555/oa"]  # the OpenAlex works' DOIs: S2 misses them, the missed keys to re-ask
    run_pass(store, plan, world)
    stages = ("collect.blogs", "collect.cards", "collect.openalex-lookup", "collect.s2-lookup")
    t0 = dt.datetime.now(dt.UTC)
    monkeypatch.setattr("stack.collect.base.utcnow", lambda: t0 + dt.timedelta(hours=1))
    soon = run_pass(store, plan, world, stages)
    assert {s: r["requests"] for s, r in soon.items()} == dict.fromkeys(stages, 0)
    assert soon["collect.openalex-lookup"]["rec"].counts["not_due"] > 0
    # the missed keys of pass 1 (``world.s2_miss``) are not due yet
    assert soon["collect.s2-lookup"]["rec"].counts["not_due"] > 0
    monkeypatch.setattr("stack.collect.base.utcnow", lambda: t0 + dt.timedelta(hours=13))
    later = run_pass(store, plan, world, stages)
    assert all(r["requests"] > 0 for r in later.values()), {
        s: r["requests"] for s, r in later.items()}
    assert later["collect.openalex-lookup"]["rec"].counts["not_due"] == 0


@pytest.mark.slow  # real store + runner
def test_partial_window_waits_and_page_budget_moves_on(tmp_path: Path, world: World,
                                                       roster: Path) -> None:
    """A cap-``partial`` sweep (AF, ``max_records: 1``) is not asked again in the next pass
    (``not_due``; the same cap truncates the same way) unless ``relist_after_h: 0``; a page budget
    (S2 ``max_pages: 1`` over two queries) reaches the second query in pass 2 because the first is
    covered, and pass 3 asks nothing."""
    store = tmp_path / "store"
    plan = plan_for(roster, {"collect.af_graphql": {"max_records": 1},
                             "collect.semantic_scholar": {"queries": ["group:umbrella",
                                                                      "group:superalignment"]}})
    stages = ("collect.af_graphql", "collect.semantic_scholar")
    p1 = run_pass(store, plan, world, stages)
    p2 = run_pass(store, plan, world, stages)
    p3 = run_pass(store, plan, world, stages)
    af1, af2 = p1["collect.af_graphql"], p2["collect.af_graphql"]
    assert af1["rec"].counts["windows_partial"] == 1
    assert af2["requests"] == 0 and af2["rec"].counts["not_due"] == 1
    # D492: the cap still stands in pass 2, so neither pass claims the scope (reason `limit` both times)
    assert [a["rec"].scope_complete for a in (af1, af2)] == [False, False]
    assert (af1["rec"].counts["windows_capped"], af2["rec"].counts["capped_not_due"]) == (1, 1)
    s2 = [p["collect.semantic_scholar"] for p in (p1, p2, p3)]
    assert [r["requests"] for r in s2] == [1, 1, 0]
    assert [r["rec"].scope_complete for r in s2] == [False, True, True]
    assert [r["rec"].counts.get("already_covered", 0) for r in s2] == [0, 1, 2]
    asked = run_one(store, plan_for(roster, {"collect.af_graphql": {"max_records": 1,
                                                                    "relist_after_h": 0}}),
                    "collect.af_graphql")
    assert asked.counts["http_requests"] == 1 and asked.counts["windows_partial"] == 1


@pytest.mark.slow  # real store + runner
def test_s2_default_year_spans_over_passes(tmp_path: Path, world: World, roster: Path) -> None:
    """S2 at its default ``split_years`` (D801; the harness pins 0): the first pass from the channel floor is cut
    in year spans, and the world's search answers a paper dated today to every range, so the first bounded span
    proves the upper bound ignored and is asked again open above (D802), banking [floor, today] in one ``ok``
    window: two requests, scope complete. The next pass asks nothing."""
    from workflows.collect.semantic_scholar.main import DEFAULT_SPLIT_YEARS

    plan = plan_for(roster, {"collect.semantic_scholar": {"max_pages": 0, "split_years": DEFAULT_SPLIT_YEARS}})
    store, stage = tmp_path / "store", "collect.semantic_scholar"
    p1, p2 = run_pass(store, plan, world, (stage,)), run_pass(store, plan, world, (stage,))
    rec1 = p1[stage]["rec"]
    sent = [urllib.parse.parse_qs(urllib.parse.urlsplit(u).query)["publicationDateOrYear"][0]
            for _, u, _ in world.calls if "semanticscholar" in u]
    assert len(sent) == 2 and sent[1].endswith(":") and not sent[0].endswith(":"), sent
    assert sent[0].split(":")[0] == sent[1].split(":")[0]  # the fallback asks from the span's own lower bound
    assert (rec1.counts["upper_bound_ignored"], rec1.counts["upper_bound_fallback"], rec1.scope_complete) == (
        1, 1, True)
    cov = read_view(resolve(store, sdir("coverage.semantic_scholar"))).to_pylist()
    assert [(r["status"], r["window_end_at"].date()) for r in cov] == [("ok", TODAY)]
    assert p2[stage]["requests"] == 0 and p2[stage]["rec"].counts["already_covered"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# (d) a provider retry_after holds the channel
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.slow  # real store + runner
def test_retry_after_holds_the_lookup_until_it_passes(
        tmp_path: Path, world: World, roster: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A long 429 halts ``collect.openalex-lookup`` (``failed_transient[rate_limit]`` with
    ``retry_after``); the next pass makes no request (``held_retry_after``, scope incomplete);
    after the retry_after it asks again and completes."""
    store, plan = tmp_path / "store", plan_for(roster)
    run_pass(store, plan, world, (*COLLECTORS, "gather"))
    world.limit = {"filter=doi:": 3600}
    p1 = run_pass(store, plan, world, ("collect.openalex-lookup",))
    rec1 = p1["collect.openalex-lookup"]["rec"]
    assert rec1.scope_complete is False and rec1.counts["halted"] == 1
    world.limit = {}
    p2 = run_pass(store, plan, world, ("collect.openalex-lookup",))
    rec2 = p2["collect.openalex-lookup"]["rec"]
    assert p2["collect.openalex-lookup"]["requests"] == 0 and rec2.scope_complete is False
    assert rec2.counts["held_retry_after"] > 0 and rec2.outputs == {}
    later = dt.datetime.now(dt.UTC) + dt.timedelta(hours=2)
    monkeypatch.setattr("stack.collect.base.utcnow", lambda: later)
    p3 = run_pass(store, plan, world, ("collect.openalex-lookup",))
    rec3 = p3["collect.openalex-lookup"]["rec"]
    assert p3["collect.openalex-lookup"]["requests"] == 1 and rec3.scope_complete is True
    assert rec3.counts.get("held_retry_after", 0) == 0


# ─────────────────────────────────────────────────────────────────────────────
# (e) query attribution on every collector's rows (D228)
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.slow  # real store + runner
@pytest.mark.usefixtures("no_keyword_cuts")  # harness fixtures are publisher / non-AI works
def test_every_collector_attributes_its_rows(tmp_path: Path, world: World, roster: Path) -> None:
    """After one pass every ``sources`` row of every collector and lookup has a non-empty
    ``metadata.matched_by``, ``metadata.query_digest`` is one of its entries, and each entry's
    digest is a ``coverage`` row of the same chain (card-dates-lookup writes no coverage: its
    query is the skip key)."""
    store, plan = tmp_path / "store", plan_for(roster)
    run_pass(store, plan, world)
    for stage in COLLECTORS + LOOKUPS:
        suffix = stage.removeprefix("collect.")
        if stage == "collect.blogs":  # the world's blog pages list no post (test_collect_blogs)
            continue
        rows = read_view(resolve(store, sdir(f"sources.{suffix}"))).to_pylist()
        assert rows, stage
        cov = set() if suffix == "card-dates-lookup" else {
            r["query_digest"] for r in read_view(resolve(store, sdir(f"coverage.{suffix}"))).to_pylist()}
        for r in rows:
            md = r["metadata"]
            assert md["matched_by"], (stage, r["identity"]["external_id"])
            digests = {m["query_digest"] for m in md["matched_by"]}
            assert md["query_digest"] in digests, stage
            assert all(m["query_id"] for m in md["matched_by"]), stage
            if cov:
                assert digests <= cov, (stage, md["matched_by"])


# ─────────────────────────────────────────────────────────────────────────────
# (f) collect.x-lookup: a missed key and a failed thread search wait relist_after_h (D226)
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.slow  # real store + runner, 5 runs
def test_x_lookup_miss_and_failed_thread_wait_between_passes(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``collect.x-lookup`` over ``tests/test_collect_x.py``'s world (``miss_cap`` 2): pass 1 looks
    up four posts, D is a miss and alice's thread search fails (a 400). Passes 2 and 3 the same
    day make no request: the observed posts are done, D's miss and the failed search wait
    ``relist_after_h`` (12 h), so ``miss_cap`` is not used up within one build (D's
    ``miss_count`` stays 1). 13 h later D and the thread are asked again (D retracted at its
    second miss, the thread rehydrated)."""
    from stack.collect import xapi
    from tests.fakes.http import FakeHttp
    from tests.test_collect_lookup import publish_documents
    from tests.test_collect_x import (
        A1,
        A2,
        A3,
        C1,
        DOCS,
        TOKEN,
        B,
        D,
        add_lookup,
        add_threads,
        add_usage,
        plan,
        record_of,
        view,
        x_run,
    )

    store = tmp_path / "store"
    publish_documents(store, DOCS)
    monkeypatch.setattr(xapi, "_sleep", lambda s: None)
    monkeypatch.setenv(xapi.TOKEN_ENV, TOKEN)
    monkeypatch.delenv(xapi.CAP_ENV, raising=False)
    monkeypatch.delenv(xapi.LIMIT_ENV, raising=False)
    fake = FakeHttp()
    monkeypatch.setattr(xapi, "SEND", fake)
    add_usage(fake, 0)
    add_lookup(fake, [A1, B, D, C1], includes=[], missing=[D])
    add_threads(fake)
    fake.add(xapi.SEARCH_RECENT_URL, xapi.search_params(A1, "100", 25, None, handle="alice"), status=400,
             json={"title": "Invalid Request"})  # alice's thread search fails (counts toward miss_cap)
    t0 = dt.datetime.now(dt.UTC)
    monkeypatch.setattr("stack.collect.base.utcnow", lambda: t0)
    rec1 = x_run(store, plan())
    assert rec1.status == "ok", rec1.error
    assert rec1.counts["keys_due"] == 4 and rec1.counts["x_miss_not_found_error"] == 1
    assert record_of(store, view(store, "sources.x-lookup")[A1])["thread_status"] == "pending"
    n1 = len(fake.calls)

    for hours in (1, 2):  # passes 2 and 3 of the same build
        monkeypatch.setattr("stack.collect.base.utcnow", lambda h=hours: t0 + dt.timedelta(hours=h))
        rec = x_run(store, plan())
        print(f"pass at +{hours} h: requests {len(fake.calls) - n1}, counts "
              f"{ {k: rec.counts.get(k) for k in ('keys_due', 'keys_done', 'not_due', 'x_threads_to_retry')} }")
        assert len(fake.calls) == n1, [c.url for c in fake.calls[n1:]]  # no usage call either
        assert (rec.counts["keys_due"], rec.counts["x_threads_to_retry"], rec.counts["not_due"]) == (0, 0, 2)
        assert rec.outputs == {} and rec.counts.get("http_requests", 0) == 0
    assert view(store, "sources.x-lookup")[D]["metadata"]["miss_count"] == 1

    add_lookup(fake, [D], missing=[D])
    add_threads(fake)  # the search answers now
    monkeypatch.setattr("stack.collect.base.utcnow", lambda: t0 + dt.timedelta(hours=13))
    rec = x_run(store, plan())
    assert (rec.counts["keys_due"], rec.counts["x_threads_to_retry"], rec.counts["not_due"]) == (1, 1, 0)
    assert rec.counts["x_thread_rows"] == 1
    src = view(store, "sources.x-lookup")
    assert src[D]["state"] == "retracted"  # second miss: miss_cap 2 reached
    assert [p["id"] for p in record_of(store, src[f"thread:{A1}:100"])["thread"]] == [A1, A2, A3]
