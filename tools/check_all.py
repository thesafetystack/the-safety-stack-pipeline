#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""The single check gate (G-S8; run by hand, there is no CI): run every spec check and fail if any fails.

Runs, from the repo root:
  1. uv run tools/check_configs.py           (configs + payloads + schemas + cross-checks)
  2. uv run --with mypy mypy spec/types/interfaces.py
  3. uv run pytest tests/ -q -p no:cacheprovider -n N   (the full suite: both tiers)

Usage (from the repo root):
  uv run tools/check_all.py                 # the gate: everything, the full suite at the default -n
  uv run tools/check_all.py -n 4            # pytest-xdist workers (0 = serial; never `auto` on a shared box)
  uv run tools/check_all.py --tier fast     # the suite's fast tier only (-m "not slow"): NOT the gate, it says so
  uv run --no-sync tools/check_all.py --no-sync   # every leg `uv run --no-sync` (a venv synced with extra groups)

Tiers (docs/SETUP.md §7): `slow` is the one tier marker; the fast tier takes ~1-3 minutes, the full suite ~6-20 (-n 6 .. -n 2).
``-n`` defaults to min(6, MemAvailable / 1.5 GB), at least 2 (``$STACK_TEST_WORKERS`` overrides the default).

The ddl_smoke and migrate --test legs, and the Postgres test substrate (STACK_TEST_DB_URL /
py-pglite), retired with the DB at T1.8 (v4 D40); the suite needs no database.

TYPE-COVERAGE CAVEAT — read before quoting this gate as "mypy strict clean" (2026-08-10):

The mypy leg checks **exactly one file**, `spec/types/interfaces.py`. Passing an explicit path on
the command line OVERRIDES pyproject's `files = ["stack"]`, so the engine is not in the gate at
all — and `workflows/` and `tools/`, where most of the code now lives, are outside even the
pyproject setting. Measured on the `dan/oa-pdf-waterfall` branch (v3; the first and last paths below are
now gone): of ~11k added lines, the largest
units (`workflows/fetch/http/main.py` +743, `workflows/collect/semantic_scholar/main.py` +782,
`workflows/annotate/affiliation/extract.py` +724, `tools/build_affiliation_reference.py` +718) are
all unchecked. "mypy strict clean" is therefore true and close to vacuous — say what was checked.

Two facts for whoever closes this:
- `stack/` does NOT pass strict: `mypy stack` under 3.12 gave "Found 61 errors in 4 files" at the v4 M1
  gate and "Found 81 errors in 16 files (checked 122 source files)" on 2026-10-13 (this note said
  in v3, 2026-08-10, that it passed). Run it under a real 3.12 (`uv run --python 3.12 ...`). On
  3.13 mypy reports spurious `pathlib` import-not-found against `python_version = "3.12"`.
  Widening the gate should pin the interpreter at the same time or it will look broken.
- `workflows/` cannot simply be added to `files`: the tree has many same-named `main.py` modules
  with no `__init__.py`, so mypy aborts with a duplicate-module error. It needs
  `--explicit-package-bases`/`MYPYPATH` (or per-workflow invocation), which is why this is a
  follow-up and not a one-line change here.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

#: Default pytest-xdist workers cap: 6 (8-core 16 GB box shared with agents: full suite 1215 s at -n 2,
#: ~470 s at -n 4, ~325-345 s at -n 6, no gain at -n 8; ~1 GB per worker). Never `auto` (A11: it starves builds).
DEFAULT_WORKERS = 6
#: Memory budget per worker (GB) for the memory-aware default, and the floor of that default.
GB_PER_WORKER = 1.5
MIN_WORKERS = 2


def default_workers() -> int:
    """``DEFAULT_WORKERS`` capped by ``MemAvailable / GB_PER_WORKER`` (floor ``MIN_WORKERS``); the cap if unreadable."""
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return max(MIN_WORKERS, min(DEFAULT_WORKERS, int(int(line.split()[1]) / 1e6 / GB_PER_WORKER)))
    except (OSError, ValueError):
        pass
    return DEFAULT_WORKERS
#: The pytest marker expression of each tier (pyproject `markers`); None = no filter.
TIERS: dict[str, str | None] = {"full": None, "fast": "not slow"}


def pytest_cmd(tier: str, workers: int) -> list[str]:
    """The suite leg's command for ``tier`` at ``workers`` (0 = serial)."""
    cmd = ["uv", "run", "pytest", "tests/", "-q", "-p", "no:cacheprovider"]
    if TIERS[tier] is not None:
        cmd += ["-m", TIERS[tier]]
    return cmd + (["-n", str(workers)] if workers > 0 else [])


# (name, command, retries). No leg retries since the embedded-Postgres legs retired (T1.8).
CHECKS: list[tuple[str, list[str], int]] = [
    ("check_configs", ["uv", "run", "tools/check_configs.py"], 0),
    # KNOWN-NARROW (see the coverage note in the module docstring): this leg passes an explicit
    # path, which OVERRIDES pyproject's `files = ["stack"]`. It checks one file, not the engine.
    ("mypy", ["uv", "run", "--with", "mypy", "mypy", "spec/types/interfaces.py"], 0),
    # The suite itself. It was NOT in this gate until now, which meant the one thing that
    # actually exercises the engine end-to-end was the one thing nobody's commit had to pass
    # (WEAKSPOTS W5: "every 'tested'/'passing' claim in this document has no automated
    # re-verification"). The suite is DB-free since T1.8, so workers share nothing; -n 6 by default (memory-capped),
    # never auto (A11: auto starves an e2e or a build running on the same machine).
    ("pytest", pytest_cmd("full", DEFAULT_WORKERS), 0),
]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tools/check_all.py", description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("--tier", choices=sorted(TIERS), default="full",
                    help="The suite's tier: full (default, the gate) or fast (-m 'not slow', not the gate).")
    env_workers = os.environ.get("STACK_TEST_WORKERS")
    ap.add_argument("--no-sync", action="store_true",
                    help="Run every leg with `uv run --no-sync` (keep the venv as it is, e.g. synced with extra groups).")
    ap.add_argument("-n", dest="workers", type=int, default=int(env_workers) if env_workers else default_workers(),
                    help=f"pytest-xdist workers (default min({DEFAULT_WORKERS}, MemAvailable/{GB_PER_WORKER} GB), at least {MIN_WORKERS}; "
                         "or $STACK_TEST_WORKERS; 0 = serial).")
    a = ap.parse_args(argv)
    if a.workers < 0:
        ap.error("-n must be >= 0")
    checks = [(n, pytest_cmd(a.tier, a.workers) if n == "pytest" else c, r) for n, c, r in CHECKS]
    if a.no_sync:
        checks = [(n, [*c[:2], "--no-sync", *c[2:]], r) for n, c, r in checks]
    failed: list[str] = []
    # Children must not inherit this runner's own ephemeral uv env (uv would warn and ignore
    # it, but a stale VIRTUAL_ENV is a classic source of env drift once a root project exists).
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    for name, cmd, retries in checks:
        print(f"\n=== {name}: {' '.join(cmd)} ===", flush=True)
        rc = subprocess.run(cmd, cwd=REPO, env=env).returncode
        for _ in range(retries):
            if rc == 0:
                break
            print(f"--- {name}: nonzero exit; retrying once ---",
                  flush=True)
            rc = subprocess.run(cmd, cwd=REPO, env=env).returncode
        if rc != 0:
            failed.append(name)
    print("\n" + "=" * 68)
    tests = "tests" if a.tier == "full" else "tests: FAST TIER ONLY, not the gate"
    if failed:
        print(f"check_all: FAILED ({', '.join(failed)}; {tests})")
        return 1
    print(f"check_all: ALL GREEN (configs + payloads + mypy + {tests})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
