"""Tests for ``collect.af_graphql`` — the complete Alignment Forum corpus via ForumMagnum GraphQL
(§5.6 LW/AF row). No network: a fake GraphQL server models ``posts(view:"new")`` — newest-first
ordering, the server-honored ``after`` date bound, and ``limit``/``offset`` pagination.

(A) Pure-unit tests pin the query shape (the exact string live-verified 2026-07-24), the strict
date guard and the descending pagination → ``exhausted`` mapping, over the module's ``http_post``
seam. (B) Stage tests run ``collect.af_graphql`` through ``stack.stage_runner.run_stage`` on a
temporary store with the fake behind the harness transport (``stack.collect.base.urllib_send``):
a first pass writes one ``sources`` row per post (NO karma gate; ``fm_post`` key, metadata, the
``htmlBody`` as an in-band ``post`` entry in ``blobs/raw``) and one ``ok`` coverage row
``[floor, today]``; a second pass sends ``after = watermark``, mints no new ``source_id`` for a
known post and writes a part file; a cap-truncated DESCENDING pass is ``partial`` and never moves
the watermark (SHOULD-14); a failed page is ``failed_transient`` and claims nothing; an empty body
is counted ``absent`` and a later body is backfilled; a changed body replaces the entry; a malformed
``STACK_AF_FROM`` fails the run before any request; ``stack verify`` (V1/V2) passes.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import pathlib
import re
from collections.abc import Callable, Mapping
from itertools import pairwise
from typing import Any

import pyarrow.parquet as pq
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
WORKFLOWS_DIR = REPO / "workflows"


def _load_wf(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(
        f"_collect_{name}_under_test", WORKFLOWS_DIR / "collect" / name / "main.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


afg = _load_wf("af_graphql")


# ─────────────────────────────────────────────────────────────────────────────
# fake GraphQL transport
# ─────────────────────────────────────────────────────────────────────────────
#: ForumMagnum's GraphQL answer to an offset past its maximum (live 2026-10-19: offset 2,000
#: answered, 2,200 refused), as a 200 with an error envelope.
SKIP_REFUSAL = {"errors": [{"message": "Exceeded maximum value for skip",
                            "path": ["posts"]}], "data": {"posts": None}}


def _fake_af_post(
    posts: list[dict[str, Any]],
    *,
    honor_after: bool = True,
    captured: list[dict[str, Any]] | None = None,
    max_skip: int | None = None,
    error_envelope: Mapping[str, Any] | None = None,
) -> Any:
    """A ``http_post`` stand-in modelling ForumMagnum's ``posts(view:"new")``: posts are served
    newest-first (date-DESCENDING), the ``after`` term filters ``postedAt`` (server-honored), a
    ``before`` term filters it from above (inclusive day, as lw_graphql's fake), and
    ``limit``/``offset`` page the result. ``max_skip``: an ``offset`` above it is refused with
    :data:`SKIP_REFUSAL` (the live server's skip limit); ``error_envelope``: every request answers
    that 200 body. Each request's parsed params land in ``captured``."""
    ordered = sorted(posts, key=lambda p: str(p["postedAt"]), reverse=True)

    def fake(url: str, payload: Any, *, timeout: float = 60.0) -> tuple[int, Any]:
        q = str(payload["query"])
        limit = int(re.search(r"limit:\s*(\d+)", q).group(1))  # type: ignore[union-attr]
        offset = int(re.search(r"offset:\s*(\d+)", q).group(1))  # type: ignore[union-attr]
        after = re.search(r'after:\s*"([^"]+)"', q).group(1)  # type: ignore[union-attr]
        m_before = re.search(r'before:\s*"([^"]+)"', q)
        before = m_before.group(1) if m_before else None
        if captured is not None:
            captured.append({"limit": limit, "offset": offset, "after": after, "before": before})
        if error_envelope is not None:
            return 200, dict(error_envelope)
        if max_skip is not None and offset > max_skip:
            return 200, SKIP_REFUSAL
        pool = ordered
        if honor_after:
            pool = [p for p in ordered if str(p["postedAt"])[:10] >= after]
        if before is not None:
            pool = [p for p in pool if str(p["postedAt"])[:10] <= before]
        batch = pool[offset : offset + limit]
        return 200, {"data": {"posts": {"results": batch}}}

    return fake


def _post(
    _id: str,
    *,
    posted: str,
    title: str = "T",
    score: int = 50,
    body: str | None = "x",
    author: str = "Alice",
    coauthors: list[str] | None = None,
) -> dict[str, Any]:
    p: dict[str, Any] = {
        "_id": _id,
        "title": title,
        "postedAt": f"{posted}T00:00:00.000Z",
        "baseScore": score,
        "slug": f"slug-{_id}",
        "user": {"displayName": author, "username": author.lower()},
        "coauthors": [{"displayName": c, "username": c.lower()} for c in (coauthors or [])],
    }
    if body is not None:
        p["htmlBody"] = body
    return p


# ─────────────────────────────────────────────────────────────────────────────
# (A) pure-unit: query shape + pagination
# ─────────────────────────────────────────────────────────────────────────────
def test_build_posts_query_shape() -> None:
    """The exact GraphQL query as landed (live-verified 2026-07-24): view:"new", the date-descending
    completeness sweep, with an after bound + limit/offset pagination and the identity/meta/body
    field selection."""
    q = afg.build_posts_query("2024-01-01", 400, 200)
    assert 'view: "new"' in q
    assert "limit: 200" in q and "offset: 400" in q and 'after: "2024-01-01"' in q
    # the fields the identity / collect.meta / in-band body need
    for field in ("_id", "title", "postedAt", "baseScore", "htmlBody", "slug"):
        assert field in q
    assert "user { displayName username }" in q
    assert "coauthors { displayName username }" in q


def test_build_posts_query_coerces_ints() -> None:
    """offset/limit are coerced to int (no injection through the numeric slots)."""
    q = afg.build_posts_query("2020-01-01", 0, 5)
    assert "offset: 0" in q and "limit: 5" in q


def test_as_query_date_accepts_strict_date() -> None:
    """A strict, calendar-valid YYYY-MM-DD passes through unchanged."""
    assert afg._as_query_date("2026-07-24", source="floor") == "2026-07-24"


def test_as_query_date_rejects_quote_injection() -> None:
    """A quote-carrying value (the inline-interpolation break-out) raises, naming the source."""
    with pytest.raises(ValueError, match="src-A"):
        afg._as_query_date('2026-01-01"}}} injected', source="src-A")


def test_as_query_date_rejects_non_date() -> None:
    """A non-date string raises, naming the source."""
    with pytest.raises(ValueError, match="src-B"):
        afg._as_query_date("not-a-date", source="src-B")


def test_as_query_date_rejects_invalid_calendar() -> None:
    """A regex-shaped but calendar-invalid value (month 13, day 40) raises, naming the source."""
    with pytest.raises(ValueError, match="src-C"):
        afg._as_query_date("2026-13-40", source="src-C")


def test_fetch_af_complete_when_exhausted(monkeypatch: Any) -> None:
    """A pull that runs out of results before the cap is ``complete`` and returns every post."""
    monkeypatch.setattr(afg, "_sleep", lambda s: None)
    posts = [_post(f"p{i}", posted=f"2024-01-0{i + 1}") for i in range(3)]
    monkeypatch.setattr(afg, "http_post", _fake_af_post(posts))
    got, exhausted = afg.fetch_af("https://af.example", "2020-01-01", 100)
    assert exhausted == "complete" and len(got) == 3


def test_fetch_af_truncated_at_cap(monkeypatch: Any) -> None:
    """A pull stopped by the cap is ``truncated`` (a descending prefix — completeness unknown)."""
    monkeypatch.setattr(afg, "_sleep", lambda s: None)
    posts = [_post(f"p{i}", posted=f"2024-01-1{i}") for i in range(5)]
    monkeypatch.setattr(afg, "http_post", _fake_af_post(posts))
    got, exhausted = afg.fetch_af("https://af.example", "2020-01-01", 2)
    assert exhausted == "truncated" and len(got) == 2


def test_fetch_af_error_on_transport_failure(monkeypatch: Any) -> None:
    """A failed page (status 0 / no data) is ``error``; nothing is claimed."""
    monkeypatch.setattr(afg, "_sleep", lambda s: None)
    monkeypatch.setattr(afg, "http_post", lambda url, payload, *, timeout=60.0: (0, None))
    got, exhausted = afg.fetch_af("https://af.example", "2020-01-01", 100)
    assert exhausted == "error" and got == []


def test_fetch_af_skip_refusal_is_skip_cap_not_error(monkeypatch: Any) -> None:
    """D645: ForumMagnum refuses an ``offset`` past its maximum skip with a 200 error envelope
    ("Exceeded maximum value for skip"). That is the server's paging cap, ``skip_cap`` (the window
    holds more posts than offset paging reaches), not a garbled page (``error``, which the stage
    files ``failed_permanent[parse_error]``); the posts so far are returned and ``before`` goes
    out when given."""
    monkeypatch.setattr(afg, "_sleep", lambda s: None)
    monkeypatch.setattr(afg, "PAGE_SIZE", 2)
    posts = [_post(f"p{i}", posted=f"2024-01-{i + 10}") for i in range(9)]
    cap: list[dict[str, Any]] = []
    monkeypatch.setattr(afg, "http_post", _fake_af_post(posts, captured=cap, max_skip=4))
    got, exhausted = afg.fetch_af("https://af.example", "2020-01-01", 100, before="2024-02-01")
    assert exhausted == "skip_cap" and len(got) == 6
    assert [c["offset"] for c in cap] == [0, 2, 4, 6] and {c["before"] for c in cap} == {"2024-02-01"}


def test_other_graphql_error_envelope_is_still_error(monkeypatch: Any) -> None:
    """Any other error envelope stays ``error`` (a 200 that is not the expected shape:
    ``parse_error``, permanent), so only the skip refusal is reclassified."""
    monkeypatch.setattr(afg, "_sleep", lambda s: None)
    monkeypatch.setattr(afg, "http_post", _fake_af_post(
        [], error_envelope={"errors": [{"message": "Cannot query field"}], "data": None}))
    got, exhausted = afg.fetch_af("https://af.example", "2020-01-01", 100)
    assert exhausted == "error" and got == []
    assert afg.error_class_of(200) == ("parse_error", True)


def test_build_posts_query_carries_before_only_when_given() -> None:
    assert "before:" not in afg.build_posts_query("2024-01-01", 0, 5)
    q = afg.build_posts_query("2024-01-01", 0, 5, "2024-07-01")
    assert 'after: "2024-01-01", before: "2024-07-01"' in q and q.count("{") == q.count("}")


# ─────────────────────────────────────────────────────────────────────────────
# (B) the stage, through the runner on a temp store
# ─────────────────────────────────────────────────────────────────────────────
_LONG_BODY = (
    "<p>This is the canonical GraphQL body of an Alignment Forum post.</p>\r\n\r\n\r\n"
    "<p>It spans two paragraphs and comfortably exceeds the in-band noise floor.</p>"
)
_ENVS = ("STACK_COLLECT_LIMIT", "STACK_AF_FROM", "STACK_COLLECT_FRACTION",
         "STACK_COLLECT_MAX_QUERIES")


def sender(model: Callable[..., tuple[int, Any]], faults: list[int] | None = None) -> Any:
    """A :class:`stack.collect.base.Sender` over a v3-style ``(url, payload) -> (status, data)``
    model; ``faults`` = statuses served first, one per request (``0`` = a transport failure)."""
    queue = list(faults or [])

    def send(method: str, url: str, *, headers: Mapping[str, str], body: bytes | None,
             timeout: float, max_bytes: int) -> HttpResponse:
        assert method == "POST" and body is not None
        if queue:
            status = queue.pop(0)
            if status == 0:
                return HttpResponse(None, {}, b"", url, error="timeout")
            return HttpResponse(status, {}, b"", url)
        status, data = model(url, json.loads(body))
        return HttpResponse(status, {}, json.dumps(data).encode(), url)

    return send


def _plan(**options: Any) -> Plan:
    return Plan.model_validate({"plan": "test", "schema_version": 2, "stages": [
        {"stage": "collect.af_graphql", "prior": ["sources", "coverage"], "options": options}]})


@pytest.fixture
def stage(tmp_path: pathlib.Path, monkeypatch: Any) -> Any:
    """The runner loads this test's module object (so its seams can be pinned); the store path;
    a ``serve(posts, today, faults=..., captured=...)`` that installs the fake for the next run."""
    for var in _ENVS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(stage_runner, "load_stage_module", lambda name: afg)
    monkeypatch.setattr(afg, "_sleep", lambda s: None)

    def serve(posts: list[dict[str, Any]], today: str, *, faults: list[int] | None = None,
              captured: list[dict[str, Any]] | None = None, **model_kw: Any) -> None:
        monkeypatch.setattr(afg, "_as_of_date", lambda: today)
        monkeypatch.setattr("stack.collect.base.utcnow", lambda: _day(today) + dt.timedelta(hours=12))
        monkeypatch.setattr("stack.collect.base.urllib_send",
                            sender(_fake_af_post(posts, captured=captured, **model_kw), faults))

    return tmp_path / "store", serve


def _view(store: pathlib.Path, chain_id: str) -> list[dict[str, Any]]:
    return read_view(resolve(store, sdir(chain_id))).to_pylist()


def _cov(store: pathlib.Path) -> list[dict[str, Any]]:
    return sorted(_view(store, "coverage.af_graphql"), key=lambda r: (r["window_end_at"],
                                                                     r["window_start_at"]))


def _day(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s).replace(tzinfo=dt.UTC)


def test_first_pass_writes_rows_body_and_ok_window(stage: Any) -> None:
    """First pass: every post is a row (NO karma gate — a baseScore 1 post lands too) with the
    ``fm_post`` key, metadata (title, user then coauthor, postedAt) and the htmlBody minted raw;
    one coverage row ``af:all`` ``[floor, today]`` ok, seen 2 kept 2; V1/V2 pass."""
    store, serve = stage
    serve([_post("afHI", posted="2024-01-15", title="High Karma", score=190, body=_LONG_BODY,
                 author="Alice", coauthors=["Bob"]),
           _post("afLO", posted="2024-02-20", title="Low Karma", score=1, body=_LONG_BODY)],
          "2026-07-24")
    rec = run_line(store, _plan(**{"from": "2020-01-01"}), "collect.af_graphql")
    assert rec.status == "ok", rec.error
    h = {k: read_header(store / o["path"]) for k, o in rec.outputs.items()}
    assert {k: (v["chain_id"], v["kind"]) for k, v in h.items()} == {
        "sources": ("sources.af_graphql", "full"), "coverage": ("coverage.af_graphql", "full")}
    assert rec.counts["minted_ids"] == 2 and rec.counts["inband_minted"] == 2
    rows = {r["identity"]["external_id"]: r for r in _view(store, "sources.af_graphql")}
    assert sorted(rows) == ["afHI", "afLO"]
    hi = rows["afHI"]
    assert hi["identity"]["channel"] == "alignmentforum"
    assert hi["identity"]["canonical_url"] == "https://www.alignmentforum.org/posts/afHI"
    assert hi["identity"]["keys"] == [{"kind": "fm_post", "value": "afHI"}]
    assert (hi["state"], hi["record_format"], hi["status"]) == ("observed", "record", "ok")
    md = hi["metadata"]
    assert (md["title"], md["doc_type"], md["published_at"]) == (
        "High Karma", "forum_post", _day("2024-01-15"))
    assert [a["name"] for a in md["authors"]] == ["Alice", "Bob"]
    (entry,) = content_entries(hi)
    assert (entry["content_role"], entry["raw"]["media_type"], entry["text"]) == (
        "post", "text/html", None)
    rec = json_record(open_blobs(store), hi)  # D522: the post without its body
    assert rec["_id"] == "afHI" and "htmlBody" not in rec and "baseScore" in rec
    assert open_blobs(store).get(entry["raw"]["sha256"]).decode() == _LONG_BODY
    assert entry["raw"]["bytes"] == len(_LONG_BODY.encode())
    (cov,) = _cov(store)
    assert (cov["channel"], cov["query"], cov["status"], cov["is_exhausted"]) == (
        "alignmentforum", "af:all", "ok", True)
    assert (cov["window_start_at"], cov["window_end_at"]) == (_day("2020-01-01"), _day("2026-07-24"))
    assert (cov["seen"], cov["kept"]) == (2, 2)
    results = {r.check: r for r in run_checks(store)}
    assert {n: r.items for n, r in results.items() if r.status in ("failed", "warn")} == {}
    assert results["V-sources"].status == "ok" and results["V-coverage"].status == "ok"


@pytest.mark.slow
def test_resume_sends_after_watermark_and_mints_no_known_id(stage: Any) -> None:
    """Run 2 (no flags; the stage line pins the own chains) sends ``after = watermark`` less the
    14-day promotion lag (D551), gets only the new post, writes part files, mints one id; run 3 the
    same day asks nothing (covered through today, D226); run 4 the next day sends ``after`` = the
    watermark less the lag: the new post comes back unchanged (no row), no mint, the first
    ``source_id`` kept."""
    store, serve = stage
    old, new = _post("afOLD", posted="2023-05-01"), _post("afNEW", posted="2026-07-23")
    cap1: list[dict[str, Any]] = []
    serve([old], "2026-07-21", captured=cap1)
    rec1 = run_line(store, _plan(**{"from": "2020-01-01"}), "collect.af_graphql")
    assert rec1.status == "ok", rec1.error
    assert cap1 and all(p["after"] == "2020-01-01" for p in cap1)

    cap2: list[dict[str, Any]] = []
    serve([old, new], "2026-07-24", captured=cap2)
    rec2 = run_line(store, _plan(**{"from": "2020-01-01"}), "collect.af_graphql", kind="part")
    assert rec2.status == "ok", rec2.error
    assert cap2 and all(p["after"] == "2026-07-07" for p in cap2)
    assert {k: read_header(store / o["path"])["kind"] for k, o in rec2.outputs.items()} == {
        "sources": "part", "coverage": "part"}
    assert rec2.counts["minted_ids"] == 1
    last = _cov(store)[-1]
    assert (last["seen"], last["kept"], last["status"]) == (1, 1, "ok")
    assert (last["window_start_at"], last["window_end_at"]) == (_day("2026-07-21"), _day("2026-07-24"))

    first_ids = {r["identity"]["external_id"]: r["source_id"] for r in _view(store, "sources.af_graphql")}
    cap3: list[dict[str, Any]] = []
    serve([old, new], "2026-07-24", captured=cap3)
    rec3 = run_line(store, _plan(**{"from": "2020-01-01"}), "collect.af_graphql")
    assert rec3.status == "ok", rec3.error
    assert cap3 == [] and rec3.outputs == {}
    assert (rec3.counts["already_covered"], rec3.counts["http_requests"]) == (1, 0)

    cap4: list[dict[str, Any]] = []
    serve([old, new], "2026-07-25", captured=cap4)
    rec4 = run_line(store, _plan(**{"from": "2020-01-01"}), "collect.af_graphql")
    assert rec4.status == "ok", rec4.error
    assert cap4 and all(p["after"] == "2026-07-10" for p in cap4)
    assert rec4.counts["minted_ids"] == 0 and rec4.counts["sources_unchanged"] == 1
    assert "sources" not in rec4.outputs  # the re-seen post wrote no row
    assert {r["identity"]["external_id"]: r["source_id"]
            for r in _view(store, "sources.af_graphql")} == first_ids


@pytest.mark.slow
def test_truncated_descending_pass_is_partial_and_holds_the_watermark(stage: Any) -> None:
    """SHOULD-14: ``view:"new"`` is date-descending, so a cap-truncated pass covered an unknown
    newest prefix: its window is ``partial``, not exhausted, and the next run starts at the floor
    again. The posts it did fetch are rows."""
    store, serve = stage
    posts = [_post(f"afN{i}", posted=f"2024-0{i + 1}-01") for i in range(4)]
    serve(posts, "2026-07-24")
    rec = run_line(store, _plan(**{"from": "2020-01-01", "max_records": 2}), "collect.af_graphql")
    assert rec.status == "ok", rec.error
    (cov,) = _cov(store)
    assert (cov["status"], cov["is_exhausted"], cov["seen"]) == ("partial", False, 2)
    assert rec.scope_complete is False and incomplete_reasons(rec.counts) == ["limit"]  # D492
    assert len(_view(store, "sources.af_graphql")) == 2
    cap: list[dict[str, Any]] = []
    serve(posts, "2026-07-25", captured=cap)
    assert run_line(store, _plan(**{"from": "2020-01-01", "max_records": 2}),
                     "collect.af_graphql").status == "ok"
    assert cap and all(p["after"] == "2020-01-01" for p in cap)  # the watermark did not move


@pytest.mark.slow
def test_failed_page_claims_nothing(stage: Any) -> None:
    """A 503 on the first page: the window is ``failed_transient[http_5xx]``, no rows; the next
    run starts at the floor again and completes."""
    store, serve = stage
    posts = [_post("afA", posted="2024-01-01")]
    serve(posts, "2026-07-24", faults=[503])
    rec = run_line(store, _plan(**{"from": "2020-01-01"}), "collect.af_graphql")
    assert rec.status == "ok", rec.error
    (cov,) = _cov(store)
    assert (cov["status"], cov["error_class"], cov["is_exhausted"]) == (
        "failed_transient", "http_5xx", False)
    assert "sources" not in rec.outputs
    cap: list[dict[str, Any]] = []
    serve(posts, "2026-07-24", captured=cap)
    rec2 = run_line(store, _plan(**{"from": "2020-01-01"}), "collect.af_graphql")
    assert rec2.status == "ok", rec2.error
    assert cap and cap[0]["after"] == "2020-01-01"
    (cov,) = _cov(store)  # the view keeps the latest row per window key
    assert (cov["status"], cov["attempts"]) == ("ok", 2)  # same window key, charged twice


@pytest.mark.slow
def test_empty_body_absent_then_backfilled_then_replaced(stage: Any) -> None:
    """An empty htmlBody mints nothing (``absent``); the same post re-seen with a body is
    backfilled (``inband_backfill``); a changed body replaces the role's entry (§5.6: new body =
    new in-band entry). One run a day (a sweep covered through today is not asked again the same
    day, D226); the post's date is the last day, so every run's ``after`` includes it."""
    store, serve = stage
    serve([_post("afE", posted="2026-07-26", body="")], "2026-07-24")
    rec1 = run_line(store, _plan(**{"from": "2020-01-01"}), "collect.af_graphql")
    assert rec1.status == "ok", rec1.error
    assert rec1.counts["inband_absent"] == 1 and rec1.counts["inband_minted"] == 0
    (row,) = _view(store, "sources.af_graphql")
    assert content_entries(row) == [] and json_record(open_blobs(store), row)["_id"] == "afE"

    serve([_post("afE", posted="2026-07-26", body=_LONG_BODY)], "2026-07-25")
    rec2 = run_line(store, _plan(**{"from": "2020-01-01"}), "collect.af_graphql")
    assert (rec2.counts["inband_minted"], rec2.counts["inband_backfill"]) == (1, 1)
    (row,) = _view(store, "sources.af_graphql")
    (e2,) = content_entries(row)

    serve([_post("afE", posted="2026-07-26", body=_LONG_BODY + "<p>Edited.</p>")], "2026-07-26")
    rec3 = run_line(store, _plan(**{"from": "2020-01-01"}), "collect.af_graphql")
    assert rec3.counts["inband_minted"] == 1 and rec3.counts["minted_ids"] == 0
    (row,) = _view(store, "sources.af_graphql")
    (e3,) = content_entries(row)
    assert e3["content_role"] == "post" and e3["raw"]["sha256"] != e2["raw"]["sha256"]


def test_malformed_af_from_fails_before_any_request(stage: Any, monkeypatch: Any) -> None:
    """A malformed STACK_AF_FROM fails the run with an error naming it, before any query is built
    (the inline-interpolation injection guard)."""
    store, serve = stage
    cap: list[dict[str, Any]] = []
    serve([], "2026-07-24", captured=cap)
    monkeypatch.setenv("STACK_AF_FROM", '2020-01-01"}}} injected')
    rec = run_line(store, _plan(), "collect.af_graphql")
    assert rec.status == "failed" and "STACK_AF_FROM" in str(rec.error)
    assert cap == []


def test_floor_moved_back_lists_only_the_gap(stage: Any) -> None:
    """B7a, D645: a first window from 2025-10-01, then `from: 2024-11-30` the same day: the hole
    [2024-11-30, 2025-10-01] (305 days) is listed as bounded 180-day spans (``after`` one day
    before, ``before`` one day after each span, as lw_graphql), oldest first, each its own `ok`
    window; posts outside a span are cut (`posts_outside_span`: the newer post the slack returns);
    the covered tail is not asked; a third run asks nothing."""
    store, serve = stage
    posts = [_post("afOLD", posted="2025-02-03"), _post("afNEW", posted="2025-11-04")]
    cap: list[dict[str, Any]] = []
    serve(posts, "2026-07-24", captured=cap)
    assert run_line(store, _plan(**{"from": "2025-10-01"}), "collect.af_graphql").status == "ok"
    cap.clear()
    rec = run_line(store, _plan(**{"from": "2024-11-30"}), "collect.af_graphql")
    assert rec.status == "ok", rec.error
    assert [(p["after"], p["before"]) for p in cap] == [
        ("2024-11-29", "2025-05-30"), ("2025-05-28", "2025-10-02")]
    assert (rec.counts["holes"], rec.counts["spans"], rec.counts["minted_ids"]) == (1, 2, 1)
    assert rec.counts["posts_outside_span"] == 0  # the fake honours `before`: nothing to cut
    assert [(r["window_start_at"], r["window_end_at"], r["status"]) for r in _cov(store)] == [
        (_day("2024-11-30"), _day("2025-05-29"), "ok"), (_day("2025-05-29"), _day("2025-10-01"), "ok"),
        (_day("2025-10-01"), _day("2026-07-24"), "ok")]
    cap.clear()
    rec3 = run_line(store, _plan(**{"from": "2024-11-30"}), "collect.af_graphql")
    assert rec3.status == "ok" and cap == [] and rec3.counts["already_covered"] == 1


@pytest.mark.slow
def test_a_hole_past_the_skip_limit_completes_in_spans(stage: Any, monkeypatch: Any) -> None:
    """D645, the incident: the floor moves back over more posts than offset paging reaches (here
    PAGE_SIZE 2 and a skip limit of 4: 6 posts per request sequence; live: 200 and ~2,000). The old
    unbounded hole failed ``failed_permanent[parse_error]`` in every build; now every span is
    bounded, and a span the server still refuses is split in halves until each fits. Every post
    lands once, every window is `ok` and the windows tile the hole; no window is written for a
    span that was split (``spans_split``)."""
    store, serve = stage
    monkeypatch.setattr(afg, "PAGE_SIZE", 2)
    new = [_post("afN", posted="2026-03-01")]
    serve(new, "2026-07-24", max_skip=4)
    assert run_line(store, _plan(**{"from": "2026-01-01"}), "collect.af_graphql").status == "ok"
    # 14 posts in Jan-Apr 2025 (one 180-day span over 6), 3 in 2024
    old = [_post(f"afA{i:02d}", posted=(dt.date(2025, 1, 2) + dt.timedelta(days=8 * i)).isoformat())
           for i in range(14)] + [_post(f"afB{i}", posted=f"2024-0{i + 3}-15") for i in range(3)]
    cap: list[dict[str, Any]] = []
    serve(new + old, "2026-07-24", captured=cap, max_skip=4)
    rec = run_line(store, _plan(**{"from": "2024-01-01"}), "collect.af_graphql")
    assert rec.status == "ok", rec.error
    assert rec.scope_complete is True and rec.counts["spans_split"] >= 1
    cov = [r for r in _cov(store) if r["window_end_at"] <= _day("2026-01-01")]
    assert {r["status"] for r in cov} == {"ok"}
    assert "windows_failed_permanent.parse_error" not in rec.counts
    tiles = sorted((r["window_start_at"], r["window_end_at"]) for r in cov)
    assert tiles[0][0] == _day("2024-01-01") and tiles[-1][1] == _day("2026-01-01")
    assert all(a[1] == b[0] for a, b in pairwise(tiles)), tiles  # contiguous, no overlap
    assert sum(r["seen"] for r in cov) == 17 and rec.counts["minted_ids"] == 17
    assert all(c["before"] is not None for c in cap)  # a hole span is always bounded
    results = {r.check: r for r in run_checks(store)}
    assert {n: r.items for n, r in results.items() if r.status in ("failed", "warn")} == {}


def test_a_long_first_tail_splits_on_the_skip_limit(stage: Any, monkeypatch: Any) -> None:
    """D645: the newest-first tail stays one unbounded window while it fits (the weekly case); a
    first pass from a far floor over more posts than the skip limit splits adaptively: the older
    halves bounded, the newest still open above. All `ok`, tiling [floor, today]."""
    store, serve = stage
    monkeypatch.setattr(afg, "PAGE_SIZE", 2)
    posts = [_post(f"afT{i:02d}", posted=(dt.date(2026, 1, 3) + dt.timedelta(days=13 * i)).isoformat())
             for i in range(15)]
    cap: list[dict[str, Any]] = []
    serve(posts, "2026-07-24", captured=cap, max_skip=4)
    rec = run_line(store, _plan(**{"from": "2026-01-01"}), "collect.af_graphql")
    assert rec.status == "ok", rec.error
    assert rec.scope_complete is True and rec.counts["spans_split"] >= 1
    cov = _cov(store)
    assert {r["status"] for r in cov} == {"ok"}
    tiles = [(r["window_start_at"], r["window_end_at"]) for r in cov]
    assert tiles[0][0] == _day("2026-01-01") and tiles[-1][1] == _day("2026-07-24")
    assert all(a[1] == b[0] for a, b in pairwise(tiles)), tiles
    assert sum(r["seen"] for r in cov) == 15 and rec.counts["minted_ids"] == 15
    assert cap[0] == {"limit": 2, "offset": 0, "after": "2026-01-01", "before": None}  # the old tail first
    assert cap[-1]["before"] is None  # the newest span stays open above


def test_an_unsplittable_skip_cap_is_a_capped_partial(stage: Any, monkeypatch: Any) -> None:
    """D645: a span of under two days that the server still refuses cannot be split: it is a
    server cap (`partial`, `windows_capped`, build reason `limit`), never `failed_permanent`; the
    posts it reached are rows."""
    store, serve = stage
    monkeypatch.setattr(afg, "PAGE_SIZE", 2)
    posts = [_post(f"afD{i}", posted="2026-07-23") for i in range(9)]
    serve(posts, "2026-07-24", max_skip=4)
    rec = run_line(store, _plan(**{"from": "2026-07-23"}), "collect.af_graphql")
    assert rec.status == "ok", rec.error
    (cov,) = _cov(store)
    assert (cov["status"], cov["is_exhausted"], cov["seen"]) == ("partial", False, 6)
    assert rec.scope_complete is False and incomplete_reasons(rec.counts) == ["limit"]
    assert rec.counts["skip_capped"] == 1 and rec.counts.get("windows_failed_permanent", 0) == 0


@pytest.mark.slow
def test_a_failed_permanent_hole_is_asked_again(stage: Any) -> None:
    """D645, the re-ask rule (pinned, unchanged): a `failed_permanent` window covers nothing
    (:meth:`Collector.holes` and the watermark read `ok` rows only), so the next run asks that
    range again with the current code. Run 2 writes the hole as one window that fails
    `parse_error` (the production row of 2026-10-19 had that shape); run 3 (default spans) asks
    it again, banks it in `ok` spans and leaves the old row standing; run 4 asks nothing; verify
    passes with the stale failed row in the view."""
    store, serve = stage
    posts = [_post("afNEW", posted="2025-11-04"), _post("afOLD", posted="2023-02-03")]
    serve(posts, "2026-07-24")
    assert run_line(store, _plan(**{"from": "2025-10-01"}), "collect.af_graphql").status == "ok"
    serve(posts, "2026-07-24", error_envelope={"errors": [{"message": "boom"}], "data": None})
    rec2 = run_line(store, _plan(**{"from": "2022-01-01", "subwindow_days": 0}), "collect.af_graphql")
    assert rec2.status == "ok", rec2.error
    assert rec2.counts["windows_failed_permanent.parse_error"] == 1
    failed = [r for r in _cov(store) if r["status"] == "failed_permanent"]
    assert [(r["window_start_at"], r["window_end_at"]) for r in failed] == [
        (_day("2022-01-01"), _day("2025-10-01"))]
    cap: list[dict[str, Any]] = []
    serve(posts, "2026-07-24", captured=cap)
    rec3 = run_line(store, _plan(**{"from": "2022-01-01"}), "collect.af_graphql")
    assert rec3.status == "ok", rec3.error
    assert rec3.counts["holes"] == 1 and cap and all(c["before"] is not None for c in cap)
    assert "afOLD" in {r["identity"]["external_id"] for r in _view(store, "sources.af_graphql")}
    cov = _cov(store)
    ok = sorted((r["window_start_at"], r["window_end_at"]) for r in cov if r["status"] == "ok")
    assert ok[0][0] == _day("2022-01-01") and all(a[1] == b[0] for a, b in pairwise(ok))
    assert [r["status"] for r in cov if (r["window_start_at"], r["window_end_at"]) == (
        _day("2022-01-01"), _day("2025-10-01"))] == ["failed_permanent"]
    cap.clear()
    rec4 = run_line(store, _plan(**{"from": "2022-01-01"}), "collect.af_graphql")
    assert rec4.status == "ok" and cap == [] and rec4.counts["already_covered"] == 1
    results = {r.check: r for r in run_checks(store)}
    assert {n: r.items for n, r in results.items() if r.status in ("failed", "warn")} == {}


@pytest.mark.slow
@pytest.mark.parametrize("lookback", [None, 0], ids=["default-14", "off"])
def test_tail_rescans_the_promotion_lag_and_writes_no_duplicate_rows(stage: Any, lookback: int | None) -> None:
    """D551: a post moved to AF after run 1, dated 9 days before run 1's watermark, is collected by
    run 2 (asked from watermark − 14 days); one dated 20 days before it is not; the post run 1
    saw comes back unchanged and writes no row; the window is written from the watermark. With
    ``lookback_days: 0`` the promoted post is missed."""
    store, serve = stage
    opts: dict[str, Any] = {"from": "2026-06-01"}
    if lookback is not None:
        opts["lookback_days"] = lookback
    seen = _post("afSEEN", posted="2026-07-15")
    serve([seen], "2026-07-21")
    assert run_line(store, _plan(**opts), "collect.af_graphql").status == "ok"
    late_in, late_out = _post("afIN", posted="2026-07-12"), _post("afOUT", posted="2026-07-01")
    new = _post("afNEW", posted="2026-07-23")
    cap: list[dict[str, Any]] = []
    serve([seen, late_in, late_out, new], "2026-07-24", captured=cap)
    rec = run_line(store, _plan(**opts), "collect.af_graphql", kind="part")
    assert rec.status == "ok", rec.error
    got = {r["identity"]["external_id"] for r in _view(store, "sources.af_graphql")}
    written = sorted(r["identity"]["external_id"]
                     for r in pq.read_table(store / rec.outputs["sources"]["path"]).to_pylist())
    if lookback is None:
        assert {p["after"] for p in cap} == {"2026-07-07"}
        assert "afIN" in got and "afOUT" not in got
        assert written == ["afIN", "afNEW"] and rec.counts["sources_unchanged"] == 1
    else:
        assert {p["after"] for p in cap} == {"2026-07-21"}
        assert "afIN" not in got and written == ["afNEW"]
    last = _cov(store)[-1]
    assert (last["status"], last["window_start_at"], last["window_end_at"]) == (
        "ok", _day("2026-07-21"), _day("2026-07-24"))


def test_split_span_halves_and_keeps_the_outer_bounds() -> None:
    """D645: a split keeps the parent's `ask` and lower cut on the older half and its `before` and
    upper cut on the newer one; the inner bounds are one day wide of the middle day; a span under
    MIN_SPLIT_DAYS is not split."""
    d = dt.date.fromisoformat
    tail = afg.tail_span(d("2026-01-01"), d("2026-07-24"), d("2025-12-18"))
    older, newer = afg.split_span(tail)
    assert older == afg.Span(d("2026-01-01"), d("2026-04-13"), d("2025-12-18"), d("2026-04-14"), False, True)
    assert newer == afg.Span(d("2026-04-13"), d("2026-07-24"), d("2026-04-12"), None, True, False)
    hole = afg.hole_span(d("2020-01-01"), d("2020-01-03"))
    assert hole == afg.Span(d("2020-01-01"), d("2020-01-03"), d("2019-12-31"), d("2020-01-04"), True, True)
    assert [h.before for h in afg.split_span(hole)] == [d("2020-01-03"), d("2020-01-04")]
    assert afg.split_span(afg.hole_span(d("2020-01-01"), d("2020-01-02"))) is None
