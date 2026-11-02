"""``fold.build``, mapping M, ``fold.release`` and the website-builder query (T5.2; §3.2, §2.9,
D21, D24; V9, V10) on a 20-document synthetic corpus.

The world: 20 acquired documents (``sid(0..19)``) through ``fold.corpus``; a ``screen`` file
over all 20 of which docs 3, 7, 11 carry a stale ``basis_digest`` (consumer F1's case: 3 of 20
shards were a stale ``ok``) and doc 8 a 400-char quote; verdict ``relevant`` for even docs and
doc 3; an ``affiliation`` file at item grain (docs 0, 1 with two authors each, given out of
order; doc 2 an ``item_key = ''`` status row, no items); an unregistered ``claim`` file (by
path). Licences: doc 5's anchor is ``shadow``; doc 6's text comes from a ``restricted`` member;
doc 9's abstract from an ``api_tos`` member.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import zstandard
from typer.testing import CliRunner

from stack.checks.verify import run_checks
from stack.cli import app
from stack.fold import FoldError, run_fold
from stack.fold.release import (
    WITHHELD_REFS,
    Destination,
    build_status,
    load_release_config,
    release_gate,
    release_schema,
    resolve_build,
    resolve_destination,
    run_release,
    write_release_dir,
)
from stack.runs import newest_finished_build, read_run_records
from stack.schemas.families import schema_digest
from stack.store.chain import HeaderFields
from stack.store.layout import RELEASES, StoreLayout
from stack.store.parquet import read_family, read_header
from tests.synth.records import build_json_path, record_hook, sdir, write_series
from tests.synth.rows import (
    AT,
    affiliation_row,
    dg,
    document_row,
    mint,
    rid,
    screen_row,
    sid,
    table,
    with_digests,
)

pytestmark = pytest.mark.e2e  # area marker(s), not a tier (pyproject markers; docs/SETUP.md §7)

STALE = (3, 7, 11)
RELEVANT = tuple(i for i in range(20) if i % 2 == 0) + (3,)
N = 20
REPO = Path(__file__).resolve().parents[1]
DEV_PLAN = REPO / "configs" / "plans" / "dev-v2.yaml"

#: §2.9's website-builder query (documented in docs/CONSUME.md, "Releases and the website query"), over a
#: release file as ``corpus``.
WEBSITE_QUERY = """
SELECT doc_id, to_json(struct_pack(
    build_id := $build_id, doc_id := doc_id, former_doc_ids := identity.former_doc_ids,
    title := metadata.title, published_at := metadata.published_at, doc_type := metadata.doc_type,
    verdict := annotations.screen.payload.verdict, buckets := annotations.screen.payload.buckets,
    basis_role := annotations.screen.payload.basis_role, text_sha256 := content.current_text_sha256,
    links := list_transform(list_filter(identity.members, m -> m.is_live),
               m -> struct_pack(source_id := m.source_id, channel := m.channel, url := m.canonical_url)),
    affiliations := annotations.affiliation.payload.items)) AS shard
FROM corpus
WHERE annotations.screen.status = 'ok' AND annotations.screen.payload.verdict = 'relevant' ORDER BY doc_id
"""



def test_website_query_is_the_documented_one() -> None:
    """CONSUME's website query is the one tested here (whitespace aside)."""
    doc = (REPO / "docs" / "CONSUME.md").read_text("utf-8")
    sql = doc.split("-- $build_id = release.json's build_id\n", 1)[1].split("```", 1)[0]
    assert " ".join(sql.split()).rstrip(";") == " ".join(WEBSITE_QUERY.split())


# ---------------------------------------------------------------- the world
def _add_member(row: dict[str, Any], n: int, license_class: str) -> str:
    m = copy.deepcopy(row["identity"]["members"][0])
    s = sid(n)
    m.update(source_id=s, key_kind="doi", key_value=f"10.1/{n}", license_class=license_class,
             edge_kind="key:doi", edge_to_source_id=row["doc_id"])
    row["identity"]["members"].append(m)
    row["identity"]["member_ids"].append(s)
    return s


def documents_rows() -> list[dict[str, Any]]:
    rows = [document_row(i, 1, state="acquired") for i in range(N)]
    rows[5]["identity"]["members"][0]["license_class"] = "shadow"
    s6 = _add_member(rows[6], 106, "restricted")  # doc 6's text comes from a restricted member
    c = rows[6]["content"]
    c["current_source_id"] = c["snapshots"][0]["source_id"] = c["roles"][0]["source_id"] = s6
    c["roles"][0]["last_source_id"] = s6
    s9 = _add_member(rows[9], 109, "api_tos")  # doc 9's abstract comes from an api_tos member
    rows[9]["metadata"]["field_sources"].append({"field": "abstract", "source_id": s9, "origin": "provider"})
    for i, r in enumerate(rows):  # the text blob a release test puts for dg(("text", i)) is str(("text", i))
        r["content"]["snapshots"][0]["text"]["bytes"] = len(str(("text", i)).encode())
    return [with_digests(r) for r in rows]


def screen_rows() -> list[dict[str, Any]]:
    out = []
    for i in range(N):
        r = screen_row(i, 7, verdict="relevant" if i in RELEVANT else "not_relevant")
        if i in STALE:
            r["basis_digest"] = dg(("old text", i))
        if i == 8:
            r["quotes"] = [{"text": "q" * 400, "offset": 0}]
        out.append(r)
    return out


def affiliation_rows() -> list[dict[str, Any]]:
    return [affiliation_row(0, "b. author", 8), affiliation_row(0, "a. author", 8),
            affiliation_row(1, "z. author", 8), affiliation_row(1, "c. author", 8),
            affiliation_row(2, "", 8)]


def write_claim(path: Path) -> Path:
    """An unregistered workflow's file: doc_id, item_key, the ANN common fields, payload columns."""
    rows = [{"doc_id": sid(i), "item_key": "", "workflow": "claim", "version": 2, "status": "ok",
             "error_class": None, "basis_kind": "text", "basis_digest": dg(("text", i)),
             "config_digest": dg("claim-config"), "decided_at": AT, "evidence_id": None,
             "origin": "computed", "is_basis_current": True, "claims": [f"c{i}", "x"], "n_claims": 2}
            for i in (0, 4)]
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows), path)
    return path


def _fields(stage: str, run: int, **kw: Any) -> HeaderFields:
    return HeaderFields(stage=stage, run_id=rid(run), config_digest=dg((stage, run)), code_digest=dg("code"),
                        plan="dev-v2", **kw)


@pytest.fixture
def world(tmp_path: Path) -> dict[str, Any]:
    store = tmp_path / "store"
    write_series(store, "documents", "documents", table("documents", documents_rows()),
                 _fields("gather", 1, blocks_written=["identity", "metadata", "inband"]),
                 finished_at=AT, on_published=record_hook(store))
    corpus = run_fold(store, "corpus", run_id=rid(5), plan_name="dev-v2", inputs={"documents": sdir("documents")},
                      out=sdir("corpus"), on_published=record_hook(store))
    # the annotation files are files without a series (read by exact path / file id)
    screen = write_series(store, "screen", "screen", table("screen", screen_rows()),
                          _fields("annotate.screen", 7, basis_kind="text"), series=None, finished_at=AT,
                          on_published=record_hook(store))
    affil = write_series(store, "affiliation", "affiliation", table("affiliation", affiliation_rows()),
                         _fields("annotate.affiliation", 8, basis_kind="text"), series=None,
                         finished_at=AT, on_published=record_hook(store))
    return {"store": store, "corpus": corpus.file_id, "screen": screen.file_id,
            "affiliation": affil.file_id, "claim": write_claim(tmp_path / "beside" / "claim.parquet")}


def build(world: dict[str, Any], run: int = 10, filter: str | None = None) -> Any:
    return run_fold(world["store"], "build", run_id=rid(run), plan_name="dev-v2",
                    inputs={"corpus": world["corpus"]}, filter=filter,
                    joins={"screen": world["screen"], "affiliation": world["affiliation"],
                           "claim": str(world["claim"])}, out=sdir("corpus.build"),
                    on_published=record_hook(world["store"]))


def by_doc(path: Path) -> dict[int, dict[str, Any]]:
    tbl, _ = read_family(path)
    return {_idx(r["doc_id"]): r for r in tbl.to_pylist()}


_IDX = {sid(i): i for i in range(200)}


def _idx(doc_id: str) -> int:
    return _IDX[doc_id]


# ---------------------------------------------------------------- fold.build and mapping M
def test_build_join_mapping_m_item_grain_and_other(world: dict[str, Any]) -> None:
    res = build(world)
    rows = by_doc(res.path)
    assert sorted(rows) == list(range(N))  # has_text keeps all 20
    stale = sorted(i for i, r in rows.items()
                   if (r["annotations"]["screen"]["status"], r["annotations"]["screen"]["error_class"])
                   == ("skipped", "stale_basis"))
    print(f"fold.build: {len(rows)} rows, skipped[stale_basis] = {stale}; counts {res.counts}")
    assert stale == list(STALE)
    for i, r in rows.items():
        s = r["annotations"]["screen"]
        assert s["is_basis_current"] is (i not in STALE)
        assert s["payload"]["verdict"] == ("relevant" if i in RELEVANT else "not_relevant")  # payload kept
        if i not in STALE:
            assert (s["status"], s["error_class"]) == ("ok", None)
        assert r["annotations"]["triage"]["is_basis_current"] is True  # carried, recomputed
    assert res.counts["stale_basis.screen"] == 3 and res.counts["joined.screen"] == N

    # item grain: the doc's item rows, ordered by item_key, into payload.items[]
    a0, a1, a2 = (rows[i]["annotations"]["affiliation"] for i in (0, 1, 2))
    assert [it["item_key"] for it in a0["payload"]["items"]] == ["a. author", "b. author"]
    assert [it["item_key"] for it in a1["payload"]["items"]] == ["c. author", "z. author"]
    assert a0["payload"]["items"][0]["affiliations"] == [{"surface": "Example Lab", "org_slugs": ["example-lab"]}]
    assert (a2["status"], a2["payload"]["items"]) == ("empty", [])
    assert rows[3]["annotations"]["affiliation"] is None and rows[3]["annotations"]["doc_class"] is None

    # unregistered workflow -> other[] as ANN<JSON>
    o0 = rows[0]["annotations"]["other"]
    assert [(o["workflow"], o["version"], o["payload"]["item_key"]) for o in o0] == [("claim", 2, "")]
    assert json.loads(o0[0]["payload"]["payload_json"]) == {"claims": ["c0", "x"], "n_claims": 2}
    assert o0[0]["is_basis_current"] is True and rows[1]["annotations"]["other"] == []

    # header and fold record (A.6, A.7, D14)
    h = read_header(res.path)
    assert (h["fold_config"], h["stage"], h["writer"], h["chain_id"], h["series"], h["kind"], h["base"],
            h["release_config"]) == ("build", "fold.build", "fold", "corpus.build", "main", "full", "", "none")
    assert res.path.parent == world["store"] / "data" / "corpus.build" / "main"  # D374
    assert json.loads(h["annotations_folded"]) == ["screen", "affiliation", "claim"]
    assert json.loads(h["inputs"]) == {"corpus": world["corpus"], "screen": world["screen"],
                                       "affiliation": world["affiliation"], "claim": str(world["claim"])}
    assert res.fold["joined"] == {"screen": world["screen"], "affiliation": world["affiliation"],
                                  "claim": str(world["claim"])}

    # V9 is green on the real output, red when mapping M is undone on one row
    by = {r.check: r for r in run_checks(world["store"], names=["V-fold"])}
    assert by["V-fold"].status == "ok", by["V-fold"].items


def test_build_filter_after_join(world: dict[str, Any]) -> None:
    res = build(world, filter="annotations.screen.payload.verdict = 'relevant'")
    got = sorted(by_doc(res.path))
    assert got == sorted(RELEVANT)
    assert read_header(res.path)["filter"] == "annotations.screen.payload.verdict = 'relevant'"
    # stale doc 3 passes a verdict-only filter (its payload is kept); status filters see M's result
    res2 = build(world, run=11, filter="annotations.screen.status = 'ok' "
                                       "AND annotations.screen.payload.verdict = 'relevant'")
    assert sorted(by_doc(res2.path)) == sorted(set(RELEVANT) - set(STALE))
    with pytest.raises(FoldError, match="fold_config"):
        run_fold(world["store"], "build", run_id=rid(12), plan_name="dev-v2", inputs={"corpus": res.file_id}, on_published=record_hook(world["store"]))
    with pytest.raises(FoldError, match="not a <wf> workflow"):
        run_fold(world["store"], "build", run_id=rid(12), plan_name="dev-v2",
                 inputs={"corpus": world["corpus"]}, joins={"triage": world["screen"]}, on_published=record_hook(world["store"]))


# ---------------------------------------------------------------- fold.release
def public_all(store: Path) -> Path:
    """public-v1 over every row (``filter: everything``): its licence rules and drops, on rows its screen filter
    (D705) leaves out here (the shadow anchor 5, the api_tos abstract 9)."""
    text = (REPO / "configs" / "releases" / "public-v1.yaml").read_text()
    assert "\nfilter: screen_relevant\n" in text
    p = store.parent / "public-all.yaml"
    p.write_text(text.replace("name: public-v1", "name: public-all").replace(
        "\nfilter: screen_relevant\n", "\nfilter: everything\n"))
    return p


@pytest.mark.slow
def test_release_licence_caps_withheld_and_v10(world: dict[str, Any]) -> None:
    store = world["store"]
    b = build(world)
    pub = public_all(store)
    res = run_release(store, run_id=rid(20), inputs={"corpus": b.file_id}, release=pub,
                      dest=resolve_destination(store, load_release_config(pub), tag="t1"), allow_unbuilt=True)  # a standalone fold.build corpus (D494)
    doc = json.loads((res.path / "release.json").read_text())
    print(f"release t1: {res.rows_in} -> {res.rows_out} rows, dropped {doc['dropped']}")
    assert doc["dropped"] == {"shadow": 1} and res.rows_out == N - 1
    assert {k: doc[k] for k in ("build_id", "tag", "config_digest")} == {
        "build_id": "none", "tag": "t1", "config_digest": load_release_config(pub).digest}
    assert doc["build_status"] == {"status": "unbuilt"}  # D705
    tbl = pq.read_table(res.corpus_path).replace_schema_metadata(None)
    assert tbl.schema.equals(release_schema(load_release_config(pub)))
    assert "inband" not in tbl.column_names and "run_id" not in tbl.column_names
    assert "run_id" not in tbl.schema.field("identity").type.names
    assert "other" not in tbl.schema.field("annotations").type.names
    assert "reasoning" not in tbl.schema.field("annotations").type.field("screen").type.field("payload").type.names
    rows = {_idx(r["doc_id"]): r for r in tbl.to_pylist()}
    assert 5 not in rows  # shadow anchor
    # withheld: text from a restricted member; no digest anywhere in the row
    c6 = rows[6]["content"]
    assert c6["current_text_status"] == "withheld"

    def values(obj: Any, path: str) -> list[Any]:
        vals = [obj]
        for part in path.split("."):
            vals = [x for v in vals if isinstance(v, dict) and v.get(part) is not None
                    for x in (v[part] if isinstance(v[part], list) else [v[part]])]
        return vals

    assert all(values(rows[6], p) == [] for p in WITHHELD_REFS), [values(rows[6], p) for p in WITHHELD_REFS]
    assert values(rows[0], "content.snapshots.raw.sha256") == [dg(("raw", 0))]  # the paths reach real values
    assert rows[0]["content"]["current_text_sha256"] == dg(("text", 0))  # open: pointer kept
    assert rows[9]["metadata"]["abstract"] is None and rows[0]["metadata"]["abstract"] == "Abstract 0"
    assert len(rows[8]["annotations"]["screen"]["payload"]["quotes"][0]["text"]) == 300  # capped
    # no class ships text: no blobs/ (D647's layout), no inline bytes anywhere
    assert not (res.path / "blobs").exists() and not (res.path / "missing-blobs.txt").exists()
    assert all(s[k] is None or s[k]["inline"] is None
               for r in rows.values() for s in r["content"]["snapshots"] for k in ("raw", "text"))
    assert values(rows[0], "content.snapshots.raw.sha256")  # (the inline check reaches real BLOBs)
    assert not list((store / RELEASES).glob(".*"))  # staging dir renamed away
    assert run_checks(store, names=["V-release"])[0].status == "ok"

    # the website-builder query over the release: one row per doc_id
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    con.execute(f"CREATE VIEW corpus AS SELECT * FROM read_parquet('{res.corpus_path}')")
    shards = con.execute(WEBSITE_QUERY, {"build_id": doc["build_id"]}).fetchall()
    ids = [d for d, _ in shards]
    print(f"website query: {len(shards)} shards")
    assert len(ids) == len(set(ids)) == len(set(RELEVANT) - set(STALE))
    s0 = json.loads(next(s for d, s in shards if d == sid(0)))
    assert [a["item_key"] for a in s0["affiliations"]] == ["a. author", "b. author"]
    assert s0["links"] == [{"source_id": sid(0), "channel": "arxiv", "url": None}]
    assert s0["text_sha256"] == dg(("text", 0)) and "text_digest" not in s0  # D647: a blob reference ends in _sha256

    # a 400-char quote in a release file fails V10; an existing tag is refused
    bad = tbl.to_pylist()
    bad[0]["annotations"]["screen"]["payload"]["quotes"] = [{"text": "q" * 400, "offset": 0}]
    write_release_dir(store, Destination("t2"), pa.Table.from_pylist(bad, schema=tbl.schema), {**doc, "tag": "t2"})
    v10 = run_checks(store, names=["V-release"])[0]
    print(f"V10 on t2: {v10.status} {v10.items}")
    assert v10.status == "failed" and any("t2:" in i and "cap 300" in i for i in v10.items)
    assert not any(i.startswith("t1:") for i in v10.items)
    # a release written before a schema bump (here: no annotations.text_usability) is checked as its
    # upcast (D208 (4)): clean rows pass, the row rules still apply
    ann = tbl.schema.field("annotations")
    old_schema = tbl.schema.set(tbl.schema.get_field_index("annotations"), pa.field(
        "annotations", pa.struct([f for f in ann.type if f.name != "text_usability"]), nullable=ann.nullable))
    old_rows = [{**r, "annotations": {k: v for k, v in r["annotations"].items() if k != "text_usability"}}
                for r in tbl.to_pylist()]
    old_doc = {**doc, "schema_digest": schema_digest(old_schema)}  # as its writer recorded it
    write_release_dir(store, Destination("t4"), pa.Table.from_pylist(old_rows, schema=old_schema), {**old_doc, "tag": "t4"})
    old_rows[0]["annotations"]["screen"]["payload"]["quotes"] = [{"text": "q" * 400, "offset": 0}]
    write_release_dir(store, Destination("t5"), pa.Table.from_pylist(old_rows, schema=old_schema), {**old_doc, "tag": "t5"})
    v10 = run_checks(store, names=["V-release"])[0]
    assert not any(i.startswith("t4:") for i in v10.items), v10.items
    assert any(i.startswith("t5:") and "cap 300" in i for i in v10.items), v10.items
    # D705: build_status must agree with build_id (none = unbuilt) and be a known status
    for t, bs in (("t6", {"status": "ok"}), ("t7", {"status": "fine"})):
        write_release_dir(store, Destination(t), tbl, {**doc, "tag": t, "build_status": bs})
    v10 = run_checks(store, names=["V-release"])[0]
    assert any(i.startswith("t6:") and "build_status 'ok' with build_id 'none'" in i for i in v10.items), v10.items
    assert any(i.startswith("t7:") and "build_status" in i for i in v10.items), v10.items
    assert not any(i.startswith("t1:") for i in v10.items), v10.items
    with pytest.raises(FoldError, match="exists"):
        run_release(store, run_id=rid(21), inputs={"corpus": b.file_id}, release=pub,
                    dest=resolve_destination(store, load_release_config(pub), tag="t1"), allow_unbuilt=True)
    with pytest.raises(FoldError, match="fold_config"):
        run_release(store, run_id=rid(21), inputs={"corpus": world["corpus"]}, release=pub,
                    dest=resolve_destination(store, load_release_config(pub), tag="t3"))


def test_release_gate_by_the_naming_builds_status(tmp_path: Path) -> None:
    """A8 / S25-14 (D494): a corpus is released when an ``ok`` build names it; an ``incomplete`` build's only with
    ``allow_incomplete``; one no build names only with ``allow_unbuilt``; one only ``failed`` (e.g. ``failed[verify]``),
    ``cancelled`` or ``running`` builds name never."""
    store = tmp_path / "store"
    StoreLayout(store).ensure()

    def named_by(n: int, status: str, corpus: str, **extra: Any) -> str:
        bid = mint("b", n)
        at = AT + dt.timedelta(hours=n)
        build_json_path(store, at, bid, "dev-v2").write_bytes(zstandard.ZstdCompressor().compress(json.dumps({
            "build_id": bid, "plan": "dev-v2", "status": status, "series": "main", "finished_at": at.isoformat(),
            "files": {"corpus": {"file_id": corpus}}, **extra}).encode()))
        return bid

    with pytest.raises(FoldError, match="no build record names it.*--allow-unbuilt"):
        release_gate(store, "c-unbuilt")
    assert release_gate(store, "c-unbuilt", allow_unbuilt=True) == ("none", "unbuilt")
    failed = named_by(1, "failed", "c-failed")
    named_by(2, "cancelled", "c-failed")
    for flags in ({}, {"allow_incomplete": True, "allow_unbuilt": True}):
        with pytest.raises(FoldError, match=rf"named only by build '{failed}' \(failed\), '.*' \(cancelled\)"):
            release_gate(store, "c-failed", **flags)
    short = [{"stage": "annotate.screen", "run_id": rid(3), "reasons": ["budget"]}]
    inc = named_by(3, "incomplete", "c-inc", incomplete=short)
    with pytest.raises(FoldError, match=f"build '{inc}', which is incomplete.*--allow-incomplete"):
        release_gate(store, "c-inc", allow_unbuilt=True)
    assert release_gate(store, "c-inc", allow_incomplete=True) == (inc, "incomplete")
    ok = named_by(4, "ok", "c-inc", incomplete_allowed=short)  # an ok build naming it wins: no flag needed
    assert release_gate(store, "c-inc") == (ok, "ok")
    # D705: release.json build_status, what stopped short copied from the admitted build's record
    assert build_status(store, *release_gate(store, "c-unbuilt", allow_unbuilt=True)) == {"status": "unbuilt"}
    assert build_status(store, inc, "incomplete") == {"status": "incomplete", "incomplete": short}
    assert build_status(store, *release_gate(store, "c-inc")) == {"status": "ok", "incomplete_allowed": short}
    with pytest.raises(FoldError, match="not one of"):
        build_status(store, failed, "failed")


def test_latest_is_the_newest_ok_or_incomplete_build_and_the_release_gate_still_holds(tmp_path: Path) -> None:
    """D703: ``latest`` (:func:`resolve_build`: ``verify --build latest``, ``diff … latest``) is the newest ``ok`` or
    ``incomplete`` build, the build ``verify`` and ``--series`` pick by default; a newer ``failed``, ``cancelled`` or
    ``running`` one is never it. Resolving it releases nothing: the incomplete build's corpus still needs
    ``allow_incomplete``. Red under: ``latest`` = the newest ``ok`` build (before D703)."""
    store = tmp_path / "store"
    StoreLayout(store).ensure()

    def named_by(n: int, status: str, corpus: str, plan: str = "dev-v2") -> str:
        bid = mint("b", n)
        at = AT + dt.timedelta(hours=n)
        build_json_path(store, at, bid, plan).write_bytes(zstandard.ZstdCompressor().compress(json.dumps({
            "build_id": bid, "plan": plan, "status": status, "series": "main", "finished_at": at.isoformat(),
            "files": {"corpus": {"file_id": corpus}}}).encode()))
        return bid

    with pytest.raises(FoldError, match="no ok or incomplete build of plan 'dev-v2'"):
        resolve_build(store, "latest", "dev-v2")
    ok = named_by(1, "ok", "c-ok")
    assert resolve_build(store, "latest")["build_id"] == ok
    inc = named_by(2, "incomplete", "c-inc")
    for status in ("failed", "cancelled", "running"):
        named_by(3 + ("failed", "cancelled", "running").index(status), status, f"c-{status}")
    other = named_by(9, "ok", "c-other", plan="std-v3")  # another plan's, newer
    assert resolve_build(store, "latest", "dev-v2")["build_id"] == inc
    assert resolve_build(store, "latest")["build_id"] == other
    assert newest_finished_build(store, "dev-v2")[1]["build_id"] == inc  # type: ignore[index]
    with pytest.raises(FoldError, match=f"build '{inc}', which is incomplete.*--allow-incomplete"):
        release_gate(store, "c-inc")
    assert release_gate(store, "c-inc", allow_incomplete=True) == (inc, "incomplete")
    assert release_gate(store, "c-ok") == (ok, "ok")


def test_release_ship_copies_text(world: dict[str, Any], tmp_path: Path) -> None:
    store = world["store"]
    b = build(world)
    from stack.store.blobs import open_blobs

    StoreLayout(store).ensure()
    blobs = open_blobs(store)
    cfg = (REPO / "configs" / "releases" / "public-v1.yaml").read_text()
    ship = tmp_path / "ship-v1.yaml"
    ship.write_text(cfg.replace("name: public-v1", "name: ship-v1").replace(
        "open:         {rows: keep, text: pointer", "open:         {rows: keep, text: ship").replace(
        "\nfilter: screen_relevant\n", "\nfilter: everything\n"))  # every row: shadow 5 counts below
    with pytest.raises(FoldError, match="pre-flight: .* neither local nor remote"):  # a digest with no blob: refused
        run_release(store, run_id=rid(22), inputs={"corpus": b.file_id}, release=ship,
                    dest=resolve_destination(store, load_release_config(ship), tag="s0"), allow_unbuilt=True)
    assert not (store / RELEASES / "s0").exists() and not list((store / RELEASES).glob(".*"))
    for i in range(N):  # dg(label) = sha256(str(label)): the blob of that digest is str(label)
        assert blobs.put(str(("text", i)).encode()) == dg(("text", i))
    res = run_release(store, run_id=rid(23), inputs={"corpus": b.file_id}, release=ship,
                      dest=resolve_destination(store, load_release_config(ship), tag="s1"), allow_unbuilt=True)
    # D647: blobs/<sha256>, the store's grammar, no suffix (text/<digest>.md before)
    assert (res.path / "blobs" / dg(("text", 0))).read_bytes() == blobs.get(dg(("text", 0)))
    assert not (res.path / "text").exists()
    assert not (res.path / "blobs" / dg(("text", 6))).exists()  # withheld
    doc = json.loads((res.path / "release.json").read_text())
    assert doc["content"]["blobs_count"] == len(list((res.path / "blobs").iterdir())) == N - 2  # shadow 5, withheld 6
    assert run_checks(store, names=["V-release"])[0].status == "ok"


# ---------------------------------------------------------------- CLI paths
runner = CliRunner()


def cli(store: Path, *args: str, code: int = 0) -> str:
    res = runner.invoke(app, ["--store", str(store), "--plan", str(DEV_PLAN), *args])
    if res.exception is not None and not isinstance(res.exception, SystemExit):
        raise res.exception
    print(f"$ stack {' '.join(args)}  -> {res.exit_code}\n{res.output}")
    assert res.exit_code == code, res.output
    return res.output


@pytest.mark.slow
def test_cli_fold_build_run_fold_build_fold_release_and_release(tmp_path: Path) -> None:
    from tests.fixtures.checks import build_store, fid

    store = tmp_path / "store"
    build_store(store)  # recorded documents chain F1-F5, a corpus file (run 8), a screen file, ok run records
    # fold.corpus never reads corpus/<series>/: a base-less full into the non-empty dir needs no
    # --restart (the restart guard, D373), as `stack run fold.corpus` does
    out = json.loads(cli(store, "fold", "corpus", "--input", f"documents={sdir('documents')}",
                         "--out", sdir("corpus")))
    corpus = out["outputs"]["corpus"]
    assert corpus.startswith("data/corpus/main/") and read_header(store / corpus)["base"] == ""
    screen = fid(7, "screen")
    out = json.loads(cli(store, "fold", "build", "--input", f"corpus={corpus}", "--join", f"screen={screen}",
                         "--out", sdir("corpus.build")))
    assert out["outputs"]["corpus"].startswith("data/corpus.build/main/")  # D374
    built = read_header(store / out["outputs"]["corpus"])
    assert (built["fold_config"], json.loads(built["annotations_folded"])) == ("build", ["screen"])
    rec = next(r for r in read_run_records(store) if r.run_id == out["run_id"])
    assert rec.fold is not None and rec.fold["joined"] == {"screen": screen} and rec.stage == "fold.build"
    assert "needs --out" in cli(store, "fold", "build", "--input", f"corpus={corpus}", code=1)  # a usage error (D381)

    out2 = json.loads(cli(store, "run", "fold.build", "--in", corpus, "--in", sdir("screen"),
                          "--out", sdir("corpus.build")).splitlines()[-1])  # stderr's ledger line is mixed in
    rec2 = next(r for r in read_run_records(store) if r.run_id == out2["run_id"])
    built2 = read_header(store / out2["outputs"]["corpus"])
    assert rec2.fold == rec.fold and built2["filter"] == built["filter"]
    assert built2["row_count"] == built["row_count"]
    assert (built2["base"], built2["after"]) == ("", Path(out["outputs"]["corpus"]).name)  # a full on the dir's head
    assert "not a stage" in cli(store, "run", "fold.release", "--in", corpus, code=1)
    assert "not a fold.corpus file" in cli(store, "fold", "build", "--input", f"corpus={built['file_id']}",
                                           "--out", sdir("corpus.build"), code=1)

    # D494: a corpus no build names (these standalone fold.build runs) is released only with --allow-unbuilt
    assert "--allow-unbuilt" in cli(store, "fold", "release", "--input", f"corpus={built['file_id']}",
                                    "--release", "public-v1", "--tag", "r1", code=1)
    assert not (store / RELEASES / "r1").exists()
    assert "writes no release" in cli(store, "fold", "build", "--input", f"corpus={corpus}", "--allow-unbuilt",
                                      "--out", sdir("corpus.build"), code=1)
    out3 = json.loads(cli(store, "fold", "release", "--input", f"corpus={built['file_id']}",
                          "--release", "public-v1", "--tag", "r1", "--allow-unbuilt").splitlines()[-1])  # after the gate's line
    assert out3["outputs"] == {"release": "releases/r1/corpus.parquet"}
    rec3 = next(r for r in read_run_records(store) if r.run_id == out3["run_id"])
    assert (rec3.stage, rec3.fold["config"], rec3.fold["release_config"]) == ("fold.release", "release", "public-v1")
    rel = rec3.outputs["release"]  # D322: a release is a chain of one
    assert (rel["chain_id"], rel.get("base", ""), rel["kind"]) == (rel["file_id"], "", "full")
    assert "exists" in cli(store, "fold", "release", "--input", f"corpus={built['file_id']}",
                           "--release", "public-v1", "--tag", "r1", code=1)
    default = json.loads(cli(store, "fold", "release", "--input", f"corpus={built['file_id']}",
                             "--allow-unbuilt").splitlines()[-1])
    assert default["outputs"]["release"] == f"releases/public-v1-{dt.datetime.now(dt.UTC):%Y%m%d}/corpus.parquet"

    # the alias verb (D379): --in a corpus.build series dir (its last file) or an exact file;
    # the release's build_id is the newest ok build record naming that file
    head = out2["outputs"]["corpus"]
    bid = mint("b", 7)
    rec_path = build_json_path(store, AT + dt.timedelta(hours=30), bid, "dev-v2")
    rec_path.write_bytes(zstandard.ZstdCompressor().compress(json.dumps({
        "build_id": bid, "plan": "dev-v2", "status": "ok", "series": "main",
        "finished_at": (AT + dt.timedelta(hours=30)).isoformat(),
        "version_chain": [fid(4), fid(5), fid(6, "sources"), fid(6, "coverage")],
        "files": {"documents": {"file_id": fid(5)}, "sources/sources.arxiv": {"file_id": fid(6, "sources")},
                  "coverage/coverage.arxiv": {"file_id": fid(6, "coverage")},
                  "corpus": {"file_id": built2["file_id"], "path": head}}}).encode()))
    o2 = cli(store, "release", "--in", sdir("corpus.build"), "--config", "public-v1", "--tag", "r2")
    assert f"release of {head}: its directory's last file" in o2
    assert f"build {bid} (ok)" in o2  # D494: the naming build and its status
    o3 = cli(store, "release", "--in", head, "--tag", "r3")
    assert f"release of {head}: its directory's last file" in o3
    for tag in ("r2", "r3"):
        assert json.loads((store / RELEASES / tag / "release.json").read_text())["build_id"] == bid
    assert json.loads((store / RELEASES / "r1" / "release.json").read_text())["build_id"] == "none"
    # an older file of the directory is released only when named exactly, and the stderr line says so
    older = out["outputs"]["corpus"]
    assert "no build record names it" in cli(store, "release", "--in", older, "--tag", "r4", code=1)
    o4 = cli(store, "release", "--in", older, "--tag", "r4", "--allow-unbuilt")
    assert f"release of {older}: NOT its directory's last file ({Path(head).name})" in o4
    assert json.loads((store / RELEASES / "r4" / "release.json").read_text())["build_id"] == "none"
    assert "has no published file" in cli(store, "release", "--in", sdir("corpus.build", "exp-2"), "--tag", "r9",
                                          code=1)
    # a newer ok build naming the same file: the release names that build
    bid2 = mint("b", 8)
    build_json_path(store, AT + dt.timedelta(hours=31), bid2, "dev-v2").write_bytes(
        zstandard.ZstdCompressor().compress(json.dumps({
            **json.loads(zstandard.ZstdDecompressor().decompress(rec_path.read_bytes())), "build_id": bid2,
            "finished_at": (AT + dt.timedelta(hours=31)).isoformat()}).encode()))
    cli(store, "release", "--in", sdir("corpus.build"), "--tag", "r5")
    assert json.loads((store / RELEASES / "r5" / "release.json").read_text())["build_id"] == bid2
    # removed with `stack latest` and `stack release <build>|latest` (D375): no release by build or plan
    # release runs are runs: X1 (no lost or unrecorded file), V9, V10 green
    by = {r.check: r for r in run_checks(store, names=["finished_files", "V-fold", "V-release"])}
    assert {n: r.status for n, r in by.items()} == {"finished_files": "ok", "V-fold": "ok", "V-release": "ok"}, \
        {n: r.items for n, r in by.items()}
