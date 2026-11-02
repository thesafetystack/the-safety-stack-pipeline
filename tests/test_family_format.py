"""Family file-format versions, upcast at read time (D295; ``stack/schemas/upcasters.py``,
``stack.store.parquet.read_family``/``file_format``, the chain reader, verify).

Two kinds of fixture:

* a **test family** ``fmt_test`` registered for the test (``FAMILIES`` and the version table
  monkeypatched) with three versions: v1 → v2 ``ADDITIVE`` (a nullable top-level column ``extra``
  and a nullable ``info.b`` inside a struct), v2 → v3 a function (``n: int64`` renamed ``count``
  and turned into a string). Files are written by hand at v1 (a legacy file: ``schema_version
  "1"``, no ``family_version``, mapped by digest), v2 and v3;
* the **real legacy formats** of ``screen_evidence`` and ``triage_evidence`` (:data:`ADDED_SINCE`): the
  current schema minus the fields added since a version must render to that version's registered digest,
  and a file written under it (legacy header) reads at current through ``read_family``.
  :func:`downgrade` rewrites a store's files to such a version; ``tests/test_migrate.py`` and
  ``tests/test_checks_scale.py`` use it. ``documents``/``corpus`` before v6 and ``sources`` before v4 (D690)
  do not read: every store was upcast once on disk (D691) and D693 removed the tool, the old types and the
  older steps, so those versions are tested by their footer only (:func:`test_files_before_the_blob_refs_rename_are_refused_by_name`);
  :func:`future_version` registers a next version of a real family, so a store's current files are an older
  version (read upcast, or refused).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import pytest

from stack.checks.chain import SAME, UPCAST, StoreView
from stack.checks.verify import run_checks
from stack.runs import file_digest, find_run_record
from stack.schemas import upcasters
from stack.schemas.families import (
    FAMILIES,
    FamilySpec,
    family,
    schema_digest,
    versioned,
)
from stack.schemas.upcasters import (
    ADDITIVE,
    HOWTO,
    VERSIONS,
    Incompatible,
    Step,
    StepRefused,
    Version,
)
from stack.store.chain import FileFormatError, head_of, read_view, resolve
from stack.store.layout import (
    DATA,
    HEADER_PREFIX,
    StoreLayout,
    make_data_file,
    parse_data_key,
    series_dir,
)
from stack.store.parquet import (
    IncompatibleFormat,
    NewerFormat,
    SchemaMismatch,
    SchemaRefused,
    UnknownFormat,
    build_header,
    file_format,
    read_family,
    read_header,
    write_family,
)
from tests.fixtures.checks import build_store
from tests.synth.records import SERIES, record_files, rewrite_record, sdir

pytestmark = pytest.mark.checks  # area marker(s), not a tier (pyproject markers; docs/SETUP.md §7)

Row = dict[str, Any]
FAM = "fmt_test"
T0 = dt.datetime(2026, 10, 13, 12, 0, tzinfo=dt.UTC)


# ---------------------------------------------------------------- the real legacy formats
#: Per (family, legacy version): the paths the current schema has gained since (stack/schemas/
#: upcasters.py's notes). Every later step of these is ``ADDITIVE``.
ADDED_SINCE: dict[tuple[str, int], list[str]] = {
    ("screen_evidence", 1): ["stack_relevance", "sr_relevance", "text_usability", "doc_class"],
    ("screen_evidence", 2): ["doc_class"],
    ("triage_evidence", 1): ["basis"],
}
#: The families whose versions before the blob-reference rename (D690) do not read: the last version
#: that did not record blob sizes.
BEFORE_BLOB_REFS: dict[str, int] = {"documents": 5, "corpus": 5, "sources": 3}


def _split(path: str) -> list[str]:
    return [p for p in path.replace("[]", ".[]").split(".") if p]


def _drop_type(t: pa.DataType, path: list[str]) -> pa.DataType:
    head, rest = path[0], path[1:]
    if head == "[]":
        return pa.list_(pa.field(t.value_field.name, _drop_type(t.value_type, rest), t.value_field.nullable))
    fields = []
    for f in t:
        if f.name != head:
            fields.append(f)
        elif rest:
            fields.append(pa.field(f.name, _drop_type(f.type, rest), f.nullable))
    return pa.struct(fields)


def drop_schema(schema: pa.Schema, paths: Iterable[str]) -> pa.Schema:
    """``schema`` without ``paths`` (``a.b[].c``)."""
    t = pa.struct(list(schema))
    for p in paths:
        t = _drop_type(t, _split(p))
    return pa.schema(list(t))


def _drop_value(v: Any, path: list[str]) -> None:
    if v is None:
        return
    head, rest = path[0], path[1:]
    if head == "[]":
        for x in v:
            _drop_value(x, rest)
    elif rest:
        _drop_value(v.get(head), rest)
    else:
        v.pop(head, None)


def legacy_schema(fam: str, version: int) -> pa.Schema:
    """The schema of ``fam`` at legacy ``version``, reconstructed from the current one."""
    return drop_schema(family(fam).schema, ADDED_SINCE[(fam, version)])


def legacy_header(header: dict[str, str], schema: pa.Schema) -> dict[str, str]:
    """``header`` as a pre-D295 writer wrote it: ``schema_version "1"``, no ``family_version``."""
    h = {k: v for k, v in header.items() if k != "family_version"}
    return {**h, "schema_version": "1", "schema_digest": schema_digest(schema)}


def write_raw(path: Path, table: pa.Table, header: dict[str, str]) -> Path:
    """Write ``table`` with ``header`` bypassing the family writer (an old or a bad file)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = {f"{HEADER_PREFIX}{k}".encode(): v.encode() for k, v in header.items()}
    pq.write_table(table.replace_schema_metadata(meta), path, compression="zstd")
    return path


#: The field :func:`future_version` adds (a nullable top-level column).
FUTURE_FIELD = pa.field("zz_future", pa.string())


def future_version(monkeypatch: pytest.MonkeyPatch, *fams: str, step: Step = ADDITIVE) -> None:
    """Register for each of ``fams`` a next version that only adds :data:`FUTURE_FIELD`, its ``step`` from the
    current one (default ``ADDITIVE``; a function, or ``Incompatible``), so the store's current files are an
    older version that reads upcast (or is refused). Since D690 no older documents/corpus/sources version
    reads, so a chain's upcast on read is exercised this way."""
    import dataclasses

    for fam in fams:
        spec = family(fam)
        schema = pa.schema([*spec.schema, FUTURE_FIELD])
        h = upcasters.history(fam)
        new = Version(h[-1].number + 1, schema_digest(schema), "test: a future version", step)
        monkeypatch.setitem(VERSIONS, fam, (*h, new))
        monkeypatch.setitem(FAMILIES, fam, versioned(dataclasses.replace(spec, schema=schema)))


def downgrade(root: Path, fam: str, version: int, *, only: Callable[[Path], bool] | None = None,
              row_fn: Callable[[Row], None] | None = None) -> list[str]:
    """Rewrite the ``data/`` files of ``fam`` (``only`` those it accepts) as a pre-D295 writer wrote
    them at legacy ``version`` (fields added since dropped, legacy header), their run records'
    ``file_digest`` following; asserts the reconstruction renders to the registered digest. ``row_fn``
    edits each legacy row in place. Returns the file ids in run order."""
    old_schema = legacy_schema(fam, version)
    reg = upcasters.version(fam, version)
    assert reg is not None and schema_digest(old_schema) == reg.digest, (fam, version)
    out = []
    files = [p for p in (root / DATA).rglob("*.parquet")
             if parse_data_key(p).family == fam and (only is None or only(p))]
    for p in sorted(files, key=lambda q: parse_data_key(q).run_id):  # run order = chain order here
        tbl, h = read_family(p)
        rows = tbl.to_pylist()
        for r in rows:
            for path in ADDED_SINCE[(fam, version)]:
                _drop_value(r, _split(path))
            if row_fn is not None:
                row_fn(r)
        old = pa.Table.from_pylist(rows, schema=old_schema)
        h = legacy_header(h, old_schema)
        before = file_digest(p)
        p.unlink()
        write_raw(p, old, h)
        rec = find_run_record(root, h["run_id"])
        assert rec is not None
        rec.outputs[fam] = {k: v for k, v in rec.outputs[fam].items() if k != "family_version"} | {
            "file_digest": file_digest(p), "schema_digest": h["schema_digest"]}
        rewrite_record(root, rec)
        _repin(root, p.relative_to(root).as_posix(), before, file_digest(p))
        out.append(h["file_id"])
    return out


def _repin(root: Path, key: str, old: str, new: str) -> None:
    """Every other record entry that pins ``key`` at ``old`` (a later run's ``in_files``, a build record's
    ``files``) pins it at ``new``, as the store held the file when they were written."""
    from stack.runs import write_json_zst
    from stack.store.index import read_json_zst

    def walk(v: Any) -> bool:
        hit = False
        if isinstance(v, dict):
            if v.get("path") == key and v.get("file_digest") == old:
                v["file_digest"], hit = new, True
            for x in v.values():
                hit = walk(x) or hit
        elif isinstance(v, list):
            for x in v:
                hit = walk(x) or hit
        return hit

    for rec in [*root.glob("builds/*/build.json.zst"), *root.glob("builds/*/runs/*.json.zst"), *root.glob("runs/*.json.zst")]:
        value = read_json_zst(rec)
        if walk(value):
            write_json_zst(rec, value)


def with_nulls(row: Row, paths: Iterable[str]) -> Row:
    """``row`` with each of ``paths`` set to None where its parent exists."""
    for path in paths:
        def put(v: Any, rest: list[str]) -> None:
            if v is None:
                return
            if rest[0] == "[]":
                for x in v:
                    put(x, rest[1:])
            elif len(rest) == 1:
                v[rest[0]] = None
            else:
                put(v.get(rest[0]), rest[1:])
        put(row, _split(path))
    return row


@pytest.mark.parametrize(("fam", "version"), sorted(ADDED_SINCE))
def test_legacy_digests_reconstruct_and_upcast_additively(fam: str, version: int) -> None:
    """Each legacy version is the current schema minus the fields added since (its registered digest),
    and every step after it is additive by ``schema_delta`` (the table says ADDITIVE)."""
    old = legacy_schema(fam, version)
    reg = upcasters.version(fam, version)
    assert reg is not None and schema_digest(old) == reg.digest
    assert all(v.step is ADDITIVE for v in upcasters.path(fam, version, family(fam).version)), fam
    delta = upcasters.schema_delta(old, family(fam).schema)
    assert delta.problems == ()
    assert sorted(delta.added) == sorted(ADDED_SINCE[(fam, version)])


#: Legacy versions the check-fixture store (``tests.fixtures.checks.build_store``) has no file of: their
#: digest reconstruction is tested, their file read is the generic additive conform's.
NO_FIXTURE_FILES = {("triage_evidence", 1)}


@pytest.mark.parametrize(("fam", "version"), sorted(set(ADDED_SINCE) - NO_FIXTURE_FILES))
def test_legacy_files_read_at_current(tmp_path: Path, fam: str, version: int) -> None:
    """A file written under each real legacy format (legacy header) reads at current through
    ``read_family`` (whole and projected), the added fields NULL."""
    root = tmp_path / "store"
    build_store(root)
    added = ADDED_SINCE[(fam, version)]
    want = {p: read_family(p)[0] for p in (root / DATA).rglob("*.parquet") if parse_data_key(p).family == fam}
    assert want
    downgrade(root, fam, version)
    key = list(family(fam).key)
    for p, cur in want.items():
        name = p.name
        fmt = file_format(p, read_header(p))
        assert (fmt.version, fmt.legacy, fmt.is_current, fmt.additive) == (version, True, False, True)
        got, h = read_family(p)
        assert h["schema_version"] == "1" and "family_version" not in h  # the header as written
        assert got.schema.equals(family(fam).schema)
        assert got.to_pylist() == [with_nulls(r, added) for r in cur.to_pylist()], name
        top = _split(added[0])[0]
        assert read_family(p, [*key, top])[0].equals(got.select([*key, top]))


@pytest.mark.parametrize("legacy", [False, True], ids=["family_version", "legacy-header"])
@pytest.mark.parametrize("fam", sorted(BEFORE_BLOB_REFS))
def test_files_before_the_blob_refs_rename_are_refused_by_name(fam: str, legacy: bool) -> None:
    """D690/D693: a documents/corpus file before v6 or a sources file before v4 (its footer: a declared
    version, or a legacy header mapped by digest) is ``IncompatibleFormat`` naming the decisions and the
    removed tool, never an ``UnknownFormat`` digest; every version entry is kept and every path from one
    crosses an ``Incompatible`` step (the older function steps are retired: nothing reads such a file).
    Red under: an entry deleted, a retired step left a function, or the reason not naming the tool."""
    cur = family(fam).version
    blob = BEFORE_BLOB_REFS[fam] + 1  # the D690 version; later versions may follow it (additive ones read v{blob})
    assert cur >= blob
    assert [v.number for v in upcasters.history(fam)] == list(range(1, cur + 1))
    assert not [v for v in upcasters.history(fam) if callable(v.step) and not isinstance(v.step, Incompatible)]
    for n in range(1, blob):
        v = upcasters.version(fam, n)
        assert v is not None
        header = {"family": fam, "schema_version": "1", "schema_digest": v.digest} if legacy else \
            {"family": fam, "schema_version": "2", "family_version": str(n), "schema_digest": v.digest}
        with pytest.raises(IncompatibleFormat) as exc:
            file_format(f"old-{fam}-v{n}.parquet", header, digest=v.digest)
        msg = str(exc.value)
        assert f"old-{fam}-v{n}.parquet: {fam} v{n} does not read at v{cur}" in msg, msg
        assert "D690" in msg and "D693" in msg and "tools/upcast_blob_refs.py" in msg, msg
    assert upcasters.version(fam, blob).step == Incompatible(upcasters.BLOB_REFS_REASON)  # type: ignore[union-attr]


def _refuse_first_documents_file(monkeypatch: pytest.MonkeyPatch, root: Path) -> Path:
    """Register a next ``documents`` version (:func:`future_version`) whose function step refuses the store's
    first documents file with doc_ids no other file has in that order (:class:`StepRefused`: a guard that
    would discard values, D648; a step sees the table only) and reads every other one; that file."""
    ids_of = {p: read_family(p, ["doc_id"])[0].column("doc_id").to_pylist()
              for p in (root / DATA).rglob("*.parquet") if parse_data_key(p).family == "documents"}
    first = min(p for p, ids in ids_of.items() if sum(x == ids for x in ids_of.values()) == 1)
    ids = ids_of[first]

    def guard(t: pa.Table) -> pa.Table:
        if t.column("doc_id").to_pylist() == ids:
            raise StepRefused("test: annotations.x is non-null in 1 row(s): refusing to drop it")
        return t

    future_version(monkeypatch, "documents", step=guard)
    return first


@pytest.mark.slow
def test_verify_reports_a_refused_file_as_its_s1_finding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """D648: ``stack verify`` over a store holding a documents file a step's guard refuses: S1 names the file
    and the refusal (``UpcastRefused``), every other check reads the file as unreadable; no check is a
    ``check_error``. Red under: the refusal raised as a plain ``UpcastBug`` (every file check and the store
    checks reading the file end ``check_error``)."""
    root = tmp_path / "store"
    build_store(root)
    first = _refuse_first_documents_file(monkeypatch, root)
    res = {r.check: r for r in run_checks(root)}
    assert not [n for n, r in res.items() if r.error_class == "check_error"], \
        {n: r.items for n, r in res.items() if r.error_class == "check_error"}
    red = {n: r for n, r in res.items() if r.status == "failed"}
    assert set(red) == {"schema_equal"}, set(red)
    findings = red["schema_equal"].findings  # the refusal, and the schema that is not the registry's
    assert findings and all(first.name in f.message for f in findings)
    assert any("UpcastRefused" in f.message and "annotations.x is non-null" in f.message for f in findings)


@pytest.mark.slow
def test_a_refused_file_with_s1_deselected_reports_nothing_and_errs_nowhere(tmp_path: Path,
                                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    """Parity (review-2 N4): with ``schema_equal`` not selected, the refused file gives no finding and no
    ``check_error`` in any other check (the file checks return early, the store checks read it as
    unreadable), exactly as a file S1 refuses for a schema mismatch; S1 is the check that names it."""
    from stack.checks.verify import CHECK_TABLE

    root = tmp_path / "store"
    build_store(root)
    _refuse_first_documents_file(monkeypatch, root)
    names = [n for _, n, _ in CHECK_TABLE if n != "schema_equal"]
    res = {r.check: r for r in run_checks(root, names=names)}
    assert set(res) == set(names)
    assert {n: (r.status, r.error_class) for n, r in res.items() if r.status not in ("ok", "skipped")} == {}


@pytest.mark.slow
def test_a_refused_file_in_the_head_view_is_s1_and_no_check_errs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every documents file refused by a step's guard, so the chain's head view holds one: S1 names each, and
    the checks that read a resolved chain's view (X4, X20: the resolver reads footers only) skip it instead of
    ending ``check_error``. Red under: X4/X20 calling ``read_view`` on the resolved chain unguarded."""
    root = tmp_path / "store"
    build_store(root)
    docs = {p.name for p in (root / DATA).rglob("*.parquet") if parse_data_key(p).family == "documents"}

    def guard(t: pa.Table) -> pa.Table:
        raise StepRefused("test: refused")

    future_version(monkeypatch, "documents", step=guard)
    res = {r.check: r for r in run_checks(root)}
    assert not [n for n, r in res.items() if r.error_class == "check_error"], \
        {n: r.items for n, r in res.items() if r.error_class == "check_error"}
    named = {n for n in docs if any(n in f.message and "UpcastRefused" in f.message for f in res["schema_equal"].findings)}
    assert named == docs


@pytest.mark.slow
def test_verify_reports_every_incompatible_documents_file_as_s1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A store whose documents files are all behind an ``Incompatible`` step (as a mirror's old local copies of
    pre-D690 files are): S1 names each as ``IncompatibleFormat`` with the step's reason; no check is a
    ``check_error``."""
    root = tmp_path / "store"
    build_store(root)
    docs = {p.name for p in (root / DATA).rglob("*.parquet") if parse_data_key(p).family == "documents"}
    future_version(monkeypatch, "documents", step=Incompatible("D999: test; re-run gather"))
    res = {r.check: r for r in run_checks(root)}
    assert not [n for n, r in res.items() if r.error_class == "check_error"], \
        {n: r.items for n, r in res.items() if r.error_class == "check_error"}
    s1 = res["schema_equal"]
    assert s1.status == "failed"
    named = {n for n in docs if any(n in f.message and "IncompatibleFormat" in f.message and "D999: test" in f.message
                                    for f in s1.findings)}
    assert named == docs


# ---------------------------------------------------------------- the test family
INFO1 = pa.struct([pa.field("a", pa.string())])
INFO2 = pa.struct([pa.field("a", pa.string()), pa.field("b", pa.string())])
S1 = pa.schema([pa.field("item_id", pa.string(), False), pa.field("n", pa.int64()), pa.field("info", INFO1)])
S2 = pa.schema([pa.field("item_id", pa.string(), False), pa.field("n", pa.int64()), pa.field("info", INFO2),
                pa.field("extra", pa.string())])
S3 = pa.schema([pa.field("item_id", pa.string(), False), pa.field("count", pa.string()), pa.field("info", INFO2),
                pa.field("extra", pa.string())])


def n_to_count(t: pa.Table) -> pa.Table:
    """v2 → v3: ``n`` (int64) renamed ``count`` and rendered as a decimal string."""
    i = t.schema.get_field_index("n")
    return t.set_column(i, pa.field("count", pa.string()), pc.cast(t.column("n"), pa.string()))


def _versions(third: Any = n_to_count) -> tuple[Version, ...]:
    return (Version(1, schema_digest(S1), "the first format"),
            Version(2, schema_digest(S2), "extra, info.b", ADDITIVE),
            Version(3, schema_digest(S3), "n renamed count, a string", third))


def register(monkeypatch: pytest.MonkeyPatch, schema: pa.Schema, versions: tuple[Version, ...]) -> FamilySpec:
    monkeypatch.setitem(VERSIONS, FAM, versions)
    spec = versioned(FamilySpec(name=FAM, kind="coverage", schema=schema, key=("item_id",)))
    monkeypatch.setitem(FAMILIES, FAM, spec)
    return spec


@pytest.fixture
def fmt_family(monkeypatch: pytest.MonkeyPatch) -> FamilySpec:
    return register(monkeypatch, S3, _versions())


def rid(i: int) -> str:
    return f"r{i:012d}"


#: The test family's directory (a chain of the series ``main``: ``data/fmt_test.c/main/``).
FAM_DIR = f"{FAM}.c"


def store_of(p: Path) -> Path:
    """The store root of a ``write_at`` file (``<root>/data/fmt_test.c/main/<name>``)."""
    return p.parents[3]


def write_at(root: Path, version: int, rows: list[Row], run: int, *, kind: str = "full", base: str = "",
             schema: pa.Schema | None = None, **over: str) -> Path:
    """A ``fmt_test`` file of ``data/fmt_test.c/main/`` at ``version`` (1: a legacy file) written by
    hand, after the directory's last file; ``base``: the base file's name; ``over`` overrides
    header keys."""
    schema = schema or {1: S1, 2: S2, 3: S3}[version]
    tbl = pa.Table.from_pylist(rows, schema=schema)
    run_id = rid(run)
    after = head_of(root, series_dir(FAM_DIR, SERIES)) if (root / DATA).exists() else ""
    h = build_header(tbl, FAM, stage="collect.test", writer="collect.test", run_id=run_id, config_digest="c" * 64,
                     finished_at=T0 + dt.timedelta(minutes=run), code_digest="d" * 64, kind=kind,
                     chain_id=FAM_DIR, series=SERIES, base=base, after=after)
    h["schema_digest"] = schema_digest(schema)
    if version == 1:
        h = legacy_header(h, schema)
    else:
        h["family_version"] = str(version)
    h.update(over)
    dest = make_data_file(FAM_DIR, SERIES, T0 + dt.timedelta(minutes=run), run_id, kind)
    path = write_raw(StoreLayout(root).ensure().data_file(dest), tbl, h)
    record_files(root, [path], at=T0 + dt.timedelta(minutes=run))  # chains resolve from records (D345)
    return path


V1_ROWS = [{"item_id": "a", "n": 1, "info": {"a": "x"}}, {"item_id": "b", "n": None, "info": None}]
V2_ROWS = [{"item_id": "b", "n": 20, "info": {"a": "y", "b": "z"}, "extra": "e"}]
V3_ROWS = [{"item_id": "c", "count": "three", "info": {"a": None, "b": "w"}, "extra": None}]
AT_V3 = {
    1: [{"item_id": "a", "count": "1", "info": {"a": "x", "b": None}, "extra": None},
        {"item_id": "b", "count": None, "info": None, "extra": None}],
    2: [{"item_id": "b", "count": "20", "info": {"a": "y", "b": "z"}, "extra": "e"}],
    3: V3_ROWS,
}


def test_read_family_gives_the_current_version_for_each(tmp_path: Path, fmt_family: FamilySpec) -> None:
    assert fmt_family.version == 3
    paths = {1: write_at(tmp_path, 1, V1_ROWS, 1), 2: write_at(tmp_path, 2, V2_ROWS, 2),
             3: write_at(tmp_path, 3, V3_ROWS, 3)}
    for v, p in paths.items():
        fmt = file_format(p, read_header(p))
        assert (fmt.version, fmt.legacy, fmt.is_current) == (v, v == 1, v == 3)
        assert [s.kind for s in fmt.steps] == {1: ["additive", "function:n_to_count"], 2: ["function:n_to_count"],
                                              3: []}[v]
        tbl, h = read_family(p)
        assert tbl.schema.equals(S3) and tbl.to_pylist() == AT_V3[v], v
        assert h.get("family_version") == (None if v == 1 else str(v))  # the header as written
        # projected reads on every path (a function step: read whole, upcast, select)
        proj = read_family(p, ["count", "item_id"])[0]
        assert proj.column_names == ["count", "item_id"]
        assert proj.to_pylist() == [{"count": r["count"], "item_id": r["item_id"]} for r in AT_V3[v]]
    assert fmt_family.version == 3 and read_family(paths[3])[0].equals(pq.read_table(paths[3]).replace_schema_metadata(None))


def test_additive_only_projection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With v2 current (only an ADDITIVE step from v1), a projected read of a v1 file reads the
    projection and conforms it: a column the file lacks comes back NULL."""
    register(monkeypatch, S2, _versions()[:2])
    p = write_at(tmp_path, 1, V1_ROWS, 1)
    assert file_format(p, read_header(p)).additive
    got = read_family(p, ["item_id", "extra", "info"])[0]
    assert got.schema.equals(pa.schema([S2.field("item_id"), S2.field("extra"), S2.field("info")]))
    assert got.to_pylist() == [{"item_id": "a", "extra": None, "info": {"a": "x", "b": None}},
                               {"item_id": "b", "extra": None, "info": None}]


def test_a_chain_of_three_versions_reads_through_read_view(tmp_path: Path, fmt_family: FamilySpec) -> None:
    f1 = write_at(tmp_path, 1, V1_ROWS, 1)
    f2 = write_at(tmp_path, 2, V2_ROWS, 2, kind="part", base=f1.name)
    write_at(tmp_path, 3, V3_ROWS, 3, kind="part", base=f2.name)
    ch = resolve(tmp_path, sdir(FAM_DIR))
    assert ch.versions == (1, 2, 3)
    view = read_view(ch)
    assert view.schema.equals(S3)
    assert view.to_pylist() == [AT_V3[1][0], AT_V3[2][0], AT_V3[3][0]]  # b from v2 wins over v1
    assert read_view(ch, columns=["count"], keys=["b", "c"]).to_pylist() == [
        {"item_id": "b", "count": "20"}, {"item_id": "c", "count": "three"}]
    view_store = StoreView(tmp_path)
    assert [view_store.schema_status(df) for df in view_store.files] == [UPCAST, UPCAST, SAME]
    head = view_store.files[-1].file_id
    assert view_store.view_at(head) is not None and view_store.view_at(head).equals(view)  # type: ignore[union-attr]


def _refusals(tmp_path: Path) -> dict[str, tuple[Path, type[SchemaMismatch], str]]:
    """One file per refusal, each a part on a readable v3 full file of its own chain."""
    out = {}
    cases: list[tuple[str, int, dict[str, str], type[SchemaMismatch], str]] = [
        ("newer", 3, {"family_version": "4"}, NewerFormat, "written by newer code"),
        ("schema_version", 3, {"schema_version": "3"}, NewerFormat, "schema_version '3'"),
        ("not_declared", 3, {"family_version": "2"}, SchemaMismatch, "is not v2's"),
        ("unregistered_number", 3, {"family_version": "0"}, UnknownFormat, "v0 is not a registered version"),
    ]
    for i, (name, version, over, exc, msg) in enumerate(cases):
        base = write_at(tmp_path / name, 3, V3_ROWS, 10 * i + 1)
        rows = V1_ROWS if version == 1 else [{**V3_ROWS[0], "item_id": "d"}]
        p = write_at(tmp_path / name, version, rows, 10 * i + 2, kind="part", base=base.name, **over)
        out[name] = (p, exc, msg)
    # a legacy file whose (honest) digest no version has
    base = write_at(tmp_path / "unknown", 3, V3_ROWS, 91)
    odd = pa.schema([*S1, pa.field("odd", pa.string())])
    p = write_at(tmp_path / "unknown", 1, [{**V1_ROWS[0], "odd": "o"}], 92, kind="part", base=base.name,
                 schema=odd)
    out["unknown"] = (p, UnknownFormat, "is no registered version")
    return out


def test_refusals_through_read_family_resolve_and_verify(tmp_path: Path, fmt_family: FamilySpec) -> None:
    for name, (p, exc, msg) in _refusals(tmp_path).items():
        with pytest.raises(SchemaMismatch) as got:
            read_family(p)
        assert type(got.value) is exc and msg in str(got.value) and p.name in str(got.value), (name, got.value)
        assert HOWTO in str(got.value)
        with pytest.raises(FileFormatError) as chain_exc:
            resolve(store_of(p), sdir(FAM_DIR))
        assert type(chain_exc.value.error) is exc and p.name in str(chain_exc.value), name
        view = StoreView(store_of(p))
        assert [view.schema_status(df) for df in view.files] == [SAME, None], name
        (s1,) = run_checks(store_of(p), names=["schema_equal"])
        assert s1.status == "failed" and any(p.name in f.message and exc.__name__ in f.message for f in s1.findings), \
            (name, s1.findings)


def test_incompatible_step_refuses_with_its_reason(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reason = "D999: info split into two columns; re-run collect.test (or `stack migrate upcast`)"
    register(monkeypatch, S3, _versions(Incompatible(reason)))
    base = write_at(tmp_path, 3, V3_ROWS, 1)
    for v, run in ((1, 2), (2, 3)):
        p = write_at(tmp_path / f"v{v}", v, V1_ROWS if v == 1 else V2_ROWS, run)
        with pytest.raises(IncompatibleFormat, match="D999: info split") as exc:
            read_family(p)
        assert "v2 → v3 is incompatible" in str(exc.value)
    p = write_at(tmp_path, 2, V2_ROWS, 4, kind="part", base=base.name)
    with pytest.raises(FileFormatError, match="re-run collect.test") as cexc:
        resolve(tmp_path, sdir(FAM_DIR))
    assert isinstance(cexc.value.error, IncompatibleFormat)
    (s1,) = run_checks(tmp_path, names=["schema_equal"])
    assert any(p.name in f.message and "IncompatibleFormat" in f.message for f in s1.findings)


@pytest.mark.parametrize("fam", ["model_roles", "model_roles_evidence"])
def test_model_roles_files_before_v3_are_refused_with_d591(fam: str) -> None:
    """D591: model_roles v1/v2 files (scratch stores only) are not read: the v3 step is ``Incompatible``
    and names the remedy; the generic refusal of such files is the test family's
    (:func:`test_incompatible_step_refuses_with_its_reason`). Red under: an ADDITIVE v3 (matches/ambiguous
    left the payload, so it is not additive)."""
    (v3,) = [v for v in upcasters.path(fam, 2, family(fam).version) if v.number == 3]
    assert isinstance(v3.step, Incompatible) and "D591" in v3.step.reason
    assert "re-run annotate.model_roles" in v3.step.reason
    assert [v.number for v in upcasters.path(fam, 1, family(fam).version)][:2] == [2, 3]
    with pytest.raises(upcasters.UpcastBug, match="incompatible"):
        upcasters.upcast(family(fam).schema.empty_table(), fam, 2, family(fam).schema, what="t")


@pytest.mark.parametrize(("fam", "old"), [("model_roles_evidence", 3), ("technique_staged_evidence", 1)])
def test_usage_columns_are_an_additive_step(fam: str, old: int) -> None:
    """D714: the usage columns of the technique_staged / model_roles evidence are one ADDITIVE version: an older
    file reads with them NULL. Red under: a non-additive step or other columns in the delta."""
    (step,) = upcasters.path(fam, old, family(fam).version)
    assert step.step is ADDITIVE and "D714" in step.note
    cur = family(fam).schema
    prev = pa.schema([f for f in cur if f.name not in ("tokens_cached", "tokens_cache_write", "tokens_reasoning")])
    delta = upcasters.schema_delta(prev, cur)
    assert delta.problems == () and sorted(delta.added) == ["tokens_cache_write", "tokens_cached", "tokens_reasoning"]
    rows = upcasters.upcast(prev.empty_table(), fam, old, cur, what="t")
    assert rows.schema.equals(cur)


def test_a_bug_in_a_step_is_loud(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A step that does not produce the next version is an ``UpcastBug`` naming file and version,
    never a "this file does not read" (not a ``ValueError``)."""
    register(monkeypatch, S3, _versions(lambda t: t))  # forgets the rename
    p = write_at(tmp_path, 2, V2_ROWS, 1)
    with pytest.raises(upcasters.UpcastBug, match=rf"{p.name} \(fmt_test v2\).*n: not in the target"):
        read_family(p)
    assert not issubclass(upcasters.UpcastBug, ValueError)


# ---------------------------------------------------------------- the registry
def test_registry_every_family_versioned() -> None:
    digests = {n: s.schema_digest for n, s in FAMILIES.items()}
    problems = upcasters.registry_problems(digests)
    assert problems == [], "\n".join(problems)  # the message prints each family's current digest
    for n, s in FAMILIES.items():
        h = upcasters.history(n)
        assert [v.number for v in h] == list(range(1, len(h) + 1)) and s.version == len(h)
        assert h[-1].digest == s.schema_digest and upcasters.registration_problem(n, s.schema_digest) is None
        assert all(v.note for v in h)


def test_a_schema_change_without_a_version_refuses_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    changed = pa.schema([*S3, pa.field("new_col", pa.string())])
    spec = register(monkeypatch, changed, _versions())
    assert spec.version == 3  # the package imports; the version is the newest registered
    problems = upcasters.registry_problems({**{n: s.schema_digest for n, s in FAMILIES.items()}})
    assert any(FAM in p and HOWTO in p for p in problems)
    tbl = pa.Table.from_pylist([{"item_id": "a"}], schema=changed)
    h = build_header(tbl, FAM, stage="collect.test", writer="collect.test", run_id=rid(1), config_digest="c" * 64,
                     finished_at=T0, code_digest="d" * 64, chain_id=FAM_DIR, series=SERIES)
    name = make_data_file(FAM_DIR, SERIES, T0, rid(1), "full").name
    with pytest.raises(SchemaRefused) as exc:
        write_family(tmp_path / name, tbl, h)
    assert HOWTO in str(exc.value) and spec.schema_digest in str(exc.value) and FAM in str(exc.value)
    assert not (tmp_path / name).exists()


def test_writers_write_the_current_version(tmp_path: Path) -> None:
    """Every writer puts ``family_version`` (the spec's) and ``schema_version "2"`` in the header,
    and the run record's output carries the version."""
    root = tmp_path / "store"
    build_store(root)
    for p in sorted((root / DATA).rglob("*.parquet")):
        h = read_header(p)
        assert (h["schema_version"], h["family_version"]) == ("2", str(family(h["family"]).version)), p.name
        rec = find_run_record(root, h["run_id"])
        if rec is not None and h["family"] in rec.outputs:
            assert rec.outputs[h["family"]].get("family_version") in (None, family(h["family"]).version)


def test_header_keys_s7(fmt_family: FamilySpec) -> None:
    """S7: ``family_version`` required on a "2" file and equal to the spec's version; absent on a
    legacy "1" file; another schema_version refused."""
    from stack.checks.schema import header_problems

    tbl = pa.Table.from_pylist(V3_ROWS, schema=S3)
    h = build_header(tbl, FAM, stage="collect.test", writer="collect.test", run_id=rid(1), config_digest="c" * 64,
                     finished_at=T0, code_digest="d" * 64, chain_id=FAM_DIR, series=SERIES)
    assert header_problems(tbl, h, fmt_family) == []
    assert any("missing keys ['family_version']" in b for b in
               header_problems(tbl, {k: v for k, v in h.items() if k != "family_version"}, fmt_family))
    assert header_problems(tbl, legacy_header(h, S3), fmt_family) == []
    assert any("family_version = '2'" in b for b in header_problems(tbl, {**h, "family_version": "2"}, fmt_family))
    assert any("legacy file has none" in b for b in
               header_problems(tbl, {**legacy_header(h, S3), "family_version": "3"}, fmt_family))
    assert any("schema_version = '9'" in b for b in header_problems(tbl, {**h, "schema_version": "9"}, fmt_family))
