"""The researcher roster (``configs/rosters/researchers.yaml``), read by ``collect.arxiv_sweep``
(the author family), ``collect.openalex`` (the author-id family) and
``tools/resolve_openalex_authors.py``. Here because two workflows read it and a workflow imports
nothing from another (round 5 (RUN) B7b, C4).

**File shape.** ``version``; ``names``: a list whose entries are a bare name string or a mapping
``{name, openalex_ids?: [A…], orcid?}``; ``disambiguate`` (optional): roster names whose arXiv
``au:`` query brings mostly namesakes (docs/design/collection-yield.md §3.2, C4), each written as
in ``names``. A ``Last, First`` name is read as ``First Last`` (:func:`roster_name`, D229), so an
entry and its twin are one researcher; their ``openalex_ids`` are merged.

``openalex_ids`` are OpenAlex author ids (``A`` + digits), written by the resolver (accepted by its
rule) or by hand; ``orcid`` is the bare 16-character ORCID id. A malformed value is a
``ValueError`` (``TypeError`` for a wrong YAML type) naming the entry, never a silently skipped
researcher.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from stack import yamlio

#: The roster path override (the arXiv sweep's v3 name; the OpenAlex author-id family reads it too).
ENV_ROSTER = "STACK_ARXIV_ROSTER"
_AUTHOR_ID = re.compile(r"^A\d+$")
_ORCID = re.compile(r"^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$")
#: Name suffixes after a comma (``John Smith, Jr.``): not an inverted ``Last, First``.
_SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv"})


def roster_name(name: str) -> str:
    """A name in ``First Last`` order: ``Last, First`` (one comma, the v1 people list's inverted
    form, e.g. ``Hilton, Benjamin``, ``van der Weij, Teun``) is turned round; anything else is
    returned with its whitespace collapsed (D229)."""
    name = " ".join(name.split())
    last, sep, first = name.partition(",")
    first, last = first.strip(), last.strip()
    if sep and first and last and "," not in first and first.lower().rstrip(".") not in _SUFFIXES:
        return f"{first} {last}"
    return name


@dataclass(frozen=True)
class RosterEntry:
    """One researcher: the ``First Last`` name, their OpenAlex author ids (sorted, unique) and
    ORCID (``None`` when not given)."""

    name: str
    openalex_ids: tuple[str, ...] = ()
    orcid: str | None = None


@dataclass(frozen=True)
class Roster:
    version: str
    entries: tuple[RosterEntry, ...]
    disambiguate: frozenset[str] = field(default_factory=frozenset)

    @property
    def names(self) -> list[str]:
        return [e.name for e in self.entries]

    def ids_of(self, name: str) -> tuple[str, ...]:
        """The OpenAlex ids of a roster name (any spelling :func:`roster_name` accepts); ``()``."""
        want = roster_name(name)
        return next((e.openalex_ids for e in self.entries if e.name == want), ())


def default_path(config_dir: Path) -> Path:
    """``$STACK_ARXIV_ROSTER``, else ``<config_dir>/rosters/researchers.yaml``."""
    env = os.environ.get(ENV_ROSTER)
    return Path(env) if env else config_dir / "rosters" / "researchers.yaml"


def _ids(value: Any, where: str) -> list[str]:
    items = [value] if isinstance(value, str) else list(value or [])
    out = [str(v).strip() for v in items]
    bad = [v for v in out if not _AUTHOR_ID.match(v)]
    if bad:
        raise ValueError(f"{where}: openalex_ids {bad} are not OpenAlex author ids (A + digits)")
    return out


def parse_roster(data: Any, *, source: str = "roster") -> Roster:
    """A :class:`Roster` from the YAML's loaded data: entries merged by :func:`roster_name` and
    sorted by name (deterministic), ``disambiguate`` normalised the same way. A ``disambiguate``
    name that is not a roster name is a ``ValueError`` (a typo would silently filter nothing)."""
    data = data or {}
    if not isinstance(data, dict):
        raise TypeError(f"{source}: not a mapping")
    merged: dict[str, dict[str, Any]] = {}
    for i, item in enumerate(data.get("names") or []):
        where = f"{source}: names[{i}]"
        if isinstance(item, str):
            raw, ids, orcid = item, [], None
        elif isinstance(item, dict):
            unknown = sorted(set(item) - {"name", "openalex_ids", "orcid"})
            if unknown:
                raise ValueError(f"{where}: unknown keys {unknown}")
            raw = str(item.get("name") or "")
            ids = _ids(item.get("openalex_ids"), where)
            orcid = str(item["orcid"]).strip().upper() if item.get("orcid") else None
            if orcid is not None and not _ORCID.match(orcid):
                raise ValueError(f"{where}: orcid {orcid!r} is not a bare ORCID id")
        else:
            raise TypeError(f"{where}: {item!r} is neither a name nor a mapping")
        name = roster_name(raw.strip())
        if not name:
            continue
        slot = merged.setdefault(name, {"ids": set(), "orcid": None})
        slot["ids"].update(ids)
        if orcid:
            if slot["orcid"] not in (None, orcid):
                raise ValueError(f"{where}: {name!r} has two ORCIDs ({slot['orcid']}, {orcid})")
            slot["orcid"] = orcid
    entries = tuple(RosterEntry(n, tuple(sorted(s["ids"])), s["orcid"])
                    for n, s in sorted(merged.items()))
    dis = frozenset(roster_name(str(n)) for n in data.get("disambiguate") or [])
    unknown = sorted(dis - set(merged))
    if unknown:
        raise ValueError(f"{source}: disambiguate names {unknown} are not roster names")
    return Roster(str(data.get("version") or ""), entries, dis)


def load_roster_file(path: Path) -> Roster:
    """:func:`parse_roster` over the YAML file at ``path``."""
    return parse_roster(yamlio.load(path.read_text(encoding="utf-8")), source=str(path))


__all__ = ["ENV_ROSTER", "Roster", "RosterEntry", "default_path", "load_roster_file",
           "parse_roster", "roster_name"]
