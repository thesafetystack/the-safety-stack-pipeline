"""``screen-v13`` (D656) and the off-by-default ``screen-v13-p4`` (D657), on the fake
model only.

(1) older names keep their bytes: template digests at fixed params and ``screen-v12``'s tier-1 / tier-2 user
messages for a row that has a date and channels (sha256 recorded at 5ac481f);
(2) the v13 user message: ``Published: <year>`` and ``Source: <channels>`` after the title on both tiers, each
left out when the row lacks it (then the message is v12's byte for byte); the lines equal triage's;
(3) registration: every template file has a row in ``SPECS`` and every row a file; an unregistered file
refuses before any call (the review's first v13 eval runs answered question S only because the names were in no
table); v13 and v13-p4 are v12's row plus the record lines; every row sends what the parallel name tables sent
before the registry (a fingerprint per name of both tiers' calls and the answer, recorded at ba4fc01); ``ask``
produces v12's records; ``stack eval screen --prompt`` takes each name in dual mode;
(4) the texts: v13 = v12 plus the narrow-settings paragraph and the record note, without the announcement
paragraph; v13-p4 = v13 plus the technique-provenance clause (question S only) and nothing else.
"""

from __future__ import annotations

import datetime as dt
import difflib
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from stack.annotate import AskContext, AskItem
from stack.config import load
from stack.core.hashes import template_hash
from stack.stage_runner import StageError
from tests.fakes.llm import FakeLlm, prompt_text
from tests.test_screen_v12 import _client, v12
from workflows.annotate.screen import main as screen
from workflows.triage import ask as triage_ask

CONFIG = load()
LUNA = "openai/gpt-6-luna"
FIXED = {"model": "x/y", "max_tokens": 1000, "temperature": 0}
NEW = ("screen-v13", "screen-v13-p4")
#: template_hash(template(name), schema_for(name), FIXED) at 5ac481f.
DIGEST_BEFORE = {
    "screen-v7": "aed978331b6f3d356fe161d9f0232363f797db4c2f9d2d03b47609ba45853c4e",
    "screen-v8": "1f0b3bc93d39c23636a01e091fea01d11e99b18cb0534534e7997c291650bef5",
    "screen-v9": "0ad2789318357137eec9ba01a444468c585ce147a8131d2521744f1d8c19712b",
    "screen-v10": "78d47080f1ac7e5fe30e5d395125171d0bd4e79371664c3b9ce037cfa2c7fefa",
    "screen-v11": "7eda329efea3b5bbb105e0912b5cfcb63432ca66113362e69d706ce66c06500c",
    "screen-v12": "adf4d44b3bfe5e9224a13294ab3c150570a327385626f378ec8039679d62a55d",
    "screen-stack-v1": "946eadccc3f7e4c5ec9c991827667f2fc26388b5e90dae3fb7d7b89353ccc208",
    "screen-sr-v1": "fe80fdaac45b2fceb63d10ba862a286fab00a6c79084206429e64b9c213c06d7"}
ROW = {"doc_id": "d1", "metadata": {"title": "Safe interruptibility", "doc_type": "preprint",
                                    "published_at": dt.datetime(2016, 6, 1, tzinfo=dt.UTC)},
       "identity": {"channels": ["alignmentforum", "lesswrong"]}, "content": {"current_role": "post"}}
SHORT = "A thin abstract about agent foundations and embedded agency."
LONG = ("word " * 3000) + "TAIL"
#: sha256 of the (tier 1, tier 2) user messages screen-v12's ask sent for ROW at 5ac481f (tier-2 rationale "x" * 50).
V12_USER = {"short": ["08708191b344dfe427733a0500605ae74d573e8cbe3e7ff2abc6157264527f35",
                      "9895170f750fec6169ad248b09fb5eda14bbfb9b95b0dded1e7ceed1b1902c6c"],
            "long": ["422f14e14beab92897bf8c3854e8ef7ee202dffd9a4ec26c6f895c71ffecf252",
                     "59d217713d76e94f02c3a780edcb0cd37293a7313235387c7d71cfa8b7300724"]}
NARROW = """# Narrow settings (era-free)

A narrow experimental setting does not by itself exclude a general safety method or an AI-alignment argument: a toy
task, a tabular or robotic agent, a narrow benchmark or a pre-2020 deep network is not grounds for exclusion when the
contribution is safe exploration, an explicit safety constraint, interruptibility or corrigibility, reward
misspecification or wireheading, low-impact agents, scalable oversight, runtime shielding, formal safety verification
of an AI policy, or robustness to adversarial inputs and distributional shift framed as a safety problem. Judge the
contribution and its intended applicability, under the vocabulary of its time (Friendly AI, value alignment, value
learning, safe interruptibility …). Ordinary task-performance improvements and domain applications stay out.
"""
PROVENANCE = """# Technique provenance (question S only)

Reusable methods, decisive negative results and evaluation protocols that establish the provenance of a catalogued
safety technique — private training, privacy and memorisation auditing, poisoning and backdoor defences,
model-extraction defences, unlearning, training-data attribution, adversarial training and certified robustness,
interpretability methods (saliency, attribution, probing, concept methods, representation analysis), model
documentation — count for question S even when demonstrated on a task-specific model without an advanced-AI framing;
this does not by itself make them relevant to question R.
"""
DOCUMENT_NOTE = ("Before the text the message gives the title and, when known, `Published:` (the year of the "
                 "document's publication date) and `Source:` (the channels it was collected from: `arxiv`, "
                 "`openalex`, `semantic_scholar`, `lesswrong`, `alignmentforum`, a lab or organisation blog …; "
                 "`openalex` and `semantic_scholar` are indexes, not venues).")


class _Capture:
    """Records each ``complete``'s ``user``; answers tier 1 ``borderline`` with every object a record reads."""

    def __init__(self) -> None:
        self.users: list[str] = []

    def complete(self, **kw: Any) -> Any:
        self.users.append(kw["user"])
        side = {"verdict": "borderline", "confidence": 0.5, "quotes": []}
        return SimpleNamespace(model=kw.get("model"), parsed={
            "verdict": "borderline", "rationale": "x" * 50, "buckets": [], "quotes": [], "confidence": 0.5,
            "category": "none", "stack": side, "sr": {**side, "category": "none", "agenda": None},
            "usability": {"verdict": "usable", "issues": [], "note": None},
            "doc_class": {"doc_type": "other", "self_published": False, "primary_lang": None}})


def _actx(pin: dict[str, Any], client: Any) -> AskContext:
    return AskContext(run_id="r", stage="annotate.screen", store_root=None, pin=screen.prompt(pin, CONFIG.llm),
                      plan_pin=pin, options={}, budget_usd=None, _config=lambda: CONFIG, _client=client)


def _item(text: str, row: dict[str, Any] = ROW) -> AskItem:
    return AskItem(doc_id="d1", source_id="d1", basis_kind="text", basis_digest="b" * 64, fields=None, row=row,
                   license_class="open", attempt=1, load_text=lambda: text)


def users(name: str, text: str, row: dict[str, Any] = ROW) -> list[str]:
    cap = _Capture()
    screen.ask(_item(text, row), _actx({"name": name, "model": LUNA, "adjudicate_model": LUNA, "tier2": True}, cap))
    return cap.users


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# ── (1) older names ──────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", sorted(DIGEST_BEFORE))
def test_older_names_keep_their_template_digests(name: str) -> None:
    assert template_hash(screen.template(name), screen.schema_for(name), FIXED) == DIGEST_BEFORE[name]
    assert not screen.spec(name).record_lines


@pytest.mark.parametrize("label", ["short", "long"])
def test_v12_user_messages_unchanged_for_a_dated_row_with_channels(label: str) -> None:
    got = users("screen-v12", SHORT if label == "short" else LONG)
    assert [sha(u) for u in got] == V12_USER[label]
    assert all("Published:" not in u and "Source:" not in u for u in got)


# ── (2) the v13 user message ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", NEW)
def test_v13_user_message_has_the_record_lines_on_both_tiers(name: str) -> None:
    t1, t2 = users(name, SHORT)
    lines = "Published: 2016\n\nSource: alignmentforum, lesswrong\n\n"
    assert t1.startswith("Title: Safe interruptibility\n\n" + lines
                         + "Screening text (abstract or document excerpt):\n")
    assert ("Title: Safe interruptibility\n\n" + lines + "Screening text:\n") in t2
    v12_1, v12_2 = users("screen-v12", SHORT)
    assert t1.replace(lines, "", 1) == v12_1 and t2.replace(lines, "", 1) == v12_2


@pytest.mark.parametrize(("metadata", "identity", "lines"), [
    ({"published_at": None}, {"channels": ["arxiv", "openalex"]}, ["Source: arxiv, openalex"]),
    ({}, None, []),
    ({"published_at": dt.date(2009, 3, 1)}, {"channels": []}, ["Published: 2009"]),
])
def test_v13_record_lines_leave_out_what_the_row_lacks(metadata: dict, identity: dict | None,
                                                       lines: list[str]) -> None:
    row: dict[str, Any] = {"doc_id": "d1", "metadata": {"title": "T", **metadata},
                           "content": {"current_role": "abstract"}}
    if identity is not None:
        row["identity"] = identity
    assert screen.record_lines(row) == lines
    t1, _ = users("screen-v13", SHORT, row)
    assert "None" not in t1.split("Screening text")[0]
    assert (t1 == users("screen-v12", SHORT, row)[0]) == (lines == [])


def test_record_lines_are_triage_s_renderer_and_refuse_a_string_date() -> None:
    assert screen.record_lines is triage_ask.provenance_lines  # one renderer, stack/annotate/provenance.py
    assert screen.record_lines(ROW) == ["Published: 2016", "Source: alignmentforum, lesswrong"]
    with pytest.raises(TypeError, match="published_at"):
        screen.record_lines({"metadata": {"published_at": "2016"}})


# ── (3) registration ─────────────────────────────────────────────────────────────────────────


def test_every_template_file_is_a_registered_name() -> None:
    files = {p.stem for p in screen.HERE.glob("*.md")}
    assert {screen.DEFAULT_NAME if f == "screen" else f for f in files} == set(screen.SPECS)
    assert {n for n, s in screen.SPECS.items() if s.record_lines} == set(NEW)


@pytest.mark.parametrize("name", NEW)
def test_new_names_are_v12_plus_the_record_lines(name: str) -> None:
    ref = "screen-v12"
    assert screen.SPECS[name] == screen.SPECS[ref]._replace(record_lines=True)
    assert screen.schema_for(name) is screen.schema_for(ref) is screen.SCHEMA_V12
    assert screen.question_mode(name) == screen.question_mode(ref) == "dual"
    assert screen.tier1_max_tokens(name) == screen.tier1_max_tokens(ref) == 1536
    assert screen.WORKFLOW.produced({"name": name}) == screen.WORKFLOW.produced({"name": ref})
    assert screen.input_limits({"name": name}) == screen.input_limits({"name": ref})


class _Recorder(_Capture):
    """:class:`_Capture` that also records what each call sends besides the user message."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[list[Any]] = []

    def complete(self, **kw: Any) -> Any:
        self.calls.append([kw["role"], kw.get("model"), kw["user"], kw.get("max_tokens"), sorted(kw["refs"]),
                           kw.get("attempt_kind"), sha(kw["template"]),
                           sha(json.dumps(kw["schema"], sort_keys=True))])
        res = super().complete(**kw)  # distinct confidences, so the payload shows which one is stored
        res.parsed |= {"confidence": 0.31, "stack": {**res.parsed["stack"], "confidence": 0.62},
                       "sr": {**res.parsed["sr"], "verdict": "relevant", "confidence": 0.47}}
        return res


def fingerprint(name: str) -> str:
    """sha256 over what ``ask`` sends for ``name`` on both tiers (role, model, user message, ``max_tokens``, refs,
    template, schema) and what it returns (payloads, records), for a short and a long text."""
    out: list[Any] = []
    for text in (SHORT, LONG):
        rec = _Recorder()
        pin = {"name": name, "model": LUNA, "adjudicate_model": LUNA, "tier2": True}
        ans = screen.ask(_item(text), _actx(pin, rec))
        assert ans is not None
        out.append([rec.calls, ans.payload, ans.records,
                     [[f.payload, f.records] for f in ans.followups if hasattr(f, "payload")]])
    return sha(json.dumps(out, sort_keys=True, default=str))


#: :func:`fingerprint` of every name at ba4fc01, the last tree with the parallel name tables (``V12_LIKE``,
#: ``NAMES``, ``RECORD_LINE_TEMPLATES``, the schema / question-mode / max-tokens / confidence / menu / records /
#: limits tables).
FINGERPRINT_BEFORE: dict[str, str] = {
    "screen-sr-v1": "abfe01e4d97b7c325e1772aa29c290258178b8c0c849c5bd5074532100bedfb9",
    "screen-stack-v1": "c71de00359742af47d0c7417259260b771fe47ad986e952b4914bb66725404b1",
    "screen-v10": "5fe5f7a7ffbec524d9700bb62dff21666a12b200176c146e8e59f9132ffa79fe",
    "screen-v11": "5318a9ca2651739445694e407c8756c5f80ae533b1e38b0d9c8a376b9cfed553",
    "screen-v12": "79ce8bb44c1bc6fd226abab55306dd2022848fde7c992bd429520fb772b18236",
    "screen-v13": "5871d5f46975f942ff9bcb17eccebd6e93f07afcd828205ef1f741b27841342b",
    "screen-v13-p4": "94192cf390cab72b58e1422e45f5c4328a2ec7520d80729b8555c6348891cd0c",
    "screen-v7": "21e0e7986deb90f2901e35d0938a1cc2a20bfd09f4c86673c765db76aa7287d5",
    "screen-v8": "61baf1dc4023c45acf92160dc8b41b5d5db0c76890aab3f2a8eb8f1ef66e8736",
    "screen-v9": "c9e9778dc52ea30584e85df2148514f8339d683791d7b05376ab1581bb24ef84",
}


def test_the_registry_has_the_names_the_tables_had() -> None:
    assert set(screen.SPECS) == set(FINGERPRINT_BEFORE)


@pytest.mark.parametrize("name", sorted(FINGERPRINT_BEFORE))
def test_every_registered_name_sends_what_the_tables_sent(name: str) -> None:
    assert fingerprint(name) == FINGERPRINT_BEFORE[name]
    s = screen.spec(name)
    assert (screen.schema_for(name), screen.question_mode(name), screen.tier1_max_tokens(name),
            screen.records({"name": name}), tuple(screen.input_limits({"name": name}))) == (
        s.schema, s.mode, s.tier1_max_tokens, s.records, s.limits)


def test_an_unregistered_template_file_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "screen-v77.md").write_text("A template nobody registered.\n")
    monkeypatch.setattr(screen, "HERE", tmp_path)
    with pytest.raises(StageError, match="not registered"):
        screen.template("screen-v77")
    with pytest.raises(StageError, match="not registered"):
        screen.prompt({"name": "screen-v77", "tier2": False}, CONFIG.llm)
    with pytest.raises(StageError, match="no template"):
        screen.template("screen-v99")


def test_v13_ask_produces_the_v12_records(tmp_path: Path) -> None:
    fake = FakeLlm()
    fake.on(model=LUNA, reply=v12("relevant", {"verdict": "relevant", "confidence": 0.9}, {"verdict": "irrelevant"},
                                  {"doc_type": "paper"}))
    pin = {"name": "screen-v13", "model": LUNA, "tier2": False}
    ans = screen.ask(_item("A body about safe interruptibility.", {**ROW, "metadata": {**ROW["metadata"],
                                                                                     "doc_type": "forum_post"}}),
                     _actx(pin, _client(tmp_path, fake)))
    assert ans is not None and set(ans.records) == {"stack_relevance", "sr_relevance", "text_usability", "doc_class"}
    assert ans.records["doc_class"]["doc_type"] == "forum_post" and ans.payload["confidence"] == 0.9
    sent = prompt_text(fake.requests[0])
    assert "{{ref:" not in sent and "Published: 2016" in sent and "# Narrow settings (era-free)" in sent


@pytest.mark.parametrize("name", NEW)
def test_stack_eval_screen_takes_each_new_name_in_dual_mode(name: str) -> None:
    from stack.evals.harness import make_adapter

    adapter = make_adapter("screen", CONFIG, None, prompt=name, models=[LUNA], tier2=True, tier2_model=LUNA)
    assert adapter.prompt_name == name and adapter.question_mode == "dual"  # type: ignore[attr-defined]
    pin = adapter.pins()[LUNA]
    assert pin["name"] == name and pin["template_digests"] == list(screen.prompt(
        {"name": name, "model": LUNA, "tier2": True, "adjudicate_model": LUNA}, CONFIG.llm).template_digests)


# ── (4) the texts ────────────────────────────────────────────────────────────────────────────


def body(name: str) -> str:
    return screen.template(name).split("\n", 1)[1]


def paras(text: str) -> list[str]:
    return [" ".join(p.split()) for p in text.split("\n\n") if p.strip()]


def inserted(old: str, new: str) -> list[str]:
    """The paragraphs ``new`` adds to ``old``; fails when ``new`` deletes or replaces any paragraph."""
    a, b = paras(old), paras(new)
    ops = difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes()
    assert [op for op in ops if op[0] not in ("equal", "insert")] == []
    return [p for tag, _i1, _i2, j1, j2 in ops if tag == "insert" for p in b[j1:j2]]


def test_v13_is_v12_plus_the_narrow_paragraph_and_the_document_note() -> None:
    v13 = body("screen-v13")
    assert inserted(body("screen-v12"), v13) == [*paras(NARROW), DOCUMENT_NOTE]
    assert "does not inherit the relevance" not in v13  # the announcement clause is dropped (C13: inert)
    assert v13.index("# Narrow settings (era-free)") < v13.index("# Answer scale")


def test_v13_p4_differs_from_v13_only_by_the_clause() -> None:
    b, v = body("screen-v13"), body("screen-v13-p4")
    assert inserted(b, v) == paras(PROVENANCE)
    assert v.replace(PROVENANCE + "\n", "", 1) == b
