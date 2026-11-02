"""The shared headless-browser rung (Playwright): render a URL to its post-JavaScript HTML (D590).

Ported from Dan Elton's v3 ``stack/browser.py`` (e9dc435), with one change for v4: no browser
is a raised :class:`BrowserUnavailable` rather than a None (the caller counts it, so a run record
shows a dead rung). Used by acquire's http lane (``workflows/acquire/lanes/http.py``, option
``headless``); it lives in ``stack`` so a collect engine can reuse it for JS-only indexes.

**Launch fallback.** Playwright pins ONE Chromium build per release. Upgrading the package
(fetchpdf pulls it into ``uv.lock``) without re-running ``playwright install chromium`` leaves
the bundled build missing, and ``chromium.launch()`` raises; v3's and v4's old rung swallowed
that, so it returned None on every call (Dan, 2026-10-13: playwright 1.62 wanted build 1234, the
cache held others). So: bundled Chromium first, then the system Google Chrome
(``channel="chrome"``). When neither starts (or Playwright does not import or start), a warning
is logged ONCE per process, the rung is disabled for the process, and every call raises
:class:`BrowserUnavailable` at once (no Playwright driver spawned per URL).

**SSRF** (D680). The URL a render starts at passes :func:`stack.http.check_url_safety` before a
browser is started; an unsafe one renders nothing. What the page then asks for (redirects,
subresources, frames, XHR, WebSockets, service workers) is not checked: v3's route guard and
v4's hop watch and service-worker lockdown (D590, D651) are gone.

**Proxy** (D680). Chromium on Linux reads the standard ``HTTPS_PROXY`` / ``HTTP_PROXY`` /
``NO_PROXY`` variables itself (checked with Playwright 1.62's bundled build, no desktop proxy
settings: ``tests/test_browser.py``), as urllib and ``requests`` do, so no launch option is passed;
unset (the default), no proxy.

**Concurrency.** Each render is its own Playwright driver and Chromium (about 0.55 GB RSS for the
process tree on a small page, measured 2026-10-16). Acquire runs up to ``fetch_workers`` lane
calls at once (8 in the production plans): at most :data:`MAX_CONCURRENT_RENDERS` renders run at
once, the rest wait for a slot (holding their fetch slot meanwhile).
"""

from __future__ import annotations

import logging
import socket
import threading
from typing import Any

from stack.http import DEFAULT_HEADERS, Resolver, check_url_safety

logger = logging.getLogger(__name__)

# (label, extra launch kwargs), tried in order.
_LAUNCH_ATTEMPTS: tuple[tuple[str, dict[str, str]], ...] = (
    ("bundled chromium", {}),
    ("system chrome", {"channel": "chrome"}),
)
MAX_CONCURRENT_RENDERS = 2
_RENDER_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT_RENDERS)
# Guards _warned and _disabled. Reads of _disabled outside it are benign races: a render that
# sees a stale None only repeats a start that fails the same way, bounded by the slots above.
_STATE_LOCK = threading.Lock()
_warned: set[str] = set()
# Why no browser could start, once that is known: later calls raise BrowserUnavailable without
# spawning another Playwright driver. Per process, like the warnings (a fix needs a restart anyway).
_disabled: str | None = None
# Seams. The resolver the start URL's check uses (tests set None to stay off DNS); extra
# Chromium arguments (tests map their hostnames to a local server with --host-resolver-rules).
_RESOLVER: Resolver | None = socket.getaddrinfo
_LAUNCH_ARGS: tuple[str, ...] = ()


class BrowserUnavailable(RuntimeError):
    """No headless browser could be started in this process (the message says why)."""


def _warn_once(key: str, message: str) -> None:
    with _STATE_LOCK:
        if key in _warned:
            return
        _warned.add(key)
    logger.warning(message)


def _disable(key: str, message: str) -> BrowserUnavailable:
    global _disabled
    with _STATE_LOCK:
        if _disabled is None:
            _disabled = message
    _warn_once(key, message)
    return BrowserUnavailable(message)


def unavailable() -> str | None:
    """Why the rung is disabled in this process, or None while it is not (yet) known dead."""
    return _disabled


def launch(pw: Any, *, headless: bool = True) -> Any:
    """Start a browser from a started Playwright handle: bundled Chromium, else system Chrome.
    Raises :class:`BrowserUnavailable` naming why each attempt failed."""
    errors: list[str] = []
    for label, kwargs in _LAUNCH_ATTEMPTS:
        try:
            browser = pw.chromium.launch(headless=headless, args=list(_LAUNCH_ARGS), **kwargs)
        except Exception as exc:  # noqa: BLE001 — Playwright raises its own Error for a missing build
            first = (str(exc).strip().splitlines() or [type(exc).__name__])[0]
            errors.append(f"{label}: {first[:200]}")
            continue
        if label != _LAUNCH_ATTEMPTS[0][0]:
            _warn_once(
                "fallback",
                f"headless browser: using {label} ({'; '.join(errors)}). "
                "Run `uv run --no-sync playwright install chromium` to restore the bundled build.",
            )
        return browser
    raise BrowserUnavailable("; ".join(errors))


def _start_playwright() -> Any:
    """Start Playwright (a seam for tests). Raises ImportError when it is not installed."""
    from playwright.sync_api import sync_playwright

    return sync_playwright().start()


def render_html(
    url: str,
    *,
    user_agent: str = DEFAULT_HEADERS["User-Agent"],
    timeout_ms: int = 45000,
    settle_ms: int = 1200,
) -> str | None:
    """Render ``url`` headlessly and return the DOM's HTML, or None when the page can't be had
    (HTTP >= 400, navigation error, an unsafe start URL: module docstring, SSRF). Raises
    :class:`BrowserUnavailable` when no browser can start in this process (now or earlier: the
    first such call logs the warning); nothing else. Waits for a free render slot
    (:data:`MAX_CONCURRENT_RENDERS`)."""
    if _disabled is not None:
        raise BrowserUnavailable(_disabled)
    if check_url_safety(url, resolver=_RESOLVER) is not None:
        return None
    with _RENDER_SLOTS:
        if _disabled is not None:  # another render may have found no browser while this one waited
            raise BrowserUnavailable(_disabled)
        return _render(url, user_agent=user_agent, timeout_ms=timeout_ms, settle_ms=settle_ms)


def _render(url: str, *, user_agent: str, timeout_ms: int, settle_ms: int) -> str | None:
    try:
        pw = _start_playwright()
    except ImportError:
        raise _disable("import", "headless browser: playwright is not installed; rung disabled") from None
    except Exception as exc:  # noqa: BLE001 — a driver that won't start is "no browser", not a crash
        raise _disable("start", f"headless browser: playwright failed to start; rung disabled: {exc}") from None
    browser = None
    try:
        try:
            browser = launch(pw)
        except BrowserUnavailable as exc:
            raise _disable("launch", f"headless browser unavailable; rung disabled: {exc}") from None
        page = browser.new_context(user_agent=user_agent, locale="en-US").new_page()
        resp = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        if resp is None or resp.status >= 400:
            return None
        page.wait_for_timeout(settle_ms)
        return str(page.content())
    except BrowserUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 — a page/proxy failure degrades to the next rung
        logger.debug("headless render failed for %s: %s", url, exc)
        return None
    finally:
        try:
            if browser is not None:
                browser.close()
        finally:
            pw.stop()
