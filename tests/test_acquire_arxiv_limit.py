"""arXiv's "slow down" answers in the acquire arXiv lane (D584, D585).

A limited answer (a 429, which the session returns at once since D588, a 503, or the Gate's cooldown
answer) is waited out
and asked again; the fourth in a row gives up (Atom: no more sweeps this run, D411; PDF/HTML: the
lane halts). The pure rule (:func:`limit_wait`, :func:`give_up_reason`), the unversioned HTML page's
version (:func:`html_version`, D585), and the lane driven over the real chain (``HttpSession`` ->
``urllib_get`` -> ``throttled_open`` -> ``Gate``) on a fake opener and a virtual clock, with a tmp
``STACK_PROVIDER_STATE`` (conftest), so the Gate's 3.1 s interval and 60 s cooldown are the real
ones. The runner cases (fake transport, roles and counts) are in ``tests/test_acquire_lanes.py``.
"""

from __future__ import annotations

import datetime as dt
import io
import types
import urllib.error
import urllib.parse
from email.message import Message
from typing import Any

import pytest

import stack.http as sh
import stack.provider_transport as pt
from stack.http import HttpResponse, urllib_get
from workflows.acquire.lanes import FetchRequest, LaneEnv
from workflows.acquire.lanes.arxiv import (
    LANE,
    LIMIT_GIVE_UP,
    LIMIT_WAIT_MAX_S,
    give_up_reason,
    html_version,
    limit_wait,
)
from workflows.acquire.lanes.http import HttpSession

NOW = dt.datetime(2026, 10, 15, 12, tzinfo=dt.UTC)
GATE_S = 3.1  # stack.provider_transport._INTERVALS["arxiv"]
IDS = ["2610.00001", "2610.00002", "2610.00003"]


# ---------------------------------------------------------------- the pure rule
def test_limit_wait_doubles_from_60_s_and_respects_a_stated_wait() -> None:
    assert [limit_wait(k, None) for k in (1, 2, 3)] == [60.0, 120.0, 240.0]
    assert limit_wait(1, 46.2) == 60.0     # a Gate cooldown answer's remainder: the 60 s floor
    assert limit_wait(1, 90.0) == 90.0     # a longer Retry-After is respected
    assert limit_wait(2, 90.0) == 120.0


def test_give_up_after_four_in_a_row_a_long_wait_or_past_the_budget() -> None:
    assert [give_up_reason(k, limit_wait(k, None), None) for k in (1, 2, 3)] == [None] * 3
    assert give_up_reason(LIMIT_GIVE_UP, 60.0, None) == f"{LIMIT_GIVE_UP} in a row"
    assert give_up_reason(1, LIMIT_WAIT_MAX_S, None) is None
    assert "over" in str(give_up_reason(1, LIMIT_WAIT_MAX_S + 1, None))    # Retry-After 7200: give up now
    assert "budget" in str(give_up_reason(1, 60.0, 59.0))                  # the fetch ends first
    assert give_up_reason(1, 60.0, 61.0) is None


@pytest.mark.parametrize(("final", "body", "want"), [
    ("https://arxiv.org/html/2610.00001v3", b"", 3),                                         # a redirect
    ("https://arxiv.org/html/2610.00001", b'<head><base href="/html/2610.00001v2/"></head>', 2),
    ("https://arxiv.org/html/2610.00001", b'<div id="watermark-tr">arXiv:2610.00001v4 [cs.LG]</div>', 4),
    ("https://arxiv.org/html/2610.00001", b'<base href="/html/2610.00001v2/">arXiv:2610.00001v2', 2),
    ("https://arxiv.org/html/2610.00001", b'<base href="/html/2610.00001v2/">arXiv:2610.00001v1', None),  # two
    ("https://arxiv.org/html/2610.00001", b'<p>cites arXiv:2610.000012v1 and /html/2610.0000v9</p>', None),
    ("https://arxiv.org/html/2610.00001", b"<html>no version anywhere</html>", None),
])
def test_html_version_of_the_unversioned_page(final: str, body: bytes, want: int | None) -> None:
    """D585: the final URL first, else the one version the page names; several or none: ``None``."""
    assert html_version(HttpResponse(200, {}, body, final), "2610.00001") == want


# ---------------------------------------------------------------- the lane over the real Gate
class Clock:
    def __init__(self) -> None:
        self.t = 2_000_000.0

    def time(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += max(0.0, s)

    def monotonic(self) -> float:
        return self.t


class Resp:
    def __init__(self, url: str, body: bytes, ctype: str) -> None:
        self.status, self.url, self._b = 200, url, body
        self.headers = Message()
        self.headers["Content-Type"] = ctype

    def __enter__(self) -> Resp:
        return self

    def __exit__(self, *a: Any) -> bool:
        return False

    def read(self, n: int = -1) -> bytes:
        return self._b

    def geturl(self) -> str:
        return self.url


def feed(ids: list[str]) -> bytes:
    entries = "".join(f"<entry><id>http://arxiv.org/abs/{a}v1</id><published>2026-10-05T10:00:00Z</published>"
                      f"<updated>2026-10-05T10:00:00Z</updated><title>P</title></entry>" for a in ids)
    return f'<feed xmlns="http://www.w3.org/2005/Atom">{entries}</feed>'.encode()


class Arxiv:
    """Real requests arrive here (the Gate let them through): Atom answers 429 (no Retry-After) to
    its first ``atom_429`` requests, then the feed; a PDF URL answers a PDF. Each takes 0.4 s."""

    def __init__(self, clock: Clock, atom_429: int) -> None:
        self.clock, self.atom_429 = clock, atom_429
        self.log: list[tuple[float, str, int]] = []

    def open(self, req: Any, timeout: float | None = None, **kw: Any) -> Resp:
        url = req.full_url
        parts = urllib.parse.urlsplit(url)
        self.clock.sleep(0.4)
        if parts.hostname == "export.arxiv.org":
            n = sum(1 for _, h, _ in self.log if h == "atom")
            status = 429 if n < self.atom_429 else 200
            self.log.append((self.clock.t - 0.4, "atom", status))
            if status == 429:
                raise urllib.error.HTTPError(url, 429, "Too Many Requests", Message(), io.BytesIO(b"slow down"))
            ids = urllib.parse.parse_qs(parts.query)["id_list"][0].split(",")
            return Resp(url, feed(ids), "application/atom+xml")
        self.log.append((self.clock.t - 0.4, "pdf", 200))
        return Resp(url, b"%PDF-1.7\n" + b"x" * 20_000, "application/pdf")


@pytest.fixture()
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    c = Clock()
    monkeypatch.setattr(pt, "time", types.SimpleNamespace(time=c.time, sleep=c.sleep))
    return c


def lane_env(clock: Clock, blobs: Any) -> LaneEnv:
    env = LaneEnv(run_id="r", now=NOW, blobs=blobs, sleep=clock.sleep, monotonic=clock.monotonic)
    # The real getter (urllib_get -> throttled_open -> Gate) without DNS (resolver None).
    env.lane_state["http"] = HttpSession(urllib_get, sleep=clock.sleep, monotonic=clock.monotonic, resolver=None)
    return env


def request(aid: str) -> FetchRequest:
    member = {"source_id": f"s-{aid}", "key_kind": "arxiv", "key_value": aid, "channel": "arxiv", "is_live": True}
    return FetchRequest(doc_id=f"d-{aid}", source_id=f"s-{aid}", content_role="full_text", channel="arxiv",
                        channel_group="arxiv", member=member, role={"status": "not_attempted", "attempts": 0},
                        now=NOW, lane="arxiv")


def run_lane(env: LaneEnv) -> list[Any]:
    LANE.prepare([request(a) for a in IDS], env)
    return [LANE.fetch(request(a), env) for a in IDS]


def test_one_atom_429_is_waited_out_and_the_gate_paces_every_request(
        clock: Clock, monkeypatch: pytest.MonkeyPatch, blobs: Any) -> None:
    """(a, e) One real Atom 429 sets the Gate's 60 s cooldown; the session returns it at once (D588:
    before, its three retries met the cooldown, sent nothing and slept 14 s), the lane waits 60 s and
    asks again: the sweep
    resumes and every paper is fetched. Every real request is at least the Gate's 3.1 s after the
    previous one, the re-ask at least 60 s after the 429, and arXiv's pacer is not halved below
    the Gate's interval (before D584 it fell to the 0.05 rps floor)."""
    srv = Arxiv(clock, atom_429=1)
    monkeypatch.setattr(sh, "_opener", lambda: srv)
    env = lane_env(clock, blobs)
    outs = run_lane(env)
    assert [o.status for o in outs] == ["ok"] * 3, [(o.status, o.error) for o in outs]
    assert [(h, s) for _, h, s in srv.log] == [("atom", 429), ("atom", 200), ("pdf", 200), ("pdf", 200), ("pdf", 200)]
    times = [t for t, _, _ in srv.log]
    gaps = [b - a for a, b in zip(times, times[1:], strict=False)]
    assert min(gaps) >= GATE_S - 1e-6, gaps
    assert gaps[0] >= 60.0                                   # the Gate's cooldown was waited out
    assert gaps[0] < 60.0 + 2 * GATE_S + 1                  # one 60 s wait, no retries into the cooldown (D588)
    c = env.counts
    assert c["arxiv.limited_waits.atom"] == 1 and c["arxiv.limited_wait_s"] == 60
    assert "arxiv.atom_limited" not in c and "arxiv.halted" not in c and c["arxiv.downloads"] == 3
    session: HttpSession = env.lane_state["http"]
    assert session.pacer._rps("export.arxiv.org") >= 1 / GATE_S - 1e-9


def test_atom_limited_four_times_gives_up_without_a_tight_loop(
        clock: Clock, monkeypatch: pytest.MonkeyPatch, blobs: Any) -> None:
    """(b) Atom answers 429 to every request: four real requests (the session returns each 429 at
    once, D588), 60/120/240 s apart, then the lane gives up
    Atom for the run (D411): every paper is ``failed_transient[rate_limit]`` without a request, no
    halt, no PDF asked."""
    srv = Arxiv(clock, atom_429=10**6)
    monkeypatch.setattr(sh, "_opener", lambda: srv)
    env = lane_env(clock, blobs)
    t0 = clock.t
    outs = run_lane(env)
    assert [(o.status, o.error_class, o.halt_run) for o in outs] == [("failed_transient", "rate_limit", False)] * 3
    assert [(h, s) for _, h, s in srv.log] == [("atom", 429)] * LIMIT_GIVE_UP
    times = [t for t, _, _ in srv.log]
    gaps = [b - a for a, b in zip(times, times[1:], strict=False)]
    assert all(g >= w for g, w in zip(gaps, (60, 120, 240), strict=True)), gaps
    assert clock.t - t0 < 60 + 120 + 240 + LIMIT_GIVE_UP * (2 * GATE_S + 1)
    c = env.counts
    assert c["arxiv.limited_waits.atom"] == LIMIT_GIVE_UP - 1 and c["arxiv.limited_wait_s"] == 60 + 120 + 240
    assert c["arxiv.atom_limited"] == 1 and c["arxiv.limited_roles"] == 3 and "arxiv.halted" not in c
    assert "4 in a row" in str(outs[0].error)
