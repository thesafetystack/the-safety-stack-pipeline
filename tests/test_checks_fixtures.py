"""The checks of Appendix B against one failing store each (``tests/fixtures/checks``).

Each implemented check goes red on exactly its own fixture (no other check goes red there) and
every check is green on the clean store (every check is implemented; D33's
``skipped[not_implemented]`` no longer occurs); X11 is the one warn-level check. S-check fixtures are also
refused by the family writer, with exactly their check.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from stack.checks import CHECK_TABLE, REGISTRY
from stack.checks.verify import run_checks, summary
from tests.fixtures.checks import FIXTURES, build_store

pytestmark = pytest.mark.checks  # area marker(s), not a tier (pyproject markers; docs/SETUP.md §7)

IMPLEMENTED = [name for _, name, _ in CHECK_TABLE if REGISTRY[name].fn is not None]
RED = ("failed", "warn")
#: The one fixture that cannot be red on its check alone: pyarrow refuses a NULL in a
#: non-nullable column on write, so S2's file declares the column nullable and S1 reports that.
ALSO_RED: dict[str, set[str]] = {"nonnull": {"schema_equal"},
                                  # D372: the writer and S7 hold base == after and the reader
                                  # after == predecessor, so a foreign base is S7's too (D375).
                                  "head_descends": {"kv_complete"},
                                  # Appendix B lists "roles per (source_id, content_role)" under
                                  # both S5 and V6 (T3.1), so S5's duplicated role is V6's too.
                                  "nested_unique": {"V-content"}}


def test_every_implemented_check_has_one_fixture() -> None:
    assert sorted(FIXTURES) == sorted(IMPLEMENTED)
    assert set(FAST_IDS) <= set(FIXTURES)  # a renamed fixture must not drop out of the fast tier silently
    assert len(CHECK_TABLE) == 47 and len({n for _, n, _ in CHECK_TABLE}) == 47


def test_clean_store_is_green(tmp_path: Path) -> None:
    world = build_store(tmp_path)
    assert world.refusals == []
    results = run_checks(tmp_path)
    assert [r.check for r in results] == [n for _, n, _ in CHECK_TABLE]
    by = {r.check: r for r in results}
    assert IMPLEMENTED == [n for _, n, _ in CHECK_TABLE]  # every check landed (T4.2: X16 was the last)
    for name in IMPLEMENTED:
        assert by[name].status == "ok", (name, by[name].items)
    assert by["dead_runs_seen"].level == "warn"
    assert {r.level for r in results if r.check != "dead_runs_seen"} == {"hard"}
    assert summary(results) == f"{len(CHECK_TABLE)} checks: {len(CHECK_TABLE)} ok"


#: The fixture that stays in the fast tier: S1 (an S fixture, also refused by the writer). Every other fixture
#: case (a store and all 47 checks each, ~5-10 s at -n 2) is ``slow``; the fast tier keeps X23 green on the clean
#: store (test_x10_skips_without_builds_x23_does_not) and V11 red in tests/test_verify_modes.py (the test-tier work).
FAST_IDS = ("schema_equal",)


@pytest.mark.parametrize("check", [c if c in FAST_IDS else pytest.param(c, marks=pytest.mark.slow)
                                   for c in FIXTURES])
def test_check_red_on_exactly_its_fixture(tmp_path: Path, check: str) -> None:
    world = build_store(tmp_path, check)
    results = run_checks(tmp_path)
    red = {r.check: r.items for r in results if r.status in RED}
    assert set(red) == {check} | ALSO_RED.get(check, set()), red
    errors = [r for r in results if r.error_class == "check_error"]
    assert not errors, errors
    if REGISTRY[check].code.startswith("S"):
        assert world.refusals, "an S-check fixture must be refused by the writer"
        assert all(checks == {check} for _, checks in world.refusals), world.refusals
    else:  # only an X fixture the writer itself refuses beside it (X23: S7's base == after) is refused
        assert all(checks <= ALSO_RED.get(check, set()) for _, checks in world.refusals), world.refusals


def test_x10_skips_without_builds_x23_does_not(tmp_path: Path) -> None:
    """X23 is per file since D375: no build is needed; X10 still needs one."""
    from tests.fixtures.checks import clean_world, write_world

    world = clean_world()
    world.build = None
    write_world(world, tmp_path)
    by = {r.check: r for r in run_checks(tmp_path, names=["ordering_pinned", "head_descends"])}
    assert {n: (r.status, r.error_class) for n, r in by.items()} == {
        "ordering_pinned": ("skipped", "no_builds"), "head_descends": ("ok", None)}
