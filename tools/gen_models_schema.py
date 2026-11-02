#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pydantic>=2"]
# ///
"""Generate ``spec/schemas/config/models.schema.json`` from ``stack.modelvocab.ModelVocab``.

``tools/check_configs.py`` validates every ``configs/*.yaml`` against a JSON Schema of the
same stem, so the model-mention vocabulary needs one. Rather than hand-writing a second
definition of the same shape, this GENERATES it from the pydantic model (ported from v3
``tomas/model-roles``). ``tests/test_annotate_model_roles_match.py`` fails if the committed file
drifts from what this script would write.

Usage (from the repo root):  uv run --no-sync python tools/gen_models_schema.py
"""
from __future__ import annotations

import json
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from stack.modelvocab import ModelVocab  # noqa: E402 — needs the sys.path insert above

OUT = REPO / "spec" / "schemas" / "config" / "models.schema.json"
HEADER = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://thesafetystack.ai/schemas/config/models.schema.json",
    "title": "models.yaml",
    "description": (
        "The model/family mention vocabulary: a flat map of family/group/model nodes with "
        "parent pointers and per-node mention patterns (doc/model-collection/schema.md v0.3). "
        "GENERATED from stack.modelvocab.ModelVocab by tools/gen_models_schema.py — edit the "
        "pydantic model, not this file."
    ),
}


def build() -> dict[str, object]:
    schema = ModelVocab.model_json_schema()
    schema.pop("title", None)
    schema.pop("description", None)
    return {**HEADER, **schema}


if __name__ == "__main__":
    OUT.write_text(json.dumps(build(), indent=2) + "\n")
    print(f"wrote {OUT.relative_to(REPO)}")
