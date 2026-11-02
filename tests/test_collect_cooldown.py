"""A provider's Gate cooldown seen through the collect transport (review finding L3-1, D401; S2's
backoff, D550).

One real 429 without ``Retry-After`` sets the cross-process Gate's cooldown
(``stack.provider_transport``): 60 s, or for S2 a backoff of 5 s doubling to 60 s (D550). Before D401 every retry inside the cooldown came back as a
synthetic 429 that ``Transport.request`` counted as ``http_429`` and answered by halving the
run's pacer: the S2 page failed, the pacer sat at its 0.05 rps floor for the run, and the S2
lookup ended at its first real 429. These tests drive the real chain (``Transport`` ->
``urllib_get``/``urllib_send`` -> ``throttled_open`` -> ``Gate``) over a fake opener on a virtual
clock and a tmp ``STACK_PROVIDER_STATE`` (conftest)."""
from __future__ import annotations

import io
import json
import types
import urllib.error
import urllib.request
from email.message import Message
from typing import Any

import pytest

import stack.http as sh
import stack.provider_transport as pt
from stack.collect.base import Pacing, RunHalted, Transport
from workflows.collect.semantic_scholar import main as s2
from workflows.fetch.metadata import main as lookup

S2_HOST = "api.semanticscholar.org"


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def time(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += max(0.0, s)

    def monotonic(self) -> float:
        return self.t


class Resp:
    def __init__(self, url: str, body: bytes) -> None:
        self.status, self.url, self._b = 200, url, body
        self.headers = Message()
        self.headers["Content-Type"] = "application/json"

    def __enter__(self) -> Resp:
        return self

    def __exit__(self, *a: Any) -> bool:
        return False

    def read(self, n: int = -1) -> bytes:
        return self._b

    def geturl(self) -> str:
        return self.url


class Server:
    """Real requests arrive here: a 429 (S2's shape, no Retry-After unless ``retry_after``) at
    the request indices in ``fail_at`` (or where ``fails(n)`` says), else 200 with ``body``; each
    takes 0.4 virtual seconds."""

    def __init__(self, clock: Clock, fail_at: set[int], body: bytes,
                 retry_after: str | None = None, fails: Any = None) -> None:
        self.clock, self.fail_at, self.body, self.retry_after = clock, fail_at, body, retry_after
        self.fails = fails or (lambda n: n in self.fail_at)
        self.log: list[float] = []

    def open(self, req: Any, timeout: float | None = None, **kw: Any) -> Resp:
        url = req.full_url if hasattr(req, "full_url") else str(req)
        n = len(self.log)
        self.log.append(self.clock.t)
        self.clock.sleep(0.4)
        if self.fails(n):
            h = Message()
            h["Content-Type"] = "application/json"
            if self.retry_after is not None:
                h["Retry-After"] = self.retry_after
            raise urllib.error.HTTPError(url, 429, "Too Many Requests", h,
                                         io.BytesIO(b'{"message": "Too Many Requests"}'))
        return Resp(url, self.body)


class Stub:
    """The collector surface :class:`Transport` uses."""

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.halted: RunHalted | None = None

    def beat(self, **k: Any) -> None:
        pass

    def halt(self, wait: float, reason: str) -> RunHalted:
        self.halted = RunHalted(wait, reason)
        return self.halted


@pytest.fixture()
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    c = Clock()
    monkeypatch.setattr(pt, "time", types.SimpleNamespace(time=c.time, sleep=c.sleep))
    return c


def test_s2_collect_one_real_429_backs_off_5_s(clock: Clock, monkeypatch: pytest.MonkeyPatch) -> None:
    """D550: S2's bare 429 sets a 5 s backoff (was the flat 60 s cooldown); the transport waits it
    out itself and re-sends (``http_429_waits``), so no ladder step and no cooldown echo."""
    srv = Server(clock, fail_at={2}, body=b'{"total": 0, "data": []}')
    monkeypatch.setattr(sh, "_opener", lambda: srv)
    stub = Stub()
    transport = Transport(stub, s2.PACING, sleep=clock.sleep, monotonic=clock.monotonic)
    monkeypatch.setattr(s2, "_T", transport)
    monkeypatch.setattr(s2, "_sleep", clock.sleep)
    failed = []
    for i in range(30):
        stats: dict[str, Any] = {}
        if s2._get_page({"query": f"q{i}", "fields": s2.FIELDS}, {}, stats) is None:
            failed.append((i, stats))
    assert failed == []
    assert stub.counts["http_429"] == 1                 # the one real 429, not its synthetic echoes
    assert stub.counts["http_429_waits"] == 1 and "http_cooldown" not in stub.counts
    assert stub.counts["http_requests"] == len(srv.log) == 31
    # The pacer stays at the Gate's interval (2.5 s), not halved towards the 0.05 rps floor.
    assert transport.pacer._rps(S2_HOST) == pytest.approx(s2.PACING.rps)
    gaps = [b - a for a, b in zip(srv.log, srv.log[1:], strict=False)]
    assert all(2.85 < g < 3.0 for g in gaps[-10:]), gaps  # 0.4 s answer + 2.5 s after it (D444)
    assert 5.3 < gaps[2] < 5.5                           # the re-send waited the 5 s backoff
    assert pt.Gate("https://api.semanticscholar.org/x").read().get("backoff") is None  # reset by the 200


def test_s2_lookup_one_real_429_keeps_the_batch(clock: Clock,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    forms = [f"ARXIV:2510.{i:05d}" for i in range(100)]
    srv = Server(clock, fail_at={2}, body=json.dumps([None] * 100).encode())
    monkeypatch.setattr(urllib.request, "urlopen", srv.open)
    stub = Stub()
    transport = Transport(stub, lookup.S2_PACING, sleep=clock.sleep, monotonic=clock.monotonic)
    monkeypatch.setattr(lookup, "_T", transport)
    errors = [lookup.s2_chunk(forms, auth={"x-api-key": "K"}).error for _ in range(6)]
    assert errors == [None] * 6
    assert stub.halted is None
    assert stub.counts["http_429"] == 1
    # D521: the lookup waits the 429 out itself (the Gate's backoff, 5 s since D550), so the re-send
    # finds the cooldown over; before D521 a 2 s ladder step met the cooldown (`http_cooldown`).
    assert stub.counts["http_429_waits"] == 1
    assert len(srv.log) == 7
    assert transport.pacer._rps(S2_HOST) >= 1 / pt.gate_interval("s2") - 1e-9
    assert 5.3 < srv.log[3] - srv.log[2] < 5.5


@pytest.mark.parametrize("standing", [False, True], ids=["real-429", "standing-cooldown"])
def test_lookup_429_asking_above_halt_threshold_still_halts(
        clock: Clock, monkeypatch: pytest.MonkeyPatch, standing: bool) -> None:
    """A real 429 asking 3600 s halts at once (as before D401); so does a cooldown of that
    length another process left in the Gate (no wait, no request sent)."""
    srv = Server(clock, fail_at=set() if standing else {0}, body=json.dumps([None]).encode(),
                 retry_after="3600")
    monkeypatch.setattr(urllib.request, "urlopen", srv.open)
    if standing:  # D700: the cooldown of the credential the lookup sends (x-api-key K)
        url = "https://api.semanticscholar.org/graph/v1/paper/batch"
        gate = pt.Gate(url, pt.credential_id(url, {"x-api-key": "K"}))
        pt.atomic_json(gate.path, {gate.cooldown_key: clock.t + 3600})
    stub = Stub()
    transport = Transport(stub, lookup.S2_PACING, sleep=clock.sleep, monotonic=clock.monotonic)
    monkeypatch.setattr(lookup, "_T", transport)
    t0 = clock.t
    with pytest.raises(RunHalted):
        lookup.s2_chunk(["ARXIV:2510.00001"], auth={"x-api-key": "K"})
    assert len(srv.log) == (0 if standing else 1)
    assert clock.t - t0 < 5.0
    assert stub.counts.get("http_429", 0) == (0 if standing else 1)


def test_ungated_host_429_still_halves_the_pacer(clock: Clock) -> None:
    """Penalize stays for hosts without a Gate (D401 changes only gate-paced families)."""
    def send(method: str, url: str, **kw: Any) -> sh.HttpResponse:
        return sh.HttpResponse(429, {"retry-after": "1"}, b"", url)

    stub = Stub()
    transport = Transport(stub, Pacing(rps=4.0, retry_delays_s=()), send=send,
                          sleep=clock.sleep, monotonic=clock.monotonic)
    transport.get("https://example.org/x")
    assert stub.counts["http_429"] == 1
    assert transport.pacer._rps("example.org") == pytest.approx(2.0)


@pytest.mark.parametrize("fails", [6, 7], ids=["six-429s-then-ok", "seven-429s-give-up"])
def test_s2_lookup_waits_out_429s_instead_of_the_ladder(
        clock: Clock, monkeypatch: pytest.MonkeyPatch, fails: int) -> None:
    """D521, D550: S2 throttles in spells. A 429 within the halt threshold is waited out (the Gate's
    backoff when S2 names no retry-after: 5, 10, 20, 40, 60, 60 s) and re-sent, at most
    ``BACKOFF_WAITS`` (6) times; the 2 s / 4 s ladder used to fail the batch after three sends. A
    seventh 429 fails the batch (``rate_limit``), no halt."""
    srv = Server(clock, fail_at=set(range(fails)), body=json.dumps([None]).encode())
    monkeypatch.setattr(urllib.request, "urlopen", srv.open)
    stub = Stub()
    transport = Transport(stub, lookup.S2_PACING, sleep=clock.sleep, monotonic=clock.monotonic)
    monkeypatch.setattr(lookup, "_T", transport)
    batch = lookup.s2_chunk(["ARXIV:2510.00001"], auth={"x-api-key": "K"})
    assert stub.halted is None
    assert len(srv.log) == 7                              # the first send and six waits
    assert batch.error == (None if fails == 6 else ("rate_limit", False))
    assert stub.counts["http_429"] == min(fails, 7)
    assert stub.counts["http_429_waits"] == 6
    assert stub.counts.get("http_retries", 0) == 0        # no ladder step spent on a 429
    gaps = [round(b - a - 0.4, 3) for a, b in zip(srv.log, srv.log[1:], strict=False)]
    assert gaps == [5.0, 10.0, 20.0, 40.0, 60.0, 60.0], gaps


def test_s2_backoff_resets_after_a_200_and_is_shared_through_the_gate_file(
        clock: Clock, monkeypatch: pytest.MonkeyPatch) -> None:
    """D550: two bare 429s back off 5 s then 10 s; a 200 resets the step, so the next 429 backs off
    5 s again. The step lives in the Gate's state file, so a Gate object of another process sees
    it (here: a fresh ``Gate`` reading the file between requests)."""
    srv = Server(clock, fail_at={0, 1, 3}, body=json.dumps([None]).encode())
    monkeypatch.setattr(urllib.request, "urlopen", srv.open)
    stub = Stub()
    transport = Transport(stub, lookup.S2_PACING, sleep=clock.sleep, monotonic=clock.monotonic)
    monkeypatch.setattr(lookup, "_T", transport)
    other = pt.Gate("https://api.semanticscholar.org/graph/v1/paper/batch")
    assert lookup.s2_chunk(["ARXIV:2510.00001"], auth={}).error is None   # sends 0 (429), 1 (429), 2
    assert other.read().get("backoff") is None                             # the 200 reset it
    assert lookup.s2_chunk(["ARXIV:2510.00002"], auth={}).error is None   # sends 3 (429), 4
    gaps = [round(b - a - 0.4, 3) for a, b in zip(srv.log, srv.log[1:], strict=False)]
    assert gaps == [5.0, 10.0, 2.5, 5.0], gaps                           # 2.5: the S2 interval


def test_s2_backoff_is_one_family_state_across_gate_objects(clock: Clock) -> None:
    """D550: the backoff step is read and written in the Gate's JSON under its lock, as the cooldown:
    three Gate objects (three processes) each observing one bare 429 double one shared step."""
    url = "https://api.semanticscholar.org/graph/v1/paper/search/bulk"
    got = [pt.Gate(url).observe(429, {}, b"") for _ in range(6)]
    assert got == [5.0, 10.0, 20.0, 40.0, 60.0, 60.0]
    state = pt.Gate(url).read()
    assert state["backoff"] == 60.0 and state["cooldown"] == pytest.approx(clock.t + 60.0)
    assert pt.Gate(url).observe(200, {}) is None and "backoff" not in pt.Gate(url).read()


def test_stated_retry_after_is_kept_and_other_families_still_wait_60_s(clock: Clock) -> None:
    """D550 changes only a bare 429 of a backoff family: a stated ``Retry-After`` is the cooldown
    (and leaves the step alone); OpenAlex's bare 429 is still a 60 s cooldown."""
    s2_url = "https://api.semanticscholar.org/graph/v1/paper/batch"
    assert pt.Gate(s2_url).observe(429, {"Retry-After": "7"}) is None
    assert pt.Gate(s2_url).read()["cooldown"] == pytest.approx(clock.t + 7.0)
    assert "backoff" not in pt.Gate(s2_url).read()
    oa_url = "https://api.openalex.org/works"
    assert pt.Gate(oa_url).observe(429, {}) is None
    assert pt.Gate(oa_url).read()["cooldown"] == pytest.approx(clock.t + 60.0)
    # retry_seconds: the stated wait, else the Gate's backoff header, else 60 s
    assert pt.retry_seconds({"Retry-After": "7", pt.BACKOFF_HEADER: "20"}) == 7.0
    assert pt.retry_seconds({pt.BACKOFF_HEADER: "20"}) == 20.0
    assert pt.retry_seconds({}) == 60.0


def test_s2_collect_halts_on_a_retry_after_above_60_s(clock: Clock, monkeypatch: pytest.MonkeyPatch) -> None:
    """D550: S2 collect now halts on a 429 asking more than 60 s, as s2-lookup does (it used to fail
    the page after its ladder and go on to the next query)."""
    srv = Server(clock, fail_at={0}, body=b'{"data": []}', retry_after="600")
    monkeypatch.setattr(sh, "_opener", lambda: srv)
    stub = Stub()
    transport = Transport(stub, s2.PACING, sleep=clock.sleep, monotonic=clock.monotonic)
    monkeypatch.setattr(s2, "_T", transport)
    with pytest.raises(RunHalted):
        s2._get_page({"query": "q", "fields": s2.FIELDS}, {}, {})
    assert len(srv.log) == 1 and stub.halted is not None


def test_s2_interval_is_the_channel_rate(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """D550: the Gate's S2 interval is ``1 / politeness.rate_rps`` of the ``semantic_scholar``
    channel (2.5 s as shipped); another config directory's rate is used; a channel without a rate
    falls back to the code's interval; other families keep the code's intervals."""
    assert pt.gate_interval("s2") == pytest.approx(2.5)
    assert pt.gate_interval("openalex") == pytest.approx(0.15)
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "channels.yaml").write_text(
        "channels:\n  - {id: semantic_scholar, politeness: {rate_rps: 0.5}}\n")
    monkeypatch.setenv("STACK_CONFIG_DIR", str(cfg))
    assert pt.gate_interval("s2") == pytest.approx(2.0)
    assert pt.Gate("https://api.semanticscholar.org/x").interval() == pytest.approx(2.0)
    bare = tmp_path / "bare"
    bare.mkdir()
    (bare / "channels.yaml").write_text("channels:\n  - {id: semantic_scholar}\n")
    monkeypatch.setenv("STACK_CONFIG_DIR", str(bare))
    monkeypatch.setitem(pt._INTERVALS, "s2", 3.0)
    assert pt.gate_interval("s2") == pytest.approx(3.0)


def test_wait_429_is_off_by_default_and_ladder_unchanged(clock: Clock) -> None:
    """Without ``wait_429`` a 429 takes a ladder step as before (OpenAlex, the collectors)."""
    sent: list[float] = []

    def send(method: str, url: str, **kw: Any) -> sh.HttpResponse:
        sent.append(clock.t)
        return sh.HttpResponse(429, {"retry-after": "30"}, b"", url)

    stub = Stub()
    transport = Transport(stub, Pacing(rps=4.0, retry_delays_s=(2.0, 4.0), halt_429_after_s=60.0),
                          send=send, sleep=clock.sleep, monotonic=clock.monotonic)
    resp = transport.fetch("GET", "https://example.org/x")
    assert resp.status == 429 and len(sent) == 3
    assert stub.counts["http_retries"] == 2 and "http_429_waits" not in stub.counts


@pytest.mark.parametrize("p429", [0.0, 0.25, 0.49])
def test_s2_collect_time_for_62_pages_under_backoff(clock: Clock, monkeypatch: pytest.MonkeyPatch,
                                                     p429: float) -> None:
    """D550's expected S2 collect time (virtual clock): 62 good pages (the e2e year's count: 121
    requests, 59 of them 429) at the 2.5 s interval, S2 answering in 0.4 s and 429ing each send
    independently with ``p429`` (a fixed seed). At D401's flat 60 s per 429 the e2e took 61 min.
    Measured here: 3.0 min clean, 5.7 min at 25 %, 23.6 min at 49 %; at 49 % one page met seven
    429s in a row (0.49^7, ~0.7 % a page) and failed ``rate_limit`` (its query stays due)."""
    import random

    rng = random.Random(20261014)
    srv = Server(clock, fail_at=set(), body=b'{"total": 0, "data": []}',
                 fails=lambda n: rng.random() < p429)
    monkeypatch.setattr(sh, "_opener", lambda: srv)
    stub = Stub()
    transport = Transport(stub, s2.PACING, sleep=clock.sleep, monotonic=clock.monotonic)
    monkeypatch.setattr(s2, "_T", transport)
    monkeypatch.setattr(s2, "_sleep", clock.sleep)
    t0 = clock.t
    failed = [i for i in range(62)
              if s2._get_page({"query": f"q{i}", "fields": s2.FIELDS}, {}, {}) is None]
    minutes = (clock.t - t0) / 60
    print(f"p429={p429}: {len(srv.log)} requests, {stub.counts.get('http_429', 0)} 429s, "
          f"{minutes:.1f} min, failed pages {failed}")
    assert len(failed) == (1 if p429 > 0.4 else 0), failed
    assert minutes < {0.0: 3.1, 0.25: 6.0, 0.49: 25.0}[p429], minutes


# ─────────────────────────────────────────────────────────────────────────────
# D583: OpenAlex's "Plan upgrade required" 429 is a refusal, not a rate limit
# ─────────────────────────────────────────────────────────────────────────────
OA_WORKS = "https://api.openalex.org/works"
#: OpenAlex's answer to `from_created_date` on a keyed plan below Premium (probed 2026-10-15): a 429
#: with no Retry-After and no rate-limit headers.
PLAN_429 = json.dumps({"error": "Plan upgrade required",
                       "message": 'The "from_created_date:2026-10-01" filter requires a Premium, '
                                  "Institutional, or Partner plan. See https://openalex.org/pricing "
                                  "for details."}).encode()


class OaServer:
    """OpenAlex at the opener: request ``n`` answers ``answers[n]`` (a 429 body), past the list a
    200 empty page; each takes 0.4 virtual seconds."""

    def __init__(self, clock: Clock, answers: list[bytes]) -> None:
        self.clock, self.answers = clock, answers
        self.log: list[float] = []

    def open(self, req: Any, timeout: float | None = None, **kw: Any) -> Resp:
        url = req.full_url if hasattr(req, "full_url") else str(req)
        n = len(self.log)
        self.log.append(self.clock.t)
        self.clock.sleep(0.4)
        if n < len(self.answers):
            h = Message()
            h["Content-Type"] = "application/json"
            raise urllib.error.HTTPError(url, 429, "Too Many Requests", h, io.BytesIO(self.answers[n]))
        return Resp(url, b'{"results": [], "meta": {"next_cursor": null}}')


def _oa_transport(clock: Clock, monkeypatch: pytest.MonkeyPatch, srv: OaServer) -> tuple[Stub, list[float]]:
    from workflows.collect.openalex import main as oa

    monkeypatch.setattr(sh, "_opener", lambda: srv)
    stub = Stub()
    sleeps: list[float] = []

    def sleep(s: float) -> None:
        sleeps.append(s)
        clock.sleep(s)

    monkeypatch.setattr(oa, "_T", Transport(stub, oa.PACING, sleep=sleep, monotonic=clock.monotonic))
    monkeypatch.setattr(oa, "_sleep", sleep)
    return stub, sleeps


def test_openalex_plan_refusal_halts_at_once_without_ladder_or_cooldown(
        clock: Clock, monkeypatch: pytest.MonkeyPatch) -> None:
    """D583: the plan-upgrade 429 raises RunRefused[provider_plan] on the first answer: one request,
    no ladder sleep, no transport cooldown wait, and the cross-process Gate sets no cooldown (it would
    hold every local OpenAlex process for 60 s). Before: the 2/8/20 s ladder against a 60 s cooldown,
    ~4 min per query, then failed_transient[rate_limit]."""
    from stack.collect.base import RunRefused
    from workflows.collect.openalex import main as oa

    srv = OaServer(clock, [PLAN_429] * 10)
    stub, sleeps = _oa_transport(clock, monkeypatch, srv)
    t0 = clock.t
    stats: dict[str, Any] = {}
    with pytest.raises(RunRefused) as info:
        oa._get_page({"filter": "from_created_date:2026-10-01"}, stats)
    assert (info.value.error_class, info.value.permanent) == ("provider_plan", True)
    assert "requires a Premium" in info.value.reason and info.value.reason.startswith("HTTP 429: ")
    assert len(srv.log) == 1 and stub.counts["http_requests"] == 1 and stub.counts["http_429"] == 1
    assert [s for s in sleeps if s > 0] == [] and "http_cooldown" not in stub.counts
    assert clock.t - t0 < 1.0  # the answer's 0.4 s, nothing waited
    assert stats["http_429"] == 1 and stats.get("retries", 0) == 0
    assert "cooldown" not in pt.Gate(OA_WORKS).read()


def test_openalex_other_429_still_takes_the_burst_path(clock: Clock, monkeypatch: pytest.MonkeyPatch) -> None:
    """A 429 without the plan body (a burst) is unchanged: the Gate sets its 60 s cooldown, the ladder's
    2 s step meets it, the transport waits it out and re-sends; the page arrives."""
    from workflows.collect.openalex import main as oa

    srv = OaServer(clock, [json.dumps({"error": "Rate limit exceeded"}).encode()])
    stub, sleeps = _oa_transport(clock, monkeypatch, srv)
    stats: dict[str, Any] = {}
    assert oa._get_page({"filter": "x"}, stats) == {"results": [], "meta": {"next_cursor": None}}
    assert len(srv.log) == 2 and stats["http_429"] == 1 and stats["retries"] == 1
    assert sleeps[0] == oa.RETRY_DELAYS_S[0] and stub.counts["http_cooldown"] == 1
    assert 55.0 < srv.log[1] - srv.log[0] < 61.0  # the re-send waited the Gate's 60 s cooldown


# ---------------------------------------------------------------- D588: Transport.fetch's ladder
def _fetch_over(clock: Clock, answers: Any, pacing: Pacing) -> tuple[sh.HttpResponse, list[float], Stub]:
    """``Transport.fetch`` over a sender answering ``answers(n)`` to its ``n``-th call."""
    sent: list[float] = []

    def send(method: str, url: str, **kw: Any) -> sh.HttpResponse:
        sent.append(clock.t)
        return answers(len(sent) - 1, url)

    stub = Stub()
    transport = Transport(stub, pacing, send=send, sleep=clock.sleep, monotonic=clock.monotonic)
    return transport.fetch("GET", "https://api.openalex.org/works"), sent, stub


COOL = {"retry-after": "5", pt.COOLDOWN_HEADER.lower(): "1"}


def test_fetch_never_ladders_a_cooldown_answer(clock: Clock) -> None:
    """D588: ``request`` waits a Gate cooldown answer out at most three times; the one it then
    returns is returned by ``fetch`` at once, not given a ladder step (a step would only meet our own
    cooldown again). Before: each ladder step ran ``request``'s three waits again (12 sends)."""
    resp, sent, stub = _fetch_over(clock, lambda n, url: sh.HttpResponse(429, COOL, b"", url),
                                   Pacing(rps=1e6, retry_delays_s=(2.0, 4.0)))
    assert resp.status == 429 and pt.COOLDOWN_HEADER.lower() in resp.headers
    assert len(sent) == 4 and [round(b - a, 6) for a, b in zip(sent, sent[1:], strict=False)] == [5.0] * 3
    assert "http_retries" not in stub.counts and stub.counts["http_cooldown"] == 4


@pytest.mark.parametrize("retry_after, sends, gap", [("30", 2, 30.0), ("600", 1, None)],
                         ids=["within-threshold", "over-threshold"])
def test_fetch_ladder_honours_a_stated_retry_after(clock: Clock, retry_after: str, sends: int,
                                                   gap: float | None) -> None:
    """D588: a 429's ladder step is no shorter than its ``Retry-After`` (30 s, not 2 s); one asking
    more than the threshold (here the 60 s default of a pacing that does not halt) is returned at
    once, no step taken. Before: 2 s, and three sends for the 600 s case."""
    def answers(n: int, url: str) -> sh.HttpResponse:
        if n == 0:
            return sh.HttpResponse(429, {"retry-after": retry_after}, b"", url)
        return sh.HttpResponse(200, {}, b"{}", url)

    resp, sent, _stub = _fetch_over(clock, answers, Pacing(rps=1e6, retry_delays_s=(2.0, 4.0)))
    assert len(sent) == sends and resp.status == (200 if gap else 429)
    if gap:
        assert sent[1] - sent[0] == pytest.approx(gap)


def test_fetch_never_retries_a_plan_refusal(clock: Clock) -> None:
    """D583 for every ``Transport`` user (``collect.openalex-lookup`` ladders 429s): a plan refusal
    is sent once. Before: three sends 2 s and 4 s apart."""
    body = b'{"error": "Plan upgrade required", "message": "from_created_date needs Premium"}'
    resp, sent, _stub = _fetch_over(clock, lambda n, url: sh.HttpResponse(429, {}, body, url),
                                    lookup.OPENALEX_PACING)
    assert resp.status == 429 and len(sent) == 1
