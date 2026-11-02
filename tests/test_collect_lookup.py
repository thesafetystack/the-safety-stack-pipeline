"""The lookups ``collect.openalex-lookup`` / ``collect.s2-lookup`` (``workflows/fetch/metadata/main.py``)
end to end through ``stack.stage_runner.run_stage`` on a temporary store with a synthetic
``documents`` head (``tests/synth/rows.py``) and ``tests/fakes/http.py`` (no network):

(a) due keys: per document the first key in the provider's order, de-duplicated across documents
(``lookup_of_source_ids`` = the members carrying it), self-skip of documents with a member on the
provider's channel; (b) a hit is a ``record`` row with typed keys, the in-band abstract and its own
``source_id``, a miss the V1 miss row; one coverage row per batch; (c) run 2 mints no id, re-asks
only the missed key, and after ``miss_cap`` consecutive misses the key is done; (d) a contradicting
S2 answer is a miss; a batch failing after the ladder ends the run with its keys still due; a long
429 halts uncharged; (e) ``stack verify`` passes; a run without the documents input is refused.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest

from stack.checks.verify import run_checks
from stack.plan import Plan
from stack.runs import RunRecord
from stack.stage_runner import run_stage
from stack.store.blobs import open_blobs
from stack.store.chain import HeaderFields, read_view, resolve
from tests.fakes.http import FakeHttp
from tests.synth.records import record_hook, run_line, sdir, write_series
from tests.synth.rows import AT, dg, document_row, rid, sid, table, with_digests
from stack.collect.inband import json_record
from tests.synth.rows import content_entries
from workflows.fetch.metadata import main as lookup

Row = dict[str, Any]
OA, S2 = lookup.OPENALEX_URL, lookup.S2_BATCH_URL
ARXIV_DOI = "10.48550/arxiv.2610.00001"
ABSTRACT = ("We study scalable oversight of language models with debate between two copies, and "
            "find that judges agree with experts more often when the debaters are stronger.")


def doc(i: int, channel: str, keys: list[tuple[str, str]], **over: Any) -> Row:
    row = document_row(i)
    m = row["identity"]["members"][0]
    m.update(channel=channel, key_kind=keys[0][0], key_value=keys[0][1])
    row["identity"].update(channels=[channel],
                           keys=[{"kind": k, "value": v, "source_id": sid(i)} for k, v in keys],
                           **over)
    return with_digests(row)


DOCS = [
    doc(1, "arxiv", [("doi", ARXIV_DOI), ("arxiv", "2610.00001")]),
    doc(2, "semantic_scholar", [("doi", "10.1000/two")]),
    doc(3, "openalex", [("doi", "10.1000/three")]),
    doc(4, "arxiv", [("pmid", "123")]),
    doc(5, "arxiv", [("doi", ARXIV_DOI), ("arxiv", "2610.00001")]),  # same paper, not yet merged
]


def publish_documents(root: Path, rows: list[Row]) -> None:
    run_id = rid(90)
    fields = HeaderFields(stage="gather", run_id=run_id, config_digest=dg("cfg"),
                          code_digest=dg("code"), plan="test",
                          blocks_written=["identity", "metadata", "inband"])

    write_series(root, "documents", "documents", table("documents", rows), fields, at=AT, finished_at=AT,
                 on_published=record_hook(root))


def plan() -> Plan:
    return Plan.model_validate({"plan": "test", "schema_version": 2, "series": "main", "stages": [
        {"stage": "collect.openalex-lookup", "prior": ["sources", "coverage"],
         "keys": ["doi", "pmid", "arxiv"], "miss_cap": 2},
        {"stage": "collect.s2-lookup", "prior": ["sources", "coverage"], "keys": ["doi", "arxiv"],
         "miss_cap": 2}]})


def oa_params(by: str, values: list[str]) -> dict[str, Any]:
    return {"filter": f"{by}:" + "|".join(values), "per_page": lookup.OPENALEX_BATCH}  # no select (D525)


def work(wid: str, doi: str | None = None, **ids: str) -> Row:
    words = ABSTRACT.split()
    return {"id": f"https://openalex.org/{wid}", "doi": f"https://doi.org/{doi}" if doi else None,
            "ids": {"openalex": f"https://openalex.org/{wid}", **ids}, "title": f"Work {wid}",
            "publication_year": 2026, "publication_date": "2026-10-01", "type": "preprint",
            "cited_by_count": 3, "authorships": [{"author": {"display_name": "Ada Lovelace",
                                                             "orcid": None}, "institutions": []}],
            "abstract_inverted_index": {w: [i for i, x in enumerate(words) if x == w] for w in words}}


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "store"
    publish_documents(root, DOCS)
    monkeypatch.setattr(lookup, "_sleep", lambda s: None)
    monkeypatch.delenv("STACK_MAILTO", raising=False)
    monkeypatch.delenv(lookup._ENV_LIMIT, raising=False)
    return root


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeHttp:
    f = FakeHttp()
    monkeypatch.setattr(lookup, "SEND", f)
    return f


def view(root: Path, chain_id: str) -> dict[str, Row]:
    rows = read_view(resolve(root, sdir(chain_id))).to_pylist()
    key = "external_id" if chain_id.startswith("sources") else "query"
    return {(r["identity"][key] if key == "external_id" else r[key]): r for r in rows}


def lookup_run(root: Path, stage: str, **kw: Any) -> RunRecord:
    return run_line(root, plan(), stage, **kw)


@pytest.mark.slow  # real store + runner
def test_openalex_lookup_hits_misses_and_continuity(store: Path, fake: FakeHttp,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    fake.add(OA, oa_params("doi", [ARXIV_DOI, "10.1000/two"]),
             json={"results": [work("W1", ARXIV_DOI)]})
    fake.add(OA, oa_params("pmid", ["123"]), json={"results": [work(
        "W4", "10.1000/four", pmid="https://pubmed.ncbi.nlm.nih.gov/123")]})
    fake.add(OA, oa_params("doi", ["10.1000/two"]), json={"results": []})
    rec = lookup_run(store, "collect.openalex-lookup")
    assert rec.status == "ok", rec.error
    assert rec.scope_complete is True and "documents" not in rec.outputs
    assert (rec.counts["keys_due"], rec.counts["docs_skipped_self"], rec.counts["minted_ids"]) == (
        3, 1, 3)
    src = view(store, "sources.openalex-lookup")
    hit = src[f"doi:{ARXIV_DOI}"]
    assert (hit["state"], hit["record_format"], hit["identity"]["channel"]) == (
        "observed", "record", "openalex")
    assert [(k["kind"], k["value"]) for k in hit["identity"]["keys"]] == [
        ("doi", ARXIV_DOI), ("arxiv", "2610.00001"), ("openalex", "W1")]
    md = hit["metadata"]
    assert md["lookup_of_source_ids"] == [sid(1), sid(5)]  # one request for both documents
    assert (md["origin"], md["title"], md["abstract"], md["doc_type"]) == (
        "provider", "Work W1", ABSTRACT, "preprint")
    [entry] = content_entries(hit)
    blob = open_blobs(store).get(entry["text"]["sha256"])
    assert (entry["content_role"], blob.decode()) == ("abstract", ABSTRACT)
    assert (entry["raw"], entry["text"]["bytes"], entry["text"]["media_type"]) == (None, len(blob), "text/plain")
    assert json_record(open_blobs(store), hit) == work("W1", ARXIV_DOI)  # D522: the work as returned
    assert all("select=" not in c.url for c in fake.calls)  # D525: full works
    assert {("pmid", "123"), ("doi", "10.1000/four")} <= {
        (k["kind"], k["value"]) for k in src["pmid:123"]["identity"]["keys"]}
    miss = src["doi:10.1000/two"]
    assert (miss["state"], miss["record_format"], miss["status"], miss["inband"]["entries"],
            miss["metadata"]["miss_count"]) == ("missing", "miss", "empty", [], 1)
    assert "doi:10.1000/three" not in src  # doc 3 has an openalex member: self-skip
    cov = view(store, "coverage.openalex-lookup")
    assert sorted((json.loads(q)["by"], r["seen"], r["kept"], r["status"]) for q, r in cov.items()) \
        == [("doi", 2, 1, "ok"), ("pmid", 1, 1, "ok")]
    assert all(r["gate_digest"] is None for r in cov.values())

    # a second pass at once: hits are done and the missed key is not asked again yet (D226)
    calls = len(fake.calls)
    again = lookup_run(store, "collect.openalex-lookup")
    assert again.status == "ok" and again.outputs == {} and len(fake.calls) == calls
    assert (again.counts["keys_due"], again.counts["not_due"], again.counts["already_covered"],
            again.counts["http_requests"]) == (0, 1, 3, 0)  # docs 1 and 5 share a key
    # run 2 a day later: only the missed key is asked again, under its own source_id
    later = dt.datetime.now(dt.UTC) + dt.timedelta(days=1)
    monkeypatch.setattr("stack.collect.base.utcnow", lambda: later)
    rec2 = lookup_run(store, "collect.openalex-lookup")
    assert rec2.status == "ok", rec2.error
    assert (rec2.counts["keys_due"], rec2.counts["minted_ids"]) == (1, 0)
    assert len(fake.calls) == calls + 1
    src2 = view(store, "sources.openalex-lookup")
    assert src2["doi:10.1000/two"]["source_id"] == miss["source_id"]
    assert src2["doi:10.1000/two"]["metadata"]["miss_count"] == 2
    # run 3 (another day): miss_cap (2) consecutive misses: the key is done, nothing is asked
    monkeypatch.setattr("stack.collect.base.utcnow", lambda: later + dt.timedelta(days=1))
    rec3 = lookup_run(store, "collect.openalex-lookup")
    assert rec3.status == "ok" and rec3.counts["keys_due"] == 0 and rec3.outputs == {}
    assert len(fake.calls) == calls + 1
    red = {r.check: r.items for r in run_checks(store) if r.status in ("failed", "warn")}
    assert red == {}


@pytest.mark.slow  # real store + runner
def test_s2_lookup_prefers_arxiv_and_rejects_contradictions(store: Path, fake: FakeHttp) -> None:
    fake.add(S2, {"fields": lookup.S2_FIELDS}, method="POST", json=[
        {"paperId": "a" * 40, "externalIds": {"ArXiv": "2610.00001", "DOI": "10.1000/published"},
         "title": "Debate", "publicationDate": "2026-10-01", "abstract": ABSTRACT,
         "publicationTypes": ["JournalArticle"], "authors": [{"name": "Ada Lovelace"}]},
        {"paperId": "b" * 40, "externalIds": {"DOI": "10.1000/other"}, "title": "Wrong paper"}])
    rec = lookup_run(store, "collect.s2-lookup")
    assert rec.status == "ok", rec.error
    assert json.loads(fake.calls[0].body or b"{}") == {"ids": ["ARXIV:2610.00001",
                                                               "DOI:10.1000/three"]}
    src = view(store, "sources.s2-lookup")
    assert set(src) == {"arxiv:2610.00001", "doi:10.1000/three"}  # doc 2: self-skip; doc 4: no kind
    hit = src["arxiv:2610.00001"]
    assert hit["identity"]["channel"] == "semantic_scholar"
    assert {(k["kind"], k["value"]) for k in hit["identity"]["keys"]} >= {
        ("arxiv", "2610.00001"), ("doi", "10.1000/published"), ("s2", "a" * 40)}
    assert (hit["metadata"]["doc_type"], hit["metadata"]["lookup_of_source_ids"]) == (
        "journal", [sid(1), sid(5)])
    assert json_record(open_blobs(store), hit)["paperId"] == "a" * 40  # D522
    assert src["doi:10.1000/three"]["state"] == "missing"  # a different DOI answered
    assert rec.counts["conflicts"] == 1


def with_abstract(row: Row, text: str) -> Row:
    row["metadata"]["abstract"] = text
    return with_digests(row)


@pytest.mark.parametrize("only_missing", [None, False], ids=["default", "full-scope"])
def test_s2_lookup_asks_only_documents_without_a_usable_abstract(
        tmp_path: Path, fake: FakeHttp, monkeypatch: pytest.MonkeyPatch,
        only_missing: bool | None) -> None:
    """D520: by default S2 is asked only for documents without a usable abstract (docs 1 and 5
    have one, doc 3's is too short); ``only_missing_abstract: false`` asks every document as
    before. OpenAlex keeps the full scope."""
    root = tmp_path / "store"
    docs = [with_abstract(DOCS[0], ABSTRACT), DOCS[1], with_abstract(DOCS[2], "Too short."),
            DOCS[3], with_abstract(DOCS[4], ABSTRACT)]
    publish_documents(root, docs)
    monkeypatch.setattr(lookup, "_sleep", lambda s: None)
    p = plan()
    if only_missing is not None:
        p = p.model_copy(update={"stages": [
            s if s.stage != "collect.s2-lookup" else s.model_copy(
                update={"options": {"only_missing_abstract": only_missing}}) for s in p.stages]})
    n = 1 if only_missing is None else 2
    fake.add(S2, {"fields": lookup.S2_FIELDS}, method="POST", json=[None] * n)  # all misses
    rec = run_line(root, p, "collect.s2-lookup")
    assert rec is not None and rec.status == "ok", rec and rec.error
    [call] = fake.calls
    asked = json.loads(call.body or b"{}")["ids"]
    if only_missing is None:
        assert asked == ["DOI:10.1000/three"]
        assert rec.counts["docs_with_abstract"] == 2
    else:
        assert asked == ["ARXIV:2610.00001", "DOI:10.1000/three"]
        assert rec.counts["docs_with_abstract"] == 0


def test_lookup_scope_option_is_s2_only_and_strict() -> None:
    """The option is declared for S2 only (D434 refuses it on the OpenAlex line) and is a real
    boolean: a string such as ``"no"`` is refused rather than read as true."""
    assert "only_missing_abstract" in lookup.LOOKUP_OPTIONS["s2"]
    assert "only_missing_abstract" not in lookup.LOOKUP_OPTIONS["openalex"]
    assert lookup.ONLY_MISSING_ABSTRACT == {"openalex": False, "s2": True}
    assert lookup._flag(False) is False
    with pytest.raises(lookup.CollectError):
        lookup._flag("no")


@pytest.mark.slow  # real store + runner
def test_failed_batch_ends_the_run_and_long_429_halts(store: Path, fake: FakeHttp) -> None:
    fake.add(OA, oa_params("doi", [ARXIV_DOI, "10.1000/two"]), status=503)
    rec = lookup_run(store, "collect.openalex-lookup")
    assert rec.status == "ok" and rec.scope_complete is False, rec.error
    assert len(fake.calls) == 3  # the ladder (2 s, 4 s), then no pmid batch: provider blocked
    assert "sources" not in rec.outputs  # the keys got no row: still due
    [cov] = view(store, "coverage.openalex-lookup").values()
    assert (cov["status"], cov["error_class"], cov["attempts"]) == ("failed_transient", "http_5xx", 1)

    fake.add(S2, {"fields": lookup.S2_FIELDS}, method="POST", status=429,
             headers={"Retry-After": "86400"})
    rec2 = lookup_run(store, "collect.s2-lookup")
    assert rec2.status == "ok" and rec2.scope_complete is False and rec2.counts["halted"] == 1
    assert len(fake.calls) == 4  # one request, no retry
    [cov2] = view(store, "coverage.s2-lookup").values()
    assert (cov2["status"], cov2["error_class"], cov2["attempts"]) == (
        "failed_transient", "rate_limit", 0)
    assert cov2["retry_after"] > dt.datetime.now(dt.UTC)


def test_lookup_without_documents_or_miss_cap_is_refused(store: Path, fake: FakeHttp) -> None:
    rec = run_stage(store, plan(), "collect.openalex-lookup", series="main")  # no --in: reads nothing
    assert rec.status == "failed" and "documents" in (rec.error or ""), rec.error
    bare = Plan.model_validate({"plan": "test", "schema_version": 2, "series": "main", "stages": [
        {"stage": "collect.s2-lookup", "prior": ["sources", "coverage"]}]})
    rec2 = run_line(store, bare, "collect.s2-lookup")
    assert rec2.status == "failed" and "miss_cap" in (rec2.error or "")
    assert fake.calls == []
