"""Every option a collector reads by name (``.option("x", ...)``) is declared in its module's ``OPTIONS``
(D434's refusal set). Guards the drift a merge produced: D405/D406 added ``announce_lag_days`` and
``karma_lag_days`` while D434 derived the sets from the older code, so a plan setting either was refused."""

from __future__ import annotations

import importlib
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
READ = re.compile(r'\.option\(\s*"([a-z_][a-z0-9_]*)"')
# Collectors that read options through ``.option("name", ...)`` (others read them otherwise: not covered here).
MAINS = sorted(p for p in (ROOT / "workflows" / "collect").glob("*/main.py")
               if "OPTIONS" in p.read_text() and READ.search(p.read_text()))


@pytest.mark.parametrize("path", MAINS, ids=[p.parent.name for p in MAINS])
def test_every_option_a_collector_reads_is_declared(path: pathlib.Path) -> None:
    mod = importlib.import_module(f"workflows.collect.{path.parent.name}.main")
    read = set(READ.findall(path.read_text()))
    assert read <= set(mod.OPTIONS), sorted(read - set(mod.OPTIONS))
