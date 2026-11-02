"""Chains as series directories (D372): name order, the walk back with the
``after`` links, missed and excluded files, forks and lost updates, the publish rule
(``head_moved``, ``name_order``), restarts, compaction equality, files without a series, mixed
schema digests and a lost tail found from the run records.

The base store is B2's scenario (the v4 design mock ``v4f-B2-partials``) on the real ``documents`` schema in
``data/documents/main/``: S0 ``gather`` full (3,000 rows), S1 ``triage`` part (every row), S2
``acquire`` part (600 rows), S3 ``acquire`` retry part (the 100 failures: 60 now ok, 40 still
failed). Every file goes through ``stage_file``/``publish`` and gets an ``ok`` final run record
from the publish hook, as the runner writes it.
"""

from __future__ import annotations

import datetime as dt
import os
import random
import shutil
from collections import Counter
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from stack.runs import (
    RunRecord,
    file_digest,
    files_without_ok_record,
    read_run_records,
    records_without_file,
    write_final_record,
)
from stack.schemas.families import family, schema_digest
from stack.store.chain import (
    ChainConsistencyError,
    ChainError,
    ChainFile,
    EmptyChainError,
    FileFormatError,
    ForkError,
    HeaderFields,
    HeadMovedError,
    MissedFileError,
    NameOrderError,
    Published,
    StagedFile,
    head_of,
    orphan,
    publish,
    read_view,
    resolve,
    stage_file,
    write_file,
)
from stack.store.layout import (
    HEADER_PREFIX,
    StoreLayout,
    make_data_file,
    parse_data_key,
    parse_dir_ref,
    tie_guard,
)
from stack.store.parquet import SchemaMismatch, build_header, read_family, write_family
from tests.synth.records import record_files
from tests.synth.rows import dg, document_row, rid, table, valid_file

UTC = dt.UTC
T0 = dt.datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
T1 = dt.datetime(2026, 10, 1, 13, 0, tzinfo=UTC)
T2 = dt.datetime(2026, 10, 1, 13, 30, tzinfo=UTC)
T3 = dt.datetime(2026, 10, 1, 14, 0, tzinfo=UTC)
N = 3000
D = "data/documents/main"
BLOCKS = {"gather": ["identity", "metadata", "inband"], "triage": ["annotations"],
          "acquire": ["content"]}


# ---------------------------------------------------------------- helpers
def fields(stage: str, run: int, **kw: Any) -> HeaderFields:
    if stage in BLOCKS:
        kw.setdefault("blocks_written", BLOCKS[stage])
    return HeaderFields(stage=stage, run_id=rid(run), config_digest=dg(("config", stage)),
                        code_digest=dg("code"), plan="dev-v2", **kw)


def record_hook(store: Path, stage: str, run: int, at: dt.datetime) -> Any:
    """The runner's part of D38: write the final ``ok`` record once the files are published."""

    def hook(published: list[Published]) -> None:
        write_final_record(store, RunRecord(
            run_id=rid(run), stage=stage, writer=stage, plan="dev-v2", status="ok",
            started_at=at, heartbeat_at=at, finished_at=at, code_digest=dg("code"),
            config_digest=dg(("config", stage)), scope_complete=True,
            outputs={p.staged.family: p.as_output() for p in published}))

    return hook


def staged(store: Path, kind: str, rows: list[dict[str, Any]], stage: str, run: int, at: dt.datetime, *,
           comment: str | None = None, after: str | None = None, base: str | None = None,
           d: str = D) -> StagedFile:
    """A staged file in ``d``: ``after`` defaults to the directory's last file, ``base`` to
    ``after`` for a part and to none for a full."""
    after = head_of(store, d) if after is None else after
    base = (after if kind == "part" else "") if base is None else base
    fam_dir, series = parse_dir_ref(d)
    dest = make_data_file(fam_dir, series, at, rid(run), kind, comment=comment)
    return stage_file(store, dest, table("documents", rows), fields(stage, run), base=base, after=after,
                      finished_at=at)


def put(store: Path, kind: str, rows: list[dict[str, Any]], stage: str, run: int, at: dt.datetime,
        comment: str | None = None, **kw: Any) -> Published:
    s = staged(store, kind, rows, stage, run, at, comment=comment, **kw)
    return publish(s, record_hook(store, stage, run, at))[0]


def stamped(row: dict[str, Any], run: int, at: dt.datetime) -> dict[str, Any]:
    row["run_id"], row["created_at"], row["updated_at"] = rid(run), T0, at
    return row


def failed_content(row: dict[str, Any], error: str) -> dict[str, Any]:
    c = row["content"]
    c["snapshots"] = []
    c["roles"][0].update(status="failed_transient", error_class="http_5xx", error=error,
                         last_outcome="failed_transient", snapshot_id=None)
    c.update(current_snapshot_id=None, current_source_id=None, current_role=None,
             current_text_sha256=None, current_text_status="no_raw", current_text_media=None,
             current_revision=None)
    return row


def states(t: pa.Table) -> dict[tuple[str, str | None], int]:
    st = t.column("state").to_pylist()
    ts = [c["current_text_status"] if c else None for c in t.column("content").to_pylist()]
    return dict(Counter(zip(st, ts)))


def naive_select(paths: list[Path]) -> dict[str, dict[str, Any]]:
    """Last writer wins over ``paths`` in the given order (plain Python, no duckdb)."""
    out: dict[str, dict[str, Any]] = {}
    for p in paths:
        for r in pq.read_table(p).to_pylist():
            out[r["doc_id"]] = r
    return out


def stray_file(store: Path, run: int, after: str, rows: list[dict[str, Any]], *,
               extra_column: bool = False, lie_digest: bool = False) -> Path:
    """A part file placed in ``data/documents/main/`` by hand, bypassing the chain writer (and, for
    the schema cases, the family writer too)."""
    tbl = table("documents", rows)
    h = build_header(tbl, "documents", stage="acquire", writer="acquire", run_id=rid(run),
                     config_digest=dg("c"), finished_at=T3, code_digest=dg("code"),
                     kind="part", chain_id="documents", blocks_written=["content"], plan="dev-v2", series="main",
                     comment="stray", base=after, after=after)
    path = StoreLayout(store).data_file(make_data_file("documents", "main", T3 + dt.timedelta(hours=1), rid(run),
                                                       "part", comment="stray"))
    if not (extra_column or lie_digest):
        return write_family(path, tbl, h)
    if extra_column:
        tbl = tbl.append_column(pa.field("extra", pa.string()), pa.array(["x"] * tbl.num_rows))
        h["schema_digest"] = schema_digest(tbl.schema)  # honest about its own schema
    if lie_digest:
        h["schema_digest"] = dg("some other schema")
    meta = {f"{HEADER_PREFIX}{k}".encode(): v.encode() for k, v in h.items()}
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(tbl.replace_schema_metadata(meta), path)
    return path


# ---------------------------------------------------------------- the B2 store
@pytest.fixture(scope="module")
def b2(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    store = tmp_path_factory.mktemp("b2") / "store"
    StoreLayout(store).ensure()
    rng = random.Random(20261001)
    s0 = put(store, "full", [stamped(document_row(i), 1, T0) for i in range(N)], "gather", 1, T0)
    s1 = put(store, "part", [stamped(document_row(i, state="triaged"), 2, T1) for i in range(N)],
             "triage", 2, T1)
    ids = rng.sample(range(N), 600)
    s2_rows = []
    for k, i in enumerate(ids):
        r = stamped(document_row(i, state="acquired"), 3, T2)
        s2_rows.append(r if k < 500 else failed_content(r, "HTTP 503"))
    s2 = put(store, "part", s2_rows, "acquire", 3, T2)
    s3_rows = []
    for k, i in enumerate(ids[500:]):
        r = stamped(document_row(i, state="acquired"), 4, T3)
        s3_rows.append(r if k < 60 else failed_content(r, "HTTP 404"))
    s3 = put(store, "part", s3_rows, "acquire", 4, T3, comment="retry")
    return {"store": store, "S": [s0, s1, s2, s3], "ids": ids}


@pytest.fixture()
def store(b2: dict[str, Any], tmp_path: Path) -> Path:
    dst = tmp_path / "store"
    shutil.copytree(b2["store"], dst)
    return dst


def names(b2: dict[str, Any]) -> list[str]:
    """File names of S0-S3 (the same in every copy of the store)."""
    return [p.path.name for p in b2["S"]]


def path(store: Path, name: str) -> Path:
    return store / D / name


# ---------------------------------------------------------------- (0) names and references
def test_names_and_references() -> None:
    f = make_data_file("sources.af_graphql", "weekly", T0, rid(1), "part", comment="v13")
    assert f.key == f"data/sources.af_graphql/weekly/20261001T120000Z_{rid(1)}_v13.part.parquet"
    assert parse_data_key(f.key) == f and f.family == "sources" and f.file_id == f"{rid(1)}.sources"
    assert parse_data_key(f"/x/store/{f.dir}/.{f.name}.tmp") == f  # a staged name reads as the name
    assert (f.family_dir, f.series, f.comment, f.is_part) == ("sources.af_graphql", "weekly", "v13", True)
    for bad in ("data/x.parquet", "data/documents/w/20261012T0130Z_r1.full.parquet",
                "data/documents/20261012T013000Z_r1.part.parquet", "data/documents/w/x/20261012T013000Z_r1.full.parquet",
                "data/documents/w/20261012T013000Z_r1.gather.documents.parquet"):
        with pytest.raises(ValueError):
            parse_data_key(bad)
    g = make_data_file("documents", None, T0, rid(2), "full", comment="exp-2")
    assert g.key == f"data/documents/20261001T120000Z_{rid(2)}_exp-2.full.parquet"
    with pytest.raises(ValueError, match="always full"):
        make_data_file("documents", None, T0, rid(2), "part")
    for comment in ("Exp_2", "has.dot"):
        with pytest.raises(ValueError, match="comment"):
            make_data_file("documents", "weekly", T0, rid(2), "full", comment=comment)
    with pytest.raises(ValueError, match="series"):
        make_data_file("documents", "-x", T0, rid(2), "full")
    with pytest.raises(ValueError, match="no implicit selection"):
        parse_dir_ref("data/documents/")
    assert parse_dir_ref("data/documents/weekly/") == ("documents", "weekly")
    # the tie guard: a run stamp never sorts at or before the newest input it reads
    assert tie_guard(T0, None) == "20261001T120000Z"
    assert tie_guard(T0, "20261001T115959Z") == "20261001T120000Z"
    assert tie_guard(T0, "20261001T120000Z") == "20261001T120001Z"
    assert tie_guard(T0, "20261001T130000Z") == "20261001T130001Z"


# ---------------------------------------------------------------- (1) name order, exact files
@pytest.mark.slow
def test_order_by_name_and_exact_files(store: Path, b2: dict[str, Any]) -> None:
    s0, s1, s2, s3 = (path(store, n) for n in names(b2))
    assert sorted((store / D).glob("*.parquet")) == [s0, s1, s2, s3]
    chain = resolve(store, D + "/")
    assert list(chain.paths) == [s0, s1, s2, s3] and chain.is_dir_head
    assert [f.kind for f in chain.files] == ["full", "part", "part", "part"]
    assert [f.after for f in chain.files] == ["", s0.name, s1.name, s2.name]
    view = read_view(chain)
    assert view.num_rows == N
    assert view.schema.equals(family("documents").schema)
    assert states(view) == {("acquired", "ok"): 560, ("acquired", "no_raw"): 40,
                            ("triaged", None): 2400}
    by_chain = naive_select(list(chain.paths))
    assert view.to_pylist() == [by_chain[k] for k in sorted(by_chain)]
    # an exact file: that file's version, walking back from it
    at_s1 = resolve(store, f"{D}/{s1.name}")
    assert list(at_s1.paths) == [s0, s1] and not at_s1.is_dir_head and at_s1.last == s3.name
    assert states(read_view(at_s1)) == {("triaged", None): N}
    with pytest.raises(ChainError, match="no implicit selection"):
        resolve(store, "data/documents/")
    with pytest.raises(EmptyChainError):
        resolve(store, "data/documents/other/")


# ---------------------------------------------------------------- (2) missed and excluded files
def test_missed_file(store: Path, b2: dict[str, Any]) -> None:
    _, _, s2, s3 = names(b2)
    # A recorded file that is neither local nor remote (D345): the version cannot be read.
    path(store, s2).unlink()
    with pytest.raises(MissedFileError) as exc:
        resolve(store, D)
    assert s2 in str(exc.value)
    shutil.copy(b2["store"] / D / s2, path(store, s2))
    # A middle file is refused without --force; excluded, the next part's link is cut.
    with pytest.raises(ChainError, match="not the last file"):
        orphan(store, f"{D}/{s2}")
    orphan(store, f"{D}/{s2}", force=True)
    with pytest.raises(MissedFileError) as exc:
        resolve(store, D)
    msg = str(exc.value)
    assert s3 in msg and s2 in msg and "not published" in msg
    print(f"MissedFileError: {msg}")
    # The repair is a new full (a restart: no base, written after the last published file).
    restart = put(store, "full", [stamped(document_row(1), 30, T3)], "gather", 30, T3 + dt.timedelta(hours=1))
    assert resolve(store, D).paths == (restart.path,)


# ---------------------------------------------------------------- (3) forks and lost updates
def test_two_writers(store: Path, b2: dict[str, Any]) -> None:
    last = head_of(store, D)
    assert last == names(b2)[3]
    later = T3 + dt.timedelta(hours=1)
    a = staged(store, "part", [stamped(document_row(1), 10, later)], "acquire", 10, later)
    b = staged(store, "part", [stamped(document_row(2), 11, later)], "acquire", 11, later + dt.timedelta(seconds=5))
    publish(a, record_hook(store, "acquire", 10, later))
    with pytest.raises(HeadMovedError) as exc:
        publish(b, record_hook(store, "acquire", 11, later))
    print(f"HeadMovedError: {exc.value}")
    assert a.name in str(exc.value) and last in str(exc.value)
    assert b.path.exists() and b.path.name == f".{b.name}.tmp" and not b.data_path.exists()
    assert resolve(store, D).head == a.file_id
    # Without the check the second file and its ok record fork the directory: the reader stops
    # with ForkError naming both runs (no locks; the backstop).
    os.rename(b.path, b.data_path)
    record_hook(store, "acquire", 11, later)([Published(b, b.data_path)])
    with pytest.raises(ForkError) as fork:
        resolve(store, D)
    assert rid(10) in str(fork.value) and rid(11) in str(fork.value)
    print(f"ForkError: {fork.value}")
    # the repair: orphan the unwanted branch (the last file: no --force needed)
    orphan(store, f"{D}/{b.name}")
    assert resolve(store, D).head == a.file_id


def test_lost_update_full_and_restart(store: Path, b2: dict[str, Any]) -> None:
    """A full computed from an older head (``after`` = S2 while S3 is published) is refused at
    publish (``head_moved``) and, written past the check, read as a fork; a restart (no base)
    written after the last file is fine and the walk stops at it."""
    _, _, s2, s3 = names(b2)
    later = T3 + dt.timedelta(hours=1)
    stale = staged(store, "full", [stamped(document_row(1), 12, later)], "gather", 12, later,
                   after=s2, base=s2)
    with pytest.raises(HeadMovedError):
        publish(stale)
    os.rename(stale.path, stale.data_path)
    record_hook(store, "gather", 12, later)([Published(stale, stale.data_path)])
    with pytest.raises(ForkError, match=s3):
        resolve(store, D)
    orphan(store, f"{D}/{stale.name}")
    restart = put(store, "full", [stamped(document_row(2), 13, later)], "gather", 13, later)
    got = resolve(store, D)
    assert got.paths == (restart.path,) and restart.staged.base == "" and restart.staged.after == s3


def test_name_order(store: Path, b2: dict[str, Any]) -> None:
    """A file whose name sorts before the directory's last file is refused (``name_order``),
    even when its ``after`` is current."""
    early = staged(store, "part", [stamped(document_row(1), 14, T2)], "acquire", 14, T2 - dt.timedelta(minutes=1))
    with pytest.raises(NameOrderError, match="does not sort after"):
        publish(early)
    assert not early.data_path.exists()


def test_data_file_stamp_and_outputs(store: Path, b2: dict[str, Any]) -> None:
    """A data file carries its run's stamp to the second; the record's ``outputs`` entry carries
    ``series``, ``kind``, ``base``, ``after`` and the family directory as ``chain_id``."""
    last = head_of(store, D)
    start = T3 + dt.timedelta(hours=2, minutes=7, seconds=3)
    s = staged(store, "part", [stamped(document_row(1), 12, T3)], "acquire", 12, start)
    assert s.name.startswith("20261001T160703Z_")
    assert s.path == store / D / f".{s.name}.tmp"  # staged in its directory
    (pub,) = publish(s)
    out = pub.as_output()
    assert (out["chain_id"], out["series"], out["kind"], out["base"], out["after"]) == (
        "documents", "main", "part", last, last)
    assert out["path"] == f"{D}/{s.name}" and not s.path.exists()
    hdr = read_family(pub.path)[1]
    assert (hdr["series"], hdr["kind"], hdr["base"], hdr["after"], hdr["comment"]) == ("main", "part", last, last, "")
    # the base is a name (D372); its file id is derived from it
    assert "base_file_id" not in out and "base_file_id" not in hdr
    assert ChainFile(pub.path, hdr).base_file_id == f"{parse_data_key(f'{D}/{last}').run_id}.documents"


# ---------------------------------------------------------------- (4) compaction equality, no series
def test_compaction_equality(store: Path, b2: dict[str, Any]) -> None:
    chain = resolve(store, D)
    view = read_view(chain)
    at = T3 + dt.timedelta(hours=1)
    dest = make_data_file("documents", "main", at + dt.timedelta(hours=1), rid(21), "full")
    c2 = write_file(store, dest, view, fields("fold.compact", 21, fold_config="compact", annotations_folded=[]),
                    base=chain.head_name, after=chain.head_name,
                    on_published=record_hook(store, "fold.compact", 21, at))
    got, header = read_family(c2.path)
    assert got.equals(view)
    assert header["base"] == chain.head_name and header["kind"] == "full"
    assert Counter(got.column("run_id").to_pylist()) == {rid(2): 2400, rid(3): 500, rid(4): 100}
    after = resolve(store, D)
    assert after.version_chain == (c2.file_id,)
    assert read_view(after).equals(view)
    s4 = put(store, "part", [stamped(document_row(5, state="acquired"), 22, at)], "acquire", 22,
             at + dt.timedelta(hours=2))
    assert resolve(store, D).version_chain == (c2.file_id, s4.file_id)


def test_file_without_series(store: Path) -> None:
    tbl, _ = valid_file("corpus")
    dest = make_data_file("corpus", None, T3, rid(23), "full", comment="exp-2")
    corpus = write_file(store, dest, tbl, fields("fold.corpus", 23, fold_config="corpus", filter="has_text",
                                                   annotations_folded=["triage"]),
                        on_published=record_hook(store, "fold.corpus", 23, T3))
    hc = read_family(corpus.path)[1]
    assert (hc["chain_id"], hc["series"], hc["base"], hc["kind"]) == ("corpus", "", "", "full")

    assert resolve(store, dest.key).version_chain == (corpus.file_id,)
    with pytest.raises(ChainError, match="no implicit selection"):
        resolve(store, "data/corpus")


# ---------------------------------------------------------------- (5) a version file not its declared format (C1)
def test_file_not_its_declared_format_refused(store: Path, b2: dict[str, Any]) -> None:
    """C1 under D295: a chain may mix format versions, but each file's digest must be its declared
    version's; a file with an extra column declares the current version with another digest."""
    last = names(b2)[3]
    p = stray_file(store, 41, last, [stamped(document_row(9), 41, T3)], extra_column=True)
    record_files(store, [p], at=T3)
    with pytest.raises(FileFormatError) as exc:
        resolve(store, D)
    print(f"FileFormatError: {exc.value}")
    assert p.name in str(exc.value) and "is not v" in str(exc.value)
    assert type(exc.value.error) is SchemaMismatch
    orphan(store, f"{D}/{p.name}")
    q = stray_file(store, 42, last, [stamped(document_row(9), 42, T3)], lie_digest=True)
    record_files(store, [q], at=T3)
    with pytest.raises(ChainConsistencyError, match="schema_digest"):
        resolve(store, D)


# ---------------------------------------------------------------- (6) lost tail from run records
def test_lost_tail_from_run_records(store: Path, b2: dict[str, Any]) -> None:
    assert not (store / "builds").exists() or not any((store / "builds").iterdir())
    assert len(read_run_records(store)) == 4
    assert records_without_file(store) == [] and files_without_ok_record(store) == []
    s3 = b2["S"][3]
    path(store, s3.path.name).unlink()
    # The records still say S3 is the last file (D345): its version does not read, X1 names it.
    assert head_of(store, D) == s3.path.name
    with pytest.raises(MissedFileError, match=s3.path.name):
        resolve(store, D)
    lost = records_without_file(store)
    assert [(x.run_id, x.file_id) for x in lost] == [(rid(4), s3.file_id)]
    stray = stray_file(store, 43, b2["S"][2].path.name, [stamped(document_row(11), 43, T3)])
    assert files_without_ok_record(store) == [stray]
    write_final_record(store, RunRecord(
        run_id=rid(43), stage="acquire", writer="acquire", plan="dev-v2", status="failed",
        error_class="code_bug", started_at=T3, heartbeat_at=T3, finished_at=T3,
        code_digest=dg("code"), config_digest=dg("c"),
        outputs={"documents": {"file_id": f"{rid(43)}.documents", "path": f"{D}/{stray.name}",
                               "file_digest": file_digest(stray), "row_count": 1,
                               "schema_digest": family("documents").schema_digest}}))
    assert files_without_ok_record(store) == [stray]
    with pytest.raises(ChainError, match="nothing to take out"):
        orphan(store, f"{D}/{stray.name}")
