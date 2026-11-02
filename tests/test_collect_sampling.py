"""Tests for the uniform collect scale knobs (``stack.collect.sampling``). (The
``aggregate.coverage_report`` tests retired with the DB and the aggregate workflows at T1.8, D40.)

The sampling tests are mostly about STABILITY, because that is the property the feature exists
for: a fractional run has to be a rehearsal of the full sweep — the same slice every time, on any
machine — rather than a random walk that never lets a query accumulate a complete receipt.
"""

from __future__ import annotations

import pytest

from stack.collect.sampling import ENV_FRACTION, ENV_MAX_QUERIES, in_fraction, select

UNITS = [f"unit-{i}" for i in range(1000)]


# ─────────────────────────────────────────────────────────────────────────────
# (A) fraction selection
# ─────────────────────────────────────────────────────────────────────────────
def test_no_env_is_the_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """A full run must be untouched by the feature existing."""
    monkeypatch.delenv(ENV_FRACTION, raising=False)
    monkeypatch.delenv(ENV_MAX_QUERIES, raising=False)
    got, meta = select(UNITS, str)
    assert got == UNITS
    assert meta == {"planned": 1000, "selected": 1000, "fraction": 1.0, "max_queries": 0}


def test_fraction_is_approximately_the_requested_share(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_MAX_QUERIES, raising=False)
    for fraction, lo, hi in [(0.1, 70, 130), (0.5, 450, 550), (0.01, 3, 25)]:
        monkeypatch.setenv(ENV_FRACTION, str(fraction))
        got, meta = select(UNITS, str)
        assert lo <= len(got) <= hi, (fraction, len(got))
        assert meta["selected"] == len(got) and meta["planned"] == 1000


def test_selection_is_stable_across_calls_and_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE property: the same fraction picks the same units every run. A time- or PRNG-seeded
    sample would walk a different slice each time, so no query would ever accumulate a complete
    receipt and no watermark would advance."""
    monkeypatch.setenv(ENV_FRACTION, "0.2")
    first, _ = select(UNITS, str)
    second, _ = select(list(reversed(UNITS)), str)
    assert first == [u for u in UNITS if u in set(second)]
    # sha256-derived, so it does not depend on PYTHONHASHSEED / process identity
    assert in_fraction("unit-7", 0.2) is in_fraction("unit-7", 0.2)


def test_fraction_is_nested_so_a_bigger_run_is_a_superset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scaling a rehearsal up must ADD work, not reshuffle it: 10% ⊂ 25% ⊂ 50%. That is what
    makes a small run a growable prefix of the full sweep instead of a separate experiment."""
    picks = {}
    for f in ("0.1", "0.25", "0.5"):
        monkeypatch.setenv(ENV_FRACTION, f)
        picks[f], _ = select(UNITS, str)
    assert set(picks["0.1"]) < set(picks["0.25"]) < set(picks["0.5"])


def test_cap_applies_after_the_fraction_and_preserves_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ENV_FRACTION, "0.5")
    monkeypatch.setenv(ENV_MAX_QUERIES, "7")
    got, meta = select(UNITS, str)
    assert len(got) == 7
    assert got == [u for u in UNITS if u in set(got)]      # order preserved, no reshuffle
    assert meta == {"planned": 1000, "selected": 7, "fraction": 0.5, "max_queries": 7}


def test_cap_alone_is_the_old_first_n_behaviour(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no fraction set, MAX_QUERIES keeps meaning exactly what it did before."""
    monkeypatch.delenv(ENV_FRACTION, raising=False)
    monkeypatch.setenv(ENV_MAX_QUERIES, "3")
    got, _ = select(UNITS, str)
    assert got == UNITS[:3]


@pytest.mark.parametrize("bad", ["0", "-0.5", "1.5", "abc"])
def test_invalid_fraction_fails_loudly(monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    """A typo'd fraction must not silently become "run everything" or "run nothing"."""
    monkeypatch.setenv(ENV_FRACTION, bad)
    with pytest.raises(ValueError, match=ENV_FRACTION):
        select(UNITS, str)


def test_empty_plan_is_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_FRACTION, "0.5")
    got, meta = select([], str)
    assert got == [] and meta["planned"] == 0


def test_small_plan_always_keeps_one_unit(monkeypatch: pytest.MonkeyPatch) -> None:
    """A scraper has 5 surfaces and a watcher 13; any modest fraction rounds them to zero, and a
    workflow that scans nothing is a useless rehearsal. A non-empty plan keeps its lowest-bucket
    unit and says so in the meta."""
    monkeypatch.delenv(ENV_MAX_QUERIES, raising=False)
    monkeypatch.setenv(ENV_FRACTION, "0.01")
    tiny = ["surface-a", "surface-b", "surface-c"]
    got, meta = select(tiny, str)
    assert len(got) == 1 and meta["floored"] is True
    assert meta["planned"] == 3 and meta["selected"] == 1
    # deterministic, and the SAME unit a larger fraction admits first (nesting survives)
    monkeypatch.setenv(ENV_FRACTION, "0.5")
    bigger, meta2 = select(tiny, str)
    assert set(got) <= set(bigger)
    assert "floored" not in meta2 or len(bigger) > 1


def test_floor_does_not_fire_on_an_empty_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing planned means nothing to run — the floor must not invent a unit."""
    monkeypatch.setenv(ENV_FRACTION, "0.01")
    got, meta = select([], str)
    assert got == [] and "floored" not in meta
