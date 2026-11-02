"""fetchpdf behind one door: the open-access-only guard and the run's shared retrieval session.

Used by ``acquire``'s OA lane (``workflows/acquire/lanes/oa.py``); nothing else imports ``fetchpdf``. ADAPTed from v3: the
guard (:func:`_require_open_access_only`, :func:`_pack_is_provably_absent`) is kept verbatim
(its messages still say ``fetch.oa``, v3's name for the OA lane); the v3 engine's database-era
imports (``stack.fetchcycle``'s selection, the writer, the runner) are dropped, since nothing here
used them.

**The request gate** (:class:`RequestGate`, D26 for the OA lane; D149). fetchpdf talks to the
network through its own ``requests`` sessions (``HttpClient``) and, in its T5 PDF chain, through
module-level ``requests.get`` and fresh ``requests.Session()`` objects, so a gate mounted on one session misses requests. While :meth:`RequestGate.active`,
every ``requests`` call **of that thread** passes :meth:`RequestGate.send`: ``HTTPAdapter.send`` is
wrapped process-wide while any thread has a gate active (counted; restored when the last block
exits) and routes each call to the calling thread's gate, a thread without one going straight
through (round 6, T-R6 OA: the OA lane's calls run in several threads at once; fetchpdf's
retrieval starts no threads of its own, so a member's requests stay on its thread). Each request
passes the run's shared :class:`stack.http.HostPacer` (the one the http and arXiv lanes use)
**holding the host's in-flight lock** (:meth:`stack.http.HostPacer.hold`, as
:class:`stack.http.PoliteFetcher` does) for pace + send + the body read (a streamed body is read
there, up to ``max_bytes + 1``, and replayed to the caller), so one host never has two requests in
flight from this process whatever the threads; then the cross-process
:class:`stack.provider_transport.Gate` for the provider families it knows (real transport only),
and a 429 retry loop (pacer penalty, :func:`stack.http.backoff_delay` through the injected
``sleep``, outside the host's lock; no sooner than a stated ``Retry-After``, and at most
``provider_transport.LIMIT_WAIT_MAX_S`` of such sleeps per request, D588). A Gate family's penalty
stops at the Gate's interval, and a standing Gate cooldown is answered before the request takes the
host's lock or pacer slot (D413, as D401 for collect: a throttled provider does not serialise every
walk at 0.05 rps). A Gate family's own 429 is not retried (D588): it has just set the Gate's
cooldown, so a retry after the 2 s step would only meet that cooldown and send nothing; the 429
stands at once, with the cooldown as its wait. A plan refusal (D583) is never retried. A 429 still
standing after the retries, or a Gate cooldown, is recorded for the calling thread (:attr:`RequestGate.limited`, :meth:`RequestGate.take`: one
member's episode runs on one thread) so the lane can classify the episode; fetchpdf itself only
sees a 429 response, and for a cooldown one that states no wait (D699: it would sleep the stated
wait, up to 60 s, and hold its own process-wide bucket of the host for as long, so one cooled
provider let every OA slot through one request a minute). With an injected HTTP transport (a test world) the gate answers from that
transport (``HttpGetter`` shape) instead of the network.

**Run-scoped answers** (T3.x-f4). fetchpdf asks the same metadata APIs several times per paper:
its resolver (ID Converter, OpenAlex, Semantic Scholar, Crossref, Unpaywall) and then its legacy
PDF chain again (ID Converter twice more, Europe PMC search three times, Unpaywall, Crossref,
OpenAlex, the DOI resolver and the landing page twice); a replay of T3.6's broader-run mix
counted 20 requests for a paper with no open-access copy, 11 of them repeats. With a ``cache``
dict the gate answers a repeated ``GET`` from the run's earlier answer (:func:`cache_key`: host,
unquoted path, query without the contact parameters; statuses 2xx/3xx/404/410, bodies up to
:data:`CACHE_MAX_BYTES`, never a PDF), counted ``cached``; a ``skip`` callable answers a request
it names (a source that cannot serve the identifier) with a synthetic 404 and no request,
counted ``skipped``. Neither ever answers a 429 or a 5xx from memory.

**No provider key reaches fetchpdf** (T-RUN item 6, review R8-1). fetchpdf loads ``.env.local``
into ``os.environ`` at import (``fetchpdf/_env.py``: its repo root, then ``find_dotenv`` from the
cwd up) and attaches ``SEMANTIC_SCHOLAR_API_KEY``, ``OPENALEXAPIKEY``, ``COREAPIKEY`` (and an NCBI
``api_key``) from the environment to its resolver calls; that would bypass the OpenAlex ledger
and caps (:mod:`stack.collect.provider_keys`). So: (a) :func:`import_fetchpdf` imports it with
``dotenv``'s loaders replaced by no-ops (every in-process import site calls it first), and
refuses (``RuntimeError``) when ``fetchpdf._env`` was imported elsewhere first and found a file; (b) the key constants fetchpdf captured at import are set to ``None``
(:data:`FETCHPDF_KEY_NAMES`) and the run's ``HttpClient`` strips credentials from its polite
headers and parameters; (c) :meth:`RequestGate.send` strips credential headers
(:data:`CREDENTIAL_HEADERS`) and query parameters (:data:`CREDENTIAL_PARAMS`) from every request
it passes, whatever built it. fetchpdf's OpenAlex calls therefore stay keyless (free, unmetered).
The process environment itself is not edited: a key exported there stays, but is not sent.

**One key, ours, added by the gate** (D592, a partial reversal of R8-1's blanket strip). After (c),
:meth:`RequestGate.send` adds OUR Semantic Scholar key (:func:`stack.collect.provider_keys.s2_headers`,
the collectors' one source: ``SEMANTIC_SCHOLAR_API_KEY`` from the environment or ``$STACK_ENV_FILE``)
to an ``https`` request for exactly :data:`S2_KEY_HOST` on the default port
(:func:`stack.collect.provider_keys.key_url`; a plain-``http`` one goes keyless) and to nothing else. It is on the request
only while it is sent, under the Gate's slot (a Gate cooldown answer sends nothing and adds nothing),
and removed from the request object afterwards, so the response fetchpdf gets (``resp.request``) and
a redirect it follows carry no key; the run cache (:func:`cache_key`: URL only), the 429 records and
a transport error's message never held it. Counted ``s2_keyed`` per request sent (the lane's
``oa.http.s2_keyed``). No key: keyless as before. Why: keyless, the S2 API answers from the shared
public pool, and fetchpdf's S2 lookups were the OA lane's 429s (build 4a: ``oa.rate_limited`` 269
and 172 per acquire run, all ``api.semanticscholar.org``); S2 is free and unmetered, so no ledger
or cap is bypassed (R8-1's reason was OpenAlex, which stays keyless), and the Gate already paces
``s2`` at one request per 2.5 s, arrival-paced, one in flight, within a keyed allowance. fetchpdf
still never sees a key: its constants stay cleared and its client still strips.

**No SSRF check here** (D680, superseding D651). The gate does not classify URLs: the OA lane checks
the URL each of its own step requests starts at (``workflows/acquire/lanes/oa.py`` ``OaLane._get``);
redirect hops, fetchpdf's own requests and the browser contexts fetchpdf opens are not checked.
"""

from __future__ import annotations

import json
import os
import pathlib
import threading
import urllib.parse
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from stack.http import (
    CREDENTIAL_HEADERS,
    DEFAULT_MAX_BYTES,
    MAX_HTTP_RETRIES,
    HostPacer,
    HttpGetter,
    backoff_delay,
)
from stack.provider_transport import (
    COOLDOWN_HEADER,
    COOLDOWN_MESSAGE,
    LIMIT_WAIT_MAX_S,
    Cooldown,
    Gate,
    credential_id,
    family,
    gate_interval,
    limited_delay,
    retry_seconds,
)

_ENV_LADDER = "FETCHPDF_LADDER"

_DEFAULT_LADDER = pathlib.Path(__file__).resolve().parents[1] / "configs" / "fetchpdf_ladder.json"

def _pack_is_provably_absent(module: Any) -> bool:
    """True when the optional source pack demonstrably is not in this build.

    Two independent facts, both required: the subpackage does not resolve as a module, and the
    chain registered no last-resort sources. Either alone could be misread — a registry can be
    emptied at runtime, and an unimported module can still be on disk — so neither is trusted by
    itself. Together they are a positive finding, not an assumption from a missing attribute.
    """
    import importlib.util

    try:
        present = importlib.util.find_spec("fetchpdf.extras") is not None
    except (ImportError, AttributeError, ValueError):
        # A guarded parent, a namespace oddity, or a blocked finder: cannot establish absence.
        return False
    return not present and not getattr(module, "LAST_RESORT_SOURCES", ())

def _require_open_access_only(module: Any) -> None:
    """Refuse to run unless this build of ``fetchpdf`` cannot reach a last-resort source.

    Three builds have to be told apart, and only two of them may proceed:

    * a build that reports reachability — ask it to turn the pack off, then believe its answer;
    * a build with no such accessor but no pack either — the published package today. Absence is
      established positively (:func:`_pack_is_provably_absent`), never inferred from the missing
      accessor, because an older build can lack the accessor and still reach a last-resort source;
    * anything else — refused. A posture that cannot be checked is not a posture.

    Raising rather than skipping the source is deliberate: this is a property of the run, not of
    any record, and a run that cannot assert it should not write bytes into the corpus at all.
    """
    disable = getattr(module, "disable_last_resorts", None)
    if callable(disable):
        disable()

    probe = getattr(module, "last_resorts_enabled", None)
    if callable(probe):
        if probe():
            raise RuntimeError(
                "fetch.oa: fetchpdf still reaches a last-resort source after being asked not "
                "to. Acquiring through one would violate D4 and the I13 egress gate; refusing "
                "to run."
            )
        return

    if _pack_is_provably_absent(module):
        return

    raise RuntimeError(
        "fetch.oa: cannot establish that the installed fetchpdf reaches no last-resort source — "
        "it has no last_resorts_enabled() and its optional pack is not provably absent. Pin a "
        "build that can answer; this unit will not acquire bytes it cannot classify for the I13 "
        "egress gate."
    )

@dataclass
class FetchpdfSession:
    """One run's fetchpdf state: the engine entry point plus what every call must share.

    fetchpdf's own batch mode builds ONE ``BatchResolver`` over ONE per-host rate limiter and
    ``prime()``s it with the whole identifier list (DOI/PMID → PMCID in chunks of 200 — the
    lookup that unlocks the T1 XML tier without touching a quota-limited API). Calling
    ``retrieve_tiered`` bare, as this unit used to, builds a fresh resolver and limiter PER
    RECORD: no shared cache, no shared pacing, and NCBI's 3/s becomes N×3/s across records.
    """

    retrieve: Any                                   # retrieve_tiered (or the test seam)
    kwargs: Mapping[str, Any] = field(default_factory=dict)   # resolver=, ladder=, email=
    prime: Callable[[Sequence[str]], int] | None = None       # once, with every due identifier
    flush: Callable[[], None] | None = None                   # resolver.cache.flush at the end
    ladder_path: str = ""
    lookup: Callable[[str], Mapping[str, Any] | None] | None = None  # resolver.cache.get: PMCID etc.

#: The provider-key constants fetchpdf reads from the environment at import (``fetchpdf/_env.py``
#: and the modules that copy them); all set to ``None`` after the import (module docstring).
FETCHPDF_KEY_NAMES: tuple[str, ...] = (
    "S2_API_KEY", "ENTREZ_API_KEY", "OPENALEX_API_KEY", "CORE_API_KEY", "SCOPUS_API_KEY",
    "OPENROUTER_API_KEY", "ELSEVIER_TDM_API_KEY", "_S2_API_KEY", "_NCBI_API_KEY",
    "_OPENALEX_API_KEY", "_CORE_API_KEY", "_SCOPUS_API_KEY")
#: The one host :class:`RequestGate` sends a key to: our S2 key (module docstring, D592).
S2_KEY_HOST = "api.semanticscholar.org"
#: Query parameters that carry a credential (lower case); the headers are
#: :data:`stack.http.CREDENTIAL_HEADERS` (imported here, one set for the gate and the urllib getter).
CREDENTIAL_PARAMS: frozenset[str] = frozenset({"api_key", "apikey"})
_fetchpdf_isolated = False


def import_fetchpdf() -> None:
    """Import ``fetchpdf`` with its ``.env.local`` load neutralised and its key constants cleared
    (module docstring). Every in-process ``fetchpdf`` import of this repository goes through here
    first (this module, ``stack.conversion.ladder``, the OA lane's identity check). Idempotent.
    When ``fetchpdf._env`` was imported elsewhere first and did load a ``.env.local``, raises
    ``RuntimeError`` (its values may be in ``os.environ``); one that found no file is harmless."""
    import importlib
    import sys

    global _fetchpdf_isolated
    if not _fetchpdf_isolated:
        env_mod = sys.modules.get("fetchpdf._env")
        if env_mod is not None and getattr(env_mod, "ENV_FILE", None):
            raise RuntimeError(
                f"fetchpdf was imported outside stack.oa_adapter.import_fetchpdf and loaded "
                f"{env_mod.ENV_FILE} into os.environ; provider keys there would reach fetchpdf "
                f"(R8-1). Remove that file or import fetchpdf only through stack.oa_adapter")
        if env_mod is None:
            import dotenv  # fetchpdf's dependency

            saved = (dotenv.load_dotenv, dotenv.find_dotenv)
            dotenv.load_dotenv = lambda *a, **k: False  # type: ignore[assignment]
            dotenv.find_dotenv = lambda *a, **k: ""  # type: ignore[assignment]
            try:
                importlib.import_module("fetchpdf._env")
            finally:
                dotenv.load_dotenv, dotenv.find_dotenv = saved
        _fetchpdf_isolated = True
    importlib.import_module("fetchpdf")
    for name, mod in list(sys.modules.items()):
        if mod is not None and (name == "fetchpdf" or name.startswith("fetchpdf.")):
            for attr in FETCHPDF_KEY_NAMES:
                if getattr(mod, attr, None) is not None:
                    setattr(mod, attr, None)


def strip_credentials(url: str, headers: Mapping[str, str]) -> tuple[str, dict[str, str]]:
    """``(url, headers)`` without credential query parameters and headers (module docstring)."""
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    kept = [(k, v) for k, v in query if k.lower() not in CREDENTIAL_PARAMS]
    if len(kept) != len(query):
        url = urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(kept)))
    return url, {k: v for k, v in headers.items() if str(k).lower() not in CREDENTIAL_HEADERS}


def _keyless_client(http: Any) -> None:
    """The run's fetchpdf ``HttpClient`` sends no credential (module docstring)."""
    polite_headers, polite_params = http.polite_headers, http.polite_params

    def headers(host: str, given: dict[str, str] | None = None) -> dict[str, str]:
        return {k: v for k, v in polite_headers(host, given).items() if str(k).lower() not in CREDENTIAL_HEADERS}

    def params(host: str, given: dict[str, Any] | None = None) -> dict[str, Any]:
        return {k: v for k, v in polite_params(host, given).items() if str(k).lower() not in CREDENTIAL_PARAMS}

    http.polite_headers, http.polite_params = headers, params


def _load_fetchpdf(scratch: str, email: str, *, gate_requests: bool = True) -> tuple[Any, FetchpdfSession]:
    """Import fetchpdf, prove the D4 posture, and build the run's shared session.

    ``gate_requests`` mounts :func:`stack.provider_transport.attach_requests_gate` on fetchpdf's
    ``HttpClient`` session (the abstract-recovery lookup's default). The OA lane passes ``False``:
    its :class:`RequestGate` gates every request itself, and a second ``Gate`` entry nested inside
    it would wait on its own arXiv lock.

    Imported lazily, inside ``run()``: a build of this repository that never runs ``fetch.oa``
    should not need the dependency installed. Its import-time ``.env.local`` load and key reads
    are neutralised (:func:`import_fetchpdf`); ``EMAIL`` is seeded from ``STACK_MAILTO`` first so
    its own defaults agree with what this unit passes explicitly.
    """
    os.environ.setdefault("EMAIL", email)
    import_fetchpdf()
    from fetchpdf import fetchpdf as chain  # type: ignore[import-untyped]
    from fetchpdf.retrieval.cache import ResolutionCache  # type: ignore[import-untyped]
    from fetchpdf.retrieval.engine import retrieve_tiered  # type: ignore[import-untyped]
    from fetchpdf.retrieval.http import HttpClient  # type: ignore[import-untyped]
    from fetchpdf.retrieval.ratelimit import shared_host_limiter  # type: ignore[import-untyped]
    from fetchpdf.retrieval.resolve import BatchResolver  # type: ignore[import-untyped]
    from fetchpdf.retrieval.tiers import load_ladder  # type: ignore[import-untyped]

    _require_open_access_only(chain)

    ladder_path = (os.environ.get(_ENV_LADDER, "") or "").strip() or str(_DEFAULT_LADDER)
    ladder = load_ladder(ladder_path)
    limiter = shared_host_limiter(ladder.rate_limits)
    http = HttpClient(limiter, email=email)
    _keyless_client(http)
    if gate_requests:
        from stack.provider_transport import attach_requests_gate
        attach_requests_gate(http.session)
    resolver = BatchResolver(http, ResolutionCache.for_output_dir(scratch), ladder)
    session = FetchpdfSession(
        retrieve=retrieve_tiered,
        kwargs={"resolver": resolver, "ladder": ladder, "email": email},
        prime=lambda ids: int(resolver.prime(list(ids))),
        flush=resolver.cache.flush,
        ladder_path=ladder_path,
        lookup=resolver.cache.get,
    )
    return chain, session


# ─────────────────────────────────────────────────────────────────────────────
# The request gate (module docstring)
# ─────────────────────────────────────────────────────────────────────────────
#: Provider families with a cross-process :class:`~stack.provider_transport.Gate` (as
#: :func:`~stack.provider_transport.throttled_open`).
GATED_FAMILIES: frozenset[str] = frozenset({"arxiv", "openalex", "s2", "crossref", "datacite", "zenodo", "pubmed"})
#: Metadata APIs fetchpdf's resolver asks for every paper, beyond the Gate families: a 429 from
#: one of these is recorded provider-level (:attr:`Limited.provider`). Whether a limit halts the OA
#: lane is the lane's call (``workflows.acquire.lanes.oa.LANE_WIDE_APIS``, D413), not this flag's.
RESOLVER_HOSTS: frozenset[str] = frozenset({"api.unpaywall.org", "www.ebi.ac.uk", "api.core.ac.uk"})


def seed_rates(pacer: HostPacer, ladder_path: str) -> int:
    """Give the run's pacer fetchpdf's per-host rates (the ladder's ``rate_limits``, unkeyed
    ``rps``) for hosts it has no rate for yet, so the resolver APIs are paced at their documented
    rate rather than the pacer's 1 rps default. Returns the number of hosts read."""
    import json

    if not ladder_path:
        return 0
    limits = json.loads(pathlib.Path(ladder_path).read_text()).get("rate_limits") or {}
    hosts = {h: v for h, v in limits.items() if isinstance(v, dict) and v.get("rps")}
    for host, spec in hosts.items():
        pacer.set_rate(host, float(spec["rps"]))
    return len(hosts)


@dataclass(frozen=True)
class Limited:
    """A 429 still standing after :class:`RequestGate`'s retries (or a ``Gate`` cooldown)."""

    url: str
    host: str
    provider: bool                     # a Gate family or a resolver API (descriptive; halting: the lane's call)
    retry_after_s: float | None        # the ``Retry-After`` it carried, if any


#: Largest body :class:`RequestGate` keeps for a repeated request (module docstring).
CACHE_MAX_BYTES = 1 << 20
#: Statuses whose answer a repeated request may reuse within the run.
CACHE_STATUSES = frozenset({200, 301, 302, 303, 307, 308, 404, 410})
#: Query parameters that name the caller, not the question (dropped from :func:`cache_key`).
CONTACT_PARAMS = frozenset({"email", "mailto", "tool", "idtype"})


def cache_key(method: str, url: str) -> tuple[str, str, str] | None:
    """The run-cache key of a request, ``None`` when not cacheable (only ``GET``): lower-cased host,
    unquoted path, sorted unquoted query without :data:`CONTACT_PARAMS` (``idtype``: the ID
    Converter answers a DOI the same with or without it)."""
    if method.upper() != "GET":
        return None
    parts = urllib.parse.urlsplit(url)
    query = sorted((k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
                   if k.lower() not in CONTACT_PARAMS)
    return ((parts.hostname or "").lower(), urllib.parse.unquote(parts.path), urllib.parse.urlencode(query))


def is_provider(url: str) -> bool:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return family(url) in GATED_FAMILIES or host in RESOLVER_HOSTS


#: The process's wrap of ``HTTPAdapter.send`` (module docstring): the original, the number of
#: active blocks over all threads, and each thread's stack of active gates.
_WRAP_LOCK = threading.Lock()
_WRAP: dict[str, Any] = {"original": None, "depth": 0}
_THREAD = threading.local()


def _thread_gates() -> list[RequestGate]:
    gates: list[RequestGate] | None = getattr(_THREAD, "gates", None)
    if gates is None:
        gates = _THREAD.gates = []
    return gates


class RequestGate:
    """Every ``requests`` call of a thread while :meth:`active` (module docstring). ``pacer`` is
    the run's shared host pacer; ``transport`` an :class:`stack.http.HttpGetter` (``None`` = the
    network); ``provider_gate`` enters the cross-process ``Gate`` for :data:`GATED_FAMILIES`
    (default: only on the network); ``max_bytes`` caps a body read under the host's lock.
    ``count(key, n)`` receives ``requests``, ``retries``, ``http_429``, ``cached``, ``skipped``,
    ``s2_keyed``. Our S2 key is read once, here (module docstring, D592).
    Safe to share between threads: the run cache is a dict of complete answers (two threads asking
    at once may both send), the 429 records are per thread."""

    def __init__(self, *, pacer: HostPacer, sleep: Callable[[float], None], transport: HttpGetter | None = None,
                 provider_gate: bool | None = None, max_retries: int = MAX_HTTP_RETRIES,
                 count: Callable[[str, int], None] | None = None,
                 cache: dict[tuple[str, str, str], tuple[int, dict[str, str], bytes]] | None = None,
                 skip: Callable[[str], str | None] | None = None, max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        self.cache = cache
        self.max_bytes = max_bytes
        self.skip = skip
        self.pacer = pacer
        self.sleep = sleep
        self.transport = transport
        self.provider_gate = transport is None if provider_gate is None else provider_gate
        self.max_retries = max_retries
        self._count = count or (lambda key, n: None)
        self._local = threading.local()
        from stack.collect.provider_keys import s2_headers

        self._s2_headers = s2_headers()  # {} when keyless; never logged, never stored

    @property
    def limited(self) -> list[Limited]:
        """The calling thread's 429s recorded since its last :meth:`take`."""
        got: list[Limited] | None = getattr(self._local, "limited", None)
        if got is None:
            got = self._local.limited = []
        return got

    @contextmanager
    def active(self) -> Iterator[RequestGate]:
        """Route every ``requests`` call of the calling thread through :meth:`send` for the
        block (module docstring); blocks nest and run in several threads at once."""
        with _WRAP_LOCK:
            if _WRAP["depth"] == 0:
                _wrap()
            _WRAP["depth"] += 1
        _thread_gates().append(self)
        try:
            yield self
        finally:
            _thread_gates().pop()
            with _WRAP_LOCK:
                _WRAP["depth"] -= 1
                if _WRAP["depth"] == 0:
                    _unwrap()

    def take(self) -> list[Limited]:
        """The calling thread's 429s recorded since its last call (cleared)."""
        out, self._local.limited = self.limited, []
        return out

    def send(self, request: Any, network: Callable[[], Any], kwargs: Mapping[str, Any]) -> Any:
        """One request: credentials stripped (module docstring) → (host's lock: pace → (Gate) →
        our S2 key for :data:`S2_KEY_HOST` only (D592) → send → body) → on 429 penalise, back off, retry (not a Gate family's, D588); record a 429
        that survives the retries."""
        request.url, clean = strip_credentials(str(request.url), request.headers)
        for name in [k for k in request.headers if k not in clean]:
            del request.headers[name]
        url = str(request.url)
        host = urllib.parse.urlsplit(url).netloc
        why = self.skip(url) if self.skip is not None else None
        if why:
            self._count("skipped", 1)
            return _response(request, 404, why.encode(), {"Content-Type": "text/plain"}, url)
        key = cache_key(str(request.method or "GET"), url) if self.cache is not None else None
        if key is not None and key in self.cache:  # type: ignore[operator]
            self._count("cached", 1)
            status, headers, body = self.cache[key]  # type: ignore[index]
            return _response(request, status, body, headers, url)
        resp: Any = None
        waited = 0.0  # seconds slept on 429s (D588)
        gated = self.provider_gate and family(url) in GATED_FAMILIES
        for attempt in range(self.max_retries + 1):
            left = self._cooldown_left(url)
            if left > 0:  # answered before the host's lock and pacer slot (module docstring, D413)
                return self._cooled(request, url, host, left)
            with self.pacer.hold(host):  # one request in flight per host, across threads
                self.pacer.acquire(host)
                self._count("requests", 1)
                try:
                    resp = self._dispatch(request, network, kwargs)
                except Cooldown as exc:  # the provider told another process to wait: no retry here
                    return self._cooled(request, url, host, exc.seconds)
                if resp.status_code != 429:
                    _read_body(resp, self.max_bytes)
            if resp.status_code != 429:
                if key is not None:
                    self._remember(key, resp)
                return resp
            self._count("http_429", 1)
            # The Gate holds a gate-paced family to its interval and sets the cooldown: the pacer
            # is not halved below that interval (D413, as D401 for collect); other hosts as before.
            interval = gate_interval(family(url))
            if interval is None:
                self.pacer.penalize(host)
            else:
                self.pacer.penalize(host, floor_rps=1.0 / interval)
            if gated or attempt >= self.max_retries:
                break  # D588: a Gate family's retry would only meet the cooldown this 429 set
            delay = limited_delay(429, resp.headers, resp.content, backoff_delay(attempt))
            if delay is None or waited + delay > LIMIT_WAIT_MAX_S:
                break  # a plan refusal, or a wait past the cap: the 429 stands now
            waited += delay
            resp.close()  # a streamed 429 holds its connection otherwise
            self._count("retries", 1)
            self.sleep(delay)
        headers = {str(k).lower(): v for k, v in resp.headers.items()}
        wait = retry_seconds(headers, b"") if "retry-after" in headers else None
        left = self._cooldown_left(url) if gated else 0.0
        self._record(url, host, left if left > 0 else wait)
        if gated:  # D699: the Gate's cooldown now holds the wait; the caller's retry meets it at once
            resp.headers.pop("Retry-After", None)
        return resp

    def _remember(self, key: tuple[str, str, str], resp: Any) -> None:
        """Keep ``resp`` for a repeated request (module docstring): never a PDF or a body over
        :data:`CACHE_MAX_BYTES` (a declared length is checked before the body is read)."""
        assert self.cache is not None
        if resp.status_code not in CACHE_STATUSES:
            return
        headers = {str(k): str(v) for k, v in resp.headers.items()}
        ctype = headers.get("Content-Type", headers.get("content-type", "")).lower()
        length = headers.get("Content-Length", headers.get("content-length", ""))
        if "pdf" in ctype or "octet-stream" in ctype or (length.isdigit() and int(length) > CACHE_MAX_BYTES):
            return
        body = resp.content  # read now; the caller's iter_content/.content replay it
        if len(body) <= CACHE_MAX_BYTES:
            self.cache[key] = (resp.status_code, headers, body)

    def _record(self, url: str, host: str, retry_after_s: float | None) -> None:
        self.limited.append(Limited(url, host, is_provider(url), retry_after_s))

    def _cooldown_left(self, url: str) -> float:
        """Seconds left of the cross-process ``Gate``'s cooldown for ``url``'s provider family
        (0 when none, or when the Gate is off). An unlocked read: :meth:`Gate.enter` checks again
        under its lock, so a cooldown set in between still answers as :class:`Cooldown`."""
        if not (self.provider_gate and family(url) in GATED_FAMILIES):
            return 0.0
        return Gate(url, self._credential(url)).cooldown_left()

    def _credential(self, url: str) -> str | None:
        """The credential this gate sends ``url`` with (:func:`stack.provider_transport.credential_id`,
        D700): our S2 key for the S2 API (D592), else none (every other credential is stripped), so
        the walks' keyless OpenAlex lookups keep a cooldown of their own, apart from the keyed
        collectors', and their keyed S2 lookups share the S2 collectors' (one key, one allowance)."""
        if self._s2_headers:
            from stack.collect.provider_keys import key_url

            if key_url(url, S2_KEY_HOST):
                return credential_id(url, self._s2_headers)
        return None

    def _cooled(self, request: Any, url: str, host: str, seconds: float) -> Any:
        """The provider told another process to wait: recorded (``seconds`` is the limit's wait, the
        lane's hint), answered as a 429 with no request sent, no penalty, no retry. The answer states
        no ``Retry-After`` (D699): fetchpdf's client sleeps a stated wait (up to 60 s) and drains its
        process-wide bucket of the host as long before each retry, which only meets the same
        cooldown; unstated, it retries after its short backoff and the walk goes on."""
        self._record(url, host, seconds)
        return _response(request, 429, json.dumps({"error": COOLDOWN_MESSAGE}).encode(),
                         {COOLDOWN_HEADER: "1"}, url)

    def _dispatch(self, request: Any, network: Callable[[], Any], kwargs: Mapping[str, Any]) -> Any:
        url = str(request.url)
        send = network if self.transport is None else (lambda: self._fake(request, kwargs))
        if self._s2_headers:
            from stack.collect.provider_keys import key_url

            if key_url(url, S2_KEY_HOST):  # D592: the S2 API only, over https only
                send = self._with_s2_key(request, send)
        if not (self.provider_gate and family(url) in GATED_FAMILIES):
            return send()
        gate = Gate(url, self._credential(url))
        with gate.enter():
            resp = send()
            gate.observe(resp.status_code, resp.headers, resp.content if resp.status_code == 429 else b"")
            return resp

    def _with_s2_key(self, request: Any, send: Callable[[], Any]) -> Callable[[], Any]:
        """``send`` with our S2 key on ``request`` for the send only (module docstring, D592)."""

        def keyed() -> Any:
            request.headers.update(self._s2_headers)
            self._count("s2_keyed", 1)
            try:
                return send()
            finally:
                for name in self._s2_headers:
                    request.headers.pop(name, None)

        return keyed

    def _fake(self, request: Any, kwargs: Mapping[str, Any]) -> Any:
        """The injected transport's answer as a ``requests.Response`` (a transport failure raises
        ``requests.ConnectionError``, as the network would)."""
        import requests

        assert self.transport is not None
        timeout = kwargs.get("timeout")
        got = self.transport(str(request.url), headers=dict(request.headers),
                             timeout=float(timeout if isinstance(timeout, (int, float)) else 30.0),
                             max_bytes=DEFAULT_MAX_BYTES)
        if got.status is None:
            raise requests.ConnectionError(f"{got.error or 'transport'} failure: {request.url}", request=request)
        return _response(request, got.status, got.body, dict(got.headers), got.final_url or str(request.url))


def _wrap() -> None:
    """Install the process-wide wrap (under ``_WRAP_LOCK``, first active block): ``HTTPAdapter.send``
    to the thread's gate (module docstring)."""
    from requests.adapters import HTTPAdapter

    original = _WRAP["original"] = HTTPAdapter.send

    def send(adapter: Any, request: Any, **kwargs: Any) -> Any:
        gates = getattr(_THREAD, "gates", None)
        if not gates:  # a thread with no gate active: untouched
            return original(adapter, request, **kwargs)
        return gates[-1].send(request, lambda: original(adapter, request, **kwargs), kwargs)

    HTTPAdapter.send = send  # type: ignore[method-assign,assignment]


def _unwrap() -> None:
    """Restore what :func:`_wrap` replaced (under ``_WRAP_LOCK``, last active block)."""
    from requests.adapters import HTTPAdapter

    HTTPAdapter.send = _WRAP["original"]  # type: ignore[method-assign]
    _WRAP["original"] = None


def _read_body(resp: Any, max_bytes: int) -> None:
    """Read ``resp``'s body now (under the host's lock; module docstring) unless already read or
    declared over ``max_bytes`` (the caller refuses that one unread, as fetchpdf's download does).
    A body that runs past ``max_bytes`` is kept as its first ``max_bytes + 1`` bytes and the
    connection closed: the caller's own cap (fetchpdf's; the lane's ``max_bytes``) refuses it.
    ``iter_content``/``.content`` then replay the bytes."""
    if getattr(resp, "_content_consumed", True) or getattr(resp, "_content", False) is not False:
        return
    length = str(resp.headers.get("Content-Length") or "")
    if length.isdigit() and int(length) > max_bytes:
        return
    buf = bytearray()
    over = False
    for chunk in resp.iter_content(1 << 16):
        buf += chunk
        if len(buf) > max_bytes:
            over = True
            break
    resp._content = bytes(buf[: max_bytes + 1])
    resp._content_consumed = True
    if over:
        resp.close()


def _response(request: Any, status: int, body: bytes, headers: Mapping[str, str], url: str) -> Any:
    """A complete ``requests.Response`` (content already read, so ``iter_content`` replays it)."""
    import requests
    from requests.structures import CaseInsensitiveDict

    resp = requests.Response()
    resp.status_code = status
    resp._content = body
    resp._content_consumed = True
    resp.headers = CaseInsensitiveDict(headers)
    resp.url = url
    resp.request = request
    resp.encoding = "utf-8"
    return resp

