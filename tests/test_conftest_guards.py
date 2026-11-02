"""Guards on ``tests/conftest.py`` itself: what the suite's session defaults promise every test.

The no-network default (D160 (C)): with a key in the environment and no hook installed, the LLM
path gets ``conftest.py``'s session fake, never the production transport. Moved here from
``test_triage_questions.py``: it tests the conftest, not triage.
"""

from __future__ import annotations

import socket
from pathlib import Path
from typing import Any

import pytest

from stack import gate
from stack.annotate import stage as annotate_stage
from tests.synth.records import run_line
from tests.test_gather_identity import Store
from tests.test_triage_questions import GATE, plan_with
from tests.test_triage_rules import paper


def test_default_test_path_makes_no_network_attempt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """D160 (C): with a key in the environment, a question gate without any hook (no asker hook,
    no transport hook) gets ``tests/conftest.py``'s session fake, which has no rules: the run fails
    loudly on the first call and no socket is ever opened. Without the conftest default the
    production transport would resolve and connect (the guard below turns that red)."""
    attempts: list[Any] = []

    def guard(*args: Any, **_kw: Any) -> Any:
        attempts.append(args[:2])
        raise OSError("socket guard: a test tried to reach the network")

    for obj, name in ((socket, "getaddrinfo"), (socket, "create_connection"),
                      (socket.socket, "connect"), (socket.socket, "connect_ex")):
        monkeypatch.setattr(obj, name, guard)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-dummy-test-value-not-a-key")
    monkeypatch.setattr(gate, "ASKER_HOOK", None)
    monkeypatch.setattr(annotate_stage, "TRANSPORT_HOOK", None)
    monkeypatch.setattr(annotate_stage, "SLEEP", lambda _s: None)
    store = Store(tmp_path / "store")
    store.add("sources.arxiv_sweep", [paper(1)])
    store.gather()
    plan, _ = plan_with(tmp_path, "g", GATE)
    rec = run_line(store.root, plan, "triage")
    assert rec is not None and attempts == []
    assert rec.status == "failed" and "no rule for model" in (rec.error or ""), rec.error
