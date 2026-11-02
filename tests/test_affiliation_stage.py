"""``annotate.affiliation`` on the annotate framework (PLAN T4.4): ``workflows/annotate/affiliation``
run through the real stage runner on a tiny ``documents`` chain → ``fold.corpus`` →
``annotate.affiliation`` with the fake model (``tests/fakes/llm.py`` via
``stack.annotate.stage.TRANSPORT_HOOK``; the key is unset, the ledger a tmp file), then the store
checks V7, V8, X6, X7, X8. The extraction itself is ``tests/test_affiliation.py`` (pure).

Cases: one item per roster author with ``item_key`` = the byline index, the roster taken from
``metadata.authors`` and the OpenAlex tier from ``affiliation_raw``; the XML tier answers without
a call; an empty answer and a per-document failure; a rerun with the first run's outputs as its
prior (``--in``) makes no call and writes
the same rows; a budget stop leaves ``not_attempted[budget]`` (the XML document is still
answered); the dev-v2 and std-v2 pins load and an unknown key refuses.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import stack.annotate.stage as annotate_stage
from stack.checks.verify import run_checks
from stack.config import load
from stack.llm import LlmRequest
from stack.plan import Plan, load_plan
from stack.stage_runner import StageError
from stack.store.blobs import open_blobs
from tests.fakes.annotate import (
    AT_RUN,
    annotate,
    doc_with_text,
    fold_corpus,
    plan,
    rows_of,
    write_documents,
)
from tests.fakes.llm import FakeLlm, ok, schema_invalid
from tests.synth.rows import sid, with_digests
from workflows.annotate.affiliation import main as affiliation

CONFIG = load()
REPO = Path(__file__).resolve().parents[1]
LUNA = "openai/gpt-6-luna"  # the model dev-v2's affiliation pin resolves to (extract_main)
ANNOTATE_CHECKS = ("V-wf", "V-evidence", "policy_pins", "basis_currency", "selection_recompute")

JATS = b"""<?xml version="1.0"?>
<article><front><article-meta>
  <contrib-group>
    <contrib contrib-type="author">
      <contrib-id contrib-id-type="orcid">https://orcid.org/0000-0002-1825-0097</contrib-id>
      <name><surname>Doe</surname><given-names>Jane</given-names></name>
      <xref ref-type="aff" rid="aff1"><sup>1</sup></xref>
    </contrib>
    <contrib contrib-type="author">
      <name><surname>Roe</surname><given-names>Bob</given-names></name>
      <xref ref-type="aff" rid="aff2"><sup>2</sup></xref>
    </contrib>
  </contrib-group>
  <aff id="aff1"><label>1</label>Anthropic, San Francisco, CA</aff>
  <aff id="aff2"><label>2</label>Google DeepMind, London</aff>
</article-meta></front></article>"""

#: doc 0: the XML tier (its converted text has lost the byline linkage).
XML_TEXT = "# Paper 0\n\nJane Doe, Bob Roe\nAnthropic; Google DeepMind\n\nMARK_XML abstract."
#: doc 1: four roster authors; the model reads three, one surface it returns is not in the text.
BYLINE_TEXT = ("# Paper 1\n\nAlice Smith¹, Bob Jones², Carol White, Dan Brown\n"
               "¹Anthropic, San Francisco\n²Google DeepMind, London\n\nAbstract. MARK_ONE reward hacking.")
ROSTER_1 = [("Alice Smith", None), ("Bob Jones", None), ("Carol White", "Anthropic; University of Nowhere"),
            ("Dan Brown", None)]
REPLY_1 = {"authors": [
    {"author_surface": "Alice Smith", "affiliations": [{"surface": "Anthropic, San Francisco", "marker": "1"}]},
    {"author_surface": "Bob Jones", "affiliations": [{"surface": "Google DeepMind, London", "marker": "2"}]},
    {"author_surface": "Carol White", "affiliations": []},
    {"author_surface": "Dan Brown", "affiliations": [{"surface": "Imaginary Institute of Elsewhere"}]},
]}


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    fake = FakeLlm()
    monkeypatch.delenv("STACK_WORKFLOWS_ROOT", raising=False)  # the real workflows/annotate/affiliation
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("STACK_PROGRESS", "0")
    monkeypatch.setattr(annotate_stage, "TRANSPORT_HOOK", lambda: fake)
    monkeypatch.setattr(annotate_stage, "SLEEP", lambda _s: None)
    monkeypatch.setattr(annotate_stage, "CLOCK", lambda: AT_RUN)
    fake.on("MARK_ONE", ok(REPLY_1, cost=0.002), model=LUNA)
    fake.on("MARK_TWO", ok({"authors": []}))
    fake.on("MARK_FAIL", schema_invalid())
    return {"store": tmp_path / "store", "fake": fake}


def paper(store: Path, i: int, text: str, authors: list[tuple[str, str | None]], *,
          xml: bytes | None = None) -> dict[str, Any]:
    """Document ``i`` with current text ``text``, ``metadata.authors`` = ``authors`` (name,
    affiliation_raw) and, with ``xml``, an ``ok`` ``xml`` role whose raw snapshot is ``xml``."""
    row = doc_with_text(store, i, text)
    row["metadata"]["authors"] = [{"name": n, "orcid": None, "affiliation_raw": a} for n, a in authors]
    if xml is not None:
        content = row["content"]
        snap = dict(content["snapshots"][0])
        snap.update(snapshot_id=snap["snapshot_id"].rsplit(".", 1)[0] + ".1", content_role="xml",
                    raw={"sha256": open_blobs(store).put(xml), "bytes": len(xml), "media_type": "application/xml"},
                    text=None, text_status=None, convert_attempts=0, convert_pin=None)
        role = dict(content["roles"][0])
        role.update(content_role="xml", snapshot_id=snap["snapshot_id"], lane="oa")
        content["snapshots"] = [*content["snapshots"], snap]
        content["roles"] = [*content["roles"], role]
    return with_digests(row)


def aplan(**line: Any) -> Plan:
    """``dev-v2`` (its real ``annotate.affiliation`` pin) with a bare affiliation line + ``line``."""
    return plan(affiliation=line or None)


def by_key(rows: list[dict[str, Any]], i: int) -> dict[str, dict[str, Any]]:
    return {r["item_key"]: r for r in rows if r["doc_id"] == sid(i)}


def user_text(request: LlmRequest) -> str:
    return str(request.payload["messages"][1]["content"])


def all_green(store: Path) -> None:
    results = run_checks(store)
    red = {r.check: r.items for r in results if r.status not in ("ok", "skipped")}
    assert red == {}, red
    by = {r.check: r for r in results}
    assert {n: by[n].status for n in ANNOTATE_CHECKS} == dict.fromkeys(ANNOTATE_CHECKS, "ok")


@pytest.mark.slow  # runner-driven store (D90; `docs/SETUP.md` §7)
def test_items_per_author_xml_tier_and_prior_rerun(env: dict[str, Any]) -> None:
    store, fake = env["store"], env["fake"]
    write_documents(store, [
        paper(store, 0, XML_TEXT, [("Doe, Jane", None), ("Bob Roe", None)], xml=JATS),
        paper(store, 1, BYLINE_TEXT, ROSTER_1),
        paper(store, 2, "# Paper 2\n\nMARK_TWO a post with no byline.", []),
        paper(store, 3, "# Paper 3\n\nAnn Other\nMARK_FAIL", [("Ann Other", None)]),
    ], run=1)
    p = aplan()
    corpus = fold_corpus(store, p)
    r1 = annotate(store, p, "affiliation", corpus)
    rows = rows_of(store, r1, "affiliation")
    ev = {e["doc_id"]: e for e in rows_of(store, r1, "affiliation_evidence")}

    # One item per roster author, item_key = byline index, roster from metadata.authors.
    d1 = by_key(rows, 1)
    assert sorted(d1) == ["0", "1", "2", "3"]
    assert [(d1[k]["byline_index"], d1[k]["author_surface"], d1[k]["author_name"], d1[k]["resolution"])
            for k in "0123"] == [(0, "Alice Smith", "alice smith", "text"), (1, "Bob Jones", "bob jones", "text"),
                                 (2, "Carol White", "carol white", "openalex"), (3, "Dan Brown", "dan brown", "unknown")]
    assert d1["0"]["affiliations"] == [{"surface": "Anthropic, San Francisco", "org_slugs": ["anthropic"]}]
    assert d1["1"]["affiliations"] == [{"surface": "Google DeepMind, London", "org_slugs": ["deepmind"]}]
    assert [a["surface"] for a in d1["2"]["affiliations"]] == ["Anthropic", "University of Nowhere"]
    assert d1["3"]["affiliations"] == []  # the surface the text never prints is voided
    assert {r["status"] for r in d1.values()} == {"ok"}
    sent = [user_text(q) for q in fake.requests if "MARK_ONE" in user_text(q)]
    assert len(sent) == 1 and "```authors\nAlice Smith\nBob Jones\nCarol White\nDan Brown\n```" in sent[0]
    assert (ev[sid(1)]["origin"], ev[sid(1)]["model"], ev[sid(1)]["prompt_name"]) == ("ai_check", LUNA,
                                                                                         "affiliation-v1")

    # The XML tier: the markup's linkage, aligned to the (inverted) roster, and no call.
    d0 = by_key(rows, 0)
    assert [(d0[k]["author_surface"], d0[k]["resolution"], [a["org_slugs"] for a in d0[k]["affiliations"]])
            for k in ("0", "1")] == [("Doe, Jane", "xml", [["anthropic"]]), ("Bob Roe", "xml", [["deepmind"]])]
    assert "0000-0002-1825-0097" in (d0["0"]["orcid"] or "")
    assert fake.count(contains="MARK_XML") == 0
    assert (ev[sid(0)]["origin"], ev[sid(0)]["model"], ev[sid(0)]["template_digest"]) == ("computed", None, None)

    # No author found → empty; a schema-invalid reply → that document's failure row, run goes on.
    assert [(r["item_key"], r["status"]) for r in by_key(rows, 2).values()] == [("", "empty")]
    d3 = by_key(rows, 3)
    assert list(d3) == [""] and d3[""]["status"] == "failed_permanent" == ev[sid(3)]["status"]
    assert r1.status == "ok" and r1.counts["xml_tier"] == 1 and r1.counts["surfaces_voided"] == 1
    all_green(store)

    # the first run's outputs as prior (--in): every work key carried, no call, the same rows.
    calls = fake.count()
    r2 = annotate(store, p, "affiliation", corpus, prior=r1)
    assert fake.count() == calls and r2.counts["carried"] == 4
    assert rows_of(store, r2, "affiliation") == rows
    all_green(store)


@pytest.mark.slow
def test_budget_stop_leaves_not_attempted_and_xml_needs_no_budget(env: dict[str, Any]) -> None:
    store, fake = env["store"], env["fake"]
    write_documents(store, [paper(store, 0, XML_TEXT, [("Jane Doe", None), ("Bob Roe", None)], xml=JATS),
                            paper(store, 1, BYLINE_TEXT, ROSTER_1)], run=1)
    tight = aplan(budget={"max_usd": 0.0001})
    corpus = fold_corpus(store, tight)
    r1 = annotate(store, tight, "affiliation", corpus)
    rows = rows_of(store, r1, "affiliation")
    assert {r["status"] for r in by_key(rows, 0).values()} == {"ok"}
    assert [(r["item_key"], r["status"], r["error_class"], r["evidence_id"]) for r in by_key(rows, 1).values()] == [
        ("", "not_attempted", "budget", None)]
    assert fake.count() == 0 and not r1.scope_complete
    all_green(store)
    r2 = annotate(store, aplan(), "affiliation", corpus, prior=r1)
    assert fake.count() == 1 and sorted(by_key(rows_of(store, r2, "affiliation"), 1)) == ["0", "1", "2", "3"]
    assert r2.scope_complete
    all_green(store)


@pytest.mark.parametrize("plan_name", ["dev-v2", "std-v2"])
def test_plan_pins_load(plan_name: str) -> None:
    pin = load_plan(REPO / "configs" / "plans" / f"{plan_name}.yaml").prompts["annotate.affiliation"]
    pp = affiliation.prompt(pin, CONFIG.llm)
    role = CONFIG.llm.roles[pin.get("role", affiliation.DEFAULT_ROLE)]
    assert pp.name == pin["name"] and pp.models[0] == pin.get("model", role.model)
    assert len(pp.template_digests) == len(pp.models) == 1 + len(role.fallbacks)


@pytest.mark.parametrize("bad", [{"name": "affiliation-v1", "modle": LUNA}, {"role": "no_such_role"}])
def test_bad_pin_refuses(bad: dict[str, Any]) -> None:
    with pytest.raises(StageError):
        affiliation.prompt(bad, CONFIG.llm)
