"""``gather`` merge and carry (T2.7b; §5.3, §6, §2.5; D6, D7, D19, D74, D75): the ``metadata``
merge over members (``fields`` overrides > provider rank > newest ``observed_at``, ``empty_is_absent``,
author union), D6's content carry on a merge and its distribution on a split, the state moves of
§2.5, and the byte-for-byte full write over unchanged sources (C6). The v3 referents are the
``read`` rows of the mined ``tests/fixtures/v3_scenarios/overrides_prune_read.yaml``
(``effective_meta_precedence``, ``metadata_override_canonical``).

Every case runs the real stage through ``stack.stage_runner.run_stage`` (the store helper of
``tests/test_gather_identity.py``) over synthetic rows from ``tests/synth/rows.py`` (D46); the
store-level checks run through ``stack.checks.verify.run_checks``.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
import yaml

from stack.checks.digests import row_fields_digest, row_members_digest
from stack.checks.verify import run_checks
from stack.runs import RunRecord, write_final_record
from stack.store.chain import HeaderFields, Published, resolve
from stack.store.parquet import read_family
from tests.fakes.overrides import add_overrides
from tests.synth.records import sdir, write_series
from tests.synth.rows import dg, document_row, rid, sid, table
from tests.test_gather_identity import Store, member, members, merge, separate, src

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "tests" / "fixtures" / "v3_scenarios" / "overrides_prune_read.yaml"
#: v3 record family → the v4 channel that plays its part in the provider rank: the scholarly
#: lookup (``fetch.metadata``) is OpenAlex, the discovery carry-through (``collect.meta``) arXiv.
V3_FAMILY = {"fetch.metadata": "openalex", "collect.meta": "arxiv"}
V3_FIELD = {"title": "title", "pub_date": "published_at"}

Row = dict[str, Any]


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path)


def _read_rows(row_id: str) -> Row:
    return next(r for r in yaml.safe_load(FIXTURE.read_text())["read"] if r["id"] == row_id)


def fields(store: Store, entries: list[Row]) -> None:
    """Add the ``fields`` overrides among ``entries`` the overrides series lacks (``overrides.add``)."""
    add_overrides(store.root, "fields", entries, only_new=True)


def fsrc(doc: Row, f: str) -> list[tuple[str, str]]:
    return [(e["source_id"], e["origin"]) for e in doc["metadata"]["field_sources"] if e["field"] == f]


def md(i: int, **over: Any) -> Row:
    """A ``sources.metadata`` for ``sid(i)`` with ``over`` applied."""
    return {**src(i, [])["metadata"], **over}


def output(store: Store, rec: RunRecord) -> tuple[pa.Table, dict[str, str]]:
    path = Path(rec.outputs["documents"]["path"])
    return read_family(path if path.is_absolute() else store.root / path)


def write_documents_part(store: Store, rows: list[Row], stage: str, blocks: list[str]) -> None:
    """Publish ``rows`` as a part on the last file of ``data/documents/main/`` with an ``ok``
    record, as ``triage`` or ``acquire`` would (their owned blocks only)."""
    store.runs += 1
    n = 500 + store.runs
    at = dt.datetime.now(dt.UTC)  # a real run's time: X17 wants updated_at non-decreasing
    for r in rows:
        r["run_id"], r["updated_at"] = rid(n), at
    hf = HeaderFields(stage=stage, run_id=rid(n), config_digest=dg("cfg"), code_digest=dg("code"),
                      plan="dev-v2", blocks_written=blocks)

    def hook(pubs: list[Published]) -> None:
        write_final_record(store.root, RunRecord(
            run_id=rid(n), stage=stage, writer=stage, plan="dev-v2", status="ok", started_at=at,
            heartbeat_at=at, finished_at=at, code_digest=dg("code"), config_digest=dg("cfg"),
            scope_complete=True, version={"rows_touched": len(rows)},
            outputs={"documents": pubs[0].as_output()}))

    write_series(store.root, "documents", "documents", table("documents", rows), hf, kind="part", at=at,
                 finished_at=at, on_published=hook)


def triage(store: Store, *doc_ids: str) -> None:
    """``triage`` marks ``doc_ids`` triaged on their current ``fields_digest`` (S9)."""
    docs = store.docs()
    rows = []
    for d in doc_ids:
        r = copy.deepcopy(docs[d])
        r["state"] = "triaged"
        r["annotations"]["triage"] = copy.deepcopy(document_row(0, state="triaged")["annotations"]["triage"])
        r["annotations"]["triage"]["basis_digest"] = r["metadata"]["fields_digest"]
        rows.append(r)
    write_documents_part(store, rows, "triage", ["annotations"])


def acquire(store: Store, doc_id: str, n: int) -> None:
    """``acquire`` gives ``doc_id`` (triaged) a fetched-and-converted ``full_text`` per member,
    the pointer on the anchor's, ``content.members_digest`` = the identity's (S9); snapshot ids
    ``rid(n).<k>``."""
    r = copy.deepcopy(store.docs()[doc_id])
    proto = document_row(0, state="acquired")["content"]
    snaps, roles = [], []
    for k, s in enumerate(members(r)):
        snap = {**proto["snapshots"][0], "snapshot_id": f"{rid(n)}.{k}", "source_id": s,
                "text": {**proto["snapshots"][0]["text"], "sha256": dg(("text", s))},
                "raw": {**proto["snapshots"][0]["raw"], "sha256": dg(("raw", s))}}
        snaps.append(snap)
        roles.append({**proto["roles"][0], "source_id": s, "snapshot_id": snap["snapshot_id"],
                      "last_source_id": s, "attempts": 2})
    cur = snaps[0]
    r["content"] = {**proto, "snapshots": snaps, "roles": roles, "current_snapshot_id": cur["snapshot_id"],
                    "current_source_id": cur["source_id"], "current_text_sha256": cur["text"]["sha256"],
                    "members_digest": r["identity"]["members_digest"]}
    r["state"] = "acquired"
    write_documents_part(store, [r], "acquire", ["content"])


def checks_ok(store: Store) -> None:
    """S1-S10, V3, V4, V6, X15, X17, X22 over every file of the store (each file vs its base view)."""
    results = {r.code: r for r in run_checks(store.root)}
    wanted = [c for c in results if c[0] == "S"] + ["V3", "V4", "V6", "X15", "X17", "X22"]
    bad = {c: (results[c].status, results[c].error_class, results[c].items) for c in wanted
           if results[c].status != "ok"}
    assert bad == {}


# ---------------------------------------------------------------- the field merge
def test_provider_rank_decides_and_a_missing_field_falls_through(store: Store) -> None:
    """v3 ``effective_meta_precedence``: the scholarly record's title wins; its missing date
    falls through to the discovery record."""
    case = _read_rows("effective_meta_precedence")
    rows: dict[str, list[Row]] = {}
    for k, m in enumerate(case["metadata"]):  # the discovery record is observed first (anchor)
        ch = V3_FAMILY[m["workflow"]]
        vals = {V3_FIELD[f]: (dt.datetime.fromisoformat(v).replace(tzinfo=dt.UTC) if f == "pub_date" else v)
                for f, v in m["payload"].items()}
        meta = md(k + 1, **{"title": None, "published_at": None, **vals})
        rows.setdefault(f"sources.{'openalex' if ch == 'openalex' else 'arxiv_sweep'}", []).append(
            src(k + 1, [("doi", "10.1000/x")], channel=ch, metadata=meta))
    for chain, rs in rows.items():
        store.add(chain, rs)
    store.gather()
    (doc,) = store.docs().values()
    want = case["expect"]
    assert doc["doc_id"] == sid(1)  # the anchor is the discovery record; rank is not the anchor rule
    assert doc["metadata"]["title"] == want["title"]
    assert doc["metadata"]["published_at"] == dt.datetime.fromisoformat(want["pub_date"]).replace(tzinfo=dt.UTC)
    assert fsrc(doc, "title") == [(sid(2), "provider")] and fsrc(doc, "published_at") == [(sid(1), "provider")]


def test_newest_observed_breaks_a_rank_tie(store: Store) -> None:
    store.add("sources.arxiv_sweep", [src(1, [("doi", "10.1000/x")], metadata=md(1, title="Old")),
                                      src(2, [("doi", "10.1000/x")], t=30, metadata=md(2, title="New"))])
    store.gather()
    (doc,) = store.docs().values()
    assert doc["metadata"]["title"] == "New" and fsrc(doc, "title") == [(sid(2), "provider")]


def test_empty_is_absent(store: Store) -> None:
    store.add("sources.openalex", [src(1, [("doi", "10.1000/x")], channel="openalex",
                                       metadata=md(1, title="", abstract=None, authors=[]))])
    store.add("sources.arxiv_sweep", [src(2, [("doi", "10.1000/x")], metadata=md(2, title="T", abstract="A"))])
    store.gather()
    (doc,) = store.docs().values()
    m = doc["metadata"]
    assert (m["title"], m["abstract"]) == ("T", "A")
    assert fsrc(doc, "title") == [(sid(2), "provider")] and fsrc(doc, "authors") == [(sid(2), "provider")]


def test_author_union_in_provider_order(store: Store) -> None:
    def au(*names: str) -> list[Row]:
        return [{"name": n, "orcid": None, "affiliation_raw": None} for n in names]

    store.add("sources.arxiv_sweep", [src(1, [("doi", "10.1000/x")],
                                          metadata=md(1, authors=au("A. Author", "Carol Third")))])
    store.add("sources.openalex", [src(2, [("doi", "10.1000/x")], channel="openalex",
                                       metadata=md(2, authors=au("Alice Author", "Bob Second")))])
    store.gather()
    (doc,) = store.docs().values()
    assert [a["name"] for a in doc["metadata"]["authors"]] == ["Alice Author", "Bob Second", "Carol Third"]
    assert fsrc(doc, "authors") == [(sid(2), "provider"), (sid(1), "provider")]


def test_fields_override_is_human_and_wins(store: Store) -> None:
    """v3 ``metadata_override_canonical``: the human value is canonical, ``origin = 'human'``;
    a document-level entry beats a member-level one, which beats every provider."""
    case = next(r for r in yaml.safe_load(FIXTURE.read_text())["overrides"]
                if r["id"] == "metadata_override_canonical")
    store.add("sources.openalex", [src(1, [("doi", "10.1000/x")], channel="openalex",
                                       metadata=md(1, title=case["target"]["payload"]["title"]))])
    store.add("sources.arxiv_sweep", [src(2, [("doi", "10.1000/x")], metadata=md(2, abstract="prov"))])
    fields(store, [
        {"override_id": "ov-f1", "doc_id": sid(1), "field": "title", "value": case["override"]["payload"]["title"]},
        {"override_id": "ov-f2", "source_id": sid(2), "field": "title", "value": "member-level"},
        {"override_id": "ov-f3", "source_id": sid(2), "field": "published_at", "value": "2026-01-01"},
    ])
    rec = store.gather()
    (doc,) = store.docs().values()
    m = doc["metadata"]
    assert m["title"] == "Human" and fsrc(doc, "title") == [(sid(1), "human")]
    assert m["published_at"] == dt.datetime(2026, 1, 1, tzinfo=dt.UTC) and fsrc(doc, "published_at") == [(sid(2), "human")]
    assert fsrc(doc, "abstract") == [(sid(1), "provider")]  # untouched fields stay provider
    assert doc["identity"]["override_ids"] == ["ov-f1", "ov-f2", "ov-f3"]
    assert m["fields_digest"] == row_fields_digest(doc)
    # the effective overrides are part of the stage's config_digest
    fields(store, [{"override_id": "ov-f4", "supersedes": "ov-f1", "doc_id": sid(1), "field": "title",
                    "value": "Other"}])
    rec2 = store.gather()
    assert rec2.config_digest != rec.config_digest
    assert store.docs()[sid(1)]["metadata"]["title"] == "Other"
    assert rec2.inputs["overrides"] != rec.inputs["overrides"]  # each run pins the head it read


def test_later_fields_row_of_one_target_and_field_wins(store: Store) -> None:
    """D337: two rows for one document and field (refused as "set twice" before the family): the
    later ``created_at`` wins, whatever the ids; equal times fall back to the id."""
    store.add("sources.arxiv_sweep", [src(1, [("doi", "10.1000/x")])])
    fields(store, [{"override_id": "ov-b", "doc_id": sid(1), "field": "title", "value": "older",
                    "created_at": "2026-01-01T00:00:00Z"},
                   {"override_id": "ov-a", "doc_id": sid(1), "field": "title", "value": "newer",
                    "created_at": "2026-01-02T00:00:00Z"}])
    store.gather()
    doc = store.docs()[sid(1)]
    assert doc["metadata"]["title"] == "newer" and doc["identity"]["override_ids"] == ["ov-a"]


def test_fields_override_naming_an_unknown_document_fails_the_run(store: Store) -> None:
    store.add("sources.arxiv_sweep", [src(1, [("doi", "10.1000/x")])])
    fields(store, [{"override_id": "ov-f", "doc_id": sid(9), "field": "title", "value": "x"}])
    from stack.stage_runner import run_stage
    rec = run_stage(store.root, store.plan, "gather", [sdir("sources.arxiv_sweep"), sdir("overrides")], series="main")
    assert rec.status == "failed" and "fields override names doc_id" in (rec.error or "")


# ---------------------------------------------------------------- states, stamps, C6
@pytest.mark.slow
def test_regather_writes_identical_row_bytes(store: Store) -> None:
    store.add("sources.arxiv_sweep", [src(1, [("doi", "10.1000/x")]), src(2, [("doi", "10.1000/x")]),
                                      src(3, [("arxiv", "2610.00003")])])
    store.add("sources.openalex", [src(4, [("doi", "10.1000/x")], channel="openalex", metadata=md(4, title="OA"))])
    fields(store, [{"override_id": "ov-f", "source_id": sid(3), "field": "doc_type", "value": "report"}])
    store.gather()
    triage(store, sid(1))
    acquire(store, sid(1), 801)
    first = store.gather()  # a full file after the triage and acquire parts
    second = store.gather()
    t1, h1 = output(store, first)
    t2, h2 = output(store, second)
    assert h1["kind"] == h2["kind"] == "full" and h2["base"] == Path(first.outputs["documents"]["path"]).name
    assert t1.num_rows == t2.num_rows == 2
    for i in range(t1.num_rows):
        assert t1.slice(i, 1).equals(t2.slice(i, 1)), t1.slice(i, 1).to_pylist()
    assert second.version is not None and second.version["rows_touched"] == 0
    assert second.counts["changed"] == 0 and second.counts["content_carried"] == 0
    assert json.loads(h2["blocks_written"]) == ["identity", "metadata", "inband"]
    flat = [(r["doc_id"], r["state"], r["run_id"], r["created_at"], r["updated_at"]) for r in t2.to_pylist()]
    assert flat == [(r["doc_id"], r["state"], r["run_id"], r["created_at"], r["updated_at"]) for r in t1.to_pylist()]
    assert store.docs()[sid(1)]["state"] == "acquired"  # nothing changed, so nothing demoted
    checks_ok(store)


def test_provider_title_change_moves_fields_digest_and_state(store: Store) -> None:
    store.add("sources.arxiv_sweep", [src(1, [("arxiv", "2610.00001")]), src(2, [("arxiv", "2610.00002")])])
    store.gather()
    triage(store, sid(1), sid(2))
    before = store.docs()
    assert before[sid(1)]["state"] == "triaged"
    store.add("sources.arxiv_sweep", [src(1, [("arxiv", "2610.00001")], t=60, metadata=md(1, title="Paper 1 v2"))])
    store.gather()
    after = store.docs()
    a, b = after[sid(1)], before[sid(1)]
    assert a["metadata"]["title"] == "Paper 1 v2"
    assert a["metadata"]["fields_digest"] != b["metadata"]["fields_digest"] == b["annotations"]["triage"]["basis_digest"]
    assert a["state"] == "identified" and a["identity"]["members_digest"] == b["identity"]["members_digest"]
    assert a["annotations"] == b["annotations"]  # carried, now stale (§2.5)
    assert a["created_at"] == b["created_at"] and a["updated_at"] > b["updated_at"]
    assert after[sid(2)] == before[sid(2)]  # untouched row: carried byte for byte, still triaged
    checks_ok(store)


# ---------------------------------------------------------------- published_at precision
def ts(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s).replace(tzinfo=dt.UTC)


@pytest.mark.slow
def test_year_only_date_never_replaces_a_precise_one(store: Store) -> None:
    """VERIFY-2A's merge (doc shcnz0x6q1ajb): a Semantic Scholar paper dated 2025-12-08 is triaged; an
    OpenAlex work on the same DOI joins, dated 2025-01-01 (OpenAlex sends a year-only date as
    ``<year>-01-01``; S2's collector widens a bare ``year`` the same way). OpenAlex ranks first, but its
    year-only date does not replace the precise one, so ``published_at`` and ``fields_digest`` stay and
    the stored triage answer is still on its basis (the row is ``identified`` for its new member; triage
    reuses an answer whose ``basis_digest`` equals ``fields_digest``). Before the rule the merge took
    2025-01-01, moved ``fields_digest`` and so made the stored answer stale."""
    doi = "10.48550/arxiv.2512.13714"
    store.add("sources.semantic_scholar", [src(1, [("doi", doi)], channel="semantic_scholar",
                                               metadata=md(1, published_at=ts("2025-12-08")))])
    store.gather()
    triage(store, sid(1))
    before = store.docs()[sid(1)]
    store.add("sources.openalex", [src(2, [("doi", doi)], channel="openalex",
                                       metadata=md(2, title=None, abstract=None, authors=[],
                                                   published_at=ts("2025-01-01")))])
    store.gather()
    after = store.docs()[sid(1)]
    assert sorted(members(after)) == [sid(1), sid(2)]
    assert after["metadata"]["published_at"] == ts("2025-12-08")
    assert fsrc(after, "published_at") == [(sid(1), "provider")]
    assert after["metadata"]["fields_digest"] == before["metadata"]["fields_digest"]
    assert after["annotations"]["triage"]["basis_digest"] == after["metadata"]["fields_digest"]
    assert after["state"] == "identified"  # members_digest moved
    checks_ok(store)


@pytest.mark.parametrize(("oa", "s2", "want", "who"), [
    ("2025-01-01", "2025-12-08", "2025-12-08", 2),  # a precise date beats a year-only one of a better-ranked member
    ("2026-01-01", "2025-08-05", "2025-08-05", 2),  # in another year too: a year-only date is not a real day
    ("2025-01-01", "2024-01-01", "2025-01-01", 1),  # both year-only: provider order, as before
    ("2025-03-01", "2025-12-08", "2025-03-01", 1),  # a first of the month is a day (month precision not inferred)
    ("2025-01-01T09:30:00", "2025-12-08", "2025-01-01T09:30:00", 1),  # a time of day: not year-only
    ("2025-06-02", None, "2025-06-02", 1),
    (None, "2025-01-01", "2025-01-01", 2),  # a year-only date is still a date when it is the only one
])
def test_published_at_precision_then_provider_order(store: Store, oa: str | None, s2: str | None, want: str,
                                                    who: int) -> None:
    store.add("sources.openalex", [src(1, [("doi", "10.1000/x")], channel="openalex",
                                       metadata=md(1, published_at=oa and ts(oa)))])
    store.add("sources.semantic_scholar", [src(2, [("doi", "10.1000/x")], channel="semantic_scholar",
                                               metadata=md(2, published_at=s2 and ts(s2)))])
    store.gather()
    (doc,) = store.docs().values()
    assert doc["metadata"]["published_at"] == ts(want) and fsrc(doc, "published_at") == [(sid(who), "provider")]
    assert fsrc(doc, "title") == [(sid(1), "provider")]  # the other fields keep the provider order


def test_fields_override_still_beats_a_precise_date(store: Store) -> None:
    store.add("sources.openalex", [src(1, [("doi", "10.1000/x")], channel="openalex",
                                       metadata=md(1, published_at=ts("2025-01-01")))])
    store.add("sources.semantic_scholar", [src(2, [("doi", "10.1000/x")], channel="semantic_scholar",
                                               metadata=md(2, published_at=ts("2025-12-08")))])
    fields(store, [{"override_id": "ov-d", "source_id": sid(1), "field": "published_at", "value": "2024-01-01"}])
    store.gather()
    (doc,) = store.docs().values()
    assert doc["metadata"]["published_at"] == ts("2024-01-01") and fsrc(doc, "published_at") == [(sid(1), "human")]


def test_next_gather_repairs_a_date_an_earlier_merge_degraded(store: Store) -> None:
    """A row stored with the year-only date an earlier merge picked (its ``fields_digest`` and triage
    basis on that date) gets the precise date back on the next gather, with a new ``fields_digest``
    and state ``identified`` (re-triaged, §2.5): the repair needs no step of its own."""
    store.add("sources.openalex", [src(1, [("doi", "10.1000/x")], channel="openalex",
                                       metadata=md(1, published_at=ts("2025-01-01")))])
    store.add("sources.semantic_scholar", [src(2, [("doi", "10.1000/x")], channel="semantic_scholar",
                                               metadata=md(2, published_at=ts("2025-12-08")))])
    store.gather()
    good = store.docs()[sid(1)]
    old = copy.deepcopy(good)
    old["metadata"]["published_at"] = ts("2025-01-01")
    old["metadata"]["field_sources"] = [
        e if e["field"] != "published_at" else {**e, "source_id": sid(1)} for e in old["metadata"]["field_sources"]]
    old["metadata"]["fields_digest"] = row_fields_digest(old)
    write_documents_part(store, [old], "gather", ["metadata"])  # as the earlier code's gather left it
    triage(store, sid(1))
    assert store.docs()[sid(1)]["state"] == "triaged"
    store.gather()
    after = store.docs()[sid(1)]
    assert after["metadata"]["published_at"] == ts("2025-12-08")
    assert after["metadata"]["fields_digest"] == good["metadata"]["fields_digest"] != old["metadata"]["fields_digest"]
    assert after["state"] == "identified"


# ---------------------------------------------------------------- D6 carry
@pytest.mark.slow
def test_merge_carries_the_retired_rows_content(store: Store) -> None:
    store.add("sources.arxiv_sweep", [src(1, [("arxiv", "2610.00001")]), src(2, [("arxiv", "2610.00002")])])
    store.gather()
    triage(store, sid(1), sid(2))
    acquire(store, sid(1), 801)
    acquire(store, sid(2), 802)
    checks_ok(store)
    before = store.docs()
    store.add("sources.openalex", [src(3, [("arxiv", "2610.00001"), ("arxiv", "2610.00002")], channel="openalex")])
    rec = store.gather()
    docs = store.docs()
    surv, gone = docs[sid(1)], docs[sid(2)]
    assert members(surv) == [sid(1), sid(2), sid(3)] and gone["identity"]["merged_into"] == sid(1)
    c, own, theirs = surv["content"], before[sid(1)]["content"], before[sid(2)]["content"]
    assert [s["snapshot_id"] for s in c["snapshots"]] == [f"{rid(801)}.0", f"{rid(802)}.0"]
    assert {(r["source_id"], r["content_role"]): r["attempts"] for r in c["roles"]} == {
        (sid(1), "full_text"): 2, (sid(2), "full_text"): 2}  # unioned, never summed
    # pointer fields and content.members_digest left for acquire (it regroups, X21)
    assert c["current_snapshot_id"] == own["current_snapshot_id"] and c["members_digest"] == own["members_digest"]
    assert c["members_digest"] != surv["identity"]["members_digest"] == row_members_digest(surv)
    assert surv["state"] == "identified" and c["run_id"] == rec.run_id
    assert gone["state"] == "retired" and gone["content"] == theirs  # frozen
    _, h = output(store, rec)
    assert json.loads(h["blocks_written"]) == ["identity", "metadata", "inband", "content"]
    assert rec.counts["content_carried"] == 1
    checks_ok(store)
    # and the next gather over unchanged sources carries everything as is
    again = store.gather()
    assert again.counts["changed"] == 0 and store.docs() == docs
    _, h2 = output(store, again)
    assert json.loads(h2["blocks_written"]) == ["identity", "metadata", "inband"]


@pytest.mark.slow
def test_split_gives_each_part_its_members_entries(store: Store) -> None:
    store.add("sources.arxiv_sweep", [src(1, [("doi", "10.1000/x")]), src(2, [("doi", "10.1000/x")]),
                                      src(3, [("doi", "10.1000/x")])])
    store.gather()
    triage(store, sid(1))
    acquire(store, sid(1), 801)
    store.overrides([separate("ov-s", sid(1), sid(3)), separate("ov-s2", sid(2), sid(3))])
    store.gather()
    docs = store.docs()
    keep, part = docs[sid(1)], docs[sid(3)]
    assert members(keep) == [sid(1), sid(2)] and members(part) == [sid(3)]
    assert [s["source_id"] for s in part["content"]["snapshots"]] == [sid(3)]
    assert [r["source_id"] for r in part["content"]["roles"]] == [sid(3)]
    # the part's base came from the donor whose pointer named the anchor's snapshot: cleared
    assert part["content"]["current_snapshot_id"] is None and part["content"]["current_text_status"] == "no_raw"
    assert len(keep["content"]["snapshots"]) == 3  # the continuing document keeps its block (§5.5)
    assert part["annotations"]["triage"] is None and part["state"] == "identified"
    checks_ok(store)


@pytest.mark.slow
def test_merge_override_carries_content_onto_a_survivor_without_content(store: Store) -> None:
    store.add("sources.arxiv_sweep", [src(1, [("arxiv", "2610.00001")]), src(2, [("arxiv", "2610.00002")])])
    store.gather()
    triage(store, sid(2))
    acquire(store, sid(2), 802)
    theirs = store.docs()[sid(2)]["content"]
    store.overrides([merge("ov-m", sid(1), sid(2))])
    store.gather()
    surv = store.docs()[sid(1)]
    c = surv["content"]
    assert [s["snapshot_id"] for s in c["snapshots"]] == [s["snapshot_id"] for s in theirs["snapshots"]]
    assert c["current_snapshot_id"] == theirs["current_snapshot_id"]  # the donor's pointer still resolves
    assert member(surv, sid(2))["edge_kind"] == "human:merge"
    checks_ok(store)


# ---------------------------------------------------------------- the CLI
@pytest.mark.slow
def test_stack_run_gather_cli_writes_a_full_file_that_verifies(store: Store) -> None:
    store.add("sources.arxiv_sweep", [src(1, [("doi", "10.1000/x")]), src(2, [("arxiv", "2610.00002")])])
    store.add("sources.openalex", [src(3, [("doi", "10.1000/x")], channel="openalex", metadata=md(3, title="OA"))])
    store.gather()  # data/documents/main/ has a file, so the CLI must pass it

    def stack_run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["uv", "run", "--no-sync", "stack", "run", "gather", *args, "--store", str(store.root),
             "--plan", str(REPO / "configs" / "plans" / "dev-v2.yaml")],
            check=False, cwd=REPO, env=dict(os.environ), capture_output=True, text=True, timeout=120)

    store.add("sources.arxiv_sweep", [src(2, [("arxiv", "2610.00002")], t=90, metadata=md(2, title="v2"))])
    proc = stack_run("--in", sdir("sources.arxiv_sweep"), "--in", sdir("sources.openalex"),
                     "--in", sdir("documents"))
    assert proc.returncode == 0, proc.stderr
    docs = store.docs()
    assert docs[sid(1)]["metadata"]["title"] == "OA" and docs[sid(2)]["metadata"]["title"] == "v2"
    _, h = read_family(resolve(store.root, sdir("documents")).files[-1].path)
    assert h["kind"] == "full"
    checks_ok(store)


def test_an_alignment_forum_member_implies_lesswrong_without_a_state_change(store: Store) -> None:
    """D770: a document with an Alignment Forum member lists ``lesswrong`` in ``identity.channels`` and the post's
    LessWrong URL key in ``identity.keys`` (with the member's id); a head written before (no implied channel or key)
    gets them at the next gather with its state, digests and annotations kept: nothing is triaged or asked again."""
    store.add("sources.forums", [src(1, [("fm_post", "AbC1")], channel="alignmentforum"),
                                 src(2, [("fm_post", "XyZ2")], channel="lesswrong"),
                                 src(3, [("fm_post", "5bd75cc58225bf06703751a3")], channel="alignmentforum"),
                                 src(4, [("fm_post", "5bd75cc58225bf06703751a3")], channel="lesswrong", t=30)])
    store.gather()
    d = store.docs()
    assert members(d[sid(3)]) == [sid(3), sid(4)]
    assert d[sid(1)]["identity"]["channels"] == ["alignmentforum", "lesswrong"]
    assert d[sid(2)]["identity"]["channels"] == ["lesswrong"]
    keys = {(k["kind"], k["value"], k["source_id"]) for k in d[sid(1)]["identity"]["keys"]}
    assert keys == {("fm_post", "AbC1", sid(1)), ("url", "https://lesswrong.com/posts/AbC1", sid(1))}
    assert ("url", "https://lesswrong.com/posts/5bd75cc58225bf06703751a3", sid(3)) in {
        (k["kind"], k["value"], k["source_id"]) for k in d[sid(3)]["identity"]["keys"]}
    assert not [k for k in d[sid(2)]["identity"]["keys"] if k["kind"] == "url"]  # a LessWrong member implies nothing
    # a head from before D770: triaged, without the implied channel and key
    old = copy.deepcopy(d[sid(1)])
    old["state"] = "triaged"
    old["annotations"]["triage"] = copy.deepcopy(document_row(0, state="triaged")["annotations"]["triage"])
    old["annotations"]["triage"]["basis_digest"] = old["metadata"]["fields_digest"]
    old["identity"]["channels"] = ["alignmentforum"]
    old["identity"]["keys"] = [k for k in old["identity"]["keys"] if k["kind"] != "url"]
    write_documents_part(store, [old], "triage", ["annotations"])
    before = store.docs()[sid(1)]
    assert before["identity"]["channels"] == ["alignmentforum"]
    store.gather()
    a = store.docs()[sid(1)]
    assert a["identity"]["channels"] == ["alignmentforum", "lesswrong"]
    assert a["state"] == "triaged" and a["annotations"] == before["annotations"]
    assert a["metadata"]["fields_digest"] == before["metadata"]["fields_digest"]
    assert a["identity"]["members_digest"] == before["identity"]["members_digest"]
