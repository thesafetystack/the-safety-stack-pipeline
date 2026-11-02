"""``annotate.technique_staged`` (techpipe, D601-D605): the prompt set, the pure call pieces
(``stages.py``), the plan pin and its digests, the payload family, and the four modes end to end
through the real stage runner on a synthetic store with a scripted transport (no network).

The transport here (:class:`Router`) answers by call type (recognised from the system part each
prompt file sends) and by substrings of the whole prompt, so one document's pre-pass, selection and
grading calls get different scripted answers. The foundation pieces (taxonomy loader, segmenter,
lexical matcher) have their own file, ``test_annotate_technique_staged_foundation.py``.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import pathlib
import re
import shutil
from typing import Any, cast

import pytest
import yaml

import stack.annotate.stage as annotate_stage
from stack.annotate import AskContext, AskItem, config_digest
from stack.annotate.prompt_files import clear_cache
from stack.checks.verify import run_checks
from stack.llm import LlmRequest, LlmResponse
from stack.schemas import upcasters
from stack.schemas.families import FAMILIES, family
from stack.schemas.payloads.registry import REGISTRY
from stack.schemas.payloads.technique_roles import ROLE_NAMES
from stack.stage_runner import StageError, run_stage
from tests.fakes.annotate import AT_RUN, doc_with_text, fold_corpus, rows_of, write_documents, write_plan_file
from tests.fakes.llm import UnscriptedCall, ok, prompt_text, schema_invalid
from tests.synth.records import SERIES

REPO = pathlib.Path(__file__).resolve().parent.parent
WF_DIR = REPO / "workflows" / "annotate" / "technique_staged"
TAXONOMY = REPO / "taxonomy" / "taxonomy-v0.9.yaml"
LUNA = "openai/gpt-6-luna"


def _load(rel: str, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


main = _load("workflows/annotate/technique_staged/main.py", "_technique_staged_main_under_test")
stages = main.stages
judge = main.judge


# ═════════════════════════════════════ the prompt set ═══════════════════════════════════════


def _pf(call: str) -> Any:
    return main.prompt_file(main.DEFAULT_NAME, call)


def test_single_and_sweep_share_their_system_part_byte_for_byte() -> None:
    cat = main.catalogue(TAXONOMY)
    single, sweep = _pf("single"), _pf("sweep")
    assert single.system_template == sweep.system_template
    assert main.system_template(single, "single", cat) == main.system_template(sweep, "sweep", cat)
    assert sweep.user_placeholders == single.user_placeholders | {"reading_notes"}
    assert "# READING NOTES (pointers, not evidence)" in sweep.user_template


V2 = "technique-staged-v2"


@pytest.mark.parametrize("name", ["technique-staged-v1", V2])
def test_every_prompt_file_loads_with_the_placeholders_main_fills(name: str) -> None:
    want = {"prepass": (set(), {"opening", "chunk_index", "chunk_total", "section_labels", "chunk"}),
            "select": ({"taxonomy_menu"}, {"opening", "mention_count", "batch_note", "mentions"}),
            "grade": ({"catalogue_index"}, {"document", "truncation", "candidate_count", "candidates"}),
            "single": ({"taxonomy_menu"}, {"document", "truncation"}),
            "sweep": ({"taxonomy_menu"}, {"document", "truncation", "reading_notes"})}
    rubric = {"technique-staged-v1": "rubric-v0.9-a.md", V2: "rubric-v0.9-b.md"}[name]
    for call, (sys_ph, user_ph) in want.items():
        pf = main.prompt_file(name, call)
        assert (set(pf.system_placeholders), set(pf.user_placeholders)) == (sys_ph, user_ph), call
        includes = () if call in ("prepass", "select") else (rubric,) if call == "grade" else ("rubric-v0.9-a.md",)
        assert pf.includes == includes, call


def test_no_techniques_found_wording_is_the_both_lists_rule_everywhere() -> None:
    """P4 in every grading contract and in the rubric; the old one-list wording nowhere."""
    rubric = (WF_DIR / "prompts" / "rubric-v0.9-a.md").read_text("utf-8")
    for text in (_pf("grade").system_template, _pf("single").system_template):
        assert "Set `no_techniques_found` only when both `techniques` and\n`extra_techniques` are empty." in text \
            or "`no_techniques_found` only when both `techniques` and `extra_techniques` are empty." in text
        assert "Set `no_techniques_found` when `techniques` is empty" not in text
    assert "`no_techniques_found` when `techniques` is empty" not in rubric
    assert "only if both `techniques` and `extra_techniques` are empty" in rubric


def test_the_grading_contract_has_sweeps_and_rejected_and_the_schema_carries_both() -> None:
    g = _pf("grade").system_template
    assert '{"sweeps": {' in g and '"rejected": [' in g and "Write `sweeps` FIRST" in g
    props = main.G_SCHEMA["properties"]
    assert {"sweeps", "rejected", "doc_kind", "techniques", "extra_techniques", "flags"} <= set(props)
    assert set(props["sweeps"]["properties"]) == {"deployment_claims", "recommendations", "controls_discussed",
                                                 "menu_gaps"}
    assert "rejected" not in main.SINGLE_SCHEMA["properties"]  # the single/sweep schema is technique_roles'
    assert main.SINGLE_SCHEMA == judge.output_schema()


def test_the_role_and_stance_lists_match_the_payload() -> None:
    for call in ("grade", "single"):
        assert ", ".join(ROLE_NAMES) + "." in _pf(call).system_template
    assert " | ".join(stages.STANCES) in _pf("prepass").system_template
    assert set(stages.STANCES) == {"cited" if r == "cited_work" else r for r in ROLE_NAMES}  # one hint per role


def test_rubric_worked_examples_keep_the_mentioned_invariant() -> None:
    rubric = (WF_DIR / "prompts" / "rubric-v0.9-a.md").read_text("utf-8")
    blocks = re.findall(r"```json\n(.*?)```", rubric, re.DOTALL)
    rows = [t for b in blocks if '"doc_kind": "paper |' not in b for t in json.loads(b)["techniques"]]
    assert len(rows) == 6
    for r in rows:
        assert r["roles"]["mentioned"] >= max(r["roles"].values()), r["slug"]
    slugs = set(main.catalogue(TAXONOMY).slugs)
    assert {r["slug"] for r in rows} <= slugs  # the examples name real v0.9 slugs


def test_round_2_prompt_changes_are_in_the_files() -> None:
    p = _pf("prepass").system_template
    assert "a false positive costs them little" not in p and "unsupported notes consume candidate slots" in p
    assert "never infer missing stages, targets or\n  implementations." in p
    assert "omit a restatement only when\n  all three are unchanged" in p
    assert "deliberately training a misaligned model organism" not in p and "fine-tuned to\nobfuscate" in p
    assert "not one per citation" in p
    rubric = (WF_DIR / "prompts" / "rubric-v0.9-a.md").read_text("utf-8")
    assert "is the specific error this rule exists to stop" not in rubric
    assert "`modified` on RLHF" not in rubric and "residual scheming evaluation" not in rubric
    assert "Nearly always co-occurs" not in rubric
    for call in ("select", "single", "grade"):
        assert "provisional" in _pf(call).system_template


def test_repo_files_carry_no_workspace_paths_or_personal_names() -> None:
    files = [*sorted((WF_DIR / "prompts").glob("*.md")), WF_DIR / "main.py", WF_DIR / "stages.py",
             WF_DIR / "NOTES.md", REPO / "stack" / "schemas" / "payloads" / "technique_staged.py"]
    bad = re.compile(r"/home/|/tmp/|(?i:mimir)|impl/|\bTom[aá]š?\b|Gaven")
    for f in files:
        hits = bad.findall(f.read_text("utf-8"))
        assert not hits, (f.name, hits)


# ═════════════════════════════════════ pure pieces (stages.py) ══════════════════════════════

SEG_TEXT = ("# DOC_S Guard models in production\n\n"
            "We deploy Llama Guard on all outputs of the production assistant to block harmful content.\n\n"
            "## Method\n\n"
            "We fine-tune the model with refusal training on harmful requests.\n\n"
            "## Discussion\n\n"
            "Developers should monitor the reasoning traces of deployed agents before granting autonomy.\n")


def test_snippet_is_a_window_centred_on_the_surface_else_the_paragraph_head() -> None:
    para = ("x " * 300) + "We deploy constitutional\nclassifiers on inputs." + (" y" * 300)
    s = stages.snippet(para, "Constitutional classifiers")
    assert s is not None and "constitutional classifiers" in s and len(s) <= stages.SNIPPET_CHARS
    assert abs(s.index("constitutional") - len(s) // 2) < 40  # centred
    assert stages.snippet(para, "circuit breakers") is None
    assert stages.head("a\n b " * 200) == " ".join(("a\n b " * 200)[:stages.SNIPPET_CHARS].split())


def _mention(paras: list[int], surface: str = "Llama Guard", stance: str = "used", **kw: Any) -> dict[str, Any]:
    return {"paras": paras, "surface": surface, "gloss": kw.get("gloss", "a guard model"), "stance_hint": stance,
            "implicit": kw.get("implicit", False)}


def test_clean_mentions_validates_paragraphs_and_cuts_lengths() -> None:
    seg = main.segment.segment(SEG_TEXT)
    part = stages.parts_of(seg)[0]
    repairs: list[str] = []
    long_gloss = " ".join(f"w{i}" for i in range(50))
    got = stages.clean_mentions({"mentions": [
        _mention([2, 99, 2], gloss=long_gloss),
        _mention([99], surface="x" * 200),
        _mention([4], surface="  "),
        _mention([6, 1, 2, 4], surface="monitor the reasoning traces", stance="bogus"),
        _mention([4], surface="the safety recipe")]}, part, repairs)
    assert [m.paras for m in got] == [[2], [], [6, 1, 2], [4]]
    # the surface is not in the cited paragraph: the paragraph's head is the snippet
    assert got[3].snippet == "We fine-tune the model with refusal training on harmful requests."
    assert got[0].snippet and "Llama Guard" in got[0].snippet and got[0].section == "DOC_S Guard models in production"
    assert len(got[0].gloss.split()) == stages.GLOSS_WORDS and len(got[1].surface) == stages.SURFACE_CHARS
    assert got[1].snippet is None and got[1].section is None  # no valid paragraph: surface and gloss only
    assert got[2].stance == "mentioned" and got[2].section == "Discussion"
    assert repairs == ["para_out_of_chunk:1:99", "para_out_of_chunk:1:99", "empty_surface:1"]


def _para(i: int, text: str = "t", section: str = "s") -> Any:
    return main.segment.Paragraph(pid=f"p{i}", index=i, start=0, end=len(text), text=text, section=section, kind="body")


def test_dedupe_drops_only_chunk_overlap_double_reads() -> None:
    p1 = stages.Part(label="1", index_text="1", total=2, chunk=1, paragraphs=(_para(1), _para(2), _para(3)))
    p2 = stages.Part(label="2", index_text="2", total=2, chunk=2, paragraphs=(_para(3), _para(4)),
                     overlap=frozenset({3}))

    def m(part: Any, paras: list[int], surface: str, stance: str = "used", pos: int = 0) -> Any:
        return stages.Mention(part=part.label, chunk=part.chunk, position=pos, paras=paras, surface=surface,
                              gloss="g", stance=stance, implicit=False, section="s", snippet=None, sort_at=paras[0])

    a = m(p1, [3], "Llama Guard")
    same_chunk_twin = m(p1, [3], "llama-guard", pos=1)  # same chunk: two occurrences, kept
    b = m(p2, [3], "LLAMA GUARD")                       # overlap double read: dropped into a
    c = m(p2, [3], "Llama Guard", stance="evaluated")   # another stance: kept
    d = m(p2, [3, 4], "Llama Guard")                    # cites past the overlap: kept
    kept, n = stages.dedupe_overlap([a, same_chunk_twin, b, c, d], [p1, p2])
    assert n == 1 and kept == [a, same_chunk_twin, c, d]
    ordered = stages.assign_ids([d, c, same_chunk_twin, a], {"1": 0, "2": 1})
    assert [x.id for x in ordered] == ["m1", "m2", "m3", "m4"] and ordered[:2] == [a, same_chunk_twin]


def test_split_part_halves_at_a_paragraph_boundary() -> None:
    paras = tuple(_para(i, "x" * n) for i, n in enumerate([100, 100, 300, 100], start=1))
    part = stages.Part(label="2", index_text="2", total=3, chunk=2, paragraphs=paras, overlap=frozenset({1}))
    a, b = stages.split_part(part)
    assert [p.index for p in a.paragraphs] == [1, 2] and [p.index for p in b.paragraphs] == [3, 4]  # 200 | 400 chars
    assert (a.label, b.label, a.index_text, b.index_text) == ("2a", "2b", "2 (first half)", "2 (second half)")
    assert a.overlap == frozenset({1}) and b.overlap == frozenset() and not b.first_body
    assert stages.split_part(stages.Part(label="1", index_text="1", total=1, chunk=1, paragraphs=paras[:1])) is None
    assert stages.prepass_fills(b, "OPEN")["opening"].startswith(stages.OPENING_HEADER)


def test_apply_selection_validates_and_folds_empty_proposals() -> None:
    def mm(mid: str) -> Any:
        x = stages.Mention(part="1", chunk=1, position=0, paras=[2], surface=mid, gloss="g", stance="used",
                           implicit=False, section="s", snippet="snip", sort_at=2)
        x.id = mid
        return x

    asked = {k: mm(k) for k in ("m1", "m2", "m3", "m4")}
    valid = frozenset({"rlhf", "machine-unlearning", "a", "b", "c", "d", "e", "f"})
    repairs: list[str] = []
    parsed = {"mentions": [
        {"id": "m1", "candidates": [{"slug": s, "confidence": "high"} for s in ("a", "b", "c", "d", "e", "f")],
         "none": None},
        {"id": "m1", "candidates": [], "none": None},
        {"id": "m2", "candidates": [{"slug": "nope", "confidence": "high"}, {"slug": "rlhf", "confidence": "med"},
                                    {"slug": "rlhf", "confidence": "low"}],
         "none": {"reason": "too_vague", "proposed_name": None, "proposed_gloss": None}},
        {"id": "m3", "candidates": [], "none": {"reason": "off_taxonomy", "proposed_name": "", "proposed_gloss": " "}},
        {"id": "m9", "candidates": [], "none": None}],
        "doc_level": [{"slug": "machine-unlearning", "confidence": "low", "why": "opening"},
                      {"slug": "ghost", "confidence": "high", "why": ""}]}
    dl, missing = stages.apply_selection(parsed, asked, valid, repairs, read_doc_level=True)
    assert missing == ["m4"]
    assert len(asked["m1"].candidates) == stages.MAX_CANDIDATES and asked["m2"].candidates == [("rlhf", "med")]
    assert asked["m2"].none_reason is None  # none beside candidates is ignored
    assert (asked["m3"].none_reason, asked["m3"].proposed_name, asked["m3"].proposed_gloss) == ("off_taxonomy", None,
                                                                                               None)
    assert dl == [{"slug": "machine-unlearning", "confidence": "low", "why": "opening"}]
    assert repairs == ["candidates_cut:m1", "duplicate_mention_id:m1", "unknown_slug:m2:nope",
                       "none_with_candidates:m2", "unknown_mention_id:m9", "unknown_slug:doc_level:ghost"]
    assert stages.apply_selection(parsed, asked, valid, [], read_doc_level=False)[0] == []


def test_submenu_cap_drops_by_class_and_never_drops_protected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stages, "SUBMENU_CAP", 4)

    def mm(i: int, cands: list[tuple[str, str]], stance: str = "used") -> Any:
        x = stages.Mention(part="1", chunk=1, position=i, paras=[i], surface=f"s{i}", gloss="g", stance=stance,
                           implicit=False, section="sec", snippet=f"snip {i}", sort_at=i)
        x.id, x.candidates = f"m{i}", cands
        return x

    ms = [mm(1, [("hi", "high"), ("lo", "low")]), mm(2, [("me", "med")]), mm(3, [("hi", "med")], stance="evaluated"),
          mm(4, [("hi", "low")], stance="used"), mm(5, [("hi", "high")], stance="considered")]
    lex = {"lx1": {"hits_body": 1, "first_para": "p9", "surfaces": ["LX"], "snippet": "lx snip"},
           "lx3": {"hits_body": 3, "first_para": "p8", "surfaces": ["LY"], "snippet": "ly"},
           "hi": {"hits_body": 2, "first_para": "p1", "surfaces": ["HI"], "snippet": "x"}}
    repairs: list[str] = []
    sub = stages.assemble_submenu(ms, [{"slug": "dl", "confidence": "low", "why": "opening says so"}], lex,
                                  {"p9": "Nine"}, repairs)
    assert sub.classes == {"dl": "protected", "hi": "protected", "lo": "low", "me": "med", "lx3": "lexical",
                           "lx1": "lexical_one"}
    assert sub.slugs == ("dl", "hi", "me", "lx3")  # rank, then first appearance
    assert repairs == ["dropped_by_cap:lexical_one:lx1", "dropped_by_cap:low:lo"]
    hi = sub.pointers["hi"]
    # distinct stances first (used m1, evaluated m3, considered m5), shown in text order; then the count lines
    assert [p.get("source") for p in hi[:3]] == ["p1", "p3", "p5"] and hi[3] == {"note": "(+1 more mentions)"}
    assert hi[-1] == {"note": "name or aliases occur 2 times in the body"}
    assert sub.pointers["dl"] == [{"note": "document-level candidate (from the opening): opening says so"}]
    assert sub.pointers["lx3"][0]["surface"] == "LY"  # a lexical-only slug shows the matcher's first hit
    monkeypatch.setattr(stages, "SUBMENU_CAP", 1)
    r2: list[str] = []
    sub2 = stages.assemble_submenu(ms, [{"slug": "dl", "confidence": "low", "why": None}], {}, {}, r2)
    assert sub2.slugs == ("dl", "hi") and "submenu_over_cap:2" in r2


def test_reading_notes_and_mention_lines() -> None:
    assert stages.reading_notes([]) == stages.NO_NOTES == "(no reading notes)"
    m = stages.Mention(part="1", chunk=1, position=0, paras=[17], surface="constitutional classifiers",
                       gloss="classifiers blocking harmful outputs", stance="used", implicit=False, section="3 Method",
                       snippet="We deploy constitutional classifiers.", sort_at=17)
    m.id = "m12"
    assert stages.note_line(m) == ('[3 Method, p17] "constitutional classifiers" - classifiers blocking harmful '
                                   'outputs (stance hint: used): "We deploy constitutional classifiers."')
    assert stages.mention_line(m) == ('m12 | "constitutional classifiers" | gloss: classifiers blocking harmful '
                                      'outputs | stance: used | implicit: no | section: 3 Method | '
                                      '[p17] "We deploy constitutional classifiers."')


# ═════════════════════════════════════ pin, digests, freeze ═════════════════════════════════


def _llm() -> Any:
    return annotate_stage.stack_config().llm


#: sha256 over the JSON list of each pin's template digests at ab07b65 (before D612): the default role
#: (no reasoning effort) must keep them; prompt set v3 adds no file to v1 / v2.
PRE_EFFORT_PIN_DIGESTS: dict[str, str] = {
    "technique-staged-v1:lexical": "a9c46940ad88e4e0f1de66fbcd296d53c1638704c5e0c6f62f86bc97329fab6b",
    "technique-staged-v1:single": "63de8109798a81f67e7265e1d203c4dc09250a43940572c32687e202714e1da5",
    "technique-staged-v1:staged": "61c867c1991c5df616f698de362237d387b2154f356c06bede77f7adb09c3472",
    "technique-staged-v1:sweep": "8d4ac348c098ed2c38ff621f954255ea50567040aae3d0b11badf13a1ee425c0",
    "technique-staged-v2:lexical": "a57db2b144f3a1bf9c8874a317afdbf52b511e77f6a0a2e39f70823f3b1404e5",
    "technique-staged-v2:single": "63de8109798a81f67e7265e1d203c4dc09250a43940572c32687e202714e1da5",
    "technique-staged-v2:staged": "f92504ae9e319fd2bd0ab66523afc71646ecbdbd9a3719932519c4a7e41d012b",
    "technique-staged-v2:sweep": "8d4ac348c098ed2c38ff621f954255ea50567040aae3d0b11badf13a1ee425c0",
}


@pytest.mark.parametrize("key", sorted(PRE_EFFORT_PIN_DIGESTS))
def test_default_role_pin_digests_are_unchanged_by_reasoning_effort(key: str) -> None:
    name, mode = key.split(":")
    digests = list(main.prompt({"mode": mode, "name": name}, _llm()).template_digests)
    assert hashlib.sha256(json.dumps(digests).encode()).hexdigest() == PRE_EFFORT_PIN_DIGESTS[key]


def test_an_effort_role_moves_the_call_digests_and_keeps_the_file_digests() -> None:
    base = main.prompt({"mode": "single"}, _llm()).template_digests
    low = main.prompt({"mode": "single", "role": "extract_staged_low"}, _llm()).template_digests
    high = main.prompt({"mode": "single", "role": "extract_staged_high"}, _llm()).template_digests
    assert base[0] != low[0] != high[0] != base[0]  # the call's template digest (params carry the effort)
    assert base[1:] == low[1:] == high[1:]  # prompt file digests and the taxonomy sha256


@pytest.mark.parametrize(("pin", "msg"), [
    ({"mode": "single", "temperature": 0}, "plan pin keys"),
    ({}, "mode None must be one of"),
    ({"mode": "fast"}, "must be one of"),
    ({"mode": "single", "prepass_model": LUNA}, r"\['prepass_model'\] are not used by mode 'single'"),
    ({"mode": "lexical", "prepass_model": LUNA}, "not used by mode 'lexical'"),
    ({"mode": "sweep", "select_model": LUNA}, r"\['select_model'\] are not used by mode 'sweep'"),
    ({"mode": "staged", "name": "technique-staged-prepass-v1"}, "is not a prompt set"),
    ({"mode": "staged", "role": "nonesuch"}, "not an llm.yaml role"),
    ({"mode": "staged", "taxonomy": "/abs/taxonomy.yaml"}, "repo-relative"),
    ({"mode": "staged", "taxonomy": "../taxonomy.yaml"}, "repo-relative"),
    ({"mode": "staged", "taxonomy": "taxonomy/none.yaml"}, "cannot read the taxonomy"),
])
def test_pin_keys_are_validated_per_mode(pin: dict[str, Any], msg: str) -> None:
    with pytest.raises(StageError, match=msg):
        main.prompt(pin, _llm())


def test_the_pin_resolves_stage_models_and_lists_every_digest() -> None:
    llm = _llm()
    p = main.pin_of({"mode": "staged", "prepass_model": "anthropic/claude-haiku-5.5"}, llm)
    assert (p.role, p.model, p.prepass_model, p.select_model) == ("extract_staged", LUNA,
                                                                  "anthropic/claude-haiku-5.5", LUNA)
    pp = main.prompt({"mode": "staged", "prepass_model": "anthropic/claude-haiku-5.5"}, llm)
    assert pp.models == ("anthropic/claude-haiku-5.5", LUNA)
    cat = main.catalogue(TAXONOMY)
    files = [_pf(c).digest for c in ("prepass", "select", "grade")]
    assert set(files) | {cat.sha256, main.versions_digest("staged")} <= set(pp.template_digests)
    # P on haiku with the role's luna fallback, S and G on luna: 4 template digests + 3 files + taxonomy + versions
    assert len(pp.template_digests) == 9
    single = main.prompt({"mode": "single"}, llm)
    assert single.template_digests[1:] == (_pf("single").digest, cat.sha256)  # no segmenter / lexical digest
    assert main.prompt({"mode": "single"}, llm) == single  # deterministic
    assert not hasattr(main, "REASONING_EFFORT") and "reasoning_effort" not in main.PIN_KEYS


def _digests(mode: str) -> tuple[str, ...]:
    return cast(tuple[str, ...], main.prompt({"mode": mode}, _llm()).template_digests)


@pytest.mark.parametrize(("edit", "moved"), [
    ("prepass", {"sweep", "staged"}),
    ("selection", {"staged"}),
    ("grading", {"staged", "lexical"}),
    ("single", {"single"}),
    ("sweep", {"sweep"}),
    ("rubric", {"single", "sweep", "staged", "lexical"}),
    ("taxonomy", {"single", "sweep", "staged", "lexical"}),
    ("segmenter", {"sweep", "staged", "lexical"}),
    ("lexical", {"staged", "lexical"}),
])
def test_digests_move_with_every_input_of_the_mode(edit: str, moved: set[str], tmp_path: pathlib.Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    """A byte edit of a prompt file (or its rubric include), the taxonomy file, or a segmenter / lexical
    version moves the template digests (so ``config_digest``) of exactly the modes that use it."""
    here, root = tmp_path / "wf", tmp_path / "repo"
    shutil.copytree(WF_DIR / "prompts", here / "prompts")
    (root / "taxonomy").mkdir(parents=True)
    shutil.copy(TAXONOMY, root / "taxonomy" / TAXONOMY.name)
    monkeypatch.setattr(main, "HERE", here)
    monkeypatch.setattr(main, "REPO", root)
    clear_cache()
    before = {m: _digests(m) for m in main.MODES}
    assert before == {m: _digests(m) for m in main.MODES}
    files = {"prepass": "technique-staged-prepass-v1.md", "selection": "technique-staged-selection-v1.md",
             "grading": "technique-staged-grading-v1.md", "single": "technique-staged-single-v1.md",
             "sweep": "technique-staged-sweep-v1.md", "rubric": "rubric-v0.9-a.md"}
    if edit in files:
        f = here / "prompts" / files[edit]
        f.write_text(f.read_text("utf-8") + "<!-- an edit -->\n", "utf-8")
    elif edit == "taxonomy":
        f = root / "taxonomy" / TAXONOMY.name
        f.write_text(f.read_text("utf-8") + "\n# an edit\n", "utf-8")
    elif edit == "segmenter":
        monkeypatch.setattr(main.segment, "SEGMENTER_VERSION", "seg-test")
    else:
        monkeypatch.setattr(main.lexical, "LEXICAL_VERSION", "lex-test")
    clear_cache()
    after = {m: _digests(m) for m in main.MODES}
    spec = REGISTRY["technique_staged"]
    assert {m for m in main.MODES if config_digest(spec, {"mode": m}, main.prompt({"mode": m}, _llm()))
            != config_digest(spec, {"mode": m}, _pp(m, before[m]))} == moved
    assert {m for m in main.MODES if after[m] != before[m]} == moved
    clear_cache()


def _pp(mode: str, digests: tuple[str, ...]) -> Any:
    from stack.annotate import PromptPin

    return PromptPin(name=main.DEFAULT_NAME, template_digests=digests, models=(LUNA,))


#: sha256 of the v1 set's files as committed: a set is never edited (prompts/README.md), and the v2 set must leave
#: v1's bytes (so v1's digests and its cached answers) alone.
V1_SHA256 = {
    "technique-staged-prepass-v1.md": "fd54cbf7bbe9004d431818862da844a260f71b2db091bb90ec6c40bde856e0ed",
    "technique-staged-selection-v1.md": "740cdd882aec4929112e9c4f4110e8a27b2ee9e98392d89b7c835bc4bf568e50",
    "technique-staged-grading-v1.md": "28268cd8a3059bb72aee1bfa09ca9b40a0272f5dd5c1decde5b8025ed6c44884",
    "technique-staged-single-v1.md": "afd7fb8ecbd46f0b72deadf2c657acc05e1932072c2992f48594ab773fa8aec8",
    "technique-staged-sweep-v1.md": "369bec0d57acbf7903e9907437293749f014f4158f71dbc1ca382892aed86d12",
    "rubric-v0.9-a.md": "c1551b8bf0283591ea318f535d20ad6e9e5468cdee2b39a7f122c15fbe6b3e52",
}


def test_v1_files_are_byte_unchanged_and_v1_stays_the_default() -> None:
    for f, digest in V1_SHA256.items():
        assert hashlib.sha256((WF_DIR / "prompts" / f).read_bytes()).hexdigest() == digest, f
    assert main.DEFAULT_NAME == "technique-staged-v1"
    assert main.prompt({"mode": "staged"}, _llm()).name == "technique-staged-v1"


def test_the_pin_name_selects_the_v2_set() -> None:
    """v2 = v1's P, single and sweep files with selection-v2 and grading-v2 (rubric 0.9-b); a pin naming v2
    resolves to it, and every call the staged and lexical modes make reads the v2 file."""
    assert main.PROMPT_SETS[V2] == {**main.PROMPT_SETS["technique-staged-v1"],
                                    "select": "technique-staged-selection-v2", "grade": "technique-staged-grading-v2"}
    llm = _llm()
    assert main.pin_of({"mode": "staged", "name": V2}, llm).name == V2
    assert main.prompt({"mode": "staged", "name": V2}, llm).name == V2
    files = {c: main.prompt_file(V2, c).path.name for c in main.CALLS["staged"]}
    assert files == {"prepass": "technique-staged-prepass-v1.md", "select": "technique-staged-selection-v2.md",
                     "grade": "technique-staged-grading-v2.md"}
    s, g = main.prompt_file(V2, "select").system_template, main.prompt_file(V2, "grade").system_template
    assert "**candidates are not alternatives**" in s and "only when no catalogue entry fits the passage" in s
    assert "**Candidates are not alternatives.**" in g and '"kept_instead":' in g
    assert "**Precision rules.** Precedents 16-19" in g
    assert "**candidates are not alternatives**" not in _pf("select").system_template  # v1 as it was
    assert "kept_instead" not in _pf("grade").system_template and "Precision rules" not in _pf("grade").system_template


def test_rubric_b_is_rubric_a_plus_the_precision_block() -> None:
    a = (WF_DIR / "prompts" / "rubric-v0.9-a.md").read_text("utf-8")
    b = (WF_DIR / "prompts" / "rubric-v0.9-b.md").read_text("utf-8")
    start = b.index("**Precision rules.**")
    end = b.index("## 5. Grain and the catalogue")
    block = b[start:end]
    assert b[:start] + b[end:] == a
    assert [int(n) for n in re.findall(r"^(\d+)\. \*\*", block, re.MULTILINE)] == [16, 17, 18, 19]


V3 = "technique-staged-v3"
V3_REMINDER = ("Before writing: fill `passages` section by section to the LAST section, run the facet\n"
               "checklist, and make every listed slug a row or a `not_graded` entry. Then emit the JSON\n"
               "object described above, in a ```json fenced block, and nothing else.")


def test_the_pin_name_selects_the_v3_set_and_only_single_changes() -> None:
    """v3 = v2 with single-v2 (inventory-first contract, rubric 0.9-c); the v1 single file is untouched."""
    assert main.PROMPT_SETS[V3] == {**main.PROMPT_SETS[V2], "single": "technique-staged-single-v2",
                                    "window": "technique-staged-window-v1", "glean": "technique-staged-glean-v1"}
    assert main.INVENTORY_FILES == frozenset({"technique-staged-single-v2", "technique-staged-window-v1",
                                              *main.V4_SINGLE_FILES, "technique-staged-window-v4-a2",
                                              *main.V5_SINGLE_FILES, *main.V6_SINGLE_FILES})
    pf = main.prompt_file(V3, "single")
    assert pf.includes == ("rubric-v0.9-c.md",)
    assert (set(pf.system_placeholders), set(pf.user_placeholders)) == ({"taxonomy_menu"}, {"document", "truncation"})
    sys_t = pf.system_template
    for needle in ("# HOW TO FIND EVERYTHING", "Facet checklist: check each before you write rows.",
                   '"passages": [', '"not_graded": [{"slug":', "parent_of_graded_child",
                   "**Precision rules.** Precedents 16, 17 and 19", "against a fixed catalogue"):
        assert needle in sys_t, needle
    assert sys_t.index("{{taxonomy_menu}}") < sys_t.index("# HOW TO FIND EVERYTHING") < sys_t.index("# YOUR OUTPUT")
    assert "**The menu is never evidence.**" not in sys_t and "**The menu is never evidence.**" in _pf("single").system_template
    assert "18. **Vague first-party mitigation statements.**" not in sys_t
    assert main.user_message("DOC_X body", name=V3).rstrip().endswith(V3_REMINDER)
    assert main.user_message("DOC_X body").rstrip().endswith("Now emit the JSON object described above, in a ```json "
                                                            "fenced block, and nothing else.")
    for call in ("prepass", "select", "grade", "sweep"):  # every other call reads v2's file
        assert main.prompt_file(V3, call).path == main.prompt_file(V2, call).path
    assert main.schema_of(V3, "single") is main.INVENTORY_SCHEMA and main.schema_of(V2, "single") is main.SINGLE_SCHEMA
    assert main.schema_of(V3, "sweep") is main.SINGLE_SCHEMA and main.schema_of(V3, "grade") is main.G_SCHEMA
    inv_props = main.INVENTORY_SCHEMA["properties"]
    assert set(inv_props) == set(main.SINGLE_SCHEMA["properties"]) | {"not_graded"}
    assert set(inv_props["sweeps"]["properties"]) == set(main.SINGLE_SCHEMA["properties"]["sweeps"]["properties"]) | {
        "passages"}
    assert main.INVENTORY_SCHEMA["required"] == main.SINGLE_SCHEMA["required"]  # permissive, as D275
    assert main.SINGLE_SCHEMA == judge.output_schema()  # the base is not mutated
    v1, v3 = (main.prompt({"mode": "single", "name": n}, _llm()).template_digests for n in ("technique-staged-v1", V3))
    assert v1[0] != v3[0] and v1[1] != v3[1] and v1[-1] == v3[-1]  # call and file digests move, taxonomy sha stays


def test_rubric_c_is_rubric_b_without_precedent_18() -> None:
    a = (WF_DIR / "prompts" / "rubric-v0.9-a.md").read_text("utf-8")
    b = (WF_DIR / "prompts" / "rubric-v0.9-b.md").read_text("utf-8")
    c = (WF_DIR / "prompts" / "rubric-v0.9-c.md").read_text("utf-8")
    start, end = c.index("**Precision rules.**"), c.index("## 5. Grain and the catalogue")
    assert c[:start] + c[end:] == a
    block = c[start:end]
    assert [int(n) for n in re.findall(r"^(\d+)\. \*\*", block, re.MULTILINE)] == [16, 17, 19]
    p18 = b[b.index("18. **"):b.index("19. **")]
    head_b = ("**Precision rules.** Precedents 16-19 settle recurring boundary cases toward precision.\n"
              "They narrow the rules above and never widen them.\n")
    head_c = ("**Precision rules.** Precedents 16, 17 and 19 settle recurring boundary cases toward\n"
              "precision. They narrow the rules above and never widen them.\n")
    assert b.replace(p18, "").replace(head_b, head_c) == c


#: v3's plain single pin as the T1 v3 run recorded it at the commit that made v3 (before the window and glean
#: options existed): the options must not move it.
V3_SINGLE_PIN = ("84a174d93e6fb19b5b685d41d3596de7d6ebfb5053c726e657f85430d9e92ca6",
                 "70c2a74f1c4b87f4e545ab771c6d3cde3678accdd38ceb92ea6401a64bda3c93",
                 "52815d3bdebf0fccfa5ffc8f31190dc4ca4ab40f27df9d5b7dcdd5466fe71cf2")


def test_window_and_glean_options_add_calls_without_moving_the_plain_v3_pin() -> None:
    llm = _llm()
    assert main.prompt({"mode": "single", "name": V3}, llm).template_digests == V3_SINGLE_PIN
    single = main.prompt_file(V3, "single")
    for call in ("window", "glean"):  # the system part is single-v2's byte for byte (cached across the calls)
        assert main.prompt_file(V3, call).system_template == single.system_template, call
    assert set(main.prompt_file(V3, "window").user_placeholders) == {
        "part", "parts", "opening", "section_labels", "outline", "chunk", "truncation"}
    assert set(main.prompt_file(V3, "glean").user_placeholders) == set()
    win = main.prompt({"mode": "single", "name": V3, "window": True}, llm).template_digests
    both = main.prompt({"mode": "single", "name": V3, "window": True, "glean": True}, llm).template_digests
    assert set(V3_SINGLE_PIN) < set(win) < set(both)
    assert main.prompt_file(V3, "window").digest in win and main.versions_digest("single", window=True) in win
    assert main.versions_digest("single") is None and main.schema_of(V3, "glean") is main.GLEAN_SCHEMA
    assert main.pin_of({"mode": "single", "name": V3, "glean": True}, llm).calls() == ("single", "glean")
    for pin, msg in (({"mode": "single", "window": True}, "has no 'window' file"),
                     ({"mode": "single", "name": V3, "glean": "yes"}, "must be true or false"),
                     ({"mode": "staged", "name": V3, "window": True}, "not used by mode 'staged'")):
        with pytest.raises(StageError, match=msg):
            main.pin_of(pin, llm)


def test_windows_pack_the_body_with_one_paragraph_overlap_and_leave_p_chunks_alone() -> None:
    seg_mod = main.segment
    paras = [f"## Section {k}\n\n" + "\n\n".join(f"Paragraph {k}.{j} " + "word " * 300 for j in range(10))
             for k in range(12)]
    text = "# Title\n\n" + "\n\n".join(paras) + "\n\n## References\n\n" + "\n".join(
        f"[{i}] A. Author et al. (2020). Title {i}. arXiv:2001.{i:05d}" for i in range(40))
    seg = seg_mod.segment(text)
    assert seg.body_chars > main.WINDOW_MIN
    parts = seg_mod.windows(seg, single_max=main.WINDOW_MIN, target=main.WINDOW_TARGET, cap=main.WINDOW_MAX)
    assert len(parts) >= 2 and all(p.chars <= main.WINDOW_MAX for p in parts)
    assert all(p.overlap == 1 and p.paragraphs[0] == parts[k].paragraphs[-1] for k, p in enumerate(parts[1:]))
    assert all(q.kind == "body" for p in parts for q in p.paragraphs)  # references never sent
    assert seg.chunks == seg_mod._chunks([q for q in seg.paragraphs if q.kind == "body"])  # P's defaults
    outline = seg_mod.outline(seg).splitlines()
    assert outline[0] == "Title" and outline[1] == "  Section 0" and "References" not in outline
    fills = main.window_fills(seg, parts[-1], last=True, truncation="T")
    assert fills["part"] == str(len(parts)) and fills["truncation"] == "T" and "[p" not in fills["chunk"][:3]
    assert len(fills["opening"]) == main.WINDOW_OPENING


def test_doc_cap_and_window_min_are_single_mode_sizes_outside_the_default_digests() -> None:
    # Red under: a default pin whose digests move, a doc_cap / window_min that leaves the config digest alone,
    # or sizes that are not validated.
    llm = _llm()
    assert main.prompt({"mode": "single", "name": V3, "doc_cap": main.DOC_CHAR_CAP}, llm).template_digests == \
        V3_SINGLE_PIN  # the default value is the default pin
    capped = main.prompt({"mode": "single", "name": V3, "doc_cap": 900_000}, llm).template_digests
    assert capped[:len(V3_SINGLE_PIN)] == V3_SINGLE_PIN
    assert capped[len(V3_SINGLE_PIN):] == (main.versions_digest("single", doc_cap=900_000),)
    win = main.prompt({"mode": "single", "name": V3, "window": True}, llm).template_digests
    routed = main.prompt({"mode": "single", "name": V3, "window": True, "window_min": 300_000,
                          "doc_cap": 900_000}, llm).template_digests
    assert set(win) - set(routed) == {main.versions_digest("single", window=True)}
    assert main.versions_digest("single", window=True, doc_cap=900_000, window_min=300_000) in routed
    p = main.pin_of({"mode": "single", "name": V3, "window": True, "window_min": 300_000}, llm)
    assert (p.doc_cap, p.window_min) == (main.DOC_CHAR_CAP, 300_000)
    for pin, msg in (({"mode": "single", "doc_cap": True}, "doc_cap must be an integer"),
                     ({"mode": "single", "doc_cap": "900000"}, "doc_cap must be an integer"),
                     ({"mode": "single", "doc_cap": main.DOC_CAP_MAX + 1}, "doc_cap must be an integer"),
                     ({"mode": "single", "name": V3, "window": True, "window_min": 1_000},
                      "window_min must be an integer"),
                     ({"mode": "single", "name": V3, "window_min": 300_000}, "only with window: true"),
                     ({"mode": "staged", "doc_cap": 900_000}, "not used by mode 'staged'")):
        with pytest.raises(StageError, match=msg):
            main.pin_of(pin, llm)
    text = "x" * (main.DOC_CHAR_CAP + 10) + " LATE"
    assert main.user_message(text, name=V3) == main.prompt_file(V3, "single").user(main.doc_fills(text))
    late = main.user_message(text, name=V3, doc_cap=900_000)
    assert "LATE" in late and main.TRUNCATION_NOTE.strip() not in late
    assert main.TRUNCATION_NOTE.strip() in main.user_message(text, name=V3)
    seg = main.segment.segment(text, cap=900_000)
    assert seg.read_chars == len(text) and not seg.truncated and main.segment.segment(text).truncated


def test_max_tokens_pin_caps_every_call_outside_the_default_digests() -> None:
    # Red under: a default pin whose digests move, a max_tokens that leaves the call's template digest alone (or
    # moves the file digests), a key some mode refuses, or values that are not validated.
    llm = _llm()
    assert main.prompt({"mode": "single", "name": V3, "max_tokens": main.MAX_TOKENS}, llm).template_digests == \
        V3_SINGLE_PIN  # the default value is the default pin
    big = main.prompt({"mode": "single", "name": V3, "max_tokens": 64_000}, llm).template_digests
    assert big[0] != V3_SINGLE_PIN[0] and big[1:] == V3_SINGLE_PIN[1:]  # the call's params carry the cap
    assert main.prompt({"mode": "single", "name": V3, "max_tokens": 32_000}, llm).template_digests[0] not in \
        (big[0], V3_SINGLE_PIN[0])
    for mode in main.MODES:
        assert main.pin_of({"mode": mode, "max_tokens": main.MAX_TOKENS_MAX}, llm).max_tokens == main.MAX_TOKENS_MAX
    assert main.pin_of({"mode": "single"}, llm).max_tokens == main.MAX_TOKENS == 16_000
    for bad in (True, "64000", 64_000.0, main.MAX_TOKENS_MIN - 1, main.MAX_TOKENS_MAX + 1):
        with pytest.raises(StageError, match="max_tokens must be an integer"):
            main.pin_of({"mode": "single", "max_tokens": bad}, llm)


def test_v5_sets_pair_a_single_file_with_its_gleaning_file() -> None:
    # Red under: a v5 set touching another call's file, a gleaning file whose system part differs from its single
    # file's (the provider cache would miss), or a v5 single file outside the inventory contract.
    llm = _llm()
    names = sorted(k for k in main.PROMPT_SETS if k.startswith("technique-staged-v5-"))
    assert names and main.V5_SINGLE_FILES == {main.PROMPT_SETS[n]["single"] for n in names}
    for name in names:
        v = name.rsplit("-", 1)[1]
        single, glean = f"technique-staged-single-v5-{v}", f"technique-staged-glean-v5-{v}"
        assert main.PROMPT_SETS[name] == {**main.PROMPT_SETS[V2], "single": single, "glean": glean}
        assert single in main.INVENTORY_FILES and main.schema_of(name, "single") is main.INVENTORY_SCHEMA
        assert main.schema_of(name, "glean") is main.GLEAN_SCHEMA
        pf, gf = main.prompt_file(name, "single"), main.prompt_file(name, "glean")
        assert pf.includes == ("rubric-v0.9-c.md",)
        assert (set(pf.system_placeholders), set(pf.user_placeholders)) == ({"taxonomy_menu"}, {"document", "truncation"})
        assert gf.system_template == pf.system_template and set(gf.user_placeholders) == set()
        assert pf.system_template != main.prompt_file(V3, "single").system_template
        assert main.pin_of({"mode": "single", "name": name, "glean": True}, llm).calls() == ("single", "glean")
        with pytest.raises(StageError, match="has no 'window' file"):
            main.pin_of({"mode": "single", "name": name, "window": True}, llm)


S1 = "technique-staged-v6-s1"


def test_v6_s1_adds_sector_turns_to_v5_a1_1_on_one_cached_system_part(monkeypatch: pytest.MonkeyPatch) -> None:
    # Red under: v6-s1 touching v5-a1.1's single / gleaning files, a sector file whose system part differs from
    # theirs (the provider cache would miss), the sector file's sha256 missing from the digests, or sectors combinable
    # with windows / a set without a sector file.
    llm = _llm()
    base = main.PROMPT_SETS["technique-staged-v5-a1.1"]
    assert main.PROMPT_SETS[S1] == {**base, "sector": "technique-staged-sector-v6-s1"}
    sf, gf = main.prompt_file(S1, "sector"), main.prompt_file(S1, "glean")
    assert sf.system_template == gf.system_template == main.prompt_file(S1, "single").system_template
    assert set(sf.user_placeholders) == {"sector_index", "sector_count", "sector_title", "sector_scope",
                                         "sector_entries"}
    assert main.schema_of(S1, "sector") is main.GLEAN_SCHEMA
    p = main.pin_of({"mode": "single", "name": S1, "glean": True, "sectors": True}, llm)
    assert p.calls() == ("single", "glean", "sector")
    cat = main.catalogue(main.REPO / "taxonomy" / "techniques-1.0.0.yaml")
    sec = main.sectors_of(S1, cat)
    tax = "taxonomy/techniques-1.0.0.yaml"
    with_s = main.prompt({"mode": "single", "name": S1, "glean": True, "sectors": True, "taxonomy": tax},
                         llm).template_digests
    without = main.prompt({"mode": "single", "name": S1, "glean": True, "taxonomy": tax}, llm).template_digests
    assert sec.sha256 in with_s and sec.sha256 not in without
    assert main.prompt({"mode": "single", "name": "technique-staged-v5-a1.1", "glean": True, "taxonomy": tax},
                       llm).template_digests == without  # without sectors, v6-s1 sends v5-a1.1's calls
    with pytest.raises(StageError, match="has no 'window' file"):
        main.pin_of({"mode": "single", "name": S1, "sectors": True, "window": True}, llm)
    monkeypatch.setitem(main.PROMPT_SETS, S1, {**main.PROMPT_SETS[S1], "window": "technique-staged-window-v1"})
    with pytest.raises(StageError, match="sectors is read only without window"):
        main.pin_of({"mode": "single", "name": S1, "sectors": True, "window": True}, llm)
    with pytest.raises(StageError, match="has no 'sector' file"):
        main.pin_of({"mode": "single", "name": "technique-staged-v5-a1.1", "sectors": True}, llm)
    with pytest.raises(StageError, match="must be true or false"):
        main.pin_of({"mode": "single", "name": S1, "sectors": "yes"}, llm)
    with pytest.raises(StageError, match="not used by mode"):
        main.pin_of({"mode": "staged", "name": S1, "sectors": True}, llm)


def test_sector_file_covers_every_technique_once(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Red under: a technique in no sector or in two, a group node or an unknown slug accepted, a schema slip.
    cat = main.catalogue(main.REPO / "taxonomy" / "techniques-1.0.0.yaml")
    sec = main.sectors_of(S1, cat)
    flat = [s for x in sec.sectors for s in x.slugs]
    assert sorted(flat) == sorted(t.slug for t in cat.tax.techniques) and len(flat) == len(set(flat))
    assert len(sec.sectors) == 6 and sec.version == "sectors-v1"
    fills = main.sector_fills(sec.sectors[0], 1, 6, cat)
    assert fills["sector_entries"].splitlines()[0].startswith(f"- {sec.sectors[0].slugs[0]}: ")
    good = yaml.safe_load((main.HERE / "prompts" / main.SECTOR_FILES[S1]).read_text("utf-8"))
    group = cat.tax.group_nodes[0].slug

    def bad(mutate: Any, match: str) -> None:
        data = json.loads(json.dumps(good))
        mutate(data)
        f = tmp_path / f"s{abs(hash(match))}.yaml"
        f.write_text(yaml.safe_dump(data), "utf-8")
        monkeypatch.setitem(main.SECTOR_FILES, S1, str(f))  # an absolute name replaces the prompts directory
        with pytest.raises(StageError, match=match):
            main.sectors_of(S1, cat)

    bad(lambda d: d["sectors"][0]["slugs"].pop(), "techniques in no sector")
    bad(lambda d: d["sectors"][1]["slugs"].append(d["sectors"][0]["slugs"][0]), "is in sectors")
    bad(lambda d: d["sectors"][0]["slugs"].append(group), "is not a technique")
    bad(lambda d: d["sectors"][0]["slugs"].append("no-such-entry"), "is not a technique")
    bad(lambda d: d.update(extra=1), "top-level keys")
    bad(lambda d: d["sectors"][0].pop("scope"), "a sector needs")


def test_v6_p1_is_v5_a1_1_with_two_system_edits_and_s2_adds_its_sector_turns() -> None:
    # Red under: p1 differing from v5-a1.1 anywhere but the added evaluation-methods bullet and the spelled-out
    # interpretability-auditing check, its gleaning or sector file's system part differing from its single file's
    # (cache miss), or s2 not being p1 plus the sector turns.
    llm = _llm()
    base = main.PROMPT_SETS["technique-staged-v5-a1.1"]
    p1, s2 = "technique-staged-v6-p1", "technique-staged-v6-s2"
    assert main.PROMPT_SETS[p1] == {**base, "single": "technique-staged-single-v6-p1",
                                    "glean": "technique-staged-glean-v6-p1"}
    assert main.PROMPT_SETS[s2] == {**main.PROMPT_SETS[p1], "sector": "technique-staged-sector-v6-s2"}
    assert main.SECTOR_FILES[s2] == main.SECTOR_FILES[S1]
    for call in ("single", "glean"):
        old = main.prompt_file("technique-staged-v5-a1.1", call).path.read_text("utf-8").splitlines()
        new = main.prompt_file(p1, call).path.read_text("utf-8").splitlines()
        added = [x for x in new if x not in old]
        removed = [x for x in old if x not in new]
        assert len(added) == 2 and len(removed) == 1, call
        assert added[0].startswith("- Evaluation and attack methods the document applies or relays")
        assert removed[0].startswith("- Do not turn a generic interpretability observation") and \
            added[1].startswith(removed[0].split(" without the defined auditing activity")[0])
    pf = main.prompt_file(s2, "single")
    assert main.prompt_file(s2, "glean").system_template == pf.system_template == \
        main.prompt_file(s2, "sector").system_template
    assert main.prompt_file(s2, "sector").user_template == main.prompt_file(S1, "sector").user_template
    assert "technique-staged-single-v6-p1" in main.INVENTORY_FILES and main.schema_of(p1, "single") is main.INVENTORY_SCHEMA
    assert main.pin_of({"mode": "single", "name": s2, "glean": True, "sectors": True}, llm).calls() == \
        ("single", "glean", "sector")


def test_v5_a1_1_is_a1_without_the_stated_generation_budget() -> None:
    # Red under: a1.1 differing from a1 anywhere but the budget sentence, or still naming a token budget (the
    # production pin's max_tokens is not the 16,000 a1 states).
    for call in ("single", "glean"):
        old = main.prompt_file("technique-staged-v5-a1", call).path.read_text("utf-8").splitlines()
        new = main.prompt_file("technique-staged-v5-a1.1", call).path.read_text("utf-8").splitlines()
        changed = [i for i, (o, n) in enumerate(zip(old, new, strict=True)) if o != n]
        assert len(changed) == 1 and "16,000-token" in old[changed[0]], call
        assert "token total generation budget" not in new[changed[0]] and "16,000" not in "\n".join(new), call


def test_v4_sets_change_only_the_single_file_and_c1_shows_the_outline() -> None:
    # Red under: a v4 set touching another call's file, a v4 single file outside the inventory contract, or c1's
    # outline missing from its user message / from the pin's digests.
    llm = _llm()
    for v in ("a1", "a2", "a3", "c1", "c2"):
        name, f = f"technique-staged-v4-{v}", f"technique-staged-single-v4-{v}"
        extra = {"window": "technique-staged-window-v4-a2", "glean": "technique-staged-glean-v4-a2"} if v == "a2" else {}
        assert main.PROMPT_SETS[name] == {**main.PROMPT_SETS[V2], "single": f, **extra}
        assert f in main.INVENTORY_FILES and main.schema_of(name, "single") is main.INVENTORY_SCHEMA
        pf = main.prompt_file(name, "single")
        assert set(pf.user_placeholders) == ({"document", "truncation", "outline"} if v == "c1"
                                             else {"document", "truncation"})
        assert pf.system_template != main.prompt_file(V3, "single").system_template
        digests = main.prompt({"mode": "single", "name": name}, llm).template_digests
        assert (main.versions_digest("single", outline=True) in digests) == (v == "c1")
    a2 = "technique-staged-v4-a2"
    for call in ("window", "glean"):  # one cached system part across a2's calls
        assert main.prompt_file(a2, call).system_template == main.prompt_file(a2, "single").system_template, call
    assert set(main.prompt_file(a2, "window").user_placeholders) == set(main.prompt_file(V3, "window").user_placeholders)
    assert main.pin_of({"mode": "single", "name": a2, "glean": True}, llm).calls() == ("single", "glean")
    assert "technique-staged-window-v4-a2" in main.INVENTORY_FILES
    with pytest.raises(StageError, match="has no 'glean' file"):
        main.pin_of({"mode": "single", "name": "technique-staged-v4-c1", "glean": True}, llm)
    text = "# Title\n\n## Method\n\nWe train it.\n\n## Results\n\nIt works.\n"
    msg = main.user_message(text, name="technique-staged-v4-c1")
    assert "# OUTLINE" in msg and "  Method\n  Results" in msg and msg.index("# OUTLINE") < msg.index("We train it.")
    assert "# OUTLINE" not in main.user_message(text, name=V3)


def _routed_doc(tag: str, sections: int) -> str:
    sec = [f"## Part {k}\n\n" + "\n\n".join(("We deploy Llama Guard on all outputs of the production assistant. "
                                               if (k, j) == (0, 0) else "") + f"Filler {k}.{j} " + "text " * 300
                                              for j in range(8)) for k in range(sections)]
    return f"# {tag} A long report\n\n" + "\n\n".join(sec) + (
        "\n\n## Late section\n\nDevelopers should monitor the reasoning traces of deployed agents.\n")


@pytest.mark.slow
def test_window_min_routes_only_bodies_over_it_and_doc_cap_reads_past_300k(tmp_path: pathlib.Path,
                                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    """window_min 300,000 + doc_cap 900,000: a 170k body is one plain single call, a 360k body is read in parts
    to its end (the late section past 300k reaches the last part, and no truncation note is sent)."""
    # Red under: the windowed call still segmenting at 300k, or window_min not replacing WINDOW_MIN.
    r = Router()
    r.on("window", "DOC_R", reply=g_reply(g_row("chain-of-thought-monitoring", {"considered": 1}, COT_Q)))
    r.on("single", "DOC_M", reply=g_reply(g_row("llm-based-safety-classifier", {"used": 1}, GUARD_Q)))
    long_text, mid_text = _routed_doc("DOC_R", 30), _routed_doc("DOC_M", 14)
    assert len(long_text) > 350_000 and 150_000 < len(mid_text) < 300_000
    pin = {"mode": "single", "name": V3, "window": True, "window_min": 300_000, "doc_cap": 900_000}
    w = _world(tmp_path, monkeypatch, [long_text, mid_text], pin, r)
    rows, _ev, _ = _run(w)
    long_row, mid_row = rows[w["ids"][0]], rows[w["ids"][1]]
    seg = main.segment.segment(long_text, cap=900_000)
    n_win = len(main.segment.windows(seg, single_max=300_000, target=main.WINDOW_TARGET, cap=main.WINDOW_MAX))
    assert r.count("window") == n_win >= 7 and r.count("single") == 1
    last = r.texts("window")[-1]
    assert "Developers should monitor the reasoning traces" in last and "TRUNCATED" not in last
    assert {t["slug"] for t in long_row["techniques"]} == {"chain-of-thought-monitoring"}
    assert _counts(long_row)["windows"] == n_win and "windows" not in _counts(mid_row)
    assert _counts(long_row)["truncated_docs"] == 0
    _green(w["store"])


def test_merge_answers_takes_role_maxima_and_unions_quotes_flags_and_sweeps() -> None:
    a = {"doc_kind": "paper", "sweeps": {"passages": [{"where": "1"}], "menu_gaps": ["x"]}, "flags": ["f1"],
         "techniques": [{"slug": "s1", "roles": {"mentioned": 1, "used": 1}, "quotes": ["q1", "q2"], "flags": ["a"]},
                        "junk"],
         "extra_techniques": [{"name_as_written": "E", "roles": {"mentioned": 1}, "quotes": ["e1"]}],
         "not_graded": [{"slug": "s9"}]}
    b = {"doc_kind": "blogpost", "sweeps": {"passages": [{"where": "2"}]}, "flags": ["f1", "f2"],
         "techniques": [{"slug": "s1 ", "roles": {"mentioned": 3, "used": 1, "evaluated": 2},
                         "quotes": ["q2", "q3", "q4"], "flags": ["a", "b"], "note": "n"},
                        {"slug": "s2", "roles": {"mentioned": 1}, "quotes": ["q5"]}],
         "extra_techniques": [{"name_as_written": " E", "roles": {"mentioned": 2}, "quotes": ["e2"]}]}
    m = main.merge_answers([a, b])
    s1, junk, s2 = m["techniques"]
    assert s1 == {"slug": "s1", "roles": {"mentioned": 3, "used": 1, "evaluated": 2}, "quotes": ["q1", "q2", "q3"],
                  "flags": ["a", "b"], "note": "n"}
    assert junk == "junk" and s2["slug"] == "s2"
    assert m["extra_techniques"] == [{"name_as_written": "E", "roles": {"mentioned": 2}, "quotes": ["e1", "e2"],
                                      "flags": []}]
    assert m["doc_kind"] == "paper" and m["flags"] == ["f1", "f2"] and m["not_graded"] == [{"slug": "s9"}]
    assert m["sweeps"] == {"passages": [{"where": "1"}, {"where": "2"}], "menu_gaps": ["x"]}
    assert a["techniques"][0]["roles"] == {"mentioned": 1, "used": 1}  # inputs untouched


def test_glean_merge_keeps_pass_one_and_adds_only_new_slugs() -> None:
    first = {"doc_kind": "paper", "techniques": [{"slug": "s1", "roles": {"used": 2}, "quotes": ["q"]}],
             "extra_techniques": [{"name_as_written": "E", "roles": {"mentioned": 1}, "quotes": ["e"]}]}
    more = {"techniques": [{"slug": "s1", "roles": {"used": 4}, "quotes": ["other"]},
                           {"slug": "s2", "roles": {"mentioned": 1}, "quotes": ["q2"]}, {"slug": 3}],
            "extra_techniques": [{"name_as_written": "E", "roles": {}}, {"name_as_written": "F", "roles": {}}]}
    out, added = main.glean_merge(first, more)
    assert added == ["s2"] and [r["slug"] for r in out["techniques"]] == ["s1", "s2"]
    assert out["techniques"][0]["roles"] == {"used": 2}  # pass 1 wins on a conflict
    assert [x["name_as_written"] for x in out["extra_techniques"]] == ["E", "F"] and out["doc_kind"] == "paper"
    assert main.glean_merge(first, {})[1] == []


def test_inventory_accounting_counts_and_never_makes_rows() -> None:
    valid = frozenset({"a-slug", "b-slug", "c-slug", "d-slug"})
    parsed = {"sweeps": {"passages": [{"where": "1", "what": "x", "slugs": ["a-slug", "b-slug", "ghost"]},
                                      {"where": "2", "what": "y", "none": "too_vague"}, "junk",
                                      {"where": "3", "slugs": "not-a-list"}],
                         "recommendations": [{"technique": "c-slug"}, {"technique": "d-slug "}]},
              "not_graded": [{"slug": "b-slug", "why": "not_in_text"}, {"slug": "a-slug", "why": "odd"},
                             {"slug": "ghost", "why": "not_in_text"}, "junk"]}
    repairs: list[str] = []
    counts = stages.inventory_accounting(parsed, valid, frozenset({"a-slug", "c-slug"}), repairs)
    assert counts == {"inv_passages": 4, "inv_passage_slugs": 2, "inv_not_graded": 2, "inv_unaccounted": 1,
                      "inv_not_graded_with_row": 1}  # d-slug unaccounted; a-slug both not_graded and a row
    assert repairs == ["not_graded_reason_unknown:a-slug", "bad_not_graded", "bad_not_graded",
                       "not_graded_with_row:a-slug"]
    empty: list[str] = []
    assert stages.inventory_accounting({}, valid, frozenset(), empty) == {
        "inv_passages": 0, "inv_passage_slugs": 0, "inv_not_graded": 0, "inv_unaccounted": 0,
        "inv_not_graded_with_row": 0} and empty == []


def test_p_requests_are_identical_between_the_sets_and_the_other_digests_differ() -> None:
    """P's system part, schema and user messages are the same bytes under v1 and v2 (the response cache serves
    v1's P answers to v2); S's and G's template digests and file digests differ; single and sweep are the same
    calls under another name."""
    llm, cat = _llm(), main.catalogue(TAXONOMY)
    v1p, v2p = main.prompt_file("technique-staged-v1", "prepass"), main.prompt_file(V2, "prepass")
    assert main.system_template(v1p, "prepass", cat) == main.system_template(v2p, "prepass", cat)
    long_text = SEG_TEXT + "".join(f"\n\n## Part {i}\n\n" + "Refusal training on harmful requests. " * 400
                                   for i in range(8))
    for text in (SEG_TEXT, long_text):
        assert main.prepass_messages(text, name=V2) == main.prepass_messages(text, name="technique-staged-v1")
    assert len(main.prepass_messages(long_text)) > 1
    a = main.freeze_info(SEG_TEXT)
    b = main.freeze_info(SEG_TEXT, name=V2)
    assert a["user_digests"]["prepass"] == b["user_digests"]["prepass"] and a["user_digests"] == b["user_digests"]

    def call_digest(name: str, call: str) -> str:
        from stack.core.hashes import template_hash

        pf = main.prompt_file(name, call)
        return cast(str, template_hash(main.system_template(pf, call, cat), main.SCHEMAS[call],
                                       {"model": LUNA, "max_tokens": main.MAX_TOKENS}))

    assert call_digest("technique-staged-v1", "prepass") == call_digest(V2, "prepass")
    for call in ("select", "grade"):
        assert call_digest("technique-staged-v1", call) != call_digest(V2, call), call
        assert main.prompt_file("technique-staged-v1", call).digest != main.prompt_file(V2, call).digest, call
    for mode in ("staged", "lexical"):
        d1 = main.prompt({"mode": mode}, llm).template_digests
        d2 = main.prompt({"mode": mode, "name": V2}, llm).template_digests
        assert d1 != d2 and len(d1) == len(d2), mode
        spec = REGISTRY["technique_staged"]
        assert (config_digest(spec, {"mode": mode}, main.prompt({"mode": mode}, llm))
                != config_digest(spec, {"mode": mode, "name": V2}, main.prompt({"mode": mode, "name": V2}, llm)))
    staged1 = set(main.prompt({"mode": "staged"}, llm).template_digests)
    staged2 = set(main.prompt({"mode": "staged", "name": V2}, llm).template_digests)
    assert {call_digest(V2, "prepass"), v2p.digest} <= staged1 & staged2  # P shared
    assert main.prompt_file(V2, "grade").digest in staged2 - staged1
    for mode in ("single", "sweep"):
        assert (main.prompt({"mode": mode}, llm).template_digests
                == main.prompt({"mode": mode, "name": V2}, llm).template_digests), mode


@pytest.mark.parametrize(("edit", "moved"), [
    ("technique-staged-selection-v2.md", {"staged"}),
    ("technique-staged-grading-v2.md", {"staged", "lexical"}),
    ("rubric-v0.9-b.md", {"staged", "lexical"}),
    ("rubric-v0.9-a.md", {"single", "sweep"}),
    ("technique-staged-prepass-v1.md", {"sweep", "staged"}),
    ("technique-staged-selection-v1.md", set()),
])
def test_v2_digests_cover_its_files(edit: str, moved: set[str], tmp_path: pathlib.Path,
                                    monkeypatch: pytest.MonkeyPatch) -> None:
    """A byte edit of a file the v2 set uses (its own files, the rubric 0.9-b include, the shared v1 files)
    moves v2's template digests in exactly the modes that read it; a v1-only file moves none."""
    here = tmp_path / "wf"
    shutil.copytree(WF_DIR / "prompts", here / "prompts")
    monkeypatch.setattr(main, "HERE", here)
    clear_cache()

    def digests() -> dict[str, tuple[str, ...]]:
        return {m: main.prompt({"mode": m, "name": V2}, _llm()).template_digests for m in main.MODES}

    before = digests()
    f = here / "prompts" / edit
    f.write_text(f.read_text("utf-8") + "<!-- an edit -->\n", "utf-8")
    clear_cache()
    after = digests()
    assert {m for m in main.MODES if after[m] != before[m]} == moved
    clear_cache()


def test_freeze_info_is_deterministic_and_matches_the_user_messages() -> None:
    a, b = main.freeze_info(SEG_TEXT), main.freeze_info(SEG_TEXT)
    assert a == b and a["chunks"] == 1 and len(a["user_digests"]["prepass"]) == 1
    from stack.core.hashes import input_digest

    assert a["user_digests"]["single"] == input_digest(main.user_message(SEG_TEXT))
    assert a["user_digests"]["prepass"] == [input_digest(m) for m in main.prepass_messages(SEG_TEXT)]
    assert a["taxonomy_sha256"] == hashlib.sha256(TAXONOMY.read_bytes()).hexdigest()
    assert main.freeze_info(SEG_TEXT + "\n\nMore text.")["segmentation_digest"] != a["segmentation_digest"]
    with pytest.raises(ValueError, match="answer-independent"):
        main.user_message(SEG_TEXT, mode="sweep")
    lex = main.user_message(SEG_TEXT, mode="lexical")
    assert "## llm-based-safety-classifier" in lex and "# THE DOCUMENT\n\n# DOC_S" in lex


def test_the_judge_is_technique_roles_file_unchanged() -> None:
    """D605: the judge is loaded from technique_roles by path; a change of that file changes this
    stage's output without moving its digests, so it must be seen here (then bump the registry version
    of technique_staged and this constant)."""
    assert hashlib.sha256(main.JUDGE_PATH.read_bytes()).hexdigest() == main.JUDGE_SHA256
    assert main.judge.__name__ == "_technique_staged__technique_roles_judge"


def test_ask_refuses_a_taxonomy_that_changed_since_the_pin(tmp_path: pathlib.Path,
                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = annotate_stage.stack_config()
    pin = {"mode": "single"}
    pp = main.prompt(pin, cfg.llm)

    class NoCall:
        def complete(self, **_kw: Any) -> Any:
            raise AssertionError("no call expected")

    root = tmp_path / "repo"
    (root / "taxonomy").mkdir(parents=True)
    (root / "taxonomy" / TAXONOMY.name).write_text(TAXONOMY.read_text("utf-8") + "\n# edit\n", "utf-8")
    monkeypatch.setattr(main, "REPO", root)
    item = AskItem(doc_id="d", source_id="d", basis_kind="text", basis_digest="b", fields=None, row={"doc_id": "d"},
                   license_class="restricted", attempt=1, load_text=lambda: SEG_TEXT)
    actx = AskContext(run_id="r", stage=main.WORKFLOW_ID, store_root=None, pin=pp, plan_pin=pin, options={},
                      budget_usd=None, _config=lambda: cfg, _client=cast(Any, NoCall()))
    with pytest.raises(StageError, match="changed during the run"):
        main.ask(item, actx)


def test_merged_llm_sums_and_takes_the_grading_calls_identity() -> None:
    from stack.llm import LlmResult

    def res(i: int, hit: bool) -> LlmResult:
        return LlmResult(parsed={}, model=f"m{i}", prompt_name="p", template_digest=f"t{i}", model_digest=f"md{i}",
                         rendered_digest=f"r{i}", response_digest=f"x{i}", basis_digest="b", cost_usd=0.001 * i,
                         tokens_in=10 * i, tokens_out=i, latency_ms=5, is_cache_hit=hit)

    got = main.merged_llm([res(1, True), res(2, False), res(3, True)], 0.01, "technique-staged-v1", 42)
    assert got["cost_usd"] == pytest.approx(0.016) and (got["tokens_in"], got["tokens_out"]) == (60, 6)
    assert (got["model"], got["template_digest"], got["model_digest"]) == ("m3", "t3", "md3")
    assert got["is_cache_hit"] is False and got["latency_ms"] == 42 and got["prompt_name"] == "technique-staged-v1"
    assert got["rendered_digest"] != main.merged_llm([res(2, True), res(1, True), res(3, True)], 0, None,
                                                     1)["rendered_digest"]  # ordered
    assert main.merged_llm([res(1, True)], 0, None, 1)["is_cache_hit"] is True
    from stack.annotate.evidence import llm_columns

    assert llm_columns(got)["cost_usd"] == got["cost_usd"]  # every key is an evidence column


# ═════════════════════════════════════ payload family ═══════════════════════════════════════


def test_registry_entry_and_family_versions() -> None:
    spec = REGISTRY["technique_staged"]
    assert (spec.version, spec.grain, spec.basis_kind, spec.evidence, spec.reads) == (1, "document", "text", True, ())
    assert list(REGISTRY).index("technique_staged") == list(REGISTRY).index("technique_roles") + 1
    ann = family("corpus").schema.field("annotations").type
    names = [ann.field(i).name for i in range(ann.num_fields)]
    assert names.index("technique_staged") == names.index("technique_roles") + 1
    for fam in ("technique_staged", "technique_staged_evidence"):
        h = upcasters.history(fam)
        # technique_staged_evidence v2: the usage columns (D714, ADDITIVE); the newest version is the current schema
        want = [1] if fam == "technique_staged" else [1, 2]
        assert [v.number for v in h] == want and h[-1].digest == FAMILIES[fam].schema_digest
    docs = upcasters.history("documents")
    v5 = docs[4]  # the technique_staged step (D690's v6 came after it)
    assert v5.number == 5 and v5.step is upcasters.ADDITIVE and "technique_staged" in v5.note
    assert upcasters.history("corpus") == docs
    # the first five payload fields are technique_roles' (same row structs)
    tr, ts = REGISTRY["technique_roles"].payload_schema, spec.payload_schema
    assert [ts.field(i) for i in range(5)] == [tr.field(i) for i in range(5)]


def _type_at(t: Any, path: str) -> Any:
    import pyarrow as pa

    for part in path.split("."):
        name, is_list = part.removesuffix("[]"), part.endswith("[]")
        t = t.field(name).type
        if is_list:
            assert pa.types.is_list(t), path
            t = t.value_type
    return t


def test_text_bearing_paths_name_string_fields() -> None:
    import pyarrow as pa

    spec = REGISTRY["technique_staged"]
    for path in spec.text_bearing:
        assert _type_at(spec.payload_schema, path) == pa.string(), path
    assert {"prepass.mentions[].surface", "unresolved[].snippet", "candidate_gaps[].quotes[].text"} <= set(
        spec.text_bearing)


# ═════════════════════════════════════ the plan stanza ══════════════════════════════════════


def test_the_ann_scratch_stanza_is_inactive_and_valid_when_uncommented(tmp_path: pathlib.Path) -> None:
    """``configs/plans/ann-scratch.yaml`` carries the technique_staged pin and stage line commented out
    (no production work); uncommented, the plan loads and the pin resolves to the production choice (D640):
    single mode, prompt set v3, taxonomy v0.9.1."""
    from stack.plan import load_plan

    path = REPO / "configs" / "plans" / "ann-scratch.yaml"
    text = path.read_text("utf-8")
    active = load_plan(path)
    assert "annotate.technique_staged" not in active.prompts
    assert all(s.stage != "annotate.technique_staged" for s in active.stages)
    lines = text.splitlines()
    marked = [i for i, ln in enumerate(lines) if ln.lstrip().startswith("#ts# ")]
    assert len(marked) >= 3
    for i in marked:
        lines[i] = lines[i].replace("#ts# ", "", 1)
    tmp = tmp_path / "ann-scratch.yaml"
    tmp.write_text("\n".join(lines) + "\n", "utf-8")
    assert yaml.safe_load(tmp.read_text("utf-8"))["plan"] == "ann-scratch"
    plan = load_plan(tmp)
    pin = plan.prompts["annotate.technique_staged"]
    assert main.prompt(pin, _llm()).name == "technique-staged-v3"
    p = main.pin_of(pin, _llm())
    assert p.mode == "single" and p.calls() == ("single",)
    assert p.taxonomy == REPO / "taxonomy" / "taxonomy-v0.9.1.yaml"
    assert any(s.stage == "annotate.technique_staged" for s in plan.stages)


# ═════════════════════════════════════ the stage on the scripted model ══════════════════════

#: Call type by a sentence only that prompt file's system part sends (sweep before single: it shares it).
CALL_MARKERS = (("sector", "Sector pass "),  # v6-s1 (its prior turns carry the gleaning tail)
                ("glean", "Second pass. Your answer is above"),
                ("glean", "Second pass: audit how the evidence was filed"),  # v5-a1 / v5-a1.1
                ("window", "# THE DOCUMENT — PART "),
                ("prepass", "You are the reading step of a pipeline"),
                ("select", "You are the selection step of a pipeline"),
                ("grade", "Earlier\nsteps of this pipeline proposed a short list of CANDIDATE"),
                ("sweep", "# READING NOTES (pointers, not evidence)"),
                ("single", "against a fixed catalogue"),
                ("single", "using the rubric and fixed catalogue below"))  # v5-a1 / v5-a1.1


def call_of(text: str) -> str:
    return next(c for c, m in CALL_MARKERS if m in text)


class Router:
    """A scripted transport answering by call type and prompt substrings (module docstring); a rule's
    reply list plays in order, then repeats its last."""

    def __init__(self) -> None:
        self.rules: list[tuple[str, tuple[str, ...], list[LlmResponse], list[int]]] = []
        self.calls: list[tuple[str, str]] = []  # (call type, prompt text)
        self.models: list[tuple[str, str]] = []  # (call type, requested model)
        self.payloads: list[dict[str, Any]] = []  # the wire payloads, in call order
        self.cost = 0.0

    def on(self, call: str, *needles: str, reply: LlmResponse | list[LlmResponse]) -> Router:
        self.rules.append((call, needles, reply if isinstance(reply, list) else [reply], [0]))
        return self

    def post(self, request: LlmRequest) -> LlmResponse:
        text = prompt_text(request)
        call = call_of(text)
        self.calls.append((call, text))
        self.models.append((call, request.model))
        self.payloads.append(request.payload)
        for c, needles, replies, served in self.rules:
            if c == call and all(n in text for n in needles):
                reply = replies[min(served[0], len(replies) - 1)]
                served[0] += 1
                self.cost += float((reply.body.get("usage") or {}).get("cost") or 0.0)
                return reply
        raise UnscriptedCall(f"no rule for {call}: {text[-400:]!r}")

    def count(self, call: str | None = None, contains: str | None = None) -> int:
        return sum(1 for c, t in self.calls if (call is None or c == call) and (contains is None or contains in t))

    def texts(self, call: str, contains: str = "") -> list[str]:
        return [t for c, t in self.calls if c == call and contains in t]


def p_reply(*mentions: dict[str, Any], overflow: bool = False, cost: float = 0.001) -> LlmResponse:
    return ok({"mentions": list(mentions), "overflow": overflow}, cost=cost)


def s_answer(mid: str, *cands: tuple[str, str], none: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": mid, "candidates": [{"slug": s, "confidence": c} for s, c in cands], "none": none}


def s_reply(*answers: dict[str, Any], doc_level: list[dict[str, Any]] | None = None, cost: float = 0.002) -> LlmResponse:
    return ok({"mentions": list(answers), "doc_level": doc_level or []}, cost=cost)


def g_row(slug: str, roles: dict[str, int], *quotes: str) -> dict[str, Any]:
    return {"slug": slug, "roles": roles, "quotes": list(quotes)}


def g_reply(*rows: dict[str, Any], extras: list[dict[str, Any]] | None = None, rejected: list[dict[str, Any]] | None = None,
            flags: list[str] | None = None, cost: float = 0.004, sweeps: bool = True) -> LlmResponse:
    body: dict[str, Any] = {"doc_kind": "paper", "techniques": list(rows), "extra_techniques": extras or [],
                            "flags": flags or []}
    if sweeps:
        body["sweeps"] = {"deployment_claims": [], "recommendations": [], "controls_discussed": [], "menu_gaps": []}
    if rejected is not None:
        body["rejected"] = rejected
    return ok(body, cost=cost)


GUARD_Q = "We deploy Llama Guard on all outputs of the production assistant"
COT_Q = "Developers should monitor the reasoning traces of deployed agents"


def _world(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, texts: list[str], pin: dict[str, Any],
           router: Router, options: dict[str, Any] | None = None) -> dict[str, Any]:
    monkeypatch.delenv("STACK_WORKFLOWS_ROOT", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("STACK_PROGRESS", "0")
    monkeypatch.setattr(annotate_stage, "TRANSPORT_HOOK", lambda: router)
    monkeypatch.setattr(annotate_stage, "SLEEP", lambda _s: None)
    monkeypatch.setattr(annotate_stage, "CLOCK", lambda: AT_RUN)
    tmp_path.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(tmp_path)  # X6 finds the plan by name under ./configs/plans
    store = tmp_path / "store"
    docs = [doc_with_text(store, i, t) for i, t in enumerate(texts)]
    write_documents(store, docs, run=1)
    plan = write_plan_file(
        tmp_path, "techstaged-test", prompts={"annotate.technique_staged": pin},
        stages=[{"stage": "fold.corpus"},
                {"stage": "annotate.technique_staged", "prior": ["technique_staged", "technique_staged_evidence"],
                 **({"options": options} if options else {})},
                {"stage": "fold.build"}],
        folded=["technique_staged"])
    return {"store": store, "plan": plan, "ids": [d["doc_id"] for d in docs]}


def _run(w: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], Any]:
    corpus = fold_corpus(w["store"], w["plan"])
    rec = run_stage(w["store"], w["plan"], "annotate.technique_staged", [corpus], series=SERIES)
    assert rec.status == "ok", rec.error
    rows = {r["doc_id"]: r for r in rows_of(w["store"], rec, "technique_staged")}
    ev = {e["doc_id"]: e for e in rows_of(w["store"], rec, "technique_staged_evidence")}
    return rows, ev, rec


def _green(store: pathlib.Path) -> None:
    results = run_checks(store)
    red = {r.check: r.items for r in results if r.status not in ("ok", "skipped")}
    assert red == {}, red


def _counts(row: dict[str, Any]) -> dict[str, int]:
    return {c["name"]: c["value"] for c in row["prepass"]["counts"]}


@pytest.mark.slow
def test_staged_mode_end_to_end(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """P -> S -> L -> sub-menu -> G_sub on one document: notes post-processed, S validated (C3), the
    sub-menu ranked with pointers, an index row outside the sub-menu kept with its repair, ``rejected``
    in the prepass record and not in the rows, cost and tokens of the three calls merged on the
    evidence row with the grading call's template digest, and the checks green."""
    r = Router()
    r.on("prepass", "DOC_S", reply=p_reply(
        {"paras": [2], "surface": "Llama Guard", "gloss": "guard model screening outputs", "stance_hint": "attested_use",
         "implicit": False},
        {"paras": [4, 99], "surface": "refusal training", "gloss": "fine-tuning to refuse harmful requests",
         "stance_hint": "used", "implicit": False},
        {"paras": [6], "surface": "monitor the reasoning traces", "gloss": "watching agents' reasoning",
         "stance_hint": "considered", "implicit": True},
        {"paras": [6], "surface": "our safeguards", "gloss": "unspecified", "stance_hint": "mentioned", "implicit": False}))
    r.on("select", "DOC_S", reply=s_reply(
        s_answer("m1", ("llm-based-safety-classifier", "high")),
        s_answer("m2", ("safety-sft-refusal-training", "med"), ("not-a-slug", "low")),
        s_answer("m3", none={"reason": "off_taxonomy", "proposed_name": "", "proposed_gloss": "trace watching"}),
        s_answer("m4", none={"reason": "too_vague", "proposed_name": None, "proposed_gloss": None}),
        doc_level=[{"slug": "rlhf", "confidence": "low", "why": "the opening names preference training"}]))
    r.on("grade", "DOC_S", reply=g_reply(
        g_row("llm-based-safety-classifier", {"mentioned": 2, "attested_use": 2, "used": 2}, GUARD_Q),
        g_row("chain-of-thought-monitoring", {"considered": 1}, COT_Q),
        rejected=[{"slug": "safety-sft-refusal-training", "reason": "not_in_text"}, {"slug": "ghost", "reason": "odd"}]))
    w = _world(tmp_path, monkeypatch, [SEG_TEXT], {"mode": "staged", "model": LUNA}, r)
    rows, ev, rec = _run(w)
    row, e = rows[w["ids"][0]], ev[w["ids"][0]]
    assert [c for c, _ in r.calls] == ["prepass", "select", "grade"]
    assert row["status"] == "ok" and row["mode"] == "staged" and row["taxonomy_version"] == "0.9"
    # taxonomy file order (judge.normalize), the index row kept
    assert [t["slug"] for t in row["techniques"]] == ["chain-of-thought-monitoring", "llm-based-safety-classifier"]
    assert all(q["verified"] for t in row["techniques"] for q in t["quotes"])
    assert row["flags"] == []  # rows present: no no_techniques_found, complete: no partial
    rep = row["repairs"]
    assert {"para_out_of_chunk:1:99", "unknown_slug:m2:not-a-slug", "outside_submenu:chain-of-thought-monitoring",
            "rejected_reason_unknown:ghost", "rejected_not_candidate:ghost"} <= set(rep)
    pre = row["prepass"]
    assert [m["id"] for m in pre["mentions"]] == ["m1", "m2", "m3", "m4"]
    m1, m3 = pre["mentions"][0], pre["mentions"][2]
    assert (m1["chunk"], m1["paras"], m1["candidates"]) == ("1", [2], [{"slug": "llm-based-safety-classifier",
                                                                       "confidence": "high"}])
    assert (m3["none_reason"], m3["candidates"]) == ("off_taxonomy", [])
    assert pre["doc_level"] == [{"slug": "rlhf", "confidence": "low", "why": "the opening names preference training"}]
    assert pre["submenu"][:3] == ["rlhf", "llm-based-safety-classifier", "safety-sft-refusal-training"]
    lex = {x["slug"]: x["hits_body"] for x in pre["lexical"]}
    # "Guard models" (the heading) and "Llama Guard"; "refusal training"
    assert lex["llm-based-safety-classifier"] == 2 and lex["safety-sft-refusal-training"] == 1
    assert pre["rejected"] == [{"slug": "safety-sft-refusal-training", "reason": "not_in_text"},
                               {"slug": "ghost", "reason": "odd"}]
    assert "safety-sft-refusal-training" not in {t["slug"] for t in row["techniques"]}
    gaps = row["candidate_gaps"]
    assert gaps == [{"name": "monitor the reasoning traces", "gloss": "trace watching",  # C3: "" -> the surface
                     "quotes": [{"text": gaps[0]["quotes"][0]["text"], "verified": True}], "reason": "off_taxonomy",
                     "source": "selection"}]
    assert "monitor the reasoning traces" in gaps[0]["quotes"][0]["text"]
    assert row["unresolved"] == [{"surface": "our safeguards", "gloss": "unspecified",
                                  "snippet": row["unresolved"][0]["snippet"]}]
    seg = pre["segmenter"]
    assert (seg["segmenter_version"], seg["lexical_version"], len(seg["chunks"])) == ("seg-v1", "lex-v2", 1)
    assert [c["stage"] for c in pre["calls"]] == ["prepass", "select", "grade"]
    counts = _counts(row)
    assert counts["outside_submenu"] == 1 and counts["ungraded_candidates"] == len(pre["submenu"]) - 2
    assert counts["mentions"] == 4 and counts["s_calls"] == 1
    # the requests: S sees the snippet line, G the candidates with pointers and the index in its system part
    s_text = r.texts("select")[0]
    assert 'm1 | "Llama Guard" | gloss: guard model screening outputs | stance: attested_use | implicit: no' in s_text
    g_text = r.texts("grade")[0]
    assert "# THE CANDIDATES (" in g_text and '- [p2; DOC_S Guard models in production] "Llama Guard"' in g_text
    assert "document-level candidate (from the opening): the opening names preference training" in g_text
    assert "chain-of-thought-monitoring — Chain-of-thought monitoring [monitoring]" in g_text  # the index
    # evidence: the three calls merged (DESIGN §7.4)
    assert e["cost_usd"] == pytest.approx(0.007) == pytest.approx(r.cost)
    assert (e["tokens_in"], e["tokens_out"]) == (3 * 120, 3 * 24)
    assert e["prompt_name"] == "technique-staged-v1" and e["model"] == LUNA and e["is_cache_hit"] is False
    pin = main.prompt(w["plan"].prompts["annotate.technique_staged"], _llm())
    assert e["template_digest"] in pin.template_digests
    g_template = main.system_template(_pf("grade"), "grade", main.catalogue(TAXONOMY))
    from stack.core.hashes import template_hash

    assert e["template_digest"] == template_hash(g_template, main.G_SCHEMA, {"model": LUNA, "max_tokens": main.MAX_TOKENS})
    assert rec.counts["technique_rows"] == 2 and rec.counts["outside_submenu"] == 1
    _green(w["store"])


class RecordingRouter(Router):
    """:class:`Router` that also keeps every request (payload, model, the cache-key hashes)."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[tuple[str, LlmRequest]] = []

    def post(self, request: LlmRequest) -> LlmResponse:
        self.requests.append((call_of(prompt_text(request)), request))
        return super().post(request)


def _staged_script(r: Router) -> Router:
    r.on("prepass", "DOC_S", reply=p_reply(
        {"paras": [2], "surface": "Llama Guard", "gloss": "guard model screening outputs", "stance_hint": "attested_use",
         "implicit": False}))
    r.on("select", "DOC_S", reply=s_reply(s_answer("m1", ("llm-based-safety-classifier", "high"),
                                                   ("input-output-content-safety-classifier", "med"))))
    r.on("grade", "DOC_S", reply=g_reply(
        g_row("llm-based-safety-classifier", {"mentioned": 2, "attested_use": 2, "used": 2}, GUARD_Q),
        rejected=[{"slug": "input-output-content-safety-classifier", "reason": "wrong_sibling",
                   "kept_instead": "llm-based-safety-classifier"}]))
    return r


def test_the_v2_set_sends_v1_p_requests_and_its_own_s_and_g(tmp_path: pathlib.Path,
                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    """The same document in staged mode under v1 and under v2: the P requests are equal in everything the
    response cache keys on (model, rendered hash, input digest) and in payload; S and G send the v2 files;
    ``kept_instead`` in a rejection is accepted without a repair and not stored; the evidence names v2."""
    sent: dict[str, RecordingRouter] = {}
    rows: dict[str, dict[str, Any]] = {}
    ev: dict[str, dict[str, Any]] = {}
    for name in ("technique-staged-v1", V2):
        r = cast(RecordingRouter, _staged_script(RecordingRouter()))
        w = _world(tmp_path / name, monkeypatch, [SEG_TEXT], {"mode": "staged", "model": LUNA, "name": name}, r)
        got, evidence, _ = _run(w)
        sent[name], rows[name], ev[name] = r, got[w["ids"][0]], evidence[w["ids"][0]]
        assert [c for c, _ in r.calls] == ["prepass", "select", "grade"], name

    def by_call(name: str, call: str) -> LlmRequest:
        return next(q for c, q in sent[name].requests if c == call)

    p1, p2 = by_call("technique-staged-v1", "prepass"), by_call(V2, "prepass")
    assert (p1.model, p1.rendered_hash, p1.input_digest) == (p2.model, p2.rendered_hash, p2.input_digest)
    assert json.dumps(p1.payload, sort_keys=True) == json.dumps(p2.payload, sort_keys=True)
    for call in ("select", "grade"):
        assert by_call("technique-staged-v1", call).rendered_hash != by_call(V2, call).rendered_hash, call
    assert "name EVERY catalogue entry its passage" in prompt_text(by_call(V2, "select"))
    g2 = prompt_text(by_call(V2, "grade"))
    assert '"kept_instead":' in g2 and "**Precision rules.**" in g2
    assert "kept_instead" not in prompt_text(by_call("technique-staged-v1", "grade"))
    row = rows[V2]
    assert row["status"] == "ok" and [t["slug"] for t in row["techniques"]] == ["llm-based-safety-classifier"]
    assert row["prepass"]["rejected"] == [{"slug": "input-output-content-safety-classifier", "reason": "wrong_sibling"}]
    assert not [x for x in row["repairs"] if x.startswith(("bad_rejected", "rejected_"))]
    assert ev[V2]["prompt_name"] == V2 and ev["technique-staged-v1"]["prompt_name"] == "technique-staged-v1"
    assert ev[V2]["template_digest"] != ev["technique-staged-v1"]["template_digest"]


@pytest.mark.slow
def test_sweep_mode_appends_reading_notes_and_single_mode_shares_the_system_part(
        tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r = Router()
    r.on("prepass", "DOC_W", reply=p_reply({"paras": [2], "surface": "Llama Guard", "gloss": "guard model",
                                            "stance_hint": "used", "implicit": False}))
    r.on("prepass", "DOC_E", reply=p_reply())
    r.on("sweep", "DOC_W", reply=g_reply(g_row("llm-based-safety-classifier", {"used": 2}, GUARD_Q)))
    r.on("sweep", "DOC_E", reply=g_reply())
    texts = [SEG_TEXT.replace("DOC_S", "DOC_W"), SEG_TEXT.replace("DOC_S", "DOC_E")]
    w = _world(tmp_path, monkeypatch, texts, {"mode": "sweep"}, r)
    rows, ev, _ = _run(w)
    a, b = rows[w["ids"][0]], rows[w["ids"][1]]
    assert a["status"] == "ok" and a["techniques"][0]["roles"]["mentioned"] == 2  # clamped by the judge
    assert a["prepass"]["submenu"] == [] and a["prepass"]["mentions"][0]["candidates"] == []
    note = '[DOC_W Guard models in production, p2] "Llama Guard" - guard model (stance hint: used): "'
    assert note in r.texts("sweep", "DOC_W")[0]
    assert "# READING NOTES (pointers, not evidence)" in r.texts("sweep", "DOC_E")[0]
    assert f"\n\n{stages.NO_NOTES}\n\nNow emit the JSON object" in r.texts("sweep", "DOC_E")[0]  # P5
    assert (b["status"], b["flags"]) == ("empty", ["no_techniques_found"])
    assert ev[w["ids"][0]]["cost_usd"] == pytest.approx(0.005)
    _green(w["store"])
    # the single call's system part is the sweep call's
    sweep_system = r.texts("sweep", "DOC_W")[0].split("# THE DOCUMENT\n\n")[0]
    r2 = Router().on("single", reply=g_reply())
    w2 = _world(tmp_path / "single", monkeypatch, ["DOC_X plain text."], {"mode": "single"}, r2)
    _run(w2)
    assert r2.texts("single")[0].split("# THE DOCUMENT\n\n")[0] == sweep_system


HAIKU = "anthropic/claude-haiku-5.5"
SONNET = "anthropic/claude-sonnet-5.5"


@pytest.mark.parametrize(("pin", "want"), [
    ({"mode": "staged"}, [("prepass", LUNA), ("select", LUNA), ("grade", LUNA)]),  # the role's primary
    ({"mode": "staged", "model": HAIKU}, [("prepass", HAIKU), ("select", HAIKU), ("grade", HAIKU)]),
    ({"mode": "staged", "model": SONNET, "prepass_model": HAIKU},
     [("prepass", HAIKU), ("select", SONNET), ("grade", SONNET)]),
    ({"mode": "staged", "model": SONNET, "prepass_model": HAIKU, "select_model": LUNA},
     [("prepass", HAIKU), ("select", LUNA), ("grade", SONNET)]),
    ({"mode": "sweep", "model": HAIKU}, [("prepass", HAIKU), ("sweep", HAIKU)]),
])
def test_the_stage_sends_each_call_to_its_pinned_or_defaulted_model(
        pin: dict[str, Any], want: list[tuple[str, str]], tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    # Red under: pin_of defaulting prepass_model / select_model to the role's primary instead of the pin's model,
    # or _Ask.call sending the grading model for every call (the eval adapter builds on the same pin semantics).
    r = Router()
    r.on("prepass", reply=p_reply({"paras": [2], "surface": "Llama Guard", "gloss": "guard model",
                                   "stance_hint": "used", "implicit": False}))
    r.on("select", reply=s_reply(s_answer("m1", ("llm-based-safety-classifier", "high"))))
    r.on("grade", reply=g_reply(g_row("llm-based-safety-classifier", {"used": 2}, GUARD_Q), rejected=[]))
    r.on("sweep", reply=g_reply(g_row("llm-based-safety-classifier", {"used": 2}, GUARD_Q)))
    w = _world(tmp_path, monkeypatch, [SEG_TEXT], pin, r)
    rows, ev, _ = _run(w)
    assert r.models == want
    row = rows[w["ids"][0]]
    assert [(c["stage"], c["model"]) for c in row["prepass"]["calls"]] == want
    assert ev[w["ids"][0]]["model"] == want[-1][1]  # the grading call's model on the evidence row


def test_single_mode_and_no_techniques_found_semantics(tmp_path: pathlib.Path,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    """P4: an answer with only an extra row is ``ok`` without ``no_techniques_found`` (judge.normalize
    would add it); an empty answer is ``empty`` with it; a model-set flag beside an extra is dropped."""
    extra = {"name_as_written": "trace auditing", "gloss": "auditing traces", "roles": {"introduced": 2},
             "quotes": ["We introduce trace auditing for agents"]}
    r = Router()
    r.on("single", "DOC_1", reply=g_reply(extras=[extra]))
    r.on("single", "DOC_2", reply=g_reply())
    r.on("single", "DOC_3", reply=g_reply(extras=[extra], flags=["no_techniques_found"]))
    texts = [f"DOC_{i}. We introduce trace auditing for agents and test it." for i in (1, 2, 3)]
    w = _world(tmp_path, monkeypatch, texts, {"mode": "single"}, r)
    rows, ev, _ = _run(w)
    one, two, three = (rows[i] for i in w["ids"])
    assert (one["status"], one["flags"], one["techniques"]) == ("ok", [], [])
    assert "no_techniques_found_added" not in one["repairs"]
    assert one["candidate_gaps"] == [{"name": "trace auditing", "gloss": "auditing traces",
                                      "quotes": [{"text": "We introduce trace auditing for agents", "verified": True}],
                                      "reason": None, "source": "extra"}]
    assert (two["status"], two["flags"]) == ("empty", ["no_techniques_found"])
    assert three["flags"] == [] and "no_techniques_found_dropped" in three["repairs"]
    assert one["prepass"]["segmenter"] is None and one["prepass"]["calls"][0]["stage"] == "single"
    # one call, no failure: the evidence row is the call's own result
    assert ev[w["ids"][0]]["cost_usd"] == pytest.approx(0.004) and ev[w["ids"][0]]["latency_ms"] is not None
    _green(w["store"])


def _inventory_reply() -> LlmResponse:
    body = {"sweeps": {"passages": [
                {"where": "intro", "what": "guard model on outputs", "slugs": ["llm-based-safety-classifier"]},
                {"where": "method", "what": "refusal fine-tuning", "slugs": ["safety-sft-refusal-training", "rlhf"]},
                {"where": "discussion", "what": "trace monitoring advice", "slugs": ["chain-of-thought-monitoring"]}],
                       "deployment_claims": [], "recommendations": [], "controls_discussed": [], "menu_gaps": []},
            "doc_kind": "paper",
            "techniques": [g_row("llm-based-safety-classifier", {"mentioned": 2, "attested_use": 2, "used": 2}, GUARD_Q),
                           g_row("safety-sft-refusal-training", {"mentioned": 1, "used": 1},
                                 "We fine-tune the model with refusal training on harmful requests")],
            "extra_techniques": [],
            "not_graded": [{"slug": "rlhf", "why": "not_in_text"},
                           {"slug": "safety-sft-refusal-training", "why": "not_in_text"},
                           {"slug": "not-a-slug", "why": "not_in_text"}],
            "flags": []}
    return ok(body, cost=0.004)


@pytest.mark.parametrize("name", [V3, "technique-staged-v1"])
def test_single_inventory_contract_is_counted_and_not_graded_never_becomes_a_row(
        tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """v3's single answer: ``passages`` and ``not_graded`` are counted (a not_graded slug with a row keeps the row,
    an unknown one is a repair), never rows; the same answer under v1 is post-processed exactly as before (no
    counts, no repairs from the bookkeeping keys)."""
    r = Router()
    r.on("single", "DOC_S", reply=_inventory_reply())
    w = _world(tmp_path, monkeypatch, [SEG_TEXT], {"mode": "single", "name": name}, r)
    rows, _, _ = _run(w)
    row = rows[w["ids"][0]]
    assert {t["slug"] for t in row["techniques"]} == {"llm-based-safety-classifier", "safety-sft-refusal-training"}
    assert "rlhf" not in {t["slug"] for t in row["techniques"]} and row["extra_techniques"] == []
    counts = _counts(row)
    inv = {k: v for k, v in counts.items() if k.startswith("inv_")}
    sent = r.payloads[0]["response_format"]["json_schema"]["schema"]["properties"]
    assert ("not_graded" in sent, "passages" in sent["sweeps"]["properties"]) == (name == V3,) * 2
    if name == V3:
        assert r.texts("single")[0].rstrip().endswith(V3_REMINDER)
        assert inv == {"inv_passages": 3, "inv_passage_slugs": 4, "inv_not_graded": 2, "inv_unaccounted": 1,
                       "inv_not_graded_with_row": 1}  # chain-of-thought-monitoring listed, neither row nor not_graded
        assert {"not_graded_with_row:safety-sft-refusal-training", "bad_not_graded"} <= set(row["repairs"])
    else:
        assert inv == {}
        assert not any(x.startswith(("not_graded", "bad_not_graded")) for x in row["repairs"])
    _green(w["store"])


def _long_doc(tag: str) -> str:
    sec = [f"## Part {k}\n\n" + "\n\n".join(("We deploy Llama Guard on all outputs of the production assistant. "
                                               if (k, j) == (0, 0) else "") + f"Filler {k}.{j} " + "text " * 300
                                              for j in range(8)) for k in range(14)]
    return f"# {tag} A long report\n\n" + "\n\n".join(sec) + (
        "\n\n## Late section\n\nDevelopers should monitor the reasoning traces of deployed agents.\n")


@pytest.mark.slow
def test_windowed_single_reads_long_bodies_in_parts_and_merges_them(tmp_path: pathlib.Path,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    """D614: a body over WINDOW_MIN -> one window call per part (system part single-v2's), the answers merged by
    slug with quotes anchored against the whole text; a short document -> the plain v3 single call."""
    r = Router()
    r.on("window", "DOC_W", "PART 1 OF", reply=g_reply(
        g_row("llm-based-safety-classifier", {"mentioned": 1, "used": 1}, GUARD_Q)))
    r.on("window", "DOC_W", reply=g_reply(
        g_row("llm-based-safety-classifier", {"mentioned": 2, "attested_use": 2}, GUARD_Q),
        g_row("chain-of-thought-monitoring", {"considered": 1}, COT_Q)))
    r.on("single", "DOC_S", reply=g_reply(g_row("llm-based-safety-classifier", {"used": 1}, GUARD_Q)))
    long_text = _long_doc("DOC_W")
    w = _world(tmp_path, monkeypatch, [long_text, SEG_TEXT], {"mode": "single", "name": V3, "window": True}, r)
    rows, ev, _ = _run(w)
    long_row, short_row = rows[w["ids"][0]], rows[w["ids"][1]]
    n_win = r.count("window")
    assert n_win >= 2 and r.count("single") == 1
    seg = main.segment.segment(long_text)
    assert n_win == len(main.segment.windows(seg, single_max=main.WINDOW_MIN, target=main.WINDOW_TARGET,
                                             cap=main.WINDOW_MAX))
    first = r.texts("window")[0]
    assert "# OUTLINE (all section headings; this part covers: " in first and "Late section" in first
    systems = {json.dumps(pl["messages"][0]) for pl in r.payloads}
    assert len(systems) == 1  # every window call and the short document's single call: one cached system part
    t = {x["slug"]: x for x in long_row["techniques"]}
    nonzero = {k: v for k, v in t["llm-based-safety-classifier"]["roles"].items() if v}
    assert nonzero == {"mentioned": 2, "used": 1, "attested_use": 2}
    assert "chain-of-thought-monitoring" in t and all(q["verified"] for x in t.values() for q in x["quotes"])
    assert _counts(long_row)["windows"] == n_win and "windows" not in _counts(short_row)
    assert [c["stage"] for c in long_row["prepass"]["calls"]] == ["window"] * n_win
    assert r.texts("single")[0].rstrip().endswith(V3_REMINDER)
    _green(w["store"])


@pytest.mark.slow
def test_gleaning_turn_continues_the_conversation_and_adds_only_new_slugs(tmp_path: pathlib.Path,
                                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    """D615: pass 2 is system + pass-1 user part + assistant(pass-1 JSON) + the tail; its new slugs are added
    (repair ``glean_added:<slug>``), a slug pass 1 graded keeps pass 1's row."""
    r = Router()
    r.on("single", "DOC_S", reply=g_reply(g_row("llm-based-safety-classifier", {"used": 1}, GUARD_Q)))
    r.on("glean", "DOC_S", reply=ok({"techniques": [
        g_row("llm-based-safety-classifier", {"used": 4}, GUARD_Q),
        g_row("chain-of-thought-monitoring", {"considered": 1}, COT_Q)], "extra_techniques": []}, cost=0.001))
    w = _world(tmp_path, monkeypatch, [SEG_TEXT], {"mode": "single", "name": V3, "glean": True}, r)
    rows, ev, _ = _run(w)
    row = rows[w["ids"][0]]
    assert [c for c, _ in r.calls] == ["single", "glean"]
    msgs = r.payloads[1]["messages"]
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user"]
    assert msgs[1]["content"] == r.payloads[0]["messages"][1]["content"]  # the pass-1 user part, byte for byte
    assert json.loads(msgs[2]["content"])["techniques"][0]["slug"] == "llm-based-safety-classifier"
    assert msgs[3]["content"].startswith("Second pass. Your answer is above")
    t = {x["slug"]: x for x in row["techniques"]}
    assert t["llm-based-safety-classifier"]["roles"]["used"] == 1  # pass 1 wins
    assert "chain-of-thought-monitoring" in t
    assert "glean_added:chain-of-thought-monitoring" in row["repairs"]
    assert "glean_added:llm-based-safety-classifier" not in row["repairs"]
    assert _counts(row)["glean_added"] == 1
    assert ev[w["ids"][0]]["cost_usd"] == pytest.approx(0.005)
    _green(w["store"])


def test_sector_turns_continue_the_conversation_after_gleaning(tmp_path: pathlib.Path,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    """The pin key ``sectors`` (v6-s1): every sector turn is system + pass-1 user part + pass-1 answer + the gleaning tail +
    the gleaning answer + its sector tail (one shared prefix); new slugs are added (repair ``sector_added:<slug>``), a
    slug an earlier turn graded keeps that row, and a failed sector turn is skipped (``sector_failed:<id>:<class>``)."""
    from tests.fakes.llm import server_error

    cat = main.catalogue(main.REPO / "taxonomy" / "techniques-1.0.0.yaml")
    sec = main.sectors_of(S1, cat)
    guard = next(x.id for x in sec.sectors if "llm-based-safety-classifier" in x.slugs)
    cot = next(x.id for x in sec.sectors if "chain-of-thought-monitoring" in x.slugs)
    refusal = next(x.id for x in sec.sectors if "safety-sft-refusal-training" in x.slugs)
    fails = next(x.id for x in sec.sectors if x.id not in (guard, cot, refusal))
    title = {x.id: x.title for x in sec.sectors}
    r = Router()
    r.on("single", "DOC_S", reply=g_reply(g_row("llm-based-safety-classifier", {"used": 1}, GUARD_Q)))
    r.on("glean", "DOC_S", reply=ok({"techniques": [g_row("chain-of-thought-monitoring", {"considered": 1}, COT_Q)],
                                     "extra_techniques": []}, cost=0.001))
    r.on("sector", "DOC_S", f": {title[guard]} (", reply=ok({"techniques": [
        g_row("llm-based-safety-classifier", {"used": 4}, GUARD_Q)], "extra_techniques": []}, cost=0.0005))
    r.on("sector", "DOC_S", f": {title[refusal]} (", reply=ok({"techniques": [
        g_row("safety-sft-refusal-training", {"used": 2}, "We fine-tune the model with refusal training")],
        "extra_techniques": []}, cost=0.0005))
    r.on("sector", "DOC_S", f": {title[fails]} (", reply=server_error(503))
    r.on("sector", "DOC_S", reply=ok({"techniques": [], "extra_techniques": []}, cost=0.0005))
    tax = "taxonomy/techniques-1.0.0.yaml"
    w = _world(tmp_path, monkeypatch, [SEG_TEXT], {"mode": "single", "name": S1, "glean": True, "sectors": True,
                                                    "taxonomy": tax}, r)
    rows, _ev, rec = _run(w)
    row = rows[w["ids"][0]]
    assert [c for c, _ in r.calls][:2] == ["single", "glean"]
    assert r.count("sector") >= len(sec.sectors)  # the failing turn is retried by the client before it gives up
    first, glean_msgs = r.payloads[0]["messages"], r.payloads[1]["messages"]
    sector_payloads = [pl for (c, _), pl in zip(r.calls, r.payloads, strict=True) if c == "sector"]
    for pl in sector_payloads:
        msgs = pl["messages"]
        assert msgs[:len(glean_msgs)] == glean_msgs  # the gleaning call's prompt whole, then its answer
        assert msgs[len(glean_msgs)]["role"] == "assistant"
        assert json.loads(msgs[len(glean_msgs)]["content"])["techniques"][0]["slug"] == "chain-of-thought-monitoring"
        assert msgs[-1]["role"] == "user" and msgs[-1]["content"].startswith("Sector pass ")
        assert msgs[1]["content"] == first[1]["content"]
    t = {x["slug"]: x for x in row["techniques"]}
    assert t["llm-based-safety-classifier"]["roles"]["used"] == 1  # pass 1 wins
    assert set(t) == {"llm-based-safety-classifier", "chain-of-thought-monitoring", "safety-sft-refusal-training"}
    assert "sector_added:safety-sft-refusal-training" in row["repairs"]
    assert not any(x.startswith("sector_added:llm-based") for x in row["repairs"])
    assert f"sector_failed:{fails}:http_5xx" in row["repairs"]
    c = _counts(row)
    assert c["sector_added"] == 1 and c["sector_failed"] == 1 and c["glean_added"] == 1
    assert row["status"] == "ok" and "partial" not in row["flags"]
    assert rec.counts["calls.sector"] == len(sec.sectors) - 1  # answered turns (the failed one has no result)
    _green(w["store"])


@pytest.mark.slow
def test_production_pin_pilot_then_full_run_carries_the_pilot(tmp_path: pathlib.Path,
                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    """D710 on plan batch-2026-10a's pin: a run under ``max_rows`` asks the first rows in doc_id order and ends
    ``scope_complete = false`` with reason ``limit``; a run of the same pin without ``max_rows`` carries them (same
    config digest, so the same work keys) and asks only the rest."""
    # Red under: max_rows entering the config digest, the limit stop uncounted (reason `unspecified`), or the
    # production pin not loading (prompt set, taxonomy path, pin keys).
    from stack.build import incomplete_reasons
    from stack.plan import load_plan

    pin = load_plan(REPO / "configs" / "plans" / "batch-2026-10a.yaml").prompts["annotate.technique_staged"]
    marks = ("DOC_S", "DOC_T", "DOC_U")
    r = Router()
    for m in marks:
        r.on("single", m, reply=g_reply(g_row("llm-based-safety-classifier", {"used": 1}, GUARD_Q)))
        r.on("glean", m, reply=ok({"techniques": [], "extra_techniques": []}, cost=0.001))
    texts = [SEG_TEXT.replace("DOC_S", m) for m in marks]
    w = _world(tmp_path / "pilot", monkeypatch, texts, pin, r, options={"max_rows": 2})
    rows, _ev, rec = _run(w)
    by_order = sorted(w["ids"])
    assert [rows[d]["status"] for d in by_order] == ["ok", "ok", "not_attempted"]
    assert rows[by_order[2]]["error_class"] == "limit" and not rec.scope_complete
    assert rec.counts["not_attempted.limit"] == 1 and incomplete_reasons(rec.counts) == ["limit"]
    assert r.count("single") == 2 and r.count("glean") == 2
    # the production roles call the OpenAI API directly (D714): its output cap is max_completion_tokens
    assert [pl["max_completion_tokens"] for pl in r.payloads] == [64_000] * 4
    full = write_plan_file(tmp_path / "pilot", "techstaged-full", prompts={"annotate.technique_staged": pin},
                           stages=[{"stage": "fold.corpus"},
                                   {"stage": "annotate.technique_staged",
                                    "prior": ["technique_staged", "technique_staged_evidence"]},
                                   {"stage": "fold.build"}], folded=["technique_staged"])
    assert full.prompts["annotate.technique_staged"] == w["plan"].prompts["annotate.technique_staged"]
    corpus = fold_corpus(w["store"], full)
    prior = [rec.outputs[f]["path"] for f in ("technique_staged", "technique_staged_evidence")]
    rec2 = run_stage(w["store"], full, "annotate.technique_staged", [corpus, *prior], series=SERIES)
    assert rec2.status == "ok" and rec2.scope_complete, rec2.error
    assert rec2.counts["carried"] == 2 and rec2.counts["asked"] == 1 and "not_attempted.limit" not in rec2.counts
    assert [r.count("single", m) for m in marks] == [1, 1, 1]  # each document asked once over both runs
    rows2 = {x["doc_id"]: x for x in rows_of(w["store"], rec2, "technique_staged")}
    assert all(rows2[d]["status"] == "ok" for d in by_order)
    assert {rows2[d]["config_digest"] for d in by_order} == {rows[by_order[0]]["config_digest"]}
    _green(w["store"])


@pytest.mark.slow
def test_a_failed_gleaning_turn_keeps_the_first_answer(tmp_path: pathlib.Path,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    """A gleaning turn that fails as one call (HTTP 5xx until the client gives up; a schema-invalid reply) leaves
    the first answer as the document's, marked ``glean_failed:<error_class>`` and counted; the failed attempts'
    cost stays on the evidence row, and the document is ``ok``, not a failure."""
    from tests.fakes.llm import server_error

    r = Router()
    r.on("single", "DOC_S", reply=g_reply(g_row("llm-based-safety-classifier", {"used": 1}, GUARD_Q)))
    r.on("glean", "DOC_S", reply=server_error(503))
    r.on("single", "DOC_T", reply=g_reply(g_row("llm-based-safety-classifier", {"used": 2}, GUARD_Q)))
    r.on("glean", "DOC_T", reply=schema_invalid({"techniques": "nope"}, cost=0.0005))
    texts = [SEG_TEXT, SEG_TEXT.replace("DOC_S", "DOC_T")]
    w = _world(tmp_path, monkeypatch, texts, {"mode": "single", "name": V3, "glean": True}, r)
    rows, ev, rec = _run(w)
    s_row, t_row = rows[w["ids"][0]], rows[w["ids"][1]]
    assert r.count("glean", "DOC_S") >= 2 and r.count("glean", "DOC_T") >= 1  # the client retried before giving up
    for row, e, cls in ((s_row, ev[w["ids"][0]], "http_5xx"), (t_row, ev[w["ids"][1]], "schema_invalid")):
        assert row["status"] == "ok" and e["status"] == "ok"
        assert [t["slug"] for t in row["techniques"]] == ["llm-based-safety-classifier"]
        assert f"glean_failed:{cls}" in row["repairs"] and not any(x.startswith("glean_added:") for x in row["repairs"])
        assert _counts(row)["glean_failed"] == 1 and "glean_added" not in _counts(row)
        assert "partial" not in row["flags"]
    assert ev[w["ids"][1]]["cost_usd"] == pytest.approx(0.004 + 0.0005 * r.count("glean", "DOC_T"))
    assert rec.counts["glean_failed"] == 2
    _green(w["store"])


@pytest.mark.parametrize(("extra", "caps"), [({}, [16_000, 32_000, 16_000]),
                                             ({"max_tokens": 64_000}, [64_000, 128_000, 64_000])])
def test_max_tokens_pin_reaches_every_call_and_its_truncation_retry(tmp_path: pathlib.Path,
                                                                    monkeypatch: pytest.MonkeyPatch,
                                                                    extra: dict[str, Any], caps: list[int]) -> None:
    """The pin's ``max_tokens`` is the wire cap of the single call and of its gleaning turn; a reply cut off at it
    is retried once at twice it (the client's rule), so 64,000 retries at 128,000."""
    # Red under: a call still sending MAX_TOKENS, or the gleaning turn not reading the pin.
    from tests.fakes.llm import truncated

    r = Router()
    r.on("single", "DOC_S", reply=[truncated('{"doc_kind": "paper", "techniques": [', completion_tokens=caps[0]),
                                   g_reply(g_row("llm-based-safety-classifier", {"used": 1}, GUARD_Q))])
    r.on("glean", "DOC_S", reply=ok({"techniques": [], "extra_techniques": []}, cost=0.001))
    w = _world(tmp_path, monkeypatch, [SEG_TEXT], {"mode": "single", "name": V3, "glean": True, **extra}, r)
    rows, _ev, _ = _run(w)
    assert [c for c, _ in r.calls] == ["single", "single", "glean"]
    assert [pl["max_tokens"] for pl in r.payloads] == caps
    assert [t["slug"] for t in rows[w["ids"][0]]["techniques"]] == ["llm-based-safety-classifier"]
    _green(w["store"])


def test_gleaning_fallback_covers_only_one_call_s_failures() -> None:
    """Budget refusals, fatal transport errors (auth, the run-wide breaker), egress and config errors are the run's,
    not the gleaning call's: they propagate as from any other call."""
    from stack.llm import BudgetExceeded, EgressRefused, FatalLlmError, MissingRef

    for exc in (BudgetExceeded, FatalLlmError, EgressRefused, MissingRef):
        assert not issubclass(exc, main.GLEAN_FALLBACK_ERRORS), exc


def test_gleaning_after_a_degraded_call_repeats_the_schema_turn(tmp_path: pathlib.Path,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    """When the first call was degraded (the provider refused the strict schema), the gleaning prompt starts with
    the first call's whole degraded prompt (user part + the folded schema turn), so the provider cache can serve it."""
    from stack.llm import fold_message
    from tests.fakes.llm import server_error

    class Refusing(Router):
        def post(self, request: LlmRequest) -> LlmResponse:
            if "response_format" in request.payload:
                self.payloads.append(request.payload)
                return server_error(400)
            return super().post(request)

    r = Refusing()
    r.on("single", "DOC_S", reply=g_reply(g_row("llm-based-safety-classifier", {"used": 1}, GUARD_Q)))
    r.on("glean", "DOC_S", reply=ok({"techniques": [], "extra_techniques": []}, cost=0.001))
    w = _world(tmp_path, monkeypatch, [SEG_TEXT], {"mode": "single", "name": V3, "glean": True}, r)
    _run(w)
    sent = [pl for pl in r.payloads if "response_format" not in pl]
    first, second = sent[0]["messages"], sent[1]["messages"]
    assert first[-1] == fold_message(main.INVENTORY_SCHEMA)
    assert second[:len(first)] == first  # the whole first prompt is the second one's prefix
    assert second[len(first)]["role"] == "assistant" and second[-1] == fold_message(main.GLEAN_SCHEMA)


def test_lexical_mode_grades_matcher_hits_and_an_empty_list_still_gets_the_call(
        tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """C1: with no lexical hit the grading call still runs, with ``(none)`` as the candidates."""
    r = Router()
    r.on("grade", "DOC_L", reply=g_reply(g_row("llm-based-safety-classifier", {"used": 1}, GUARD_Q)))
    r.on("grade", "DOC_N", reply=g_reply())
    texts = [SEG_TEXT.replace("DOC_S", "DOC_L"), "# DOC_N\n\nA recipe for sourdough bread with rye flour."]
    w = _world(tmp_path, monkeypatch, texts, {"mode": "lexical"}, r)
    rows, _, _ = _run(w)
    a, b = rows[w["ids"][0]], rows[w["ids"][1]]
    assert [c for c, _ in r.calls] == ["grade", "grade"]
    assert "## llm-based-safety-classifier — Guard models" in r.texts("grade", "DOC_L")[0]
    assert r.texts("grade", "DOC_N")[0].rstrip().endswith("# THE CANDIDATES (0)\n\n(none)\n\nNow emit the JSON "
                                                          "object described above, in a ```json fenced block, and "
                                                          "nothing else.")
    assert a["status"] == "ok" and a["prepass"]["mentions"] == [] and "llm-based-safety-classifier" in a["prepass"]["submenu"]
    assert (b["status"], b["flags"], b["prepass"]["submenu"]) == ("empty", ["no_techniques_found"], [])
    assert r.texts("grade", "DOC_L")[0].endswith(main.user_message(texts[0], mode="lexical"))  # deterministic
    _green(w["store"])


def test_staged_with_no_notes_and_no_lexical_hits_still_grades(tmp_path: pathlib.Path,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    r = Router()
    r.on("prepass", "DOC_Z", reply=p_reply())
    r.on("grade", "DOC_Z", reply=g_reply(g_row("tool-use-agent-execution-sandboxing", {"used": 1},
                                               "We ran each agent inside a container")))
    w = _world(tmp_path, monkeypatch, ["# DOC_Z\n\nWe ran each agent inside a container with no network."],
               {"mode": "staged"}, r)
    rows, _, _ = _run(w)
    row = rows[w["ids"][0]]
    assert [c for c, _ in r.calls] == ["prepass", "grade"]  # no notes: no S call
    assert row["status"] == "ok" and "outside_submenu:tool-use-agent-execution-sandboxing" in row["repairs"]
    assert row["prepass"]["submenu"] == []


LONG = "# DOC_P A long study\n\n" + "\n\n".join(
    f"Paragraph {i}: we deploy Llama Guard on outputs and also discuss many other things at length." for i in range(1, 9))


@pytest.mark.slow
def test_a_failing_chunk_is_split_once_and_a_failing_half_flags_partial(tmp_path: pathlib.Path,
                                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    """P on the whole part is schema-invalid (the client's attempts), so it is split; the first half
    answers, the second fails again: ``prepass_incomplete`` + ``partial`` with the graded rows kept;
    the failed calls' cost is on the evidence row. A second document whose halves both fail and whose
    grading finds nothing is a failure, not an empty result."""
    note = {"paras": [2], "surface": "Llama Guard", "gloss": "guard model", "stance_hint": "used", "implicit": False}
    r = Router()
    bad = schema_invalid({"mentions": "nope"}, cost=0.0005)
    r.on("prepass", "DOC_P", "Part 1 of 1", reply=bad)
    r.on("prepass", "DOC_P", "Part 1 (first half) of 1", reply=p_reply(note))
    r.on("prepass", "DOC_P", "Part 1 (second half) of 1", reply=bad)
    r.on("select", "DOC_P", reply=s_reply(s_answer("m1", ("llm-based-safety-classifier", "high"))))
    r.on("grade", "DOC_P", reply=g_reply(g_row("llm-based-safety-classifier", {"used": 3},
                                               "Paragraph 1: we deploy Llama Guard on outputs")))
    r.on("prepass", "DOC_Q", reply=bad)
    r.on("grade", "DOC_Q", reply=g_reply())
    texts = [LONG, LONG.replace("DOC_P", "DOC_Q")]
    w = _world(tmp_path, monkeypatch, texts, {"mode": "staged"}, r)
    rows, ev, rec = _run(w)
    row, e = rows[w["ids"][0]], ev[w["ids"][0]]
    assert row["status"] == "ok" and row["flags"] == ["prepass_incomplete", "partial"]
    assert {"prepass_split:1:schema_invalid", "prepass_failed:1b:schema_invalid"} <= set(row["repairs"])
    assert [m["chunk"] for m in row["prepass"]["mentions"]] == ["1a"]
    n_bad = r.count("prepass", "DOC_P") - 1  # every P attempt but the first half's answer was the bad reply
    assert n_bad >= 2
    assert e["cost_usd"] == pytest.approx(0.0005 * n_bad + 0.001 + 0.002 + 0.004)
    assert _counts(row)["p_splits"] == 1 and rec.counts["partial_docs"] == 1
    q = ev[w["ids"][1]]
    assert (q["status"], q["error_class"]) == ("failed_permanent", "schema_invalid")
    assert "incomplete" in q["error"] and q["cost_usd"] == pytest.approx(0.0005 * r.count("prepass", "DOC_Q") + 0.004)
    assert rows[w["ids"][1]]["status"] == "failed_permanent"
    _green(w["store"])


def test_an_overflowing_chunk_is_split_and_reread_completely(tmp_path: pathlib.Path,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    note = {"paras": [2], "surface": "Llama Guard", "gloss": "g", "stance_hint": "used", "implicit": False}
    r = Router()
    r.on("prepass", "DOC_O", "Part 1 of 1", reply=p_reply(note, overflow=True))
    r.on("prepass", "DOC_O", "(first half)", reply=p_reply(note))
    r.on("prepass", "DOC_O", "(second half)", reply=p_reply())
    r.on("sweep", "DOC_O", reply=g_reply(g_row("llm-based-safety-classifier", {"used": 3},
                                               "Paragraph 1: we deploy Llama Guard on outputs")))
    w = _world(tmp_path, monkeypatch, [LONG.replace("DOC_P", "DOC_O")], {"mode": "sweep"}, r)
    rows, _, _ = _run(w)
    row = rows[w["ids"][0]]
    assert row["flags"] == [] and "prepass_split:1:overflow" in row["repairs"]
    assert [m["chunk"] for m in row["prepass"]["mentions"]] == ["1a"]  # the overflowing answer is not kept
    second = r.texts("prepass", "(second half)")[0]
    assert "# DOCUMENT OPENING (context only)" in second and "# DOCUMENT OPENING" not in r.texts("prepass",
                                                                                                "(first half)")[0]


def test_selection_reasks_missing_ids_once_then_flags_incomplete(tmp_path: pathlib.Path,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    notes = [{"paras": [2], "surface": "Llama Guard", "gloss": "g", "stance_hint": "used", "implicit": False},
             {"paras": [4], "surface": "refusal training", "gloss": "g", "stance_hint": "used", "implicit": False}]
    r = Router()
    for doc in ("DOC_R", "DOC_U"):
        r.on("prepass", doc, reply=p_reply(*notes))
        r.on("select", doc, "a re-ask of mentions",
             reply=s_reply(s_answer("m2", ("safety-sft-refusal-training", "high"))) if doc == "DOC_R" else s_reply())
        r.on("select", doc, reply=s_reply(s_answer("m1", ("llm-based-safety-classifier", "high"))))
        r.on("grade", doc, reply=g_reply(g_row("llm-based-safety-classifier", {"used": 2}, GUARD_Q)))
    texts = [SEG_TEXT.replace("DOC_S", d) for d in ("DOC_R", "DOC_U")]
    w = _world(tmp_path, monkeypatch, texts, {"mode": "staged"}, r)
    rows, _, _ = _run(w)
    ok_row, part = rows[w["ids"][0]], rows[w["ids"][1]]
    assert r.count("select", "DOC_R") == 2 and r.count("select", "DOC_U") == 2  # one re-ask each
    reask = r.texts("select", "a re-ask of mentions")[0]
    assert "# MENTIONS (1 mentions; a re-ask of mentions the previous answer left out: leave doc_level empty)" in reask
    assert 'm2 | "refusal training"' in reask and 'm1 | "Llama Guard"' not in reask
    assert ok_row["flags"] == [] and "select_reask:1" in ok_row["repairs"]
    assert ok_row["prepass"]["mentions"][1]["candidates"] == [{"slug": "safety-sft-refusal-training",
                                                              "confidence": "high"}]
    assert part["flags"] == ["select_incomplete", "partial"] and "select_missing:m2" in part["repairs"]
    assert part["status"] == "ok" and _counts(part)["s_calls"] == 2


def test_an_answer_whose_every_row_has_empty_roles_fails_with_the_total_cost(tmp_path: pathlib.Path,
                                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    r = Router()
    r.on("prepass", "DOC_H", reply=p_reply())
    r.on("sweep", "DOC_H", reply=g_reply(g_row("llm-based-safety-classifier", {}, GUARD_Q)))
    w = _world(tmp_path, monkeypatch, [SEG_TEXT.replace("DOC_S", "DOC_H")], {"mode": "sweep"}, r)
    _rows, ev, _ = _run(w)
    e = ev[w["ids"][0]]
    assert (e["status"], e["error_class"]) == ("failed_permanent", "schema_invalid")
    assert e["cost_usd"] == pytest.approx(0.005)
