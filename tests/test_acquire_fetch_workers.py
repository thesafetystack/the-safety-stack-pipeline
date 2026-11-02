"""Acquire ``options.fetch_workers`` (T-RUN B6; ``workflows/acquire/main.py`` "Fetch across hosts"):
lane calls grouped by host (:func:`workflows.acquire.lanes.fetch_group`), at most one call per
host in flight, different hosts at once, outcomes applied in row order in the calling thread (the
same rows, snapshot ids and counts as one worker for the same outcomes); halts, ``max_rows``,
``max_minutes``; the thread-safety of the pieces the
workers share (:class:`stack.http.HostPacer`, :class:`stack.http.PoliteFetcher`,
:class:`~workflows.acquire.lanes.LaneEnv`). Fake lanes and transports only; no network.
"""

from __future__ import annotations

import datetime as dt
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from stack.http import HostPacer, HttpResponse, PoliteFetcher, RobotsCache
from stack.stage_runner import StageError
from tests.fakes.lanes import FakeLane
from tests.synth.rows import sid
from tests.test_acquire_core import doc, go, role
from workflows.acquire import lanes
from workflows.acquire.lanes import (
    FetchOutcome,
    FetchRequest,
    LaneEnv,
    fetch_group,
    host_group,
)
from workflows.acquire.lanes.arxiv import LANE as ARXIV_LANE
from workflows.acquire.lanes.http import LANE as HTTP_LANE
from workflows.acquire.main import acquire_rows, fetch_workers_of

Row = dict[str, Any]
AT_NEWEST = dt.datetime(2026, 10, 1, tzinfo=dt.UTC)
DAY = dt.timedelta(days=1)


@dataclass
class HostLane(FakeLane):
    """A :class:`FakeLane` whose calls take ``delay`` seconds, grouped by ``hosts[source_id]``
    (``host_key``), recording the calls in flight per host and overall. With ``barrier``, the first
    call of each host waits until ``barrier.parties`` hosts are in flight at once (a broken barrier
    is a lane crash, which the tests see as a changed outcome)."""

    hosts: dict[str, str] = field(default_factory=dict)
    delay: float = 0.05
    inflight: dict[str, int] = field(default_factory=dict)
    peak_host: dict[str, int] = field(default_factory=dict)
    peak: int = 0
    order: dict[str, list[str]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    shared: dict[str, int] | None = None  # in flight across several HostLanes
    barrier: threading.Barrier | None = None
    _gated: set[str] = field(default_factory=set)

    def host_key(self, request: FetchRequest, env: LaneEnv) -> str:
        return self.hosts[request.source_id]

    def fetch(self, request: FetchRequest, env: LaneEnv) -> FetchOutcome:
        host = self.hosts[request.source_id]
        total = self.shared if self.shared is not None else {}
        with self._lock:
            self.inflight[host] = self.inflight.get(host, 0) + 1
            self.peak_host[host] = max(self.peak_host.get(host, 0), self.inflight[host])
            total["n"] = total.get("n", 0) + 1
            total["peak"] = max(total.get("peak", 0), total["n"])
            self.peak = max(self.peak, sum(self.inflight.values()))
            self.order.setdefault(host, []).append(request.source_id)
            first = self.barrier is not None and host not in self._gated
            self._gated.add(host)
        try:
            if first:
                assert self.barrier is not None
                self.barrier.wait()
            time.sleep(self.delay)
            with self._lock:
                return super().fetch(request, env)
        finally:
            with self._lock:
                self.inflight[host] -= 1
                total["n"] -= 1


@dataclass
class KeylessLane(HostLane):
    """A :class:`HostLane` without ``host_key`` (a lane of one group)."""

    host_key = None  # type: ignore[assignment]


def web(i: int) -> Row:
    return doc(i, channel="web")


def mixed_world() -> tuple[list[Row], dict[str, FakeLane]]:
    """Ten rows over two lanes and four hosts, with every kind of outcome."""
    rows = [doc(i) if i % 3 == 0 else web(i) for i in range(1, 11)]
    hosts = {sid(i): ("a.test", "b.test", "c.test")[i % 3] for i in range(1, 11)}
    http = HostLane.answering("http", {(sid(2), "page"): "transient", (sid(4), "page"): "dead",
                                       (sid(5), "page"): "crash", (sid(7), "page"): b"<html>7</html>"},
                              default="ok", media_type="text/html")
    http.hosts, http.delay = hosts, 0.02
    arxiv = FakeLane.answering("arxiv", {(sid(6), "full_text"): "transient"}, default=b"%PDF-1.7 x")
    return rows, {"http": http, "arxiv": arxiv}


COUNTS = ("lane_calls", "lane_calls.http", "lane_calls.arxiv", "lane_crashed", "snapshots_minted",
          "outcome.ok", "outcome.failed_transient", "outcome.failed_permanent", "rows_written", "unreached")


def test_parallel_output_equals_sequential(tmp_path: Path) -> None:
    rows, lanes1 = mixed_world()
    res1, e1 = go(rows, tmp_path / "one", lanes1)
    rows4, lanes4 = mixed_world()
    res4, e4 = go(rows4, tmp_path / "four", lanes4, fetch_workers=4)
    assert res4.rows == res1.rows  # same rows, snapshot ids, roles, in the same order
    assert (res4.scope_complete, res4.unreached) == (res1.scope_complete, res1.unreached)
    assert {k: e4.counts.get(k) for k in COUNTS} == {k: e1.counts.get(k) for k in COUNTS}
    assert e1.counts["lane_crashed"] == 1 and e1.counts["lane_calls"] == 10
    assert (e4.counts["fetch.workers"], e4.counts["fetch.groups"]) == (4, 3)  # two web hosts + the arXiv lane
    assert "fetch.workers" not in e1.counts


def test_one_call_per_host_in_flight_hosts_at_once(tmp_path: Path) -> None:
    """Twelve rows over three hosts, eight workers: the three hosts' first calls are in flight at
    once (a three-party barrier, no wall-clock threshold), never two calls to one host at once,
    each host's calls in row order, the same rows as one worker's."""
    rows = [web(i) for i in range(1, 13)]
    hosts = {sid(i): f"h{i % 3}.test" for i in range(1, 13)}

    def lane(barrier: threading.Barrier | None) -> HostLane:
        lane = HostLane.answering("http", {}, default="ok", media_type="text/html")
        lane.hosts, lane.delay, lane.barrier = hosts, 0.01, barrier
        return lane

    one = lane(None)
    res1, _ = go(rows, tmp_path / "one", {"http": one})
    par = lane(threading.Barrier(3, timeout=30))
    res8, e8 = go(rows, tmp_path / "eight", {"http": par}, fetch_workers=8)
    assert "lane_crashed" not in e8.counts  # the barrier held: three hosts in flight together
    assert set(par.peak_host.values()) == {1} and par.peak == 3
    assert one.peak == 1
    for h, got in par.order.items():
        assert got == sorted(got) and len(got) == 4, h
    assert res8.rows == res1.rows and e8.counts["fetch.groups"] == 3


def test_lanes_without_host_key_are_one_group(tmp_path: Path) -> None:
    """A lane with no ``host_key`` is one group: its calls never overlap, whatever the workers."""
    shared: dict[str, int] = {}
    oa = KeylessLane.answering("oa", {}, default=b"%PDF-1.7 o")
    oa.hosts, oa.shared = {sid(i): "lane:oa" for i in range(1, 7)}, shared
    rows = [doc(i, channel="openalex") for i in range(1, 7)]
    for r in rows:
        r["identity"]["members"][0].update(key_kind="doi", key_value=f"10.1000/j.{r['doc_id']}")
    res, e = go(rows, tmp_path, {"oa": oa}, wants={"openalex": ["full_text"]}, fetch_workers=8)
    assert shared["peak"] == 1 and e.counts["fetch.groups"] == 1 and len(res.rows) == 6


def test_halt_under_workers_stops_only_its_lane(tmp_path: Path) -> None:
    """``test_halt_stops_only_its_own_lane`` under four workers: the same rows and counts."""
    def world() -> tuple[list[Row], dict[str, FakeLane]]:
        rows = [doc(1), web(2), doc(3), web(4)]
        arxiv = FakeLane.answering("arxiv", {(sid(1), "full_text"): ["429", b"pdf 1"]}, default=b"pdf")
        http = HostLane.answering("http", {}, default="ok", media_type="text/html")
        http.hosts = {sid(2): "a.test", sid(4): "b.test"}
        return rows, {"arxiv": arxiv, "http": http}

    rows, l1 = world()
    res1, e1 = go(rows, tmp_path / "one", l1)
    rows, l4 = world()
    res4, e4 = go(rows, tmp_path / "four", l4, fetch_workers=4)
    assert l4["arxiv"].keys() == [(sid(1), "full_text")] and len(l4["http"].calls) == 2
    assert res4.rows == res1.rows and not res4.scope_complete and res4.unreached == []
    keys = ("halted", "halted.arxiv", "stopped.halted", "halted_roles", "lane_calls.http")
    assert {k: e4.counts.get(k) for k in keys} == {k: e1.counts.get(k) for k in keys} == dict(zip(keys, (1, 1, 1, 1, 2)))
    assert role(next(r for r in res4.rows if r["doc_id"] == sid(3)), sid(3))["error_class"] == "halted"


@pytest.mark.parametrize("workers", [1, 8])
def test_halted_rows_do_not_hold_the_row_quota(tmp_path: Path, workers: int) -> None:
    """D412: four newest OpenAlex papers whose OA lane halts on its first
    call, then six older arXiv papers, ``max_rows`` 5, ``newest_first``, three runs. A row whose only
    lane halted (beyond the halting call itself) does not count toward ``max_rows``, so the slots refill with arXiv rows and the six
    are fetched over the runs, with one worker and with eight alike (before D412, under workers the
    halted OA rows were admitted first in every run and held their slots: 1 + 1 + 1 arXiv calls)."""
    from tests.test_acquire_core import merge

    rows = [doc(i, channel="openalex") for i in range(1, 5)] + [doc(i) for i in range(5, 11)]
    for n, row in enumerate(rows):
        member = row["identity"]["members"][0]
        if member["channel"] == "openalex":
            member.update(key_kind="doi", key_value=f"10.1000/j.{row['doc_id']}")
        row["metadata"]["published_at"] = AT_NEWEST - n * DAY
    oa = FakeLane.answering("oa", {}, default="429")
    arxiv = FakeLane.answering("arxiv", {}, default=b"%PDF-1.7 a")
    calls = []
    for run in (1, 2, 3):
        oa.calls.clear()
        arxiv.calls.clear()
        res, e = go(rows, tmp_path / f"r{run}", {"oa": oa, "arxiv": arxiv},
                    wants={"openalex": ["full_text"], "arxiv": ["full_text"]}, max_rows=5,
                    fetch_workers=workers, order="newest_first", run=50 + run)
        rows = merge(rows, res)
        calls.append(len(arxiv.calls))
        assert e.counts["halted.oa"] == 1
    assert calls == [4, 2, 0], calls  # run 1: the halting OA call takes one slot, as in the sequential loop


def test_max_rows_admits_the_first_rows(tmp_path: Path) -> None:
    http = HostLane.answering("http", {}, default="ok", media_type="text/html")
    http.hosts = {sid(i): f"h{i}.test" for i in range(1, 6)}
    res, e = go([web(i) for i in range(1, 6)], tmp_path, {"http": http}, max_rows=2, fetch_workers=4)
    assert [r["doc_id"] for r in res.rows] == [sid(1), sid(2)] and res.unreached == [sid(3), sid(4), sid(5)]
    assert not res.scope_complete and e.counts["stopped.limit"] == 1 and len(http.calls) == 2


class _Clock:
    """A fake ``time`` for ``workflows.acquire.main``: ``monotonic()`` reads ``now``, which the
    lane advances by a minute per call (the stage's wall clock, deterministic under load)."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now


def test_max_minutes_stops_starting_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One host, each call a (fake) minute, a 2.5-minute budget: calls start at 0, 1 and 2
    minutes, the fourth would start at 3 and does not; the three are written, the rest are
    unreached; the scope is incomplete."""
    import workflows.acquire.main as acquire_main

    clock = _Clock()
    monkeypatch.setattr(acquire_main, "time", clock)

    @dataclass
    class TickLane(HostLane):
        def fetch(self, request: FetchRequest, env: LaneEnv) -> FetchOutcome:
            out = super().fetch(request, env)
            clock.now += 60.0
            return out

    http = TickLane.answering("http", {}, default="ok", media_type="text/html")
    http.hosts, http.delay = {sid(i): "slow.test" for i in range(1, 9)}, 0.0
    res, e = go([web(i) for i in range(1, 9)], tmp_path, {"http": http}, max_minutes=2.5, fetch_workers=4)
    assert len(http.calls) == 3
    assert [r["doc_id"] for r in res.rows] == [sid(1), sid(2), sid(3)]
    assert res.unreached == [sid(i) for i in range(4, 9)]
    assert e.counts["stopped.budget"] == 1 and not res.scope_complete


@pytest.mark.parametrize("bad", [0, 33, "4", True, 2.5])
def test_bad_fetch_workers_are_refused(bad: Any) -> None:
    with pytest.raises(StageError, match="fetch_workers"):
        fetch_workers_of({"fetch_workers": bad})
    assert fetch_workers_of({}) == 1 and fetch_workers_of({"fetch_workers": 32}) == 32


def test_fetch_groups_of_the_real_lanes() -> None:
    def req(lane: str, member: dict[str, Any]) -> FetchRequest:
        return FetchRequest(doc_id="d", source_id="s", content_role="page", channel="web", channel_group="url",
                            member=member, role=None, now=None, lane=lane)  # type: ignore[arg-type]

    e = LaneEnv(run_id="r", now=None, blobs=None)  # type: ignore[arg-type]
    page = {"canonical_url": "https://Blog.Example.org/post/1", "key_kind": "url", "key_value": "x"}
    assert fetch_group(HTTP_LANE, req("http", page), e) == "blog.example.org"
    on_arxiv = {"canonical_url": "https://export.arxiv.org/abs/2401.1", "key_kind": "url", "key_value": "x"}
    assert fetch_group(HTTP_LANE, req("http", on_arxiv), e) == "arxiv.org"
    assert fetch_group(ARXIV_LANE, req("arxiv", {}), e) == "arxiv.org"
    assert fetch_group(FakeLane("oa"), req("oa", {}), e) == "lane:oa"
    assert host_group(None, "lane:http") == "lane:http"
    from workflows.acquire.lanes.oa import LANE as OA_LANE
    from workflows.acquire.lanes.oa import slots

    assert fetch_group(OA_LANE, req("oa", {"key_kind": "doi", "key_value": "10.1/a"}), e) == "oa:0"  # one worker
    assert [slots(LaneEnv(run_id="r", now=None, blobs=None, options={"fetch_workers": w}))  # type: ignore[arg-type]
            for w in (1, 2, 3, 8, 32)] == [1, 1, 1, 6, 30]
    e8 = LaneEnv(run_id="r", now=None, blobs=None, options={"fetch_workers": 8})  # type: ignore[arg-type]
    groups = {fetch_group(OA_LANE, req("oa", {"key_kind": "doi", "key_value": f"10.{i}/x"}), e8) for i in range(60)}
    assert groups == {f"oa:{k}" for k in range(6)}
    same = [req("oa", {"key_kind": "doi", "key_value": "10.1/SAME"}), req("oa", {"canonical_url": "https://doi.org/10.1/same"})]
    assert len({fetch_group(OA_LANE, r, e8) for r in same}) == 1  # one DOI, one slot (its negatives in order)


# ---------------------------------------------------------------- shared state, thread-safe
@pytest.fixture
def switchy() -> Iterator[None]:
    """Switch threads as often as the interpreter allows, so a racy read-modify-write shows."""
    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        yield
    finally:
        sys.setswitchinterval(old)


def test_lane_env_count_and_state_are_thread_safe(switchy: None) -> None:
    e = LaneEnv(run_id="r", now=None, blobs=None)  # type: ignore[arg-type]
    made: list[int] = []

    def make() -> object:
        made.append(1)
        time.sleep(0.05)
        return object()

    got: list[object] = []

    def work() -> None:
        got.append(e.state("x", make))
        for _ in range(20_000):
            e.count("n")

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert e.counts["n"] == 160_000 and len(made) == 1 and len({id(g) for g in got}) == 1


def test_pacer_spaces_one_host_across_threads_and_leaves_others_alone(switchy: None) -> None:
    """Eight threads × three requests on one host at 20 rps, under a frozen clock (no real
    waiting): the slots handed out are exactly 0, 50, 100, … ms, one each (the pacer is global per
    host, not per thread); a second host's six slots start at 0 on their own."""
    pacer = HostPacer(monotonic=lambda: 100.0, sleep=lambda _s: None)
    pacer.set_rate("a", 20.0)
    pacer.set_rate("b", 20.0)
    slots: dict[str, list[float]] = {"a": [], "b": []}
    lock = threading.Lock()

    def hit(host: str) -> None:
        for _ in range(3):
            with pacer.hold(host):
                waited = pacer.acquire(host)
                with lock:
                    slots[host].append(round(waited, 6))

    threads = [threading.Thread(target=hit, args=("a" if k < 8 else "b",)) for k in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(slots["a"]) == [round(0.05 * k, 6) for k in range(24)]
    assert sorted(slots["b"]) == [round(0.05 * k, 6) for k in range(6)]


def test_polite_fetcher_keeps_one_request_per_host_in_flight() -> None:
    """Threads sharing one :class:`PoliteFetcher` (as acquire's lanes share the run's session):
    the getter never sees two requests to one host at once, and does see two hosts at once (each
    host's first request waits at a two-party barrier, which breaks if the hosts serialise)."""
    inflight: dict[str, int] = {}
    peak: dict[str, int] = {}
    both = {"peak": 0}
    lock = threading.Lock()
    barrier = threading.Barrier(2, timeout=30)
    first: set[str] = set()

    def get(url: str, **_kw: Any) -> HttpResponse:
        host = url.split("/")[2]
        with lock:
            inflight[host] = inflight.get(host, 0) + 1
            peak[host] = max(peak.get(host, 0), inflight[host])
            both["peak"] = max(both["peak"], sum(inflight.values()))
            wait_here = host not in first
            first.add(host)
        if wait_here:
            barrier.wait()
        time.sleep(0.005)
        with lock:
            inflight[host] -= 1
        return HttpResponse(200, {}, b"x", url)

    pacer = HostPacer(monotonic=time.monotonic, sleep=time.sleep, default_rps=1000.0)
    fetcher = PoliteFetcher(get, pacer=pacer, robots=RobotsCache(get, "ua", 1.0), timeout=1.0,
                            sleep=time.sleep)
    threads = [threading.Thread(target=fetcher.fetch, args=(f"https://{'ab'[k % 2]}.test/{k}",),
                                kwargs={"rate_rps": None, "respect_robots": False}) for k in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak == {"a.test": 1, "b.test": 1} and both["peak"] == 2


# ---------------------------------------------------------------- the OA lane across slots (T-R6 OA)
OA_WANTS = {"openalex": ["full_text", "xml"]}


@dataclass
class SlowWeb:
    """:class:`~tests.fakes.oa_world.OaWorld` behind a transport that takes ``delay`` seconds per
    request and records the requests in flight per host (``peak``) and overall (``peak_all``).
    ``barrier_hosts``: the first request to each waits at ``barrier`` (a broken barrier raises,
    which the lane sees as a crash). ``limited``: DOIs whose Unpaywall request answers 429."""

    world: Any
    delay: float = 0.01
    barrier: threading.Barrier | None = None
    barrier_hosts: frozenset[str] = frozenset()
    limited: frozenset[str] = frozenset()
    inflight: dict[str, int] = field(default_factory=dict)
    peak: dict[str, int] = field(default_factory=dict)
    peak_all: int = 0
    urls: list[str] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _waited: set[str] = field(default_factory=set)

    def get(self, url: str, *, headers: Any, timeout: float, max_bytes: int) -> HttpResponse:
        host = url.split("/")[2]
        with self._lock:
            self.urls.append(url)
            self.inflight[host] = self.inflight.get(host, 0) + 1
            self.peak[host] = max(self.peak.get(host, 0), self.inflight[host])
            self.peak_all = max(self.peak_all, sum(self.inflight.values()))
            wait = host in self.barrier_hosts and host not in self._waited
            self._waited.add(host)
        try:
            if wait:
                assert self.barrier is not None
                self.barrier.wait()
            time.sleep(self.delay)
            if host == "api.unpaywall.org" and any(d in url for d in self.limited):
                return HttpResponse(429, {"retry-after": "60", "content-type": "text/plain"}, b"slow down", url)
            return self.world.get(url, headers=headers, timeout=timeout, max_bytes=max_bytes)
        finally:
            with self._lock:
                self.inflight[host] -= 1

    __call__ = get


class SlowFetchpdf:
    """``fetchpdf`` for the walk route: each retrieval takes ``delay`` seconds (so the slots'
    walks overlap) and yields a JATS and a PDF for every identifier; calls are recorded."""

    def __init__(self, delay: float = 0.01) -> None:
        from tests.fakes.lanes import FakeFetchpdf

        self.delay, self.fake = delay, FakeFetchpdf()

    def loader(self, scratch: str, email: str) -> tuple[Any, Any]:
        chain, session = self.fake.loader(scratch, email)
        session.retrieve = self.retrieve
        return chain, session

    def retrieve(self, raw_identifier: str, **kw: Any) -> Any:
        from types import SimpleNamespace

        time.sleep(self.delay)
        self.fake.calls.append({"raw": raw_identifier, **kw})
        jats = b'<?xml version="1.0"?><article><body><p>' + raw_identifier.encode() + b"</p></body></article>"
        pdf = b"%PDF-1.7\n" + raw_identifier.encode() * 200
        return SimpleNamespace(artifacts=[SimpleNamespace(content=b, url=f"https://pub.test/{raw_identifier}.{x}",
                                                          source="europepmc", http_status=200)
                                          for b, x in ((jats, "xml"), (pdf, "pdf"))], reason="")


def oa_rows(dois: list[str]) -> list[Row]:
    rows = [doc(i, channel="openalex") for i in range(1, len(dois) + 1)]
    for r, d in zip(rows, dois, strict=True):
        r["identity"]["members"][0].update(key_kind="doi", key_value=d)
    return rows


def oa_go(rows: list[Row], tmp_path: Path, transport: Any, load: Any, *, workers: int,
          route: str = "direct") -> tuple[Any, LaneEnv]:
    from types import SimpleNamespace

    from tests.test_acquire_core import env as core_env
    from workflows.acquire.lanes.http import HttpSession
    from workflows.acquire.lanes.oa import OaLane

    e = core_env(tmp_path)
    e.transport, e.mailto, e.sleep = transport, "lane-tests@example.invalid", (lambda s: None)
    e.options = {"fetch_workers": workers, "oa_route": route}
    e.acquire = SimpleNamespace(wants=OA_WANTS)
    e.lane_state["http"] = HttpSession(transport.get, sleep=lambda s: None)  # paced, never waited
    with lanes.using({"oa": OaLane(load=load), "arxiv": FakeLane("arxiv"), "http": FakeLane("http")}):
        res = acquire_rows(rows, e, wants=OA_WANTS, channel_group={"openalex": "paper"}.get, fetch_workers=workers)
    return res, e


def mixed_oa_world() -> tuple[Any, list[str]]:
    """Eighteen members: hits on twelve publisher hosts, misses, DOIs Unpaywall does not know (walked),
    Zenodo and MDPI DOIs; two members share a DOI (one search, its negative reused)."""
    from tests.fakes.oa_world import OaWorld

    world = OaWorld()
    dois = [world.add(f"10.{2000 + i}/hit.{i}", "hit").doi for i in range(12)]
    dois += [world.add("10.1000/miss.1", "miss").doi, world.add("10.3000/unlisted.1", "unlisted").doi,
             world.add("10.5281/zenodo.23000001", "zenodo").doi, world.add("10.3390/su18000001", "mdpi").doi,
             world.add("10.1000/miss.2", "miss").doi, "10.1000/miss.1"]
    return world, dois


OA_COUNTS = ("lane_calls", "lane_calls.oa", "lane_crashed", "snapshots_minted", "outcome.ok",
             "outcome.failed_permanent", "outcome.failed_transient", "rows_written", "unreached", "oa.retrievals",
             "oa.walks", "oa.negative_reused", "oa.no_artifact.full_text", "oa.no_artifact.xml")


def test_oa_slots_overlap_one_request_per_host_same_rows(tmp_path: Path) -> None:
    """The OA lane under eight workers (six slots): calls of different slots are in flight at once
    (the first PDF requests of two members in different slots meet at a two-party barrier, which
    breaks when the lane is one group); no host, Unpaywall included, ever has two requests in
    flight; the rows, snapshot ids and counts are those of one worker."""
    from tests.fakes.lanes import FakeFetchpdf
    from workflows.acquire.lanes.oa import slot_of

    world1, dois = mixed_oa_world()
    res1, e1 = oa_go(oa_rows(dois), tmp_path / "one", SlowWeb(world1, delay=0.0), FakeFetchpdf().loader, workers=1)

    world8, _ = mixed_oa_world()
    probe = oa_rows(dois)
    e = LaneEnv(run_id="r", now=None, blobs=None, options={"fetch_workers": 8})  # type: ignore[arg-type]
    reqs = {d: FetchRequest(doc_id=r["doc_id"], source_id=r["identity"]["members"][0]["source_id"],
                            content_role="full_text", channel="openalex", channel_group="paper",
                            member=r["identity"]["members"][0], role=None, now=None, lane="oa")  # type: ignore[arg-type]
            for d, r in zip(dois, probe, strict=True)}
    a = dois[0]
    b = next(d for d in dois[1:12] if slot_of(reqs[d], e) != slot_of(reqs[a], e))
    hosts = frozenset(world8.papers[d].pdf_url.split("/")[2] for d in (a, b))
    web8 = SlowWeb(world8, barrier=threading.Barrier(2, timeout=10), barrier_hosts=hosts)
    res8, e8 = oa_go(oa_rows(dois), tmp_path / "eight", web8, FakeFetchpdf().loader, workers=8)

    assert "lane_crashed" not in e8.counts  # the barrier held: two slots in flight together
    assert set(web8.peak.values()) == {1} and web8.peak_all >= 2
    assert web8.peak["api.unpaywall.org"] == 1 and sum("unpaywall" in u for u in web8.urls) == 15
    assert res8.rows == res1.rows and res8.scope_complete
    assert {k: e8.counts.get(k) for k in OA_COUNTS} == {k: e1.counts.get(k) for k in OA_COUNTS}
    assert (e1.counts["outcome.ok"], e1.counts["oa.negative_reused"], e1.counts["oa.retrievals"]) == (14, 2, 18)
    assert (e8.counts["fetch.workers"], e8.counts["fetch.groups"]) == (8, len({slot_of(r, e) for r in reqs.values()}))


def test_oa_two_roles_of_a_member_share_one_retrieval_under_slots(tmp_path: Path) -> None:
    """``oa_route: walk``, twelve members, eight workers: one fetchpdf walk per member whatever
    the slots interleave, both roles ``ok`` from it."""
    fp = SlowFetchpdf()
    dois = [f"10.{4000 + i}/walk.{i}" for i in range(12)]
    res, e = oa_go(oa_rows(dois), tmp_path, SlowWeb(None), fp.loader, workers=8, route="walk")
    assert sorted(c["raw"] for c in fp.fake.calls) == sorted(dois)
    assert e.counts["oa.retrievals"] == e.counts["oa.walks"] == 12 and e.counts["lane_calls.oa"] == 24
    assert {(x["content_role"], x["status"]) for r in res.rows for x in r["content"]["roles"]} == {
        ("full_text", "ok"), ("xml", "ok")}


def test_oa_provider_429_halts_the_slots_in_flight_calls_finish(tmp_path: Path) -> None:
    """Unpaywall answering 429 for the first member after the retries: the lane halts (no row
    charged); a member of another slot already in flight (its Zenodo request met the halting
    member's first Unpaywall request at a barrier) finishes and is applied; no later call of the
    halting member's slot is made."""
    from tests.fakes.oa_world import OaWorld
    from workflows.acquire.lanes.oa import slot_of

    world = OaWorld()
    dois = [world.add(f"10.{2000 + i}/hit.{i}", "hit").doi for i in range(20)]
    zen = world.add("10.5281/zenodo.23000009", "zenodo").doi
    e = LaneEnv(run_id="r", now=None, blobs=None, options={"fetch_workers": 8})  # type: ignore[arg-type]

    def slot(d: str) -> int:
        return slot_of(FetchRequest(doc_id="d", source_id="s", content_role="full_text", channel="openalex",
                                    channel_group="paper", member={"key_kind": "doi", "key_value": d}, role=None,
                                    now=None, lane="oa"), e)  # type: ignore[arg-type]

    x = dois[0]
    assert slot(zen) != slot(x)
    same = [d for d in dois[1:] if slot(d) == slot(x)]
    assert same, "the world needs a later member in the halting member's slot"
    web = SlowWeb(world, limited=frozenset({x}), barrier=threading.Barrier(2, timeout=10),
                  barrier_hosts=frozenset({"api.unpaywall.org", "zenodo.org"}))
    from tests.fakes.lanes import FakeFetchpdf

    res, e8 = oa_go(oa_rows([x, zen, *dois[1:]]), tmp_path, web, FakeFetchpdf().loader, workers=8)
    by_doi = {r["identity"]["members"][0]["key_value"]: r for r in res.rows}
    assert "lane_crashed" not in e8.counts and (e8.counts["halted"], e8.counts["halted.oa"]) == (1, 1)
    assert not res.scope_complete
    assert {x["status"] for x in by_doi[zen]["content"]["roles"]} >= {"ok"}  # in flight at the halt: applied
    assert role(by_doi[x], by_doi[x]["identity"]["members"][0]["source_id"])["error_class"] == "halted"
    assert role(by_doi[x], by_doi[x]["identity"]["members"][0]["source_id"])["attempts"] == 0  # not charged
    assert not any(d in u for d in same for u in web.urls)  # the slot admitted nothing after the halt
    for d in same:
        if d in by_doi:
            assert all(r["status"] == "not_attempted" for r in by_doi[d]["content"]["roles"])
