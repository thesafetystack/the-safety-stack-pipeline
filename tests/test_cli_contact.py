"""``stack.cli.seed_contact``: STACK_MAILTO reaches the stages under ``STACK_ENV_FILE`` alone (round 5, T-RUN;
B6 found acquire's oa lane halting ``oa.no_mailto`` with ``STACK_ENV_FILE=.env``)."""

from __future__ import annotations

import os

from stack.cli import seed_contact


def test_seeds_mailto_from_env_file(tmp_path, monkeypatch):
    f = tmp_path / "env"
    f.write_text("OPENROUTER_API_KEY=sk-dummy\nexport STACK_MAILTO='ops@example.org'\n", "utf-8")
    monkeypatch.delenv("STACK_MAILTO", raising=False)
    monkeypatch.setenv("STACK_ENV_FILE", str(f))
    seed_contact()
    assert os.environ["STACK_MAILTO"] == "ops@example.org"
    assert "OPENROUTER_API_KEY" not in os.environ  # keys are never seeded


def test_environment_wins_and_no_file_is_a_noop(tmp_path, monkeypatch):
    f = tmp_path / "env"
    f.write_text("STACK_MAILTO=file@example.org\n", "utf-8")
    monkeypatch.setenv("STACK_MAILTO", "env@example.org")
    monkeypatch.setenv("STACK_ENV_FILE", str(f))
    seed_contact()
    assert os.environ["STACK_MAILTO"] == "env@example.org"
    monkeypatch.delenv("STACK_MAILTO")
    monkeypatch.delenv("STACK_ENV_FILE")
    seed_contact()
    assert "STACK_MAILTO" not in os.environ


def test_missing_env_file_is_a_noop(tmp_path, monkeypatch):
    monkeypatch.delenv("STACK_MAILTO", raising=False)
    monkeypatch.setenv("STACK_ENV_FILE", str(tmp_path / "absent"))
    seed_contact()
    assert "STACK_MAILTO" not in os.environ
