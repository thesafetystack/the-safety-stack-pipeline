"""Tests for ``collect.lw_graphql``: every LessWrong post under the AI tag via ForumMagnum GraphQL,
no karma gate. No network: a fake GraphQL server models ``posts(view:"new")`` with the tag
``filterSettings`` (``Required``), newest-first order, the ``after`` date bound and
``limit``/``offset`` paging; it can be told to ignore the tag filter (the backstop case).

(A) Pure: the query shape; the tag-id guard; descending paging → ``complete``/``truncated``;
karma and tag relevance stay out of ``record_digest`` under the channel's projection while a body
edit does not. (B) Stage tests through ``stack.stage_runner.run_stage`` on a temp store: the tag
backstop drops untagged posts (``hits_not_tagged``); a karma-0 post is registered and its karma
lands in the ``provider_record``; a cap-truncated pass is ``partial``, a full pass ``ok``; the
overlap re-scan (``watermark − tag_lag_days``) writes no row for an unchanged post and picks up a
late-tagged one; run 2 over the same pages mints no ``source_id``; V1/V2 pass.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import pathlib
import re
from collections.abc import Mapping
from typing import Any

import pyarrow.parquet as pq
import pytest

from stack.build import incomplete_reasons
from stack import stage_runner
from stack.checks.verify import run_checks
from stack.collect.base import record_digest
from stack.config import load
from stack.http import HttpResponse
from stack.plan import Plan
from tests.synth.records import run_line, sdir
from stack.collect.inband import json_record
from stack.store.blobs import open_blobs
from stack.store.chain import read_view, resolve
from tests.synth.rows import content_entries

REPO = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "_collect_lw_graphql_under_test", REPO / "workflows" / "collect" / "lw_graphql" / "main.py")
assert _spec is not None and _spec.loader is not None
lwg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lwg)

AI = lwg.AI_TAG_ID
OTHER_TAG = "zzzzOtherTagzzzzz"
_BODY = ("<p>A LessWrong post body under the AI tag, long enough to clear the in-band noise "
         "floor.</p>\n<p>Second paragraph.</p>")


# ─────────────────────────────────────────────────────────────────────────────
# fake GraphQL server
# ─────────────────────────────────────────────────────────────────────────────
def _post(_id: str, *, posted: str, score: int = 50, tags: Mapping[str, int] | None = None,
          title: str = "T", body: str | None = _BODY) -> dict[str, Any]:
    p: dict[str, Any] = {
        "_id": _id, "title": title, "postedAt": f"{posted}T12:00:00.000Z", "baseScore": score,
        "voteCount": 3, "commentCount": 1, "af": False, "url": None, "slug": f"slug-{_id}",
        "tagRelevance": dict(tags if tags is not None else {AI: 2}),
        "user": {"displayName": "Alice", "username": "alice"}, "coauthors": [],
    }
    if body is not None:
        p["htmlBody"] = body
    return p


def _model(posts: list[dict[str, Any]], *, honor_filter: bool = True,
           captured: list[dict[str, Any]] | None = None, honor_before: bool = True,
           fail_after: str | None = None, max_skip: int | None = None) -> Any:
    ordered = sorted(posts, key=lambda p: str(p["postedAt"]), reverse=True)

    def serve(url: str, payload: Any) -> tuple[int, Any]:
        q = str(payload["query"])
        limit = int(re.search(r"limit:\s*(\d+)", q).group(1))  # type: ignore[union-attr]
        offset = int(re.search(r"offset:\s*(\d+)", q).group(1))  # type: ignore[union-attr]
        after = re.search(r'after:\s*"([^"]+)"', q).group(1)  # type: ignore[union-attr]
        tag = re.search(r'tagId:\s*"([^"]+)",\s*filterMode:\s*"Required"', q)
        before = re.search(r'before:\s*"([^"]+)"', q)
        if captured is not None:
            captured.append({"url": url, "limit": limit, "offset": offset, "after": after,
                             "tag": tag.group(1) if tag else None,
                             "before": before.group(1) if before else None})
        if after == fail_after:
            return 503, None
        if max_skip is not None and offset > max_skip:  # the live skip limit (D645)
            return 200, {"errors": [{"message": "Exceeded maximum value for skip"}],
                         "data": {"posts": None}}
        pool = [p for p in ordered if str(p["postedAt"])[:10] >= after]
        if before is not None and honor_before:  # inclusive day, as ForumMagnum's endOf('day')
            pool = [p for p in pool if str(p["postedAt"])[:10] <= before.group(1)]
        if honor_filter and tag:
            pool = [p for p in pool if tag.group(1) in p["tagRelevance"]]
        return 200, {"data": {"posts": {"results": pool[offset:offset + limit]}}}

    return serve


def _sender(model: Any) -> Any:
    def send(method: str, url: str, *, headers: Mapping[str, str], body: bytes | None,
             timeout: float, max_bytes: int) -> HttpResponse:
        assert method == "POST" and body is not None
        status, data = model(url, json.loads(body))
        return HttpResponse(status, {}, json.dumps(data).encode(), url)

    return send


# ─────────────────────────────────────────────────────────────────────────────
# (A) pure
# ─────────────────────────────────────────────────────────────────────────────
def test_query_shape_requires_the_tag_and_selects_counters_and_tags() -> None:
    q = lwg.build_posts_query(AI, "2026-10-05", 100, 50)
    assert 'view: "new"' in q and 'after: "2026-10-05"' in q
    assert "limit: 50" in q and "offset: 100" in q
    assert f'filterSettings: {{tags: [{{tagId: "{AI}", filterMode: "Required"}}]}}' in q
    for f in ("tagRelevance", "baseScore", "voteCount", "commentCount", "af", "htmlBody"):
        assert re.search(rf"\b{f}\b", q), f
    assert q.count("{") == q.count("}")


def test_tag_id_guard_rejects_injection() -> None:
    assert lwg.as_tag_id(AI, source="x") == AI
    with pytest.raises(ValueError, match="opt-T"):
        lwg.as_tag_id('abc"}]} injected', source="opt-T")


def test_paging_complete_and_truncated(monkeypatch: Any) -> None:
    monkeypatch.setattr(lwg, "PAGE_SIZE", 2)
    posts = [_post(f"p{i}", posted=f"2026-10-0{i + 1}") for i in range(5)]
    cap: list[dict[str, Any]] = []
    model = _model(posts, captured=cap)
    monkeypatch.setattr(lwg, "http_post", lambda url, payload, **_k: model(url, payload))
    got, exhausted = lwg.fetch_tagged("https://lw.test", AI, "2026-01-01", 100)
    assert (exhausted, len(got)) == ("complete", 5)
    assert [c["offset"] for c in cap] == [0, 2, 4] and {c["tag"] for c in cap} == {AI}
    got, exhausted = lwg.fetch_tagged("https://lw.test", AI, "2026-01-01", 3)
    assert (exhausted, [p["_id"] for p in got]) == ("truncated", ["p4", "p3", "p2"])


def test_karma_and_tags_stay_out_of_record_digest_body_does_not() -> None:
    """C23 under the channel's projection (configs/channels.yaml `lesswrong`): a karma, vote or
    tag-relevance change is not a record change; a body edit is."""
    proj = next(c for c in load().channels.channels if c.id == "lesswrong").record_projection
    a = _post("x", posted="2026-10-10", score=3)
    b = {**a, "baseScore": 99, "voteCount": 40, "commentCount": 9, "tagRelevance": {AI: 7}}
    assert record_digest(a, proj) == record_digest(b, proj)
    assert record_digest(a, proj) != record_digest({**a, "htmlBody": _BODY + "<p>edit</p>"}, proj)


# ─────────────────────────────────────────────────────────────────────────────
# (B) the stage, through the runner on a temp store
# ─────────────────────────────────────────────────────────────────────────────
def _plan(**options: Any) -> Plan:
    return Plan.model_validate({"plan": "test", "schema_version": 2, "stages": [
        {"stage": "collect.lw_graphql", "prior": ["sources", "coverage"], "options": options}]})


@pytest.fixture
def stage(tmp_path: pathlib.Path, monkeypatch: Any) -> Any:
    monkeypatch.delenv("STACK_COLLECT_LIMIT", raising=False)
    monkeypatch.setattr(stage_runner, "load_stage_module", lambda name: lwg)
    monkeypatch.setattr(lwg, "_sleep", lambda s: None)

    def serve(posts: list[dict[str, Any]], today: str, *, honor_filter: bool = True,
              captured: list[dict[str, Any]] | None = None, **model_kw: Any) -> None:
        monkeypatch.setattr(lwg, "_as_of_date", lambda: today)
        # the harness clock on the same day (coverage stamps, the D226 pass rule)
        monkeypatch.setattr("stack.collect.base.utcnow", lambda: dt.datetime.fromisoformat(
            today).replace(tzinfo=dt.UTC) + dt.timedelta(hours=12))
        monkeypatch.setattr("stack.collect.base.urllib_send",
                            _sender(_model(posts, honor_filter=honor_filter, captured=captured,
                                           **model_kw)))

    return tmp_path / "store", serve


def _rows(store: pathlib.Path) -> dict[str, dict[str, Any]]:
    return {r["identity"]["external_id"]: r
            for r in read_view(resolve(store, sdir("sources.lw_graphql"))).to_pylist()}


def _cov(store: pathlib.Path) -> list[dict[str, Any]]:
    return sorted(read_view(resolve(store, sdir("coverage.lw_graphql"))).to_pylist(),
                  key=lambda r: (r["window_end_at"], r["window_start_at"]))


def _file_rows(store: pathlib.Path, rec: Any, family: str) -> list[dict[str, Any]]:
    return pq.read_table(store / rec.outputs[family]["path"]).to_pylist()


def _day(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s).replace(tzinfo=dt.UTC)


def _checks_clean(store: pathlib.Path) -> None:
    results = {r.check: r for r in run_checks(store)}
    assert {n: r.items for n, r in results.items() if r.status in ("failed", "warn")} == {}
    assert results["V-sources"].status == "ok" and results["V-coverage"].status == "ok"


def test_tag_backstop_no_karma_gate_karma_in_provider_record(stage: Any) -> None:
    """A server ignoring the tag filter returns an untagged and a zero-relevance post: both are
    dropped and counted; a karma-0 tagged post is registered (no gate) with its karma, votes and
    tag ids in the ``provider_record`` (no ``htmlBody`` there) and its body in-band."""
    store, serve = stage
    cap: list[dict[str, Any]] = []
    serve([_post("lwZERO", posted="2026-10-10", score=0, tags={AI: 1, OTHER_TAG: 3}),
           _post("lwHI", posted="2026-10-09", score=120),
           _post("lwOFF", posted="2026-10-08", tags={OTHER_TAG: 5}),
           _post("lwNEG", posted="2026-10-07", tags={AI: 0})],
          "2026-10-12", honor_filter=False, captured=cap)
    rec = run_line(store, _plan(window_days=7), "collect.lw_graphql")
    assert rec.status == "ok", rec.error
    assert cap and all(c["tag"] == AI and c["after"] == "2026-10-05" for c in cap)
    assert (rec.counts["hits_not_tagged"], rec.counts["posts_seen"]) == (2, 2)
    rows = _rows(store)
    assert sorted(rows) == ["lwHI", "lwZERO"]
    z = rows["lwZERO"]
    assert (z["identity"]["channel"], z["identity"]["canonical_url"], z["identity"]["keys"]) == (
        "lesswrong", "https://www.lesswrong.com/posts/lwZERO", [{"kind": "fm_post", "value": "lwZERO"}])
    blobs = open_blobs(store)
    payload = json_record(blobs, z)
    assert (payload["baseScore"], payload["voteCount"], payload["tagRelevance"]) == (
        0, 3, {AI: 1, OTHER_TAG: 3})
    assert "htmlBody" not in payload and payload["_id"] == "lwZERO"
    (entry,) = content_entries(z)
    assert (entry["content_role"], entry["raw"]["media_type"], entry["text"]) == ("post", "text/html", None)
    assert blobs.get(entry["raw"]["sha256"]).decode() == _BODY
    assert entry["raw"]["bytes"] == len(_BODY.encode())
    (cov,) = _cov(store)
    assert (cov["query"], cov["status"], cov["seen"], cov["kept"]) == ("tag:ai", "ok", 2, 2)
    assert (cov["window_start_at"], cov["window_end_at"]) == (_day("2026-10-05"), _day("2026-10-12"))
    _checks_clean(store)


def test_cap_is_partial_full_pass_is_ok(stage: Any, monkeypatch: Any) -> None:
    """Descending pages: a cap-truncated pass writes ``partial`` and leaves the watermark; the next
    full pass (``relist_after_h: 0``: by default a ``partial`` tag is not asked again within 12 h,
    D226) starts at the floor again and writes ``ok``."""
    store, serve = stage
    monkeypatch.setattr(lwg, "PAGE_SIZE", 2)
    posts = [_post(f"lw{i}", posted=f"2026-10-0{i + 2}") for i in range(5)]
    serve(posts, "2026-10-12")
    rec = run_line(store, _plan(**{"from": "2026-09-01", "max_records": 3}), "collect.lw_graphql")
    assert rec.status == "ok", rec.error
    (cov,) = _cov(store)
    assert (cov["status"], cov["is_exhausted"], cov["seen"]) == ("partial", False, 3)
    assert rec.scope_complete is False and incomplete_reasons(rec.counts) == ["limit"]  # D492
    cap: list[dict[str, Any]] = []
    serve(posts, "2026-10-12", captured=cap)
    rec2 = run_line(store, _plan(**{"from": "2026-09-01", "relist_after_h": 0}), "collect.lw_graphql")
    assert rec2.status == "ok", rec2.error
    assert rec2.scope_complete is True  # D492: the uncapped pass completes the window
    assert cap and all(c["after"] == "2026-09-01" for c in cap)
    assert [c["offset"] for c in cap] == [0, 2, 4]
    (cov,) = _cov(store)  # the same window key, now ok
    assert (cov["status"], cov["is_exhausted"], cov["seen"], cov["kept"]) == ("ok", True, 5, 2)
    assert len(_rows(store)) == 5


@pytest.mark.slow
def test_overlap_rescan_writes_no_duplicate_and_finds_late_tagged(stage: Any) -> None:
    """Run 2, three days after run 1's ``ok`` window, sends ``after = watermark − tag_lag_days``:
    the unchanged posts it re-sees write no row, a post tagged after run 1 (dated before run 1's
    end) is registered, an edited post writes a new row; no new id for a known post."""
    store, serve = stage
    old = _post("lwOLD", posted="2026-10-08")
    edited = _post("lwEDIT", posted="2026-10-09")
    late = _post("lwLATE", posted="2026-10-10", tags={OTHER_TAG: 1})
    serve([old, edited, late], "2026-10-12")
    rec1 = run_line(store, _plan(**{"from": "2026-09-01", "tag_lag_days": 5}), "collect.lw_graphql")
    assert rec1.status == "ok", rec1.error
    ids1 = {k: r["source_id"] for k, r in _rows(store).items()}
    assert sorted(ids1) == ["lwEDIT", "lwOLD"]

    cap: list[dict[str, Any]] = []
    late_tagged = {**late, "tagRelevance": {OTHER_TAG: 1, AI: 1}}
    serve([old, {**edited, "baseScore": 80, "htmlBody": _BODY + "<p>edit</p>"}, late_tagged,
           _post("lwNEW", posted="2026-10-14")], "2026-10-15", captured=cap)
    rec2 = run_line(store, _plan(**{"from": "2026-09-01", "tag_lag_days": 5}), "collect.lw_graphql",
                    kind="part")
    assert rec2.status == "ok", rec2.error
    assert cap and all(c["after"] == "2026-10-07" for c in cap)  # 2026-10-12 − 5 days
    written = sorted(r["identity"]["external_id"] for r in _file_rows(store, rec2, "sources"))
    assert written == ["lwEDIT", "lwLATE", "lwNEW"]  # lwOLD unchanged: no row
    assert rec2.counts["sources_unchanged"] == 1 and rec2.counts["minted_ids"] == 2
    rows = _rows(store)
    assert {k: rows[k]["source_id"] for k in ids1} == ids1
    payload = json_record(open_blobs(store), rows["lwEDIT"])
    assert payload["baseScore"] == 80
    last = _cov(store)[-1]
    assert (last["window_start_at"], last["window_end_at"], last["status"]) == (
        _day("2026-10-07"), _day("2026-10-15"), "ok")
    assert (last["seen"], last["kept"]) == (4, 2)
    _checks_clean(store)


@pytest.mark.slow
def test_end_to_end_rerun_mints_no_source_id(stage: Any) -> None:
    """Run 1 writes full files on both chains; run 2 (no flags: the stage line pins the own
    chains) the same day asks nothing (the tag is covered through today, D226), mints nothing and
    writes no file; the checks pass."""
    store, serve = stage
    posts = [_post("lwA", posted="2026-10-10"), _post("lwB", posted="2026-10-11", score=2)]
    serve(posts, "2026-10-12")
    rec1 = run_line(store, _plan(window_days=7), "collect.lw_graphql")
    assert rec1.status == "ok", rec1.error
    assert set(rec1.outputs) == {"sources", "coverage"} and rec1.counts["minted_ids"] == 2
    ids1 = {k: r["source_id"] for k, r in _rows(store).items()}
    serve(posts, "2026-10-12")
    rec2 = run_line(store, _plan(window_days=7), "collect.lw_graphql")
    assert rec2.status == "ok", rec2.error
    assert rec2.counts["minted_ids"] == 0
    assert (rec2.counts["already_covered"], rec2.counts["http_requests"]) == (1, 0)
    assert rec2.outputs == {}
    assert {k: r["source_id"] for k, r in _rows(store).items()} == ids1
    _checks_clean(store)


# ─────────────────────────────────────────────────────────────────────────────
# (C) a list of tags (option `tags`)
# ─────────────────────────────────────────────────────────────────────────────
DT = lwg.TAG_IDS["decision-theory"]


def test_unknown_tag_slug_fails_before_any_request() -> None:
    with pytest.raises(ValueError, match="no-such-tag"):
        lwg.resolve_tags(["ai", "no-such-tag"], {})
    assert lwg.resolve_tags(["ai", "decision-theory", "ai"], {}) == [("ai", AI), ("decision-theory", DT)]
    assert lwg.resolve_tags(["x"], {"x": "abcXYZ123"}) == [("x", "abcXYZ123")]
    assert "ai" not in lwg.SR_EXTENSION_TAGS and "decision-theory" in lwg.SR_EXTENSION_TAGS


@pytest.mark.slow
def test_one_coverage_query_per_tag_with_its_own_watermark(stage: Any) -> None:
    """Two tags → two coverage rows ``tag:ai`` / ``tag:decision-theory`` with their own seen
    counts; each request carries its tag; run 2 resumes each tag from its own watermark."""
    store, serve = stage
    posts = [_post("lwA", posted="2026-10-10", tags={AI: 1}),
             _post("lwD1", posted="2026-10-09", tags={DT: 2}),
             _post("lwD2", posted="2026-10-08", tags={DT: 1})]
    cap: list[dict[str, Any]] = []
    serve(posts, "2026-10-12", captured=cap)
    rec = run_line(store, _plan(**{"from": "2026-09-01", "tags": ["ai", "decision-theory"]}),
                    "collect.lw_graphql")
    assert rec.status == "ok", rec.error
    assert [c["tag"] for c in cap] == [AI, DT]
    cov = {r["query"]: r for r in _cov(store)}
    assert {q: (r["status"], r["seen"], r["kept"]) for q, r in cov.items()} == {
        "tag:ai": ("ok", 1, 1), "tag:decision-theory": ("ok", 2, 2)}
    assert sorted(_rows(store)) == ["lwA", "lwD1", "lwD2"]
    cap2: list[dict[str, Any]] = []
    serve(posts, "2026-10-20", captured=cap2)
    rec2 = run_line(store, _plan(**{"from": "2026-09-01", "tags": ["decision-theory"],
                                     "tag_lag_days": 3}), "collect.lw_graphql")
    assert rec2.status == "ok", rec2.error
    assert [(c["tag"], c["after"]) for c in cap2] == [(DT, "2026-10-09")]
    _checks_clean(store)


def test_backstop_is_per_queried_tag(stage: Any) -> None:
    """A server ignoring the filter serves every post to every query: the AI query drops the
    decision-theory-only post and the decision-theory query drops the AI-only post; each post is
    registered once, under the tag it carries."""
    store, serve = stage
    serve([_post("lwA", posted="2026-10-10", tags={AI: 1}),
           _post("lwD", posted="2026-10-09", tags={DT: 1})], "2026-10-12", honor_filter=False)
    rec = run_line(store, _plan(**{"from": "2026-09-01", "tags": ["ai", "decision-theory"]}),
                    "collect.lw_graphql")
    assert rec.status == "ok", rec.error
    assert rec.counts["hits_not_tagged"] == 2 and rec.counts["posts_multi_tag"] == 0
    cov = {r["query"]: (r["seen"], r["kept"]) for r in _cov(store)}
    assert cov == {"tag:ai": (1, 1), "tag:decision-theory": (1, 1)}
    assert sorted(_rows(store)) == ["lwA", "lwD"]


def test_post_under_two_tags_is_one_source(stage: Any) -> None:
    """A post carrying both tags is seen by both queries (both windows count it) but observed
    once: one row, one minted id, ``posts_multi_tag`` 1, no harness repeat."""
    store, serve = stage
    serve([_post("lwBOTH", posted="2026-10-10", tags={AI: 1, DT: 3}),
           _post("lwD", posted="2026-10-09", tags={DT: 1})], "2026-10-12")
    rec = run_line(store, _plan(**{"from": "2026-09-01", "tags": ["ai", "decision-theory"]}),
                    "collect.lw_graphql")
    assert rec.status == "ok", rec.error
    assert (rec.counts["posts_multi_tag"], rec.counts["minted_ids"], rec.counts["posts_seen"]) == (1, 2, 2)
    assert rec.counts.get("sources_repeat", 0) == 0
    assert len(_file_rows(store, rec, "sources")) == 2
    cov = {r["query"]: (r["seen"], r["kept"]) for r in _cov(store)}
    assert cov == {"tag:ai": (1, 1), "tag:decision-theory": (2, 1)}
    _checks_clean(store)


def test_linkpost_target_is_an_extra_url_key(stage: Any) -> None:
    """Round 5, T-ID: ``Collector.observe`` runs ``stack.identity``'s key adders; a linkpost's ``url``
    (a blog post) becomes a ``url`` key beside ``fm_post``; a plain post and a linkpost to a paper get none."""
    store, serve = stage
    link = _post("lwLINK", posted="2026-10-10")
    link["url"] = "https://blog.redwoodresearch.org/p/some-post?utm_source=lw"
    paper = _post("lwPAPER", posted="2026-10-09")
    paper["url"] = "https://arxiv.org/abs/2610.00001"
    serve([link, paper, _post("lwPLAIN", posted="2026-10-08")], "2026-10-12")
    rec = run_line(store, _plan(window_days=7), "collect.lw_graphql")
    assert rec.status == "ok", rec.error
    keys = {e: [(k["kind"], k["value"]) for k in r["identity"]["keys"]] for e, r in _rows(store).items()}
    assert keys == {"lwLINK": [("fm_post", "lwLINK"), ("url", "https://blog.redwoodresearch.org/p/some-post")],
                    "lwPAPER": [("fm_post", "lwPAPER")], "lwPLAIN": [("fm_post", "lwPLAIN")]}


# ─────────────────────────────────────────────────────────────────────────────
# (C) date sub-windows (option subwindow_days; round 5 (RUN) B7a)
# ─────────────────────────────────────────────────────────────────────────────
SPAN_POSTS = [_post("lwA", posted="2026-09-02"), _post("lwB", posted="2026-09-11"),
              _post("lwC", posted="2026-09-25"), _post("lwD", posted="2026-10-11")]


def test_query_carries_before_only_when_given() -> None:
    assert "before:" not in lwg.build_posts_query(AI, "2026-10-05", 0, 5)
    q = lwg.build_posts_query(AI, "2026-10-05", 0, 5, "2026-10-20")
    assert 'after: "2026-10-05", before: "2026-10-20", ' in q and q.count("{") == q.count("}")


def test_subwindows_bank_each_span_and_cut_posts_to_their_span(stage: Any) -> None:
    """[2026-09-01, 2026-10-12] in 10-day spans: one `ok` coverage row per span, oldest first;
    every post once, in its own span (the day-wide slack returns neighbours, which are cut and
    counted); bounds sent one day wider; the last span has no `before`."""
    from stack.collect.base import date_windows

    store, serve = stage
    cap: list[dict[str, Any]] = []
    serve(SPAN_POSTS, "2026-10-12", captured=cap)
    rec = run_line(store, _plan(**{"from": "2026-09-01", "subwindow_days": 10}),
                    "collect.lw_graphql")
    assert rec.status == "ok", rec.error
    spans = date_windows(dt.date(2026, 9, 1), dt.date(2026, 10, 12), 10)
    assert len(spans) == 5 and rec.counts["spans"] == 5
    cov = _cov(store)
    assert [(r["window_start_at"].date(), r["window_end_at"].date(), r["status"]) for r in cov] == [
        (s, e, "ok") for s, e in spans]
    assert [r["seen"] for r in cov] == [1, 1, 1, 0, 1]  # lwB (09-11) is the second span's first day
    assert sorted(_rows(store)) == ["lwA", "lwB", "lwC", "lwD"]
    assert [(c["after"], c["before"]) for c in cap] == [
        ("2026-08-31", "2026-09-12"), ("2026-09-10", "2026-09-22"), ("2026-09-20", "2026-10-02"),
        ("2026-09-30", "2026-10-12"), ("2026-10-10", None)]
    assert rec.counts["posts_outside_span"] == 2 and rec.counts["before_ignored"] == 0  # lwB, lwD
    _checks_clean(store)


def test_subwindows_stop_at_a_failed_span_and_resume_there(stage: Any) -> None:
    """The second span fails: the first is `ok`, the second `failed_transient`, the rest are not
    asked (a later `ok` span would carry the watermark over the hole); the next run resumes at the
    second span (tag_lag_days 0) and completes."""
    store, serve = stage
    cap: list[dict[str, Any]] = []
    serve(SPAN_POSTS, "2026-10-12", captured=cap, fail_after="2026-09-10")
    opts = {"from": "2026-09-01", "subwindow_days": 10, "tag_lag_days": 0}
    rec = run_line(store, _plan(**opts), "collect.lw_graphql")
    assert rec.status == "ok", rec.error
    assert rec.scope_complete is False  # R3-2: the failed span is retried by the next run
    cov = _cov(store)
    assert [(r["window_start_at"].date().isoformat(), r["status"]) for r in cov] == [
        ("2026-09-01", "ok"), ("2026-09-11", "failed_transient")]
    assert [c["after"] for c in cap] == ["2026-08-31", "2026-09-10"]
    assert sorted(_rows(store)) == ["lwA"]
    cap.clear()
    serve(SPAN_POSTS, "2026-10-12", captured=cap)
    rec2 = run_line(store, _plan(**opts), "collect.lw_graphql")
    assert rec2.status == "ok", rec2.error
    assert cap[0]["after"] == "2026-09-10"  # the watermark 09-11 (first span's end) − 1 day slack
    assert sorted(_rows(store)) == ["lwA", "lwB", "lwC", "lwD"]
    assert {r["status"] for r in _cov(store) if r["window_start_at"] >= _day("2026-09-11")} == {"ok"}


@pytest.mark.slow
def test_subwindows_count_a_server_ignoring_before(stage: Any) -> None:
    """A server that drops `before` returns every newer post in each early span: they are cut and
    counted `before_ignored`; with room under the cap the result is still each post once."""
    store, serve = stage
    serve(SPAN_POSTS, "2026-10-12", honor_before=False)
    rec = run_line(store, _plan(**{"from": "2026-09-01", "subwindow_days": 10}),
                    "collect.lw_graphql")
    assert rec.status == "ok", rec.error
    assert rec.counts["before_ignored"] > 0
    assert sorted(_rows(store)) == ["lwA", "lwB", "lwC", "lwD"]
    assert {r["status"] for r in _cov(store)} == {"ok"}


def test_floor_moved_back_lists_only_the_gap(stage: Any) -> None:
    """B7a: a first window from 2025-10-01, then `from: 2024-11-30` the same day: the only request
    is the hole [2024-11-30, 2025-10-01] (bounds one day wider, `before` sent); the post in it is
    registered, the covered range is not listed again; a third run asks nothing."""
    store, serve = stage
    posts = [_post("lwOLD", posted="2025-02-03"), _post("lwNEW", posted="2025-11-04")]
    cap: list[dict[str, Any]] = []
    serve(posts, "2026-10-12", captured=cap)
    rec1 = run_line(store, _plan(**{"from": "2025-10-01"}), "collect.lw_graphql")
    assert rec1.status == "ok", rec1.error
    assert sorted(_rows(store)) == ["lwNEW"]
    cap.clear()
    rec2 = run_line(store, _plan(**{"from": "2024-11-30"}), "collect.lw_graphql")
    assert rec2.status == "ok", rec2.error
    assert [(c["after"], c["before"]) for c in cap] == [("2024-11-29", "2025-10-02")]
    assert rec2.counts["holes"] == 1 and sorted(_rows(store)) == ["lwNEW", "lwOLD"]
    assert [(r["window_start_at"], r["window_end_at"], r["status"]) for r in _cov(store)] == [
        (_day("2024-11-30"), _day("2025-10-01"), "ok"), (_day("2025-10-01"), _day("2026-10-12"), "ok")]
    cap.clear()
    rec3 = run_line(store, _plan(**{"from": "2024-11-30"}), "collect.lw_graphql")
    assert rec3.status == "ok" and cap == [] and rec3.counts["already_covered"] == 1


def test_a_hole_is_split_by_subwindow_days(stage: Any) -> None:
    """B7a: with `subwindow_days`, the hole [2024-11-30, 2025-10-01] is listed as 120-day spans,
    each its own `ok` window, like a first window."""
    from stack.collect.base import date_windows

    store, serve = stage
    posts = [_post("lwOLD", posted="2025-02-03"), _post("lwNEW", posted="2025-11-04")]
    serve(posts, "2026-10-12")
    opts = {"subwindow_days": 120}
    assert run_line(store, _plan(**opts, **{"from": "2025-10-01"}), "collect.lw_graphql").status == "ok"
    cap: list[dict[str, Any]] = []
    serve(posts, "2026-10-12", captured=cap)
    rec = run_line(store, _plan(**opts, **{"from": "2024-11-30"}), "collect.lw_graphql")
    assert rec.status == "ok", rec.error
    spans = date_windows(dt.date(2024, 11, 30), dt.date(2025, 10, 1), 120)
    assert len(spans) == 3 and len(cap) == 3 and sorted(_rows(store)) == ["lwNEW", "lwOLD"]
    got = [(r["window_start_at"].date(), r["window_end_at"].date()) for r in _cov(store)
           if r["window_end_at"] <= _day("2025-10-01")]
    assert sorted(got) == spans


def test_skip_refusal_is_a_capped_partial_not_parse_error(stage: Any, monkeypatch: Any) -> None:
    """D645 (sibling of af_graphql's fix): ForumMagnum's "Exceeded maximum value for skip" on a
    later page is the server's paging cap: the window is `partial` (`windows_capped`, build reason
    `limit`, counted `skip_capped`) with the posts reached as rows, not
    `failed_permanent[parse_error]`. (lw_graphql's spans are 30 days in the plan, far under the
    limit; it does not split.)"""
    store, serve = stage
    monkeypatch.setattr(lwg, "PAGE_SIZE", 2)
    posts = [_post(f"lw{i}", posted=f"2026-10-0{i + 1}") for i in range(8)]
    serve(posts, "2026-10-12", max_skip=4)
    rec = run_line(store, _plan(**{"from": "2026-09-01"}), "collect.lw_graphql")
    assert rec.status == "ok", rec.error
    (cov,) = _cov(store)
    assert (cov["status"], cov["is_exhausted"], cov["seen"]) == ("partial", False, 6)
    assert rec.scope_complete is False and incomplete_reasons(rec.counts) == ["limit"]
    assert rec.counts["skip_capped"] == 1
    assert not any(k.startswith("windows_failed_permanent") for k in rec.counts)
