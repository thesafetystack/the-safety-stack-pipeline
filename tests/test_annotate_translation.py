"""``annotate.translation`` (D755): English machine translations of a non-English document's title and abstract.

The pure parts (the answer → payload rule, the user message, the pin, the plan's scope filter over duckdb rows) and
the stage on synthetic documents with the fake model: one call per document in scope on its metadata, the originals
untouched, ``empty`` for an English answer, a rerun carries, an edited title re-asks.
"""

from __future__ import annotations

import importlib.util
import pathlib
from typing import Any

import pyarrow as pa
import pytest

import stack.annotate.stage as annotate_stage
from stack import duck
from stack.config import load
from stack.schemas.payloads.registry import REGISTRY
from stack.stage_runner import StageError, run_stage
from tests.fakes.annotate import AT_RUN, doc_with_text, fold_corpus, rows_of, write_documents, write_plan_file
from tests.fakes.llm import FakeLlm, ok
from tests.synth.records import SERIES, run_line
from tests.synth.rows import with_digests

REPO = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("_translation_under_test",
                                               REPO / "workflows" / "annotate" / "translation" / "main.py")
assert _spec is not None and _spec.loader is not None
tr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tr)
LUNA = "openai/gpt-6-luna"


def test_payload_rule_and_the_stored_type() -> None:
    p, st = tr.payload_of({"from_lang": "zh", "title_en": " AI Safety Governance Framework ", "abstract_en": None}, False)
    assert st == "ok" and p == {"from_lang": "zh", "title_en": "AI Safety Governance Framework", "abstract_en": None,
                                "method": "llm"}
    assert tr.payload_of({"from_lang": "en", "title_en": "x", "abstract_en": "y"}, True) == (
        {"from_lang": "en", "title_en": None, "abstract_en": None, "method": None}, "empty")
    # an abstract the document lacks is never stored, whatever the model wrote
    assert tr.payload_of({"from_lang": "ru", "title_en": "T", "abstract_en": "invented"}, False)[0]["abstract_en"] is None
    pa.array([p], type=REGISTRY["translation"].payload_schema)
    assert REGISTRY["translation"].basis_kind == "fields"
    assert REGISTRY["translation"].text_bearing == ("title_en", "abstract_en")


def test_user_message_and_pin() -> None:
    msg = tr.user_message({"title": "人工智能安全", "abstract": "摘" * 7000})
    assert msg.startswith("title:\n人工智能安全\n\nabstract:\n") and msg.count("摘") == tr.MAX_ABSTRACT_CHARS
    assert tr.user_message({"title": "T", "abstract": None}).endswith("abstract: (none)")
    llm = load().llm
    pin = tr.prompt({}, llm)
    assert pin.name == "translate-short-v1" and pin.models == (LUNA,)
    terra = tr.prompt({"model": "openai/gpt-5.6-terra"}, llm)
    assert terra.models == ("openai/gpt-5.6-terra",) and terra.template_digests != pin.template_digests
    with pytest.raises(StageError, match="plan pin keys"):
        tr.prompt({"temperature": 0}, llm)
    with pytest.raises(StageError, match="not an llm.yaml role"):
        tr.prompt({"role": "nope"}, llm)


def _scope_rows(filter_sql: str) -> list[str]:
    rows = [
        ("zh", "人工智能安全治理框架", None, "relevant", "zh"),
        ("ru", "Безопасность ИИ", "Аннотация", "relevant", None),
        ("de", "Sicherheit von Sprachmodellen", "Wir untersuchen", "relevant", "de"),
        ("en", "Jailbreaking LLMs", "We study", "relevant", "en"),
        ("greek", "The μ-parametrisation", None, "relevant", "en"),   # Greek is maths, not a language signal
        ("zh-irrelevant", "食品安全", None, "irrelevant", "zh"),
    ]
    tbl = pa.Table.from_pylist([{
        "doc_id": d, "metadata": {"title": t, "abstract": a},
        "annotations": {"screen": {"status": "ok", "payload": {"verdict": v}},
                        "doc_class": {"payload": {"primary_lang": lang}}}} for d, t, a, v, lang in rows])
    con = duck.connect()
    con.register("corpus", tbl)
    return [r[0] for r in con.sql(f"select doc_id from corpus where {filter_sql} order by doc_id").fetchall()]


def test_scope_filter_selects_screen_relevant_non_english_rows() -> None:
    assert _scope_rows(tr.SCOPE_FILTER) == ["de", "ru", "zh"]


def test_the_batch_plan_line_uses_the_module_scope() -> None:
    from stack.plan import load_plan

    plan = load_plan(REPO / "configs" / "plans" / "batch-2026-10a.yaml")
    line = next(s for s in plan.stages if s.stage == "annotate.translation")
    assert line.filter == tr.SCOPE_FILTER and set(line.reads) == {"screen", "doc_class"}
    assert plan.prompts["annotate.translation"]["name"] == "translate-short-v1"
    assert "translation" in plan.fold.build.annotations_folded


# ---------------------------------------------------------------- the stage, fake model
TITLES = [("人工智能安全治理框架", "本框架提出人工智能安全治理的原则。"),
          ("Безопасность больших языковых моделей", None),
          ("An English title", "An English abstract.")]
REPLIES = {"人工智能安全治理框架": {"from_lang": "zh", "title_en": "AI Safety Governance Framework",
                                     "abstract_en": "This framework sets out principles of AI safety governance."},
           "Безопасность больших": {"from_lang": "ru", "title_en": "Safety of large language models",
                                    "abstract_en": None},
           "An English title": {"from_lang": "en", "title_en": None, "abstract_en": None}}


@pytest.fixture
def world(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    fake = FakeLlm()
    for marker, reply in REPLIES.items():
        fake.on(marker, ok(reply), model=LUNA)
    monkeypatch.delenv("STACK_WORKFLOWS_ROOT", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("STACK_PROGRESS", "0")
    monkeypatch.setattr(annotate_stage, "TRANSPORT_HOOK", lambda: fake)
    monkeypatch.setattr(annotate_stage, "SLEEP", lambda _s: None)
    monkeypatch.setattr(annotate_stage, "CLOCK", lambda: AT_RUN)
    monkeypatch.chdir(tmp_path)
    store = tmp_path / "store"
    docs = []
    for i, (title, abstract) in enumerate(TITLES):
        d = doc_with_text(store, i, f"body {i}")
        d["metadata"]["title"], d["metadata"]["abstract"] = title, abstract
        docs.append(with_digests(d))
    write_documents(store, docs, run=1)
    plan = write_plan_file(
        tmp_path, "translation-test", prompts={"annotate.translation": {"name": "translate-short-v1"}},
        stages=[{"stage": "fold.corpus"},
                {"stage": "annotate.translation", "prior": ["translation", "translation_evidence"],
                 "options": {"workers": 1}},
                {"stage": "fold.build"}],
        folded=["translation"])
    return {"store": store, "fake": fake, "plan": plan, "ids": [d["doc_id"] for d in docs], "docs": docs}


@pytest.mark.slow
def test_stage_translates_title_and_abstract_beside_the_originals(world: dict[str, Any]) -> None:
    store, fake, plan, ids = world["store"], world["fake"], world["plan"], world["ids"]
    corpus = fold_corpus(store, plan)
    rec = run_stage(store, plan, "annotate.translation", [corpus], series=SERIES)
    assert rec.status == "ok", rec.error
    assert fake.count() == 3
    rows = {r["doc_id"]: r for r in rows_of(store, rec, "translation")}
    zh, ru, en = (rows[i] for i in ids)
    assert (zh["status"], zh["origin"], zh["basis_kind"]) == ("ok", "ai_check", "fields")
    assert (zh["from_lang"], zh["title_en"], zh["method"]) == ("zh", "AI Safety Governance Framework", "llm")
    assert zh["abstract_en"].startswith("This framework")
    assert (ru["status"], ru["title_en"], ru["abstract_en"]) == ("ok", "Safety of large language models", None)
    assert en["status"] == "empty" and en["title_en"] is None
    assert zh["basis_digest"] == world["docs"][0]["metadata"]["fields_digest"]
    ev = {e["doc_id"]: e for e in rows_of(store, rec, "translation_evidence")}
    assert all(ev[i]["model"] == LUNA and ev[i]["prompt_name"] == "translate-short-v1" for i in ids)
    assert rec.counts["translated.ok"] == 2 and rec.counts["translated.empty"] == 1


@pytest.mark.slow
def test_a_rerun_carries_and_an_edited_title_reasks(world: dict[str, Any]) -> None:
    store, fake, plan, docs = world["store"], world["fake"], world["plan"], world["docs"]
    built = {"corpus_input": {"path": fold_corpus(store, plan)}}
    rec = run_line(store, plan, "annotate.translation", built=built)
    assert rec is not None and rec.status == "ok", rec.error
    again = run_line(store, plan, "annotate.translation", built=built)
    assert again is not None and again.counts["carried"] == 3 and not again.counts.get("asked")
    edited = dict(docs[1])
    edited["metadata"] = {**docs[1]["metadata"], "title": "Безопасность больших языковых моделей (2)"}
    write_documents(store, [docs[0], with_digests(edited), docs[2]], run=3)
    built2 = {"corpus_input": {"path": fold_corpus(store, plan)}}
    third = run_line(store, plan, "annotate.translation", built=built2)
    assert third is not None and third.status == "ok", third.error
    assert third.counts["asked"] == 1 and third.counts["carried"] == 2 and fake.count() == 4
