"""Projected reads (field paths into blocks) and the two verify checks built on them: X19
``state_monotone`` over :meth:`StoreView.base_join` and X7/X8's narrowed corpus read
(``stack/checks/annotate.py`` ``CORPUS_FIELDS``).

The reference implementations below are the row-wise code these replaced (X19 over whole
``identity``/``metadata`` rows, X7/X8 over whole corpus rows), kept verbatim so that every
verdict can be compared, item for item, on the fixture stores and on planted faults.
"""

from __future__ import annotations

import copy
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import pytest

from stack.checks import Finding
from stack.checks import annotate as ann_checks
from stack.checks import triage as triage_checks
from stack.checks.chain import BATCH_ROWS, DataFile, StoreView, base_gap
from stack.checks.triage import _finding
from stack.states import is_forward
from stack.store.layout import HEADER_PREFIX
from stack.store.chain import read_view, resolve
from stack.store.parquet import field_paths, project, project_schema, read_family, read_family_fields
from tests.fixtures.checks import FIXTURES, World, _stamp, build_store, clean_world, read_only, write_world
from tests.fixtures.checks.annotate import _annotate
from tests.synth.records import sdir
from tests.test_family_format import future_version

pytestmark = pytest.mark.checks  # area marker(s), not a tier (pyproject markers; docs/SETUP.md §7)

X19_PATHS = ["state", "identity.members_digest", "metadata.fields_digest"]
DEEP = ["doc_id", "identity.members.source_id", "identity.members.is_live", "content.roles.attempts",
        "content.current_text_sha256", "metadata.title"]


# ---------------------------------------------------------------- reference: the row-wise code
def ref_base_pairs(view: StoreView, df: DataFile, columns: list[str]
                   ) -> Iterator[tuple[str, dict[str, Any], dict[str, Any]]]:
    """``StoreView.base_pairs`` as it was: whole columns, rows matched in Python."""
    cols = ["doc_id", *(c for c in columns if c != "doc_id")]
    tbl = view.read(df, cols)
    if tbl is None or tbl.num_rows == 0:
        return
    ids = tbl.column("doc_id").to_pylist()
    base = view.base_view(df, cols, keys=tbl.column("doc_id").unique())
    if base is None:
        return
    pos = {d: i for i, d in enumerate(base.column("doc_id").to_pylist())}
    for start in range(0, tbl.num_rows, BATCH_ROWS):
        at = [i for i, d in enumerate(ids[start:start + BATCH_ROWS]) if d in pos]
        if not at:
            continue
        cur = tbl.slice(start, BATCH_ROWS).take(at).to_pylist()
        prev = base.take([pos[ids[start + i]] for i in at]).to_pylist()
        for r, p in zip(cur, prev, strict=True):
            yield r["doc_id"], r, p


def ref_state_monotone(view: StoreView) -> list[Finding]:
    """X19 as it was (whole ``identity`` and ``metadata`` rows in Python)."""
    bad: list[str] = []
    for df in view.pair_files("X19"):
        if view.readable(df) and not view.has_base(df):
            base_gap(view, df, bad)
        stage = df.header.get("stage")
        for d, cur, prev in ref_base_pairs(view, df, ["state", "identity", "metadata"]):
            a, b = prev["state"], cur["state"]
            changed = (cur["identity"]["members_digest"] != prev["identity"]["members_digest"]
                       or cur["metadata"]["fields_digest"] != prev["metadata"]["fields_digest"])
            try:
                ok = is_forward(a, b, digest_changed=changed, separate=stage == "gather")
            except KeyError:
                ok = False
            if ok and b == "retired" and a != b and stage != "gather":
                ok = False
            if not ok:
                bad.append(f"{df.file_id}: {d} {a} -> {b} ({stage})")
    return _finding("state_monotone", "{n} state move(s) not allowed", bad)


def ref_joined_corpus(view: StoreView, df: DataFile, corpus_id: str, mine: list[str], spec: Any,
                      cache: dict[Any, Any]) -> dict[str, dict[str, Any]] | None:
    """``_joined_corpus`` as it was: the whole corpus file, every column, per run."""
    from stack.annotate.inputs import header_reads, join_reads

    c_df = view.file(corpus_id)
    tbl = view.table(c_df) if c_df is not None else None
    if tbl is None:
        mine.append(f"{df.key}: corpus input {corpus_id} not readable under data/")
        return None
    reads = {}
    for name, fid in header_reads(ann_checks._inputs(df)).items():
        r_df = view.file(fid)
        r_tbl = view.table(r_df) if r_df is not None else None
        if r_tbl is None:
            mine.append(f"{df.key}: read input {name}={fid} not readable under data/")
            return None
        reads[name] = r_tbl
    return {r["doc_id"]: r for r in join_reads(tbl, reads).to_pylist()}


def py_project(value: Any, tree: dict[str, Any] | None) -> Any:
    """Narrow a Python row value by a projection tree (the reference for ``project``)."""
    if tree is None or value is None:
        return value
    if isinstance(value, list):
        return [py_project(v, tree) for v in value]
    return {k: py_project(v, tree[k]) for k, v in value.items() if k in tree}


def tree_of(paths: list[str]) -> dict[str, Any]:
    tree: dict[str, Any] = {}
    for p in paths:
        node = tree
        parts = p.split(".")
        for i, name in enumerate(parts):
            if i == len(parts) - 1:
                node[name] = None
            else:
                node = node.setdefault(name, {})
    return tree


# ---------------------------------------------------------------- the projection primitive
def _nested() -> pa.Table:
    item = pa.struct([pa.field("x", pa.int32(), False), pa.field("y", pa.string())])
    t = pa.struct([pa.field("a", pa.string(), False), pa.field("b", pa.list_(pa.field("element", item))),
                   pa.field("c", pa.struct([pa.field("d", pa.string()), pa.field("e", pa.int64())]))])
    rows = [{"a": "1", "b": [{"x": 1, "y": "p"}, {"x": 2, "y": None}], "c": {"d": "q", "e": 5}},
            None,
            {"a": "3", "b": None, "c": None},
            {"a": "4", "b": [], "c": {"d": None, "e": None}},
            {"a": "5", "b": [{"x": 7, "y": "z"}], "c": {"d": "w", "e": 9}}]
    arr = pa.array(rows, t)
    return pa.table({"k": pa.array(["k0", "k1", "k2", "k3", "k4"]), "s": arr})


def test_field_paths_are_validated_and_canonical() -> None:
    schema = _nested().schema
    assert field_paths(schema, ["s.c.e", "k", "s.b.x", "s.a"]) == ("k", "s.a", "s.b.x", "s.c.e")
    assert field_paths(schema, ["s.b.x", "s"]) == ("s",)  # a whole field absorbs its children
    assert field_paths(schema, ["s", "s.b.x", "k", "k"]) == ("k", "s")
    for bad in ("nope", "s.nope", "s.b.nope", "k.x", "s..a", "s.a.z"):
        with pytest.raises(ValueError):
            field_paths(schema, [bad])


@pytest.mark.parametrize("paths", [["s.a"], ["s.b.y"], ["s.b.x", "s.c.d"], ["k", "s.c"], ["s"]])
@pytest.mark.parametrize("sliced", [False, True])
def test_project_keeps_values_and_nulls_at_every_level(paths: list[str], sliced: bool) -> None:
    tbl = _nested()
    if sliced:  # a slice and several chunks
        tbl = pa.concat_tables([tbl.slice(1, 2), tbl.slice(3, 2)])
    got = project(tbl, paths)
    got.validate(full=True)
    assert got.schema == project_schema(tbl.schema, paths)
    tree = tree_of(list(field_paths(tbl.schema, paths)))
    want = [{k: py_project(v, tree[k]) for k, v in r.items() if k in tree} for r in tbl.to_pylist()]
    assert got.to_pylist() == want
    # the list keeps its element field (name, nullability)
    if "s.b.y" in paths:
        assert got.schema.field("s").type.field("b").type.value_field.name == "element"


def test_project_on_a_whole_documents_view_is_the_python_narrowing(tmp_path: Path) -> None:
    root = tmp_path / "store"
    build_store(root)
    full = read_view(resolve(root, sdir("documents")))
    got = project(full, DEEP)
    tree = tree_of(list(field_paths(full.schema, DEEP)))
    assert got.to_pylist() == [{k: py_project(v, tree[k]) for k, v in r.items() if k in tree} for r in full.to_pylist()]
    assert got.nbytes < full.nbytes / 4


@pytest.mark.parametrize("older", [False, True])
@pytest.mark.slow
def test_read_family_fields_is_the_projected_upcast(tmp_path: Path, older: bool,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "store"
    build_store(root)
    if older:  # the files one version behind (D690: no older documents version reads)
        future_version(monkeypatch, "documents", "corpus")
    view = StoreView(root)
    n = 0
    for df in view.files:
        if df.family not in ("documents", "corpus"):
            continue
        full = read_family(df.path)[0]
        got, header = read_family_fields(df.path, DEEP + ["annotations.triage.status"])
        assert got.equals(project(full, DEEP + ["annotations.triage.status"])), df.path.name
        assert header == read_family(df.path)[1]
        plain, _ = read_family_fields(df.path, ["state", "doc_id"])
        assert plain.equals(full.select(["doc_id", "state"]))  # plain names: the reader's own, registry order
        n += 1
    assert n >= 4


def test_read_view_with_paths_is_the_projected_view(tmp_path: Path) -> None:
    root = tmp_path / "store"
    build_store(root)
    ch = resolve(root, sdir("documents"))
    full = read_view(ch)
    some = full.column("doc_id").to_pylist()[::2]
    got = read_view(ch, columns=X19_PATHS, keys=some)
    exp = project(full.filter(pc.is_in(full.column("doc_id"), value_set=pa.array(some))), ["doc_id", *X19_PATHS])
    assert got.equals(exp)
    assert read_view(ch, columns=["identity.members.source_id"]).equals(
        project(full, ["doc_id", "identity.members.source_id"]))
    with pytest.raises(ValueError, match="no column"):
        read_view(ch, columns=["identity.nope"])


@pytest.mark.parametrize("columns", [X19_PATHS, ["created_at", "identity", "content"], DEEP])
@pytest.mark.slow
def test_base_join_is_the_row_wise_pairing(tmp_path: Path, columns: list[str]) -> None:
    root = tmp_path / "store"
    build_store(root)
    view = StoreView(root)
    seen = 0
    for df in view.pair_files("X17"):
        want = list(ref_base_pairs(view, df, [c.split(".")[0] for c in columns]))
        tree = tree_of(list(field_paths(df.spec.schema, ["doc_id", *columns])))  # type: ignore[union-attr]
        want = [(d, py_project(c, tree), py_project(p, tree)) for d, c, p in want]
        assert list(view.base_pairs(df, columns)) == want, df.file_id
        seen += len(want)
    assert seen > 5


# ---------------------------------------------------------------- X19
def _x19_world(edit: str) -> World:
    """The clean world with one planted move in the F5 gather part (doc 0, ``acquired`` in F3)."""
    w = clean_world()
    if edit == "clean":
        return w
    row = _stamp(copy.deepcopy(w.doc("F3", 0)), 5)
    if edit == "back":  # acquired -> triaged (not allowed)
        row["state"] = "triaged"
    elif edit == "reset_same":  # acquired -> identified with no digest change (not allowed)
        row["state"] = "identified"
    elif edit == "reset_changed":  # acquired -> identified after a members change (allowed in gather)
        row["state"] = "identified"
        row["identity"]["members_digest"] = "0" * 64
    else:
        raise ValueError(edit)
    w.rows("F5").append(row)
    return w


@pytest.mark.parametrize("edit", [pytest.param(e, marks=pytest.mark.slow) if e != "back" else e
                                  for e in ("clean", "back", "reset_same", "reset_changed")])
def test_x19_equals_the_row_wise_check(tmp_path: Path, edit: str) -> None:
    write_world(_x19_world(edit), tmp_path)
    view = StoreView(tmp_path)
    want = ref_state_monotone(view)
    view.drop_cache()
    got = triage_checks.state_monotone(view)
    assert got == want
    assert bool(got) == (edit in ("back", "reset_same"))  # the planted fault is seen


@pytest.mark.slow
def test_x19_on_its_fixture_and_on_every_fixture_store_equals_the_row_wise_check(tmp_path: Path) -> None:
    for i, name in enumerate(["state_monotone", "full_covers_base", "created_preserved", "V-compaction-equal"]):
        root = tmp_path / str(i)
        build_store(root, name)
        view = StoreView(root)
        assert triage_checks.state_monotone(view) == ref_state_monotone(view), name
    assert "state_monotone" in FIXTURES


@pytest.mark.slow
def test_x19_reads_only_its_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "store"
    build_store(root, "state_monotone")
    asked: list[tuple[str, ...] | None] = []
    real = StoreView.base_join

    def spy(self: StoreView, df: DataFile, fields: Any = None) -> Any:
        asked.append(None if fields is None else tuple(fields))
        return real(self, df, fields)

    monkeypatch.setattr(StoreView, "base_join", spy)
    assert triage_checks.state_monotone(StoreView(root))
    assert asked and set(asked) == {tuple(X19_PATHS)}


# ---------------------------------------------------------------- X7 / X8
def _annotate_results(root: Path, monkeypatch: pytest.MonkeyPatch, reference: bool) -> tuple[Any, ...]:
    with monkeypatch.context() as m:
        if reference:
            m.setattr(ann_checks, "_joined_corpus", ref_joined_corpus)
        view = StoreView(root)
        out = []
        for fn in (ann_checks.policy_pins, ann_checks.basis_currency, ann_checks.selection_recompute):
            out.append(fn(view))
            view.drop_cache()
    return tuple(out)


def annotated_store(root: Path) -> None:
    """The clean store with the annotated pair (a corpus, a ``screen`` file and its evidence in
    the build): X6-X8 have a run to check, and it is green."""
    w = clean_world()
    _annotate(w)
    write_world(w, root)


def _corpus_text_moved(root: Path) -> None:
    """Plant a basis fault: the corpus's first row's current text digest changed (X7 and X8 red)."""
    view = StoreView(root)
    screen = next(f for f in view.files if f.family == "screen" and "corpus" in ann_checks._inputs(f))
    df = view.file(ann_checks._inputs(screen)["corpus"])
    assert df is not None
    tbl, header = read_family(df.path)
    rows = tbl.to_pylist()
    rows[0]["content"]["current_text_sha256"] = "f" * 64
    new = pa.Table.from_pylist(rows, schema=tbl.schema)
    df.path.unlink()
    pq.write_table(new.replace_schema_metadata({f"{HEADER_PREFIX}{k}".encode(): v.encode() for k, v in header.items()}),
                   df.path)


@pytest.mark.parametrize("fixture", [f if f == "basis_currency" else pytest.param(f, marks=pytest.mark.slow)
                                     for f in (None, "basis_currency", "selection_recompute", "policy_pins", "corpus_moved")])
def test_x6_x7_x8_equal_the_whole_row_checks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                              fixture: str | None) -> None:
    root = tmp_path / "store"
    if fixture in (None, "corpus_moved"):
        annotated_store(root)
    else:
        build_store(root, fixture)
    if fixture == "corpus_moved":
        _corpus_text_moved(root)
    want = _annotate_results(root, monkeypatch, reference=True)
    got = _annotate_results(root, monkeypatch, reference=False)
    assert got == want
    red = {i for i, fs in enumerate(got) if fs}
    assert red == {None: set(), "policy_pins": {0}, "basis_currency": {1}, "selection_recompute": {2},
                   "corpus_moved": {1, 2}}[fixture]


@pytest.mark.slow
def test_the_corpus_is_read_narrowed_once_per_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "store"
    annotated_store(root)
    reads: list[tuple[str, tuple[str, ...] | None]] = []
    real = StoreView.read

    def spy(self: StoreView, df: DataFile, columns: Any = None) -> Any:
        if df.family == "corpus":
            reads.append((df.file_id, None if columns is None else tuple(columns)))
        return real(self, df, columns)

    monkeypatch.setattr(StoreView, "read", spy)
    view = StoreView(root)
    it, _ = ann_checks._annotate_runs(view)
    runs = list(it)
    assert runs and all(r.corpus for r in runs)
    assert reads == [(reads[0][0], ann_checks.CORPUS_FIELDS)]  # one narrowed read, shared
    reads.clear()
    assert all(r.corpus is None for r in ann_checks._annotate_runs(view, corpus=False)[0])
    assert reads == []  # X6 reads no corpus


class _Tracked(dict):  # type: ignore[type-arg]
    """A row (or block) that records every key read from it, ``.get`` included, as a dotted path."""

    def __init__(self, data: dict[str, Any], seen: set[str], prefix: str = "") -> None:
        super().__init__({k: _track(v, seen, f"{prefix}{k}.") for k, v in data.items()})
        self._seen, self._prefix = seen, prefix

    def __getitem__(self, key: str) -> Any:
        self._seen.add(self._prefix + key)
        return super().__getitem__(key)

    def get(self, key: str, default: Any = None) -> Any:
        self._seen.add(self._prefix + key)
        return super().get(key, default)


def _track(v: Any, seen: set[str], prefix: str) -> Any:
    if isinstance(v, dict):
        return _Tracked(v, seen, prefix)
    if isinstance(v, list):
        return [_track(x, seen, prefix) for x in v]
    return v


@pytest.mark.parametrize("kind", [pytest.param("text", marks=pytest.mark.slow), "fields",
                                  pytest.param("chained", marks=pytest.mark.slow)])
def test_corpus_fields_cover_what_select_and_basis_read(tmp_path: Path, kind: str,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    """Every corpus field ``select`` (basis, live members, ``is_basis_current``) reads is in
    ``CORPUS_FIELDS``; a missed one would read as NULL (``.get``) on a narrowed row, not raise.
    ``annotations`` is read (and passed along) for every kind, used only by ``chained``, for which
    ``_joined_corpus`` reads the whole block."""
    import dataclasses
    import datetime as dt

    from stack.annotate.select import select
    from stack.schemas.payloads.registry import REGISTRY

    root = tmp_path / "store"
    annotated_store(root)
    view = StoreView(root)
    screen = next(f for f in view.files if f.family == "screen" and "corpus" in ann_checks._inputs(f))
    corpus = view.table(view.file(ann_checks._inputs(screen)["corpus"]))  # type: ignore[arg-type]
    assert corpus is not None and corpus.num_rows
    spec = REGISTRY["screen"]
    if kind == "fields":
        spec = dataclasses.replace(spec, basis_kind="fields")
    elif kind == "chained":
        spec = dataclasses.replace(spec, basis_kind="chained", reads=("doc_class",))
        monkeypatch.setitem(REGISTRY, "screen", spec)
    seen: set[str] = set()
    rows = [_Tracked(r, seen) for r in corpus.to_pylist()]
    select(spec, rows, [], [], config_digest="c", run_id="r", now=dt.datetime(2026, 1, 1, tzinfo=dt.UTC))
    allowed = set(ann_checks.CORPUS_FIELDS) | {"annotations"}
    assert seen, "nothing tracked: the test cannot fail"
    # a block on the way to a projected field is read too (``row["identity"]``)
    outside = {p for p in seen if not any(p == a or p.startswith(a + ".") or a.startswith(p + ".") for a in allowed)}
    assert outside == set()
    # members are a list: their fields are read per element, under the list's path
    assert {"identity.members.source_id", "identity.members.is_live"} <= seen
    if kind == "fields":
        assert {"metadata.title", "metadata.fields_digest"} <= seen


# ---------------------------------------------------------------- X4, X13, X17, X24 (siblings)
def ref_cardinalities(view: StoreView) -> list[Finding]:
    """X4 as it was: whole ``identity`` rows in Python."""
    from collections import Counter, defaultdict

    from stack.checks.blocks import _finding as bfinding
    from stack.store import chain as store_chain

    bad: list[str] = []
    corpora = []
    for df in view.files:
        spec = df.spec
        if spec is None or spec.kind not in ("documents", "corpus"):
            continue
        tbl = view.read(df, ["doc_id"])
        if tbl is None:
            continue
        dup = [d for d, c in Counter(tbl.column("doc_id").to_pylist()).items() if c > 1]
        bad += [f"{df.file_id}: doc_id {d} more than once" for d in dup]
        if spec.kind == "corpus":
            corpora.append(df)
    cols = ["doc_id", "state", "identity"]
    views: list[Any] = [(df.file_id, lambda df=df: view.read(df, cols)) for df in corpora]
    for chain_id, files in view.chains().items():
        if files[0].spec is None or files[0].spec.kind != "documents":
            continue
        res = view.resolve(chain_id)
        if isinstance(res, store_chain.ChainError):
            continue
        views.append((chain_id, lambda res=res: store_chain.read_view(res, columns=cols)))
    for name, read in views:
        tbl = read()
        live: dict[str, set[str]] = defaultdict(set)
        for r in tbl.select(cols).to_pylist():
            if r["state"] == "retired":
                continue
            for m in r["identity"]["members"] or []:
                if m["is_live"]:
                    live[m["source_id"]].add(r["doc_id"])
        bad += [f"{name}: live source {s} in {len(ds)} live documents {sorted(ds)}"
                for s, ds in sorted(live.items()) if len(ds) > 1]
    return bfinding("cardinalities", "{n} cardinality violation(s)", bad)


def ref_created_preserved(view: StoreView) -> list[Finding]:
    """X17 as it was: four whole blocks per pair in Python."""
    from stack.checks.blocks import _STAMPS, STAMPED_BLOCKS, _unstamped
    from stack.checks.blocks import _finding as bfinding

    bad: list[str] = []
    for df in view.pair_files("X17"):
        if view.readable(df) and not view.has_base(df):
            base_gap(view, df, bad)
        for d, cur, prev in ref_base_pairs(view, df, ["created_at", "updated_at", *STAMPED_BLOCKS]):
            if cur["created_at"] != prev["created_at"]:
                bad.append(f"{df.file_id}: {d} created_at {prev['created_at']} -> {cur['created_at']}")
            if cur["updated_at"] < prev["updated_at"]:
                bad.append(f"{df.file_id}: {d} updated_at goes back")
            for blk in STAMPED_BLOCKS:
                a, b = prev[blk], cur[blk]
                if a is None or b is None or _unstamped(a) != _unstamped(b):
                    continue
                if any(a[s] != b[s] for s in _STAMPS):
                    bad.append(f"{df.file_id}: {d} untouched {blk} restamped {a['run_id']} -> {b['run_id']}")
    return bfinding("created_preserved", "{n} stamp violation(s)", bad)


def ref_attempts_monotone(view: StoreView) -> list[Finding]:
    """X24 as it was: whole ``content`` per pair."""
    from stack.checks.blocks import _finding as bfinding

    bad: list[str] = []
    for df in view.pair_files("X24"):
        if view.readable(df) and not view.has_base(df):
            base_gap(view, df, bad)
        for d, cur, prev in ref_base_pairs(view, df, ["content"]):
            a, b = prev["content"], cur["content"]
            if a is None or b is None:
                continue
            if a["convert_pin"] == b["convert_pin"] and a["members_digest"] == b["members_digest"]:
                before = {(r["source_id"], r["content_role"]): r for r in a["roles"]}
                for r in b["roles"]:
                    p = before.get((r["source_id"], r["content_role"]))
                    if p is None or "ok" in (p["status"], r["status"]):
                        continue
                    if r["attempts"] < p["attempts"]:
                        bad.append(f"{df.file_id}: {d} role {r['content_role']}/{r['source_id']} "
                                   f"attempts {p['attempts']} -> {r['attempts']}")
            snaps = {s["snapshot_id"]: s for s in a["snapshots"]}
            for s in b["snapshots"]:
                p = snaps.get(s["snapshot_id"])
                if p is None or "ok" in (p["text_status"], s["text_status"]):
                    continue
                if p["convert_pin"] != s["convert_pin"]:
                    continue
                if s["convert_attempts"] < p["convert_attempts"]:
                    bad.append(f"{df.file_id}: {d} snapshot {s['snapshot_id']} convert_attempts "
                               f"{p['convert_attempts']} -> {s['convert_attempts']}")
    return bfinding("attempts_monotone", "{n} attempt counter(s) decreased", bad)


def ref_gate_obeyed(view: StoreView) -> list[Finding]:
    """X13 as it was: whole ``content`` and ``annotations``."""
    from stack import gate

    bad: list[str] = []
    for df in view.pair_files("X13"):
        if df.header.get("stage") != "acquire" or not view.readable(df):
            continue
        if not view.has_base(df):
            base_gap(view, df, bad)
            continue
        tbl = view.read(df, ["doc_id", "content", "annotations"])
        base = view.base_view(df, ["doc_id", "content"], keys=tbl.column("doc_id").unique())  # type: ignore[union-attr]
        if base is None:
            base_gap(view, df, bad)
            continue
        pos = {d: i for i, d in enumerate(base.column("doc_id").to_pylist())}
        for r in tbl.to_pylist():  # type: ignore[union-attr]
            prev = base.slice(pos[r["doc_id"]], 1).to_pylist()[0] if r["doc_id"] in pos else None
            roles = triage_checks._attempted_roles(r, prev)
            if roles and not triage_checks._fetchable(r):
                t = gate.triage_of(r)
                what = "no triage" if t is None else (
                    f"{(t['payload'] or {}).get('decision')} {t['status']}[{t['error_class']}]")
                bad.append(f"{df.file_id}: {r['doc_id']} fetched {roles[:2]} under {what}")
    return _finding("gate_obeyed", "{n} fetch(es) outside the gate", bad)


def ref_block_ownership(view: StoreView) -> list[Finding]:
    """X15 as it was: every block outside ``blocks_written`` of the file and its base view at once."""
    import duckdb

    from stack.checks.blocks import _finding as bfinding
    from stack.checks.blocks import _ident
    from stack.schemas.blocks import BLOCK_NAMES

    bad: list[str] = []
    for df in view.pair_files("X15"):
        written = df.blocks_written
        if written is None:
            continue
        others = [b for b in BLOCK_NAMES if b not in written]
        if others and view.readable(df) and not view.has_base(df):
            base_gap(view, df, bad)
        if not others or not view.readable(df) or not view.has_base(df):
            continue
        tbl = view.read(df, ["doc_id", *others])
        base = view.base_view(df, ["doc_id", *others], keys=tbl.column("doc_id").unique())  # type: ignore[union-attr]
        if base is None:
            continue
        con = duckdb.connect()
        try:
            con.register("f", tbl)
            con.register("b", base)
            for blk in others:
                c = _ident(blk)
                rows = con.execute(f"SELECT f.doc_id FROM f JOIN b USING (doc_id) "
                                   f"WHERE f.{c} IS DISTINCT FROM b.{c} ORDER BY f.doc_id").fetchall()
                bad += [f"{df.file_id}: {r[0]} changes {blk} (writes {written})" for r in rows]
        finally:
            con.close()
    return bfinding("block_ownership", "{n} block(s) changed outside blocks_written", bad)


def _restamp_untouched(w: World) -> None:
    """F3 (acquire part) restamps doc 0's identity without changing it (X17 red)."""
    w.doc("F3", 0)["identity"]["run_id"] = "r" + "9" * 12


def _restamp_changed(w: World) -> None:
    """F3 changes doc 1's identity (its override ids, outside every digest) and restamps it: allowed
    by X17."""
    ident = w.doc("F3", 1)["identity"]
    ident["run_id"] = "r" + "9" * 12
    ident["override_ids"] = [*(ident["override_ids"] or []), "o" + "1" * 12]


SIBLINGS: dict[str, tuple[Any, Any]] = {
    "cardinalities": (ref_cardinalities, "stack.checks.blocks:cardinalities"),
    "created_preserved": (ref_created_preserved, "stack.checks.blocks:created_preserved"),
    "attempts_monotone": (ref_attempts_monotone, "stack.checks.blocks:attempts_monotone"),
    "gate_obeyed": (ref_gate_obeyed, "stack.checks.triage:gate_obeyed"),
    "block_ownership": (ref_block_ownership, "stack.checks.blocks:block_ownership"),
}


def _new(check: str) -> Any:
    import importlib

    mod, fn = SIBLINGS[check][1].split(":")
    return getattr(importlib.import_module(mod), fn)


@pytest.fixture(scope="module")
def world_store(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """``world -> root``: each check-independent world of the sibling comparison written once per module
    (writing one costs ~1 s, about half a case) and shared read-only by its five cases."""
    from tests.fixtures.checks.triage import x13_gated

    roots: dict[str, Path] = {}

    def get(world: str) -> Path:
        if world not in roots:
            w = clean_world()
            if world == "restamp_untouched":
                _restamp_untouched(w)
            elif world == "restamp_changed":
                _restamp_changed(w)
            elif world.startswith("gated_"):
                x13_gated(w, mark=world.removeprefix("gated_"))
            else:
                assert world == "clean", world
            root = tmp_path_factory.mktemp(f"world-{world}")
            write_world(w, root)
            read_only(root)
            roots[world] = root
        return roots[world]

    return get


#: The sibling cases kept in the fast tier: every check on its own (red) fixture, the restamp message of X17, and
#: the cheap checks (X13 gate, cardinalities) on every world; the other (world, check) pairs are ``slow`` (the test-tier work).
def _sibling_fast(world: str, check: str) -> bool:
    return (world == "own_fixture" or (world, check) == ("restamp_untouched", "created_preserved")
            or check in ("gate_obeyed", "cardinalities"))


@pytest.mark.parametrize(("world", "check"), [
    (w, c) if _sibling_fast(w, c) else pytest.param(w, c, marks=pytest.mark.slow)
    for c in sorted(SIBLINGS)
    for w in ("clean", "own_fixture", "restamp_untouched", "restamp_changed", "gated_attempt", "gated_snapshot")])
def test_projected_siblings_equal_the_row_wise_checks(tmp_path: Path, world_store: Any, check: str,
                                                      world: str) -> None:
    if world == "own_fixture":
        w = clean_world()
        FIXTURES[check](w)
        write_world(w, tmp_path)
        root = tmp_path
    else:
        root = world_store(world)
    view = StoreView(root)
    want = SIBLINGS[check][0](view)
    view.drop_cache()
    got = _new(check)(view)
    assert got == want
    if world == "own_fixture":
        assert got  # its own fixture is red: the comparison can fail
    if world == "restamp_untouched" and check == "created_preserved":
        assert got and "untouched identity restamped" in got[0].items[0]
    if world.startswith("gated_") and check == "gate_obeyed":
        assert got


@pytest.mark.slow
def test_x13_x24_projections_cover_what_they_read(tmp_path: Path) -> None:
    """Every key ``_attempted_roles``, ``_fetchable`` (X13) and X24's comparison read is projected."""
    from stack.checks.blocks import X24_FIELDS
    from stack.checks.triage import X13_CONTENT, X13_TRIAGE
    from tests.fixtures.checks.triage import x13_gated

    w = clean_world()
    x13_gated(w, mark="snapshot")
    write_world(w, tmp_path)
    view = StoreView(tmp_path)
    seen: set[str] = set()
    for df in view.pair_files("X13"):
        for _, cur, prev in ref_base_pairs(view, df, ["content", "annotations"]):
            c, p = _Tracked(cur, seen), _Tracked(prev, seen)
            triage_checks._attempted_roles(c, p)
            triage_checks._fetchable(c)
    allowed = {*X13_CONTENT, *X13_TRIAGE, *X24_FIELDS}
    outside = {q for q in seen if not any(q == a or q.startswith(a + ".") or a.startswith(q + ".") for a in allowed)}
    assert seen and outside == set()
    assert "content.roles.lane" in seen and "annotations.triage.payload.decision" in seen


# ---------------------------------------------------------------- X9's comparison
def ref_compare(where: str, got: pa.Table, want: pa.Table, bad: list[str]) -> None:
    """X9's ``_compare`` as it was: every common row to Python."""
    g = {d: i for i, d in enumerate(got.column("doc_id").to_pylist())}
    w = {d: i for i, d in enumerate(want.column("doc_id").to_pylist())}
    bad += [f"{where}: {d} missing (the recomputation has it)" for d in sorted(set(w) - set(g))]
    bad += [f"{where}: {d} not in the recomputation" for d in sorted(set(g) - set(w))]
    common = sorted(set(g) & set(w))
    for start in range(0, len(common), BATCH_ROWS):
        ds = common[start:start + BATCH_ROWS]
        gs = got.take([g[d] for d in ds]).to_pylist()
        ws = want.take([w[d] for d in ds]).to_pylist()
        for d, gr, wr in zip(ds, gs, ws, strict=True):
            if gr != wr:
                cols = [c for c in gr if gr[c] != wr.get(c)]
                bad.append(f"{where}: {d} differs from the recomputation in {cols}")


@pytest.mark.parametrize("fixture", [None, "fold_recompute"])
def test_x9_equals_the_row_wise_comparison(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                           fixture: str | None) -> None:
    from stack.checks import build_checks

    build_store(tmp_path, fixture)
    got = build_checks.fold_recompute(StoreView(tmp_path))
    with monkeypatch.context() as m:
        m.setattr(build_checks, "_compare", ref_compare)
        want = build_checks.fold_recompute(StoreView(tmp_path))
    assert got == want
    assert bool(got) == (fixture is not None)


def test_x9_compare_fast_path_and_fallback() -> None:
    from stack.checks.build_checks import _compare

    t = pa.table({"doc_id": ["a", "b", "c"], "x": [1, None, 3],
                  "s": pa.array([{"p": 1.0}, None, {"p": float("nan")}])})
    for other in (t, t.slice(0, 2), t.set_column(1, "x", pa.array([1, 2, 3])),
                  t.set_column(1, "x", pa.array([1, None, 3], pa.int32()))):  # another schema: the Python path
        a: list[str] = []
        b: list[str] = []
        _compare("w", t, other, a)
        ref_compare("w", t, other, b)
        assert a == b
    nan: list[str] = []
    _compare("w", t, t, nan)
    assert nan == ["w: c differs from the recomputation in ['s']"]  # NaN != NaN, as the Python rows say


# ---------------------------------------------------------------- read_view over one full file
def test_read_view_of_one_full_file_equals_the_general_path(tmp_path: Path) -> None:
    """A version of one full file (every series written in full files) is read once, without the
    row selection; the view equals the general path's (the keyed read, which always selects)."""
    from stack.store import chain as store_chain

    root = tmp_path / "store"
    build_store(root)
    seen = 0
    for d in (sdir("sources.arxiv"), sdir("documents")):
        files = store_chain.dir_files(root, d)
        for f in files:
            if f.kind != "full":
                continue
            ch = resolve(root, f.path)
            assert len(ch.files) == 1
            whole = read_view(ch)
            ids = read_family(f.path, list(ch.spec.key))[0].column(0)  # every key of the file
            assert whole.equals(read_view(ch, keys=ids))
            proj = read_view(ch, columns=["state"])
            assert proj.equals(read_view(ch, columns=proj.column_names, keys=ids))
            if ch.family == "documents":
                deep = read_view(ch, columns=X19_PATHS)
                assert deep.equals(read_view(ch, columns=X19_PATHS, keys=ids))
            seen += 1
    assert seen >= 3


def test_filter_rows_keeping_every_row_is_the_table() -> None:
    from stack.fold import filter_rows

    t = pa.table({"doc_id": ["a", "b"], "state": ["identified", "retired"]})
    assert filter_rows(t, "true").equals(t)
    assert filter_rows(t, "state = 'retired'").column("doc_id").to_pylist() == ["b"]
