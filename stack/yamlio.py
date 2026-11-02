"""The one YAML reader for config, plan, release and override files (D433).

``load(text)`` is ``yaml.load`` with libyaml's ``CSafeLoader`` (the pure-Python ``SafeLoader``
when PyYAML was built without libyaml: same result, ~5x slower on ``models.yaml``), and a
duplicate mapping key is REFUSED: PyYAML keeps the last of two equal keys without a word, so a
second ``options:`` on a plan line silently dropped the first one's keys (finding L6-17). The
error is a ``yaml.constructor.ConstructorError`` (a ``yaml.YAMLError``, which every caller
already wraps with its file name) naming the key and the line and column of both occurrences.

Merge keys (``<<: *base``) are not duplicates: a key a mapping states itself overrides the merged
one, as YAML says. Only scalar keys are compared (no config or plan uses another kind).
Digests and config stamps hash the raw file bytes, never the parsed data, so the loader choice
moves none of them.
"""

from __future__ import annotations

from typing import Any

import yaml
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode, ScalarNode

_MERGE_TAG = "tag:yaml.org,2002:merge"


class _RefuseDuplicates:
    """Mixin for a ``SafeConstructor`` loader: check each mapping's own keys once, before
    ``flatten_mapping`` folds merged keys in (it rewrites ``node.value`` in place, so a second
    visit would see merged keys next to their overrides)."""

    def flatten_mapping(self, node: MappingNode) -> None:
        checked: set[int] = self.__dict__.setdefault("_dup_checked", set())
        if id(node) not in checked:
            checked.add(id(node))
            seen: dict[Any, ScalarNode] = {}
            for key_node, _ in node.value:
                if key_node.tag == _MERGE_TAG or not isinstance(key_node, ScalarNode):
                    continue
                key = self.construct_object(key_node)  # type: ignore[attr-defined]
                first = seen.setdefault(key, key_node)
                if first is not key_node:
                    m = first.start_mark
                    raise ConstructorError(
                        "while constructing a mapping", node.start_mark,
                        f"found duplicate key {key!r} (first at line {m.line + 1}, column {m.column + 1})",
                        key_node.start_mark)
        super().flatten_mapping(node)  # type: ignore[misc]


class StrictCSafeLoader(_RefuseDuplicates, getattr(yaml, "CSafeLoader", yaml.SafeLoader)):  # type: ignore[misc]
    """libyaml's safe loader (or the pure-Python one), refusing duplicate mapping keys."""


class StrictSafeLoader(_RefuseDuplicates, yaml.SafeLoader):
    """The pure-Python safe loader refusing duplicate mapping keys (tests compare the two)."""


def load(text: str | bytes) -> Any:
    """Parse one YAML document safely (libyaml when present), refusing duplicate keys."""
    return yaml.load(text, Loader=StrictCSafeLoader)  # a SafeLoader subclass


__all__ = ["StrictCSafeLoader", "StrictSafeLoader", "load"]
