"""The state dir after T-STORE: :func:`stack.state.state_dir` resolves as before (operator tools'
caches), and no run writes there: the cost ledgers are per run and in memory, the spend is in the
run record. ``stack status`` and ``stack run`` say nothing about ledgers.

Every test points ``HOME``, ``XDG_STATE_HOME`` and ``$STACK_STATE_DIR`` at temp paths, so none can
reach ``~/.local/state``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import stack.annotate.stage as annotate_stage
from stack.cli import app
from stack.state import state_dir
from tests.fakes.annotate import AT_RUN, annotate, fold_corpus
from tests.fakes.llm import FakeLlm
from tests.test_screen_tiers import LUNA, RELEVANT_MARK, splan, store_docs, verdict

REPO = Path(__file__).resolve().parents[1]
DEV_PLAN = REPO / "configs" / "plans" / "dev-v2.yaml"


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: Any) -> Path:
    """No state env set, HOME in ``tmp_path``; returns ``tmp_path``."""
    for name in ("STACK_STATE_DIR", "XDG_STATE_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return tmp_path


def test_resolution_order_state_dir_then_xdg_then_home(env: Path, monkeypatch: Any) -> None:
    assert state_dir() == env / "home" / ".local" / "state" / "stack"
    monkeypatch.setenv("XDG_STATE_HOME", str(env / "xdg"))
    assert state_dir() == env / "xdg" / "stack"
    monkeypatch.setenv("STACK_STATE_DIR", str(env / "sd"))
    assert state_dir() == env / "sd"


def _files(*roots: Path) -> list[Path]:
    return [p for r in roots if r.exists() for p in r.rglob("*") if p.is_file()]


def test_a_run_writes_nothing_to_the_state_dir(env: Path, monkeypatch: Any) -> None:
    """An LLM stage run (sync calls, reservations, a settled spend) leaves ``$STACK_STATE_DIR``,
    ``$XDG_STATE_HOME/stack`` and ``~/.local/state`` without a file; its spend is in its record."""
    monkeypatch.setenv("STACK_STATE_DIR", str(env / "sd"))
    monkeypatch.setenv("XDG_STATE_HOME", str(env / "xdg"))
    fake = FakeLlm()
    fake.on(model=LUNA, reply=verdict("relevant", "safety method", 0.002))
    monkeypatch.delenv("STACK_WORKFLOWS_ROOT", raising=False)
    monkeypatch.setenv("STACK_PROGRESS", "0")
    monkeypatch.setattr(annotate_stage, "TRANSPORT_HOOK", lambda: fake)
    monkeypatch.setattr(annotate_stage, "SLEEP", lambda _s: None)
    monkeypatch.setattr(annotate_stage, "CLOCK", lambda: AT_RUN)
    store = env / "store"
    store_docs(store, [f"{RELEVANT_MARK} {i}" for i in range(3)])
    p = splan()
    rec = annotate(store, p, "screen", fold_corpus(store, p))
    assert fake.count() == 3
    assert rec.spend == {"usd_micro": {f"openrouter/{LUNA}": 6000}, "reserved_open_usd_micro": 0, "batch_ids": [],
                         "abandoned_batch_ids": []}
    assert _files(env / "sd", env / "xdg", env / "home") == []


def test_status_and_run_print_no_ledger(env: Path) -> None:
    store = env / "store"
    store.mkdir()
    runner = CliRunner()
    res = runner.invoke(app, ["--store", str(store), "--plan", str(DEV_PLAN), "run", "no_such_stage"])
    assert res.exit_code == 1 and "ledger" not in res.stderr + res.stdout
    res = runner.invoke(app, ["--store", str(store), "status"])
    assert res.exit_code == 0 and "ledger" not in res.stdout
