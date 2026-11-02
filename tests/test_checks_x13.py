"""X13 ``gate_obeyed`` on a triage-``skip`` row that ``carry`` gated (T3.6-f4).

The first acquire over a ``skip`` row writes its roles ``skipped[gated]`` with no attempt (D108);
X13 counts a role as attempted only when the file records one (``last_attempt_at``/``attempts``,
or a raw snapshot minted in the file). The fetched-``skip`` fixture itself (``x13``) is red in
``tests/test_checks_fixtures.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from stack.checks import CHECK_TABLE, REGISTRY
from stack.checks.verify import run_checks
from tests.fixtures.checks import clean_world, write_world
from tests.fixtures.checks.triage import x13_gated

pytestmark = pytest.mark.checks  # area marker(s), not a tier (pyproject markers; docs/SETUP.md §7)

IMPLEMENTED = [name for _, name, _ in CHECK_TABLE if REGISTRY[name].fn is not None]


def _results(root: Path, mark: str | None) -> dict[str, tuple[str, list[str]]]:
    world = clean_world()
    x13_gated(world, mark=mark)
    write_world(world, root)
    assert world.refusals == []
    return {r.check: (r.status, r.items) for r in run_checks(root) if r.check in IMPLEMENTED}


def test_gated_skip_row_is_not_a_fetch(tmp_path: Path) -> None:
    by = _results(tmp_path, None)
    assert {n: s for n, (s, _) in by.items() if s != "ok"} == {}, by


@pytest.mark.parametrize("mark", ["attempt", "snapshot"])
@pytest.mark.slow
def test_gated_skip_row_with_a_recorded_fetch_is_red(tmp_path: Path, mark: str) -> None:
    by = _results(tmp_path, mark)
    red = {n: items for n, (s, items) in by.items() if s != "ok"}
    assert set(red) == {"gate_obeyed"}, red
    assert any("fetched ['full_text/" in i and "under skip ok[None]" in i
               for i in red["gate_obeyed"]), red["gate_obeyed"]
