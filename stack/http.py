"""HTTP transport and politeness for every workflow that fetches a URL.

Extracted from ``workflows/fetch/http/main.py`` unchanged — workflows never import each other,
and ``fetch.arxiv`` needs the same restricted urllib opener, per-host pacer, robots cache and
typed retry loop that ``fetch.http`` was built on. One copy: a second acquisition
unit that classified a 429 differently, or paced a host per-thread instead of globally, would
not fail a test — it would show up as a ban.

Layers, bottom up:

* :func:`check_url_safety` — scheme allowlist + interior-host rejection, resolver-injectable: the
  one SSRF check, applied to the URL a fetch starts at (D680).
* :class:`HttpResponse` / :class:`HttpGetter` / :func:`urllib_get` — the one network surface;
  TOTAL (never raises), restricted handlers, no credential header on a cross-origin redirect, one
  SSL context per process and one opener per thread; an outbound proxy from the standard
  ``HTTPS_PROXY`` / ``HTTP_PROXY`` / ``NO_PROXY`` variables (off when unset). A
  ``Content-Encoding: gzip|x-gzip|deflate`` the server applied unasked (we send no
  ``Accept-Encoding``) is undone, the decoded body capped like a raw one (:func:`decode_content`,
  D588).
* :class:`HostPacer` — per-host politeness, injectable clock, and a per-host in-flight lock
  (:meth:`HostPacer.hold`); :class:`RobotsCache` — per host. Both are thread-safe.
* :func:`classify` / :func:`backoff_delay` — the typed failure taxonomy → outcome + action.
* :class:`PoliteFetcher` — the episode's one-host-hit: robots (when the channel opts in) →
  pace → GET → classify → retry with backoff / 429 penalty; a Gate cooldown answer is no attempt
  and a stated ``Retry-After`` is honoured, both up to a cap (D588). What ``fetch.http`` calls
  ``_fetch_url``. Each pace + GET holds the host's in-flight lock, so threads sharing one
  fetcher (acquire's ``fetch_workers``) never have two requests to one host in flight.
"""

from __future__ import annotations

import ipaddress
import os
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import zlib
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Protocol

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────
# Sent with every request this module makes (and reused by acquire's oa lane and the headless rung,
# stack/browser.py): a desktop Chrome User-Agent string. robots.txt is read and matched against this
# same string by RobotsCache.allowed (below), which PoliteFetcher.fetch consults before the request
# when the channel sets politeness.respect_robots (configs/channels.yaml); a Disallow is terminal.
DEFAULT_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# PDF sniffing (ported from pipeline/stages/lib/doc_fetch.py).
PDF_MIN_BYTES = 10_000

# Size cap for a single fetched artifact; a module constant, not a config knob. 64 MiB comfortably
# covers papers/cards while bounding a runaway body.
DEFAULT_MAX_BYTES = 64 * 1024 * 1024

# HTTP-level retry (within ONE fetch episode). Distinct from a role's ``attempts`` in
# ``documents.content.roles[]``, which counts consecutive FAILED EPISODES (runs) and drives quarantine
# at ``stack.fetchcycle.FETCH_CAP``.
MAX_HTTP_RETRIES = 3
BASE_BACKOFF_S = 2.0
MAX_BACKOFF_S = 60.0

# Per-host default pace when a channel declares no rate (polite ~1 rps).
_DEFAULT_RPS = 1.0


# ─────────────────────────────────────────────────────────────────────────────
# URL safety (SSRF check) — scheme allowlist + interior-host rejection
# ─────────────────────────────────────────────────────────────────────────────
_ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})
# Hostnames that always denote an interior/metadata target regardless of DNS.
_INTERIOR_NAMES: frozenset[str] = frozenset(
    {"localhost", "metadata.google.internal", "metadata"}
)
_INTERIOR_SUFFIXES: tuple[str, ...] = (".localhost", ".local", ".internal")

# The resolver signature (a subset of socket.getaddrinfo) — injectable so tests stay offline.
Resolver = Callable[..., Sequence[Any]]


#: Interior ranges checked explicitly, whatever the Python version's ``ipaddress`` flags: carrier-grade
#: NAT 100.64.0.0/10 (RFC 6598; Tailscale's addresses; already ``not is_global`` on 3.12.13, so the entry
#: is belt and braces there) and the deprecated IPv6 site-local fec0::/10 (RFC 3879; ``is_global`` on
#: 3.12, so this entry is what blocks it).
_INTERIOR_NETS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fec0::/10"))
#: NAT64's well-known prefix (RFC 6052): the last 32 bits are an IPv4 address.
_NAT64 = ipaddress.ip_network("64:ff9b::/96")


def _addr_is_interior(ip: str) -> bool:
    """True if ``ip`` is an SSRF target: not globally reachable (``ipaddress``'s ``is_global`` false:
    private, shared/CGNAT, documentation, …), loopback, link-local, reserved, multicast, unspecified,
    or in :data:`_INTERIOR_NETS` (cloud metadata 169.254.169.254, 127.0.0.1, 10/172.16/192.168,
    100.64/10, …); an IPv6 address that embeds an IPv4 one (IPv4-mapped ``::ffff:a.b.c.d``, 6to4
    ``2002::/16``, NAT64 ``64:ff9b::/96``) is also interior when that IPv4 address is.

    NAT64 and 6to4 are in fact refused as a class (D650): on Python 3.12 every ``64:ff9b::/96``
    address is ``is_reserved`` (inside ``::/8``) and every ``2002::/16`` one ``is_private``, so the
    embedded-IPv4 test only decides IPv4-mapped addresses. The lanes therefore need an IPv4 route: on
    a DNS64/NAT64-only host every publisher's synthesised AAAA would be refused."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address):
        embedded = addr.ipv4_mapped or addr.sixtofour or (
            ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF) if addr in _NAT64 else None)
        if embedded is not None and _addr_is_interior(str(embedded)):
            return True
    return bool(
        not addr.is_global
        or addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
        or any(addr in net for net in _INTERIOR_NETS)
    )


def check_url_safety(url: str, *, resolver: Resolver | None = None) -> str | None:
    """Return a terminal ``fetch_outcome`` if ``url`` must NOT be fetched, else None.

    The one SSRF check (D680, superseding D650/D651's enforcement points): each fetch entry point
    calls it once on the URL the fetch starts at, before any request (the http lane's episode, the
    OA lane's step request, the headless render). Nothing checks redirect hops, links found in a
    page, the address a connection reached, or the requests a library or browser makes on its own.

    ``dead`` — a non-``http(s)`` scheme (``file``/``ftp``/``data``/…) or an unparseable/host-less
    URL. ``blocked`` — the host is (or, when a ``resolver`` is configured, resolves to) an interior
    address, or is a loopback/metadata name. A resolution FAILURE is treated as allow (the proxied
    sandbox often cannot resolve external names locally, and the egress proxy will not route to an
    interior host); a name is only caught when a resolver resolves it, and the connection makes its
    own lookup (no pinning)."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return "dead"
    if parts.scheme not in _ALLOWED_SCHEMES:
        return "dead"
    host = parts.hostname
    if not host:
        return "dead"
    host = host.lower().rstrip(".")  # ``localhost.`` / ``127.0.0.1.`` name the same host (FQDN form)
    # Literal IP → range-check directly (no DNS).
    try:
        ipaddress.ip_address(host)
        return "blocked" if _addr_is_interior(host) else None
    except ValueError:
        pass
    if host in _INTERIOR_NAMES or host.endswith(_INTERIOR_SUFFIXES):
        return "blocked"
    if resolver is not None:
        try:
            infos = resolver(host, parts.port or (443 if parts.scheme == "https" else 80))
        except Exception:  # noqa: BLE001 — cannot resolve (proxied env) → allow, proxy won't route interior
            return None
        for info in infos:
            try:
                ip = info[4][0]
            except (IndexError, TypeError):
                continue
            if _addr_is_interior(str(ip)):
                return "blocked"
    return None


# ─────────────────────────────────────────────────────────────────────────────
# HTTP layer (injectable)
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class HttpResponse:
    """One HTTP result. ``status`` is the HTTP code, or None on a transport failure (then
    ``error`` names the class: ``timeout`` | ``dns`` | ``connection`` | ``other``). ``headers``
    keys are lower-cased. ``final_url`` is the post-redirect URL."""

    status: int | None
    headers: Mapping[str, str]
    body: bytes
    final_url: str
    error: str | None = None


class HttpGetter(Protocol):
    """The one network surface the engine needs. A fake satisfies this in tests."""

    def __call__(
        self, url: str, *, headers: Mapping[str, str], timeout: float, max_bytes: int
    ) -> HttpResponse: ...


_SSL_LOCK = threading.Lock()
_SSL_CACHE: dict[str, ssl.SSLContext | None] = {}
_TLS = threading.local()


def _ssl_context() -> ssl.SSLContext | None:
    """The process's one SSL context (see :func:`_build_ssl_context`). Building one parses the
    whole CA bundle — tens of ms of CPU — and it used to happen on EVERY request because the
    opener was rebuilt per call."""
    with _SSL_LOCK:
        if "ctx" not in _SSL_CACHE:
            _SSL_CACHE["ctx"] = _build_ssl_context()
        return _SSL_CACHE["ctx"]


def _build_ssl_context() -> ssl.SSLContext | None:
    """An SSL context trusting the agent-proxy CA bundle when present (the sandbox routes HTTPS
    through a MITM proxy). Falls back to the system trust store."""
    for var in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        path = os.environ.get(var)
        if path and os.path.exists(path):
            return ssl.create_default_context(cafile=path)
    bundle = "/root/.ccr/ca-bundle.crt"
    if os.path.exists(bundle):
        try:
            return ssl.create_default_context(cafile=bundle)
        except OSError:
            return None
    return None


#: Request headers that carry a credential: never forwarded by a redirect that leaves the request's
#: origin (:class:`_RedirectHandler`, D649), and stripped by the OA lane's gate from every request
#: (:data:`stack.oa_adapter.CREDENTIAL_HEADERS`, the same set).
CREDENTIAL_HEADERS: frozenset[str] = frozenset({"authorization", "x-api-key", "apikey", "x-els-apikey"})
#: What a cross-origin redirect drops: the credential headers, cookies and proxy credentials.
_REDIRECT_DROPPED: frozenset[str] = CREDENTIAL_HEADERS | {"cookie", "proxy-authorization"}


def _origin(url: str) -> tuple[str, str, int | None]:
    """(scheme, host, port) with the scheme's default port filled in: what a redirect must keep for
    a credential header to ride along (requests' ``rebuild_auth`` rule)."""
    parts = urllib.parse.urlsplit(url)
    scheme = parts.scheme.lower()
    try:
        port = parts.port
    except ValueError:
        port = -1
    return scheme, (parts.hostname or "").lower().rstrip("."), port or {"http": 80, "https": 443}.get(scheme)


class _RedirectHandler(urllib.request.HTTPRedirectHandler):
    """The stdlib's redirect handling, except that a hop that changes the scheme, host or port
    carries no credential header (:data:`_REDIRECT_DROPPED`; the stdlib forwards every header but
    ``Content-*``), so a provider key (the OpenAlex meter's ``Authorization``) reaches only the
    origin it was attached for (D649). Secret hygiene, not an SSRF check: the hop's target is not
    classified (D680)."""

    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> Any:
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and _origin(newurl) != _origin(req.full_url):
            for name in [k for k in new.headers if k.lower() in _REDIRECT_DROPPED]:
                del new.headers[name]
        return new


def _build_opener() -> urllib.request.OpenerDirector:
    """An opener with ONLY the http(s) transport handlers + the credential-dropping redirect
    handler — no ``FileHandler`` / ``FTPHandler`` / ``DataHandler`` / ``UnknownHandler``, so a
    ``file://`` or ``ftp://`` URL cannot be dispatched even if one reaches the getter."""
    opener = urllib.request.OpenerDirector()
    for handler in (
        urllib.request.ProxyHandler(),                       # honours HTTPS_PROXY / HTTP_PROXY / NO_PROXY
        urllib.request.HTTPHandler(),
        urllib.request.HTTPSHandler(context=_ssl_context()),
        urllib.request.HTTPDefaultErrorHandler(),
        urllib.request.HTTPErrorProcessor(),
        _RedirectHandler(),
    ):
        opener.add_handler(handler)
    return opener


def _opener() -> urllib.request.OpenerDirector:
    """One opener per thread, built on first use. An OpenerDirector is not documented as
    thread-safe (handlers keep per-request state), so it is per-thread rather than global;
    a test that patches ``OpenerDirector.open`` on the class still intercepts it."""
    op = getattr(_TLS, "opener", None)
    if op is None:
        op = _build_opener()
        _TLS.opener = op
    return op


def decode_content(body: bytes, headers: Mapping[str, str], max_bytes: int) -> tuple[bytes, dict[str, str]]:
    """Undo a ``Content-Encoding: gzip|x-gzip|deflate`` the server applied unasked (D588, after Dan
    Elton's v3 ``decode_content``, e9dc435: we send no ``Accept-Encoding``, and some servers compress
    anyway, so the bytes we stored or parsed were the compressed stream). Returns the decoded body,
    **capped at** ``max_bytes + 1`` bytes (a decompression bomb reads as over the cap, exactly as an
    over-cap raw body does, so each caller's own over-cap rule applies: acquire's
    ``failed_permanent[too_large]``), and the headers (lower-cased keys) without
    ``content-encoding``/``content-length``. Left as they are: no or another encoding, an empty body,
    a compressed body already over the cap (truncated: it cannot be decoded whole), and a stream
    that does not decode (``deflate`` is tried zlib-wrapped, as the RFC says, then raw)."""
    hdrs = {str(k).lower(): v for k, v in headers.items()}
    encoding = str(hdrs.get("content-encoding", "")).strip().lower()
    if encoding not in ("gzip", "x-gzip", "deflate") or not body or len(body) > max_bytes:
        return body, hdrs
    attempts = (zlib.MAX_WBITS | 32,) if encoding != "deflate" else (zlib.MAX_WBITS, -zlib.MAX_WBITS)
    for wbits in attempts:
        try:
            decoded = zlib.decompressobj(wbits).decompress(body, max_bytes + 1)
        except zlib.error:
            continue
        hdrs.pop("content-encoding", None)
        hdrs.pop("content-length", None)
        return decoded, hdrs
    return body, hdrs


def urllib_get(
    url: str, *, headers: Mapping[str, str], timeout: float, max_bytes: int
) -> HttpResponse:
    """The default getter: stdlib ``urllib`` via a restricted opener (honours ``https_proxy`` /
    ``no_proxy`` env and the proxy CA bundle; no file/ftp/data handlers; no credential header on a
    cross-origin redirect). It does not check ``url`` (the fetch entry points do, D680).
    Caps the body at ``max_bytes`` + 1 so the engine can flag ``too_large`` without buffering an
    unbounded response; an unasked gzip/deflate body is decoded under the same cap
    (:func:`decode_content`). TOTAL — it NEVER raises: every failure (incl. an unexpected exception
    or a missing status) becomes a typed transport :class:`HttpResponse`."""
    try:
        opener = _opener()
        req = urllib.request.Request(url, headers=dict(headers), method="GET")
        try:
            from stack.provider_transport import throttled_open
            with throttled_open(req, opener=opener.open, timeout=timeout) as resp:
                status = getattr(resp, "status", None)
                if status is None:
                    return HttpResponse(None, {}, b"", url, error="other")
                body, resp_headers = decode_content(resp.read(max_bytes + 1), resp.headers, max_bytes)
                return HttpResponse(
                    status=int(status),
                    headers=resp_headers,
                    body=body,
                    final_url=resp.geturl(),
                )
        except urllib.error.HTTPError as exc:  # a 4xx/5xx with a response
            try:
                body = exc.read(max_bytes + 1)
            except Exception:  # noqa: BLE001 — the status is what matters
                body = b""
            body, err_headers = decode_content(body, exc.headers or {}, max_bytes)
            return HttpResponse(
                status=int(exc.code),
                headers=err_headers,
                body=body,
                final_url=getattr(exc, "url", None) or url,
            )
        except urllib.error.URLError as exc:
            return HttpResponse(None, {}, b"", url, error=_classify_urlerror(exc))
        except (TimeoutError, OSError) as exc:  # socket timeout, connection reset, …
            return HttpResponse(None, {}, b"", url, error=_classify_oserror(exc))
    except Exception:  # noqa: BLE001 — TOTAL: an unexpected error is a typed transport failure
        return HttpResponse(None, {}, b"", url, error="other")


def _classify_urlerror(exc: urllib.error.URLError) -> str:
    reason = exc.reason
    if isinstance(reason, TimeoutError):
        return "timeout"
    text = str(reason).lower()
    if (
        "name or service not known" in text
        or "nodename nor servname" in text
        or "getaddrinfo" in text
    ):
        return "dns"
    if "timed out" in text:
        return "timeout"
    if "refused" in text or "reset" in text or "unreachable" in text:
        return "connection"
    return "other"


def _classify_oserror(exc: BaseException) -> str:
    text = str(exc).lower()
    if "timed out" in text or isinstance(exc, TimeoutError):
        return "timeout"
    if "refused" in text or "reset" in text or "unreachable" in text:
        return "connection"
    return "other"


# ─────────────────────────────────────────────────────────────────────────────
# Per-host token-bucket pacer (injectable clock/sleep)
# ─────────────────────────────────────────────────────────────────────────────
class HostPacer:
    """Per-host politeness pacing. ``acquire(host)`` blocks (via the injected ``sleep``) until the
    host's next-allowed instant, computed from its rate. A 429 halves the host's rate FOR THE RUN
    (``penalize``) — the fabled typed-taxonomy borrow. Deterministic under an injected clock."""

    def __init__(
        self,
        *,
        monotonic: Callable[[], float],
        sleep: Callable[[float], None],
        default_rps: float = _DEFAULT_RPS,
    ) -> None:
        self._monotonic = monotonic
        self._sleep = sleep
        self._default_rps = default_rps
        self._rate: dict[str, float] = {}
        self._next_allowed: dict[str, float] = {}
        # Thread-safe: fetch.http runs its network phase on a pool of workers, and politeness
        # must stay GLOBAL per host (v1 paced per thread, so 8 workers were 8x the stated rate).
        # A worker RESERVES its slot under the lock and sleeps outside it, so a long wait on one
        # host never blocks another host's workers. Under one thread the numbers are exactly
        # the old ones.
        self._lock = threading.Lock()
        self._holds: dict[str, threading.Lock] = {}

    @contextmanager
    def hold(self, host: str) -> Iterator[None]:
        """The host's in-flight lock: one holder per host at a time (other hosts unaffected).
        :class:`PoliteFetcher` holds it around each pace + GET, so a waiting request also waits
        its pacing slot after the one before it has answered."""
        with self._lock:
            lock = self._holds.setdefault(host, threading.Lock())
        with lock:
            yield

    def set_rate(self, host: str, rps: float | None) -> None:
        with self._lock:
            if rps and rps > 0 and host not in self._rate:
                self._rate[host] = rps

    def _rps(self, host: str) -> float:
        return self._rate.get(host, self._default_rps)

    def acquire(self, host: str) -> float:
        """Block until the host may be hit; return the seconds actually slept (0 if none)."""
        with self._lock:
            rps = self._rps(host)
            interval = 1.0 / rps if rps > 0 else 0.0
            now = self._monotonic()
            slot = max(now, self._next_allowed.get(host, now))
            self._next_allowed[host] = slot + interval
        slept = slot - now
        if slept > 0:
            self._sleep(slept)
        return slept

    def penalize(self, host: str, floor_rps: float = 0.05) -> None:
        """429 → halve the host's effective rate for the rest of the run (min ``floor_rps``, by
        default 0.05 rps; a rate already below the floor is kept, never raised)."""
        with self._lock:
            rps = self._rps(host)
            self._rate[host] = max(rps / 2.0, min(rps, floor_rps))


# ─────────────────────────────────────────────────────────────────────────────
# Robots cache (per host, injectable via the same getter)
# ─────────────────────────────────────────────────────────────────────────────
class RobotsCache:
    """Per-host robots.txt fetch + parse, cached for the run. A robots fetch that fails (404,
    transport error) is treated as ALLOW-ALL (the standard convention). Only consulted when the
    channel's politeness enables it."""

    def __init__(self, http_get: HttpGetter, ua: str, timeout: float) -> None:
        self._get = http_get
        self._ua = ua
        self._timeout = timeout
        self._cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        # One lock per host around the miss-and-load path, so two workers reaching a host at
        # once fetch its robots.txt once (and pay one pacing slot for it), not twice.
        self._lock = threading.Lock()
        self._host_locks: dict[str, threading.Lock] = {}

    def allowed(self, url: str, *, before_fetch: Callable[[], Any] | None = None) -> bool:
        """``before_fetch`` runs only when robots.txt actually has to be fetched (a cache miss)
        — it is where the caller pays the host's politeness pacing. Paying it on every call
        charged the 0.5 rps scrape/watch channels TWO pacing intervals per real request."""
        parts = urllib.parse.urlsplit(url)
        host = parts.netloc
        if host not in self._cache:
            with self._lock:
                host_lock = self._host_locks.setdefault(host, threading.Lock())
            with host_lock:
                if host not in self._cache:
                    if before_fetch is not None:
                        before_fetch()
                    self._cache[host] = self._load(parts.scheme or "https", host)
        rp = self._cache[host]
        if rp is None:
            return True
        return rp.can_fetch(self._ua, url)

    def _load(
        self, scheme: str, host: str
    ) -> urllib.robotparser.RobotFileParser | None:
        robots_url = f"{scheme}://{host}/robots.txt"
        resp = self._get(
            robots_url, headers=DEFAULT_HEADERS, timeout=self._timeout, max_bytes=1 << 20
        )
        if resp.status != 200 or not resp.body:
            return None  # no robots → allow all
        rp = urllib.robotparser.RobotFileParser()
        try:
            rp.parse(resp.body.decode("utf-8", errors="replace").splitlines())
        except Exception:  # noqa: BLE001 — a malformed robots is treated as allow-all
            return None
        return rp


# ─────────────────────────────────────────────────────────────────────────────
# Typed retry policy — the 8-class failure taxonomy → a fetch_outcome + an action
# ─────────────────────────────────────────────────────────────────────────────
# Actions the classifier can request of the episode loop.
_STOP = "stop"                # terminal — use the paired outcome
_RETRY_BACKOFF = "backoff"    # transient — exponential backoff then retry
_RETRY_PENALIZE = "penalize"  # 429 — halve host rate, backoff, retry

# Internal (non-enum) sentinel outcome from _fetch_url: a robots.txt Disallow. TERMINAL — unlike an
# HTTP 403/451 bot-block, a robots refusal is the site owner's explicit wish, so the ladder does NOT
# escalate it to the headless/Wayback rungs (coordinator decision). Logged to fetch_log as 'blocked'.
_ROBOTS_BLOCKED = "robots_blocked"


def classify(status: int | None, error: str | None) -> tuple[str | None, str]:
    """Map an HTTP status (or transport ``error`` class) to ``(terminal_outcome, action)``.

    ``terminal_outcome`` is a fetch outcome (the values in the mapping below; v3's ``enums.fetch_outcome``,
    archived in v3's ``configs/enums.yaml``) when the action is ``_STOP`` (or the
    fallback outcome to use once retries are exhausted); it is None only for the two retry actions
    on their happy transient path. The mapping IS the typed taxonomy:

      * 200            → (ok, stop)        — caller decides ok vs unchanged (raw-digest / 304)
      * 304            → (unchanged, stop)
      * 401 / 402      → (paywalled, stop)
      * 403 / 451      → (blocked, stop)   — bot-block / legal block; the ladder may still try
      * 404 / 410      → (dead, stop)      — arXiv 404 is disambiguated to withdrawn upstream
      * other 4xx      → (dead, stop)
      * 429            → (error, penalize) — rate-limited: halve host rate, backoff, retry
      * 5xx            → (error, backoff)
      * timeout/conn   → (error, backoff)  — transient transport
      * dns            → (error, stop)     — transient (round 5, R4-2): a resolver outage or a lapsed
                                             domain look the same; no in-episode retry, the role's
                                             backoff retries it and the cap quarantines it (90 d recheck)
      * other error    → (error, stop)
    """
    if error is not None:
        if error in ("timeout", "connection"):
            return "error", _RETRY_BACKOFF
        if error == "dns":
            return "error", _STOP
        return "error", _STOP
    assert status is not None
    if status == 200:
        return "ok", _STOP
    if status == 304:
        return "unchanged", _STOP
    if status in (401, 402):
        return "paywalled", _STOP
    if status in (403, 451):
        return "blocked", _STOP
    if status in (404, 410):
        return "dead", _STOP
    if status == 429:
        return "error", _RETRY_PENALIZE
    if 500 <= status < 600:
        return "error", _RETRY_BACKOFF
    if 400 <= status < 500:
        return "dead", _STOP
    return "error", _STOP


def backoff_delay(attempt: int) -> float:
    """Exponential backoff for HTTP retry ``attempt`` (0-based): base·2ⁿ capped."""
    return float(min(BASE_BACKOFF_S * (2**attempt), MAX_BACKOFF_S))


# ─────────────────────────────────────────────────────────────────────────────
# PDF sniffing + landing-page → PDF resolution (ported from v1 doc_fetch.py)
# ─────────────────────────────────────────────────────────────────────────────
def looks_like_pdf(data: bytes) -> bool:
    """A real PDF: the ``%PDF-`` magic AND a plausible size (v1 PDF_MIN_BYTES gate)."""
    return data[:5] == b"%PDF-" and len(data) >= PDF_MIN_BYTES


def looks_like_html(data: bytes) -> bool:
    head = data[:512].lstrip().lower()
    return head.startswith((b"<!doctype", b"<html", b"<head", b"<?xml"))


def _retry_after(resp: HttpResponse) -> float:
    """The wait a limited answer states (``Retry-After``/body ``retryAfter``; a Gate cooldown
    answer's is the time left), 60 s when it states none."""
    from stack.provider_transport import retry_seconds

    return retry_seconds(resp.headers, resp.body)


# ─────────────────────────────────────────────────────────────────────────────
# One host hit: robots → pace → GET → classify → retry
# ─────────────────────────────────────────────────────────────────────────────
class PoliteFetcher:
    """The per-episode network primitive shared by the acquisition units: one URL, fetched under
    a host's politeness with the typed retry loop. Holds the run-scoped pacer and robots cache so
    every rung of every episode in a run shares one view of each host."""

    def __init__(
        self,
        http_get: HttpGetter,
        *,
        pacer: HostPacer,
        robots: RobotsCache,
        timeout: float,
        max_bytes: int = DEFAULT_MAX_BYTES,
        sleep: Callable[[float], None],
    ) -> None:
        self._get = http_get
        self._pacer = pacer
        self._robots = robots
        self._timeout = timeout
        self._max_bytes = max_bytes
        self._sleep = sleep

    def fetch(
        self,
        url: str,
        *,
        rate_rps: float | None,
        respect_robots: bool,
        conditional: Mapping[str, str] | None = None,
        wait_limits: bool = True,
    ) -> tuple[str | None, HttpResponse | None, list[dict[str, Any]]]:
        """Fetch one URL with pacing + the typed retry loop. Returns (terminal_outcome_or_None,
        last_response, attempt_history). A None outcome with a response means a 200/304 the caller
        interprets. Robots is checked first when the channel enables it. ``attempt_history`` has one
        entry per request sent (a Gate cooldown answer sent nothing and has none).

        ``rate_rps`` / ``respect_robots`` are the channel's politeness (channels.yaml): the first
        call for a host pins its rate; scrape/watch channels set respect_robots, search/api
        channels omit it (respect only when the channel opts in — avoids over-fetching api hosts).

        Limited answers (D588, ``provider_transport`` "Retry loops"): a Gate cooldown answer
        (``COOLDOWN_HEADER``, nothing sent) spends no retry and is waited out (its time left), at
        most ``COOLDOWN_WAITS`` times; a 429 or 503 that states a wait is retried after
        ``max(backoff, the wait)``. The sleeps on limited answers total at most
        ``LIMIT_WAIT_MAX_S`` (120 s) per call: a wait past that ends the call at once with the
        limited answer (outcome ``error``; the caller's ``rate_limit``), as does a plan refusal
        (D583). ``wait_limits=False`` (the arXiv lane, whose D584 rule waits limited answers out
        itself) returns the first limited answer at once: a 429, a cooldown answer, or a 503 that
        states a wait.
        """
        from stack.provider_transport import COOLDOWN_WAITS, LIMIT_WAIT_MAX_S, is_cooldown, limited_delay

        host = urllib.parse.urlsplit(url).netloc
        self._pacer.set_rate(host, rate_rps)
        history: list[dict[str, Any]] = []

        if respect_robots:
            if not self._robots.allowed(url, before_fetch=lambda: self._pacer.acquire(host)):
                history.append({"url": url, "robots": "disallowed"})
                return _ROBOTS_BLOCKED, None, history  # TERMINAL — no ladder escalation

        headers = dict(DEFAULT_HEADERS)
        if conditional:
            headers.update(conditional)

        last: HttpResponse | None = None
        attempt = 0
        cooldowns = 0
        waited = 0.0  # seconds slept on limited answers (D588)
        while True:
            with self._pacer.hold(host):  # one request in flight per host, across threads
                self._pacer.acquire(host)
                resp = self._get(
                    url, headers=headers, timeout=self._timeout, max_bytes=self._max_bytes
                )
            last = resp
            if resp.status == 429 and is_cooldown(resp.headers):
                # Nothing was sent: our own Gate cooldown answered. Not an attempt (D588).
                left = _retry_after(resp)
                if not wait_limits or cooldowns >= COOLDOWN_WAITS or waited + left > LIMIT_WAIT_MAX_S:
                    return "error", resp, history
                cooldowns += 1
                waited += left
                self._sleep(left)
                continue
            outcome, action = classify(resp.status, resp.error)
            history.append(
                {"url": url, "status": resp.status, "error": resp.error, "outcome": outcome}
            )
            if action == _STOP:
                # 200/304 → let the caller interpret (return None outcome + response).
                if resp.status in (200, 304):
                    return None, resp, history
                return outcome, resp, history
            if action == _RETRY_PENALIZE:
                self._penalize(url, host, resp)
            limited = resp.status == 429 or (resp.status == 503 and "retry-after" in resp.headers)
            if limited and not wait_limits:
                return "error", resp, history
            if attempt >= MAX_HTTP_RETRIES:
                break
            delay = limited_delay(resp.status, resp.headers, resp.body, backoff_delay(attempt))
            if delay is None or (limited and waited + delay > LIMIT_WAIT_MAX_S):
                return "error", resp, history
            if limited:
                waited += delay
            attempt += 1
            self._sleep(delay)
        # Retries exhausted → the transient outcome becomes terminal (error).
        return "error", last, history

    def _penalize(self, url: str, host: str, resp: HttpResponse) -> None:
        """A 429 halves the host's rate, as D401 does in the collect transport: not for the Gate's
        cooldown answer (``COOLDOWN_HEADER``: nothing was sent), and for a gate-paced family no lower
        than the Gate's interval (the Gate already paces it and sets the cooldown). Before D584 one
        arXiv 429 and its three cooldown echoes left arXiv's pacer at the 0.05 rps floor for the run."""
        from stack.provider_transport import family, gate_interval, is_cooldown

        if is_cooldown(resp.headers):
            return
        interval = gate_interval(family(url))
        if interval is None:
            self._pacer.penalize(host)
        else:
            self._pacer.penalize(host, floor_rps=1.0 / interval)


__all__ = [
    "BASE_BACKOFF_S", "DEFAULT_HEADERS", "DEFAULT_MAX_BYTES", "MAX_BACKOFF_S",
    "MAX_HTTP_RETRIES", "PDF_MIN_BYTES", "HostPacer", "HttpGetter", "HttpResponse",
    "PoliteFetcher", "Resolver", "RobotsCache", "backoff_delay", "check_url_safety",
    "classify", "looks_like_html", "looks_like_pdf", "urllib_get",
]
