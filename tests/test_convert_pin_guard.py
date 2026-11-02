"""The convert pin guard (R4-1; ``workflows/acquire/convert.py`` "The pin guard"): ``acquire.run``
refuses, before any substage and as ``failed[environment]``, when a tool of the plan's convert
config is absent in this venv or the computed pin does not start with ``acquire.convert.expect_pin``;
``options.allow_degraded_convert: true`` lifts the first refusal only. The environment is faked by
replacing :func:`~workflows.acquire.convert.tool_versions` (the pin's tool versions), so these
cases run the same in any venv. Runner cases use ``--only`` and trap lanes: zero network.
:func:`workflows.acquire.main.preflight` finds the refusal without a run (the build-start side is in
``tests/test_build_overrides.py``).
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from stack.plan import Plan, PlanError, load_plan
from stack.runs import RUN_ERROR_CLASSES
from stack.stage_runner import EnvironmentRefused, StageContext, StageError, run_stage
from stack.store.blobs import open_blobs
from stack.store.chain import head_of, read_view, resolve
from tests.fakes.lanes import TrapLane, TrapTransport
from tests.synth.records import sdir
from tests.synth.rows import AT, blob, document_row
from tests.synth.world import write_on_head
from workflows.acquire import convert as convert_mod
from workflows.acquire import lanes
from workflows.acquire.convert import CONFIGS, guard_pin, pin_of
from workflows.acquire.lanes import FETCH_LANES
from workflows.acquire.main import preflight

Row = dict[str, Any]
NOW = AT + dt.timedelta(hours=30)
BODY = "A genuine paragraph of page text, long enough to be complete. " * 30
TRAPS = {name: TrapLane(name) for name in FETCH_LANES}
FULL = "9.9.9+test"  # a fake version for every tool: the "complete" venv


def fake_tools(monkeypatch: pytest.MonkeyPatch, *absent: str) -> None:
    """Every config tool at :data:`FULL`, except ``absent``."""
    monkeypatch.setattr(convert_mod, "tool_versions",
                        lambda cfg: {t: "absent" if t in absent else FULL for t in cfg.tools})


def plan_of(convert: dict[str, Any] | None = None, **options: Any) -> Plan:
    acquire: dict[str, Any] = {"wants": {"arxiv": ["full_text"]}}
    if convert is not None:
        acquire["convert"] = convert
    return Plan.model_validate({"plan": "test-pin-guard", "schema_version": 2, "acquire": acquire,
                                "stages": [{"stage": "acquire", "options": options}]})


def store_with_text(tmp_path: Path, pin: str) -> Path:
    """One acquired row whose raw snapshot was converted ``ok`` at ``pin``."""
    root = tmp_path / "store"
    blobs = open_blobs(root)
    row = document_row(1, state="acquired")
    (snap,) = row["content"]["snapshots"]
    raw = b"Raw. " + BODY.encode()
    snap.update(raw=blob(blobs.put(raw), len(raw), "text/plain"),
                text=blob(blobs.put(BODY.encode()), len(BODY.encode()), "text/markdown"), text_status="ok",
                text_error_class=None, convert_attempts=1, convert_pin=pin, quality=None)
    write_on_head(root, "documents", "documents", [row], stage="gather", run=1, kind="full",
                  blocks_written=["identity", "metadata", "inband", "content"])
    return root


@contextmanager
def spied(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Records every substage entered and every view read through the runner's context."""
    seen: list[str] = []
    substage, view = StageContext.substage, StageContext.view

    @contextmanager
    def sub(self: StageContext, name: str) -> Iterator[Any]:
        seen.append(f"substage:{name}")
        with substage(self, name) as s:
            yield s

    def look(self: StageContext, key: str) -> Any:
        seen.append(f"view:{key}")
        return view(self, key)

    monkeypatch.setattr(StageContext, "substage", sub)
    monkeypatch.setattr(StageContext, "view", look)
    yield seen


def go(root: Path, plan: Plan, only: list[str] | None) -> Any:
    with lanes.using(TRAPS, now=NOW, transport=TrapTransport()):
        return run_stage(root, plan, "acquire", [sdir("documents")], series="main", only=only)


def text_status(root: Path) -> str:
    (row,) = read_view(resolve(root, sdir("documents"))).to_pylist()
    return str(row["content"]["current_text_status"])


# ---------------------------------------------------------------- absent tools
@pytest.mark.parametrize("only", [None, ["point"], ["quality"], ["convert"], ["fetch"]])
def test_absent_tool_refuses_before_any_substage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                 only: list[str] | None) -> None:
    fake_tools(monkeypatch, "pymupdf4llm", "trafilatura")
    cfg = CONFIGS["pymupdf4llm-v1"]
    root = store_with_text(tmp_path, "0" * 64)
    head = head_of(root, "data/documents/main")
    with spied(monkeypatch) as seen:
        rec = go(root, plan_of({"config": cfg.name}), only)
    assert (rec.status, rec.error_class, rec.outputs) == ("failed", "environment", {})
    assert seen == [] and not rec.substages
    for part in ("'pymupdf4llm-v1'", "['pymupdf4llm', 'trafilatura']", pin_of(cfg), "uv sync --group convert-pdf",
                 "allow_degraded_convert"):
        assert part in rec.error, (part, rec.error)
    assert "Traceback" not in rec.error and "\n" not in rec.error  # the build prints the last line
    assert head_of(root, "data/documents/main") == head


def test_the_remedy_names_the_config_group(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_tools(monkeypatch, "docling")
    with pytest.raises(EnvironmentRefused, match=r"'docling-v2' needs \['docling'\].*uv sync --group convert`"):
        guard_pin(plan_of({"config": "docling-v2"}).acquire, {}, {})
    fake_tools(monkeypatch, "pymupdf")
    with pytest.raises(EnvironmentRefused, match="uv sync --group convert-pdf`"):
        guard_pin(plan_of({"config": "pymupdf4llm-v1"}).acquire, {}, {})


def test_allow_degraded_convert_proceeds_and_is_counted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The hatch runs at the degraded pin: the stored text (another pin) reads ``convert_pending``,
    which is exactly what the guard otherwise prevents."""
    fake_tools(monkeypatch, "trafilatura")
    root = store_with_text(tmp_path, "0" * 64)
    with spied(monkeypatch) as seen:
        rec = go(root, plan_of({"config": "pymupdf4llm-v1"}, allow_degraded_convert=True), ["point"])
    assert rec.status == "ok", rec.error
    assert rec.counts["convert.degraded_allowed"] == 1
    assert "substage:point" in seen
    assert text_status(root) == "convert_pending"


def test_allow_degraded_convert_must_be_a_bool(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_tools(monkeypatch)
    with pytest.raises(StageError, match="allow_degraded_convert must be true or false"):
        guard_pin(plan_of({"config": "pymupdf4llm-v1"}).acquire, {"allow_degraded_convert": "yes"}, {})


# ---------------------------------------------------------------- expect_pin
def test_expect_pin_match_proceeds_and_keeps_the_texts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_tools(monkeypatch)
    pin = pin_of(CONFIGS["pymupdf4llm-v1"])
    root = store_with_text(tmp_path, pin)
    assert text_status(root) == "ok"
    counts: dict[str, Any] = {}
    assert guard_pin(plan_of({"config": "pymupdf4llm-v1", "expect_pin": pin[:8]}).acquire, {}, counts) == pin
    assert counts == {}
    rec = go(root, plan_of({"config": "pymupdf4llm-v1", "expect_pin": pin[:12]}), ["point"])
    assert rec.status == "ok", rec.error
    assert "convert.degraded_allowed" not in rec.counts
    assert text_status(root) == "ok"


def test_expect_pin_mismatch_refuses_with_the_pin_document(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_tools(monkeypatch)
    cfg = CONFIGS["pymupdf4llm-v1"]
    pin = pin_of(cfg)
    other = "0123abcd" if not pin.startswith("0123abcd") else "fedc9876"
    root = store_with_text(tmp_path, pin)
    with spied(monkeypatch) as seen:
        rec = go(root, plan_of({"config": cfg.name, "expect_pin": other}), ["point"])
    assert (rec.status, rec.error_class) == ("failed", "environment") and seen == []
    doc = json.dumps({"config": cfg.name, "mem_gb": cfg.mem_gb, "tier": cfg.tier,
                      "tools": {t: FULL for t in cfg.tools}}, sort_keys=True)
    for part in (pin, other, doc):
        assert part in rec.error, (part, rec.error)
    assert str(tmp_path) not in rec.error
    # allow_degraded_convert lifts the absent-tool refusal only, never an expect_pin mismatch
    with pytest.raises(EnvironmentRefused, match="expect_pin"):
        guard_pin(plan_of({"config": cfg.name, "expect_pin": other}).acquire, {"allow_degraded_convert": True}, {})


@pytest.mark.parametrize("bad", ["0142cc9", "0142CC97", "0142cc9g", "", "0" * 65])
def test_expect_pin_is_a_lowercase_hex_prefix(bad: str) -> None:
    with pytest.raises(ValueError, match="expect_pin"):
        plan_of({"config": "pymupdf4llm-v1", "expect_pin": bad})


def test_expect_pin_is_in_no_digest(tmp_path: Path) -> None:
    """A guard, not configuration: the plan digest is the same with and without it (and the
    acquire run's ``config_digest`` is its stage line's, which never held the acquire block)."""
    a, b = plan_of({"config": "pymupdf4llm-v1"}), plan_of({"config": "pymupdf4llm-v1", "expect_pin": "0142cc97"})
    assert b.acquire.convert is not None and b.acquire.convert.expect_pin == "0142cc97"
    assert a.plan_digest == b.plan_digest and "expect_pin" not in json.dumps(b.to_json())
    assert a.stage_line("acquire").digest() == b.stage_line("acquire").digest()  # type: ignore[union-attr]
    p = tmp_path / "p.yaml"
    p.write_text("plan: p\nschema_version: 2\nacquire: {convert: {config: pymupdf4llm-v1, expect_pin: 0142cc97}}\n")
    assert load_plan(p).acquire.convert.expect_pin == "0142cc97"  # type: ignore[union-attr]
    p.write_text("plan: p\nschema_version: 2\nacquire: {convert: {config: pymupdf4llm-v1, expect_pin: 0142}}\n")
    with pytest.raises(PlanError, match="expect_pin"):
        load_plan(p)


# ---------------------------------------------------------------- no convert config
def test_a_plan_without_a_convert_config_is_unaffected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_tools(monkeypatch, *CONFIGS["docling-v2"].tools)  # nothing installed at all
    assert guard_pin(plan_of().acquire, {}, {}) is None
    root = store_with_text(tmp_path, "0" * 64)
    rec = go(root, plan_of(), ["point"])
    assert rec.status == "ok", rec.error
    assert "convert.degraded_allowed" not in rec.counts


def test_environment_is_a_run_error_class() -> None:
    assert "environment" in RUN_ERROR_CLASSES
    assert issubclass(EnvironmentRefused, StageError)


# ---------------------------------------------------------------- the build-start preflight (REH F1)
def test_preflight_has_the_pin_guard_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A tool absent: one line, without a run (cheap: no rows, no network)."""
    fake_tools(monkeypatch, "pymupdf4llm")
    root = tmp_path / "store"
    plan = plan_of({"config": "pymupdf4llm-v1"})
    msgs = preflight(plan, plan.stages[0], root)
    assert len(msgs) == 1 and "needs ['pymupdf4llm']" in msgs[0]
    ok = plan_of({"config": "pymupdf4llm-v1"}, allow_degraded_convert=True)
    assert preflight(ok, ok.stages[0], root) == []
