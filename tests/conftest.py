"""Shared fixtures for the stack test suite.

Since T1.8 (v4 D40) the suite needs no database: the Postgres fixtures (``pg_dsn``, ``db``,
``fresh_db``, ``writer``, the py-pglite manager and the live-run guard) retired with ``db/`` and
the DB-bound tests. Their v3 form is not in this repository.
"""

from __future__ import annotations

import os
import pathlib
import sys
from typing import Any

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO / "configs"

# tools/ on sys.path so tests can import the standalone tool scripts.
sys.path.insert(0, str(REPO / "tools"))

# No test touches the network (docs/SETUP.md §7): stop botocore probing the EC2 instance-metadata
# endpoint (169.254.169.254) for a region/credentials; it cost ~2 s per S3-region test in
# test_blobstore and is a network call (T1.8-s finding, D91).
os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")


@pytest.fixture(scope="session")
def config() -> Any:
    from stack.config import load

    return load(CONFIG_DIR)


@pytest.fixture()
def blobs(tmp_path: pathlib.Path) -> Any:
    from stack.store.blobs import open_blobs

    return open_blobs(tmp_path)


@pytest.fixture(autouse=True)
def isolated_provider_state(tmp_path: pathlib.Path, monkeypatch: Any) -> None:
    """Offline HTTP fixtures must never alter the operator's live API cooldowns."""
    monkeypatch.setenv('STACK_PROVIDER_STATE', str(tmp_path / 'providers'))
    monkeypatch.setenv('STACK_METADATA_SPOOL', str(tmp_path / 'spool'))


#: The session's default LLM transport (``stack.llm.env_transport``): a fake with no rules unless
#: a test installs one, so a call nobody scripted fails loudly instead of reaching OpenRouter.
FAKE_TRANSPORT = "tests.fakes.llm:session_default"
#: The opt-in flag for tests that call the real provider (the ``real_llm`` fixture).
REAL_LLM_FLAG = "STACK_TEST_REAL_LLM"
#: What a real call needs from the environment; the default test path removes all of it.
KEY_ENVS = ("OPENROUTER_API_KEY", "STACK_ENV_FILE")
#: Scholarly-provider keys (stack.collect.provider_keys); removed for every test (a test sets a fake).
PROVIDER_KEY_ENVS = ("OPENALEX_API_KEY", "SEMANTIC_SCHOLAR_API_KEY")


@pytest.fixture(autouse=True)
def no_real_llm(request: pytest.FixtureRequest, tmp_path: pathlib.Path, monkeypatch: Any) -> None:
    """No test reaches OpenRouter unless it asks for ``real_llm`` (v4 D160 (C)). Two independent
    layers: ``$STACK_LLM_TRANSPORT`` names the fake (a client built without an explicit transport,
    e.g. a triage question gate without a hook, gets it), and the key and the env-file path are
    removed (the production transport refuses before any request). The state dir
    (:mod:`stack.state`) defaults to a temp dir, so no test reaches ``~/.local/state``; the cost
    ledgers are in memory, per run. A test may still set any of these itself (``monkeypatch``
    restores them)."""
    if "real_llm" in request.fixturenames:
        return
    monkeypatch.setenv("STACK_LLM_TRANSPORT", FAKE_TRANSPORT)
    for name in KEY_ENVS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("STACK_STATE_DIR", str(tmp_path / "state"))
    for name in PROVIDER_KEY_ENVS:  # an exported provider key never reaches a test's fake transport
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def no_cap_env(monkeypatch: Any) -> None:
    """A collector cap/sampling variable exported in the developer's shell (``STACK_*_LIMIT``,
    ``*_FRACTION``, ``*_MAX_QUERIES``) would make every build and collector run refuse to start
    (D553): removed for every test; a test sets one itself."""
    from stack.collect.sampling import is_cap_env

    for name in [k for k in os.environ if is_cap_env(k)]:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def no_host_task_limit(tmp_path: pathlib.Path, monkeypatch: Any) -> None:
    """Stage pools cap their threads at the cgroup's task room (D786, ``stack.stage_runner.thread_room``):
    every test reads an empty cgroup directory (no limit) instead of the machine's, and no
    ``$STACK_POOL_MAX_THREADS``, so a pool's size does not depend on the host, the shell or the other
    xdist workers; a test writes its own ``pids.*`` there or sets the variable."""
    from stack import stage_runner

    monkeypatch.setattr(stage_runner, "CGROUP_ROOT", tmp_path / "cgroup")
    monkeypatch.setattr(stage_runner, "PROC_CGROUP", tmp_path / "cgroup.proc")
    monkeypatch.delenv(stage_runner.POOL_MAX_THREADS_ENV, raising=False)  # nor the shell's pool ceiling


@pytest.fixture(autouse=True)
def fresh_run_ledgers() -> Any:
    """Every test starts and ends with no per-run ledger in this process (run ids such as ``r1``
    repeat across tests; a ledger left behind would carry its total into the next test)."""
    import stack.llm.ledger as ledger_mod

    ledger_mod._RUNS.clear()
    yield
    ledger_mod._RUNS.clear()


@pytest.fixture()
def run_ledgers(monkeypatch: Any) -> Any:
    """Keep every run's in-memory ledgers after the run ends (the runner drops them,
    :func:`stack.llm.ledger.forget_run`); returns ``(run_id, provider='openrouter') -> Ledger``."""
    import stack.llm.ledger as ledger_mod

    monkeypatch.setattr(ledger_mod, "forget_run", lambda _run_id: None)
    return lambda run_id, provider=ledger_mod.LLM_PROVIDER: ledger_mod._RUNS[run_id][provider]


@pytest.fixture()
def real_llm(request: pytest.FixtureRequest, monkeypatch: Any) -> None:
    """Opt in to the production transport: the test must be marked ``slow`` and is skipped unless
    ``$STACK_TEST_REAL_LLM=1``. The key comes from ``$OPENROUTER_API_KEY`` or ``$STACK_ENV_FILE``
    (read by the client, never printed); the spend is in the run's in-memory ledger and record."""
    if request.node.get_closest_marker("slow") is None:
        pytest.fail("a real_llm test must be marked slow")
    if os.environ.get(REAL_LLM_FLAG) != "1":
        pytest.skip(f"real LLM calls need ${REAL_LLM_FLAG}=1")
    monkeypatch.delenv("STACK_LLM_TRANSPORT", raising=False)


@pytest.fixture()
def no_keyword_cuts(monkeypatch: Any) -> None:
    """``stack.config.load`` returns the repo config without any channel's ``keyword_cuts`` (the backfill cuts,
    channels 1.12.0). For harness tests whose fixtures are publisher, no-DOI or non-AI works on concept-group
    queries; the cuts themselves are tested in tests/test_keyword_cuts.py."""
    import dataclasses

    import stack.config as sc

    real = sc.load

    def load(*args: Any, **kwargs: Any) -> Any:
        cfg = real(*args, **kwargs)
        chans = [c.model_copy(update={"keyword_cuts": None}) for c in cfg.channels.channels]
        return dataclasses.replace(cfg, channels=cfg.channels.model_copy(update={"channels": chans}))

    monkeypatch.setattr(sc, "load", load)
