"""Tests for ``collect.forums`` — the LessWrong + Alignment Forum keyword sweep over ForumMagnum's
Algolia-shaped ``/api/search`` (§5.6 LW/AF row). No network: a fake search backend (per forum
host) models ``hitsPerPage``/``page``/``nbPages`` paging and the ``publicDateMs>=`` lower-bound
``numericFilters`` (honored, or silently ignored), behind the harness transport
(``stack.collect.base.urllib_send``); the stage runs through ``stack.stage_runner.run_stage`` on a
temporary store with one query per forum (``max_queries: 1``, no tags).

Covered (v3 ``test_collect_search.py`` forum cases, DB side effects now asserted on the emitted
``sources`` / ``coverage`` rows): the karma and date-floor gates; rows per channel with the
``fm_post`` key, so an LW/AF crosspost is two members with one key; the search body as an in-band
``post`` text entry, gently normalised, a body-less hit counted ``absent``; a first pass sends no
filter and writes ``[floor, today]``; a resume sends the filter at the watermark − ``karma_lag_days``
(D406), writes ``[that start, today]``, mints no known id and writes part files; an ignored filter falls back to
the floor range; a truncated windowed pass is ``partial`` and holds the watermark; ``window_days``
narrows the first pass server-side; a query already covered today makes no request; V1/V2 pass.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import pathlib
import re
from collections.abc import Mapping
from typing import Any

import pytest

from stack.build import incomplete_reasons
from stack import stage_runner
from stack.checks.verify import run_checks
from stack.http import HttpResponse
from stack.plan import Plan
from tests.synth.records import run_line, sdir
from stack.store.blobs import open_blobs
from stack.store.chain import read_view, resolve
from stack.store.parquet import read_header
from stack.collect.inband import json_record
from tests.synth.rows import content_entries

REPO = pathlib.Path(__file__).resolve().parent.parent


def _load_wf(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(
        f"_collect_{name}_under_test", REPO / "workflows" / "collect" / name / "main.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


forums = _load_wf("forums")
LW, AF = "www.lesswrong.com", "www.alignmentforum.org"
_ENVS = ("STACK_COLLECT_LIMIT", "STACK_COLLECT_FRACTION", "STACK_COLLECT_MAX_QUERIES",
         "STACK_FORUMS_MIN_KARMA", "STACK_FORUMS_TAGS", "STACK_FORUMS_FROM")
_LONG_BODY = ("A search-indexed copy of the post body, first paragraph.\r\n\r\n\r\n"
              "Second paragraph, comfortably past the in-band noise floor.")


def hit(_id: str, posted: str, *, score: int = 80, body: str | None = None,
        author: str = "Alice", af: bool = False) -> dict[str, Any]:
    h: dict[str, Any] = {"_id": _id, "title": f"Post {_id}", "baseScore": score,
                         "postedAt": f"{posted}T00:00:00Z", "publicDateMs": forums._ms(posted),
                         "authorDisplayName": author, "af": af}
    if body is not None:
        h["body"] = body
    return h


class Backend:
    """The fake ``/api/search``: ``hits[host]`` served in order, paged as the real backend pages
    (offset ``page × hitsPerPage``, ``nbPages`` over ``hitsPerPage``); ``honor`` applies the
    ``numericFilters`` clauses (``publicDateMs>=``/``<``, ``baseScore>=``) and the ``af:true``
    facet, ``honor_upper=False`` drops only the ``publicDateMs<`` clause (a deploy that ignores
    the upper bound); ``result_window`` models Elasticsearch's ``max_result_window`` (a page
    reaching past it is an HTTP 400 with a plain-text body, as live). Every request's
    ``(host, params)`` is logged in ``calls``."""

    _CLAUSE = re.compile(r"^(publicDateMs|baseScore)(>=|<)(\d+)$")

    def __init__(self, hits: dict[str, list[dict[str, Any]]], *, honor: bool = True,
                 honor_upper: bool = True, page_cap: int | None = None,
                 result_window: int | None = None) -> None:
        self.hits, self.honor, self.honor_upper = hits, honor, honor_upper
        self.page_cap, self.result_window = page_cap, result_window
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, method: str, url: str, *, headers: Mapping[str, str], body: bytes | None,
                 timeout: float, max_bytes: int) -> HttpResponse:
        assert method == "POST" and url.endswith("/api/search") and body is not None
        host = url.split("/")[2]
        params = json.loads(body)[0]["params"]
        self.calls.append((host, params))
        pool = self.hits.get(host, [])
        for clause in params.get("numericFilters") or []:
            m = self._CLAUSE.match(clause)
            assert m is not None, clause
            field, op, value = m.group(1), m.group(2), int(m.group(3))
            if not self.honor or (op == "<" and not self.honor_upper):
                continue
            pool = [h for h in pool if (int(h[field]) >= value if op == ">=" else int(h[field]) < value)]
        if self.honor and ["af:true"] in params.get("facetFilters", []):
            pool = [h for h in pool if h.get("af")]
        page, size = int(params["page"]), int(params["hitsPerPage"])
        if self.result_window is not None and (page + 1) * size > self.result_window:
            return HttpResponse(400, {}, b"search_phase_execution_exception: Result window is too "
                                b"large, from + size must be less than or equal to: [10000]", url)
        batch = pool[page * size: (page + 1) * size]
        nb_pages = (len(pool) + size - 1) // size or 1
        if self.page_cap is not None:  # an Algolia-style pagination limit: nbHits stays true
            nb_pages = min(nb_pages, self.page_cap)
            batch = batch if page < self.page_cap else []
        data = [{"hits": batch, "nbPages": nb_pages, "nbHits": len(pool)}]
        return HttpResponse(200, {}, json.dumps(data).encode(), url)

    def filters(self, host: str) -> list[Any]:
        return [p.get("numericFilters") for h, p in self.calls if h == host]


#: The karma floor every pass sends (D406's gate, default 25, pushed to the server)
K25 = "baseScore>=25"


def _ge(day: str) -> list[str]:
    """The filters a windowed pass from ``day`` sends: its lower bound and the karma floor."""
    return [f"publicDateMs>={forums._ms(day)}", K25]


def _plan(**options: Any) -> Plan:
    opts = {"max_queries": 1, "tags": [], "from": "2020-01-01", **options}
    return Plan.model_validate({"plan": "test", "schema_version": 2, "stages": [
        {"stage": "collect.forums", "prior": ["sources", "coverage"], "options": opts}]})


@pytest.fixture
def stage(tmp_path: pathlib.Path, monkeypatch: Any) -> Any:
    """The runner loads this test's module object (its seams pinned); ``serve(backend, today)``
    installs the fake and the date for the next run."""
    for var in _ENVS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(stage_runner, "load_stage_module", lambda name: forums)
    monkeypatch.setattr(forums, "_sleep", lambda s: None)

    def serve(backend: Backend, today: str) -> Backend:
        monkeypatch.setattr(forums, "_as_of_date", lambda: today)
        # the harness clock on the same day (coverage stamps, the D226 pass rule)
        monkeypatch.setattr("stack.collect.base.utcnow", lambda: dt.datetime.fromisoformat(
            today).replace(tzinfo=dt.UTC) + dt.timedelta(hours=12))
        monkeypatch.setattr("stack.collect.base.urllib_send", backend)
        return backend

    return tmp_path / "store", serve


def _go(store: pathlib.Path, *, kind: str | None = None, **options: Any) -> Any:
    rec = run_line(store, _plan(**options), "collect.forums", **({"kind": kind} if kind else {}))
    assert rec.status == "ok", rec.error
    return rec


def _rows(store: pathlib.Path) -> dict[tuple[str, str], dict[str, Any]]:
    return {(r["identity"]["channel"], r["identity"]["external_id"]): r
            for r in read_view(resolve(store, sdir("sources.forums"))).to_pylist()}


def _cov(store: pathlib.Path, channel: str) -> dict[str, Any]:
    (row,) = [r for r in read_view(resolve(store, sdir("coverage.forums"))).to_pylist()
              if r["channel"] == channel and r["window_end_at"] == max(
                  x["window_end_at"] for x in read_view(resolve(store, sdir("coverage.forums"))).to_pylist()
                  if x["channel"] == channel)]
    return row


def _day(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s).replace(tzinfo=dt.UTC)


def _window(row: dict[str, Any]) -> tuple[str, str]:
    return row["window_start_at"].date().isoformat(), row["window_end_at"].date().isoformat()


def test_gates_rows_crosspost_inband_and_first_window(stage: Any) -> None:
    """Karma and date-floor gates; rows per channel with the ``fm_post`` key (the crosspost
    ``xPOST`` is two rows, one key); the AF host serves the whole LessWrong index, so its query
    carries ``af:true`` and only the ``af`` post is an ``alignmentforum`` row; the search body
    minted as ``post`` text, normalised; a body-less hit counted ``absent``; a first pass sends no
    date filter (only the karma floor) and writes ``[floor, today]`` ok; V1/V2 pass."""
    store, serve = stage
    index = [hit("lwKEEP", "2023-05-01", body=_LONG_BODY), hit("lwLOW", "2023-05-01", score=5),
             hit("lwOLD", "2019-01-01"), hit("xPOST", "2024-02-02", author="Bob", af=True)]
    backend = serve(Backend({LW: index, AF: index}), "2026-07-21")
    rec = _go(store)
    assert backend.filters(LW) == backend.filters(AF) == [[K25]]  # no date filter, pass 1
    assert [p.get("facetFilters") for h, p in backend.calls] == [None, [["af:true"]]]
    assert {k: read_header(store / o["path"])["chain_id"] for k, o in rec.outputs.items()} == {
        "sources": "sources.forums", "coverage": "coverage.forums"}
    rows = _rows(store)
    assert sorted(rows) == [("alignmentforum", "xPOST"), ("lesswrong", "lwKEEP"),
                            ("lesswrong", "xPOST")]
    keep = rows[("lesswrong", "lwKEEP")]
    assert keep["identity"]["canonical_url"] == "https://www.lesswrong.com/posts/lwKEEP"
    assert keep["identity"]["keys"] == [{"kind": "fm_post", "value": "lwKEEP"}]
    md = keep["metadata"]
    assert (md["title"], md["doc_type"], md["published_at"]) == (
        "Post lwKEEP", "forum_post", _day("2023-05-01"))
    assert [a["name"] for a in md["authors"]] == ["Alice"]
    (entry,) = content_entries(keep)
    assert (entry["content_role"], entry["raw"], entry["text"]["media_type"]) == (
        "post", None, forums.SEARCH_BODY_MEDIA_TYPE)
    record = json_record(open_blobs(store), keep)  # D522: the hit without its body
    assert record == {k: v for k, v in index[0].items() if k != "body"}
    assert forums.record_payload({"_id": "x", "body": "b", "_highlightResult": {}, "_snippetResult": {}}) == {
        "_id": "x"}
    stored = open_blobs(store).get(entry["text"]["sha256"]).decode()
    assert entry["text"]["bytes"] == len(stored.encode())
    assert "\r" not in stored and "\n\n" in stored and "\n\n\n" not in stored
    lw_x, af_x = rows[("lesswrong", "xPOST")], rows[("alignmentforum", "xPOST")]
    assert lw_x["identity"]["keys"] == af_x["identity"]["keys"] == [
        {"kind": "fm_post", "value": "xPOST"}]
    assert lw_x["source_id"] != af_x["source_id"]  # two members; gather joins them on the key
    assert rec.counts["inband_minted"] == 1 and rec.counts["inband_absent"] == 2
    lw = _cov(store, "lesswrong")
    assert (lw["status"], lw["seen"], lw["kept"], _window(lw)) == (
        "ok", 2, 2, ("2020-01-01", "2026-07-21"))
    assert lw["query"].startswith("kw:")
    results = {r.check: r for r in run_checks(store)}
    assert {n: r.items for n, r in results.items() if r.status in ("failed", "warn")} == {}
    assert results["V-sources"].status == "ok" and results["V-coverage"].status == "ok"


@pytest.mark.slow
def test_resume_sends_filter_at_watermark_and_mints_no_known_id(stage: Any) -> None:
    """Run 2 (no flags) sends ``publicDateMs >= watermark − karma_lag_days`` (D406: 14 days) on
    both forums, processes only the new hit, writes part files and ``[that start, today]`` ok;
    run 3 the same day asks nothing (``already_covered``)."""
    store, serve = stage
    old, new = hit("lwOLD", "2023-05-01"), hit("lwNEW", "2026-07-22")
    serve(Backend({LW: [old]}), "2026-07-21")
    rec1 = _go(store)
    ids1 = {k: r["source_id"] for k, r in _rows(store).items()}
    backend = serve(Backend({LW: [old, new]}), "2026-07-24")
    rec2 = _go(store, kind="part")
    expect = _ge("2026-07-07")  # watermark 07-21 − 14 days (D406)
    assert backend.filters(LW) == [expect] and backend.filters(AF) == [expect]
    assert sorted(rec2.prior) == sorted(o["file_id"] for o in rec1.outputs.values())
    assert {k: read_header(store / o["path"])["kind"] for k, o in rec2.outputs.items()} == {
        "sources": "part", "coverage": "part"}
    assert rec2.counts["minted_ids"] == 1 and rec2.counts["windowed"] == 2
    lw = _cov(store, "lesswrong")
    assert (lw["status"], lw["seen"], lw["kept"], _window(lw)) == (
        "ok", 1, 1, ("2026-07-07", "2026-07-24"))
    rows = _rows(store)
    assert {k: rows[k]["source_id"] for k in ids1} == ids1
    backend3 = serve(Backend({LW: [old, new]}), "2026-07-24")
    rec3 = _go(store)
    assert backend3.calls == [] and rec3.counts["already_covered"] == 2


def test_ignored_filter_falls_back_to_floor_range(stage: Any) -> None:
    """A backend that IGNORES the filter returns a hit older than the watermark: honor-detection
    trips and the window claims ``[floor, today]``, never ``[watermark, today]``."""
    store, serve = stage
    old = hit("lwOLD", "2023-05-01")
    serve(Backend({LW: [old]}), "2026-07-21")
    _go(store)
    serve(Backend({LW: [old]}, honor=False), "2026-07-24")
    rec = _go(store)
    assert rec.counts["windowed"] == 2 and rec.counts["filter_ignored"] == 1  # only LW had hits
    lw = _cov(store, "lesswrong")
    assert (lw["status"], _window(lw)) == ("ok", ("2020-01-01", "2026-07-24"))


@pytest.mark.slow
def test_truncated_windowed_pass_is_partial_and_holds_the_watermark(stage: Any) -> None:
    """Search results are rank-ordered, so a capped windowed pass covered an unknown subset of its
    window: ``partial``, not exhausted; the next run sends the same lower bound again (the
    watermark − ``karma_lag_days``, D406)."""
    store, serve = stage
    old = hit("lwOLD", "2023-05-01")
    new = [hit(f"lwN{i}", "2026-07-22") for i in range(3)]
    serve(Backend({LW: [old]}), "2026-07-21")
    _go(store, max_records=2)
    serve(Backend({LW: [old, *new]}), "2026-07-24")
    rec = _go(store, max_records=2)
    lw = _cov(store, "lesswrong")
    assert (lw["status"], lw["is_exhausted"], _window(lw)) == (
        "partial", False, ("2026-07-07", "2026-07-24"))
    # D492: the cap-partial window leaves the run short, build reason `limit`
    assert rec.scope_complete is False and incomplete_reasons(rec.counts) == ["limit"]
    backend = serve(Backend({LW: [old, *new]}), "2026-07-25")
    _go(store, max_records=2)
    assert backend.filters(LW)[0] == _ge("2026-07-07")


def test_window_days_narrows_the_first_pass_server_side(stage: Any) -> None:
    """``window_days: 7`` (no ``from``): the first pass already sends the lower bound at today − 7
    and writes ``[today − 7, today]``; a hit older than that is never a row."""
    store, serve = stage
    backend = serve(Backend({LW: [hit("lwIN", "2026-07-20"), hit("lwOUT", "2026-07-01")]},
                            honor=False), "2026-07-24")
    rec = _go(store, **{"from": None, "window_days": 7})
    assert backend.filters(LW) == [_ge("2026-07-17")]
    assert sorted(_rows(store)) == [("lesswrong", "lwIN")]
    assert rec.counts["filter_ignored"] == 1  # the fake ignored it; the floor gate held
    assert _window(_cov(store, "lesswrong")) == ("2026-07-17", "2026-07-24")


def test_term_in_two_concept_groups_is_one_query(stage: Any, monkeypatch: Any) -> None:
    """``channels.yaml`` lists a term under two concept groups (live: "backdoor language models"):
    one search and one coverage window per forum, the repeat counted, the run ok."""
    store, serve = stage
    item = {"kind": "term", "term": "backdoor language models", "tag": None}
    monkeypatch.setattr(forums, "plan_queries", lambda config, chan, tags: [
        {**item, "topic_keys": ["group:a"]}, {**item, "topic_keys": ["group:b"]}])
    backend = serve(Backend({LW: [hit("lwB", "2026-07-20")]}), "2026-07-24")
    rec = _go(store, max_queries=0)
    assert len(backend.calls) == 2 and rec.counts["duplicate_queries"] == 2
    assert _cov(store, "lesswrong")["query"] == "kw:backdoor language models"


def test_af_flag_backstop_when_the_facet_is_dropped(stage: Any) -> None:
    """A backend that ignores the ``af:true`` facet returns LessWrong-only posts on the AF host:
    the hit's ``af`` flag keeps them off the ``alignmentforum`` channel (counted). The same
    backend ignores the karma floor it was sent: the client-side karma gate still holds."""
    store, serve = stage
    index = [hit("lwONLY", "2026-07-20"), hit("xPOST", "2026-07-20", af=True),
             hit("lwLOW", "2026-07-20", score=5, af=True)]
    backend = serve(Backend({LW: index, AF: index}, honor=False), "2026-07-24")
    rec = _go(store)
    assert backend.filters(LW) == backend.filters(AF) == [[K25]]
    assert sorted(_rows(store)) == [("alignmentforum", "xPOST"), ("lesswrong", "lwONLY"),
                                    ("lesswrong", "xPOST")]
    assert rec.counts["hits_not_af"] == 1


def test_min_karma_zero_sends_no_karma_clause(stage: Any) -> None:
    """``min_karma: 0`` turns the gate off on both sides: a first pass sends no ``numericFilters``
    at all and a low-karma hit is a row."""
    store, serve = stage
    backend = serve(Backend({LW: [hit("lwLOW", "2026-07-20", score=1)]}), "2026-07-24")
    _go(store, min_karma=0)
    assert backend.filters(LW) == backend.filters(AF) == [None]
    assert sorted(_rows(store)) == [("lesswrong", "lwLOW")]


def test_a_later_from_is_sent_on_the_first_pass(stage: Any) -> None:
    """B7a: a plan floor past the channel's `from_date` is the first pass's lower bound (not a
    whole-index scan) and the window is `[from, today]`; a hit before it never becomes a row."""
    store, serve = stage
    backend = serve(Backend({LW: [hit("lwIN", "2025-11-20"), hit("lwOUT", "2025-01-01")]}),
                    "2026-07-24")
    rec = _go(store, **{"from": "2025-10-01"})
    assert backend.filters(LW) == [_ge("2025-10-01")]
    assert rec.counts["windowed"] == 2 and sorted(_rows(store)) == [("lesswrong", "lwIN")]
    assert _window(_cov(store, "lesswrong")) == ("2025-10-01", "2026-07-24")


def test_a_server_page_cap_is_partial_not_complete(stage: Any) -> None:
    """B7a backstop: the backend stops paging (nbPages capped) while nbHits reports a page more:
    the pass is `partial` and counted `server_page_cap`, never an `ok` claim on the window."""
    store, serve = stage
    many = [hit(f"lw{i:03d}", "2026-07-20") for i in range(250)]
    serve(Backend({LW: many}, page_cap=1), "2026-07-24")
    rec = _go(store, max_records=1000)
    lw = _cov(store, "lesswrong")
    assert (lw["status"], lw["seen"]) == ("partial", 100)
    assert rec.counts["server_page_cap"] == 1 and rec.counts["queries_truncated"] == 1
    # D492: the provider's own cap (Algolia's page limit) is a cap: the run is short, reason `limit`
    assert rec.scope_complete is False and rec.counts["windows_capped"] == 1
    assert incomplete_reasons(rec.counts) == ["limit"]


@pytest.mark.slow
def test_the_result_window_400_is_a_server_page_cap(stage: Any) -> None:
    """D642: ForumMagnum's Elasticsearch answers a page past its 10,000-hit result window with an
    HTTP 400 (plain-text body). On a page after the first (same request, only the page number
    differs) that is the server's page cap: the hits so far are rows, the window `partial`,
    counted `server_page_cap` and `windows_capped`, build reason `limit`; not
    `failed_transient[network]` (what the 400 was filed as before)."""
    store, serve = stage
    many = [hit(f"lw{i:03d}", "2026-07-20") for i in range(250)]
    serve(Backend({LW: many}, result_window=200), "2026-07-24")
    rec = _go(store, max_records=1000)
    lw = _cov(store, "lesswrong")
    assert (lw["status"], lw["error_class"], lw["seen"]) == ("partial", None, 200)
    assert rec.counts["server_page_cap"] == 1 and rec.counts["queries_truncated"] == 1
    assert rec.counts["windows_capped"] == 1 and not rec.counts.get("windows_failed_transient")
    assert rec.scope_complete is False and incomplete_reasons(rec.counts) == ["limit"]


def _paged(pool: list[dict[str, Any]], calls: list[dict[str, Any]], *, first: int = 200) -> Any:
    """An ``http_post`` stub paging ``pool`` as the backend does (offset ``page × hitsPerPage``);
    ``first`` = the status of every page after page 0 when not 200."""
    def post(_url: str, payload: Any, **_kw: Any) -> tuple[int, Any]:
        params = payload[0]["params"]
        calls.append(params)
        page, size = int(params["page"]), int(params["hitsPerPage"])
        if page > 0 and first != 200:
            return first, None
        return 200, [{"hits": pool[page * size: (page + 1) * size],
                      "nbPages": (len(pool) + size - 1) // size, "nbHits": len(pool)}]
    return post


def test_page_size_is_constant_and_the_cap_applies_client_side(monkeypatch: Any) -> None:
    """The backend pages by ``page × hitsPerPage``: shrinking the last page (limit 150 → page 1 at
    size 50) would re-read hits 50-99 and never reach 100-149. Every page is asked at
    :data:`LW_PAGE_SIZE`, the cap is applied client-side, and a page carrying more than the room
    left marks the pass ``truncated``."""
    pool = [{"_id": f"p{i:03d}"} for i in range(250)]
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(forums, "http_post", _paged(pool, calls))
    hits, exhausted = forums.fetch_forum("https://lw.test", "q", None, 150)
    assert [h["_id"] for h in hits] == [f"p{i:03d}" for i in range(150)]
    assert exhausted == "truncated" and [c["hitsPerPage"] for c in calls] == [100, 100]
    calls.clear()  # a cap equal to the whole result: complete, not truncated
    hits, exhausted = forums.fetch_forum("https://lw.test", "q", None, 250)
    assert (len(hits), exhausted, [c["page"] for c in calls]) == (250, "complete", [0, 1, 2])


@pytest.mark.parametrize("status, page0, expect", [(400, False, "truncated"), (400, True, "error"),
                                                   (500, False, "error")],
                         ids=["400-later-page", "400-page-0", "500-later-page"])
def test_only_a_later_page_400_is_the_page_cap(monkeypatch: Any, status: int, page0: bool,
                                               expect: str) -> None:
    """A 400 on page 0 is a refused query (``failed_transient[network]`` as before), and a 5xx on a
    later page stays an error; only a 400 after a page of the same request answered is the cap."""
    pool = [{"_id": f"p{i:03d}"} for i in range(250)]
    calls: list[dict[str, Any]] = []
    post = _paged(pool, calls, first=status)
    monkeypatch.setattr(forums, "http_post", (lambda u, p, **k: (status, None)) if page0 else post)
    stats: dict[str, Any] = {}
    hits, exhausted = forums.fetch_forum("https://lw.test", "q", None, 1000, stats=stats)
    assert exhausted == expect and len(hits) == (0 if page0 else 100)
    assert bool(stats.get("server_page_cap")) == (expect == "truncated")
    if expect == "error":
        assert forums.error_class_of(int(stats["status"])) == (
            ("network", False) if status == 400 else ("http_5xx", False))


def test_floor_moved_back_asks_the_gap_and_cuts_at_its_end(stage: Any) -> None:
    """B7a, option C (D642): a first window from 2025-10-01, then `from: 2024-11-30` the same day:
    one request per forum carrying only the hole's UPPER bound (a lone `publicDateMs<` is honoured;
    a two-sided range is dropped server-side) and the karma floor; the server returns everything
    before the hole's end, so a hit before its start is cut client-side (`hits_outside_hole`); the
    window [2024-11-30, 2025-10-01] written; the covered tail is not asked; a third run asks
    nothing."""
    store, serve = stage
    index = [hit("lwPRE", "2024-06-01"), hit("lwOLD", "2025-02-03"), hit("lwNEW", "2025-11-04")]
    serve(Backend({LW: index}), "2026-07-24")
    _go(store, **{"from": "2025-10-01"})
    assert sorted(_rows(store)) == [("lesswrong", "lwNEW")]
    backend = serve(Backend({LW: index}), "2026-07-24")
    rec = _go(store, **{"from": "2024-11-30"})
    assert backend.filters(LW) == backend.filters(AF) == [[f"publicDateMs<{forums._ms('2025-10-01')}", K25]]
    assert rec.counts["holes"] == 2 and rec.counts["hits_outside_hole"] == 1  # LW and AF; lwPRE cut
    assert rec.counts["upper_bound_ignored"] == 0
    assert sorted(_rows(store)) == [("lesswrong", "lwNEW"), ("lesswrong", "lwOLD")]
    lw = sorted((r["window_start_at"].date().isoformat(), r["window_end_at"].date().isoformat(),
                 r["status"]) for r in read_view(resolve(store, sdir("coverage.forums"))).to_pylist()
                if r["channel"] == "lesswrong")
    assert lw == [("2024-11-30", "2025-10-01", "ok"), ("2025-10-01", "2026-07-24", "ok")]
    backend3 = serve(Backend({LW: index}), "2026-07-24")
    rec3 = _go(store, **{"from": "2024-11-30"})
    assert backend3.calls == [] and rec3.counts["already_covered"] == 2


@pytest.mark.parametrize("max_records, status", [(1000, "ok"), (2, "partial")], ids=["complete", "capped"])
def test_an_ignored_upper_bound_is_flagged(stage: Any, max_records: int, status: str) -> None:
    """Honour-detection on the hole's upper bound (D642): a backend that drops `publicDateMs<`
    returns hits after the hole's end; they are cut and the pass is counted
    `upper_bound_ignored`. What came back is then the whole index for the query, a superset of
    the hole, so a COMPLETE pass still covers the hole (as the lower-bound backstop, D-SWEEP1);
    a capped one stays `partial` and claims nothing."""
    store, serve = stage
    index = [hit("lwOLD", "2025-02-03"), hit("lwNEW", "2025-11-04"), hit("lwNEW2", "2025-12-04")]
    serve(Backend({LW: index}), "2026-07-24")
    _go(store, **{"from": "2025-10-01"})
    serve(Backend({LW: index}, honor_upper=False), "2026-07-24")
    rec = _go(store, **{"from": "2024-11-30", "max_records": max_records})
    assert rec.counts["upper_bound_ignored"] == 1  # LW only (AF returned nothing)
    hole = [r for r in read_view(resolve(store, sdir("coverage.forums"))).to_pylist()
            if r["channel"] == "lesswrong" and _window(r) == ("2024-11-30", "2025-10-01")]
    assert [r["status"] for r in hole] == [status]
    assert ("lesswrong", "lwOLD") in _rows(store) or status == "partial"


@pytest.mark.slow
def test_karma_lag_rescans_below_the_watermark_and_writes_no_duplicate_rows(stage: Any) -> None:
    """L3-14 (D406): a post under the karma floor at run 1 (posted the day before) and above it a
    week later is collected by run 2, which re-scans ``karma_lag_days`` (default 14) below the
    watermark: the filter goes out at watermark − 14 and the window is written from there. A post
    already collected and re-seen unchanged in the overlap writes no row (``sources_unchanged``);
    a third run the next day re-sees both and writes none."""
    store, serve = stage
    young, kept = hit("lwYOUNG", "2026-10-13", score=10), hit("lwKEPT", "2026-10-12", score=40)
    serve(Backend({LW: [young, kept]}), "2026-10-14")
    _go(store)
    assert sorted(_rows(store)) == [("lesswrong", "lwKEPT")]  # lwYOUNG under 25 karma today
    young60, kept45 = hit("lwYOUNG", "2026-10-13", score=60), hit("lwKEPT", "2026-10-12", score=45)
    backend = serve(Backend({LW: [young60, kept45]}), "2026-10-21")
    rec2 = _go(store)
    assert backend.filters(LW) == [_ge("2026-09-30")]  # 10-14 − 14
    assert sorted(_rows(store)) == [("lesswrong", "lwKEPT"), ("lesswrong", "lwYOUNG")]
    # lwKEPT re-seen unchanged (karma is not in record_projection): no row; lwYOUNG new
    assert (rec2.counts["sources_written"], rec2.counts["sources_unchanged"]) == (1, 1)
    lw = _cov(store, "lesswrong")
    assert (lw["status"], lw["seen"], lw["kept"], _window(lw)) == (
        "ok", 2, 1, ("2026-09-30", "2026-10-21"))
    serve(Backend({LW: [young60, kept45]}), "2026-10-22")
    rec3 = _go(store)
    assert rec3.counts.get("sources_written", 0) == 0 and rec3.counts["sources_unchanged"] == 2
    assert {k[1]: r["observed_at"] for k, r in _rows(store).items()} == {
        "lwKEPT": _day("2026-10-14") + dt.timedelta(hours=12),
        "lwYOUNG": _day("2026-10-21") + dt.timedelta(hours=12)}  # one row each over three runs
    backend4 = serve(Backend({LW: [young60, kept45]}), "2026-10-23")
    _go(store, karma_lag_days=0)  # lag 0: the resume at the watermark, as before D406
    assert backend4.filters(LW) == [_ge("2026-10-22")]


def _sampled_labels(cid: str, n: int = 1) -> list[str]:
    """The query labels ``_plan`` with ``max_queries: n`` and ``tags: []`` selects on forum ``cid``,
    computed as ``run`` does."""
    from stack.collect.sampling import select
    from stack.config import load

    config = load()
    full = forums.plan_queries(config, config.channels.channel(cid), ())
    plan, _ = select(full, forums._unit_key(cid), env={"STACK_COLLECT_MAX_QUERIES": str(n)})
    return [str(forums._query_label(item)) for item in plan]


def test_query_from_narrows_one_query_server_side(stage: Any) -> None:
    """D552: the backend takes no upper bound, so a broad query cannot be split by date; the
    operator narrows it with ``query_from``: that query's lower bound, client gate and first window
    start at its own floor on both forums; the other query keeps the stage floor."""
    store, serve = stage
    narrow, other = _sampled_labels("lesswrong", 2)
    assert set(_sampled_labels("alignmentforum", 2)) == {narrow, other}
    backend = serve(Backend({LW: [hit("lwNEW", "2026-07-10"), hit("lwOLD", "2026-05-01")]}),
                    "2026-07-24")
    rec = _go(store, **{"from": "2026-01-01", "max_queries": 2, "query_from": {narrow: "2026-07-01"}})
    sent = {(h, p["query"]): p["numericFilters"] for h, p in backend.calls}
    term = lambda label: label.split(":", 1)[1]
    for host in (LW, AF):
        assert sent[(host, term(narrow))] == _ge("2026-07-01")
        assert sent[(host, term(other))] == _ge("2026-01-01")
    # lwOLD came back only for the other query (the backend returns the pool for every query)
    assert sorted(_rows(store)) == [("lesswrong", "lwNEW"), ("lesswrong", "lwOLD")]
    cov = {(r["channel"], r["query"]): _window(r)
           for r in read_view(resolve(store, sdir("coverage.forums"))).to_pylist()}
    assert cov[("lesswrong", narrow)] == ("2026-07-01", "2026-07-24")
    assert cov[("lesswrong", other)] == ("2026-01-01", "2026-07-24")
    assert rec.counts["query_from_applied"] == 2  # one query on each forum


@pytest.mark.parametrize("query_from, why", [({"kw:no such term": "2026-07-01"}, "not query labels"),
                                             ("2026-07-01", "a mapping"),
                                             (None, "before the floor")], ids=["label", "shape", "early"])
def test_query_from_refuses_unknown_labels_and_early_dates(stage: Any, query_from: Any, why: str) -> None:
    """D552: a ``query_from`` label the plan does not ask, a non-mapping or a date before the
    stage's floor fails the run (a silently ignored narrowing would claim coverage it did not)."""
    store, serve = stage
    serve(Backend({LW: [], AF: []}), "2026-07-24")
    if query_from is None:
        query_from = {_sampled_labels("lesswrong")[0]: "2025-06-01"}
    rec = run_line(store, _plan(**{"from": "2026-01-01", "query_from": query_from}), "collect.forums")
    assert rec.status == "failed" and why in (rec.error or "")


def test_llm_safety_group_asks_no_forum_query() -> None:
    """The Chinese-coverage work: `llm-safety` has `forum_terms: []` (LessWrong's loose search returns 1,148-21,963 posts per phrase),
    so neither forum gains a label; every other group's terms are asked as before."""
    from stack.config import load

    config = load()
    want = {f"kw:{t}" for g in config.channels.concept_groups if g.id != "llm-safety" for t in g.terms}
    for cid in forums.FORUM_CHANNELS:
        plan = forums.plan_queries(config, config.channels.channel(cid), ())
        labels = {str(forums._query_label(i)) for i in plan if i["kind"] == "term"}
        assert not any("group:llm-safety" in i["topic_keys"] for i in plan)
        assert want <= labels and "kw:honesty" not in labels
