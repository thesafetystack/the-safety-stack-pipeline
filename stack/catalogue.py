"""The catalogue envelope (D737, D738; ``docs/design/catalogue-envelope.md``): the one structure the taxonomy files
share, and the one validator path for it.

A catalogue file is YAML: ``version`` (``<major>.<minor>.<patch>``, the content version), ``meta`` (``schema`` = the
name of its JSON Schema, ``<catalogue>-<n>``, ``n`` counting structure versions on its own: models 1.1.0 is
``models-2``; ``updated``; ``status`` ``draft`` | ``released``; ``description``; ``sources``; ``references``, the
version of each catalogue its references resolve in; plus the catalogue's own keys) and one list ``entries``, each
with the core fields ``id``, ``name``, ``parent``, ``aliases`` (strings, or ``{text, kind}``), ``note`` and
``external_ids`` (``{<scheme>: [ids]}``) and the catalogue's own fields. The schemas are the data repository's
(vendored at ``data-repo/``, the single source): the shared ``taxonomies/schemas/catalogue-0.schema.json`` and per
catalogue ``taxonomies/<catalogue>/schemas/<catalogue>-<n>.schema.json``, which refers to it and closes the shape.

Readers: :mod:`stack.orgvocab` (orgs 0.2.0), :mod:`stack.modelvocab` (models 1.1.0) and
``workflows/annotate/technique_staged/taxonomy.py`` (techniques 1.1.0); each still reads its catalogue's earlier
shape (pinned eval sets and stored runs name those files).
"""

from __future__ import annotations

import functools
import json
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
#: The data repository's taxonomies (the git subtree ``data-repo/``): ``schemas/`` (the envelope) and
#: ``<catalogue>/schemas/``.
TAXONOMIES = REPO_ROOT / "data-repo" / "taxonomies"
#: The shared envelope's schema (``$id``); every catalogue schema refers to it.
ENVELOPE = "catalogue-0"


class CatalogueError(ValueError):
    """A catalogue file does not validate against its schema (the message names the first problems)."""


def schema_paths() -> dict[str, Path]:
    """Every schema file of the data repository by name (``catalogue-0``, ``orgs-0``, ``models-2``, ...)."""
    files = sorted(TAXONOMIES.glob("schemas/*.schema.json")) + sorted(TAXONOMIES.glob("*/schemas/*.schema.json"))
    return {p.name.removesuffix(".schema.json"): p for p in files}


@functools.lru_cache(maxsize=1)
def _registry() -> Any:
    from referencing import Registry, Resource

    resources = []
    for p in schema_paths().values():
        doc = json.loads(p.read_text("utf-8"))
        resources.append((doc["$id"], Resource.from_contents(doc)))
    return Registry().with_resources(resources)


def schema(name: str) -> dict[str, Any]:
    """The JSON Schema ``<name>.schema.json`` of the data repository (:func:`schema_paths`)."""
    p = schema_paths().get(name)
    if p is None:
        raise CatalogueError(f"no catalogue schema {name}.schema.json under {TAXONOMIES.relative_to(REPO_ROOT)}")
    doc: dict[str, Any] = json.loads(p.read_text("utf-8"))
    return doc


def validate(data: Mapping[str, Any], name: str) -> None:
    """Validate ``data`` against schema ``name`` (which refers to the envelope); ``meta.schema`` must name it and
    entry ids must be unique. Raises :class:`CatalogueError`."""
    from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

    meta = data.get("meta") if isinstance(data, Mapping) else None
    if not isinstance(meta, Mapping) or meta.get("schema") != name:
        raise CatalogueError(f"catalogue: meta.schema is {(meta or {}).get('schema')!r}, expected {name!r}")
    v = Draft202012Validator(schema(name), registry=_registry())
    errors = sorted(v.iter_errors(data), key=lambda e: [str(x) for x in e.absolute_path])
    if errors:
        shown = "; ".join(f"{'/'.join(str(x) for x in e.absolute_path) or '<root>'}: {e.message[:200]}"
                          for e in errors[:5])
        raise CatalogueError(f"catalogue {name} {data.get('version')}: {len(errors)} schema problem(s): {shown}")
    counts = Counter(e["id"] for e in data["entries"])
    dup = sorted(i for i, n in counts.items() if n > 1)
    if dup:
        raise CatalogueError(f"catalogue {name} {data.get('version')}: duplicate ids {dup[:5]}")


__all__ = ["ENVELOPE", "TAXONOMIES", "CatalogueError", "schema", "schema_paths", "validate"]
