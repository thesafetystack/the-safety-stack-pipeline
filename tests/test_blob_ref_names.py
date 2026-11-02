"""No old blob-reference name left in the pipeline code (D690).

The names v5/v3 used for blob references are read with ``.get`` in places (the gather entry key, the
excerpt, the release helpers), where a missed rename returns NULL instead of failing. So every string
literal and attribute name in ``stack/``, ``workflows/`` and ``tools/`` is scanned for them; only the
places that legitimately keep an old name are allowed: the hash helpers named ``raw_digest``/``text_digest``,
JSON keys that are not blob references of the stored schema (eval items, a payload's grounding), and the read of
gates stored before the rename (D693). The one-off upcast and the older steps that named them went with D693.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
OLD = ("raw_digest", "text_digest", "current_text_digest", "inband_digests", "convert_config_digest",
       "inband_text_digest", "inband_sha256s", "inband_excerpt_digest", "topic_digest")
#: path -> the old names it may use (and why).
ALLOWED: dict[str, set[str]] = {
    "stack/schemas/upcasters.py": {"inband_digests"},  # a version note names what changed
    "stack/core/hashes.py": {"raw_digest", "text_digest"},  # the hash helpers' names
    "stack/collect/inband.py": {"raw_digest", "text_digest"},  # imports of the hash helpers
    "stack/evals/freeze.py": {"text_digest"},  # eval item key input.text_digest (frozen sets on disk)
    "stack/evals/sr.py": {"text_digest"},
    "stack/evals/annotators.py": {"text_digest"},
    "stack/evals/harness.py": {"text_digest"},
    "stack/evals/score.py": {"text_digest"},
    "stack/evals/release_quality.py": {"text_digest"},  # the judge eval items' input.text_digest (D728)
    "tools/fetch_eval_texts.py": {"text_digest"},  # an eval item's top-level text_digest (screen 2026-10-13-t2pool, D790)
    "workflows/annotate/affiliation/extract.py": {"text_digest"},  # the payload's grounding entries (JSON)
    "stack/gate.py": {"inband_text_digest", "text_digest"},  # gates stored before D690 name the old view column
}


def _names(tree: ast.AST) -> set[str]:
    """The old names in ``tree``'s string literals (docstrings aside: prose may name the history),
    attribute names and imports."""
    docs = {id(n.body[0].value) for n in ast.walk(tree)
            if isinstance(n, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) and n.body
            and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            out |= {n for n in OLD if n in node.value.replace("full_text_digest", "")}
        elif isinstance(node, ast.Attribute) and node.attr in OLD:
            out.add(node.attr)
        elif isinstance(node, ast.alias) and node.name in OLD:
            out.add(node.name)
    return out


@pytest.mark.slow
def test_no_old_blob_reference_names() -> None:
    bad: dict[str, set[str]] = {}
    for base in ("stack", "workflows", "tools"):
        for p in sorted((REPO / base).rglob("*.py")):
            rel = p.relative_to(REPO).as_posix()
            found = _names(ast.parse(p.read_text("utf-8"))) - ALLOWED.get(rel, set())
            if found:
                bad[rel] = found
    assert bad == {}


@pytest.mark.parametrize("src", ['x = row["text_digest"]', "y = e.raw_digest", 'z = {"inband_digests": []}'])
def test_the_scan_sees_each_form(src: str) -> None:
    assert _names(ast.parse(src))
