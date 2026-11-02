"""Unit tests of :mod:`stack.provider_transport`: the response journal (:class:`JournalRequest`)
and the cross-process :class:`Gate` (cooldown, arXiv's single connection). Moved from the retired
``tests/test_metadata_v5.py``; conftest isolates ``STACK_PROVIDER_STATE`` and
``STACK_METADATA_SPOOL`` per test."""
from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from stack.provider_transport import Cooldown, Gate, JournalRequest


def test_response_journal_replays_after_new_instance(tmp_path):
    calls = []
    class Raw:
        status = 200
        headers = {}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return b'{"title":"Saved"}'
    def opener(*args, **kwargs):
        calls.append(1)
        return Raw()
    first = JournalRequest('test', opener=opener)
    assert first('get', 'https://example.test/work').json()['title'] == 'Saved'
    second = JournalRequest('test', opener=lambda *a, **k: pytest.fail('repeated HTTP'))
    assert second('get', 'https://example.test/work').json()['title'] == 'Saved'
    assert second.replayed == 1
    assert len(calls) == 1


def test_shared_cooldown_prevents_next_request():
    first = Gate('https://api.openalex.org/works')
    with first.enter():
        first.observe(429, {'Retry-After': '600'})
    with pytest.raises(Cooldown):
        with Gate('https://api.openalex.org/works/123').enter():
            pytest.fail('budget should block')


def test_arxiv_single_connection_across_gate_instances(monkeypatch):
    monkeypatch.setattr(Gate, 'interval', lambda _: 0.)
    active = 0
    maximum = 0
    lock = threading.Lock()
    def worker(url):
        nonlocal active, maximum
        with Gate(url).enter():
            with lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(.02)
            with lock: active -= 1
    threads = [threading.Thread(target=worker, args=(u,)) for u in
               ['https://export.arxiv.org/api/query', 'https://oaipmh.arxiv.org/oai']]
    for t in threads: t.start()
    for t in threads: t.join()
    assert maximum == 1


@pytest.mark.slow
def test_s2_arrivals_are_an_interval_apart_whatever_the_setup_delay(monkeypatch):
    """S25-17 / L3-3 (D444), time-scaled: S2's interval set to 0.2 s; two threads (a collect GET
    through ``throttled_open`` and a fetchpdf-style direct ``Gate.enter``) each send 6 requests to S2
    through a fake network whose connect/TLS setup alternates 0.15 s / 0 s before the request
    "arrives", then 0.05 s of response. Stamped only at dispatch, a slow setup followed by a fast one
    lands two arrivals ~0.05 s apart; re-stamped when the response ends, arrivals stay >= 0.2 s apart."""
    import urllib.request

    from stack import provider_transport as pt
    interval = 0.2
    monkeypatch.setitem(pt._INTERVALS, 's2', interval)
    monkeypatch.setattr(pt, 'CHANNEL_PACED', {})  # D550: else the channel's 2.5 s wins
    arrivals: list[float] = []
    lock = threading.Lock()

    def network() -> None:
        with lock:
            setup = 0.15 if len(arrivals) % 2 == 0 else 0.0
        time.sleep(setup)
        with lock:
            arrivals.append(time.monotonic())
        time.sleep(0.05)

    class Raw:
        status = 200
        headers: dict[str, str] = {}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return b'{}'

    def opener(req, **kw):
        network()
        return Raw()

    def collect():
        for _ in range(6):
            req = urllib.request.Request('https://api.semanticscholar.org/graph/v1/paper/search')
            with pt.throttled_open(req, opener=opener) as resp:
                resp.read()

    def fetchpdf():
        for _ in range(6):
            gate = Gate('https://api.semanticscholar.org/graph/v1/paper/X')
            with gate.enter():
                network()
                gate.observe(200, {})

    threads = [threading.Thread(target=f) for f in (collect, fetchpdf)]
    for t in threads: t.start()
    for t in threads: t.join()
    gaps = [b - a for a, b in zip(arrivals, arrivals[1:])]
    assert len(arrivals) == 12
    assert min(gaps) >= interval - 0.005, [round(g, 3) for g in gaps]


_PLAN_429 = (b'{"error": "Plan upgrade required", "message": "The \\"from_created_date:2026-10-01\\" filter '
             b'requires a Premium, Institutional, or Partner plan."}')


def test_plan_refusal_reads_the_error_field_case_insensitively():
    """D583: the body's `error` decides, any case; the message is returned as the provider wrote it."""
    from stack.provider_transport import plan_refusal

    assert plan_refusal(_PLAN_429).startswith('The "from_created_date:2026-10-01" filter requires')
    assert plan_refusal({"error": "PLAN UPGRADE REQUIRED"}) == "PLAN UPGRADE REQUIRED"
    assert plan_refusal(b'{"error": "Rate limit exceeded", "retryAfter": 5}') is None
    assert plan_refusal(b'not json') is None and plan_refusal(None) is None


def test_an_openalex_plan_refusal_sets_no_cooldown_other_families_unchanged():
    """D583: the Gate skips the cooldown only for an OpenAlex plan refusal; a plain OpenAlex 429 still
    cools down 60 s, and another family's 429 with the same body is handled as before (S2's backoff)."""
    oa = Gate('https://api.openalex.org/works')
    oa.observe(429, {}, _PLAN_429)
    assert 'cooldown' not in oa.read()
    oa.observe(429, {}, b'{"error": "Rate limit exceeded"}')
    assert 55 < oa.read()['cooldown'] - time.time() <= 60
    s2 = Gate('https://api.semanticscholar.org/graph/v1/paper/batch')
    assert s2.observe(429, {}, _PLAN_429) == 5.  # D550's first backoff step
    assert s2.read()['cooldown'] > time.time()


# ---------------------------------------------------------------- D588: limited answers
def test_limited_delay_lengthens_a_step_to_the_stated_wait_up_to_the_cap() -> None:
    """The ladder step, lengthened to a 429's or 503's stated wait (header or body); ``None`` (no
    retry) past the cap or for a plan refusal (D583); other statuses keep the step."""
    from stack.provider_transport import LIMIT_WAIT_MAX_S, limited_delay

    assert LIMIT_WAIT_MAX_S == 120.0
    assert limited_delay(429, {}, b'', 2.0) == 2.0
    assert limited_delay(429, {'Retry-After': '30'}, b'', 2.0) == 30.0
    assert limited_delay(503, {'retry-after': '1'}, b'', 4.0) == 4.0
    assert limited_delay(429, {}, b'{"retryAfter": 90}', 8.0) == 90.0
    assert limited_delay(429, {'Retry-After': '121'}, b'', 2.0) is None
    assert limited_delay(429, {'Retry-After': '121'}, b'', 2.0, cap=300.0) == 121.0
    assert limited_delay(429, {}, b'{"error": "Plan upgrade required"}', 2.0) is None
    assert limited_delay(500, {'Retry-After': '600'}, b'', 2.0) == 2.0
    assert limited_delay(None, {}, b'', 2.0) == 2.0


def test_every_synthetic_cooldown_answer_is_marked(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """``throttled_open``, ``JournalRequest`` and ``attach_requests_gate`` answer an active cooldown
    with a 429 carrying ``X-Stack-Cooldown`` (``is_cooldown``), so no retry loop takes it for the
    provider's answer (D588). Before, the last two carried no marker."""
    import urllib.error
    import urllib.request
    from email.message import Message

    import requests

    from stack.provider_transport import attach_requests_gate, is_cooldown, throttled_open

    url = 'https://api.openalex.org/works'
    Gate(url).observe(429, {}, b'')  # a 60 s cooldown for the family
    with pytest.raises(urllib.error.HTTPError) as exc:
        with throttled_open(urllib.request.Request(url), opener=lambda *a, **k: pytest.fail('sent')):
            pass
    assert exc.value.code == 429 and is_cooldown(exc.value.headers)
    from stack.provider_transport import COOLDOWN_MESSAGE  # what collect.openalex's _get_page reads
    assert __import__('json').loads(exc.value.read())['message'] == COOLDOWN_MESSAGE == 'Provider cooldown active'
    got = JournalRequest('t', opener=lambda *a, **k: pytest.fail('sent'), store_root=tmp_path)('GET', url)
    assert got.status_code == 429 and is_cooldown(got.headers)
    session = requests.Session()
    attach_requests_gate(session)
    monkeypatch.setattr(requests.adapters.HTTPAdapter, 'send', lambda *a, **k: pytest.fail('sent'))
    resp = session.get(url, timeout=5)
    assert resp.status_code == 429 and is_cooldown(resp.headers)
    assert not is_cooldown({'Retry-After': '5'}) and not is_cooldown(Message()) and not is_cooldown(None)


# ---------------------------------------------------------------- D700: a cooldown per credential
def test_keyless_and_keyed_callers_keep_separate_cooldowns(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """D700: OpenAlex's 429 to a keyless caller (fetchpdf's walks: their daily limit) cools only the
    keyless callers; a keyed collector's request (``Authorization: Bearer``) is still sent, and its own
    429 cools only that key. The keyless cooldown keeps the state file's old field; no key value is
    written. Through every Gate user: ``throttled_open``, ``JournalRequest``, ``attach_requests_gate``.
    Before, one cooldown per provider family held both."""
    import urllib.error
    import urllib.request

    import requests

    from stack.provider_transport import attach_requests_gate, credential_id, throttled_open

    monkeypatch.setenv('STACK_PROVIDER_STATE', str(tmp_path / 'providers'))
    url = 'https://api.openalex.org/works/W1'
    secret = 'k3y-not-in-any-file'
    keyed = {'Authorization': f'Bearer {secret}'}
    cid = credential_id(url, keyed)
    assert cid is not None and secret not in cid and credential_id(url, {}) is None
    assert credential_id(url + '?api_key=' + secret) is not None and credential_id(url + '?api_key=') is None
    Gate(url).observe(429, {'Retry-After': '50000'}, b'')  # the keyless daily limit
    sent: list[str] = []

    class Ok:
        status = 200
        headers: dict[str, str] = {}
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self, *a): return b'{}'

    def opener(req, **kw):
        sent.append(req.full_url)
        return Ok()

    with throttled_open(urllib.request.Request(url, headers=keyed), opener=opener):
        pass
    assert sent == [url]  # the keyed collector is not held by the keyless cooldown
    with pytest.raises(urllib.error.HTTPError) as exc:
        with throttled_open(urllib.request.Request(url), opener=opener):
            pass
    assert exc.value.code == 429 and sent == [url]  # the keyless caller is
    got = JournalRequest('t', opener=opener, store_root=tmp_path)('GET', url, headers=keyed)
    assert got.status_code == 200 and len(sent) == 2
    assert JournalRequest('t2', opener=opener, store_root=tmp_path)('GET', url).status_code == 429
    Gate(url, cid).observe(429, {}, b'')  # the key's own 429: 60 s for that key only
    assert 55 < Gate(url, cid).cooldown_left() <= 60 and Gate(url, credential_id(url, {'x-api-key': 'other'})).cooldown_left() <= 0
    with pytest.raises(urllib.error.HTTPError):
        with throttled_open(urllib.request.Request(url, headers=keyed), opener=opener):
            pass
    session = requests.Session()
    attach_requests_gate(session)
    monkeypatch.setattr(requests.adapters.HTTPAdapter, 'send', lambda *a, **k: pytest.fail('sent'))
    assert session.get(url, timeout=5).status_code == 429  # keyless: cooled
    state = (Gate(url).path).read_text()
    assert secret not in state and f'cooldown.{cid}' in state and '"cooldown"' in state
