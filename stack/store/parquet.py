"""The one family writer and reader: parquet files with a ``stack.*`` header (A.6, §7.2).

:func:`write_family` runs S1-S10 (:mod:`stack.checks.schema`, D45) plus the file-name/header
agreement and refuses the file on any finding: :class:`SchemaRefused`, nothing written. A file
that passes is written to a temp name in the target directory and hard-linked into place, so a
reader never sees a partial file and an existing file is never overwritten (files are immutable).
Every file is written at its family's current format version (``stack.family_version``,
``stack.schema_version = "2"``; D295).

:func:`read_family` is the one read path of a family file: it returns the table **at the family's
current version**, an older file upcast through the steps of :mod:`stack.schemas.upcasters`, and
the header as written. The file's version comes from its footer only (:func:`file_format`): the
declared ``family_version``, or for a legacy file (``schema_version "1"``) the version whose
registered digest is the header's ``schema_digest``; the header digest must be the file's own
schema's and the declared version's. A file that does not read raises a :class:`SchemaMismatch`:
:class:`NewerFormat` (written by newer code), :class:`UnknownFormat` (a digest or version number
no version has), :class:`IncompatibleFormat` (behind an ``Incompatible`` step), or the base class
itself (a digest that is not the file's own schema's or its declared version's). The chain reader
(:mod:`stack.store.chain`) reads every file through it.

**Footers** (D731). Every footer read here and in the checks goes through :func:`read_footer`,
every raw data read through :func:`read_raw`. Inside a :func:`footer_cache` block (a verify,
:func:`stack.checks.verify.run_checks`; a build, :func:`stack.build.run_build`) each distinct footer
is parsed once and its schema digest computed once, and a data read reuses the parsed metadata of
the :data:`METADATA_ENTRIES` newest files: the cache is keyed by the file's size and the sha256 of
its footer bytes, which every lookup reads afresh from the file (the footer only, up to a few hundred
KB), so a file changed or replaced within the block is a miss and reads as it now is; everything
cached (schema, header, row count, schema digest, parsed metadata) is a function of those bytes
alone. A data page is never cached. Nothing outlives the block.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from collections import OrderedDict
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from stack.checks import Finding
from stack.checks.schema import run_schema_checks
from stack.schemas import upcasters
from stack.schemas.families import (
    FAMILIES,
    FamilySpec,
    family,
    ids_digest,
    schema_digest,
)
from stack.store.layout import (
    HEADER_PREFIX,
    LEGACY_SCHEMA_VERSION,
    NONE,
    SCHEMA_VERSIONS,
    STORE_SCHEMA_VERSION,
    WILDCARD,
    canonical_key_header,
    file_id,
    parse_data_key,
)

Header = dict[str, str]


class SchemaRefused(ValueError):
    """``write_family`` refused a file; ``findings`` names the failing checks."""

    def __init__(self, path: Path, findings: list[Finding]) -> None:
        self.path = path
        self.findings = findings
        lines = "; ".join(f"{f.check}: {f.message}" for f in findings)
        super().__init__(f"refused {path.name}: {lines}")

    @property
    def checks(self) -> set[str]:
        return {f.check for f in self.findings}


class SchemaMismatch(ValueError):
    """A family file does not read (module docstring): the base of the format refusals; raised
    as itself when the header's ``schema_digest`` is not the file's own schema's or its declared
    version's (C1)."""


class NewerFormat(SchemaMismatch):
    """The file's ``family_version`` (or ``schema_version``) is newer than this code reads."""


class UnknownFormat(SchemaMismatch):
    """A legacy file whose digest no version has, a version number not registered, or a family
    that is not registered."""


class IncompatibleFormat(SchemaMismatch):
    """The path from the file's version to the current one crosses an ``Incompatible`` step."""


def build_header(
    table: pa.Table,
    family_name: str,
    *,
    stage: str,
    writer: str,
    run_id: str,
    config_digest: str,
    finished_at: dt.datetime,
    code_digest: str,
    kind: str = "full",
    chain_id: str | None = None,
    blocks_written: Sequence[str] | str = WILDCARD,
    plan: str = NONE,
    inputs: Mapping[str, object] | None = None,
    prior: Sequence[str] | None = None,
    only: Sequence[str] | None = None,
    scope_complete: bool = True,
    basis_kind: str = NONE,
    fold_config: str = NONE,
    filter: str = NONE,
    annotations_folded: Sequence[str] | None = None,
    release_config: str = NONE,
    series: str | None = None,
    comment: str | None = None,
    base: str = "",
    after: str = "",
) -> Header:
    """A complete header for ``table`` with the computed keys filled in (``family``, ``key``,
    ``file_id``, ``row_count``, ``schema_digest``, ``family_version``, ``ids_digest``,
    ``schema_version``).
    ``chain_id`` (the family directory, D372) defaults to the family. ``series``, ``comment``:
    empty when ``None``; ``base``: the base file's name in its directory, ``after``: the directory's
    last published file at pin time (D372)."""
    spec = family(family_name)
    fid = file_id(run_id, family_name)
    return {
        "schema_version": STORE_SCHEMA_VERSION,
        "family": family_name,
        "key": canonical_key_header(spec.key),
        "stage": stage,
        "writer": writer,
        "run_id": run_id,
        "file_id": fid,
        "plan": plan,
        "inputs": json.dumps(dict(inputs or {}), sort_keys=True),
        "prior": json.dumps(list(prior or [])),
        "only": json.dumps(list(only) if only is not None else None),
        "config_digest": config_digest,
        "scope_complete": "true" if scope_complete else "false",
        "row_count": str(table.num_rows),
        "finished_at": finished_at.astimezone(dt.UTC).isoformat(),
        "code_digest": code_digest,
        "schema_digest": spec.schema_digest,
        "family_version": str(spec.version),
        "chain_id": chain_id if chain_id is not None else family_name,
        "ids_digest": ids_digest(table, spec.key),
        "blocks_written": blocks_written if isinstance(blocks_written, str)
        else json.dumps(list(blocks_written)),
        "basis_kind": basis_kind,
        "fold_config": fold_config,
        "filter": filter,
        "annotations_folded": json.dumps(list(annotations_folded))
        if annotations_folded is not None else NONE,
        "release_config": release_config,
        "series": series or "",
        "comment": comment or "",
        "kind": kind,
        "base": base,
        "after": after,
    }


def _name_problems(path: Path, header: Mapping[str, object]) -> list[Finding]:
    """The header must agree with the data path it is written under (D372)."""
    try:
        f = parse_data_key(path)
    except ValueError as exc:
        return [Finding("kv_complete", "hard", str(exc))]
    pairs = {"family": f.family, "run_id": f.run_id, "kind": f.kind,
             "chain_id": f.family_dir, "series": f.series or "", "comment": f.comment or ""}
    out = [Finding("kv_complete", "hard", f"header {k} = {header.get(k)!r} but the path says {v!r}", 1, (k,))
           for k, v in pairs.items() if header.get(k) != v]
    if f.kind == "part" and not header.get("base"):
        out.append(Finding("kv_complete", "hard", f"{f.name}: a part names its base (stack.base)", 1, ("base",)))
    if header.get("base") and header.get("base") != header.get("after"):
        out.append(Finding("kv_complete", "hard", f"{f.name}: base {header.get('base')!r} is not after "
                           f"{header.get('after')!r} (a file with a base extends its directory's last file)", 1,
                           ("base",)))
    return out


def check_family(path: Path, table: pa.Table, header: Mapping[str, object]) -> list[Finding]:
    """Every write-time finding for ``table`` with ``header`` under ``path`` (empty = writable)."""
    name_findings = _name_problems(path, header)
    fam = header.get("family")
    if not isinstance(fam, str):
        return name_findings or [Finding("kv_complete", "hard", "header has no family")]
    try:
        spec = family(fam)
    except KeyError as exc:
        return [*name_findings, Finding("kv_complete", "hard", str(exc))]
    return name_findings + run_schema_checks(table, dict(header), spec)  # type: ignore[arg-type]


def write_family(path: str | Path, table: pa.Table, header: Mapping[str, str], *,
                 name: str | None = None) -> Path:
    """Write ``table`` as a family file with ``header`` under ``path``, or raise
    :class:`SchemaRefused` and write nothing. Never overwrites (``FileExistsError``). ``name``:
    the file name (in ``path``'s directory) the header is checked against; a staging name
    ``.<name>.tmp`` is read as ``<name>`` without it."""
    path = Path(path)
    findings = check_family(path.parent / name if name is not None else path, table, header)
    if findings:
        raise SchemaRefused(path, findings)
    meta = {f"{HEADER_PREFIX}{k}".encode(): v.encode() for k, v in header.items()}
    tbl = table.replace_schema_metadata(meta)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{os.getpid()}.{os.urandom(4).hex()}.tmp"
    try:
        pq.write_table(tbl, tmp, compression="zstd")
        os.link(tmp, path)  # atomic, and refuses an existing target
    finally:
        tmp.unlink(missing_ok=True)
    return path


def _header_of(meta: Mapping[bytes, bytes] | None) -> Header:
    return {k.decode()[len(HEADER_PREFIX):]: v.decode() for k, v in (meta or {}).items()
            if k.startswith(HEADER_PREFIX.encode())}


# ---------------------------------------------------------------- footers
#: :func:`footer_cache`'s bound on the parsed ``pq.FileMetaData`` it keeps for :func:`read_raw` (the newest
#: used): a family file's metadata is ~0.5 MB in memory (a few hundred leaf columns), its schema a tenth of it.
METADATA_ENTRIES = 64
_Key = tuple[int, bytes]


class Footer:
    """A parquet file's footer as :func:`read_footer` read it: ``schema`` (``pq.read_schema``'s,
    key-value metadata included), ``num_rows`` (``pq.read_metadata``'s), ``header`` (its ``stack.*``
    keys, a fresh dict per access) and ``digest`` (:func:`stack.schemas.families.schema_digest` of
    the schema without metadata, computed on first use). ``schema`` raises what ``pq.read_schema``
    raises on a footer whose metadata reads but whose schema does not."""

    __slots__ = ("_digest", "_schema", "num_rows")

    def __init__(self, num_rows: int, schema: pa.Schema | Exception) -> None:
        self.num_rows = num_rows
        self._schema = schema
        self._digest: str | None = None

    @property
    def schema(self) -> pa.Schema:
        if isinstance(self._schema, Exception):
            raise self._schema
        return self._schema

    @property
    def header(self) -> Header:
        return _header_of(self.schema.metadata)

    @property
    def digest(self) -> str:
        if self._digest is None:
            self._digest = schema_digest(self.schema.remove_metadata())
        return self._digest


class _Cache:
    """One :func:`footer_cache` block's state: footers by key, and the newest :data:`METADATA_ENTRIES`
    parsed metadata by key."""

    def __init__(self) -> None:
        self.footers: dict[_Key, Footer] = {}
        self.metadata: OrderedDict[_Key, pq.FileMetaData] = OrderedDict()

    def keep_metadata(self, key: _Key, metadata: pq.FileMetaData) -> None:
        self.metadata[key] = metadata
        self.metadata.move_to_end(key)
        while len(self.metadata) > METADATA_ENTRIES:
            self.metadata.popitem(last=False)


#: The open :func:`footer_cache` block's state; ``None`` outside one.
_CACHE: ContextVar[_Cache | None] = ContextVar("stack_footer_cache", default=None)


@contextmanager
def footer_cache() -> Iterator[None]:
    """Within the block :func:`read_footer` parses each distinct footer once and :func:`read_raw`
    reuses a parsed footer (module docstring, D731). A nested block shares the outer one's cache;
    the cache is dropped when the outermost block ends. Usable as a decorator."""
    if _CACHE.get() is not None:
        yield
        return
    token = _CACHE.set(_Cache())
    try:
        yield
    finally:
        _CACHE.reset(token)


def _footer_key(path: str | Path) -> _Key | None:
    """``(file size, sha256 of the footer and its 8-byte trailer)`` read from the file now, or
    ``None`` when the file has no plain parquet footer to key (the uncached read then raises what
    it raises)."""
    try:
        with open(path, "rb") as fh:
            size = os.fstat(fh.fileno()).st_size
            if size < 12:
                return None
            fh.seek(size - 8)
            tail = fh.read(8)
            n = int.from_bytes(tail[:4], "little")
            if len(tail) != 8 or tail[4:] != b"PAR1" or n + 12 > size:
                return None
            fh.seek(size - 8 - n)
            body = fh.read(n)
    except OSError:
        return None
    return (size, hashlib.sha256(body + tail).digest()) if len(body) == n else None


def _read_footer_uncached(path: str | Path) -> tuple[Footer, pq.FileMetaData]:
    """One footer parse: what ``pq.read_metadata`` and ``pq.read_schema`` each do (the same
    ``ParquetFile`` open), their exceptions unchanged."""
    with pq.ParquetFile(path) as pf:
        metadata = pf.metadata
        try:
            schema: pa.Schema | Exception = pf.schema_arrow
        except Exception as exc:  # noqa: BLE001 - re-raised by Footer.schema, where read_schema raised it
            schema = exc
    return Footer(metadata.num_rows, schema), metadata


def read_footer(path: str | Path) -> Footer:
    """The parquet footer of ``path`` (:class:`Footer`): parsed once per distinct footer inside a
    :func:`footer_cache` block, else parsed now. Raises what ``pq.read_metadata`` raises."""
    cache = _CACHE.get()
    if cache is None:
        return _read_footer_uncached(path)[0]
    key = _footer_key(path)
    if key is not None and (hit := cache.footers.get(key)) is not None:
        return hit
    got, metadata = _read_footer_uncached(path)
    # Kept only when the footer bytes are the same after the parse as before it (no write in between)
    # and the schema reads; a footer whose schema does not read is parsed again on every call.
    if key is not None and not isinstance(got._schema, Exception) and _footer_key(path) == key:
        cache.footers[key] = got
        cache.keep_metadata(key, metadata)
    return got


def read_header(path: str | Path) -> Header:
    """The ``stack.*`` header (prefix stripped) from the parquet footer only."""
    return read_footer(path).header


def read_raw(path: str | Path, columns: Sequence[str] | None = None, *, footer: Footer | None = None) -> pa.Table:
    """The file's rows as stored (no upcast), key-value metadata dropped: what
    ``pq.read_table(path, columns=columns)`` reads, without its dataset discovery and, inside a
    :func:`footer_cache` block, with the footer parsed once (the metadata of the newest
    :data:`METADATA_ENTRIES` files kept). ``columns``: only those, in that order; a name not in the
    file's schema (``footer``'s when given, else :func:`read_footer`'s), or a repeated one, raises
    ``ValueError``. Each column is one chunk (``pq.read_table`` splits at row groups and 128Ki rows;
    the values are the same)."""
    if columns is not None:
        names = (footer if footer is not None else read_footer(path)).schema.names
        bad = [c for c in columns if c not in names]
        if bad or len(set(columns)) != len(columns):
            raise ValueError(f"{Path(path).name}: columns {list(columns)}: not in the file {bad} or repeated")
    cols = list(columns) if columns is not None else None
    cache = _CACHE.get()
    key = _footer_key(path) if cache is not None else None
    metadata = cache.metadata.get(key) if cache is not None and key is not None else None
    if cache is not None and key is not None and metadata is not None:
        cache.metadata.move_to_end(key)
    with pq.ParquetFile(path, metadata=metadata) as pf:
        tbl = pf.read(columns=cols)
        if cache is not None and key is not None and metadata is None and _footer_key(path) == key:
            cache.keep_metadata(key, pf.metadata)
    return tbl.replace_schema_metadata(None)


@dataclass(frozen=True)
class FileFormat:
    """A family file's format version from its footer (:func:`file_format`): ``version`` (the
    file's), ``current`` (the family's), ``legacy`` (no ``family_version`` key: mapped by digest),
    ``digest`` (the file's schema digest) and ``steps`` (the versions ``version+1 .. current``)."""

    family: str
    version: int
    current: int
    legacy: bool
    digest: str
    steps: tuple[upcasters.Version, ...]

    @property
    def is_current(self) -> bool:
        """The file's schema is the family's current one: it reads as is."""
        return self.digest == family(self.family).schema_digest

    @property
    def additive(self) -> bool:
        """Every step to the current version is ``ADDITIVE`` (a projected read stays projected)."""
        return upcasters.additive(self.steps)

    def describe(self) -> str:
        """``corpus v2 (current v3, upcast on read)`` / ``corpus v3 (current)``."""
        if self.is_current:
            return f"{self.family} v{self.version} (current)"
        return f"{self.family} v{self.version} (current v{self.current}, upcast on read)"


def _versions_line(fam: str) -> str:
    return ", ".join(f"v{v.number} {v.digest[:12]}…" for v in upcasters.history(fam)) or "none"


def file_format(path: str | Path, header: Mapping[str, str], schema: pa.Schema | None = None, *,
                digest: str | None = None) -> FileFormat:
    """The format version of the family file at ``path`` from its footer only: ``header`` (its
    ``stack.*`` keys) and ``schema`` (its parquet schema; read from the footer when ``None``), or
    ``digest``, that schema's digest when the caller recomputed it already. Raises a
    :class:`SchemaMismatch` (module docstring) naming the file, family, versions and
    :data:`stack.schemas.upcasters.HOWTO`."""
    name = Path(path).name
    fam = str(header.get("family"))
    spec = FAMILIES.get(fam)
    how = f"({upcasters.HOWTO})"
    if spec is None:
        raise UnknownFormat(f"{name}: family {fam!r} is not registered {how}")
    sv = header.get("schema_version")
    if sv not in SCHEMA_VERSIONS:
        raise NewerFormat(f"{name}: stack.schema_version {sv!r} is not one this code reads "
                          f"({list(SCHEMA_VERSIONS)}): written by newer code? {how}")
    if digest is not None:
        actual = digest
    elif schema is not None:
        actual = schema_digest(schema.remove_metadata())
    else:
        actual = read_footer(path).digest
    declared = header.get("schema_digest")
    if declared != actual:
        raise SchemaMismatch(f"{name}: header schema_digest {str(declared)[:12]}… is not the file's own schema's "
                             f"{actual[:12]}… (C1) {how}")
    cur = spec.version
    fv = header.get("family_version")
    if fv is None:
        if sv != LEGACY_SCHEMA_VERSION:
            raise SchemaMismatch(f"{name}: schema_version {sv!r} but no stack.family_version {how}")
        v = upcasters.version_of_digest(fam, actual)
        if v is None:
            raise UnknownFormat(f"{name}: a legacy {fam} file (no stack.family_version) whose schema_digest "
                                f"{actual[:12]}… is no registered version of {fam} ({_versions_line(fam)}) {how}")
        number, legacy = v.number, True
    else:
        try:
            number = int(fv)
        except ValueError:
            raise UnknownFormat(f"{name}: stack.family_version {fv!r} is not a number {how}") from None
        if number > cur:
            raise NewerFormat(f"{name}: {fam} v{number}, written by newer code: this code reads {fam} up to "
                              f"v{cur} {how}")
        v = upcasters.version(fam, number)
        if v is None:
            raise UnknownFormat(f"{name}: {fam} v{number} is not a registered version ({_versions_line(fam)}) {how}")
        if v.digest != actual:
            raise SchemaMismatch(f"{name}: declares {fam} v{number}, but its schema_digest {actual[:12]}… is not "
                                 f"v{number}'s {v.digest[:12]}… {how}")
        legacy = False
    steps = upcasters.path(fam, number, cur)
    for s in steps:
        if isinstance(s.step, upcasters.Incompatible):
            raise IncompatibleFormat(f"{name}: {fam} v{number} does not read at v{cur}: v{s.number - 1} → "
                                     f"v{s.number} is incompatible: {s.step.reason} {how}")
    return FileFormat(fam, number, cur, legacy, actual, steps)


def _target(spec: FamilySpec, columns: Sequence[str]) -> pa.Schema:
    names = list(columns)
    unknown = [c for c in names if c not in spec.schema.names]
    if unknown or len(set(names)) != len(names):
        raise ValueError(f"{spec.name}: columns {names}: unknown {unknown} or repeated")
    return pa.schema([spec.schema.field(c) for c in names])


def _rows(path: Path, fmt: FileFormat, spec: FamilySpec, ft: Footer,
          columns: Sequence[str] | None) -> pa.Table:
    """The file's rows at the current version (module docstring); ``columns``: only those."""
    what = f"{path.name} ({fmt.family} v{fmt.version})"
    if columns is None:
        tbl = read_raw(path, footer=ft)
        if fmt.is_current:
            if not tbl.schema.equals(spec.schema, check_metadata=False):
                raise SchemaMismatch(f"{path.name}: schema is not the registry's for {spec.name}")
            return tbl
        return upcasters.upcast(tbl, fmt.family, fmt.version, spec.schema, what=what)
    target = _target(spec, columns)
    if not fmt.additive:  # a function step reads the whole file
        full = upcasters.upcast(read_raw(path, footer=ft), fmt.family, fmt.version, spec.schema, what=what)
        return full.select(list(columns))
    have = ft.schema.remove_metadata()
    if not fmt.is_current:
        problems = upcasters.schema_delta(have, spec.schema).problems
        if problems:
            raise upcasters.UpcastBug(f"{what}: registered as additive to v{fmt.current}, but: {'; '.join(problems)}")
    tbl = read_raw(path, [c for c in columns if c in have.names], footer=ft)
    tbl = upcasters.upcast_table(tbl.select([c for c in columns if c in have.names]), target)
    if not tbl.schema.equals(target, check_metadata=False):
        raise SchemaMismatch(f"{path.name}: columns {list(columns)} are not the registry's for {spec.name}")
    return tbl


def read_family(path: str | Path, columns: Sequence[str] | None = None) -> tuple[pa.Table, Header]:
    """``(table, header)``: the file's rows at the family's **current** version (an older file
    upcast, module docstring) without key-value metadata, and the header as written. ``columns``:
    only those, in the given order (a projected read; a path with a function step reads the whole
    file, upcasts, then selects). Raises :class:`SchemaMismatch` (or a subclass) when the file
    does not read."""
    path = Path(path)
    ft = read_footer(path)
    header = ft.header
    fmt = file_format(path, header, digest=ft.digest)
    return _rows(path, fmt, family(fmt.family), ft, columns), header


# ---------------------------------------------------------------- field projection
#: A projection tree: field name -> the subtree of its children to keep, ``None`` = the whole field.
FieldTree = dict[str, "FieldTree | None"]


def _list_like(t: pa.DataType) -> bool:
    return bool(pa.types.is_list(t) or pa.types.is_large_list(t))


def _tree(schema: pa.Schema, fields: Sequence[str]) -> FieldTree:
    """The projection tree of ``fields`` (dotted paths) over ``schema``; an unknown path raises
    ``ValueError``. A path steps through ``struct`` fields by name and through a ``list<struct>``
    into its elements without a name of its own (``identity.members.source_id``); a whole field
    absorbs every deeper path under it."""
    tree: FieldTree = {}
    for path in fields:
        parts = path.split(".")
        if not all(parts):
            raise ValueError(f"field path {path!r}: empty component")
        node: FieldTree = tree
        t: pa.DataType | None = None
        for i, name in enumerate(parts):
            if i == 0:
                if name not in schema.names:
                    raise ValueError(f"field path {path!r}: no column {name!r}")
                t = schema.field(name).type
            else:
                assert t is not None
                while _list_like(t):
                    t = t.value_type
                if not pa.types.is_struct(t) or t.get_field_index(name) < 0:
                    raise ValueError(f"field path {path!r}: no field {name!r} under {'.'.join(parts[:i])}")
                t = t.field(name).type
            last = i == len(parts) - 1
            if name in node and node[name] is None:
                break  # a whole field already
            if last:
                node[name] = None
            else:
                sub = node.setdefault(name, {})
                assert sub is not None
                node = sub
    return tree


def _paths(schema_fields: Sequence[pa.Field], tree: FieldTree, prefix: str = "") -> list[str]:
    out: list[str] = []
    for f in schema_fields:
        if f.name not in tree:
            continue
        sub = tree[f.name]
        if sub is None:
            out.append(prefix + f.name)
            continue
        t = f.type
        while _list_like(t):
            t = t.value_type
        out += _paths(list(t), sub, f"{prefix}{f.name}.")
    return out


def field_paths(schema: pa.Schema, fields: Sequence[str]) -> tuple[str, ...]:
    """``fields`` (column names or dotted paths into ``struct`` / ``list<struct>`` columns)
    validated and canonical: in schema order, each once, a path under a whole field dropped.
    Plain column names come back as themselves, in registry order."""
    return tuple(_paths(list(schema), _tree(schema, fields)))


def _project_type(t: pa.DataType, tree: FieldTree | None) -> pa.DataType:
    if tree is None:
        return t
    if _list_like(t):
        vf = t.value_field
        inner = vf.with_type(_project_type(vf.type, tree))
        return pa.large_list(inner) if pa.types.is_large_list(t) else pa.list_(inner)
    return pa.struct([f.with_type(_project_type(f.type, tree[f.name])) for f in t if f.name in tree])


def project_schema(schema: pa.Schema, fields: Sequence[str]) -> pa.Schema:
    """``schema`` narrowed to ``fields`` (:func:`field_paths`): the top-level columns named, each
    struct keeping only the named children (same names, order, types and nullability)."""
    tree = _tree(schema, fields)
    return pa.schema([f.with_type(_project_type(f.type, tree[f.name])) for f in schema if f.name in tree])


def _project_array(arr: pa.Array, tree: FieldTree | None) -> pa.Array:
    if tree is None:
        return arr
    if arr.offset:  # a slice: rebuilt with zero offsets (the constructors refuse a mask on a slice)
        arr = pa.concat_arrays([arr])
    t = arr.type
    mask = arr.is_null() if arr.null_count else None
    if _list_like(t):
        values = _project_array(arr.values, tree)
        cls = pa.LargeListArray if pa.types.is_large_list(t) else pa.ListArray
        return cls.from_arrays(arr.offsets, values, type=_project_type(t, tree), mask=mask)
    keep = [(i, f) for i, f in enumerate(t) if f.name in tree]
    kids = [_project_array(arr.field(i), tree[f.name]) for i, f in keep]
    out_type = _project_type(t, tree)
    return pa.StructArray.from_arrays(kids, fields=list(out_type), mask=mask)


def project(table: pa.Table, fields: Sequence[str]) -> pa.Table:
    """``table`` narrowed to ``fields`` (:func:`project_schema`; the rows, values and NULLs of the
    kept fields unchanged). The building block of the projected reads below."""
    schema = table.schema.remove_metadata()
    tree = _tree(schema, fields)
    target = project_schema(schema, fields)
    cols = []
    for f in target:
        col = table.column(f.name)
        cols.append(pa.chunked_array([_project_array(c, tree[f.name]) for c in col.chunks], type=f.type))
    return pa.Table.from_arrays(cols, schema=target)


def read_family_fields(path: str | Path, fields: Sequence[str]) -> tuple[pa.Table, Header]:
    """:func:`read_family` projected to ``fields`` (column names or dotted paths,
    :func:`field_paths`): ``(table, header)`` with :func:`project_schema`'s schema; a file that
    does not read raises as :func:`read_family` does. A plain column list is :func:`read_family`
    itself. Otherwise each top-level column is read (and upcast) on its own and narrowed before
    the next is read, so the memory held is the narrowed columns plus one whole column of the
    file at a time (a file behind a function step is read whole once, as :func:`read_family`
    reads it)."""
    path = Path(path)
    ft = read_footer(path)
    header = ft.header
    fmt = file_format(path, header, digest=ft.digest)
    spec = family(fmt.family)
    canon = field_paths(spec.schema, fields)
    tops = list(dict.fromkeys(f.split(".", 1)[0] for f in canon))
    if all("." not in f for f in canon):
        return _rows(path, fmt, spec, ft, canon), header
    tree = _tree(spec.schema, canon)
    target = project_schema(spec.schema, canon)
    whole = None if fmt.additive else _rows(path, fmt, spec, ft, tops)
    cols: list[pa.ChunkedArray] = []
    for f in target:
        col = (whole if whole is not None else _rows(path, fmt, spec, ft, [f.name])).column(f.name)
        cols.append(pa.chunked_array([_project_array(c, tree[f.name]) for c in col.chunks], type=f.type))
        del col
    return pa.Table.from_arrays(cols, schema=target), header
