"""A reply cut off at the output cap (``finish_reason = "length"``) in the LLM client.

Real run R1 (T-ANN, 2026-10-13): a model_roles reply that hit its 8,192-token cap failed three
paid schema attempts and ended ``failed_permanent[schema_invalid]``. Now the client reads
OpenRouter's normalized ``choices[0].finish_reason``: a first truncation is settled (paid,
``truncated``) and the same messages are re-sent ONCE at twice the cap, outside the schema
attempts; a second truncation raises :class:`TruncatedOutput` (``failed_permanent[truncated]``).
Every digest and the cache key stay at the call's own cap. No network: the fake model.
"""

from __future__ import annotations

import pathlib
from typing import Any
from uuid import uuid4

import pytest

from stack.llm import Ledger, LlmClient, ResponseCache, TruncatedOutput
from stack.schemas.vocab import ERROR_CLASSES
from tests.fakes.llm import FakeLlm, ok, truncated

SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False, "required": ["verdict", "rationale"],
    "properties": {"verdict": {"enum": ["relevant", "borderline", "irrelevant"]},
                   "rationale": {"type": "string", "maxLength": 400}},
}
VALID = {"verdict": "relevant", "rationale": "in scope"}
TEMPLATE = "# Role\nYou screen documents.\n\n{{input:document}}\nJSON only.\n"
CAP = 512  # configs/llm.yaml roles.screen.max_tokens


def client(config: Any, tmp_path: pathlib.Path, fake: FakeLlm, *, cache: bool = False) -> LlmClient:
    return LlmClient(config=config, transport=fake, ledger=Ledger(),
                     run_id="r-trunc", stage="annotate.screen",
                     cache=ResponseCache(tmp_path / "cache") if cache else None)


def ask(c: LlmClient, doc: str) -> Any:
    return c.complete(role="screen", template=TEMPLATE, refs={}, user=doc, schema=SCHEMA, license_class="open")


@pytest.fixture()
def doc() -> str:
    return "Title: A Paper\n\nAbstract: " + uuid4().hex


def test_the_role_cap_is_the_one_this_file_assumes(config: Any) -> None:
    assert config.llm.roles["screen"].max_tokens == CAP


def test_a_truncated_reply_is_retried_once_at_twice_the_cap(config: Any, tmp_path: pathlib.Path,
                                                            doc: str) -> None:
    fake = FakeLlm().on(doc, [truncated(cost=0.002), ok(VALID, cost=0.003)])
    c = client(config, tmp_path / "a", fake, cache=True)
    res = ask(c, doc)
    assert res.parsed == VALID
    assert [r.payload["max_tokens"] for r in fake.requests] == [CAP, 2 * CAP]
    assert [r.payload["messages"][1:] for r in fake.requests] == [[{"role": "user", "content": doc}]] * 2
    assert [(a.kind, a.status, a.error_class) for a in res.attempts] == [
        ("first", 200, "truncated"), ("retry", 200, None)]
    assert res.cost_usd == pytest.approx(0.005)
    # the second reservation was made at the doubled cap
    assert [x["tokens_out"] for x in c.ledger.lines() if x["kind"] == "reserve"] == [CAP, 2 * CAP]
    # the digests are those of an untruncated call at the call's own cap
    plain = FakeLlm().on(doc, ok(VALID, cost=0.003))
    ref = ask(client(config, tmp_path / "b", plain), doc)
    assert plain.requests[0].payload["max_tokens"] == CAP
    digests = ("template_digest", "model_digest", "rendered_digest", "basis_digest")
    assert {k: getattr(res, k) for k in digests} == {k: getattr(ref, k) for k in digests}
    # ... and so is the cache key: the same call is a hit, nothing sent
    again = ask(c, doc)
    assert again.is_cache_hit and len(fake.requests) == 2 and again.template_digest == ref.template_digest


def test_a_truncation_is_not_a_schema_attempt(config: Any, tmp_path: pathlib.Path, doc: str) -> None:
    """A truncation, then three schema-invalid answers: the schema attempts are all three still
    (four paid requests), and the retries keep the doubled cap."""
    fake = FakeLlm().on(doc, [truncated(), ok({"nope": 1})])
    with pytest.raises(Exception, match="schema-invalid output on all 3 attempts"):
        ask(client(config, tmp_path, fake), doc)
    assert [r.payload["max_tokens"] for r in fake.requests] == [CAP] + [2 * CAP] * 3


def test_a_reply_that_parses_but_hit_the_cap_is_still_a_truncation(config: Any, tmp_path: pathlib.Path,
                                                                   doc: str) -> None:
    fake = FakeLlm().on(doc, [ok(VALID, finish_reason="length"), ok(VALID, finish_reason="stop")])
    res = ask(client(config, tmp_path, fake), doc)
    assert [a.error_class for a in res.attempts] == ["truncated", None] and len(fake.requests) == 2


def test_truncated_twice_raises_truncated_output_with_the_spend(config: Any, tmp_path: pathlib.Path,
                                                                doc: str) -> None:
    fake = FakeLlm().on(doc, truncated(cost=0.002, completion_tokens=1024))
    with pytest.raises(TruncatedOutput) as info:
        ask(client(config, tmp_path, fake), doc)
    exc = info.value
    assert (exc.status, exc.error_class) == ("failed_permanent", "truncated")
    assert "truncated" in ERROR_CLASSES["failed_permanent"]
    assert exc.cost_usd == pytest.approx(0.004)  # type: ignore[attr-defined]
    assert [a.error_class for a in exc.attempts] == ["truncated", "truncated"]  # type: ignore[attr-defined]
    assert [r.payload["max_tokens"] for r in fake.requests] == [CAP, 2 * CAP]
    assert all(s in str(exc) for s in ("openai/gpt-6-luna", f"max_tokens {CAP}", f"then {2 * CAP}", "1024 tokens"))
