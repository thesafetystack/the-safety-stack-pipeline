"""Overrides (D336-D338): the ``overrides`` family, the input formats
``overrides.add`` reads, and the one effective rule every consumer applies.

**Storage.** One chain, ``overrides`` (family :mod:`stack.schemas.families.overrides`, key
``override_id``), written only by the stage ``overrides.add`` (``stack overrides add <file>``,
``workflows/overrides/add/main.py``): an add emits its rows as a part and, without ``--part``, the
runner writes the full file that part makes. Nothing is edited in place; the view is the union of
every row. Consumers pin the chain head like any other input (``--in data/overrides/<series>/``;
``stack build`` passes it to ``gather``, ``collect.manual`` and ``annotate.<wf>`` when the chain
exists, so their run records' ``inputs`` name the head they read); a standalone run without it into a
series that has one is refused (:func:`stack.stage_runner.overrides_refusal`, D495). The targets:

```
target            consumer                  key                         item_key           kind
identity          gather (identity)         source id (mask) or the     ''                 row
                                            sorted ids' canonical JSON
                                            (merge, separate)
fields            gather (metadata)         doc id or source id         doc_id|source_id   field
<wf>              annotate.<wf> select      doc id                      the item ('' doc)  row|field|quarantine
manual_sources    collect.manual            <kind>=<value> of the URL's ''                 row
                                            first typed key
```

A ``revoke`` row (``kind = 'revoke'``, ``revokes`` = the id it retires, the retired row's target
and keys copied for reading) carries no payload. ``payload`` is JSON text: ``value`` (the identity
operation, the field value, the ``<wf>`` row or field value), ``mask_reason``, ``url``,
``flags`` and the given :data:`MANUAL_FIELDS` (manual sources) and ``note``.

**Input formats** (:func:`read_file`). YAML: today's entry format, unchanged (the models below
validate it as before): a list of entries whose target is the file's stem (``identity.yaml``,
``fields.yaml``, ``manual_sources.yaml``, ``<wf>.yaml``), or a mapping ``target -> list``. An
entry's ``supersedes: X`` becomes two rows, a revoke of ``X`` (id ``<id>.supersedes``) and the
entry. A missing ``created_at`` (``added_at``) is the add run's time; a naive one is UTC. CSV: one
row per line with the family's column names (``payload`` JSON text, ``scope_members`` a JSON
list); each row is turned back into its target's entry and validated by the same model.

**The effective rule** (:func:`effective`; D337). First the revokes: a revoke drops the row it
names when its tier is at least that row's (``human`` > ``ai_check``); a revoke of a revoke, an
under-tier revoke and a dangling revoke drop nothing and are warnings (``overrides.add`` refuses
all three, so they reach a view only from rows written some other way). Then per group
(``target``, ``key``, ``item_key``, ``field``, and whether the row is a ``quarantine``) the row
that sorts last by (``tier``, ``created_at``, ``override_id``) wins. ``<wf>`` field rows compose
over the winning row-level row (:mod:`stack.annotate.select`, which also drops rows whose scope no
longer matches before the choice, as ``override_stale_ids`` today). With no row left, the stage's
own value stands.
"""

from __future__ import annotations

import csv
import dataclasses
import datetime as dt
import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import pyarrow as pa
import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

from stack import yamlio
from stack.core.hashes import jcs


class OverrideError(Exception):
    """Override input that does not load or validate, or an ``overrides.add`` refusal."""


#: The family and its one chain.
FAMILY = "overrides"
CHAIN_ID = "overrides"
#: Tier order: a later tier wins (D130).
TIERS: dict[str, int] = {"ai_check": 0, "human": 1}
IDENTITY, FIELDS, MANUAL_SOURCES = "identity", "fields", "manual_sources"
FIXED_TARGETS: tuple[str, ...] = (IDENTITY, FIELDS, MANUAL_SOURCES)
KINDS: tuple[str, ...] = ("row", "field", "quarantine", "revoke")
#: The family's columns, in order (also the CSV header).
COLUMNS: tuple[str, ...] = ("override_id", "target", "key", "item_key", "kind", "field", "payload",
                            "scope_members", "scope_basis_digest", "tier", "author", "created_at", "revokes")


# ---------------------------------------------------------------- entry models (YAML input)
class _Entry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    override_id: str = Field(min_length=1)
    author: str | None = None
    created_at: dt.datetime | None = None
    note: str | None = None
    revokes: str | None = None
    supersedes: str | None = None

    def _is_revoke(self) -> bool:
        return self.revokes is not None

    @model_validator(mode="after")
    def _one_retirement(self) -> _Entry:
        if self.revokes is not None and self.supersedes is not None:
            raise ValueError(f"{self.override_id}: a revoke does not also supersede")
        return self


class IdentityKey(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str | None = None
    source_ids: list[str] | None = None


class IdentityOverride(_Entry):
    """``merge``/``separate`` name two or more ``source_ids``; ``mask`` one ``source_id``."""

    value: Literal["merge", "separate", "mask"] | None = None
    key: IdentityKey | None = None
    mask_reason: str | None = None

    @model_validator(mode="after")
    def _shape(self) -> IdentityOverride:
        if self._is_revoke():
            if self.value is not None or self.key is not None:
                raise ValueError(f"{self.override_id}: a revoke carries no value/key")
            return self
        if self.value is None or self.key is None:
            raise ValueError(f"{self.override_id}: identity override needs `value` and `key`")
        if self.value == "mask":
            if self.key.source_id is None or self.key.source_ids is not None:
                raise ValueError(f"{self.override_id}: mask names one `key.source_id`")
        elif self.key.source_ids is None or len(set(self.key.source_ids)) < 2 or self.key.source_id is not None:
            raise ValueError(f"{self.override_id}: {self.value} names two or more distinct `key.source_ids`")
        return self


class FieldOverride(_Entry):
    """One metadata field of one document (``doc_id``) or member (``source_id``)."""

    doc_id: str | None = None
    source_id: str | None = None
    field: str | None = None
    value: Any = None

    @model_validator(mode="after")
    def _shape(self) -> FieldOverride:
        if self._is_revoke():
            return self
        if (self.doc_id is None) == (self.source_id is None):
            raise ValueError(f"{self.override_id}: a field override names exactly one of doc_id/source_id")
        if not self.field:
            raise ValueError(f"{self.override_id}: a field override needs `field`")
        return self


class WfKey(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    doc_id: str
    item_key: str = ""


class WfScope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    members: list[str] = Field(default_factory=list)
    basis_digest: str | None = None


class WfOverride(_Entry):
    """``row: true`` + ``value`` (a whole payload), ``field`` + ``value`` (one payload field), or
    ``quarantine: true``. ``scope.members`` pins the members it was decided on (an unscoped
    override is ``skipped[review_required]`` at select time, not refused here)."""

    key: WfKey | None = None
    origin: Literal["human", "ai_check"] = "human"
    scope: WfScope | None = None
    row: bool = False
    field: str | None = None
    value: Any = None
    quarantine: bool = False

    @model_validator(mode="after")
    def _shape(self) -> WfOverride:
        if self._is_revoke():
            return self
        if self.key is None:
            raise ValueError(f"{self.override_id}: a <wf> override needs `key.doc_id`")
        kinds = [self.row, self.field is not None, self.quarantine]
        if sum(kinds) != 1:
            raise ValueError(f"{self.override_id}: exactly one of `row: true`, `field`, `quarantine: true`")
        if self.row and not isinstance(self.value, dict):
            raise ValueError(f"{self.override_id}: `row: true` takes a mapping `value`")
        return self


#: ``manual_sources`` flags (mirrors :data:`stack.scholarly_records.FLAGS`, pinned by test):
#: ``collect_links`` = harvest the document's outbound links (``collect.links``); ``accept`` =
#: triage fetches it (source rule ``manual_accept``).
ManualFlag = Literal["collect_links", "accept"]


#: The ``sources`` ``metadata`` fields a manual source may give (D666): ``collect.manual`` writes them into its
#: source's ``metadata`` like any collector, and gather merges them like any member's (``fields`` overrides still
#: win). Values in the forms a ``fields`` override takes, read by :func:`field_value`.
MANUAL_FIELDS: tuple[str, ...] = ("title", "authors", "published_at")


def _timestamp(v: Any) -> dt.datetime | None:
    if v is None:
        return None
    if isinstance(v, dt.datetime):
        out = v
    elif isinstance(v, dt.date):
        out = dt.datetime(v.year, v.month, v.day)
    elif isinstance(v, str):
        try:
            out = dt.datetime.fromisoformat(v)
        except ValueError:
            raise ValueError(f"{v!r} is not an ISO timestamp") from None
    else:
        raise ValueError(f"a timestamp field takes a string or date, got {v!r}")
    return out.replace(tzinfo=dt.UTC) if out.tzinfo is None else out.astimezone(dt.UTC)


def _author_list(v: Any) -> list[dict[str, Any]] | None:
    if v is None:
        return None
    if not isinstance(v, list):
        raise ValueError(f"authors takes a list, got {v!r}")
    out: list[dict[str, Any]] = []
    for a in v:
        if isinstance(a, str) and a.strip():
            out.append({"name": a, "orcid": None, "affiliation_raw": None})
        elif isinstance(a, dict) and isinstance(a.get("name"), str) and a["name"].strip() \
                and set(a) <= {"name", "orcid", "affiliation_raw"}:
            out.append({"name": a["name"], "orcid": a.get("orcid"), "affiliation_raw": a.get("affiliation_raw")})
        else:
            raise ValueError(f"bad author {a!r} (a name or {{name, orcid, affiliation_raw}})")
    return out


def field_value(field: str, v: Any) -> Any:
    """A hand-written ``metadata`` value (a ``fields`` override's, a manual source's) as the ``metadata`` block
    types it: ``published_at``/``provider_updated_at`` from an ISO string or a date (a date is midnight UTC, a
    naive time UTC; a year alone is written ``<year>-01-01``, which gather reads as year-only), ``authors`` from a
    list of names or ``{name, orcid, affiliation_raw}`` mappings, any other field a string; ``None`` stays
    ``None``. Raises ``ValueError`` on an ill-typed value."""
    if field in ("published_at", "provider_updated_at"):
        return _timestamp(v)
    if field == "authors":
        return _author_list(v)
    if v is not None and not isinstance(v, str):
        raise ValueError(f"{field} takes a string, got {v!r}")
    return v


class ManualSource(_Entry):
    """One hand-added source (``collect.manual``, round 4 (L)): ``url`` (an absolute http(s) URL),
    ``note`` (why it is here; carried into the source's record), ``flags``, ``added_by``,
    ``added_at``, and optionally the source's metadata :data:`MANUAL_FIELDS` (``title``, ``authors``,
    ``published_at``; D666). A revoke carries none of url, flags or fields. ``author``/``created_at`` are spelled
    ``added_by``/``added_at`` here."""

    url: str | None = None
    flags: list[ManualFlag] = Field(default_factory=list)
    added_by: str | None = None
    added_at: dt.date | dt.datetime | None = None
    # Any: checked by field_value, so a YAML year (an int) is refused instead of read as a Unix time
    title: Any = None
    authors: Any = None
    published_at: Any = None

    def metadata_fields(self) -> dict[str, Any]:
        """The given :data:`MANUAL_FIELDS`, as JSON data (a date as its ISO string)."""
        return {f: _json(getattr(self, f)) for f in MANUAL_FIELDS if getattr(self, f) is not None}

    @model_validator(mode="after")
    def _shape(self) -> ManualSource:
        if self.author is not None or self.created_at is not None:
            raise ValueError(f"{self.override_id}: a manual source names `added_by`/`added_at`, "
                             f"not `author`/`created_at`")
        if len(set(self.flags)) != len(self.flags):
            raise ValueError(f"{self.override_id}: flags listed twice: {self.flags}")
        if self._is_revoke():
            if self.url is not None or self.flags or self.metadata_fields():
                raise ValueError(f"{self.override_id}: a revoke carries no url/flags/{'/'.join(MANUAL_FIELDS)}")
            return self
        if not self.url or not self.url.strip():
            raise ValueError(f"{self.override_id}: a manual source needs `url`")
        for f, v in self.metadata_fields().items():
            try:
                field_value(f, v)
            except ValueError as exc:
                raise ValueError(f"{self.override_id}: {exc}") from None
        if self.title is not None and not self.title.strip():
            raise ValueError(f"{self.override_id}: an empty `title` (leave it out)")
        return self


# ---------------------------------------------------------------- the row
@dataclass(frozen=True)
class Override:
    """One row of the ``overrides`` family, ``payload`` parsed (module docstring)."""

    override_id: str
    target: str
    key: str
    item_key: str
    kind: str
    field: str
    payload: Mapping[str, Any]
    tier: str
    created_at: dt.datetime
    scope_members: tuple[str, ...] | None = None
    scope_basis_digest: str | None = None
    author: str | None = None
    revokes: str | None = None

    @property
    def value(self) -> Any:
        return self.payload.get("value")

    @property
    def note(self) -> str | None:
        return self.payload.get("note")

    @property
    def is_scoped(self) -> bool:
        return bool(self.scope_members)

    def to_row(self) -> dict[str, Any]:
        return {"override_id": self.override_id, "target": self.target, "key": self.key,
                "item_key": self.item_key, "kind": self.kind, "field": self.field,
                "payload": jcs(dict(self.payload)),
                "scope_members": list(self.scope_members) if self.scope_members is not None else None,
                "scope_basis_digest": self.scope_basis_digest, "tier": self.tier, "author": self.author,
                "created_at": self.created_at, "revokes": self.revokes}

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> Override:
        sm = row.get("scope_members")
        return cls(override_id=row["override_id"], target=row["target"], key=row["key"],
                   item_key=row["item_key"], kind=row["kind"], field=row["field"],
                   payload=json.loads(row["payload"]), tier=row["tier"], created_at=row["created_at"],
                   scope_members=tuple(sm) if sm is not None else None,
                   scope_basis_digest=row.get("scope_basis_digest"), author=row.get("author"),
                   revokes=row.get("revokes"))


def rows_of(table: pa.Table) -> list[Override]:
    """The rows of an ``overrides`` view, in ``override_id`` order."""
    return sorted((Override.from_row(r) for r in table.to_pylist()), key=lambda o: o.override_id)


def to_table(rows: Iterable[Override]) -> pa.Table:
    from stack.schemas.families import family

    return pa.Table.from_pylist([o.to_row() for o in rows], schema=family(FAMILY).schema)


def rows_digest(rows: Sequence[Override]) -> str | None:
    """sha256 of the canonical JSON of ``rows`` in ``override_id`` order; ``None`` for no rows (as
    a missing override file had no digest)."""
    if not rows:
        return None
    data = [{**o.to_row(), "created_at": _iso(o.created_at)} for o in sorted(rows, key=lambda o: o.override_id)]
    return hashlib.sha256(jcs(data).encode()).hexdigest()


def _iso(t: dt.datetime) -> str:
    return t.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# ---------------------------------------------------------------- the effective rule (D337)
def drop_revoked(rows: Iterable[Override]) -> tuple[list[Override], list[str]]:
    """The rows left after the revokes (revoke rows themselves dropped), and the warnings: a
    dangling revoke, a revoke of a revoke and an under-tier revoke (these drop nothing)."""
    rows = list(rows)
    by_id = {o.override_id: o for o in rows}
    dead: set[str] = set()
    warnings: list[str] = []
    for r in rows:
        if r.kind != "revoke":
            continue
        t = by_id.get(r.revokes or "")
        if t is None:
            warnings.append(f"{r.override_id}: revokes {r.revokes!r}, which no row has (dangling)")
        elif t.kind == "revoke":
            warnings.append(f"{r.override_id}: revokes the revoke {t.override_id} (refused; ignored)")
        elif TIERS[r.tier] < TIERS[t.tier]:
            warnings.append(f"{r.override_id}: tier {r.tier} cannot revoke {t.override_id} (tier {t.tier}; ignored)")
        else:
            dead.add(t.override_id)
    return [o for o in rows if o.kind != "revoke" and o.override_id not in dead], warnings


def group_of(o: Override) -> tuple[str, str, str, bool, str]:
    """The group one winner is chosen in: ``(target, key, item_key, quarantine?, field)``."""
    return (o.target, o.key, o.item_key, o.kind == "quarantine", o.field)


def rank(o: Override) -> tuple[int, dt.datetime, str]:
    """The order within a group; the last wins (``human`` above ``ai_check``)."""
    return (TIERS[o.tier], o.created_at, o.override_id)


def winners(rows: Iterable[Override]) -> list[Override]:
    """Per group the row that sorts last by :func:`rank`; the result in :func:`rank` order. Revokes
    must already be applied (:func:`drop_revoked`)."""
    best: dict[tuple[str, str, str, bool, str], Override] = {}
    for o in rows:
        if o.kind == "revoke":
            raise ValueError(f"winners: revoke row {o.override_id}; apply drop_revoked first")
        g = group_of(o)
        if g not in best or rank(o) > rank(best[g]):
            best[g] = o
    return sorted(best.values(), key=rank)


@dataclass(frozen=True)
class Effective:
    """The effective rows of one target (or all) and the revoke warnings."""

    rows: tuple[Override, ...]
    warnings: tuple[str, ...] = ()

    @property
    def digest(self) -> str | None:
        return rows_digest(self.rows)


def effective(rows: Iterable[Override], target: str | None = None) -> Effective:
    """The effective rule (module docstring), restricted to ``target`` when given."""
    live, warnings = drop_revoked(rows)
    if target is not None:
        live = [o for o in live if o.target == target]
    return Effective(tuple(winners(live)), tuple(warnings))


def active(rows: Iterable[Override], target: str) -> tuple[list[Override], list[str]]:
    """The rows of ``target`` left after the revokes, before the per-group choice (``select``
    filters by scope first, then calls :func:`winners`)."""
    live, warnings = drop_revoked(rows)
    return [o for o in live if o.target == target], warnings


# ---------------------------------------------------------------- the pinned input
def input_rows(ctx: Any) -> list[Override]:
    """The rows of the run's pinned ``overrides`` input (``ctx``: a
    :class:`stack.stage_runner.StageContext`); ``[]`` when the run was given none.
    A run given no ``--in data/overrides/<series>/`` reads none (D377); its
    record's ``in_files`` says so."""
    keys = [k for k, ch in ctx.inputs.items() if ch.family == FAMILY]
    if len(keys) > 1:
        raise OverrideError(f"{ctx.stage}: more than one overrides input {keys}")
    if not keys:
        return []
    return rows_of(ctx.view(keys[0]))


# ---------------------------------------------------------------- entries -> rows
def _utc(t: dt.date | dt.datetime | None, now: dt.datetime) -> dt.datetime:
    if t is None:
        return now
    if not isinstance(t, dt.datetime):
        t = dt.datetime(t.year, t.month, t.day)
    return t.replace(tzinfo=dt.UTC) if t.tzinfo is None else t.astimezone(dt.UTC)


def _with_note(payload: dict[str, Any], e: _Entry) -> dict[str, Any]:
    return payload | ({"note": e.note} if e.note is not None else {})


def entry_rows(target: str, e: _Entry, *, now: dt.datetime, tier: str = "human") -> list[Override]:
    """The rows of one validated entry of ``target``: one row, or for ``supersedes: X`` a revoke of
    ``X`` (id ``<id>.supersedes``) and the row. A revoke's keys are filled from the row it names by
    :func:`check_add`."""
    if isinstance(e, WfOverride):
        tier = e.origin
    created = _utc(e.added_at if isinstance(e, ManualSource) else e.created_at, now)
    author = e.added_by if isinstance(e, ManualSource) else e.author
    common: dict[str, Any] = {"target": target, "tier": tier, "created_at": created, "author": author}
    if e.revokes is not None:
        return [Override(override_id=e.override_id, key="", item_key="", kind="revoke", field="",
                         payload=_with_note({}, e), revokes=e.revokes, **common)]
    if isinstance(e, IdentityOverride):
        assert e.key is not None and e.value is not None
        key = e.key.source_id if e.value == "mask" else jcs(sorted(set(e.key.source_ids or [])))
        assert key is not None
        pay: dict[str, Any] = {"value": e.value}
        if e.mask_reason is not None:
            pay["mask_reason"] = e.mask_reason
        row = Override(override_id=e.override_id, key=key, item_key="", kind="row", field="",
                       payload=_with_note(pay, e), **common)
    elif isinstance(e, FieldOverride):
        assert e.field is not None
        by = "doc_id" if e.doc_id is not None else "source_id"
        row = Override(override_id=e.override_id, key=str(e.doc_id or e.source_id), item_key=by, kind="field",
                       field=e.field, payload=_with_note({"value": _json(e.value)}, e), **common)
    elif isinstance(e, WfOverride):
        assert e.key is not None
        kind = "row" if e.row else "field" if e.field is not None else "quarantine"
        pay = {} if kind == "quarantine" else {"value": _json(e.value)}
        sc = e.scope
        row = Override(override_id=e.override_id, key=e.key.doc_id, item_key=e.key.item_key, kind=kind,
                       field=e.field or "", payload=_with_note(pay, e),
                       scope_members=tuple(sc.members) if sc is not None else None,
                       scope_basis_digest=sc.basis_digest if sc is not None else None, **common)
    elif isinstance(e, ManualSource):
        assert e.url is not None
        row = Override(override_id=e.override_id, key=manual_key(e.url, e.override_id), item_key="", kind="row",
                       field="", payload=_with_note({"url": e.url, "flags": list(e.flags)}, e)
                       | e.metadata_fields(), **common)
    else:
        raise TypeError(f"entry of unknown type {type(e).__name__}")
    if e.supersedes is None:
        return [row]
    rev = Override(override_id=f"{e.override_id}.supersedes", key="", item_key="", kind="revoke", field="",
                   payload={}, revokes=e.supersedes, **common)
    return [rev, row]


def _json(v: Any) -> Any:
    """``v`` as JSON data (a YAML date becomes its ISO string, as the consumers parse it)."""
    return json.loads(json.dumps(v, default=lambda x: x.isoformat() if hasattr(x, "isoformat") else str(x)))


def manual_key(url: str, oid: str = "") -> str:
    """A manual source's ``key``: ``<kind>=<value>`` of its URL's first typed key, so two entries
    naming one target (an ``arxiv.org/abs/`` and a ``/pdf/`` URL of one paper) share a group.
    Refuses a URL that is not an absolute http(s) URL."""
    from stack.urls import link_keys

    target = link_keys(url)
    if target is None or not target.keys:
        raise OverrideError(f"{oid}: url {url!r} is not an absolute http(s) URL")
    kind, value = target.keys[0]
    return f"{kind}={value}"


def _model(target: str) -> type[_Entry]:
    if target == IDENTITY:
        return IdentityOverride
    if target == FIELDS:
        return FieldOverride
    if target == MANUAL_SOURCES:
        return ManualSource
    from stack.schemas.families import CHAIN_WORKFLOWS
    from stack.schemas.payloads.registry import REGISTRY

    if target not in REGISTRY or target in CHAIN_WORKFLOWS:
        known = sorted([*FIXED_TARGETS, *(n for n in REGISTRY if n not in CHAIN_WORKFLOWS)])
        raise OverrideError(f"unknown override target {target!r}; known: {known}")
    return WfOverride


def entries_rows(target: str, raw: Sequence[Any], *, now: dt.datetime, tier: str = "human",
                 where: str = "") -> list[Override]:
    """Validate ``raw`` (YAML entries) as entries of ``target`` and return their rows."""
    model = _model(target)
    if not isinstance(raw, list):
        raise OverrideError(f"{where}: the {target} entries are a YAML list, got {type(raw).__name__}")
    try:
        entries = [model.model_validate(item) for item in raw]
    except ValidationError as exc:
        raise OverrideError(f"{where}: invalid {target} entries:\n{exc}") from exc
    return [o for e in entries for o in entry_rows(target, e, now=now, tier=tier)]


# ---------------------------------------------------------------- reading an input file
def read_file(path: str | Path, *, now: dt.datetime) -> list[Override]:
    """The rows of one input file (module docstring): ``.yaml``/``.yml`` or ``.csv``."""
    p = Path(path)
    try:
        text = p.read_text("utf-8")
    except OSError as exc:
        raise OverrideError(f"{p}: cannot read: {exc}") from exc
    if p.suffix in (".yaml", ".yml"):
        try:
            raw = yamlio.load(text)
        except yaml.YAMLError as exc:
            raise OverrideError(f"{p}: not YAML: {exc}") from exc
        raw = [] if raw is None else raw
        if isinstance(raw, dict):
            return [o for t, es in raw.items() for o in entries_rows(str(t), es, now=now, where=f"{p}: {t}")]
        return entries_rows(p.stem, raw, now=now, where=str(p))
    if p.suffix == ".csv":
        return _read_csv(p, text, now=now)
    raise OverrideError(f"{p}: an overrides file is .yaml, .yml or .csv")


def _csv_entry(r: Mapping[str, str], line: int, where: str) -> tuple[str, dict[str, Any], str]:
    """One CSV row as ``(target, entry dict, tier)``."""
    def col(name: str) -> str:
        return (r.get(name) or "").strip()

    def js(name: str, default: Any) -> Any:
        v = col(name)
        if not v:
            return default
        try:
            return json.loads(v)
        except json.JSONDecodeError as exc:
            raise OverrideError(f"{where}:{line}: {name} is not JSON: {exc}") from None

    target, kind, tier = col("target"), col("kind"), col("tier") or "human"
    if not target:
        raise OverrideError(f"{where}:{line}: no target")
    if tier not in TIERS:
        raise OverrideError(f"{where}:{line}: tier {tier!r} not in {list(TIERS)}")
    if kind and kind not in KINDS:
        raise OverrideError(f"{where}:{line}: kind {kind!r} not in {list(KINDS)}")
    pay = js("payload", {})
    if not isinstance(pay, dict):
        raise OverrideError(f"{where}:{line}: payload is a JSON object")
    e: dict[str, Any] = {"override_id": col("override_id")}
    if pay.get("note") is not None:
        e["note"] = pay["note"]
    stamp = col("created_at") or None
    if target == MANUAL_SOURCES:
        e.update(added_by=col("author") or None, added_at=stamp)
    else:
        e.update(author=col("author") or None, created_at=stamp)
    if _model(target) is WfOverride:
        e["origin"] = tier
    if kind == "revoke" or col("revokes"):
        e["revokes"] = col("revokes") or None
        return target, e, tier
    key = col("key")
    if target == IDENTITY:
        value = pay.get("value")
        e.update(value=value, key={"source_id": key} if value == "mask" else {"source_ids": js("key", None)})
        if pay.get("mask_reason") is not None:
            e["mask_reason"] = pay["mask_reason"]
    elif target == FIELDS:
        by = col("item_key") or "doc_id"
        if by not in ("doc_id", "source_id"):
            raise OverrideError(f"{where}:{line}: a fields row's item_key is doc_id or source_id, got {by!r}")
        e.update({by: key, "field": col("field") or None, "value": pay.get("value")})
    elif target == MANUAL_SOURCES:
        e.update(url=pay.get("url"), flags=pay.get("flags") or [])
        e.update({f: pay[f] for f in MANUAL_FIELDS if pay.get(f) is not None})
    else:
        e["key"] = {"doc_id": key, "item_key": col("item_key")}
        members, basis = js("scope_members", None), col("scope_basis_digest") or None
        if members is not None or basis is not None:
            e["scope"] = {"members": members or [], "basis_digest": basis}
        if kind == "quarantine":
            e["quarantine"] = True
        elif kind == "field" or col("field"):
            e.update(field=col("field") or None, value=pay.get("value"))
        else:
            e.update(row=True, value=pay.get("value"))
    return target, e, tier


def _read_csv(p: Path, text: str, *, now: dt.datetime) -> list[Override]:
    reader = csv.DictReader(text.splitlines())
    unknown = sorted(set(reader.fieldnames or []) - set(COLUMNS))
    if unknown:
        raise OverrideError(f"{p}: unknown columns {unknown}; the columns are {list(COLUMNS)}")
    out: list[Override] = []
    for line, r in enumerate(reader, start=2):
        target, e, tier = _csv_entry(r, line, str(p))
        rows = entries_rows(target, [e], now=now, tier=tier, where=f"{p}:{line}")
        given_kind = (r.get("kind") or "").strip()
        if given_kind and rows[-1].kind != given_kind:
            raise OverrideError(f"{p}:{line}: kind {given_kind!r} does not fit the row (reads as {rows[-1].kind!r})")
        out += rows
    return out


# ---------------------------------------------------------------- overrides.add's checks
def check_add(new: Sequence[Override], chain: Sequence[Override]) -> list[Override]:
    """Validate ``new`` against itself and the chain's rows: refuse (:class:`OverrideError`) a
    duplicate ``override_id``, a dangling revoke (naming no row of the chain or of ``new``: kept,
    it would cancel a row added later under that id), a revoke of a revoke, a revoke whose tier is
    below the row it names and a revoke naming a row of another target. Returns the rows (each
    revoke's keys and field copied from the row it names)."""
    ids = [o.override_id for o in new]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        raise OverrideError(f"override ids listed twice in the input: {dup}")
    have = {o.override_id for o in chain}
    clash = sorted(set(ids) & have)
    if clash:
        raise OverrideError(f"override ids already in the overrides chain: {clash} (ids are unique; "
                            f"revoke and add a new id)")
    by_id = {o.override_id: o for o in [*chain, *new]}
    out: list[Override] = []
    for o in new:
        if o.kind != "revoke":
            out.append(o)
            continue
        t = by_id.get(o.revokes or "")
        if t is None:
            raise OverrideError(f"{o.override_id}: revokes {o.revokes!r}, which names no row of the overrides "
                                f"chain or this file (a revoke may not wait for a later row)")
        if t.kind == "revoke":
            raise OverrideError(f"{o.override_id}: revokes the revoke {t.override_id}; a revoke is final "
                                f"(add the override again under a new id)")
        if t.target != o.target:
            raise OverrideError(f"{o.override_id}: a {o.target} revoke names {t.override_id}, a {t.target} override")
        if TIERS[o.tier] < TIERS[t.tier]:
            raise OverrideError(f"{o.override_id}: tier {o.tier} cannot revoke {t.override_id} (tier {t.tier})")
        out.append(dataclasses.replace(o, key=t.key, item_key=t.item_key, field=t.field))
    return out


# ---------------------------------------------------------------- the standalone add
ADD_STAGE = "overrides.add"
#: The plan name a standalone ``overrides.add`` run records (no plan file is read).
ADD_PLAN = "overrides"


def add_plan(path: str | Path) -> Any:
    """The in-memory plan of a standalone ``overrides.add`` run of ``path``."""
    from stack.plan import Plan

    return Plan.model_validate({"plan": ADD_PLAN, "schema_version": 2,
                                "stages": [{"stage": ADD_STAGE, "options": {"file": str(Path(path).resolve())}}]})


def named_ids(rows: Iterable[Override]) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """The ids the ``identity`` and ``fields`` rows of ``rows`` name, each with the override ids
    naming it: ``(source ids, doc ids)``. Revokes and other targets name none."""
    sources: dict[str, set[str]] = {}
    docs: dict[str, set[str]] = {}
    for o in rows:
        if o.kind == "revoke":
            continue
        if o.target == IDENTITY:
            named = [o.key] if o.payload.get("value") == "mask" else json.loads(o.key)
            for s in named:
                sources.setdefault(s, set()).add(o.override_id)
        elif o.target == FIELDS:
            (docs if o.item_key == "doc_id" else sources).setdefault(o.key, set()).add(o.override_id)
    return sources, docs


def series_of(out: str) -> str:
    """The series of an overrides series directory ``data/overrides/<series>/``."""
    parts = out.strip("/").split("/")
    if len(parts) != 3 or parts[0] != "data" or parts[1] != CHAIN_ID:
        raise OverrideError(f"--out {out!r} is not an overrides series directory data/{CHAIN_ID}/<series>/")
    return parts[2]


def known_ids(store_root: str | Path, series: str) -> tuple[set[str], set[str]]:
    """``(source ids, doc ids)`` the store knows in ``series``: every ``source_id`` of the
    ``sources.*/<series>/`` views (what gather reads, :mod:`stack.build`), and every ``doc_id`` and
    ``former_doc_ids`` entry of the ``documents/<series>/`` head. Empty for a missing store."""
    from stack.store.chain import EmptyChainError, read_view, resolve
    from stack.store.index import store_index
    from stack.store.layout import family_of_dir

    if not Path(store_root).is_dir():
        return set(), set()
    sources: set[str] = set()
    for x in store_index(store_root).series_dirs():
        _, fam, s = x.split("/")
        if s == series and family_of_dir(fam) == "sources":
            sources.update(read_view(resolve(store_root, f"{x}/"), columns=["source_id"])
                           .column("source_id").to_pylist())
    docs: set[str] = set()
    try:
        head = read_view(resolve(store_root, f"data/documents/{series}/"), columns=["doc_id", "identity"])
    except EmptyChainError:
        head = None
    if head is not None:
        for r in head.to_pylist():
            docs.add(r["doc_id"])
            docs.update((r["identity"] or {}).get("former_doc_ids") or [])
    return sources, docs


def check_known(store_root: str | Path, new: Sequence[Override], chain: Sequence[Override], out: str) -> None:
    """Refuse (:class:`OverrideError`) a row of ``new`` left live after the revokes whose ``identity``
    or ``fields`` entry names an id the series does not know (:func:`known_ids`): gather would
    refuse every later run until a revoke (:func:`workflows.gather.overrides.check_known`). A
    ``source_id`` must be a source of the series; a ``doc_id`` a head ``doc_id`` or a
    ``former_doc_ids`` entry (a source not gathered yet has no document to name: gather first)."""
    ids = {o.override_id for o in new}
    live, _ = drop_revoked([*chain, *new])
    want_s, want_d = named_ids(o for o in live if o.override_id in ids)
    if not want_s and not want_d:
        return
    have_s, have_d = known_ids(store_root, series_of(out))
    bad = {**{s: oids for s, oids in want_s.items() if s not in have_s},
           **{d: oids for d, oids in want_d.items() if d not in have_d}}
    if bad:
        named = "; ".join(f"{i} (in {', '.join(sorted(bad[i]))})" for i in sorted(bad))
        raise OverrideError(f"ids no source or document of series {series_of(out)!r} has: {named} "
                            f"(a mistyped id or one of another series; gather would refuse every run "
                            f"until a revoke)")


def precheck(store_root: str | Path, path: str | Path, out: str) -> list[Override]:
    """What ``overrides.add`` of ``path`` would add to the series directory ``out``
    (``data/overrides/<series>/``) now; refuses (:class:`OverrideError`) as the stage would, and
    also an ``identity`` or ``fields`` row naming an id the series does not know
    (:func:`check_known`; the stage itself, run by ``stack run``, does not check this)."""
    from stack.store.chain import EmptyChainError, read_view, resolve

    try:
        chain = rows_of(read_view(resolve(store_root, out)))
    except EmptyChainError:
        chain = []
    rows = check_add(read_file(path, now=dt.datetime.now(dt.UTC).replace(microsecond=0)), chain)
    check_known(store_root, rows, chain, out)
    return rows


def run_add(store_root: str | Path, path: str | Path, out: str, *, comment: str | None = None,
            warn: Any = None) -> Any:
    """One standalone ``overrides.add`` run of ``path`` into the series directory ``out``
    (``data/overrides/<series>/``, D377), which it reads as its prior (an empty directory:
    the series' first file); returns its final :class:`stack.runs.RunRecord`. ``comment`` goes
    into the record's ``comment`` and the job's head (D322)."""
    from stack.stage_runner import run_stage

    return run_stage(store_root, add_plan(path), ADD_STAGE, [out], [out], warn=warn, comment=comment)


__all__ = [
    "ADD_PLAN",
    "ADD_STAGE",
    "CHAIN_ID",
    "COLUMNS",
    "FAMILY",
    "FIELDS",
    "FIXED_TARGETS",
    "IDENTITY",
    "KINDS",
    "MANUAL_FIELDS",
    "MANUAL_SOURCES",
    "TIERS",
    "Effective",
    "FieldOverride",
    "IdentityOverride",
    "ManualSource",
    "Override",
    "OverrideError",
    "WfOverride",
    "active",
    "add_plan",
    "check_add",
    "check_known",
    "drop_revoked",
    "effective",
    "entries_rows",
    "entry_rows",
    "field_value",
    "group_of",
    "input_rows",
    "known_ids",
    "manual_key",
    "named_ids",
    "precheck",
    "rank",
    "read_file",
    "rows_digest",
    "rows_of",
    "run_add",
    "series_of",
    "to_table",
    "winners",
]
