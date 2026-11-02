"""``annotate.model_roles`` (round 5 T-ANN, D235): the judge parse/repair core, the prompt
renderer, and the stage on a synthetic world with the fake model.

Ported from v3 ``tests/test_annotate_model_roles.py`` (``tomas/model-roles``, d011e5e), the
DB-free parts: fence extraction, each post-processing clamp the rubric promises and the
calibration measured judges dropping, the quote re-find check, the menu builder and window, the
renderer. Every repaired payload is also mapped onto the stored ``MODEL_ROLES`` type
(:func:`judge.to_payload`) and built as an arrow value: repair is only worth anything if what
comes out is writable. Not ported: the two-judge merge (v4 runs single-judge mode only), v3's
registration and llm-role checks (``configs/workflows.yaml``), and the DB loop's concurrency
tests (the v4 framework owns workers). New here: the v4 stage (menu render, empty-menu no-call,
payload shape, repair flags, provenance in the config digest).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import pathlib
import re
from typing import Any

import pytest

import pyarrow as pa

from stack.modelvocab import VocabNode, load_vocab
from stack.schemas.payloads.model_roles import ROLE_NAMES
from stack.schemas.payloads.registry import REGISTRY

REPO = pathlib.Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO / "configs"
WORKFLOWS_DIR = REPO / "workflows"
WF_DIR = WORKFLOWS_DIR / "annotate" / "model_roles"


def _load_module(rel: str, name: str) -> Any:
    """By-path import, mirroring the runner's dispatch: NOT registered in ``sys.modules`` (v4's
    ``render.MenuEntry`` is a ``NamedTuple``, so nothing needs the registration v3 required)."""
    spec = importlib.util.spec_from_file_location(name, WORKFLOWS_DIR / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


judge = _load_module("annotate/model_roles/judge.py", "_model_roles_judge_under_test")
render = _load_module("annotate/model_roles/render.py", "_model_roles_render_under_test")

DOC = (
    "# Safety tuning of open models\n\n"
    "We fine-tune Llama 3.1 70B with LoRA on the safety split and evaluate refusal rates.\n"
    "Responses were scored by GPT-4.5 acting as judge, following prior work.\n"
    "Assistants such as ChatGPT and Gemini are discussed only in passing.\n\n"
    "## References\n\n"
    "Taori et al. Alpaca 7B: an instruction-following model. 2023.\n"
)

MENU = ["llama-3.1-70b", "gpt-4.5", "gemini", "alpaca-7b"]
NAMES = {
    "llama-3.1-70b": "Llama 3.1 70B",
    "gpt-4.5": "GPT-4.5",
    "gemini": "Gemini",
    "alpaca-7b": "Alpaca 7B",
}


def normalize(obj: dict[str, Any], *, text: str = DOC) -> dict[str, Any]:
    return dict(
        judge.normalize(obj, menu_nodes=MENU, menu_names=NAMES, text=text)
    )


def validated(payload: dict[str, Any]) -> dict[str, Any]:
    """Every payload this module asserts on must also be WRITABLE: mapped onto ``MODEL_ROLES``
    (every role an explicit integer 0-4) and built as an arrow value of the registry type."""
    stored = judge.to_payload(payload)
    pa.array([stored], type=REGISTRY["model_roles"].payload_schema)
    for row in [*stored["models"], *stored["variant_models"], *stored["extra_models"]]:
        assert list(row["roles"]) == list(ROLE_NAMES) and all(0 <= v <= 4 for v in row["roles"].values())
    return stored


# ─────────────────────────── (a) fence extraction ──────────────────────────


def test_extracts_json_from_a_fenced_block_with_prose_around_it() -> None:
    """The production prompt asks for a bare fenced object; judges wrap it anyway."""
    raw = (
        "Here is my grading. I checked each menu entry against the text.\n\n"
        '```json\n{"doc_kind": "paper", "models": []}\n```\n\n'
        "Let me know if you want the reasoning."
    )
    assert judge.extract_json(raw) == {"doc_kind": "paper", "models": []}


def test_extracts_a_bare_object_with_no_fence() -> None:
    raw = 'Grading:\n{"doc_kind": "blogpost", "models": []}\nDone.'
    assert judge.extract_json(raw)["doc_kind"] == "blogpost"


def test_repairs_a_trailing_comma() -> None:
    """Measured once in the 110-output round-2 calibration (luna/v2)."""
    raw = '```json\n{"doc_kind": "paper", "models": [{"node_id": "gemini", "roles": {}},],}\n```'
    assert judge.extract_json(raw)["models"] == [{"node_id": "gemini", "roles": {}}]


def test_unparseable_response_raises_rather_than_grading_empty() -> None:
    for raw in ("", "I could not complete this task.", '```json\n{"doc_kind": "pap\n```'):
        with pytest.raises(judge.JudgeParseError):
            judge.extract_json(raw)


def test_a_json_array_is_not_a_payload() -> None:
    with pytest.raises(judge.JudgeParseError):
        judge.extract_json('```json\n[{"node_id": "gemini"}]\n```')


# ─────────────────────────── (b) the clamps ────────────────────────────────


def test_mentioned_is_clamped_up_to_the_max_of_the_other_roles() -> None:
    """Rubric §3.1's invariant — the one every calibration arm dropped somewhere."""
    payload = normalize({
        "doc_kind": "paper",
        "models": [{"node_id": "llama-3.1-70b", "roles": {"mentioned": 1, "modified": 4}}],
    })
    row = payload["models"][0]
    assert row["roles"] == {"mentioned": 4, "modified": 4}  # and in rubric §3 order
    assert "mentioned_clamped:llama-3.1-70b:1->4" in payload["repairs"]
    validated(payload)


def test_a_clean_row_is_not_clamped_and_records_no_repair() -> None:
    payload = normalize({
        "doc_kind": "paper",
        "models": [{"node_id": "gpt-4.5", "roles": {"mentioned": 3, "instrument": 2}}],
    })
    assert payload["models"][1]["roles"] == {"mentioned": 3, "instrument": 2}
    assert not [r for r in payload["repairs"] if r.startswith("mentioned_clamped")]


def test_unknown_roles_and_out_of_range_strengths_are_dropped_and_recorded() -> None:
    payload = normalize({
        "doc_kind": "paper",
        "models": [{
            "node_id": "gemini",
            "roles": {"mentioned": 2, "vibes": 3, "discussed": 9, "instrument": "2",
                      "cited_work": 0},
        }],
    })
    row = next(r for r in payload["models"] if r["node_id"] == "gemini")
    assert row["roles"] == {"mentioned": 2}
    assert "unknown_role:gemini:vibes" in payload["repairs"]
    assert "bad_strength:gemini:discussed:9" in payload["repairs"]
    assert "bad_strength:gemini:instrument:2" in payload["repairs"]
    assert not [r for r in payload["repairs"] if "cited_work" in r]  # 0 = omission, not an error
    validated(payload)


def test_unknown_doc_kind_falls_back_to_other() -> None:
    payload = normalize({"doc_kind": "manifesto", "models": []})
    assert payload["doc_kind"] == "other"
    assert "doc_kind_unknown:manifesto" in payload["repairs"]


def test_quotes_are_capped_and_nul_stripped() -> None:
    long_quote = "We fine-tune Llama 3.1 70B with LoRA on the safety split" + "x" * 400
    payload = normalize({
        "doc_kind": "paper",
        "models": [{
            "node_id": "llama-3.1-70b",
            "roles": {"modified": 4},
            "evidence": [{"roles": ["modified"], "quote": "\x00" + long_quote}],
        }],
    })
    quote = payload["models"][0]["evidence"][0]["quote"]
    assert len(quote) == judge.MAX_QUOTE_CHARS and "\x00" not in quote
    assert "quote_truncated:llama-3.1-70b" in payload["repairs"]
    assert "nul_stripped" in payload["repairs"]
    validated(payload)


# ─────────────────────────── (c) quote verification ────────────────────────


def test_a_real_quote_verifies_across_whitespace_and_entities() -> None:
    text = "We fine-tune  Llama&nbsp;3.1 70B\nwith LoRA &amp; evaluate refusal rates."
    payload = normalize(
        {
            "doc_kind": "paper",
            "models": [{
                "node_id": "llama-3.1-70b",
                "roles": {"modified": 3},
                "evidence": [{"roles": ["modified"], "quote": "Llama 3.1 70B with LoRA & evaluate"}],
            }],
        },
        text=text,
    )
    assert not [r for r in payload["repairs"] if r.startswith("quote_unverified")]


def test_a_fabricated_quote_is_kept_but_marked() -> None:
    """Deleting it would hide exactly what the check exists to expose (deepseek/v2, doc 06)."""
    payload = normalize({
        "doc_kind": "paper",
        "models": [{
            "node_id": "llama-3.1-70b",
            "roles": {"modified": 4},
            "evidence": [{"roles": ["modified"],
                          "quote": "We fine-tune LLaMA-30B and evaluate 69 distribution shifts."}],
        }],
    })
    assert payload["models"][0]["evidence"][0]["quote"].startswith("We fine-tune LLaMA-30B")
    assert "quote_unverified:llama-3.1-70b" in payload["repairs"]


def test_an_elided_quote_verifies_from_its_segments() -> None:
    payload = normalize({
        "doc_kind": "paper",
        "models": [{
            "node_id": "llama-3.1-70b",
            "roles": {"modified": 3},
            "evidence": [{"roles": ["modified"],
                          "quote": "We fine-tune Llama 3.1 70B with LoRA … evaluate refusal rates"}],
        }],
    })
    assert not [r for r in payload["repairs"] if r.startswith("quote_unverified")]


def test_a_quote_survives_converter_artifacts_the_judge_silently_repaired() -> None:
    """Measured on the round-2 corpus: docling markdown escapes, and PDF-lost word spaces."""
    text = r"Wefirst evaluate LLaMA-30B on ranking\_logic\_hard with LoRA as the intervention."
    norm = judge._normalize(text)
    assert judge._found_in("We first evaluate LLaMA-30B on ranking_logic_hard with LoRA", norm)
    assert not judge._found_in("We evaluate LLaMA-30B on 69 distribution shifts with LoRA", norm)


def test_an_elision_of_two_short_fragments_does_not_verify() -> None:
    """Short fragments match anywhere; an elision check that accepts them checks nothing."""
    assert not judge._found_in("the … we", judge._normalize(DOC))


# ─────────────────────────── (d) rows and dedup ────────────────────────────


def test_every_menu_node_gets_a_row_in_menu_order() -> None:
    payload = normalize({"doc_kind": "paper", "models": [{"node_id": "gemini",
                                                          "roles": {"mentioned": 1}}]})
    assert [r["node_id"] for r in payload["models"]] == MENU
    assert {r for r in payload["repairs"] if r.startswith("row_missing")} == {
        "row_missing:llama-3.1-70b", "row_missing:gpt-4.5", "row_missing:alpaca-7b"
    }
    assert all(r["roles"] == {} for r in payload["models"] if r["node_id"] != "gemini")
    validated(payload)


def test_a_duplicate_row_is_dropped_and_the_first_wins() -> None:
    payload = normalize({
        "doc_kind": "paper",
        "models": [
            {"node_id": "gemini", "roles": {"mentioned": 1}},
            {"node_id": "gemini", "roles": {"mentioned": 4, "discussed": 4}},
        ],
    })
    row = next(r for r in payload["models"] if r["node_id"] == "gemini")
    assert row["roles"] == {"mentioned": 1}
    assert "duplicate_row:gemini" in payload["repairs"]


def test_a_row_for_a_node_that_was_not_on_the_menu_is_dropped() -> None:
    payload = normalize({
        "doc_kind": "paper",
        "models": [{"node_id": "claude-opus-4", "roles": {"mentioned": 2}}],
    })
    assert [r["node_id"] for r in payload["models"]] == MENU
    assert "unknown_node:claude-opus-4" in payload["repairs"]


def test_an_extra_model_that_is_on_the_menu_is_dropped_by_id_or_by_name() -> None:
    """Rubric §6: each model appears exactly once, and the menu row is the one that grades it."""
    payload = normalize({
        "doc_kind": "paper",
        "models": [],
        "extra_models": [
            {"name_as_written": "GPT-4.5", "quote": "scored by GPT-4.5", "roles": {"mentioned": 2}},
            {"name_as_written": "gemini", "quote": "and Gemini", "roles": {"mentioned": 1}},
            {"name_as_written": "Qwen2.5-Math-7B", "quote": "we also try Qwen2.5-Math-7B",
             "roles": {"mentioned": 2, "modified": 3}},
        ],
    })
    assert [e["name_as_written"] for e in payload["extra_models"]] == ["Qwen2.5-Math-7B"]
    assert "extra_on_menu:GPT-4.5" in payload["repairs"]
    assert "extra_on_menu:gemini" in payload["repairs"]
    extra = payload["extra_models"][0]
    assert extra["roles"] == {"mentioned": 3, "modified": 3}  # the clamp applies here too
    assert "quote_unverified:extra:Qwen2.5-Math-7B" in payload["repairs"]
    validated(payload)


# ─────────────────────────── (d2) variant models ──────────────────────────


def test_all_derivatives_of_one_base_share_a_single_variant_row() -> None:
    """One role vector per base; the checkpoints are told apart only by their names."""
    payload = normalize({
        "doc_kind": "paper",
        "models": [],
        "variant_models": [
            {"base_node_id": "llama-3.1-70b", "names_as_written": ["Llama-3.1-70B-Instruct"],
             "quote": "We fine-tune Llama 3.1 70B with LoRA", "roles": {"modified": 3}},
            {"base_node_id": "llama-3.1-70b", "names_as_written": ["Llama-3.1-70B-Instruct-AWQ"],
             "quote": "on the safety split", "roles": {"modified": 2, "evaluated_behavioral": 4}},
        ],
    })
    assert len(payload["variant_models"]) == 1
    row = payload["variant_models"][0]
    assert row["names_as_written"] == ["Llama-3.1-70B-Instruct", "Llama-3.1-70B-Instruct-AWQ"]
    assert row["roles"] == {"mentioned": 4, "modified": 3, "evaluated_behavioral": 4}  # max + clamp
    assert "variant_merged:llama-3.1-70b" in payload["repairs"]
    validated(payload)


def test_an_extra_model_that_derives_from_a_menu_node_becomes_a_variant() -> None:
    payload = normalize({
        "doc_kind": "paper",
        "models": [],
        "extra_models": [{"name_as_written": "Llama-3.1-70B-Tulu",
                          "quote": "We fine-tune Llama 3.1 70B with LoRA",
                          "roles": {"mentioned": 2, "modified": 3}}],
    })
    assert payload["extra_models"] == []
    assert payload["variant_models"][0]["base_node_id"] == "llama-3.1-70b"
    assert "variant_from_extra:Llama-3.1-70B-Tulu:llama-3.1-70b" in payload["repairs"]
    validated(payload)


def test_a_sibling_version_is_not_a_derivative_but_a_family_member_is() -> None:
    """"GPT-4.5" starts with the "gpt-4" key; the following digit says version, not variant — but
    under a FAMILY node the same digit is what a member looks like (rubric §5, v0.5)."""
    assert judge._derives_from("GPT-4.5-preview", ["gpt-4"], {"gpt-4": "GPT-4"}) is None
    assert judge._derives_from("Llama 3.1 70B Instruct", MENU, NAMES) == "llama-3.1-70b"
    assert judge._derives_from("Zephyr-7B-beta", MENU, NAMES) is None  # no shared string: judge's job
    # an unlisted maker's own member under a family/series node
    assert judge._derives_from(
        "Qwen2.5-Math-7B", ["qwen2.5"], {"qwen2.5": "Qwen2.5"}, {"qwen2.5": "release series"}
    ) == "qwen2.5"
    assert judge._derives_from(
        "Llama 3.3 70B", ["llama"], {"llama": "Llama"}, {"llama": "model family"}
    ) == "llama"


def test_a_variant_of_an_off_menu_base_is_demoted_to_extra_models() -> None:
    payload = normalize({
        "doc_kind": "paper",
        "models": [],
        "variant_models": [{"base_node_id": "mistral-7b", "names_as_written": ["Zephyr-7B-beta"],
                            "quote": "we replicate on Zephyr-7B-beta", "roles": {"modified": 2}}],
    })
    assert payload["variant_models"] == []
    assert [e["name_as_written"] for e in payload["extra_models"]] == ["Zephyr-7B-beta"]
    assert "variant_demoted:mistral-7b" in payload["repairs"]
    validated(payload)


def test_a_junk_shaped_response_still_produces_a_writable_payload() -> None:
    payload = normalize({
        "doc_kind": None,
        "models": ["not a row", {"roles": {"mentioned": 2}}],
        "extra_models": [{"quote": "x", "roles": {}}],
        "flags": ["bib_only:alpaca-7b", 7],
    })
    assert [r["node_id"] for r in payload["models"]] == MENU
    # the judge's own flag survives; the bare all-zero rows pick up synthesized ones (fix 4)
    assert "bib_only:alpaca-7b" in payload["flags"]
    assert {f for f in payload["flags"] if f.startswith("not_found:")} == {
        f"not_found:{nid}" for nid in MENU if nid != "alpaca-7b"
    }
    assert payload["extra_models"] == []
    assert {"bad_row", "unknown_node:", "extra_unnamed", "bad_flag:7"} <= set(payload["repairs"])
    validated(payload)


# ─────────────────────────── (f) the renderer ──────────────────────────────


@pytest.fixture(scope="module")
def vocab() -> dict[str, Any]:
    return render.vocab_view(load_vocab(CONFIG_DIR).validated)  # type: ignore[no-any-return]


def test_menu_carries_identity_weak_flag_and_location(vocab: dict[str, Any]) -> None:
    nodes = vocab
    payload = {
        "mentions": {"gpt-4.5": ["GPT-4.5", "GPT-4.5"], "alpaca-7b": ["Alpaca 7B"]},
        "weak": {"claude-opus": ["Opus"], "gpt-4.5": ["GPT-4.5"]},
        "ambiguous": [],
    }
    menu = render.build_menu(payload, nodes, DOC)
    by_id = {e.node_id: e for e in menu}
    assert set(by_id) == {"gpt-4.5", "alpaca-7b", "claude-opus"}
    assert by_id["gpt-4.5"].weak is False and by_id["claude-opus"].weak is True  # strong wins
    assert by_id["gpt-4.5"].location == "body"
    assert by_id["alpaca-7b"].location == "references-only"  # the §4.15 deletion-test support
    assert by_id["gpt-4.5"].maker and by_id["gpt-4.5"].denotation


@pytest.mark.slow
def test_prompt_puts_the_document_first_then_rubric_then_menu(
    vocab: tuple[dict[str, Any], dict[str, str]]
) -> None:
    """The calibrated order (CALIBRATION-2.md), kept by both prompt versions."""
    for name in ("model-roles-v1", "model-roles-v2"):
        pf = main.prompt_file(name)
        scan = match.Scanner(load_vocab(CONFIG_DIR)).scan(DOC)
        menu, beyond, prompt = main.user_message(pf, DOC, scan, load_vocab(CONFIG_DIR))
        assert beyond == [] and "gpt-4.5" in [e.node_id for e in menu]
        assert prompt.index("# THE DOCUMENT") < prompt.index("# THE RUBRIC") < prompt.index("# THE MENU"), name
        assert "Emit ONLY the JSON object" in prompt
        assert "## 8. Design notes" not in prompt and "changelog:" not in prompt  # judge never sees the notes
        assert "`gpt-4.5`" in prompt and "matched text:" in prompt
        assert "occurrences)" not in prompt                # counts off by default (§2 functional load)
        assert "[DOCUMENT TRUNCATED AT CHARACTER CAP]" not in prompt and "{{" not in prompt  # filled, expanded


@pytest.mark.slow
def test_document_is_truncated_at_the_cap_and_says_so() -> None:
    pf = main.prompt_file(None)  # the default name
    assert pf.name == main.DEFAULT_NAME == "model-roles-v2"
    text = "GPT-4.5 first. " + "x" * main.DOC_CHAR_CAP
    vocab = load_vocab(CONFIG_DIR)
    _menu, _beyond, prompt = main.user_message(pf, text, match.Scanner(vocab).scan(text), vocab)
    assert "[DOCUMENT TRUNCATED AT CHARACTER CAP]" in prompt and text[:main.DOC_CHAR_CAP] in prompt
    assert text[:main.DOC_CHAR_CAP + 1] not in prompt


def test_the_menu_shows_the_matched_text_as_written() -> None:
    """Smoke review: "gpt-5-thinking" showed as bare "gpt-5". Red under: written_forms not extending (the
    menu says "gpt-4o"), extending inside words ("option" for "opt"), or the cap not applied."""
    text = "We compare gpt-4o-mini with GPT-4o. You can opt out of options. ChatGPT is the product."
    assert render.written_forms(["gpt-4o", "GPT-4o"], text) == ["gpt-4o-mini", "GPT-4o"]
    assert render.written_forms(["opt"], text) == ["opt"]
    vocab = load_vocab(GOLDEN_DIR)
    scan = match.Scanner(vocab).scan(text)
    menu, _beyond, fills = main.fills(text, scan, vocab)
    by = {e.node_id: e for e in menu}
    assert by["gpt-4o"].written == ("gpt-4o-mini", "GPT-4o") and by["gpt"].written == ("ChatGPT",)
    assert 'matched text: "gpt-4o-mini", "GPT-4o"' in fills["menu"] and 'matched text: "ChatGPT"' in fills["menu"]
    many = " ".join(f"gpt-4o-v{i}" for i in range(9))
    line = main.fills(many, match.Scanner(vocab).scan(many), vocab)[2]["menu"]
    assert "(+3 more)" in line


# ─────────────── (f2) the expected render of model-roles-v2 (prompt edits are deliberate) ───────

GOLDEN_DIR = REPO / "tests" / "fixtures" / "model_roles"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_expected() -> None:
    """Regenerate the expected renders after a deliberate prompt, rubric or menu edit: ``uv run --no-sync
    python -c "import tests.test_annotate_model_roles as t; t.write_expected()"`` (review the diff)."""
    exp = json.loads((GOLDEN_DIR / "expected-v2.json").read_text("utf-8"))
    vocab = load_vocab(GOLDEN_DIR)
    pf = main.prompt_file(exp["prompt"])
    (GOLDEN_DIR / "expected-v2.system.txt").write_text(pf.system(), "utf-8")
    exp["vocab_stamp"], exp["system_sha256"] = vocab.stamp, _sha(pf.system())
    for case in exp["cases"]:
        menu, beyond, user = main.user_message(pf, case["text"], match.Scanner(vocab).scan(case["text"]), vocab)
        case.update(menu=[[e.node_id, e.weak] for e in menu], beyond=beyond, user_sha256=_sha(user),
                    user_chars=len(user))
        if case["id"] == "small":
            (GOLDEN_DIR / "expected-v2-small.user.txt").write_text(user, "utf-8")
    (GOLDEN_DIR / "expected-v2.json").write_text(json.dumps(exp, indent=1, ensure_ascii=False) + "\n", "utf-8")


def test_model_roles_v2_renders_as_expected() -> None:
    """``prompts/model-roles-v2.md`` (+ ``rubric-v0.6.0.md``) renders the frozen expected system part and
    user messages over the frozen vocabulary subset ``tests/fixtures/model_roles/models.yaml``: a small
    document (product name, suffixed names, a weak entry, references) and an empty-menu document. Red
    under: any byte of the prompt file, the rubric include or the menu rendering changed — regenerate
    deliberately (:func:`write_expected`)."""
    exp = json.loads((GOLDEN_DIR / "expected-v2.json").read_text("utf-8"))
    vocab = load_vocab(GOLDEN_DIR)
    assert vocab.stamp == exp["vocab_stamp"]
    pf = main.prompt_file(exp["prompt"])
    assert pf.system() == (GOLDEN_DIR / "expected-v2.system.txt").read_text("utf-8")
    assert _sha(pf.system()) == exp["system_sha256"]
    for case in exp["cases"]:
        menu, beyond, user = main.user_message(pf, case["text"], match.Scanner(vocab).scan(case["text"]), vocab)
        assert [[e.node_id, e.weak] for e in menu] == case["menu"] and beyond == case["beyond"], case["id"]
        if case["id"] == "small":
            assert user == (GOLDEN_DIR / "expected-v2-small.user.txt").read_text("utf-8")
        assert (_sha(user), len(user)) == (case["user_sha256"], case["user_chars"]), case["id"]
    empty = next(c for c in exp["cases"] if c["id"] == "empty-menu")
    assert empty["menu"] == []


def test_rubric_version_is_in_the_include_name_and_the_notes() -> None:
    """The judge-visible rubric is ``prompts/rubric-v<version>.md`` (what the prompt includes); its
    metadata and design notes are ``rubric-v<version>.notes.md`` (frontmatter ``version``), not sent."""
    for name, version in (("model-roles-v1", "0.5.3"), ("model-roles-v2", "0.6.0")):
        pf = main.prompt_file(name)
        (inc,) = pf.includes
        assert inc == f"rubric-v{version}.md"
        notes = (WF_DIR / "prompts" / f"rubric-v{version}.notes.md").read_text("utf-8")
        assert re.search(rf"^version: {re.escape(version)}$", notes, flags=re.M)
        assert "## 8. Design notes" in notes and "## 8. Design notes" not in (WF_DIR / "prompts" / inc).read_text()
    rubric = (WF_DIR / "prompts" / "rubric-v0.6.0.md").read_text("utf-8")
    assert rubric.count("> **Product-name rule (ChatGPT).**") == 1  # the one rule to edit
    assert "- 0.6.0 —" in (WF_DIR / "prompts" / "rubric-v0.6.0.notes.md").read_text("utf-8")


# ─────────────────────────── (g) the rubric's categories ─────────────────


def test_the_ten_role_categories_are_the_rubric_ones() -> None:
    """``ROLE_NAMES`` is the single source; the pinned prompt's rubric (§3 headings) must still match it."""
    pf = main.prompt_file(main.DEFAULT_NAME)
    for role in ROLE_NAMES:
        assert f"`{role}`" in pf.user_template, f"{role} is not defined in the rubric {pf.includes}"


# ─────────────────── (g2) the prompt window (pilot fix 1) ──────────────────


def _cap_payload() -> dict[str, Any]:
    return {"mentions": {"gpt-4.5": ["GPT-4.5"], "gemini": ["Gemini"]},
            "weak": {"claude-opus": ["Opus"]}, "ambiguous": []}


def test_a_node_only_past_the_cap_is_withheld_from_the_menu() -> None:
    """The pilot bug: build_menu located over the FULL text while the prompt was capped, so the
    judge got menu entries for models it could not see and confidently flagged them bib_only
    (doc nr43qcgn4d5fv: 4 nodes first occurring past 631k of 854k chars)."""
    text = "GPT-4.5 leads the table. " + ("filler " * 200) + "We also ran Gemini and Opus."
    cap = 100
    assert text.index("Gemini") > cap
    in_window, beyond = render.window_split(_cap_payload(), text, cap)
    assert beyond == ["claude-opus", "gemini"]
    assert set(in_window["mentions"]) == {"gpt-4.5"} and in_window["weak"] == {}
    menu = render.build_menu(in_window, render.vocab_view(load_vocab(CONFIG_DIR).validated), text[:cap])
    assert [e.node_id for e in menu] == ["gpt-4.5"]


def test_a_document_under_the_cap_is_untouched() -> None:
    payload = _cap_payload()
    in_window, beyond = render.window_split(payload, "GPT-4.5 and Gemini and Opus.", 300_000)
    assert beyond == [] and in_window is payload


def test_a_node_found_nowhere_is_not_beyond_the_window() -> None:
    """Absent everywhere is a matcher artifact, which the rubric's flags already cover — calling
    it `beyond_window` would claim the document contains a model it does not."""
    text = "GPT-4.5 leads the table. " + ("filler " * 200)
    _in_window, beyond = render.window_split(
        {"mentions": {"gpt-4.5": ["GPT-4.5"], "gemini": ["Gemini"]}, "weak": {},
         "ambiguous": []},
        text, 100,
    )
    assert beyond == []


def test_withheld_nodes_come_back_as_ungraded_rows() -> None:
    payload = judge.normalize(
        {"doc_kind": "paper", "models": [{"node_id": "gpt-4.5", "roles": {"mentioned": 2}}]},
        menu_nodes=["gpt-4.5"], menu_names={"gpt-4.5": "GPT-4.5"}, text=DOC,
        beyond_window=["gemini", "alpaca-7b"],
    )
    assert [r["node_id"] for r in payload["models"]] == ["gpt-4.5", "gemini", "alpaca-7b"]
    assert all(r["roles"] == {} for r in payload["models"][1:])
    assert {"beyond_window:gemini", "beyond_window:alpaca-7b"} <= set(payload["repairs"])
    # exempt from the all-zero flag rule: no rubric flag is TRUE of a row nobody graded
    assert not [f for f in payload["flags"] if "gemini" in f]
    validated(dict(payload))


# ────────────── (g3) quote tolerance + flag hygiene (pilot fixes 2, 4, 5) ───


@pytest.mark.parametrize("quote", [
    "Responses were scored by GPT-4.5 acting as judge.",        # terminal punctuation added
    "responses were scored by GPT-4.5 acting as judge",         # de-capitalized
    "Responses were scored by GPT-4.5 acting as judge, follow-ing prior work",  # OCR hyphen
    "[Responses were scored by GPT-4.5](#f1) acting as judge",  # markdown link syntax
])
def test_presentational_differences_do_not_read_as_fabrication(quote: str) -> None:
    """10.6% of pilot quotes were marked unverified; hand-audit put real fabrication at 1-2%.
    None of these points at a different span of the document."""
    assert judge._found_in(quote, judge._normalize(DOC))


@pytest.mark.parametrize("quote", [
    "Responses were scored by Gemini acting as judge",          # substituted subject
    "We fine-tune Llama 3.1 70B and responses were scored by GPT-4.5",  # spliced sentences
    "GPT-4.5 was the most accurate judge in our evaluation",    # compressed paraphrase
])
def test_real_fabrication_is_still_caught(quote: str) -> None:
    assert not judge._found_in(quote, judge._normalize(DOC))


def test_a_bare_all_zero_row_gets_a_synthesized_not_found_flag() -> None:
    """Rubric 0.5.2: an all-zero row must say why. 52 of 496 pilot rows were bare."""
    payload = normalize({
        "doc_kind": "paper",
        "models": [{"node_id": "alpaca-7b", "roles": {}}, {"node_id": "gemini", "roles": {}}],
        "flags": ["bib_only:alpaca-7b"],
    })
    assert "not_found:gemini" in payload["flags"]
    assert "flag_synthesized:not_found:gemini" in payload["repairs"]
    assert payload["flags"].count("bib_only:alpaca-7b") == 1  # already explained, left alone
    assert not [r for r in payload["repairs"] if r.startswith("flag_synthesized:not_found:alpaca")]
    validated(payload)


def test_a_graded_row_needs_no_flag() -> None:
    payload = normalize({"doc_kind": "paper",
                         "models": [{"node_id": "gemini", "roles": {"mentioned": 1}}]})
    assert not [f for f in payload["flags"] if f.startswith("not_found:gemini")]


def test_two_flags_on_one_row_are_recorded_not_resolved() -> None:
    payload = normalize({
        "doc_kind": "paper", "models": [{"node_id": "gemini", "roles": {}}],
        "flags": ["bib_only:gemini", "matcher_fp:gemini"],
    })
    assert "flag_ambiguous:gemini" in payload["repairs"]
    assert len([f for f in payload["flags"] if "gemini" in f]) == 2  # nothing dropped


def test_a_subsumed_flag_naming_a_variant_is_recased_to_the_payload_spelling() -> None:
    payload = normalize({
        "doc_kind": "paper",
        "models": [{"node_id": "llama-3.1-70b", "roles": {"mentioned": 2}}],
        "variant_models": [{"base_node_id": "llama-3.1-70b",
                            "names_as_written": ["Llama-3.1-70B-Tulu"],
                            "quote": "We fine-tune Llama 3.1 70B with LoRA",
                            "roles": {"modified": 3}}],
        "flags": ["subsumed:llama-3.1-70b:llama-3.1-70b-tulu"],
    })
    assert "subsumed:llama-3.1-70b:Llama-3.1-70B-Tulu" in payload["flags"]
    assert [r for r in payload["repairs"] if r.startswith("flag_recased:")]
    validated(payload)


def test_output_schema_is_self_contained_strict_shaped_and_content_permissive() -> None:
    """What is sent (``main.SCHEMA``) is strict mode's shape (closed objects, every key required,
    the optional ones nullable; ``roles`` a fixed-key object) and constrains no content: no
    strength bounds, no menu enum (every constraint is a paid retry trigger)."""
    from stack.llm import strict_schema, wire_schema
    from tests.test_llm_strict import strict_violations

    base, schema = judge.output_schema(), main.SCHEMA
    assert "$ref" not in json.dumps(schema)                  # stack.llm.validate_json has no $ref
    assert base["required"] == ["doc_kind", "models"] and "additionalProperties" not in base
    assert schema == strict_schema(base) == strict_schema(schema)
    assert strict_violations(wire_schema(schema)) == []
    roles = schema["properties"]["models"]["items"]["properties"]["roles"]
    assert roles["properties"] == {r: {"type": ["integer", "null"]} for r in ROLE_NAMES}
    assert roles["additionalProperties"] is False and roles["required"] == list(ROLE_NAMES)
    assert not {"minimum", "maximum", "enum"} & {k for v in roles["properties"].values() for k in v}
    assert schema["properties"]["models"]["items"]["properties"]["node_id"] == {"type": "string"}


def _all_roles(roles: dict[str, int]) -> dict[str, int | None]:
    """A sparse ``roles`` map as strict mode returns it: every role a key, ``null`` for zero."""
    return {r: roles.get(r) for r in ROLE_NAMES}


#: One logical answer, permissive (sparse roles, optional keys omitted, an explicit 0) ...
PERMISSIVE = {
    "doc_kind": "paper",
    "models": [
        {"node_id": "llama-3.1-70b", "roles": {"mentioned": 3, "modified": 3, "discussed": 0},
         "evidence": [{"roles": ["modified"], "quote": "We fine-tune Llama 3.1 70B with LoRA on the safety split"}]},
        {"node_id": "gpt-4.5", "roles": {"instrument": 3}, "note": "the judge"},
        {"node_id": "gemini", "roles": {"mentioned": 1}},
    ],
    "extra_models": [{"name_as_written": "Alpaca 7B", "quote": "Alpaca 7B: an instruction-following model",
                      "roles": {"mentioned": 1}},
                     {"name_as_written": "Llama 3.1 70B Tulu", "quote": "We fine-tune Llama 3.1 70B",
                      "roles": {"modified": 2}}],
    "flags": ["bib_only:alpaca-7b"],
}
#: ... and as strict mode returns it (every key present, null where the permissive one omits).
STRICT = {
    "doc_kind": "paper",
    "models": [
        {"node_id": "llama-3.1-70b", "roles": {**_all_roles({"mentioned": 3, "modified": 3}), "discussed": 0},
         "evidence": [{"roles": ["modified"], "quote": "We fine-tune Llama 3.1 70B with LoRA on the safety split"}],
         "note": None, "refined_name": None},
        {"node_id": "gpt-4.5", "roles": _all_roles({"instrument": 3}), "evidence": None, "note": "the judge", "refined_name": None},
        {"node_id": "gemini", "roles": _all_roles({"mentioned": 1}), "evidence": None, "note": None, "refined_name": None},
    ],
    "variant_models": None,
    "extra_models": [{"name_as_written": "Alpaca 7B", "quote": "Alpaca 7B: an instruction-following model",
                      "roles": _all_roles({"mentioned": 1})},
                     {"name_as_written": "Llama 3.1 70B Tulu", "quote": "We fine-tune Llama 3.1 70B",
                      "roles": _all_roles({"modified": 2})}],
    "flags": ["bib_only:alpaca-7b"],
}


def test_a_strict_shaped_answer_and_the_permissive_one_give_the_same_payload() -> None:
    """Strict mode answers ``null`` where the rubric omits; the repairs (clamp, missing row,
    variant move) and the stored payload are those of the permissive answer."""
    from stack.llm import fill_nulls, validate_json

    assert validate_json(STRICT, main.SCHEMA) == []
    assert validate_json(fill_nulls(PERMISSIVE, main.SCHEMA), main.SCHEMA) == []  # the degrade path
    strict, permissive = normalize(STRICT), normalize(PERMISSIVE)
    assert strict == permissive and validated(strict) == validated(permissive)
    assert not [r for r in strict["repairs"] if r.startswith(("bad_strength", "bad_roles", "unknown_role"))]
    assert {"mentioned_clamped:gpt-4.5:0->3", "row_missing:alpaca-7b",
            "variant_from_extra:Llama 3.1 70B Tulu:llama-3.1-70b"} <= set(strict["repairs"])
    assert [v["base_node_id"] for v in strict["variant_models"]] == ["llama-3.1-70b"]


def _refine_stub(table: dict[tuple[str, str], tuple[str | None, str]]) -> Any:
    return lambda menu_node, name: table.get((menu_node, name), (None, "unresolved"))


def test_a_refined_row_moves_to_the_specific_node_and_keeps_the_menu_node() -> None:
    """Concretization (rubric 0.6 §5): ``refined_name`` accepted by the resolver moves the row; the stored
    ``node_id`` is the refined node, ``matched_node_id`` the menu node. Red under: refinement ignored."""
    refine = _refine_stub({("gemini", "Gemini 2.5 Pro"): ("gemini-2.5-pro", "")})
    payload = dict(judge.normalize({
        "doc_kind": "paper",
        "models": [{"node_id": "gemini", "roles": {"mentioned": 2, "evaluated_behavioral": 2},
                    "refined_name": "Gemini 2.5 Pro"}],
    }, menu_nodes=MENU, menu_names=NAMES, text=DOC, refine=refine))
    row = next(m for m in payload["models"] if m["matched_node_id"] == "gemini")
    assert row["node_id"] == "gemini-2.5-pro" and "refined:gemini->gemini-2.5-pro" in payload["repairs"]
    stored = validated(payload)
    srow = next(m for m in stored["models"] if m["node_id"] == "gemini-2.5-pro")
    assert srow["matched_node_id"] == "gemini" and srow["roles"]["evaluated_behavioral"] == 2
    assert all(m["matched_node_id"] == m["node_id"] for m in stored["models"] if m is not srow)


def test_a_refusal_keeps_the_row_on_the_menu_node_and_says_why() -> None:
    """Out of lineage or unresolvable: the row stays on its menu node, the refusal is a repair."""
    refine = _refine_stub({("gemini", "GPT-4"): (None, "not_in_lineage:gpt-4")})
    obj = {"doc_kind": "paper", "models": [{"node_id": "gemini", "roles": {"mentioned": 1}, "refined_name": "GPT-4"},
                                           {"node_id": "gpt-4.5", "roles": {"mentioned": 1}, "refined_name": "FooLM"}]}
    payload = dict(judge.normalize(obj, menu_nodes=MENU, menu_names=NAMES, text=DOC, refine=refine))
    assert {m["node_id"] for m in payload["models"]} == set(MENU)
    assert {"refinement_dropped:gemini:not_in_lineage:gpt-4", "refinement_dropped:gpt-4.5:unresolved"} <= set(
        payload["repairs"])
    plain = dict(judge.normalize(obj, menu_nodes=MENU, menu_names=NAMES, text=DOC))  # no resolver
    assert "refinement_dropped:gemini:no_resolver" in plain["repairs"]


def test_rows_refined_to_the_same_node_are_merged() -> None:
    """Two menu rows (family and weak line) refined to one model, and a row refined onto another menu row,
    merge deterministically: roles by max, evidence concatenated, the merge recorded."""
    refine = _refine_stub({("gemini", "GPT-4.5"): ("gpt-4.5", ""), ("alpaca-7b", "GPT-4.5"): ("gpt-4.5", "")})
    payload = dict(judge.normalize({"doc_kind": "paper", "models": [
        {"node_id": "gpt-4.5", "roles": {"mentioned": 2, "instrument": 2},
         "evidence": [{"roles": ["instrument"], "quote": "Responses were scored by GPT-4.5 acting as judge"}]},
        {"node_id": "gemini", "roles": {"mentioned": 3, "discussed": 3}, "refined_name": "GPT-4.5"},
        {"node_id": "alpaca-7b", "roles": {"mentioned": 1}, "refined_name": "GPT-4.5"}]},
        menu_nodes=MENU, menu_names=NAMES, text=DOC, refine=refine))
    rows = [m for m in payload["models"] if m["node_id"] == "gpt-4.5"]
    assert len(rows) == 1 and rows[0]["matched_node_id"] == "gpt-4.5"
    assert rows[0]["roles"] == {"mentioned": 3, "discussed": 3, "instrument": 2}
    assert {"refinement_merged:gpt-4.5:gemini", "refinement_merged:gpt-4.5:alpaca-7b"} <= set(payload["repairs"])
    validated(payload)


def test_lineage_is_the_node_its_descendants_and_its_parents_subtree() -> None:
    """``main.in_lineage`` over the real catalogue: family → model accepted; a sibling version (shared
    parent) accepted; another family refused."""
    nodes = load_vocab(CONFIG_DIR).nodes
    assert main.in_lineage(nodes, "gpt", "gpt-4") and main.in_lineage(nodes, "claude", "claude-3.5-sonnet")
    assert main.in_lineage(nodes, "claude-opus-5", "claude-opus-5.5")  # sibling under the Opus line
    assert not main.in_lineage(nodes, "claude", "gpt-4") and not main.in_lineage(nodes, "gemini", "gpt-4o")


@pytest.mark.parametrize("names", [["Llama-3.1-70B-Instruct"], ["LLaMA-3.1-70B-IT", "Llama 3.1 70B Chat"]])
def test_a_standard_release_variant_row_folds_onto_its_node(names: list[str]) -> None:
    """Rubric 0.6 grain rule: the Instruct/IT/Chat release is the node. The smoke run's error (node zeroed
    with ``subsumed``, roles exiled to a variant row) is repaired: roles merged by max onto the node, the
    subsumed flag dropped, the repair recorded. Red under: no fold (the variant row stays)."""
    payload = normalize({
        "doc_kind": "paper",
        "models": [{"node_id": "llama-3.1-70b", "roles": {}}],
        "variant_models": [{"base_node_id": "llama-3.1-70b", "names_as_written": names,
                            "quote": "We fine-tune Llama 3.1 70B with LoRA on the safety split",
                            "roles": {"mentioned": 3, "modified": 3}}],
        "flags": [f"subsumed:llama-3.1-70b:{names[0]}"],
    })
    assert payload["variant_models"] == []
    row = next(m for m in payload["models"] if m["node_id"] == "llama-3.1-70b")
    assert row["roles"] == {"mentioned": 3, "modified": 3}
    assert row["evidence"] == [{"roles": ["mentioned", "modified"],
                                "quote": "We fine-tune Llama 3.1 70B with LoRA on the safety split"}]
    assert "instruct_folded:llama-3.1-70b" in payload["repairs"]
    assert not [f for f in payload["flags"] if f.startswith("subsumed:")]
    validated(payload)


def test_a_variant_row_with_a_real_derivative_is_not_folded() -> None:
    """Only rows naming nothing but standard releases fold: a real fine-tune keeps its variant row."""
    payload = normalize({
        "doc_kind": "paper",
        "models": [{"node_id": "llama-3.1-70b", "roles": {"mentioned": 1}}],
        "variant_models": [{"base_node_id": "llama-3.1-70b",
                            "names_as_written": ["Llama-3.1-70B-Instruct", "Llama-3.1-70B-Tulu"],
                            "quote": "We fine-tune Llama 3.1 70B with LoRA", "roles": {"modified": 3}}],
    })
    assert [v["base_node_id"] for v in payload["variant_models"]] == ["llama-3.1-70b"]
    assert not [r for r in payload["repairs"] if r.startswith("instruct_folded")]


def test_maker_display_list_maker() -> None:
    """Vocabulary 0.4 collaboration nodes carry maker as a LIST (llava = uw-madison +
    microsoft); the pilot run rr91swn997nez crashed on exactly this. Regression."""
    def _n(nid: str, **kw: object) -> VocabNode:
        return VocabNode.model_validate({"id": nid, "kind": "model", "name": "x", **kw})

    a, b = {"id": "a", "name": "Lab A"}, {"id": "b", "name": "Lab B"}
    nodes = {"collab": _n("collab", maker=[a, b]), "solo": _n("solo", maker=a),
             "child": _n("child", parent="collab"), "orphan": _n("orphan")}
    assert render.maker_display(nodes, "collab") == "Lab A / Lab B"
    assert render.maker_display(nodes, "child") == "Lab A / Lab B"
    assert render.maker_display(nodes, "solo") == "Lab A"
    assert render.maker_display(nodes, "orphan") == "maker unknown"


# ─────────────────────────── (i) v4: the stage on the fake model ───────────


import shutil  # noqa: E402

import stack.annotate.stage as annotate_stage  # noqa: E402
from stack.annotate import config_digest  # noqa: E402
from stack.annotate.prompt_files import clear_cache  # noqa: E402
from stack.checks.verify import run_checks  # noqa: E402
from stack.modelvocab import vocab_digest  # noqa: E402
from stack.stage_runner import StageError, run_stage  # noqa: E402
from tests.fakes.annotate import (  # noqa: E402
    AT_RUN,
    doc_with_text,
    fold_corpus,
    rows_of,
    write_documents,
    write_plan_file,
)
from tests.fakes.llm import FakeLlm, ok  # noqa: E402
from tests.synth.records import SERIES, run_line  # noqa: E402

main = _load_module("annotate/model_roles/main.py", "_model_roles_main_under_test")
match = main.match

LUNA = "openai/gpt-6-luna"
STAGE_TEXTS = [
    "DOC_A. " + DOC,                                                       # strong mentions
    "DOC_B. A sourdough recipe; the author asked Zephyrine-9B for help.",  # no hit: empty menu, still a call
    "DOC_C. A Magnum Opus of prose.",                                      # weak only: still a call
    "DOC_D. We studied how ChatGPT version 4 responds to a million requests.",  # family only; the text says GPT-4
]
#: an empty menu: the judge reports the off-catalogue model it read (no node to resolve to)
REPLY_B = {"doc_kind": "blogpost", "models": [], "flags": [],
           "extra_models": [{"name_as_written": "Zephyrine-9B", "quote": "the author asked Zephyrine-9B for help",
                             "roles": {"mentioned": 1, "usage_experience": 1}}]}
#: a weak-only menu the judge rejects: called, nothing graded -> empty
REPLY_C = {"doc_kind": "other", "models": [{"node_id": "claude-opus", "roles": {}}], "flags": ["matcher_fp:claude-opus"]}
#: a lone ChatGPT the text pins to GPT-4: the judge refines the gpt row
REPLY_D = {"doc_kind": "blogpost", "flags": [],
           "models": [{"node_id": "gpt", "roles": {"mentioned": 3, "evaluated_behavioral": 3}, "refined_name": "GPT-4",
                       "evidence": [{"roles": ["evaluated_behavioral"],
                                     "quote": "We studied how ChatGPT version 4 responds to a million requests."}]}]}
REPLY_A = {
    "doc_kind": "paper",
    "models": [
        {"node_id": "llama-3.1-70b", "roles": {"mentioned": 3, "modified": 3},
         "evidence": [{"roles": ["modified"], "quote": "We fine-tune Llama 3.1 70B with LoRA on the safety split"}]},
        {"node_id": "gpt-4.5", "roles": {"instrument": 3},  # mentioned omitted: clamped up to 3
         "evidence": [{"roles": ["instrument"], "quote": "GPT-4.5 was our favourite judge by far"}]},  # fabricated
    ],
    # off-menu models the judge reports: one the vocabulary knows (node_id set), one it does not (NULL)
    "extra_models": [
        {"name_as_written": "Claude Opus 4.7", "quote": "Responses were scored by GPT-4.5", "roles": {"mentioned": 1}},
        {"name_as_written": "FooLM-7B", "quote": "Responses were scored by GPT-4.5", "roles": {"mentioned": 1}},
    ],
    "flags": [],
}


@pytest.fixture
def world(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    fake = FakeLlm()
    for marker, reply in (("DOC_A", REPLY_A), ("DOC_B", REPLY_B), ("DOC_C", REPLY_C), ("DOC_D", REPLY_D)):
        fake.on(marker, ok(reply), model=LUNA)
    monkeypatch.delenv("STACK_WORKFLOWS_ROOT", raising=False)  # the real workflows
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("STACK_PROGRESS", "0")
    monkeypatch.setattr(annotate_stage, "TRANSPORT_HOOK", lambda: fake)
    monkeypatch.setattr(annotate_stage, "SLEEP", lambda _s: None)
    monkeypatch.setattr(annotate_stage, "CLOCK", lambda: AT_RUN)
    monkeypatch.chdir(tmp_path)  # X6 finds the plan by name under ./configs/plans
    store = tmp_path / "store"
    docs = [doc_with_text(store, i, t) for i, t in enumerate(STAGE_TEXTS)]
    write_documents(store, docs, run=1)
    plan = write_plan_file(
        tmp_path, "roles-test",
        prompts={"annotate.model_roles": {"name": "model-roles-v2", "role": "extract_main", "model": LUNA}},
        stages=[{"stage": "fold.corpus"},
                {"stage": "annotate.model_roles", "prior": ["model_roles", "model_roles_evidence"]},
                {"stage": "fold.build"}],
        folded=["model_roles"])
    return {"store": store, "fake": fake, "plan": plan, "ids": [d["doc_id"] for d in docs], "tmp": tmp_path,
            "monkeypatch": monkeypatch}


@pytest.mark.slow
def test_stage_judges_every_document_whatever_the_menu(world: dict[str, Any]) -> None:
    """Registry version 3: one call per document, an empty or weak-only menu included; extras resolved;
    ``empty`` = called, nothing graded. Red under: any early no-call return (fewer calls, ``computed``
    rows), or the status rule (DOC_C ``ok``)."""
    store, fake, plan, ids = world["store"], world["fake"], world["plan"], world["ids"]
    corpus = fold_corpus(store, plan)
    rec = run_stage(store, plan, "annotate.model_roles", [corpus], series=SERIES)
    assert rec.status == "ok", rec.error
    assert fake.count() == 4 and all(fake.count(contains=f"DOC_{x}") == 1 for x in "ABCD")
    rows = {r["doc_id"]: r for r in rows_of(store, rec, "model_roles")}
    a, b, c, d = (rows[i] for i in ids)
    assert all((r["origin"], r["basis_kind"]) == ("ai_check", "text") for r in (a, b, c, d))
    # empty menu: the judge's off-catalogue model, no node to resolve to
    assert (b["status"], b["models"], b["doc_kind"]) == ("ok", [], "blogpost")
    assert [(x["name_as_written"], x["node_id"]) for x in b["extra_models"]] == [("Zephyrine-9B", None)]
    # weak only, rejected by the judge: called, nothing graded
    assert (c["status"], c["flags"]) == ("empty", ["matcher_fp:claude-opus"])
    # concretized: the gpt row (ChatGPT) refined to gpt-4, the menu node kept
    assert d["status"] == "ok" and "refined:gpt->gpt-4" in d["repairs"]
    assert [(m["node_id"], m["matched_node_id"], m["roles"]["evaluated_behavioral"]) for m in d["models"]] == [
        ("gpt-4", "gpt", 3)]
    # the matcher's hits are the record model_regexp_matches of every answer, not in the payload
    rx = {r["doc_id"]: r for r in rows_of(store, rec, "model_regexp_matches")}
    assert set(rx) == set(ids) and "matches" not in a
    assert rx[ids[2]]["matches"] == [{"node_id": "claude-opus", "surfaces": ["Opus"], "strength": "weak"}]
    assert rx[ids[1]]["matches"] == [] and rx[ids[3]]["matches"][0]["node_id"] == "gpt"
    assert rx[ids[0]]["evidence_id"] == {r["doc_id"]: r for r in rows_of(store, rec, "model_roles")}[ids[0]]["evidence_id"]
    # the payload: every menu node a row in menu order, roles an explicit 0..4 struct; every hit in matches
    by_node = {m["node_id"]: m for m in a["models"]}
    assert set(by_node) >= {"llama-3.1-70b", "gpt-4.5"} and a["doc_kind"] == "paper" and a["status"] == "ok"
    ra = {r["doc_id"]: r for r in rows_of(store, rec, "model_regexp_matches")}[ids[0]]
    strong = [m["node_id"] for m in ra["matches"] if m["strength"] == "strong"]
    assert strong == sorted(strong) and {"llama-3.1-70b", "gpt-4.5"} <= set(strong)
    assert set(by_node) == {m["node_id"] for m in ra["matches"]}  # the menu is the matcher's hits
    assert list(by_node["gpt-4.5"]["roles"]) == list(ROLE_NAMES)
    assert by_node["gpt-4.5"]["roles"]["mentioned"] == 3 and by_node["gpt-4.5"]["roles"]["discussed"] == 0
    assert "mentioned_clamped:gpt-4.5:0->3" in a["repairs"]
    assert "quote_unverified:gpt-4.5" in a["repairs"] and "quote_unverified:llama-3.1-70b" not in a["repairs"]
    assert [(x["name_as_written"], x["node_id"]) for x in a["extra_models"]] == [
        ("Claude Opus 4.7", "claude-opus-4.7"), ("FooLM-7B", None)]
    assert (rec.counts["quote_unverified"], rec.counts["menu_empty"], rec.counts["menu_weak_only"],
            rec.counts["refined"], rec.counts["extra_resolved"], rec.counts["extra_models"]) == (
        1, 1, 2, 1, 1, 3)  # DOC_D's menu (gpt, matched only weakly) is weak-only too
    assert rec.counts["rows.model_regexp_matches"] == 4
    assert "no_match" not in rec.counts and "not_judged" not in rec.counts
    # the requests: the prompt file's system part as the fixed prefix, then document → rubric → menu
    texts = {x: prompt_text_of(r) for r in fake.requests for x in "ABCD" if f"DOC_{x}" in prompt_text_of(r)}
    ta = texts["A"]
    assert "# Role" in ta and ta.index("# THE DOCUMENT") < ta.index("# THE RUBRIC") < ta.index("# THE MENU")
    assert "`gpt-4.5`" in ta and "`llama-3.1-70b`" in ta and "## 8. Design notes" not in ta
    assert not re.search(r"\{\{(?:[a-z][a-z0-9_]*|include:[^}]*|split:user)\}\}", ta)  # all filled/expanded
    assert "the menu is empty" in texts["B"] and "\n- `" not in texts["B"].split("# THE MENU", 1)[1]
    assert 'matched text: "ChatGPT"' in texts["D"]
    # the evidence rows name the pinned prompt and template; the basis is the text
    ev = {e["doc_id"]: e for e in rows_of(store, rec, "model_roles_evidence")}
    pin = main.prompt(plan.prompts["annotate.model_roles"], annotate_stage.stack_config().llm)
    for i in ids:
        assert ev[i]["prompt_name"] == "model-roles-v2" and ev[i]["template_digest"] in pin.template_digests
        assert ev[i]["model"] == LUNA
    assert ev[ids[0]]["basis_digest"] == a["basis_digest"] != ev[ids[0]]["work_digest"]
    results = run_checks(store)
    red = {r.check: r.items for r in results if r.status not in ("ok", "skipped")}
    assert red == {}, red
    by = {r.check: r.status for r in results}
    names = ("V-wf", "V-evidence", "policy_pins", "basis_currency", "selection_recompute")
    assert {n: by[n] for n in names} == dict.fromkeys(names, "ok")


@pytest.mark.slow
def test_a_vocabulary_edit_reasks_every_document(world: dict[str, Any]) -> None:
    """The vocabulary's sha256 is in the config digest, so an edited ``models.yaml`` re-asks every
    document (a rerun on the same one carries everything); every unchanged request is served from the
    LLM cache, no second call. Red under: ``vocab_digest()`` dropped from ``prompt``'s digests (the
    second run then carries all three)."""
    store, fake, plan, tmp = world["store"], world["fake"], world["plan"], world["tmp"]
    built = {"corpus_input": {"path": fold_corpus(store, plan)}}  # the build's fold.corpus file
    rec = run_line(store, plan, "annotate.model_roles", built=built)  # the line's --in: corpus + prior dirs
    assert rec is not None and rec.status == "ok", rec.error
    again = run_line(store, plan, "annotate.model_roles", built=built)
    assert again is not None and again.status == "ok", again.error
    assert again.counts["carried"] == 4 and not again.counts.get("asked")
    cfg = tmp / "cfg-vocab-edit"
    shutil.copytree(REPO / "configs", cfg, ignore=shutil.ignore_patterns("plans"))
    with (cfg / "models.yaml").open("a", encoding="utf-8") as fh:
        fh.write("# an edit\n")
    world["monkeypatch"].setenv("STACK_CONFIG_DIR", str(cfg))
    edited = run_line(store, plan, "annotate.model_roles", built=built)
    assert edited is not None and edited.status == "ok", edited.error
    assert edited.config_digest != rec.config_digest
    assert edited.counts["asked"] == 4 and not edited.counts.get("carried")
    assert fake.count() == 4 and edited.counts["cache_hits"] == 4


@pytest.mark.slow
def test_the_stage_sends_a_strict_schema_and_is_accepted_on_the_first_attempt(
        world: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """Through ``ask`` and the client, against a provider playing OpenAI's strict-mode check
    (``tests.test_llm_strict.StrictProvider``: a 400 for a schema strict mode refuses): one
    request per document, ``response_format`` json_schema with ``strict: true``, no degrade."""
    from tests.test_llm_strict import StrictProvider, strict_violations

    reply = {**REPLY_A, "models": [{**m, "roles": _all_roles(m["roles"]), "note": None} for m in REPLY_A["models"]],
             "variant_models": None, "extra_models": None}
    provider = StrictProvider(ok(reply))
    monkeypatch.setattr(annotate_stage, "TRANSPORT_HOOK", lambda: provider)
    store, plan, ids = world["store"], world["plan"], world["ids"]
    corpus = fold_corpus(store, plan)
    rec = run_stage(store, plan, "annotate.model_roles", [corpus], series=SERIES)
    assert rec.status == "ok", rec.error
    assert len(provider.requests) == len(ids)
    for req in provider.requests:
        fmt = req.payload["response_format"]
        assert fmt["type"] == "json_schema" and fmt["json_schema"]["strict"] is True
        assert strict_violations(fmt["json_schema"]["schema"]) == []
    a = {r["doc_id"]: r for r in rows_of(store, rec, "model_roles")}[ids[0]]
    assert a["status"] == "ok" and "mentioned_clamped:gpt-4.5:0->3" in a["repairs"]
    assert not [r for r in a["repairs"] if r.startswith("bad_")]


def prompt_text_of(request: Any) -> str:
    from tests.fakes.llm import prompt_text

    return prompt_text(request)


def test_prompt_rubric_and_vocabulary_are_in_the_config_digest(tmp_path: pathlib.Path,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    """An edit of the prompt file, of its rubric include or of the vocabulary changes ``config_digest``
    (v3's provenance hole, closed); the pin name selects the file; bad names and unknown keys refuse."""
    llm = annotate_stage.stack_config().llm
    spec = REGISTRY["model_roles"]
    pin = {"name": "model-roles-v2", "role": "extract_main"}
    pp = main.prompt(pin, llm)
    assert pp.name == "model-roles-v2" and vocab_digest() in pp.template_digests
    assert main.prompt_file("model-roles-v2").digest in pp.template_digests
    before = config_digest(spec, pin, pp)
    assert main.prompt({"role": "extract_main"}, llm).name == main.DEFAULT_NAME  # no name: the default file

    def digest_with(edit: str) -> str:
        here = tmp_path / edit
        shutil.copytree(WF_DIR / "prompts", here / "prompts")
        target = {"prompt": "model-roles-v2.md", "rubric": "rubric-v0.6.0.md"}[edit]
        with (here / "prompts" / target).open("a", encoding="utf-8") as fh:
            fh.write("<!-- an edit -->\n")
        monkeypatch.setattr(main, "HERE", here)
        try:
            return config_digest(spec, pin, main.prompt(pin, llm))
        finally:
            monkeypatch.setattr(main, "HERE", WF_DIR)

    assert len({before, digest_with("prompt"), digest_with("rubric")}) == 3
    cfg = tmp_path / "configs"
    shutil.copytree(CONFIG_DIR, cfg, ignore=shutil.ignore_patterns("plans"))
    with (cfg / "models.yaml").open("a", encoding="utf-8") as fh:
        fh.write("# an edit\n")
    monkeypatch.setenv("STACK_CONFIG_DIR", str(cfg))
    assert config_digest(spec, pin, main.prompt(pin, llm)) != before
    monkeypatch.delenv("STACK_CONFIG_DIR")
    assert config_digest(spec, pin, main.prompt(pin, llm)) == before
    clear_cache()
    with pytest.raises(StageError, match="plan pin keys"):
        main.prompt({"name": "x", "temperature": 0}, llm)
    with pytest.raises(StageError, match="is not a prompt name"):
        main.prompt({"name": "../screen/screen-v12"}, llm)
    with pytest.raises(StageError, match="no prompt file"):
        main.prompt({"name": "model-roles-v0"}, llm)
    with pytest.raises(StageError, match="exactly one"):  # the rubric include is no prompt
        main.prompt({"name": "rubric-v0.6.0"}, llm)


@pytest.mark.slow
def test_doc_cap_pin_reads_and_menus_past_300k_and_moves_only_its_own_digest() -> None:
    # Red under: a default pin whose digests move, a doc_cap that leaves the digests alone, the menu or the
    # document still cut at 300k under doc_cap, or an unvalidated doc_cap.
    llm = annotate_stage.stack_config().llm
    base = main.prompt({"name": "model-roles-v2", "role": "extract_main"}, llm).template_digests
    same = main.prompt({"name": "model-roles-v2", "role": "extract_main", "doc_cap": main.DOC_CHAR_CAP},
                       llm).template_digests
    capped = main.prompt({"name": "model-roles-v2", "role": "extract_main", "doc_cap": 900_000}, llm).template_digests
    assert same == base and capped[:len(base)] == base and len(capped) == len(base) + 1
    assert main.pin_of({"doc_cap": 900_000}, llm).doc_cap == 900_000
    for bad in (True, "900000", 10, main.DOC_CAP_MAX + 1):
        with pytest.raises(StageError, match="doc_cap must be an integer"):
            main.pin_of({"doc_cap": bad}, llm)
    pf = main.prompt_file(None)
    text = "GPT-4.5 first. " + "x" * main.DOC_CHAR_CAP + " Then Gemini 2.5 Pro late."
    vocab = load_vocab(CONFIG_DIR)
    scan = match.Scanner(vocab).scan(text)
    menu, beyond, prompt = main.user_message(pf, text, scan, vocab)
    menu9, beyond9, prompt9 = main.user_message(pf, text, scan, vocab, 900_000)
    assert beyond and not beyond9 and set(beyond) <= {e.node_id for e in menu9}
    assert "Gemini 2.5 Pro late." in prompt9 and "[DOCUMENT TRUNCATED AT CHARACTER CAP]" not in prompt9
    assert "Gemini 2.5 Pro late." not in prompt and "[DOCUMENT TRUNCATED AT CHARACTER CAP]" in prompt
