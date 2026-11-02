"""The screen's output schema selected by the plan pin's name (track E2, D???(E)):
``workflows/annotate/screen/main.py`` ``schema_for`` / ``SCHEMA_SR`` (``SPECS``).

``screen-v9`` (and ``screen-v10``) answers carry the Shallow Review scope fields; every other name
keeps :data:`SCHEMA`, so the template digests of ``screen-v7`` / ``screen-v8`` (and every existing
work key) are byte-for-byte unchanged: pinned below as literals recorded before the change (fixed
call parameters, so ``llm.yaml`` edits do not move them) and recomputed from the pre-change
construction for the plans' pins. ``ask`` is called directly (no stage runner) with a real
:class:`stack.llm.LlmClient` on the fake model; a ``screen-v9`` template is installed through
``main._TEMPLATES`` until the conductor writes ``screen-v9.md``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from stack.annotate.stage import AskContext, AskItem
from stack.config import load
from stack.core.hashes import template_hash
from stack.llm import LlmClient, render_call, strict_schema, wire_schema
from stack.llm.ledger import Ledger
from stack.plan import load_plan
from stack.stage_runner import StageError
from tests.fakes.llm import FakeLlm, ok, schema_invalid
from tests.test_llm_strict import strict_violations
from workflows.annotate.screen import main as screen

REPO = Path(__file__).resolve().parents[1]
CONFIG = load()
LUNA = "openai/gpt-6-luna"
TERRA = CONFIG.llm.roles["screen_adjudicate"].model
SR_KEYS = ("scope", "sr_category", "sr_agenda")
#: template_hash(template(name), schema_for(name), FIXED) recorded on the tree before SCHEMA_SR existed (HEAD
#: fee4619; v7, v8) and before the per-question schemas existed (HEAD 50bab6f; v9: screen-v9.md with SCHEMA_SR).
FIXED = {"model": "x/y", "max_tokens": 1000, "temperature": 0}
DIGEST_BEFORE = {"screen-v7": "aed978331b6f3d356fe161d9f0232363f797db4c2f9d2d03b47609ba45853c4e",
                 "screen-v8": "1f0b3bc93d39c23636a01e091fea01d11e99b18cb0534534e7997c291650bef5",
                 "screen-v9": "0ad2789318357137eec9ba01a444468c585ce147a8131d2521744f1d8c19712b"}


@pytest.fixture
def v9(monkeypatch: pytest.MonkeyPatch) -> str:
    """A stand-in ``screen-v9`` template (``screen-v8.md`` plus a marker), unless the file exists."""
    if not (screen.HERE / "screen-v9.md").is_file():
        text = (screen.HERE / "screen-v8.md").read_text("utf-8") + "\nSR-V9-STANDIN: also answer scope.\n"
        monkeypatch.setitem(screen._TEMPLATES, "screen-v9", text)
    return screen.template("screen-v9")


def test_sr_schema_has_the_scope_fields_in_strict_form() -> None:
    sr = screen.SCHEMA_SR
    assert screen.schema_for("screen-v9") is sr
    assert screen.schema_for("screen-v10") is screen.SCHEMA_DUAL  # since the per-question schemas (D???(E))
    for name in (None, "screen-v7", "screen-v8"):
        assert screen.schema_for(name) is screen.SCHEMA
    for bad, why in (("screen-v99", "no template"), (7, "not a template name")):  # no silent fallback to v7's
        with pytest.raises(StageError, match=why):
            screen.schema_for(bad)
    props = sr["properties"]
    assert props["scope"] == {"type": "string", "enum": ["core", "sr_only", "out"]}  # required: not nullable
    assert props["sr_category"]["type"] == ["string", "null"]
    assert props["sr_category"]["enum"] == ["labs", "black_box", "white_box", "safety_by_construction",
                                            "make_ai_solve_it", "theory", "multi_agent_first", "evals", "none", None]
    assert props["sr_agenda"] == {"type": ["string", "null"], "maxLength": 80}
    # strict invariants: closed, every key required, the optional ones nullable; accepted on the wire
    assert sr["additionalProperties"] is False and sorted(sr["required"]) == sorted(props)
    assert strict_schema(sr) == sr
    assert strict_violations(wire_schema(sr)) == []
    # the rest is SCHEMA unchanged
    assert {k: v for k, v in props.items() if k not in SR_KEYS} == screen.SCHEMA["properties"]
    assert not set(SR_KEYS) & set(screen.SCHEMA["properties"])


def test_v7_v8_v9_digests_unchanged(v9: str) -> None:
    assert (screen.HERE / "screen-v9.md").is_file()  # the v9 literal is of the real file
    for name, before in DIGEST_BEFORE.items():
        assert template_hash(screen.template(name), screen.schema_for(name), FIXED) == before, name
    # the plans' pins: prompt() == the pre-change construction (SCHEMA as it was built, not via schema_for)
    old_schema = strict_schema(screen._output_schema(screen.SCHEMA_REL))
    for plan in ("dev-v2", "dev-v2-m4"):
        pin = dict(load_plan(REPO / "configs" / "plans" / f"{plan}.yaml").prompts["annotate.screen"])
        for name in ("screen-v7", "screen-v8"):
            p = {**pin, "name": name}
            t = screen.tiers(p, CONFIG.llm)
            want = []
            for role, model in ((screen.ROLE, t.model), (screen.ADJUDICATE_ROLE, t.adjudicate_model)):
                rc = CONFIG.llm.roles[role]
                for m in [model, *rc.fallbacks]:
                    params: dict[str, Any] = {"model": m, "max_tokens": rc.max_tokens}
                    if rc.temperature is not None:
                        params["temperature"] = rc.temperature
                    want.append(template_hash(screen.template(name), old_schema, params))
            assert screen.prompt(p, CONFIG.llm).template_digests == tuple(dict.fromkeys(want)), (plan, name)
    # the schema is part of the digest: a v9 pin's digests differ from the same text under SCHEMA
    assert template_hash(v9, screen.SCHEMA_SR, FIXED) != template_hash(v9, screen.SCHEMA, FIXED)
    pin9 = {"name": "screen-v9", "model": LUNA}
    rc = CONFIG.llm.roles[screen.ROLE]
    params9: dict[str, Any] = {"model": LUNA, "max_tokens": 1024}  # tier 1's budget for v9 (D???(E))
    if rc.temperature is not None:
        params9["temperature"] = rc.temperature
    assert template_hash(v9, screen.SCHEMA_SR, params9) in screen.prompt(pin9, CONFIG.llm).template_digests


TEXT = "A thin abstract about agent foundations and embedded agency."


def _ask(tmp_path: Path, fake: FakeLlm, name: str, text: str = TEXT) -> Any:
    client = LlmClient(config=CONFIG.llm, transport=fake, ledger=Ledger(),
                       run_id="r-sr", stage="annotate.screen", cache=None)
    item = AskItem(doc_id="d1", source_id="d1", basis_kind="text", basis_digest="b" * 64, fields=None,
                   row={"doc_id": "d1", "metadata": {"title": "Paper"}, "content": {"current_role": "abstract"}},
                   license_class="open", attempt=1, load_text=lambda: text)
    plan_pin = {"name": name, "model": LUNA, "tier2": True, "adjudicate_model": TERRA}
    actx = AskContext(run_id="r-sr", stage="annotate.screen", store_root=None,
                      pin=screen.prompt(plan_pin, CONFIG.llm), plan_pin=plan_pin, options={}, budget_usd=None,
                      _config=lambda: CONFIG, _client=client)
    return screen.ask(item, actx)


def _sent_schema(fake: FakeLlm) -> list[Any]:
    return [r.payload["response_format"]["json_schema"]["schema"] for r in fake.requests]


def test_v9_ask_sends_sr_schema_on_both_tiers(tmp_path: Path, v9: str) -> None:
    fake = FakeLlm()
    fake.on(model=LUNA, reply=ok({"verdict": "borderline", "rationale": "thin", "buckets": ["paper"],
                                   "quotes": [], "scope": "sr_only", "sr_category": "theory",
                                   "sr_agenda": "agent_foundations"}))
    fake.on(model=TERRA, reply=ok({"verdict": "relevant", "rationale": "theory", "buckets": ["paper"],
                                   "quotes": [], "scope": "sr_only", "sr_category": "theory", "sr_agenda": None}))
    ans = _ask(tmp_path, fake, "screen-v9")
    assert [r.model for r in fake.requests] == [LUNA, TERRA]
    assert _sent_schema(fake) == [wire_schema(screen.SCHEMA_SR)] * 2
    refs = {"enums": screen._render_menu(CONFIG, screen.MENUS)}
    for req in fake.requests:  # both tiers send the v9 template (rendered as the client renders it)
        rc = CONFIG.llm.roles[screen.ADJUDICATE_ROLE if req.model == TERRA else screen.ROLE]
        sent = render_call(v9, refs, model=req.model, max_tokens=rc.max_tokens, schema=screen.SCHEMA_SR,
                           temperature=rc.temperature)
        assert req.payload["messages"][0]["content"][0]["text"] == sent.sent_prefix
    # the normal SCREEN payload: the scope fields are not stored (known gap, docstring)
    assert ans.payload["verdict"] == "borderline" and ans.followups[0].payload["verdict"] == "relevant"
    assert ans.followups[0].payload["adjudication_tier"] == 2
    assert not set(SR_KEYS) & set(ans.payload) and not set(SR_KEYS) & set(ans.followups[0].payload)
    assert ans.followups[0].llm.parsed["scope"] == "sr_only"  # the parsed answer keeps them (the eval reads them)


def test_v9_answer_without_scope_is_schema_invalid(tmp_path: Path, v9: str) -> None:
    fake = FakeLlm()
    fake.on(model=LUNA, reply=schema_invalid({"verdict": "relevant", "rationale": "x", "buckets": ["paper"],
                                               "quotes": []}))
    with pytest.raises(Exception, match="schema"):
        _ask(tmp_path, fake, "screen-v9")


def test_v8_ask_still_sends_schema(tmp_path: Path) -> None:
    fake = FakeLlm()
    fake.on(model=LUNA, reply=ok({"verdict": "borderline", "rationale": "thin", "buckets": ["paper"], "quotes": []}))
    fake.on(model=TERRA, reply=ok({"verdict": "irrelevant", "rationale": "no", "buckets": ["offtopic"], "quotes": []}))
    ans = _ask(tmp_path, fake, "screen-v8")
    assert _sent_schema(fake) == [wire_schema(screen.SCHEMA)] * 2
    assert ans.followups[0].payload["verdict"] == "irrelevant"


# ── the two-question screen (D???(E)): screen-v10, screen-stack-v1, screen-sr-v1 ──────────────

CATS = ["labs", "black_box", "white_box", "safety_by_construction", "make_ai_solve_it", "theory",
        "multi_agent_first", "evals", "none"]
VERDICT = {"type": "string", "enum": ["relevant", "borderline", "irrelevant"]}
CONF = {"type": "number", "minimum": 0, "maximum": 1}


def _strip(schema: Any) -> Any:
    """``schema`` without ``description`` (recursively), for shape comparisons."""
    if isinstance(schema, dict):
        return {k: _strip(v) for k, v in schema.items() if k != "description"}
    if isinstance(schema, list):
        return [_strip(v) for v in schema]
    return schema


def _strict_ok(schema: dict[str, Any]) -> None:
    assert strict_schema(schema) == schema
    assert strict_violations(wire_schema(schema)) == []


def test_single_question_schemas() -> None:
    st, sq = screen.schema_for("screen-stack-v1"), screen.schema_for("screen-sr-v1")
    assert st is screen.SCHEMA_STACK and sq is screen.SCHEMA_SR_QUESTION
    for sch in (st, sq):
        _strict_ok(sch)
        assert sch["additionalProperties"] is False and sorted(sch["required"]) == sorted(sch["properties"])
        assert sch["properties"]["confidence"] == CONF  # required: not nullable
    assert {k: v for k, v in st["properties"].items() if k != "confidence"} == screen.SCHEMA["properties"]
    assert set(sq["properties"]) - set(screen.SCHEMA["properties"]) == {"confidence", "category", "agenda"}
    assert sq["properties"]["category"] == {"type": "string", "enum": CATS}  # required, no null in the enum
    assert sq["properties"]["agenda"] == {"type": ["string", "null"], "maxLength": 80}


def test_dual_schema_nested_objects_closed_and_required() -> None:
    d = screen.schema_for("screen-v10")
    assert d is screen.SCHEMA_DUAL
    _strict_ok(d)
    props = d["properties"]
    assert sorted(props) == ["buckets", "off_topic_reason", "rationale", "sr", "stack", "verdict"]
    assert sorted(d["required"]) == sorted(props) and d["additionalProperties"] is False
    for k in ("verdict", "rationale", "buckets", "off_topic_reason"):
        assert props[k] == screen.SCHEMA["properties"][k]
    quotes = {"type": "array", "items": {"type": "string"}}
    assert _strip(props["stack"]) == {"type": "object", "additionalProperties": False,
                                      "required": ["verdict", "confidence", "quotes"],
                                      "properties": {"verdict": VERDICT, "confidence": CONF, "quotes": quotes}}
    assert _strip(props["sr"]) == {"type": "object", "additionalProperties": False,
                                   "required": ["verdict", "confidence", "quotes", "category", "agenda"],
                                   "properties": {"verdict": VERDICT, "confidence": CONF, "quotes": quotes,
                                                  "category": {"type": "string", "enum": CATS},
                                                  "agenda": {"type": ["string", "null"], "maxLength": 80}}}
    assert screen.question_mode("screen-v10") == "dual" and screen.question_mode("screen-v8") == "stack"
    assert screen.question_mode(None) == "stack" and screen.question_mode("screen-v9") == "scope"


@pytest.mark.parametrize(("name", "tier1"), [("screen-v9", 1024), ("screen-stack-v1", 1024),
                                             ("screen-sr-v1", 1024), ("screen-v10", 1536),
                                             ("screen-v8", None), ("screen-v7", None)])
def test_tier1_max_tokens_sent_and_hashed(name: str, tier1: int | None) -> None:
    rc1, rc2 = CONFIG.llm.roles[screen.ROLE], CONFIG.llm.roles[screen.ADJUDICATE_ROLE]
    assert screen.tier1_max_tokens(name) == tier1
    pin = {"name": name, "model": LUNA, "adjudicate_model": TERRA}
    text, schema = screen.template(name), screen.schema_for(name)

    def digest(model: str, rc: Any, max_tokens: int) -> str:
        params: dict[str, Any] = {"model": model, "max_tokens": max_tokens}
        if rc.temperature is not None:
            params["temperature"] = rc.temperature
        return template_hash(text, schema, params)

    want = (digest(LUNA, rc1, tier1 or rc1.max_tokens), digest(TERRA, rc2, rc2.max_tokens))  # tier 2: its role's
    assert screen.prompt(pin, CONFIG.llm).template_digests[:2] == want


def _dual(verdict: str, stack: dict[str, Any], sr: dict[str, Any]) -> Any:
    return ok({"verdict": verdict, "rationale": "S and R", "buckets": ["paper"], "off_topic_reason": None,
               "stack": stack, "sr": {"category": "theory", "agenda": None, **sr}})


def test_dual_ask_sends_budget_and_merges_quotes(tmp_path: Path) -> None:
    fake = FakeLlm()
    fake.on(model=LUNA, reply=_dual(
        "borderline",
        {"verdict": "irrelevant", "confidence": 0.7, "quotes": ["agent foundations", "not in the text"]},
        {"verdict": "borderline", "confidence": 0.6, "quotes": ["embedded agency", "agent foundations"]}))
    fake.on(model=TERRA, reply=_dual("relevant", {"verdict": "irrelevant", "confidence": 0.9, "quotes": []},
                                     {"verdict": "relevant", "confidence": 0.8, "quotes": ["A thin abstract"]}))
    ans = _ask(tmp_path, fake, "screen-v10")
    assert [r.payload["max_tokens"] for r in fake.requests] == [1536, CONFIG.llm.roles[screen.ADJUDICATE_ROLE].max_tokens]
    assert _sent_schema(fake) == [wire_schema(screen.SCHEMA_DUAL)] * 2
    p1 = ans.payload
    # stack quotes then sr quotes, the exact repeat dropped, the unanchorable one voided
    assert [q["text"] for q in p1["quotes"]] == ["agent foundations", "embedded agency"]
    assert ans.counts["quotes_voided"] == 1
    assert all(TEXT[q["offset"]:q["offset"] + len(q["text"])] == q["text"] for q in p1["quotes"])
    assert p1["confidence"] is None and p1["verdict"] == "borderline"
    p2 = ans.followups[0].payload
    assert [q["text"] for q in p2["quotes"]] == ["A thin abstract"] and p2["verdict"] == "relevant"
    assert sorted(p1) == sorted(["verdict", "basis_role", "buckets", "quotes", "off_topic_reason", "confidence",
                                 "reasoning", "adjudication_tier", "tier1_verdict", "tier1_model"])


def test_answer_quotes() -> None:
    assert screen.answer_quotes({"quotes": ["a", "a", 3]}) == ["a", "a", "3"]  # top level: as given
    assert screen.answer_quotes({"quotes": None}) == []
    assert screen.answer_quotes({"stack": {"quotes": ["a", "b"]}, "sr": {"quotes": ["b", "c", "a"]}}) == ["a", "b", "c"]
    assert screen.answer_quotes({"stack": None, "sr": {"quotes": ["c"]}}) == ["c"]


@pytest.mark.parametrize("name", ["screen-stack-v1", "screen-sr-v1"])
def test_single_question_ask_stores_confidence(tmp_path: Path, name: str) -> None:
    extra = {"category": "theory", "agenda": "agent_foundations"} if name == "screen-sr-v1" else {}
    fake = FakeLlm()
    fake.on(model=LUNA, reply=ok({"verdict": "relevant", "confidence": 0.85, "rationale": "r", "buckets": ["paper"],
                                   "quotes": ["embedded agency"], "off_topic_reason": None, "grounding_idx": None,
                                   **extra}))
    ans = _ask(tmp_path, fake, name)
    assert [r.payload["max_tokens"] for r in fake.requests] == [1024]
    assert _sent_schema(fake) == [wire_schema(screen.schema_for(name))]
    assert ans.payload["confidence"] == 0.85 and [q["text"] for q in ans.payload["quotes"]] == ["embedded agency"]


def test_sr_question_without_category_is_schema_invalid(tmp_path: Path) -> None:
    fake = FakeLlm()
    fake.on(model=LUNA, reply=schema_invalid({"verdict": "relevant", "confidence": 0.8, "rationale": "x",
                                               "buckets": ["paper"], "quotes": [], "off_topic_reason": None,
                                               "grounding_idx": None, "category": None, "agenda": None}))
    with pytest.raises(Exception, match="schema"):
        _ask(tmp_path, fake, "screen-sr-v1")


def test_v8_confidence_stays_null(tmp_path: Path) -> None:
    fake2 = FakeLlm()
    fake2.on(model=LUNA, reply=ok({"verdict": "relevant", "rationale": "r", "buckets": ["paper"], "quotes": []}))
    ans = _ask(tmp_path, fake2, "screen-v8")
    assert ans.payload["confidence"] is None
    assert [r.payload["max_tokens"] for r in fake2.requests] == [CONFIG.llm.roles[screen.ROLE].max_tokens]
