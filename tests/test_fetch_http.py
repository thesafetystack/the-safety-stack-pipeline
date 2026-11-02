"""Pure cases of the ``http`` lane (``workflows/acquire/lanes/http.py``, ADAPTed from v3's
``fetch.http``) and the transport it is built on (``stack/http.py``): the outcome taxonomy per
status class, the SSRF start-URL check (D680), the retry/backoff policy and per-host pacer (injected clock, no real
sleeps), robots, landing-page → PDF scoring goldens (ported from v1 ``test_doc_fetch.py``), URL
choice, URL-scoped conditional validators, the Wayback switch.

Ported from v3's ``tests/test_fetch_http.py`` (20 of 46: the DB-backed ``FetchEngine`` cases went
with the database; the lane's episodes through the runner are in ``tests/test_acquire_lanes.py``).
v3's host-scheduler case went with the worker pool (the skeleton calls lanes one role at a time);
the URL-scoped validator case replaces it. ``urllib_get``'s totality is checked on ``file://``
only: v3's unresolvable-host half made a DNS query. Offline throughout: the getter is a fake.
"""

from __future__ import annotations

import datetime as dt
import logging
import pathlib
from typing import Any

import pytest

import stack.http as sh
from stack.schemas.vocab import ERROR_CLASSES
from workflows.acquire.lanes import http as fh

REPO = pathlib.Path(__file__).resolve().parent.parent


# ─────────────────────────────────────────────────────────────────────────────
# fakes
# ─────────────────────────────────────────────────────────────────────────────
PDF_BYTES = b"%PDF-1.7\n" + b"x" * 20_000
HTML_BYTES = b"<!doctype html><html><body>a landing page</body></html>"


def _resp(
    status: int | None,
    body: bytes = b"",
    *,
    headers: dict[str, str] | None = None,
    error: str | None = None,
    url: str = "http://example.test",
) -> Any:
    return sh.HttpResponse(
        status,
        {k.lower(): v for k, v in (headers or {}).items()},
        body,
        url,
        error,
    )


class FakeGetter:
    """Maps a URL to a response (or a FIFO queue of responses, one per call, for retry tests).
    Unrouted URLs return 404 — so an absent robots.txt reads as allow-all and an unrouted target
    reads as dead."""

    def __init__(self, routes: dict[str, Any] | None = None) -> None:
        self.routes = routes or {}
        self.calls: list[str] = []

    def __call__(
        self, url: str, *, headers: dict[str, str], timeout: float, max_bytes: int
    ) -> Any:
        self.calls.append(url)
        r = self.routes.get(url)
        if r is None:
            return _resp(404, url=url)
        if isinstance(r, list):
            return r.pop(0) if len(r) > 1 else r[0]
        return r

    def count(self, url: str) -> int:
        return sum(1 for c in self.calls if c == url)


class Clock:
    """Injectable monotonic clock + sleep recorder (advances on sleep — no real waiting)."""

    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


def _session(getter: Any, **kw: Any) -> tuple[fh.HttpSession, Clock]:
    """A session over ``getter`` with an injected clock and no DNS resolver (hermetic); channel
    politeness from the repo's ``channels.yaml``."""
    clock = Clock()
    return fh.HttpSession(getter, sleep=clock.sleep, monotonic=clock.monotonic, resolver=None, **kw), clock


# ═════════════════════════════════════════════════════════════════════════════
# (A) pure unit — classify taxonomy, backoff, pacer
# ═════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize(
    "status, error, expect_outcome, expect_action",
    [
        (200, None, "ok", sh._STOP),
        (304, None, "unchanged", sh._STOP),
        (401, None, "paywalled", sh._STOP),
        (402, None, "paywalled", sh._STOP),
        (403, None, "blocked", sh._STOP),
        (451, None, "blocked", sh._STOP),
        (404, None, "dead", sh._STOP),
        (410, None, "dead", sh._STOP),
        (400, None, "dead", sh._STOP),
        (429, None, "error", sh._RETRY_PENALIZE),
        (500, None, "error", sh._RETRY_BACKOFF),
        (503, None, "error", sh._RETRY_BACKOFF),
        (None, "timeout", "error", sh._RETRY_BACKOFF),
        (None, "connection", "error", sh._RETRY_BACKOFF),
        (None, "dns", "error", sh._STOP),  # transient since R4-2 (round 5)
        (None, "other", "error", sh._STOP),
    ],
)
def test_classify_taxonomy(
    status: int | None, error: str | None, expect_outcome: str, expect_action: str
) -> None:
    assert sh.classify(status, error) == (expect_outcome, expect_action)


def test_every_emit_site_outcome_is_a_valid_enum() -> None:
    """Non-vacuous: every ``error_class`` the lane can emit is in the §2.6 vocabulary of its status
    — the permanent map of every non-``ok`` ladder outcome classify returns (plus ``too_large`` and
    ``no_fetchable_url``, which classify never returns) and every transient class of an exhausted
    retry loop."""
    permanent = {*fh._PERMANENT.values(), "no_fetchable_url"}
    assert permanent <= ERROR_CLASSES["failed_permanent"], permanent - ERROR_CLASSES["failed_permanent"]
    for status in (401, 402, 403, 451, 404, 410, 400):
        assert sh.classify(status, None)[0] in fh._PERMANENT
    transient = {fh.transient_class(r) for r in (
        _resp(429), _resp(500), _resp(503), _resp(None, error="timeout"), _resp(None, error="connection"),
        _resp(None, error="other"), _resp(None, error="dns"), None)}
    assert transient == {"rate_limit", "http_5xx", "timeout", "network"}
    assert transient <= ERROR_CLASSES["failed_transient"]
    for attempts in range(4):  # the 403 rule's two exits
        out = fh.forbidden({"attempts": attempts}, "e", {})
        assert out.error_class in ERROR_CLASSES[out.status], out


def test_403_is_transient_until_the_cap() -> None:
    """An HTTP 403 (bot-block or refusal, indistinguishable on the wire): ``failed_transient
    [forbidden]`` while the attempt it makes is below the fetch cap (``attempts`` counts
    consecutive failures, D107), ``failed_permanent[blocked]`` on the attempt that reaches it."""
    from stack.fetchcycle import FETCH_CAP

    got = [(o.status, o.error_class) for o in (fh.forbidden({"attempts": a}, "x: HTTP 403", {"tried_urls": ["x"]})
                                               for a in range(FETCH_CAP + 1))]
    assert got == [("failed_transient", "forbidden")] * (FETCH_CAP - 1) + [("failed_permanent", "blocked")] * 2
    assert fh.forbidden(None, "e", {}).status == "failed_transient"
    last = fh.forbidden({"attempts": FETCH_CAP - 1}, "x: HTTP 403", {"tried_urls": ["x"]})
    assert last.role_patch == {"tried_urls": ["x"]} and f"attempt {FETCH_CAP} of {FETCH_CAP}" in (last.error or "")


@pytest.mark.parametrize(
    "url, expect",
    [
        ("https://arxiv.org/pdf/2401.1", None),        # ok (resolver=None → allow)
        ("http://example.org/x", None),
        ("file:///etc/passwd", "dead"),                # bad scheme
        ("ftp://host/x", "dead"),
        ("data:text/plain,hi", "dead"),
        ("https://", "dead"),                          # no host
        ("http://127.0.0.1/x", "blocked"),             # loopback literal
        ("http://169.254.169.254/latest/meta-data/", "blocked"),  # cloud metadata literal
        ("http://10.0.0.5/x", "blocked"),              # private literal
        ("http://[::1]/x", "blocked"),                 # ipv6 loopback
        ("http://localhost:8080/x", "blocked"),        # loopback name
        ("http://foo.internal/x", "blocked"),          # interior suffix
        ("http://100.64.0.1/x", "blocked"),            # carrier-grade NAT 100.64/10 (not is_private on 3.12)
        ("http://100.100.100.100/x", "blocked"),       # Tailscale's resolver address, inside 100.64/10
        ("http://100.128.0.1/x", None),                # just above 100.64/10: public
        ("http://[::ffff:100.64.0.1]/x", "blocked"),   # IPv4-mapped CGNAT
        ("http://[fec0::1]/x", "blocked"),             # IPv6 site-local (deprecated; is_global on 3.12)
        ("http://[::ffff:127.0.0.1]/x", "blocked"),    # IPv4-mapped loopback
        ("http://[2002:7f00:1::1]/x", "blocked"),      # 6to4 of 127.0.0.1
        ("http://[64:ff9b::a9fe:a9fe]/x", "blocked"),  # NAT64 of the metadata address
        ("http://[fd7a:115c:a1e0::1]/x", "blocked"),   # IPv6 ULA (Tailscale's range)
        ("http://[2606:4700::1111]/x", None),          # public IPv6
        ("http://[::ffff:8.8.8.8]/x", None),           # IPv4-mapped public
        ("http://127.0.0.1./x", "blocked"),            # a trailing dot: the same host (B1, review-2)
        ("http://localhost./x", "blocked"),
        ("http://foo.internal./x", "blocked"),
        ("http://metadata.google.internal./x", "blocked"),
        ("http://LOCALHOST./x", "blocked"),
    ],
)
def test_check_url_safety(url: str, expect: str | None) -> None:
    assert sh.check_url_safety(url, resolver=None) == expect


@pytest.mark.parametrize("host", ["127.1", "0x7f000001", "2130706433", "0177.0.0.1", "0x7f.0.0.1",
                                  "017700000001", "0", "0x7f.1", "127.0.1"])
def test_numeric_ipv4_spellings_are_blocked_through_the_real_resolver(host: str) -> None:
    """The IPv4 spellings ``inet_aton`` accepts (short, decimal, octal, hex) are not ``ipaddress``
    literals, so they are caught through the resolver: glibc parses them without DNS. Pins that the
    production resolver (``socket.getaddrinfo``) answers them (a resolver answering names only would
    lose them silently)."""
    import socket

    assert sh.check_url_safety(f"http://{host}/x", resolver=socket.getaddrinfo) == "blocked"


def _redirected(url: str, newurl: str, headers: dict[str, str]) -> Any:
    """``_RedirectHandler.redirect_request`` as a pure function: the request it would send next."""
    import email.message
    import urllib.request

    req = urllib.request.Request(url, headers=headers)
    return sh._RedirectHandler().redirect_request(req, None, 302, "Found", email.message.Message(), newurl)


_KEYS = {"Authorization": "Bearer k", "x-api-key": "k", "x-els-apikey": "k", "apikey": "k", "Cookie": "s=k",
         "User-Agent": "ua"}


@pytest.mark.parametrize("newurl", [
    "https://evil.example/works",           # another host
    "http://api.openalex.org/works",        # downgrade to http
    "https://api.openalex.org:8443/works",  # another port
])
def test_urllib_redirect_drops_credentials_off_the_origin(newurl: str) -> None:
    """D649 (review-2 B2): the urllib getter's redirect handler forwards every header but
    ``Content-*`` (stdlib); a hop that changes scheme, host or port now carries no credential header
    (requests' ``rebuild_auth`` rule), other headers stay. Kept by D680 (secret hygiene, not SSRF):
    the handler no longer classifies the hop's target, so no resolver is needed here."""
    new = _redirected("https://api.openalex.org/works", newurl, dict(_KEYS))
    sent = {k.lower() for k in new.headers}
    assert not sent & {"authorization", "x-api-key", "x-els-apikey", "apikey", "cookie"}, new.headers
    assert "user-agent" in sent


@pytest.mark.parametrize("newurl", ["https://api.openalex.org/works?page=2", "https://API.openalex.org:443/w"])
def test_urllib_redirect_keeps_credentials_on_the_same_origin(newurl: str) -> None:
    new = _redirected("https://api.openalex.org/works", newurl, dict(_KEYS))
    assert new.headers.get("Authorization") == "Bearer k" and new.headers.get("X-els-apikey") == "k"


@pytest.fixture()
def two_origins() -> Any:
    """Two local HTTP servers (two ports: two origins). ``/hop`` on the first redirects to ``/land``
    on the target given in the query; every request's headers are recorded per server."""
    import http.server
    import threading

    seen: dict[int, list[tuple[str, dict[str, str]]]] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a: Any) -> None:
            pass

        def do_GET(self) -> None:
            port = self.server.server_address[1]
            seen.setdefault(port, []).append((self.path, {k.lower(): v for k, v in self.headers.items()}))
            if self.path.startswith("/hop?to="):
                self.send_response(302)
                self.send_header("Location", self.path.split("=", 1)[1] + "/land")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"landed")

    servers = [http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler) for _ in range(2)]
    for srv in servers:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield [srv.server_address[1] for srv in servers], seen
    for srv in servers:
        srv.shutdown()


def test_urllib_get_drops_the_key_on_a_real_cross_origin_redirect(two_origins: Any) -> None:
    """D649 kept by D680, end to end through the real opener: a request carrying the OpenAlex
    meter's ``Authorization`` header is redirected to another origin (another port), which gets no
    credential header; a same-origin hop keeps it. The hops go to 127.0.0.1 and are followed: no
    redirect hop is classified any more (D680)."""
    (a, b), seen = two_origins
    key = {"Authorization": "Bearer sk-test", "User-Agent": "ua"}
    cross = sh.urllib_get(f"http://127.0.0.1:{a}/hop?to=http://127.0.0.1:{b}", headers=key, timeout=5,
                          max_bytes=1000)
    assert (cross.status, cross.body, cross.final_url) == (200, b"landed", f"http://127.0.0.1:{b}/land")
    assert seen[a][0][1]["authorization"] == "Bearer sk-test"
    assert [p for p, _ in seen[b]] == ["/land"] and "authorization" not in seen[b][0][1]
    assert seen[b][0][1]["user-agent"] == "ua"
    same = sh.urllib_get(f"http://127.0.0.1:{a}/hop?to=http://127.0.0.1:{a}", headers=key, timeout=5,
                         max_bytes=1000)
    assert same.status == 200 and seen[a][-1] == ("/land", seen[a][-1][1])
    assert seen[a][-1][1]["authorization"] == "Bearer sk-test"


def test_the_standard_proxy_variables_route_urllib_and_requests(two_origins: Any,
                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    """The proxy setting (D680): ``HTTP_PROXY``/``HTTPS_PROXY`` (unset by default) are honoured
    natively by the urllib getter (its ``ProxyHandler``, read when a thread's opener is built) and
    by ``requests`` (the OA lane and fetchpdf). A local server stands in for the proxy: it receives
    the absolute URL of a host that does not exist."""
    import threading

    import requests

    (proxy_port, _), seen = two_origins
    monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{proxy_port}")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    got: list[Any] = []
    thread = threading.Thread(target=lambda: got.append(sh.urllib_get(  # a fresh thread: a fresh opener
        "http://nowhere.invalid/u", headers={}, timeout=5, max_bytes=1000)))
    thread.start()
    thread.join()
    assert got[0].status == 200 and got[0].body == b"landed"
    assert requests.get("http://nowhere.invalid/r", timeout=5).content == b"landed"
    assert [p for p, _ in seen[proxy_port]] == ["http://nowhere.invalid/u", "http://nowhere.invalid/r"]


def test_check_url_safety_resolves_rebinding() -> None:
    """With a resolver, a public NAME that RESOLVES to a private IP is blocked (at check time only:
    the connection makes its own lookup, D680)."""
    def rebind(host: str, port: int, *a: Any) -> list[Any]:
        return [(2, 1, 6, "", ("10.1.2.3", port))]
    assert sh.check_url_safety("https://evil.example.com/x", resolver=rebind) == "blocked"

    def cgnat(host: str, port: int, *a: Any) -> list[Any]:
        return [(2, 1, 6, "", ("100.101.102.103", port))]  # a tailnet address
    assert sh.check_url_safety("https://tailnet.example.com/x", resolver=cgnat) == "blocked"

    def public(host: str, port: int, *a: Any) -> list[Any]:
        return [(2, 1, 6, "", ("93.184.216.34", port))]
    assert sh.check_url_safety("https://example.com/x", resolver=public) is None

    def boom(host: str, port: int, *a: Any) -> list[Any]:
        raise OSError("no DNS in proxied env")
    assert sh.check_url_safety("https://unresolvable.example/x", resolver=boom) is None


def test_urllib_get_is_total_on_bad_scheme_and_host(tmp_path: pathlib.Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    """The default getter NEVER raises — file:// (no handler) becomes a typed transport
    HttpResponse with status=None. (The provider gate's state goes to ``tmp_path``.)"""
    monkeypatch.setenv("STACK_PROVIDER_STATE", str(tmp_path / "providers"))
    r1 = sh.urllib_get("file:///etc/passwd", headers=sh.DEFAULT_HEADERS, timeout=5, max_bytes=1000)
    assert r1.status is None and r1.error is not None


def test_backoff_delay_is_exponential_and_capped() -> None:
    assert [sh.backoff_delay(n) for n in range(6)] == [2.0, 4.0, 8.0, 16.0, 32.0, 60.0]


def test_retry_loop_backoff_schedule() -> None:
    """A persistent 503 retries MAX_HTTP_RETRIES times with exponential backoff, then terminates
    'error' (the lane's ``failed_transient[http_5xx]``). Clock injected — no real sleeps."""
    url = "https://arxiv.org/pdf/2401.00001"
    getter = FakeGetter({url: [_resp(503) for _ in range(sh.MAX_HTTP_RETRIES + 1)]})
    session, clock = _session(getter)
    session.pacer.set_rate("arxiv.org", 1e6)  # isolate backoff from politeness pacing
    outcome, resp, history = session.fetch_url(url, "arxiv")
    assert outcome == "error" and fh.transient_class(resp) == "http_5xx"
    assert getter.count(url) == sh.MAX_HTTP_RETRIES + 1  # 4 attempts
    assert clock.sleeps == [2.0, 4.0, 8.0]               # 3 backoffs between 4 attempts
    assert len(history) == sh.MAX_HTTP_RETRIES + 1


def test_retry_loop_recovers_after_transient() -> None:
    url = "https://arxiv.org/pdf/2401.00001"
    getter = FakeGetter({url: [_resp(503), _resp(200, PDF_BYTES)]})
    session, clock = _session(getter)
    session.pacer.set_rate("arxiv.org", 1e6)  # isolate backoff from politeness pacing
    outcome, resp, _hist = session.fetch_url(url, "arxiv")
    assert outcome is None and resp is not None and resp.status == 200  # 200 → caller interprets
    assert clock.sleeps == [2.0]


def test_429_penalizes_host_rate() -> None:
    """A 429 halves the host's per-run rate (the fabled 429 borrow) and retries."""
    url = "https://x.test/a"
    getter = FakeGetter({url: [_resp(429), _resp(200, PDF_BYTES)]})
    session, clock = _session(getter)
    session.pacer.set_rate("x.test", 4.0)
    session.fetch_url(url, "arxiv")
    assert session.pacer._rps("x.test") == 2.0  # halved once


def test_pacer_spaces_requests_and_penalty_halves_rate() -> None:
    clock = Clock()
    pacer = sh.HostPacer(monotonic=clock.monotonic, sleep=clock.sleep, default_rps=10.0)
    pacer.set_rate("h", 2.0)  # interval 0.5s
    assert pacer.acquire("h") == 0.0        # first hit is free
    assert pacer.acquire("h") == pytest.approx(0.5)  # second waits one interval
    assert pacer.acquire("h") == pytest.approx(0.5)
    pacer.penalize("h")
    assert pacer._rps("h") == 1.0           # 2.0 → 1.0
    assert clock.sleeps == [pytest.approx(0.5), pytest.approx(0.5)]


# ═════════════════════════════════════════════════════════════════════════════
# (B) pure unit — PDF sniffing + landing-page scoring (ported v1 doc_fetch goldens)
# ═════════════════════════════════════════════════════════════════════════════
def test_looks_like_pdf() -> None:
    assert fh.looks_like_pdf(b"%PDF-1.7" + b"x" * 20_000)
    assert not fh.looks_like_pdf(b"<html>not a pdf</html>" + b"x" * 20_000)
    assert not fh.looks_like_pdf(b"%PDF-1.7 too small")  # under PDF_MIN_BYTES


def test_pdf_href_re_finds_links() -> None:
    html = """<a href="/files/claude-4-model-card.pdf">card</a>
              url('/assets/rsp.pdf?v=2') <a href="/about">x</a>"""
    assert [m.group("u") for m in fh.PDF_HREF_RE.finditer(html)] == [
        "/files/claude-4-model-card.pdf",
        "/assets/rsp.pdf?v=2",
    ]


def test_id_tokens_versions_and_dates() -> None:
    toks = fh.id_tokens("systemcard:claude-opus-4-5-v2-0", "2025-11-24")
    for expected in ("opus", "4", "5", "2.0", "v2.0", "2025-11-24", "2025.11.24", "2025-11"):
        assert expected in toks
    assert "claude" not in toks  # plain-word slug tokens dropped


def test_pdf_candidates_and_pick_scoring() -> None:
    html = (
        '<a href="/files/claude-opus-4-5-v2-0.pdf">card</a>'
        '<a href="/files/other-release.pdf">other</a>'
    )
    base = "https://www.anthropic.com/cards/opus"
    cands = fh.pdf_candidates(html, base)
    assert cands == [
        "https://www.anthropic.com/files/claude-opus-4-5-v2-0.pdf",
        "https://www.anthropic.com/files/other-release.pdf",
    ]
    pick, why = fh.pick_pdf(cands, fh.id_tokens("claude-opus-4-5-v2-0", "2025-11-24"))
    assert pick == "https://www.anthropic.com/files/claude-opus-4-5-v2-0.pdf"


def test_pick_pdf_single_and_ambiguous() -> None:
    assert fh.pick_pdf(["http://a/x.pdf"], [])[0] == "http://a/x.pdf"      # single wins
    assert fh.pick_pdf([], [])[0] is None                                  # none
    # two zero-scoring candidates → ambiguous, no pick
    pick, why = fh.pick_pdf(["http://a/1.pdf", "http://a/2.pdf"], ["zzz"])
    assert pick is None and why.startswith("AMBIGUOUS")


def test_choose_url_canonical() -> None:
    """The member's canonical_url, expecting a PDF for .pdf URLs and the card channels. (arXiv's
    constructed URLs are the arxiv lane's — no channel branch is left here.)"""
    def member(channel: str, url: str | None) -> dict[str, Any]:
        return {"channel": channel, "canonical_url": url}
    assert fh.choose_url(member("arxiv", None)) == (None, False)
    assert fh.choose_url(member("lesswrong", "https://lw.test/posts/p1")) == ("https://lw.test/posts/p1", False)
    assert fh.choose_url(member("anthropic_cards", "https://a.test/x")) == ("https://a.test/x", True)
    assert fh.choose_url(member("openai_blog", "https://o.test/r.PDF")) == ("https://o.test/r.PDF", True)
    assert fh.choose_url(member("openalex", "  ")) == (None, False)  # DOI-only, not http-fetchable


# ═════════════════════════════════════════════════════════════════════════════
# (C) the session — robots, pacing, the Wayback switch, validators
# ═════════════════════════════════════════════════════════════════════════════
def test_robots_cache_hit_pays_one_pacing_slot() -> None:
    """A respect_robots channel used to pay TWO pacing intervals per request — one before the
    robots check (even on a cache hit) and one before the GET. Only a real robots.txt fetch
    should cost a slot: two pages on one 2 rps host = robots + 2 GETs = 3 slots, not 5."""
    host = "https://openai.com"
    getter = FakeGetter({
        f"{host}/robots.txt": _resp(200, b"User-agent: *\nAllow: /\n"),
        f"{host}/a": _resp(200, HTML_BYTES),
        f"{host}/b": _resp(200, HTML_BYTES),
    })
    session, clock = _session(getter)
    session.fetch_url(f"{host}/a", "openai_blog")
    session.fetch_url(f"{host}/b", "openai_blog")
    assert getter.calls == [f"{host}/robots.txt", f"{host}/a", f"{host}/b"]
    # 0.5 rps → 2 s slots; three requests → two sleeps of 2 s, never four.
    assert clock.sleeps == [2.0, 2.0]


def test_wayback_rung_can_be_switched_off() -> None:
    """Option ``wayback: false``: a dead page stays dead without the archive.org GET; so does a
    role that already holds a live body (``archive_ok=False``)."""
    getter = FakeGetter({"https://lw.test/gone": _resp(404)})
    on, _ = _session(getter)
    on.ladder("https://lw.test/gone", "lesswrong", None)
    assert any("archive.org" in u for u in getter.calls)
    getter.calls.clear()
    assert on.ladder("https://lw.test/gone", "lesswrong", None, archive_ok=False).outcome == "dead"
    assert not any("archive.org" in u for u in getter.calls)
    off, _ = _session(getter, wayback=False)
    outcome = off.ladder("https://lw.test/gone", "lesswrong", None).outcome
    assert outcome == "dead"
    assert not any("archive.org" in u for u in getter.calls)


WB_API = f"{fh.WAYBACK_API}?url=https%3A%2F%2Flw.test%2Fgone"


def _closest(ts: str) -> Any:
    return _resp(200, (
        '{"archived_snapshots": {"closest": {"status": "200", "available": true, '
        f'"url": "http://web.archive.org/web/{ts}/https://lw.test/gone", "timestamp": "{ts}"}}}}}}'
    ).encode())


def test_wayback_fetches_the_original_bytes() -> None:
    """D589 (Dan Elton's v3 fix): the capture the availability API names is fetched as
    ``web.archive.org/web/<ts>id_/<url>`` (the archived response itself), never the replay page
    the API's ``url`` points at. The capture time is the one in the archive's final URL (it
    redirects ``id_`` to the capture it serves), else the API's."""
    raw = "https://web.archive.org/web/20250301120000id_/https://lw.test/gone"
    replay = "http://web.archive.org/web/20250301120000/https://lw.test/gone"
    served = "https://web.archive.org/web/20250302080000id_/https://lw.test/gone"
    getter = FakeGetter({
        "https://lw.test/gone": _resp(404), WB_API: _closest("20250301120000"),
        replay: _resp(200, b"<html>toolbar + rewritten links</html>"),
        raw: _resp(200, b"<div>original</div>", headers={"Content-Type": "text/html"}, url=served),
    })
    session, _ = _session(getter)
    lad = session.ladder("https://lw.test/gone", "lesswrong", None)
    assert replay not in getter.calls and getter.calls[-1] == raw
    assert (lad.outcome, lad.via, lad.body, lad.final_url) == ("ok", "wayback", b"<div>original</div>", served)
    assert lad.captured == dt.datetime(2025, 3, 2, 8, tzinfo=dt.UTC)
    assert lad.capture is not None and lad.capture.headers["content-type"] == "text/html"
    # No usable timestamp in the API answer: no id_ URL can be formed, no capture.
    getter.routes[WB_API] = _resp(200, b'{"archived_snapshots": {"closest": {"status": "200", "url": "x"}}}')
    assert session.ladder("https://lw.test/gone", "lesswrong", None).outcome == "dead"


@pytest.mark.parametrize("status, backed, asks", [
    (404, False, True), (404, True, True),    # dead: the archive's newest capture
    (403, True, True), (451, True, True),     # blocked
    (503, False, True), (503, True, False),   # error: only while no bytes are held
    (401, False, False),                      # paywalled: never
])
def test_wayback_is_asked_on_blocked_dead_or_an_unbacked_error(status: int, backed: bool, asks: bool) -> None:
    """D589 (Dan Elton's v3 rule): Wayback on ``blocked``/``dead``, or on ``error`` (5xx, 429,
    timeout, connection, DNS after the retries) only when the role holds no raw bytes (``backed``:
    an archived copy counts). ``archive_ok=False`` (a live or headless body held) still rules it
    out entirely (D124)."""
    getter = FakeGetter({"https://lw.test/gone": _resp(status), WB_API: _resp(200, b"{}")})
    session, _ = _session(getter)
    session.ladder("https://lw.test/gone", "lesswrong", None, backed=backed)
    assert (WB_API in getter.calls) is asks
    getter.calls.clear()
    session.ladder("https://lw.test/gone", "lesswrong", None, archive_ok=False, backed=True)
    assert WB_API not in getter.calls


class _Blobs:
    def put(self, body: bytes) -> str:
        import hashlib

        return hashlib.sha256(body).hexdigest()


def test_a_pinned_wayback_capture_is_fetched_as_written_and_minted_as_wayback() -> None:
    """A ``canonical_url`` naming one capture's original bytes (``collect.manual`` keeps a hand-added source's pin,
    stack.urls.wayback_capture) is fetched as written: the snapshot is ``via = 'wayback'`` with the capture time of
    the archive's final URL and the archived Content-Type, no validators; no availability lookup, also when the
    capture fails (the live host, which answers scripted clients with its homepage, is never asked)."""
    from workflows.acquire.lanes import FetchRequest, LaneEnv

    live = "https://www.tc260.org.cn/upload/2024-09-09/1725849192841090989.pdf"
    pin = f"https://web.archive.org/web/20250708033232id_/{live}"
    served = f"https://web.archive.org/web/20250708033233id_/{live}"
    getter = FakeGetter({pin: _resp(200, PDF_BYTES, headers={"Content-Type": "application/pdf", "ETag": '"wb"'},
                                    url=served),
                         live: _resp(200, HTML_BYTES)})
    session, _ = _session(getter)
    now = dt.datetime(2026, 10, 30, tzinfo=dt.UTC)
    env = LaneEnv(run_id="r", now=now, blobs=_Blobs(), lane_state={"http": session})
    req = FetchRequest(doc_id="d1", source_id="s1", content_role="manual_url", channel="manual_url",
                       channel_group="url", member={"canonical_url": pin, "channel": "manual_url"},
                       role={"status": "not_attempted", "attempts": 0}, now=now, lane="http")
    out = fh.LANE.fetch(req, env)
    assert out.status == "ok", out
    (snap,) = out.snapshots
    assert (snap["via"], snap["url"], snap["final_url"], snap["media_type"], snap["etag"]) == (
        "wayback", pin, served, "application/pdf", None)
    assert snap["as_of"] == dt.datetime(2025, 7, 8, 3, 32, 33, tzinfo=dt.UTC)
    assert out.role_patch["validator_url"] is None and out.role_patch["etag"] is None
    assert live not in getter.calls and not any(c.startswith(fh.WAYBACK_API) for c in getter.calls)
    assert env.counts["http.via.wayback"] == 1
    # a dead capture: failed, and the archive is not asked for another one
    getter.routes[pin] = _resp(404, url=pin)
    getter.calls.clear()
    out = fh.LANE.fetch(req, env)
    assert (out.status, out.error_class) == ("failed_permanent", "dead")
    assert not any(c.startswith(fh.WAYBACK_API) for c in getter.calls)


def test_headless_html_with_lone_surrogates_encodes() -> None:
    """D586: a rendered DOM is a JS (UTF-16) string and can hold a lone surrogate; the headless
    rung hands it on with U+FFFD in its place, so the body encodes as UTF-8."""
    session, _ = _session(FakeGetter({}), headless=True, browser_render=lambda url: "<p>a \ud83d b</p>")
    html, how = session._safe_browser("https://lw.test/x")
    assert how == "rendered" and html is not None and html.encode("utf-8") == "<p>a \ufffd b</p>".encode()


def _headless_request(url: str) -> Any:
    from workflows.acquire.lanes import FetchRequest

    return FetchRequest(doc_id="d1", source_id="s1", content_role="page", channel="lesswrong", channel_group="url",
                        member={"canonical_url": url, "channel": "lesswrong"},
                        role={"status": "not_attempted", "attempts": 0},
                        now=dt.datetime(2026, 10, 16, tzinfo=dt.UTC), lane="http")


def _headless_env(session: fh.HttpSession) -> Any:
    from workflows.acquire.lanes import LaneEnv

    return LaneEnv(run_id="r", now=dt.datetime(2026, 10, 16, tzinfo=dt.UTC), blobs=None,
                   lane_state={"http": session})


def test_headless_rung_without_a_browser_is_counted_and_warned_once(
        monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """D590: with no browser, the default rung (stack.browser) warns once per process and the lane
    counts ``http.headless.unavailable`` per role, so the run record shows the rung is dead (the
    old rung swallowed the launch error and returned None: silently dead). The ladder then goes
    on to Wayback as before."""
    import stack.browser as br

    class NoBrowser:
        class chromium:
            @staticmethod
            def launch(**kw: Any) -> Any:
                raise RuntimeError("Executable doesn't exist")

        @staticmethod
        def stop() -> None:
            pass

    monkeypatch.setattr(br, "_warned", set())
    monkeypatch.setattr(br, "_disabled", None)
    monkeypatch.setattr(br, "_RESOLVER", None)
    monkeypatch.setattr(br, "_start_playwright", NoBrowser)
    getter = FakeGetter({"https://lw.test/a": _resp(403), "https://lw.test/b": _resp(503)})
    session, _ = _session(getter, headless=True, wayback=False)
    env = _headless_env(session)
    with caplog.at_level(logging.WARNING, logger="stack.browser"):
        outcomes = [fh.LANE.fetch(_headless_request(u), env) for u in ("https://lw.test/a", "https://lw.test/b")]
    assert [o.status for o in outcomes] == ["failed_transient", "failed_transient"]
    assert env.counts["http.headless.unavailable"] == 2
    assert len([r for r in caplog.records if r.name == "stack.browser"]) == 1


def test_headless_rung_rendered_and_failed_are_counted() -> None:
    pages = {"https://lw.test/a": "<p>rendered</p>", "https://lw.test/b": None}
    getter = FakeGetter({u: _resp(403) for u in pages})
    session, _ = _session(getter, headless=True, wayback=False, browser_render=pages.get)
    env = _headless_env(session)

    class Blobs:
        @staticmethod
        def put(body: bytes) -> str:
            return "sha256:x"

    env.blobs = Blobs()
    ok, bad = (fh.LANE.fetch(_headless_request(u), env) for u in pages)
    assert ok.status == "ok" and ok.snapshots[0]["via"] == "headless" and bad.status == "failed_transient"
    assert env.counts["http.headless.rendered"] == 1 and env.counts["http.headless.failed"] == 1


@pytest.mark.parametrize("url", ["http://127.0.0.1/x", "http://169.254.169.254/latest/meta-data/",
                                 "http://10.1.2.3/x", "http://[::1]/x"])
def test_the_lane_refuses_an_interior_start_url_before_any_request(url: str) -> None:
    """D680: the one SSRF check is the episode's start URL (the member's ``canonical_url``):
    loopback, the cloud metadata address, a private range and IPv6 loopback end
    ``failed_permanent[blocked]`` with no request (robots, live, headless or Wayback)."""
    getter = FakeGetter()
    session, _ = _session(getter, headless=True, browser_render=lambda u: pytest.fail("browser asked"))
    out = fh.LANE.fetch(_headless_request(url), _headless_env(session))
    assert (out.status, out.error_class) == ("failed_permanent", "blocked")
    assert "url_unsafe:blocked" in (out.error or "") and getter.calls == []


def test_the_lane_fetches_a_public_start_url_and_does_not_check_its_redirect() -> None:
    """A public start URL is fetched; where it redirected (an interior host here, as the fake's
    final URL) is not checked (D680: no redirect-hop checks)."""
    getter = FakeGetter({"https://lw.test/p": sh.HttpResponse(200, {"content-type": "text/html"}, HTML_BYTES,
                                                              "http://10.0.0.1/p")})
    session, _ = _session(getter, wayback=False)
    env = _headless_env(session)

    class Blobs:
        @staticmethod
        def put(body: bytes) -> str:
            return "sha256:x"

    env.blobs = Blobs()
    out = fh.LANE.fetch(_headless_request("https://lw.test/p"), env)
    assert out.status == "ok" and getter.calls[-1] == "https://lw.test/p"


def test_conditional_validators_are_url_scoped_and_backed() -> None:
    """Validators replay only for the URL they came from (or a role from before
    ``validator_url``) and only while a live body backs them (v3's finding 8)."""
    role = {"etag": '"e1"', "last_modified": "Mon, 05 Oct 2026 10:00:00 GMT", "validator_url": "https://a.test/p"}
    both = {"If-None-Match": '"e1"', "If-Modified-Since": "Mon, 05 Oct 2026 10:00:00 GMT"}
    assert fh.conditional_headers(role, "https://a.test/p", backed=True) == both
    assert fh.conditional_headers(role, "https://a.test/other", backed=True) == {}
    assert fh.conditional_headers(role, "https://a.test/p", backed=False) == {}
    assert fh.conditional_headers({**role, "validator_url": None}, "https://a.test/x", backed=True) == both
    assert fh.conditional_headers({"etag": None, "last_modified": None}, "https://a.test/p", backed=True) == {}


# ═════════════════════════════════════════════════════════════════════════════
# (H) landing-page → PDF resolution (the citation_pdf_url / href-scoring rungs)
# ═════════════════════════════════════════════════════════════════════════════
def test_citation_pdf_urls_both_attribute_orders() -> None:
    html = (
        '<html><head>'
        '<meta name="citation_pdf_url" content="/files/paper.pdf">'
        '<meta content="https://cdn.test/og.pdf" property="og:pdf">'
        '<meta name="citation_title" content="not a pdf">'
        '</head></html>'
    )
    urls = fh.citation_pdf_urls(html, "https://pub.test/article/1")
    assert urls == ["https://pub.test/files/paper.pdf", "https://cdn.test/og.pdf"]
    assert fh.citation_pdf_urls("<html></html>", "https://pub.test/") == []


# ═════════════════════════════════════════════════════════════════════════════
# (I) limited answers (D588): no retry into our own Gate cooldown; Retry-After up to a cap
# ═════════════════════════════════════════════════════════════════════════════
COOLDOWN = {"Retry-After": "5", "X-Stack-Cooldown": "1"}  # the Gate's synthetic answer: nothing sent


def test_a_cooldown_answer_spends_no_retry() -> None:
    """A Gate cooldown answer (nothing was sent) is waited out (its time left) and is not one of the
    MAX_HTTP_RETRIES retries, nor a history entry (``history`` counts requests sent): one cooldown and
    three 500s still leave the fourth real request, which succeeds. Before D588 the cooldown answer
    took the first attempt and the 200 was never asked for."""
    url = "https://x.test/a"
    getter = FakeGetter({url: [_resp(429, headers=COOLDOWN), _resp(500), _resp(500), _resp(500),
                               _resp(200, PDF_BYTES)]})
    session, clock = _session(getter)
    session.pacer.set_rate("x.test", 1e6)
    outcome, resp, history = session.fetch_url(url, None)
    assert outcome is None and resp is not None and resp.status == 200
    assert clock.sleeps == [5.0, 2.0, 4.0, 8.0]
    assert len(history) == 4 and all(h["status"] != 429 for h in history)
    assert session.pacer._rps("x.test") == 1e6  # a cooldown answer is no penalty (D584)


def test_cooldown_answers_are_waited_at_most_three_times() -> None:
    """``COOLDOWN_WAITS`` (3) cooldown answers are waited out; the fourth ends the call at once as
    ``error`` (the lane's ``rate_limit``), with no request sent at all."""
    url = "https://x.test/a"
    getter = FakeGetter({url: [_resp(429, headers=COOLDOWN)]})
    session, clock = _session(getter)
    session.pacer.set_rate("x.test", 1e6)
    outcome, resp, history = session.fetch_url(url, None)
    assert outcome == "error" and fh.transient_class(resp) == "rate_limit"
    assert getter.count(url) == 4 and clock.sleeps == [5.0, 5.0, 5.0] and history == []


@pytest.mark.parametrize("retry_after, sleeps, asked", [
    ("30", [30.0, 30.0, 30.0], 4),   # each step waits the stated 30 s (90 s in all, under the cap)
    ("50", [50.0, 50.0], 3),          # a third 50 s wait would pass 120 s in all: stands at once
    ("600", [], 1),                   # over the 120 s cap: the 429 stands at once
], ids=["honoured", "cumulative-cap", "over-cap"])
def test_a_stated_retry_after_is_honoured_up_to_the_cap(retry_after: str, sleeps: list[float], asked: int) -> None:
    """A real 429 is retried no sooner than its ``Retry-After`` (not the 2/4/8 s step), and the sleeps
    on limited answers total at most ``LIMIT_WAIT_MAX_S`` (120 s) per call; past it the 429 ends the
    call at once (``error`` → ``rate_limit``). Before D588: 2/4/8 s whatever the answer asked."""
    url = "https://x.test/a"
    getter = FakeGetter({url: [_resp(429, headers={"Retry-After": retry_after})]})
    session, clock = _session(getter)
    session.pacer.set_rate("x.test", 1e6)
    outcome, resp, history = session.fetch_url(url, None)
    assert outcome == "error" and fh.transient_class(resp) == "rate_limit"
    assert clock.sleeps == sleeps and getter.count(url) == asked == len(history)


def test_a_plan_refusal_is_never_retried() -> None:
    """D583 through the acquire session too: a 429 whose body is a plan refusal stands at once."""
    url = "https://x.test/a"
    getter = FakeGetter({url: [_resp(429, b'{"error": "Plan upgrade required", "message": "no"}')]})
    session, clock = _session(getter)
    outcome, _resp429, _h = session.fetch_url(url, None)
    assert outcome == "error" and getter.count(url) == 1 and clock.sleeps == []


@pytest.mark.parametrize("first", [_resp(429), _resp(429, headers=COOLDOWN),
                                   _resp(503, headers={"Retry-After": "30"})],
                         ids=["429", "cooldown", "503-retry-after"])
def test_wait_limits_off_returns_a_limited_answer_at_once(first: Any) -> None:
    """``wait_limits=False`` (the arXiv lane, whose D584 rule waits limited answers out itself): the
    first 429, cooldown answer or 503 that states a wait comes back at once, nothing slept; a bare
    503 keeps the 5xx ladder."""
    url = "https://x.test/a"
    getter = FakeGetter({url: [first, _resp(200, PDF_BYTES)]})
    session, clock = _session(getter)
    session.pacer.set_rate("x.test", 1e6)
    outcome, resp, _h = session.fetch_url(url, None, wait_limits=False)
    assert outcome == "error" and resp is first and getter.count(url) == 1 and clock.sleeps == []
    bare = FakeGetter({url: [_resp(503), _resp(200, PDF_BYTES)]})
    session2, clock2 = _session(bare)
    session2.pacer.set_rate("x.test", 1e6)
    assert session2.fetch_url(url, None, wait_limits=False)[0] is None and clock2.sleeps == [2.0]


class _GatedServer:
    """Real requests that passed the Gate arrive here: ``zenodo.org`` (a Gate family, 2.1 s interval,
    60 s cooldown on a bare 429) answers 429 to its first ``n_429`` requests, then a PDF."""

    def __init__(self, clock: Any, n_429: int) -> None:
        self.clock, self.n_429 = clock, n_429
        self.log: list[tuple[float, int]] = []

    def open(self, req: Any, timeout: float | None = None, **kw: Any) -> Any:
        import io
        import urllib.error
        from email.message import Message

        status = 429 if len(self.log) < self.n_429 else 200
        self.log.append((self.clock.t, status))
        if status == 429:
            raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", Message(), io.BytesIO(b"slow"))
        resp = io.BytesIO(PDF_BYTES)
        resp.status, resp.headers = 200, Message()  # type: ignore[attr-defined]
        resp.geturl = lambda: req.full_url  # type: ignore[attr-defined]
        return resp


def test_a_gate_familys_429_is_waited_out_not_retried_into_its_own_cooldown(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The bug (D588; Dan Elton's v3 fix e9dc435): over the real chain (``urllib_get`` →
    ``throttled_open`` → ``Gate``) on a virtual clock, one bare 429 from a Gate family sets the Gate's
    60 s cooldown; the 2/4/8 s retries all met that cooldown, sent nothing, and the URL failed
    ``rate_limit`` with the provider asked once. Now the cooldown answers are waited out without
    spending a retry and the provider is asked again after the cooldown: the PDF arrives."""
    import types

    import stack.provider_transport as pt

    class VClock(Clock):
        def time(self) -> float:
            return 2_000_000.0 + self.t

    clock = VClock()
    monkeypatch.setattr(pt, "time", types.SimpleNamespace(time=clock.time, sleep=clock.sleep))
    srv = _GatedServer(clock, n_429=1)
    monkeypatch.setattr(sh, "_opener", lambda: srv)
    session = fh.HttpSession(sh.urllib_get, sleep=clock.sleep, monotonic=clock.monotonic, resolver=None)
    url = "https://zenodo.org/records/1/files/a.pdf"
    outcome, resp, history = session.fetch_url(url, None)
    assert outcome is None and resp is not None and resp.status == 200, (outcome, resp)
    assert [s for _, s in srv.log] == [429, 200] and len(history) == 2
    assert srv.log[1][0] - srv.log[0][0] >= 60.0  # asked again only after the Gate's cooldown


# ═════════════════════════════════════════════════════════════════════════════
# (J) unrequested Content-Encoding (D588; Dan Elton's v3 decode_content, e9dc435)
# ═════════════════════════════════════════════════════════════════════════════
def test_decode_content_undoes_gzip_and_deflate_under_the_cap() -> None:
    import gzip
    import zlib

    text = b"<html>" + b"a" * 5000 + b"</html>"
    for enc, body in [("gzip", gzip.compress(text)), ("x-gzip", gzip.compress(text)),
                      ("deflate", zlib.compress(text)), ("deflate", zlib.compress(text)[2:-4])]:
        out, hdrs = sh.decode_content(body, {"Content-Encoding": enc, "Content-Length": str(len(body))}, 1 << 20)
        assert out == text and "content-encoding" not in hdrs and "content-length" not in hdrs, enc
    bomb = gzip.compress(b"\0" * 5_000_000)
    assert len(bomb) < 10_000
    out, hdrs = sh.decode_content(bomb, {"content-encoding": "gzip"}, 10_000)
    assert len(out) == 10_001 and "content-encoding" not in hdrs  # over the cap: the caller's too_large
    for body, hdr in [(b"plain", {}), (b"plain", {"content-encoding": "br"}), (b"not gzip", {"content-encoding": "gzip"}),
                      (gzip.compress(text), {"content-encoding": "gzip", "x": "1"})]:
        cap = 10 if hdr.get("x") else 1 << 20  # a compressed body already over the cap is left as is
        out, hdrs = sh.decode_content(body, hdr, cap)
        assert out == body and hdrs.get("content-encoding") == hdr.get("content-encoding")


def test_urllib_get_decodes_an_unrequested_gzip_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """A server that gzips although we sent no ``Accept-Encoding``: ``urllib_get`` returns the page,
    not the compressed stream (before D588 the gzip bytes were the body)."""
    import gzip
    import io
    from email.message import Message

    class Srv:
        def open(self, req: Any, timeout: float | None = None, **kw: Any) -> Any:
            assert "Accept-encoding" not in req.headers and "Accept-Encoding" not in req.headers
            resp = io.BytesIO(gzip.compress(HTML_BYTES))
            resp.status, resp.headers = 200, Message()  # type: ignore[attr-defined]
            resp.headers["Content-Encoding"] = "gzip"  # type: ignore[attr-defined]
            resp.geturl = lambda: req.full_url  # type: ignore[attr-defined]
            return resp

    monkeypatch.setattr(sh, "_opener", lambda: Srv())
    got = sh.urllib_get("https://blog.test/p", headers=sh.DEFAULT_HEADERS, timeout=5, max_bytes=1 << 20)
    assert got.status == 200 and got.body == HTML_BYTES and "content-encoding" not in got.headers
