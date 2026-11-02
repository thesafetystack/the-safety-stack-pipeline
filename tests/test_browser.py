"""The shared headless-browser rung, ``stack/browser.py`` (D590, ported from Dan Elton's v3 module).

Hermetic part (fakes, no browser): the launch fallback to system Chrome, the warn-once and
disabled-for-the-process behaviour when no browser starts, the render cap, the start URL's SSRF
check (D680). Browser part: a real Chromium against a local
``http.server`` only; the test hostnames ``*.test`` are mapped to 127.0.0.1 by Chromium's
``--host-resolver-rules`` (no DNS, no outward request). Skipped when Playwright or a browser is
missing, so the suite passes either way.
"""

from __future__ import annotations

import http.server
import logging
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest

import stack.browser as br


@pytest.fixture(autouse=True)
def fresh_rung(monkeypatch: pytest.MonkeyPatch) -> None:
    """Per-process state reset for every test: no warning given yet, not disabled."""
    monkeypatch.setattr(br, "_warned", set())
    monkeypatch.setattr(br, "_disabled", None)
    monkeypatch.setattr(br, "_RESOLVER", None)


# ─────────────────────────────────────────────────────────────────────────────
# hermetic
# ─────────────────────────────────────────────────────────────────────────────
class _FakeChromium:
    def __init__(self, ok_channels: set[str | None]) -> None:
        self.ok = ok_channels
        self.calls: list[str | None] = []

    def launch(self, *, headless: bool, args: list[str], channel: str | None = None) -> Any:
        self.calls.append(channel)
        if channel not in self.ok:
            raise RuntimeError(f"Executable doesn't exist ({channel or 'bundled'})\nlong help text")
        return object()


class _FakePw:
    def __init__(self, ok_channels: set[str | None]) -> None:
        self.chromium = _FakeChromium(ok_channels)
        self.stopped = 0

    def stop(self) -> None:
        self.stopped += 1


def test_launch_falls_back_to_system_chrome_and_warns_once(caplog: pytest.LogCaptureFixture) -> None:
    pw = _FakePw({"chrome"})
    with caplog.at_level(logging.WARNING, logger="stack.browser"):
        br.launch(pw)
        br.launch(pw)
    assert pw.chromium.calls == [None, "chrome", None, "chrome"]
    warnings = [r.getMessage() for r in caplog.records]
    assert len(warnings) == 1 and "system chrome" in warnings[0] and "bundled chromium:" in warnings[0]


def test_no_browser_warns_once_disables_and_raises(monkeypatch: pytest.MonkeyPatch,
                                                    caplog: pytest.LogCaptureFixture) -> None:
    """The old rung swallowed the launch error (silently dead). Now: one warning per process, the
    rung disabled (no second driver started), and every call raises BrowserUnavailable."""
    pws: list[_FakePw] = []

    def start() -> _FakePw:
        pws.append(_FakePw(set()))
        return pws[-1]

    monkeypatch.setattr(br, "_start_playwright", start)
    with caplog.at_level(logging.WARNING, logger="stack.browser"):
        for _ in range(3):
            with pytest.raises(br.BrowserUnavailable, match="bundled chromium: Executable"):
                br.render_html("https://a.test/x")
    assert len(pws) == 1 and pws[0].stopped == 1
    assert [r.getMessage() for r in caplog.records] == [
        ("headless browser unavailable; rung disabled: bundled chromium: Executable doesn't exist (bundled); "
         "system chrome: Executable doesn't exist (chrome)")]
    assert br.unavailable() is not None


def test_playwright_missing_is_unavailable(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    def start() -> Any:
        raise ImportError("No module named 'playwright'")

    monkeypatch.setattr(br, "_start_playwright", start)
    with caplog.at_level(logging.WARNING, logger="stack.browser"):
        for _ in range(2):
            with pytest.raises(br.BrowserUnavailable, match="not installed"):
                br.render_html("https://a.test/x")
    assert len(caplog.records) == 1


def test_unsafe_start_url_is_refused_without_a_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    """D680: the render's start URL is the browser's one SSRF check."""
    monkeypatch.setattr(br, "_start_playwright", lambda: pytest.fail("no browser for an unsafe URL"))
    for url in ("http://127.0.0.1/", "http://169.254.169.254/latest/meta-data/", "http://localhost:8080/",
                "http://10.0.0.5/", "http://[::1]/", "file:///etc/passwd", "http://metadata.google.internal/"):
        assert br.render_html(url) is None


def test_at_most_two_renders_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Six lane threads asking at once: two render, the rest wait for a slot."""
    lock, live, peak = threading.Lock(), [0], [0]

    def render(url: str, **kw: Any) -> str:
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        time.sleep(0.05)
        with lock:
            live[0] -= 1
        return "<p>ok</p>"

    monkeypatch.setattr(br, "_render", render)
    threads = [threading.Thread(target=br.render_html, args=(f"https://h{i}.test/",)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert br.MAX_CONCURRENT_RENDERS == 2 and peak[0] == 2


# ─────────────────────────────────────────────────────────────────────────────
# a real browser against a local server
# ─────────────────────────────────────────────────────────────────────────────
_PAGE = """<html><body><p id=t>static</p>
<img src="http://sub.test:{port}/fine.png"><img src="http://127.0.0.1:{port}/literal.png">
<script>document.getElementById('t').textContent = 'rendered by js';</script></body></html>"""


@pytest.fixture(scope="module")
def site() -> Iterator[tuple[int, list[tuple[str, str]]]]:
    """A local server; it records ``(Host, path)`` per request, and also answers as a proxy (a
    request line with an absolute URL is recorded with that URL as the path)."""
    hits: list[tuple[str, str]] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a: Any) -> None:
            pass

        def do_GET(self) -> None:
            host = (self.headers.get("Host") or "").split(":")[0]
            hits.append((host, self.path))
            if self.path == "/redirect-ok":
                self.send_response(302)
                self.send_header("Location", f"http://hop.test:{port}/page")
                self.end_headers()
                return
            body = {"/page": _PAGE, "/plain": "<p>plain</p>",
                    "http://nowhere.test/plain": "<p>via the proxy</p>"}.get(self.path, "")
            self.send_response(200)
            self.send_header("Content-Type", "text/html" if body else "image/png")
            self.end_headers()
            self.wfile.write(body.format(port=port).encode())

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield port, hits
    srv.shutdown()


@pytest.fixture()
def real_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    """Chromium resolves ``*.test`` to the local server. Skips when no browser."""
    sync_api = pytest.importorskip("playwright.sync_api", reason="playwright not installed")
    monkeypatch.setattr(br, "_LAUNCH_ARGS", ("--host-resolver-rules=MAP *.test 127.0.0.1",))
    for name in ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    with sync_api.sync_playwright() as pw:
        try:
            br.launch(pw).close()
        except br.BrowserUnavailable as exc:
            pytest.skip(f"no headless browser: {exc}")


@pytest.mark.slow
def test_real_render_follows_redirects_and_runs_scripts(
        real_browser: None, site: tuple[int, list[tuple[str, str]]]) -> None:
    """A render through a redirect: the DOM after JavaScript. What the page asks for is not checked
    (D680): its subresource on a literal loopback address is requested like any other."""
    port, hits = site
    since = len(hits)
    html = br.render_html(f"http://start.test:{port}/redirect-ok", settle_ms=300)
    assert html is not None and "rendered by js" in html
    seen = set(hits[since:])
    assert {("start.test", "/redirect-ok"), ("hop.test", "/page"), ("sub.test", "/fine.png"),
            ("127.0.0.1", "/literal.png")} <= seen


@pytest.mark.slow
def test_real_render_goes_through_the_proxy_setting(real_browser: None, site: tuple[int, list[tuple[str, str]]],
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    """D680: with ``HTTP_PROXY`` set Chromium sends the render to the proxy (here the local server),
    which receives the absolute URL of a host nothing else answers for (port 80 is not served).
    Chromium reads the variable itself: no launch option is passed (``stack.browser``, Proxy)."""
    port, hits = site
    monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{port}")
    since = len(hits)
    html = br.render_html("http://nowhere.test/plain", settle_ms=100)
    assert html is not None and "via the proxy" in html
    assert ("nowhere.test", "http://nowhere.test/plain") in set(hits[since:])
