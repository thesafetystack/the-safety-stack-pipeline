"""The export format (D647): release configs with ``filter``, ``snapshots``, ``content``; per-snapshot
inline bytes in the release schema only; ``blobs/<sha256>``; ``--out`` exports; the pre-flight;
``validate_release``; the readers (``resolve``, the duckdb macros); ``stack export``.

The world: 10 acquired documents through ``fold.corpus`` and ``fold.build`` (screen joined), every digest
the sha256 of a blob in the store with its size. Doc 0 has three snapshots: the current PDF, an in-band abstract whose
raw bytes are its text (one digest, raw = text) and a non-current HTML page; doc 2's text comes from a
``restricted`` member; doc 3's anchor is ``shadow``; doc 4 has a second, non-current snapshot from a
``public_web`` member (and an in-band entry of it); doc 6 an in-band ``provider_record`` entry; doc 7's current snapshot is an in-band
post (raw = text). Screen: borderline for doc 5, not relevant for 8 and 9, relevant for the rest, so
``screen_relevant`` keeps 0, 1, 2, 3, 4, 6, 7 and internal-v1 then drops 3 (shadow).
"""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from typer.testing import CliRunner

from stack.checks.fold_checks import validate_release
from stack.checks.verify import run_checks
from stack.cli import app
from stack.core.hashes import jcs
from stack.fold import FoldError, filter_rows, run_fold
from stack.fold.release import (
    BLOB,
    ReleaseConfig,
    blob_ref_paths,
    load_release_config,
    release_row,
    release_schema,
    resolve_destination,
    run_release,
)
from stack.query import register_macros, sql_connection
from stack.schemas.blocks import BLOB as PIPELINE_BLOB
from stack.schemas.structs import text_blob
from stack.schemas.families import family, walk
from stack.store.blobs import BlobCorrupt, open_blobs, resolve
from stack.store.chain import HeaderFields
from stack.store.layout import RELEASES, StoreLayout
from tests.synth.records import record_hook, sdir, write_series
from tests.synth.rows import (
    AT,
    dg,
    document_row,
    rid,
    screen_row,
    sid,
    table,
    with_digests,
)

pytestmark = pytest.mark.e2e  # area marker(s), not a tier (pyproject markers; docs/SETUP.md §7)

N = 10
REPO = Path(__file__).resolve().parents[1]
RELEASES_DIR = REPO / "configs" / "releases"
VERDICT = {5: "borderline", 8: "not_relevant", 9: "not_relevant"}
INTERNAL_ROWS = (0, 1, 2, 4, 6, 7)  # screen_relevant, minus doc 3 (shadow anchor)


def text(i: int) -> bytes:
    return f"# Paper {i}\n\nThe body of paper {i}.\n".encode()


def _snap(base: dict[str, Any], n: int, source_id: str, **kw: Any) -> dict[str, Any]:
    s = copy.deepcopy(base)
    s.update(snapshot_id=f"{rid(1)}.{n}", source_id=source_id, **kw)
    return s


def _member(row: dict[str, Any], n: int, license_class: str) -> str:
    m = copy.deepcopy(row["identity"]["members"][0])
    s = sid(n)
    m.update(source_id=s, key_kind="doi", key_value=f"10.1/{n}", license_class=license_class,
             edge_kind="key:doi", edge_to_source_id=row["doc_id"])
    row["identity"]["members"].append(m)
    row["identity"]["member_ids"].append(s)
    return s


def documents_rows(blobs: Any, edit: Any = None) -> list[dict[str, Any]]:
    def ref(data: bytes, media_type: str) -> dict[str, Any]:
        return {"sha256": blobs.put(data), "bytes": len(data), "media_type": media_type}

    rows = []
    for i in range(N):
        r = document_row(i, 1, state="acquired")
        c = r["content"]
        snap = c["snapshots"][0]
        snap.update(raw=ref(f"%PDF-1.7 paper {i}".encode(), "application/pdf"), text=ref(text(i), "text/markdown"))
        c["current_text_sha256"] = snap["text"]["sha256"]
        r["inband"]["entries"][0]["text"] = ref(f"Abstract {i}".encode(), "text/plain")
        rows.append(r)
    # doc 0: + an in-band abstract snapshot (raw = text) and a non-current HTML page
    r0 = rows[0]
    base, a0 = r0["content"]["snapshots"][0], r0["inband"]["entries"][0]["text"]
    r0["content"]["snapshots"] += [
        _snap(base, 1, sid(0), kind="inband", via="inband", content_role="abstract", url=None, http_status=None,
              raw=a0, text=a0, convert_pin=None, quality=None),
        _snap(base, 2, sid(0), content_role="html", url="https://example.org/0",
              raw=ref(b"<html>page 0</html>", "text/html"), text=ref(b"page 0 as markdown", "text/markdown"))]
    # doc 2: the text comes from a restricted member
    s2 = _member(rows[2], 102, "restricted")
    c2 = rows[2]["content"]
    c2["current_source_id"] = c2["snapshots"][0]["source_id"] = c2["roles"][0]["source_id"] = s2
    c2["roles"][0]["last_source_id"] = s2
    # doc 3: a shadow anchor
    rows[3]["identity"]["members"][0]["license_class"] = "shadow"
    # doc 4: a non-current snapshot from a public_web member
    s4 = _member(rows[4], 104, "public_web")
    rows[4]["content"]["snapshots"].append(_snap(rows[4]["content"]["snapshots"][0], 1, s4, content_role="html",
                                                 raw=ref(b"<html>4</html>", "text/html"),
                                                 text=ref(b"web 4", "text/markdown")))
    rows[4]["inband"]["entries"].append({"source_id": s4, "content_role": "abstract", "raw": None,
                                         "text": ref(b"web abstract 4", "text/plain"), "captured_at": AT})
    # doc 6: a provider_record in-band entry (D522)
    rows[6]["inband"]["entries"].append({"source_id": sid(6), "content_role": "provider_record",
                                         "raw": ref(b'{"provider": "record of 6"}', "application/json"),
                                         "text": None, "captured_at": AT})
    # doc 7: the current snapshot is an in-band post (raw = text)
    s7 = rows[7]["content"]["snapshots"][0]
    post = ref(b"An in-band forum post, already Markdown.", "text/markdown")
    s7.update(kind="inband", via="inband", content_role="post", raw=post, text=post, convert_pin=None, quality=None)
    rows[7]["content"]["current_text_sha256"] = post["sha256"]
    if edit is not None:  # a test's variant of the world, before the digests
        edit(rows)
    return [with_digests(r) for r in rows]


def _fields(stage: str, run: int, **kw: Any) -> HeaderFields:
    return HeaderFields(stage=stage, run_id=rid(run), config_digest=dg((stage, run)), code_digest=dg("code"),
                        plan="dev-v2", **kw)


@pytest.fixture
def world(tmp_path: Path) -> dict[str, Any]:
    return make_world(tmp_path)


def make_world(tmp_path: Path, edit: Any = None) -> dict[str, Any]:
    """The module docstring's world; ``edit(rows)`` changes the documents rows before they are written."""
    store = tmp_path / "store"
    StoreLayout(store).ensure()
    blobs = open_blobs(store)
    docs = documents_rows(blobs, edit)
    write_series(store, "documents", "documents", table("documents", docs),
                 _fields("gather", 1, blocks_written=["identity", "metadata", "inband"]),
                 finished_at=AT, on_published=record_hook(store))
    corpus = run_fold(store, "corpus", run_id=rid(5), plan_name="dev-v2", inputs={"documents": sdir("documents")},
                      out=sdir("corpus"), on_published=record_hook(store))
    screens = []
    for i, r in enumerate(docs):
        s = screen_row(i, 7, verdict=VERDICT.get(i, "relevant"))
        s["basis_digest"] = r["content"]["current_text_sha256"]
        screens.append(s)
    screen = write_series(store, "screen", "screen", table("screen", screens),
                          _fields("annotate.screen", 7, basis_kind="text"), series=None, finished_at=AT,
                          on_published=record_hook(store))
    b = run_fold(store, "build", run_id=rid(10), plan_name="dev-v2", inputs={"corpus": corpus.file_id},
                 joins={"screen": screen.file_id}, out=sdir("corpus.build"), on_published=record_hook(store))
    return {"store": store, "tmp": tmp_path, "build": b.file_id, "build_path": b.path, "docs": docs,
            "blobs": blobs}


def config(tmp: Path, name: str, base: str = "internal-v1", **over: Any) -> Path:
    """A release config file ``name`` = the ``base`` config with top-level keys replaced (``content``
    merged)."""
    import yaml

    raw = yaml.safe_load((RELEASES_DIR / f"{base}.yaml").read_text())
    content = {**raw.get("content", {}), **over.pop("content", {})}
    raw.update(name=name, content=content, **over)
    p = tmp / f"{name}.yaml"
    p.write_text(yaml.safe_dump(raw))
    return p


def export(world: dict[str, Any], out: str, release: str | Path = "internal-v1", run: int = 40) -> Any:
    dest = resolve_destination(world["store"], load_release_config(release), world["tmp"] / out)
    return run_release(world["store"], run_id=rid(run), inputs={"corpus": world["build"]}, release=release,
                       dest=dest, allow_unbuilt=True)


def rows_of(path: Path) -> dict[int, dict[str, Any]]:
    idx = {sid(i): i for i in range(N)}
    return {idx[r["doc_id"]]: r for r in pq.read_table(path).to_pylist()}


def store_bytes(world: dict[str, Any], digest: str) -> bytes:
    return world["blobs"].get(digest)


# ---------------------------------------------------------------- config and schema
def test_config_validation(tmp_path: Path) -> None:
    cfg = load_release_config("internal-v1")
    assert (cfg.audience, cfg.filter, cfg.snapshots, cfg.content.select, cfg.content.place) == \
        ("internal", "screen_relevant", "current", "text", "blobs")
    assert cfg.rule("restricted").ships and cfg.rule("restricted").excerpt_cap_chars is None
    assert not cfg.rule("shadow").ships and cfg.other == "keep"
    assert cfg.column_drops == ("inband", "identity.members_digest", "content.members_digest")
    pub = load_release_config("public-v1")
    assert not any(pub.rule(c).ships for c in pub.licence_rules)  # public-v1 ships no bytes
    rules = {c: {"rows": "keep", "text": "ship", "abstract": "keep", "excerpt_cap_chars": None}
             for c in pub.licence_rules}
    with pytest.raises(ValueError, match="provider_records needs inband"):
        ReleaseConfig.model_validate({"name": "t", "licence_rules": rules, "content": {"provider_records": True}})
    ReleaseConfig.model_validate({"name": "t", "licence_rules": rules, "column_drops": [],
                                  "content": {"provider_records": True}})
    for bad in ({"content": {"select": "current"}}, {"content": {"place": "pointer"}}, {"snapshots": "two"},
                {"content": {"max_total_bytes": -1}}, {"filter": " "}, {"audience": "world"}):
        with pytest.raises(ValueError):
            ReleaseConfig.model_validate({"name": "t", "licence_rules": rules, **bad})
    # D690: no members' digest list any more, so dropping inband alone is a complete config
    assert load_release_config(config(tmp_path, "inband-only", column_drops=["inband"])).column_drops == ("inband",)


def test_blob_refs_and_inline_only_in_the_release_schema() -> None:
    """Build stages cannot write inline bytes: the family specs have no ``inline`` field and no release
    ``BLOB`` (S1 refuses a schema that differs); the release schema names its references as the pipeline
    does (D690), ``raw``/``text`` BLOBs ``{sha256, bytes, media_type, inline}`` (``inline`` nullable
    ``large_binary``) and flat ones ``*_sha256``; no ``*_digest`` there is a reference."""
    for fam in ("documents", "corpus"):
        assert not [p for p, f in walk(family(fam).schema) if p.endswith("inline") or f.type == BLOB]
        pipeline = [p for p, f in walk(family(fam).schema) if f.type == PIPELINE_BLOB]
        assert sorted(pipeline) == ["content.snapshots[].raw", "content.snapshots[].text", "inband.entries[].raw",
                                    "inband.entries[].text"]
    cfg = load_release_config("internal-v1")
    rs = release_schema(cfg)
    assert sorted(blob_ref_paths(rs)) == ["content.current_text_sha256", "content.snapshots[].raw",
                                          "content.snapshots[].text"]
    assert BLOB.field("inline").type == pa.large_binary() and BLOB.field("inline").nullable
    snap = rs.field("content").type.field("snapshots").type.value_type
    assert not {"digest", "text_digest", "bytes", "media_type", "raw_inline", "text_inline"} & set(snap.names)
    digests = [p for p, _ in walk(rs) if p.endswith("_digest")]
    assert "content.current_text_digest" not in digests and "content.snapshots[].text_digest" not in digests
    assert "content.snapshots[].quality.gate_digest" in digests  # fingerprints keep *_digest
    assert "content.snapshots[].convert_pin" in [p for p, _ in walk(rs)] and "content.convert_pin" in [p for p, _ in walk(rs)]
    full = release_schema(ReleaseConfig.model_validate({**cfg.as_json(), "column_drops": []}))
    entry = full.field("inband").type.field("entries").type.value_type
    assert entry.field("raw").type == BLOB and "raw_digest" not in entry.names and "media_type" not in entry.names
    assert "inband_sha256s" not in full.field("identity").type.field("members").type.value_type.names
    assert "inband" not in rs.names
    assert "inband_digests" not in rs.field("identity").type.field("members").type.value_type.names
    assert "members_digest" not in rs.field("identity").type.names
    assert "members_digest" not in rs.field("content").type.names


def test_text_media_type_and_the_release_form() -> None:
    """``text.media_type`` (D690, ``stack.schemas.structs.text_blob``, written by acquire and by the upcast):
    the raw's when the text is the raw bytes and those are ``text/*`` (an in-band abstract or post), else
    ``text/markdown`` (a converted text; also an octet-stream page whose conversion left its bytes
    unchanged). ``release_row`` only adds ``inline`` (NULL) to each BLOB."""
    def media(raw_media: str, same: bool) -> str:
        raw = {"sha256": "a" * 64, "bytes": 3, "media_type": raw_media}
        snap = {"snapshot_id": "x", "raw": raw, "text": text_blob("a" * 64 if same else "b" * 64, 3, raw)}
        row = {"content": {"current_text_sha256": snap["text"]["sha256"], "snapshots": [snap]},
               "inband": {"entries": [{"raw": None, "text": dict(raw)}]}, "identity": {"members": []}}
        out = release_row(row)
        assert out["content"]["snapshots"][0]["raw"] == {**raw, "inline": None}
        assert out["inband"]["entries"][0] == {"raw": None, "text": {**raw, "inline": None}}
        return str(out["content"]["snapshots"][0]["text"]["media_type"])

    assert media("text/plain", True) == "text/plain" and media("text/markdown", True) == "text/markdown"
    assert media("application/octet-stream", True) == "text/markdown"
    assert media("application/pdf", False) == media("text/plain", False) == "text/markdown"


# ---------------------------------------------------------------- the default internal export
def test_internal_export_blobs(world: dict[str, Any]) -> None:
    store = world["store"]
    runs_before = sorted(p for p in (store / "runs").rglob("*") if p.is_file())
    res = export(world, "exp")
    assert res.out and res.path == world["tmp"] / "exp"
    assert sorted(p for p in (store / "runs").rglob("*") if p.is_file()) == runs_before  # no run record
    assert not list((store / RELEASES).glob("*"))  # nothing in the store's releases/
    assert not list(world["tmp"].glob(".exp.*"))  # stage renamed away
    doc = json.loads((res.path / "release.json").read_text())
    rows = rows_of(res.corpus_path)
    assert sorted(rows) == list(INTERNAL_ROWS)
    assert (doc["rows_in"], doc["filtered_out"], doc["dropped"], doc["row_count"]) == (N, 3, {"shadow": 1}, 6)
    assert (doc["release_format"], doc["file_id"], doc["tag"], doc["audience"]) == (2, f"{rid(40)}.release", "exp",
                                                                                   "internal")
    assert doc["filter"] == "annotations.screen.status = 'ok' AND annotations.screen.payload.verdict = 'relevant'"
    assert doc["filter_checkable"] is True and doc["classes"] == {"open": 5, "restricted": 1}
    assert doc["source"]["file_id"] == world["build"] and doc["source"]["path"].startswith("data/corpus.build/")
    assert doc["config_digest"] == hashlib.sha256(jcs(doc["config"]).encode()).hexdigest()
    footer = pq.read_schema(res.corpus_path).metadata
    assert footer[b"stack.file_id"] == f"{rid(40)}.release".encode() and footer[b"stack.family"] == b"release"
    assert json.loads(footer[b"stack.blob_refs"]) == blob_ref_paths(release_schema(load_release_config("internal-v1")))
    assert footer[b"stack.blob_layout"] == b"blobs/<sha256>"
    # one snapshot per row, the current one; the abstract and HTML of doc 0 are gone
    for r in rows.values():
        c = r["content"]
        assert [s["snapshot_id"] for s in c["snapshots"]] == [c["current_snapshot_id"]]
        s = c["snapshots"][0]
        assert s["text"]["inline"] is None and s["raw"]["inline"] is None
        assert s["text"]["bytes"] == len(store_bytes(world, s["text"]["sha256"]))  # the corpus records the size
        same = s["text"]["sha256"] == s["raw"]["sha256"] and s["raw"]["media_type"].startswith("text/")
        assert s["text"]["media_type"] == (s["raw"]["media_type"] if same else "text/markdown")
    # blobs/: exactly the current texts, byte-identical to the store's, restricted text included
    want = {rows[i]["content"]["current_text_sha256"] for i in INTERNAL_ROWS}
    assert {p.name for p in (res.path / "blobs").iterdir()} == want
    for d in want:
        assert (res.path / "blobs" / d).read_bytes() == store_bytes(world, d)
    assert rows[2]["content"]["current_text_status"] == "ok"  # restricted ships internally
    assert doc["content"] | {} == {**doc["content"], "elements": 6, "blobs_count": 6, "inline_bytes": 0,
                                   "missing": 0, "records": 0}
    assert validate_release(res.path) == []
    # internal-v1 keeps run_ids, evidence_ids and the screen reasoning (only inband goes)
    s0 = rows[0]["annotations"]["screen"]
    assert s0["evidence_id"] and s0["payload"]["reasoning"] and rows[0]["identity"]["run_id"]


@pytest.mark.slow
def test_round_trip_inline_and_blobs(world: dict[str, Any]) -> None:
    blob_res = export(world, "exp-blobs")
    inline_res = export(world, "exp-inline", config(world["tmp"], "inline-v1", content={"place": "inline"}), run=41)
    assert not (inline_res.path / "blobs").exists()
    doc = json.loads((inline_res.path / "release.json").read_text())
    assert (doc["content"]["inline_count"], doc["content"]["blobs_count"]) == (6, 0)
    for res in (blob_res, inline_res):
        assert validate_release(res.path) == []
        rows = pq.read_table(res.corpus_path).to_pylist()  # pyarrow
        for r in rows:
            d = r["content"]["current_text_sha256"]
            got = resolve(r, d, [res.path])
            assert got == store_bytes(world, d)
            assert resolve(r, d, []) == (got if res is inline_res else None)  # inline needs no store
        con = duckdb.connect()  # duckdb, with the shipped macros
        register_macros(con)
        out = con.execute(f"SELECT d.doc_id, current_text(d), current_snapshot(d).text.sha256 "
                          f"FROM read_parquet('{res.corpus_path}') d ORDER BY doc_id").fetchall()
        assert len(out) == 6
        for doc_id, txt, digest in out:
            if res is inline_res:
                assert txt.encode() == store_bytes(world, digest)
            else:
                assert txt is None  # bytes are in blobs/<digest>
    assert resolve(rows[0], None, [blob_res.path]) is None
    # a store that lies is caught
    d0 = rows[0]["content"]["current_text_sha256"]
    (blob_res.path / "blobs" / d0).write_bytes(b"not it")
    with pytest.raises(BlobCorrupt):
        resolve({"content": {"snapshots": []}}, d0, [blob_res.path])


def test_snapshots_all_select_all_per_reference_licence(world: dict[str, Any]) -> None:
    """``snapshots: all`` + ``select: all``: each element by its own supplier (doc 4's public_web snapshot is
    ``pointer`` here: references and sizes kept, no bytes); doc 0's raw = text abstract is one blob, and inline fills both
    of its fields; a withheld row (doc 2 under ``restricted: withhold``) ships nothing."""
    rules = load_release_config("internal-v1").as_json()["licence_rules"]
    rules["public_web"] = {**rules["public_web"], "text": "pointer"}
    rules["restricted"] = {**rules["restricted"], "text": "withhold"}
    for place in ("blobs", "inline"):
        p = config(world["tmp"], f"all-{place}", snapshots="all", licence_rules=rules,
                   content={"select": "all", "place": place})
        res = export(world, f"exp-all-{place}", p, run=42 if place == "blobs" else 43)
        assert validate_release(res.path) == [], place
        rows = rows_of(res.corpus_path)
        s0 = rows[0]["content"]["snapshots"]
        assert len(s0) == 3
        web = rows[4]["content"]["snapshots"][1]
        assert web["raw"]["sha256"] and web["text"]["sha256"]  # pointer: kept
        assert (web["raw"]["bytes"], web["text"]["bytes"]) == (len(b"<html>4</html>"), len(b"web 4"))  # the rows' sizes
        c2 = rows[2]["content"]
        assert c2["current_text_status"] == "withheld" and c2["current_text_sha256"] is None
        assert all(s["raw"]["sha256"] is None and s["text"] is None for s in c2["snapshots"])
        assert s0[1]["text"]["media_type"] == "text/plain"  # the abstract's text is its raw bytes
        if place == "blobs":
            names = {q.name for q in (res.path / "blobs").iterdir()}
            assert web["raw"]["sha256"] not in names and web["text"]["sha256"] not in names
            assert {s0[1]["raw"]["sha256"], s0[2]["raw"]["sha256"], s0[0]["raw"]["sha256"],
                    s0[2]["text"]["sha256"]} <= names
            assert all((s[k] or {}).get("inline") is None
                       for r in rows.values() for s in r["content"]["snapshots"] for k in ("raw", "text"))
        else:
            assert s0[1]["raw"]["inline"] == s0[1]["text"]["inline"] == b"Abstract 0"  # per element, no dedupe
            assert s0[0]["raw"]["inline"] == store_bytes(world, s0[0]["raw"]["sha256"])
            assert web["raw"]["inline"] is None and web["text"]["inline"] is None
            assert not (res.path / "blobs").exists()


def test_withholding_member_in_a_shipped_row(world: dict[str, Any]) -> None:
    """A member whose class withholds, in a row whose text ships: its snapshots and in-band entries lose their
    references (D647 adds the entries); the anchor's stay."""
    rules = load_release_config("internal-v1").as_json()["licence_rules"]
    rules["public_web"] = {**rules["public_web"], "text": "withhold"}
    p = config(world["tmp"], "wm-v1", snapshots="all", licence_rules=rules, column_drops=[])
    res = export(world, "exp-wm", p)
    assert validate_release(res.path) == []
    r4 = rows_of(res.corpus_path)[4]
    s4 = sid(104)
    assert r4["content"]["current_text_status"] == "ok" and r4["content"]["current_text_sha256"]
    web = [s for s in r4["content"]["snapshots"] if s["source_id"] == s4]
    assert web and all(s["raw"]["sha256"] is None and s["text"] is None for s in web)
    assert [(e["raw"], e["text"]) for e in r4["inband"]["entries"] if e["source_id"] == s4] == [(None, None)]
    assert all(e["text"]["sha256"] for e in r4["inband"]["entries"] if e["source_id"] == sid(4))
    # the validator sees a leak put back
    _rewrite(res.corpus_path, lambda rows: [e.update(text={"sha256": "f" * 64, "bytes": 1, "media_type": "text/plain",
                                                           "inline": None})
                                            for r in rows for e in r["inband"]["entries"] if e["source_id"] == s4])
    assert any(f"in-band entry of withholding member {s4} keeps its references" in q for q in validate_release(res.path))


@pytest.mark.slow
def test_preflight_missing_and_cap(world: dict[str, Any]) -> None:
    tmp = world["tmp"]
    d1 = world["docs"][1]["content"]["current_text_sha256"]
    world["blobs"].local(d1).unlink()
    with pytest.raises(FoldError, match="pre-flight: 1 selected blob"):
        export(world, "exp-miss")
    assert not (tmp / "exp-miss").exists() and not list(tmp.glob(".exp-miss.*"))
    res = export(world, "exp-ptr", config(tmp, "ptr-v1", content={"missing": "pointer"}), run=44)
    assert (res.path / "missing-blobs.txt").read_text() == f"{d1}\n"
    assert not (res.path / "blobs" / d1).exists()
    doc = json.loads((res.path / "release.json").read_text())
    assert (doc["content"]["missing"], doc["content"]["blobs_count"]) == (1, 5)
    assert validate_release(res.path) == []
    res_i = export(world, "exp-ptr-i", config(tmp, "ptri-v1", content={"missing": "pointer", "place": "inline"}),
                   run=45)
    assert validate_release(res_i.path) == []
    assert rows_of(res_i.corpus_path)[1]["content"]["snapshots"][0]["text"]["inline"] is None
    with pytest.raises(FoldError, match="over content.max_total_bytes 10"):
        export(world, "exp-cap", config(tmp, "cap-v1", content={"max_total_bytes": 10, "missing": "pointer"}),
               run=46)
    assert not (tmp / "exp-cap").exists() and not list(tmp.glob(".exp-cap.*"))


def test_provider_records_opt_in(world: dict[str, Any]) -> None:
    rec = world["docs"][6]["inband"]["entries"][1]["raw"]["sha256"]
    res = export(world, "exp-rec", config(world["tmp"], "rec-v1", column_drops=[],
                                          content={"provider_records": True}), run=47)
    doc = json.loads((res.path / "release.json").read_text())
    assert doc["content"]["provider_records"] is True and doc["content"]["records"] == 1
    assert (res.path / "blobs" / rec).read_bytes() == b'{"provider": "record of 6"}'
    assert validate_release(res.path) == []
    plain = export(world, "exp-norec", run=48)
    assert not (plain.path / "blobs" / rec).exists()
    assert json.loads((plain.path / "release.json").read_text())["content"]["provider_records"] is False


@pytest.mark.slow
def test_out_never_records_or_pushes_and_refusals(world: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from stack.store import job

    pushed: list[str] = []

    class FakeJob:
        upload = True
        store = None

    monkeypatch.setattr(job, "current_job", lambda: FakeJob())
    monkeypatch.setattr(job, "push_release_dir", lambda store, tag: pushed.append(tag))
    export(world, "exp-push")
    assert pushed == []
    # the control: the in-store path does push under that job
    run_release(world["store"], run_id=rid(49), inputs={"corpus": world["build"]}, release="public-v1",
                dest=resolve_destination(world["store"], load_release_config("public-v1"), tag="pub"), allow_unbuilt=True)
    assert pushed == ["pub"]
    assert (world["store"] / RELEASES / "pub" / "corpus.parquet").exists()
    # an internal config that ships bytes is not written into the store
    with pytest.raises(FoldError, match="--out DIR"):
        run_release(world["store"], run_id=rid(50), inputs={"corpus": world["build"]}, release="internal-v1",
                    dest=resolve_destination(world["store"], load_release_config("internal-v1"), tag="int"), allow_unbuilt=True)
    # a non-empty --out is refused; an empty one is used
    (world["tmp"] / "full").mkdir()
    (world["tmp"] / "full" / "x").write_text("x")
    with pytest.raises(FoldError, match="not an empty directory"):
        export(world, "full", run=51)
    (world["tmp"] / "empty").mkdir()
    assert validate_release(export(world, "empty", run=52).path) == []


# ---------------------------------------------------------------- the validator
def _rewrite(path: Path, edit: Any) -> None:
    tbl = pq.read_table(path)
    rows = tbl.to_pylist()
    edit(rows)
    pq.write_table(pa.Table.from_pylist(rows, schema=tbl.schema), path)


@pytest.mark.slow
def test_validator_negatives(world: dict[str, Any]) -> None:
    tmp = world["tmp"]
    base = export(world, "v-inline", config(tmp, "vi-v1", content={"place": "inline"}))
    blobs = export(world, "v-blobs", run=53)

    def copy_of(src: Any, name: str) -> Path:
        dst = tmp / name
        shutil.copytree(src.path, dst)
        doc = json.loads((dst / "release.json").read_text())
        (dst / "release.json").write_text(json.dumps({**doc, "tag": name}))
        return dst

    d = copy_of(base, "bad-sha")  # inline bytes that do not hash to the digest
    _rewrite(d / "corpus.parquet", lambda rows: rows[0]["content"]["snapshots"][0]["text"].update(inline=b"tampered"))
    assert any("does not hash" in p for p in validate_release(d))
    d = copy_of(base, "bad-size")  # inline bytes whose BLOB says another size
    _rewrite(d / "corpus.parquet", lambda rows: rows[0]["content"]["snapshots"][0]["text"].update(bytes=1))
    assert any("is not the inline length" in p for p in validate_release(d))
    d = copy_of(base, "bad-null")  # a selected element without its bytes
    _rewrite(d / "corpus.parquet", lambda rows: rows[0]["content"]["snapshots"][0]["text"].update(inline=None))
    assert any("NULL but selected" in p for p in validate_release(d))
    d = copy_of(base, "bad-raw")  # raw bytes inline under select: text
    _rewrite(d / "corpus.parquet", lambda rows: rows[0]["content"]["snapshots"][0]["raw"].update(inline=b"x"))
    assert any("not selected, eligible and placed inline" in p for p in validate_release(d))
    d = copy_of(blobs, "bad-orphan")  # an unreferenced blob
    (d / "blobs" / hashlib.sha256(b"orphan").hexdigest()).write_bytes(b"orphan")
    assert any("not referenced" in p for p in validate_release(d))
    d = copy_of(blobs, "bad-missing")  # a selected blob gone
    victim = min((d / "blobs").iterdir())
    victim.unlink()
    assert any("neither in blobs/ nor in missing-blobs.txt" in p for p in validate_release(d))
    d = copy_of(blobs, "bad-name")  # a blob whose bytes are not its name's
    min((d / "blobs").iterdir()).write_bytes(b"other bytes")
    assert any("hashes to" in p for p in validate_release(d))
    d = copy_of(blobs, "bad-digest")
    doc = json.loads((d / "release.json").read_text())
    (d / "release.json").write_text(json.dumps({**doc, "config_digest": "0" * 64}))
    assert "config_digest is not the embedded config's" in validate_release(d)
    d = copy_of(blobs, "bad-fileid")
    doc = json.loads((d / "release.json").read_text())
    (d / "release.json").write_text(json.dumps({**doc, "file_id": "x.release"}))
    assert any("footer file_id" in p for p in validate_release(d))
    d = copy_of(blobs, "bad-filter")  # a row the recorded filter does not admit
    _rewrite(d / "corpus.parquet", lambda rows: rows[0]["annotations"]["screen"]["payload"].update(verdict="borderline"))
    assert any("do not satisfy the recorded filter" in p for p in validate_release(d))
    d = copy_of(blobs, "bad-snaps")  # snapshots: current, but two snapshots
    _rewrite(d / "corpus.parquet", lambda rows: rows[0]["content"]["snapshots"].append(
        {**rows[0]["content"]["snapshots"][0], "snapshot_id": "other"}))
    assert any("snapshots: current" in p for p in validate_release(d))


SUPERSEDED = ("release_format 1 is superseded by release_format 2 (D647): not validated or upcast; re-release "
              "from a build instead")


def test_withheld_inline_and_superseded_release_formats(world: dict[str, Any]) -> None:
    """Inline bytes in a withheld element fail; a release without ``release_format: 2`` (written before
    D647) is superseded: ``validate_release`` reports only that, V10 lists it as a warn (not validated)."""
    tmp = world["tmp"]
    rules = load_release_config("internal-v1").as_json()["licence_rules"]
    rules["restricted"] = {**rules["restricted"], "text": "withhold"}
    res = export(world, "w-inline", config(tmp, "wi-v1", licence_rules=rules, content={"place": "inline"}))
    assert validate_release(res.path) == []
    _rewrite(res.corpus_path, lambda rows: [r["content"]["snapshots"][0].update(
        text={"sha256": None, "bytes": 4, "media_type": "text/markdown", "inline": b"leak"})
        for r in rows if r["content"]["current_text_status"] == "withheld"])
    assert any("not selected, eligible and placed inline" in p for p in validate_release(res.path))

    # format 1 (no release_format, text/<digest>.md): superseded, whatever its content
    store = world["store"]
    doc = json.loads((res.path / "release.json").read_text())
    for fmt, tag in ((None, "old"), (1, "old1")):
        d = store / RELEASES / tag
        shutil.copytree(res.path, d)
        old = {k: v for k, v in doc.items() if k != "release_format"} | ({} if fmt is None else {"release_format": fmt})
        (d / "release.json").write_text(json.dumps({**old, "tag": tag}))
        (d / "text").mkdir()
        assert validate_release(d) == [SUPERSEDED]
    v10 = run_checks(store, names=["V-release"])[0]
    assert v10.status == "warn" and sorted(i.split(":")[0] for i in v10.items) == ["old", "old1"], v10.items
    shutil.rmtree(store / RELEASES / "old")
    shutil.rmtree(store / RELEASES / "old1")


# ---------------------------------------------------------------- CLI
runner = CliRunner()


def cli(*args: str, code: int = 0) -> str:
    res = runner.invoke(app, list(args))
    if res.exception is not None and not isinstance(res.exception, SystemExit):
        raise res.exception
    assert res.exit_code == code, res.output
    return res.output


@pytest.mark.slow
def test_cli_export_and_verify_release(world: dict[str, Any]) -> None:
    store, out = world["store"], world["tmp"] / "cli-exp"
    rel = world["build_path"].relative_to(store).as_posix()
    o = cli("--store", str(store), "export", "--in", rel, "--out", str(out), "--allow-unbuilt")
    line = json.loads(o.strip().splitlines()[-1])
    assert line["status"] == "ok" and line["outputs"] == {"release": str(out / "corpus.parquet")}
    assert f"to {out}" in o  # the destination line on stderr
    assert "release " + str(out) + ": ok" in cli("verify", "--release", str(out))
    assert not list((store / RELEASES).glob("*"))
    min((out / "blobs").iterdir()).write_bytes(b"x")
    assert "problem(s)" in cli("verify", "--release", str(out), code=1)
    # stack release refuses internal-v1 without --out, before any run
    plan = REPO / "configs" / "plans" / "dev-v2.yaml"
    assert "--out DIR" in cli("--store", str(store), "--plan", str(plan), "release", "--in", rel, "--config",
                              "internal-v1", "--allow-unbuilt", code=1)
    # stack export writes it (a second export under an explicit config; the tag is the directory name)
    o2 = cli("--store", str(store), "export", "--in", rel, "--config", "internal-v1",
             "--out", str(world["tmp"] / "cli-exp2"), "--allow-unbuilt")
    assert json.loads(o2.strip().splitlines()[-1])["status"] == "ok"
    assert json.loads((world["tmp"] / "cli-exp2" / "release.json").read_text())["tag"] == "cli-exp2"


@pytest.mark.slow
def test_release_destinations_through_every_entry_point(world: dict[str, Any]) -> None:
    """The destinations (D647; one resolver since D694): ``stack release`` and ``stack fold release`` into the
    store are one recorded run each (``releases/<tag>/``, the run record's ``outputs.release``, the printed
    line naming the store path); ``stack export`` (any ``--config``, default ``internal-v1``) writes a local
    directory (the tag is its name) with no run record, the printed line naming the absolute path; every
    destination refusal comes before any run and writes nothing, ``--out`` on ``stack release`` and ``stack
    fold release`` included (D694, D695: one export verb)."""
    from stack.runs import read_run_records

    store, tmp = world["store"], world["tmp"]
    rel = world["build_path"].relative_to(store).as_posix()
    plan = str(REPO / "configs" / "plans" / "dev-v2.yaml")
    base = ("--store", str(store), "--plan", plan)

    def line(out: str) -> dict[str, Any]:
        return json.loads(out.strip().splitlines()[-1])

    def runs() -> set[str]:
        return {r.run_id for r in read_run_records(store)}

    # into the store: a recorded run
    before = runs()
    got = line(cli(*base, "release", "--in", rel, "--config", "public-v1", "--tag", "t-store", "--allow-unbuilt"))
    assert got == {"run_id": got["run_id"], "status": "ok", "error_class": None,
                   "outputs": {"release": "releases/t-store/corpus.parquet"}}
    assert runs() - before == {got["run_id"]}
    rec = next(r for r in read_run_records(store) if r.run_id == got["run_id"])
    assert (rec.stage, rec.status, rec.inputs) == ("fold.release", "ok", {"corpus": world["build"]})
    assert set(rec.outputs["release"]) == {"file_id", "path", "file_digest", "row_count", "schema_digest",
                                           "chain_id", "kind"}
    assert rec.outputs["release"]["path"] == "releases/t-store/corpus.parquet"
    doc = json.loads((store / RELEASES / "t-store" / "release.json").read_text())
    assert (doc["tag"], doc["run_id"], doc["config"]["name"]) == ("t-store", got["run_id"], "public-v1")
    got = line(cli(*base, "fold", "release", "--input", f"corpus={world['build']}", "--release", "public-v1",
                   "--tag", "t-fold", "--allow-unbuilt"))
    assert got["outputs"] == {"release": "releases/t-fold/corpus.parquet"} and got["run_id"] in runs()

    # a local export: no run record, the tag is the directory's name
    for args, d, cfg in ((("export", "--in", rel, "--config", "public-v1", "--out", str(tmp / "x-rel")),
                          tmp / "x-rel", "public-v1"),
                         (("export", "--in", rel, "--out", str(tmp / "x-exp")), tmp / "x-exp", "internal-v1")):
        before = runs()
        out = cli(*base, *args, "--allow-unbuilt")
        got = line(out)
        assert got == {"run_id": got["run_id"], "status": "ok", "error_class": None,
                       "outputs": {"release": str(d / "corpus.parquet")}}
        assert f"to {d} (no run record, not pushed)" in out
        assert runs() == before
        doc = json.loads((d / "release.json").read_text())
        assert (doc["tag"], doc["run_id"], doc["config"]["name"]) == (d.name, got["run_id"], cfg)
    # stack export needs no plan
    assert line(cli("--store", str(store), "export", "--in", rel, "--out", str(tmp / "x-noplan"),
                    "--allow-unbuilt"))["status"] == "ok"

    # refusals, before any run, nothing written
    (tmp / "full").mkdir()
    (tmp / "full" / "x").write_text("x")
    before, listing = runs(), sorted(p.name for p in (store / RELEASES).iterdir())
    for args, msg in (
            (("release", "--in", rel, "--config", "public-v1", "--tag", "t-store"), "exists"),
            (("fold", "release", "--input", f"corpus={world['build']}", "--release", "public-v1", "--tag", "t-store"),
             "exists"),
            (("release", "--in", rel, "--config", "internal-v1", "--tag", "t-int"), "--out DIR"),
            (("fold", "release", "--input", f"corpus={world['build']}", "--release", "internal-v1"), "--out DIR"),
            # D695: stack release writes the store only; the export verb is stack export
            (("release", "--in", rel, "--config", "public-v1", "--out", str(tmp / "x-rel2")), "stack export --in"),
            (("release", "--in", rel, "--config", "internal-v1", "--out", str(tmp / "x-rel2")), "stack export --in"),
            (("export", "--in", rel, "--out", str(tmp / "full")), "not an empty directory"),
            (("export", "--in", rel, "--out", str(store / "inside")), "under the store root"),
            (("release", "--in", rel, "--config", "public-v1", "--tag", "../x"), "a plain directory name"),
            # D694: fold release writes the store only; the export verb is stack export
            (("fold", "release", "--input", f"corpus={world['build']}", "--release", "internal-v1",
              "--out", str(tmp / "x-fold")), "stack export")):
        out = cli(*base, *args, "--allow-unbuilt", code=1)
        assert msg in out and "Traceback" not in out, (args, out)
    assert runs() == before and sorted(p.name for p in (store / RELEASES).iterdir()) == listing
    assert not (tmp / "x-rel2").exists() and not (store / "inside").exists() and not (tmp / "x-fold").exists()
    assert "Missing option '--out'" in cli("--store", str(store), "export", "--in", rel, code=2)
    assert "--tag" in cli("--store", str(store), "export", "--in", rel, "--out", str(tmp / "x-t"), "--tag", "x",
                          code=2)  # a usage error: an export's tag is its directory's name
    assert not (tmp / "x-t").exists()
    # the library keeps the export tag rule for a caller passing both
    with pytest.raises(FoldError, match="the tag of an export is its directory's name"):
        resolve_destination(store, load_release_config("public-v1"), tmp / "x-tag", "other")
    assert not (tmp / "x-tag").exists()


# ---------------------------------------------------------------- review fixes (pre-merge, D647)
def _copy(src: Path, tmp: Path, name: str, **doc_over: Any) -> Path:
    """A copy of release directory ``src`` named ``name`` (its ``release.json`` ``tag`` updated, plus
    ``doc_over``)."""
    dst = tmp / name
    shutil.copytree(src, dst)
    doc = json.loads((dst / "release.json").read_text())
    (dst / "release.json").write_text(json.dumps({**doc, "tag": name, **doc_over}))
    return dst


def test_supplied_sql_has_no_file_access(world: dict[str, Any], tmp_path: Path) -> None:
    """B1: a filter or gate predicate that a release bundle, a corpus header or a plan supplies is evaluated
    on a duckdb connection without file or network access, as one statement: a ``COPY ... TO`` writes
    nothing and ``read_text`` puts no file content into any message."""
    from stack.checks.fold_checks import v_fold
    from stack.gate import Clause, GateError, sql_hits

    target = tmp_path / "pwned.txt"
    secret = tmp_path / "secret.txt"
    secret.write_text("the-secret-content")
    copy_ = f"true) OR 1=1; COPY (SELECT 42 AS a) TO '{target}' (FORMAT CSV); SELECT 1 WHERE (true"
    read = f"error((SELECT content FROM read_text('{secret}')))"
    tbl = pq.read_table(world["build_path"])
    for pred in (copy_, read):
        with pytest.raises(FoldError, match="does not evaluate") as ei:
            filter_rows(tbl, pred)
        assert "the-secret-content" not in str(ei.value)
        with pytest.raises(GateError, match="does not evaluate") as gi:
            sql_hits(tbl.select(["doc_id"]), [Clause(0, "t", sql=pred)])
        assert "the-secret-content" not in str(gi.value)
    assert not target.exists()
    # one statement: a second, in-memory statement is refused too; and the connection's settings are locked
    more = "true); CREATE TEMP TABLE x AS SELECT 1; SELECT 'zz' AS doc_id WHERE (true"
    with pytest.raises(GateError, match="not one"):
        sql_hits(tbl.select(["doc_id"]), [Clause(0, "t", sql=more)])
    con = sql_connection()
    with pytest.raises(duckdb.Error, match="locked"):
        con.execute("SET threads = 1")
    con.close()
    # a crafted export bundle, through stack verify --release's validator
    src = export(world, "b1-src")
    for i, pred in enumerate((copy_, read)):
        d = _copy(src.path, world["tmp"], f"b1-{i}", filter=pred, filter_checkable=True)
        problems = validate_release(d)
        assert any("does not evaluate" in p for p in problems), problems
        assert not any("the-secret-content" in p for p in problems)
    assert not target.exists()
    # a crafted corpus header, through V9 (its wrapper closes the predicate differently)
    v9_copy = f"true), false); COPY (SELECT 42 AS a) TO '{target}' (FORMAT CSV); SELECT 1 WHERE coalesce((true"
    for pred in (v9_copy, read):
        found = v_fold(tbl, {"fold_config": "build", "filter": pred, "annotations_folded": '["screen"]'},
                       family("corpus"))
        items = [i for f in found for i in f.items]
        assert any("does not evaluate" in i for i in items), items
        assert not any("the-secret-content" in i for i in items)
    assert not target.exists()


def test_internal_provider_records_ship_bytes(world: dict[str, Any]) -> None:
    """S1: ``select: none`` + ``provider_records: true`` still ships bytes, so an internal config is refused
    into the store (run_release and the CLI) before anything is written; ``--out`` takes it."""
    store, tmp = world["store"], world["tmp"]
    p = config(tmp, "recnone-v1", column_drops=[], content={"select": "none", "provider_records": True})
    with pytest.raises(FoldError, match="--out DIR"):
        run_release(store, run_id=rid(55), inputs={"corpus": world["build"]}, release=p,
                    dest=resolve_destination(store, load_release_config(p), tag="recnone"), allow_unbuilt=True)
    assert not list((store / RELEASES).glob("*")) and not list((store / RELEASES).glob(".*"))
    rel = world["build_path"].relative_to(store).as_posix()
    plan = REPO / "configs" / "plans" / "dev-v2.yaml"
    assert "--out DIR" in cli("--store", str(store), "--plan", str(plan), "release", "--in", rel, "--config",
                              str(p), "--tag", "recnone", "--allow-unbuilt", code=1)
    assert not list((store / RELEASES).glob("*"))
    res = export(world, "exp-recnone", p, run=56)
    assert validate_release(res.path) == []
    assert [q.name for q in (res.path / "blobs").iterdir()] == [world["docs"][6]["inband"]["entries"][1]["raw"]["sha256"]]


def test_preflight_lookup_errors_carry_no_body(world: dict[str, Any]) -> None:
    """S2: a failing remote LIST (botocore's errors are plain ``Exception`` subclasses) becomes a
    ``FoldError`` naming only the error type; a malformed digest is named."""
    import dataclasses

    from stack.fold.release import Ref, preflight
    from stack.store.blobs import BlobStore
    from stack.store.remote import open_store

    class ClientError(Exception):
        pass

    class FakeRemote:
        def list(self, prefix: str = "") -> Any:
            raise ClientError("GET https://secret.example/b?X-Amz-Signature=abc: AccessDenied")

    blobs = BlobStore(dataclasses.replace(open_store(world["store"]), remote=FakeRemote()))
    cfg = load_release_config("internal-v1")
    with pytest.raises(FoldError, match=r"blob lookup failed \(ClientError\); nothing written") as ei:
        preflight(blobs, [[[Ref("e" * 64, "current")]]], cfg)
    assert "secret" not in str(ei.value) and ei.value.__cause__ is None and ei.value.__suppress_context__
    with pytest.raises(FoldError, match=r"'\.\./x' is not a sha256 hex digest"):
        preflight(blobs, [[[Ref("../x", "current")]]], cfg)


def test_validator_malformed_bundles(world: dict[str, Any]) -> None:
    """S3: the validator reads only regular, non-symlink files (a symlink, directory or FIFO is a
    finding, never followed or read) and turns an unreadable parquet or filesystem error into a finding
    (text and ``--json``); V10 does too."""
    tmp = world["tmp"]
    src = export(world, "m-src").path
    outside = tmp / "outside.txt"
    outside.write_bytes(b"a file outside the bundle")
    d = _copy(src, tmp, "m-link")
    (d / "blobs" / hashlib.sha256(outside.read_bytes()).hexdigest()).symlink_to(outside)
    assert any("not a regular file" in p for p in validate_release(d))
    d = _copy(src, tmp, "m-dir")
    (d / "blobs" / ("a" * 64)).mkdir()
    assert any("not a regular file" in p for p in validate_release(d))
    d = _copy(src, tmp, "m-rj")  # release.json a symlink to a valid copy outside
    (tmp / "m-rj.json").write_text((d / "release.json").read_text())
    (d / "release.json").unlink()
    (d / "release.json").symlink_to(tmp / "m-rj.json")
    assert any("release.json: not a regular file" in p for p in validate_release(d))
    d = _copy(src, tmp, "m-bdir")  # blobs/ itself a symlink
    shutil.move(d / "blobs", tmp / "m-bdir-blobs")
    (d / "blobs").symlink_to(tmp / "m-bdir-blobs")
    assert any("blobs: not a directory" in p for p in validate_release(d))
    d = _copy(src, tmp, "m-trunc")
    data = (d / "corpus.parquet").read_bytes()
    (d / "corpus.parquet").write_bytes(data[: len(data) // 2])
    problems = validate_release(d)
    assert any(p.startswith("corpus.parquet unreadable") for p in problems), problems
    assert "corpus.parquet unreadable" in cli("verify", "--release", str(d), code=1)
    out = json.loads(cli("verify", "--release", str(d), "--json", code=1))
    assert out["status"] == "failed" and any("unreadable" in p for p in out["problems"])
    d = _copy(src, tmp, "m-fifo")  # last: the unfixed validator hangs reading it
    import os

    os.mkfifo(d / "blobs" / ("b" * 64))
    assert any("not a regular file" in p for p in validate_release(d))
    # V10: a release.json that is a directory is a finding, not a crash
    (world["store"] / RELEASES / "broken" / "release.json").mkdir(parents=True)
    v10 = run_checks(world["store"], names=["V-release"])[0]
    assert v10.status == "failed" and any("not a regular file" in i for i in v10.items), (v10.status, v10.items)
    shutil.rmtree(world["store"] / RELEASES / "broken")


@pytest.mark.slow
def test_validator_consistency(world: dict[str, Any]) -> None:
    """S4: a blobs-placed BLOB's ``bytes`` is its file's size; ``schema_digest`` and ``filter`` are the
    file's and the config's; ``missing-blobs.txt`` lists 64-hex digests that are selected and not in
    ``blobs/``; the ``content`` counters recompute."""
    tmp = world["tmp"]
    src = export(world, "c-src").path
    d = _copy(src, tmp, "c-bytes")
    _rewrite(d / "corpus.parquet", lambda rows: rows[0]["content"]["snapshots"][0]["text"].update(bytes=1))
    assert any("text.bytes 1 is not the size of blobs/" in p for p in validate_release(d))
    d = _copy(src, tmp, "c-schema", schema_digest="0" * 64)
    assert any("schema_digest" in p for p in validate_release(d))
    d = _copy(src, tmp, "c-filter", filter="true")
    assert any("filter 'true' is not the config's" in p for p in validate_release(d))
    doc = json.loads((src / "release.json").read_text())
    d = _copy(src, tmp, "c-count", content={**doc["content"], "elements": 99, "blobs_bytes": 1})
    problems = validate_release(d)
    assert any("content.elements 99" in p for p in problems) and any("content.blobs_bytes 1" in p for p in problems)
    ptr = export(world, "c-ptr", config(tmp, "cptr-v1", content={"missing": "pointer"}), run=57).path
    assert validate_release(ptr) == []
    in_blobs = min(q.name for q in (ptr / "blobs").iterdir())
    d = _copy(ptr, tmp, "c-missing")
    (d / "missing-blobs.txt").write_text(f"not-a-digest\n{'c' * 64}\n{in_blobs}\n")
    problems = validate_release(d)
    assert any("missing-blobs.txt: 'not-a-digest' is not a sha256" in p for p in problems), problems
    assert any(f"missing-blobs.txt: {'c' * 64} is not a selected element" in p for p in problems), problems
    assert any(f"missing-blobs.txt: {in_blobs} is also in blobs/" in p for p in problems), problems


def test_column_drops_keep_what_bytes_and_licence_read(world: dict[str, Any]) -> None:
    """S5: ``column_drops`` cannot drop the reference fields the bytes are filled into nor the fields the
    licence is read from (by path, ancestor or ``*.<name>``), so an embedded config obeys it too; the
    review's crash is a refusal before any work."""
    rules = {c: {"rows": "keep", "text": "ship", "abstract": "keep", "excerpt_cap_chars": None}
             for c in load_release_config("public-v1").licence_rules}
    pair = ["inband"]
    for drop in ("content.snapshots.raw", "content.snapshots.text", "content.snapshots.raw.sha256", "identity.members",
                 "identity.anchor_source_id", "metadata.field_sources", "content.current_source_id",
                 "content.current_text_sha256", "content.current_text_status", "*.raw", "*.sha256", "content",
                 "identity.members.license_class", "*.source_id"):
        with pytest.raises(ValueError, match="column_drops"):
            ReleaseConfig.model_validate({"name": "t", "licence_rules": rules, "column_drops": [drop, *pair]})
    with pytest.raises(ValueError, match="column_drops"):  # inband kept: its references are filled too
        ReleaseConfig.model_validate({"name": "t", "licence_rules": rules, "column_drops": ["inband.entries.raw"]})
    ReleaseConfig.model_validate({"name": "t", "licence_rules": rules,
                                  "column_drops": ["inband.entries.raw", *pair]})  # inband gone: fine
    p = config(world["tmp"], "dropref-v1", content={"select": "all"},
               column_drops=["content.snapshots.raw", *pair])
    with pytest.raises(FoldError, match="column_drops"):
        export(world, "exp-dropref", p)
    assert not (world["tmp"] / "exp-dropref").exists() and not list(world["tmp"].glob(".exp-dropref.*"))


def test_column_drops_keep_the_privacy_fields() -> None:
    """``identity.is_masked`` and ``metadata.sensitivity`` cannot be dropped (by path or ``*.<name>``): the
    validator's masked and sensitivity checks read them from the shipped rows, and without them those checks
    pass vacuously. ``state`` may still be dropped."""
    rules = {c: {"rows": "keep", "text": "ship", "abstract": "keep", "excerpt_cap_chars": None}
             for c in load_release_config("public-v1").licence_rules}
    pair = ["inband"]
    for drop in ("identity.is_masked", "metadata.sensitivity", "*.is_masked", "*.sensitivity"):
        with pytest.raises(ValueError, match="column_drops.*privacy"):
            ReleaseConfig.model_validate({"name": "t", "licence_rules": rules, "column_drops": [drop, *pair]})
    cfg = ReleaseConfig.model_validate({"name": "t", "licence_rules": rules, "column_drops": ["state", *pair]})
    assert "state" in cfg.column_drops


def test_column_drops_must_name_a_documents_field() -> None:
    """D705: a ``column_drops`` entry that names no documents field (a typo; ``inline``, which only the release
    schema has) would drop nothing; it is refused. Red before (accepted, and the release shipped the field)."""
    base = load_release_config("public-v1").as_json()
    for bad in ("annotations.model_role", "annotation.screen", "*.evidence_ids", "*.inline",
                "content.snapshots.text.inline"):
        with pytest.raises(ValueError, match="names no documents field"):
            ReleaseConfig.model_validate({**base, "column_drops": [*base["column_drops"], bad]})
    for good in ("annotations.model_roles", "content.snapshots.quality", "*.decided_at", "annotations.other"):
        ReleaseConfig.model_validate({**base, "column_drops": [*base["column_drops"], good]})
    # public-v1 before D705 (the config embedded in releases written before it) still validates
    old = {**base, "filter": "everything", "column_drops": [d for d in base["column_drops"]
                                                            if not d.startswith(("annotations.model_",
                                                                                 "annotations.technique_",
                                                                                 "annotations.affiliation_orgs",
                                                                                 "annotations.translation"))]}
    assert len(old["column_drops"]) == 7
    ReleaseConfig.model_validate(old)


UNPOPULATED = ("model_roles", "model_regexp_matches", "technique_roles", "technique_staged")


def test_public_v1_releases_the_screen_relevant_rows_without_the_unpopulated_structs(world: dict[str, Any]) -> None:
    """D705: public-v1 filters as internal-v1 (screen ``ok`` and verdict ``relevant``: borderline doc 5 and the
    not relevant 8, 9 are filtered out), then its licence rules drop doc 3 (shadow anchor) and withhold doc 2's
    text (restricted); the four annotation structs no build populates yet do not ship (internal-v1 keeps them).
    The hash pointers and the provenance fields stay; evidence_id and the screen reasoning do not. Red under the
    previous public-v1 (``filter: everything``: 9 rows; the four structs shipped NULL)."""
    store = world["store"]
    pub, internal = load_release_config("public-v1"), load_release_config("internal-v1")
    assert pub.filter == internal.filter == "screen_relevant"
    res = run_release(store, run_id=rid(45), inputs={"corpus": world["build"]}, release="public-v1",
                      dest=resolve_destination(store, pub, tag="p1"), allow_unbuilt=True)
    doc = json.loads((res.path / "release.json").read_text())
    rows = rows_of(res.corpus_path)
    print(f"public-v1: {doc['rows_in']} -> {doc['row_count']}, filtered out {doc['filtered_out']}, "
          f"dropped {doc['dropped']}")
    assert sorted(rows) == list(INTERNAL_ROWS)
    assert (doc["rows_in"], doc["filtered_out"], doc["dropped"], doc["row_count"]) == (N, 3, {"shadow": 1}, 6)
    assert doc["filter_checkable"] is True and doc["build_status"] == {"status": "unbuilt"}
    assert rows[2]["content"]["current_text_status"] == "withheld" and rows[2]["content"]["current_text_sha256"] is None
    assert rows[0]["content"]["current_text_sha256"] == world["docs"][0]["content"]["current_text_sha256"]
    pub_names = release_schema(pub).field("annotations").type.names
    assert not set(UNPOPULATED) & set(pub_names) and not set(UNPOPULATED) & set(rows[0]["annotations"])
    assert set(UNPOPULATED) <= set(release_schema(internal).field("annotations").type.names)
    assert {"triage", "screen", "affiliation", "doc_class", "text_usability"} <= set(pub_names)
    paths = {p.replace("[]", "") for p, _f in walk(release_schema(pub))}
    kept = ("content.current_text_sha256", "content.snapshots.raw.sha256", "content.snapshots.text.sha256",
            "content.snapshots.text.bytes", "content.snapshots.text.media_type", "annotations.screen.basis_digest",
            "metadata.field_sources.source_id", "identity.members.license_class", "annotations.screen.decided_at",
            "annotations.screen.config_digest", "annotations.screen.payload.quotes.text",
            "annotations.screen.payload.confidence")
    gone = ("annotations.screen.evidence_id", "annotations.screen.payload.reasoning", "inband")
    assert set(kept) <= paths and not set(gone) & paths, (set(kept) - paths, set(gone) & paths)
    assert not (res.path / "blobs").exists() and not (res.path / "missing-blobs.txt").exists()
    assert validate_release(res.path) == []


def test_store_release_validated_before_rename(world: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """S6: a release into the store is validated over its stage before the rename, as an export is: a
    problem leaves no tag directory, no stage and nothing pushed."""
    from stack.checks import fold_checks
    from stack.store import job

    pushed: list[str] = []

    class FakeJob:
        upload = True
        store = None

    monkeypatch.setattr(job, "current_job", lambda: FakeJob())
    monkeypatch.setattr(job, "push_release_dir", lambda store, tag: pushed.append(tag))
    monkeypatch.setattr(fold_checks, "validate_release", lambda path, tag=None: ["injected problem"])
    with pytest.raises(FoldError, match="injected problem"):
        run_release(world["store"], run_id=rid(58), inputs={"corpus": world["build"]}, release="public-v1",
                    dest=resolve_destination(world["store"], load_release_config("public-v1"), tag="inj"), allow_unbuilt=True)
    assert pushed == [] and list((world["store"] / RELEASES).iterdir()) == []


NEWER = "release_format 3 is newer than this code reads (release_format 2): not validated; read it with newer code"


def test_validate_release_classifies_the_format_first(world: dict[str, Any]) -> None:
    """S7: ``release_format`` is classified before anything else is checked: an older release without
    a corpus is superseded (V10: a warn), a newer one is reported as newer, whatever else it has."""
    tmp = world["tmp"]
    for fmt, want in ((None, SUPERSEDED), (1, SUPERSEDED), (3, NEWER), ("x", "release_format 'x' is not an integer")):
        d = tmp / f"fmt-{fmt}"
        d.mkdir()
        (d / "release.json").write_text(json.dumps({"tag": d.name} | ({} if fmt is None else {"release_format": fmt})))
        assert validate_release(d) == [want]
    src = export(world, "fmt-src").path
    assert validate_release(_copy(src, tmp, "fmt-3c", release_format=3)) == [NEWER]
    d = world["store"] / RELEASES / "old-nocorpus"
    d.mkdir(parents=True)
    (d / "release.json").write_text(json.dumps({"tag": "old-nocorpus"}))
    v10 = run_checks(world["store"], names=["V-release"])[0]
    assert v10.status == "warn" and [i.split(":")[0] for i in v10.items] == ["old-nocorpus"], v10.items
    shutil.rmtree(d)


@pytest.mark.slow
def test_current_text_ref_without_a_kept_snapshot(tmp_path: Path) -> None:
    """N2: a current text that no kept snapshot carries (doc 1's ``current_snapshot_id`` names no
    snapshot, so ``snapshots: current`` keeps none) ships as ``Ref('current')``: to ``blobs/`` even under
    ``place: inline``, and the validator wants it there."""
    w = make_world(tmp_path, edit=lambda rows: rows[1]["content"].update(current_snapshot_id=f"{rid(1)}.9"))
    d1 = w["docs"][1]["content"]["current_text_sha256"]
    for place, run in (("blobs", 40), ("inline", 41)):
        res = export(w, f"cur-{place}", config(w["tmp"], f"cur{place}-v1", content={"place": place}), run=run)
        assert validate_release(res.path) == [], place
        r1 = rows_of(res.corpus_path)[1]
        assert r1["content"]["snapshots"] == [] and r1["content"]["current_text_sha256"] == d1
        names = {q.name for q in (res.path / "blobs").iterdir()}
        assert (d1 in names) and (names == {d1} if place == "inline" else len(names) == 6), names
        assert (res.path / "blobs" / d1).read_bytes() == store_bytes(w, d1)
        (res.path / "blobs" / d1).unlink()
        assert any(f"selected current {d1} neither in blobs/" in p for p in validate_release(res.path))


@pytest.mark.slow
def test_preflight_cap_counts_inline_bytes_per_element(world: dict[str, Any]) -> None:
    """N4: under ``place: inline`` the cap counts every inline element's bytes, a digest inline twice
    (doc 0's abstract: raw = text) twice."""
    tmp = world["tmp"]
    base = {"select": "all", "place": "inline"}
    res = export(world, "capi", config(tmp, "capi-v1", snapshots="all", content=base))
    doc = json.loads((res.path / "release.json").read_text())
    inl = [(s[k]["sha256"], len(s[k]["inline"])) for r in pq.read_table(res.corpus_path).to_pylist()
           for s in r["content"]["snapshots"] for k in ("raw", "text") if s[k] and s[k]["inline"] is not None]
    assert len(inl) > len({h for h, _ in inl})  # a digest inline twice
    assert doc["content"]["inline_bytes"] == sum(n for _, n in inl) > 0
    total = doc["content"]["inline_bytes"] + doc["content"]["blobs_bytes"]
    with pytest.raises(FoldError, match=f"over content.max_total_bytes {total - 1}"):
        export(world, "capi-1", config(tmp, "capi1-v1", snapshots="all", content={**base, "max_total_bytes": total - 1}),
               run=61)
    ok = export(world, "capi-0", config(tmp, "capi0-v1", snapshots="all", content={**base, "max_total_bytes": total}),
                run=62)
    assert validate_release(ok.path) == []


def test_out_dir_not_under_the_store_and_tag_grammar(world: dict[str, Any]) -> None:
    """N6: ``--out`` under the store root is refused (V10 would read it as a store release). N7: ``stack
    release --tag ../x`` is a refusal, not a traceback."""
    store = world["store"]
    for d in (store / RELEASES / "exp", store / "elsewhere"):
        with pytest.raises(FoldError, match="under the store root"):
            run_release(store, run_id=rid(63), inputs={"corpus": world["build"]}, release="internal-v1",
                        dest=resolve_destination(store, load_release_config("internal-v1"), d), allow_unbuilt=True)
        assert not d.exists()
    rel = world["build_path"].relative_to(store).as_posix()
    plan = REPO / "configs" / "plans" / "dev-v2.yaml"
    assert "a plain directory name" in cli("--store", str(store), "--plan", str(plan), "release", "--in", rel,
                                           "--config", "public-v1", "--tag", "../x", "--allow-unbuilt", code=1)
