"""Source rules (track S, S5; ``stack/rules.py``) and the gate's ``rule:`` clause
(``stack/plan.py``, ``stack/gate.py``): one triage scenario on a synthetic world with all three
rule kinds and a fake asker (lab post without and with an abstract, the latter arriving through
``manual`` and matched by URL; a Zvi post; an excluded Zvi post that goes to the asker; non-matching
rows, one by an author whose name contains "zvi"), X14 green; a rule-list edit changing every gate
digest and re-scoping ``triage --only select`` with zero asker calls while X14 still recomputes the
earlier run under the gate it stored; refusals at plan load; SQL quoting; the lab URL prefixes'
provenance in the collectors' configuration.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pytest

from stack import gate, rules
from stack.plan import PlanError
from stack.schemas.families import family
from stack.stage_runner import StageContext
from stack.urls import url_key
from tests.synth.rows import document_row, sid
from tests.test_gather_identity import Store, src
from tests.test_triage_rules import ann, assert_green, checks, fid, plan_with, triage

Row = dict[str, Any]
QUESTION = {"question": "safety_relevance", "prompt": "triage.safety-relevance-v3",
            "fetch_on": ["relevant", "borderline"]}
GATE = {"screen": [{"rule": "frontier_lab_posts"}, {"rule": "safety_org_posts"}, {"rule": "zvi_posts"},
                   QUESTION]}


class IrrelevantAsker:
    """Answers ``irrelevant`` to every question it is asked; records which rows it saw."""

    name = "fake:irrelevant"
    origin = "ai_check"

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def ask(self, rows: list[Row], questions: list[gate.Question], ctx: StageContext) -> gate.AskResult:
        self.calls.append(sorted(r["doc_id"] for r in rows))
        res = gate.AskResult()
        for r in rows:
            for q in questions:
                res.answers[(r["doc_id"], q.name)] = {**gate.question_row(q, "ok"), "verdict": "irrelevant",
                                                       "confidence": 0.9}
        return res


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "store")


@pytest.fixture
def asker(monkeypatch: pytest.MonkeyPatch) -> IrrelevantAsker:
    fake = IrrelevantAsker()
    monkeypatch.setattr(gate, "ASKER_HOOK", lambda plan, line: fake)
    return fake


def url_row(i: int, channel: str, url: str, *, abstract: str | None) -> Row:
    row = src(i, [("url", url)], channel=channel)
    row["identity"]["canonical_url"] = url
    row["metadata"]["abstract"] = abstract
    return row


def forum_post(i: int, title: str, author: str) -> Row:
    row = src(i, [("fm_post", f"post{i}")], channel="lesswrong")
    row["identity"]["canonical_url"] = f"https://www.lesswrong.com/posts/post{i}"
    row["metadata"].update(title=title, doc_type="forum_post",
                           authors=[{"name": author, "orcid": None, "affiliation_raw": None}])
    return row


def world(store: Store) -> None:
    store.add("sources.blogs", [
        url_row(1, "openai_blog", "https://openai.com/index/post-1", abstract=None),
        url_row(2, "manual", "https://alignment.anthropic.com/2025/post-2", abstract="Has an abstract"),
        url_row(6, "manual", "https://openai.com.example.org/post-6", abstract="Lookalike host"),
    ])
    store.add("sources.forums", [
        forum_post(3, "AI #190: More Math", " Zvi "),
        forum_post(4, "Childhood and Education #22: Homework", "Zvi"),
        forum_post(5, "Zvi-style roundup of decoding", "Marjan Ghazvininejad"),
    ])
    store.gather()


@pytest.mark.slow
def test_rules_fetch_by_source_and_the_rest_goes_to_the_asker(store: Store, tmp_path: Path,
                                                                asker: IrrelevantAsker) -> None:
    plan, path = plan_with(tmp_path, "rules", GATE)
    world(store)
    rec = triage(store, plan)
    docs = store.docs()
    got = {d: (ann(r)["payload"]["decision"], ann(r)["origin"], ann(r)["payload"]["wanted_by"])
           for d, r in docs.items()}
    assert got == {sid(1): ("fetch", "rule", ["screen"]),       # lab channel, no abstract
                   sid(2): ("fetch", "rule", ["screen"]),       # lab URL via manual, with an abstract
                   sid(3): ("fetch", "rule", ["screen"]),       # Zvi
                   sid(4): ("skip", "ai_check", []),            # excluded Zvi post: asked
                   sid(5): ("skip", "ai_check", []),            # "zvi" inside another name: asked
                   sid(6): ("skip", "ai_check", [])}            # lookalike host: asked
    assert asker.calls == [[sid(i) for i in range(1, 7)]]      # D724: the rule-fetched rows are asked too
    for d in (sid(1), sid(2), sid(3)):
        assert [(q["status"], q["error_class"], q["verdict"]) for q in ann(docs[d])["payload"]["questions"]] == [
            ("skipped", "not_needed", "irrelevant")]  # outside the question scope: a signal, the rule decides
    assert rec.counts["arm1"] == 3 and rec.counts["arm2"] == 3
    head = fid(store)
    assert_green(store.root, {"plan": str(path), "files": {"documents": {"file_id": head}}})
    assert_green(store.root, {"plan": "no-such-plan", "gate_digest": plan.gate_digest,
                              "files": {"documents": {"file_id": head}}})


@pytest.mark.slow
def test_a_rule_list_edit_rescopes_triage_and_x14_keeps_the_stored_gate(
        store: Store, tmp_path: Path, asker: IrrelevantAsker, monkeypatch: pytest.MonkeyPatch) -> None:
    plan_a, _ = plan_with(tmp_path, "a", GATE)
    world(store)
    triage(store, plan_a)
    old_head = fid(store)
    line = plan_a.stage_line("triage")
    assert line is not None
    cfg_a = gate.triage_config_digest(line, plan_a.gate, asker)

    # The edit: Tomáš decides the Childhood series is not to be excluded after all.
    zvi = rules.rule("zvi_posts", "triage")
    edited = tuple(dataclasses.replace(r, exclude=None) if r is zvi else r for r in rules.TRIAGE_RULES)
    monkeypatch.setitem(rules.RULES, "triage", edited)
    plan_b, path_b = plan_with(tmp_path, "b", GATE)
    assert plan_b.gate_digest != plan_a.gate_digest and plan_b.plan_digest != plan_a.plan_digest
    assert gate.triage_config_digest(line, plan_b.gate, asker) != cfg_a

    rec = triage(store, plan_b, only=["select"])
    assert rec.counts["scoped"] == 6 and len(asker.calls) == 1   # every row re-scoped, no ask
    assert ann(store.docs()[sid(4)])["payload"]["decision"] == "fetch"
    new_head = fid(store)
    assert_green(store.root, {"plan": str(path_b), "files": {"documents": {"file_id": new_head}}})
    # The earlier run recomputes under the gate it stored (its rule_sql), not today's rules.
    assert_green(store.root, {"plan": "no-such-plan", "gate_digest": plan_a.gate_digest,
                              "files": {"documents": {"file_id": old_head}}})
    x14 = checks(store.root, {"plan": "no-such-plan", "gate_digest": plan_b.gate_digest,
                              "files": {"documents": {"file_id": old_head}}})["gate_union"]
    assert x14[0] == "failed" and any(sid(4) in i for i in x14[1]), x14
    # S25-54: X14 recomputes BATCH_ROWS rows at a time, with the findings of one whole-view pass
    from stack.checks import rows as rows_mod

    sizes: list[int] = []
    real = gate.recompute
    monkeypatch.setattr(rows_mod, "BATCH_ROWS", 2)
    monkeypatch.setattr(gate, "recompute", lambda rows, *a, **k: sizes.append(len(rows)) or real(rows, *a, **k))
    again = checks(store.root, {"plan": "no-such-plan", "gate_digest": plan_b.gate_digest,
                                "files": {"documents": {"file_id": old_head}}})["gate_union"]
    assert again == x14 and len(sizes) > 1 and max(sizes) <= 2, (again, sizes)


def test_plan_load_refuses_unknown_and_foreign_rules_and_stale_sql(tmp_path: Path,
                                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(PlanError, match="unknown source rule 'no_such_rule'"):
        plan_with(tmp_path, "unknown", {"screen": [{"rule": "no_such_rule"}]})
    other = rules.SourceRule("acquire_only", "acquire", any_of=(rules.Match(channels=("arxiv",)),),
                             exclude=None, why="test")
    monkeypatch.setitem(rules.RULES, "acquire", (other,))
    with pytest.raises(PlanError, match="'acquire_only' is a acquire rule, not a triage rule"):
        plan_with(tmp_path, "foreign", {"screen": [{"rule": "acquire_only"}]})
    with pytest.raises(PlanError, match="differs from the current compile"):
        plan_with(tmp_path, "stale", {"screen": [{"rule": "zvi_posts", "rule_sql": "true"}]})
    with pytest.raises(PlanError, match="takes no `prompt`/`fetch_on`"):
        plan_with(tmp_path, "prompt", {"screen": [{"rule": "zvi_posts", "fetch_on": ["relevant"]}]})
    with pytest.raises(PlanError, match="exactly one of"):
        plan_with(tmp_path, "two", {"screen": [{"rule": "zvi_posts", "sql": "true"}]})
    plan, _ = plan_with(tmp_path, "ok", {"screen": [{"rule": "zvi_posts"}]})
    shape = plan.gate.file_shape()["screen"][0]
    assert shape == {"rule": "zvi_posts", "rule_sql": rules.rule("zvi_posts", "triage").sql()}


def test_sql_literals_are_quoted() -> None:
    nasty = "Zvi's \"odd\" -- x') OR true OR ('"
    rule = rules.SourceRule("quoted", "triage", any_of=(rules.Match(title_keywords=(nasty,)),),
                            exclude=rules.Match(authors=("O'Brien",)), why="test")
    rows = []
    for i, (title, author) in enumerate([(f"A {nasty.upper()} post", "Someone"), ("Plain", "Someone"),
                                         (f"{nasty}", "o'brien "), (None, "Someone")], start=1):
        r = document_row(i)
        r["metadata"].update(title=title, authors=[{"name": author, "orcid": None, "affiliation_raw": None}])
        rows.append(r)
    con = duckdb.connect()
    con.register("d", pa.Table.from_pylist(rows, schema=family("documents").schema))
    hit = [d for (d,) in con.execute(f"SELECT doc_id FROM d WHERE {rule.sql()} ORDER BY doc_id").fetchall()]
    assert hit == [rows[0]["doc_id"]]
    with pytest.raises(rules.RuleError, match="NUL"):
        rules.sql_literal("a\x00b")
    with pytest.raises(rules.RuleError, match="at least one criterion"):
        rules.Match()


def test_lab_url_prefixes_come_from_the_collectors_and_rule_channels_exist() -> None:
    from workflows.collect.blogs.main import SITE_SPECS
    from workflows.collect.cards.main import WATCH_SPECS

    configured: dict[str, list[str]] = {}
    for ch, spec in SITE_SPECS.items():
        configured[ch] = [u for s in spec["surfaces"] for u in s.urls]
    for ch, watch in WATCH_SPECS.items():
        urls = [u for s in watch.surfaces for u in s.urls] + [p["url"] for s in watch.surfaces for p in s.pins]
        urls += [f"https://raw.githubusercontent.com/{g['owner']}/" for s in watch.surfaces for g in s.github]
        urls += [f"https://raw.githubusercontent.com/{s.github_org}/" for s in watch.surfaces if s.github_org]
        configured[ch] = urls
    lab = {ch: [url_key(u) + "/" for u in configured[ch]] for ch in rules.LAB_CHANNELS}
    flat = [u for us in lab.values() for u in us]
    assert all(any(u.startswith(p) for u in flat) for p in rules.LAB_URL_PREFIXES)   # taken from config
    assert all(any(u.startswith(p) for p in rules.LAB_URL_PREFIXES) for u in flat)   # and all of it
    assert set(rules.SAFETY_ORG_CHANNELS) == set(SITE_SPECS) - set(rules.LAB_CHANNELS)
    known = set(gate.channel_groups())
    for r in (r for rs in rules.RULES.values() for r in rs):
        for m in (*r.any_of, *([r.exclude] if r.exclude else [])):
            assert set(m.channels) <= known, (r.name, set(m.channels) - known)


def test_safety_stems_are_one_list_for_the_proxy_and_the_collectors() -> None:
    """B7a: the stems live in stack.rules; the eval proxy reads the same object, so a collector
    filter (C6, C9) and the yield measurements cannot drift apart."""
    from stack.evals import freeze

    assert freeze.SAFETY_STEMS is rules.SAFETY_STEMS
    assert rules.has_safety_stem("Red-Teaming a reward HACKING model")
    assert not rules.has_safety_stem("A weather model") and not rules.has_safety_stem(None)
    assert re.search(rules.safety_stem_regex(), "Oversight", re.IGNORECASE)


@pytest.mark.parametrize("text, expect", [
    ("面向大语言模型的越狱攻击与防御综述", True),
    ("大模型价值对齐研究进展", True),
    ("人工智能安全治理框架", True),
    ("LLM安全评测基准", True),            # an English stem next to Chinese characters (re.ASCII)
    ("jailbreak攻击", True),
    ("机制可解释性综述", True),
    ("食品安全检测方法", False),          # 安全 alone is "security"/"safety" of anything
    ("序列对齐算法", False),              # 对齐 alone is sequence alignment
    ("监督学习在图像分类中的应用", False),  # 监督 alone is supervised learning
])
def test_safety_stems_match_chinese(text: str, expect: bool) -> None:
    assert rules.has_safety_stem(text) is expect
