"""Provider keys and the OpenAlex spend ledger (``stack/collect/provider_keys.py``, T-RUN item 6):
the key read through ``STACK_ENV_FILE`` and sent only as a header through the meter; the ledger's
reserve / reconcile / release; the per-run caps stopping ``collect.openalex`` and
``collect.openalex-lookup`` cleanly; the keyless warning; the S2 key on the lookup; fetchpdf getting
neither ``.env.local`` nor a key (``stack/oa_adapter.py``, R8-1) while the OA lane's gate adds our S2 key
to the S2 API only (D592). A recognisable fake key is
searched for in everything a run writes. No network: ``tests/fakes/http.py``.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import textwrap
import types
from typing import Any

import pytest

from stack.collect import provider_keys as pk
from stack.http import HttpResponse
from stack.llm.ledger import forget_run
from tests.fakes.http import FakeHttp

OA_KEY = "fake-oa-key-7f3a9c41"
S2_KEY = "fake-s2-key-2b8e60d5"
REPO = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture
def env_file(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """Both keys in a ``STACK_ENV_FILE`` (the production mechanism), nothing in ``os.environ``."""
    path = tmp_path / "keys.env"
    path.write_text(f"# test keys\nexport OPENALEX_API_KEY='{OA_KEY}'\nSEMANTIC_SCHOLAR_API_KEY={S2_KEY}\n")
    monkeypatch.setenv("STACK_ENV_FILE", str(path))
    return path


def _readable(p: pathlib.Path) -> bytes:
    """A file's content as searchable bytes: zstd run records decompressed, parquet files as the
    JSON of their rows (their pages are compressed), anything else raw."""
    raw = p.read_bytes()
    if p.name.endswith(".zst"):
        import zstandard

        return zstandard.ZstdDecompressor().decompressobj().decompress(raw)
    if p.suffix == ".parquet":
        import pyarrow.parquet as pq

        return json.dumps(pq.read_table(p).to_pylist(), default=str).encode()
    return raw


def _grep_tree(root: pathlib.Path, needle: str, *, skip: set[pathlib.Path]) -> list[str]:
    """Every file under ``root`` (not in ``skip``) whose content (:func:`_readable`) contains
    ``needle``; also fails when nothing was searched."""
    hits, searched = [], 0
    for p in root.rglob("*"):
        if p.is_file() and p not in skip:
            searched += 1
            if needle.encode() in _readable(p):
                hits.append(str(p))
    assert searched, f"nothing written under {root}"
    return hits


# ─────────────────────────────────────────────────────────────── units
def test_keys_read_by_one_name_through_the_env_file(env_file: pathlib.Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    assert (pk.openalex_key(), pk.s2_key()) == (OA_KEY, S2_KEY)
    assert pk.s2_headers() == {"x-api-key": S2_KEY}
    assert "OPENALEX_API_KEY" not in os.environ and "SEMANTIC_SCHOLAR_API_KEY" not in os.environ
    monkeypatch.setenv("OPENALEXAPIKEY", "old-name-never-read")
    monkeypatch.setenv("STACK_ENV_FILE", str(env_file.parent / "empty.env"))
    (env_file.parent / "empty.env").write_text("")
    assert pk.openalex_key() is None and pk.s2_headers() == {}


def test_request_kind_and_prices() -> None:
    assert pk.request_kind("https://api.openalex.org/works?filter=title_and_abstract.search%3Ax") == "search"
    assert pk.request_kind("https://api.openalex.org/works?search=debate") == "search"
    assert pk.request_kind("https://api.openalex.org/works?filter=doi%3A10.1%2Fa") == "list"
    assert pk.request_kind("https://api.openalex.org/works/W1") == "single"
    assert pk.reserve_usd("search") == pytest.approx(0.0025)
    assert pk.reserve_usd("list") == pytest.approx(0.00025)


def test_ledger_cap_may_only_be_lowered(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The OpenAlex ledger is the run's own (in memory), capped per run at CAP_USD (env may only lower)."""
    try:
        assert pk.openalex_ledger("t-oa1").cap_usd == pk.CAP_USD == 10.0
        monkeypatch.setenv(pk.CAP_ENV, "2")
        assert pk.openalex_ledger("t-oa2").cap_usd == 2.0
        monkeypatch.setenv(pk.CAP_ENV, "11")
        with pytest.raises(pk.CollectError, match="only lower"):
            pk.openalex_ledger("t-oa3")
    finally:
        for r in ("t-oa1", "t-oa2", "t-oa3"):
            forget_run(r)


def _fake_collector(options: dict[str, Any] | None = None, budget: float | None = None,
                    run_id: str = "r1") -> Any:
    """What :class:`OpenAlexMeter` reads of a Collector: counts, the ctx ids, ``option``."""
    opts = dict(options or {})
    line = types.SimpleNamespace(options=opts, budget=types.SimpleNamespace(max_usd=budget))
    ctx = types.SimpleNamespace(run_id=run_id, stage="collect.openalex", plan_stage=line)

    def option(name: str, default: Any, *, env: str | None = None, cast: Any = None) -> Any:
        raw = opts.get(name, default)
        return cast(raw) if cast is not None and raw is not None else raw

    return types.SimpleNamespace(counts={}, ctx=ctx, option=option)


def test_meter_reserves_reconciles_and_releases(env_file: pathlib.Path) -> None:
    """Cost header → actual; else credits; else the remaining-usd drop; else the reserved amount; a transport
    failure releases; a non-OpenAlex host passes through untouched and unmetered."""
    c = _fake_collector()
    meter = pk.OpenAlexMeter.for_run(c)
    assert meter.keyed and meter.max_usd == pk.DEFAULT_RUN_MAX_USD
    answers = [
        HttpResponse(200, {"x-ratelimit-cost-usd": "0.0004", "x-ratelimit-credits-used": "1"}, b"{}", ""),
        HttpResponse(200, {"x-ratelimit-credits-used": "10", "x-ratelimit-remaining-usd": "9.0"}, b"{}", ""),
        HttpResponse(200, {"x-ratelimit-remaining-usd": "8.9985"}, b"{}", ""),
        HttpResponse(200, {}, b"{}", ""),
        HttpResponse(None, {}, b"", "", error="timeout"),
        HttpResponse(200, {}, b"ok", ""),
    ]
    seen: list[tuple[str, dict[str, str]]] = []

    def send(method: str, url: str, *, headers: Any, body: Any, timeout: float, max_bytes: int) -> HttpResponse:
        seen.append((url, dict(headers)))
        return answers.pop(0)

    wrapped = meter.wrap(send)
    search = "https://api.openalex.org/works?filter=title_and_abstract.search%3Ax"
    for _ in range(5):
        wrapped("GET", search, headers={"User-Agent": "t", "Authorization": "Bearer smuggled"},
                body=None, timeout=1, max_bytes=10)
    wrapped("GET", "https://example.org/x", headers={"User-Agent": "t"}, body=None, timeout=1, max_bytes=10)
    assert [h.get("Authorization") for _, h in seen[:5]] == [f"Bearer {OA_KEY}"] * 5
    assert "Authorization" not in seen[5][1] and all(OA_KEY not in u for u, _ in seen)
    lines = meter.ledger.lines()  # type: ignore[union-attr]
    kinds = [ln["kind"] for ln in lines]
    assert kinds == ["reserve", "reconcile"] * 4 + ["reserve", "release"]
    actual = [ln["actual_usd"] for ln in lines if ln["kind"] == "reconcile"]
    assert actual == pytest.approx([0.0004, 0.001, 0.0015, 0.0025])  # cost, credits, remaining drop, reserved
    assert meter.ledger.total() == pytest.approx(0.0054)  # type: ignore[union-attr]
    assert {ln["model"] for ln in lines} == {"openalex/search"}
    assert (c.counts["openalex_keyed_requests"], c.counts["openalex_released"],
            c.counts["openalex_reconciled_credits"], c.counts["openalex_reconciled_remaining_usd"],
            c.counts["openalex_reconciled_reserved"], c.counts["openalex_cost_micro_usd"],
            c.counts["openalex_reconciled_cost_usd"]) == (5, 1, 1, 1, 1, 5400, 1)
    assert OA_KEY not in json.dumps(meter.ledger.lines())  # type: ignore[union-attr]



def test_meter_never_sends_the_key_over_plain_http(env_file: pathlib.Path) -> None:
    """The OpenAlex key goes only to ``https://api.openalex.org`` on the default port (``key_url``): a plain-http
    or other-port request to that host is sent keyless (credentials stripped) and unmetered. Red under a
    host-only check (the key over http)."""
    c = _fake_collector()
    meter = pk.OpenAlexMeter.for_run(c)
    seen: list[dict[str, str]] = []

    def send(method: str, url: str, *, headers: Any, body: Any, timeout: float, max_bytes: int) -> HttpResponse:
        seen.append(dict(headers))
        return HttpResponse(200, {"x-ratelimit-credits-used": "1"}, b"{}", "")

    w = meter.wrap(send)
    for url in ("http://api.openalex.org/works?search=x", "https://api.openalex.org:8443/works?search=x",
                "https://API.OpenAlex.org:443/works?search=x"):
        w("GET", url, headers={"Authorization": "Bearer smuggled"}, body=None, timeout=1, max_bytes=1)
    assert [h.get("Authorization") for h in seen] == [None, None, f"Bearer {OA_KEY}"]
    assert c.counts.get("openalex_keyed_requests") == 1
    assert [pk.key_url(u, pk.HOST) for u in ("https://api.openalex.org/x", "http://api.openalex.org/x",
                                             "https://api.openalex.org.evil.test/x", "https://x@api.openalex.org/x",
                                             "https://api.openalex.org@evil.test/x", "https://[::1/x")] == [
        True, False, False, True, False, False]

def test_meter_caps_refuse_before_sending(env_file: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[str] = []

    def send(method: str, url: str, **kw: Any) -> HttpResponse:
        sent.append(url)
        return HttpResponse(200, {"x-ratelimit-credits-used": "10"}, b"{}", "")

    url = "https://api.openalex.org/works?search=x"
    # max_requests
    c = _fake_collector({"max_requests": 2})
    w = pk.OpenAlexMeter.for_run(c).wrap(send)
    w("GET", url, headers={}, body=None, timeout=1, max_bytes=1)
    w("GET", url, headers={}, body=None, timeout=1, max_bytes=1)
    with pytest.raises(pk.OpenAlexBudgetStop) as stop:
        w("GET", url, headers={}, body=None, timeout=1, max_bytes=1)
    assert stop.value.reason == "max_requests" and len(sent) == 2
    assert c.counts["openalex_budget_stop_max_requests"] == 1
    # max_usd (the line's budget.max_usd lowers the option): 0.0025 reserved, 0.001 actual each
    c2 = _fake_collector({"max_usd": 1.0}, budget=0.004, run_id="r2")
    m2 = pk.OpenAlexMeter.for_run(c2)
    assert m2.max_usd == pytest.approx(0.004)
    w2 = m2.wrap(send)
    w2("GET", url, headers={}, body=None, timeout=1, max_bytes=1)
    w2("GET", url, headers={}, body=None, timeout=1, max_bytes=1)  # 0.001 + 0.0025 <= 0.004
    with pytest.raises(pk.OpenAlexBudgetStop, match="max_usd"):
        w2("GET", url, headers={}, body=None, timeout=1, max_bytes=1)  # 0.002 + 0.0025 > 0.004
    assert len(sent) == 4
    # the per-run cap (CAP_USD, lowered by the env): a fresh run gets the whole cap, not what is left
    monkeypatch.setenv(pk.CAP_ENV, "0.003")
    c3 = _fake_collector(run_id="r3")
    w3 = pk.OpenAlexMeter.for_run(c3).wrap(send)
    w3("GET", url, headers={}, body=None, timeout=1, max_bytes=1)  # 0.0025 <= 0.003
    with pytest.raises(pk.OpenAlexBudgetStop, match="max_usd"):
        w3("GET", url, headers={}, body=None, timeout=1, max_bytes=1)  # 0.001 + 0.0025 > 0.003
    assert len(sent) == 5


def test_keyless_meter_warns_and_passes_through(tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    c = _fake_collector()
    meter = pk.OpenAlexMeter.for_run(c)
    assert not meter.keyed and meter.ledger is None and c.counts == {"openalex_keyless": 1}
    assert "OPENALEX_API_KEY" in capsys.readouterr().err
    got: list[dict[str, str]] = []
    meter.wrap(lambda m, u, *, headers, **kw: got.append(dict(headers)) or HttpResponse(200, {}, b"", u))(
        "GET", "https://api.openalex.org/works?search=x", headers={"Authorization": "Bearer x", "UA": "u"},
        body=None, timeout=1, max_bytes=1)
    assert got == [{"UA": "u"}]


# ─────────────────────────────────────────────────────────────── collect.openalex
def test_collect_openalex_keyed_run_stops_at_its_cap(tmp_path: pathlib.Path, env_file: pathlib.Path,
                                                     monkeypatch: pytest.MonkeyPatch,
                                                     capfd: pytest.CaptureFixture[str]) -> None:
    """Keyed: Bearer header on every request, never the URL; two pages reserved and reconciled from
    the credits header; ``max_requests: 2`` stops before the second query (no coverage row, run
    ``scope_complete = false``); a second run with ``max_usd`` stops the same way; the key is in
    no file the runs wrote and on no output stream."""
    import time

    from tests.synth.records import run_line
    from tests.test_collect_openalex import (
        API,
        FLOOR,
        W1,
        W2,
        W3,
        _filters,
        _oa_plan,
        _page,
        _params,
        _view,
    )

    for var in ("STACK_COLLECT_LIMIT", "STACK_COLLECT_FRACTION", "STACK_COLLECT_MAX_QUERIES",
                "STACK_OPENALEX_FROM", "STACK_OPENALEX_FULL", "STACK_OPENALEX_RETRY_FAILED_RUN", "STACK_MAILTO"):
        monkeypatch.delenv(var, raising=False)
    fake = FakeHttp()
    monkeypatch.setattr("stack.collect.base.urllib_send", fake)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    credits = {"X-RateLimit-Credits-Used": "10", "X-RateLimit-Remaining-USD": "9.5"}
    f0 = _filters()
    sup, rlhf = "group:superalignment", "group:rlhf"
    fake.add(API, _params(f0[sup]), json=_page([W1, W2], "c2"), headers=credits)
    fake.add(API, _params(f0[sup], "c2"), json=_page([W3]), headers=credits)
    fake.add(API, _params(f0[rlhf]), json=_page([W1]), headers=credits)
    store = tmp_path / "store"

    rec = run_line(store, _oa_plan(queries=[sup, rlhf], **{"from": FLOOR}, max_requests=2), "collect.openalex")
    assert rec.status == "ok" and rec.scope_complete is False, rec.error
    assert len(fake.calls) == 2 and all(c.headers.get("Authorization") == f"Bearer {OA_KEY}" for c in fake.calls)
    assert all(OA_KEY not in c.url for c in fake.calls)
    assert (rec.counts["openalex_keyed_requests"], rec.counts["openalex_cost_micro_usd"],
            rec.counts["openalex_budget_stop_max_requests"]) == (2, 2000, 1)
    assert "openalex_keyless" not in rec.counts
    cov = {json.loads(r["query"])["query"]: r["status"] for r in _view(store, "coverage.openalex")}
    assert cov == {sup: "ok"}  # rlhf not reached: no row, due next run
    # the run's spend is in its record (T-STORE: the per-run ledger, closed before the final record)
    assert rec.spend == {"usd_micro": {"openalex": 2000}, "reserved_open_usd_micro": 0, "batch_ids": [],
                         "abandoned_batch_ids": []}

    # run 2: rlhf is due; a $0.002 cap affords no 0.0025 reservation → stop before any request
    rec2 = run_line(store, _oa_plan(queries=[sup, rlhf], **{"from": FLOOR}, max_usd=0.002), "collect.openalex")
    assert rec2.status == "ok" and rec2.scope_complete is False, rec2.error
    assert len(fake.calls) == 2 and rec2.counts["openalex_budget_stop_max_usd"] == 1
    # run 3 with the default cap completes rlhf
    rec3 = run_line(store, _oa_plan(queries=[sup, rlhf], **{"from": FLOOR}), "collect.openalex")
    assert rec3.status == "ok" and rec3.scope_complete is True, rec3.error
    assert rec3.counts["openalex_max_micro_usd"] == 500_000 and len(fake.calls) == 3
    out = capfd.readouterr()
    assert OA_KEY not in out.out + out.err
    for r in (rec, rec2, rec3):
        assert OA_KEY not in json.dumps(r.model_dump(mode="json") if hasattr(r, "model_dump") else vars(r), default=str)
    assert _grep_tree(tmp_path, OA_KEY, skip={env_file}) == []


def test_collect_openalex_keyless_says_so(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
                                          capfd: pytest.CaptureFixture[str]) -> None:
    import time

    from tests.synth.records import run_line
    from tests.test_collect_openalex import (
        API,
        FLOOR,
        W1,
        _filters,
        _oa_plan,
        _page,
        _params,
    )

    monkeypatch.delenv("STACK_MAILTO", raising=False)
    monkeypatch.setenv("OPENALEXAPIKEY", OA_KEY)  # the old name: never read
    fake = FakeHttp()
    monkeypatch.setattr("stack.collect.base.urllib_send", fake)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    sup = "group:superalignment"
    fake.add(API, _params(_filters()[sup]), json=_page([W1]))
    rec = run_line(tmp_path / "store", _oa_plan(queries=[sup], **{"from": FLOOR}), "collect.openalex")
    assert rec.status == "ok" and rec.scope_complete is True, rec.error
    assert rec.counts["openalex_keyless"] == 1 and "openalex_keyed_requests" not in rec.counts
    assert all("Authorization" not in c.headers for c in fake.calls)
    assert "no OPENALEX_API_KEY" in capfd.readouterr().err


# ─────────────────────────────────────────────────────────────── the lookups
@pytest.mark.slow  # real store + runner
def test_openalex_lookup_cap_leaves_keys_due_and_s2_lookup_sends_its_key(
        tmp_path: pathlib.Path, env_file: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.test_collect_lookup import (
        ARXIV_DOI,
        DOCS,
        OA,
        S2,
        lookup_run,
        oa_params,
        publish_documents,
        view,
        work,
    )
    from workflows.fetch.metadata import main as lookup

    store = tmp_path / "store"
    publish_documents(store, DOCS)
    monkeypatch.setattr(lookup, "_sleep", lambda s: None)
    monkeypatch.delenv("STACK_MAILTO", raising=False)
    monkeypatch.delenv(lookup._ENV_LIMIT, raising=False)
    fake = FakeHttp()
    monkeypatch.setattr(lookup, "SEND", fake)
    fake.add(OA, oa_params("doi", [ARXIV_DOI, "10.1000/two"]), json={"results": [work("W1", ARXIV_DOI)]},
             headers={"X-RateLimit-Credits-Used": "1"})
    plan = lookup_run.__globals__["plan"]()
    plan.stages[0].options["max_requests"] = 1
    from tests.synth.records import run_line

    rec = run_line(store, plan, "collect.openalex-lookup")
    assert rec.status == "ok" and rec.scope_complete is False, rec.error
    assert len(fake.calls) == 1 and fake.calls[0].headers["Authorization"] == f"Bearer {OA_KEY}"
    assert rec.counts["openalex_budget_stop_max_requests"] == 1
    assert set(view(store, "sources.openalex-lookup")) == {f"doi:{ARXIV_DOI}", "doi:10.1000/two"}
    cov = view(store, "coverage.openalex-lookup")
    assert [json.loads(q)["by"] for q in cov] == ["doi"]  # the pmid batch: no row, its key stays due
    assert rec.spend == {"usd_micro": {"openalex": 100}, "reserved_open_usd_micro": 0, "batch_ids": [],
                         "abandoned_batch_ids": []}

    fake.add(S2, {"fields": lookup.S2_FIELDS}, method="POST", json=[None, None])
    rec2 = lookup_run(store, "collect.s2-lookup")
    assert rec2.status == "ok", rec2.error
    assert fake.calls[-1].headers.get("x-api-key") == S2_KEY and rec2.counts["s2_keyed"] == 1
    assert _grep_tree(tmp_path, OA_KEY, skip={env_file}) == []
    assert _grep_tree(tmp_path, S2_KEY, skip={env_file}) == []


# ─────────────────────────────────────────────────────────────── fetchpdf (R8-1)
def _oa_gate(fake: FakeHttp, counts: dict[str, int], cache: dict[Any, Any] | None = None) -> Any:
    from stack.http import HostPacer
    from stack.oa_adapter import RequestGate

    return RequestGate(pacer=HostPacer(monotonic=lambda: 0.0, sleep=lambda s: None), sleep=lambda s: None,
                       transport=fake.get, cache=cache,
                       count=lambda k, n: counts.__setitem__(k, counts.get(k, 0) + n))


S2_API = "https://api.semanticscholar.org/graph/v1/paper/DOI:10.1000/one"
#: Requests the OA lane's gate must send with no credential, each with one fetchpdf (or anyone) tried.
_UNKEYED = [("https://api.openalex.org/works/doi:10.1000/one?api_key=smuggled",
             {"Authorization": "Bearer smuggled"}),
            ("https://api.crossref.org/works/10.1000/one", {"x-api-key": "smuggled"}),
            ("https://api.unpaywall.org/v2/10.1000/one", {"apikey": "smuggled"}),
            ("https://api.core.ac.uk/v3/search/works", {"Authorization": "Bearer smuggled"}),
            ("https://www.semanticscholar.org/paper/abc", {"x-api-key": "smuggled"}),  # family s2, not the API
            ("https://pdfs.semanticscholar.org/ab/cd.pdf", {}),
            ("http://api.semanticscholar.org/graph/v1/paper/DOI:10.1000/plain", {}),  # the API host, plain http
            ("https://api.semanticscholar.org:8443/graph/v1/paper/DOI:10.1000/port", {}),  # another port
            ("https://journal.test/one.pdf", {"x-els-apikey": "smuggled"})]
_CRED = {"authorization", "x-api-key", "apikey", "x-els-apikey"}


def test_oa_gate_sends_our_s2_key_only_to_the_s2_api(env_file: pathlib.Path) -> None:
    """D592: the OA lane's gate attaches OUR S2 key (``provider_keys.s2_key``) to requests to
    ``api.semanticscholar.org`` only, after its credential strip; every other request (OpenAlex
    first of all) still carries none, whatever the caller added. The key is on the wire only: not
    in the response's request (fetchpdf's view), the run cache, the 429 records, a transport
    error's message; counted ``s2_keyed`` per request sent (not per cached answer)."""
    import requests

    fake = FakeHttp()
    fake.add(S2_API, json={"paperId": "p1"})
    for url, _ in _UNKEYED:
        fake.add(url.split("?")[0], json={})  # the gate drops ``api_key`` before sending
    s2_busy = "https://api.semanticscholar.org/graph/v1/paper/DOI:10.1000/two"
    fake.add(s2_busy, status=429)
    s2_down = "https://api.semanticscholar.org/graph/v1/paper/DOI:10.1000/three"
    fake.add(s2_down, json={})
    fake.inject(s2_down, status=None, times=9, error="timeout")
    counts: dict[str, int] = {}
    cache: dict[Any, Any] = {}
    gate = _oa_gate(fake, counts, cache)
    with gate.active():
        first = requests.get(S2_API, headers={"x-api-key": "smuggled"}, timeout=5)
        again = requests.get(S2_API, timeout=5)  # the run cache answers: nothing sent, not counted
        for url, headers in _UNKEYED:
            requests.get(url, headers=headers, timeout=5)
        requests.get(s2_busy, timeout=5)
        with pytest.raises(requests.ConnectionError) as failed:
            requests.get(s2_down, timeout=5)
    by_url = {c.url: {k.lower(): v for k, v in c.headers.items()} for c in fake.calls}
    assert by_url[S2_API]["x-api-key"] == S2_KEY
    assert by_url[s2_busy]["x-api-key"] == S2_KEY and by_url[s2_down]["x-api-key"] == S2_KEY
    for url, _ in _UNKEYED:
        sent = next(c for c in fake.calls if c.url.split("?")[0] == url.split("?")[0])
        assert not {k.lower() for k in sent.headers} & _CRED, sent
        assert "api_key" not in sent.url and "smuggled" not in json.dumps(sent.headers)
    assert first.status_code == 200 and again.status_code == 200
    assert [c.url for c in fake.calls].count(S2_API) == 1 and counts["cached"] == 1
    s2_sent = [c for c in fake.calls if c.url.startswith("https://api.semanticscholar.org/")]
    assert len(s2_sent) >= 3 and all(c.headers.get("x-api-key") == S2_KEY for c in s2_sent)
    assert counts["s2_keyed"] == len(s2_sent)  # each one sent (the 429's retries included)
    for resp in (first, again):
        assert not {k.lower() for k in resp.request.headers} & _CRED  # fetchpdf never sees the key
    limited = gate.take()
    assert [lim.url for lim in limited] == [s2_busy]
    assert S2_KEY not in repr(cache) + repr(limited) + str(failed.value) + repr(counts)


def test_oa_gate_never_sends_the_s2_key_on_a_redirect_hop(env_file: pathlib.Path) -> None:
    """D649 on the requests path (review-2 B2: verified clean, pinned here): a redirect from the S2
    API to another host, a downgrade to http or another port carries no key, and a same-host https hop
    carries it only because the gate attaches it to that request itself; no response's request
    (fetchpdf's view, the history included) carries it."""
    import requests

    fake = FakeHttp()
    base = "https://api.semanticscholar.org/graph/v1/paper/DOI:10.1000/"
    hops = {"cross": "https://elsewhere.test/r", "down": "http://api.semanticscholar.org/graph/v1/r",
            "port": "https://api.semanticscholar.org:8443/graph/v1/r", "same": base + "same-target"}
    for name, target in hops.items():
        fake.add(base + name, status=302, headers={"Location": target})
        fake.add(target, json={})
    counts: dict[str, int] = {}
    gate = _oa_gate(fake, counts)
    with gate.active():
        responses = [requests.get(base + name, timeout=5) for name in hops]
    keyed = {c.url for c in fake.calls if c.headers.get("x-api-key") == S2_KEY}
    assert keyed == {base + name for name in hops} | {hops["same"]}
    assert all(not {k.lower() for k in c.headers} & _CRED for c in fake.calls if c.url not in keyed)
    for resp in responses:
        for r in [*resp.history, resp]:
            assert not {k.lower() for k in r.request.headers} & _CRED


def test_oa_gate_without_an_s2_key_sends_none(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No ``SEMANTIC_SCHOLAR_API_KEY`` (environment or ``$STACK_ENV_FILE``): S2 asked keyless, as
    before D592; nothing counted."""
    import requests

    (tmp_path / "oa-only.env").write_text("OPENALEX_API_KEY=only-openalex\n")
    monkeypatch.setenv("STACK_ENV_FILE", str(tmp_path / "oa-only.env"))
    fake = FakeHttp()
    fake.add(S2_API, json={})
    counts: dict[str, int] = {}
    with _oa_gate(fake, counts).active():
        requests.get(S2_API, headers={"x-api-key": "smuggled"}, timeout=5)
    (call,) = fake.calls
    assert not {k.lower() for k in call.headers} & _CRED and "s2_keyed" not in counts


_FETCHPDF_PROBE = textwrap.dedent("""
    import json, os, sys
    sys.path.insert(0, sys.argv[1])
    from stack.oa_adapter import RequestGate, _load_fetchpdf
    from stack.http import HostPacer, HttpResponse
    import requests

    _, session = _load_fetchpdf(sys.argv[2], "probe@example.org")
    http = session.kwargs["resolver"].http
    seen = []

    ours = os.environ["SEMANTIC_SCHOLAR_API_KEY"]

    def transport(url, *, headers, timeout, max_bytes):  # our S2 key is reported, never printed
        seen.append({"url": url, "headers": {k: "<our-s2-key>" if v == ours else v for k, v in headers.items()}})
        return HttpResponse(200, {"content-type": "application/json"}, b"{}", url)

    gate = RequestGate(pacer=HostPacer(monotonic=lambda: 0.0, sleep=lambda s: None),
                       sleep=lambda s: None, transport=transport)
    hosts = ["https://api.openalex.org/works/doi:10.1/x", "https://api.semanticscholar.org/graph/v1/paper/x",
             "https://api.core.ac.uk/v3/search/works", "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"]
    with gate.active():
        for url in hosts:
            http.get(url, retries=1)
        requests.get(hosts[0] + "?api_key=smuggled", headers={"Authorization": "Bearer smuggled"})
    from fetchpdf import _env
    print(json.dumps({
        "seen": seen,
        "polite": {h: http.polite_headers(h) for h in ("api.openalex.org", "api.semanticscholar.org", "api.core.ac.uk")},
        "ncbi_params": http.polite_params("eutils.ncbi.nlm.nih.gov"),
        "dotenv_in_environ": os.environ.get("FROM_DOTENV_LOCAL"),
        "env_file": _env.ENV_FILE,
        "consts": [getattr(_env, n) for n in ("S2_API_KEY", "OPENALEX_API_KEY", "CORE_API_KEY", "ENTREZ_API_KEY")],
    }))
""")


@pytest.mark.slow  # a subprocess importing fetchpdf
def test_fetchpdf_gets_no_env_local_and_sends_no_key(tmp_path: pathlib.Path) -> None:
    """fetchpdf loads no ``.env.local``, holds no key constant and its client adds no credential; the
    gate strips whatever a request carries and adds only our S2 key to the S2 API (D592)."""
    pytest.importorskip("fetchpdf")
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / ".env.local").write_text("FROM_DOTENV_LOCAL=loaded\nOPENALEXAPIKEY=dotenv-oa-secret\n")
    script = tmp_path / "probe.py"
    script.write_text(_FETCHPDF_PROBE)
    import os

    env = {k: v for k, v in os.environ.items() if not k.endswith("API_KEY") and k != "STACK_ENV_FILE"}
    env.update(SEMANTIC_SCHOLAR_API_KEY="dummy-s2", OPENALEXAPIKEY="dummy-oa", COREAPIKEY="dummy-core",
               ENTREZ_EUTILS_API_KEY="dummy-ncbi", OPENALEX_API_KEY="dummy-new-oa",
               STACK_PROVIDER_STATE=str(tmp_path / "providers"))
    done = subprocess.run([sys.executable, str(script), str(REPO), str(tmp_path / "scratch")], cwd=cwd,
                          env=env, capture_output=True, text=True, timeout=300, check=False)
    assert done.returncode == 0, done.stderr[-3000:]
    out = json.loads(done.stdout.strip().splitlines()[-1])
    assert out["dotenv_in_environ"] is None and out["env_file"] is None
    assert out["consts"] == [None, None, None, None]
    assert out["polite"] == {"api.openalex.org": {}, "api.semanticscholar.org": {}, "api.core.ac.uk": {}}
    assert "api_key" not in out["ncbi_params"]
    assert len(out["seen"]) == 5
    for call in out["seen"]:
        creds = {k.lower(): v for k, v in call["headers"].items()}
        creds = {k: v for k, v in creds.items() if k in {"authorization", "x-api-key", "apikey"}}
        if call["url"].startswith("https://api.semanticscholar.org/"):  # D592: our key, added by our gate
            assert creds == {"x-api-key": "<our-s2-key>"}, call
        else:
            assert creds == {}, call
        assert "api_key" not in call["url"] and "smuggled" not in json.dumps(call)
    assert not any(s in done.stdout + done.stderr for s in ("dummy-", "dotenv-oa-secret"))
