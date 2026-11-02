"""The dev test tier (A11, D490): the tests for what you touched, plus a smoke set.

    uv run --no-sync python tools/devtest.py                 # touched paths' tests + the smoke set, -n 2
    uv run --no-sync python tools/devtest.py --smoke         # the smoke set only (about 1 min serial)
    uv run --no-sync python tools/devtest.py --touched       # the touched paths' tests only
    uv run --no-sync python tools/devtest.py stack/build.py  # these paths instead of git's
    uv run --no-sync python tools/devtest.py --list          # print the selection, run nothing
    uv run --no-sync python tools/devtest.py -- -x -k guard  # anything after `--` goes to pytest

**Touched paths**: ``git diff --name-only HEAD`` (staged and unstaged) plus untracked files; with ``--base REF``
also every path changed since ``git merge-base REF HEAD`` (a branch's commits). Explicit paths replace git's.
Each path maps to test files through ``tests/modules.toml`` (fnmatch globs; the union of every matching entry);
a touched ``tests/test_*.py`` runs itself; a path that matches nothing is listed as ``unmapped`` (it adds no test).
**Smoke set**: ``tests/smoke.txt`` (whole files and node ids; pytest errors on an id that no longer exists).

Always ``-q -p no:cacheprovider`` and ``-m "not slow and not tools"``. ``--with-slow`` adds each entry's ``slow``
files and drops the marker filter (scenario worlds, crash tests, tool tests). ``-n N`` (default 2; ``0`` serial) is
pytest-xdist's worker count; never ``auto`` while an e2e or a build runs on the machine. ``--fast-budget S``
reports every test slower than S seconds (``--durations=0 --durations-min=S``), the L6-1 guard for unmarked heavy
tests. Prints the selection, then pytest's output, then the wall time; exits with pytest's status (5, "no tests
collected", becomes 0: the marker filter left nothing, e.g. only tool tests were touched).

Cadence (`docs/SETUP.md` §7, D490): this tier per edit; ``pytest -m "not slow" -n 2`` (the fast tier, a few minutes) at
a sub-task's end and before a commit; the full suite (``tools/check_all.py``) at a sub-project's end.
"""

from __future__ import annotations

import argparse
import fnmatch
import subprocess
import sys
import time
import tomllib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SMOKE = ROOT / "tests" / "smoke.txt"
MODULES = ROOT / "tests" / "modules.toml"
DEV_MARKER = "not slow and not tools"
#: Two workers: the box is shared by several agents and builds (an e2e at -n 4 starves them; never ``auto``).
DEFAULT_WORKERS = 2


@dataclass(frozen=True)
class Entry:
    """One ``[[map]]`` entry of ``tests/modules.toml``."""

    paths: tuple[str, ...]
    tests: tuple[str, ...]
    slow: tuple[str, ...]

    def matches(self, path: str) -> bool:
        return any(fnmatch.fnmatchcase(path, g) for g in self.paths)


def load_map(path: Path = MODULES) -> list[Entry]:
    """The entries of ``tests/modules.toml``; a malformed entry raises ``ValueError``."""
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    out = []
    for i, e in enumerate(data.get("map", [])):
        extra = set(e) - {"paths", "tests", "slow"}
        if extra or not e.get("paths"):
            raise ValueError(f"{path}: [[map]] #{i + 1}: needs `paths`; unknown keys {sorted(extra)}")
        out.append(Entry(tuple(e["paths"]), tuple(e.get("tests", ())), tuple(e.get("slow", ()))))
    return out


def load_smoke(path: Path = SMOKE) -> list[str]:
    """The smoke set's lines (pytest targets), comments and blank lines dropped."""
    lines = (ln.split("#", 1)[0].strip() for ln in path.read_text(encoding="utf-8").splitlines())
    return [ln for ln in lines if ln]


def target(name: str) -> str:
    """A map name as a pytest target: ``store_chain`` -> ``tests/test_store_chain.py``, ``x::t`` ->
    ``tests/test_x.py::t``."""
    stem, sep, node = name.partition("::")
    return f"tests/test_{stem}.py{sep}{node}"


def is_test_file(path: str) -> bool:
    p = Path(path)
    return p.parts[:1] == ("tests",) and p.name.startswith("test_") and p.suffix == ".py"


def select(paths: Iterable[str], entries: Sequence[Entry], *, with_slow: bool = False
           ) -> tuple[list[str], list[str]]:
    """``(targets, unmapped)`` for the touched ``paths``: the tests of every entry a path matches (and its ``slow``
    ones with ``with_slow``), a touched test file itself; first-seen order, unique. ``unmapped``: the paths that
    matched nothing (a touched test file that no longer exists is dropped)."""
    targets: list[str] = []
    unmapped: list[str] = []
    for p in paths:
        if is_test_file(p):
            if (ROOT / p).exists():
                targets.append(p)
            continue
        hit = [e for e in entries if e.matches(p)]
        if not hit:
            unmapped.append(p)
        for e in hit:
            targets += [target(n) for n in (*e.tests, *(e.slow if with_slow else ()))]
    return list(dict.fromkeys(targets)), unmapped


def uncovered(paths: Iterable[str], entries: Sequence[Entry]) -> list[str]:
    """The ``paths`` no entry's glob matches (the module-map test's red set)."""
    return [p for p in paths if not any(e.matches(p) for e in entries)]


def merge(*groups: Iterable[str]) -> list[str]:
    """The targets of all ``groups``, unique, a node id dropped when its whole file is already selected."""
    flat = list(dict.fromkeys(t for g in groups for t in g))
    files = {t for t in flat if "::" not in t}
    return [t for t in flat if "::" not in t or t.split("::", 1)[0] not in files]


def _git(*args: str) -> list[str]:
    res = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True)
    return [ln for ln in res.stdout.splitlines() if ln]


def touched(base: str | None = None) -> list[str]:
    """The working tree's changed and untracked paths, plus those since ``merge-base(base, HEAD)``."""
    paths = _git("diff", "--name-only", "HEAD") + _git("ls-files", "--others", "--exclude-standard")
    if base is not None:
        mb = _git("merge-base", base, "HEAD")[0]
        paths += _git("diff", "--name-only", mb, "HEAD")
    return sorted(dict.fromkeys(paths))


def pytest_args(targets: Sequence[str], *, with_slow: bool, workers: int, fast_budget: float | None,
                extra: Sequence[str] = ()) -> list[str]:
    args = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
    if not with_slow:
        args += ["-m", DEV_MARKER]
    if workers > 0:
        args += ["-n", str(workers)]
    if fast_budget is not None:
        args += ["--durations=0", f"--durations-min={fast_budget}"]
    return [*args, *extra, *targets]


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    extra: list[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, extra = argv[:i], argv[i + 1:]
    ap = argparse.ArgumentParser(prog="tools/devtest.py", description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("paths", nargs="*", help="Touched paths (default: git's, see --base).")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--smoke", action="store_true", help="Run the smoke set only.")
    mode.add_argument("--touched", action="store_true", help="Run the touched paths' tests only (no smoke set).")
    ap.add_argument("--base", help="Also the paths changed since merge-base(BASE, HEAD).")
    ap.add_argument("--with-slow", action="store_true", help="Add the map's slow files; drop the marker filter.")
    ap.add_argument("-n", dest="workers", type=int, default=DEFAULT_WORKERS,
                    help=f"pytest-xdist workers (default {DEFAULT_WORKERS}; 0 = serial).")
    ap.add_argument("--fast-budget", type=float, metavar="S", help="Report tests slower than S seconds.")
    ap.add_argument("--list", action="store_true", help="Print the selection and exit.")
    a = ap.parse_args(argv)

    smoke = load_smoke()
    picked: list[str] = []
    unmapped: list[str] = []
    if not a.smoke:
        paths = a.paths or touched(a.base)
        picked, unmapped = select(paths, load_map(), with_slow=a.with_slow)
        print(f"touched: {len(paths)} path(s) -> {len(picked)} test target(s)")
        for u in unmapped:
            print(f"  unmapped (no test): {u}")
    targets = merge(picked, () if a.touched else smoke)
    print(f"selection ({len(targets)}): " + " ".join(t.removeprefix("tests/") for t in targets), flush=True)
    if a.list:
        return 0
    if not targets:
        print("nothing to run")
        return 0
    cmd = pytest_args(targets, with_slow=a.with_slow, workers=a.workers, fast_budget=a.fast_budget, extra=extra)
    t0 = time.monotonic()
    rc = subprocess.run(cmd, cwd=ROOT).returncode
    print(f"devtest: {time.monotonic() - t0:.1f} s wall, pytest exit {rc}")
    if rc == 5:  # every selected test deselected by the marker (e.g. only tool tests touched)
        print("devtest: no test left after the marker filter (--with-slow runs them)")
        return 0
    return rc


if __name__ == "__main__":
    sys.exit(main())
