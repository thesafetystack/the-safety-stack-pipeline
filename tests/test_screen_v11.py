"""``screen-v11`` (track S, S3) and the screening-text limits (S2), on the fake model only.

(1) the v11 template and schema: v10's text plus question U, the strict ``usability`` object;
(2) the excerpt rendering (:func:`main.excerpt`): a short text whole, a long one as head, marker
and tail cut at whitespace, a cut already at a boundary, a hard cut, the length line;
(3) every older name keeps its pins: template digests at fixed params, the tier-1 and tier-2 user
messages byte for byte (sha256 recorded at 98ecf14, before the limit keys existed), legacy limits;
bad limit values refuse before any call;
(4) ``ask`` with ``screen-v11``: the excerpt reaches tier 1, quotes anchor against the whole text,
the ``text_usability`` record per tier, the limited tier-2 preamble;
(5) a ``stack build`` with a ``screen-v11`` pin: the ``text_usability`` evidence struct, record
file and typed ``annotations.text_usability`` field, verify green (slow);
(6) ``stack eval screen --prompt screen-v11``: usability and chars-sent row fields, the summary's
usability table, ``--pin`` reaching the call and run.json, the user-message check only under the
legacy tier-1 limit, tier 2 on (the estimate's probe answers every per-question object);
(7) ``stack eval compare`` on two synthetic run directories.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

import stack.annotate.stage as annotate_stage
import tests.fakes.llm as fakes
from stack.annotate import AskContext, AskItem
from stack.build import run_build
from stack.checks.verify import run_checks
from stack.config import load
from stack.core.hashes import template_hash
from stack.evals import score as scorer
from stack.llm import LlmClient, strict_schema, wire_schema
from stack.llm.ledger import Ledger
from stack.plan import Plan, load_plan
from stack.schemas.payloads.usability import USABILITY_ISSUES
from stack.stage_runner import StageError
from stack.store.parquet import read_family
from tests.fakes.annotate import AT_RUN, doc_with_text, write_documents
from tests.fakes.llm import FakeLlm, ok, prompt_text
from tests.synth.rows import sid
from tests.test_eval_harness import LONG_TEXT, SET, TAIL, TERRA, H, cli, read_jsonl, run_dirs, screen_set, write_jsonl
from tests.test_llm_strict import strict_violations
from workflows.annotate.screen import main as screen

REPO = Path(__file__).resolve().parents[1]
CONFIG = load()
LUNA = "openai/gpt-6-luna"
OLDER = ("screen-v7", "screen-v8", "screen-v9", "screen-v10", "screen-stack-v1", "screen-sr-v1")


# ── (1) template and schema ──────────────────────────────────────────────────────────────────


def test_v11_template_is_v10_plus_question_u() -> None:
    v10, v11 = screen.template("screen-v10"), screen.template("screen-v11")
    body10 = v10.split("\n", 1)[1]
    body11 = v11.split("\n", 1)[1]
    # after the comment line, v11 = v10 up to "# Out of scope for both", with question U inserted before it
    pre10 = body10[:body10.index("# Out of scope for both")]
    assert body11.startswith(pre10 + "# Question U: text usability\n")
    u = body11[body11.index("# Question U"):body11.index("# Out of scope for both")]
    for code in ("usable", "degraded", "unusable", *USABILITY_ISSUES):
        assert f"`{code}`" in u, code
    assert "[… N characters omitted by the screener …]" in u and "Document length:" in u
    assert "never changes your answers to S or R" in u
    tail10, tail11 = body10[body10.index("# Out of scope"):], body11[body11.index("# Out of scope"):]
    assert tail11.split("# Your task")[0] == tail10.split("# Your task")[0]  # the shared middle
    assert "usability: {verdict,\nissues, note}}" in tail11


def test_v11_schema_strict_and_mapping() -> None:
    d = screen.schema_for("screen-v11")
    assert d is screen.SCHEMA_DUAL_USABILITY
    assert strict_schema(d) == d and strict_violations(wire_schema(d)) == []
    assert {k: v for k, v in d["properties"].items() if k != "usability"} == screen.SCHEMA_DUAL["properties"]
    u = d["properties"]["usability"]
    assert u["required"] == ["verdict", "issues", "note"] and u["additionalProperties"] is False
    assert u["properties"]["verdict"] == {"type": "string", "enum": ["usable", "degraded", "unusable"]}  # not nullable
    assert u["properties"]["issues"] == {"type": "array", "items": {
        "type": "string", "enum": ["not_body", "cut_off", "garbled_maths", "garbled_tables", "bad_encoding",
                                   "mostly_markup", "stub", "other"]}}
    assert u["properties"]["note"] == {"type": ["string", "null"], "maxLength": 200}
    assert screen.records({"name": "screen-v11"}) == ("stack_relevance", "sr_relevance", "text_usability")
    assert screen.question_mode("screen-v11") == "dual" and screen.tier1_max_tokens("screen-v11") == 1536
    assert screen.input_limits({"name": "screen-v11"}) == ((12_000, 2_000), None)  # tier 2: whole text
    assert "tier1_chars" in screen.PIN_KEYS and "tier2_chars" in screen.PIN_KEYS


# ── (2) excerpt rendering ────────────────────────────────────────────────────────────────────

WORDS = "alpha beta gamma delta epsilon zeta eta theta iota kappa"  # 56 chars


def test_excerpt_short_text_is_whole() -> None:
    out, shown = screen.excerpt(WORDS, 40, 16)
    assert (out, shown) == (f"Document length: 56 characters; shown: all.\n\n{WORDS}", 56)


def test_excerpt_long_text_head_marker_tail() -> None:
    out, shown = screen.excerpt(WORDS, 12, 7)  # head ends inside "gamma", tail starts inside "iota"
    assert out == ("Document length: 56 characters; shown: the first 10 and the last 5.\n\n"
                   "alpha beta\n\n[… 41 characters omitted by the screener …]\n\nkappa")
    assert shown == 15 and 10 + 41 + 5 == len(WORDS)


def test_excerpt_boundary_already_at_whitespace() -> None:
    out, shown = screen.excerpt(WORDS, 10, 10)  # WORDS[10] is a space; WORDS[45] (before "iota") too
    assert out.endswith("alpha beta\n\n[… 36 characters omitted by the screener …]\n\niota kappa") and shown == 20
    assert "shown: the first 10 and the last 10." in out


def test_excerpt_hard_cut_and_no_tail() -> None:
    text = "x" * 1000
    out, shown = screen.excerpt(text, 300, 200)
    assert out == ("Document length: 1000 characters; shown: the first 300 and the last 200.\n\n" + "x" * 300
                   + "\n\n[… 500 characters omitted by the screener …]\n\n" + "x" * 200) and shown == 500
    out0, shown0 = screen.excerpt(WORDS, 12, 0)
    assert out0.endswith("alpha beta\n\n[… 46 characters omitted by the screener …]") and shown0 == 10


def test_screening_text_forms() -> None:
    long = "w " * 5000
    assert screen.screening_text(long, 8000) == (long[:8000] + "\n…[truncated]", 8000)  # v3's tier 1
    assert screen.screening_text(long, None) == (long, len(long))
    assert screen.screening_text(WORDS, (12, 7))[1] == 15


# ── (3) older names keep their pins ──────────────────────────────────────────────────────────

FIXED = {"model": "x/y", "max_tokens": 1000, "temperature": 0}
#: template_hash(template(name), schema_for(name), FIXED) at 98ecf14 (v7-v9: tests/test_screen_sr_schema.py).
DIGEST_BEFORE = {"screen-v10": "78d47080f1ac7e5fe30e5d395125171d0bd4e79371664c3b9ce037cfa2c7fefa",
                 "screen-stack-v1": "946eadccc3f7e4c5ec9c991827667f2fc26388b5e90dae3fb7d7b89353ccc208",
                 "screen-sr-v1": "fe80fdaac45b2fceb63d10ba862a286fab00a6c79084206429e64b9c213c06d7"}
SHORT = "A thin abstract about agent foundations and embedded agency."
LONG = ("word " * 3000) + "TAIL"
#: sha256 of the (tier 1, tier 2) user messages ``ask`` sent at 98ecf14 for SHORT and LONG (title "Paper",
#: tier-2 rationale "x" * 50), the same for every older name.
USER_BEFORE = {"short": ["4cbb0bb85d97d4e594aedb570f9fce070dcdf6891c7216f6c03c8a014a6e5524",
                         "3b650c08b324257b3bd8d3a0b7384ec1179e8b9b7dc562c4b5a986ea9184b90d"],
               "long": ["c969a9adf775ac2574b4ab217240a12eae8a1d45b189395978824012d4cfaab2",
                        "41748e6e4f727dfc4c05b4b7d8ddcb35a36c03fb22ababe6d541cbe06bcf7352"]}


class _Capture:
    """Records each ``complete``'s ``user``; answers tier 1 ``borderline`` with every object a record reads."""

    def __init__(self) -> None:
        self.users: list[str] = []

    def complete(self, **kw: Any) -> Any:
        from types import SimpleNamespace

        self.users.append(kw["user"])
        side = {"verdict": "borderline", "confidence": 0.5, "quotes": []}
        return SimpleNamespace(model=kw.get("model"), parsed={
            "verdict": "borderline", "rationale": "x" * 50, "buckets": [], "quotes": [], "confidence": 0.5,
            "category": "none", "stack": side, "sr": {**side, "category": "none", "agenda": None},
            "usability": {"verdict": "usable", "issues": [], "note": None}})


def _actx(pin: dict[str, Any], client: Any) -> AskContext:
    return AskContext(run_id="r", stage="annotate.screen", store_root=None, pin=screen.prompt(pin, CONFIG.llm),
                      plan_pin=pin, options={}, budget_usd=None, _config=lambda: CONFIG, _client=client)


def _item(text: str) -> AskItem:
    return AskItem(doc_id="d1", source_id="d1", basis_kind="text", basis_digest="b" * 64, fields=None,
                   row={"doc_id": "d1", "metadata": {"title": "Paper"}, "content": {"current_role": "abstract"}},
                   license_class="open", attempt=1, load_text=lambda: text)


@pytest.mark.parametrize("name", OLDER)
def test_older_names_send_the_same_bytes(name: str) -> None:
    assert screen.input_limits({"name": name}) == screen.LEGACY_LIMITS == (8000, None)
    if name in DIGEST_BEFORE:
        assert template_hash(screen.template(name), screen.schema_for(name), FIXED) == DIGEST_BEFORE[name]
    pin = {"name": name, "model": LUNA, "adjudicate_model": TERRA, "tier2": True}
    for label, text in (("short", SHORT), ("long", LONG)):
        cap = _Capture()
        screen.ask(_item(text), _actx(pin, cap))
        assert [hashlib.sha256(u.encode()).hexdigest() for u in cap.users] == USER_BEFORE[label], (name, label)


@pytest.mark.parametrize("bad", [0, -1, True, "8000", [1], [0, 5], [10, -1], [1.5, 2], {"head": 1}])
def test_bad_limits_refuse_before_any_call(bad: Any) -> None:
    for key in ("tier1_chars", "tier2_chars"):
        with pytest.raises(StageError, match=key):
            screen.prompt({"name": "screen-v11", "model": LUNA, key: bad}, CONFIG.llm)


def test_limit_keys_override_and_render() -> None:
    pin = {"name": "screen-v10", "model": LUNA, "adjudicate_model": TERRA, "tier1_chars": [20, 10],
           "tier2_chars": 30}
    assert screen.input_limits(pin) == ((20, 10), 30)
    cap = _Capture()
    screen.ask(_item(LONG), _actx(pin, cap))
    t1, t2 = cap.users
    assert "Document length: 15004 characters; shown: the first 19 and the last 9." in t1
    assert t2.endswith("Screening text:\n" + LONG[:30] + "\n…[truncated]")
    assert "possibly the same excerpt the first pass saw" in t2 and "entire stored snapshot" not in t2
    whole = {**pin, "tier1_chars": None, "tier2_chars": None}
    cap2 = _Capture()
    screen.ask(_item(LONG), _actx(whole, cap2))
    assert cap2.users[0].endswith("excerpt):\n" + LONG) and "entire stored snapshot" in cap2.users[1]


# ── (4) ask with screen-v11 ──────────────────────────────────────────────────────────────────

MIDDLE = "a sentence about reward hacking that only the omitted middle carries"
V11_TEXT = "HEAD-MARK opening words. " + "filler text here. " * 2000 + MIDDLE + " " + "more filler. " * 2000 \
    + "TAIL-MARK closing words."


def v11(verdict: str, stack: dict[str, Any], sr: dict[str, Any], usability: dict[str, Any]) -> Any:
    return ok({"verdict": verdict, "rationale": f"overall {verdict}", "buckets": ["paper"], "off_topic_reason": None,
               "stack": {"quotes": [], "confidence": 0.8, **stack},
               "sr": {"quotes": [], "confidence": 0.6, "category": "none", "agenda": None, **sr},
               "usability": {"issues": [], "note": None, **usability}})


def test_v11_ask_excerpt_records_and_tier2(tmp_path: Path) -> None:
    assert len(V11_TEXT) > 32_000
    fake = FakeLlm()
    fake.on(model=LUNA, reply=v11("borderline", {"verdict": "borderline", "quotes": ["TAIL-MARK closing words.",
                                                                                      MIDDLE]},
                                   {"verdict": "irrelevant"},
                                   {"verdict": "degraded", "issues": ["garbled_maths", "cut_off"], "note": "eqs"}))
    fake.on(model=TERRA, reply=v11("relevant", {"verdict": "relevant", "quotes": ["HEAD-MARK opening words."]},
                                   {"verdict": "irrelevant"}, {"verdict": "usable"}))
    client = LlmClient(config=CONFIG.llm, transport=fake, ledger=Ledger(),
                       run_id="r-v11", stage="annotate.screen", cache=None)
    pin = {"name": "screen-v11", "model": LUNA, "tier2": True, "adjudicate_model": TERRA}
    ans = screen.ask(_item(V11_TEXT), _actx(pin, client))
    assert [r.model for r in fake.requests] == [LUNA, TERRA]
    assert [r.payload["max_tokens"] for r in fake.requests][0] == 1536
    sent1, sent2 = (prompt_text(r) for r in fake.requests)
    assert f"Document length: {len(V11_TEXT)} characters; shown: the first " in sent1
    assert "characters omitted by the screener …]" in sent1 and "HEAD-MARK" in sent1 and "TAIL-MARK" in sent1
    assert MIDDLE not in sent1.split("# Document")[-1]  # the omitted middle is not sent to tier 1
    # tier 2 (v11 default null): the whole text, v3's "entire stored snapshot" preamble
    assert MIDDLE in sent2 and not re.search(r"\[… [0-9,]+ characters omitted by the screener …\]", sent2)
    assert re.search(r"\[… [0-9,]+ characters omitted by the screener …\]", sent1)
    assert "possibly the same excerpt the first pass saw" not in sent2 and "entire stored snapshot" in sent2
    # quotes anchor against the whole text: the middle one too (the model could not see it; the anchor can)
    r1 = ans.records
    assert [q["text"] for q in r1["stack_relevance"]["quotes"]] == ["TAIL-MARK closing words.", MIDDLE]
    assert r1["text_usability"] == {"verdict": "degraded", "issues": ["garbled_maths", "cut_off"], "note": "eqs",
                                    "prompt_name": "screen-v11", "model": LUNA, "basis_role": "abstract",
                                    "adjudication_tier": 1}
    r2 = ans.followups[0].records
    assert r2["text_usability"] == {"verdict": "usable", "issues": [], "note": None, "prompt_name": "screen-v11",
                                    "model": TERRA, "basis_role": "abstract", "adjudication_tier": 2}
    assert r2["stack_relevance"]["verdict"] == "relevant" and r2["stack_relevance"]["tier1_verdict"] == "borderline"
    assert ans.payload["verdict"] == "borderline" and ans.followups[0].payload["verdict"] == "relevant"


# ── (5) through stack build ──────────────────────────────────────────────────────────────────

TEXTS = ["DOC_OK. A paper on reward hacking in reinforcement learning agents.",
         "DOC_STUB. Please enable JavaScript to continue."]


@pytest.mark.slow  # runner-driven build
def test_v11_records_through_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeLlm()
    fake.on("DOC_OK", v11("relevant", {"verdict": "relevant", "quotes": ["reward hacking"]}, {"verdict": "relevant"},
                          {"verdict": "usable"}), model=LUNA)
    fake.on("DOC_STUB", v11("irrelevant", {"verdict": "irrelevant"}, {"verdict": "irrelevant"},
                            {"verdict": "unusable", "issues": ["stub"], "note": "JavaScript wall"}), model=LUNA)
    monkeypatch.delenv("STACK_WORKFLOWS_ROOT", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("STACK_PROGRESS", "0")
    monkeypatch.setattr(annotate_stage, "TRANSPORT_HOOK", lambda: fake)
    monkeypatch.setattr(annotate_stage, "SLEEP", lambda _s: None)
    monkeypatch.setattr(annotate_stage, "CLOCK", lambda: AT_RUN)
    monkeypatch.chdir(tmp_path)
    store = tmp_path / "store"
    write_documents(store, [doc_with_text(store, i, t) for i, t in enumerate(TEXTS)], run=1)
    data = yaml.safe_load((REPO / "configs" / "plans" / "dev-v2.yaml").read_text("utf-8"))
    data["plan"] = "s3-v11"
    data["series"] = "main"  # the test world's series (write_documents)
    data["prompts"]["annotate.screen"] = {"name": "screen-v11", "model": LUNA, "tier2": False}
    data["fold"]["build"]["annotations_folded"] = ["screen", "stack_relevance", "sr_relevance", "text_usability"]
    data["fold"]["diagnostic"] = False
    data["stages"] = [{"stage": "fold.corpus"}, {"stage": "annotate.screen", "prior": ["screen", "screen_evidence"]},
                      {"stage": "fold.build"}]
    data["require_complete"] = ["screen", "screen_evidence", "text_usability"]
    path = tmp_path / "configs" / "plans" / "s3-v11.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), "utf-8")
    p: Plan = load_plan(path)
    b = run_build(store, p)
    assert b.status == "ok", b.data.get("error")
    assert {"screen", "stack_relevance", "sr_relevance", "text_usability", "screen_evidence"} <= set(b.files)
    ev = {e["doc_id"]: e for e in read_family(store / b.files["screen_evidence"]["path"])[0].to_pylist()}
    assert ev[sid(1)]["text_usability"] == {"verdict": "unusable", "issues": ["stub"], "note": "JavaScript wall",
                                            "prompt_name": "screen-v11", "model": LUNA,
                                            "basis_role": ev[sid(1)]["text_usability"]["basis_role"],
                                            "adjudication_tier": 1}
    tu = {r["doc_id"]: r for r in read_family(store / b.files["text_usability"]["path"])[0].to_pylist()}
    scr = {r["doc_id"]: r for r in read_family(store / b.files["screen"]["path"])[0].to_pylist()}
    assert tu[sid(0)]["verdict"] == "usable" and tu[sid(0)]["evidence_id"] == scr[sid(0)]["evidence_id"]
    assert (tu[sid(1)]["verdict"], tu[sid(1)]["issues"], tu[sid(1)]["workflow"]) == (
        "unusable", ["stub"], "text_usability")
    corpus = {r["doc_id"]: r for r in read_family(store / b.files["corpus"]["path"])[0].to_pylist()}
    a1 = corpus[sid(1)]["annotations"]["text_usability"]
    assert a1["status"] == "ok" and a1["payload"]["verdict"] == "unusable" and a1["payload"]["note"] == "JavaScript wall"
    assert b.data["verify"]["status"] == "ok"
    assert {r.check for r in run_checks(store) if r.status not in ("ok", "skipped")} == set()


# ── (6) stack eval screen --prompt screen-v11, --pin ────────────────────────────────────────


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeLlm:
    f = FakeLlm()
    monkeypatch.setattr(fakes, "INSTALLED", f)
    return f


def script_v11(fake: FakeLlm) -> None:
    fake.on("Title: Golf study", v11("relevant", {"verdict": "relevant"}, {"verdict": "relevant"},
                                     {"verdict": "usable"}), model=H)
    fake.on("Title: Hotel study", v11("irrelevant", {"verdict": "irrelevant"}, {"verdict": "irrelevant"},
                                      {"verdict": "unusable", "issues": ["stub", "mostly_markup"], "note": "wall"}),
            model=H)
    fake.on("Title: India study", v11("relevant", {"verdict": "relevant"}, {"verdict": "irrelevant"},
                                      {"verdict": "degraded", "issues": ["garbled_tables"]}), model=H)
    fake.on("Title: Juliet study", v11("borderline", {"verdict": "borderline"}, {"verdict": "irrelevant"},
                                       {"verdict": "usable"}), model=H)
    fake.on(model=TERRA, reply=v11("relevant", {"verdict": "relevant"}, {"verdict": "borderline"},
                                   {"verdict": "degraded", "issues": ["cut_off"]}))


def _juliet_user(fake: FakeLlm, model: str = H) -> str:
    """The user message of the latest request about Juliet (the long text) to ``model``."""
    reqs = [r for r in fake.requests if r.model == model and "Title: Juliet study" in prompt_text(r)]
    return str(reqs[-1].payload["messages"][-1]["content"])


def test_eval_v11_rows_usability_and_default_limits(tmp_path: Path, fake: FakeLlm) -> None:
    screen_set(tmp_path)  # frozen at the legacy 8,000-head cut: input_digest is not v11's message
    script_v11(fake)
    cli(tmp_path, "screen", "--set", SET, "--models", H, "--prompt", "screen-v11", "--workers", "1")
    (run,) = run_dirs(tmp_path, "screen")
    meta = json.loads((run / "run.json").read_text())
    assert meta["input_check"] == "frozen_only" and meta["pin_overrides"] == {}
    rows = {r["item_id"]: r for r in read_jsonl(run / "rows.jsonl")}
    j = rows["scr-0004"]  # Juliet: 8.9k chars fit in 12,000 + 2,000, so tier 1 sees the whole text (TAIL too)
    assert len(LONG_TEXT) < 14_000 and TAIL in _juliet_user(fake)
    assert "shown: all." in _juliet_user(fake)
    assert (j["chars_sent_tier1"], j["chars_sent_tier2"], j["tier2_attempted"]) == (len(LONG_TEXT), len(LONG_TEXT),
                                                                                    True)
    assert (j["usability_verdict"], j["usability_issues"], j["verdict"]) == ("degraded", ["cut_off"], "relevant")
    h = rows["scr-0002"]
    assert (h["usability_verdict"], h["usability_issues"], h["usability_note"], h["chars_sent_tier2"]) == (
        "unusable", ["stub", "mostly_markup"], "wall", None)
    assert all(r["question_mode"] == "dual" for r in rows.values())
    u = json.loads((run / "summary.json").read_text())["per_model"][H]["screen"]["usability"]
    assert u["verdicts"] == {"usable": 1, "degraded": 2, "unusable": 1, "none": 0}
    assert u["issues"] == {"cut_off": 1, "garbled_tables": 1, "mostly_markup": 1, "stub": 1} and u["notes"] == 1
    md = (run / "summary.md").read_text()
    assert "### text usability (question U)" in md and "| cut_off 1, garbled_tables 1, mostly_markup 1, stub 1 |" in md


def test_eval_pin_override_reaches_the_call(tmp_path: Path, fake: FakeLlm) -> None:
    screen_set(tmp_path)
    script_v11(fake)
    cli(tmp_path, "screen", "--set", SET, "--models", H, "--prompt", "screen-v11", "--no-tier2", "--workers", "1",
        "--pin", "tier1_chars=8000")
    (run,) = run_dirs(tmp_path, "screen")
    meta = json.loads((run / "run.json").read_text())
    assert meta["pin_overrides"] == {"tier1_chars": 8000} and meta["pins"][H]["tier1_chars"] == 8000
    assert meta["input_check"] == "user_message+frozen"  # the legacy limit: the frozen message is checked (and matches)
    user = _juliet_user(fake)
    assert TAIL not in user and user.endswith("\n…[truncated]")
    rows = {r["item_id"]: r for r in read_jsonl(run / "rows.jsonl")}
    assert rows["scr-0004"]["chars_sent_tier1"] == 8000 and rows["scr-0004"]["chars_sent_tier2"] is None
    # an excerpt override, and refusals: an option's key, a bad limit, a non-JSON value, a task without pins
    cli(tmp_path, "screen", "--set", SET, "--models", H, "--prompt", "screen-v11", "--no-tier2", "--workers", "1",
        "--pin", "tier1_chars=[200, 100]")
    user = _juliet_user(fake)
    assert user.endswith("excerpt):\n" + screen.excerpt(LONG_TEXT, 200, 100)[0])
    assert f"Document length: {len(LONG_TEXT)} characters; shown: the first 193 and the last 96." in user
    assert TAIL in user and "characters omitted by the screener …]" in user
    calls = fake.count()
    for bad in ('model="x/y"', "tier1_chars=0", "tier1_chars=[1,", "nonsense"):
        cli(tmp_path, "screen", "--set", SET, "--models", H, "--prompt", "screen-v11", "--pin", bad, code=1)
    cli(tmp_path, "triage", "--set", SET, "--models", H, "--pin", "tier1_chars=8000", code=2)  # no such option
    assert fake.count() == calls


def test_eval_legacy_limit_still_checks_the_frozen_message(tmp_path: Path, fake: FakeLlm) -> None:
    """Under the legacy tier-1 limit a moved frozen message refuses (the check is live, not skipped)."""
    d = screen_set(tmp_path)
    items = read_jsonl(d / "items.jsonl")
    items[0]["input_digest"] = "0" * 64
    write_jsonl(d / "items.jsonl", items)
    cli(tmp_path, "screen", "--set", SET, "--models", H, "--prompt", "screen-v11", "--no-tier2",
        "--pin", "tier1_chars=8000", code=1)
    assert fake.count() == 0
    script_v11(fake)
    cli(tmp_path, "screen", "--set", SET, "--models", H, "--prompt", "screen-v11", "--no-tier2", "--workers", "1")
    assert fake.count() == 4  # the default excerpt limit: the frozen message is not v11's, only the whole text checks


# ── (7) compare ──────────────────────────────────────────────────────────────────────────────


def _row(iid: str, verdict: str | None, stack: str | None, sr: str | None, *, status: str = "ok",
         prompt_tokens: int = 100, cost: float = 0.001) -> dict[str, Any]:
    return {"item_id": iid, "model": H, "repeat": 1, "status": status, "error_class": None if status == "ok" else "x",
            "verdict": verdict, "stack_verdict": stack, "sr_verdict": sr, "cache_hit": False, "cost_usd": cost,
            "prompt_tokens": prompt_tokens, "cached_tokens": 0, "cache_write_tokens": 0, "completion_tokens": 10,
            "reasoning_tokens": 0, "call_cost_usd": cost, "cost_nocache_usd": cost, "usage_source": "response",
            "usage_missing": 0}


def _run_dir(root: Path, name: str, set_dir: Path, rows: list[dict[str, Any]], **extra: Any) -> Path:
    d = root / name
    d.mkdir(parents=True)
    run = {"run_id": name, "task": "screen", "set": SET, "set_dir": str(set_dir), "split": "dev", "models": [H],
           "items": sorted({r["item_id"] for r in rows}), "gold": "gold.jsonl", "prompt": "screen-v10",
           "pins": {}, "cache_root": str(root / "cache"),  # set_items_digest: every harness run records it
           "set_items_digest": hashlib.sha256((set_dir / "items.jsonl").read_bytes()).hexdigest(), **extra}
    (d / "run.json").write_text(json.dumps(run))
    write_jsonl(d / "rows.jsonl", rows)
    return d


def test_compare_two_runs(tmp_path: Path) -> None:
    sd = tmp_path / "set"
    write_jsonl(sd / "items.jsonl", [{"item_id": f"i{n}", "input": {}} for n in range(1, 6)])
    gold = {"i1": ("relevant", "relevant", "irrelevant"), "i2": ("irrelevant", "irrelevant", "irrelevant"),
            "i3": ("relevant", "irrelevant", "relevant"), "i4": ("borderline", "borderline", "irrelevant"),
            "i5": ("relevant", "relevant", "relevant")}
    write_jsonl(sd / "gold.jsonl", [{"item_id": i, "label": {"verdict": v, "stack_verdict": s, "sr_verdict": r}}
                                    for i, (v, s, r) in gold.items()])
    a = _run_dir(tmp_path / "out", "A", sd, [
        _row("i1", "relevant", "relevant", "irrelevant"), _row("i2", "irrelevant", "irrelevant", "irrelevant"),
        _row("i3", "irrelevant", "irrelevant", "irrelevant"), _row("i4", "borderline", "borderline", "irrelevant"),
        _row("i5", None, None, None, status="failed_permanent")])
    b = _run_dir(tmp_path / "out", "B", sd, [
        _row("i1", "relevant", "relevant", "irrelevant", prompt_tokens=300, cost=0.003),
        _row("i2", "relevant", "relevant", "irrelevant", prompt_tokens=300, cost=0.003),
        _row("i3", "relevant", "irrelevant", "relevant", prompt_tokens=300, cost=0.003),
        _row("i4", "borderline", "borderline", "irrelevant", prompt_tokens=300, cost=0.003),
        _row("i5", "relevant", "relevant", "relevant", prompt_tokens=300, cost=0.003)],
        prompt="screen-v11", pin_overrides={"tier1_chars": 8000})
    c = scorer.compare_runs(a, b, price_for=CONFIG.llm.price_for)
    m = c["per_model"][H]
    assert m["pairs"] == 5
    ov, st, sr = m["questions"]["overall"], m["questions"]["stack"], m["questions"]["sr"]
    # overall: A right on i1 i2 i4 (3/5), B right on i1 i3 i4 i5 (4/5): b = {i2}, c = {i3, i5}
    assert (ov["accuracy_a"], ov["accuracy_b"], ov["b"], ov["c"]) == (0.6, 0.8, 1, 2)
    assert ov["p_value"] == pytest.approx(1.0)
    assert [(d["item_id"], d["gold"], d["a"], d["b"]) for d in ov["discordant"]] == [
        ("i2", "irrelevant", "irrelevant", "relevant"), ("i3", "relevant", "irrelevant", "relevant"),
        ("i5", "relevant", "no_answer", "relevant")]
    # S: A right on i1 i2 i3 i4, B right on i1 i3 i4 i5
    assert (st["accuracy_a"], st["accuracy_b"], st["b"], st["c"], st["answered_a"]) == (0.8, 0.8, 1, 1, 4)
    # R: A right on i1 i2 i4, B right on all five
    assert (sr["b"], sr["c"], sr["p_value"]) == (0, 2, pytest.approx(0.5))
    assert (m["prompt_tokens_per_row_a"], m["prompt_tokens_per_row_b"]) == (100, 300)
    assert (m["usd_per_1000_settled_a"], m["usd_per_1000_settled_b"]) == (pytest.approx(1.0), pytest.approx(3.0))
    md = Path(c["path"]).read_text()
    assert "B = `B` (screen-v11 (tier1_chars=8000))" in md and "i3 gold relevant, A irrelevant, B relevant" in md
    # refusals: different items, no model in common
    other = _run_dir(tmp_path / "out", "C", sd, [_row("i1", "relevant", "relevant", "irrelevant")])
    with pytest.raises(ValueError, match="different items"):
        scorer.compare_runs(a, other, price_for=CONFIG.llm.price_for, write=False)
    (other / "run.json").write_text(json.dumps({**json.loads((a / "run.json").read_text()), "models": ["x/y"]}))
    with pytest.raises(ValueError, match="no model in common"):
        scorer.compare_runs(a, other, price_for=CONFIG.llm.price_for, write=False)
