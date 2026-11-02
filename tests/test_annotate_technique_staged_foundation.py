"""``annotate.technique_staged`` foundation: the taxonomy v0.9 converter
(``taxonomy/sources/convert_v09.py``), the v0.9 loader and its renderings
(``workflows/annotate/technique_staged/taxonomy.py``), the segmenter (``segment.py``) and the lexical
matcher (``lexical.py``). No LLM, no store.

The converter runs on the small TSV fixture under ``tests/fixtures/technique_staged/`` (the draft
TSVs live outside the repository); its output is the fixture taxonomy the rendering goldens use.
Two tests load the real ``taxonomy/taxonomy-v0.9.yaml`` and ``taxonomy-v0.9-234.yaml``. The workflow
modules are loaded by path and registered in ``sys.modules``, as the runner loads siblings
(``@dataclass`` needs the module there).
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from stack.stage_runner import load_path_module

REPO = Path(__file__).resolve().parents[1]
WF = REPO / "workflows" / "annotate" / "technique_staged"
FIX = REPO / "tests" / "fixtures" / "technique_staged"
TECH_TSV = FIX / "techniques.tsv"
GROUP_TSV = FIX / "groups.tsv"
FIX_YAML = FIX / "taxonomy.yaml"
REAL_YAML = REPO / "taxonomy" / "taxonomy-v0.9.yaml"
REAL_YAML_234 = REPO / "taxonomy" / "taxonomy-v0.9-234.yaml"
REAL_YAML_091 = REPO / "taxonomy" / "taxonomy-v0.9.1.yaml"


def _load(name: str, path: Path) -> Any:
    mod = load_path_module(name, path)
    assert mod is not None
    return mod


T = _load("_technique_staged_taxonomy_under_test", WF / "taxonomy.py")
S = _load("_technique_staged_segment_under_test", WF / "segment.py")
L = _load("_technique_staged_lexical_under_test", WF / "lexical.py")

_spec = importlib.util.spec_from_file_location("_convert_v09_under_test", REPO / "taxonomy" / "sources" / "convert_v09.py")
assert _spec is not None and _spec.loader is not None
C = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(C)


@pytest.fixture(scope="module")
def tax() -> Any:
    return T.load_taxonomy(FIX_YAML)


@pytest.fixture(scope="module")
def raw() -> Any:
    return yaml.safe_load(FIX_YAML.read_text("utf-8"))


# ═════════════════════════════════════════ converter ═════════════════════════════════════════


def test_convert_fixture_is_deterministic_and_matches_committed_yaml() -> None:
    a, rewritten = C.convert(TECH_TSV, GROUP_TSV)
    b, _ = C.convert(TECH_TSV, GROUP_TSV)
    assert a == b
    assert rewritten == 0
    assert a == FIX_YAML.read_text("utf-8"), "fixture YAML is stale: re-run the converter on the fixture TSVs"
    assert f"# source techniques.tsv sha256 {hashlib.sha256(TECH_TSV.read_bytes()).hexdigest()}\n" in a
    assert f"# source groups.tsv sha256 {hashlib.sha256(GROUP_TSV.read_bytes()).hexdigest()}\n" in a
    assert not re.search(r"/(home|tmp)/", a)  # no absolute paths of the machine it ran on


def test_convert_counts_and_mapping(raw: Any) -> None:
    assert raw["version"] == "0.9" and raw["status"] == "draft" and "legacy" in raw["notes"]
    assert len(raw["techniques"]) == 10 and len(raw["group_nodes"]) == 2
    by = {t["slug"]: t for t in raw["techniques"]}
    guard = by["llm-based-safety-classifier"]
    # CSV quoting: the quoted cell comes out without the outer quotes, doubled quotes collapsed
    assert guard["definition"] == 'Scores "unsafe" content with a dedicated safety LLM.'
    assert guard["note"] == 'renamed display name ("Guard models")'
    assert guard["aliases"] == {"synonym": ["safeguard model"], "instance": ["Llama Guard", "ShieldGemma"],
                                "legacy": ["LLM-based safety classifier"]}
    assert guard["ml_methods"] == ["classifier-training"]
    assert list(guard) == ["slug", "name", "kind", "status", "origin", "definition", "aliases", "ml_methods",
                           "v08_name", "note"]
    assert "parent" not in by["rlhf"] and "aliases" not in by["keyword-blocklist-filter"]
    assert by["constitutional-ai"]["parent"] == "policy-alignment"
    assert by["dp-training"]["status"] == "provisional"
    policy = raw["group_nodes"][0]
    assert policy["children"] == ["constitutional-ai", "rlhf"]
    assert policy["aliases"] == {"synonym": ["policy-guided alignment"], "legacy": ["Policy alignment (old name)"]}
    assert "family" not in policy and "aliases" not in raw["group_nodes"][1]


def test_convert_cli_writes_the_file(tmp_path: Path) -> None:
    out = tmp_path / "t.yaml"
    assert C.main([str(TECH_TSV), str(GROUP_TSV), "--out", str(out)]) == 0
    assert out.read_text("utf-8") == FIX_YAML.read_text("utf-8")


def _tsv_variant(tmp_path: Path, edit: Any, groups: bool = False) -> tuple[Path, Path]:
    src = GROUP_TSV if groups else TECH_TSV
    text = edit(src.read_text("utf-8"))
    dst = tmp_path / src.name
    dst.write_text(text, encoding="utf-8")
    return (TECH_TSV, dst) if groups else (dst, GROUP_TSV)


@pytest.mark.parametrize(("edit", "groups", "msg"), [
    (lambda s: s.replace("machine-unlearning\t", "rlhf\t", 1), False, "duplicate slug 'rlhf'"),
    (lambda s: s.replace("\tpolicy-alignment\t", "\tno-such-parent\t", 1), False, "parent 'no-such-parent' does not resolve"),
    (lambda s: s.replace("\tassessment\t", "\tevaluation\t", 1), False, "kind 'evaluation'"),
    (lambda s: s.replace("\tprovisional\n", "\tdraft\n", 1), False, "status 'draft'"),
    (lambda s: s.replace("slug\tname\tv08_name", "slug\tname\tv08", 1), False, "header"),
    (lambda s: s.replace("\tfinal\n", "\tfinal\textra\n", 1), False, "18 fields"),
    (lambda s: s.replace("DP-SGD\t", "DP-SGD |  | X\t", 1), False, "empty item"),
    # scope_change is the 8th column: put a value right after the definition of the first row
    (lambda s: s.replace("any optimiser.\t\t", "any optimiser.\tnarrowed\t", 1), False, "scope_change"),
    (lambda s: s.replace("constitutional-ai,rlhf", "constitutional-ai,ghost", 1), True, "children ['ghost']"),
    (lambda s: s.replace("(old name) (legacy)", "(legacy) old name", 1), True, "'(legacy)' inside alias"),
])
def test_convert_refuses_bad_input(tmp_path: Path, edit: Any, groups: bool, msg: str,
                                   capsys: pytest.CaptureFixture[str]) -> None:
    t, g = _tsv_variant(tmp_path, edit, groups)
    with pytest.raises(C.ConvertError, match=re.escape(msg)):
        C.convert(t, g)
    assert C.main([str(t), str(g)]) == 1
    assert "convert_v09:" in capsys.readouterr().err


def test_convert_rewrites_workspace_refs_and_redacts(tmp_path: Path) -> None:
    ref = "impl" + "/" + "48"  # built so this file holds no workspace path
    t, g = _tsv_variant(tmp_path, lambda s: s.replace("\tnew-2026-10\t", f"\tv0.9 add (D7; {ref} C464)\t", 1))
    text, n = C.convert(t, g)
    assert n == 1 and ref not in text and "v0.9 add (D7; tax-review C464)" in text and "impl-48" not in text
    # a bare folder reference becomes a generic phrase (no round number)
    t2, g2 = _tsv_variant(tmp_path, lambda s: s.replace("\tnew-2026-10\t", f"\tsee {ref} notes\t", 1))
    text3, n3 = C.convert(t2, g2)
    assert n3 == 1 and "see a review round notes" in text3 and ref not in text3 and "impl-48" not in text3
    # a folder reference with a round-internal item label is removed with it, and the brackets tidied
    t4, g4 = _tsv_variant(tmp_path, lambda s: s.replace("\tnew-2026-10\t", f"\tv0.9 add (pass 4; {ref} K12)\t", 1))
    text4, n4 = C.convert(t4, g4)
    assert n4 == 1 and "origin: v0.9 add (pass 4)" in text4 and "K12" not in text4 and "impl-48" not in text4
    t5, g5 = _tsv_variant(tmp_path, lambda s: s.replace("\tnew-2026-10\t", f"\twidened in pass 4 ({ref} K13); kept\t", 1))
    text5, _ = C.convert(t5, g5)
    assert "widened in pass 4; kept" in text5
    text2, _ = C.convert(t, g, [re.compile(r"Abadi et al\. ")])
    assert "source_papers: '2016'" in text2 and "# 1 operator redaction(s) applied (--redact)" in text2
    assert "operator redaction" not in text


# ═════════════════════════════════════════ loader ════════════════════════════════════════════


def test_load_fixture(tax: Any) -> None:
    assert tax.version == "0.9" and tax.status == "draft"
    assert len(tax.techniques) == 10 and len(tax.group_nodes) == 2
    guard = tax.get("llm-based-safety-classifier")
    assert isinstance(guard, T.Technique)
    assert guard.synonyms == ("safeguard model",) and guard.instances == ("Llama Guard", "ShieldGemma")
    assert guard.legacy == ("LLM-based safety classifier",) and guard.kind == "monitoring"
    assert tax.get("dp-training").provisional and not guard.provisional
    assert isinstance(tax.get("runtime-monitoring"), T.GroupNode)
    assert tax.get("nope") is None
    assert T.all_slugs(tax)[-2:] == ("policy-alignment", "runtime-monitoring")
    assert len(T.all_slugs(tax)) == 12
    assert T.taxonomy_digest(FIX_YAML) == hashlib.sha256(FIX_YAML.read_bytes()).hexdigest()


def _set(path: str, value: Any) -> Any:
    def edit(d: Any) -> None:
        *head, last = path.split(".")
        for k in head:
            d = d[int(k)] if isinstance(d, list) else d[k]
        if value is _DEL:
            del d[last]
        else:
            d[int(last) if isinstance(d, list) else last] = value
    return edit


_DEL = object()


@pytest.mark.parametrize(("edit", "msg"), [
    (_set("extra", 1), "unknown top-level key"),
    (_set("version", 0.9), "`version` must be a non-empty string"),
    (_set("techniques", []), "no `techniques` list"),
    (_set("group_nodes", {}), "`group_nodes` must be a list"),
    (_set("techniques.0.name", _DEL), "lacks ['name']"),
    (_set("techniques.0.kind", "evaluation"), "kind 'evaluation'"),
    (_set("techniques.0.status", "draft"), "status 'draft'"),
    (_set("techniques.0.facets", {}), "unknown key(s) ['facets']"),
    (_set("techniques.0.aliases", {"synonym": ["x"], "alias": ["y"]}), "unknown alias type(s) ['alias']"),
    (_set("techniques.0.aliases", ["x"]), "`aliases` must be a mapping"),
    (_set("techniques.0.aliases", {"synonym": "x"}), "aliases.synonym: must be a list"),
    (_set("techniques.0.ml_methods", [""]), "ml_methods: must be a list of non-empty strings"),
    (_set("techniques.0.note", ""), "`note` must be a non-empty string"),
    (_set("techniques.1.parent", "ghost"), "parent 'ghost' does not resolve"),
    (_set("techniques.1.parent", "constitutional-ai"), "parent 'constitutional-ai' does not resolve"),
    (_set("techniques.2.slug", "rlhf"), "duplicate slug 'rlhf'"),
    (_set("group_nodes.0.slug", "rlhf"), "duplicate slug 'rlhf'"),
    (_set("group_nodes.0.children", []), "`children` is empty"),
    (_set("group_nodes.0.children", ["ghost"]), "children ['ghost'] do not resolve"),
    (_set("group_nodes.0.aliases", {"instance": ["x"]}), "unknown alias type(s) ['instance']"),
    (_set("group_nodes.0.definition", " "), "lacks ['definition']"),
])
def test_loader_refuses_schema_errors(raw: Any, edit: Any, msg: str) -> None:
    bad = copy.deepcopy(raw)
    edit(bad)
    with pytest.raises(ValueError, match=re.escape(msg)):
        T.parse_taxonomy(bad)


def test_load_taxonomy_names_the_file(tmp_path: Path) -> None:
    p = tmp_path / "broken.yaml"
    p.write_text("version: '0.9'\ntechniques: []\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"^broken\.yaml: no `techniques` list"):
        T.load_taxonomy(p)


# ═════════════════════════════════════════ renderings ════════════════════════════════════════


def test_menu_golden_and_properties(tax: Any) -> None:
    menu = T.menu_text(tax)
    assert menu == (FIX / "expected-menu.txt").read_text("utf-8")
    assert menu == T.menu_text(tax)  # byte-stable
    heads = [ln for ln in menu.splitlines() if ln.startswith("## ")]
    assert heads == ["## intervention", "## monitoring", "## assessment", "## group nodes (generic-mention targets)"]
    # legacy aliases, ml_methods and informal fields never shown
    for hidden in ("LLM-based safety classifier", "Constitutional-AI feedback", "Differential-privacy training",
                   "Policy alignment (old name)", "classifier-training", "misaligned behaviour", "Abadi",
                   "Direct Principle Feedback"):
        assert hidden not in menu, hidden
    assert ("- dp-training [intervention] — Differentially private training (provisional) "
            "(aka: DP training; e.g. DP-SGD): Trains under") in menu
    assert "{child of: runtime-monitoring}" in menu
    entries = [ln.split()[1] for ln in menu.splitlines() if ln.startswith("- ")]
    assert sorted(entries) == sorted(T.all_slugs(tax)) and len(entries) == len(set(entries))


def test_compact_index_golden(tax: Any) -> None:
    idx = T.compact_index(tax)
    assert idx == (FIX / "expected-index.txt").read_text("utf-8")
    lines = idx.splitlines()
    assert len(lines) == 12 and lines == sorted(lines)
    assert "policy-alignment — Safety-policy-guided alignment [group]" in lines
    assert ("dangerous-capability-evaluation.cbrn — CBRN / high-risk-domain evaluation [assessment] "
            "{child of: dangerous-capability-evaluation}") in lines
    assert "rlhf — RLHF [intervention]" in lines  # no parent, no suffix


POINTERS = {
    "llm-based-safety-classifier": [
        {"source": "prepass", "section": "3 Method", "surface": "Llama Guard",
         "snippet": "We filter outputs with\nLlama Guard 3.", "stance": "used"},
        {"source": "lexical", "section": None, "surface": "Llama Guard", "snippet": None, "stance": None}],
    "runtime-monitoring": [{"source": "prepass", "section": "front", "surface": "runtime monitors", "snippet": "",
                            "stance": "considered"}],
}
SUB_SLUGS = ["llm-based-safety-classifier", "dp-training", "runtime-monitoring", "dangerous-capability-evaluation.cbrn"]


def test_submenu_golden(tax: Any) -> None:
    text = T.submenu_text(tax, SUB_SLUGS, POINTERS)
    assert text == (FIX / "expected-submenu.txt").read_text("utf-8")
    assert "LLM-based safety classifier" not in text
    assert "[kind: none; parent: none; group node: yes]" in text
    assert "parent: dangerous-capability-evaluation; group node: no]" in text


@pytest.mark.parametrize(("slugs", "pointers", "msg"), [
    (["ghost"], {}, "unknown slug 'ghost'"),
    (["rlhf", "rlhf"], {}, "repeated slug(s) ['rlhf']"),
    (["rlhf"], {"dp-training": []}, "not in the sub-menu ['dp-training']"),
    (["rlhf"], {"rlhf": [{"quote": "x"}]}, "unknown key(s) ['quote']"),
    (["rlhf"], {"rlhf": [{"surface": 3}]}, "`surface` must be a string or None"),
])
def test_submenu_refuses(tax: Any, slugs: list[str], pointers: Any, msg: str) -> None:
    with pytest.raises(ValueError, match=re.escape(msg)):
        T.submenu_text(tax, slugs, pointers)


def test_real_taxonomy_v09() -> None:
    tax = T.load_taxonomy(REAL_YAML)
    assert tax.version == "0.9"
    assert len(tax.techniques) == 214 and len(tax.group_nodes) == 9
    kinds = [t.kind for t in tax.techniques]
    assert set(kinds) == {"intervention", "monitoring", "assessment"}
    assert len(set(T.all_slugs(tax))) == 223
    menu = T.menu_text(tax)
    assert menu.encode("utf-8") == T.menu_text(tax).encode("utf-8")
    assert len(T.compact_index(tax).splitlines()) == 223
    head = REAL_YAML.read_text("utf-8").splitlines()[:4]
    assert re.fullmatch(r"# source proposed-taxonomy-v0\.9\.tsv sha256 [0-9a-f]{64}", head[1])
    assert re.fullmatch(r"# source group-nodes-v0\.9\.tsv sha256 [0-9a-f]{64}", head[2])
    for t in tax.techniques:  # legacy aliases never reach the menu as aliases
        for a in t.legacy:
            assert f"aka: {a}" not in menu and f"e.g. {a}" not in menu


def test_real_taxonomy_v09_234() -> None:
    # The 234-entry v0.9 draft (techpipe D625), beside the 214-entry file kept for frozen sets and pin digests.
    # Red under: a regenerated or hand-edited file that drops or renames one of the 214 entries, a parent or group
    # child that no longer resolves, or the old file replaced in place (its sha256 is pinned elsewhere).
    tax = T.load_taxonomy(REAL_YAML_234)
    assert tax.version == "0.9"
    assert len(tax.techniques) == 234 and len(tax.group_nodes) == 9
    assert len(set(T.all_slugs(tax))) == 243
    techs = {t.slug for t in tax.techniques}
    groups = {g.slug for g in tax.group_nodes}
    parents = {t.slug: t.parent for t in tax.techniques if t.parent}
    assert len(parents) == 13 and set(parents.values()) <= techs | groups
    assert sum(p == "dangerous-capability-evaluation" for p in parents.values()) == 4
    assert all(c in techs for g in tax.group_nodes for c in g.children)
    old = T.load_taxonomy(REAL_YAML)
    assert {t.slug for t in old.techniques} < techs and {g.slug for g in old.group_nodes} == groups
    assert len(techs - {t.slug for t in old.techniques}) == 20
    head = REAL_YAML_234.read_text("utf-8").splitlines()[:4]
    assert re.fullmatch(r"# source proposed-taxonomy-v0\.9\.tsv sha256 [0-9a-f]{64}", head[1])
    assert head[3] == "# 234 techniques, 9 group nodes"
    assert hashlib.sha256(REAL_YAML.read_bytes()).hexdigest() == (
        "52815d3bdebf0fccfa5ffc8f31190dc4ca4ab40f27df9d5b7dcdd5466fe71cf2")  # the frozen sets' taxonomy_sha256


def test_real_taxonomy_v091_is_the_234_entries_with_clean_provenance() -> None:
    # The production taxonomy (D640): taxonomy-v0.9-234.yaml regenerated by the converter that drops internal
    # review-round labels (D631). Red under: an entry, alias, definition, parent or group node that differs from
    # the 234-entry file, a difference outside `origin` / `note`, a round label (`impl-<n>`) left in, or the
    # 234-entry file regenerated in place (its sha256 is pinned by eval sets 2026-10-18a/18b/18c).
    clean, frozen = (yaml.safe_load(p.read_text("utf-8")) for p in (REAL_YAML_091, REAL_YAML_234))
    assert clean["version"] == frozen["version"] == "0.9" and clean["group_nodes"] == frozen["group_nodes"]
    assert [t["slug"] for t in clean["techniques"]] == [t["slug"] for t in frozen["techniques"]]
    changed: dict[str, int] = {}
    for c, f in zip(clean["techniques"], frozen["techniques"], strict=True):
        for k in set(c) | set(f):
            if c.get(k) != f.get(k):
                changed[k] = changed.get(k, 0) + 1
    assert set(changed) <= {"origin", "note"} and sum(changed.values()) == 23
    assert "impl-" not in REAL_YAML_091.read_text("utf-8") and "impl-" in REAL_YAML_234.read_text("utf-8")
    tax = T.load_taxonomy(REAL_YAML_091)
    assert len(tax.techniques) == 234 and len(tax.group_nodes) == 9
    assert hashlib.sha256(REAL_YAML_234.read_bytes()).hexdigest() == (
        "07017821efe1a5d04d5cbf0b52f516c28a094f00870fd5d8d2f1726a6d7617a8")  # 18a/18b/18c taxonomy_sha256


# ═════════════════════════════════════════ segmenter ═════════════════════════════════════════


def _check_invariants(text: str, seg: Any) -> None:
    src = text[:S.DOC_CHAR_CAP]
    for i, p in enumerate(seg.paragraphs, start=1):
        assert p.pid == f"p{i}" and p.index == i
        assert p.text == src[p.start:p.end] and p.text == p.text.strip() and p.text
        assert p.chars <= S.MAX_PARA_CHARS
    starts = [p.start for p in seg.paragraphs]
    assert starts == sorted(starts)
    assert all(a.end <= b.start for a, b in zip(seg.paragraphs, seg.paragraphs[1:]))
    # every non-space character of the read text is in exactly one paragraph
    covered = "".join(p.text for p in seg.paragraphs)
    assert re.sub(r"\s", "", covered) == re.sub(r"\s", "", src)
    body = [p.pid for p in seg.paragraphs if p.kind == "body"]
    seen = [pid for c in seg.chunks for pid in c.pids[c.overlap:]]
    assert seen == body  # each body paragraph once (outside overlaps), in order; references never
    for c in seg.chunks:
        assert c.chars <= S.CHUNK_MAX
        assert all(p.kind == "body" for p in c.paragraphs)


PAPER = """# Steering Without Tears

Abstract

We study activation steering and RLHF for safety.

## 1 Introduction

Prior work used constitutional classifiers (Smith et al., 2024).

## 2 Method

2.1 Training setup
We fine-tune with DPO.

1. Collect preference pairs
2. Train the reward model

## References

Bai, Y., et al. (2022). Constitutional AI. arXiv:2212.08073.

12 Ouyang, L., et al. Training language models to follow instructions

Zou, A. (2023). Representation engineering. https://arxiv.org/abs/2310.01405

## Appendix A: Extra results

More experiments with circuit breakers.

### A.1 Details

Hyperparameters.
"""


def test_segment_markdown_paper_with_references_before_appendix() -> None:
    seg = S.segment(PAPER)
    _check_invariants(PAPER, seg)
    by_text = {p.text.splitlines()[0]: p for p in seg.paragraphs}
    assert seg.references_method == "heading"
    refs = [p.text.splitlines()[0][:12] for p in seg.paragraphs if p.kind == "references"]
    assert refs == ["## Reference", "Bai, Y., et ", "12 Ouyang, L", "Zou, A. (202"]
    assert by_text["## Appendix A: Extra results"].kind == "body"
    assert by_text["More experiments with circuit breakers."].section == "Appendix A: Extra results"
    assert by_text["Hyperparameters."].section == "A.1 Details"
    assert by_text["# Steering Without Tears"].heading_level == 1
    assert by_text["Abstract"].is_heading and by_text["Abstract"].heading_level == 1
    assert by_text["We study activation steering and RLHF for safety."].section == "Abstract"
    # a numbered heading on a block's first line splits from the text after it
    assert by_text["2.1 Training setup"].heading_level == 2
    assert by_text["We fine-tune with DPO."].section == "2.1 Training setup"
    # numbered list items inside a block are not headings
    assert by_text["1. Collect preference pairs"].heading_level == 1  # first line of a block: a heading
    assert not any(p.text.startswith("2. Train") for p in seg.paragraphs if p.is_heading)
    assert len(seg.chunks) == 1
    rendered = S.render_chunk(seg.chunks[0])
    assert rendered.startswith("[p1] # Steering Without Tears\n\n[p2] Abstract\n\n[p3] We study")
    assert "Bai, Y." not in rendered and "## References" not in rendered
    assert "## Appendix A: Extra results" in rendered
    assert seg.opening.startswith("# Steering Without Tears\n\nAbstract\n\nWe study")
    assert S.opening(PAPER) == seg.opening
    sm = seg.summary()
    assert sm["segmenter_version"] == "seg-v1" and sm["references"]["method"] == "heading"
    assert [s["label"] for s in sm["sections"] if s["kind"] == "references"] == ["References"]


def test_segment_references_level_rule() -> None:
    text = ("# Paper\n\nBody.\n\n## References\n\nA. Author (2020). Title.\n\n### Extra refs\n\n"
            "B. Author (2021). Other.\n\n## Discussion\n\nMore body.\n")
    seg = S.segment(text)
    kinds = {p.text: p.kind for p in seg.paragraphs}
    assert kinds["### Extra refs"] == "references"  # deeper heading stays inside
    assert kinds["B. Author (2021). Other."] == "references"
    assert kinds["## Discussion"] == "body" and kinds["More body."] == "body"


def test_segment_appendix_ends_references_even_when_deeper() -> None:
    text = ("# Paper\n\nBody.\n\n# References\n\nA. Author (2020). Title.\n\n## Appendix B: More\n\nAppendix body.\n\n"
            "# References\n\nB. Author (2021). Other.\n\nA.2 Proofs\n\nProof body.\n")
    seg = S.segment(text)
    kinds = {p.text: p.kind for p in seg.paragraphs}
    assert kinds["A. Author (2020). Title."] == "references" and kinds["B. Author (2021). Other."] == "references"
    assert kinds["## Appendix B: More"] == "body" and kinds["Appendix body."] == "body"
    assert kinds["A.2 Proofs"] == "body" and kinds["Proof body."] == "body"


FORUM = ("I have been thinking about how labs use RLHF and whether red-teaming catches much.\n\n"
         "My view: guard models like Llama Guard are underrated.\n\n"
         "Curious what others think. See https://example.org/post for more.\n")


def test_segment_forum_post_without_headings() -> None:
    seg = S.segment(FORUM)
    _check_invariants(FORUM, seg)
    assert [p.section for p in seg.paragraphs] == ["front"] * 3
    assert seg.references_method == "none" and len(seg.chunks) == 1
    assert S.render_chunk(seg.chunks[0]).count("[p") == 3
    assert seg.opening == "\n\n".join(p.text for p in seg.paragraphs)


def _reference(i: int) -> str:
    return f"Author{i}, A., et al. ({2000 + i % 20}). A study of things number {i}. arXiv:2101.{i:05d}"


def test_segment_reference_fallback_without_heading() -> None:
    body = "\n\n".join(f"Body paragraph {i} about safety training and evaluation of models." for i in range(40))
    refs = "\n".join(_reference(i) for i in range(15))
    text = body + "\n\n" + "Thanks for reading.\n" + refs + "\n"
    seg = S.segment(text)
    _check_invariants(text, seg)
    assert seg.references_method == "fallback"
    refs_paras = [p for p in seg.paragraphs if p.kind == "references"]
    assert refs_paras and refs_paras[0].text.startswith("Author0, A., et al.")
    # the paragraph is split at the first reference-like line: the line before it stays in the body
    assert any(p.text == "Thanks for reading." and p.kind == "body" for p in seg.paragraphs)


def test_segment_fallback_ignores_prose_with_citations() -> None:
    long_cite = ("Prior work (Smith et al., 2021) studied filtering; others (Lee et al. (2022)) did "
                 "unlearning, and https://example.org has a survey. ") * 6
    text = "\n\n".join(["Intro paragraph."] * 20 + [long_cite.strip()] * 30) + "\n"
    tail = [ln for ln in S._lines(text) if ln.start >= S.REF_TAIL_FRACTION * len(text)]
    assert len(tail) >= S.REF_RUN and all(S.looks_like_reference(ln.text) for ln in tail)  # only the length guard saves it
    seg = S.segment(text)
    assert seg.references_method == "none"
    assert all(p.kind == "body" for p in seg.paragraphs)


def test_segment_very_long_single_paragraph() -> None:
    sentence = "Models were trained with adversarial training and evaluated against jailbreaks. "
    text = sentence * 120  # ~9.7k characters, no newline at all
    seg = S.segment(text)
    _check_invariants(text, seg)
    assert len(seg.paragraphs) == 5
    assert all(p.text.endswith("jailbreaks.") for p in seg.paragraphs)  # cut at sentence ends
    # one unbroken 5k-character word run is hard-split
    blob = "x" * 5_000
    seg2 = S.segment(blob)
    _check_invariants(blob, seg2)
    assert [p.chars for p in seg2.paragraphs] == [2_000, 2_000, 1_000]


def test_segment_long_block_splits_at_line_breaks() -> None:
    lines = [f"Line {i}: the agent sandbox restricts file and network access for tool calls." for i in range(60)]
    text = "\n".join(lines)
    seg = S.segment(text)
    _check_invariants(text, seg)
    assert len(seg.paragraphs) > 1
    assert all(p.text.startswith("Line ") and p.text.endswith("tool calls.") for p in seg.paragraphs)


def _long_doc(n_paras: int, para_chars: int = 900) -> str:
    paras = []
    for i in range(n_paras):
        stem = f"Paragraph {i} discusses monitoring and evaluation. "
        paras.append((stem * (para_chars // len(stem) + 1))[:para_chars].rstrip() + ".")
    return "\n\n".join(paras)


def test_segment_single_chunk_up_to_40k() -> None:
    text = _long_doc(44)  # 44 x 900 = 39.6k body characters
    seg = S.segment(text)
    assert seg.body_chars <= S.SINGLE_CHUNK_MAX and len(seg.chunks) == 1


def test_segment_chunking_with_overlap() -> None:
    text = _long_doc(120)  # ~108k characters
    seg = S.segment(text)
    _check_invariants(text, seg)
    assert seg.body_chars > S.SINGLE_CHUNK_MAX
    chunks = seg.chunks
    assert len(chunks) == 4 and [c.index for c in chunks] == [1, 2, 3, 4] and {c.total for c in chunks} == {4}
    assert chunks[0].overlap == 0
    for prev, cur in zip(chunks, chunks[1:]):
        assert cur.overlap >= 1
        assert cur.overlap_pids == prev.pids[-cur.overlap:]  # the previous chunk's last paragraph(s)
        assert sum(p.chars for p in cur.paragraphs[: cur.overlap]) <= max(S.OVERLAP_CHARS, prev.paragraphs[-1].chars)
    for c in chunks[:-1]:
        assert c.chars <= S.CHUNK_TARGET
    sm = seg.summary()
    assert [c["overlap_pids"] for c in sm["chunks"]][0] == []
    assert S.render_chunk(chunks[1]).startswith(f"[{chunks[1].pids[0]}] Paragraph")


def test_segment_chunk_never_ends_with_a_heading() -> None:
    parts = []
    for k in range(60):
        parts += [f"## Section {k}", _long_doc(2, 700)]
    text = "\n\n".join(parts)
    seg = S.segment(text)
    _check_invariants(text, seg)
    assert len(seg.chunks) >= 3
    assert not any(c.paragraphs[-1].is_heading for c in seg.chunks[:-1])


def test_segment_remainder_rule() -> None:
    seg = S.segment(_long_doc(40, 1_100))  # 44k: over the single-chunk limit although under CHUNK_MAX
    assert len(seg.chunks) == 2
    seg2 = S.segment(_long_doc(70))  # 63k: ~30k, then a ~33k remainder (+ overlap) under 45k: one chunk, not two
    assert len(seg2.chunks) == 2 and S.CHUNK_TARGET < seg2.chunks[1].chars <= S.CHUNK_MAX


def test_segment_truncates_at_cap() -> None:
    text = _long_doc(400, 800)  # ~321k characters
    assert len(text) > S.DOC_CHAR_CAP
    seg = S.segment(text)
    _check_invariants(text, seg)
    assert seg.truncated and seg.read_chars == S.DOC_CHAR_CAP and seg.doc_chars == len(text)
    assert seg.paragraphs[-1].end <= S.DOC_CHAR_CAP
    sm = seg.summary()
    assert sm["truncated"] is True and sm["read_chars"] == 300_000
    assert not S.segment(text[:1000]).truncated


def test_segment_is_deterministic_and_handles_empty() -> None:
    text = PAPER + _long_doc(60)
    a, b = S.segment(text), S.segment(text)
    assert a.summary() == b.summary()
    assert [S.render_chunk(c) for c in a.chunks] == [S.render_chunk(c) for c in b.chunks]
    empty = S.segment("  \n\n ")
    assert empty.paragraphs == () and empty.chunks == () and empty.opening == ""


def test_segment_ignores_headings_in_code_fences() -> None:
    text = "Intro.\n\n```\n# not a heading\nReferences\n```\n\nAfter.\n"
    seg = S.segment(text)
    assert not any(p.is_heading for p in seg.paragraphs)
    assert seg.references_method == "none"


# ═════════════════════════════════════════ lexical ═══════════════════════════════════════════


@pytest.fixture(scope="module")
def lex(tax: Any) -> Any:
    return L.Lexicon(tax)


def _match(lex: Any, text: str) -> dict[str, Any]:
    seg = S.segment(text)
    return dict(lex.match(text, seg.paragraphs))


def test_lexical_surfaces_and_drops(lex: Any) -> None:
    # the display name without its parenthetical parts, never split at " / " (lex-v2)
    assert lex.surfaces["constitutional-ai"] == ("Constitutional AI",)
    assert lex.surfaces["anomaly-detection"] == ("Activation-space anomaly detection",)
    assert lex.surfaces["dangerous-capability-evaluation.cbrn"] == ("CBRN / high-risk-domain evaluation",)
    assert lex.surfaces["keyword-blocklist-filter"] == ("Keyword / blocklist filter",)
    assert L.name_surfaces("A / B (C / D)") == ["A / B"] and L.name_surfaces("(only)") == []
    assert "Llama Guard" in lex.surfaces["llm-based-safety-classifier"]
    assert all("LLM-based safety classifier" != s for s in lex.surfaces["llm-based-safety-classifier"])  # legacy
    assert lex.dropped == ()  # the fixture has no short or stoplisted surface
    assert L.STOPLIST == frozenset({"cascade", "debate"})
    assert L.drop_reason("AB") == "short" and L.drop_reason("Debate") == "stoplist"
    assert L.drop_reason("Keyword") is None and L.drop_reason("AI safety via debate") is None


def test_lexical_case_rules(lex: Any) -> None:
    assert "machine-unlearning" in _match(lex, "We apply RMU to the model.")
    assert "machine-unlearning" not in _match(lex, "We apply rmu to the model.")
    assert "dp-training" in _match(lex, "Trained with DP-SGD.")
    assert "dp-training" not in _match(lex, "Trained with dp-sgd.")  # all caps: case-sensitive
    assert "llm-based-safety-classifier" in _match(lex, "we deploy llama guard here")  # long: insensitive
    assert L.is_case_sensitive("RMU") and L.is_case_sensitive("DP-SGD") and not L.is_case_sensitive("Llama Guard")


def test_lexical_plural_and_hyphen_variants(lex: Any) -> None:
    for text in ("Chain of thought monitoring helps.", "chain‑of‑thought monitoring helps.",
                 "Chain – of – thought monitoring helps.", "chainofthought monitoring helps.",
                 "CoT-monitoring helps.", "chain-of-thought\nmonitoring helps."):
        assert "chain-of-thought-monitoring" in _match(lex, text), text
    assert "chain-of-thought-monitoring" not in _match(lex, "chain-of-thoughtmonitoring")  # space: separator required
    assert "llm-based-safety-classifier" in _match(lex, "Two safeguard models were used.")  # plural added
    hit = _match(lex, "A guard model screens outputs.")  # singular of "Guard models"
    assert hit["llm-based-safety-classifier"]["surfaces"] == ["guard model"]


def test_lexical_word_boundaries(lex: Any) -> None:
    assert "rlhf" not in _match(lex, "preRLHF and RLHFX are not names.")
    assert "rlhf" in _match(lex, "(RLHF) is used.")


def test_lexical_body_vs_references(lex: Any) -> None:
    text = ("# Paper\n\nWe use RLHF and then more RLHF.\n\n## References\n\n"
            "Ouyang et al. (2022). Training with RLHF. arXiv:2203.02155\n\n"
            "Smith (2023). Machine unlearning at scale.\n")
    seg = S.segment(text)
    out = lex.match(text, seg.paragraphs)
    assert list(out) == sorted(out)
    r = out["rlhf"]
    assert (r["hits_body"], r["hits_refs"]) == (2, 1)
    assert r["first_para"] == "p2" and r["surfaces"] == ["RLHF"]
    assert r["snippet"] == "We use RLHF and then more RLHF."
    u = out["machine-unlearning"]
    assert (u["hits_body"], u["hits_refs"], u["snippet"]) == (0, 1, None)
    assert u["first_para"] == seg.paragraphs[-1].pid


def test_lexical_snippet_is_bounded_and_centered(lex: Any) -> None:
    text = ("Filler text about nothing in particular. " * 20) + "Then RMU was applied. " + ("More filler. " * 30)
    seg = S.segment(text)
    snip = lex.match(text, seg.paragraphs)["machine-unlearning"]["snippet"]
    assert len(snip) <= L.SNIPPET_CHARS and "RMU" in snip


def test_lexical_nfkc_maps_back_to_original_text(lex: Any) -> None:
    text = "ﬁller: we used ＲＬＨＦ and the Llama Guard model."
    out = _match(lex, text)
    assert out["rlhf"]["surfaces"] == ["ＲＬＨＦ"]
    assert out["llm-based-safety-classifier"]["surfaces"] == ["Llama Guard"]


def test_lexical_positional_prefilter_equals_finditer(lex: Any) -> None:
    text = PAPER + FORUM + "CoT monitoring, Chain-of-thought monitors; DP training; ShieldGemma. RLHF RLHF."
    seg = S.segment(text)
    norm = text  # ascii
    for slug in lex.surfaces:
        if slug in lex._regex:
            assert lex._spans(slug, norm, norm.lower(), True) == lex._spans(slug, norm, norm.lower(), False), slug


def test_lexical_ignores_text_past_the_paragraphs(lex: Any) -> None:
    text = "Nothing here.\n\nStill nothing."
    seg = S.segment(text)
    assert lex.match(text + " RLHF", seg.paragraphs) == {}
    assert lex.match(text, ()) == {}


def test_lexicon_for_caches_per_digest(tax: Any) -> None:
    a = L.lexicon_for(tax, "d1")
    assert L.lexicon_for(tax, "d1") is a and L.lexicon_for(tax, "d2") is not a


def test_versions() -> None:
    assert S.SEGMENTER_VERSION == "seg-v1" and L.LEXICAL_VERSION == "lex-v2"
