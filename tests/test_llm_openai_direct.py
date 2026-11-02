"""D714: gpt-6-luna on the OpenAI API directly for the corpus annotations, with no fallback model.

What is pinned here: the request shape of a role whose ``provider`` has ``dialect: openai`` (no OpenRouter
routing block, ``max_completion_tokens``, ``prompt_cache_key`` shared by a call and its gleaning turn, the key's
env var name on the request, the model id without ``openai/``), the cost from the price table at the cached and
cache-write rates when the reply has no ``usage.cost``, the usage details on the result and on the evidence of
technique_staged / model_roles only, model_roles' explicit prefix-only caching, the no-fallback roles of the plan,
and the digests the new roles leave alone.
"""

from __future__ import annotations

from typing import Any

import pytest

from stack.annotate.evidence import evidence_row
from stack.config import ModelPrice
from stack.llm import AttemptsExhausted, Ledger, LlmClient, LlmResponse
from stack.schemas.payloads.registry import payload as registry_entry
from tests.fakes.llm import FakeLlm, server_error

SCHEMA: dict[str, Any] = {"type": "object", "additionalProperties": False, "required": ["verdict"],
                          "properties": {"verdict": {"type": "string"}}}
TEMPLATE = "# Role\nYou grade documents.\n" + "Fixed instructions. " * 50


def openai_ok(parsed: Any, *, prompt: int = 50_000, cached: int = 39_000, write: int = 10_990, out: int = 5_000,
              reasoning: int = 4_000) -> LlmResponse:
    """A 200 as the OpenAI API returns it: no ``usage.cost``, no ``provider``, cache and reasoning details."""
    import json

    return LlmResponse(200, {}, {
        "id": "chatcmpl-x", "object": "chat.completion", "model": "gpt-6-luna",
        "choices": [{"message": {"content": json.dumps(parsed)}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": prompt, "completion_tokens": out,
                  "prompt_tokens_details": {"cached_tokens": cached, "cache_write_tokens": write},
                  "completion_tokens_details": {"reasoning_tokens": reasoning}}})


def client(config: Any, tr: Any, **kw: Any) -> LlmClient:
    return LlmClient(config=config, transport=tr, ledger=Ledger(), run_id="direct", stage="annotate.model_roles",
                     cache=None, sleep=lambda _s: None, **kw)


def call(c: LlmClient, role: str, **kw: Any) -> Any:
    return c.complete(role=role, template=TEMPLATE, refs={}, user=kw.pop("user", "DOC TEXT"), schema=SCHEMA,
                      license_class="open", max_tokens=16_000, prompt_name="set-v1", **kw)


def test_direct_openai_request_shape_and_cost(config: Any) -> None:
    """Red under: the OpenRouter shape sent to the OpenAI API (``provider`` block, ``max_tokens``, prefixed
    model), a key per call type, or the cost at the plain input rate."""
    tr = FakeLlm(default=openai_ok({"verdict": "x"}))
    res = call(client(config, tr), "extract_staged_openai")
    (req,) = tr.requests
    p = req.payload
    assert req.url == "https://api.openai.com/v1/chat/completions"
    assert req.api_key_env == "OPENAI_API_KEY" and "Authorization" not in req.headers
    assert req.timeout_s == 900  # the OpenAI API sends no keepalives: the transport's 120 s cut long calls off
    assert p["model"] == "gpt-6-luna" and req.model == res.model == "openai/gpt-6-luna"
    assert p["max_completion_tokens"] == 16_000 and "max_tokens" not in p
    assert "provider" not in p and "usage" not in p and "reasoning" not in p
    assert p["prompt_cache_key"] == "stack:extract_staged_openai:set-v1"
    assert p["messages"][0] == {"role": "system", "content": p["messages"][0]["content"]}
    assert isinstance(p["messages"][0]["content"], str) and "cache_control" not in str(p)
    assert p["response_format"]["json_schema"]["strict"] is True
    # cost: 10 fresh x $0.10 + 39,000 cached x $0.01 + 10,990 written x $0.125 + 5,000 out x $0.50 per 1M
    want = (10 * 0.1 + 39_000 * 0.01 + 10_990 * 0.125 + 5_000 * 0.5) / 1e6
    assert res.cost_usd == pytest.approx(want)
    assert (res.tokens_cached, res.tokens_cache_write, res.tokens_reasoning) == (39_000, 10_990, 4_000)


def test_gleaning_turn_reuses_the_first_prompt_and_its_cache_key(config: Any) -> None:
    """D615 + D714: the continuation's messages start with the first call's whole prompt (system + document)
    and carry the same ``prompt_cache_key``, so OpenAI serves the document from its cache."""
    tr = FakeLlm(default=openai_ok({"verdict": "x"}))
    c = client(config, tr)
    call(c, "extract_staged_openai")
    call(c, "extract_staged_openai", user="GLEAN TURN",
         prior=[{"role": "user", "content": "DOC TEXT"}, {"role": "assistant", "content": '{"verdict": "x"}'}])
    first, second = (r.payload for r in tr.requests)
    assert second["messages"][: len(first["messages"])] == first["messages"]
    assert second["prompt_cache_key"] == first["prompt_cache_key"]


def test_model_roles_caches_only_the_prefix_on_the_openai_api(config: Any) -> None:
    """``cache_prefix_only``: explicit caching with one breakpoint after the system part (the document is not
    written at the cache-write rate); the OpenRouter dialect ignores it. Red under: implicit caching for
    model_roles, or OpenAI-only fields sent to OpenRouter."""
    tr = FakeLlm(default=openai_ok({"verdict": "x"}))
    call(client(config, tr), "extract_main_openai", cache_prefix_only=True)
    p = tr.requests[0].payload
    assert p["prompt_cache_options"] == {"mode": "explicit"}
    (part,) = p["messages"][0]["content"]
    assert part["type"] == "text" and part["prompt_cache_breakpoint"] == {"mode": "explicit"}
    assert part["text"].startswith("# Role\nYou grade documents.")
    from tests.fakes.llm import ok

    tr2 = FakeLlm(default=ok({"verdict": "x"}))
    call(client(config, tr2), "extract_staged", cache_prefix_only=True)
    call(client(config, tr2), "extract_staged")
    assert tr2.requests[0].payload == tr2.requests[1].payload
    assert "prompt_cache_options" not in tr2.requests[0].payload


@pytest.mark.parametrize(("role", "fallback"), [("extract_main_openai", None), ("extract_staged_openai", None),
                                                 ("extract_main", "openai/gpt-5.2")])
def test_no_fallback_model_for_the_annotation_roles(config: Any, role: str, fallback: str | None) -> None:
    """D714: a luna failure under the corpus annotations' roles fails the call after the client's attempts, every
    attempt to luna; extract_main (affiliation's, unchanged) still falls back to gpt-5.2. Red under: a fallback
    model in the new roles."""
    tr = FakeLlm(default=server_error())
    with pytest.raises(AttemptsExhausted) as exc:
        call(client(config, tr), role)
    assert exc.value.error_class == "http_5xx"
    models = {r.model for r in tr.requests}
    assert models == ({"openai/gpt-6-luna"} if fallback is None else {"openai/gpt-6-luna", fallback})


def test_plan_pins_name_the_direct_no_fallback_roles(config: Any) -> None:
    """The production plan's technique_staged / model_roles pins name roles with provider openai and no
    fallbacks; affiliation keeps extract_main (its digests and priors stay)."""
    from stack.plan import load_plan

    plan = load_plan("configs/plans/batch-2026-10a.yaml")
    roles = config.llm.roles
    for stage in ("annotate.technique_staged", "annotate.model_roles"):
        r = roles[plan.prompts[stage]["role"]]
        assert r.provider == "openai" and r.fallbacks == [] and r.model == "openai/gpt-6-luna", stage
    assert plan.prompts["annotate.affiliation"]["role"] == "extract_main"
    assert config.llm.providers["openai"].dialect == "openai"


def test_prices_with_cache_and_long_context() -> None:
    p = ModelPrice(in_usd_per_m=0.1, out_usd_per_m=0.5, cached_in_usd_per_m=0.01, cache_write_usd_per_m=0.125,
                   long_above_tokens=272_000, long=ModelPrice(in_usd_per_m=0.2, out_usd_per_m=0.75,
                                                              cached_in_usd_per_m=0.02, cache_write_usd_per_m=0.25))
    assert p.cost(200_000, 0) == pytest.approx(0.02)
    assert p.cost(1_000, 1_000) == ModelPrice(in_usd_per_m=0.1, out_usd_per_m=0.5).cost(1_000, 1_000)
    assert p.cost(100_000, 0, cached=50_000, cache_write=50_000) == pytest.approx((50_000 * 0.01 + 50_000 * 0.125) / 1e6)
    assert p.cost(300_000, 1_000, cached=200_000) == pytest.approx((100_000 * 0.2 + 200_000 * 0.02 + 1_000 * 0.75) / 1e6)


def _row(workflow: str, llm: Any) -> dict[str, Any]:
    import datetime as dt

    spec = registry_entry(workflow)
    now = dt.datetime(2026, 10, 24, tzinfo=dt.UTC)
    return evidence_row(spec, doc_id="d1", source_id="s1", basis_digest="b", config_digest="c", run_id="r",
                        now=now, status="failed_permanent", payload=None, llm=llm, origin="ai_check", attempts=1,
                        first_attempt_at=now, supersedes=None, error_class="x", error="x")


def test_usage_columns_only_on_the_annotation_evidence(config: Any) -> None:
    """The usage details reach technique_staged / model_roles evidence rows (LlmResult or a merged mapping) and no
    other family's; they are outside the payload, so ``result_digest`` does not see them."""
    tr = FakeLlm(default=openai_ok({"verdict": "x"}))
    res = call(client(config, tr), "extract_main_openai")
    row = _row("model_roles", res)
    assert (row["tokens_cached"], row["tokens_cache_write"], row["tokens_reasoning"]) == (39_000, 10_990, 4_000)
    merged = {**res.evidence_fields(), "tokens_cached": 7, "tokens_cache_write": None, "tokens_reasoning": 3}
    row2 = _row("technique_staged", merged)
    assert (row2["tokens_cached"], row2["tokens_cache_write"], row2["tokens_reasoning"]) == (7, None, 3)
    assert "tokens_cached" not in _row("screen", res) and "tokens_cached" not in _row("affiliation", res)
    assert "tokens_cached" not in {f.name for f in registry_entry("model_roles").payload_schema}


def test_technique_staged_usage_counts_per_call_type(config: Any) -> None:
    """Run counts per call type (``single`` / ``glean``), cache hits skipped."""
    from dataclasses import replace

    from stack.stage_runner import load_path_module

    ts = load_path_module("_ts_main_d714", __import__("pathlib").Path("workflows/annotate/technique_staged/main.py"))
    tr = FakeLlm(default=openai_ok({"verdict": "x"}))
    res = call(client(config, tr), "extract_staged_openai")
    hit = replace(res, is_cache_hit=True)
    counts = ts.usage_counts([("single", res), ("glean", res), ("glean", hit)])
    assert counts["calls.single"] == counts["calls.glean"] == 1
    assert counts["tokens.cached.glean"] == 39_000 and counts["tokens.prompt.single"] == 50_000
    assert counts["tokens.reasoning.single"] == 4_000 and counts["tokens.cache_write.glean"] == 10_990
    sums = ts.usage_sums([res, res])
    assert sums == {"tokens_cached": 78_000, "tokens_cache_write": 21_980, "tokens_reasoning": 8_000}


def test_a_non_strict_schema_goes_folded_from_the_first_attempt_on_the_openai_api(config: Any) -> None:
    """OpenAI refuses a strict response_format whose objects are not closed (HTTP 400: "'additionalProperties' is
    required to be supplied and to be false", technique_staged's single / glean schemas). On the OpenAI dialect such
    a schema is sent folded into the prompt from the first attempt (the degrade's request), so no call pays a doomed
    400; a strict-shaped schema keeps response_format; OpenRouter keeps its strict attempt. Red under: the strict
    attempt sent first on the OpenAI dialect."""
    from stack.llm import is_strict_schema, strict_schema
    from tests.fakes.llm import ok

    open_schema = {"type": "object", "required": ["verdict"], "properties": {"verdict": {"type": "string"}}}
    assert not is_strict_schema(open_schema) and is_strict_schema(SCHEMA) and is_strict_schema(strict_schema(open_schema))
    tr = FakeLlm(default=openai_ok({"verdict": "x"}))
    res = client(config, tr).complete(role="extract_staged_openai", template=TEMPLATE, refs={}, user="DOC",
                                      schema=open_schema, license_class="open", max_tokens=16_000, prompt_name="s")
    (req,) = tr.requests
    assert "response_format" not in req.payload and res.degraded
    assert req.payload["messages"][-1]["content"].startswith("Return ONLY a JSON object that conforms")
    assert [m["content"] for m in req.payload["messages"][1:2]] == ["DOC"]
    call(client(config, tr), "extract_staged_openai")
    assert "response_format" in tr.requests[-1].payload  # SCHEMA is strict-shaped
    tr2 = FakeLlm(default=ok({"verdict": "x"}))
    client(config, tr2).complete(role="extract_staged", template=TEMPLATE, refs={}, user="DOC", schema=open_schema,
                                 license_class="open", max_tokens=16_000, prompt_name="s")
    assert "response_format" in tr2.requests[0].payload


def test_transport_uses_the_request_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A request's ``timeout_s`` replaces the transport's 120 s socket timeout (none: the transport's). Red under: a
    direct OpenAI call cut off at 120 s, retried, and billed again (seen on gold-v3: 4 x 120 s, failed_transient)."""
    from stack.llm import LlmRequest, UrllibTransport

    seen: list[float] = []
    t = UrllibTransport(api_key_env="X")
    monkeypatch.setattr(t, "_send", lambda m, u, h, d, timeout, **_kw: seen.append(timeout))
    base = {"url": "https://x/chat/completions", "headers": {}, "payload": {}, "model": "m", "attempt": 1,
            "rendered_hash": "", "input_digest": ""}
    t.post(LlmRequest(**base, timeout_s=900))
    t.post(LlmRequest(**base))
    assert seen == [900, 120.0]
