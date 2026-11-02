"""Step 4 of the fold (§3.2, D21, D24; T5.2; D647): release rules and the release writer.

A **release config** (``configs/releases/<name>.yaml``, :class:`ReleaseConfig`) says, per
``license_class``, whether rows ship (``rows: keep|drop``), what happens to the text
(``text: ship|pointer|withhold``), whether the abstract ships (``abstract: keep|drop``) and the
excerpt cap of the registry's ``text_bearing`` paths (``excerpt_cap_chars``; ``null`` = no cap); plus
``unknown_class`` (the class read for a supplying member that is missing or has a NULL class),
``other: drop|keep`` (``annotations.other[]``), ``column_drops`` and ``row_drops``; and (D647)
``audience: public|internal`` (advisory; ``internal`` requires ``--out`` unless it ships no bytes:
``content.select: none`` and no ``provider_records``),
``filter`` (a named filter or SQL predicate over the **input** rows, applied before the rules;
``screen_relevant`` = screen ``ok`` and verdict ``relevant``), ``snapshots: current|all`` (``current``
keeps only the snapshot ``content.current_snapshot_id`` names) and ``content`` = ``{select:
none|text|all, place: blobs|inline, max_total_bytes, missing: fail|pointer, provider_records}``.

**Whose licence** (C18): the member that supplied the thing.

* the **row** rule reads the anchor member (``identity.anchor_source_id``); a class with
  ``rows: drop`` drops the row, counted under the class name (``dropped.shadow``);
* the **abstract** reads ``metadata.field_sources[]``'s ``abstract`` entry (anchor when there is
  none); it is dropped when that class has ``abstract: drop`` or ``rows: drop``;
* the **text** reads ``content.current_source_id`` (anchor when NULL); ``withhold`` (or a class
  with ``rows: drop``) makes the row **withheld** (C25): ``current_text_status = 'withheld'`` and
  no blob reference anywhere in the row (``current_text_sha256``, ``snapshots[].raw.sha256`` and
  ``.text``, ``inband.entries[]``'s ``raw`` and ``text``); ``pointer`` keeps the references and ships no
  bytes. In a row that is not withheld, a snapshot whose own member's class withholds text loses its
  references, and so do that member's in-band entries (D647);
* **bytes** (D647, :func:`selected_refs`): a blob ships only when its own supplier's class has
  ``text: ship``: a kept snapshot's ``text`` under ``select: text|all``, its ``raw`` under
  ``select: all`` (the snapshot's ``source_id``); the current text when the text rule is ``ship`` and
  no selected snapshot element carries it (to ``blobs/``); with ``provider_records: true`` the
  ``provider_record`` in-band entries' blobs (D522: not content; opt-in, needs ``inband`` kept; inline
  into the entry's BLOB under ``place: inline``);
* **excerpts**: every registry ``text_bearing`` path of every typed annotation is cut to the
  ``excerpt_cap_chars`` of the text supplier's class (anchor when there is no content).

``row_drops`` tokens: ``masked`` (``identity.is_masked``), ``retired`` (``state``),
``sensitivity`` (a non-empty ``metadata.sensitivity``: §5.6, a leak's row is dropped). Reasons
are counted in that precedence: ``retired``, ``masked``, the anchor's class, ``sensitivity``.

``column_drops`` entries are dotted paths over the row with list levels transparent
(``inband``, ``annotations.screen.payload.reasoning``) or ``*.<name>`` = every field called
``<name>`` at any depth (``*.run_id``, ``*.evidence_id``). None
may drop a field of :data:`KEPT_FIELDS` (nor of :data:`KEPT_INBAND` while ``inband`` is kept) by its path,
an ancestor or ``*.<name>``: the writer fills bytes into those references and the licence is read from
those fields (:meth:`ReleaseConfig._check`, so an embedded config obeys it too); nor a field of
:data:`PRIVACY_FIELDS` (``identity.is_masked``, ``metadata.sensitivity``: the validator's masked and
sensitivity checks read them, and would pass vacuously without them; ``state`` may go); ``column_drops`` name
documents fields, and each must name one (a path of the documents schema, or a field name in it, D705): an
entry that names nothing would drop nothing, silently. The **release schema**
(:func:`release_schema`) is the ``documents`` schema minus ``column_drops`` (and ``annotations.other``
under ``other: drop``) with the documents' own names (D690: the pipeline and the release share one
vocabulary, which D647 introduced in the release): a blob reference is a ``BLOB`` struct named by its
role (``raw``, ``text``), a flat string reference ends in ``_sha256`` (``content.current_text_sha256``),
and ``*_digest`` is a fingerprint (``members_digest``, ``basis_digest``). The release :data:`BLOB` is the
pipeline's ``{sha256, bytes, media_type}`` plus ``inline`` (nullable ``large_binary``: the bytes, only
under ``content.place: inline``), every field nullable: a withheld snapshot keeps its ``raw`` with
``sha256`` NULL (C25) and loses its ``text``; a withheld in-band entry loses both. ``inline`` exists in the
release schema only: every family writer's S1 refuses it, so no build stage can write inline bytes. V10 compares a release file
against it with S1's function. A release without ``release_format: 2`` (written before D647, with
``text/<digest>.md``) is superseded: it is neither validated nor upcast; re-release instead.

Output (D21 as amended by D647): ``corpus.parquet`` (1024-row groups; footer ``stack.family =
release``, ``stack.file_id = <run_id>.release``, ``stack.blob_refs`` (JSON list of the ``BLOB`` and
``*_sha256`` paths), ``stack.blob_layout = blobs/<sha256>`` and ``release.json``'s scalar keys),
``release.json`` = ``{build_id, tag, config_digest, config, dropped: {reason: n}}`` plus
``corpus_file_id``, ``run_id``, ``row_count``, ``schema_digest``, ``release_format: 2``, ``file_id``,
``audience``, ``filter`` (resolved SQL), ``filter_checkable``, ``source``, ``rows_in``,
``filtered_out``, ``classes`` and ``content`` (counts), and (D705) ``build_status`` (:func:`build_status`:
``status`` ``ok``, ``incomplete`` or ``unbuilt``, with the build record's ``incomplete``,
``incomplete_allowed`` and ``stopped`` when it has them), ``blobs/<sha256>`` for the bytes placed
there (the store's blob grammar, no suffix) and
``missing-blobs.txt`` under ``missing: pointer`` when something was missing. Destinations: the
store's ``releases/<tag>/`` (staged under ``releases/.<tag>.<run_id>.tmp``, renamed into place; an
existing tag is refused; pushed by an uploading job) or, with ``out`` (``--out DIR``), a local
directory outside the store root that must not exist or be empty (staged at ``DIR/../.<name>.<run_id>.tmp``;
no run record, never pushed). :func:`resolve_destination` decides and checks the destination once, before
anything runs (D694; :class:`Destination`, which :func:`run_release` and :func:`write_release_dir` take). Both are
checked by :func:`stack.checks.fold_checks.validate_release` over the stage before the rename: a problem keeps
nothing (and pushes nothing).

**Pre-flight** (D647): before anything is written, every placed digest is looked up (local file,
else one LIST of the remote's ``blobs/``) with its size; a missing one refuses the release under
``missing: fail`` (under ``pointer`` it is listed in ``missing-blobs.txt`` and its element stays
NULL), and so does a total over ``max_total_bytes`` (inline bytes counted per element, ``blobs/``
per digest).

**Which corpus may be released** (:func:`release_gate`, D494; S25-14): one an ``ok`` build
record names (its ``files.corpus``; ``build_id`` = the newest such build); else, with ``allow_incomplete``
(``--allow-incomplete``), one an ``incomplete`` build names; else, with ``allow_unbuilt`` (``--allow-unbuilt``),
one no build record names at all (a standalone ``fold.build`` run; ``build_id = none``). A corpus only a
``failed``, ``cancelled`` or still ``running`` build names is refused whatever the flags (``failed[verify]``
included: it is not a verified corpus), and so is one only an ``incomplete`` build names without the flag.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import os
import shutil
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from stack import yamlio
from stack.core.hashes import jcs
from stack.fold import FoldError, corpus_input, filter_rows
from stack.query import EVERYTHING, resolve_filter
from stack.runs import FoldInfo, newest_finished_build, read_build_record
from stack.schemas.blocks import BLOB as PIPELINE_BLOB
from stack.schemas.blocks import DOCUMENT_FIELDS
from stack.schemas.families import FamilySpec, walk
from stack.schemas.families.documents import NESTED_UNIQUE
from stack.schemas.payloads.registry import REGISTRY
from stack.schemas.structs import BLOB_PARTS, blob_refs
from stack.schemas.vocab import LICENSE_CLASSES, RECORD_ROLES
from stack.store.blobs import BlobStore, blob_key, open_blobs
from stack.store.index import store_index
from stack.store.layout import BUILDS_DIR, HEADER_PREFIX, NONE, RELEASES
from stack.store.remote import open_store

REPO_ROOT = Path(__file__).resolve().parents[2]
RELEASE_CONFIGS = REPO_ROOT / "configs" / "releases"
ROW_DROPS: tuple[str, ...] = ("retired", "masked", "sensitivity")
WITHHELD = "withheld"
#: ``release.json`` ``release_format`` (D647: ``blobs/<sha256>``, the release form). The only format this
#: code writes or reads: an older release (absent = 1, ``text/<digest>.md``) is superseded, not upcast.
RELEASE_FORMAT = 2
#: The release schema's blob reference (D647, D690): the pipeline ``BLOB`` (``sha256``, NULL when withheld,
#: ``bytes``, ``media_type``) plus ``inline`` (the bytes themselves, only under ``place: inline``); nullable.
BLOB = pa.struct([pa.field("sha256", pa.string()), pa.field("bytes", pa.int64()),
                  pa.field("media_type", pa.string()), pa.field("inline", pa.large_binary())])
#: A blob reference's two roles in a snapshot or in-band entry (release form: one ``BLOB`` each).
PARTS = BLOB_PARTS
#: A withheld row's references (C25): none of these may be non-NULL.
WITHHELD_REFS: tuple[str, ...] = (
    "content.current_text_sha256", "content.snapshots.raw.sha256", "content.snapshots.text.sha256",
    "inband.entries.raw.sha256", "inband.entries.text.sha256")
#: Footer ``stack.blob_layout``: where a release's ``blobs/`` files are.
BLOB_LAYOUT = "blobs/<sha256>"
#: Documents fields no ``column_drops`` entry may drop (module docstring): the references the writer fills
#: (``bytes``, ``inline``) and the fields :func:`supplier_classes`, :func:`selected_refs` and the validator read.
KEPT_FIELDS: tuple[str, ...] = (
    "identity.anchor_source_id", "identity.members.source_id", "identity.members.license_class",
    "metadata.field_sources.field", "metadata.field_sources.source_id",
    "content.current_source_id", "content.current_snapshot_id", "content.current_text_sha256",
    "content.current_text_status", "content.snapshots.snapshot_id", "content.snapshots.source_id",
    "content.snapshots.raw.sha256", "content.snapshots.text.sha256")
#: Documents fields no ``column_drops`` entry may drop because the validator's privacy checks read them from
#: the shipped rows (:func:`stack.checks.fold_checks`: masked, sensitivity); dropped, those checks would pass
#: vacuously. ``state`` (the retired check) may be dropped.
PRIVACY_FIELDS: tuple[str, ...] = ("identity.is_masked", "metadata.sensitivity")
#: The same for ``inband.entries`` while ``inband`` is kept.
KEPT_INBAND: tuple[str, ...] = ("inband.entries.source_id", "inband.entries.content_role",
                                "inband.entries.raw.sha256", "inband.entries.text.sha256")
#: What a ``column_drops`` entry may name (D705): every dotted path of the documents schema (list levels
#: transparent) and every field name in it. An entry naming neither would drop nothing, silently.
DOC_PATHS: frozenset[str] = frozenset(p.replace("[]", "") for p, _f in walk(pa.schema(DOCUMENT_FIELDS)))
DOC_NAMES: frozenset[str] = frozenset(p.rsplit(".", 1)[-1] for p in DOC_PATHS)
#: ``release.json`` ``build_status.status`` (D705): the :func:`release_gate` outcome.
BUILD_STATUSES: tuple[str, ...] = ("ok", "incomplete", "unbuilt")
#: Rows per parquet row group of ``corpus.parquet`` (D647).
ROW_GROUP_ROWS = 1024
GIB = 1 << 30


# ---------------------------------------------------------------- config
class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LicenceRule(_Model):
    rows: Literal["keep", "drop"]
    text: Literal["ship", "pointer", "withhold"]
    abstract: Literal["keep", "drop"]
    excerpt_cap_chars: int | None  # None: no cap (D647)

    @field_validator("excerpt_cap_chars")
    @classmethod
    def _cap(cls, v: int | None) -> int | None:
        if v is not None and v < 0:
            raise ValueError("excerpt_cap_chars must be >= 0 (or null: no cap)")
        return v

    @property
    def withholds(self) -> bool:
        """Text from a member of this class does not ship (``withhold``, or rows dropped)."""
        return self.text == "withhold" or self.rows == "drop"

    @property
    def ships(self) -> bool:
        """Bytes supplied by a member of this class may ship (``text: ship`` and rows kept)."""
        return self.text == "ship" and not self.withholds


class ContentRule(_Model):
    """``content:`` of a release config (D647): which digests of the kept elements ship bytes
    (``select``), where (``place``), the size cap over every placed byte, what a digest neither local
    nor remote does (``missing``), and the opt-in for ``provider_record`` in-band bodies."""

    select: Literal["none", "text", "all"] = "text"
    place: Literal["blobs", "inline"] = "blobs"
    max_total_bytes: int = 4 * GIB
    missing: Literal["fail", "pointer"] = "fail"
    provider_records: bool = False

    @field_validator("max_total_bytes")
    @classmethod
    def _max(cls, v: int) -> int:
        if v < 0:
            raise ValueError("content.max_total_bytes must be >= 0")
        return v

    @property
    def ships_bytes(self) -> bool:
        """Whether this rule can ship any bytes: ``select`` is not ``none``, or ``provider_records`` (they
        ship whatever ``select`` says). An ``internal`` config that does needs ``--out`` (D647)."""
        return self.select != "none" or self.provider_records


class ReleaseConfig(_Model):
    """A release config (module docstring)."""

    name: str
    licence_rules: dict[str, LicenceRule]
    unknown_class: str = "restricted"
    other: Literal["drop", "keep"] = "drop"
    column_drops: tuple[str, ...] = ("inband", "identity.members_digest", "content.members_digest",
                                     "*.run_id", "*.evidence_id",
                                     "annotations.screen.payload.reasoning",
                                     "annotations.text_usability.payload.note")
    row_drops: tuple[str, ...] = ("masked", "retired", "sensitivity")
    audience: Literal["public", "internal"] = "public"
    filter: str = "everything"
    snapshots: Literal["current", "all"] = "current"
    content: ContentRule = ContentRule()

    @model_validator(mode="after")
    def _check(self) -> ReleaseConfig:
        missing = sorted(set(LICENSE_CLASSES) - set(self.licence_rules))
        extra = sorted(set(self.licence_rules) - set(LICENSE_CLASSES))
        if missing or extra:
            raise ValueError(f"licence_rules must name exactly {list(LICENSE_CLASSES)} "
                             f"(missing {missing}, unknown {extra})")
        if self.unknown_class not in self.licence_rules:
            raise ValueError(f"unknown_class {self.unknown_class!r} is not a licence class")
        bad = sorted(set(self.row_drops) - set(ROW_DROPS))
        if bad:
            raise ValueError(f"row_drops {bad} not in {list(ROW_DROPS)} (licence classes drop rows "
                             f"through licence_rules.<class>.rows)")
        for d in self.column_drops:
            if not d or d.startswith(".") or d.endswith(".") or ("*" in d and not
                                                                   (d.startswith("*.") and d.count("*") == 1)):
                raise ValueError(f"column_drops entry {d!r}: a dotted path or '*.<name>'")
            if not (d[2:] in DOC_NAMES if d.startswith("*.") else d in DOC_PATHS):
                raise ValueError(f"column_drops entry {d!r} names no documents field; it would drop nothing (D705)")
        if any(d in ("doc_id", "*.doc_id") for d in self.column_drops):
            raise ValueError("column_drops cannot drop doc_id (the release key)")
        inband_gone = _dropped_by(self.column_drops, "inband.entries") is not None
        for path in KEPT_FIELDS + (() if inband_gone else KEPT_INBAND):
            if (by := _dropped_by(self.column_drops, path)) is not None:
                raise ValueError(f"column_drops entry {by!r} drops {path}, which the release's bytes or its "
                                 "licence check read (D647)")
        for path in PRIVACY_FIELDS:
            if (by := _dropped_by(self.column_drops, path)) is not None:
                raise ValueError(f"column_drops entry {by!r} drops {path}, which the validator's privacy checks "
                                 "read; without it they pass vacuously (D647)")
        if not self.filter.strip():
            raise ValueError("filter: a named filter or a SQL predicate over the input rows")
        if self.content.provider_records:
            entry = _find(release_schema(self), "inband.entries")
            need = {"source_id", "content_role", "raw", "text"}
            if entry is None or not need <= set(entry.names):
                raise ValueError("content.provider_records needs inband.entries (source_id, content_role, "
                                 "raw, text) kept: remove 'inband' from column_drops")
        return self

    def rule(self, cls: str | None) -> LicenceRule:
        return self.licence_rules[cls if cls in self.licence_rules else self.unknown_class]

    def class_of(self, cls: str | None) -> str:
        return cls if cls in self.licence_rules else self.unknown_class

    def as_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @property
    def digest(self) -> str:
        """sha256 of the JCS of the resolved config (``release.json`` ``config_digest``)."""
        return config_json_digest(self.as_json())


def _dropped_by(drops: Iterable[str], path: str) -> str | None:
    """The ``column_drops`` entry that drops dotted ``path``: the path itself, an ancestor, or
    ``*.<name>`` naming one of its parts (as :func:`_drop` prunes); else ``None``."""
    parts = path.split(".")
    for d in drops:
        if (d[2:] in parts) if d.startswith("*.") else (d == path or path.startswith(d + ".")):
            return d
    return None


def config_json_digest(config: Mapping[str, Any]) -> str:
    """sha256 of the JCS of a config as JSON: :attr:`ReleaseConfig.digest`, and what V10 recomputes
    from ``release.json``'s embedded ``config`` as stored (D647: a re-dump through the current model
    would add defaulted keys and fail every older release)."""
    return hashlib.sha256(jcs(dict(config)).encode("utf-8")).hexdigest()


def _find(schema: pa.Schema, path: str) -> pa.StructType | None:
    """The struct type at dotted ``path`` (lists transparent) of ``schema``, else ``None``."""
    head, *rest = path.split(".")
    if head not in schema.names:
        return None
    t = schema.field(head).type
    for part in rest:
        if pa.types.is_list(t):
            t = t.value_type
        if not pa.types.is_struct(t) or t.get_field_index(part) < 0:
            return None
        t = t.field(part).type
    if pa.types.is_list(t):
        t = t.value_type
    return t if pa.types.is_struct(t) else None


def config_path(ref: str | Path) -> Path:
    """A release config by name (``public-v1`` = ``configs/releases/public-v1.yaml``) or path."""
    s = str(ref)
    if "/" in s or s.endswith((".yaml", ".yml")):
        p = Path(s)
        return p if p.is_absolute() or p.exists() else REPO_ROOT / p
    return RELEASE_CONFIGS / f"{s}.yaml"


def load_release_config(ref: str | Path) -> ReleaseConfig:
    """Load and validate a release config (``FoldError`` when missing or invalid). The file's
    ``name`` must equal its stem."""
    path = config_path(ref)
    if not path.exists():
        raise FoldError(f"release config {ref!r}: no file {path}")
    raw = yamlio.load(path.read_text("utf-8"))
    try:
        cfg = ReleaseConfig.model_validate(raw)
    except ValueError as exc:
        raise FoldError(f"release config {path}: {exc}") from exc
    if cfg.name != path.stem:
        raise FoldError(f"release config {path}: name {cfg.name!r} is not the file stem {path.stem!r}")
    return cfg


# ---------------------------------------------------------------- the release schema
def _drop(path: str, name: str, cfg: ReleaseConfig) -> bool:
    exact = {d for d in cfg.column_drops if not d.startswith("*.")}
    names = {d[2:] for d in cfg.column_drops if d.startswith("*.")}
    if cfg.other == "drop":
        exact.add("annotations.other")
    return path in exact or name in names


def _prune(f: pa.Field, path: str, cfg: ReleaseConfig) -> pa.Field:
    t = f.type
    name = path.rsplit(".", 1)[-1]
    if t == PIPELINE_BLOB:
        return pa.field(name, BLOB, True)
    if pa.types.is_list(t) and pa.types.is_struct(t.value_type):
        inner = _prune_struct(t.value_type, path, cfg)
        return pa.field(name, pa.list_(pa.field(t.value_field.name, inner, t.value_field.nullable)), f.nullable)
    if pa.types.is_struct(t):
        return pa.field(name, _prune_struct(t, path, cfg), f.nullable)
    return pa.field(name, t, f.nullable)


def _prune_struct(t: pa.StructType, path: str, cfg: ReleaseConfig) -> pa.StructType:
    return pa.struct([_prune(sub, f"{path}.{sub.name}", cfg) for sub in t
                      if not _drop(f"{path}.{sub.name}", sub.name, cfg)])


def release_row(row: dict[str, Any]) -> dict[str, Any]:
    """A row after the rules in the release form, in place: every ``raw``/``text`` BLOB of a snapshot or
    an in-band entry gains ``inline`` (NULL; the writer fills it under ``place: inline``). Returns ``row``."""
    for _holder, _i, _part, b in list(blob_refs(row)):
        b["inline"] = None
    return row


def ref_sha256(obj: Mapping[str, Any], part: str) -> str | None:
    """The sha256 a snapshot's or in-band entry's ``part`` (``raw``/``text``) BLOB names (``None``: no
    BLOB, or a withheld one)."""
    return (obj.get(part) or {}).get("sha256")


def blob_ref_paths(schema: pa.Schema) -> list[str]:
    """Footer ``stack.blob_refs``: every ``BLOB`` field and flat ``*_sha256`` reference of ``schema``."""
    return [p for p, f in walk(schema) if f.type == BLOB or p.removesuffix("[]").endswith(("_sha256", "_sha256s"))]


def release_schema(cfg: ReleaseConfig) -> pa.Schema:
    """The ``documents`` schema minus ``column_drops`` (and ``annotations.other`` under ``other: drop``),
    each pipeline ``BLOB`` as the release :data:`BLOB` (module docstring)."""
    return pa.schema([_prune(f, f.name, cfg) for f in DOCUMENT_FIELDS if not _drop(f.name, f.name, cfg)])


def release_spec(cfg: ReleaseConfig) -> FamilySpec:
    """A :class:`FamilySpec` over :func:`release_schema` for S1's comparison (V10)."""
    schema = release_schema(cfg)
    present = {f.name for f in schema}
    nested = tuple((p, k) for p, k in NESTED_UNIQUE if p.split(".")[0] in present)
    return FamilySpec(name=f"release:{cfg.name}", kind="release", schema=schema, key=("doc_id",),
                      nested_unique=nested)


# ---------------------------------------------------------------- per-path helpers
def _cap_path(obj: Any, parts: list[str], cap: int) -> None:
    """Cut every string at ``parts`` (``a``, ``b[]``, ``c``) under ``obj`` to ``cap`` chars."""
    if obj is None or not parts:
        return
    head, rest = parts[0], parts[1:]
    is_list = head.endswith("[]")
    key = head.removesuffix("[]")
    if not isinstance(obj, dict) or key not in obj:
        return
    if not rest:
        if isinstance(obj[key], str):
            obj[key] = obj[key][:cap]
        elif is_list and isinstance(obj[key], list):
            obj[key] = [v[:cap] if isinstance(v, str) else v for v in obj[key]]
        return
    vals = obj[key] if is_list else [obj[key]]
    for v in vals or []:
        _cap_path(v, rest, cap)


def text_bearing_values(payload: Any, path: str) -> list[str]:
    """Every string at registry ``text_bearing`` ``path`` (``a.b[].c``) under ``payload``."""
    out: list[str] = []

    def walk(obj: Any, parts: list[str]) -> None:
        if obj is None or not parts or not isinstance(obj, dict):
            return
        head, rest = parts[0], parts[1:]
        key = head.removesuffix("[]")
        val = obj.get(key)
        vals = (val or []) if head.endswith("[]") else [val]
        for v in vals:
            if not rest:
                if isinstance(v, str):
                    out.append(v)
            else:
                walk(v, rest)

    walk(payload, path.split("."))
    return out


def _members(row: Mapping[str, Any]) -> dict[str, str | None]:
    return {m["source_id"]: m.get("license_class") for m in row["identity"]["members"]}


def supplier_classes(row: Mapping[str, Any], cfg: ReleaseConfig) -> dict[str, str]:
    """``{anchor, abstract, text}`` → the licence class read for each (C18; module docstring)."""
    members = _members(row)
    anchor = row["identity"]["anchor_source_id"]

    def cls(source_id: str | None) -> str:
        return cfg.class_of(members.get(source_id or anchor))

    abstract_src = next((fs["source_id"] for fs in row["metadata"]["field_sources"]
                         if fs["field"] == "abstract"), None)
    content = row.get("content")
    text_src = content.get("current_source_id") if content else None
    return {"anchor": cls(anchor), "abstract": cls(abstract_src), "text": cls(text_src)}


def _withhold_snapshot(snap: dict[str, Any]) -> None:
    snap["raw"] = {**snap["raw"], "sha256": None}
    snap["text"] = None


def _withhold_entry(e: dict[str, Any]) -> None:
    e["raw"], e["text"] = None, None


def _withhold(row: dict[str, Any]) -> None:
    c = row["content"]
    c["current_text_status"], c["current_text_sha256"] = WITHHELD, None
    for snap in c["snapshots"]:
        _withhold_snapshot(snap)
    for e in (row.get("inband") or {}).get("entries") or []:
        _withhold_entry(e)


def _withhold_members(row: dict[str, Any], cfg: ReleaseConfig) -> None:
    """In a row that is not withheld: a withholding member's snapshots and in-band entries lose their
    references (D647 adds the entries)."""
    members = _members(row)

    def withholds(source_id: str | None) -> bool:
        return cfg.rule(cfg.class_of(members.get(source_id or ""))).withholds

    c = row.get("content")
    for snap in (c or {}).get("snapshots") or []:
        if withholds(snap["source_id"]):
            _withhold_snapshot(snap)
    for e in (row.get("inband") or {}).get("entries") or []:
        if withholds(e["source_id"]):
            _withhold_entry(e)


@dataclass(frozen=True)
class Ref:
    """One selected blob of a release row (D647, :func:`selected_refs`): ``where`` ``snapshot`` or
    ``record`` names the ``part`` (``raw``/``text``) BLOB of ``content.snapshots[index]`` or of the
    ``provider_record`` entry ``inband.entries[index]``; ``current`` is the current text when no
    selected snapshot element carries it (no BLOB of its own: only ever under ``blobs/``)."""

    digest: str
    where: Literal["snapshot", "record", "current"]
    part: str | None = None
    index: int | None = None

    @property
    def slot(self) -> str:
        """``raw``, ``text``, ``current`` or ``record`` (for counts and messages)."""
        return self.where if self.where != "snapshot" else str(self.part)

    def inline(self, cfg: ReleaseConfig) -> bool:
        """Whether this element's bytes go into its BLOB's ``inline`` (else ``blobs/``)."""
        return cfg.content.place == "inline" and self.where != "current"


def selected_refs(row: Mapping[str, Any], cfg: ReleaseConfig) -> list[Ref]:
    """The digests of a row **after the rules** (as written, or read back from the release file)
    that ship bytes under ``cfg`` (module docstring, "bytes"): each element independently, per its own
    supplier's licence; a withheld row ships nothing. The writer and the validator share it."""
    content = row.get("content")
    if content is not None and content.get("current_text_status") == WITHHELD:
        return []
    members = _members(row)

    def ships(source_id: str | None) -> bool:
        return cfg.rule(cfg.class_of(members.get(source_id or ""))).ships

    out: list[Ref] = []
    sel = cfg.content.select
    if content is not None and sel != "none":
        for i, snap in enumerate(content.get("snapshots") or []):
            if not ships(snap.get("source_id")):
                continue
            for part in ("text", "raw") if sel == "all" else ("text",):
                d = ref_sha256(snap, part)
                if d:
                    out.append(Ref(d, "snapshot", part, i))
        cur = content.get("current_text_sha256")
        anchor = row["identity"]["anchor_source_id"]
        if cur and ships(content.get("current_source_id") or anchor) and \
                not any(r.part == "text" and r.digest == cur for r in out):
            out.append(Ref(cur, "current"))
    if cfg.content.provider_records:
        for i, e in enumerate((row.get("inband") or {}).get("entries") or []):
            if e.get("content_role") in RECORD_ROLES and ships(e.get("source_id")):
                for part in PARTS:
                    d = ref_sha256(e, part)
                    if d:
                        out.append(Ref(d, "record", part, i))
    return out


def rule_rows(rows: list[dict[str, Any]], cfg: ReleaseConfig) -> tuple[list[dict[str, Any]], dict[str, int], list[list[Ref]]]:
    """Licence rules, row drops, snapshot pruning, excerpt caps over ``rows`` (copies of documents
    rows). Returns ``(kept rows in the release form (:func:`release_row`), dropped {reason: n}, the
    selected refs of each kept row)``; columns are dropped by the release schema."""
    kept: list[dict[str, Any]] = []
    dropped: dict[str, int] = {}
    refs: list[list[Ref]] = []
    for src in rows:
        row = copy.deepcopy(src)
        cls = supplier_classes(row, cfg)
        reason = None
        if "retired" in cfg.row_drops and row["state"] == "retired":
            reason = "retired"
        elif "masked" in cfg.row_drops and row["identity"]["is_masked"]:
            reason = "masked"
        elif cfg.rule(cls["anchor"]).rows == "drop":
            reason = cls["anchor"]
        elif "sensitivity" in cfg.row_drops and row["metadata"]["sensitivity"]:
            reason = "sensitivity"
        if reason is not None:
            dropped[reason] = dropped.get(reason, 0) + 1
            continue
        arule = cfg.rule(cls["abstract"])
        if arule.abstract == "drop" or arule.rows == "drop":
            row["metadata"]["abstract"] = None
        c = row.get("content")
        if c is not None and cfg.rule(cls["text"]).withholds:
            _withhold(row)
        else:
            _withhold_members(row, cfg)
        if c is not None and cfg.snapshots == "current":  # D647: one snapshot per row
            c["snapshots"] = [s for s in c["snapshots"] if s["snapshot_id"] == c.get("current_snapshot_id")]
        cap = cfg.rule(cls["text"]).excerpt_cap_chars
        ann = row["annotations"]
        if cap is not None:
            for wf, spec in REGISTRY.items():
                a = ann.get(wf)
                if a is not None and a.get("payload") is not None:
                    for path in spec.text_bearing:
                        _cap_path(a["payload"], path.split("."), cap)
        if cfg.other == "drop":
            ann["other"] = []
        refs.append(selected_refs(row, cfg))
        kept.append(release_row(row))
    return kept, dropped, refs


def apply_rules(rows: list[dict[str, Any]], cfg: ReleaseConfig) -> tuple[list[dict[str, Any]], dict[str, int], list[str]]:
    """:func:`rule_rows` with the refs flattened: ``(kept rows, dropped {reason: n}, the distinct
    digests that ship bytes, sorted)``."""
    kept, dropped, refs = rule_rows(rows, cfg)
    return kept, dropped, sorted({r.digest for rr in refs for r in rr})


# ---------------------------------------------------------------- writing
@dataclass(frozen=True)
class ReleaseResult:
    """What ``fold.release`` wrote."""

    path: Path  # releases/<tag>/
    tag: str
    build_id: str
    corpus_file_id: str
    config: ReleaseConfig
    dropped: dict[str, int]
    rows_in: int
    rows_out: int
    fold: FoldInfo
    release_json: dict[str, Any]
    filtered_out: int = 0
    out: bool = False  # written to a local --out directory (no run record, never pushed)

    @property
    def corpus_path(self) -> Path:
        return self.path / "corpus.parquet"


def default_tag(cfg: ReleaseConfig, at: dt.datetime | None = None) -> str:
    """``<release config name>-<YYYYMMDD>`` (UTC)."""
    return f"{cfg.name}-{(at or dt.datetime.now(dt.UTC)).astimezone(dt.UTC):%Y%m%d}"


def build_id_for(store_root: Path, corpus_file_id: str, status: str = "ok") -> str:
    """The id of the newest build record of ``status`` (default ``ok``) whose ``files.corpus`` is
    ``corpus_file_id``; ``'none'`` when no such build record names it."""
    hits = []
    for b in store_index(store_root).builds:
        rec = b.record
        if rec.get("status") == status and ((rec.get("files") or {}).get("corpus") or {}).get("file_id") == corpus_file_id:
            hits.append((str(rec.get("finished_at") or ""), b.name, str(rec.get("build_id"))))
    return max(hits)[2] if hits else NONE


def release_gate(store_root: str | Path, corpus_file_id: str, *, allow_incomplete: bool = False,
                 allow_unbuilt: bool = False) -> tuple[str, str]:
    """``(build_id, status)`` of the build a release of ``corpus_file_id`` records (module docstring, "Which
    corpus may be released"; D494): the newest ``ok`` build naming it, else (``allow_incomplete``) the newest
    ``incomplete`` one, else (``allow_unbuilt``, no build names it) ``('none', 'unbuilt')``. Raises
    :class:`FoldError` naming the build and its status otherwise."""
    root = Path(store_root)
    ok = build_id_for(root, corpus_file_id)
    if ok != NONE:
        return ok, "ok"
    inc = build_id_for(root, corpus_file_id, "incomplete")
    if inc != NONE:
        if allow_incomplete:
            return inc, "incomplete"
        raise FoldError(f"corpus {corpus_file_id} is the corpus of build {inc!r}, which is incomplete (a stage "
                        "stopped short); release it only with --allow-incomplete, or rerun the build to complete it")
    other = [(st, bid) for st in ("failed", "cancelled", "running")
             if (bid := build_id_for(root, corpus_file_id, st)) != NONE]
    if other:
        named = ", ".join(f"{bid!r} ({st})" for st, bid in other)
        raise FoldError(f"corpus {corpus_file_id} is named only by build {named}: not a verified corpus; "
                        "release the corpus of an ok build (or --allow-incomplete for an incomplete one)")
    if allow_unbuilt:
        return NONE, "unbuilt"
    raise FoldError(f"corpus {corpus_file_id}: no build record names it (a standalone fold.build run?); release it "
                    "only with --allow-unbuilt, or release a build's corpus")


def build_status(store_root: str | Path, build_id: str, status: str) -> dict[str, Any]:
    """``release.json`` ``build_status`` (D705) for the build :func:`release_gate` admitted: ``{status}``
    (``ok``, ``incomplete``, ``unbuilt``) plus what stopped short, copied from the build record when it
    carries them: ``incomplete`` and ``incomplete_allowed`` (``[{stage, run_id, reasons}]``) and ``stopped``."""
    if status not in BUILD_STATUSES:
        raise FoldError(f"build_status: {status!r} is not one of {list(BUILD_STATUSES)}")
    out: dict[str, Any] = {"status": status}
    if build_id == NONE:
        return out
    entry = store_index(Path(store_root)).build(build_id)
    if entry is None:
        raise FoldError(f"build_status: no build record carries id {build_id!r}")
    for k in ("incomplete", "incomplete_allowed", "stopped"):
        if entry.record.get(k):
            out[k] = entry.record[k]
    return out


RELEASE_JSON = "release.json"


def release_exists(store_root: str | Path, tag: str) -> bool:
    """Whether release ``tag`` exists: ``releases/<tag>/release.json`` locally or, on an S3 store,
    on S3 (a HEAD; D346). A tag directory without it is incomplete, not a release."""
    store = open_store(store_root)
    key = f"{RELEASES}/{tag}/{RELEASE_JSON}"
    if store.path(key).is_file():
        return True
    return store.remote is not None and store.remote.head(key) is not None


def resolve_build(store_root: Path, ref: str, plan: str | None = None) -> dict[str, Any]:
    """A build record by ``build_id``, by record path, or ``latest`` (the newest ``ok`` or ``incomplete``
    build of ``plan``, :func:`stack.runs.newest_finished_build`, D703). Any status by id or path. Resolving
    a build releases nothing: :func:`release_gate` judges a release's corpus by the builds naming it."""
    if ref == "latest":
        hit = newest_finished_build(store_root, plan)
        if hit is None:
            raise FoldError(f"no ok or incomplete build{f' of plan {plan!r}' if plan else ''} "
                            f"under {store_root / BUILDS_DIR}")
        return hit[1]
    p = Path(ref)
    if p.suffix == ".zst" or "/" in ref:
        p = p if p.is_absolute() or p.exists() else store_root / p
        if not p.exists():
            raise FoldError(f"build {ref!r}: no such record")
        return read_build_record(p)
    entry = store_index(store_root).build(ref)
    if entry is None:
        raise FoldError(f"build {ref!r}: no build record carries that id")
    return entry.record


#: Rows of the build's corpus turned into dicts at a time by :func:`run_release` (the rules are
#: row-wise): the whole corpus as dicts was ~53 kB per released row, the largest fold footprint
#: (T-SCALE, measurement S-17: 2.4 GB at 150k documents).
RELEASE_BATCH_ROWS = 4096


def check_out_dir(out: str | Path, store_root: str | Path | None = None) -> Path:
    """``--out DIR`` (D647): a directory that does not exist or is empty, whose parent exists and, with
    ``store_root``, that is not under the store root (an export in ``releases/`` would be read by V10 as a
    store release and block its tag); :class:`FoldError` otherwise. Returns it absolute."""
    d = Path(out).absolute()
    if store_root is not None and d.resolve().is_relative_to(Path(store_root).resolve()):
        raise FoldError(f"--out {out}: under the store root {store_root}; an export goes outside the store")
    if d.exists() and (not d.is_dir() or any(d.iterdir())):
        raise FoldError(f"--out {out}: exists and is not an empty directory; an export never overwrites")
    if not d.parent.is_dir():
        raise FoldError(f"--out {out}: parent directory {d.parent} does not exist")
    return d


def check_tag(tag: str | None) -> str:
    """A release tag: a plain directory name (no ``/``, not ``.``-prefixed: staging directories are);
    :class:`FoldError` otherwise. Returns it."""
    if not tag or "/" in tag or tag.startswith("."):
        raise FoldError(f"release tag {tag!r}: a plain directory name")
    return tag


@dataclass(frozen=True)
class Destination:
    """Where a release is written (D647, D694), from :func:`resolve_destination`: the store's
    ``releases/<tag>/`` (``out`` None: a recorded run, pushed with its job) or the local export directory
    ``out`` (absolute; ``tag`` is its name; no run record, never pushed)."""

    tag: str
    out: Path | None = None


def resolve_destination(store_root: str | Path, cfg: ReleaseConfig, out: str | Path | None = None,
                        tag: str | None = None, *, now: dt.datetime | None = None) -> Destination:
    """The one destination check of a release (D694), made once, before anything runs or is written. Into the
    store (``out`` None): a config with ``audience: internal`` that ships bytes is refused (D647: it is
    exported with ``--out``), and the tag (default :func:`default_tag` at ``now``) must be a plain name
    (:func:`check_tag`) that is no release yet (:func:`release_exists`, the remote's ``release.json`` too). An
    export (``out``; from the CLI only ``stack export``, D695): :func:`check_out_dir`; the tag is the
    directory's name, a ``tag`` naming another one is refused. :class:`FoldError` names the refusal."""
    root = Path(store_root)
    if out is None:
        if cfg.audience == "internal" and cfg.content.ships_bytes:
            raise FoldError(f"release config {cfg.name}: audience internal ships bytes; export it to a local "
                            "directory with `stack export … --out DIR` (D647), not into the store's releases/")
        name = check_tag(tag or default_tag(cfg, now))
        if release_exists(root, name):
            raise FoldError(f"release tag {name!r} exists ({RELEASES}/{name}/{RELEASE_JSON}); releases are never "
                            "overwritten")
        return Destination(name)
    d = check_out_dir(out, root)
    if tag is not None and tag != d.name:
        raise FoldError(f"--tag {tag!r} with --out: the tag of an export is its directory's name ({d.name!r})")
    return Destination(check_tag(d.name), d)


@dataclass(frozen=True)
class Placement:
    """The pre-flight's result (D647): the selected refs per row, which digests are placed where,
    the sizes, and what is missing."""

    refs: list[list[list[Ref]]]  # per part, per row
    sizes: dict[str, int]
    missing: list[str]
    blobs: list[str]  # digests copied to blobs/
    counts: dict[str, int]


def preflight(blobs: BlobStore, parts_refs: list[list[list[Ref]]], cfg: ReleaseConfig) -> Placement:
    """Availability and sizes of every selected digest (local file, else one LIST of the remote's
    ``blobs/``: :meth:`stack.store.blobs.BlobStore.sizes`); refuses (:class:`FoldError`, nothing
    written) on a missing digest under ``missing: fail`` or a total over ``max_total_bytes``
    (module docstring, "Pre-flight")."""
    all_refs = [r for part in parts_refs for rr in part for r in rr]
    for r in all_refs:
        try:
            blob_key(r.digest)
        except ValueError:
            raise FoldError(f"pre-flight: selected digest {r.digest!r} is not a sha256 hex digest; "
                            "nothing written") from None
    try:
        sizes, missing = blobs.sizes(r.digest for r in all_refs)
    except Exception as exc:  # transport/auth (botocore's errors are plain Exceptions): not "absent";
        # no error body, no chained cause (it may carry a URL)
        raise FoldError(f"pre-flight: the blob lookup failed ({type(exc).__name__}); nothing written") from None
    if missing and cfg.content.missing == "fail":
        ex = ", ".join(sorted(missing)[:3])
        raise FoldError(f"pre-flight: {len(missing)} selected blob(s) neither local nor remote (e.g. {ex}); "
                        "nothing written (content.missing: pointer lists them in missing-blobs.txt instead)")
    inline = [r for r in all_refs if r.inline(cfg) and r.digest not in missing]
    to_blobs = sorted({r.digest for r in all_refs if not r.inline(cfg) and r.digest not in missing})
    counts = {"elements": len(all_refs), "inline_count": len(inline),
              "inline_bytes": sum(sizes[r.digest] for r in inline), "blobs_count": len(to_blobs),
              "blobs_bytes": sum(sizes[d] for d in to_blobs), "records": sum(r.slot == "record" for r in all_refs),
              "missing": len(missing)}
    total = counts["inline_bytes"] + counts["blobs_bytes"]
    if total > cfg.content.max_total_bytes:
        raise FoldError(f"pre-flight: the selection places {total} bytes ({counts['inline_bytes']} inline, "
                        f"{counts['blobs_bytes']} in blobs/), over content.max_total_bytes "
                        f"{cfg.content.max_total_bytes}; nothing written")
    return Placement(parts_refs, sizes, sorted(missing), to_blobs, counts)


def _blob_of(row: dict[str, Any], r: Ref) -> dict[str, Any] | None:
    holder = {"snapshot": lambda: row["content"]["snapshots"], "record": lambda: row["inband"]["entries"]}.get(r.where)
    if holder is None:
        return None
    b: dict[str, Any] | None = holder()[r.index][r.part]
    return b


def _fill(table: pa.Table, refs: list[list[Ref]], blobs: BlobStore, plc: Placement, cfg: ReleaseConfig,
          schema: pa.Schema) -> Iterator[pa.Table]:
    """``table`` (rows in ``refs`` order) with each selected, available element's BLOB ``inline`` filled
    under ``place: inline`` (its ``bytes`` are the corpus's, D690); a row group at a time (the bytes of one
    group in memory at once)."""
    missing = set(plc.missing)
    for start in range(0, table.num_rows, ROW_GROUP_ROWS):
        rows = table.slice(start, ROW_GROUP_ROWS).to_pylist()
        for row, rr in zip(rows, refs[start:start + ROW_GROUP_ROWS], strict=True):
            for r in rr:
                b = _blob_of(row, r)
                if b is not None and r.digest not in missing and r.inline(cfg):
                    b["inline"] = blobs.get(r.digest)
        yield pa.Table.from_pylist(rows, schema=schema)


def _filter_holds(tables: Iterable[pa.Table], sql: str) -> bool:
    """Whether the filter evaluates over the release rows and every row satisfies it (V10 then
    re-checks it: ``filter_checkable``)."""
    if sql == EVERYTHING:
        return True
    try:
        return all(filter_rows(t, sql).num_rows == t.num_rows for t in tables if t.num_rows)
    except FoldError:
        return False


def run_release(store_root: str | Path, *, run_id: str, inputs: Mapping[str, str], release: str | Path,
                dest: Destination, allow_incomplete: bool = False, allow_unbuilt: bool = False) -> ReleaseResult:
    """``fold.release``: read a build's ``corpus`` file (``inputs = {'corpus': <file id or
    path>}``, header ``fold_config = build``), apply ``release`` (a config name or path): filter, rules,
    pre-flight, write ``dest`` (:func:`resolve_destination`, which checked it: ``releases/<tag>/`` or the
    local directory ``dest.out``; module docstring). ``build_id`` (in ``release.json``): the build
    :func:`release_gate` admits (an ``ok`` build naming the corpus; an ``incomplete`` one with
    ``allow_incomplete``; ``none`` for an unbuilt corpus with ``allow_unbuilt``); any other corpus is refused
    before anything is written (D494), and so is an input no ``ok`` run record names (D345). Validated over
    the stage before the rename, either destination."""
    root = Path(store_root)
    if set(inputs) != {"corpus"}:
        raise FoldError(f"fold.release takes inputs ['corpus'], got {sorted(inputs)}")
    cfg = load_release_config(release)
    tbl, h, cpath = corpus_input(root, inputs["corpus"], fold_config="build")
    tag = dest.tag
    bid, status = release_gate(root, h["file_id"], allow_incomplete=allow_incomplete, allow_unbuilt=allow_unbuilt)
    bstatus = build_status(root, bid, status)
    pred = resolve_filter(cfg.filter)
    picked = tbl if pred == EVERYTHING else filter_rows(tbl, pred)  # over the input rows (D647)
    schema = release_schema(cfg)
    parts: list[pa.Table] = []
    parts_refs: list[list[list[Ref]]] = []
    dropped: dict[str, int] = {}
    classes: dict[str, int] = {}
    for batch in picked.to_batches(max_chunksize=RELEASE_BATCH_ROWS):  # 4. release rules, per batch (T-SCALE)
        kept, drops, refs = rule_rows(batch.to_pylist(), cfg)
        for row in kept:
            c = supplier_classes(row, cfg)["text"]
            classes[c] = classes.get(c, 0) + 1
        parts.append(pa.Table.from_pylist(kept, schema=schema))
        parts_refs.append(refs)
        for reason, n in drops.items():
            dropped[reason] = dropped.get(reason, 0) + n
    filtered_out = tbl.num_rows - picked.num_rows
    del picked
    blobs = open_blobs(root)
    plc = preflight(blobs, parts_refs, cfg)
    rows_out = sum(t.num_rows for t in parts)
    folded = json.loads(h.get("annotations_folded") or "[]")
    fold: FoldInfo = {"config": "release", "filter": pred, "joined": {},
                      "annotations_folded": [str(w) for w in folded], "release_config": cfg.name}
    spec = release_spec(cfg)
    try:
        source_path = cpath.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        source_path = cpath.name
    doc = {"build_id": bid, "tag": tag, "config_digest": cfg.digest, "config": cfg.as_json(),
           "dropped": dict(sorted(dropped.items())), "corpus_file_id": h["file_id"], "run_id": run_id,
           "row_count": rows_out, "schema_digest": spec.schema_digest,
           "release_format": RELEASE_FORMAT, "file_id": f"{run_id}.release", "audience": cfg.audience,
           "filter": pred, "filter_checkable": _filter_holds(parts, pred),
           "source": {"file_id": h["file_id"], "path": source_path, "family_version": h.get("family_version")},
           "rows_in": tbl.num_rows, "filtered_out": filtered_out, "build_status": bstatus,
           "classes": dict(sorted(classes.items())),
           "content": {"snapshots": cfg.snapshots, "select": cfg.content.select, "place": cfg.content.place,
                       "provider_records": cfg.content.provider_records, "missing_policy": cfg.content.missing,
                       "max_total_bytes": cfg.content.max_total_bytes, **plc.counts}}
    rows_in = tbl.num_rows
    del tbl
    if plc.counts["elements"]:
        tables: Iterable[pa.Table] = (t for part, refs in zip(parts, parts_refs, strict=True)
                                      for t in _fill(part, refs, blobs, plc, cfg, schema))
    else:
        tables = parts
    final = write_release_dir(root, dest, tables, doc, plc.blobs, schema=schema, validate=True,
                              missing=plc.missing, blob_store=blobs)
    return ReleaseResult(final, tag, bid, h["file_id"], cfg, dropped, rows_in, rows_out, fold, doc,
                         filtered_out=filtered_out, out=dest.out is not None)


def write_release_dir(store_root: str | Path, dest: Destination, table: pa.Table | Iterable[pa.Table],
                      doc: Mapping[str, Any], ship: list[str] | None = None, *, schema: pa.Schema | None = None,
                      push: bool = True, validate: bool = False,
                      missing: list[str] | None = None, blob_store: BlobStore | None = None) -> Path:
    """Write a release directory = ``corpus.parquet`` (``table``, or the tables of an iterable streamed
    through one writer under ``schema``, in 1024-row groups, with a small ``stack.*`` footer:
    ``family = release``, ``file_id`` when ``doc`` has one, and ``release.json``'s scalar keys),
    ``release.json`` = ``doc``, ``blobs/<digest>`` copied from the store's blobs for each ``ship``
    digest and ``missing-blobs.txt`` (one digest per line) when ``missing`` is non-empty.

    ``dest`` is :func:`resolve_destination`'s (the tag and the export directory are checked there). In the
    store (``dest.out`` None): ``releases/<tag>/``, staged under ``releases/.<tag>.<run_id>.tmp``
    (``release.json`` written last) and renamed into place; a tag whose ``release.json`` appeared meanwhile
    is refused, a tag directory without ``release.json`` (an interrupted release) is replaced; when
    ``push`` and the active job uploads (:func:`stack.store.job.current_job`), the directory is PUT after
    the rename, ``release.json`` last, before the run's final record. An export (``dest.out``): staged at
    ``out/../.<name>.<run_id>.tmp``, refused if the directory was filled meanwhile, never pushed.
    ``validate``: :func:`stack.checks.fold_checks.validate_release` over the stage before the rename; any
    problem refuses (the stage is removed)."""
    from stack.store.job import current_job, push_release_dir

    root = Path(store_root)
    tag, out = dest.tag, dest.out
    if out is None:
        final = root / RELEASES / tag
        stage = root / RELEASES / f".{tag}.{doc['run_id']}.tmp"
    else:
        final = out
        stage = final.parent / f".{final.name}.{doc['run_id']}.tmp"
    keys = ("tag", "build_id", "corpus_file_id", "config_digest", "run_id", "schema_digest", "row_count",
            "file_id", "release_format")
    tables: Iterable[pa.Table] = [table] if isinstance(table, pa.Table) else table
    if schema is None:
        if not isinstance(table, pa.Table):
            raise TypeError("write_release_dir: an iterable of tables needs schema=")
        schema = table.schema
    extra = {"blob_refs": json.dumps(blob_ref_paths(schema)), "blob_layout": BLOB_LAYOUT}
    meta = {f"{HEADER_PREFIX}{k}".encode(): str(v).encode()
            for k, v in {"family": "release", "release_config": doc["config"]["name"],
                         **{k: doc[k] for k in keys if k in doc}, **extra}.items()}
    stage.mkdir(parents=True)
    try:
        with pq.ParquetWriter(stage / "corpus.parquet", schema.with_metadata(meta), compression="zstd") as w:
            for t in tables:
                if t.num_rows:
                    w.write_table(t.replace_schema_metadata(meta), row_group_size=ROW_GROUP_ROWS)
        if ship:
            blobs = blob_store or open_blobs(root)
            (stage / "blobs").mkdir()
            for d in ship:  # a copy, never a hard link: an edit in the export must not reach the store
                shutil.copyfile(blobs.path(d), stage / "blobs" / d)
        if missing:
            (stage / "missing-blobs.txt").write_text("".join(f"{d}\n" for d in missing), "utf-8")
        (stage / RELEASE_JSON).write_text(json.dumps(dict(doc), indent=2, sort_keys=True) + "\n", "utf-8")
        if validate:
            from stack.checks.fold_checks import validate_release

            problems = validate_release(stage, tag=final.name)
            if problems:
                raise FoldError(f"release {final.name}: {len(problems)} validation problem(s), nothing kept: "
                                + "; ".join(problems[:5]))
        if out is None:
            if (final / RELEASE_JSON).exists():
                raise FoldError(f"release tag {tag!r} appeared while writing; not overwritten")
            if final.exists():  # incomplete: no release.json (D346)
                shutil.rmtree(final)
        else:
            check_out_dir(final, root)  # filled while writing: an export never overwrites
            if final.exists():
                final.rmdir()
        os.rename(stage, final)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    job = current_job()
    if out is None and push and job is not None and job.upload:  # the files, release.json last (D346, D355)
        push_release_dir(job.store, tag)
    return final


__all__ = [
    "BLOB",
    "BUILD_STATUSES",
    "DOC_NAMES",
    "DOC_PATHS",
    "KEPT_FIELDS",
    "KEPT_INBAND",
    "PARTS",
    "RELEASE_CONFIGS",
    "RELEASE_FORMAT",
    "WITHHELD",
    "WITHHELD_REFS",
    "ContentRule",
    "Destination",
    "LicenceRule",
    "Ref",
    "ReleaseConfig",
    "ReleaseResult",
    "apply_rules",
    "blob_ref_paths",
    "build_id_for",
    "build_status",
    "check_out_dir",
    "check_tag",
    "config_json_digest",
    "config_path",
    "default_tag",
    "load_release_config",
    "preflight",
    "ref_sha256",
    "release_exists",
    "release_gate",
    "release_row",
    "release_schema",
    "release_spec",
    "resolve_build",
    "resolve_destination",
    "rule_rows",
    "run_release",
    "selected_refs",
    "supplier_classes",
    "text_bearing_values",
    "write_release_dir",
]
