"""Pure cases of the OA lane (``workflows/acquire/lanes/oa.py``, ADAPTed from v3's ``fetch.oa``)
and of its fetchpdf door (``stack/oa_adapter.py``): 8 of v3's 29 (the rest exercised the
database engine).

Offline throughout (no socket is opened): one ``slow`` case runs fetchpdf's real engine against
the fake web of ``tests/fakes/oa_world.py`` (the T3.x-f4 replay); the one that inspects the
installed build only imports it. The T3.x-f4 cases (the direct Zenodo, MDPI and Unpaywall steps,
the run cache, DataCite skips, the ``xml`` step, priming, negative reuse) close the file. The request-gate cases (D26 for this lane) drive a fake fetchpdf that talks
``requests`` and are answered by ``tests/fakes/http.FakeHttp`` through the gate. The lane's runner-level cases (a PDF as ``full_text``, an XML as ``xml``, the
guard refusing, the missing contact address) are in ``tests/test_acquire_lanes.py``.

Coverage: the D4 licence guard (the reason the lane may refuse to fetch at all); the identifier
rule (a DOI wins, a PMID otherwise, no other key); which artifact each role takes.
"""

from __future__ import annotations

import datetime as dt
import functools
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import stack.oa_adapter as oa
import stack.provider_transport as oa_
from stack.store.blobs import open_blobs
from tests.fakes.http import FakeHttp
from tests.fakes.lanes import FakeFetchpdf
from tests.fakes.oa_world import B1_MIX, OaWorld, b1_mix, pdf_bytes
from workflows.acquire.lanes import FetchOutcome, FetchRequest, LaneEnv
from workflows.acquire.lanes import oa as oa_lane
from workflows.acquire.lanes.http import HttpSession
from workflows.acquire.lanes.oa import OaLane, identifiers_of, pick


# ═════════════════════════════════════════════════════════════════════════════
# D4 — the licence guard. This unit may refuse to start, and that is the point.
# ═════════════════════════════════════════════════════════════════════════════
class TestItRefusesToRunUnlessOpenAccessIsProven:

    def test_a_build_that_reaches_nothing_is_accepted(self) -> None:
        """The public build: the optional pack is absent, so there is nothing to turn off."""
        mod = SimpleNamespace(last_resorts_enabled=lambda: False)
        oa._require_open_access_only(mod)  # must not raise

    def test_a_build_that_reaches_something_is_turned_off_then_accepted(self) -> None:
        """The development build enables one of those sources BY DEFAULT, so simply importing it
        and fetching would acquire through it. Asking is not optional."""
        state = {"on": True}
        mod = SimpleNamespace(
            disable_last_resorts=lambda: state.__setitem__("on", False),
            last_resorts_enabled=lambda: state["on"],
        )
        oa._require_open_access_only(mod)
        assert state["on"] is False

    def test_a_build_that_will_not_turn_off_is_refused(self) -> None:
        mod = SimpleNamespace(
            disable_last_resorts=lambda: None,      # accepts the call, ignores it
            last_resorts_enabled=lambda: True,
        )
        with pytest.raises(RuntimeError, match="last-resort"):
            oa._require_open_access_only(mod)

    def test_a_build_with_no_accessor_but_no_pack_either_is_accepted(self,
                                                                     monkeypatch: Any) -> None:
        """The published package today: it cannot report reachability, but the pack is genuinely
        not there. Refusing it would be strictness with nothing behind it."""
        monkeypatch.setattr(oa, "_pack_is_provably_absent", lambda _m: True)
        oa._require_open_access_only(SimpleNamespace())  # must not raise

    def test_a_build_with_no_accessor_and_an_unprovable_pack_is_refused(self,
                                                                       monkeypatch: Any) -> None:
        """An OLD PRIVATE build looks exactly like the published one from the outside — no
        accessor — and it ships the pack with an entry enabled. So absence has to be established,
        never inferred from the missing accessor."""
        monkeypatch.setattr(oa, "_pack_is_provably_absent", lambda _m: False)
        with pytest.raises(RuntimeError, match="cannot establish"):
            oa._require_open_access_only(SimpleNamespace())

    def test_absence_needs_both_the_module_gone_and_the_registry_empty(self) -> None:
        """Neither fact alone is trusted: a registry can be emptied at runtime, and a module can
        sit on disk unimported."""
        assert oa._pack_is_provably_absent(
            SimpleNamespace(LAST_RESORT_SOURCES=())) is (
            __import__("importlib.util", fromlist=["util"]).find_spec("fetchpdf.extras") is None)
        # A populated registry is disqualifying regardless of what the module search says.
        assert oa._pack_is_provably_absent(
            SimpleNamespace(LAST_RESORT_SOURCES=(SimpleNamespace(name="x"),))) is False

    def test_the_real_installed_fetchpdf_satisfies_the_guard(self) -> None:
        """Against whatever build is actually installed here.

        This is the test that would have caught this stack resolving `import fetchpdf` to a tree
        whose last-resort source is enabled by default — which is what an editable install of the
        development repository does, and what was in fact on this machine's path.
        """
        fetchpdf = pytest.importorskip("fetchpdf.fetchpdf")
        oa._require_open_access_only(fetchpdf)   # the assertion is that this does not raise

        # …and independently: nothing is reachable, by whichever route this build can show it.
        probe = getattr(fetchpdf, "last_resorts_enabled", None)
        if callable(probe):
            assert probe() is False
        else:
            assert oa._pack_is_provably_absent(fetchpdf)


# ═════════════════════════════════════════════════════════════════════════════
# Identifiers and the role → artifact rule
# ═════════════════════════════════════════════════════════════════════════════
def test_scholarly_identifiers_win_and_other_keys_are_not_handed_over() -> None:
    """v3 read DOI/PMID from ``lookup_keys`` with the row's own DOI as fallback; a member carries
    one key, so: its DOI (key or ``doi.org`` URL), else its PMID, else nothing."""
    assert identifiers_of({"key_kind": "doi", "key_value": "10.1/scholarly"}).doi == "10.1/scholarly"
    ids = identifiers_of({"key_kind": "pmid", "key_value": "123"})
    assert (ids.doi, ids.pmid, ids.raw) == ("", "123", "123")
    ids = identifiers_of({"key_kind": "openalex", "key_value": "W42", "canonical_url": "https://doi.org/10.1/x"})
    assert (ids.doi, ids.raw) == ("10.1/x", "10.1/x")
    assert identifiers_of({"key_kind": "openalex", "key_value": "W42", "canonical_url": None}).raw == ""


def test_each_role_takes_its_artifact() -> None:
    """``full_text`` takes the PDF, else publisher HTML; ``xml`` the XML only (never text)."""
    xml, pdf, html = ("application/xml", b"x", 1), ("application/pdf", b"p", 2), ("text/html", b"h", 3)
    assert pick([xml, pdf], "full_text") == pdf and pick([xml, pdf], "xml") == xml
    assert pick([html, xml], "full_text") == html and pick([pdf, html], "full_text") == pdf
    assert pick([pdf], "xml") is None and pick([xml], "full_text") is None and pick([pdf], "page") is None


# ═════════════════════════════════════════════════════════════════════════════
# D26 for the OA lane: every request fetchpdf makes passes the run's gate
# ═════════════════════════════════════════════════════════════════════════════
NOW = dt.datetime(2026, 10, 12, 12, tzinfo=dt.UTC)
DOI = "10.5555/gate.1"
PDF = b"%PDF-1.7\n" + b"x" * 20_000
JATS = b'<?xml version="1.0"?><article><body><p>' + b"text " * 200 + b"</p></body></article>"


class Clock:
    """A monotonic clock that the injected sleep advances: pacing is measured, never waited."""

    def __init__(self) -> None:
        self.t, self.sleeps = 1000.0, []

    def monotonic(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


def oa_env(tmp_path: Path, fake: FakeHttp, clock: Clock, route: str = "walk") -> LaneEnv:
    """The gate cases drive fetchpdf's walk (``oa_route: walk``); the direct steps' cases pass
    ``route="direct"``."""
    env = LaneEnv(run_id="r1", now=NOW, blobs=open_blobs(tmp_path), transport=fake,
                  mailto="lane-tests@example.invalid", sleep=clock.sleep, options={"oa_route": route})
    env.lane_state["http"] = HttpSession(fake.get, sleep=clock.sleep, monotonic=clock.monotonic)
    return env


def oa_request(role: str = "full_text") -> FetchRequest:
    member = {"source_id": "s1", "channel": "openalex", "key_kind": "doi", "key_value": DOI}
    return FetchRequest(doc_id="d1", source_id="s1", content_role=role, channel="openalex",
                        channel_group="paper", member=member, role={"attempts": 0}, now=NOW, lane="oa")


def over_http(*arts: tuple[bytes, str, str], lookups: tuple[str, ...] = ()) -> FakeFetchpdf:
    return FakeFetchpdf({DOI: list(arts)}, over_http=True, lookups={DOI: list(lookups)})


def fetch(fp: FakeFetchpdf, env: LaneEnv, role: str = "full_text") -> FetchOutcome:
    return OaLane(load=fp.loader).fetch(oa_request(role), env)


def test_fetchpdf_requests_share_the_run_pacer(tmp_path: Path) -> None:
    """fetchpdf's own requests (a session's and a module-level ``requests.get``) are paced by the
    run's ``HostPacer``, the same one the http lane uses: a slot the http lane just took on
    ``pub.test`` makes fetchpdf's first request there wait, and two requests to the same host
    are a full interval apart (1 rps default); another host is not held up. Every request went to
    the run's transport (none to the network: the test guard would refuse it)."""
    fake, clock = FakeHttp(), Clock()
    fake.add("https://api.unpaywall.org/v2/" + DOI, json={"best_oa_location": None})
    fake.add("https://pub.test/a.xml", body=JATS)
    fake.add("https://pub.test/a.pdf", body=PDF)
    env = oa_env(tmp_path, fake, clock)
    HttpSession.of(env).pacer.acquire("pub.test")  # the http lane hit pub.test a moment ago
    fp = over_http((JATS, "https://pub.test/a.xml", "europepmc"), (PDF, "https://pub.test/a.pdf", "unpaywall"),
                   lookups=("https://api.unpaywall.org/v2/" + DOI,))
    out = fetch(fp, env)
    assert (out.status, out.error_class) == ("ok", None) and out.snapshots[0]["media_type"] == "application/pdf"
    assert fake.urls() == ["https://api.unpaywall.org/v2/" + DOI, "https://pub.test/a.xml", "https://pub.test/a.pdf"]
    assert clock.sleeps == [1.0, 1.0]  # pub.test: after the http lane's slot, then after the XML
    assert env.counts["oa.http.requests"] == 3


def test_a_429_is_retried_with_backoff_then_charged_as_transient(tmp_path: Path) -> None:
    """A content host answering 429 to every try: the gate retries with 2/4/8 s backoff, each step no
    shorter than the stated ``Retry-After`` (5 s: 5, 5, 8 s; D588) (penalising
    the host's rate), and the role, with no artifact, is ``failed_transient[rate_limit]`` with the
    ``Retry-After`` as the hint and no halt — never ``failed_permanent[no_fetchable_url]`` (the
    fetchpdf reason "no open-access copy" is not believed), never ``ok``. The sibling role from
    the same retrieval (``xml``, whose copy came through) is ``ok``. (Before D588 this used
    ``Retry-After: 900``, which is now over the cap and stands at once:
    ``test_a_429_asking_past_the_cap_stands_at_once``.)"""
    fake, clock = FakeHttp(), Clock()
    fake.add("https://pub.test/a.xml", body=JATS)
    fake.add("https://pub.test/a.pdf", body=PDF)
    fake.inject("https://pub.test/a.pdf", status=429, times=4, retry_after=5)
    env = oa_env(tmp_path, fake, clock)
    fp = over_http((JATS, "https://pub.test/a.xml", "europepmc"), (PDF, "https://pub.test/a.pdf", "unpaywall"))
    lane = OaLane(load=fp.loader)
    out = lane.fetch(oa_request("full_text"), env)
    assert (out.status, out.error_class, out.halt_run) == ("failed_transient", "rate_limit", False)
    assert out.retry_after_hint == NOW + dt.timedelta(seconds=5) and "pub.test" in (out.error or "")
    assert fake.urls().count("https://pub.test/a.pdf") == 4
    assert [s for s in clock.sleeps if s in (2.0, 4.0, 5.0, 8.0)] == [5.0, 5.0, 8.0]
    assert (env.counts["oa.http.http_429"], env.counts["oa.http.retries"], env.counts["oa.rate_limited"]) == (4, 3, 1)
    xml = lane.fetch(oa_request("xml"), env)
    assert xml.status == "ok" and len(fp.calls) == 1


def test_a_429_that_clears_is_an_ordinary_fetch(tmp_path: Path) -> None:
    """Two 429s, then the PDF: retried inside the episode, ``ok``, nothing recorded as limited."""
    fake, clock = FakeHttp(), Clock()
    fake.add("https://pub.test/a.pdf", body=PDF)
    fake.inject("https://pub.test/a.pdf", status=429, times=2)
    env = oa_env(tmp_path, fake, clock)
    out = fetch(over_http((PDF, "https://pub.test/a.pdf", "unpaywall")), env)
    assert out.status == "ok" and "oa.rate_limited" not in env.counts
    assert fake.urls().count("https://pub.test/a.pdf") == 3


def test_a_429_from_a_resolver_api_halts_the_run(tmp_path: Path) -> None:
    """Unpaywall (a resolver API every paper needs) still 429 after the retries and no artifact:
    provider-level, so ``halt_run`` (§5.6 "OpenAlex": a 429 halts the lane for the run, D164; no row charge)."""
    fake, clock = FakeHttp(), Clock()
    fake.add("https://api.unpaywall.org/v2/" + DOI, status=429, headers={"Retry-After": "60"})
    env = oa_env(tmp_path, fake, clock)
    out = fetch(over_http(lookups=("https://api.unpaywall.org/v2/" + DOI,)), env)
    assert (out.status, out.error_class, out.halt_run) == ("failed_transient", "rate_limit", True)


S2_URL = f"https://api.semanticscholar.org/graph/v1/paper/DOI:{DOI}?fields=openAccessPdf"


def test_an_s2_429_without_a_copy_does_not_halt_the_lane(tmp_path: Path) -> None:
    """S2 is one optional source of fetchpdf's walk, not a resolver every paper needs (review
    L4-1, D402, amends D164 for S2): its 429 on a paper with no copy charges that role
    ``failed_transient[rate_limit]`` with the ``Retry-After`` hint, like a content host, and the
    lane goes on: the next paper of the run is still fetched."""
    fake, clock = FakeHttp(), Clock()
    fake.add(S2_URL, status=429, headers={"Retry-After": "600"})
    fake.add("https://pub.test/b.pdf", body=PDF)
    env = oa_env(tmp_path, fake, clock)
    doi2 = "10.5555/gate.2"
    fp = FakeFetchpdf({DOI: [], doi2: [(PDF, "https://pub.test/b.pdf", "unpaywall")]}, over_http=True,
                      lookups={DOI: [S2_URL]})
    lane = OaLane(load=fp.loader)
    out = lane.fetch(oa_request(), env)
    assert (out.status, out.error_class, out.halt_run) == ("failed_transient", "rate_limit", False)
    assert out.retry_after_hint == NOW + dt.timedelta(seconds=600) and "api.semanticscholar.org" in (out.error or "")
    member = {"source_id": "s2", "channel": "openalex", "key_kind": "doi", "key_value": doi2}
    nxt = lane.fetch(FetchRequest(doc_id="d2", source_id="s2", content_role="full_text", channel="openalex",
                                  channel_group="paper", member=member, role={"attempts": 0}, now=NOW,
                                  lane="oa"), env)
    assert nxt.status == "ok" and [c["raw"] for c in fp.calls] == [DOI, doi2]


def test_a_standing_s2_or_openalex_cooldown_does_not_halt_the_lane_but_a_resolver_limit_does(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A cross-process ``Gate`` cooldown is recorded as a provider-level limit without a request;
    for S2 or OpenAlex (one rung of a member's search) the role is only charged, no halt (D402,
    D413: until D413 OpenAlex halted); a limit on Unpaywall or the ID Converter (a resolver every
    member needs, ``LANE_WIDE_APIS``) halts the lane, alone or alongside S2."""
    from stack.http import HostPacer
    from stack.oa_adapter import Limited, RequestGate

    monkeypatch.setenv("STACK_PROVIDER_STATE", str(tmp_path / "providers"))
    fake, clock = FakeHttp(), Clock()
    openalex = "https://api.openalex.org/works/doi:" + DOI
    fake.add(S2_URL, status=429, headers={"Retry-After": "120"})
    fake.add(openalex, status=429, headers={"Retry-After": "120"})
    gate = RequestGate(pacer=HostPacer(monotonic=clock.monotonic, sleep=clock.sleep), sleep=clock.sleep,
                       transport=fake.get, provider_gate=True)
    import requests

    with gate.active():
        assert requests.get(S2_URL, timeout=5).status_code == 429
    (s2,) = gate.take()
    assert (s2.host, s2.provider) == ("api.semanticscholar.org", True) and fake.urls() == [S2_URL]
    with gate.active():
        assert requests.get(openalex, timeout=5).status_code == 429
    (oax,) = gate.take()
    env = oa_env(tmp_path, fake, clock)
    ids = identifiers_of(oa_request().member)
    out = oa_lane.rate_limited([s2], ids, "full_text", env)
    assert (out.status, out.error_class, out.halt_run) == ("failed_transient", "rate_limit", False)
    assert out.retry_after_hint is not None
    assert oa_lane.rate_limited([oax], ids, "full_text", env).halt_run is False
    assert oa_lane.rate_limited([s2, oax], ids, "full_text", env).halt_run is False
    unpaywall = Limited("https://api.unpaywall.org/v2/" + DOI, "api.unpaywall.org", True, 60.0)
    idconv = Limited("https://pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles/?ids=" + DOI,
                     "pmc.ncbi.nlm.nih.gov", True, None)
    pmc_pdf = Limited("https://pmc.ncbi.nlm.nih.gov/articles/PMC1/pdf/a.pdf", "pmc.ncbi.nlm.nih.gov", True, None)
    assert oa_lane.rate_limited([unpaywall], ids, "full_text", env).halt_run is True
    assert oa_lane.rate_limited([s2, unpaywall], ids, "full_text", env).halt_run is True
    assert oa_lane.rate_limited([idconv], ids, "xml", env).halt_run is True
    assert oa_lane.rate_limited([pmc_pdf], ids, "xml", env).halt_run is False  # PMC's full text: one rung


ARXIV_PDF = "https://arxiv.org/pdf/2401.00001"


def _two_papers(fake: FakeHttp) -> tuple[FakeFetchpdf, str]:
    """Paper 1 (a Science DOI, as in e2e build 2) whose walk reaches arxiv.org and nothing else;
    paper 2 with its PDF on a publisher host."""
    fake.add("https://pub.test/b.pdf", body=PDF)
    doi2 = "10.5555/gate.2"
    return FakeFetchpdf({DOI: [(PDF, ARXIV_PDF, "arxiv")], doi2: [(PDF, "https://pub.test/b.pdf", "unpaywall")]},
                        over_http=True), doi2


def _next_paper(lane: OaLane, env: LaneEnv, doi2: str) -> FetchOutcome:
    member = {"source_id": "s2", "channel": "openalex", "key_kind": "doi", "key_value": doi2}
    return lane.fetch(FetchRequest(doc_id="d2", source_id="s2", content_role="full_text", channel="openalex",
                                   channel_group="paper", member=member, role={"attempts": 0}, now=NOW,
                                   lane="oa"), env)


def test_a_standing_arxiv_cooldown_does_not_halt_the_lane(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """e2e build 2 (D413): the arXiv lane's Atom 429 set the cross-process arXiv ``Gate`` cooldown
    (60 s, no ``Retry-After``); an OA walk that reached arxiv.org as one rung met it and the OA lane
    halted for the run. Now that role is ``failed_transient[rate_limit]`` with the cooldown as the
    hint, the next paper is still fetched; the cooldown is answered with no request sent and before
    the host's pacer slot (L4-9: no wait behind the arXiv lane's last request)."""
    import time

    from stack.provider_transport import Gate

    monkeypatch.setenv("STACK_PROVIDER_STATE", str(tmp_path / "providers"))
    monkeypatch.setattr(oa_lane, "RequestGate", functools.partial(oa.RequestGate, provider_gate=True))
    Gate(ARXIV_PDF).observe(429, {})  # what the arXiv lane's Atom 429 leaves behind
    fake, clock = FakeHttp(), Clock()
    fp, doi2 = _two_papers(fake)
    env = oa_env(tmp_path, fake, clock)
    HttpSession.of(env).pacer.acquire("arxiv.org")  # the arXiv lane hit arxiv.org a moment ago
    lane = OaLane(load=fp.loader)
    before = time.time()
    out = lane.fetch(oa_request(), env)
    assert (out.status, out.error_class, out.halt_run) == ("failed_transient", "rate_limit", False)
    assert "arxiv.org" in (out.error or "") and out.retry_after_hint is not None
    assert NOW + dt.timedelta(seconds=50) < out.retry_after_hint <= NOW + dt.timedelta(seconds=60 + time.time() - before)
    assert ARXIV_PDF not in fake.urls() and clock.sleeps == []
    nxt = _next_paper(lane, env, doi2)
    assert nxt.status == "ok" and [c["raw"] for c in fp.calls] == [DOI, doi2]


def test_an_arxiv_429_without_a_copy_does_not_halt_the_lane(tmp_path: Path) -> None:
    """A real 429 from arxiv.org standing after the gate's retries, on a paper with no other copy:
    the role answers ``failed_transient[rate_limit]`` (not counted toward the cap, D484) with the
    ``Retry-After`` hint, no halt, the next paper is still fetched (D413). arxiv.org's pacer is halved no lower than the arXiv Gate's
    interval (3.1 s; L4-9, as D401 for collect), not to 0.0625 rps. (``Retry-After: 10``, inside
    D588's cap, so the four tries happen; it was 300 s before D588.)"""
    fake, clock = FakeHttp(), Clock()
    fake.add(ARXIV_PDF, status=429, headers={"Retry-After": "10"})
    fp, doi2 = _two_papers(fake)
    env = oa_env(tmp_path, fake, clock)
    lane = OaLane(load=fp.loader)
    out = lane.fetch(oa_request(), env)
    assert (out.status, out.error_class, out.halt_run) == ("failed_transient", "rate_limit", False)
    assert out.retry_after_hint == NOW + dt.timedelta(seconds=10) and "arxiv.org" in (out.error or "")
    assert fake.urls().count(ARXIV_PDF) == 4
    pacer = HttpSession.of(env).pacer
    pacer.acquire("arxiv.org")
    assert pacer.acquire("arxiv.org") == pytest.approx(3.1)
    nxt = _next_paper(lane, env, doi2)
    assert nxt.status == "ok" and [c["raw"] for c in fp.calls] == [DOI, doi2]


def test_the_gate_honours_a_provider_cooldown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With the cross-process ``Gate`` on (the network path; here over the fake): OpenAlex's 429
    with ``Retry-After`` sets the provider's cooldown, the retry meets it and is answered locally
    as a 429 without a request; the cooldown is recorded as a provider-level limit."""
    from stack.http import HostPacer
    from stack.oa_adapter import RequestGate

    monkeypatch.setenv("STACK_PROVIDER_STATE", str(tmp_path / "providers"))
    fake, clock = FakeHttp(), Clock()
    url = "https://api.openalex.org/works/doi:" + DOI
    fake.add(url, status=429, headers={"Retry-After": "120"})
    gate = RequestGate(pacer=HostPacer(monotonic=clock.monotonic, sleep=clock.sleep), sleep=clock.sleep,
                       transport=fake.get, provider_gate=True)
    import requests

    with gate.active():
        r = requests.get(url, timeout=5)
    assert r.status_code == 429 and fake.urls() == [url]
    (lim,) = gate.take()
    assert (lim.host, lim.provider) == ("api.openalex.org", True) and lim.retry_after_s is not None
    assert lim.retry_after_s > 100


def test_a_gate_familys_429_is_not_retried_into_its_own_cooldown(tmp_path: Path,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    """D588 (Dan Elton's v3 fix, e9dc435): a bare 429 from a Gate family (OpenAlex) sets the Gate's
    60 s cooldown, so the 2 s backoff retry could only meet that cooldown and send nothing. The 429
    now stands at once: one request, no backoff sleep, no retry counted, recorded as limited with
    the cooldown as its wait (the lane then goes on, D413). Before: a 2 s sleep and a retry answered
    locally."""
    from stack.http import HostPacer
    from stack.oa_adapter import RequestGate

    monkeypatch.setenv("STACK_PROVIDER_STATE", str(tmp_path / "providers"))
    fake, clock = FakeHttp(), Clock()
    url = "https://api.openalex.org/works/doi:" + DOI
    fake.add(url, status=429)
    counts: dict[str, int] = {}
    gate = RequestGate(pacer=HostPacer(monotonic=clock.monotonic, sleep=clock.sleep), sleep=clock.sleep,
                       transport=fake.get, provider_gate=True,
                       count=lambda k, n: counts.__setitem__(k, counts.get(k, 0) + n))
    import requests

    with gate.active():
        r = requests.get(url, timeout=5)
    assert r.status_code == 429 and fake.urls() == [url] and clock.sleeps == []
    assert counts.get("retries", 0) == 0 and counts["http_429"] == 1
    (lim,) = gate.take()
    assert lim.retry_after_s is not None and 50 < lim.retry_after_s <= 60


@pytest.mark.parametrize("left", [50_000.0, 45.0])
def test_a_standing_cooldown_does_not_make_fetchpdf_wait(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                         left: float) -> None:
    """D699: a standing Gate cooldown (OpenAlex's daily limit: until midnight UTC; S2's backoff: up to
    60 s) is answered with a 429 that states no wait, so fetchpdf's own client (``retrieval.http``,
    the walk's resolver lookups) retries it after its short backoff instead of sleeping the stated
    wait (capped at 60 s) and draining its process-wide host bucket for as long, which held every OA
    slot behind one OpenAlex request a minute (chain-r5 2c: ~50 OA calls per 45-minute run). Nothing
    is sent; the cooldown is still the recorded limit's wait, the lane's hint."""
    import time as _time

    import requests

    from stack.http import HostPacer
    from stack.oa_adapter import RequestGate
    from stack.provider_transport import Gate, atomic_json

    oa.import_fetchpdf()
    from fetchpdf.retrieval import http as fhttp
    from fetchpdf.retrieval import ratelimit

    monkeypatch.setenv("STACK_PROVIDER_STATE", str(tmp_path / "providers"))
    url = "https://api.openalex.org/works/https://doi.org/" + DOI
    atomic_json(Gate(url).path, {"cooldown": _time.time() + left})
    fake, clock = FakeHttp(), Clock()

    def tick(s: float) -> None:  # a wait below the clock's float resolution still moves it
        clock.sleep(s)
        clock.t += 1e-6

    fake_time = SimpleNamespace(sleep=tick, monotonic=clock.monotonic, time=_time.time)
    monkeypatch.setattr(fhttp, "time", fake_time)
    monkeypatch.setattr(ratelimit, "time", fake_time)
    client = fhttp.HttpClient(ratelimit.HostRateLimiter({"api.openalex.org": {"rps": 10, "burst": 10}}))
    gate = RequestGate(pacer=HostPacer(monotonic=clock.monotonic, sleep=clock.sleep), sleep=clock.sleep,
                       transport=fake.get, provider_gate=True)
    assert requests  # the gate patches the transport requests uses
    with gate.active():
        r = client.get(url, timeout=5)
    assert r.status == 429 and fake.urls() == []  # three attempts, each answered locally
    assert sum(clock.sleeps) < 5 and max(clock.sleeps) < 2, clock.sleeps
    lims = gate.take()
    assert len(lims) == 3 and all(x.retry_after_s is not None and x.retry_after_s > left - 60 for x in lims)


def test_a_gate_familys_standing_429_states_no_wait_to_fetchpdf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """D699, the cooldown's onset: OpenAlex's own 429 with a long ``Retry-After`` sets the Gate's
    cooldown and stands at once (D588); fetchpdf gets it without the header (the wait is the
    recorded limit's), so its client does not sleep 60 s before retrying into the cooldown."""
    import time as _time

    from stack.http import HostPacer
    from stack.oa_adapter import RequestGate

    oa.import_fetchpdf()
    from fetchpdf.retrieval import http as fhttp
    from fetchpdf.retrieval import ratelimit

    monkeypatch.setenv("STACK_PROVIDER_STATE", str(tmp_path / "providers"))
    url = "https://api.openalex.org/works/https://doi.org/" + DOI
    fake, clock = FakeHttp(), Clock()
    fake.add(url, status=429, headers={"Retry-After": "50000"})

    def tick(s: float) -> None:
        clock.sleep(s)
        clock.t += 1e-6

    fake_time = SimpleNamespace(sleep=tick, monotonic=clock.monotonic, time=_time.time)
    monkeypatch.setattr(fhttp, "time", fake_time)
    monkeypatch.setattr(ratelimit, "time", fake_time)
    client = fhttp.HttpClient(ratelimit.HostRateLimiter({"api.openalex.org": {"rps": 10, "burst": 10}}))
    gate = RequestGate(pacer=HostPacer(monotonic=clock.monotonic, sleep=clock.sleep), sleep=clock.sleep,
                       transport=fake.get, provider_gate=True)
    with gate.active():
        r = client.get(url, timeout=5)
    assert r.status == 429 and fake.urls() == [url]  # one request; the retries meet the cooldown
    assert sum(clock.sleeps) < 5, clock.sleeps
    lims = gate.take()
    assert len(lims) == 3 and all(x.retry_after_s is not None and x.retry_after_s > 49_000 for x in lims)


def test_a_429_asking_past_the_cap_stands_at_once(tmp_path: Path) -> None:
    """D588: a content host's 429 whose ``Retry-After`` (900 s) is over ``LIMIT_WAIT_MAX_S`` (120 s)
    is not retried: one request, no sleep; the role is ``failed_transient[rate_limit]`` with the
    900 s hint, as before. Before: four requests 2/4/8 s apart, each asked to wait 15 min."""
    fake, clock = FakeHttp(), Clock()
    fake.add("https://pub.test/a.pdf", status=429, headers={"Retry-After": "900"})
    env = oa_env(tmp_path, fake, clock)
    out = fetch(over_http((PDF, "https://pub.test/a.pdf", "unpaywall")), env)
    assert (out.status, out.error_class, out.halt_run) == ("failed_transient", "rate_limit", False)
    assert out.retry_after_hint == NOW + dt.timedelta(seconds=900)
    assert fake.urls().count("https://pub.test/a.pdf") == 1 and [s for s in clock.sleeps if s >= 2] == []
    assert env.counts.get("oa.http.retries", 0) == 0


def test_the_gate_is_only_active_inside_its_block(tmp_path: Path) -> None:
    """Outside :meth:`RequestGate.active` ``requests`` is untouched (the adapter is restored)."""
    from requests.adapters import HTTPAdapter

    from stack.http import HostPacer
    from stack.oa_adapter import RequestGate

    before = HTTPAdapter.send
    clock = Clock()
    gate = RequestGate(pacer=HostPacer(monotonic=clock.monotonic, sleep=clock.sleep), sleep=clock.sleep,
                       transport=FakeHttp().get)
    with gate.active():
        assert HTTPAdapter.send is not before
    assert HTTPAdapter.send is before


def test_the_pacer_takes_fetchpdfs_api_rates(tmp_path: Path) -> None:
    """The pinned ladder's ``rate_limits`` (unkeyed ``rps``) seed the shared pacer for hosts it has
    no rate for; a rate already set (a channel's politeness) is kept."""
    from stack.http import HostPacer

    clock = Clock()
    pacer = HostPacer(monotonic=clock.monotonic, sleep=clock.sleep)
    pacer.set_rate("www.ebi.ac.uk", 0.5)
    assert oa.seed_rates(pacer, str(oa._DEFAULT_LADDER)) > 10 and oa.seed_rates(pacer, "") == 0
    pacer.acquire("api.unpaywall.org")
    pacer.acquire("api.unpaywall.org")
    pacer.acquire("www.ebi.ac.uk")
    pacer.acquire("www.ebi.ac.uk")
    assert clock.sleeps == [pytest.approx(0.2), pytest.approx(2.0)]  # unpaywall 5 rps; ebi kept at 0.5



# ═════════════════════════════════════════════════════════════════════════════
# T3.x-f4: the direct steps, the run cache, negative results, priming
# ═════════════════════════════════════════════════════════════════════════════

MAILTO = "lane-tests@example.invalid"


def direct_env(tmp_path: Path, web: Any, clock: Clock | None = None, **options: Any) -> LaneEnv:
    clock = clock or Clock()
    acq = SimpleNamespace(wants={"openalex": ["full_text", "xml"], "semantic_scholar": ["full_text"]})
    env = LaneEnv(run_id="r1", now=NOW, blobs=open_blobs(tmp_path), transport=web, mailto=MAILTO,
                  sleep=clock.sleep, acquire=acq, options={"oa_route": "direct", **options})
    env.lane_state["http"] = HttpSession(web.get, sleep=clock.sleep, monotonic=clock.monotonic)
    return env


def req(doi: str, role: str = "full_text", sid: str = "s1", attempts: int = 0,
        channel: str = "openalex") -> FetchRequest:
    member = {"source_id": sid, "channel": channel, "key_kind": "doi", "key_value": doi}
    return FetchRequest(doc_id="d" + sid, source_id=sid, content_role=role, channel=channel, channel_group="paper",
                        member=member, role={"attempts": attempts}, now=NOW, lane="oa")


class PmcFetchpdf(FakeFetchpdf):
    """:class:`FakeFetchpdf` with fetchpdf's resolver priming and cache: ``pmcids`` is what the
    ID Converter knows; ``prime`` records each batch and fills the cache."""

    def __init__(self, pmcids: dict[str, str] | None = None, **kw: Any) -> None:
        super().__init__(**kw)
        self.pmcids, self.cache, self.primes = dict(pmcids or {}), {}, []

    def loader(self, scratch: str, email: str) -> tuple[Any, Any]:
        chain, session = super().loader(scratch, email)
        session.prime = self.prime
        session.lookup = self.cache.get
        return chain, session

    def prime(self, ids: Any) -> int:
        self.primes.append(list(ids))
        for i in ids:
            self.cache[i] = {"pmcid": self.pmcids[i]} if i in self.pmcids else {"_idconv": "invalid article id"}
        return 1


def test_zenodo_doi_is_read_from_the_record_api(tmp_path: Path) -> None:
    """A Zenodo DOI: the record API, then its PDF — two requests, fetchpdf never called; the
    ``xml`` role ends without a request (Zenodo has no JATS route)."""
    world = OaWorld()
    p = world.add("10.5281/zenodo.23014618", "zenodo")
    env, fp = direct_env(tmp_path, world), FakeFetchpdf()
    lane = OaLane(load=fp.loader)
    out = lane.fetch(req(p.doi), env)
    assert (out.status, out.snapshots[0]["media_type"]) == ("ok", "application/pdf")
    assert world.calls == ["https://zenodo.org/api/records/23014618",
                           "https://zenodo.org/api/records/23014618/files/paper.pdf/content"]
    xml = lane.fetch(req(p.doi, "xml"), env)
    assert (xml.status, xml.error_class) == ("failed_permanent", "no_fetchable_url") and "zenodo" in (xml.error or "")
    assert len(world.calls) == 2 and fp.calls == [] and env.counts["oa.source.zenodo_api"] == 1


@pytest.mark.parametrize(("record", "why"), [
    ({"metadata": {"access_right": "restricted"}, "files": []}, "access_right restricted"),
    ({"access": {"files": "restricted"}, "files": {"entries": {}}}, "files restricted"),
    ({"access": {"files": "public"}, "files": {"entries": {"data.zip": {"key": "data.zip", "size": 9,
                                                                      "links": {"content": "https://z/x"}}}}},
     "no PDF file"),
])
def test_a_zenodo_record_without_an_open_pdf_ends_without_fetchpdf(tmp_path: Path, record: dict[str, Any],
                                                                     why: str) -> None:
    fake = FakeHttp()
    fake.add("https://zenodo.org/api/records/7", json=record)
    env, fp = direct_env(tmp_path, fake), FakeFetchpdf()
    out = OaLane(load=fp.loader).fetch(req("10.5281/zenodo.7"), env)
    assert (out.status, out.error_class) == ("failed_permanent", "no_fetchable_url") and why in (out.error or "")
    assert fake.urls() == ["https://zenodo.org/api/records/7"] and fp.calls == []


def test_zenodo_files_read_both_serialisations() -> None:
    legacy = {"files": [{"key": "s.pdf", "size": 5, "links": {"self": "https://z/s"}},
                        {"key": "paper.PDF", "size": 50, "links": {"self": "https://z/p"}},
                        {"key": "code.zip", "size": 500, "links": {"self": "https://z/c"}}]}
    rdm = {"files": {"entries": {"paper.pdf": {"key": "paper.pdf", "size": 7,
                                               "links": {"self": "https://z/meta", "content": "https://z/bytes"}}}}}
    assert oa_lane.zenodo_files(legacy) == [(50, "https://z/p"), (5, "https://z/s")]
    assert oa_lane.zenodo_files(rdm) == [(7, "https://z/bytes")]


def test_mdpi_doi_follows_the_redirect_to_the_pdf(tmp_path: Path) -> None:
    """An MDPI DOI: the DOI resolver's redirect (not followed) names the article page; ``/pdf`` is
    the PDF. Refused with 403 (MDPI's answer to plain clients): ``failed_transient[forbidden]``
    with no fetchpdf walk, ``failed_permanent[blocked]`` at the attempt that reaches the cap."""
    world = OaWorld()
    p = world.add("10.3390/su18199836", "mdpi")
    env, fp = direct_env(tmp_path / "a", world), FakeFetchpdf()
    out = OaLane(load=fp.loader).fetch(req(p.doi), env)
    assert out.status == "ok" and world.calls == [f"https://doi.org/{p.doi}", f"{p.landing}/pdf"]

    blocked = OaWorld(blocked_hosts=frozenset({"www.mdpi.com"}))
    blocked.add(p.doi, "mdpi")
    for attempts, want in ((0, ("failed_transient", "forbidden")), (3, ("failed_permanent", "blocked"))):
        env = direct_env(tmp_path / f"b{attempts}", blocked)
        out = OaLane(load=fp.loader).fetch(req(p.doi, attempts=attempts), env)
        assert (out.status, out.error_class) == want
    assert fp.calls == []


DISTILL_DOI = "10.23915/distill.00010"
DISTILL_PAGE = "https://distill.pub/2018/building-blocks/"


def distill_page(doi: str = DISTILL_DOI) -> bytes:
    return (b"<!doctype html><html><head><title>The Building Blocks of Interpretability</title></head><body>"
            b"<h1>The Building Blocks of Interpretability</h1><p>Interpretability techniques are normally studied "
            b"in isolation.</p><dl><dt>DOI</dt><dd>" + doi.encode() + b"</dd></dl></body></html>")


def distill_web(page: bytes | None = None) -> FakeHttp:
    """doi.org names the article (an old record by http:, distill.pub adds the trailing slash)."""
    fake = FakeHttp()
    fake.add(f"https://doi.org/{DISTILL_DOI}", status=302,
             headers={"Location": "http://distill.pub/2018/building-blocks"})
    fake.add("http://distill.pub/2018/building-blocks", status=301, headers={"Location": DISTILL_PAGE})
    fake.add(DISTILL_PAGE, body=distill_page() if page is None else page,
             headers={"Content-Type": "text/html; charset=utf-8"})
    return fake


def test_a_distill_doi_is_fetched_as_its_html_article(tmp_path: Path) -> None:
    """A Distill DOI (``10.23915``): Distill publishes HTML only, and the walk finds no PDF of the
    article (or a cited paper's arXiv PDF). The DOI resolver's redirect names the page; the page
    that names the DOI is ``full_text`` as ``text/html``; no Unpaywall request, no walk; the ``xml``
    role ends without a request."""
    fake = distill_web()
    env, fp = direct_env(tmp_path, fake), FakeFetchpdf()
    lane = OaLane(load=fp.loader)
    out = lane.fetch(req(DISTILL_DOI), env)
    assert (out.status, out.snapshots[0]["media_type"], out.snapshots[0]["url"]) == ("ok", "text/html", DISTILL_PAGE)
    assert fake.urls() == [f"https://doi.org/{DISTILL_DOI}", "http://distill.pub/2018/building-blocks", DISTILL_PAGE]
    assert env.counts["oa.source.html_article"] == 1 and fp.calls == []
    xml = lane.fetch(req(DISTILL_DOI, "xml"), env)
    assert (xml.status, xml.error_class) == ("failed_permanent", "no_fetchable_url") and "html" in (xml.error or "")
    assert len(fake.calls) == 3 and oa_lane.route_of(identifiers_of(req(DISTILL_DOI).member)) == "html"


def test_a_distill_doi_the_resolver_does_not_know_ends_without_the_walk(tmp_path: Path) -> None:
    """doi.org answers 404 (a Circuits article whose DOI was never registered): a negative answer,
    ``failed_permanent[no_fetchable_url]`` with no walk (it would only find a cited paper's PDF)."""
    fake = FakeHttp()
    fake.add(f"https://doi.org/{DISTILL_DOI}", status=404)
    env, fp = direct_env(tmp_path, fake), FakeFetchpdf()
    out = OaLane(load=fp.loader).fetch(req(DISTILL_DOI), env)
    assert (out.status, out.error_class) == ("failed_permanent", "no_fetchable_url") and "404" in (out.error or "")
    assert fake.urls() == [f"https://doi.org/{DISTILL_DOI}"] and fp.calls == []


def test_a_page_that_does_not_name_the_doi_is_not_taken(tmp_path: Path) -> None:
    """The resolver's page does not name the DOI (moved, an index page): inconclusive, so the
    route goes on as for any DOI (Unpaywall, then the walk)."""
    fake = distill_web(page=distill_page("10.23915/distill.00099"))
    fake.add(f"https://api.unpaywall.org/v2/{DISTILL_DOI}?email=lane-tests%40example.invalid", status=404)
    env, fp = direct_env(tmp_path, fake), FakeFetchpdf()
    out = OaLane(load=fp.loader).fetch(req(DISTILL_DOI), env)
    assert (out.status, out.error_class) == ("failed_permanent", "no_fetchable_url")
    assert "does not name the DOI" in (out.error or "") and len(fp.calls) == 1


def test_an_unpaywall_pdf_is_the_first_usable_artifact(tmp_path: Path) -> None:
    world = OaWorld()
    p = world.add("10.2000/hit.1", "hit")
    env, fp = direct_env(tmp_path, world), FakeFetchpdf()
    out = OaLane(load=fp.loader).fetch(req(p.doi), env)
    assert out.status == "ok" and out.snapshots[0]["url"] == p.pdf_url
    assert world.calls == [f"https://api.unpaywall.org/v2/{p.doi}?email=lane-tests%40example.invalid", p.pdf_url]
    assert fp.calls == [] and env.counts["oa.source.unpaywall"] == 1


def test_a_pdf_of_another_article_is_refused_and_the_walk_tries(tmp_path: Path) -> None:
    """fetchpdf's identity check: an OA location whose PDF names a different DOI is not taken;
    the step is inconclusive, so the walk runs."""
    pytest.importorskip("fetchpdf")
    p = OaWorld().add("10.2000/hit.2", "hit")
    other = pdf_bytes("A different paper doi:10.9999/other.1")  # the OA location serves another article
    fake = FakeHttp()
    fake.add(f"https://api.unpaywall.org/v2/{p.doi}?email=lane-tests%40example.invalid",
             json={"is_oa": True, "title": p.title, "best_oa_location": {"url_for_pdf": "https://pub.test/x.pdf"}})
    fake.add("https://pub.test/x.pdf", body=other, headers={"Content-Type": "application/pdf"})
    env, fp = direct_env(tmp_path, fake), FakeFetchpdf()
    out = OaLane(load=fp.loader).fetch(req(p.doi), env)
    assert (out.status, out.error_class) == ("failed_permanent", "no_fetchable_url")
    assert len(fp.calls) == 1 and "does not contain the requested DOI" in (out.error or "")


def test_no_oa_location_ends_the_role_without_the_walk_unless_asked(tmp_path: Path) -> None:
    """Unpaywall knows the DOI and has no OA location: one request, ``failed_permanent`` naming it,
    no walk. ``oa_route: walk`` (v3's behaviour) walks instead and asks nothing first."""
    world = OaWorld()
    p = world.add("10.1000/miss.1", "miss")
    env, fp = direct_env(tmp_path / "a", world), FakeFetchpdf()
    out = OaLane(load=fp.loader).fetch(req(p.doi), env)
    assert (out.status, out.error_class) == ("failed_permanent", "no_fetchable_url")
    assert "Unpaywall: no open-access location" in (out.error or "") and len(world.calls) == 1 and fp.calls == []
    env = direct_env(tmp_path / "b", world, oa_route="walk")
    OaLane(load=fp.loader).fetch(req(p.doi), env)
    assert len(fp.calls) == 1 and fp.calls[0]["get_xml_or_html"] is True and len(world.calls) == 1


def test_a_doi_unpaywall_does_not_know_falls_back_to_the_walk(tmp_path: Path) -> None:
    world = OaWorld()
    p = world.add("10.3000/unlisted.1", "unlisted")
    env, fp = direct_env(tmp_path, world), FakeFetchpdf()
    out = OaLane(load=fp.loader).fetch(req(p.doi), env)
    assert out.status == "failed_permanent" and [c["raw"] for c in fp.calls] == [p.doi]
    assert fp.calls[0]["get_xml_or_html"] is True and "does not know" in (out.error or "")


def test_repeated_requests_are_answered_from_the_run_cache(tmp_path: Path) -> None:
    """The same metadata question twice (contact parameters differ, as fetchpdf's resolver and its
    legacy chain send them): one request; a PDF and a 429 are never kept."""
    from stack.http import HostPacer
    from stack.oa_adapter import RequestGate

    clock, fake = Clock(), FakeHttp()
    fake.add(f"https://api.unpaywall.org/v2/{DOI}?email=a%40b", json={"is_oa": False})
    fake.add(f"https://api.unpaywall.org/v2/{DOI}?email=a@b", json={"is_oa": False})
    fake.add("https://pub.test/a.pdf", body=PDF, headers={"Content-Type": "application/pdf"})
    fake.add("https://pub.test/busy", status=200, body=b"ok")
    fake.inject("https://pub.test/busy", status=429, times=1)
    counts: dict[str, int] = {}
    gate = RequestGate(pacer=HostPacer(monotonic=clock.monotonic, sleep=clock.sleep), sleep=clock.sleep,
                       transport=fake.get, cache={}, count=lambda k, n: counts.__setitem__(k, counts.get(k, 0) + n))
    import requests

    with gate.active():
        a = requests.get(f"https://api.unpaywall.org/v2/{DOI}?email=a%40b", timeout=5)
        b = requests.get(f"https://api.unpaywall.org/v2/{DOI}", params={"email": "a@b"}, timeout=5)
        requests.get("https://pub.test/a.pdf", timeout=5)
        requests.get("https://pub.test/a.pdf", timeout=5)
        assert requests.get("https://pub.test/busy", timeout=5).status_code == 200
        assert requests.get("https://pub.test/busy", timeout=5).status_code == 200
    assert a.json() == b.json() == {"is_oa": False}
    assert fake.urls().count("https://pub.test/a.pdf") == 2 and fake.urls().count("https://pub.test/busy") == 2
    assert len([u for u in fake.urls() if "unpaywall" in u]) == 1 and counts["cached"] == 2


def test_crossref_and_unpaywall_are_never_asked_about_a_datacite_doi(tmp_path: Path) -> None:
    """A figshare DOI (DataCite): no Unpaywall step; inside the walk, fetchpdf's Crossref and
    Unpaywall requests are answered 404 by the gate without a request (``oa.http.skipped``)."""
    doi = "10.6084/m9.figshare.123"
    fake = FakeHttp()
    fake.add("https://api.datacite.org/dois/" + doi, json={"data": {}})
    lookups = (f"https://api.crossref.org/works/{doi}", f"https://api.unpaywall.org/v2/{doi}?email=x",
               "https://api.datacite.org/dois/" + doi)
    fp = FakeFetchpdf({}, over_http=True, lookups={doi: list(lookups)})
    env = direct_env(tmp_path, fake)
    out = OaLane(load=fp.loader).fetch(req(doi), env)
    assert out.status == "failed_permanent" and len(fp.calls) == 1
    assert fake.urls() == ["https://api.datacite.org/dois/" + doi] and env.counts["oa.http.skipped"] == 2


def test_xml_is_asked_only_for_a_paper_in_pubmed_central(tmp_path: Path) -> None:
    """The ``xml`` role without a walk: a structured-only walk for a DOI the ID Converter maps to a
    PMCID (or a bioRxiv DOI); for any other DOI no request and no walk."""
    world = OaWorld()
    pmc, other, biorxiv = (world.add(d, "miss") for d in ("10.1000/miss.pmc", "10.1000/miss.no", "10.1101/2024.1"))
    fp = PmcFetchpdf({pmc.doi: "PMC42"})
    env = direct_env(tmp_path, world)
    lane = OaLane(load=fp.loader)
    for p, walked in ((pmc, True), (other, False), (biorxiv, True)):
        n = len(fp.calls)
        out = lane.fetch(req(p.doi, "xml", sid=p.doi), env)
        assert out.status == "failed_permanent" and (len(fp.calls) == n + 1) is walked
        if walked:
            assert fp.calls[-1]["xml_only"] is True and "get_xml_or_html" not in fp.calls[-1]
        else:
            assert "not in PubMed Central" in (out.error or "")
    assert world.calls == [] and fp.primes == [[pmc.doi], [other.doi], [biorxiv.doi]]


def test_prepare_primes_the_runs_identifiers_once(tmp_path: Path) -> None:
    """``prepare`` (the skeleton's hook, before the first call): one priming batch of the run's
    DOIs, the prefix-routed (Zenodo, MDPI) left out; the ``xml`` steps then need no lookup."""
    world = OaWorld()
    dois = ["10.1000/miss.a", "10.1000/miss.b", "10.5281/zenodo.9", "10.3390/x1"]
    for d in dois:
        world.add(d, "miss" if d.startswith("10.1000") else ("zenodo" if "zenodo" in d else "mdpi"))
    fp = PmcFetchpdf()
    env = direct_env(tmp_path, world)
    lane = OaLane(load=fp.loader)
    lane.prepare([req(d, r, sid=d) for d in dois for r in ("full_text", "xml")], env)
    assert fp.primes == [["10.1000/miss.a", "10.1000/miss.b"]] and env.counts["oa.primed"] == 1
    lane.fetch(req("10.1000/miss.a", "xml", sid="10.1000/miss.a"), env)
    assert len(fp.primes) == 1


def test_a_negative_answer_is_reused_within_the_run(tmp_path: Path) -> None:
    world = OaWorld()
    p = world.add("10.1000/miss.2", "miss")
    env, fp = direct_env(tmp_path, world), FakeFetchpdf()
    lane = OaLane(load=fp.loader)
    first, second = lane.fetch(req(p.doi, sid="s1"), env), lane.fetch(req(p.doi, sid="s2"), env)
    assert first.status == second.status == "failed_permanent" and len(world.calls) == 1
    assert env.counts["oa.negative_reused"] == 1 and env.counts["oa.step.unpaywall"] == 1


@pytest.mark.slow
def test_replay_of_the_broader_runs_mix_needs_few_requests(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The real fetchpdf over :class:`~tests.fakes.oa_world.OaWorld` with T3.6's broader-run mix
    (80 retrievals: 31 Zenodo, 4 MDPI, 27 without an OA copy, 18 with one; ``zenodo.org`` and
    ``www.mdpi.com`` refusing, which reproduced T3.6's 21 requests per retrieval and its 18 PDFs
    on the code before T3.x-f4), plus 5 DOIs Unpaywall does not know (walked): the lane's
    requests per retrieval, priming included, stay under 2."""
    pytest.importorskip("fetchpdf")
    import time

    monkeypatch.setattr(time, "sleep", lambda s: None)  # fetchpdf's own limiter: counted, not waited
    world = OaWorld(blocked_hosts=frozenset({"zenodo.org", "www.mdpi.com"}))
    mix = b1_mix(world, {**B1_MIX, "unlisted": 5})
    env = direct_env(tmp_path, world)
    lane = OaLane(load=oa_lane._load_without_session_gate)
    reqs = [req(p.doi, r, sid=f"s{i}", channel=ch) for i, (p, ch) in enumerate(mix)
            for r in env.acquire.wants[ch]]
    lane.prepare(reqs, env)
    outs = [(r.member["key_value"], r.content_role, lane.fetch(r, env)) for r in reqs]
    found = sum(1 for _, role, o in outs if role == "full_text" and o.status == "ok")
    per = env.counts["oa.http.requests"] / len(mix)
    print(f"replay: {env.counts['oa.http.requests']} requests / {len(mix)} retrievals = {per:.2f}; "
          f"walks {env.counts.get('oa.walks', 0)}; cached {env.counts.get('oa.http.cached', 0)}; full_text ok {found}")
    assert env.counts["oa.retrievals"] == len(mix) == 85 and found == 18
    assert env.counts["oa.walks"] == 5 and per < 2  # measured 1.80 (153 / 85); before T3.x-f4: 21.2
    assert env.counts["oa.http.cached"] == 50  # 10 of each walk's 20 requests repeated an earlier one
    assert {o.error_class for d, _, o in outs if "zenodo" in d and o.status != "ok"} <= {"forbidden", "no_fetchable_url"}


def test_vor_first_orders_the_unpaywall_candidates() -> None:
    """An Article-in-Press URL goes last, after the version-of-record URL derived from it (an extra
    candidate); other URLs keep their order between (round 4, P)."""
    from workflows.acquire.lanes.oa import vor_first

    aip = "https://www.nature.com/articles/s41598-026-72982-1_reference.pdf"
    vor = "https://www.nature.com/articles/s41598-026-72982-1.pdf"
    repo = "https://europepmc.test/pdf/1"
    assert vor_first([aip, repo]) == ([vor, repo, aip], 1)
    assert vor_first([vor, aip]) == ([vor, aip], 0)  # already listed: not derived again
    assert vor_first([repo, "https://journal.test/1.pdf"]) == ([repo, "https://journal.test/1.pdf"], 0)


# ═════════════════════════════════════════════════════════════════════════════
# T-R6 OA: the gate under several threads (the OA lane's slots)
# ═════════════════════════════════════════════════════════════════════════════
def test_the_gate_serves_several_threads_and_leaves_others_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """The run's one gate active in two threads at once (two OA slots) and a third thread with
    none: both slots' requests pass the gate, a 429 is recorded for the thread that met it alone
    (the other slot's member is not rate-limited by it), the third thread's request goes straight
    to ``requests`` (here a stub adapter), and the adapter is restored once the last block has
    exited, whichever thread opened it first."""
    import threading

    import requests
    from requests.adapters import HTTPAdapter

    from stack.http import HostPacer
    from stack.oa_adapter import RequestGate

    plain: list[str] = []

    def stub(adapter: Any, request: Any, **kw: Any) -> Any:
        plain.append(str(request.url))
        return oa._response(request, 200, b"plain", {}, str(request.url))

    monkeypatch.setattr(HTTPAdapter, "send", stub)
    fake = FakeHttp()
    fake.add("https://a.test/1", body=b"a")
    fake.add("https://b.test/1", status=429)
    counts: dict[str, int] = {}
    gate = RequestGate(pacer=HostPacer(monotonic=Clock().monotonic, sleep=lambda s: None), sleep=lambda s: None,
                       transport=fake.get, count=lambda k, n: counts.__setitem__(k, counts.get(k, 0) + n))
    inside, done = threading.Barrier(3, timeout=10), threading.Event()
    recorded, a_took = threading.Event(), threading.Event()
    seen: dict[str, Any] = {}

    def slot_a() -> None:
        with gate.active():
            inside.wait()
            seen["a"] = requests.get("https://a.test/1", timeout=5).content
            recorded.wait(10)  # slot b's 429 is on record by now
            seen["a_take"] = gate.take()
            a_took.set()
            done.wait(10)

    def slot_b() -> None:
        with gate.active():
            inside.wait()
            seen["b"] = requests.get("https://b.test/1", timeout=5).status_code
            recorded.set()
            a_took.wait(10)  # slot a has asked for its own 429s first
            seen["b_take"] = gate.take()
        done.wait(10)

    threads = [threading.Thread(target=slot_a), threading.Thread(target=slot_b)]
    for t in threads:
        t.start()
    inside.wait()
    seen["c"] = requests.get("https://c.test/1", timeout=5).content  # this thread: no gate
    done.set()
    for t in threads:
        t.join()
    assert seen["a"] == b"a" and seen["b"] == 429 and seen["c"] == b"plain" and plain == ["https://c.test/1"]
    assert counts["requests"] == 5 and counts["http_429"] == 4  # a once; b with its three retries
    assert [x.host for x in seen["b_take"]] == ["b.test"] and seen["a_take"] == []
    assert HTTPAdapter.send is stub


def test_the_gate_reads_a_streamed_body_under_the_hosts_lock() -> None:
    """A streamed response (fetchpdf's PDF download) is read inside the host's in-flight lock and
    replayed to the caller; a body that runs past ``max_bytes`` is cut at ``max_bytes + 1`` (the
    caller's cap refuses it); one declared over the cap is left unread for the caller to refuse."""
    import io

    import requests
    from urllib3.response import HTTPResponse

    from stack.http import HostPacer
    from stack.oa_adapter import RequestGate

    pacer = HostPacer(monotonic=time_monotonic(), sleep=lambda s: None)
    held: list[bool] = []

    class Body(io.BytesIO):
        def read(self, *a: Any, **k: Any) -> bytes:
            held.append(pacer._holds["pub.test"].locked())
            return super().read(*a, **k)

    def streamed(data: bytes, headers: dict[str, str] | None = None) -> Any:
        resp = requests.Response()
        resp.status_code, resp.url = 200, "https://pub.test/a.pdf"
        resp.raw = HTTPResponse(body=Body(data), preload_content=False, headers=headers or {}, status=200)
        resp.headers = requests.structures.CaseInsensitiveDict(headers or {})
        return resp

    gate = RequestGate(pacer=pacer, sleep=lambda s: None, provider_gate=False, max_bytes=1000)
    request = requests.Request("GET", "https://pub.test/a.pdf").prepare()
    ok = gate.send(request, lambda: streamed(b"%PDF" + b"x" * 500), {"stream": True})
    assert held and all(held) and b"".join(ok.iter_content(64)) == b"%PDF" + b"x" * 500
    held.clear()
    big = gate.send(request, lambda: streamed(b"y" * 5000), {"stream": True})
    assert all(held) and len(big.content) == 1001
    held.clear()
    declared = gate.send(request, lambda: streamed(b"z" * 5000, {"Content-Length": "5000"}), {"stream": True})
    assert held == [] and declared._content is False  # unread: the caller refuses it by its length


def time_monotonic() -> Any:
    import time

    return time.monotonic


# ═════════════════════════════════════════════════════════════════════════════
# D680: the one SSRF check is the URL each of the lane's own step requests starts at
# ═════════════════════════════════════════════════════════════════════════════
def unpaywall_with(fake: FakeHttp, doi: str, title: str, *urls: str) -> None:
    fake.add(f"https://api.unpaywall.org/v2/{doi}?email=lane-tests%40example.invalid",
             json={"is_oa": True, "title": title, "best_oa_location": {"url_for_pdf": urls[0]},
                   "oa_locations": [{"url_for_pdf": u} for u in urls[1:]]})


@pytest.mark.parametrize("interior", ["http://127.0.0.1/a.pdf", "http://169.254.169.254/latest/meta-data/",
                                      "http://10.0.0.5/a.pdf", "http://[::1]/a.pdf"])
def test_an_interior_oa_location_is_never_requested(tmp_path: Path, interior: str) -> None:
    """A provider record (Unpaywall's OA location) naming an interior URL: the step's request for it
    is refused before it leaves (``OaLane._get``), the walk still runs, and with no copy anywhere
    the role ends ``no_fetchable_url`` with the refusal in its reason."""
    doi, title = "10.2000/ssrf.1", "A study of SSRF"
    fake = FakeHttp()
    unpaywall_with(fake, doi, title, interior)
    env, fp = direct_env(tmp_path, fake), FakeFetchpdf()
    out = OaLane(load=fp.loader).fetch(req(doi), env)
    assert (out.status, out.error_class) == ("failed_permanent", "no_fetchable_url")
    assert f"{interior}: refused (url_unsafe:blocked)" in (out.error or "")
    assert len(fp.calls) == 1 and interior not in fake.urls()


def test_an_interior_url_does_not_hide_a_public_copy_or_a_transient(tmp_path: Path) -> None:
    """The refusal is per URL: the next OA location is tried and taken; a 403 on the other file of
    a Zenodo record outranks it (the role may still find its copy on a retry)."""
    doi, title = "10.2000/ssrf.2", "A study of SSRF"
    body = pdf_bytes(f"{title} doi:{doi}")
    fake = FakeHttp()
    unpaywall_with(fake, doi, title, "http://10.0.0.7/a.pdf", "https://pub.test/a.pdf")
    fake.add("https://pub.test/a.pdf", body=body, headers={"Content-Type": "application/pdf"})
    env, fp = direct_env(tmp_path / "a", fake), FakeFetchpdf()
    out = OaLane(load=fp.loader).fetch(req(doi), env)
    assert out.status == "ok" and out.snapshots[0]["url"] == "https://pub.test/a.pdf" and fp.calls == []
    assert "10.0.0.7" not in " ".join(fake.urls())

    fake = FakeHttp()
    fake.add("https://zenodo.org/api/records/77", json={"metadata": {"access_right": "open", "title": title}, "files": [
        {"key": "a.pdf", "size": 9, "links": {"self": "http://10.0.0.7/a.pdf"}},
        {"key": "b.pdf", "size": 5, "links": {"self": "https://pub.test/b.pdf"}}]})
    fake.add("https://pub.test/b.pdf", status=403)
    env = direct_env(tmp_path / "b", fake)
    out = OaLane(load=fp.loader).fetch(req("10.5281/zenodo.77"), env)
    assert (out.status, out.error_class) == ("failed_transient", "forbidden") and fp.calls == []
    assert "10.0.0.7" not in " ".join(fake.urls())


def test_the_gate_checks_no_url_and_the_walk_is_not_checked(tmp_path: Path) -> None:
    """D680: the request gate paces and keys, it does not classify URLs, so a redirect hop or
    fetchpdf's own request to an interior address reaches the transport (here the fake)."""
    import requests

    fake = FakeHttp()
    fake.add("https://pub.test/a.pdf", status=302, headers={"Location": "http://10.1.2.3/b.pdf"})
    fake.add("http://10.1.2.3/b.pdf", body=PDF)
    from stack.http import HostPacer
    from stack.oa_adapter import RequestGate

    gate = RequestGate(pacer=HostPacer(monotonic=Clock().monotonic, sleep=lambda s: None), sleep=lambda s: None,
                       transport=fake.get)
    with gate.active():
        assert requests.get("https://pub.test/a.pdf", timeout=5).content == PDF
    assert fake.urls() == ["https://pub.test/a.pdf", "http://10.1.2.3/b.pdf"]

    fake = FakeHttp()
    fake.add("http://100.64.1.1/a.pdf", body=PDF)
    out = fetch(over_http((PDF, "http://100.64.1.1/a.pdf", "unpaywall")), oa_env(tmp_path, fake, Clock()))
    assert out.status == "ok" and fake.urls() == ["http://100.64.1.1/a.pdf"]


def test_a_redirect_to_a_non_http_scheme_is_inconclusive_not_a_network_transient(tmp_path: Path) -> None:
    """review-2 N1: a file link whose redirect points at ``file:`` dies in ``requests`` as
    ``InvalidSchema`` before any adapter (no egress); that is a bad URL, not a network failure
    retried every run: the step is inconclusive and the role ends ``no_fetchable_url``."""
    title = "A record with a bad link"
    fake = FakeHttp()
    fake.add("https://zenodo.org/api/records/78", json={"metadata": {"access_right": "open", "title": title},
             "files": [{"key": "a.pdf", "size": 9, "links": {"self": "https://pub.test/a.pdf"}}]})
    fake.add("https://pub.test/a.pdf", status=302, headers={"Location": "file:///etc/passwd"})
    env, fp = direct_env(tmp_path, fake), FakeFetchpdf()
    out = OaLane(load=fp.loader).fetch(req("10.5281/zenodo.78"), env)
    assert (out.status, out.error_class) == ("failed_permanent", "no_fetchable_url"), out


def test_the_walks_cooldowns_are_their_own_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """D700: the OA lane's gate sends OpenAlex keyless and S2 with our key (D592), so a cooldown on
    the keyed OpenAlex collectors' credential does not cool the walk's OpenAlex lookup, the keyless
    one does; for S2 the walk shares the cooldown of our key, the collectors' (one key, one allowance)."""
    import requests

    from stack.http import HostPacer
    from stack.oa_adapter import RequestGate
    from stack.provider_transport import credential_id

    monkeypatch.setenv("STACK_PROVIDER_STATE", str(tmp_path / "providers"))
    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "s2-test-key")
    openalex = "https://api.openalex.org/works/doi:" + DOI
    fake, clock = FakeHttp(), Clock()
    fake.add(openalex, body=b"{}")
    fake.add(S2_URL, body=b"{}")

    def gate() -> RequestGate:
        return RequestGate(pacer=HostPacer(monotonic=clock.monotonic, sleep=clock.sleep), sleep=clock.sleep,
                           transport=fake.get, provider_gate=True)

    oa_.Gate(openalex, credential_id(openalex, {"Authorization": "Bearer collector"})).observe(429, {}, b"")
    with gate().active():
        assert requests.get(openalex, timeout=5).status_code == 200  # the collectors' cooldown: not ours
    oa_.Gate(openalex).observe(429, {}, b"")  # keyless
    g = gate()
    with g.active():
        assert requests.get(openalex, timeout=5).status_code == 429
    assert fake.urls().count(openalex) == 1 and len(g.take()) == 1
    oa_.Gate(S2_URL, credential_id(S2_URL, {"x-api-key": "s2-test-key"})).observe(429, {"Retry-After": "30"}, b"")
    with gate().active():
        assert requests.get(S2_URL, timeout=5).status_code == 429  # our key's cooldown holds the walk too
    assert fake.urls().count(S2_URL) == 0
