"""D722: several providers per prompt: a provider-qualified model reference, the Anthropic Messages API dialect,
egress by a direct provider's slug, and the digests of every existing role and production pin left alone.

What is pinned here: the routing of a model reference (qualifier, else the role's provider, else the default); the
Anthropic request (``/messages``, ``x-api-key``, ``anthropic-version``, the system part with a ``cache_control``
breakpoint, merged same-role turns, ``output_config.format`` / ``.effort``, no ``temperature``, ``stream``); the
reply read through :func:`stack.llm.anthropic_view` (text blocks, usage with the cache fields, cost from the price
table); a refusal (``stop_reason: refusal``) as ``failed_permanent[query_invalid]`` without schema retries; the
retry / fatal / too-large classes on Anthropic statuses; the SSE reassembly; config validation; and golden values
for the requests of the 3.6.0 roles and the config digests of plan batch-2026-10a revision 7's annotation pins.
No network: every transport is a fake.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from stack.config import LlmConfig, RoleConfig, split_model_ref
from stack.core import hashes
from stack.llm import (
    AttemptsExhausted,
    ContextTooLarge,
    EgressRefused,
    FatalLlmError,
    Ledger,
    LlmClient,
    LlmError,
    LlmResponse,
    ProviderRefused,
    RouteInvalid,
    TransportError,
    anthropic_view,
    assemble_sse,
    render_call,
)
from tests.fakes.llm import FakeLlm, ok

REPO = Path(__file__).resolve().parents[1]

SCHEMA: dict[str, Any] = {"type": "object", "additionalProperties": False, "required": ["verdict", "score"],
                          "properties": {"verdict": {"type": "string"},
                                         "score": {"type": "integer", "minimum": 0, "maximum": 3}}}
TEMPLATE = "# Role\nYou grade documents.\n" + "Fixed instructions. " * 50


def anthropic_ok(parsed: Any, *, inp: int = 1_000, read: int | None = 40_000, write: int | None = 9_000,
                 out: int = 3_000, stop: str = "end_turn", model: str = "claude-sonnet-5-5") -> LlmResponse:
    """A 200 as the Messages API returns it: a (display-omitted) thinking block, the JSON text block, no cost."""
    usage: dict[str, Any] = {"input_tokens": inp, "output_tokens": out}
    if read is not None:
        usage["cache_read_input_tokens"] = read
    if write is not None:
        usage["cache_creation_input_tokens"] = write
    return LlmResponse(200, {}, {
        "id": "msg_x", "type": "message", "role": "assistant", "model": model,
        "content": [{"type": "thinking", "thinking": "", "signature": "sig"},
                    {"type": "text", "text": json.dumps(parsed)}],
        "stop_reason": stop, "stop_sequence": None, "usage": usage})


def refusal() -> LlmResponse:
    return LlmResponse(200, {}, {"id": "msg_r", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
                                 "content": [], "stop_reason": "refusal",
                                 "stop_details": {"category": "cyber", "explanation": None},
                                 "usage": {"input_tokens": 50_000, "output_tokens": 0}})


def llm_with(config: Any, **roles: dict[str, Any]) -> LlmConfig:
    """``config.llm`` plus ``roles`` (validated: the shape a real llm.yaml would load)."""
    data = config.llm.model_dump()
    data["roles"].update(roles)
    return LlmConfig.model_validate(data)


def client(llm: LlmConfig, tr: Any) -> LlmClient:
    return LlmClient(config=llm, transport=tr, ledger=Ledger(), run_id="anthropic", stage="annotate.x",
                     cache=None, sleep=lambda _s: None)


def call(c: LlmClient, role: str, **kw: Any) -> Any:
    return c.complete(role=role, template=TEMPLATE, refs={}, user=kw.pop("user", "DOC TEXT"),
                      schema=kw.pop("schema", SCHEMA), license_class=kw.pop("license_class", "open"),
                      max_tokens=16_000, prompt_name="set-v1", **kw)


SONNET = {"provider": "anthropic", "model": "claude-sonnet-5-5", "max_tokens": 16_000}


def test_anthropic_request_shape_and_cost(config: Any) -> None:
    """Red under: the chat-completions shape sent to the Messages API, the key as a Bearer token, ``minimum`` in
    the wire schema (the API takes no numerical constraints), the prompt counted as ``input_tokens`` alone (the
    uncached remainder), or the cache tokens priced at the plain input rate."""
    llm = llm_with(config, x_sonnet={**SONNET, "reasoning_effort": "high"})
    tr = FakeLlm(default=anthropic_ok({"verdict": "x", "score": 2}))
    res = call(client(llm, tr), "x_sonnet")
    (req,) = tr.requests
    p = req.payload
    assert req.url == "https://api.anthropic.com/v1/messages"
    assert req.headers == {"Content-Type": "application/json", "anthropic-version": "2026-10-21"}
    assert req.api_key_env == "ANTHROPIC_API_KEY" and req.key_header == "x-api-key"
    assert p["model"] == "claude-sonnet-5-5" and req.model == res.model == "claude-sonnet-5-5"
    assert p["max_tokens"] == 16_000 and p["stream"] is True
    assert p["system"] == [{"type": "text", "text": p["system"][0]["text"], "cache_control": {"type": "ephemeral"}}]
    assert p["system"][0]["text"].startswith("# Role\nYou grade documents.")
    assert p["messages"] == [{"role": "user", "content": [{"type": "text", "text": "DOC TEXT"}]}]
    assert p["cache_control"] == {"type": "ephemeral"}  # through the last block: a continuation reads the document
    assert p["output_config"]["effort"] == "high"
    fmt = p["output_config"]["format"]
    assert fmt["type"] == "json_schema" and fmt["schema"]["properties"]["score"] == {"type": "integer"}
    assert fmt["schema"]["additionalProperties"] is False
    for key in ("temperature", "provider", "response_format", "usage", "reasoning", "thinking"):
        assert key not in p, key
    assert res.parsed == {"verdict": "x", "score": 2} and not res.degraded
    assert res.tokens_in == 1_000 + 40_000 + 9_000 and res.tokens_out == 3_000
    assert (res.tokens_cached, res.tokens_cache_write, res.tokens_reasoning) == (40_000, 9_000, None)
    want = (1_000 * 2.0 + 40_000 * 0.2 + 9_000 * 2.5 + 3_000 * 10.0) / 1e6  # llm.yaml claude-sonnet-5-5
    assert res.cost_usd == pytest.approx(want)
    raw = anthropic_ok({"verdict": "x", "score": 2}).body  # the digest is of the raw body, not the view
    assert res.response_digest == hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()


def test_a_qualified_model_reference_routes_per_model(config: Any) -> None:
    """A pin's ``model: anthropic:claude-opus-5-5`` under an OpenAI-direct role goes to the Anthropic API with the
    bare id on the wire, is priced by the bare id's entry, and is the model in the digests as written. Red under:
    the role's provider winning over the qualifier, or the qualifier on the wire."""
    tr = FakeLlm(default=anthropic_ok({"verdict": "x", "score": 1}, read=None, write=None))
    res = call(client(config.llm, tr), "extract_main_openai", model="anthropic:claude-opus-5-5")
    (req,) = tr.requests
    assert req.url.endswith("/v1/messages") and req.payload["model"] == "claude-opus-5-5"
    assert res.model == "anthropic:claude-opus-5-5"
    assert res.cost_usd == pytest.approx((1_000 * 4.0 + 3_000 * 20.0) / 1e6)
    r = render_call(TEMPLATE, {}, model="anthropic:claude-opus-5-5", max_tokens=16_000, schema=SCHEMA)
    assert res.template_digest == r.template_hash
    assert split_model_ref("anthropic:claude-opus-5-5") == ("anthropic", "claude-opus-5-5")
    assert split_model_ref("openai/gpt-4o:free") == (None, "openai/gpt-4o:free")
    # unqualified: the role's provider (OpenAI direct), else the default (OpenRouter)
    tr2 = FakeLlm(default=ok({"verdict": "x", "score": 1}))
    call(client(config.llm, tr2), "extract_main_openai")
    call(client(config.llm, tr2), "extract_main")
    assert [q.url for q in tr2.requests] == ["https://api.openai.com/v1/chat/completions",
                                            "https://openrouter.ai/api/v1/chat/completions"]


def test_mixed_provider_fallback_and_refusal(config: Any) -> None:
    """A refusal is a paid attempt that moves to the next model without schema retries (here the unqualified
    fallback, which goes to the role's provider: the default, OpenRouter); on the last model it fails the call as
    failed_permanent[query_invalid]. Red under: a refusal parsed as schema-invalid and retried."""
    llm = llm_with(config, x_mixed={"model": "anthropic:claude-sonnet-5-5", "fallbacks": ["openai/gpt-6-luna"],
                                    "max_tokens": 16_000})
    tr = FakeLlm()
    tr.on(model="anthropic:claude-sonnet-5-5", reply=refusal())
    tr.on(model="openai/gpt-6-luna", reply=ok({"verdict": "y", "score": 0}))
    res = call(client(llm, tr), "x_mixed")
    assert [q.model for q in tr.requests] == ["anthropic:claude-sonnet-5-5", "openai/gpt-6-luna"]
    assert tr.requests[1].url == "https://openrouter.ai/api/v1/chat/completions"
    assert res.parsed["verdict"] == "y" and res.attempts[0].error_class == "refused"
    assert res.attempts[0].cost_usd == pytest.approx(50_000 * 2.0 / 1e6)

    llm2 = llm_with(config, x_sonnet=SONNET)
    tr2 = FakeLlm(default=refusal())
    with pytest.raises(ProviderRefused) as exc:
        call(client(llm2, tr2), "x_sonnet")
    assert (exc.value.status, exc.value.error_class) == ("failed_permanent", "query_invalid")
    assert "cyber" in str(exc.value) and len(tr2.requests) == 1
    assert exc.value.cost_usd == pytest.approx(50_000 * 2.0 / 1e6)  # type: ignore[attr-defined]


def test_gleaning_turn_and_folded_schema(config: Any) -> None:
    """D615 on the Anthropic API: the continuation's messages start with the first call's (so the cached document
    is read), consecutive user turns are merged (the API wants alternating roles), a non-strict schema goes folded
    from the first attempt (the API requires closed objects), and ``cache_prefix_only`` drops the tail breakpoint."""
    llm = llm_with(config, x_sonnet=SONNET)
    tr = FakeLlm(default=anthropic_ok({"verdict": "x", "score": 1}))
    c = client(llm, tr)
    call(c, "x_sonnet")
    call(c, "x_sonnet", user="GLEAN TURN",
         prior=[{"role": "user", "content": "DOC TEXT"}, {"role": "assistant", "content": '{"verdict": "x"}'}])
    first, second = (r.payload for r in tr.requests)
    assert second["messages"][: len(first["messages"])] == first["messages"]
    assert [m["role"] for m in second["messages"]] == ["user", "assistant", "user"]
    assert second["system"] == first["system"]

    open_schema = {"type": "object", "required": ["verdict"], "properties": {"verdict": {"type": "string"}}}
    res = call(c, "x_sonnet", schema=open_schema, cache_prefix_only=True)
    p = tr.requests[-1].payload
    assert res.degraded and "format" not in p.get("output_config", {})
    (msg,) = p["messages"]  # the document and the fold turn: one user message, two text blocks
    assert msg["role"] == "user" and [b["text"][:6] for b in msg["content"]] == ["DOC TE", "Return"]
    assert "cache_control" not in p and p["system"][0]["cache_control"] == {"type": "ephemeral"}


@pytest.mark.parametrize(("replies", "raises", "n"), [
    ([LlmResponse(529, {}, {"type": "error", "error": {"type": "overloaded_error"}})], None, 2),
    ([LlmResponse(429, {"retry-after": "3"}, {"type": "error", "error": {"type": "rate_limit_error"}})], None, 2),
    ([LlmResponse(200, {}, {"type": "error", "error": {"type": "overloaded_error"}})], None, 2),  # a stream's error
    ([LlmResponse(400, {}, {"type": "error", "error": {"type": "invalid_request_error",
                                                       "message": "prompt is too long: 1200000 tokens > 1000000 maximum"}})],
     ContextTooLarge, 1),
    ([LlmResponse(401, {}, {"type": "error", "error": {"type": "authentication_error"}})], FatalLlmError, 1),
    ([LlmResponse(404, {}, {"type": "error", "error": {"type": "not_found_error"}})] * 4, AttemptsExhausted, 4),
])
def test_anthropic_statuses(config: Any, replies: list[LlmResponse], raises: type[Exception] | None, n: int) -> None:
    """529 overloaded, 429 and an error event are retried (http_5xx / rate_limit); "prompt is too long" is
    too_large; 401 is fatal; an unknown model's 404 fails the call after the client's attempts."""
    llm = llm_with(config, x_sonnet=SONNET)
    tr = FakeLlm()
    tr.on(model="claude-sonnet-5-5", reply=[*replies, anthropic_ok({"verdict": "x", "score": 1})])
    if raises is None:
        assert call(client(llm, tr), "x_sonnet").parsed["verdict"] == "x"
    else:
        with pytest.raises(raises):
            call(client(llm, tr), "x_sonnet")
    assert len(tr.requests) == n


def test_truncated_reply_retries_at_twice_the_cap(config: Any) -> None:
    llm = llm_with(config, x_sonnet=SONNET)
    tr = FakeLlm()
    tr.on(model="claude-sonnet-5-5", reply=[anthropic_ok({"verdict": "x"}, stop="max_tokens"),
                                            anthropic_ok({"verdict": "x", "score": 1})])
    call(client(llm, tr), "x_sonnet")
    assert [q.payload["max_tokens"] for q in tr.requests] == [16_000, 32_000]


def test_egress_gates_a_direct_provider_by_its_slug(config: Any) -> None:
    """The Anthropic API is egress provider ``anthropic`` for every model it serves: refused for ``shadow`` (OpenAI
    only), allowed for ``restricted``, whatever the bare id looks like. Red under: a bare ``claude-…`` id gated out
    as an unmappable prefix, or let through for shadow."""
    llm = llm_with(config, x_sonnet=SONNET)
    tr = FakeLlm(default=anthropic_ok({"verdict": "x", "score": 1}))
    with pytest.raises(EgressRefused):
        call(client(llm, tr), "x_sonnet", license_class="shadow")
    assert not tr.requests
    call(client(llm, tr), "x_sonnet", license_class="restricted")
    assert len(tr.requests) == 1


def test_config_refuses_what_the_route_cannot_send(config: Any) -> None:
    """At load: an unknown qualifier, ``temperature`` or effort ``minimal`` on the Anthropic dialect, ``stream`` on
    another dialect. At call time (a pin's model): an unknown qualifier is fatal before any request."""
    with pytest.raises(ValueError, match="not in llm.providers"):
        llm_with(config, x={"model": "nosuch:m", "max_tokens": 10})
    with pytest.raises(ValueError, match="temperature"):
        llm_with(config, x={**SONNET, "temperature": 0.2})
    with pytest.raises(ValueError, match="minimal"):
        llm_with(config, x={"model": "anthropic:claude-haiku-5-5", "max_tokens": 10, "reasoning_effort": "minimal"})
    data = config.llm.model_dump()
    data["providers"]["openai"]["stream"] = True
    with pytest.raises(ValueError, match="stream"):
        LlmConfig.model_validate(data)
    tr = FakeLlm(default=ok({"verdict": "x", "score": 1}))
    c = client(config.llm, tr)
    with pytest.raises(RouteInvalid) as exc:
        call(c, "extract_main", model="nosuch:m")
    assert exc.value.error_class == "config_missing" and not tr.requests
    with pytest.raises(RouteInvalid):  # fatal: the client's later calls re-raise it
        call(c, "extract_main")


def test_names_of_a_qualified_reference_outside_the_client(config: Any) -> None:
    """The dataset's config id keeps four ``:``-separated parts (the bare model name), and a request built without
    a role (the eval harness's probe) still goes to the qualifier's provider."""
    from stack.fold.dataset import _strip_provider

    assert _strip_provider("anthropic:claude-opus-5-5") == "claude-opus-5-5"
    assert _strip_provider("openai/gpt-6-luna") == "gpt-6-luna" and _strip_provider(None) is None
    c = client(config.llm, FakeLlm())
    req = c._build_request(model="anthropic:claude-opus-5-5", sent_prefix="P", messages=[{"role": "user", "content": "U"}],
                           schema=SCHEMA, max_tokens=10, temperature=None, rule=c._egress_rule("open"), attempt=1,
                           rendered_hash="", input_digest="")
    assert req.url.endswith("/v1/messages") and req.payload["model"] == "claude-opus-5-5"


def test_batch_mode_refuses_a_direct_route(config: Any) -> None:
    c = client(config.llm, FakeLlm(default=ok({"verdict": "x"})))
    c.batch_collect()
    with pytest.raises(LlmError, match="batch mode sends only to the default provider"):
        call(c, "extract_main", model="anthropic:claude-opus-5-5")


def test_key_envs_cover_qualified_references(config: Any) -> None:
    """The deploy preflight's helper: the providers of a role's models and a pin's references."""
    llm = config.llm
    assert llm.key_envs(llm.roles["extract_main"]) == {"OPENROUTER_API_KEY"}
    assert llm.key_envs(llm.roles["extract_main_openai"]) == {"OPENAI_API_KEY"}
    assert llm.key_envs(llm.roles["extract_main_openai"], ["anthropic:claude-opus-5-5"]) == {
        "OPENAI_API_KEY", "ANTHROPIC_API_KEY"}


def _sse(*events: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for ev in events:
        lines += [f"event: {ev['type']}\n", f"data: {json.dumps(ev)}\n", "\n"]
    return lines


def test_sse_reassembly() -> None:
    """The streamed reply reassembled as the non-streamed body: content blocks from their deltas, the stop reason
    and the cumulative usage from message_delta. A stream cut before message_stop is a network failure."""
    events: list[dict[str, Any]] = [
        {"type": "message_start", "message": {"id": "msg_s", "type": "message", "role": "assistant", "content": [],
                                              "model": "claude-opus-5-5", "stop_reason": None,
                                              "usage": {"input_tokens": 10, "cache_read_input_tokens": 500,
                                                        "cache_creation_input_tokens": 0, "output_tokens": 1}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "s1"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "ping"},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": '{"verdict": '}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": '"x"}'}},
        {"type": "content_block_stop", "index": 1},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
         "usage": {"output_tokens": 42}},
        {"type": "message_stop"},
    ]
    body = assemble_sse(_sse(*events))
    assert body["content"] == [{"type": "thinking", "thinking": "", "signature": "s1"},
                               {"type": "text", "text": '{"verdict": "x"}'}]
    assert body["stop_reason"] == "end_turn" and body["usage"]["output_tokens"] == 42
    assert body["usage"]["cache_read_input_tokens"] == 500
    view = anthropic_view(body)
    assert view["choices"][0] == {"message": {"content": '{"verdict": "x"}'}, "finish_reason": "stop"}
    assert view["usage"] == {"prompt_tokens": 510, "completion_tokens": 42,
                             "prompt_tokens_details": {"cached_tokens": 500, "cache_write_tokens": 0}}
    with pytest.raises(TransportError, match="message_stop"):
        assemble_sse(_sse(*events[:-1]))
    err = assemble_sse(_sse(events[0], {"type": "error", "error": {"type": "overloaded_error"}}))
    assert err["type"] == "error" and "choices" not in anthropic_view(err)


def test_transport_streams_and_sends_the_key_header(monkeypatch: pytest.MonkeyPatch) -> None:
    """The production transport: the key in ``x-api-key`` (no Authorization header), a ``stream: true`` request read
    as server-sent events. Red under: a Bearer key to the Anthropic API, or the SSE body parsed as JSON."""
    import urllib.request

    from stack.llm import LlmRequest, UrllibTransport

    seen: dict[str, Any] = {}

    class Resp:
        status = 200
        headers = {"content-type": "text/event-stream"}

        def __enter__(self) -> Resp:
            return self

        def __exit__(self, *_a: Any) -> None:
            return None

        def __iter__(self) -> Any:
            return iter(line.encode() for line in _sse(
                {"type": "message_start", "message": {"id": "m", "content": [], "usage": {"input_tokens": 3}}},
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": "{}"}},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}},
                {"type": "message_stop"}))

    def urlopen(req: Any, timeout: float, context: Any) -> Resp:
        seen["headers"] = {k.lower(): v for k, v in req.header_items()}
        seen["timeout"] = timeout
        return Resp()

    monkeypatch.setenv("FAKE_ANTHROPIC_KEY", "not-a-real-key")
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    t = UrllibTransport(api_key_env="UNUSED")
    resp = t.post(LlmRequest(url="https://x/v1/messages", headers={"anthropic-version": "2026-10-21"},
                             payload={"stream": True}, model="m", attempt=1, rendered_hash="", input_digest="",
                             api_key_env="FAKE_ANTHROPIC_KEY", key_header="x-api-key"))
    assert seen["headers"]["x-api-key"] == "not-a-real-key" and "authorization" not in seen["headers"]
    assert resp.body["stop_reason"] == "end_turn" and resp.body["usage"] == {"input_tokens": 3, "output_tokens": 2}


def test_eval_usage_of_an_anthropic_cache_entry() -> None:
    """stack eval's usage from a cached raw Messages body: the prompt is the three input fields' sum."""
    from stack.config import ModelPrice
    from stack.evals.score import entry_usage

    entry = {"body": anthropic_ok({"v": 1}).body, "cost_usd": 0.5}
    u = entry_usage(entry, ModelPrice(in_usd_per_m=2.0, out_usd_per_m=10.0))
    assert (u["prompt_tokens"], u["cached_tokens"], u["cache_write_tokens"], u["completion_tokens"]) == (
        50_000, 40_000, 9_000, 3_000)


# ── digest stability (D722 changed no existing request) ─────────────────────────────────────────────────────

#: Per 3.6.0 role: sha256 of the wire payload of one fixed call through a fake (an ``ok`` reply), and the first 16
#: hex of its template / model / rendered digests, computed on the base of D722 (5afa9d1) and unchanged after it.
ROLE_GOLDEN = {
    "classify": ("0a5e72649aec19a7a605b263d718e90f04e151b337bb4f6214f44fe9611806f3", "cfc992af7b084da9", "fba00629103b3031", "f4ed386347319af3"),
    "classify_1k": ("9517504220ce5e86e64f105a9431d6c6db1433c5fcce470c0bcc0f9ccfd77621", "205c18ec721b1ec3", "e564824d7f3becc9", "9e3468fd2d52de2b"),
    "extract_cheap": ("94de24ac5b64c31680a84162cfb2d7be937e8373eeb73973d2fa0f9510668a88", "033a585ae7443a55", "9c1063b5e9d6e01c", "bfd8f7378c70ade6"),
    "extract_main": ("ef83ef4b912d6a2833d6c8eecf5827415ecbbf8bc45d283ba494ca33ec81101d", "31ca9c31e7665856", "ee35cbf452cd9f8c", "8eebcb91c55d1f35"),
    "extract_main_openai": ("edc11781a4491e67d0f4db8fac8ccd3b1df86c92f5c48646a6884a99fcc6c2db", "31ca9c31e7665856", "ee35cbf452cd9f8c", "8eebcb91c55d1f35"),
    "extract_staged": ("af3e86766fbb56554115cd6f55856de0bd4c4f786279db4579e23b2333e830e8", "ff7e707b361fcf58", "4e8c304415356212", "c02d7140ce95ccc1"),
    "extract_staged_high": ("3c1ec2f85135866c019612c9b5146fe16d91d88900c9db1ca3f9c50d5ae77c9f", "3ab8a359e1e32699", "56e41550ed3fa2ef", "bb25470ba08a24d6"),
    "extract_staged_low": ("f2bb2782f3f63413d1fab21f3e0a19d3e08d30059dae3e71ce586d35f852f530", "3564db98db596013", "fbe3425c7ab2adda", "9bae82a43ed9e699"),
    "extract_staged_medium": ("5c5eae5589d2609b0439c61acc4080f5b4762f9f0b72d76b89255dbc97760543", "e24900305258ae44", "480d8d6e92e57617", "3e7c73483d54582e"),
    "extract_staged_openai": ("c523004bca53e577e5a359fea880c5991f3cbff8fd613f579e5efba7283a10bc", "ff7e707b361fcf58", "4e8c304415356212", "c02d7140ce95ccc1"),
    "screen": ("0a5e72649aec19a7a605b263d718e90f04e151b337bb4f6214f44fe9611806f3", "cfc992af7b084da9", "fba00629103b3031", "f4ed386347319af3"),
    "screen_adjudicate": ("e97346d09e1f8a6662a8a10c725a6fdece31693a94c98dc2539f289df67d0698", "83dd237759f55633", "ec9fb29e881b7d13", "d98029307abf7f06"),
}


@pytest.mark.parametrize("role", sorted(ROLE_GOLDEN))
def test_existing_roles_send_the_same_bytes(config: Any, role: str) -> None:
    """Red under: any change to an existing role's request (OpenRouter or OpenAI dialect) or digests."""
    schema = {"type": "object", "additionalProperties": False, "required": ["v"], "properties": {"v": {"type": "string"}}}
    tr = FakeLlm(default=ok({"v": "x"}))
    res = client(config.llm, tr).complete(role=role, template="# T\n" + "fixed " * 300, refs={}, user="DOC",
                                          schema=schema, license_class="open", prompt_name="p")
    (req,) = tr.requests
    got = (hashlib.sha256(json.dumps(dict(req.payload), sort_keys=True).encode()).hexdigest(),
           res.template_digest[:16], res.model_digest[:16], res.rendered_digest[:16])
    assert got == ROLE_GOLDEN[role]


#: Plan batch-2026-10a revision 7's annotation pins (copied, so a later plan revision does not move this test) and
#: their config digests (D716's; the production runs' work keys).
PIN_GOLDEN: dict[str, tuple[dict[str, Any], str]] = {
    "annotate.screen": ({"name": "screen-v12", "model": "openai/gpt-6-luna", "adjudicate_model": "openai/gpt-6-luna"},
                        "63dc7d28127071f85deae9a3c81a0a934c2ff2c6595267b4779997e02d1f1184"),
    "annotate.affiliation": ({"name": "affiliation-v1", "role": "extract_main"},
                             "744785dd64f36b02083a42120468c289f5b5f65fcb594f2ea666f9bca5bd585d"),
    "annotate.technique_staged": ({"name": "technique-staged-v5-a1.1", "role": "extract_staged_openai", "mode": "single",
                                   "glean": True, "doc_cap": 900000, "max_tokens": 64000,
                                   "taxonomy": "taxonomy/techniques-1.0.0.yaml"},
                                  "afc8e6f6e2d638c68d762d9101884aba6b0e3d0001f83a610646b1b6b107af35"),
    "annotate.model_roles": ({"name": "model-roles-v3b", "role": "extract_main_openai", "doc_cap": 900000},
                             "bb1fcb36642568f1cd82c97e147b223f0665e010b4957c0135069fe34fe66c0a"),
}


#: Revision 9's catalogue pins (D738: techniques 1.1.0, models 1.1.0 as ``configs/models.yaml``): new config digests,
#: the same prompt bytes (tests/test_vendored_taxonomies.py).
PIN_GOLDEN_R9: dict[str, tuple[dict[str, Any], str]] = {
    "annotate.technique_staged": ({**PIN_GOLDEN["annotate.technique_staged"][0],
                                   "taxonomy": "taxonomy/techniques-1.1.0.yaml"},
                                  "2c2dbd49105f08ed0ae9c3ab8c1431dcc2e86ac4bca9403ea5967d0a7402b273"),
    "annotate.model_roles": (PIN_GOLDEN["annotate.model_roles"][0],
                             "ad8e4f41f458f5a560bbc1c2dddf2d418925f96f91d429748778905b8c3232e6"),
}


@pytest.mark.parametrize("stage", sorted(PIN_GOLDEN))
def test_production_pin_digests_unchanged(config: Any, stage: str, tmp_path: Path,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    """Revision 7's digests from revision 7's files: model_roles reads its catalogue from the config dir
    (``configs/models.yaml``, models 1.1.0 since D738), so the check runs on a copy with models 1.0.0 there."""
    from stack.annotate.stage import config_digest, resolve_pin
    from stack.stage_runner import load_stage_module

    cfg = tmp_path / "configs"
    shutil.copytree(REPO / "configs", cfg, ignore=shutil.ignore_patterns("plans", "models.yaml"))
    shutil.copyfile(REPO / "data-repo" / "taxonomies" / "models" / "models-1.0.0.yaml", cfg / "models.yaml")
    monkeypatch.setenv("STACK_CONFIG_DIR", str(cfg))
    pin, want = PIN_GOLDEN[stage]
    wf = load_stage_module(stage).WORKFLOW
    assert config_digest(wf.spec, pin, resolve_pin(wf, pin, lambda: config)) == want
    assert hashes.jcs(pin)  # the pin is plain data


@pytest.mark.parametrize("stage", sorted(PIN_GOLDEN_R9))
def test_revision_9_pin_digests(config: Any, stage: str) -> None:
    from stack.annotate.stage import config_digest, resolve_pin
    from stack.stage_runner import load_stage_module

    pin, want = PIN_GOLDEN_R9[stage]
    wf = load_stage_module(stage).WORKFLOW
    assert config_digest(wf.spec, pin, resolve_pin(wf, pin, lambda: config)) == want
    plan = yaml.safe_load((REPO / "configs" / "plans" / "batch-2026-10a.yaml").read_text("utf-8"))
    assert plan["prompts"][stage] == pin  # the production plan pins these
