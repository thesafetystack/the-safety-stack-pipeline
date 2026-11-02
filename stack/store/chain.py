"""Chains: the reader and writer of a family's versioned files (D372, D373; ``docs/OPERATE.md``,
"Data files: series and chains"; §7.1-7.6, D14, D16, D38).

**A chain is a directory** ``data/<family>/<series>/``: the **published** files in it, in name
order (``<utc>_<run>[_<comment>].<full|part>.parquet``; names sort in time order). A file is
published when an ``ok`` run record lists it (path, ``file_digest``, ``kind``, ``base``,
``after``) and no ``ok`` record's ``excludes`` takes it out (``stack chain orphan``); every other
file is ignored. A file without a series (``data/<family>/<name>.full.parquet``) is a full read
only by its exact path.

**Reading** (:func:`resolve`): a series directory means its last published file, an exact file
that file. Walking back from it to the newest full file (that full included), each file's
``after`` (the directory's last file when its run pinned its inputs, D372) must be its
predecessor in name order (empty only for the directory's first file): a mismatch is a
:class:`ForkError` naming both runs (two writers, a lost update) or, when ``after`` names a file
that is not published (excluded), a :class:`MissedFileError`. Links older than that full file are
not checked. All of this comes from the records; only then are the version's files read,
**downloaded first when absent** (pull on read, checked against ``file_digest``) and their
footers checked against the records. :func:`read_view` unions the version's files by position
(§7.3's SQL, duckdb).

**Writing** (D16, D38, D372): :func:`stage_file` writes
``data/<family>/<series>/.<name>.tmp`` through the family writer (S1-S7); :func:`publish` checks
the active job's cancel flag, refreshes the index and applies **the publish rule** per file
(:func:`claim`): the directory's last published file must still be the file's ``after``
(:class:`HeadMovedError` ``head_moved``) and the new name must sort after it
(:class:`NameOrderError` ``name_order``); a directory whose last file is this run's own output is
already committed (a lost acknowledgement). Then each file is renamed into place without
clobbering, PUT with ``If-None-Match: *`` when the active job uploads, and the caller's hook
writes the final run record (the commit, D345).

**No locks** (D355, D356; ``docs/OPERATE.md``, "Two writers, forks and their repair"): two writers
that pass the check before either commits both commit; the next resolve raises :class:`ForkError`.
The repair is :func:`orphan` on the unwanted branch.

The reader trusts nothing it can recompute: per version file it checks the footer against the
record, recomputes ``schema_digest`` from the parquet schema and ``ids_digest`` (and key
uniqueness) from the key columns.

**Format versions** (D295; amends D43, D208 (2)). A chain may hold files of several format
versions of its family (:mod:`stack.schemas.upcasters`): C1 is "every file's ``schema_digest`` is its
declared version's". The resolver finds each version file's format from its footer
(:func:`stack.store.parquet.file_format`) and refuses, with :class:`FileFormatError` and before any
row is read, a file that is newer than this code, of an unknown format, or behind an
``Incompatible`` step; :func:`read_view` reads every file through
:func:`stack.store.parquet.read_family`, so the view is at the family's current version.
"""

from __future__ import annotations

import datetime as dt
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc

from stack import duck
from stack.checks.schema import schema_equal
from stack.runs import (
    OutputInfo,
    RunRecord,
    file_digest,
)
from stack.schemas.families import FamilySpec, family, ids_digest
from stack.store.layout import (
    DATA,
    NONE,
    WILDCARD,
    DataFile,
    StoreLayout,
    file_id,
    parse_data_key,
    parse_data_name,
    parse_dir_ref,
)
from stack.store.parquet import (
    Header,
    SchemaMismatch,
    build_header,
    field_paths,
    file_format,
    project_schema,
    read_family,
    read_family_fields,
    read_footer,
    read_raw,
    write_family,
)

#: Family kinds whose files were chains of one before D372 (D14, D54): no part files.
CHAIN_OF_ONE_KINDS: frozenset[str] = frozenset({"corpus", "wf", "evidence"})


# ---------------------------------------------------------------- errors
class ChainError(Exception):
    """A chain cannot be resolved, read or written as asked."""


class EmptyChainError(ChainError):
    """No ``ok`` run record names a file in this directory."""


class MissedFileError(ChainError):
    """A file on the walk names, as its ``after``, a file that is not published (excluded, or no
    ``ok`` record names it), or a version file is missing."""


class ForkError(ChainError):
    """A file on the walk was written after another file than its predecessor in name order: two
    writers, or a lost update (D372)."""


class FileFormatError(ChainError):
    """A file of the version (newest full file onward) does not read at the family's current
    format (C1, D295): newer than this code, unknown, its digest not its declared version's, or
    behind an ``Incompatible`` step. ``error`` is the reader's
    :class:`~stack.store.parquet.SchemaMismatch` (:class:`~stack.store.parquet.NewerFormat`,
    :class:`~stack.store.parquet.UnknownFormat`, :class:`~stack.store.parquet.IncompatibleFormat`)."""

    def __init__(self, message: str, error: SchemaMismatch) -> None:
        super().__init__(message)
        self.error = error


class NoFullFileError(ChainError):
    """The walk back from the head ends without reaching a full file."""


class ChainConsistencyError(ChainError):
    """A file's header disagrees with its path, its record or its own contents (file id, kind,
    family, recomputed ``schema_digest``/``ids_digest``, duplicate keys)."""


class HeadMovedError(ChainError):
    """The publish rule: the directory gained a file since the run pinned its inputs (D372)."""


class NameOrderError(ChainError):
    """The publish rule: the new file's name does not sort after the directory's last file."""


@dataclass(frozen=True)
class ChainFile:
    """One published file: from its run record's output entry (``output``; ``header`` then holds
    the keys the record gives) until the version is loaded, then from its footer."""

    path: Path
    header: Header
    output: OutputInfo | None = None

    @property
    def name(self) -> str:
        """The published file name (a staged ``.<name>.tmp`` reads as ``<name>``)."""
        n = self.path.name
        return n.removeprefix(".").removesuffix(".tmp") if n.endswith(".tmp") else n

    @property
    def file_id(self) -> str:
        return self.header["file_id"]

    @property
    def base(self) -> str:
        """The base file's name in the directory; empty for a full without a base."""
        return self.header.get("base", "")

    @property
    def after(self) -> str:
        """The directory's last file when this file's run pinned its inputs; empty for the first."""
        return self.header.get("after", "")

    @property
    def base_file_id(self) -> str:
        """The base's file id (``<run>.<family>``, derived from :attr:`base`), ``none`` without one."""
        return file_id(parse_data_name(self.base)[1], self.family) if self.base else NONE

    @property
    def kind(self) -> str:
        """``full`` or ``part`` (``stack.kind``)."""
        return self.header["kind"]

    @property
    def is_full(self) -> bool:
        return self.kind == "full"

    @property
    def chain_id(self) -> str:
        """The family directory (``documents``, ``sources.af_graphql``)."""
        return self.header["chain_id"]

    @property
    def series(self) -> str:
        return self.header.get("series", "")

    @property
    def family(self) -> str:
        return self.header["family"]

    @property
    def run_id(self) -> str:
        return self.header["run_id"]

    @property
    def schema_digest(self) -> str:
        return self.header["schema_digest"]

    @property
    def row_count(self) -> int:
        return int(self.header["row_count"])


@dataclass(frozen=True)
class Chain:
    """A resolved version: the newest full file first, then its parts in name order; ``dir`` is
    the series directory key (``None`` for a file without a series), ``last`` the directory's last
    published file name when resolved (the ``after`` of a file the run writes there)."""

    chain_id: str
    family: str
    files: tuple[ChainFile, ...]
    #: Per file (chain order), its format version (:func:`stack.store.parquet.file_format`).
    versions: tuple[int, ...] = ()
    dir: str | None = None
    last: str = ""

    @property
    def head(self) -> str:
        """The resolved file's file id."""
        return self.files[-1].file_id

    @property
    def head_name(self) -> str:
        return self.files[-1].name

    @property
    def head_key(self) -> str:
        """The resolved file's store key."""
        return f"{self.dir or DATA + '/' + self.chain_id}/{self.head_name}"

    @property
    def is_dir_head(self) -> bool:
        """The resolved file is the directory's last file."""
        return self.dir is not None and self.head_name == self.last

    @property
    def series(self) -> str | None:
        return None if self.dir is None else self.dir.rsplit("/", 1)[-1]

    @property
    def version_chain(self) -> tuple[str, ...]:
        """File ids in chain order."""
        return tuple(f.file_id for f in self.files)

    @property
    def spec(self) -> FamilySpec:
        return family(self.family)

    @property
    def paths(self) -> tuple[Path, ...]:
        return tuple(f.path for f in self.files)


# ---------------------------------------------------------------- resolve
def _read_chain_file(path: Path, output: OutputInfo | None = None) -> tuple[ChainFile, str]:
    """``(file, recomputed schema_digest)`` from the parquet footer only."""
    ft = read_footer(path)
    return ChainFile(path, ft.header, output), ft.digest


#: The header keys a record's output entry gives, checked against the footer when loaded.
_RECORD_KEYS: tuple[str, ...] = ("file_id", "kind", "chain_id", "family", "run_id",
                                 "schema_digest", "series", "base", "after")


def _output_file(idx: object, e: object, key: str, o: OutputInfo) -> ChainFile:
    missing = [k for k in ("kind", "series", "base", "after") if k not in o]
    if missing:
        raise ChainConsistencyError(f"{e.key}: output {key} has no {', '.join(missing)} (a record "  # type: ignore[attr-defined]
                                    "written before D372; the store needs the one-off series migration)")
    header = {"file_id": o["file_id"], "kind": o["kind"],
              "chain_id": o["chain_id"], "family": key.split("/", 1)[0], "run_id": e.record.run_id,  # type: ignore[attr-defined]
              "schema_digest": o["schema_digest"], "row_count": str(o["row_count"]),
              "series": o["series"], "base": o["base"], "after": o["after"]}
    return ChainFile(idx.store.path(o["path"]), header, o)  # type: ignore[attr-defined]


def dir_files(store_root: str | Path, dir_key: str) -> list[ChainFile]:
    """The published files of a directory (``data/<family>/<series>``), in name order, from the
    store index (no file is read). Two records naming one path raise."""
    from stack.store.index import store_index

    idx = store_index(Path(store_root))
    out = [_output_file(idx, e, k, o) for e, k, o in idx.dir_outputs(dir_key.rstrip("/"))]
    out.sort(key=lambda c: c.name)
    for a, b in zip(out, out[1:], strict=False):
        if a.name == b.name:
            raise ChainConsistencyError(f"{dir_key}: {a.name} named by two ok records")
    return out


def last_file(store_root: str | Path, dir_key: str) -> ChainFile | None:
    """The directory's last published file (records only), ``None`` for an empty directory."""
    files = dir_files(store_root, dir_key)
    return files[-1] if files else None


def _check_file(cf: ChainFile, actual_schema_digest: str) -> None:
    """Header against path and recomputed schema digest (footer-only checks)."""
    h, f = cf.header, parse_data_key(cf.path)
    for k in ("file_id", "family", "chain_id", "schema_digest",
              "ids_digest", "row_count", "series", "kind", "base", "after"):
        if k not in h:
            raise ChainConsistencyError(f"{cf.name}: header has no stack.{k}")
    want = {"file_id": f.file_id, "family": f.family, "kind": f.kind,
            "chain_id": f.family_dir, "series": f.series or ""}
    for k, v in want.items():
        if h[k] != v:
            raise ChainConsistencyError(f"{cf.name}: header {k} = {h[k]!r} but the path says {v!r}")
    if h["schema_digest"] != actual_schema_digest:
        raise ChainConsistencyError(
            f"{cf.name}: header schema_digest {h['schema_digest'][:12]}… but the file's schema "
            f"renders to {actual_schema_digest[:12]}…")


def _load_file(cf: ChainFile) -> tuple[ChainFile, str]:
    """The footer form of a record-made :class:`ChainFile` (downloaded first when absent, the
    download checked against ``file_digest``), its footer checked against the record."""
    if cf.output is None:
        return _read_chain_file(cf.path)
    from stack.store.index import OutputDigestError, store_index

    root = cf.path.parents[3] if cf.series else cf.path.parents[2]  # <root>/data/<family>[/<series>]/<name>
    try:
        path = store_index(root).fetch_output(cf.output)
    except (FileNotFoundError, OutputDigestError) as exc:
        raise MissedFileError(f"{cf.output['path']}: {exc}") from None
    loaded, digest = _read_chain_file(path, cf.output)
    bad = [k for k in _RECORD_KEYS if loaded.header.get(k, "") != cf.header[k]]
    if bad:
        raise ChainConsistencyError(f"{cf.name}: footer and run record disagree on "
                                    + ", ".join(f"{k} ({loaded.header.get(k)!r} vs {cf.header[k]!r})" for k in bad))
    return loaded, digest


def _check_ids(cf: ChainFile, spec: FamilySpec) -> None:
    """An id at most once per file (S3, re-checked) and ``ids_digest`` recomputes (§7.5)."""
    keys = read_raw(cf.path, list(spec.key))
    if keys.num_rows != cf.row_count:
        raise ChainConsistencyError(f"{cf.path.name}: {keys.num_rows} rows, header row_count {cf.row_count}")
    if keys.num_rows:
        n_unique = keys.group_by(list(spec.key)).aggregate([]).num_rows
        if n_unique != keys.num_rows:
            raise ChainConsistencyError(
                f"{cf.path.name}: {keys.num_rows - n_unique} duplicate {'/'.join(spec.key)} rows")
    got = ids_digest(keys, spec.key)
    if got != cf.header["ids_digest"]:
        raise ChainConsistencyError(f"{cf.path.name}: ids_digest does not recompute")


def _walk(dir_key: str, files: Sequence[ChainFile], target: int) -> list[ChainFile]:
    """From ``files[target]`` back to the newest full file at or before it, each file's
    ``after`` checked against its predecessor (module docstring); chain order."""
    names = {cf.name for cf in files}
    walk: list[ChainFile] = []
    i = target
    while True:
        cur = files[i]
        pred = files[i - 1].name if i > 0 else ""
        if cur.after != pred:
            if cur.after and cur.after not in names:
                raise MissedFileError(
                    f"{dir_key}: {cur.name} (run {cur.run_id}) was written after {cur.after}, which is not "
                    f"published (excluded, or no ok record names it); the repair is a new full file")
            other = f"{files[i - 1].name} (run {files[i - 1].run_id})" if i > 0 else "nothing (the first file)"
            raise ForkError(
                f"{dir_key}: {cur.name} (run {cur.run_id}) was written after {cur.after or 'an empty directory'}, "
                f"but its predecessor is {other}: two writers or a lost update; "
                f"`stack chain orphan` the unwanted branch")
        walk.append(cur)
        if cur.is_full:
            return walk[::-1]
        if i == 0:
            raise NoFullFileError(f"{dir_key}: the walk back from {files[target].name} ends at {cur.name} "
                                  "(a part) without a full file")
        i -= 1


def _load(chain: Chain, *, verify_ids: bool) -> Chain:
    """Read the version's files (:func:`_load_file`) and check them (C1, footers, ``verify_ids``)."""
    loaded = [_load_file(cf) for cf in chain.files]
    for cf, digest in loaded:
        _check_file(cf, digest)
    versions = []
    for cf, digest in loaded:  # C1 (D295): every file's digest its declared version's, and readable
        try:
            versions.append(file_format(cf.path, cf.header, digest=digest).version)
        except SchemaMismatch as exc:
            raise FileFormatError(f"{chain.dir or chain.chain_id}: {type(exc).__name__}: {exc}", exc) from None
    spec = family(chain.family)
    if verify_ids:
        for cf, _ in loaded:
            _check_ids(cf, spec)
    return Chain(chain.chain_id, chain.family, tuple(cf for cf, _ in loaded), tuple(versions), chain.dir, chain.last)


def _resolve_ref(root: Path, ref: str | Path) -> Chain:
    """The records-only version a reference names (module docstring); no file is read."""
    text = str(ref)
    if text.endswith(".parquet"):
        p = Path(text)
        key = text if not p.is_absolute() else StoreLayout(root).key(p)
        try:
            f = parse_data_key(key)
        except ValueError as exc:
            raise ChainError(str(exc)) from None
        if f.series is None:
            from stack.store.index import store_index

            ex = store_index(root).excluded()
            hits = [h for h in store_index(root).data_outputs() if h[2]["path"] == f.key]
            if not hits or hits[0][2]["file_id"] in ex:
                raise EmptyChainError(f"{f.key}: not published (no ok run record lists it, or it is excluded)")
            cf = _output_file(store_index(root), *hits[0])
            return Chain(f.family_dir, f.family, (cf,), dir=None, last="")
        files = dir_files(root, f.dir)
        at = [i for i, cf in enumerate(files) if cf.name == f.name]
        if not at:
            raise EmptyChainError(f"{f.key}: not published (no ok run record lists it, or it is excluded)")
        return Chain(f.family_dir, f.family, tuple(_walk(f.dir, files, at[0])), dir=f.dir, last=files[-1].name)
    try:
        fam_dir, series = parse_dir_ref(text)
    except ValueError as exc:
        raise ChainError(str(exc)) from None
    d = f"{DATA}/{fam_dir}/{series}"
    files = dir_files(root, d)
    if not files:
        raise EmptyChainError(f"{d}/: no published file")
    return Chain(fam_dir, files[-1].family, tuple(_walk(d, files, len(files) - 1)), dir=d, last=files[-1].name)


def resolve(store_root: str | Path, ref: str | Path, *, verify_ids: bool = True) -> Chain:
    """The version ``ref`` names (a series directory ``data/<family>/<series>/``: its last
    published file; an exact file key or path: that file), its files read (downloaded when
    absent) and checked. Raises :class:`EmptyChainError`, :class:`MissedFileError`,
    :class:`ForkError`, :class:`FileFormatError`, :class:`NoFullFileError`,
    :class:`ChainConsistencyError` or :class:`ChainError` (a reference outside the grammar, a
    family directory without a series). ``verify_ids`` also reads each version file's key columns
    (uniqueness, ``ids_digest``)."""
    return _load(_resolve_ref(Path(store_root), ref), verify_ids=verify_ids)


def resolve_records(store_root: str | Path, ref: str | Path) -> Chain:
    """:func:`resolve` from the run records alone: no file is read or downloaded; each
    :class:`ChainFile` carries its record ``output`` and the record's keys (``versions`` empty)."""
    return _resolve_ref(Path(store_root), ref)


def head_of(store_root: str | Path, dir_key: str) -> str:
    """The directory's last published file name from the records alone, ``''`` when empty."""
    cf = last_file(store_root, dir_key)
    return "" if cf is None else cf.name


# ---------------------------------------------------------------- union and select
def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _keys_table(spec: FamilySpec, keys: object) -> pa.Table:
    """``read_view``'s ``keys`` as a table of the key columns: a table holding them, an array of
    a one-column key's values, or an iterable of values (one-column key) or of tuples."""
    cols = list(spec.key)
    if isinstance(keys, pa.Table):
        return keys.select(cols)
    types = [spec.schema.field(k).type for k in cols]
    if len(cols) == 1:
        arr = keys if isinstance(keys, pa.Array | pa.ChunkedArray) else pa.array(list(keys), types[0])  # type: ignore[call-overload]
        return pa.table({cols[0]: arr})
    rows = list(keys)  # type: ignore[call-overload]
    return pa.table({k: pa.array([r[i] for r in rows], t) for i, (k, t) in enumerate(zip(cols, types, strict=True))})


def read_view(chain: Chain, *, columns: Sequence[str] | None = None, keys: object = None) -> pa.Table:
    """Union-and-select by chain position (§7.3): per key, the row of the newest file in the
    chain; ordered by key, in the family's columns and current schema (``schema_equal``
    asserted). Every file is read through :func:`stack.store.parquet.read_family`, so a file of an
    older format version reads upcast to the current one (D295); one that does not read raises
    its :class:`~stack.store.parquet.SchemaMismatch`.

    ``columns`` (the store checks' projected reads): only those columns and the key columns, in
    registry order, read from each file (the reader's projected read), the projected schema
    asserted. A column may be a dotted path into a ``struct`` / ``list<struct>`` column
    (``identity.members_digest``, ``identity.members.source_id``;
    :func:`stack.store.parquet.field_paths`): the view then holds those columns narrowed to the
    named children (:func:`stack.store.parquet.project_schema`), each file read one column at a
    time and narrowed (:func:`stack.store.parquet.read_family_fields`), so the memory held is
    proportional to the projected fields. ``keys``: only the rows whose key
    is in the given set (a table with the key columns, an array of a one-column key's values,
    or an iterable of values or key tuples); the view filtered, in key order. Both default to
    the whole view.

    duckdb runs §7.3's window over ``read_parquet(paths, filename := true, union_by_name :=
    true)`` joined to the position table, but selects ``(pos, file_row_number)`` rather than the
    rows: it then reads only the key columns (which never change across format versions, so the
    raw files can be windowed), and the rows are taken from the files with pyarrow, so the view
    is byte-identical to what was written (at the current version). (Rows that came back through duckdb carry NULLs in
    non-nullable children of a NULL struct, e.g. ``content`` before ``acquire``, which the parquet
    writer refuses; pyarrow fills those slots with empty values.)"""
    spec = chain.spec
    cols: list[str] | None = None
    if columns is not None:
        try:
            cols = list(field_paths(spec.schema, [*columns, *spec.key]))
        except ValueError as exc:
            raise ValueError(f"chain {chain.chain_id!r}: no column(s) in {spec.name}: {exc}") from None
    target = spec.schema if cols is None else project_schema(spec.schema, cols)
    if not chain.files:
        return target.empty_table()
    if keys is None and len(chain.files) == 1 and len(spec.key) == 1:
        # one full file (every series written in full files): the view is the file in key order;
        # read once, no row selection, when its keys already ascend (else the general path)
        one = read_family(chain.files[0].path) if cols is None else read_family_fields(chain.files[0].path, cols)
        tbl = one[0]
        k = tbl.column(spec.key[0])
        if len(k) < 2 or pc.all(pc.greater(k.slice(1), k.slice(0, len(k) - 1))).as_py():
            out = tbl.combine_chunks()
            if cols is None:
                findings = schema_equal(out, {}, spec)
                if findings:
                    raise ChainError(f"chain {chain.chain_id!r}: view schema differs from the registry: "
                                     f"{findings[0].items}")
            elif not out.schema.equals(target, check_metadata=False):
                raise ChainError(f"chain {chain.chain_id!r}: projected view schema differs from the registry's "
                                 f"{cols}: {out.schema} <> {target}")
            return out
        del tbl, k, one
    paths = [str(p) for p in chain.paths]
    positions = pa.table({"path": pa.array(paths, pa.string()),
                          "pos": pa.array(range(len(paths)), pa.int32())})
    key = ", ".join(_ident(k) for k in spec.key)
    where = "" if keys is None else (
        "WHERE EXISTS (SELECT 1 FROM view_keys k WHERE "
        + " AND ".join(f"k.{_ident(k)} = d.{_ident(k)}" for k in spec.key) + ")")
    sql = f"""
        SELECT c.pos, d.file_row_number AS row
        FROM read_parquet($paths, filename := true, file_row_number := true, union_by_name := true) d
        JOIN chain c ON d.filename = c.path
        {where}
        QUALIFY row_number() OVER (PARTITION BY {key} ORDER BY c.pos DESC) = 1
        ORDER BY {key}"""
    con = duck.connect()
    try:
        con.execute("SET TimeZone = 'UTC'")
        con.register("chain", positions)
        if keys is not None:
            con.register("view_keys", _keys_table(spec, keys))
        sel = con.execute(sql, {"paths": paths}).to_arrow_table()
    finally:
        con.close()
    # Take each file's winning rows (grouped by file), then restore the key order.
    order = pc.sort_indices(sel, [("pos", "ascending")])  # stable
    grouped = sel.take(order)
    parts = []
    for i, cf in enumerate(chain.files):
        rows = grouped.filter(pc.equal(grouped["pos"], i))["row"]
        if len(rows):
            parts.append((read_family(cf.path) if cols is None else read_family_fields(cf.path, cols))[0].take(rows))
    taken = pa.concat_tables(parts) if parts else target.empty_table()
    out = taken.take(pc.sort_indices(order)).combine_chunks()
    if cols is None:
        findings = schema_equal(out, {}, spec)
        if findings:
            raise ChainError(f"chain {chain.chain_id!r}: view schema differs from the registry: "
                             f"{findings[0].items}")
    elif not out.schema.equals(target, check_metadata=False):
        raise ChainError(f"chain {chain.chain_id!r}: projected view schema differs from the registry's "
                         f"{cols}: {out.schema} <> {target}")
    return out


# ---------------------------------------------------------------- write: stage, claim, publish
@dataclass(frozen=True)
class HeaderFields:
    """The header keys the writing stage owns (A.6); the chain writer fills the rest
    (``kind``, ``base``, ``after``, ``chain_id``, digests, ``row_count``, ``finished_at``)."""

    stage: str
    run_id: str
    config_digest: str
    code_digest: str
    writer: str | None = None  # defaults to ``stage``
    plan: str = NONE
    inputs: Mapping[str, object] = field(default_factory=dict)
    prior: Sequence[str] = ()
    only: Sequence[str] | None = None
    scope_complete: bool = True
    blocks_written: Sequence[str] | str = WILDCARD
    basis_kind: str = NONE
    fold_config: str = NONE
    filter: str = NONE
    annotations_folded: Sequence[str] | None = None
    release_config: str = NONE


@dataclass(frozen=True)
class StagedFile:
    """A file written as ``data/<family>[/<series>]/.<name>.tmp`` and not yet published."""

    store_root: Path
    path: Path
    header: Header
    file_digest: str
    dest: DataFile

    @property
    def chain_id(self) -> str:
        return self.dest.family_dir

    @property
    def dir(self) -> str:
        return self.dest.dir

    @property
    def file_id(self) -> str:
        return self.header["file_id"]

    @property
    def base(self) -> str:
        return self.header["base"]

    @property
    def after(self) -> str:
        return self.header["after"]

    @property
    def run_id(self) -> str:
        return self.header["run_id"]

    @property
    def family(self) -> str:
        return self.header["family"]

    @property
    def name(self) -> str:
        return self.dest.name

    @property
    def data_path(self) -> Path:
        return self.store_root / self.dest.key


@dataclass(frozen=True)
class Published:
    """A file renamed into place; :meth:`as_output` is the run record's ``outputs`` entry."""

    staged: StagedFile
    path: Path

    @property
    def file_id(self) -> str:
        return self.staged.file_id

    def as_output(self) -> OutputInfo:
        h = self.staged.header
        return {"file_id": h["file_id"], "path": self.staged.dest.key,
                "file_digest": self.staged.file_digest, "row_count": int(h["row_count"]),
                "schema_digest": h["schema_digest"], "family_version": int(h["family_version"]),
                "chain_id": h["chain_id"],
                "series": h["series"], "kind": h["kind"], "base": h["base"], "after": h["after"],
                "comment": h["comment"]}


def stage_file(store_root: str | Path, dest: DataFile, table: pa.Table, fields: HeaderFields, *,
               base: str = "", after: str = "", finished_at: dt.datetime | None = None) -> StagedFile:
    """Write ``table`` as ``<dest dir>/.<name>.tmp`` with a complete header through the family
    writer (S1-S7 refuse it there). ``base``: the file of the destination directory the run read
    (empty: a full without a base); ``after``: the directory's last published file when the run
    pinned its inputs (D372). A part needs a series and a base; a base equals
    ``after``."""
    root = Path(store_root)
    spec = family(dest.family)
    if fields.run_id != dest.run_id:
        raise ValueError(f"{dest.key}: run {fields.run_id} does not write another run's file")
    if dest.kind == "part" and not base:
        raise ValueError(f"{dest.key}: a part file extends a base; none given")
    if dest.kind == "part" and spec.kind in CHAIN_OF_ONE_KINDS:
        raise ValueError(f"{dest.key}: {dest.family} files are always full (D14)")
    if base and base != after:
        raise ValueError(f"{dest.key}: base {base!r} is not the directory's last file {after!r}")
    if dest.series is None and (base or after):
        raise ValueError(f"{dest.key}: a file without a series has no base")
    writer = fields.writer or fields.stage
    header = build_header(
        table, dest.family, stage=fields.stage, writer=writer, run_id=fields.run_id,
        config_digest=fields.config_digest, finished_at=finished_at or dt.datetime.now(dt.UTC),
        code_digest=fields.code_digest, kind=dest.kind, chain_id=dest.family_dir,
        blocks_written=fields.blocks_written, plan=fields.plan, inputs=fields.inputs,
        prior=fields.prior, only=fields.only, scope_complete=fields.scope_complete,
        basis_kind=fields.basis_kind, fold_config=fields.fold_config, filter=fields.filter,
        annotations_folded=fields.annotations_folded, release_config=fields.release_config,
        series=dest.series, comment=dest.comment, base=base, after=after)
    path = write_family(StoreLayout(root).staged_file(dest), table, header)
    return StagedFile(root, path, header, file_digest(path), dest)


def claim(store_root: str | Path, staged: StagedFile, *, refresh: bool = True,
          own_committed: bool = False) -> bool:
    """The publish rule for one staged file (D372) against the store index (a
    mirror re-LISTs S3 with ``refresh``; an uploading job's index counts uploaded records only,
    D353): the destination directory's last published file must be the file's ``after``
    (:class:`HeadMovedError`) and the name must sort after it (:class:`NameOrderError`). Returns
    ``False`` when the directory's last file is this run's own output and ``own_committed``
    (already committed: a lost acknowledgement), ``True`` otherwise. A part is also resolved as
    it would be once published (its version's files read: format and key checks before
    publishing). A file without a series only must not exist yet."""
    from stack.store.index import store_index

    root = Path(store_root)
    if refresh:
        store_index(root, refresh=True)
    if staged.dest.series is None:
        return True
    files = dir_files(root, staged.dir)
    last = files[-1] if files else None
    last_name = last.name if last is not None else ""
    if last is not None and last.run_id == staged.run_id and own_committed:
        return False
    if last_name != staged.after:
        raise HeadMovedError(f"{staged.dir}/: the run pinned it at {staged.after or 'an empty directory'}, "
                             f"but its last file is now {last_name or 'none'}; refusing to publish {staged.name}")
    if last_name and staged.name <= last_name:
        raise NameOrderError(f"{staged.dir}/: {staged.name} does not sort after the last file {last_name}; "
                             "refusing to publish")
    if staged.dest.kind == "part":
        cand = _read_chain_file(staged.path)[0]
        version = Chain(staged.chain_id, staged.family, tuple(_walk(staged.dir, [*files, cand], len(files))),
                        dir=staged.dir, last=staged.name)
        _load(version, verify_ids=True)
    return True


def _move_no_clobber(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.link(src, dst)  # refuses an existing target
    src.unlink()


#: Waits (seconds) before each repeat of a publish's data PUT after a transient S3 failure
#: (:func:`stack.store.s3config.is_s3_transient`; each attempt already carries the client's own
#: retries). The PUT is create-only and a repeat that finds the same bytes is a no-op
#: (:meth:`stack.store.remote.Store.push`), so repeating it is safe (S25-47).
DATA_PUT_RETRY_DELAYS: tuple[float, ...] = (5.0, 15.0, 45.0)


def _sleep(seconds: float) -> None:
    import time

    time.sleep(seconds)


def _push_data(store: Any, key: str) -> None:
    """``store.push(key, create_only=True)``, repeated after a transient S3 failure
    (:data:`DATA_PUT_RETRY_DELAYS`); the last failure, or any other, raises."""
    from stack.store.job import say
    from stack.store.s3config import is_s3_transient, redact_error

    for delay in (*DATA_PUT_RETRY_DELAYS, None):
        try:
            store.push(key, create_only=True)
            return
        except Exception as exc:
            if delay is None or not is_s3_transient(exc):
                raise
            say(f"warning: data PUT {key} failed ({redact_error(exc)}); retrying in {delay:g} s")
            _sleep(delay)


def publish(staged: StagedFile | Sequence[StagedFile],
            on_published: Callable[[list[Published]], None] | None = None) -> list[Published]:
    """Publish one run's staged files (D355, D372): the active job's cancel check
    (:meth:`stack.store.job.Job.check_cancel`, before anything is claimed; never after), the
    publish rule per file (:func:`claim`) against a refreshed index, the renames into place
    without clobbering, the data PUTs with ``If-None-Match: *`` when the active job uploads (a
    transient S3 failure repeated, :data:`DATA_PUT_RETRY_DELAYS`), then
    ``on_published`` (the runner writes the final run record there: the commit, D345). If a claim
    fails nothing moves and the staged files stay (garbage); a failure after the rename leaves
    files no ``ok`` record lists (garbage; on S3 invisible)."""
    from stack.store.index import store_index
    from stack.store.job import current_job, maybe_die

    items = [staged] if isinstance(staged, StagedFile) else list(staged)
    if not items:
        return []
    keys = [s.dest.key for s in items]
    if len({s.dir for s in items}) != len(items):
        raise ValueError(f"publish: two staged files for one directory: {keys}")
    roots = {s.store_root for s in items}
    if len(roots) != 1:
        raise ValueError(f"publish: staged files from several stores {sorted(map(str, roots))}")
    root = roots.pop()
    if len({s.run_id for s in items}) != 1:
        raise ValueError(f"publish: staged files of several runs {sorted({s.run_id for s in items})}")
    job = current_job()
    if job is not None:
        job.check_cancel()
    store_index(root, refresh=True)
    for s in items:
        claim(root, s, refresh=False)
    maybe_die("after_claim")
    done: list[Published] = []
    for s in items:
        _move_no_clobber(s.path, s.data_path)
        done.append(Published(s, s.data_path))
    maybe_die("after_rename")
    if job is not None and job.upload and job.store.root.resolve() == Path(root).resolve():
        for p in done:
            _push_data(job.store, job.store.key(p.path))
    maybe_die("after_data_put")
    if on_published is not None:
        on_published(done)
    return done


def write_file(store_root: str | Path, dest: DataFile, table: pa.Table, fields: HeaderFields, *,
               base: str = "", after: str = "", finished_at: dt.datetime | None = None,
               on_published: Callable[[list[Published]], None] | None = None) -> Published:
    """Stage and publish one file (:func:`stage_file`, :func:`publish`)."""
    s = stage_file(store_root, dest, table, fields, base=base, after=after, finished_at=finished_at)
    return publish(s, on_published)[0]


# ---------------------------------------------------------------- recovery
#: The stage name of a ``chain orphan`` record.
ORPHAN_STAGE = "chain.orphan"


def orphan(store_root: str | Path, ref: str | Path, *, force: bool = False, comment: str | None = None,
           note: str | None = None, upload: bool | None = None) -> Path:
    """Take the published file ``ref`` (a store-relative path, D375) out of its
    directory (§7.6: fork and missed-file recovery; the verb is ``stack chain orphan``): write a
    standalone ``ok`` run record of stage ``chain.orphan`` whose ``excludes`` is the file's id
    (D345; nothing is moved or deleted), uploaded on an S3 store. Returns the record's path. Guard
    (R2-9): without ``force`` only the directory's last file, or a file of a directory that does
    not resolve (a fork, a missed file), is taken; a middle file is refused. A file no ``ok`` record
    lists, or an excluded one, is refused. ``note``: free text (why), recorded."""
    from stack.core import ids
    from stack.stage_runner import code_digest
    from stack.store.job import RunJob, default_upload, prepare, say
    from stack.store.remote import open_store

    root = Path(store_root)
    upload = default_upload(open_store(root), upload)
    idx = prepare(root, upload)
    text = str(ref)
    key = StoreLayout(root).key(text) if Path(text).is_absolute() else text
    try:
        f = parse_data_key(key)
    except ValueError as exc:
        raise ChainError(f"orphan: {exc}") from None
    hits = [h for h in idx.data_outputs() if h[2]["path"] == f.key]
    if not hits:
        raise ChainError(f"orphan: no ok run record lists {f.key}; a data file no record lists is in no "
                         "chain (garbage), so there is nothing to take out")
    e, k, out = hits[0]
    fid = out["file_id"]
    if fid in idx.excluded():
        raise ChainError(f"orphan: {f.key} is excluded already")
    if not force and f.series is not None:
        last = head_of(root, f.dir)
        try:
            _resolve_ref(root, f.dir)
            ok_chain = True
        except ChainError:
            ok_chain = False  # a fork or a missed file: the recovery orphan exists for
        if ok_chain and last != f.name:
            raise ChainError(f"orphan: {f.name} is not the last file of {f.dir}/ (last {last}); refusing "
                             "(`--force` to take it anyway)")
    now = dt.datetime.now(dt.UTC)
    rec = RunRecord(run_id=ids.mint("run"), stage=ORPHAN_STAGE, writer=ORPHAN_STAGE, plan=NONE, status="running",
                    started_at=now, heartbeat_at=now, code_digest=code_digest(),
                    config_digest=NONE, scope_complete=True, excludes=[fid], comment=comment, note=note,
                    inputs={k.split("/", 1)[0]: fid})
    job = RunJob(root, rec, upload=upload)
    job.start()
    with job.lock:
        rec.status, rec.finished_at = "ok", dt.datetime.now(dt.UTC)
    for note in job.finish_record():
        say(f"warning: {note}")
    return job.json_path


__all__ = [
    "CHAIN_OF_ONE_KINDS",
    "ORPHAN_STAGE",
    "Chain",
    "ChainConsistencyError",
    "ChainError",
    "ChainFile",
    "EmptyChainError",
    "FileFormatError",
    "ForkError",
    "HeadMovedError",
    "HeaderFields",
    "MissedFileError",
    "NameOrderError",
    "NoFullFileError",
    "Published",
    "StagedFile",
    "claim",
    "dir_files",
    "head_of",
    "last_file",
    "orphan",
    "publish",
    "read_view",
    "resolve",
    "resolve_records",
    "stage_file",
    "write_file",
]
