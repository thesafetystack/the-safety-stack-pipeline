"""``tools/devtest.py`` and its two lists (A11, D490): every ``stack/**`` and ``workflows/**`` file is in
``tests/modules.toml`` (a new module cannot fall out of the map), every name in the map and line in
``tests/smoke.txt`` exists, and the selection rules."""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("devtest", ROOT / "tools" / "devtest.py")
assert _spec and _spec.loader
devtest = importlib.util.module_from_spec(_spec)
sys.modules["devtest"] = devtest  # dataclasses resolve the module by name
_spec.loader.exec_module(devtest)


def _repo_files(*tops: str) -> list[str]:
    out = []
    for top in tops:
        for p in (ROOT / top).rglob("*"):
            if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc":
                out.append(p.relative_to(ROOT).as_posix())
    return sorted(out)


def _missing(target: str) -> str | None:
    """Why a pytest target does not exist (``None`` when it does): no file, or no ``def``/``class`` of the node."""
    file, _, node = target.partition("::")
    path = ROOT / file
    if not path.is_file():
        return f"{target}: no file {file}"
    name = node.split("::")[-1].split("[", 1)[0]
    if name and not re.search(rf"^\s*(async\s+)?(def|class)\s+{re.escape(name)}\b", path.read_text(), re.M):
        return f"{target}: no {name} in {file}"
    return None


def test_every_stack_and_workflow_file_is_mapped() -> None:
    entries = devtest.load_map()
    files = [f for f in _repo_files("stack", "workflows") if f.startswith("workflows/") or f.endswith(".py")]
    assert len(files) > 100
    assert devtest.uncovered(files, entries) == []


def test_a_new_module_is_reported_unmapped() -> None:
    entries = devtest.load_map()
    assert devtest.uncovered(["stack/brand_new.py", "stack/build.py"], entries) == ["stack/brand_new.py"]


def test_every_map_name_exists() -> None:
    names = {n for e in devtest.load_map() for n in (*e.tests, *e.slow)}
    assert [m for n in sorted(names) if (m := _missing(devtest.target(n))) is not None] == []


def test_every_smoke_line_exists() -> None:
    lines = devtest.load_smoke()
    assert len(lines) > 30
    assert [m for t in lines if (m := _missing(t)) is not None] == []


def test_missing_target_is_red() -> None:
    assert _missing("tests/test_ids.py::test_there_is_no_such_test") is not None
    assert _missing("tests/test_no_such_file.py") is not None


def test_select_maps_touched_paths() -> None:
    entries = devtest.load_map()
    got, unmapped = devtest.select(["stack/collect/base.py", "tests/test_ids.py", "README.md"], entries)
    assert "tests/test_collect_base.py" in got and "tests/test_ids.py" in got
    assert "tests/test_passes_collect.py" not in got
    assert unmapped == ["README.md"]
    slow, _ = devtest.select(["stack/collect/base.py"], entries, with_slow=True)
    assert "tests/test_passes_collect.py" in slow


def test_select_drops_a_deleted_test_file() -> None:
    assert devtest.select(["tests/test_gone_away.py"], devtest.load_map()) == ([], [])


def test_merge_drops_node_ids_of_whole_files() -> None:
    got = devtest.merge(["tests/test_a.py", "tests/test_b.py::t"], ["tests/test_a.py::u", "tests/test_b.py::t"])
    assert got == ["tests/test_a.py", "tests/test_b.py::t"]


@pytest.mark.parametrize(("with_slow", "marker"), [(False, True), (True, False)])
def test_pytest_args(with_slow: bool, marker: bool) -> None:
    args = devtest.pytest_args(["tests/test_ids.py"], with_slow=with_slow, workers=4, fast_budget=2.0)
    assert (devtest.DEV_MARKER in args) is marker and args[args.index("-n") + 1] == "4"
    assert "--durations-min=2.0" in args and args[-1] == "tests/test_ids.py"
    assert "-n" not in devtest.pytest_args([], with_slow=False, workers=0, fast_budget=None)


def test_check_all_tiers_and_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    """``tools/check_all.py`` (the test-tier work): the full suite by default at the memory-aware -n (cap 6, floor 2), ``--tier fast`` is ``-m "not slow"``,
    ``-n`` and ``$STACK_TEST_WORKERS`` set the workers, ``--no-sync`` reaches every leg, and the tier's marker
    expression names only registered markers."""
    import subprocess
    import tomllib

    spec = importlib.util.spec_from_file_location("check_all", ROOT / "tools" / "check_all.py")
    assert spec and spec.loader
    ca = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ca)
    calls: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: (calls.append(cmd), subprocess.CompletedProcess(cmd, 0))[1])

    def suite(*argv: str) -> list[str]:
        calls.clear()
        assert ca.main(list(argv)) == 0
        assert len(calls) == len(ca.CHECKS)
        return calls[-1]

    monkeypatch.delenv("STACK_TEST_WORKERS", raising=False)
    full = suite()
    assert "-m" not in full and full[full.index("-n") + 1] == str(ca.default_workers()) and full[:3] == ["uv", "run", "pytest"]
    fast = suite("--tier", "fast", "-n", "0", "--no-sync")
    assert fast[fast.index("-m") + 1] == "not slow" and "-n" not in fast
    assert all(c[:3] == ["uv", "run", "--no-sync"] for c in calls)
    monkeypatch.setenv("STACK_TEST_WORKERS", "3")
    assert suite()[-1] == "3" and suite("-n", "1")[-1] == "1"
    assert ca.MIN_WORKERS <= ca.default_workers() <= ca.DEFAULT_WORKERS
    for kb, want in {3000000: 2, 6000000: 4, 14000000: 6, 500000: 2}.items():
        monkeypatch.setattr(ca.Path, "read_text", lambda self, *a, _kb=kb, **k: f"MemTotal: 1 kB\nMemAvailable: {_kb} kB\n")
        assert ca.default_workers() == want
    monkeypatch.undo()
    registered = {m.split(":", 1)[0] for m in tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["pytest"][
        "ini_options"]["markers"]}
    assert {"slow", "e2e", "checks", "evalsets", "tools"} <= registered
    assert all(set(re.findall(r"\w+", e)) - {"not", "and", "or"} <= registered for e in ca.TIERS.values() if e)
