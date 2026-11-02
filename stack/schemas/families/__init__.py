"""The family registry (§2.3, A.5): one :class:`FamilySpec` per family the store writes.

A spec carries the pyarrow schema (the registry S1 compares against), the key columns (§2.3),
``schema_digest``, and what the write-time checks read off it: the status/error_class pairs
(S4), the nested lists whose entries must be unique (S5) and the id-bearing paths with their
formats (S6). Pairs and id paths are derived from the schema by field name, so a new struct
that follows the naming table (§2.2) is covered without listing it.

Paths use ``a.b[].c``: ``.`` descends into a struct field, ``[]`` into every list element.

**schema_digest** = sha256 hex of :func:`stack.core.hashes.jcs` over the schema rendered as
``[[name, type, nullable], ...]`` in column order, where a type renders as its pyarrow string for
primitives (``"string"``, ``"int32"``, ``"timestamp[us, tz=UTC]"``), ``{"list": [item_type,
item_nullable]}`` for a list (the item field's *name* is ignored: parquet renames ``item`` to
``element`` on a round trip) and ``{"struct": [[name, type, nullable], ...]}`` for a struct.
Key-value metadata (schema and field level) is not part of the rendering, so a header can never
perturb the digest.

**ids_digest** (D19) = sha256 hex of the UTF-8 bytes of the sorted rendered key values joined by
``"\\n"``; a composite key's parts are joined by ``"\\x1f"`` before sorting; strings render as
themselves, timestamps as ``YYYY-MM-DDTHH:MM:SS.ffffffZ`` (UTC). An empty file's digest is the
sha256 of the empty string.

**version** (D295): the family's current format version, the newest number registered for it in
:data:`stack.schemas.upcasters.VERSIONS` (0: none), assigned when the registry is built
(:func:`versioned`). Writers put it in the header as ``stack.family_version``; the reader upcasts an
older file to it (:func:`stack.store.parquet.read_family`).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from collections.abc import Iterator
from dataclasses import dataclass, field

import pyarrow as pa

from stack.core.hashes import jcs

# ---------------------------------------------------------------- id formats (S6)
#: S6 format names → full-match regexes. Minted ids follow ``stack.core.ids`` (type letter + 12
#: lowercase Crockford base32, ``stack/core/ids.py:18-31``, ``stack.core.idkinds.ID_RE``).
_CROCKFORD12 = "[0-9abcdefghjkmnpqrstvwxyz]{12}"
ID_FORMATS: dict[str, re.Pattern[str]] = {
    "digest": re.compile(r"[0-9a-f]{64}"),
    "source_id": re.compile(rf"s{_CROCKFORD12}"),
    "run_id": re.compile(rf"r{_CROCKFORD12}"),
    "evidence_id": re.compile(rf"[snmagkvrwcof]{_CROCKFORD12}"),
    "snapshot_id": re.compile(rf"r{_CROCKFORD12}\.[0-9]+"),
}

#: Leaf field name → S6 format. `doc_id` is the anchor's `source_id` (§2.2), so every column
#: holding a document or source id follows `source_id`. A name ending in `_digest` (or the
#: snapshot's bare `digest`, or a `*_digests` list) is a 64-hex digest.
ID_NAME_FORMAT: dict[str, str] = {
    "source_id": "source_id",
    "doc_id": "source_id",
    "anchor_source_id": "source_id",
    "edge_to_source_id": "source_id",
    "merged_into": "source_id",
    "current_source_id": "source_id",
    "last_source_id": "source_id",
    "member_ids": "source_id",
    "former_doc_ids": "source_id",
    "same_raw_doc_ids": "source_id",
    "lookup_of_source_ids": "source_id",
    "run_id": "run_id",
    "evidence_id": "evidence_id",
    "supersedes": "evidence_id",
    "snapshot_id": "snapshot_id",
    "current_snapshot_id": "snapshot_id",
}


def id_format_of(name: str) -> str | None:
    """The S6 format of a leaf field called ``name``, or ``None`` if it carries no id."""
    if name in ("digest", "sha256", "convert_pin") or name.endswith(("_digest", "_digests", "_sha256")):
        return "digest"  # a fingerprint (the convert pin too), or a blob reference (D690: ``BLOB.sha256``)
    return ID_NAME_FORMAT.get(name)


# ---------------------------------------------------------------- schema walking
def walk(schema: pa.Schema) -> Iterator[tuple[str, pa.Field]]:
    """Every field of ``schema`` at every depth as ``(path, field)``, parents before children."""

    def rec(prefix: str, f: pa.Field) -> Iterator[tuple[str, pa.Field]]:
        path = f"{prefix}{f.name}"
        yield path, f
        t = f.type
        if pa.types.is_list(t):
            path, t = path + "[]", t.value_type
        if pa.types.is_struct(t):
            for sub in t:
                yield from rec(path + ".", sub)

    for f in schema:
        yield from rec("", f)


def _render_type(t: pa.DataType) -> object:
    if pa.types.is_struct(t):
        return {"struct": [[f.name, _render_type(f.type), f.nullable] for f in t]}
    if pa.types.is_list(t):
        return {"list": [_render_type(t.value_type), t.value_field.nullable]}
    if pa.types.is_nested(t):
        raise TypeError(f"schema_digest: unsupported nested type {t}")
    return str(t)


def schema_digest(schema: pa.Schema) -> str:
    """sha256 of the canonical rendering of ``schema`` without metadata (module docstring)."""
    rendered = [[f.name, _render_type(f.type), f.nullable] for f in schema]
    return hashlib.sha256(jcs(rendered).encode("utf-8")).hexdigest()


def _render_key_value(v: object) -> str:
    if isinstance(v, str):
        return v
    if isinstance(v, dt.datetime):
        if v.tzinfo is None:
            raise ValueError(f"ids_digest: naive timestamp {v!r}")
        return v.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    raise TypeError(f"ids_digest: key value of unsupported type {type(v).__name__}: {v!r}")


def ids_digest(table: pa.Table, key: tuple[str, ...]) -> str:
    """D19's ``ids_digest`` over the key columns of ``table`` (module docstring)."""
    cols = [table.column(k).to_pylist() for k in key]
    rows = sorted("\x1f".join(_render_key_value(v) for v in parts) for parts in zip(*cols))
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- the spec
@dataclass(frozen=True)
class FamilySpec:
    """One family: its schema, key and the per-family inputs of the S-checks."""

    name: str
    kind: str  # sources | coverage | documents | corpus | wf | evidence | overrides
    schema: pa.Schema
    key: tuple[str, ...]
    nested_unique: tuple[tuple[str, tuple[str, ...]], ...] = ()
    workflow: str | None = None
    schema_digest: str = field(init=False)
    status_pairs: tuple[tuple[str, str], ...] = field(init=False)
    id_paths: tuple[tuple[str, str], ...] = field(init=False)
    #: The current format version (module docstring); set by :func:`versioned`.
    version: int = field(init=False, default=0)

    def __post_init__(self) -> None:
        names = [f.name for f in self.schema]
        if len(set(names)) != len(names):
            raise ValueError(f"{self.name}: duplicate column names {names}")
        missing = [k for k in self.key if k not in names]
        if missing:
            raise ValueError(f"{self.name}: key columns {missing} not in the schema")
        pairs: list[tuple[str, str]] = []
        ids: list[tuple[str, str]] = []
        for path, f in walk(self.schema):
            sub = f.type.value_type if pa.types.is_list(f.type) else f.type
            if pa.types.is_struct(sub):
                inner = {x.name for x in sub}
                base = path + ("[]" if pa.types.is_list(f.type) else "") + "."
                for st, er in (("status", "error_class"), ("text_status", "text_error_class")):
                    if st in inner and er in inner:
                        pairs.append((base + st, base + er))
            fmt = id_format_of(f.name)
            if fmt is not None:
                if pa.types.is_string(f.type):
                    ids.append((path, fmt))
                elif pa.types.is_list(f.type) and pa.types.is_string(f.type.value_type):
                    ids.append((path + "[]", fmt))
        top = set(names)
        if "status" in top and "error_class" in top:
            pairs.insert(0, ("status", "error_class"))
        object.__setattr__(self, "schema_digest", schema_digest(self.schema))
        object.__setattr__(self, "status_pairs", tuple(pairs))
        object.__setattr__(self, "id_paths", tuple(ids))


def merge_columns(*groups: list[pa.Field]) -> list[pa.Field]:
    """Concatenate column groups, keeping the first occurrence of a repeated name; a repeat must
    have the same type and nullability (else ``ValueError``: two meanings for one column)."""
    out: dict[str, pa.Field] = {}
    for group in groups:
        for f in group:
            if f.name in out:
                if not out[f.name].equals(f):
                    raise ValueError(f"column {f.name!r} declared twice differently: {out[f.name]} vs {f}")
                continue
            out[f.name] = f
    return list(out.values())


def versioned(spec: FamilySpec) -> FamilySpec:
    """``spec`` with its ``version`` set to the newest number :mod:`stack.schemas.upcasters`
    registers for it (0: none). A schema that is not that version's still loads; ``write_family``
    refuses it (S7, :func:`stack.schemas.upcasters.registration_problem`)."""
    from stack.schemas import upcasters

    object.__setattr__(spec, "version", upcasters.newest(spec.name))
    return spec


# The family modules use FamilySpec; imported after it is defined.
from stack.schemas.families import coverage, documents, evidence, overrides, sources, wf
from stack.schemas.payloads.registry import REGISTRY

#: Workflows whose selected results live in ``documents.annotations`` and that have no ``<wf>``
#: family of their own (``triage`` is a stage, §2.2).
CHAIN_WORKFLOWS: frozenset[str] = frozenset({"triage"})


def _build() -> dict[str, FamilySpec]:
    fams = [sources.SPEC, coverage.SPEC, documents.SPEC, documents.CORPUS_SPEC, overrides.SPEC]
    for name, spec in REGISTRY.items():
        if name not in CHAIN_WORKFLOWS:
            fams.append(wf.spec(name))
        if spec.evidence and not spec.is_record:  # a record's evidence is its producer's
            fams.append(evidence.spec(name))
    out: dict[str, FamilySpec] = {}
    for f in fams:
        if f.name in out:
            raise ValueError(f"family name {f.name!r} registered twice")
        out[f.name] = versioned(f)
    return out


FAMILIES: dict[str, FamilySpec] = _build()


def family(name: str) -> FamilySpec:
    """The spec of family ``name``; ``KeyError`` names the registered families."""
    try:
        return FAMILIES[name]
    except KeyError:
        raise KeyError(f"unknown family {name!r}; registered: {list(FAMILIES)}") from None
