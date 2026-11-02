"""The collector harness (``stack/collect/base.py``) driven by a toy ``collect.fake`` stage over
``tests/fakes/http.py``, through ``stack.stage_runner.run_stage`` on a temporary store:

(a) run 1 starts ``data/sources.fake/main/`` / ``data/coverage.fake/main/``; (b) run 2 over the
same pages, with the ``--in`` a build line passes (``tests.synth.records.run_line``: the stage
line's ``prior: [sources, coverage]`` series directories), mints no new ``source_id`` and advances
the watermark only past ``ok`` windows; (c) in-band bodies land in
``blobs/raw`` / ``blobs/text`` with their ``INBAND`` entries, carried unchanged on re-runs;
(d) a run-level 429 halts the run: no further request, the window ``failed_transient
[rate_limit]`` uncharged, no watermark advance, run ``ok`` with ``scope_complete = false``, the
observations made emitted; (e) ``stack verify`` passes on the store, V1/V2 included; (f) query
attribution (D228): every query that returned a source in ``metadata.matched_by``, a repeat adding
to the standing row, the protocol refusals, and a store written before the field upcast by
``stack migrate upcast --family sources``. Also: the provider response journal lives under
``<store>/cache/provider-responses/``.
"""

from __future__ import annotations

import datetime as dt
import textwrap
from pathlib import Path
from typing import Any, Self

import pyarrow.parquet as pq
import pytest

from stack import stage_runner
from stack.checks.verify import run_checks
from stack.plan import Plan
from stack.provider_transport import JournalRequest
from stack.runs import read_run_record
from stack.schemas.families import family
from stack.stage_runner import StageError, run_stage
from stack.store.blobs import open_blobs
from stack.store.chain import read_view, resolve, resolve_records
from stack.store.parquet import read_header
from tests.fakes.http import INSTALLED, FakeHttp
from tests.synth.records import run_line, sdir

API = "https://api.fake.test/items"
TOY = '''
import json

from stack.collect.base import Collector, Pacing
from stack.config import ChannelDef
from tests.fakes.http import INSTALLED

CHANNELS = {"fake": ChannelDef(id="fake", kind="search", license_class="open",
                               channel_group="paper", record_projection=["id", "title", "abstract"])}
PACING = Pacing(rps=1000.0, retry_delays_s=(1.0, 2.0), halt_429_after_s=60.0)
API = "https://api.fake.test/items"


def run(ctx):
    c = Collector(ctx, channels=CHANNELS)
    t = c.transport(PACING, send=INSTALLED["fake"], sleep=lambda s: None)
    to = c.option("to", "2026-10-01")
    attribute = c.option("attribute", None)  # None | "query" | "mapping" | "metadata" | "label"
    permanent = c.option("permanent", [])  # queries whose window fails for good (R3-2)
    c.option("max_records", 0, env="STACK_COLLECT_LIMIT", cast=int)  # a cap knob (D445)
    c.option("from", None, env="STACK_FAKE_FROM")  # not a cap knob: not counted
    cap = c.option("cap", 0)  # D492: stop each window after this many items, `partial`
    soft = c.option("soft", False)  # D492: that partial is not a cap (truncated(cap=False))
    record = c.option("record", False)  # D522: observe with provider_record=item
    with c.halts():
        for q in c.option("queries", ["a"]):
            start = c.watermark("fake", q) or "2026-01-01"
            with c.window("fake", q, start, to) as w:
                if q in permanent:
                    w.fail("query_invalid", permanent=True)
                    continue
                page = 1
                while True:
                    resp = t.fetch("GET", API, params={"q": q, "page": page})
                    if resp.status != 200:
                        w.fail("http_5xx")
                        break
                    data = json.loads(resp.body)
                    for item in data["items"]:
                        w.seen += 1
                        md = {"title": item["title"], "published_at": "2026-09-01"}
                        match = {}
                        if attribute == "query":
                            match = {"query": q, "label": "term " + q}
                        elif attribute == "mapping":  # no query_id: the mapping's JCS
                            match = {"query": {"q": q}}
                        elif attribute == "metadata":  # refused: the harness owns the field
                            md["query_digest"] = "0" * 64
                        elif attribute == "label":  # refused: a label without its query
                            match = {"label": q}
                        c.observe("fake", item["id"], item, keys=[("doi", item["doi"])],
                                  metadata=md, provider_record=item if record else None, **match)
                        sid = c.source_id("fake", item["id"])
                        if item.get("abstract"):
                            c.add_inband(sid, "abstract", text=item["abstract"],
                                         media_type="text/plain")
                        if item.get("html"):
                            c.add_inband(sid, "post", raw=item["html"].encode(),
                                         media_type="text/html")
                        w.kept += 1
                    if cap and w.seen >= cap:
                        w.truncated(cap=not soft)
                        break
                    if not data.get("next"):
                        w.complete()
                        break
                    page += 1
            if attribute == "query":  # attribute(): a re-find the collector does not observe again
                for _ in range(2):  # a repeated entry is not doubled
                    assert c.attribute("fake", "F1", query=q, query_id="extra:" + q) is True
                assert c.attribute("fake", "F999", query=q) is False
    return c.finish()
'''


def item(i: int, **over: Any) -> dict[str, Any]:
    return {"id": f"F{i}", "doi": f"10.1/{i}", "title": f"Item {i}", "score": i * 10,
            "abstract": f"An abstract about scalable oversight, item number {i}.", **over}


def plan(**options: Any) -> Plan:
    return Plan.model_validate({"plan": "test", "schema_version": 2, "series": "main", "stages": [
        {"stage": "collect.fake", "prior": ["sources", "coverage"], "out": {"kind": "part"}, "options": options}]})


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "workflows"
    (root / "collect" / "fake").mkdir(parents=True)
    (root / "collect" / "fake" / "main.py").write_text(textwrap.dedent(TOY))
    monkeypatch.setattr(stage_runner, "WORKFLOWS_ROOT", root)
    return tmp_path / "store"


@pytest.fixture
def fake() -> Any:
    f = FakeHttp()
    f.add(API, {"q": "a", "page": 1}, json={"items": [item(1), item(2, html="<p>Body two</p>")],
                                           "next": 2})
    f.add(API, {"q": "a", "page": 2}, json={"items": [item(3)], "next": None})
    f.add(API, {"q": "b", "page": 1}, json={"items": [item(4), item(1)], "next": None})
    INSTALLED["fake"] = f
    yield f
    INSTALLED.pop("fake", None)


def view(store: Path, family_dir: str) -> list[dict[str, Any]]:
    return read_view(resolve(store, sdir(family_dir))).to_pylist()


def name(rec: Any, fam: str) -> str:
    """The file name of ``rec``'s ``fam`` output (what a later file's ``base`` names)."""
    return Path(rec.outputs[fam]["path"]).name


def headers(store: Path, rec: Any) -> dict[str, dict[str, str]]:
    return {k: read_header(store / o["path"]) for k, o in rec.outputs.items()}


def test_runs_mint_once_and_watermark_moves_only_past_ok(store: Path, fake: FakeHttp) -> None:
    fake.inject(API, {"q": "b", "page": 1}, status=503, times=3)  # b fails after the 2-step ladder
    rec1 = run_line(store, plan(queries=["a", "b"], to="2026-10-01"), "collect.fake")
    assert rec1.status == "ok", rec1.error
    # (a) both series started by run 1 (full files, no base)
    h1 = headers(store, rec1)
    assert {k: (h["chain_id"], h["series"], h["kind"], h["base"]) for k, h in h1.items()} == {
        "sources": ("sources.fake", "main", "full", ""), "coverage": ("coverage.fake", "main", "full", "")}
    assert rec1.counts["minted_ids"] == 3 and rec1.counts["sources_written"] == 3
    src1 = {r["identity"]["external_id"]: r for r in view(store, "sources.fake")}
    cov1 = {r["query"]: r for r in view(store, "coverage.fake")}
    assert (cov1["a"]["status"], cov1["a"]["is_exhausted"], cov1["a"]["seen"]) == ("ok", True, 3)
    assert (cov1["b"]["status"], cov1["b"]["error_class"]) == ("failed_transient", "http_5xx")
    assert rec1.scope_complete is False  # R3-2: b's transient window is retried by the next run
    # record_digest is over the projection: `score` (volatile) is out
    assert src1["F1"]["record_format"] == "record" and src1["F1"]["state"] == "observed"

    # (b) run 2: the line's --in are the own series dirs; same pages, later `to`
    rec2 = run_line(store, plan(queries=["a", "b"], to="2026-10-10"), "collect.fake")
    assert rec2.status == "ok", rec2.error
    assert sorted(rec2.prior) == sorted([rec1.outputs["sources"]["file_id"],
                                         rec1.outputs["coverage"]["file_id"]])
    h2 = headers(store, rec2)
    assert {k: (h["kind"], h["base"]) for k, h in h2.items()} == {
        "sources": ("part", name(rec1, "sources")), "coverage": ("part", name(rec1, "coverage"))}
    assert rec2.counts["minted_ids"] == 1  # F4 only (b failed in run 1)
    src2 = {r["identity"]["external_id"]: r for r in view(store, "sources.fake")}
    assert {k: src2[k]["source_id"] for k in src1} == {k: r["source_id"] for k, r in src1.items()}
    cov2 = pq.read_table(store / rec2.outputs["coverage"]["path"]).to_pylist()
    starts = {r["query"]: r["window_start_at"] for r in cov2}
    assert starts["a"] == dt.datetime(2026, 10, 1, tzinfo=dt.UTC)  # past run 1's ok window
    assert starts["b"] == dt.datetime(2026, 1, 1, tzinfo=dt.UTC)   # b's failed window: no advance
    assert all(r["attempts"] == 1 for r in cov2)  # new window keys


def test_inband_bodies_land_in_blobs_and_carry(store: Path, fake: FakeHttp) -> None:
    rec1 = run_line(store, plan(queries=["a"]), "collect.fake")
    assert rec1.status == "ok", rec1.error
    blobs = open_blobs(store)
    src = {r["identity"]["external_id"]: r for r in view(store, "sources.fake")}
    e1 = src["F1"]["inband"]["entries"]
    assert [(e["content_role"], e["raw"], e["text"]["media_type"]) for e in e1] == [
        ("abstract", None, "text/plain")]
    body = blobs.get(e1[0]["text"]["sha256"])
    assert body.decode().startswith("An abstract") and e1[0]["text"]["bytes"] == len(body)
    e2 = {e["content_role"]: e for e in src["F2"]["inband"]["entries"]}
    assert set(e2) == {"abstract", "post"}  # per role: the abstract never displaces the body
    assert e2["post"]["text"] is None
    assert blobs.get(e2["post"]["raw"]["sha256"]) == b"<p>Body two</p>"
    assert (e2["post"]["raw"]["bytes"], e2["post"]["raw"]["media_type"]) == (len(b"<p>Body two</p>"), "text/html")
    assert e2["post"]["source_id"] == src["F2"]["source_id"]
    assert rec1.counts["inband_minted"] == 4 and rec1.counts["inband_skipped"] == 0
    # a re-run mints nothing: the entries are carried with their first captured_at
    rec2 = run_line(store, plan(queries=["a"], to="2026-10-02"), "collect.fake")
    assert rec2.status == "ok", rec2.error
    assert rec2.counts["inband_minted"] == 0
    src2 = {r["identity"]["external_id"]: r for r in view(store, "sources.fake")}
    assert {k: r["inband"]["entries"] for k, r in src2.items()} == {
        k: r["inband"]["entries"] for k, r in src.items()}
    assert src2["F1"]["run_id"] == rec2.run_id


def test_provider_records_mint_canonical_json_and_dedupe(store: Path, fake: FakeHttp) -> None:
    """D522: ``observe(provider_record=item)`` mints the item as canonical JSON (sorted keys, no
    whitespace) under role ``provider_record`` (raw only, ``application/json``, ``captured_at`` = the
    observation); a re-run with the same items keeps the entry (first ``captured_at``); a changed item
    (``score``, outside the projection) gets a new record. ``record_*`` and ``http_bytes`` are counted."""
    rec1 = run_line(store, plan(queries=["a"], record=True), "collect.fake")
    assert rec1.status == "ok", rec1.error
    blobs = open_blobs(store)
    src = {r["identity"]["external_id"]: r for r in view(store, "sources.fake")}
    rec_e = {k: next(e for e in r["inband"]["entries"] if e["content_role"] == "provider_record")
             for k, r in src.items()}
    e = rec_e["F1"]
    assert (e["text"], e["raw"]["media_type"], e["source_id"]) == (
        None, "application/json", src["F1"]["source_id"])
    assert e["captured_at"] == src["F1"]["observed_at"]
    assert blobs.get(e["raw"]["sha256"]) == (
        b'{"abstract":"An abstract about scalable oversight, item number 1.","doi":"10.1/1",'
        b'"id":"F1","score":10,"title":"Item 1"}')
    assert (rec1.counts["record_minted"], rec1.counts["inband_minted"]) == (3, 4)  # apart
    assert rec1.counts["record_bytes"] == sum(len(blobs.get(x["raw"]["sha256"])) for x in rec_e.values())
    assert all(x["raw"]["bytes"] == len(blobs.get(x["raw"]["sha256"])) for x in rec_e.values())
    assert rec1.counts["http_bytes"] > 0 and "http_ms" in rec1.counts
    assert "record_ref" not in src["F1"]["metadata"]  # retired (D524)

    fake.add(API, {"q": "a", "page": 2}, json={"items": [item(3, score=99)], "next": None})
    rec2 = run_line(store, plan(queries=["a"], record=True, to="2026-10-02"), "collect.fake")
    assert rec2.status == "ok", rec2.error
    assert (rec2.counts["record_minted"], rec2.counts["record_skipped"]) == (1, 2)
    src2 = {r["identity"]["external_id"]: r for r in view(store, "sources.fake")}
    rec_e2 = {k: next(e for e in r["inband"]["entries"] if e["content_role"] == "provider_record")
              for k, r in src2.items()}
    assert rec_e2["F1"] == rec_e["F1"]  # unchanged: carried with its first captured_at
    assert rec_e2["F3"]["raw"]["sha256"] != rec_e["F3"]["raw"]["sha256"]
    assert b'"score":99' in blobs.get(rec_e2["F3"]["raw"]["sha256"])
    red = {r.check: r.items for r in run_checks(store) if r.status in ("failed", "warn")}
    assert red == {}


def test_run_level_429_halts_without_advancing(store: Path, fake: FakeHttp) -> None:
    fake.inject(API, {"q": "a", "page": 2}, status=429, retry_after=3600)
    rec = run_line(store, plan(queries=["a", "b"]), "collect.fake")
    assert rec.status == "ok", rec.error
    assert rec.scope_complete is False and rec.counts["halted"] == 1
    assert not any("q=b" in u for u in fake.urls()), fake.urls()  # no request after the halt
    assert len(fake.calls) == 2
    cov = view(store, "coverage.fake")
    assert [(r["query"], r["status"], r["error_class"], r["attempts"]) for r in cov] == [
        ("a", "failed_transient", "rate_limit", 0)]
    assert cov[0]["retry_after"] is not None and cov[0]["last_attempt_at"] is None
    assert sorted(r["identity"]["external_id"] for r in view(store, "sources.fake")) == ["F1", "F2"]
    # the next run starts window `a` from the default again: the halted window never advanced
    rec2 = run_line(store, plan(queries=["a"]), "collect.fake")
    assert rec2.status == "ok" and rec2.scope_complete is True
    cov2 = {r["query"]: r for r in view(store, "coverage.fake")}
    assert (cov2["a"]["status"], cov2["a"]["attempts"]) == ("ok", 1)
    assert cov2["a"]["window_start_at"] == dt.datetime(2026, 1, 1, tzinfo=dt.UTC)


def test_verify_passes_on_the_collector_store(store: Path, fake: FakeHttp) -> None:
    fake.inject(API, {"q": "b", "page": 1}, status=503, times=3)
    assert run_line(store, plan(queries=["a", "b"]), "collect.fake").status == "ok"
    assert run_line(store, plan(queries=["a", "b"], to="2026-10-05"), "collect.fake").status == "ok"
    results = {r.check: r for r in run_checks(store)}
    red = {n: r.items for n, r in results.items() if r.status in ("failed", "warn")}
    assert red == {}
    assert results["V-sources"].status == "ok" and results["V-coverage"].status == "ok"


def test_unread_own_series_needs_restart(store: Path, fake: FakeHttp) -> None:
    """A run that does not read its own non-empty series would restart it without a base: the
    restart guard refuses (D377; the C32 "has files but is not pinned" guard it replaces), as a
    run failure when the destination comes from ``--series``, before any run for an ``--out``."""
    assert run_line(store, plan(queries=["a"]), "collect.fake").status == "ok"
    rec = run_stage(store, plan(queries=["a"]), "collect.fake", series="main")
    assert rec.status == "failed" and "is a restart; pass --restart" in (rec.error or ""), rec.error
    with pytest.raises(StageError, match="not empty and not read by this run"):
        run_stage(store, plan(queries=["a"]), "collect.fake", outs=[sdir("sources.fake"), sdir("coverage.fake")])


def test_provisional_record_shows_live_counts(store: Path, fake: FakeHttp) -> None:
    # Fix for the 40-minute arXiv window with `counts: {}`: the running record (D344:
    # runs/<head>.<stage>.json.zst) read from inside the fake's page handler shows the run's
    # counters before the run finishes.
    seen: list[tuple[str, dict[str, int]]] = []

    def spy(method: str, url: str, **kw: Any) -> Any:
        rec = read_run_record(next((store / "runs").glob("*.collect.fake.json.zst"))).to_json()
        seen.append((url, dict(rec["counts"])))
        return fake(method, url, **kw)

    INSTALLED["fake"] = spy
    rec = run_line(store, plan(queries=["a", "b"]), "collect.fake")
    assert rec.status == "ok", rec.error
    (u1, c1), _, (u3, c3) = seen
    assert "q=a" in u1 and "page=1" in u1 and c1["http_requests"] == 1  # first request beats at once
    # q=b page 1 follows window a's boundary heartbeat (its own request is inside the 15 s
    # throttle): a's two requests, pages, rows and the ok window are on disk
    assert "q=b" in u3 and c3["http_requests"] == 2 and c3["pages"] == 2
    assert (c3["rows_so_far"], c3["windows_ok"]) == (3, 1)
    assert rec.counts["rows_so_far"] == rec.counts["sources_written"] == 4


def test_journal_lives_under_the_store_cache(tmp_path: Path) -> None:
    class Resp:
        status, headers = 200, {"x-ratelimit-remaining": "9"}

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *a: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"ok": true}'

    j = JournalRequest("r1", opener=lambda *a, **k: Resp(), store_root=tmp_path / "store")
    assert j.root == tmp_path / "store" / "cache" / "provider-responses" / "r1"
    assert j("GET", "https://example.test/x").json() == {"ok": True}
    assert len(list(j.root.glob("*.json"))) == 1


def test_matched_by_lists_every_query_of_the_run(store: Path, fake: FakeHttp) -> None:
    """(f) F1 is returned by queries a and b: one row (``sources_repeat`` 1) whose ``matched_by``
    holds both, sorted and deduplicated (``attribute()`` adds without a second observation, a
    repeated entry is not doubled), joined to the coverage rows by digest; ``query_digest`` is the
    first query's. Unattributed rows keep ``matched_by`` NULL."""
    rec = run_line(store, plan(queries=["a", "b"], attribute="query"), "collect.fake")
    assert rec.status == "ok", rec.error
    assert rec.counts["sources_repeat"] == 1 and rec.counts["sources_written"] == 4
    src = {r["identity"]["external_id"]: r["metadata"] for r in view(store, "sources.fake")}
    cov = {r["query"]: r["query_digest"] for r in view(store, "coverage.fake")}
    e = lambda qid, q, label: {"query_id": qid, "query_digest": cov[q], "label": label}
    assert src["F1"]["matched_by"] == [e("a", "a", "term a"), e("b", "b", "term b"),
                                       e("extra:a", "a", None), e("extra:b", "b", None)]
    assert src["F1"]["query_digest"] == cov["a"]
    assert src["F3"]["matched_by"] == [e("a", "a", "term a")]
    assert src["F4"]["matched_by"] == [e("b", "b", "term b")] and src["F4"]["query_digest"] == cov["b"]
    red = {r.check: r.items for r in run_checks(store) if r.status in ("failed", "warn")}
    assert red == {}
    other = store.parent / "store2"
    assert run_line(other, plan(queries=["a"]), "collect.fake").status == "ok"
    assert {r["metadata"]["matched_by"] for r in view(other, "sources.fake")} == {None}


@pytest.mark.parametrize("bad, msg", [("metadata", "pass the query to observe"),
                                      ("label", "without the query")])
def test_attribution_protocol_refusals(store: Path, fake: FakeHttp, bad: str, msg: str) -> None:
    rec = run_line(store, plan(queries=["a"], attribute=bad), "collect.fake")
    assert rec.status == "failed" and msg in (rec.error or ""), rec.error


def test_mapping_query_without_query_id_is_its_jcs(store: Path, fake: FakeHttp) -> None:
    """A caller passing only a mapping ``query`` (``miss(query=...)`` predates ``query_id``) gets
    the mapping's JCS as ``query_id``."""
    rec = run_line(store, plan(queries=["a"], attribute="mapping"), "collect.fake")
    assert rec.status == "ok", rec.error
    ids = {m["query_id"] for r in view(store, "sources.fake") for m in r["metadata"]["matched_by"]}
    assert ids == {'{"q":"a"}'}


def test_a_sources_chain_that_does_not_read_refuses_the_next_run_before_it_starts(
        store: Path, fake: FakeHttp, monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``sources`` chain behind an ``Incompatible`` step (as every sources file before v4 is, D690): the next
    collector run is refused before it starts, naming the step's reason, and writes nothing."""
    from stack.schemas.upcasters import Incompatible
    from stack.stage_runner import StageError
    from tests.test_family_format import future_version

    assert run_line(store, plan(queries=["a"]), "collect.fake").status == "ok"
    files = sorted((store / "data").rglob("*.parquet"))
    future_version(monkeypatch, "sources", step=Incompatible("D999: test; re-collect"))
    with pytest.raises(StageError, match="IncompatibleFormat.*D999: test"):
        run_line(store, plan(queries=["a"], attribute="query", to="2026-10-05"), "collect.fake")
    assert sorted((store / "data").rglob("*.parquet")) == files


# ---------------------------------------------------------------- failed windows (R3-2, round 5)
@pytest.mark.slow  # real store + runner + builds
def test_failed_windows_scope_and_build_warning(store: Path, fake: FakeHttp) -> None:
    """A ``failed_transient`` window makes the run ``scope_complete = false`` and a build of it
    ``incomplete`` (reason ``windows_failed``); a ``failed_permanent`` window keeps the scope
    complete, is counted per error class and is a build warning naming the run."""
    from stack.build import run_build
    from stack.runs import read_run_records

    bplan = Plan.model_validate({"plan": "fakebuild", "schema_version": 2, "series": "main",
                                 "require_complete": ["sources", "coverage"],
                                 "stages": [{"stage": "collect.fake", "prior": ["sources", "coverage"],
                                             "options": {"queries": ["a", "p"], "permanent": ["p"]}}]})
    b1 = run_build(store, bplan)
    assert b1.status == "ok", b1.data.get("error")
    (rid,) = b1.data["runs"]
    rec = next(r for r in read_run_records(store) if r.run_id == rid)
    assert rec.scope_complete is True and rec.counts["windows_failed_permanent"] == 1
    assert rec.counts["windows_failed_permanent.query_invalid"] == 1
    warned = [w for w in b1.data["warnings"] if "failed_permanent" in w]
    assert warned == [(f"collect.fake run {rid}: 1 coverage window(s) failed_permanent (query_invalid x1); "
                       "the scope stays complete; the next run asks those windows again and they fail the "
                       "same way until the query or the channel is fixed")]
    assert "incomplete" not in b1.data
    # a transient failure: the build runs on, verifies and ends incomplete
    fake.inject(API, {"q": "b", "page": 1}, status=503, times=3)
    tplan = bplan.model_copy(update={"stages": [bplan.stages[0].model_copy(
        update={"options": {"queries": ["b"], "to": "2026-10-02"}})]})
    b2 = run_build(store, tplan)
    assert b2.status == "incomplete", b2.data.get("error")
    assert b2.data["verify"]["status"] == "ok"
    assert [(e["stage"], e["reasons"]) for e in b2.data["incomplete"]] == [("collect.fake", ["windows_failed"])]
    # the next build retries it (b answers now) and ends ok, continuing from the series heads
    # the incomplete build left (no prior-build selection, D373)
    heads = sorted(resolve_records(store, sdir(d)).head for d in ("sources.fake", "coverage.fake"))
    b3 = run_build(store, tplan)
    assert b3.status == "ok", b3.data.get("error")
    (r3,) = b3.data["runs"]
    assert sorted(next(r for r in read_run_records(store) if r.run_id == r3).prior) == heads


def test_a_failed_permanent_window_is_due_again_in_the_next_run() -> None:
    """S25-94: the build warning says a ``failed_permanent`` window is asked again by the next run
    (it said "not retried"): it moves no watermark (a window collector's ``until``) and is not an
    ``ok``/``partial`` attempt that ``relist_after`` holds back (a listing collector)."""
    from stack.collect.base import Collector, query_digest

    d = lambda s: dt.datetime.fromisoformat(s).replace(tzinfo=dt.UTC)
    c = Collector.__new__(Collector)
    c._coverage_view = [{"channel": "fake", "query_digest": query_digest("p"), "window_start_at": d("2026-10-01"),
                         "window_end_at": d("2026-10-01"), "status": "failed_permanent", "retry_after": None,
                         "last_attempt_at": d("2026-10-12")}]
    c.now, c.counts, c.held = d("2026-10-13"), {}, None
    assert c.watermark("fake", "p") is None
    assert c.due("fake", "p", until="2026-10-13") is True
    assert c.due("fake", "p", relist_after=dt.timedelta(days=7)) is True
    assert c.counts == {}


@pytest.mark.parametrize("soft", [False, True])
def test_a_capped_window_ends_the_run_short_and_the_build_incomplete(store: Path, fake: FakeHttp, soft: bool) -> None:
    """A5 / S25-21 (D492): a window a cap stopped short is ``partial``; the run ends ``scope_complete = false``
    (``windows_capped``, reason ``limit``) and a build of it ``incomplete``, not ``ok``. A ``partial`` that is
    not a cap (``truncated(cap=False)``) leaves the scope as the run says."""
    from stack.build import incomplete_reasons, run_build
    from stack.runs import read_run_records

    bplan = Plan.model_validate({"plan": "fakebuild", "schema_version": 2, "series": "main",
                                 "require_complete": ["sources", "coverage"],
                                 "stages": [{"stage": "collect.fake", "prior": ["sources", "coverage"],
                                             "options": {"queries": ["a"], "cap": 2, "soft": soft}}]})
    b = run_build(store, bplan)
    (rid,) = b.data["runs"]
    rec = next(r for r in read_run_records(store) if r.run_id == rid)
    assert [r["status"] for r in view(store, "coverage.fake")] == ["partial"]
    assert rec.counts["windows_partial"] == 1
    if soft:
        assert (rec.scope_complete, rec.counts.get("windows_capped"), b.status) == (True, None, "ok")
    else:
        assert (rec.scope_complete, rec.counts["windows_capped"], incomplete_reasons(rec.counts)) == (
            False, 1, ["limit"])
        assert b.status == "incomplete", b.data.get("error")
        assert [(e["stage"], e["reasons"]) for e in b.data["incomplete"]] == [("collect.fake", ["limit"])]


def test_a_capped_unit_not_asked_again_keeps_the_scope_short() -> None:
    """D492: a unit whose newest attempt was a cap ``partial`` and that ``due(partial_due=False)`` holds back
    (it would truncate the same way) is counted ``capped_not_due``, reason ``limit``: a second pass within
    ``relist_after`` must not turn the build ``ok``. ``truncated()`` marks a cap by default."""
    from stack.build import incomplete_reasons
    from stack.collect.base import Collector, Window, query_digest

    d = lambda s: dt.datetime.fromisoformat(s).replace(tzinfo=dt.UTC)
    c = Collector.__new__(Collector)
    c._coverage_view = [{"channel": "fake", "query_digest": query_digest("p"), "window_start_at": d("2026-10-01"),
                         "window_end_at": d("2026-10-01"), "status": "partial", "retry_after": None,
                         "last_attempt_at": d("2026-10-13T06:00:00")}]
    c.now, c.counts, c.held = d("2026-10-13T12:00:00"), {}, None
    assert c.due("fake", "p", relist_after=dt.timedelta(hours=12), partial_due=False) is False
    assert c.counts == {"not_due": 1, "capped_not_due": 1} and incomplete_reasons(c.counts) == ["limit"]
    c.counts = {}
    assert c.due("fake", "p", relist_after=dt.timedelta(hours=12)) is True and c.counts == {}  # partial_due
    w = Window("fake", "p", query_digest("p"), d("2026-10-01"), d("2026-10-02"))
    w.truncated()
    assert (w.outcome[0], w.capped) == ("partial", True)
    w.truncated(cap=False)
    assert (w.outcome[0], w.capped) == ("partial", False)


def test_date_windows_split_ascending_and_end_at_the_end() -> None:
    """Round 5 (RUN) B7a: consecutive fixed-length spans, the last one ending at ``end``; no split
    when the span fits or ``days`` is 0; a reversed window is refused."""
    import itertools

    from stack.collect.base import CollectError, date_windows

    d = dt.date
    assert date_windows(d(2025, 10, 1), d(2025, 10, 31), 30) == [(d(2025, 10, 1), d(2025, 10, 31))]
    assert date_windows(d(2025, 10, 1), d(2026, 10, 13), 0) == [(d(2025, 10, 1), d(2026, 10, 13))]
    spans = date_windows(d(2025, 10, 1), d(2026, 10, 13), 30)
    assert len(spans) == 13 and spans[0] == (d(2025, 10, 1), d(2025, 10, 31))
    assert spans[-1] == (d(2026, 9, 26), d(2026, 10, 13))
    assert all(a[1] == b[0] for a, b in itertools.pairwise(spans))
    assert date_windows(d(2026, 1, 1), d(2026, 1, 1), 30) == [(d(2026, 1, 1), d(2026, 1, 1))]
    with pytest.raises(CollectError, match="start after end"):
        date_windows(d(2026, 1, 2), d(2026, 1, 1), 30)


def test_holes_are_the_uncovered_spans_between_floor_and_watermark() -> None:
    """B7a "floor moved back": only `ok` windows of the query count, touching windows are
    contiguous, a floor the windows pass (window_days moving forward) leaves no hole, and `due`
    with `floor` keeps a covered query with a hole due."""
    from stack.collect.base import Collector, query_digest

    d = lambda s: dt.datetime.fromisoformat(s).replace(tzinfo=dt.UTC)
    row = lambda s, e, st="ok", q="a": {"channel": "fake", "query_digest": query_digest(q),
                                        "window_start_at": d(s), "window_end_at": d(e),
                                        "status": st, "retry_after": None, "last_attempt_at": None}
    c = Collector.__new__(Collector)
    c._coverage_view = [row("2025-10-01", "2026-01-01"), row("2026-01-01", "2026-10-13"),
                        row("2024-12-01", "2025-01-01", st="failed_transient"),
                        row("2024-01-01", "2026-10-13", q="other")]
    c.now, c.counts, c.held = d("2026-10-13"), {}, None
    assert c.holes("fake", "a", "2025-10-01") == []
    assert c.holes("fake", "a", "2025-11-15") == []                      # floor moved forward
    assert c.holes("fake", "a", "2024-11-30") == [(d("2024-11-30"), d("2025-10-01"))]
    assert c.holes("fake", "b", "2024-11-30") == []                      # no watermark: no hole
    c._coverage_view.append(row("2025-03-01", "2025-05-01"))
    assert c.holes("fake", "a", "2024-11-30") == [(d("2024-11-30"), d("2025-03-01")),
                                                  (d("2025-05-01"), d("2025-10-01"))]
    assert c.due("fake", "a", until="2026-10-13") is False and c.counts["already_covered"] == 1
    assert c.due("fake", "a", until="2026-10-13", floor="2024-11-30") is True
    assert c.due("fake", "a", until="2026-10-13", floor="2025-10-01") is False


def test_a_cap_env_override_is_counted_and_warned(store: Path, fake: FakeHttp, monkeypatch: pytest.MonkeyPatch,
                                                  capfd: pytest.CaptureFixture[str]) -> None:
    """S25-24 / L3-18 (D445, D553): a set ``STACK_COLLECT_LIMIT`` beats the plan's ``max_records`` with no
    trace in the digest; a run allowed to start with it (``allow_env_limits``) counts
    ``env_override.STACK_COLLECT_LIMIT`` = 1, says a warning line and records the values as ``env_limits``.
    A non-cap env override (``*_FROM``) is not counted; with nothing set there is no ``env_override.*`` key
    and no ``env_limits``."""
    monkeypatch.delenv("STACK_COLLECT_LIMIT", raising=False)
    rec0 = run_line(store, plan(max_records=100), "collect.fake")
    assert rec0.status == "ok", rec0.error
    assert not [k for k in rec0.counts if k.startswith("env_override.")]
    assert rec0.env_limits is None and "env_limits" not in rec0.to_json()
    monkeypatch.setenv("STACK_COLLECT_LIMIT", "50")
    monkeypatch.setenv("STACK_FAKE_FROM", "2026-01-01")
    capfd.readouterr()
    rec = run_line(store, plan(max_records=100), "collect.fake", allow_env_limits=True)
    assert rec.status == "ok", rec.error
    assert {k: v for k, v in rec.counts.items() if k.startswith("env_override.")} == {
        "env_override.STACK_COLLECT_LIMIT": 1}
    err = capfd.readouterr().err
    assert "warning: collect.fake: $STACK_COLLECT_LIMIT=50 overrides option 'max_records' (plan: 100)" in err
    stored = read_run_record(next((store / "runs").glob(f"*{rec.run_id}*.json.zst")))
    assert stored.env_limits == {"STACK_COLLECT_LIMIT": "50"}


def test_a_collector_run_refuses_cap_env_without_the_flag(store: Path, fake: FakeHttp,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    """D553: a ``collect.*`` run started while a cap/sampling variable is set is refused before any
    record (the error lists the variables and the flag); a run of another phase is not refused (no
    collector reads them)."""
    monkeypatch.setenv("STACK_COLLECT_FRACTION", "0.1")
    monkeypatch.setenv("STACK_BLOGS_LIMIT", "3")
    with pytest.raises(StageError, match=r"STACK_BLOGS_LIMIT=3, STACK_COLLECT_FRACTION=0\.1.*--allow-env-limits"):
        run_line(store, plan(), "collect.fake")
    assert not list(store.glob("runs/*.json.zst"))
    root = stage_runner.WORKFLOWS_ROOT
    (root / "noop").mkdir()
    (root / "noop" / "main.py").write_text(
        "from stack.stage_runner import StageResult\n\ndef run(ctx):\n    return StageResult(scope_complete=True)\n")
    rec = run_stage(store, plan(), "noop")
    assert rec.status == "ok" and rec.env_limits is None


def test_a_build_refuses_cap_env_unless_allowed_and_records_it(store: Path, fake: FakeHttp,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    """S25-24 (D445, D553): ``stack build`` refuses to start while a collector cap/sampling variable is
    set (``BuildFailed`` ``env_limits``, no record); with ``allow_env_limits`` it warns once at its start
    (the build's ``warnings``) and records the values as ``env_limits`` on the build and on each
    collector run; ``dry_run`` reports the verdict."""
    from stack.build import BuildFailed, dry_run, run_build

    said: list[str] = []
    clean = run_build(store, plan(), warn=said.append)
    assert clean.status == "ok" and not [m for m in said if "cap/sampling env" in m]
    assert "env_limits" not in clean.data and dry_run(store, plan())["env_limits"] is None
    monkeypatch.setenv("STACK_COLLECT_LIMIT", "50")
    monkeypatch.setenv("STACK_COLLECT_FRACTION", "0.5")
    builds = sorted(store.glob("builds/*"))
    with pytest.raises(BuildFailed) as exc:
        run_build(store, plan(), warn=said.append)
    assert exc.value.error_class == "env_limits" and "STACK_COLLECT_FRACTION=0.5, STACK_COLLECT_LIMIT=50" in str(exc.value)
    assert sorted(store.glob("builds/*")) == builds  # refused before any record
    assert dry_run(store, plan())["env_limits"]["allowed"] is False
    said.clear()
    res = run_build(store, plan(), warn=said.append, allow_env_limits=True)
    assert res.status == "ok"
    caps = [m for m in said if "cap/sampling env" in m]
    assert len(caps) == 1 and caps[0].endswith("STACK_COLLECT_FRACTION=0.5, STACK_COLLECT_LIMIT=50")
    assert caps[0] in res.data["warnings"]
    want = {"STACK_COLLECT_FRACTION": "0.5", "STACK_COLLECT_LIMIT": "50"}
    assert res.data["env_limits"] == want
    (run_file,) = store.glob(f"builds/*{res.build_id}*/runs/*.json.zst")
    assert read_run_record(run_file).env_limits == want
