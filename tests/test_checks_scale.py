"""Verify at scale (round 5, S-17 / T-SCALE S2-S3): the store checks' projected, key-restricted,
per-check-cached reads (``stack/checks/chain.py`` ``StoreView``, ``stack.store.chain.read_view``'s
``columns``/``keys``).

Synthetic only: the clean check store (``tests.fixtures.checks``), and the same store downgraded
to older format versions (``tests.test_family_format.downgrade``: ``screen_evidence``) or read under a
future additive version (``tests.test_family_format.future_version``: ``documents``/``corpus``, whose
older versions do not read since D690) for the upcast reads. That every
verdict is unchanged is shown by the per-check fixtures (``tests/test_checks_fixtures.py``) and, on
real stores, by the before/after ``verify_full`` comparison recorded in the 2026-10 run log (not in this repository).
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import pytest

from stack.checks import REGISTRY
from stack.checks import chain as checks_chain
from stack.checks.chain import SAME, UPCAST, StoreView, as_family
from stack.checks.verify import run_checks
from stack.schemas.families import schema_digest
from stack.store import chain as store_chain
from stack.store.chain import read_view, resolve
from stack.store.layout import DATA, HEADER_PREFIX, parse_data_key
from stack.store.parquet import read_family
from tests.fixtures.checks import build_store
from tests.synth.records import sdir
from tests.test_family_format import FUTURE_FIELD, downgrade, future_version

pytestmark = pytest.mark.checks  # area marker(s), not a tier (pyproject markers; docs/SETUP.md §7)


def _full(df: checks_chain.DataFile) -> pa.Table | None:
    assert df.spec is not None
    return as_family(df.path)


@pytest.fixture
def old_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The clean store with ``documents`` and ``corpus`` one version behind (a future additive version
    adds :data:`FUTURE_FIELD`), ``screen_evidence`` at v1 (four record columns missing), and one
    ``screen`` file that does not read (``doc_id`` a large string, declaring the current version)."""
    root = tmp_path / "store"
    build_store(root)
    downgrade(root, "screen_evidence", 1)
    future_version(monkeypatch, "documents", "corpus")
    p = next(q for q in (root / DATA).rglob("*.parquet") if parse_data_key(q).family == "screen")
    tbl, h = read_family(p)
    i = tbl.schema.get_field_index("doc_id")
    tbl = tbl.set_column(i, pa.field("doc_id", pa.large_string(), False), tbl.column(i).cast(pa.large_string()))
    h["schema_digest"] = schema_digest(tbl.schema)  # its own, not its declared version's: unreadable (S1's)
    p.unlink()
    pq.write_table(tbl.replace_schema_metadata({f"{HEADER_PREFIX}{k}".encode(): v.encode() for k, v in h.items()}), p)
    return root


def test_schema_status_is_as_family_on_the_whole_file(old_store: Path) -> None:
    view = StoreView(old_store)
    seen = set()
    for df in view.files:
        full = _full(df)
        status = view.schema_status(df)
        seen.add(status)
        if full is None:
            assert status is None, df.path.name
        elif pq.read_schema(df.path).remove_metadata().equals(df.spec.schema, check_metadata=False):  # type: ignore[union-attr]
            assert status == SAME, df.path.name
        else:
            assert status == UPCAST, df.path.name
    assert seen == {SAME, UPCAST, None}  # every outcome exercised


@pytest.mark.slow
def test_projected_table_of_an_older_schema_is_the_upcast_selected(old_store: Path) -> None:
    view = StoreView(old_store)
    picks = {"documents": ["doc_id", "state", "content", "annotations"],
             "corpus": ["annotations", "doc_id"], "screen_evidence": ["stack_relevance", "evidence_id"]}
    n = 0
    for df in view.files:
        if df.family not in picks or view.schema_status(df) != UPCAST:
            continue
        full = _full(df)
        assert full is not None
        got = view.table(df, picks[df.family])
        want = full.select([c for c in full.schema.names if c in picks[df.family]])  # registry order
        assert got is not None and got.equals(want), df.path.name
        assert view.table(df) is not None and view.table(df).equals(full)  # type: ignore[union-attr]
        n += 1
    assert n >= 3
    doc = next(df for df in view.files if df.family == "documents" and view.schema_status(df) == UPCAST
               and view.table(df, ["doc_id"]).num_rows)  # type: ignore[union-attr]
    added = view.table(doc, ["doc_id", FUTURE_FIELD.name]).column(FUTURE_FIELD.name)  # type: ignore[union-attr]
    assert len(added) > 0 and added.null_count == len(added)  # the added field: NULL
    unreadable = next(df for df in view.files if df.family == "screen" and view.schema_status(df) is None)
    assert view.table(unreadable, ["doc_id"]) is None


@pytest.mark.parametrize("older", [False, True])
@pytest.mark.slow
def test_read_view_columns_and_keys_are_the_view_filtered_and_selected(tmp_path: Path, older: bool,
                                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "store"
    build_store(root)
    written = resolve(root, sdir("documents")).spec.version
    if older:  # every documents file one version behind: each read upcast
        future_version(monkeypatch, "documents")
    ch = resolve(root, sdir("documents"))
    assert set(ch.versions) == {written} and ch.spec.version == written + older
    full = read_view(ch)
    ids = full.column("doc_id").to_pylist()
    some = ids[::2] + ["no-such-doc"]
    exp = full.filter(pc.is_in(full.column("doc_id"), value_set=pa.array(some)))
    got = read_view(ch, columns=["content", "state"], keys=some)
    assert got.column_names == ["doc_id", "state", "content"]  # the key added, registry order
    assert got.equals(exp.select(["doc_id", "state", "content"]))
    assert read_view(ch, keys=pa.array(some)).equals(exp)
    assert read_view(ch, columns=["identity"]).equals(full.select(["doc_id", "identity"]))
    assert read_view(ch, keys=[]).num_rows == 0
    with pytest.raises(ValueError, match="no column"):
        read_view(ch, columns=["nope"])
    # a multi-column key: keys as tuples
    cov = resolve(root, sdir("coverage.arxiv"))
    cfull = read_view(cov)
    key = list(cov.spec.key)
    first = tuple(cfull.select(key).to_pylist()[0].values())
    one = read_view(cov, columns=["channel"], keys=[first])
    assert one.num_rows == 1 and one.equals(cfull.slice(0, 1).select(one.column_names))


def test_read_view_projected_on_an_older_schema(old_store: Path) -> None:
    view = StoreView(old_store)
    head = resolve(old_store, sdir("documents")).head
    full = view.view_at(head)
    assert full is not None
    some = full.column("doc_id").to_pylist()[1::2]
    got = view.view_at(head, ["annotations"], keys=some)
    exp = full.filter(pc.is_in(full.column("doc_id"), value_set=pa.array(some))).select(["doc_id", "annotations"])
    assert got is not None and got.equals(exp)


@pytest.mark.slow
def test_the_cache_is_empty_after_each_store_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "store"
    build_store(root)
    seen: list[tuple[str, int, int]] = []  # (check, cache entries at start, at end)

    def spy(name: str, fn: Any) -> Any:
        def run(view: StoreView) -> Any:
            start = len(view._cache)
            try:
                return fn(view)
            finally:
                seen.append((name, start, len(view._cache)))
        return run

    for name, cs in list(REGISTRY.items()):
        if cs.fn is not None and cs.scope == "store":
            monkeypatch.setitem(REGISTRY, name, dataclasses.replace(cs, fn=spy(name, cs.fn)))
    run_checks(root)
    assert len(seen) > 20
    assert all(start == 0 for _, start, _ in seen), [s for s in seen if s[1]]
    assert any(end > 0 for _, _, end in seen)  # the checks do cache: the test can fail


@pytest.mark.slow
def test_a_pair_check_reads_only_the_parts_keys_from_the_base(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "store"
    build_store(root)
    calls: list[tuple[str, tuple[str, ...] | None, set[str] | None, int]] = []
    real = store_chain.read_view

    def spy(chain: store_chain.Chain, **kw: Any) -> pa.Table:
        out = real(chain, **kw)
        keys = kw.get("keys")
        calls.append((chain.head, kw.get("columns"), None if keys is None else set(pa.array(keys).to_pylist()),
                      out.num_rows))
        return out

    monkeypatch.setattr(store_chain, "read_view", spy)
    view = StoreView(root)
    part = next(df for df in view.pair_files("X17") if df.header.get("stage") == "acquire")
    ids = set(pq.read_table(part.path, columns=["doc_id"]).column("doc_id").to_pylist())
    base_ids = set(real(resolve(root, sdir("documents"))).column("doc_id").to_pylist())  # any later view
    assert ids < base_ids  # a part over a larger base
    pairs = list(view.base_pairs(part, ["created_at"]))
    assert {d for d, _, _ in pairs} == ids and all(set(c) == {"doc_id", "created_at"} for _, c, _ in pairs)
    (call,) = [c for c in calls if c[0] == part.base_file_id]
    assert call[2] == ids and call[3] == len(ids)
    assert call[1] is not None and set(call[1]) == {"doc_id", "created_at"}
    # and the check itself: each base read keyed by its file's ids, never the whole base view
    calls.clear()
    view.drop_cache()
    from stack.checks.blocks import created_preserved
    assert created_preserved(view) == []
    assert calls and all(c[2] is not None and c[3] <= len(c[2]) for c in calls)


def test_the_cache_is_bounded_in_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "store"
    build_store(root)
    view = StoreView(root)
    docs = [df for df in view.files if df.family == "documents"]
    sizes = [view.read(df).nbytes for df in docs]  # type: ignore[union-attr]
    bound = max(sizes) + min(sizes) // 2
    monkeypatch.setattr(checks_chain, "CACHE_BYTES", bound)
    for df in docs:
        view.table(df)
        assert view.cache_bytes <= bound
    assert 0 < len(view._cache) < len(docs)  # older entries evicted
    monkeypatch.setattr(checks_chain, "CACHE_BYTES", min(sizes) - 1)
    view.drop_cache()
    assert view.table(docs[0]) is not None and view.cache_bytes == 0  # larger than the bound: not kept

