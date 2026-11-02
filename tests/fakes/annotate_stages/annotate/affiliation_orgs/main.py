"""TEST-ONLY shim: the REAL ``annotate.affiliation_orgs`` stage (``workflows/annotate/affiliation_orgs/main.py``,
which asks no model) under the fake stage root, so it runs after the fake ``annotate.affiliation`` of
``tests/fakes/annotate.py`` (``STACK_WORKFLOWS_ROOT=tests/fakes/annotate_stages``)."""

from __future__ import annotations

from pathlib import Path

from stack.stage_runner import load_path_module

_REAL = load_path_module("_affiliation_orgs__real",
                         Path(__file__).resolve().parents[5] / "workflows" / "annotate" / "affiliation_orgs" / "main.py")
assert _REAL is not None
OPTIONS, PURE_SUBSTAGES, SUBSTAGES, WORKFLOW, run = (_REAL.OPTIONS, _REAL.PURE_SUBSTAGES, _REAL.SUBSTAGES,
                                                     _REAL.WORKFLOW, _REAL.run)

__all__ = ["OPTIONS", "PURE_SUBSTAGES", "SUBSTAGES", "WORKFLOW", "run"]
