"""``screen-v12`` and the tier-2 allowance (track T-SCREEN, round 5), on the fake model only.

(1) the v12 template: v11 plus question D, the menus written in (no ``{{ref:}}`` fill; they equal
``configs/enums.yaml``'s rendering, so an enum edit goes red here and needs a new name; review R5-2);
the strict ``doc_class`` schema object;
(2) ``ask`` with ``screen-v12``: no refs sent, the ``doc_class`` record (the channel's ``doc_type``
where it decides, the model's otherwise), ``SCREEN.confidence`` from the question(s) that gave the
top-level verdict, both tiers;
(3) the allowance (:class:`main.Tier2Allowance`): ``tier2_max_usd``, ``tier2_budget_share``,
``tier2_max_share``, bad values refused, ``ask`` returning None when tier 2 does not fit; the rate's
first-call admission (D706: a lone long-text borderline on a $0 tier-1 answer is not refused forever);
(4) through the stage runner (slow): a deferred borderline is ``not_attempted[budget]`` while later
documents are still asked, the run's budget scope untouched, counted ``not_attempted.deferred``
(build reason ``tier2_allowance``, D706), and a rerun with the first run's
outputs as its prior (``--in``) asks exactly that
document again with tier 1 from the response cache; the ``doc_class`` record file; checks green;
the build 2b case through the runner (D706).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

import stack.annotate.stage as annotate_stage
from stack.annotate import AskContext, AskItem
from stack.build import incomplete_reasons
from stack.checks.verify import run_checks
from stack.config import load
from stack.llm import LlmClient, strict_schema, wire_schema
from stack.llm.ledger import Ledger
from stack.plan import load_plan
from stack.stage_runner import StageError
from tests.fakes.annotate import AT_RUN, annotate, doc_with_text, fold_corpus, rows_of, write_documents
from tests.fakes.llm import FakeLlm, ok, prompt_text
from tests.synth.rows import sid
from tests.test_llm_strict import strict_violations
from workflows.annotate.screen import main as screen

REPO = Path(__file__).resolve().parents[1]
CONFIG = load()
LUNA = "openai/gpt-6-luna"
TERRA = "openai/gpt-5.6-terra"


# ── (1) template and schema ──────────────────────────────────────────────────────────────────


def test_v12_template_is_v11_plus_question_d_with_inline_menus() -> None:
    v11, v12 = screen.template("screen-v11"), screen.template("screen-v12")
    assert "{{ref:" not in v12 and "{{ref:enums}}" in v11
    b11, b12 = v11.split("\n", 1)[1], v12.split("\n", 1)[1]
    pre = b11[:b11.index("# Out of scope for both")]
    assert b12.startswith(pre + "# Question D: document type\n")
    menus = screen._render_menu(CONFIG, screen.INLINE_MENUS)
    # R5-2: the menus are part of the file (so of the template digest); a changed enums.yaml menu
    # turns this red: write a new template name with the new menu rather than editing v12.
    assert b12.count(menus.rstrip("\n")) == 1
    assert b11.replace("{{ref:enums}}", menus.rstrip("\n")).split("# Your task")[0] == \
        b12.replace(b12[b12.index("# Question D"):b12.index("# Out of scope for both")], "").split("# Your task")[0]
    assert "doc_class: {doc_type, self_published, primary_lang}" in b12


def test_v12_schema_strict_and_tables() -> None:
    d = screen.schema_for("screen-v12")
    assert d is screen.SCHEMA_V12
    assert strict_schema(d) == d and strict_violations(wire_schema(d)) == []
    assert {k: v for k, v in d["properties"].items() if k != "doc_class"} == screen.SCHEMA_DUAL_USABILITY["properties"]
    dc = d["properties"]["doc_class"]
    assert dc["required"] == ["doc_type", "self_published", "primary_lang"] and dc["additionalProperties"] is False
    assert dc["properties"]["doc_type"]["enum"] == list(screen.DOC_TYPES)
    assert set(screen.DOC_TYPES) == set(CONFIG.enums.enums["doc_type"].values)  # spec enum = enums.yaml domain
    assert dc["properties"]["self_published"] == {"type": "boolean"}
    assert dc["properties"]["primary_lang"] == {"type": ["string", "null"], "maxLength": 16}
    assert screen.records({"name": "screen-v12"}) == ("stack_relevance", "sr_relevance", "text_usability", "doc_class")
    assert screen.WORKFLOW.produced({"name": "screen-v12"}) == (
        "doc_class", "stack_relevance", "sr_relevance", "text_usability")
    assert screen.question_mode("screen-v12") == "dual" and screen.tier1_max_tokens("screen-v12") == 1536


@pytest.mark.parametrize(("doc_type", "decided"), [
    ("forum_post", "forum_post"), ("tweet", "tweet"), ("social_post", "tweet"), ("lab_blog", "lab_blog"),
    ("system_card", "system_card"), ("framework_policy", "framework_policy"), ("model_card", "model_card"),
    ("paper", None), ("preprint", None), ("journal", None), ("conference", None), ("webpage", None),
    ("other", None), (None, None)])
def test_channel_doc_type(doc_type: str | None, decided: str | None) -> None:
    assert screen.channel_doc_type({"metadata": {"doc_type": doc_type}}) == decided


# ── (2) ask with screen-v12 ──────────────────────────────────────────────────────────────────


def v12(verdict: str, stack: dict[str, Any], sr: dict[str, Any], doc_class: dict[str, Any], cost: float = 0.001) -> Any:
    return ok({"verdict": verdict, "rationale": f"overall {verdict}", "buckets": ["paper"], "off_topic_reason": None,
               "stack": {"quotes": [], "confidence": 0.8, **stack},
               "sr": {"quotes": [], "confidence": 0.6, "category": "none", "agenda": None, **sr},
               "usability": {"verdict": "usable", "issues": [], "note": None},
               "doc_class": {"self_published": False, "primary_lang": "en", **doc_class}}, cost=cost)


def _actx(pin: dict[str, Any], client: Any, *, options: dict[str, Any] | None = None, budget: float | None = None,
          run_id: str = "r") -> AskContext:
    return AskContext(run_id=run_id, stage="annotate.screen", store_root=None, pin=screen.prompt(pin, CONFIG.llm),
                      plan_pin=pin, options=options or {}, budget_usd=budget, _config=lambda: CONFIG, _client=client)


def _item(text: str, doc_type: str | None) -> AskItem:
    return AskItem(doc_id="d1", source_id="d1", basis_kind="text", basis_digest="b" * 64, fields=None,
                   row={"doc_id": "d1", "metadata": {"title": "Paper", "doc_type": doc_type},
                        "content": {"current_role": "full_text"}},
                   license_class="open", attempt=1, load_text=lambda: text)


PIN = {"name": "screen-v12", "model": LUNA, "tier2": True, "adjudicate_model": TERRA}


def _client(tmp_path: Path, fake: FakeLlm) -> LlmClient:
    return LlmClient(config=CONFIG.llm, transport=fake, ledger=Ledger(),
                     run_id="r-v12", stage="annotate.screen", cache=None)


def test_v12_ask_doc_class_confidence_and_tier2(tmp_path: Path) -> None:
    fake = FakeLlm()
    fake.on(model=LUNA, reply=v12("borderline", {"verdict": "borderline", "confidence": 0.45},
                                  {"verdict": "borderline", "confidence": 0.55},
                                  {"doc_type": "eval_report", "self_published": True, "primary_lang": "de"}))
    fake.on(model=TERRA, reply=v12("relevant", {"verdict": "irrelevant", "confidence": 0.9},
                                   {"verdict": "relevant", "confidence": 0.7},
                                   {"doc_type": "paper", "primary_lang": None}))
    ans = screen.ask(_item("A body about reward hacking.", "preprint"), _actx(PIN, _client(tmp_path, fake)))
    assert ans is not None
    sent = prompt_text(fake.requests[0])
    assert "{{ref:" not in sent and "doc_type:\n- paper: Peer-reviewed paper." in sent
    assert ans.records["doc_class"] == {"doc_type": "eval_report", "self_published": True, "primary_lang": "de"}
    assert ans.payload["confidence"] == 0.55  # both questions borderline = top verdict: the larger
    f = ans.followups[0]
    # tier 2 said paper; the row's metadata says preprint, which settles paper vs preprint
    assert f.records["doc_class"] == {"doc_type": "preprint", "self_published": False, "primary_lang": None}
    assert f.payload["confidence"] == 0.7  # only SR gave the top-level "relevant"
    assert f.payload["adjudication_tier"] == 2


def test_v12_channel_decides_doc_type(tmp_path: Path) -> None:
    fake = FakeLlm()
    fake.on(model=LUNA, reply=v12("relevant", {"verdict": "relevant", "confidence": 0.9}, {"verdict": "irrelevant"},
                                  {"doc_type": "paper"}))
    ans = screen.ask(_item("A lab post.", "lab_blog"), _actx(PIN, _client(tmp_path, fake)))
    assert ans is not None and ans.records["doc_class"]["doc_type"] == "lab_blog"
    assert ans.payload["confidence"] == 0.9 and not ans.followups


@pytest.mark.parametrize(("doc_type", "model", "expect"), [
    ("paper", "preprint", "paper"), ("journal", "preprint", "paper"), ("conference", "preprint", "paper"),
    ("preprint", "paper", "preprint"), ("other", "paper", "paper"), (None, "preprint", "preprint"),
    ("paper", "eval_report", "eval_report"), ("paper", "other", "other"), ("lab_blog", "paper", "lab_blog")])
def test_doc_class_paper_vs_preprint_from_metadata(tmp_path: Path, doc_type: str | None, model: str,
                                                   expect: str) -> None:
    fake = FakeLlm()
    fake.on(model=LUNA, reply=v12("relevant", {"verdict": "relevant"}, {"verdict": "irrelevant"}, {"doc_type": model}))
    ans = screen.ask(_item("A body.", doc_type), _actx(PIN, _client(tmp_path, fake)))
    assert ans is not None and ans.records["doc_class"]["doc_type"] == expect
    assert screen.metadata_paper_type({"metadata": {"doc_type": doc_type}}) == screen.METADATA_PAPER_TYPES.get(doc_type)


def test_v11_confidence_stays_null(tmp_path: Path) -> None:
    fake = FakeLlm()
    reply = v12("relevant", {"verdict": "relevant"}, {"verdict": "irrelevant"}, {"doc_type": "paper"})
    body = json.loads(reply.body["choices"][0]["message"]["content"])
    body.pop("doc_class")  # v11's schema has no doc_class object
    fake.on(model=LUNA, reply=ok(body))
    pin = {**PIN, "name": "screen-v11", "tier2": False}
    pin.pop("adjudicate_model")
    ans = screen.ask(_item("Body.", None), _actx(pin, _client(tmp_path, fake)))
    assert ans is not None and ans.payload["confidence"] is None and "doc_class" not in ans.records


# ── (3) the allowance ────────────────────────────────────────────────────────────────────────


def test_allowance_max_usd_and_share_of_spend() -> None:
    a = screen.Tier2Allowance(0.10, None, "t")
    assert a.admit(0.06) and not a.admit(0.05)  # in flight counts
    a.settle(0.06, 0.02)
    assert a.admit(0.05) and a.spent == pytest.approx(0.02)
    s = screen.Tier2Allowance(None, 0.1, "s")
    assert not s.admit(0.01)  # no tier-1 spend yet: 0.01 > 0.1 * 0.01
    s.add_tier1(0.09)
    assert s.admit(0.01)  # 0.01 <= 0.1 * (0.09 + 0.01)
    s.settle(0.01, 0.01)
    assert not s.admit(0.001) and s.deferred == 2


def test_allowance_screen_usd_per_1k() -> None:
    a = screen.Tier2Allowance(None, None, "r", usd_per_1k=3.0)
    for _ in range(10):
        a.add_tier1(0.001)  # 10 documents at $0.001: $0.02 left of the $0.03 the rate gives
    # nothing of tier 2 spent or in flight: the first call is admitted even over the rate (D706)
    assert a.docs == 10 and a.admit(0.021)
    a.settle(0.021, 0.004)  # $0.016 left
    assert not a.admit(0.0161) and a.admit(0.016)
    a.settle(0.016, 0.0)  # a tier-2 cache hit
    a.add_tier1(0.0)  # a tier-1 cache hit: one more document, $0.003 more room: $0.033 - $0.014
    assert a.admit(0.019) and not a.admit(0.0001)


def test_allowance_rate_admits_a_runs_first_tier2_call() -> None:
    """Build 2b (D706): the run's first answer a $0 tier-1 cache hit (one document: $0.003 of room), a
    long-text borderline whose tier-2 estimate is $0.00306. Admitted, as nothing of tier 2 is spent or
    in flight; then the rate binds again, in flight or settled; the hard cap and the share still refuse."""
    a = screen.Tier2Allowance(None, None, "r", usd_per_1k=3.0)
    a.add_tier1(0.0)
    assert a.admit(0.00306) and a.deferred == 0
    assert not a.admit(0.0001)  # in flight: $0.00316 > $0.003
    a.settle(0.00306, 0.0031)
    a.add_tier1(0.0)  # $0.006 of room, $0.0031 spent
    assert not a.admit(0.00306) and a.admit(0.0029)
    capped = screen.Tier2Allowance(0.003, None, "c", usd_per_1k=3.0)
    capped.add_tier1(0.0)
    assert not capped.admit(0.00306)  # tier2_max_usd stays a hard cap
    shared = screen.Tier2Allowance(None, 0.5, "s", usd_per_1k=3.0)
    shared.add_tier1(0.0)
    assert not shared.admit(0.00306)  # tier2_max_share: no tier-1 spend, nothing fits (D256)


@pytest.mark.parametrize(("options", "budget", "match"), [
    ({"screen_usd_per_1k": -3}, 1.0, "screen_usd_per_1k"),
    ({"tier2_max_usd": -1}, 1.0, "tier2_max_usd"), ({"tier2_max_usd": True}, 1.0, "tier2_max_usd"),
    ({"tier2_budget_share": 1.5}, 1.0, "tier2_budget_share"), ({"tier2_max_share": "0.1"}, 1.0, "tier2_max_share"),
    ({"tier2_budget_share": 0.1}, None, "budget.max_usd")])
def test_allowance_bad_options_refuse(options: dict[str, Any], budget: float | None, match: str) -> None:
    with pytest.raises(StageError, match=match):
        screen.tier2_allowance(_actx(PIN, None, options=options, budget=budget, run_id=f"bad-{match}-{budget}"))


def test_allowance_tightest_cap() -> None:
    a = screen.tier2_allowance(_actx(PIN, None, options={"tier2_max_usd": 0.5, "tier2_budget_share": 0.1},
                                     budget=3.0, run_id="tight"))
    assert a is not None and a.max_usd == pytest.approx(0.3) and a.max_share is None
    assert screen.tier2_allowance(_actx(PIN, None, run_id="none")) is None


def test_ask_returns_none_when_tier2_does_not_fit(tmp_path: Path) -> None:
    fake = FakeLlm()
    fake.on(model=LUNA, reply=v12("borderline", {"verdict": "borderline"}, {"verdict": "irrelevant"},
                                  {"doc_type": "paper"}))
    client = _client(tmp_path, fake)
    actx = _actx(PIN, client, options={"tier2_max_usd": 0.0001}, budget=1.0, run_id="deferred")
    assert screen.ask(_item("Body.", None), actx) is None
    assert [r.model for r in fake.requests] == [LUNA]  # tier 1 paid, tier 2 never sent
    a = screen.tier2_allowance(actx)
    assert a is not None and a.deferred == 1 and a.tier1 == pytest.approx(0.001)
    roomy = _actx(PIN, client, options={"tier2_max_usd": 1.0}, budget=1.0, run_id="roomy")
    fake.on(model=TERRA, reply=v12("relevant", {"verdict": "relevant"}, {"verdict": "irrelevant"},
                                   {"doc_type": "paper"}, cost=0.02))
    ans = screen.ask(_item("Body.", None), roomy)
    assert ans is not None and ans.followups and screen.tier2_allowance(roomy).spent == pytest.approx(0.02)  # type: ignore[union-attr]


#: The production screen pin (std-v3, batch-2026-10a): luna on both tiers, tier 2 on a [48000, 8000] excerpt
PROD_PIN = {"name": "screen-v12", "model": LUNA, "tier2": True, "adjudicate_model": LUNA}
#: A text whose tier-2 estimate at luna's price exceeds one document's $0.003 of room (from ~41k chars)
LONG = "Inductive coherence of logical uncertainty. " * 1400


def test_ask_admits_a_lone_long_borderline_after_a_free_tier1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Build 2b (D706): ``screen_usd_per_1k: 3.0``, tier 1 a $0 answer (a response-cache hit) saying
    borderline on a long text, the run's first answer: tier 2 is asked, not deferred."""
    fake = FakeLlm()
    fake.on(model=LUNA, reply=[
        v12("borderline", {"verdict": "borderline"}, {"verdict": "irrelevant"}, {"doc_type": "preprint"}, cost=0.0),
        v12("relevant", {"verdict": "relevant"}, {"verdict": "irrelevant"}, {"doc_type": "preprint"}, cost=0.003)])
    estimates: list[float] = []
    real = screen.tier2_estimate
    monkeypatch.setattr(screen, "tier2_estimate", lambda *a, **k: estimates.append(real(*a, **k)) or estimates[-1])
    actx = _actx(PROD_PIN, _client(tmp_path, fake), options={"screen_usd_per_1k": 3.0}, run_id="2b-lone")
    ans = screen.ask(_item(LONG, "preprint"), actx)
    assert len(estimates) == 1 and 0.003 < estimates[0] < 0.0035
    assert ans is not None and ans.followups and ans.counts["tier2"] == 1
    assert len(fake.requests) == 2
    a = screen.tier2_allowance(actx)
    assert a is not None and (a.docs, a.tier1, a.admitted, a.deferred) == (1, 0.0, 1, 0)


# ── (4) through the stage runner ─────────────────────────────────────────────────────────────

TEXTS = ["DOC_A. A paper on reward hacking.", "DOC_B. A thin abstract about embedded agency.",
         "DOC_C. Sourdough bread.", "DOC_D. Scalable oversight with debate."]


def _runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeLlm, texts: list[str],
            pin: dict[str, Any]) -> tuple[Path, Any]:
    """A scenario store holding ``texts`` and a plan maker ``plan(options)`` for the stage runner."""
    monkeypatch.delenv("STACK_WORKFLOWS_ROOT", raising=False)  # the real workflows/annotate/screen
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("STACK_PROGRESS", "0")
    monkeypatch.setattr(annotate_stage, "TRANSPORT_HOOK", lambda: fake)
    monkeypatch.setattr(annotate_stage, "SLEEP", lambda _s: None)
    monkeypatch.setattr(annotate_stage, "CLOCK", lambda: AT_RUN)
    monkeypatch.chdir(tmp_path)
    store = tmp_path / "store"
    write_documents(store, [doc_with_text(store, i, t) for i, t in enumerate(texts)], run=1)

    def plan(options: dict[str, Any]) -> Any:
        data = yaml.safe_load((REPO / "configs" / "plans" / "dev-v2.yaml").read_text("utf-8"))
        data["plan"] = f"v12-{len(options)}"
        data["prompts"]["annotate.screen"] = dict(pin)
        data["fold"]["build"]["annotations_folded"] = ["screen", "doc_class"]
        data["stages"] = [{"stage": "fold.corpus"},
                          {"stage": "annotate.screen", "prior": ["screen", "screen_evidence"],
                           "budget": {"max_usd": 1.0}, "options": options}]
        path = tmp_path / "configs" / "plans" / f"{data['plan']}.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(data, sort_keys=False), "utf-8")
        return load_plan(path)

    return store, plan


@pytest.mark.slow  # runner-driven scenario store
def test_deferred_borderline_is_retried_next_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeLlm()
    fake.on(model=TERRA, reply=v12("relevant", {"verdict": "relevant"}, {"verdict": "irrelevant"},
                                   {"doc_type": "preprint"}, cost=0.02))
    fake.on("DOC_A", v12("relevant", {"verdict": "relevant"}, {"verdict": "relevant"}, {"doc_type": "preprint"}),
            model=LUNA)
    fake.on("DOC_B", v12("borderline", {"verdict": "borderline"}, {"verdict": "irrelevant"}, {"doc_type": "preprint"}),
            model=LUNA)
    fake.on("DOC_C", v12("irrelevant", {"verdict": "irrelevant"}, {"verdict": "irrelevant"}, {"doc_type": "other"}),
            model=LUNA)
    fake.on("DOC_D", v12("relevant", {"verdict": "relevant"}, {"verdict": "relevant"}, {"doc_type": "paper"}),
            model=LUNA)
    store, plan = _runner(tmp_path, monkeypatch, fake, TEXTS, PIN)

    tight = plan({"tier2_max_usd": 0.001})  # below any terra estimate
    corpus = fold_corpus(store, tight)
    r1 = annotate(store, tight, "screen", corpus)
    s1 = {r["doc_id"]: r for r in rows_of(store, r1, "screen")}
    assert [(s1[sid(i)]["status"], s1[sid(i)]["error_class"]) for i in range(4)] == [
        ("ok", None), ("not_attempted", "budget"), ("ok", None), ("ok", None)]  # DOC_D after B still asked
    assert not r1.scope_complete and "not_attempted.budget" not in r1.counts  # the run's budget scope untouched
    assert r1.counts["not_attempted.deferred"] == 1 and incomplete_reasons(r1.counts) == ["tier2_allowance"]  # D706
    assert fake.count(model=LUNA) == 4 and fake.count(model=TERRA) == 0
    dc = {r["doc_id"]: r for r in rows_of(store, r1, "doc_class")}
    # synthetic rows carry metadata.doc_type 'preprint', which settles DOC_D's model answer 'paper'
    assert dc[sid(0)]["doc_type"] == "preprint" and dc[sid(3)]["doc_type"] == "preprint"
    assert dc[sid(1)]["status"] == "not_attempted"

    roomy = plan({"tier2_max_usd": 1.0, "tier2_budget_share": 0.5})
    r2 = annotate(store, roomy, "screen", corpus, prior=r1)
    s2 = {r["doc_id"]: r for r in rows_of(store, r2, "screen")}
    assert r2.scope_complete and s2[sid(1)]["verdict"] == "relevant" and s2[sid(1)]["adjudication_tier"] == 2
    assert fake.count(model=LUNA) == 4 and fake.count(model=TERRA) == 1  # tier 1 from the response cache
    assert r2.counts["carried"] == 3
    red = {r.check: r.items for r in run_checks(store) if r.status not in ("ok", "skipped")}
    assert red == {}, red


@pytest.mark.slow  # runner-driven scenario store
def test_build_2b_lone_long_borderline_admitted_next_deferred_and_reported(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Build 2b through the runner (D706), the production pin and ``screen_usd_per_1k: 3.0``, one
    worker: the first document a long-text borderline whose tier 1 is a $0 answer (as a response-cache
    hit) gets tier 2 although its estimate exceeds the run's $0.003 of room; the second, also long, does
    not fit the rate after it and is ``not_attempted[budget]``, counted ``not_attempted.deferred``, the
    build reason ``tier2_allowance`` (was ``unspecified``), the budget scope untouched."""
    fake = FakeLlm()
    for mark in ("DOC_L", "DOC_M"):
        fake.on(mark, [v12("borderline", {"verdict": "borderline"}, {"verdict": "irrelevant"},
                           {"doc_type": "preprint"}, cost=0.0),
                       v12("relevant", {"verdict": "relevant"}, {"verdict": "irrelevant"},
                           {"doc_type": "preprint"}, cost=0.003)], model=LUNA)
    store, plan = _runner(tmp_path, monkeypatch, fake, [f"DOC_L. {LONG}", f"DOC_M. {LONG}"], PROD_PIN)
    p = plan({"screen_usd_per_1k": 3.0, "workers": 1})
    r = annotate(store, p, "screen", fold_corpus(store, p))
    rows = {x["doc_id"]: x for x in rows_of(store, r, "screen")}
    assert [(rows[sid(i)]["status"], rows[sid(i)]["error_class"], rows[sid(i)]["adjudication_tier"])
            for i in range(2)] == [("ok", None, 2), ("not_attempted", "budget", None)]
    assert fake.count(model=LUNA) == 3 and r.counts["tier2"] == 1
    assert not r.scope_complete and "not_attempted.budget" not in r.counts
    assert r.counts["not_attempted.deferred"] == 1 and incomplete_reasons(r.counts) == ["tier2_allowance"]
