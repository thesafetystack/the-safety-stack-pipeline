"""The store foundation (T1.1): every family round-trips through the parquet writer/reader with
its header, each write-time check S1-S7 refuses its own fixture and writes nothing, file names
parse right to left, and the S3 backend round-trips against an in-memory fake client. The footer
cache (D731): each footer parsed once inside a block, a file changed in place (size and mtime kept)
read as it now is, nothing cached outside a block or after it."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
import pytest

from stack.schemas.families import FAMILIES
from stack.store import parquet
from stack.store.layout import StoreLayout, make_data_file
from stack.store.parquet import (
    SchemaMismatch,
    SchemaRefused,
    footer_cache,
    read_family,
    read_family_fields,
    read_footer,
    read_header,
    read_raw,
    write_family,
)
from tests.synth.rows import (
    AT,
    S_FIXTURES,
    data_file,
    file_name,
    rid,
    valid_file,
)

ROUND_TRIP = ["sources", "coverage", "documents", "corpus", "screen", "screen_evidence",
              "triage_evidence"]


@pytest.mark.parametrize("fam", ROUND_TRIP)
def test_family_round_trip(tmp_path: Path, fam: str) -> None:
    tbl, header = valid_file(fam)
    path = StoreLayout(tmp_path).ensure().data_file(data_file(header))
    write_family(path, tbl, header)
    got, got_header = read_family(path)
    assert got.equals(tbl)
    assert got_header == header
    assert read_header(path) == header
    assert got.schema.equals(FAMILIES[fam].schema)
    assert [p.name for p in path.parent.iterdir()] == [path.name]  # no temp file left behind
    with pytest.raises(FileExistsError):  # files are immutable
        write_family(path, tbl, header)


@pytest.mark.parametrize("check", list(S_FIXTURES))
def test_s_check_refuses_its_fixture(tmp_path: Path, check: str) -> None:
    tbl, header = S_FIXTURES[check]()
    path = tmp_path / file_name(header)
    with pytest.raises(SchemaRefused) as exc:
        write_family(path, tbl, header)
    assert exc.value.checks == {check}, exc.value
    assert not path.parent.exists() or not any(path.parent.iterdir())


def test_header_must_match_file_name(tmp_path: Path) -> None:
    """D372: the header's family directory, series and kind must be the path's."""
    tbl, header = valid_file("documents")
    wrong = tmp_path / make_data_file("documents", "other", AT, rid(), "full").key
    with pytest.raises(SchemaRefused, match="series"):
        write_family(wrong, tbl, header)
    wrong = tmp_path / make_data_file("documents.x", "main", AT, rid(), "full").key
    with pytest.raises(SchemaRefused, match="chain_id"):
        write_family(wrong, tbl, header)


def test_part_file_cannot_start_a_chain(tmp_path: Path) -> None:
    tbl, header = valid_file("documents")
    header = {**header, "kind": "part"}
    path = tmp_path / make_data_file("documents", "main", AT, rid(), "part").key
    with pytest.raises(SchemaRefused, match="a part file with no base") as exc:
        write_family(path, tbl, header)
    assert "names its base" in str(exc.value)


def test_reader_refuses_a_foreign_schema_digest(tmp_path: Path) -> None:
    tbl, header = valid_file("coverage")
    path = tmp_path / file_name(header)
    path.parent.mkdir(parents=True)
    meta = {f"stack.{k}".encode(): v.encode() for k, v in {**header, "schema_digest": "0" * 64}.items()}
    pq.write_table(tbl.replace_schema_metadata(meta), path)  # bypasses the writer on purpose
    with pytest.raises(SchemaMismatch, match="C1"):
        read_family(path)




# ---------------------------------------------------------------- the footer cache (D731)
def rewrite_header_in_place(path: Path, key: str, value: str) -> None:
    """Rewrite the family file at ``path`` with header ``key`` set to ``value`` (the same length as the old
    value), in place: the same inode, the same size, the old mtime restored. A cache keyed by path, size,
    mtime or inode would not see the change."""
    tbl = pq.read_table(path)
    meta = dict(tbl.schema.metadata or {})
    old = meta[f"stack.{key}".encode()]
    assert len(old) == len(value.encode()) and old != value.encode(), (old, value)
    meta[f"stack.{key}".encode()] = value.encode()
    tmp = path.parent / f".{path.name}.rewrite"
    pq.write_table(tbl.replace_schema_metadata(meta), tmp, compression="zstd")  # as write_family writes
    st = path.stat()
    body = tmp.read_bytes()
    tmp.unlink()
    assert len(body) == st.st_size  # the precondition the test relies on: same size
    mode = st.st_mode
    path.chmod(0o644)
    with open(path, "r+b") as fh:
        fh.write(body)
    path.chmod(mode)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    after = path.stat()
    assert (after.st_ino, after.st_size, after.st_mtime_ns) == (st.st_ino, st.st_size, st.st_mtime_ns)


def count_footer_parses(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """The paths whose footer is parsed: by :func:`stack.store.parquet.read_footer` (a miss, or no block), and
    by :func:`stack.store.parquet.read_raw` without kept metadata (``("raw", path)``)."""
    seen: list[Any] = []
    real, real_pf = parquet._read_footer_uncached, pq.ParquetFile
    in_footer = [False]

    def counting(path: Any) -> Any:
        seen.append(path)
        in_footer[0] = True
        try:
            return real(path)
        finally:
            in_footer[0] = False

    class CountingParquetFile(real_pf):  # type: ignore[misc]
        def __init__(self, source: Any, *, metadata: Any = None, **kw: Any) -> None:
            if metadata is None and not in_footer[0]:
                seen.append(("raw", source))
            super().__init__(source, metadata=metadata, **kw)

    monkeypatch.setattr(parquet, "_read_footer_uncached", counting)
    monkeypatch.setattr(parquet.pq, "ParquetFile", CountingParquetFile)
    return seen


def _written(tmp_path: Path, fam: str = "coverage") -> tuple[Path, dict[str, str]]:
    tbl, header = valid_file(fam)
    path = StoreLayout(tmp_path).ensure().data_file(data_file(header))
    write_family(path, tbl, header)
    return path, header


def test_footer_cache_parses_each_footer_once_inside_a_block_only(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path, header = _written(tmp_path, "documents")
    seen = count_footer_parses(monkeypatch)
    read_header(path)
    read_family(path)
    assert seen == [path, path, ("raw", path)]  # outside a block every read parses
    seen.clear()
    with footer_cache():
        assert read_header(path) == header
        read_family(path)
        read_family_fields(path, ["doc_id", "identity.anchor_source_id"])
        read_raw(path, ["doc_id"])
        with footer_cache():  # a nested block shares the outer cache
            assert read_footer(path).digest == read_footer(path).digest
        assert seen == [path]
    with footer_cache():  # a new block starts empty: nothing outlives a block
        read_header(path)
    assert seen == [path, path]


def test_footer_cache_keeps_the_newest_metadata_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    a, _ = _written(tmp_path / "a")
    b, _ = _written(tmp_path / "b", "screen")
    monkeypatch.setattr(parquet, "METADATA_ENTRIES", 1)
    seen = count_footer_parses(monkeypatch)
    with footer_cache():
        want = [read_family(a)[0], read_family(b)[0]]
        assert seen == [a, b]
        assert [read_family(a)[0], read_family(b)[0]] == want  # a's metadata was dropped for b's
        assert seen == [a, b, ("raw", a), ("raw", b)]


def test_footer_cache_reads_a_file_changed_in_place_as_it_now_is(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Within one block, a file rewritten with its size, mtime and inode kept (rewrite_header_in_place) reads
    with its new footer: the key is the footer's bytes, read afresh on every lookup."""
    path, header = _written(tmp_path)
    seen = count_footer_parses(monkeypatch)
    with footer_cache():
        assert read_family(path)[1] == header
        rewrite_header_in_place(path, "schema_digest", "0" * 64)
        assert read_header(path)["schema_digest"] == "0" * 64
        with pytest.raises(SchemaMismatch, match="C1"):
            read_family(path)
    assert seen == [path, path]


def test_a_footer_that_does_not_read_raises_alike_inside_and_outside_a_block(tmp_path: Path) -> None:
    path, _ = _written(tmp_path)
    path.write_bytes(path.read_bytes()[:-200])  # truncated: no footer
    with pytest.raises(Exception) as outside:
        read_header(path)
    with footer_cache():
        for _ in range(2):  # not cached as a failure either
            with pytest.raises(type(outside.value)) as inside:
                read_header(path)
            assert str(inside.value) == str(outside.value)


@pytest.mark.parametrize("fam", ROUND_TRIP)
def test_read_raw_reads_what_read_table_reads(tmp_path: Path, fam: str) -> None:
    path, _ = _written(tmp_path, fam)
    want = pq.read_table(path).replace_schema_metadata(None)
    assert read_raw(path).equals(want)
    cols = list(reversed(want.schema.names[:3]))
    assert read_raw(path, cols).equals(pq.read_table(path, columns=cols).replace_schema_metadata(None))
    with pytest.raises(ValueError, match="not in the file"):
        read_raw(path, ["no_such_column"])
    with pytest.raises(ValueError, match="repeated"):
        read_raw(path, [cols[0], cols[0]])
