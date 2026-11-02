"""The store as store-scope checks see it (:class:`StoreView`), and the chain checks V11, V12,
X1, X10, X22, X23 (Appendix B, §7.2-7.5, D14, D20, D38).

A store-scope check is ``(view: StoreView) -> list[Finding]``; it may raise :class:`SkipCheck`
(e.g. ``no_builds``) and :func:`~stack.checks.verify.run_checks` reports ``skipped[<why>]``.

**Reads** (round 5, S-17's scale). Whether a file reads (as is, upcast, or not at all, as
:func:`stack.store.parquet.file_format` decides) is decided once from its footer
(:meth:`StoreView.schema_status`), never from its rows. :meth:`StoreView.table` and :meth:`StoreView.view_at` take ``columns`` (only those are
read) and the view ``keys`` (only those rows); their results are cached by ``(file, columns)`` /
``(file, columns)`` of the view in an LRU bounded by :data:`CACHE_BYTES`, which ``run_checks``
empties after every store-scope check. The pair checks walk a file against its base view
(:meth:`StoreView.base_pairs`, :meth:`StoreView.pair_files`): the base view restricted to the
file's keys, both projected, converted to Python :data:`BATCH_ROWS` rows at a time.

**Full and parts** (:mod:`stack.checks.verify`). A full verify hashes every local copy of a recorded
data file against its record (:class:`StoreView`, X1) and every check sees every file. Otherwise
(``rehash=False``) a local copy is trusted without hashing (X1 still compares its footer's row count
with the record's and, on a mirror, its size with the store object's), and :attr:`StoreView.scope`
(the store keys of the files a build's or a run's records name) restricts the checks that judge one
file at a time: :meth:`StoreView.pair_files` and V7/V8 (:meth:`StoreView.in_scope`).

**Base views.** X15, X17, X22, X24 and V12 compare a chain file with its **base view**: the
union-and-select (§7.3, :func:`stack.store.chain.read_view`) of the files from the newest full
file at or before the file's base up to that base, found by walking ``base_file_id`` back over
every recorded file of the chain, superseded ones included. A walk that meets a file no ``ok``
record names is a finding of the check. (``stack gc``'s tombstones, D79/D89, are no longer read:
data is never deleted, D356; D89's ``skipped[base_collected]`` is gone with them.) A file whose
chain cannot be resolved at all is V11's finding, and the view-based checks (X4, X23) skip that chain rather
than report one fault many times. Chains of one (``corpus``, ``<wf>``, ``<wf>_evidence``, D14)
have no base: V11, X15, X22 do not apply to them.

**Format versions** (D295; amends D43, D208 (4)). Every file is read through the one family
reader (:func:`stack.store.parquet.read_family`): a file of an older format version of its family
reads **upcast** to the current one by :meth:`StoreView.table`, by the base views and by the
file-scope checks (:mod:`stack.checks.verify`), so a chain of mixed versions, a compaction on top of
an older version (V12, X22, X17, X24, X15) and an older build's files all read. A file that does not
read (newer, unknown, its digest not its declared version's, behind an ``Incompatible`` step)
reads as ``None`` (S1's finding; V11's when it is in a chain's version).
"""

from __future__ import annotations

import json
from collections import OrderedDict, defaultdict
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc

from stack.checks import Finding, register
from stack.runs import (
    files_without_ok_record,
    newest_finished_build,
    read_build_record,
    records_without_file,
)
from stack.schemas.families import FAMILIES, FamilySpec
from stack.schemas.upcasters import UpcastRefused
from stack.store import chain as store_chain
from stack.store.index import OutputDigestError, local_digest, store_index
from stack.store.layout import DATA, NONE, parse_data_name
from stack.store.parquet import (
    SchemaMismatch,
    field_paths,
    file_format,
    read_family,
    read_family_fields,
    read_footer,
)

Header = dict[str, str]
_EXAMPLES = 5
#: Build-record keys of chained families (D17): ``<family>`` or ``<family>/<chain_id>``.
CHAINED_KINDS: frozenset[str] = frozenset({"documents", "sources", "coverage", "overrides"})
#: Bound of :class:`StoreView`'s table and view cache within one check (``pa.Table.nbytes``); a
#: result larger than this is not cached.
CACHE_BYTES = 1 << 30
#: Rows converted to Python at a time by the pair walks and the one-pass checks.
BATCH_ROWS = 4096
#: How a file reads: at its family's current format, an older version upcast on read, or (``None``)
#: not at all (:meth:`StoreView.schema_status`).
SAME, UPCAST = "same", "upcast"
#: The pair checks (a chain file against its base view, :meth:`StoreView.pair_files`) by code.
PAIR_CHECKS: tuple[str, ...] = ("V12", "X13", "X15", "X17", "X19", "X22", "X24")


@dataclass(frozen=True)
class BaseJoin:
    """A chain file joined with its base view (:meth:`StoreView.base_join`): ``cur`` the file's
    rows, ``base`` the base view's row of the same ``doc_id`` at the same position."""

    cur: pa.Table
    base: pa.Table


class SkipCheck(Exception):
    """A check cannot run on this store; ``args[0]`` is the reason (``no_builds``)."""


def _finding(check: str, message: str, bad: list[str]) -> list[Finding]:
    return [Finding(check, "hard", message.format(n=len(bad)), len(bad), tuple(bad[:_EXAMPLES]))] \
        if bad else []


def base_gap(view: StoreView, df: DataFile, bad: list[str]) -> None:
    """For a file whose base view is ``None``: a base walk that leaves the recorded files goes to
    ``bad``; nothing when the walk is whole (the view is ``None`` for another reason, e.g. S1's
    schema finding)."""
    gap = view.base_gap(df)
    if gap is not None:
        bad.append(f"{df.file_id}: {gap}")


# ---------------------------------------------------------------- the view
def as_family(path: str | Path) -> pa.Table | None:
    """The rows of the ``data/`` file at ``path`` at its family's current format version
    (:func:`stack.store.parquet.read_family`: as is, or an older version upcast), or ``None`` when
    the file does not read (:class:`~stack.store.parquet.SchemaMismatch`, or a step refused its data:
    :class:`~stack.schemas.upcasters.UpcastRefused`; S1 reports both)."""
    try:
        return read_family(path)[0]
    except (SchemaMismatch, UpcastRefused):
        return None


def _header(path: Path) -> Header:
    """The ``stack.*`` header keys of a data file, from its footer
    (:func:`stack.store.parquet.read_footer`)."""
    return read_footer(path).header


@dataclass(frozen=True)
class DataFile:
    """One ``data/*.parquet`` file: path, ``stack.*`` header (prefix stripped), and its family
    spec when the header names a registered family."""

    path: Path
    header: Header

    @property
    def file_id(self) -> str:
        return self.header.get("file_id", self.path.name)

    @property
    def key(self) -> str:
        """The store key ``data/<family>[/<series>]/<name>``, the label findings use: files of one
        run in different family directories share a name (D381)."""
        parts = self.path.parts
        i = len(parts) - 1 - parts[::-1].index(DATA) if DATA in parts else len(parts) - 1
        return "/".join(parts[i:])

    @property
    def family(self) -> str | None:
        return self.header.get("family")

    @property
    def spec(self) -> FamilySpec | None:
        return FAMILIES.get(self.family or "")

    @property
    def chain_id(self) -> str | None:
        return self.header.get("chain_id")

    @property
    def base_file_id(self) -> str:
        """The base's file id, derived from ``stack.base`` (its name in the directory), ``none``
        for a full without a base."""
        base = self.header.get("base", "")
        return f"{parse_data_name(base)[1]}.{self.family}" if base else NONE

    @property
    def is_full(self) -> bool:
        return self.header.get("kind") == "full"

    @property
    def is_chain_of_one(self) -> bool:
        spec = self.spec
        return spec is not None and spec.kind in store_chain.CHAIN_OF_ONE_KINDS

    @property
    def blocks_written(self) -> list[str] | None:
        """The JSON list of a chain stage's file, ``None`` for ``'*'`` (all blocks) or junk."""
        try:
            v = json.loads(self.header.get("blocks_written", "*"))
        except ValueError:
            return None
        return v if isinstance(v, list) else None


def last_build(store_root: str | Path, series: str | None = None) -> dict[str, Any] | None:
    """The newest finished (``ok`` or ``incomplete``) build record (the store index; of ``series``
    when given: the record's ``series``, D373) by ``finished_at`` (:func:`stack.runs.newest_finished_build`,
    D80, D652)."""
    hit = newest_finished_build(store_root, series=series)
    return hit[1] if hit is not None else None


class MixedSeriesError(ValueError):
    """``verify --in`` directories of more than one series with no ``--build``: no one reference build."""


def dirs_series(dirs: Sequence[str]) -> str:
    """The one series of the series directory keys ``dirs`` (``data/<family>/<series>``);
    :class:`MixedSeriesError` when they span several (S25-39)."""
    series = sorted({d.strip("/").split("/")[2] for d in dirs})
    if len(series) != 1:
        raise MixedSeriesError(f"--in directories of several series {series}: the reference build (X2, X10, "
                               "X14) is per series; check one series at a time or pass --build")
    return series[0]


class StoreView:
    """The store's recorded data files (every output of an ``ok`` run record, minus the files an
    ``excludes`` took out; D345; downloaded when absent, headers read once), their chains, base
    views, the reference build, and the family filter the checks honour. Tables and views are
    cached per check (module docstring, :meth:`drop_cache`)."""

    def __init__(self, store_root: str | Path, *, families: list[str] | None = None,
                 dirs: Sequence[str] | None = None,
                 build: str | Path | Mapping[str, Any] | None = None, rehash: bool = True) -> None:
        self.root = Path(store_root)
        #: Full verify: every local copy of a recorded file is hashed against its record (X1 too).
        self.rehash = rehash
        #: The files part of a verify (``run_checks`` sets it): the store keys of the files a build or runs
        #: wrote, the only files the per-file store checks (:meth:`in_scope`) look at; ``None``: every file.
        self.scope: frozenset[str] | None = None
        self.families = set(families) if families is not None else None
        #: ``verify --in``: the series directory keys (``data/<family>/<series>``) checked; ``None`` = all.
        self.dirs = {d.strip("/") for d in dirs} if dirs is not None else None
        self.all_files: list[DataFile] = []
        #: ``<file_id>: <error>`` for each recorded file of a mirror that no copy (local, then
        #: downloaded) has with its ``file_digest`` (D371); the file is left out of the view and X1
        #: reports it.
        self.digest_errors: list[str] = []
        idx = store_index(self.root)
        excluded = idx.excluded()
        outs = sorted((o for _, _, o in idx.data_outputs() if o["file_id"] not in excluded), key=lambda o: o["path"])
        #: Store path -> the record's ``file_digest`` of every published file (X2's pins, D380).
        self.published: dict[str, str] = {o["path"]: o["file_digest"] for o in outs}
        for o in outs:
            p = idx.store.path(o["path"])
            if rehash or not p.is_file():  # not full: a local copy is trusted (X1 checks its size and rows)
                try:
                    p = idx.fetch_output(o)
                except FileNotFoundError:
                    continue  # a lost recorded file: X1's finding (records_without_file)
                except OutputDigestError as exc:
                    if idx.store.remote is not None:  # no copy has the record's digest: left out, X1 reports it
                        self.digest_errors.append(f"{o['file_id']}: no copy has its record's file_digest ({exc})")
                        continue
                    # an altered local-only file: checked as it is, X1 reports it
            try:
                hdr = _header(p)
            except (pa.ArrowException, OSError):
                continue  # a footer that does not read: left out of the view, X1 reports it
            self.all_files.append(DataFile(p, hdr))
        if build is None:  # the newest finished build (verify --in: of those directories' series, S25-39)
            self.build = last_build(self.root, dirs_series(list(self.dirs)) if self.dirs else None)
        elif isinstance(build, Mapping):
            self.build = dict(build)
        else:
            self.build = read_build_record(build)
        self._by_id: dict[str, DataFile] = {}
        for df in self.all_files:
            self._by_id.setdefault(df.file_id, df)
        self._status: dict[Path, str | None] = {}
        self._cache: OrderedDict[tuple[Any, ...], pa.Table | None] = OrderedDict()
        self._cache_bytes = 0
        self._resolved: dict[str, store_chain.Chain | store_chain.ChainError] = {}

    # -- selection
    def selected(self, df: DataFile) -> bool:
        if self.dirs is not None and df.path.parent.relative_to(self.root).as_posix() not in self.dirs:
            return False
        return self.families is None or df.family in self.families

    def in_scope(self, df: DataFile) -> bool:
        """Whether a per-file check looks at ``df`` (:attr:`scope`)."""
        return self.scope is None or df.key in self.scope

    @property
    def files(self) -> list[DataFile]:
        """The files of the selected families."""
        return [df for df in self.all_files if self.selected(df)]

    def file(self, fid: str) -> DataFile | None:
        return self._by_id.get(fid)

    def chains(self) -> dict[str, list[DataFile]]:
        """Series directory key (``data/<family>/<series>``, a chain, D372) → files, for the
        selected families (chains of one and files without a series excluded)."""
        out: dict[str, list[DataFile]] = defaultdict(list)
        for df in self.files:
            if df.header.get("series") and not df.is_chain_of_one:
                out[df.path.parent.relative_to(self.root).as_posix()].append(df)
        return dict(sorted(out.items()))

    def resolve(self, chain_id: str) -> store_chain.Chain | store_chain.ChainError:
        """The resolved chain of a series directory key (:meth:`chains`), or the resolver's error
        (V11 reports it; others skip)."""
        if chain_id not in self._resolved:
            try:
                self._resolved[chain_id] = store_chain.resolve(self.root, chain_id)
            except store_chain.ChainError as exc:
                self._resolved[chain_id] = exc
        return self._resolved[chain_id]

    # -- the cache
    def _cached(self, key: tuple[Any, ...], make: Any) -> pa.Table | None:
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        val = make()
        size = val.nbytes if val is not None else 0
        if size <= CACHE_BYTES:
            self._cache[key] = val
            self._cache_bytes += size
            while self._cache_bytes > CACHE_BYTES:
                _, old = self._cache.popitem(last=False)
                self._cache_bytes -= old.nbytes if old is not None else 0
        return val

    def drop_cache(self) -> None:
        """Forget every cached table and view (``run_checks`` calls it after each store-scope
        check); headers, schema status, resolved chains and tombstones stay."""
        self._cache.clear()
        self._cache_bytes = 0
        pa.default_memory_pool().release_unused()  # hand the freed Arrow memory back to the OS

    @property
    def cache_bytes(self) -> int:
        """Bytes (``pa.Table.nbytes``) the cache holds now."""
        return self._cache_bytes

    # -- tables
    def schema_status(self, df: DataFile) -> str | None:
        """How the file reads, from its footer only (:func:`stack.store.parquet.file_format`):
        :data:`SAME` (its family's current format), :data:`UPCAST` (an older format version,
        upcast on read) or ``None`` (no family, or the file does not read)."""
        if df.path not in self._status:
            got: str | None = None
            if df.spec is not None:
                try:
                    got = SAME if file_format(df.path, df.header).is_current else UPCAST
                except SchemaMismatch:
                    got = None
            self._status[df.path] = got
        return self._status[df.path]

    def readable(self, df: DataFile) -> bool:
        """Whether :meth:`table` gives the file's rows (not ``None``), without reading them."""
        return self.schema_status(df) is not None

    def _columns(self, spec: FamilySpec, columns: Sequence[str] | None) -> tuple[str, ...] | None:
        """``columns`` in registry order (``None``: all); an unknown name raises. A column may be
        a dotted path into a block (:func:`stack.store.parquet.field_paths`): the read is then
        narrowed to the named children (:meth:`base_join`)."""
        if columns is None:
            return None
        try:
            return field_paths(spec.schema, list(columns))
        except ValueError as exc:
            raise ValueError(f"no column(s) in {spec.name}: {exc}") from None

    def read(self, df: DataFile, columns: Sequence[str] | None = None) -> pa.Table | None:
        """:meth:`table` without the cache."""
        spec = df.spec
        if spec is None or self.schema_status(df) is None:
            return None
        cols = self._columns(spec, columns)
        try:
            return (read_family(df.path) if cols is None else read_family_fields(df.path, cols))[0]
        except UpcastRefused:  # its footer reads, its data does not (S1's finding): unreadable from now on
            self._status[df.path] = None
            return None

    def table(self, df: DataFile, columns: Sequence[str] | None = None) -> pa.Table | None:
        """The file's rows (metadata stripped) at its family's current format version: an older
        version upcast (module docstring); ``None`` when the file does not read (S1's finding;
        nothing downstream reads such a file). ``columns``: only those (registry order). Cached
        (module docstring)."""
        spec = df.spec
        cols = self._columns(spec, columns) if spec is not None else None
        return self._cached(("table", df.path, cols), lambda: self.read(df, cols))

    def walk_back(self, fid: str) -> list[DataFile] | None:
        """The files from the newest full file at or before ``fid`` up to ``fid``, in chain
        order; ``None`` when a base on the way is not in ``data/`` or the walk cycles."""
        out: list[DataFile] = []
        seen: set[str] = set()
        cur = self.file(fid)
        while cur is not None and cur.file_id not in seen:
            seen.add(cur.file_id)
            out.append(cur)
            if cur.is_full:
                return out[::-1]
            if cur.base_file_id == NONE:
                return None
            cur = self.file(cur.base_file_id)
        return None

    def has_view(self, fid: str) -> bool:
        """Whether the walk back from ``fid`` stays in ``data/`` and every file on it reads
        (:meth:`view_at` is then ``None`` only if the reader refuses the union)."""
        files = self.walk_back(fid)
        return bool(files) and all(self.readable(f) for f in files or [])

    def _read_view(self, fid: str, columns: tuple[str, ...] | None, keys: object) -> pa.Table | None:
        files = self.walk_back(fid)
        if not files or not all(self.readable(f) for f in files):
            return None
        ch = store_chain.Chain(files[0].chain_id or fid, files[0].family or "",
                               tuple(store_chain.ChainFile(f.path, f.header) for f in files))
        try:
            return store_chain.read_view(ch, columns=columns, keys=keys)
        except (store_chain.ChainError, ValueError, UpcastRefused):  # a SchemaMismatch is a ValueError
            return None

    def chain_view(self, chain: store_chain.Chain, columns: Sequence[str] | None = None) -> pa.Table | None:
        """:func:`stack.store.chain.read_view` of a resolved chain (:meth:`resolve`), or ``None`` when a step
        refused a file's data (:class:`~stack.schemas.upcasters.UpcastRefused`: S1's finding; the resolver
        reads footers only, so it does not see it). Not cached."""
        try:
            return store_chain.read_view(chain, columns=columns)
        except UpcastRefused:
            return None

    def view_at(self, fid: str, columns: Sequence[str] | None = None, *, keys: object = None) -> pa.Table | None:
        """The chain view as of file ``fid`` (union-and-select up to and including it).
        ``columns``: only those and the key; ``keys``: only those rows
        (:func:`stack.store.chain.read_view`). Cached when ``keys`` is ``None``."""
        df = self.file(fid)
        spec = df.spec if df is not None else None
        cols = self._columns(spec, columns) if spec is not None else None
        if spec is not None and cols is not None:
            cols = field_paths(spec.schema, [*cols, *spec.key])
        if keys is not None:
            return self._read_view(fid, cols, keys)
        return self._cached(("view", fid, cols), lambda: self._read_view(fid, cols, None))

    def base_gap(self, df: DataFile) -> str | None:
        """Why the walk from ``df``'s base back to a full file leaves the recorded files: a
        message when it meets a file no ``ok`` record names, a part on base ``none`` or a cycle;
        ``None`` when the walk is whole (or ``df`` has no base)."""
        if df.is_chain_of_one or df.base_file_id == NONE:
            return None
        seen: set[str] = set()
        fid = df.base_file_id
        while fid not in seen:
            seen.add(fid)
            cur = self.file(fid)
            if cur is None:
                return f"base walk meets {fid}, which no ok run record names"
            if cur.is_full:
                return None
            if cur.base_file_id == NONE:
                return f"base walk ends at part {fid} on base none"
            fid = cur.base_file_id
        return f"base walk cycles at {fid}"

    def has_base(self, df: DataFile) -> bool:
        """Whether ``df`` has a base view to read (:meth:`has_view` of its base)."""
        return not (df.is_chain_of_one or df.base_file_id == NONE) and self.has_view(df.base_file_id)

    def base_view(self, df: DataFile, columns: Sequence[str] | None = None, *,
                  keys: object = None) -> pa.Table | None:
        """The view the file was written over (its base's chain view); ``None`` for a chain's
        first file, a chain of one, or a base outside ``data/``."""
        if df.is_chain_of_one or df.base_file_id == NONE:
            return None
        return self.view_at(df.base_file_id, columns, keys=keys)

    def base_join(self, df: DataFile, fields: Sequence[str] | None = None) -> BaseJoin | None:
        """The file joined with its base view on ``doc_id`` (a ``documents`` chain file): the rows
        of the keys in both, in the file's row order, as two aligned tables (:class:`BaseJoin`),
        both holding ``doc_id`` and ``fields`` only (``None``: all columns). A field may be a
        dotted path into a block (``metadata.fields_digest``, ``identity.members.source_id``;
        :func:`stack.store.parquet.field_paths`): both sides are then narrowed to it as they are
        read, a file one column at a time, so the memory held is proportional to the projected
        fields, not to the blocks. The base view is read for the file's keys only; nothing is
        cached; a file with no rows has an empty join. ``None`` when the file does not read or has
        no base view (the caller reports :func:`base_gap`)."""
        cols = None if fields is None else ["doc_id", *(c for c in fields if c != "doc_id")]
        tbl = self.read(df, cols)
        if tbl is None:
            return None
        if tbl.num_rows == 0:
            return BaseJoin(tbl, tbl)
        base = self.base_view(df, cols, keys=tbl.column("doc_id").unique())
        if base is None:
            return None
        at = pc.index_in(tbl.column("doc_id"), value_set=base.column("doc_id"))
        hit = pc.is_valid(at)
        return BaseJoin(tbl.filter(hit), base.take(at.filter(hit)))

    def base_pairs(self, df: DataFile, columns: Sequence[str] | None = None
                   ) -> Iterator[tuple[str, dict[str, Any], dict[str, Any]]]:
        """``(doc_id, row in df, row in its base view)`` for every ``documents`` key in both, in
        the file's row order (:meth:`base_join`); both rows hold ``doc_id`` and ``columns`` only
        (``None``: all; dotted paths narrow a block). Rows reach Python :data:`BATCH_ROWS` at a
        time; nothing is cached."""
        j = self.base_join(df, columns)
        if j is None:
            return
        for start in range(0, j.cur.num_rows, BATCH_ROWS):
            cur = j.cur.slice(start, BATCH_ROWS).to_pylist()
            prev = j.base.slice(start, BATCH_ROWS).to_pylist()
            for r, p in zip(cur, prev, strict=True):
                yield r["doc_id"], r, p

    def document_chain_files(self) -> list[DataFile]:
        """Files of the selected ``documents`` chains (not chains of one) that have a base."""
        return [df for fs in self.chains().values() for df in fs
                if df.spec is not None and df.spec.kind == "documents"
                and df.base_file_id != NONE]

    def pair_files(self, check: str) -> list[DataFile]:
        """The chain files a pair check (a file against its base view) looks at, in chain order:
        ``V12`` the compactions (``fold_config = compact``), ``X22`` the full files, and ``X13``,
        ``X15``, ``X17``, ``X19``, ``X24`` :meth:`document_chain_files`; only those :meth:`in_scope`."""
        if check == "V12":
            files = [df for fs in self.chains().values() for df in fs
                     if df.header.get("fold_config") == "compact" and df.spec is not None]
        elif check == "X22":
            files = [df for fs in self.chains().values() for df in fs if df.is_full and df.spec is not None]
        elif check in PAIR_CHECKS:
            files = self.document_chain_files()
        else:
            raise KeyError(f"{check!r} is not a pair check")
        return [df for df in files if self.in_scope(df)]

    # -- build
    def build_heads(self) -> dict[str, str]:
        """The build's last file per series directory it wrote (``files``, this build's own
        outputs, D373): directory key ``data/<family>/<series>`` -> file id, for chained families
        only. ``SkipCheck('no_builds')``."""
        if self.build is None:
            raise SkipCheck("no_builds")
        out: dict[str, str] = {}
        for key, entry in (self.build.get("files") or {}).items():
            fam = key.partition("/")[0]
            spec = FAMILIES.get(fam)
            if spec is None or spec.kind not in CHAINED_KINDS:
                continue
            if self.families is not None and fam not in self.families:
                continue
            df = self.file(str(entry["file_id"]))
            d = (str(entry["path"]).rsplit("/", 1)[0] if entry.get("path")
                 else self.dir_of(df) if df is not None else key)  # an unknown file: X1's
            out[d] = entry["file_id"]
        return out

    def dir_of(self, df: DataFile) -> str:
        """``df``'s directory key (``data/<family>/<series>``)."""
        return df.path.parent.relative_to(self.root).as_posix()


# ---------------------------------------------------------------- V11
@register("V11", "V-chain-linear", scope="store")
def v_chain_linear(view: StoreView) -> list[Finding]:
    """V11: every chain resolves (§7.5): an id once per file, one base per file, no two files on
    one base (full files count, C2), bases present from the newest full file on, the head reaches
    a full file, ``ids_digest``/``schema_digest`` recompute, every version file's digest its
    declared format version's and readable at the current one (C1, D295; versions may mix) —
    the resolver's errors (:mod:`stack.store.chain`) as findings."""
    bad = []
    for chain_id in view.chains():
        res = view.resolve(chain_id)
        if isinstance(res, store_chain.ChainError):
            bad.append(f"{chain_id}: {type(res).__name__}: {res}")
    return _finding("V-chain-linear", "{n} chain(s) not linear", bad)


# ---------------------------------------------------------------- V12
def _key_index(tbl: pa.Table, key: tuple[str, ...]) -> dict[tuple[Any, ...], int]:
    """Key → row index (the last row of a key, as a dict over the rows keeps it)."""
    return {k: i for i, k in enumerate(zip(*(tbl.column(c).to_pylist() for c in key), strict=True))}


def _observed_under(view: StoreView, df: DataFile, missed: set[tuple[Any, ...]]
                    ) -> dict[tuple[Any, ...], dict[str, Any]]:
    """For a ``sources`` compaction (D264): per key in ``missed`` (its base-view row is a miss),
    the last non-miss row of that key in the superseded version's files (chain order); each file's
    rows of those keys only reach Python."""
    files = view.walk_back(df.base_file_id) if missed else None
    out: dict[tuple[Any, ...], dict[str, Any]] = {}
    want = pa.array(sorted(k[0] for k in missed), pa.string()) if missed else None
    for f in files or []:
        tbl = view.table(f)
        if tbl is None:
            continue
        for r in tbl.filter(pc.is_in(tbl.column("source_id"), value_set=want)).to_pylist():
            k = (r["source_id"],)
            if k in missed and r["state"] != "missing":
                out[k] = r
    return out


def _restates(fs: list[DataFile], view: StoreView, df: DataFile, misses: list[dict[str, Any]]) -> str | None:
    """``None`` when a ``migrate.restate`` part on ``df`` holds exactly ``misses`` (run id aside),
    else what is wrong (D264)."""
    on = [f for f in fs if f.base_file_id == df.file_id and f.header.get("stage") == "migrate.restate"]
    if not on:
        return f"{df.file_id}: {len(misses)} miss row(s) replaced by observations, no migrate.restate part on it"
    tbl = view.table(on[0])
    got = sorted(({**r, "run_id": None} for r in (tbl.to_pylist() if tbl is not None else [])), key=repr)
    if got != sorted(({**r, "run_id": None} for r in misses), key=repr):
        return f"{on[0].file_id}: the restated rows differ from the miss rows {df.file_id} replaced"
    return None


@register("V12", "V-compaction-equal", scope="store")
def v_compaction_equal(view: StoreView) -> list[Finding]:
    """V12: a full file written by ``fold.compact`` (header ``fold_config = compact``) equals the
    union-and-select of the chain it superseded (its base view), row for row, stamps included
    (§7.4). A ``sources`` compaction may carry,
    where the base view's row is a miss, the last non-miss row of that source in the superseded
    version, and then a ``migrate.restate`` part on it holds exactly those miss rows (D264).
    Whole rows, compared :data:`BATCH_ROWS` keys at a time in ``repr`` key order."""
    bad: list[str] = []
    chains = view.chains()
    for df in view.pair_files("V12"):
        assert df.spec is not None
        if view.readable(df) and not view.has_base(df):
            base_gap(view, df, bad)
        tbl = view.read(df)
        base = view.base_view(df) if tbl is not None and view.has_base(df) else None
        if tbl is None or base is None:
            continue
        key = df.spec.key
        got, want = _key_index(tbl, key), _key_index(base, key)
        alt: dict[tuple[Any, ...], dict[str, Any]] = {}
        if df.family == "sources":
            states = base.column("state").to_pylist()
            alt = _observed_under(view, df, {k for k, i in want.items() if states[i] == "missing"})
        replaced: list[dict[str, Any]] = []
        order = sorted(set(got) | set(want), key=repr)
        for start in range(0, len(order), BATCH_ROWS):
            ks = order[start:start + BATCH_ROWS]
            both = [k for k in ks if k in got and k in want]
            g = dict(zip(both, tbl.take([got[k] for k in both]).to_pylist(), strict=True))
            w = dict(zip(both, base.take([want[k] for k in both]).to_pylist(), strict=True))
            for k in ks:
                if k not in got:
                    bad.append(f"{df.file_id}: {k} missing from the compaction")
                elif k not in want:
                    bad.append(f"{df.file_id}: {k} not in the superseded chain")
                elif g[k] != w[k] and k in alt and g[k] == alt[k]:
                    replaced.append(w[k])
                elif g[k] != w[k]:
                    cols = [c for c in g[k] if g[k][c] != w[k][c]]
                    bad.append(f"{df.file_id}: {k} differs in {cols}")
        if replaced and (msg := _restates(chains[view.dir_of(df)], view, df, replaced)) is not None:
            bad.append(msg)
    return _finding("V-compaction-equal", "{n} compaction row(s) differ from the union", bad)


# ---------------------------------------------------------------- X1
@register("X1", "finished_files", scope="store")
def finished_files(view: StoreView) -> list[Finding]:
    """X1 (D38, D346): over **all** ``ok`` run records (not only a build's): every file an ``ok``
    record names exists, locally or on the store's remote; a local copy's footer reads and holds the
    record's ``row_count`` rows, on a mirror its size is the store object's (one LIST of ``data/``),
    and in a full verify (:attr:`StoreView.rehash`) it has the record's ``file_digest`` (excluded
    files too: an ``excludes`` moves nothing), and a remote copy the view downloaded had it
    (:attr:`StoreView.digest_errors`, the key named). On a mirror a full verify's view has already
    replaced an altered local copy of a file it reads by the store's (moved aside, D371), so only a
    file no copy of which matches is reported for it. A local ``data/`` file no record names is
    garbage (a crash between rename and record, a failed publish): reported as a warn-level
    finding, never a failure (it is in no chain)."""
    bad: list[str] = []
    idx = store_index(view.root)
    remote = idx.store.remote
    listed = {o.key: o.size for o in remote.list(f"{DATA}/")} if remote is not None else {}
    for _, _, out in idx.outputs():
        p = view.root / out["path"]
        if not p.is_file():
            continue
        at = f"{out['file_id']}: {out['path']}"
        if view.rehash and local_digest(p) != out["file_digest"]:
            bad.append(f"{at} does not have its record's file_digest")
            continue
        size = listed.get(out["path"])
        if size is not None and p.stat().st_size != size:
            bad.append(f"{at} is {p.stat().st_size} bytes, the store's object {size}")
            continue
        try:
            rows = read_footer(p).num_rows
        except Exception as exc:  # noqa: BLE001 - a footer that does not read is this check's finding
            bad.append(f"{at}: footer does not read: {type(exc).__name__}: {exc}")
            continue
        if rows != out["row_count"]:
            bad.append(f"{at} has {rows} rows, its record says {out['row_count']}")
    bad += view.digest_errors
    bad += [f"{lost.file_id}: ok record of {lost.run_id} names {lost.path}, neither local nor remote"
            for lost in records_without_file(view.root)]
    garbage = [f"{p.name}: no ok run record names it (garbage)" for p in files_without_ok_record(view.root)]
    return _finding("finished_files", "{n} missing or altered recorded file(s)", bad) + (
        [Finding("finished_files", "warn", f"{len(garbage)} data/ file(s) no record names (garbage)",
                 len(garbage), tuple(garbage[:_EXAMPLES]))] if garbage else [])


# ---------------------------------------------------------------- X10
@register("X10", "ordering_pinned", scope="store")
def ordering_pinned(view: StoreView) -> list[Finding]:
    """X10: the build's recorded ``version_chain`` is the header walk. The list is grouped by
    chain (order kept); each group must equal the walk from the newest full file to the group's
    last file (the pinned head) by ``base_file_id``, never an order by name or time."""
    if view.build is None:
        raise SkipCheck("no_builds")
    groups: dict[str, list[str]] = defaultdict(list)
    bad: list[str] = []
    for fid in view.build.get("version_chain") or []:
        df = view.file(fid)
        if df is None:
            bad.append(f"{fid}: in version_chain but not in data/")
            continue
        if view.selected(df) and df.chain_id:
            groups[df.chain_id].append(fid)
    for chain_id, recorded in sorted(groups.items()):
        walk = view.walk_back(recorded[-1])
        want = [f.file_id for f in walk] if walk is not None else None
        if want != recorded:
            bad.append(f"{chain_id}: recorded {recorded}, header walk {want}")
    return _finding("ordering_pinned", "{n} version_chain entr(y/ies) not the header walk", bad)


# ---------------------------------------------------------------- X22
@register("X22", "full_covers_base", scope="store")
def full_covers_base(view: StoreView) -> list[Finding]:
    """X22 (D20): a full file with a base holds every id of its base view; a full file drops an
    id only by writing it as a tombstone (``retired``), which keeps the id. Chains of one have
    no base (D14)."""
    bad: list[str] = []
    for df in view.pair_files("X22"):
        assert df.spec is not None
        if view.readable(df) and not view.has_base(df):
            base_gap(view, df, bad)
        key = list(df.spec.key)
        tbl = view.read(df, key)
        base = view.base_view(df, key) if tbl is not None and view.has_base(df) else None
        if tbl is None or base is None:
            continue
        have = set(zip(*(tbl.column(k).to_pylist() for k in key)))
        missing = [k for k in zip(*(base.column(k).to_pylist() for k in key)) if k not in have]
        bad += [f"{df.file_id}: drops {k[0] if len(k) == 1 else k}" for k in missing]
    return _finding("full_covers_base", "{n} base-view id(s) dropped by a full file", bad)


# ---------------------------------------------------------------- X23
@register("X23", "head_descends", scope="store")
def head_descends(view: StoreView) -> list[Finding]:
    """X23, per file (D375): every selected chained file with a base names
    a published file of its own directory as that base, so it descends from its base's version
    (instead of comparing a build's heads with its prior build's; there is no prior build). A base
    that is gone or excluded is a file on a lost tail; a walk that does not resolve is V11's."""
    bad: list[str] = []
    for df in view.files:
        if df.is_chain_of_one or df.base_file_id == NONE or not view.selected(df):
            continue
        base = view.file(df.base_file_id)
        if base is None or base.path.parent != df.path.parent:
            bad.append(f"{df.key}: base {df.header.get('base')} is not a published file of {df.path.parent.name}/")
    return _finding("head_descends", "{n} file(s) whose base is not a published file of their directory", bad)


__all__ = ["BATCH_ROWS", "CACHE_BYTES", "CHAINED_KINDS", "PAIR_CHECKS", "BaseJoin", "DataFile",
           "MixedSeriesError", "SkipCheck", "StoreView", "as_family", "base_gap", "dirs_series",
           "last_build", "read_build_record"]

