"""Local cross-process API pacing and crash-safe response journaling.

No database access from network threads. Credentials are hashed for cache keys,
never included in filenames or stored request diagnostics.

The response journal (:class:`JournalRequest`) of a run against a store lives under
``<store>/cache/provider-responses/<run_id>/`` (:func:`journal_root`; pass ``store_root``);
without a store it falls back to ``$STACK_METADATA_SPOOL`` / ``~/.cache/stack/metadata-spool``
as in v3. The cross-process pacing state (:class:`Gate`) stays per machine
(``$STACK_PROVIDER_STATE``), since it coordinates every process that talks to a provider.
"""
from __future__ import annotations

import base64
import fcntl
import functools
import hashlib
import io
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any


def atomic_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f'.{path.name}.{os.getpid()}.{threading.get_ident()}.tmp')
    with tmp.open('w', encoding='utf8') as f:
        json.dump(data, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def family(url: str) -> str:
    host = urllib.parse.urlsplit(url).hostname or ''
    for domain, name in [('arxiv.org', 'arxiv'), ('openalex.org', 'openalex'),
                         ('semanticscholar.org', 's2'), ('crossref.org', 'crossref'),
                         ('datacite.org', 'datacite'), ('zenodo.org', 'zenodo'),
                         ('ncbi.nlm.nih.gov', 'pubmed')]:
        if host == domain or host.endswith('.' + domain):
            return name
    return host


#: Header :func:`throttled_open` sets on the synthetic 429 it raises for an active cooldown (no
#: request was sent): :class:`stack.collect.base.Transport` neither counts it as ``http_429`` nor
#: penalizes its pacer, and waits the cooldown out (D401). Every synthetic cooldown answer carries
#: it (``attach_requests_gate``, :class:`JournalRequest`, ``oa_adapter.RequestGate``; D588).
COOLDOWN_HEADER = 'X-Stack-Cooldown'
#: The ``message`` of a synthetic cooldown answer's JSON body (a caller that sees only the decoded
#: body, such as ``collect.openalex``'s ``_get_page``, tells it apart by this).
COOLDOWN_MESSAGE = 'Provider cooldown active'

#: Retry loops and limited answers (D588). A request's retry loop must not retry into our own Gate
#: cooldown: a cooldown answer (nothing sent) spends no retry step, and is waited out (its time
#: left) at most :data:`COOLDOWN_WAITS` times per request; a real 429 (or 503) that states a wait
#: (``Retry-After``, body ``retryAfter``) is retried no sooner than that wait; a wait the loop will
#: not sleep through ends the request at once with the limited answer, which its caller classifies
#: as before (``rate_limit``). The acquire loops (``stack.http.PoliteFetcher``,
#: ``stack.oa_adapter.RequestGate``) sleep at most :data:`LIMIT_WAIT_MAX_S` in all per request on
#: limited answers; the collect transport keeps its own threshold (``halt_429_after_s``, else
#: ``stack.collect.base.COOLDOWN_WAIT_MAX_S``, D401/D521/D550). A plan refusal (D583) is never
#: retried (:func:`limited_delay`).
LIMIT_WAIT_MAX_S = 120.
COOLDOWN_WAITS = 3


def is_cooldown(headers: Any) -> bool:
    """Whether an answer with ``headers`` (any mapping or ``Message``) is a synthetic Gate cooldown
    answer (:data:`COOLDOWN_HEADER`: nothing was sent)."""
    if not headers:
        return False
    return any(str(k).lower() == COOLDOWN_HEADER.lower() for k in headers.keys())

#: Seconds between requests per provider family the Gate paces (:meth:`Gate.interval`). For a
#: family in :data:`CHANNEL_PACED` this is only the fallback for a channel without a rate.
_INTERVALS = {'arxiv': 3.1, 'openalex': .15, 's2': 2.5, 'crossref': (.105, .21),
              'datacite': (.31, .61), 'zenodo': 2.1, 'pubmed': .35}

#: Families whose Gate interval is ``1 / politeness.rate_rps`` of a ``channels.yaml`` channel
#: (D550): one setting for every process that talks to the provider (collect, the lookups,
#: acquire). Only S2 so far: the other channels' ``rate_rps`` values are not the Gate's intervals.
CHANNEL_PACED: dict[str, str] = {'s2': 'semantic_scholar'}


@functools.cache
def _channel_rate(config_dir: str, channel: str) -> float | None:
    """``politeness.rate_rps`` of ``channel`` in ``<config_dir>/channels.yaml`` (read once per
    process), ``None`` when the channel sets none. An unreadable file raises."""
    from stack import yamlio

    data = yamlio.load((Path(config_dir) / 'channels.yaml').read_text())
    for ch in data.get('channels') or []:
        if ch.get('id') == channel:
            rate = (ch.get('politeness') or {}).get('rate_rps')
            if rate is None:
                return None
            if float(rate) <= 0:
                raise ValueError(f'channels.yaml {channel}: politeness.rate_rps {rate} is not > 0')
            return float(rate)
    return None


def gate_interval(name: str) -> float | None:
    """The Gate's interval for provider family ``name`` (:func:`family`), ``None`` for a host the
    Gate does not pace. Crossref/DataCite are faster with a contact mailto. A family in
    :data:`CHANNEL_PACED` takes its channel's ``politeness.rate_rps`` (the config directory as
    :mod:`stack.config` finds it)."""
    channel = CHANNEL_PACED.get(name)
    if channel is not None:
        from stack.config import _resolve_dir

        rate = _channel_rate(str(_resolve_dir(None)), channel)
        if rate is not None:
            return 1. / rate
    value = _INTERVALS.get(name)
    if isinstance(value, tuple):
        contact = bool(os.environ.get('STACK_METADATA_MAILTO') or os.environ.get('STACK_MAILTO'))
        return value[0] if contact else value[1]
    return value


#: Families whose Gate also re-stamps ``next`` when a response ends, so the provider sees arrivals
#: (not just dispatches) one interval apart (D444): S2's key quota is 1 request per second across
#: all endpoints, counted on arrival; D444 was written at a 1.05 s interval, a 50 ms margin
#: (S25-17 / L3-3); since D550 the interval is the channel's (2.5 s).
ARRIVAL_PACED = frozenset({'s2'})


#: Families whose 429 without a stated retry-after backs off exponentially (D550): the Gate's
#: cooldown is :data:`BACKOFF_START_S`, doubled for each further such 429 up to
#: :data:`BACKOFF_MAX_S`, and back to the start after a 2xx; the step is kept in the Gate's state
#: file (``backoff``), so every process shares it, as the cooldown. Other families wait 60 s.
#: S2 answers a throttled request with a bare 429 (no ``Retry-After``) and the next one a few
#: seconds later usually passes.
BACKOFF_FAMILIES = frozenset({'s2'})
BACKOFF_START_S = 5.
BACKOFF_MAX_S = 60.
#: Header the Gate's callers add to a backed-off 429 (:func:`mark_backoff`): the cooldown the Gate
#: set, which :func:`retry_seconds` reads when the provider stated no retry-after.
BACKOFF_HEADER = 'X-Stack-Backoff'


#: Families whose answer can be a refusal of the account's plan (D583): OpenAlex answers a filter its
#: plan does not include (``from_created_date``, ``from_updated_date`` without Premium) with a 429 whose
#: JSON body's ``error`` is "Plan upgrade required", no ``Retry-After``. That is no rate limit: the
#: Gate sets no cooldown for it (it would hold every local process of the family for 60 s).
PLAN_REFUSAL_FAMILIES = frozenset({'openalex'})


def plan_refusal(body: Any) -> str | None:
    """The provider's message when ``body`` (bytes, or an already decoded JSON value) is a plan
    refusal (D583: its ``error`` says "plan upgrade required", any case), else ``None``. The message
    is the body's ``message`` (else its ``error``), as the provider wrote it."""
    data = body
    if isinstance(body, (bytes, bytearray, str)):
        try:
            data = json.loads(body)
        except (ValueError, TypeError):
            return None
    if not isinstance(data, dict) or 'plan upgrade required' not in str(data.get('error') or '').lower():
        return None
    return str(data.get('message') or data.get('error'))


class Cooldown(Exception):
    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        super().__init__('Provider cooldown active')


def stated_retry_seconds(headers: Any, body: bytes = b'') -> float | None:
    """The wait the answer states (``Retry-After`` as seconds or a date, else a JSON body's
    ``retryAfter``), ``None`` when it states none."""
    headers = {str(k).lower(): v for k, v in headers.items()}
    value = headers.get('retry-after')
    if value:
        try:
            return max(0., float(value))
        except ValueError:
            try:
                return max(0., (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds())
            except (ValueError, TypeError):
                pass
    try:
        data = json.loads(body)
        if isinstance(data, dict) and data.get('retryAfter'):
            return max(0., float(data['retryAfter']))
    except (ValueError, TypeError):
        pass
    return None


def retry_seconds(headers: Any, body: bytes = b'') -> float:
    """How long to wait after a 429: the stated wait (:func:`stated_retry_seconds`), else the
    Gate's backoff (:data:`BACKOFF_HEADER`, D550), else 60 s."""
    stated = stated_retry_seconds(headers, body)
    if stated is not None:
        return stated
    backoff = {str(k).lower(): v for k, v in headers.items()}.get(BACKOFF_HEADER.lower())
    if backoff:
        try:
            return max(0., float(backoff))
        except ValueError:
            pass
    return 60.


def limited_delay(status: int | None, headers: Any, body: bytes, step: float,
                  cap: float = LIMIT_WAIT_MAX_S) -> float | None:
    """The sleep before a retry loop re-sends after a failed answer whose ladder step is ``step``
    (D588): ``step``, lengthened to the wait a 429 or 503 states (:func:`stated_retry_seconds`).
    ``None`` = do not retry, the request ends with this answer: the stated wait is over ``cap``, or
    the answer is a plan refusal (D583: no wait changes it)."""
    if status == 429 and plan_refusal(body) is not None:
        return None
    stated = stated_retry_seconds(headers, body) if status in (429, 503) else None
    if stated is None:
        return step
    if stated > cap:
        return None
    return max(step, stated)


def mark_backoff(headers: Any, seconds: float | None) -> None:
    """Add :data:`BACKOFF_HEADER` = ``seconds`` to ``headers`` (a ``Message`` or a mapping) when
    the Gate backed off (``seconds`` not ``None``, :meth:`Gate.observe`)."""
    if seconds is not None and headers is not None:
        headers[BACKOFF_HEADER] = f'{seconds:g}'


#: Query parameters that carry a provider credential (OpenAlex's ``api_key``; the OA lane strips the
#: same names, :data:`stack.oa_adapter.CREDENTIAL_PARAMS`).
CREDENTIAL_QUERY: frozenset[str] = frozenset({'api_key', 'apikey'})


def credential_id(url: str, headers: Any = None) -> str | None:
    """Which credential a request presents to its provider, as a short opaque id (``None`` =
    keyless): a digest of the first credential header (:data:`stack.http.CREDENTIAL_HEADERS`, in
    sorted order) or credential query parameter (:data:`CREDENTIAL_QUERY`) it carries. The value
    itself is never kept, logged or written; the id names the :class:`Gate`'s cooldown (D700)."""
    from stack.http import CREDENTIAL_HEADERS

    found: list[str] = []
    for k, v in (headers.items() if headers else ()):
        if str(k).lower() in CREDENTIAL_HEADERS and v:
            found.append(f'{str(k).lower()}={v}')
    for k, v in urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query, keep_blank_values=False):
        if k.lower() in CREDENTIAL_QUERY and v:
            found.append(f'{k.lower()}={v}')
    if not found:
        return None
    return hashlib.sha256(min(found).encode()).hexdigest()[:16]


class Gate:
    """File locks coordinate all local processes; arXiv holds one through response read.

    ``credential`` (:func:`credential_id`, D700): the cooldown and the S2 backoff are kept per
    credential, so a provider's 429 to keyless callers (fetchpdf's walks) does not hold the keyed
    collectors, nor the other way round; the keyless ones in the state file's ``cooldown`` and
    ``backoff`` (the format before D700), a keyed one in ``cooldown.<id>`` and ``backoff.<id>``. The
    pacing (``next``, ``interval``) and the slots stay per provider family: one machine, one rate."""
    def __init__(self, url: str, credential: str | None = None) -> None:
        self.url = url
        self.name = family(url)
        self.credential = credential
        suffix = '' if credential is None else f'.{credential}'
        self.cooldown_key, self.backoff_key = f'cooldown{suffix}', f'backoff{suffix}'
        root = Path(os.environ.get('STACK_PROVIDER_STATE', str(Path.home() / '.cache/stack/providers')))
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = root / (hashlib.sha256(self.name.encode()).hexdigest() + '.json')
        self.lock = self.path.with_suffix('.lock')

    def interval(self) -> float:
        interval = gate_interval(self.name)
        return 1. if interval is None else interval

    def read(self) -> dict[str, Any]:
        return json.loads(self.path.read_text()) if self.path.exists() else {}

    def cooldown_left(self) -> float:
        """Seconds left of this credential's cooldown (an unlocked read; ``<= 0``: none)."""
        return float(self.read().get(self.cooldown_key, 0)) - time.time()

    @contextmanager
    def enter(self, stop: threading.Event | None = None) -> Iterator[Gate]:
        capacity = {'arxiv': 1, 'openalex': 10, 's2': 1, 'crossref': 3,
                    'datacite': 2, 'zenodo': 1, 'pubmed': 2}.get(self.name, 2)
        slot = None
        while slot is None:
            for i in range(capacity):
                candidate = self.path.with_suffix(f'.slot{i}').open('a')
                try:
                    fcntl.flock(candidate, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    slot = candidate
                    break
                except BlockingIOError:
                    candidate.close()
            if slot is None:
                if stop is not None and stop.wait(.1):
                    raise InterruptedError('Network dispatch stopped')
                if stop is None:
                    time.sleep(.1)
        try:
            with self._enter(stop) as gate:
                yield gate
        finally:
            slot.close()

    @contextmanager
    def _enter(self, stop: threading.Event | None = None) -> Iterator[Gate]:
        with self.lock.open('a') as f:
            while True:
                try:
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if stop is not None and stop.wait(.1):
                        raise InterruptedError('Network dispatch stopped')
                    if stop is None:
                        time.sleep(.1)
            state = self.read()
            now = time.time()
            cooldown = state.get(self.cooldown_key, 0) - now
            if cooldown > 0:
                raise Cooldown(cooldown)
            delay = max(0., state.get('next', 0) - now)
            if stop is not None:
                if stop.wait(delay):
                    raise InterruptedError('Network dispatch stopped')
            else:
                time.sleep(delay)
            state['next'] = time.time() + max(self.interval(), state.get('interval', 0))
            atomic_json(self.path, state)
            if self.name != 'arxiv':
                fcntl.flock(f, fcntl.LOCK_UN)
            try:
                yield self
            finally:
                if self.name in ARRIVAL_PACED:
                    self._restamp(f)

    def _restamp(self, f: Any) -> None:
        """Push ``next`` to at least one interval after this response ended (D444): the stamp at
        dispatch spaces dispatches, but a request's bytes reach the provider after a variable connect
        and TLS setup, so two arrivals can come closer than the interval. Re-stamped at the end, the
        next dispatch (hence arrival) is at least one interval after this one's arrival, whatever
        the latency; holds for a family with one request in flight (the slot is still held here)."""
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            state = self.read()
            state['next'] = max(state.get('next', 0),
                                time.time() + max(self.interval(), state.get('interval', 0)))
            atomic_json(self.path, state)
        finally:
            if self.name != 'arxiv':
                fcntl.flock(f, fcntl.LOCK_UN)

    def observe(self, status: int, headers: Any, body: bytes = b'') -> float | None:
        """Record an answer: a 429 (or a 503 with ``Retry-After``) sets the cooldown, except a plan
        refusal of a :data:`PLAN_REFUSAL_FAMILIES` family (D583). Returns the
        backoff the Gate chose for a :data:`BACKOFF_FAMILIES` 429 that stated no wait (D550), else
        ``None``."""
        if self.name == 'arxiv':
            return self._observe(status, headers, body)
        with self.lock.open('a') as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            return self._observe(status, headers, body)

    def _observe(self, status: int, headers: Any, body: bytes = b'') -> float | None:
        headers = {str(k).lower(): v for k, v in headers.items()}
        state = self.read()
        backoff = None
        # D583: a plan refusal is not a rate limit: no cooldown
        refused = status == 429 and self.name in PLAN_REFUSAL_FAMILIES and plan_refusal(body) is not None
        if (status == 429 and not refused) or (status == 503 and headers.get('retry-after')):
            stated = stated_retry_seconds(headers, body)
            if stated is None and status == 429 and self.name in BACKOFF_FAMILIES:
                prev = float(state.get(self.backoff_key) or 0)
                backoff = min(BACKOFF_MAX_S, 2 * prev) if prev else BACKOFF_START_S
                state[self.backoff_key] = delay = backoff
            else:
                delay = 60. if stated is None else stated
            if self.name == 'openalex' and headers.get('x-ratelimit-remaining') == '0':
                try:
                    delay = max(delay, float(headers.get('x-ratelimit-reset', 60)))
                except ValueError:
                    pass
            state[self.cooldown_key] = time.time() + delay
        elif 200 <= status < 300:
            state.pop(self.backoff_key, None)  # D550: a success resets the backoff
        if self.name == 'crossref':
            try:
                limit = float(headers.get('x-rate-limit-limit', 0))
                interval = float(str(headers.get('x-rate-limit-interval', '1s')).rstrip('s'))
                if limit > 0:
                    state['interval'] = interval / limit * 1.05
            except ValueError:
                pass
        atomic_json(self.path, state)
        return backoff


@contextmanager
def throttled_open(req: Any, *, opener: Any = None, **kwargs: Any) -> Iterator[Any]:
    """urllib-compatible seam shared by collect and acquisition HTTP clients."""
    url = req.full_url if hasattr(req, 'full_url') else str(req)
    opener = opener or urllib.request.urlopen
    gate = Gate(url, credential_id(url, dict(req.header_items()) if hasattr(req, 'header_items') else None))
    if gate.name not in ('arxiv', 'openalex', 's2', 'crossref', 'datacite', 'zenodo', 'pubmed'):
        with opener(req, **kwargs) as resp:
            yield resp
        return
    try:
        with gate.enter():
            try:
                with opener(req, **kwargs) as resp:
                    gate.observe(resp.status, resp.headers)
                    yield resp
            except urllib.error.HTTPError as exc:
                body = exc.read()
                mark_backoff(exc.headers, gate.observe(exc.code, exc.headers, body))
                raise urllib.error.HTTPError(exc.url, exc.code, exc.reason, exc.headers, io.BytesIO(body)) from None
    except Cooldown as exc:
        from email.message import Message
        headers = Message()
        headers['Retry-After'] = str(exc.seconds)
        headers[COOLDOWN_HEADER] = '1'
        body = json.dumps({'retryAfter': exc.seconds, 'message': COOLDOWN_MESSAGE}).encode()
        raise urllib.error.HTTPError(url, 429, COOLDOWN_MESSAGE, headers, io.BytesIO(body)) from None


class Response:
    def __init__(self, status: int, content: bytes = b'', headers: Any = None, attempts: int = 1) -> None:
        self.status_code, self.content = status, content
        self.text = content.decode('utf8', 'replace')
        self.headers = dict(headers or {})
        self.attempts = attempts

    def json(self) -> Any:
        try:
            return json.loads(self.text)
        except ValueError:
            return None


def attach_requests_gate(session: Any) -> None:
    """Apply the shared gate to fetchpdf's metadata API transports, not file downloads."""
    from requests import Response as RequestsResponse
    from requests.adapters import HTTPAdapter

    class GatedAdapter(HTTPAdapter):
        def send(self, request: Any, *args: Any, **kwargs: Any) -> Any:
            gate = Gate(request.url, credential_id(request.url, request.headers))
            try:
                with gate.enter():
                    response = super().send(request, *args, **kwargs)
                    mark_backoff(response.headers, gate.observe(
                        response.status_code, response.headers,
                        response.content if response.status_code == 429 else b''))
                    return response
            except Cooldown as exc:
                response = RequestsResponse()
                response.status_code = 429
                response.url = request.url
                response.headers['Retry-After'] = str(exc.seconds)
                response.headers[COOLDOWN_HEADER] = '1'
                response._content = json.dumps({'error': COOLDOWN_MESSAGE}).encode()
                return response

    for prefix in ('https://api.openalex.org/', 'https://api.semanticscholar.org/',
                   'https://api.crossref.org/', 'https://api.datacite.org/',
                   'https://zenodo.org/api/', 'https://eutils.ncbi.nlm.nih.gov/'):
        session.mount(prefix, GatedAdapter())


#: The journal's directory under a store's ``cache/`` (store layout §7.2).
PROVIDER_RESPONSES = 'provider-responses'


def journal_root(store_root: str | Path | None = None) -> Path:
    """``<store>/cache/provider-responses`` for a store; else the v3 spool (env or ~/.cache)."""
    if store_root is not None:
        from stack.store.layout import CACHE
        return Path(store_root) / CACHE / PROVIDER_RESPONSES
    return Path(os.environ.get('STACK_METADATA_SPOOL', str(Path.home() / '.cache/stack/metadata-spool')))


class JournalRequest:
    def __init__(self, run_id: str, *, opener: Any = None, store_root: str | Path | None = None) -> None:
        self.root = journal_root(store_root) / run_id
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.stop = threading.Event()
        self.opener = opener or urllib.request.urlopen
        self.requests = 0
        self.replayed = 0
        self.activity = 'starting'
        self.compatible_roots: list[Path] = []
        self.blobs: Any = None

    def __call__(self, method: str, url: str, **kw: Any) -> Response:
        fingerprint = json.dumps([method, url, kw], sort_keys=True, default=str).encode()
        path = self.root / (hashlib.sha256(fingerprint).hexdigest() + '.json')
        if not path.exists():
            for root in self.compatible_roots:
                previous = root / path.name
                if previous.exists():
                    saved = json.loads(previous.read_text())
                    if saved.get('status') in (200, 404):
                        atomic_json(path, saved)
                        break
        if path.exists():
            saved = json.loads(path.read_text())
            # Retry transient responses after their cooldown, not on every resume.
            if saved['status'] not in (0, 429, 500, 502, 503, 504) or saved['retry_at'] > time.time():
                self.replayed += 1
                return Response(saved['status'], base64.b64decode(saved['body']), saved['headers'], attempts=0)
        if self.stop.is_set():
            raise InterruptedError('Network dispatch stopped')
        params = kw.get('params') or {}
        full = url + ('?' + urllib.parse.urlencode(params, doseq=True) if params else '')
        headers = dict(kw.get('headers') or {})
        mail = os.environ.get('STACK_METADATA_MAILTO') or os.environ.get('STACK_MAILTO', '')
        headers.setdefault('User-Agent', f'stack-metadata/5 (mailto:{mail})')
        body = kw.get('raw_body')
        if kw.get('json_body') is not None:
            body = json.dumps(kw['json_body']).encode()
            headers.setdefault('Content-Type', 'application/json')
        req = urllib.request.Request(full, data=body, headers=headers, method=method.upper())
        gate = Gate(url, credential_id(full, headers))
        self.activity = f'{gate.name}: waiting/requesting'
        try:
            with gate.enter(self.stop):
                self.requests += 1
                try:
                    with self.opener(req, timeout=kw.get('timeout', 30)) as resp:
                        result = Response(resp.status, resp.read(), resp.headers)
                except urllib.error.HTTPError as exc:
                    result = Response(exc.code, exc.read(), exc.headers)
                except (urllib.error.URLError, TimeoutError, OSError):
                    result = Response(0)
                mark_backoff(result.headers, gate.observe(result.status_code, result.headers, result.content))
        except Cooldown as exc:
            self.activity = f'{gate.name}: cooldown {exc.seconds:.0f}s'
            return Response(429, headers={'Retry-After': str(exc.seconds), COOLDOWN_HEADER: '1'}, attempts=0)
        # Authentication response bodies may contain bearer tokens: never journal them.
        if '/auth' not in url:
            atomic_json(path, {'status': result.status_code,
                'body': base64.b64encode(result.content).decode(),
                'headers': {k: v for k, v in result.headers.items() if k.lower() in
                            ('retry-after', 'x-ratelimit-remaining', 'x-ratelimit-reset')},
                'retry_at': time.time() + (retry_seconds(result.headers, result.content) if result.status_code == 429 else 60)})
        return result
