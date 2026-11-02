"""The technique taxonomy (v0.9 on) for ``annotate.technique_staged``: loader and the three renderings.

The stage's pin key ``taxonomy`` names the file (production: ``taxonomy/techniques-1.1.0.yaml``, vendored from the
data repository). Two shapes, read into the same :class:`Taxonomy`:

* **1.0.0 and before** (``taxonomy/sources/convert_v09.py``'s output): top-level ``version``, ``status``, ``notes``,
  ``techniques`` (each ``slug, name, kind, status, origin, definition`` required; ``parent``, ``aliases {synonym,
  instance, legacy}``, ``absorbed``, ``ml_methods``, ``risk_note``, ``source_papers``, ``v08_name``, ``note``
  optional) and ``group_nodes`` (``slug, name, definition, children`` required; ``aliases {synonym, legacy}``);
* **1.1.0 on**, the catalogue envelope (:mod:`stack.catalogue`, schema ``techniques-2``, validated first):
  ``version``, ``meta`` (``status``, ``description``, ...) and one ``entries`` list: ``id`` (the slug), ``node``
  (``technique`` | ``group``; ``kind`` stays the technique's kind), the same fields otherwise, ``aliases`` a list of
  ``{text, kind}``. :func:`from_envelope` maps it onto the first shape: the techniques and the group nodes each in
  file order, each alias kind's texts in file order, so a file converted from 1.0.0 gives the same
  :class:`Taxonomy` and the same renderings byte for byte (``tests/test_vendored_taxonomies.py``).

:func:`load_taxonomy` refuses anything else with a ``ValueError`` naming the entry: a schema change
must break here, not reach a run as an empty or wrong menu.

What models see (all three renderings are deterministic: the same file gives the same bytes, so
a rendering placed in a prompt's system part stays cacheable):

* :func:`menu_text` - the full annotator menu, used by the ``single`` / ``sweep`` calls. Sections
  by kind (intervention, monitoring, assessment), then the group nodes; entries sorted by slug.
  ``synonym`` aliases are shown as ``aka``, ``instance`` aliases as ``e.g.``; ``legacy`` aliases,
  ``ml_methods`` and the informal fields (``risk_note``, ``note``, ``source_papers``, ...) never.
* :func:`compact_index` - one line per entry and group node (slug, name, kind, parent), the grading
  call's catalogue index for adding a row the candidates missed.
* :func:`submenu_text` - full blocks for a chosen slug list with code-derived pointers, the
  grading call's sub-menu.

Group nodes are not techniques: they are the targets of generic mentions (a bare umbrella name)
and list their children; the rubric decides how they count. A technique's ``parent`` may be a
group node or another technique (``dangerous-capability-evaluation.cbrn``).

Imports ``stack.*`` only (``yamlio``); loaded by path like every workflow sibling.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from stack import yamlio

KINDS: tuple[str, ...] = ("intervention", "monitoring", "assessment")
ENTRY_STATUSES: tuple[str, ...] = ("final", "provisional")

_TOP_KEYS = {"version", "status", "notes", "techniques", "group_nodes"}
_TECH_REQUIRED = ("slug", "name", "kind", "status", "origin", "definition")
_TECH_OPTIONAL_STR = ("parent", "absorbed", "risk_note", "source_papers", "v08_name", "note")
_TECH_KEYS = {*_TECH_REQUIRED, *_TECH_OPTIONAL_STR, "aliases", "ml_methods"}
_TECH_ALIAS_KEYS = ("synonym", "instance", "legacy")
_GROUP_REQUIRED = ("slug", "name", "definition")
_GROUP_KEYS = {*_GROUP_REQUIRED, "children", "aliases"}
_GROUP_ALIAS_KEYS = ("synonym", "legacy")
_POINTER_KEYS = ("source", "section", "surface", "snippet", "stance", "note")


@dataclass(frozen=True)
class Technique:
    """One taxonomy entry (a technique)."""

    slug: str
    name: str
    kind: str
    status: str
    origin: str
    definition: str
    parent: str | None = None
    synonyms: tuple[str, ...] = ()
    instances: tuple[str, ...] = ()
    legacy: tuple[str, ...] = ()
    ml_methods: tuple[str, ...] = ()
    absorbed: str = ""
    risk_note: str = ""
    source_papers: str = ""
    v08_name: str = ""
    note: str = ""

    @property
    def provisional(self) -> bool:
        return self.status == "provisional"


@dataclass(frozen=True)
class GroupNode:
    """A generic-mention target: not a technique of its own, the parent of its ``children``."""

    slug: str
    name: str
    definition: str
    children: tuple[str, ...]
    synonyms: tuple[str, ...] = ()
    legacy: tuple[str, ...] = ()


@dataclass(frozen=True)
class Taxonomy:
    version: str
    status: str
    techniques: tuple[Technique, ...]
    group_nodes: tuple[GroupNode, ...]

    def get(self, slug: str) -> Technique | GroupNode | None:
        """The entry or group node with ``slug`` (linear; the catalogue is a few hundred lines)."""
        for e in self.techniques:
            if e.slug == slug:
                return e
        for g in self.group_nodes:
            if g.slug == slug:
                return g
        return None


# ── loading ──────────────────────────────────────────────────────────────────────────────────


def _str_list(where: str, value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise ValueError(f"{where}: must be a list of non-empty strings")
    return tuple(value)


def _aliases(where: str, raw: Any, keys: tuple[str, ...]) -> dict[str, tuple[str, ...]]:
    if raw is None:
        return {k: () for k in keys}
    if not isinstance(raw, dict):
        raise ValueError(f"{where}: `aliases` must be a mapping")
    unknown = sorted(set(raw) - set(keys))
    if unknown:
        raise ValueError(f"{where}: unknown alias type(s) {unknown} (allowed: {list(keys)})")
    return {k: _str_list(f"{where}: aliases.{k}", raw[k]) if k in raw else () for k in keys}


def _technique(where: str, t: Any) -> Technique:
    if not isinstance(t, dict):
        raise ValueError(f"{where}: not a mapping")
    where = f"{where} ({t.get('slug')!r})"
    unknown = sorted(set(t) - _TECH_KEYS)
    if unknown:
        raise ValueError(f"{where}: unknown key(s) {unknown}")
    missing = [k for k in _TECH_REQUIRED if not isinstance(t.get(k), str) or not t[k].strip()]
    if missing:
        raise ValueError(f"{where}: lacks {missing}")
    for k in _TECH_OPTIONAL_STR:
        if k in t and (not isinstance(t[k], str) or not t[k].strip()):
            raise ValueError(f"{where}: `{k}` must be a non-empty string when present")
    if t["kind"] not in KINDS:
        raise ValueError(f"{where}: kind {t['kind']!r} not in {list(KINDS)}")
    if t["status"] not in ENTRY_STATUSES:
        raise ValueError(f"{where}: status {t['status']!r} not in {list(ENTRY_STATUSES)}")
    al = _aliases(where, t.get("aliases"), _TECH_ALIAS_KEYS)
    ml = _str_list(f"{where}: ml_methods", t["ml_methods"]) if "ml_methods" in t else ()
    return Technique(
        slug=t["slug"], name=t["name"], kind=t["kind"], status=t["status"], origin=t["origin"],
        definition=t["definition"], parent=t.get("parent"), synonyms=al["synonym"],
        instances=al["instance"], legacy=al["legacy"], ml_methods=ml,
        **{k: t.get(k, "") for k in ("absorbed", "risk_note", "source_papers", "v08_name", "note")},
    )


def _group(where: str, g: Any) -> GroupNode:
    if not isinstance(g, dict):
        raise ValueError(f"{where}: not a mapping")
    where = f"{where} ({g.get('slug')!r})"
    unknown = sorted(set(g) - _GROUP_KEYS)
    if unknown:
        raise ValueError(f"{where}: unknown key(s) {unknown}")
    missing = [k for k in _GROUP_REQUIRED if not isinstance(g.get(k), str) or not g[k].strip()]
    if missing:
        raise ValueError(f"{where}: lacks {missing}")
    children = _str_list(f"{where}: children", g.get("children"))
    if not children:
        raise ValueError(f"{where}: `children` is empty")
    al = _aliases(where, g.get("aliases"), _GROUP_ALIAS_KEYS)
    return GroupNode(slug=g["slug"], name=g["name"], definition=g["definition"], children=children,
                     synonyms=al["synonym"], legacy=al["legacy"])


#: The JSON Schema of the envelope files (``meta.schema``; :mod:`stack.catalogue`).
SCHEMA = "techniques-2"


def from_envelope(raw: Mapping[str, Any], where: str = "taxonomy") -> dict[str, Any]:
    """An envelope file (``entries``), validated against :data:`SCHEMA`, in the 1.0.0 shape (module docstring):
    ``node`` dropped, ``id`` -> ``slug``, the aliases grouped by kind in file order."""
    from stack import catalogue

    try:
        catalogue.validate(raw, SCHEMA)
    except catalogue.CatalogueError as exc:
        raise ValueError(f"{where}: {exc}") from exc
    techniques: list[dict[str, Any]] = []
    groups: list[dict[str, Any]] = []
    for e in raw["entries"]:
        out: dict[str, Any] = {"slug": e["id"]}
        for k, v in e.items():
            if k in ("id", "node"):
                continue
            if k == "aliases":
                by_kind: dict[str, list[str]] = {}
                for a in v:
                    by_kind.setdefault(a["kind"], []).append(a["text"])
                v = by_kind
            out[k] = v
        (groups if e["node"] == "group" else techniques).append(out)
    meta = raw["meta"]
    return {"version": raw["version"], "status": meta["status"], "notes": meta["description"],
            "techniques": techniques, "group_nodes": groups}


def parse_taxonomy(raw: Any, where: str = "taxonomy") -> Taxonomy:
    """Validate a parsed taxonomy document, either shape (see the module docstring)."""
    if isinstance(raw, Mapping) and "entries" in raw:
        raw = from_envelope(raw, where)
    if not isinstance(raw, dict):
        raise ValueError(f"{where}: not a mapping")
    unknown = sorted(set(raw) - _TOP_KEYS)
    if unknown:
        raise ValueError(f"{where}: unknown top-level key(s) {unknown}")
    if not isinstance(raw.get("version"), str) or not raw["version"]:
        raise ValueError(f"{where}: `version` must be a non-empty string (quote it: '0.9')")
    if not isinstance(raw.get("techniques"), list) or not raw["techniques"]:
        raise ValueError(f"{where}: no `techniques` list")
    groups_raw = raw.get("group_nodes", [])
    if not isinstance(groups_raw, list):
        raise ValueError(f"{where}: `group_nodes` must be a list")
    techniques = tuple(_technique(f"{where}: techniques[{i}]", t) for i, t in enumerate(raw["techniques"]))
    groups = tuple(_group(f"{where}: group_nodes[{i}]", g) for i, g in enumerate(groups_raw))
    seen: set[str] = set()
    for slug in [*(t.slug for t in techniques), *(g.slug for g in groups)]:
        if slug in seen:
            raise ValueError(f"{where}: duplicate slug {slug!r} (unique over techniques and group nodes)")
        seen.add(slug)
    for t in techniques:
        if t.parent is not None and (t.parent not in seen or t.parent == t.slug):
            raise ValueError(f"{where}: {t.slug}: parent {t.parent!r} does not resolve")
    for g in groups:
        missing = [c for c in g.children if c not in seen or c == g.slug]
        if missing:
            raise ValueError(f"{where}: group {g.slug}: children {missing} do not resolve")
    return Taxonomy(version=raw["version"], status=str(raw.get("status", "")), techniques=techniques,
                    group_nodes=groups)


def load_taxonomy(path: Path) -> Taxonomy:
    """Read and validate a taxonomy file (either shape). Raises ``ValueError`` naming the bad entry."""
    return parse_taxonomy(yamlio.load(path.read_text("utf-8")), where=str(path.name))


def taxonomy_digest(path: Path) -> str:
    """sha256 (hex) of the file's bytes: the digest pinned into template digests."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def all_slugs(tax: Taxonomy) -> tuple[str, ...]:
    """Every valid slug: techniques in file order, then group nodes in file order."""
    return tuple(e.slug for e in tax.techniques) + tuple(g.slug for g in tax.group_nodes)


# ── renderings ───────────────────────────────────────────────────────────────────────────────


def _aka(synonyms: Sequence[str], instances: Sequence[str] = ()) -> str:
    parts = ([f"aka: {', '.join(synonyms)}"] if synonyms else []) + (
        [f"e.g. {', '.join(instances)}"] if instances else [])
    return f" ({'; '.join(parts)})" if parts else ""


def menu_line(e: Technique) -> str:
    """``- <slug> [<kind>] — <Name> (provisional) (aka: ...; e.g. ...) {child of: <parent>}: <definition>``
    (each optional part only when present)."""
    prov = " (provisional)" if e.provisional else ""
    child = f" {{child of: {e.parent}}}" if e.parent else ""
    return f"- {e.slug} [{e.kind}] — {e.name}{prov}{_aka(e.synonyms, e.instances)}{child}: {e.definition}"


def group_menu_line(g: GroupNode) -> str:
    """``- <slug> [group] — <Name> (aka: ...) {children: a, b}: <definition>``."""
    return f"- {g.slug} [group] — {g.name}{_aka(g.synonyms)} {{children: {', '.join(g.children)}}}: {g.definition}"


def menu_text(tax: Taxonomy) -> str:
    """The full annotator menu (module docstring). No trailing newline."""
    sections: list[str] = []
    for kind in KINDS:
        members = sorted((e for e in tax.techniques if e.kind == kind), key=lambda e: e.slug)
        if members:
            sections.append("\n".join([f"## {kind}", *(menu_line(e) for e in members)]))
    if tax.group_nodes:
        groups = sorted(tax.group_nodes, key=lambda g: g.slug)
        sections.append("\n".join(["## group nodes (generic-mention targets)",
                                   *(group_menu_line(g) for g in groups)]))
    return "\n\n".join(sections)


def compact_index(tax: Taxonomy) -> str:
    """``<slug> — <Name> [<kind>|group]`` (+ `` {child of: <parent>}`` for an entry with a parent), one
    line per entry and group node, all sorted by slug. No trailing newline."""
    rows = [(e.slug, f"{e.slug} — {e.name} [{e.kind}]" + (f" {{child of: {e.parent}}}" if e.parent else ""))
            for e in tax.techniques]
    rows += [(g.slug, f"{g.slug} — {g.name} [group]") for g in tax.group_nodes]
    return "\n".join(line for _, line in sorted(rows))


def _one_line(s: str) -> str:
    return " ".join(s.split())


def pointer_line(p: Mapping[str, Any]) -> str:
    """``- [<source>; <section>] "<surface>" — "<snippet>" (stance hint: <stance>)``; empty parts left
    out, whitespace collapsed so a pointer stays on one line. A pointer with a ``note`` is the line
    ``- <note>`` (a count or a summary line; the other keys must then be empty)."""
    unknown = sorted(set(p) - set(_POINTER_KEYS))
    if unknown:
        raise ValueError(f"pointer has unknown key(s) {unknown} (allowed: {list(_POINTER_KEYS)})")
    vals: dict[str, str] = {}
    for k in _POINTER_KEYS:
        v = p.get(k)
        if v is not None and not isinstance(v, str):
            raise ValueError(f"pointer `{k}` must be a string or None, got {type(v).__name__}")
        vals[k] = _one_line(v or "")
    if vals["note"]:
        if any(vals[k] for k in _POINTER_KEYS if k != "note"):
            raise ValueError("a pointer with a `note` has no other key")
        return f"- {vals['note']}"
    where = "; ".join(x for x in (vals["source"], vals["section"]) if x)
    parts = [f"[{where}]" if where else "", f'"{vals["surface"]}"' if vals["surface"] else ""]
    head = " ".join(x for x in parts if x)
    snippet = f'"{vals["snippet"]}"' if vals["snippet"] else ""
    body = " — ".join(x for x in (head, snippet) if x)
    stance = f" (stance hint: {vals['stance']})" if vals["stance"] else ""
    return f"- {body}{stance}"


def submenu_block(entry: Technique | GroupNode, pointers: Sequence[Mapping[str, Any]]) -> str:
    """One sub-menu block (see :func:`submenu_text`)."""
    if isinstance(entry, Technique):
        prov = "; provisional" if entry.provisional else ""
        head = (f"## {entry.slug} — {entry.name} [kind: {entry.kind}; parent: {entry.parent or 'none'}; "
                f"group node: no{prov}]")
        synonyms, instances = entry.synonyms, entry.instances
    else:
        head = f"## {entry.slug} — {entry.name} [kind: none; parent: none; group node: yes]"
        synonyms, instances = entry.synonyms, ()
    lines = [head, f"Definition: {entry.definition}"]
    if isinstance(entry, GroupNode):
        lines.append(f"Children: {', '.join(entry.children)}")
    also = " ".join(x for x in (f"Also called: {', '.join(synonyms)}." if synonyms else "",
                                f"Named instances: {', '.join(instances)}." if instances else "") if x)
    if also:
        lines.append(also)
    if pointers:
        lines.append("Pointers (not evidence; verify in the document):")
        lines.extend(pointer_line(p) for p in pointers)
    else:
        lines.append("Pointers: none.")
    return "\n".join(lines)


def submenu_text(tax: Taxonomy, slugs: Sequence[str],
                 pointers: Mapping[str, Sequence[Mapping[str, Any]]]) -> str:
    """The grading call's sub-menu: one block per slug, in the given order (the caller ranks), blocks
    separated by a blank line, no trailing newline. ``pointers`` maps a slug to its pointer dicts
    (keys ``source, section, surface, snippet, stance`` or ``note``, each a string or None); a slug without
    pointers gets ``Pointers: none.``. An unknown or repeated slug, or a pointer for a slug not in
    ``slugs``, raises ``ValueError``."""
    if len(set(slugs)) != len(slugs):
        raise ValueError(f"submenu: repeated slug(s) {sorted({s for s in slugs if list(slugs).count(s) > 1})}")
    stray = sorted(set(pointers) - set(slugs))
    if stray:
        raise ValueError(f"submenu: pointers for slug(s) not in the sub-menu {stray}")
    blocks: list[str] = []
    for slug in slugs:
        entry = tax.get(slug)
        if entry is None:
            raise ValueError(f"submenu: unknown slug {slug!r}")
        blocks.append(submenu_block(entry, pointers.get(slug, ())))
    return "\n\n".join(blocks)


__all__ = ["ENTRY_STATUSES", "KINDS", "SCHEMA", "GroupNode", "Taxonomy", "Technique", "all_slugs", "compact_index",
           "from_envelope", "group_menu_line", "load_taxonomy", "menu_line", "menu_text", "parse_taxonomy", "pointer_line",
           "submenu_block", "submenu_text", "taxonomy_digest"]
