"""The drain (D570, D571) in the stages that do network or model work; fakes only, no network.

* ``acquire``: drained mid-fetch (one worker and three) it publishes the roles it fetched, skips convert,
  and the next run neither fetches them again nor leaves them unconverted (scope clause ``convert``,
  D403); under ``fetch_workers`` a call still in flight at the end of the bounded wait is abandoned (its
  role still due, its row unreached); convert abandons a child the same way and puts its snapshot back
  uncharged;
* ``triage`` and ``annotate.screen``: drained mid-batch they publish the answered rows, the rest
  ``not_attempted[stopped]``; the next run asks only the rest; a call in flight past the wait is
  abandoned and asked again next run;
* collectors (``collect.fake`` of ``tests/test_collect_base.py``): drained between windows the finished
  windows stay ``ok`` and the next window gets no row; drained between pages the open window gets no
  row (abandoned, uncharged) and the observations made are kept; a collector without ``halts()`` ends the
  same way through the runner (``ctx.on_stopped``).

The tests with a bounded wait use a barrier to have both calls in flight and an event to hold the
abandoned one (no sleeps in the assertions); they were looped 20 times (STATUS).
"""

from __future__ import annotations

import copy
import textwrap
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from stack.build import incomplete_reasons
from stack.llm import LlmRequest
from stack.stage_runner import DrainCheck
from stack.store import job as J
from tests.fakes.http import INSTALLED, FakeHttp
from tests.fakes.lanes import FakeLane
from tests.synth.records import run_line
from tests.synth.rows import sid
from tests.test_acquire_convert import (
    BODY,
    CAP_WANTS,
    env_for,
    fake_child,
    merged,
    pending_row,
    snap_of,
    text_status,
)
from tests.test_collect_base import API, TOY, item, plan, view
from tests.test_triage_questions import GATE, plan_with, run_triage, verdict
from tests.test_triage_workers import fake, world  # noqa: F401 — fixtures
from workflows.acquire import convert as convert_mod
from workflows.acquire import lanes
from workflows.acquire.convert import ConvertOutcome, run_convert
from workflows.acquire.lanes import FetchOutcome, FetchRequest, LaneEnv
from workflows.acquire.main import acquire_rows, channel_group
from tests.synth.rows import document_row

Row = dict[str, Any]


@pytest.fixture(autouse=True)
def no_drain_left() -> Iterator[None]:
    J.clear_drain()
    yield
    J.clear_drain()


@dataclass
class Switch:
    """A drain the test turns on (``on(wait_s)``): a :class:`DrainCheck` for library callers."""

    deadline: float | None = None

    def on(self, wait_s: float = 120.0) -> None:
        if self.deadline is None:
            self.deadline = time.monotonic() + wait_s

    @property
    def check(self) -> DrainCheck:
        return DrainCheck(lambda: self.deadline is not None, lambda: self.deadline)


# ---------------------------------------------------------------- acquire
@dataclass
class DrainingLane(FakeLane):
    """A :class:`FakeLane` that turns the drain on during its ``after``-th call (the call finishes)."""

    switch: Switch = field(default_factory=Switch)
    after: int = 2

    def fetch(self, request: FetchRequest, env: LaneEnv) -> FetchOutcome:
        out = super().fetch(request, env)
        if len(self.calls) == self.after:
            self.switch.on()
        return out


def papers(n: int = 5) -> list[Row]:
    return [document_row(i, state="triaged") for i in range(4, 4 + n)]


def lane_for(rows: list[Row], cls: type[FakeLane] = FakeLane, **kw: Any) -> Any:
    lane = cls.answering("arxiv", {(r["doc_id"], "full_text"): f"{r['doc_id']}|{BODY}".encode() for r in rows},
                         media_type="text/plain")
    for k, v in kw.items():
        setattr(lane, k, v)
    return lane


@pytest.mark.parametrize("workers", [1, 3])
def test_acquire_drained_mid_fetch_publishes_fetched_roles_and_the_next_run_converts_them(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workers: int) -> None:
    monkeypatch.setattr(convert_mod, "convert_one", fake_child)
    rows = papers()
    switch = Switch()
    lane = lane_for(rows, DrainingLane, switch=switch)
    e = env_for(tmp_path)
    with lanes.using({"arxiv": lane}, now=e.now):
        res = acquire_rows(copy.deepcopy(rows), e, wants=CAP_WANTS, channel_group=channel_group,
                           fetch_workers=workers, drain=switch.check)
    assert [c.source_id for c in lane.calls] == [sid(4), sid(5)]
    assert res.unreached == [sid(6), sid(7), sid(8)] and not res.scope_complete
    assert e.counts["stopped.drain"] == 1 and "convert.ok" not in e.counts  # convert did not start
    assert text_status(res) == {sid(4): "convert_pending", sid(5): "convert_pending"}
    assert incomplete_reasons(e.counts) == ["stopped"]

    rows2 = merged(rows, res)
    lane2 = lane_for(rows)
    e2 = env_for(tmp_path)
    with lanes.using({"arxiv": lane2}, now=e2.now):
        res2 = acquire_rows(rows2, e2, wants=CAP_WANTS, channel_group=channel_group, fetch_workers=workers)
    assert [c.source_id for c in lane2.calls] == [sid(6), sid(7), sid(8)]  # 4 and 5 are not fetched again
    assert e2.counts["scope.convert"] == 2 and e2.counts["convert.ok"] == 5  # ... and are converted
    assert text_status(res2) == dict.fromkeys([sid(i) for i in range(4, 9)], "ok") and res2.scope_complete


@dataclass
class HeldLane(FakeLane):
    """Two hosts: the first call of each meets at a barrier (both in flight); ``sid(4)``'s call then
    drains with ``wait_s`` and returns, ``sid(5)``'s holds until ``release`` is set."""

    switch: Switch = field(default_factory=Switch)
    wait_s: float = 0.3
    barrier: threading.Barrier = field(default_factory=lambda: threading.Barrier(2, timeout=10))
    release: threading.Event = field(default_factory=threading.Event)

    def host_key(self, request: FetchRequest, env: LaneEnv) -> str:
        return "a.test" if request.source_id in (sid(4), sid(6)) else "b.test"

    def fetch(self, request: FetchRequest, env: LaneEnv) -> FetchOutcome:
        if request.source_id in (sid(4), sid(5)):
            self.barrier.wait()
        if request.source_id == sid(5):
            self.release.wait(10)
        out = super().fetch(request, env)
        if request.source_id == sid(4):
            self.switch.on(self.wait_s)
        return out


def test_acquire_abandons_a_call_in_flight_past_the_wait(tmp_path: Path) -> None:
    rows = papers(3)
    switch = Switch()
    lane = lane_for(rows, HeldLane, switch=switch)
    e = env_for(tmp_path)
    t0 = time.monotonic()
    try:
        with lanes.using({"arxiv": lane}, now=e.now):
            res = acquire_rows(copy.deepcopy(rows), e, wants=CAP_WANTS, channel_group=channel_group,
                               fetch_workers=2, drain=switch.check)
        took = time.monotonic() - t0
    finally:
        lane.release.set()
    assert took < 5, took  # the held call was not waited for
    assert e.counts["drain.abandoned"] == 1 and e.counts["stopped.drain"] == 1
    assert [r["doc_id"] for r in res.rows] == [sid(4)]  # sid(5) abandoned, sid(6) never started
    assert res.unreached == [sid(5), sid(6)]


class GatedChild:
    """``convert_one``'s stand-in: the first two items meet at a barrier; ``sid(1)``'s then drains and
    converts, ``sid(2)``'s holds until ``release``."""

    def __init__(self, switch: Switch) -> None:
        self.switch = switch
        self.barrier = threading.Barrier(2, timeout=10)
        self.release = threading.Event()

    def __call__(self, raw: bytes, media_type: str | None, **kw: Any) -> ConvertOutcome:
        doc = raw.decode().split("|", 1)[0]
        if doc in (sid(1), sid(2)):
            self.barrier.wait()
        if doc == sid(2):
            self.release.wait(10)
        if doc == sid(1):
            self.switch.on(0.3)
        return ConvertOutcome("ok", markdown=f"Converted {raw.decode()}", converter="fake@1+tier2")


def test_convert_drain_abandons_and_uncharges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    switch = Switch()
    child = GatedChild(switch)
    monkeypatch.setattr(convert_mod, "convert_one", child)
    e = env_for(tmp_path, convert_workers=2)
    rows = [pending_row(e.blobs, i, f"{sid(i)}|{BODY}".encode(), "text/plain") for i in range(1, 5)]
    before = [copy.deepcopy(snap_of(r)) for r in rows]
    try:
        assert run_convert(rows, e, drain=switch.check) is False
    finally:
        child.release.set()
    assert snap_of(rows[0])["text_status"] == "ok"
    assert [snap_of(r) for r in rows[1:]] == before[1:]  # abandoned (2) and never started (3, 4): as they were
    assert (e.counts["convert.ok"], e.counts["convert.abandoned"], e.counts["convert.stopped.drain"]) == (1, 1, 1)


# ---------------------------------------------------------------- triage
def asked(fake: Any, since: int = 0) -> list[int]:  # noqa: F811
    """The paper numbers of the requests since ``since``."""
    import re

    from tests.fakes.llm import prompt_text
    out = []
    for req in fake.requests[since:]:
        m = re.search(r"Title: Paper (\d+)", prompt_text(req))
        out.append(int(m.group(1)) if m else -1)
    return out


def answer_all(fake: Any) -> None:  # noqa: F811
    from tests.test_triage_questions import title
    for i in range(1, 9):
        fake.on(title(i), verdict("relevant", confidence=0.9))


def test_triage_drained_mid_batch_publishes_answers_and_the_next_run_asks_the_rest(
        tmp_path: Path, fake: Any) -> None:  # noqa: F811
    answer_all(fake)
    seen = {"n": 0}

    def hook(_req: LlmRequest) -> None:
        seen["n"] += 1
        if seen["n"] == 3:
            J.request_drain("test")  # the third call is in flight: it finishes and is kept

    fake.hook = hook
    p, _ = plan_with(tmp_path, "t1", GATE, options={"workers": 1})  # sequential (the default is 64 since D692)
    store = world(tmp_path, "s")
    rec = run_triage(store, p)
    first = asked(fake)
    assert len(first) == 3 and rec.scope_complete is False
    assert (rec.counts["ask.asked"], rec.counts["ask.not_attempted.stopped"], rec.counts["stopped.drain"]) == (3, 5, 1)
    assert incomplete_reasons(rec.counts)[0] == "stopped"
    fake.hook = None
    n = len(fake.requests)
    rec2 = run_triage(store, p)
    assert sorted(asked(fake, n)) == sorted(set(range(1, 9)) - set(first))  # only the rest
    assert rec2.scope_complete and "ask.not_attempted.stopped" not in rec2.counts


def test_triage_abandons_a_call_in_flight_past_the_wait(tmp_path: Path, fake: Any) -> None:  # noqa: F811
    answer_all(fake)
    bar = threading.Barrier(2, timeout=10)
    release = threading.Event()
    lock = threading.Lock()
    order: list[int] = []

    def hook(req: LlmRequest) -> None:
        with lock:
            order.append(len(order) + 1)
            mine = order[-1]
        if mine <= 2:
            bar.wait()
        if mine == 1:
            J.request_drain("test", wait_s=0.3)
        elif mine == 2:
            release.wait(10)

    fake.hook = hook
    p, _ = plan_with(tmp_path, "t2", GATE, options={"workers": 2})
    store = world(tmp_path, "s")
    try:
        rec = run_triage(store, p)
    finally:
        release.set()
    assert rec.counts["ask.drain.abandoned"] == 1 and rec.counts["ask.asked"] == 1
    assert rec.counts["ask.not_attempted.stopped"] == 7 and not rec.scope_complete
    fake.hook = None
    n = len(fake.requests)
    rec2 = run_triage(store, p)
    assert rec2.scope_complete and len(asked(fake, n)) + rec2.counts.get("ask.cache_hits", 0) == 7


# ---------------------------------------------------------------- annotate.screen
@pytest.fixture
def ann(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    import stack.annotate.stage as annotate_stage
    from tests.fakes.annotate import STAGES_ROOT
    from tests.fakes.llm import FakeLlm
    f = FakeLlm()
    monkeypatch.setenv("STACK_WORKFLOWS_ROOT", str(STAGES_ROOT))
    monkeypatch.setattr(annotate_stage, "TRANSPORT_HOOK", lambda: f)
    return {"store": tmp_path / "store", "fake": f}


def test_screen_drained_mid_batch_publishes_answers_and_the_next_run_asks_the_rest(ann: dict[str, Any]) -> None:
    from tests.fakes.annotate import annotate, doc_with_text, fold_corpus, rows_of, write_documents
    from tests.fakes.annotate import plan as ann_plan
    from tests.test_annotate import verdicts
    store, f = ann["store"], ann["fake"]
    verdicts(f, 6)
    write_documents(store, [doc_with_text(store, i, f"Body of paper {i}.") for i in range(6)], run=1)
    p = ann_plan()
    corpus = fold_corpus(store, p)

    def hook(_req: LlmRequest) -> None:
        if f.count() == 2:
            J.request_drain("test")

    f.hook = hook
    r1 = annotate(store, p, "screen", corpus)
    assert f.count() == 2 and not r1.scope_complete
    assert (r1.counts["asked"], r1.counts["not_attempted.stopped"], r1.counts["stopped.drain"]) == (2, 4, 1)
    rows = rows_of(store, r1, "screen")
    assert sorted((r["status"], r["error_class"]) for r in rows) == [("not_attempted", "stopped")] * 4 + [("ok", None)] * 2
    f.hook = None
    r2 = annotate(store, p, "screen", corpus, prior=r1)
    assert f.count() == 6 and r2.counts["carried"] == 2 and r2.counts["asked"] == 4 and r2.scope_complete


# ---------------------------------------------------------------- collectors
NO_HALTS = TOY.replace("    with c.halts():\n        for q in", "    if True:\n        for q in")


@pytest.fixture
def cstore(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from stack import stage_runner
    root = tmp_path / "workflows"
    for name, body in (("fake", TOY), ("nohalt", NO_HALTS)):
        (root / "collect" / name).mkdir(parents=True)
        (root / "collect" / name / "main.py").write_text(textwrap.dedent(body))
    monkeypatch.setattr(stage_runner, "WORKFLOWS_ROOT", root)
    return tmp_path / "store"


class DrainAfter:
    """The fake transport, turning the process's drain on after its ``n``-th request."""

    def __init__(self, inner: FakeHttp, n: int) -> None:
        self.inner, self.n, self.calls = inner, n, 0

    def __call__(self, *a: Any, **k: Any) -> Any:
        out = self.inner(*a, **k)
        self.calls += 1
        if self.calls == self.n:
            J.request_drain("test")
        return out


@pytest.fixture
def http() -> Iterator[FakeHttp]:
    f = FakeHttp()
    f.add(API, {"q": "a", "page": 1}, json={"items": [item(1), item(2)], "next": 2})
    f.add(API, {"q": "a", "page": 2}, json={"items": [item(3)], "next": None})
    f.add(API, {"q": "b", "page": 1}, json={"items": [item(4)], "next": None})
    f.add(API, {"q": "c", "page": 1}, json={"items": [item(5)], "next": None})
    yield f
    INSTALLED.pop("fake", None)


def cplan(stage: str = "collect.fake") -> Any:
    p = plan(queries=["a", "b", "c"], to="2026-10-01")
    return p.model_copy(update={"stages": [p.stages[0].model_copy(update={"stage": stage})]})


@pytest.mark.parametrize("stage", ["collect.fake", "collect.nohalt"])
def test_collector_drained_between_windows_keeps_its_finished_windows(
        cstore: Path, http: FakeHttp, stage: str) -> None:
    INSTALLED["fake"] = DrainAfter(http, 2)  # after a's second page: a's window closes, b's never opens
    rec = run_line(cstore, cplan(stage), stage)
    assert (rec.status, rec.scope_complete, rec.counts["stopped.drain"]) == ("ok", False, 1), rec.error
    suffix = stage.split(".", 1)[1]
    cov = {r["query"]: r for r in view(cstore, f"coverage.{suffix}")}
    assert list(cov) == ["a"] and (cov["a"]["status"], cov["a"]["attempts"]) == ("ok", 1)
    assert sorted(r["identity"]["external_id"] for r in view(cstore, f"sources.{suffix}")) == ["F1", "F2", "F3"]
    assert "halted" not in rec.counts and incomplete_reasons(rec.counts) == ["stopped"]
    INSTALLED["fake"] = http
    n = len(http.calls)
    rec2 = run_line(cstore, cplan(stage), stage)
    assert rec2.status == "ok" and rec2.scope_complete, rec2.error
    asked_q = [c.url.split("q=")[1].split("&")[0] for c in http.calls[n:]]
    assert "b" in asked_q and "c" in asked_q
    cov2 = {r["query"]: r for r in view(cstore, f"coverage.{suffix}")}
    assert {q: r["status"] for q, r in cov2.items()} == dict.fromkeys("abc", "ok")


def test_collector_drained_between_pages_abandons_the_open_window(cstore: Path, http: FakeHttp) -> None:
    INSTALLED["fake"] = DrainAfter(http, 1)  # after a's first page: its second is not asked
    rec = run_line(cstore, cplan(), "collect.fake")
    assert (rec.status, rec.scope_complete, rec.counts["stopped.drain"]) == ("ok", False, 1), rec.error
    assert len(http.calls) == 1 and "coverage" not in rec.outputs  # no window row: abandoned, uncharged
    assert sorted(r["identity"]["external_id"] for r in view(cstore, "sources.fake")) == ["F1", "F2"]
