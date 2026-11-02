"""Strict structured outputs (D185 (C)) and the served-provider name → egress slug map.

Real runs found every OpenAI call with the screen schema answered HTTP 400 on the strict
``response_format`` and re-sent degraded (run ``rjcc50mywvd3z``: 30 ``first`` attempts settled $0,
31 paid ``retry``s); triage's closed, all-required schema was accepted. Here a fake provider
plays OpenAI's strict-mode check (:class:`StrictProvider`: a closed object listing every property
in ``required``, a conservative keyword subset, a nullable enum listing ``null``): it answers 400
to a schema that breaks it and 200 to one that keeps it. The 400 body is modelled on OpenAI's
documented ``invalid_json_schema`` error as OpenRouter wraps it (``error.metadata.raw``); no real
400 body was recorded (the ledger keeps only the status). The provider-name pairs are the
``provider_name`` / ``tag`` of keyless OpenRouter ``/endpoints`` reads of 2026-10-12 (track E,
gemini-3.5-flash-lite and deepseek-v4.1-flash). No network.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from stack.annotate import AskContext, AskItem
from stack.llm import (
    Ledger,
    LlmClient,
    LlmRequest,
    LlmResponse,
    _provider_anomaly,
    _served_provider_slug,
    strict_schema,
)
from stack.schemas.payloads.screen import SCREEN
from tests.fakes.llm import ok, prompt_text
from workflows.annotate.affiliation import extract as ex
from workflows.annotate.screen import main as screen

TEMPLATE = "# Role\nYou screen documents.\n\n# Menus\n{{ref:enums}}\n\n{{input:document}}\nJSON only.\n"
REFS = {"enums": "verdict ∈ {relevant, borderline, irrelevant}"}
#: What OpenAI's strict mode is taken to accept here (the shapes triage sends and is accepted with).
ACCEPTED = {"type", "properties", "required", "additionalProperties", "enum", "items", "anyOf",
            "description", "minimum", "maximum"}
NULLS = {"verdict": "irrelevant", "rationale": "a recipe blog", "buckets": None,
         "off_topic_reason": "non-AI: cooking", "grounding_idx": None, "quotes": None}


def strict_violations(schema: Mapping[str, Any], where: str = "$") -> list[str]:
    """Why OpenAI's strict mode would refuse ``schema`` (empty = accepted)."""
    out = [f"{where}: keyword {k!r}" for k in schema if k not in ACCEPTED]
    types = schema.get("type")
    types = [types] if isinstance(types, str) else list(types or [])
    if "properties" in schema or "object" in types:
        props = schema.get("properties") or {}
        if schema.get("additionalProperties") is not False:
            out.append(f"{where}: additionalProperties must be false")
        if set(schema.get("required") or []) != set(props):
            out.append(f"{where}: 'required' must include every key in properties")
        for k, v in props.items():
            out += strict_violations(v, f"{where}.{k}")
    if "null" in types and "enum" in schema and None not in schema["enum"]:
        out.append(f"{where}: nullable enum without null")
    if isinstance(schema.get("items"), Mapping):
        out += strict_violations(schema["items"], f"{where}[]")
    for i, s in enumerate(schema.get("anyOf") or []):
        out += strict_violations(s, f"{where}|{i}")
    return out


def invalid_schema(message: str) -> LlmResponse:
    """OpenRouter's 400 around OpenAI's ``invalid_json_schema`` (shape modelled, not recorded)."""
    raw = {"error": {"message": f"Invalid schema for response_format 'stack_output': {message}",
                     "type": "invalid_request_error", "param": "response_format",
                     "code": "invalid_json_schema"}}
    return LlmResponse(400, {}, {"error": {"code": 400, "message": "Provider returned error",
                                           "metadata": {"raw": json.dumps(raw), "provider_name": "OpenAI"}}})


class StrictProvider:
    """A transport playing OpenAI behind OpenRouter: a ``response_format`` schema that breaks
    strict mode (or any, with ``refuse_all``: a provider that still refuses) gets
    :func:`invalid_schema`; otherwise ``reply``."""

    def __init__(self, reply: LlmResponse, *, refuse_all: bool = False) -> None:
        self.reply, self.refuse_all = reply, refuse_all
        self.requests: list[LlmRequest] = []

    def post(self, request: LlmRequest) -> LlmResponse:
        self.requests.append(request)
        fmt = request.payload.get("response_format")
        if fmt is not None:
            problems = (["structured outputs are not supported"] if self.refuse_all
                        else strict_violations(fmt["json_schema"]["schema"]))
            if problems or fmt["json_schema"].get("strict") is not True:
                return invalid_schema("; ".join(problems))
        return self.reply


def client(config: Any, tmp_path: Path, transport: Any) -> LlmClient:
    return LlmClient(config=config, transport=transport, ledger=Ledger(),
                     run_id="r-strict", stage="annotate.screen", cache=None)


def reserves(c: LlmClient) -> int:
    return sum(1 for x in c.ledger.lines() if x["kind"] == "reserve")


def keywords(schema: Any) -> set[str]:
    """Every key anywhere in ``schema`` (property names included)."""
    if isinstance(schema, Mapping):
        return set(schema) | {k for v in schema.values() for k in keywords(v)}
    if isinstance(schema, list):
        return {k for v in schema for k in keywords(v)}
    return set()


def pre_strict_screen_schema() -> dict[str, Any]:
    """The LLM-output schema screen sent before D185 (optional keys, open object): still
    :func:`screen._output_schema`, which v3's ``tools/eval_screen.py`` reads."""
    return screen._output_schema(screen.SCHEMA_REL)


# ═══════════════════════════════ strict schema on the wire ═══════════════════════════════════


def test_screen_schema_is_accepted_on_the_first_attempt(config: Any, tmp_path: Path) -> None:
    """The screen schema passes strict mode: one request, one reservation, not degraded; the
    nullable keys come back ``null``; ``maxLength`` is not sent but still holds client-side."""
    provider = StrictProvider(ok(NULLS))
    c = client(config, tmp_path, provider)
    result = c.complete(role="screen", template=TEMPLATE, refs=REFS, user="Title: Pancakes",
                        schema=screen.SCHEMA, license_class="open")
    assert result.ok and not result.degraded
    assert len(provider.requests) == 1 and reserves(c) == 1
    assert [a.kind for a in result.attempts] == ["first"]
    assert result.parsed == NULLS
    wire = provider.requests[0].payload["response_format"]["json_schema"]
    assert wire["strict"] is True and strict_violations(wire["schema"]) == []
    assert "maxLength" not in keywords(wire["schema"]) and "maxLength" in keywords(screen.SCHEMA)
    assert strict_schema(screen.SCHEMA) == screen.SCHEMA  # idempotent: the helper is the rule


def test_a_long_rationale_is_still_refused_client_side(config: Any, tmp_path: Path) -> None:
    """``maxLength`` left the wire but not the validator: a 401-character rationale is
    schema-invalid (retried, then the call fails), as before."""
    provider = StrictProvider(ok({**NULLS, "rationale": "x" * 401}))
    c = client(config, tmp_path, provider)
    with pytest.raises(Exception, match="schema-invalid"):
        c.complete(role="screen", template=TEMPLATE, refs=REFS, user="Title: Long",
                   schema=screen.SCHEMA, license_class="open")
    assert len(provider.requests) == 3


def test_pre_strict_schema_is_refused_and_degrades(config: Any, tmp_path: Path) -> None:
    """The fake tells the shapes apart: the pre-D185 screen schema gets the 400 and the client
    degrades (two requests, the second without ``response_format``) — the real runs' pattern."""
    old = pre_strict_screen_schema()
    assert strict_violations(old)
    provider = StrictProvider(ok({"verdict": "relevant", "rationale": "in scope"}))
    c = client(config, tmp_path, provider)
    result = c.complete(role="screen", template=TEMPLATE, refs=REFS, user="Title: Old",
                        schema=old, license_class="open")
    assert result.ok and result.degraded
    assert [r.payload.get("response_format") is None for r in provider.requests] == [False, True]
    assert [a.status for a in result.attempts] == [400, 200] and reserves(c) == 2


def test_a_provider_still_refusing_degrades_and_omitted_keys_read_null(config: Any, tmp_path: Path) -> None:
    """The degrade stays the fallback: a provider that refuses even the strict schema gets the
    schema folded into the prompt; its answer omits the optional keys (nothing enforces them)
    and they read ``null`` instead of a schema-invalid retry."""
    provider = StrictProvider(ok({"verdict": "relevant", "rationale": "in scope"}), refuse_all=True)
    c = client(config, tmp_path, provider)
    result = c.complete(role="screen", template=TEMPLATE, refs=REFS, user="Title: Refused",
                        schema=screen.SCHEMA, license_class="open")
    assert result.ok and result.degraded and len(provider.requests) == 2
    assert "conforms to this JSON Schema" in prompt_text(provider.requests[1])
    assert result.parsed == {"verdict": "relevant", "rationale": "in scope", "buckets": None,
                             "off_topic_reason": None, "grounding_idx": None, "quotes": None}


def test_nested_objects_and_optional_enums_become_strict(config: Any, tmp_path: Path) -> None:
    """The rule beyond screen's shapes: an object inside an array (affiliation's authors) is
    closed and all-required, its optional keys nullable; an optional enum lists ``null``;
    omitted nested keys read ``null`` on the degrade path too."""
    toy = {"type": "object", "required": ["authors"], "properties": {
        "kind": {"enum": ["paper", "blog"]},
        "authors": {"type": "array", "items": {
            "type": "object", "required": ["name"],
            "properties": {"name": {"type": "string"}, "email": {"type": "string"}}}}}}
    strict = strict_schema(toy)
    assert strict_violations(toy) and strict_violations(strict) == []
    assert strict["properties"]["kind"] == {"enum": ["paper", "blog", None], "type": ["string", "null"]}
    reply = ok({"kind": None, "authors": [{"name": "A", "email": None}]})
    c = client(config, tmp_path, StrictProvider(reply))
    result = c.complete(role="screen", template=TEMPLATE, refs=REFS, user="Title: Toy", schema=strict,
                        license_class="open")
    assert not result.degraded and result.parsed["authors"] == [{"name": "A", "email": None}]
    refusing = StrictProvider(ok({"authors": [{"name": "B"}]}), refuse_all=True)
    result = client(config, tmp_path / "r", refusing).complete(
        role="screen", template=TEMPLATE, refs=REFS, user="Title: Toy 2", schema=strict, license_class="open")
    assert result.degraded and result.parsed == {"kind": None, "authors": [{"name": "B", "email": None}]}


# ═══════════════════════════════ nulls into the SCREEN payload ═══════════════════════════════


def ask_one(config: Any, tmp_path: Path, reply: dict[str, Any], text: str) -> dict[str, Any]:
    """``screen.ask`` on one document (tier 1 only) through the strict provider; its payload."""
    pin = {"name": "screen-v7", "tier2": False}
    actx = AskContext(run_id="r-strict", stage="annotate.screen", store_root=tmp_path,
                      pin=screen.prompt(pin, config.llm), plan_pin=pin, options={}, budget_usd=None,
                      _config=lambda: config, _client=client(config, tmp_path, StrictProvider(ok(reply))))
    item = AskItem(doc_id="d1", source_id="s1", basis_kind="text", basis_digest="b" * 64, fields=None,
                   row={"metadata": {"title": "A doc"}, "content": {"current_role": "abstract"}},
                   license_class="open", attempt=1, load_text=lambda: text)
    answer = screen.ask(item, actx)
    assert answer.llm is not None and not answer.llm.degraded
    payload = dict(answer.payload)
    pa.array([payload], type=SCREEN)  # the SCREEN struct takes it
    return payload


def test_null_optional_fields_flow_into_the_screen_payload(config: Any, tmp_path: Path) -> None:
    p = ask_one(config, tmp_path, NULLS, "A pancake recipe.")
    assert (p["verdict"], p["buckets"], p["quotes"], p["off_topic_reason"]) == (
        "irrelevant", [], [], "non-AI: cooking")


def test_present_optional_fields_still_flow(config: Any, tmp_path: Path) -> None:
    text = "We study reward hacking in frontier models."
    p = ask_one(config, tmp_path, {"verdict": "relevant", "rationale": "reward hacking", "buckets": ["paper"],
                                   "off_topic_reason": None, "grounding_idx": [0],
                                   "quotes": ["reward hacking in frontier models"]}, text)
    assert p["buckets"] == ["paper"] and p["off_topic_reason"] is None
    assert p["quotes"] == [{"text": "reward hacking in frontier models", "offset": 9}]


def test_affiliation_schema_is_accepted_and_null_keys_parse_as_absent(config: Any, tmp_path: Path) -> None:
    """Affiliation's ``RESPONSE_SCHEMA`` passes strict mode on the first attempt; ``null``
    ``email`` / ``marker`` reach :func:`parse_authors` as absent (no ``"None"`` strings)."""
    reply = {"authors": [{"author_surface": "Jane Doe", "email": None,
                          "affiliations": [{"surface": "Anthropic", "marker": None}]}]}
    provider = StrictProvider(ok(reply))
    c = client(config, tmp_path, provider)
    result = c.complete(role="screen", template=TEMPLATE, refs=REFS, user="Title: Byline",
                        schema=ex.RESPONSE_SCHEMA, license_class="open")
    assert not result.degraded and len(provider.requests) == 1 and reserves(c) == 1
    (jane,) = ex.parse_authors(result.parsed)
    assert (jane.author_surface, jane.email) == ("Jane Doe", "")
    assert [(a.surface, a.marker) for a in jane.affiliations] == [("Anthropic", None)]


# ═══════════════════════════════ served provider → egress slug ═══════════════════════════════

#: (provider_name, tag) as recorded from OpenRouter ``/endpoints`` (2026-10-12), one per name.
RECORDED = [
    ("Google", "google-vertex/global"), ("Google AI Studio", "google-ai-studio"),
    ("OpenInference", "open-inference/fp4"), ("Morph", "morph/fp8"), ("Relace", "relace"),
    ("InferenceNet", "inference-net/fp8"), ("Sail Research", "sail-research/fp4"), ("Wafer", "wafer"),
    ("Decart", "decart/fp4"), ("Ionstream", "ionstream"), ("DeepInfra", "deepinfra/fp8"),
    ("StreamLake", "streamlake/fp8"), ("Baidu", "baidu/fp8"), ("DeepSeek", "deepseek"),
    ("DigitalOcean", "digitalocean"), ("GMICloud", "gmicloud/fp8"), ("Novita", "novita/fp8"),
    ("CoreWeave", "coreweave/fp8"), ("Phala", "phala"), ("Makora", "makora/fp8"), ("Crusoe", "crusoe/fp8"),
    ("AtlasCloud", "atlas-cloud/fp8"), ("BaseTen", "baseten/fp8"), ("Alibaba", "alibaba"),
    ("Together", "together"), ("SiliconFlow", "siliconflow/fp8"), ("Modal", "modal"),
    ("Parasail", "parasail/fp8"), ("Fireworks", "fireworks"), ("Venice", "venice/fp8"),
]


@pytest.mark.parametrize("name,tag", RECORDED)
def test_recorded_provider_names_map_to_their_slugs(name: str, tag: str) -> None:
    assert _served_provider_slug({"provider": name}) == tag.split("/")[0]


def test_gemini_on_vertex_is_not_an_anomaly_ai_studio_is(config: Any, tmp_path: Path) -> None:
    """Under the ``open`` class (google-vertex sanctioned), ``Google`` (Vertex) is fine, ``Google
    AI Studio`` is the anomaly, and an unknown name outside the set stays one (today's rule)."""
    assert _provider_anomaly({"provider": "Google"}, ["google-vertex"]) is None
    assert _provider_anomaly({"provider": "Google AI Studio"}, ["google-vertex"]) == "Google AI Studio"
    assert _provider_anomaly({"provider": "Some New Host"}, ["google-vertex"]) == "Some New Host"
    flags = []
    for served in ("Google", "Google AI Studio"):
        reply = ok({"verdict": "relevant", "rationale": "x"}, provider=served)
        c = client(config, tmp_path / served, StrictProvider(reply))
        result = c.complete(role="screen", model="google/gemini-3.5-flash-lite", template=TEMPLATE,
                            refs=REFS, user=f"Title: {served}", schema=screen.SCHEMA, license_class="open")
        flags.append(result.provider_anomaly)
    assert flags == [False, True]
