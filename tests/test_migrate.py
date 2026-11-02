"""Materialising format upcasts of stored files (``stack/migrate.py``, ``stack migrate upcast``; D208,
amended by D295).

Synthetic only. The clean check store (``tests.fixtures.checks``: a ``documents`` chain F1-F5
with a compaction F4, a ``screen`` file and its evidence, a ``corpus`` file, a build record) is
written at the current format; ``screen_evidence`` is then **downgraded** to its real legacy v1 as a
pre-D295 writer wrote it (:func:`tests.test_family_format.downgrade`), and ``documents``/``corpus``
are read under a **future additive version** (:func:`tests.test_family_format.future_version`: since
D690 no older documents version reads, so the store's files are made the older version instead). The
readers read the old files upcast; ``stack migrate upcast`` (dry run with per file ``v<k> → v<N>``,
then real) materialises the chain and the evidence file at the current format; the compaction is the old view with the new fields NULL; the store-scope checks pass and
V12 compares with the upcast base view; a file that does not read refuses. The slow test seeds a
build with an older and with an upcast evidence file as the head of ``data/screen_evidence/main/``
(the build line's prior directory, D373; D200).
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from typer.testing import CliRunner

from stack.checks.chain import StoreView, v_compaction_equal
from stack.checks.verify import run_checks
from stack.cli import app
from stack.migrate import UpcastError, upcast_table
from stack.runs import file_digest, find_run_record, read_run_records
from stack.schemas import upcasters
from stack.schemas.families import family, schema_digest
from stack.stage_runner import WORKFLOWS_ROOT_ENV
from stack.store.chain import HeaderFields, read_view, resolve
from stack.store.layout import DATA, HEADER_PREFIX, parse_data_key
from stack.store.parquet import file_format, read_family, read_header, write_family
from tests.fixtures.checks import build_store
from tests.synth.records import record_files, rewrite_record, sdir, write_series
from tests.synth.rows import AT, dg, document_row, rid, table
from tests.test_family_format import ADDED_SINCE, FUTURE_FIELD, downgrade, future_version, with_nulls

pytestmark = pytest.mark.e2e  # area marker(s), not a tier (pyproject markers; docs/SETUP.md §7)

runner = CliRunner()
Row = dict[str, Any]


# ---------------------------------------------------------------- the old formats (tests.test_family_format)
def _path(root: Path, fid: str) -> Path:
    (p,) = [q for q in (root / DATA).rglob("*.parquet") if read_header(q)["file_id"] == fid]
    return p


def _files(root: Path, fam: str) -> list[Path]:
    """The ``data/`` files of family ``fam``."""
    return [q for q in (root / DATA).rglob("*.parquet") if parse_data_key(q).family == fam]


def _old_view(root: Path, fids: list[str]) -> dict[str, Row]:
    """The old chain's view computed naively, at the current version by hand: per doc_id the row
    of the newest file (``fids`` in chain order from the newest full file), read raw, the fields
    its legacy version lacks set NULL (:data:`ADDED_SINCE`)."""
    out: dict[str, Row] = {}
    for fid in fids:
        path = _path(root, fid)
        fmt = file_format(path, read_header(path))
        for r in pq.read_table(path).to_pylist():
            out[r["doc_id"]] = r if fmt.is_current else {**r, FUTURE_FIELD.name: None}
    return out


def _listing(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for d in ("data", "runs") for p in (root / d).rglob("*"))


def _cli(*args: str) -> Any:
    return runner.invoke(app, list(args))


@pytest.fixture
def old_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    root = tmp_path / "store"
    build_store(root)
    docs = sorted((read_header(p)["file_id"] for p in _files(root, "documents")), key=lambda f: f.split(".")[0])
    (ev,) = downgrade(root, "screen_evidence", 1)
    written = family("documents").version
    future_version(monkeypatch, "documents", "corpus")
    return {"root": root, "doc_paths": [FUTURE_FIELD.name], "ev_paths": ADDED_SINCE[("screen_evidence", 1)],
            "docs": docs, "ev": ev, "written": written}


# ---------------------------------------------------------------- tests
def test_upcast_table_additive_and_refusals() -> None:
    inner_old = pa.struct([pa.field("a", pa.string(), False)])
    inner = pa.struct([pa.field("a", pa.string(), False), pa.field("q", pa.struct([
        pa.field("x", pa.string(), False), pa.field("l", pa.list_(pa.string()), False)]), True)])
    old = pa.schema([pa.field("k", pa.string(), False), pa.field("s", pa.list_(pa.field("item", inner_old, False)), True)])
    new = pa.schema([pa.field("n", pa.int64(), True), pa.field("k", pa.string(), False),
                     pa.field("s", pa.list_(pa.field("item", inner, False)), True)])
    t = pa.table({"k": ["a", "b", "c"], "s": [[{"a": "1"}, {"a": "2"}], None, []]}, schema=old)
    up = upcast_table(t.slice(1), new)  # a sliced table: offsets not at 0
    assert up.schema == new
    assert up.to_pylist() == [{"n": None, "k": "b", "s": None}, {"n": None, "k": "c", "s": []}]
    up = upcast_table(t, new)
    assert up.column("s").to_pylist()[0] == [{"a": "1", "q": None}, {"a": "2", "q": None}]
    buf = io.BytesIO()
    pq.write_table(up, buf)  # a NULL struct with non-nullable children writes
    # already current: the same object, so it writes byte-identically
    assert upcast_table(up, new) is up

    def refused(schema: pa.Schema, target: pa.Schema) -> str:
        with pytest.raises(UpcastError) as exc:
            upcast_table(schema.empty_table(), target)
        return str(exc.value)

    assert "s[].a: type string in the old schema, int32 in the target" in refused(old, pa.schema([
        old.field("k"), pa.field("s", pa.list_(pa.field("item", pa.struct([pa.field("a", pa.int32(), False)]), False)))]))
    assert "s: not in the target schema" in refused(old, pa.schema([old.field("k")]))
    assert "m: missing in the old schema and not nullable" in refused(old, old.append(pa.field("m", pa.int64(), False)))
    assert "s: nullable in the old schema, not nullable" in refused(old, pa.schema([old.field("k"), old.field("s").with_nullable(False)]))
    # relaxing a field to nullable keeps its values
    relaxed = pa.schema([old.field("k").with_nullable(True), old.field("s")])
    assert upcast_table(t, relaxed).column("k").to_pylist() == ["a", "b", "c"]


@pytest.mark.slow
def test_migrate_documents_chain_and_evidence_file(old_store: dict[str, Any]) -> None:
    root, docs, ev = old_store["root"], old_store["docs"], old_store["ev"]
    doc_paths, ev_paths = old_store["doc_paths"], old_store["ev_paths"]
    spec = family("documents")
    v0 = old_store["written"]
    d0 = upcasters.version("documents", v0).digest  # type: ignore[union-attr]
    old_ids = docs[3:]  # F4 (the compaction, full) and F5: the version, one version behind

    # the readers read the old version upcast (D295)
    assert resolve(root, sdir("documents")).versions == (v0, v0) and spec.version == v0 + 1
    assert read_family(_path(root, ev))[0].schema.equals(family("screen_evidence").schema)
    old = _old_view(root, old_ids)
    assert {r["doc_id"]: r for r in read_view(resolve(root, sdir("documents"))).to_pylist()} == old

    # dry run: what would change; nothing written
    before = _listing(root)
    r = _cli("migrate", "upcast", "--store", str(root), "--family", "documents", "--dry-run")
    assert r.exit_code == 0, r.output
    (dry,) = [json.loads(ln) for ln in r.output.splitlines()]
    cur = f"v{spec.version}"
    def kinds(start: int) -> list[str]:
        return [v.kind for v in upcasters.path("documents", start, spec.version)]

    assert kinds(v0) == ["additive"]
    assert dry["files"] == [
        {"file_id": old_ids[0], "from": f"v{v0}", "to": cur, "steps": kinds(v0)},
        {"file_id": old_ids[1], "from": f"v{v0}", "to": cur, "steps": kinds(v0)}]
    assert dry["old_schema_digests"] == {d0: old_ids}
    assert dry["new_schema_digest"] == spec.schema_digest and dry["current_version"] == spec.version
    # "added" is the union over the files whose steps are all additive (a function step is named by its kind)
    want_added = doc_paths
    assert sorted(dry["added"]) == sorted(want_added) and dry["head"] == docs[-1] and not dry["current"]
    assert dry["chain_id"] == "data/documents/main"  # the chain is the series directory (D372)
    assert dry["rows"] == len(old)
    r = _cli("migrate", "upcast", "--store", str(root), "--family", "screen_evidence", "--file", ev, "--dry-run")
    assert r.exit_code == 0, r.output
    assert sorted(json.loads(r.output)["added"]) == sorted(ev_paths)
    assert _listing(root) == before

    # real: one fold.compact run on the head
    r = _cli("migrate", "upcast", "--store", str(root), "--family", "documents")
    assert r.exit_code == 0, r.output
    out = json.loads(r.output)
    assert (out["status"], out["stage"]) == ("ok", "fold.compact")
    chain = resolve(root, sdir("documents"))
    assert chain.version_chain == (out["file_id"],)
    h = read_header(chain.files[0].path)
    f5_name = _path(root, docs[-1]).name
    assert (h["kind"], h["base"], h["after"], h["fold_config"], h["stage"], h["schema_digest"]) == (
        "full", f5_name, f5_name, "compact", "fold.compact", spec.schema_digest)
    rec = find_run_record(root, out["run_id"])
    assert rec is not None and rec.counts[f"upcast_from.{d0}"] == 2
    assert rec.counts["fields_added"] == len(want_added) and rec.counts["rows"] == len(old)
    # the compaction is the old view, new fields NULL, row for row, written at the current version
    assert chain.versions == (spec.version,) and h["family_version"] == str(spec.version)
    got = {row["doc_id"]: row for row in read_view(chain).to_pylist()}
    assert got == old
    assert any(row["content"] and row["content"]["snapshots"] for row in got.values())  # (b) exercised

    # the evidence file: a new full file, the old one untouched
    old_ev_digest = file_digest(_path(root, ev))
    r = _cli("migrate", "upcast", "--store", str(root), "--family", "screen_evidence", "--file", ev)
    assert r.exit_code == 0, r.output
    out = json.loads(r.output)
    assert (out["status"], out["stage"]) == ("ok", "migrate.upcast")
    new_tbl, nh = read_family(_path(root, out["file_id"]))
    # a pair family: the sibling screen file of the same old run is upcast in the same run (D380)
    sib = f"{ev.rsplit('.', 1)[0]}.screen"
    assert (nh["stage"], nh["writer"], json.loads(nh["inputs"])) == ("migrate.upcast", "migrate.upcast",
                                                                     {"screen_evidence": ev, "screen": sib})
    up = find_run_record(root, out["run_id"])
    assert up is not None and set(up.outputs) == {"screen", "screen_evidence"}
    assert {parse_data_key(o["path"]).run_id for o in up.outputs.values()} == {out["run_id"]}
    assert new_tbl.to_pylist() == [with_nulls(row, ev_paths) for row in pq.read_table(_path(root, ev)).to_pylist()]
    assert file_digest(_path(root, ev)) == old_ev_digest

    # refusals before any run: already current, a corpus file, a chained family with --file
    n_runs = len(read_run_records(root))
    for args, msg in (
        (("--family", "screen_evidence", "--file", out["file_id"]), "already at the current format"),
        (("--family", "documents"), "nothing to upcast"),
        (("--family", "corpus", "--file", read_header(_files(root, "corpus")[0])["file_id"]), "not migrated"),
        (("--family", "documents", "--file", docs[0]), "migrated whole"),
    ):
        r = _cli("migrate", "upcast", "--store", str(root), *args)
        assert r.exit_code == 1 and msg in r.output, (args, r.output)
    assert len(read_run_records(root)) == n_runs

    # verify: every check passes (V8 exempts the upcast evidence file from the same-run pairing); the
    # superseded older-version files stay in data/ and are checked as their upcast
    results = {c.name: c for c in run_checks(root)}
    bad = {n: c.items for n, c in results.items() if c.status == "failed"}
    assert bad == {}
    assert results["V-compaction-equal"].status == results["schema_equal"].status == "ok"

    # V12 compares the compaction with the upcast base view: an old file changed under it is red
    view = StoreView(root)
    base = view.base_view(view.file(chain.head))  # type: ignore[arg-type]
    assert base is not None and base.equals(read_view(chain))
    f5 = _path(root, docs[-1])
    t5 = pq.read_table(f5)
    rows = t5.to_pylist()
    rows[0]["state"] = "masked"
    p5_meta = t5.schema.metadata
    f5.unlink()
    pq.write_table(pa.Table.from_pylist(rows, schema=t5.schema).replace_schema_metadata(p5_meta), f5)
    bad = v_compaction_equal(StoreView(root))
    assert bad and "differs in ['state']" in " ".join(bad[0].items)


@pytest.mark.slow
def test_v8_accepts_an_upcast_evidence_file(old_store: dict[str, Any]) -> None:
    root, ev = old_store["root"], old_store["ev"]
    r = _cli("migrate", "upcast", "--store", str(root), "--family", "screen_evidence", "--file", ev)
    assert r.exit_code == 0, r.output
    (v8,) = run_checks(root, names=["V-evidence"])
    assert v8.status == "ok", v8.items


def test_upcast_refuses_a_file_that_is_not_its_directorys_current_head(old_store: dict[str, Any],
                                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    """S25-13: the new full goes after the directory's head, so an older file (here: the evidence
    file once its upcast is the head) or a pair whose sibling is orphaned would publish old rows as
    the series' newest. Both are refused, dry run included, before any run exists; the run checks
    the head again before it stages (a head that moved after the check: a failed run, nothing
    published)."""
    import stack.migrate as M

    root, ev = old_store["root"], old_store["ev"]
    r = _cli("migrate", "upcast", "--store", str(root), "--family", "screen_evidence", "--file", ev)
    assert r.exit_code == 0, r.output
    n_runs, before = len(read_run_records(root)), _listing(root)
    for extra in ((), ("--dry-run",)):
        r = _cli("migrate", "upcast", "--store", str(root), "--family", "screen_evidence", "--file", ev, *extra)
        assert r.exit_code == 1 and "is not the last published file of data/screen_evidence/main" in r.output, r.output
    assert len(read_run_records(root)) == n_runs and _listing(root) == before
    monkeypatch.setattr(M, "_refuse_not_head", lambda *_a: None)  # the head moves after the check
    rec = M.upcast_file(root, "screen_evidence", ev)
    assert rec.status == "failed" and "is no longer the last file" in str(rec.error) and not rec.outputs


def test_upcast_refuses_a_pair_with_an_orphaned_sibling(old_store: dict[str, Any]) -> None:
    from stack.store.chain import orphan

    root, ev = old_store["root"], old_store["ev"]
    sib = f"{ev.rsplit('.', 1)[0]}.screen"
    orphan(root, _path(root, sib).relative_to(root).as_posix(), note="test: the answers only")
    n_runs = len(read_run_records(root))
    r = _cli("migrate", "upcast", "--store", str(root), "--family", "screen_evidence", "--file", ev)
    assert r.exit_code == 1 and f"{sib} is excluded" in r.output, r.output
    assert len(read_run_records(root)) == n_runs


@pytest.mark.slow
def test_file_not_its_declared_version_refused(tmp_path: Path) -> None:
    """A file whose schema differs by a type change while it declares the current version (its
    digest honest about its own schema) does not read: S1 names it, the migration refuses it by
    name, nothing written."""
    root = tmp_path / "store"
    build_store(root)
    p = _files(root, "screen_evidence")[0]
    tbl, h = read_family(p)
    i = tbl.schema.get_field_index("attempts")
    bad = tbl.set_column(i, pa.field("attempts", pa.int64(), False), tbl.column(i).cast(pa.int64()))
    h["schema_digest"] = schema_digest(bad.schema)
    p.unlink()
    pq.write_table(bad.replace_schema_metadata({f"{HEADER_PREFIX}{k}".encode(): v.encode() for k, v in h.items()}), p)
    rec = find_run_record(root, h["run_id"])  # the record honest about the file's bytes (D371 checks them)
    assert rec is not None
    next(o for o in rec.outputs.values() if o["file_id"] == h["file_id"])["file_digest"] = file_digest(p)
    rewrite_record(root, rec)
    s1 = run_checks(root, names=["schema_equal"])[0]
    assert s1.status == "failed" and p.name in " ".join(f.message for f in s1.findings)
    before = _listing(root)
    r = _cli("migrate", "upcast", "--store", str(root), "--family", "screen_evidence", "--file", h["file_id"])
    want = f"declares screen_evidence v{family('screen_evidence').version}, but its schema_digest"
    assert r.exit_code == 1 and want in r.output and p.name in r.output, r.output
    assert _listing(root) == before
    # and the writer still refuses the old table (the registry is the only schema written)
    with pytest.raises(Exception, match="schema_equal"):
        write_family(tmp_path / "x" / p.name, bad, h)


def test_upcast_in_selects_series_directories(old_store: dict[str, Any]) -> None:
    """S25-38: ``--in data/<family>/<series>/`` (repeatable) plans and migrates only those series
    directories; without it every series is planned, so a second ``documents`` series whose file
    does not read blocks the plan. A directory of another family, without a series, or with no
    published file, and ``--in`` on a chain-of-one family, are refused before any run."""
    import datetime as dt

    root, ev = old_store["root"], old_store["ev"]
    fields = HeaderFields(stage="gather", run_id=rid(60), config_digest=dg("c"), code_digest=dg("code"),
                          plan="dev-v2", blocks_written=["identity", "metadata", "inband"])
    at = AT + dt.timedelta(days=1)
    other = write_series(root, "documents", "documents", table("documents", [document_row(i, 60) for i in range(3)]),
                         fields, series="other", at=at, finished_at=at)
    record_files(root, [other.path], at=at)
    p = other.path  # not its declared version (the type change of test_file_not_its_declared_version_refused)
    tbl, h = read_family(p)
    i = tbl.schema.get_field_index("created_at")
    ms = pa.timestamp("ms", tz="UTC")
    bad = tbl.set_column(i, pa.field("created_at", ms, False), tbl.column(i).cast(ms))
    h["schema_digest"] = schema_digest(bad.schema)
    p.unlink()
    pq.write_table(bad.replace_schema_metadata({f"{HEADER_PREFIX}{k}".encode(): v.encode() for k, v in h.items()}), p)
    rec = find_run_record(root, h["run_id"])
    assert rec is not None
    o = next(o for o in rec.outputs.values() if o["file_id"] == h["file_id"])
    o["file_digest"], o["schema_digest"] = file_digest(p), h["schema_digest"]
    rewrite_record(root, rec)
    before = _listing(root)
    up = ("migrate", "upcast", "--store", str(root), "--family", "documents")

    r = _cli(*up, "--dry-run")  # default: every series; the unreadable one blocks the plan
    assert r.exit_code == 1 and p.name in r.output, r.output
    r = _cli(*up, "--in", "data/documents/main/", "--dry-run")
    assert r.exit_code == 0, r.output
    (dry,) = [json.loads(ln) for ln in r.output.splitlines()]
    assert dry["chain_id"] == "data/documents/main" and not dry["current"]
    for args, msg in (
        (("--in", "data/documents/nope/"), "no published documents file there"),
        (("--in", "data/sources.arxiv/main/"), "not a documents directory"),
        (("--in", "data/documents/"), "without a series"),
    ):
        r = _cli(*up, *args, "--dry-run")
        assert r.exit_code == 1 and msg in r.output, (args, r.output)
    r = _cli("migrate", "upcast", "--store", str(root), "--family", "screen_evidence", "--file", ev,
             "--in", "data/screen_evidence/main/")
    assert r.exit_code == 1 and "no --in" in r.output, r.output
    assert _listing(root) == before

    r = _cli(*up, "--in", "data/documents/main/", "--in", "data/documents/main")  # a repeat counts once
    assert r.exit_code == 0, r.output
    (out,) = [json.loads(ln) for ln in r.output.splitlines()]
    assert (out["status"], out["stage"]) == ("ok", "fold.compact")
    assert resolve(root, sdir("documents")).version_chain == (out["file_id"],)
    assert [q.name for q in (root / sdir("documents", "other")).glob("*.parquet")] == [p.name]  # untouched
    assert file_digest(p) == o["file_digest"]


# ---------------------------------------------------------------- the evidence prior (slow: builds)
@pytest.mark.slow
def test_upcast_evidence_is_accepted_as_prior(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from stack.build import run_build
    from stack.migrate import upcast_file
    from tests.synth.world import build_world
    from tests.test_build_overrides import STAND_INS, runs_by_stage, synth_plan

    monkeypatch.setenv(WORKFLOWS_ROOT_ENV, str(STAND_INS))
    root = tmp_path / "store"
    build_world(root)
    b1 = run_build(root, synth_plan("synth-a"))
    assert b1.status == "ok", b1.data.get("error")
    ev = b1.files["screen_evidence"]["file_id"]
    downgrade(root, "screen_evidence", 1)
    # an older version reads upcast: the next build's annotate.screen pins it as prior as it is, the
    # head of the line's prior directory data/screen_evidence/main/ (D295; was MixedSchemaError)
    old = run_build(root, synth_plan("synth-b"))
    assert old.status == "ok", old.data.get("error")
    by_old = runs_by_stage(root, old.data)
    assert ev in by_old["annotate.screen"].prior and by_old["annotate.screen"].counts["prior_evidence"] > 0
    # the head evidence file at an older version again, then upcast: the upcast file is the new head
    ev2 = by_old["annotate.screen"].outputs["screen_evidence"]["file_id"]
    downgrade(root, "screen_evidence", 1, only=lambda p: read_header(p)["file_id"] == ev2)
    rec = upcast_file(root, "screen_evidence", ev2)
    assert rec.status == "ok", rec.error
    new = rec.outputs["screen_evidence"]["file_id"]
    b2 = run_build(root, synth_plan("synth-b"))
    by = runs_by_stage(root, b2.data)
    assert by["annotate.screen"].status == "ok"
    assert new in by["annotate.screen"].prior and by["annotate.screen"].counts["prior_evidence"] > 0
    # Every stage ran and the build verifies: V8 exempts the migrate.upcast evidence file from the
    # same-run pairing (its digests still recompute).
    assert b2.status == "ok", b2.data.get("error")


# ---------------------------------------------------------------- re-key (track U, url-normalisation.md §3)
@pytest.mark.slow
def test_rekey_predicts_merges_and_splits_and_gather_agrees(tmp_path: Path) -> None:
    """Sources keyed by v3's URL rule (www. kept, every query dropped): a www./bare pair of one page
    in two channels stays apart, two OpenReview papers share one collapsed key. ``stack migrate
    rekey``'s dry run predicts one merge and one split; ``--apply`` writes one part per changed
    chain in one run; the next gather makes exactly the predicted documents and the store checks
    stay green."""
    from stack.migrate import apply_rekey, plan_rekey, rekey_report
    from tests.synth.rows import sid
    from tests.test_gather_identity import Store, src

    def url_src(i: int, channel: str, canonical: str, old_key: str) -> dict[str, Any]:
        row = src(i, [("url", old_key)], channel=channel)
        row["identity"]["canonical_url"] = canonical
        return row

    store = Store(tmp_path / "store")
    store.add("sources.blogs", [
        url_src(1, "openai_blog", "https://www.example.org/post", "https://www.example.org/post"),
        url_src(3, "openai_blog", "https://openreview.net/forum?id=PaperA", "https://openreview.net/forum"),
        url_src(4, "openai_blog", "https://openreview.net/forum?id=PaperB", "https://openreview.net/forum"),
    ])
    store.add("sources.manual", [url_src(2, "manual", "http://example.org/post/?utm_source=x", "https://example.org/post")])
    store.add("sources.arxiv_sweep", [src(5, [("doi", "10.48550/arxiv.2310.01405"), ("arxiv", "2310.01405")])])
    store.gather()
    before = {d: sorted(m["source_id"] for m in r["identity"]["members"]) for d, r in store.docs().items()}
    assert before == {sid(1): [sid(1)], sid(2): [sid(2)], sid(3): [sid(3), sid(4)], sid(5): [sid(5)]}

    plan = plan_rekey(store.root)
    rep = rekey_report(plan)
    assert rep["baseline"]["merges"] == rep["baseline"]["splits"] == []   # the harness reproduces the head
    # the www. row and the collapsed pair change keys; the bare-host row only its fetch form
    assert rep["rows_rekeyed_per_channel"] == {"manual": 1, "openai_blog": 3}
    (merge,) = rep["rekeyed"]["merges"]
    assert (merge["doc_id"], merge["from"], merge["joined_by"]) == (
        sid(1), [sid(1), sid(2)], ["url=https://example.org/post"])
    (split,) = rep["rekeyed"]["splits"]
    assert (split["doc_id"], split["into"], split["head_keys"]) == (
        sid(3), [sid(3), sid(4)], ["url=https://openreview.net/forum"])
    assert split["part_keys"] == {sid(3): ["url=https://openreview.net/forum?id=PaperA"],
                                  sid(4): ["url=https://openreview.net/forum?id=PaperB"]}

    recs = apply_rekey(plan)  # one run and one part file per changed chain
    assert [(r.status, r.error, r.counts) for r in recs] == [("ok", None, {"rows": 3}), ("ok", None, {"rows": 1})]
    for r in recs:
        h = read_header(store.root / r.outputs["sources"]["path"])
        was = next(ch for ch in plan.chains.values() if ch.head == r.inputs["sources"])  # the head it pinned
        assert (h["kind"], h["stage"], h["base"], h["after"]) == ("part", "migrate.rekey", was.head_name, was.head_name)
        assert json.loads(h["inputs"]) == {"sources": was.head}
    assert read_view(resolve(store.root, sdir("sources.manual"))).to_pylist()[0]["identity"]["canonical_url"] == \
        "http://example.org/post/"
    gathered = store.gather()
    after = {d: sorted(m["source_id"] for m in r["identity"]["members"])
             for d, r in store.docs().items() if r["state"] != "retired"}
    assert after == rep["predicted_docs"]
    # gather counts the merge (sid(2) retired into sid(1)); the split-off sid(4) is a new document
    # while sid(3) continues, which gather's `split` count (tombstones only) does not count
    assert (gathered.counts["merged"], gathered.counts["retired"], gathered.counts["split"]) == (1, 1, 0)
    # a restated row is not a new observation (gather counts distinct observed_at)
    assert {m["observation_count"] for r in store.docs().values() for m in r["identity"]["members"]} == {1}
    bad = {c.name: [f.message for f in c.findings] for c in run_checks(store.root) if c.status == "failed"}
    assert not bad, bad
    # a second dry run finds nothing to change
    assert rekey_report(plan_rekey(store.root))["rows_rekeyed"] == 0


@pytest.mark.slow
def test_sources_compaction_keeps_the_observation_under_a_miss(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """D264: an old-schema ``sources`` chain (one version behind a future additive version; since D690 no
    older sources version reads) whose newest rows for ``sid(1)`` and ``sid(2)`` are
    non-retracting misses over earlier observations (``sid(3)``: misses only). The upcast writes a
    compaction holding the observed rows of 1 and 2 and the miss of 3, then a ``migrate.restate``
    part with the two misses: the view is the old view (run ids of the restated rows aside), the
    next gather changes nothing (D262 speaks from the kept observations), the checks pass, and V12
    is red when the restated rows do not match."""
    from stack.migrate import RESTATE_STAGE, upcast_chain
    from tests.synth.rows import sid
    from tests.test_gather_identity import Store, src

    store = Store(tmp_path / "store")
    cid = "sources.arxiv_sweep"
    store.add(cid, [src(1, [("arxiv", "a1")], t=1), src(2, [("arxiv", "a1")], t=2)])
    store.gather()
    miss = {"state": "missing", "record_format": "miss", "status": "empty"}
    store.add(cid, [src(1, [], t=10, **miss), src(2, [], t=10, **miss), src(3, [("arxiv", "a3")], t=11, **miss)])
    store.gather()
    docs = store.docs()
    future_version(monkeypatch, "sources")
    old_view = {r["source_id"]: r for r in read_view(resolve(store.root, sdir(cid))).to_pylist()}

    rec = upcast_chain(store.root, f"data/{cid}/main")  # the chain is its series directory
    assert (rec.status, rec.stage) == ("ok", "fold.compact"), rec.error
    assert rec.counts["observed_under_misses"] == 2
    chain = resolve(store.root, sdir(cid))
    assert len(chain.files) == 2
    full, part = (read_header(f.path) for f in chain.files)
    assert (full["fold_config"], part["kind"], part["stage"]) == ("compact", "part", RESTATE_STAGE)
    assert part["base"] == chain.files[0].name
    compacted = {r["source_id"]: r["state"] for r in read_family(chain.files[0].path)[0].to_pylist()}
    assert compacted == {sid(1): "observed", sid(2): "observed", sid(3): "missing"}
    view = {r["source_id"]: r for r in read_view(chain).to_pylist()}
    assert {s: {**r, "run_id": None} for s, r in view.items()} == {
        s: {**r, "run_id": None} for s, r in old_view.items()}
    assert view[sid(3)] == old_view[sid(3)] and view[sid(1)]["run_id"] == part["run_id"]
    assert store.gather().counts["changed"] == 0 and store.docs() == docs
    bad = {c.name: [f.message for f in c.findings] for c in run_checks(store.root) if c.status == "failed"}
    assert not bad, bad

    # V12: the restate part must hold exactly the replaced misses
    p = chain.files[1].path
    t = pq.read_table(p)
    rows = [r for r in t.to_pylist() if r["source_id"] == sid(1)]
    p.unlink()
    pq.write_table(pa.Table.from_pylist(rows, schema=t.schema).replace_schema_metadata(t.schema.metadata), p)
    (finding,) = v_compaction_equal(StoreView(store.root))
    assert "restated rows differ" in " ".join(finding.items)
