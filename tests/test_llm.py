"""Pure tests of ``stack.llm`` (v3's 25, ported to the v4 client; D23).

Offline only: the fake model (``tests/fakes/llm.py``) stands in for OpenRouter. Covered here:
rendering and both hashes ({{ref}} vs {{input}}, golden values), the minimal schema validator,
rate-limit detection from status/headers only, fence-tolerant JSON recovery, provider routing
pin + post-hoc anomaly, the transport TLS wiring, the client's interface shape, and that the
transport is never called under a client lock. Ledger, cache, budget and fallback scenarios
live in ``tests/test_llm_ledger.py``.
"""

from __future__ import annotations

import json
import pathlib
import re
from typing import Any
from uuid import uuid4

import pytest

from stack.core import hashes
from stack.llm import (
    Ledger,
    LlmClient,
    LlmResponse,
    LlmResult,
    MissingRef,
    _extract_parsed,
    _is_rate_limited,
    _provider_anomaly,
    _recover_json_object,
    _served_provider_slug,
    estimate_tokens,
    render_call,
    strip_inputs,
    substitute_refs,
    validate_json,
)
from tests.fakes.llm import FakeLlm, ok

FIXTURE_ROOT = pathlib.Path(__file__).resolve().parent / "fixtures" / "llm"

# ── a small, stable schema (not the shipped file — golden-independent of workflow edits) ──
SCREEN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "rationale"],
    "properties": {
        "verdict": {"enum": ["relevant", "borderline", "irrelevant"]},
        "rationale": {"type": "string", "maxLength": 400},
        "buckets": {
            "type": "array",
            "items": {"enum": ["lab_safety_doc", "eval_report", "paper", "release_news"]},
        },
    },
}
VALID_OUT: dict[str, Any] = {"verdict": "relevant", "rationale": "clearly in scope"}

TEMPLATE = (
    "# Role\nYou screen documents.\n\n# Menus\n{{ref:enums}}\n\n"
    "# Document\n```document\n{{input:document}}\n```\n# Output\nJSON only.\n"
)
REFS = {"enums": "verdict ∈ {relevant, borderline, irrelevant}"}


@pytest.fixture()
def doc() -> str:
    """A per-test unique document (unique input_digest → unique cache key)."""
    return "Title: A Paper\n\nAbstract: " + uuid4().hex


def make_client(config: Any, tmp_path: pathlib.Path, transport: Any, run_id: str) -> LlmClient:
    return LlmClient(config=config, transport=transport, ledger=Ledger(),
                     run_id=run_id, stage="annotate.screen", cache=None)


# ═════════════════════════════ interfaces.py conformance ════════════════════════════════════


def test_satisfies_interfaces_protocol() -> None:
    """``complete()`` keeps v3's keyword-only signature (``spec/types/interfaces.py``, loaded by
    path) as its leading parameters, and ``LlmResult`` carries every LLM column of an evidence
    row (EVI, DESIGN A.1). Ported: v3 compared ``LlmResult`` field-for-field with the v3
    interface; v4's result is defined by A.1 instead."""
    import dataclasses
    import importlib.util
    import inspect
    import sys

    spec_path = pathlib.Path(__file__).resolve().parent.parent / "spec" / "types" / "interfaces.py"
    spec = importlib.util.spec_from_file_location("stack_spec_interfaces", spec_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolves annotations via sys.modules
    spec.loader.exec_module(module)

    mine_sig = inspect.signature(LlmClient.complete)
    iface_sig = inspect.signature(module.LlmClient.complete)
    expected = ["role", "template", "refs", "user", "schema", "license_class", "max_tokens"]
    mine_params = [p for p in mine_sig.parameters.values() if p.name != "self"]
    assert [p.name for p in mine_params][: len(expected)] == expected
    assert [p.name for p in iface_sig.parameters.values() if p.name != "self"] == expected
    for p in mine_params:
        assert p.kind == inspect.Parameter.KEYWORD_ONLY
    assert mine_sig.parameters["max_tokens"].default is None
    for p in mine_params[len(expected):]:
        assert p.default is not inspect.Parameter.empty  # v4 additions are optional

    evi_llm = {"is_cache_hit", "template_digest", "model_digest", "prompt_name", "rendered_digest",
               "model", "response_digest", "cost_usd", "tokens_in", "tokens_out", "latency_ms"}
    mine = {f.name for f in dataclasses.fields(LlmResult)}
    assert evi_llm <= mine
    assert {"parsed", "attempts", "basis_digest"} <= mine


# ═════════════════════════════════════ pure-function tests ═══════════════════════════════════


def test_substitute_refs_replaces_ref_keeps_input() -> None:
    out = substitute_refs("A {{ref:enums}} B {{input:document}} C", {"enums": "MENU"})
    assert out == "A MENU B {{input:document}} C"  # {{input}} stays literal (20-technical §3)


def test_substitute_refs_missing_ref_raises() -> None:
    with pytest.raises(MissingRef):
        substitute_refs("{{ref:taxonomy}}", {"enums": "x"})


def test_strip_inputs_removes_only_inputs() -> None:
    assert strip_inputs("a {{input:document}} b {{input:technique}} c") == "a  b  c"
    assert strip_inputs("keep {{ref:enums}}") == "keep {{ref:enums}}"


def test_render_call_prefix_and_sent_prefix() -> None:
    r = render_call(TEMPLATE, REFS, model="openai/gpt-6-luna", max_tokens=512, schema=SCREEN_SCHEMA)
    assert "{{ref:enums}}" not in r.prefix and REFS["enums"] in r.prefix
    assert "{{input:document}}" in r.prefix  # kept literal in the hash subject
    assert "{{input:document}}" not in r.sent_prefix  # stripped from the bytes we send
    assert r.params == {"model": "openai/gpt-6-luna", "max_tokens": 512}


def test_render_hash_golden() -> None:
    """rendered_hash / template_hash are stable golden values over a fixed template+refs+schema+
    params (20-technical §3). rendered_hash != template_hash because refs are substituted in one."""
    r = render_call(TEMPLATE, REFS, model="openai/gpt-6-luna", max_tokens=512, schema=SCREEN_SCHEMA)
    assert r.rendered_hash == "88bb08f43abb213d0b835b0b427e9bc7f319ac4b78a929f25ec634490ec892f6"
    assert r.template_hash == "a71c6ed8bbca56237ef260b27537bf9d9a8febfdc226369ee8f5363f2f8cba90"
    assert r.rendered_hash != r.template_hash


def test_template_hash_versionless() -> None:
    """template_hash is over the UNRENDERED template, so it does NOT change when a referenced
    config's rendered text (refs value) changes — only wording/params do (D-M8, I-M2)."""
    a = render_call(TEMPLATE, {"enums": "MENU-A"}, model="m/x", max_tokens=10, schema=SCREEN_SCHEMA)
    b = render_call(TEMPLATE, {"enums": "MENU-B-different"}, model="m/x", max_tokens=10, schema=SCREEN_SCHEMA)
    assert a.template_hash == b.template_hash  # refs content does not touch template_hash
    assert a.rendered_hash != b.rendered_hash  # but it does touch rendered_hash


def test_render_hash_matches_core_recipe() -> None:
    """The client never reimplements the hash — it delegates to stack.core.hashes (§3)."""
    r = render_call(TEMPLATE, REFS, model="m/x", max_tokens=42, schema=SCREEN_SCHEMA)
    params = {"model": "m/x", "max_tokens": 42}
    assert r.rendered_hash == hashes.rendered_hash(substitute_refs(TEMPLATE, REFS), SCREEN_SCHEMA, params)
    assert r.template_hash == hashes.template_hash(TEMPLATE, SCREEN_SCHEMA, params)


def test_estimate_tokens() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("a" * 400) == 100
    # The Chinese-coverage work: one token per CJK character, char/4 over the rest (a Chinese prompt was under-reserved ~4x)
    assert estimate_tokens("人工智能安全") == 6
    assert estimate_tokens("人工智能安全 AI safety") == 6 + 3  # 10 non-CJK chars -> 3
    assert estimate_tokens("인공지능 안전") == 6 + 1
    assert estimate_tokens("é" * 8) == 2  # non-CJK non-ASCII stays char/4


@pytest.mark.parametrize(
    "instance,ok",
    [
        ({"verdict": "relevant", "rationale": "x"}, True),
        ({"verdict": "maybe", "rationale": "x"}, False),  # bad enum
        ({"verdict": "relevant"}, False),  # missing required
        ({"verdict": "relevant", "rationale": "x", "extra": 1}, False),  # additionalProperties:false
        ({"verdict": "relevant", "rationale": "x", "buckets": ["paper"]}, True),
        ({"verdict": "relevant", "rationale": "x", "buckets": ["nope"]}, False),  # bad item enum
        ({"verdict": "relevant", "rationale": 5}, False),  # wrong type
    ],
)
def test_validate_json_screen(instance: dict[str, Any], ok: bool) -> None:
    assert (validate_json(instance, SCREEN_SCHEMA) == []) is ok


def test_validate_json_lenient_ref() -> None:
    """A cross-file $ref is treated leniently (no error) — the validator's job is retry/degrade
    decisions, not full Draft 2020-12 resolution."""
    assert validate_json({"anything": 1}, {"$ref": "_core.schema.json#/$defs/grounding_idx"}) == []


def test_shipped_templates_render() -> None:
    """Every shipped workflow template renders when given the refs it references, and raises
    MissingRef when a referenced kind is absent (the {{ref}}/{{input}} legend contract). The
    legend comment's ``{{ref:<kind>}}`` is not a real placeholder (``<`` is outside the grammar),
    so the check keys on the real placeholder regex, not a bare substring."""
    root = pathlib.Path(__file__).resolve().parent.parent / "workflows" / "annotate"
    ref_re = re.compile(r"\{\{ref:([a-z0-9_]+)\}\}")
    mds = sorted(root.rglob("*.md"))
    assert mds  # the shipped templates exist
    for md in mds:
        text = md.read_text()
        needed = set(ref_re.findall(text))
        out = substitute_refs(text, {k: f"<{k}>" for k in needed})
        assert ref_re.search(out) is None  # all real {{ref}} placeholders got substituted
        if needed:  # omitting a required ref must raise
            with pytest.raises(MissingRef):
                substitute_refs(text, {})


def test_rate_limit_from_headers_not_body() -> None:
    """Rate-limit detection is from status/headers ONLY — never reply text (the v1/taxman bug)."""
    assert _is_rate_limited(429, {})
    assert _is_rate_limited(503, {"Retry-After": "5"})
    assert _is_rate_limited(400, {"X-RateLimit-Remaining": "0"})
    assert not _is_rate_limited(400, {"X-RateLimit-Remaining": "9"})
    assert not _is_rate_limited(200, {})  # a 200 that merely mentions "rate limit" in its body is not one
    # A 2xx is a paid answer: its rate-limit headers are not read (L5-5, D427).
    assert not _is_rate_limited(200, {"Retry-After": "5"})
    assert not _is_rate_limited(200, {"X-RateLimit-Remaining": "0"})


@pytest.mark.parametrize("headers", [{"x-ratelimit-remaining": "0"}, {"Retry-After": "5"}],
                         ids=["remaining-0", "retry-after"])
def test_a_paid_200_with_rate_limit_headers_is_the_answer(config: Any, headers: dict[str, str]) -> None:
    """The last request of a provider's rate window can come back 200 with the window's headers:
    the answer is kept, not settled ``rate_limit`` and paid again (L5-5, D427)."""
    fake = FakeLlm(default=LlmResponse(200, headers, ok(VALID_OUT, cost=0.001).body))
    client = LlmClient(config=config, transport=fake, ledger=Ledger(), run_id="r-200-headers",
                       stage="annotate.screen", cache=None, budget_usd=12.0, sleep=lambda s: None)
    res = client.complete(role="classify_1k", template="Q {{ref:x}}", refs={"x": "y"}, user="doc",
                          schema=SCREEN_SCHEMA, license_class="open")
    assert res.parsed == VALID_OUT and fake.count() == 1
    assert [(a.status, a.error_class) for a in res.attempts] == [(200, None)]


# ═══════════════════════════════ transport TLS / proxy wiring (fix 4) ════════════════════════


def test_make_default_transport_ca_bundle_from_env(config: Any, monkeypatch: Any) -> None:
    from stack.llm import make_default_transport

    monkeypatch.delenv("AWS_CA_BUNDLE", raising=False)
    monkeypatch.setenv("SSL_CERT_FILE", "/some/ssl_cert_file.pem")
    assert make_default_transport(config)._ca_bundle == "/some/ssl_cert_file.pem"
    monkeypatch.delenv("SSL_CERT_FILE")
    monkeypatch.setenv("AWS_CA_BUNDLE", "/root/.ccr/ca-bundle.crt")
    assert make_default_transport(config)._ca_bundle == "/root/.ccr/ca-bundle.crt"
    monkeypatch.delenv("AWS_CA_BUNDLE")
    assert make_default_transport(config)._ca_bundle is None


def test_urllib_transport_builds_ssl_context() -> None:
    import os
    import ssl

    from stack.llm import UrllibTransport

    assert UrllibTransport(api_key_env="X")._ssl_context() is None  # no bundle → system trust
    cafile = ssl.get_default_verify_paths().cafile
    if not cafile or not os.path.exists(cafile):  # pragma: no cover — depends on host trust store
        pytest.skip("no system CA file to build a context from")
    ctx = UrllibTransport(api_key_env="X", ca_bundle=cafile)._ssl_context()
    assert isinstance(ctx, ssl.SSLContext)


# ═══════════════════════════ the key from an env file by path (D160 (C)) ═══════════════════════
DUMMY = "sk-or-v1-dummy-test-value-not-a-key"


def test_provider_key_env_first_then_the_env_file_by_path(tmp_path: pathlib.Path, monkeypatch: Any) -> None:
    import os

    from stack.llm import provider_key

    env = tmp_path / ".env"
    env.write_text(f"# comment\nSTACK_DB_URL=postgresql://u:p@h/db\nexport OPENROUTER_API_KEY='{DUMMY}'\n"
                   'STACK_MAILTO="ops@example.org"  # trailing\nPLAIN=abc # note\nEMPTY=\n')
    for name in ("STACK_MAILTO", "PLAIN", "EMPTY", "MISSING"):
        monkeypatch.delenv(name, raising=False)
    assert provider_key("OPENROUTER_API_KEY") is None  # conftest removed the key and the file path
    monkeypatch.setenv("STACK_ENV_FILE", str(env))
    assert provider_key("OPENROUTER_API_KEY") == DUMMY
    assert provider_key("STACK_MAILTO") == "ops@example.org" and provider_key("PLAIN") == "abc"
    assert provider_key("EMPTY") is None and provider_key("MISSING") is None
    assert "STACK_DB_URL" not in os.environ or os.environ["STACK_DB_URL"] != "postgresql://u:p@h/db"
    assert os.environ.get("OPENROUTER_API_KEY") is None  # read for the caller, never exported
    monkeypatch.setenv("OPENROUTER_API_KEY", "from-env")
    assert provider_key("OPENROUTER_API_KEY") == "from-env"  # the process environment wins


def test_provider_key_missing_env_file_is_fatal_and_names_only_the_path(tmp_path: pathlib.Path,
                                                                         monkeypatch: Any) -> None:
    from stack.llm import FatalLlmError, provider_key

    monkeypatch.setenv("STACK_ENV_FILE", str(tmp_path / "nope.env"))
    with pytest.raises(FatalLlmError, match="nope.env: cannot read"):
        provider_key("OPENROUTER_API_KEY")


def test_urllib_transport_sends_the_env_file_key(tmp_path: pathlib.Path, monkeypatch: Any) -> None:
    """The production transport reads the key at POST time from the file ``$STACK_ENV_FILE``
    names; ``urlopen`` is replaced, so nothing leaves the process."""
    import io
    import urllib.request

    from stack.llm import LlmRequest, UrllibTransport

    (tmp_path / ".env").write_text(f"OPENROUTER_API_KEY={DUMMY}\n")
    monkeypatch.setenv("STACK_ENV_FILE", str(tmp_path / ".env"))
    sent: list[Any] = []

    class Resp(io.BytesIO):
        status, headers = 200, {"content-type": "application/json"}

        def __enter__(self) -> Any:
            return self

        def __exit__(self, *_a: object) -> None:
            return None

    def fake_urlopen(req: Any, **_kw: Any) -> Any:
        sent.append(req)
        return Resp(b"{}")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    req = LlmRequest(url="https://openrouter.invalid/v1", headers={}, payload={"x": 1}, model="m/a",
                     attempt=1, rendered_hash="r", input_digest="i")
    assert UrllibTransport(api_key_env="OPENROUTER_API_KEY").post(req).status == 200
    assert sent[0].get_header("Authorization") == f"Bearer {DUMMY}"


def test_open_client_without_any_key_fails_before_a_reservation(config: Any, tmp_path: pathlib.Path,
                                                                 monkeypatch: Any) -> None:
    from stack.llm import FatalLlmError, open_client

    monkeypatch.delenv("STACK_LLM_TRANSPORT")  # conftest's fake: here the production path is asked
    ledger = tmp_path / "ledger.jsonl"
    with pytest.raises(FatalLlmError, match="OPENROUTER_API_KEY is unset and not in \\$STACK_ENV_FILE"):
        open_client(config, run_id="r", stage="s", store_root=None)
    assert not ledger.exists()


# ═══════════ fence-tolerant extraction + provider routing/anomaly (2026-07-21 live fixes) ══════


LIVE_ROOT = FIXTURE_ROOT / "live"

# ── fix 1: fence-tolerant JSON recovery (unit) ──────────────────────────────


def test_recover_bare_json_not_unwrapped() -> None:
    obj, unwrapped = _recover_json_object('{"verdict": "relevant", "rationale": "x"}')
    assert obj == {"verdict": "relevant", "rationale": "x"} and unwrapped is False


def test_recover_markdown_fence() -> None:
    obj, unwrapped = _recover_json_object('```json\n{"a": 1}\n```')
    assert obj == {"a": 1} and unwrapped is True


def test_recover_fence_with_prose() -> None:
    text = 'Sure — here is the result:\n```json\n{"a": 1, "b": 2}\n```\nHope that helps!'
    obj, unwrapped = _recover_json_object(text)
    assert obj == {"a": 1, "b": 2} and unwrapped is True


def test_recover_fence_inside_string_value_survives() -> None:
    """JSON whose STRING value contains ``` must NOT be over-stripped: bare JSON parses as-is,
    so ``unwrapped`` stays False and the backticks are preserved verbatim."""
    payload = {"rationale": "use ```json fences``` sparingly", "verdict": "relevant"}
    obj, unwrapped = _recover_json_object(json.dumps(payload))
    assert obj == payload and unwrapped is False
    assert obj["rationale"] == "use ```json fences``` sparingly"


def test_recover_fenced_json_with_inner_backticks() -> None:
    """A FENCED reply whose inner JSON string itself contains ``` (greedy close, not the inner)."""
    inner = {"note": "wrap in ```json``` please", "verdict": "borderline"}
    obj, unwrapped = _recover_json_object("```json\n" + json.dumps(inner) + "\n```")
    assert obj == inner and unwrapped is True


def test_recover_total_garbage_fails() -> None:
    obj, unwrapped = _recover_json_object("I could not comply.")
    assert obj is None and unwrapped is False


def test_extract_parsed_live_bedrock_fixture() -> None:
    """The real Bedrock body (fenced ```json) parses via fence recovery, flagged unwrapped."""
    body = json.loads((LIVE_ROOT / "bedrock_doc_class_fenced.json").read_text())
    parsed, err, unwrapped = _extract_parsed(body)
    assert err is None and unwrapped is True
    assert parsed is not None and parsed["doc_type"] == "paper" and parsed["self_published"] is True


# ── fix 2: provider routing pin + post-hoc anomaly ──────────────────────────


def test_provider_anomaly_helpers() -> None:
    assert _served_provider_slug({"provider": "Amazon Bedrock"}) == "amazon-bedrock"
    assert _served_provider_slug({"provider": "Google Vertex"}) == "google-vertex"
    assert _served_provider_slug({}) is None
    assert _provider_anomaly({"provider": "Amazon Bedrock"}, ["anthropic"]) == "Amazon Bedrock"
    assert _provider_anomaly({"provider": "Anthropic"}, ["anthropic"]) is None
    assert _provider_anomaly({}, ["anthropic"]) is None  # absent → cannot tell → not an anomaly


@pytest.mark.parametrize("license_class", ["open", "public_web", "restricted", "api_tos", "shadow"])
@pytest.mark.parametrize("role", ["screen", "screen_adjudicate"])
def test_openai_allowed_for_every_class(config: Any, doc: str, tmp_path: Any,
                                        license_class: str, role: str) -> None:
    tr = FakeLlm(default=ok(VALID_OUT))
    client = make_client(config, tmp_path, tr, "test-openai-permissions")
    result = client.complete(role=role, template=TEMPLATE, refs=REFS, user=doc,
                             schema=SCREEN_SCHEMA, license_class=license_class)
    assert result.ok
    assert tr.requests[0].model.startswith("openai/")
    assert tr.requests[0].payload["provider"]["only"] == ["openai"]
    assert tr.requests[0].payload["provider"]["data_collection"] == "deny"


def test_transport_not_called_under_client_lock(config: Any, doc: str, tmp_path: Any) -> None:
    """Two threads are inside ``transport.post`` at once (a barrier in the fake): the client
    holds no lock over network I/O. Ported from v3's upload-lock test (v4 has no blob upload)."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    barrier = threading.Barrier(2)
    tr = FakeLlm(default=ok(VALID_OUT), hook=lambda _req: barrier.wait(timeout=5))
    client = make_client(config, tmp_path, tr, "lock-test")

    def call(index: int) -> Any:
        return client.complete(role="screen", template=TEMPLATE, refs=REFS, user=doc + str(index),
                               schema=SCREEN_SCHEMA, license_class="open")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(call, range(2)))
    assert all(result.ok for result in results)
    assert client.ledger.outstanding() == []  # every reservation reconciled


# ═════════════════════════════ reasoning effort (techpipe D612) ═════════════════════════════

#: Captured at ab07b65, before the role field existed: a role without ``reasoning_effort`` must keep
#: these byte for byte (payload sha256 over sorted-key JSON, the three digests and the cache key).
NO_EFFORT_GOLDEN: dict[str, str] = {
    "payload": "5da68de4bd1431f1c33cd5fd88f22c44c24762531e6c72f782781c8de9ed4ea1",
    "template": "0419792878ce00b9b534d6c6bd7abf4fccb6e27088f015c48b8162dbadbeec84",
    "rendered": "0ace2e3f0f34ba2c0668bddcd45d6d06ac2aff7de757a5cb79217020e596efd2",
    "model_digest": "4e8c304415356212c3ad05eaa7ac649bc741db213fb0131ff942e59dea780b5e",
    "key": "61f3aa597ef91fa2eaa4df1e017b31468bd20b335592e50fa61a1a7994f4ece2",
}


def _effort_call(config: Any, role: str, cache: Any = None) -> tuple[LlmResult, FakeLlm]:
    tr = FakeLlm(default=ok(VALID_OUT))
    client = LlmClient(config=config, transport=tr, ledger=Ledger(), run_id="effort", stage="annotate.screen",
                       cache=cache)
    res = client.complete(role=role, template=TEMPLATE, refs=REFS, user="DOC", schema=SCREEN_SCHEMA,
                          license_class="open", max_tokens=16000)
    return res, tr


def _fingerprint(res: LlmResult, tr: FakeLlm) -> dict[str, str]:
    import hashlib

    from stack.llm.cache import cache_key

    payload = tr.requests[0].payload
    return {"payload": hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(),
            "template": res.template_digest, "rendered": res.rendered_digest, "model_digest": res.model_digest,
            "key": cache_key(res.model, res.rendered_digest, hashes.input_digest("DOC"))}


@pytest.mark.parametrize("role", ["extract_staged", "classify"])
def test_no_reasoning_effort_is_byte_identical_to_before(config: Any, role: str) -> None:
    """Unset effort: the wire payload, digests and cache key equal the pre-D612 goldens."""
    assert config.llm.roles[role].reasoning_effort is None
    res, tr = _effort_call(config, role)
    assert "reasoning" not in tr.requests[0].payload
    assert _fingerprint(res, tr) == NO_EFFORT_GOLDEN


def test_render_call_params_carry_reasoning_effort_only_when_set() -> None:
    plain = render_call(TEMPLATE, REFS, model="m/x", max_tokens=8, schema=SCREEN_SCHEMA)
    low = render_call(TEMPLATE, REFS, model="m/x", max_tokens=8, schema=SCREEN_SCHEMA, reasoning_effort="low")
    assert plain.params == {"model": "m/x", "max_tokens": 8}
    assert low.params == {"model": "m/x", "max_tokens": 8, "reasoning_effort": "low"}
    assert low.template_hash != plain.template_hash and low.rendered_hash != plain.rendered_hash


@pytest.mark.parametrize(("role", "effort"), [("extract_staged_low", "low"), ("extract_staged_high", "high")])
def test_reasoning_effort_is_sent_and_moves_every_digest(config: Any, role: str, effort: str) -> None:
    res, tr = _effort_call(config, role)
    assert tr.requests[0].payload["reasoning"] == {"effort": effort}
    fp = _fingerprint(res, tr)
    assert all(fp[k] != NO_EFFORT_GOLDEN[k] for k in fp), fp
    # the rest of the request is the default one's
    base = dict(tr.requests[0].payload)
    base.pop("reasoning")
    _res0, tr0 = _effort_call(config, "extract_staged")
    assert base == tr0.requests[0].payload


def test_response_cache_never_serves_a_default_effort_answer_to_an_effort_call(config: Any,
                                                                               tmp_path: pathlib.Path) -> None:
    from stack.llm.cache import ResponseCache

    cache = ResponseCache(tmp_path / "cache")
    _effort_call(config, "extract_staged", cache)  # writes the default-effort answer
    again, tr_again = _effort_call(config, "extract_staged", cache)
    assert again.is_cache_hit and tr_again.requests == []  # the cache works for the same params
    low, tr_low = _effort_call(config, "extract_staged_low", cache)
    assert not low.is_cache_hit and len(tr_low.requests) == 1


def test_batch_collect_request_carries_reasoning_effort(config: Any) -> None:
    from stack.llm import Deferred

    tr = FakeLlm(default=ok(VALID_OUT))
    client = LlmClient(config=config, transport=tr, ledger=Ledger(), run_id="effort-b", stage="annotate.screen",
                       cache=None)
    client.batch_collect()
    for role in ("extract_staged", "extract_staged_low"):
        with pytest.raises(Deferred):
            client.complete(role=role, template=TEMPLATE, refs=REFS, user="DOC", schema=SCREEN_SCHEMA,
                            license_class="open", max_tokens=16000)
    plain, low = client._batch_take()
    assert "reasoning" not in plain.request.payload
    assert low.request.payload["reasoning"] == {"effort": "low"}
    assert low.key != plain.key and low.model_digest != plain.model_digest


def test_role_reasoning_effort_value_is_checked(config: Any) -> None:
    from pydantic import ValidationError

    from stack.config import RoleConfig

    assert RoleConfig(model="m/x", max_tokens=1, reasoning_effort="low").reasoning_effort == "low"
    with pytest.raises(ValidationError):
        RoleConfig(model="m/x", max_tokens=1, reasoning_effort="extreme")  # type: ignore[arg-type]


# ═════════════════════════════ prior turns (techpipe D615) ══════════════════════════════════


def test_prior_turns_are_sent_in_order_and_enter_the_input_digest(config: Any, tmp_path: pathlib.Path) -> None:
    from stack.llm import conversation_input
    from stack.llm.cache import ResponseCache

    prior = [{"role": "user", "content": "DOC"}, {"role": "assistant", "content": '{"techniques": []}'}]
    cache = ResponseCache(tmp_path / "cache")
    tr = FakeLlm(default=ok(VALID_OUT))
    client = LlmClient(config=config, transport=tr, ledger=Ledger(), run_id="prior", stage="annotate.screen",
                       cache=cache)
    kw: dict[str, Any] = {"role": "extract_staged", "template": TEMPLATE, "refs": REFS, "schema": SCREEN_SCHEMA,
                          "license_class": "open", "max_tokens": 16000}
    plain = client.complete(user="DOC", **kw)
    two = client.complete(user="TAIL", prior=prior, **kw)
    assert [m["role"] for m in tr.requests[1].payload["messages"]] == ["system", "user", "assistant", "user"]
    assert [m["content"] for m in tr.requests[1].payload["messages"][1:]] == ["DOC", '{"techniques": []}', "TAIL"]
    assert two.basis_digest == hashes.input_digest(conversation_input(prior, "TAIL")) != hashes.input_digest("TAIL")
    assert plain.basis_digest == hashes.input_digest("DOC")  # no prior: the single-turn digest as before
    assert conversation_input((), "X") == "X"
    again = client.complete(user="TAIL", prior=prior, **kw)
    assert again.is_cache_hit and len(tr.requests) == 2
    other = client.complete(user="TAIL", prior=[*prior[:1], {"role": "assistant", "content": "{}"}], **kw)
    assert not other.is_cache_hit and len(tr.requests) == 3  # another pass-1 answer is another request
    from stack.llm import LlmError

    with pytest.raises(LlmError, match="prior turn"):
        client.complete(user="TAIL", prior=[{"role": "system", "content": "x"}], **kw)


def test_batch_collect_request_carries_prior_turns(config: Any) -> None:
    from stack.llm import Deferred

    tr = FakeLlm(default=ok(VALID_OUT))
    client = LlmClient(config=config, transport=tr, ledger=Ledger(), run_id="prior-b", stage="annotate.screen",
                       cache=None)
    client.batch_collect()
    with pytest.raises(Deferred):
        client.complete(role="extract_staged", template=TEMPLATE, refs=REFS, user="TAIL", schema=SCREEN_SCHEMA,
                        license_class="open", max_tokens=16000, prior=[{"role": "user", "content": "DOC"},
                                                                       {"role": "assistant", "content": "{}"}])
    (call,) = client._batch_take()
    assert [m["role"] for m in call.request.payload["messages"]] == ["system", "user", "assistant", "user"]
