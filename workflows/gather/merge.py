"""The ``metadata`` merge over a document's members (§5.3; ADAPT of v3 ``stack/read.py``
``effective_meta`` and ``configs/policies.yaml`` ``metadata:``) and the ``fields`` overrides (D74).

**Precedence per field** (``title``, ``abstract``, ``published_at``, ``doc_type``):

1. a ``fields`` override naming the document (``doc_id``; a former id resolves to the live
   document listing it) — ``origin = 'human'``, ``field_sources[].source_id`` = the doc id;
2. a ``fields`` override naming a member (``source_id``), members in provider order (below) —
   ``origin = 'human'``, ``source_id`` = that member;
3. the first member in **provider order** whose value is present: provider rank of its channel
   (:data:`PROVIDER_RANK`; a channel not listed ranks after every listed one, and the
   :data:`PROVIDER_LAST` channels after every other channel), then newest
   ``observed_at`` of its speaking row, then ``source_id``. ``origin`` = that row's
   ``metadata.origin``. For ``published_at`` a **year-only** value (:func:`is_year_only`: midnight
   UTC on 1 January) ranks after every other present value, whatever its member's rank, so a
   less precise date never replaces a more precise one (VERIFY-2A: an OpenAlex work on an
   arXiv DOI merged into a Semantic Scholar paper turned 2025-12-08 into 2025-01-01). No
   provider says how precise its date is: OpenAlex sends a year-only date as ``<year>-01-01``,
   and S2's collector widens a bare ``year`` to the same (``paper_pub_date``); the arXiv and
   forum dates are days or times. A day-1 date of another month is taken as a day (month
   precision is not inferred: on the 2026-10 store it would mostly swap a journal's month for a
   preprint's day). Year-only values among themselves, and the other values among themselves,
   keep the provider order.

The candidates are the live members (``MEMBER.is_live``); a document with no live member merges
over all of them. **Absent** = ``None``, an empty list, and the empty string (v3's
``empty_is_absent: true``, the only value v3 ran).

**Authors** (v3's ``merge: union``): the first candidate in provider order that has authors gives
its list verbatim (its order, its structs); every later candidate appends the authors whose name
key (:func:`author_key`: folded surname + first initial) is not yet present. ``field_sources``
holds one ``authors`` entry per contributing member, the first one first. A human ``authors``
override replaces the list (no union).

**provider_updated_at** = the newest value over the candidates (the document's provider data
last changed then); its member is named in ``field_sources``.

**Copy vs aggregate** (§5.3): a one-member document (pages, X posts, and any paper seen once)
takes its member's fields verbatim — the rules above reduce to that — except that an empty
string is still absent; aggregation over several members follows the rules above.

**Whole-row fields**: ``is_withdrawn`` iff every member (masked included) is withdrawn (its row
is ``retracted`` or says ``metadata.is_withdrawn``); ``sensitivity`` = sorted union over every
member; ``fields_digest`` per D75 with D19's ``inband_excerpt_sha256`` (both imported from
:mod:`stack.checks.digests`).

**fields** overrides (the effective ``fields`` rows of the ``overrides`` family,
:func:`stack.overrides.effective`: revokes applied, the latest per id and field, D337) name a
``doc_id`` or a ``source_id`` (``item_key``), a ``field`` among :data:`OVERRIDABLE` and a
``value`` (``null`` sets the field NULL; timestamps as ISO strings, a date meaning midnight UTC;
``authors`` a list of names or ``{name, orcid, affiliation_raw}`` mappings; read by
:func:`stack.overrides.field_value`, which also checks a manual source's fields, D666). gather refuses
(``GatherError``) a row naming an unknown document or source, an unknown field or an ill-typed
value. The ids of the entries naming a document or one of its members (applied or shadowed
by a higher one) go into its ``identity.override_ids``, as the identity entries touching it do.
"""

from __future__ import annotations

import datetime as dt
import unicodedata
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from stack.checks import digests
from stack.overrides import FIELDS, Override, effective, field_value
from workflows.gather.normalise import GatherError, Observation

Row = dict[str, Any]

#: Fields a ``fields`` override may set.
OVERRIDABLE = ("title", "abstract", "authors", "published_at", "provider_updated_at", "doc_type")
#: Provider order of channels for the field merge, best first (v3's ``fetch.metadata`` scholarly
#: record before the discovery carry-through ``collect.meta``: the bibliographic providers, then
#: arXiv, then the forums; unlisted channels — pages, cards — after these, then PROVIDER_LAST). A proposed
#: decision of T2.7b; it belongs in ``configs/`` once the v3 ``policies.yaml`` ``metadata:``
#: block is retired. Its digest is part of gather's ``config_digest``.
PROVIDER_RANK: tuple[str, ...] = (
    "openalex", "semantic_scholar", "europepmc", "arxiv", "alignmentforum", "lesswrong")
#: Channels ranked after every other channel, listed or not, in this order: the manual sources'
#: channels of ``collect.manual`` (D666: a hand-given title, authors or date only fills what no
#: collected member has; a ``fields`` override forces a value), then the link channels of
#: ``collect.links`` (round 4, L2), whose only title is an anchor text, so an anchor never overwrites
#: a real title of a member that joined the same document (link-crawling.md §4.11).
PROVIDER_LAST: tuple[str, ...] = ("manual_paper", "manual_forum", "manual_url", "manual_social",
                                  "links_paper", "links_forum", "links_url", "links_social")
_RANK = {c: i for i, c in enumerate(PROVIDER_RANK)}
_RANK.update({c: len(PROVIDER_RANK) + 1 + i for i, c in enumerate(PROVIDER_LAST)})


def is_absent(v: object) -> bool:
    """v3's ``_is_absent`` with ``empty_is_absent: true``; an empty list is absent too."""
    return v is None or v == "" or v == []


def provider_key(o: Observation) -> tuple[int, float, str]:
    """Sort key of a member in provider order (module docstring)."""
    return (_RANK.get(o.channel, len(PROVIDER_RANK)), -o.row["observed_at"].timestamp(), o.source_id)


def is_year_only(v: dt.datetime) -> bool:
    """Whether a ``published_at`` reads as a year only: midnight UTC on 1 January (module docstring)."""
    v = v.astimezone(dt.UTC) if v.tzinfo is not None else v  # stored timestamps are UTC
    return (v.month, v.day, v.hour, v.minute, v.second, v.microsecond) == (1, 1, 0, 0, 0, 0)


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c)).casefold().strip(" .")


def author_key(name: str | None) -> tuple[str, str]:
    """``(surname, first initial)``, accents and case folded; ``"Last, First"`` understood.
    ``"Alice Author"``, ``"A. Author"`` and ``"Author, Alice"`` share a key."""
    if not name or not name.strip():
        return ("", "")
    if "," in name:
        last, _, first = name.partition(",")
    else:
        parts = name.split()
        last, first = parts[-1], " ".join(parts[:-1])
    first = _fold(first)
    return (_fold(last), first[:1])


# ---------------------------------------------------------------- fields overrides
@dataclass(frozen=True)
class FieldOps:
    """The effective ``fields`` overrides by target, values coerced."""

    by_doc: dict[str, dict[str, tuple[str, Any]]] = field(default_factory=dict)     # doc_id -> field -> (oid, value)
    by_source: dict[str, dict[str, tuple[str, Any]]] = field(default_factory=dict)  # source_id -> ...
    config_digest: str | None = None

    def resolve_docs(self, docs: Collection[str], former: Mapping[str, Sequence[str]]) -> dict[str, str]:
        """``doc_id`` named by an entry → the live document it means (itself, or the live doc
        listing it in ``former_doc_ids``). Unknown ids are refused."""
        alias = {f: d for d, fs in former.items() for f in fs}
        out: dict[str, str] = {}
        for d in self.by_doc:
            target = d if d in docs else alias.get(d)
            if target is None:
                raise GatherError(f"fields override names doc_id {d} that is neither a document of this "
                                  f"gather nor in any former_doc_ids")
            if target in out.values() and d != target:
                raise GatherError(f"fields override names {target} twice (as {d} and by its own id)")
            out[d] = target
        return out

    def check_known(self, sources: Collection[str]) -> None:
        unknown = sorted(set(self.by_source) - set(sources))
        if unknown:
            raise GatherError(f"fields override names source ids no sources input speaks for: {unknown}")


def _coerce(e: Override) -> Any:
    """The entry's value typed (:func:`stack.overrides.field_value`, the manual sources' validator too)."""
    try:
        return field_value(e.field, e.value)
    except ValueError as exc:
        raise GatherError(f"fields override {e.override_id}: {exc}") from None


def field_ops(rows: Iterable[Override]) -> FieldOps:
    """The effective ``fields`` rows of ``rows`` (every ``overrides`` row) by target and field."""
    eff = effective(rows, FIELDS)
    by_doc: dict[str, dict[str, tuple[str, Any]]] = {}
    by_source: dict[str, dict[str, tuple[str, Any]]] = {}
    for e in eff.rows:
        if e.field not in OVERRIDABLE:
            raise GatherError(f"fields override {e.override_id}: field {e.field!r} not in {OVERRIDABLE}")
        tbl = by_doc if e.item_key == "doc_id" else by_source
        tbl.setdefault(e.key, {})[e.field] = (e.override_id, _coerce(e))
    return FieldOps(by_doc, by_source, eff.digest)


# ---------------------------------------------------------------- the merge
def _md(o: Observation) -> Row:
    return o.row["metadata"] or {}


def _withdrawn(o: Observation) -> bool:
    return o.withdrawn_at is not None or bool(_md(o).get("is_withdrawn"))


def metadata_payload(doc_id: str, members: Sequence[str], obs: Mapping[str, Observation],
                     live: Collection[str], fops: FieldOps, doc_override: str | None,
                     entries: Sequence[Row]) -> tuple[Row, list[str]]:
    """The ``metadata`` block without its stamps (module docstring), and the ids of the
    ``fields`` overrides naming the document or a member (applied or shadowed). ``doc_override`` is the ``fields`` override doc id resolving to
    this document (its own id or a former one), if any."""
    cands = sorted((s for s in members if s in live), key=lambda s: provider_key(obs[s])) \
        or sorted(members, key=lambda s: provider_key(obs[s]))
    ranked_all = sorted(members, key=lambda s: provider_key(obs[s]))
    human: dict[str, tuple[str, str, Any]] = {}  # field -> (override_id, field_sources source_id, value)
    for s in reversed(ranked_all):  # the best-ranked member's entry wins among members
        for f, (oid, v) in fops.by_source.get(s, {}).items():
            human[f] = (oid, s, v)
    for f, (oid, v) in (fops.by_doc.get(doc_override, {}) if doc_override else {}).items():
        human[f] = (oid, doc_id, v)

    out: Row = {}
    fsrc: list[Row] = []
    for f in OVERRIDABLE:
        if f in human:
            oid, s, v = human[f]
            out[f] = v
            fsrc.append({"field": f, "source_id": s, "origin": "human"})
            continue
        if f == "authors":
            out[f] = None
            seen: set[tuple[str, str]] = set()
            for s in cands:
                new = [a for a in _md(obs[s]).get("authors") or [] if author_key(a.get("name")) not in seen]
                if out[f] is None:
                    new = list(_md(obs[s]).get("authors") or [])  # the first list verbatim
                if not new:
                    continue
                out[f] = (out[f] or []) + new
                seen.update(author_key(a.get("name")) for a in new)
                fsrc.append({"field": f, "source_id": s, "origin": _md(obs[s]).get("origin") or "provider"})
            continue
        if f == "provider_updated_at":
            dated = [s for s in cands if _md(obs[s]).get(f) is not None]
            src = max(dated, key=lambda s: (_md(obs[s])[f], s)) if dated else None
        else:
            present = [s for s in cands if not is_absent(_md(obs[s]).get(f))]
            if f == "published_at":  # stable: provider order within each precision
                present.sort(key=lambda s: is_year_only(_md(obs[s])[f]))
            src = present[0] if present else None
        out[f] = _md(obs[src])[f] if src is not None else None
        if src is not None:
            fsrc.append({"field": f, "source_id": src, "origin": _md(obs[src]).get("origin") or "provider"})

    sens = sorted({x for s in members for x in _md(obs[s]).get("sensitivity") or []})
    out.update(
        is_withdrawn=all(_withdrawn(obs[s]) for s in members),
        sensitivity=sens, field_sources=fsrc,
        fields_digest=digests.fields_digest(
            title=out["title"], abstract=out["abstract"],
            author_names=[a.get("name") for a in out["authors"] or []],
            published_at=out["published_at"], doc_type=out["doc_type"],
            inband_excerpt_sha256=digests.inband_excerpt_sha256(doc_id, entries)))
    used = sorted({oid for s in members for oid, _v in fops.by_source.get(s, {}).values()}
                  | {oid for oid, _v in (fops.by_doc.get(doc_override, {}) if doc_override else {}).values()})
    return {k: out[k] for k in _ORDER}, used


#: ``metadata`` column order without the stamps (A.3).
_ORDER = ("title", "abstract", "authors", "published_at", "provider_updated_at", "doc_type",
          "is_withdrawn", "sensitivity", "field_sources", "fields_digest")


def rank_digest() -> str:
    """Digest of :data:`PROVIDER_RANK`, :data:`PROVIDER_LAST` and the ``published_at`` precision rule
    (part of gather's ``config_digest``)."""
    return digests.H({"rank": list(PROVIDER_RANK), "last": list(PROVIDER_LAST), "published_at": "year_only_last"})


__all__ = ["OVERRIDABLE", "PROVIDER_LAST", "PROVIDER_RANK", "FieldOps", "author_key", "field_ops", "is_absent",
           "is_year_only", "load", "metadata_payload", "provider_key", "rank_digest"]
