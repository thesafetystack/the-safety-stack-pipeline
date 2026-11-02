"""``safety-relevance-v4`` and its title variant ``safety-relevance-title-v2`` (D656), the record version 2 (``Published:`` / ``Source:`` lines), and the off-by-default ``-p4`` variants (D657).

(1) older names keep their bytes: template digests at fixed params and the records they are sent
(:func:`document_text`, :func:`title_text`) for a row that has a date and channels, recorded at 5ac481f;
(2) record version 2: the lines after ``Authors``, the year of ``metadata.published_at``, the channels of
``identity.channels``; an undated row or a row with no channels leaves its line out (never ``None``);
(3) registration: every template file has a ``SPECS`` row and every row a file; every row is sent what the
replaced tables (``RECORD_VERSIONS``, ``TITLE_VARIANTS``, as at ba4fc01) sent; an unlisted
name refuses before any call; the title variants of v4 and v4-p4; ``stack eval triage --prompt`` resolves
every new name;
(4) the texts: v4 = v3 plus the narrow-settings clause and the record note; title-v2 = title-v1 plus the
same; each ``-p4`` = its base plus the technique-provenance clause and nothing else.
"""

from __future__ import annotations

import datetime as dt
import difflib
import hashlib
from pathlib import Path

import pytest

from stack import gate
from stack.config import load
from stack.core.hashes import template_hash
from workflows.triage import ask as triage_ask

CONFIG = load()
LUNA = "openai/gpt-6-luna"
FIXED = {"model": "x/y", "max_tokens": 1000, "temperature": 0}
HERE = triage_ask.HERE
#: template_hash(<name>.md, SCHEMA, FIXED) at 5ac481f, before record version 2 existed.
DIGEST_BEFORE = {
    "safety-relevance-v1": "c38022dfe9a4b2b66837ef80bbbe9bb38ec514f5d3f6221d2f8509a85fe1a59f",
    "safety-relevance-v2": "7e9c3a871515d1bba33ccb3ff9f11a9551fdf31d905e3bfdf36831aea64a4470",
    "safety-relevance-v3": "e14c269b63e96d44b1aac7c63367cfc34d11b4e6a290624a8def17858b08f9f1",
    "safety-relevance-title-v1": "f0822b563da4cc7b3e14e5cb3a14c56b97cb7fb69e817e076b7b249bd9a10b5a"}
ROW = {"doc_id": "d1", "metadata": {"title": "Safe interruptibility", "abstract": "We study agents.",
                                    "authors": [{"name": "A", "affiliation_raw": "Lab X"},
                                                {"name": "B", "affiliation_raw": None}],
                                    "published_at": dt.datetime(2016, 6, 1, tzinfo=dt.UTC), "doc_type": "preprint"},
       "identity": {"channels": ["alignmentforum", "lesswrong"]}}
#: sha256 of document_text(ROW, "An excerpt.") and title_text(ROW, {"primary_topic": "AI Safety"}, ["umbrella"])
#: at 5ac481f: the date and the channels never reached a version-1 record.
RECORD_BEFORE = {"document_text": "a6e23616722afd028b979a79bb9565c2b55e8d8a5988f184f216cbb5d94784a5",
                 "title_text": "823768a5bf9a923be8aab9e3db02ee9287e18b54fd333226ccdf3f23e4caea3c"}
NEW = ("safety-relevance-v4", "safety-relevance-title-v2", "safety-relevance-v4-p4", "safety-relevance-title-v2-p4")
NARROW = """# Narrow settings (era-free)

A narrow experimental setting does not by itself exclude a general safety method or an AI-alignment argument: a toy
task, a tabular or robotic agent, a narrow benchmark or a pre-2020 deep network is not grounds for exclusion when the
contribution is safe exploration, an explicit safety constraint, interruptibility or corrigibility, reward
misspecification or wireheading, low-impact agents, scalable oversight, runtime shielding, formal safety verification
of an AI policy, or robustness to adversarial inputs and distributional shift framed as a safety problem. Judge the
contribution and its intended applicability, under the vocabulary of its time (Friendly AI, value alignment, value
learning, safe interruptibility …). Ordinary task-performance improvements and domain applications stay out.
"""
PROVENANCE = """# Technique provenance

Reusable methods, decisive negative results and evaluation protocols that establish the provenance of a catalogued
safety technique — private training, privacy and memorisation auditing, poisoning and backdoor defences,
model-extraction defences, unlearning, training-data attribution, adversarial training and certified robustness,
interpretability methods (saliency, attribution, probing, concept methods, representation analysis), model
documentation — count for the catalogue (route A) even when demonstrated on a task-specific model without an
advanced-AI framing; fetch them as `relevant` or `borderline`.
"""

def body(name: str) -> str:
    """The template after its header comment (the first line)."""
    return (HERE / f"{name}.md").read_text("utf-8").split("\n", 1)[1]


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# ── (1) older names ──────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", sorted(DIGEST_BEFORE))
def test_older_templates_keep_their_digests(name: str) -> None:
    assert template_hash((HERE / f"{name}.md").read_text("utf-8"), triage_ask.SCHEMA, FIXED) == DIGEST_BEFORE[name]
    assert triage_ask.SPECS[name].record_version == 1


def test_version_1_records_are_unchanged() -> None:
    assert sha(triage_ask.document_text(ROW, "An excerpt.")) == RECORD_BEFORE["document_text"]
    assert sha(triage_ask.title_text(ROW, {"primary_topic": "AI Safety"}, ["umbrella"])) == RECORD_BEFORE["title_text"]
    for name in DIGEST_BEFORE:  # the dispatcher sends version 1 for every older name
        assert triage_ask.record_text(name, ROW, "An excerpt.") == triage_ask.document_text(ROW, "An excerpt.")


# ── (2) record version 2 ─────────────────────────────────────────────────────────────────────


def test_record_v2_lines() -> None:
    text = triage_ask.document_text_v2(ROW, "An excerpt.")
    assert text.split("\n\n") == ["Title: Safe interruptibility", "Authors: A, B", "Published: 2016",
                                  "Source: alignmentforum, lesswrong", "Abstract:\nWe study agents.",
                                  "Excerpt (start of the text):\nAn excerpt."]
    title = triage_ask.title_text_v2(ROW, {"primary_topic": "AI Safety"}, ["umbrella"])
    assert title.split("\n\n") == ["Title: Safe interruptibility", "Authors: A, B", "Published: 2016",
                                   "Source: alignmentforum, lesswrong", "Affiliations: Lab X",
                                   "Abstract: none (no abstract or text is available for this record)",
                                   "Primary topic: AI Safety", "Search groups that found it: umbrella"]
    for name in ("safety-relevance-v4", "safety-relevance-v4-p4"):
        assert triage_ask.record_text(name, ROW, "An excerpt.") == text
    for name in ("safety-relevance-title-v2", "safety-relevance-title-v2-p4"):
        assert triage_ask.title_record_text(name, ROW, {"primary_topic": "AI Safety"}, ["umbrella"]) == title


@pytest.mark.parametrize(("metadata", "identity", "lines"), [
    ({"published_at": None}, {"channels": ["arxiv"]}, ["Source: arxiv"]),
    ({}, {"channels": []}, []),
    ({"published_at": dt.date(2009, 1, 1)}, None, ["Published: 2009"]),
    ({"published_at": dt.datetime(2019, 12, 31, 23, 0, tzinfo=dt.UTC)}, {"channels": None}, ["Published: 2019"]),
])
def test_record_v2_leaves_out_what_the_row_lacks(metadata: dict, identity: dict | None, lines: list[str]) -> None:
    row = {"metadata": {"title": "T", "abstract": "Abs.", "authors": [], **metadata}}
    if identity is not None:
        row["identity"] = identity
    assert triage_ask.provenance_lines(row) == lines
    v1, v2 = triage_ask.document_text(row, None), triage_ask.document_text_v2(row, None)
    assert v2.split("\n\n") == ["Title: T", "Authors: Unknown", *lines, "Abstract:\nAbs."]
    assert "None" not in v2 and (v2 == v1) == (lines == [])


def test_record_v2_refuses_a_date_it_cannot_read() -> None:
    with pytest.raises(TypeError, match="published_at"):
        triage_ask.provenance_lines({"metadata": {"published_at": "2016-06-01"}})


# ── (3) registration ─────────────────────────────────────────────────────────────────────────


#: The two name tables ``SPECS`` replaced, as at ba4fc01 (``RECORD_VERSIONS``, ``TITLE_VARIANTS``).
RECORD_VERSIONS_BEFORE = {
    "safety-relevance-v1": 1, "safety-relevance-v2": 1, "safety-relevance-v3": 1, "safety-relevance-title-v1": 1,
    "safety-relevance-v4": 2, "safety-relevance-title-v2": 2,
    "safety-relevance-v4-p4": 2, "safety-relevance-title-v2-p4": 2}
TITLE_VARIANTS_BEFORE = {"safety-relevance-v3": "safety-relevance-title-v1",
                         "safety-relevance-v4": "safety-relevance-title-v2",
                         "safety-relevance-v4-p4": "safety-relevance-title-v2-p4"}


def test_every_template_has_a_row_and_every_row_a_template() -> None:
    files = {p.stem for p in HERE.glob("*.md")}
    assert files == set(triage_ask.SPECS)
    assert {n: triage_ask.SPECS[n].record_version for n in NEW} == dict.fromkeys(NEW, 2)
    for s in triage_ask.SPECS.values():  # a variant is a row of its own, sent the same record version
        if s.title_variant is not None:
            assert triage_ask.SPECS[s.title_variant].record_version == s.record_version


def test_the_registry_is_the_tables_it_replaced() -> None:
    assert {n: s.record_version for n, s in triage_ask.SPECS.items()} == RECORD_VERSIONS_BEFORE
    assert {n: s.title_variant for n, s in triage_ask.SPECS.items() if s.title_variant} == TITLE_VARIANTS_BEFORE


@pytest.mark.parametrize("name", sorted(RECORD_VERSIONS_BEFORE))
def test_every_registered_name_is_sent_its_record(name: str) -> None:
    v1 = RECORD_VERSIONS_BEFORE[name] == 1
    subj, labels = {"primary_topic": "AI Safety"}, ["umbrella"]
    assert triage_ask.record_text(name, ROW, "An excerpt.") == (
        triage_ask.document_text if v1 else triage_ask.document_text_v2)(ROW, "An excerpt.")
    assert triage_ask.title_record_text(name, ROW, subj, labels) == (
        triage_ask.title_text if v1 else triage_ask.title_text_v2)(ROW, subj, labels)
    if name in TITLE_VARIANTS_BEFORE:
        qp = triage_ask.resolve(gate.Question("safety_relevance", "triage.x", {"name": name}), CONFIG.llm)
        assert triage_ask.resolve_title(qp, CONFIG.llm).name == TITLE_VARIANTS_BEFORE[name]


def test_an_unregistered_template_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "safety-relevance-v9.md").write_text("A template nobody registered.\n")
    monkeypatch.setattr(triage_ask, "HERE", tmp_path)
    q = gate.Question("safety_relevance", "triage.x", {"name": "safety-relevance-v9"})
    with pytest.raises(gate.GateError, match="SPECS"):
        triage_ask.resolve(q, CONFIG.llm)
    with pytest.raises(gate.GateError, match="no template"):
        triage_ask.resolve(gate.Question("safety_relevance", "triage.x", {"name": "safety-relevance-v8"}), CONFIG.llm)


@pytest.mark.parametrize("name", ["safety-relevance-v4", "safety-relevance-v4-p4"])
def test_new_names_resolve_with_their_title_variant(name: str) -> None:
    q = gate.Question("safety_relevance", "triage.x", {"name": name, "model": LUNA, "role": "classify_1k"})
    qp = triage_ask.resolve(q, CONFIG.llm)
    title = triage_ask.resolve_title(qp, CONFIG.llm)
    assert (qp.name, title.name) == (name, triage_ask.SPECS[name].title_variant)
    assert title.template_digest != qp.template_digest and title.model == qp.model == LUNA


@pytest.mark.parametrize("name", NEW)
def test_stack_eval_triage_resolves_each_new_name(name: str) -> None:
    from stack.evals.harness import make_adapter

    adapter = make_adapter("triage", CONFIG, None, prompt=name, models=[LUNA])
    assert adapter.prompt_name == name and adapter.pins()[LUNA]["name"] == name


# ── (4) the texts ────────────────────────────────────────────────────────────────────────────


def paras(text: str) -> list[str]:
    """The paragraphs of ``text``, whitespace-normalised (a rewrapped paragraph is the same paragraph)."""
    return [" ".join(p.split()) for p in text.split("\n\n") if p.strip()]


def inserted(old: str, new: str) -> list[str]:
    """The paragraphs ``new`` adds to ``old``; fails when ``new`` deletes or replaces any paragraph."""
    a, b = paras(old), paras(new)
    ops = difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes()
    assert [op for op in ops if op[0] not in ("equal", "insert")] == []
    return [p for tag, _i1, _i2, j1, j2 in ops if tag == "insert" for p in b[j1:j2]]


RECORD_NOTE = ("Besides title and authors the record may give `Published:` (the year of the document's publication "
               "date) and `Source:` (the channels it was collected from: `arxiv`, `openalex`, `semantic_scholar`, "
               "`lesswrong`, `alignmentforum`, a lab or organisation blog …; `openalex` and `semantic_scholar` are "
               "indexes, not venues).")
YOU_SEE = ("and the names of the search groups that found it.",
           "the names of the search groups that found it, and its publication year and the channels it was collected "
           "from when known.")


def test_v4_is_v3_plus_the_narrow_clause_and_the_record_note() -> None:
    assert inserted(body("safety-relevance-v3"), body("safety-relevance-v4")) == [*paras(NARROW), RECORD_NOTE]
    assert "safety-relevance-v4" in (HERE / "safety-relevance-v4.md").read_text().split("\n", 1)[0]


def test_title_v2_is_title_v1_plus_the_narrow_clause_and_the_record_note() -> None:
    old, new = body("safety-relevance-title-v1"), body("safety-relevance-title-v2")
    role = next(p for p in paras(old) if YOU_SEE[0] in p)
    assert role.replace(YOU_SEE[0], YOU_SEE[1]) in paras(new)  # the one changed sentence: what the record shows
    back = "\n\n".join(p.replace(YOU_SEE[1], YOU_SEE[0]) for p in paras(new))
    assert inserted(old, back) == [*paras(NARROW), RECORD_NOTE]


@pytest.mark.parametrize(("base", "p4"), [("safety-relevance-v4", "safety-relevance-v4-p4"),
                                          ("safety-relevance-title-v2", "safety-relevance-title-v2-p4")])
def test_p4_variants_differ_from_their_base_only_by_the_clause(base: str, p4: str) -> None:
    b, v = body(base), body(p4)
    assert inserted(b, v) == paras(PROVENANCE)
    assert v.index("# Technique provenance") > v.index("# Narrow settings (era-free)")
    assert v.replace(PROVENANCE + "\n", "", 1) == b  # byte for byte: the clause and its blank line, nothing else
