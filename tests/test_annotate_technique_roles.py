"""``annotate.technique_roles`` (round 5 T-ANN): the menu, the prompt file (byte-identical to the
pre-prompt-file renderer: a golden; the pin name selects the file, D591), the judge parse/repair core,
quote verification with the v4 anchorer, the payload, the provenance in the config digest, and the
stage on a synthetic world with the fake model.

The prototype (``tomas/technique-roles`` df411eb) has no unit tests; the parse/repair cases here
follow its ``tools/technique_roles_smoke.py`` rules and the brief's changes to them (no-quote rows
dropped, unknown slugs moved to ``extra_techniques``, quotes anchored with :mod:`stack.anchor`).
Every repaired payload is also mapped onto the stored ``TECHNIQUE_ROLES`` type and built as an
arrow value: repair is only worth anything if what comes out is writable.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import pathlib
import re
import shutil
from typing import Any, cast

import pyarrow as pa
import pytest
import yaml

from stack.anchor import Anchorer
from stack.schemas.payloads.registry import REGISTRY
from stack.schemas.payloads.technique_roles import DOC_KINDS, ROLE_NAMES

REPO = pathlib.Path(__file__).resolve().parent.parent
WORKFLOWS_DIR = REPO / "workflows"
WF_DIR = WORKFLOWS_DIR / "annotate" / "technique_roles"
TAXONOMY = REPO / "taxonomy" / "taxonomy-v0.8.yaml"
FIXTURES = REPO / "tests" / "fixtures" / "technique_roles"


def _load_module(rel: str, name: str) -> Any:
    """By-path import, mirroring the runner's dispatch (not registered in ``sys.modules``)."""
    spec = importlib.util.spec_from_file_location(name, WORKFLOWS_DIR / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


judge = _load_module("annotate/technique_roles/judge.py", "_technique_roles_judge_under_test")
render = _load_module("annotate/technique_roles/render.py", "_technique_roles_render_under_test")
main = _load_module("annotate/technique_roles/main.py", "_technique_roles_main_under_test")

DOC = (
    "# Preference tuning without a reward model\n\n"
    "We fine-tune Llama-3-8B with DPO on 60k preference pairs drawn from the safety split.\n"
    "We compare against a PPO baseline trained on the same data, and we found the gap small.\n"
    "Instruction-following models are typically aligned with RLHF, which needs a reward model.\n"
    "Responses were scored by GPT-4 acting as a judge of harmlessness, following prior work.\n"
)
SLUGS = ["rlhf", "dpo", "dpo.ipo", "llm-as-judge"]
ANCHORER = Anchorer()


def normalize(obj: dict[str, Any], *, text: str = DOC) -> dict[str, Any]:
    return dict(judge.normalize(obj, slugs=SLUGS, text=text, anchorer=ANCHORER))


def validated(normalized: dict[str, Any]) -> dict[str, Any]:
    """Map onto ``TECHNIQUE_ROLES`` (every role an explicit integer 0-4) and build the arrow value."""
    stored: dict[str, Any] = judge.to_payload(normalized)
    pa.array([stored], type=REGISTRY["technique_roles"].payload_schema)
    for r in [*stored["techniques"], *stored["extra_techniques"]]:
        assert list(r["roles"]) == list(ROLE_NAMES) and all(0 <= v <= 4 for v in r["roles"].values())
    return stored


def row(slug: str, roles: dict[str, int], *quotes: str, **kw: Any) -> dict[str, Any]:
    return {"slug": slug, "roles": roles, "quotes": list(quotes), **kw}


DPO_Q = "We fine-tune Llama-3-8B with DPO on 60k preference pairs"
RLHF_Q = "Instruction-following models are typically aligned with RLHF"


# ─────────────────────────── (a) the menu and the prompt ───────────────────


@pytest.fixture(scope="module")
def tax() -> Any:
    return render.load_taxonomy(TAXONOMY)


def test_menu_lists_every_technique_once_grouped_by_family(tax: Any) -> None:
    raw = yaml.safe_load(TAXONOMY.read_text("utf-8"))
    slugs = [t["slug"] for t in raw["techniques"]]
    menu = render.full_menu(tax)
    listed = re.findall(r"^- (\S+) — ", menu, flags=re.M)
    assert sorted(listed) == sorted(slugs) and len(listed) == len(set(listed)) == 292
    families = re.findall(r"^## (\S+) — ", menu, flags=re.M)
    assert families == list(render.by_family(tax)) and len(families) == 17
    # members follow their family header, sorted by slug
    for block in menu.split("\n\n"):
        fam = re.match(r"## (\S+) — ", block)
        assert fam is not None
        members = re.findall(r"^- (\S+) — ", block, flags=re.M)
        assert members == sorted(members)
        assert all(e.family == fam.group(1) for e in tax.techniques if e.slug in members)
    # aliases are rendered (the taxonomy's own identity-resolution answer)
    with_alias = next(e for e in tax.techniques if e.aliases)
    assert f"- {with_alias.slug} — {with_alias.name} (aka: {', '.join(with_alias.aliases)}): " in menu


def test_variants_are_marked_by_their_dotted_slug(tax: Any) -> None:
    """The menu does not render ``variant_of``: the dotted prefix IS the parent (49/49 in v0.8)."""
    by_slug = {e.slug: e for e in tax.techniques}
    variants = [e for e in tax.techniques if "." in e.slug]
    assert len(variants) == 49
    for v in variants:
        parent = v.slug.rsplit(".", 1)[0]
        assert parent in by_slug and v.variant_of == parent and v.type == "variant"
    assert all(e.variant_of is None for e in tax.techniques if "." not in e.slug)
    prefix = main.system_template(main.prompt_file(None), main.menu())
    assert "A slug containing a dot is a variant of the entry named by\nits prefix" in prefix


def test_a_taxonomy_entry_missing_a_menu_field_is_refused(tmp_path: pathlib.Path) -> None:
    bad = tmp_path / "tax.yaml"
    bad.write_text(yaml.safe_dump({"version": "0.9", "techniques": [{"slug": "x", "name": "X", "type": "t",
                                                                     "family": "f"}]}))
    with pytest.raises(ValueError, match="lacks \\['description'\\]"):
        render.load_taxonomy(bad)


def test_prefix_is_rubric_menu_contract_and_the_document_is_the_user_message() -> None:
    m = main.menu()
    pf = main.prompt_file("technique-roles-v2")
    prefix = main.system_template(pf, m)
    assert prefix.endswith("\n\n" + main.INPUT_MARK + "\n")  # the marker closes it
    assert "{{ " not in prefix and "{%" not in prefix and "{{taxonomy_menu}}" not in prefix
    i_rubric, i_menu, i_out = (prefix.index(h) for h in ("# THE RUBRIC", "# THE MENU", "# YOUR OUTPUT"))
    assert i_rubric < i_menu < i_out
    assert "## 7. Grading procedure" in prefix and "## 8. Design notes" not in prefix and "changelog:" not in prefix
    # the output contract's lists are plain text in the file: they must still be the payload's
    assert f"The {len(ROLE_NAMES)} roles, each an integer 1–4" in prefix and ", ".join(ROLE_NAMES) + ".\n" in prefix
    assert ' "doc_kind": "' + " | ".join(DOC_KINDS) + '",\n' in prefix
    # v2: the sweeps come first in the contract, with every list the schema names
    contract = prefix[i_out:]
    assert contract.index('{"sweeps": {') < contract.index('"doc_kind"') < contract.index('"techniques"')
    sweep_props = judge.output_schema()["properties"]["sweeps"]["properties"]
    assert all(f'"{k}"' in contract for k in sweep_props) and len(sweep_props) == 4
    assert m.text in prefix and "- dpo — " in prefix and "# THE DOCUMENT" not in prefix
    assert pf.system_placeholders == {"taxonomy_menu"} and pf.user_placeholders == {"document", "truncation"}
    user = main.user_message(pf, "DOC TEXT here.")
    assert user == ("# THE DOCUMENT\n\nDOC TEXT here.\n\nNow emit the JSON object described above, in a ```json "
                    "fenced block, and nothing else.\n")
    assert m.slugs[:1] == (yaml.safe_load(TAXONOMY.read_text())["techniques"][0]["slug"],)


def test_v1_stays_loadable_for_ab_and_shares_the_user_part() -> None:
    """``technique-roles-v1`` (rubric v0.3.1, the pre-prompt-file prompt) still renders, with the same user
    part as v2 (so the frozen eval set's input digests hold for both), and a different system part."""
    m = main.menu()
    v1, v2 = main.prompt_file("technique-roles-v1"), main.prompt_file("technique-roles-v2")
    assert v1.includes == ("rubric-v0.3.1.md",) and v2.includes == ("rubric-v0.4.0.md",)
    assert v1.user_template == v2.user_template
    s1 = main.system_template(v1, m)
    assert s1.endswith("technique.\n\n" + main.INPUT_MARK) and '"sweeps"' not in s1
    assert s1 != main.system_template(v2, m)


def test_document_is_truncated_at_the_cap_and_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    pf = main.prompt_file(None)
    monkeypatch.setattr(main, "DOC_CHAR_CAP", 50)
    user = main.user_message(pf, "x" * 50 + "TAIL")
    assert "TAIL" not in user and "\n[DOCUMENT TRUNCATED AT CHARACTER CAP]\n\nNow emit" in user
    assert "TRUNCATED" not in main.user_message(pf, "x" * 50)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_v2_renders_as_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    """``prompts/technique-roles-v2.md`` + ``rubric-v0.4.0.md`` + the menu fill render as recorded in
    ``tests/fixtures/technique_roles/expected-v2.json`` over a frozen taxonomy subset (every family, a
    variant, aliases, non-ASCII, one synthetic entry in a family with no gloss) and four documents
    (non-ASCII and placeholder-shaped text, a blank one, exactly the cap, past the cap). A pinned prompt
    is never edited (a new version is a new file). Red under: one changed byte of the prompt file or its
    rubric include, a menu rendering change, or a changed fill (the truncation note, the window)."""
    exp = json.loads((FIXTURES / "expected-v2.json").read_text("utf-8"))
    taxonomy = FIXTURES / "taxonomy.yaml"
    assert hashlib.sha256(taxonomy.read_bytes()).hexdigest() == exp["taxonomy_sha256"]
    monkeypatch.setattr(main, "TAXONOMY_PATH", taxonomy)
    m = main.menu()
    assert (_sha(m.text), len(m.text)) == (exp["menu_sha256"], exp["menu_chars"])
    pf = main.prompt_file(exp["prompt"])
    assert list(pf.includes) == exp["includes"] and pf.digest == exp["prompt_file_digest"]
    system = main.system_template(pf, m)
    assert (_sha(system), len(system)) == (exp["system_sha256"], exp["system_chars"])
    assert len(exp["cases"]) == 4
    for case in exp["cases"]:
        text = "".join(part * n for part, n in case["text"])
        assert (len(text), len(text) > main.DOC_CHAR_CAP) == (case["text_chars"], case["truncated"])
        user = main.user_message(pf, text)
        if case["id"] == "small":
            assert user == (FIXTURES / "expected-v2-small.user.txt").read_text("utf-8")
        assert (_sha(user), len(user)) == (case["user_sha256"], case["user_chars"]), case["id"]


@pytest.mark.parametrize("name", ["technique-roles-v1", "technique-roles-v2"])
def test_rubric_version_is_in_the_include_name_and_the_notes(name: str) -> None:
    """The judge-visible rubric is ``prompts/rubric-v<version>.md`` (what the prompt includes); its
    metadata and design notes are ``rubric-v<version>.notes.md`` (frontmatter ``version``), not sent."""
    pf = main.prompt_file(name)
    (inc,) = pf.includes
    m = re.fullmatch(r"rubric-v(\d+(?:\.\d+)+)\.md", inc)
    assert m is not None, inc
    notes = (WF_DIR / "prompts" / f"rubric-v{m.group(1)}.notes.md").read_text("utf-8")
    assert re.search(rf'^version: "{re.escape(m.group(1))}"$', notes, flags=re.M)
    assert re.search(rf"^  - {re.escape(m.group(1))} — ", notes, flags=re.M)  # its changelog entry
    assert "## 8. Design notes" in notes and "## 8. Design notes" not in (WF_DIR / "prompts" / inc).read_text()
    dep = re.search(r"^depends_on: (\S+)", notes, flags=re.M)
    assert dep is not None and (REPO / dep.group(1)).is_file() and REPO / dep.group(1) == main.TAXONOMY_PATH


@pytest.mark.parametrize("name", ["technique-roles-v1", "technique-roles-v2"])
def test_the_twelve_role_categories_are_the_rubric_ones(name: str) -> None:
    rubric = (WF_DIR / "prompts" / main.prompt_file(name).includes[0]).read_text()
    in_rubric = re.findall(r"^### 3\.\d+ `([a-z_]+)` — ", rubric, flags=re.M)
    assert tuple(in_rubric) == ROLE_NAMES and len(ROLE_NAMES) == 12


def test_the_v040_precedents_are_in_the_rubric() -> None:
    """The smoke-review fixes are in the judge-visible rubric (the notes' changelog lists them)."""
    rubric = (WF_DIR / "prompts" / "rubric-v0.4.0.md").read_text()
    for needle in ("**One sentence → 1.**", "11. **An evaluation technique applied is `used`",
                   "12. **System cards: every shipped mitigation is attested", "13. **Generic surfaces",
                   "**The document's own new framework built on a catalogue technique**",
                   "**Hedged inference is still inference.**", "REQUIRED: deployment sweep",
                   "REQUIRED: recommendation sweep", "controls sweep", "REQUIRED: menu-gap sweep"):
        assert needle in rubric, needle
    assert re.findall(r"^(\d+)\. \*\*", rubric.split("## 7. Grading procedure")[1], flags=re.M) == [
        str(i) for i in range(1, 13)]


# ─────────────────────────── (b) reply parsing ─────────────────────────────


def test_extracts_json_from_a_fenced_block_with_prose_around_it() -> None:
    raw = 'Here you go.\n\n```json\n{"doc_kind": "paper", "techniques": []}\n```\nDone.'
    assert judge.extract_json(raw) == {"doc_kind": "paper", "techniques": []}


def test_repairs_a_trailing_comma() -> None:
    raw = '```json\n{"doc_kind": "paper", "techniques": [],}\n```'
    assert judge.extract_json(raw)["techniques"] == []


@pytest.mark.parametrize("raw", [
    "",
    "I could not complete this task.",
    # truncated mid-reply (the prototype's finish_reason=length shape)
    '```json\n{"doc_kind": "paper", "techniques": [{"slug": "dpo", "roles": {"mentioned": 4, "us',
])
def test_an_unparseable_or_truncated_reply_raises_rather_than_grading_empty(raw: str) -> None:
    with pytest.raises(judge.JudgeParseError):
        judge.extract_json(raw)


def test_a_json_array_is_not_a_payload() -> None:
    with pytest.raises(judge.JudgeParseError):
        judge.extract_json('```json\n[{"slug": "dpo"}]\n```')


def test_output_schema_is_self_contained_and_permissive() -> None:
    import json

    schema = judge.output_schema()
    assert "$ref" not in json.dumps(schema) and "additionalProperties" not in schema
    assert schema["required"] == ["doc_kind", "techniques"]  # D275: sweeps stays optional on the wire
    sweeps = schema["properties"]["sweeps"]
    assert list(schema["properties"])[0] == "sweeps" and "required" not in sweeps
    assert set(sweeps["properties"]) == {*judge.SWEEP_LISTS, "menu_gaps"}


def test_roles_is_a_fixed_key_object_with_optional_keys_not_an_open_map() -> None:
    """R6 item 5: no open map anywhere (the Anthropic route filled one with ``{}``); every role a
    named integer property, none required (zero is written by omission, rubric §6); and the
    schema is still not in strict shape, so OpenAI keeps refusing it (luna's degrade path, D275)."""
    import json

    from stack.llm import strict_schema

    schema = judge.output_schema()
    assert "additionalProperties" not in json.dumps(schema)
    items = schema["properties"]["techniques"]["items"]["properties"]
    extras = schema["properties"]["extra_techniques"]["items"]["properties"]
    for roles in (items["roles"], extras["roles"]):
        assert roles["type"] == "object" and "required" not in roles
        assert roles["properties"] == {r: {"type": "integer"} for r in ROLE_NAMES}
    assert strict_schema(schema) != schema and main.SCHEMA == schema


def test_an_answer_whose_every_row_has_empty_roles_is_degenerate_and_others_are_not() -> None:
    hole = normalize({"doc_kind": "paper", "techniques": [row("dpo", {}, DPO_Q), row("rlhf", {}, RLHF_Q)],
                      "extra_techniques": [{"name_as_written": "SteerLM", "roles": {}, "quotes": [DPO_Q]}]})
    assert hole["techniques"] == [] and judge.roles_degenerate(hole)
    zeros = normalize({"doc_kind": "paper", "techniques": [row("dpo", {"mentioned": 0, "used": 0}, DPO_Q)]})
    assert judge.roles_degenerate(zeros)  # all-zero strengths are the same empty map
    none = normalize({"doc_kind": "news", "techniques": [], "flags": ["no_techniques_found"]})
    assert not judge.roles_degenerate(none)  # a genuine "no technique here" stays empty
    mixed = normalize({"doc_kind": "paper", "techniques": [row("dpo", {}, DPO_Q), row("rlhf", {"mentioned": 2})]})
    assert mixed["techniques"] == [] and not judge.roles_degenerate(mixed)  # a row had a role (no quote)
    kept = normalize({"doc_kind": "paper", "techniques": [row("dpo", {}, DPO_Q), row("rlhf", {"mentioned": 2}, RLHF_Q)]})
    assert [t["slug"] for t in kept["techniques"]] == ["rlhf"] and not judge.roles_degenerate(kept)


# ─────────────────────────── (c) the repair rules ──────────────────────────


def test_a_clean_answer_is_kept_in_taxonomy_order_with_no_repairs() -> None:
    out = normalize({"doc_kind": "paper", "techniques": [
        row("dpo", {"mentioned": 4, "used": 4}, DPO_Q),
        row("rlhf", {"mentioned": 1}, RLHF_Q, flags=["supportive"], note="background lineage"),
    ], "extra_techniques": [], "flags": []})
    assert out["repairs"] == [] and [t["slug"] for t in out["techniques"]] == ["rlhf", "dpo"]
    stored = validated(out)
    dpo = stored["techniques"][1]
    assert dpo["roles"]["used"] == 4 and dpo["roles"]["critiqued"] == 0 and dpo["flags"] == []
    assert dpo["quotes"] == [{"text": DPO_Q, "verified": True}] and dpo["note"] is None
    rlhf = stored["techniques"][0]
    assert rlhf["flags"] == ["supportive"] and rlhf["note"] == "background lineage"


def test_no_quote_no_row() -> None:
    out = normalize({"doc_kind": "paper", "techniques": [
        row("dpo", {"mentioned": 2}),
        row("rlhf", {"mentioned": 1}, "   "),
        row("llm-as-judge", {"used": 2}, "Responses were scored by GPT-4 acting as a judge"),
    ], "extra_techniques": [{"name_as_written": "KTO", "gloss": "a loss", "roles": {"mentioned": 2}}]})
    assert [t["slug"] for t in out["techniques"]] == ["llm-as-judge"]
    assert {"no_quote_dropped:dpo", "no_quote_dropped:rlhf", "no_quote_dropped:extra:KTO"} <= set(out["repairs"])
    assert out["extra_techniques"] == []
    validated(out)


def test_mentioned_is_clamped_up_to_the_max_of_the_other_roles() -> None:
    out = normalize({"doc_kind": "paper", "techniques": [row("dpo", {"used": 3, "evaluated": 2}, DPO_Q)]})
    assert out["techniques"][0]["roles"] == {"mentioned": 3, "used": 3, "evaluated": 2}
    assert "mentioned_clamped:dpo:0->3" in out["repairs"]
    assert validated(out)["techniques"][0]["roles"]["mentioned"] == 3


def test_an_unknown_slug_moves_to_extra_techniques_and_is_never_minted() -> None:
    out = normalize({"doc_kind": "paper", "techniques": [
        row("dpo.kto", {"mentioned": 2, "used": 2}, DPO_Q, note="a DPO relative"),
        row("constitutional-dpo", {"mentioned": 1}, RLHF_Q),
    ], "extra_techniques": []})
    assert out["techniques"] == []
    kto, cdpo = out["extra_techniques"]
    assert (kto["name_as_written"], kto["hint_technique"], kto["gloss"]) == ("dpo.kto", "dpo", "a DPO relative")
    assert cdpo["hint_technique"] is None and cdpo["roles"] == {"mentioned": 1}
    assert {"unknown_slug_to_extra:dpo.kto", "unknown_slug_to_extra:constitutional-dpo"} <= set(out["repairs"])
    validated(out)


def test_duplicates_bad_roles_empty_rows_and_doc_kind_are_repaired_and_recorded() -> None:
    out = normalize({"doc_kind": "poem", "techniques": [
        row("dpo", {"mentioned": 4, "used": 4}, DPO_Q),
        row("dpo", {"mentioned": 1}, DPO_Q),
        row("rlhf", {"mentioned": 9, "vibes": 2, "used": "high", "discussed": True}, RLHF_Q),  # type: ignore[dict-item]
        row("llm-as-judge", {"mentioned": 0}, "Responses were scored by GPT-4"),
        "not a row",
    ], "extra_techniques": [{"name_as_written": "  ", "roles": {"mentioned": 2}, "quotes": [DPO_Q]},
                            {"name_as_written": "SteerLM", "hint_technique": "nonesuch", "roles": {"used": 2},
                             "quotes": [DPO_Q]}]})
    assert out["doc_kind"] == "other"
    assert [t["slug"] for t in out["techniques"]] == ["dpo"]
    assert {"doc_kind_unknown:poem", "duplicate_row:dpo", "bad_strength:rlhf:mentioned:9", "unknown_role:rlhf:vibes",
            "bad_strength:rlhf:used:high", "bad_strength:rlhf:discussed:True", "empty_roles_dropped:rlhf",
            "empty_roles_dropped:llm-as-judge", "bad_row", "extra_unnamed", "unknown_hint:nonesuch",
            "mentioned_clamped:extra:SteerLM:0->2"} <= set(out["repairs"])
    (steer,) = out["extra_techniques"]
    assert steer["hint_technique"] is None and steer["roles"] == {"mentioned": 2, "used": 2}
    validated(out)


def test_quotes_are_capped_and_nul_stripped() -> None:
    long_q = DOC[2:2 + 400]  # a real span, longer than the cap: the capped prefix still anchors
    out = normalize({"doc_kind": "paper",
                     "techniques": [row("dpo", {"used": 2}, long_q, "We fine-\x00tune Llama-3-8B")]})
    (q1, q2) = out["techniques"][0]["quotes"]
    assert len(q1["text"]) == judge.MAX_QUOTE_CHARS and q1["verified"]
    assert q2["text"] == "We fine-tune Llama-3-8B" and q2["verified"]
    assert {"quote_truncated:dpo", "nul_stripped"} <= set(out["repairs"])


def test_a_document_level_flag_and_an_empty_answer_pass_through() -> None:
    out = normalize({"doc_kind": "news", "techniques": [], "flags": ["no_techniques_found", "menu_gap:vibe tuning"]})
    assert out["flags"] == ["no_techniques_found", "menu_gap:vibe tuning"] and out["techniques"] == []
    assert validated(out)["extra_techniques"] == [] and out["repairs"] == [] and out["sweeps"] is None


def test_no_techniques_found_follows_the_techniques_list() -> None:
    """Set when ``techniques`` is empty (extras do not count: they are off-catalogue), cleared when it
    is not; each change recorded. Red under: _doc_flags passing the judge's flags through."""
    only_extra = normalize({"doc_kind": "paper", "techniques": [], "extra_techniques": [
        {"name_as_written": "llmSHAP", "roles": {"introduced": 4}, "quotes": [DPO_Q]}]})
    assert only_extra["flags"] == ["no_techniques_found"] and "no_techniques_found_added" in only_extra["repairs"]
    contradicted = normalize({"doc_kind": "paper", "techniques": [row("dpo", {"used": 3}, DPO_Q)],
                              "flags": ["no_techniques_found"]})
    assert contradicted["flags"] == [] and "no_techniques_found_dropped" in contradicted["repairs"]


def test_sweeps_become_menu_gap_flags_and_unmatched_entries_are_recorded() -> None:
    """``sweeps.menu_gaps`` → ``menu_gap:<surface>`` flags (merged with the judge's own, deduplicated
    case-insensitively, ``menu_gap: x`` respelled); a sweep entry naming a menu slug with no row →
    ``sweep_without_row``; entries naming no slug are not checked; nothing is synthesized and the
    sweeps are not in the stored payload. A ``suspected_use`` row (with ``claims_absent``) and a
    ``considered`` row the sweeps announced are kept as graded."""
    out = normalize({
        "sweeps": {
            "deployment_claims": [
                {"system": "GLM-5.3", "technique": "rlhf", "quote": RLHF_Q, "direction": "does_not_use"},
                {"system": "GLM-5.3", "technique": "llm-as-judge", "quote": DPO_Q, "direction": "uses"}],
            "recommendations": [{"technique": "dpo", "quote": DPO_Q, "direction": "recommended"},
                                {"technique": "incident reporting", "quote": "x"}],
            "controls_discussed": [{"surface": "containment", "technique": "dpo.ipo"}, "junk", {"surface": "s"}],
            "menu_gaps": ["safety cases", "Vibe Tuning", "", 7, "x" * 200]},
        "doc_kind": "commentary",
        "techniques": [row("rlhf", {"suspected_use": 2}, RLHF_Q, flags=["claims_absent"]),
                       row("dpo", {"considered": 1}, DPO_Q, flags=["recommended"])],
        "flags": ["menu_gap: vibe tuning", "menu_gap:"]})
    assert out["flags"] == ["menu_gap:vibe tuning", "menu_gap:safety cases", "menu_gap:" + "x" * judge.MAX_GAP_CHARS]
    assert [r for r in out["repairs"] if r.startswith("sweep_without_row:")] == [
        "sweep_without_row:deployment_claims:llm-as-judge", "sweep_without_row:controls_discussed:dpo.ipo"]
    assert out["sweeps"] == {"deployment_claims": 2, "recommendations": 2, "controls_discussed": 3, "menu_gaps": 5}
    rlhf, dpo = out["techniques"]
    assert (rlhf["roles"], rlhf["flags"]) == ({"mentioned": 2, "suspected_use": 2}, ["claims_absent"])
    assert (dpo["roles"], dpo["flags"]) == ({"mentioned": 1, "considered": 1}, ["recommended"])
    stored = validated(out)
    assert "sweeps" not in stored and stored["flags"] == out["flags"]


def test_menu_gap_flags_are_capped() -> None:
    out = normalize({"doc_kind": "policy", "techniques": [row("dpo", {"considered": 1}, DPO_Q)],
                     "sweeps": {"menu_gaps": [f"gap {i}" for i in range(50)]}})
    assert len(out["flags"]) == judge.MAX_GAPS and out["flags"][-1] == f"menu_gap:gap {judge.MAX_GAPS - 1}"


def test_a_junk_shaped_response_still_produces_a_writable_payload() -> None:
    out = normalize({"doc_kind": None, "techniques": "nope", "extra_techniques": [None, {"roles": []}],
                     "flags": [None, 3]})
    stored = validated(out)
    assert stored["techniques"] == [] and stored["flags"] == ["None", "3", "no_techniques_found"]


# ─────────────────────────── (d) quote verification ────────────────────────


def test_a_verbatim_quote_verifies_and_an_invented_one_does_not() -> None:
    out = normalize({"doc_kind": "paper", "techniques": [
        row("dpo", {"used": 3}, DPO_Q, "We train with DPO on two million synthetic dialogues"),
        row("rlhf", {"mentioned": 2}, "RLHF is the gold standard for alignment in 2025"),
    ]})
    rlhf, dpo = out["techniques"]  # SLUGS order: rlhf first
    assert rlhf["slug"] == "rlhf" and rlhf["flags"] == ["quote_unverified"]
    assert rlhf["quotes"] == [{"text": "RLHF is the gold standard for alignment in 2025", "verified": False}]
    assert dpo["slug"] == "dpo" and [q["verified"] for q in dpo["quotes"]] == [True, False]
    assert "quote_unverified" not in dpo["flags"]  # one verified quote is enough for the row
    assert out["repairs"].count("quote_unverified:dpo") == 1 and "quote_unverified:rlhf" in out["repairs"]


def test_an_unverified_extra_row_is_kept_and_flagged() -> None:
    out = normalize({"doc_kind": "paper", "techniques": [], "extra_techniques": [
        {"name_as_written": "ORPO", "gloss": "odds-ratio preference optimisation", "roles": {"used": 2},
         "quotes": ["We apply ORPO to every checkpoint"]}]})
    (orpo,) = out["extra_techniques"]
    assert orpo["flags"] == ["quote_unverified"] and orpo["quotes"][0]["verified"] is False
    assert validated(out)["extra_techniques"][0]["flags"] == ["quote_unverified"]


@pytest.mark.parametrize("quote", [
    "We fine-tune  Llama-3-8B\nwith DPO on 60k preference pairs",          # whitespace
    "We fine‑tune Llama‑3‑8B with DPO on 60k preference pairs",          # typographic hyphens
    "We fine-tune Llama-3-8B with DPO ... trained on the same data, and we found the gap small",  # elision
    "We fine-tune Llama-3-8B with DPO on 60k preference pairs drawn … PPO baseline trained on the same data",
])
def test_presentational_differences_and_elisions_verify(quote: str) -> None:
    assert judge.verify_quote(quote, DOC, ANCHORER)


@pytest.mark.parametrize("quote", [
    "We fine-tune Llama-3-8B with DPO on 80k preference pairs",           # a changed number
    "We fine-tune Llama-3-70B with PPO on 60k preference pairs",          # a changed method
    "fine-tune ... pairs",                                                 # elision of short fragments
    "We fine-tune Llama-3-8B with DPO ... we never compared to PPO at all",  # half invented
])
def test_fabrication_is_caught(quote: str) -> None:
    assert not judge.verify_quote(quote, DOC, ANCHORER)


LOOSE = json.loads((FIXTURES / "loose_quotes.json").read_text("utf-8"))["cases"]


@pytest.mark.parametrize("case", LOOSE, ids=[f"{c['doc_id']}-{i}" for i, c in enumerate(LOOSE)])
def test_smoke_run_false_negatives_verify_and_the_paraphrase_does_not(case: dict[str, Any]) -> None:
    """The quotes the strict anchor left unverified in the v4 smoke run (inline citations dropped, LaTeX
    vs Unicode, a converter-split table row, spacing around brackets, a capitalised first word), on
    their copied context: verified, by the loose tier where the strict one fails on the snippet, and the
    one genuine paraphrase ("We assess…" for "our goal is to assess…") still not. Red under: the loose
    tier removed, or any one of its projections (citations, LaTeX, tables) dropped."""
    how = judge.verify_how(case["quote"], case["context"], ANCHORER)
    if case["expect"] is None:
        assert how is None
        return
    assert how == case["expect"] if case["expect"] == "loose" else how is not None
    assert judge.loosen(case["quote"]) in judge.loosen(case["context"])  # an exact hit on the projection


TABLE = ("Table 1: Overall metrics\n\n|**4o image generation**|**Not_unsafe**|**Not_**|\n|---|---|---|\n"
         "|With system mitigations only (prompt|0.955|0.941||\n|blocking and output blocking)||||\n")
CITED = ("Within this broader category, Constitutional Classifiers Sharma et al. (2025) currently represent the "
         "state-of-the-art defense against adversarial jailbreaks, utilizing specialized models trained on "
         "synthetic, rule-based data. We use GPT-4o as an automated judge, following prior multi-turn studies "
         "( Rahman et al., 2025 ; Ren et al., 2024 ) . The classifier [12] was trained using LoRA adapters "
         "( $r=16$ , $\\alpha=32$ ) and the safety filter (trained on synthetic data) refuses harmful requests.")


@pytest.mark.parametrize("quote", [
    "Constitutional Classifiers currently represent the state-of-the-art defense against adversarial jailbreaks",
    "We use GPT-4o as an automated judge, following prior multi-turn studies.",
    "The classifier was trained using LoRA adapters (r=16, α=32)",
    "the classifier was trained using LoRA adapters ($r=16$, $\\alpha=32$)",
])
def test_loose_tier_positives(quote: str) -> None:
    assert judge.verify_how(quote, CITED, ANCHORER) in ("strict", "loose")


@pytest.mark.parametrize("quote", [
    "Constitutional Classifiers currently represent the strongest defense against all jailbreaks",  # reworded
    "The classifier was trained using LoRA adapters (r=8, α=32)",           # a changed number
    "the safety filter refuses harmful requests",                            # a dropped non-citation clause
    "We use GPT-4o as an automated judge, unlike prior multi-turn studies.",  # a changed claim
    "Sharma et al. argue that classifiers trained on synthetic data refuse harmful requests",  # stitched
])
def test_loose_tier_still_refuses_paraphrases(quote: str) -> None:
    assert judge.verify_how(quote, CITED, ANCHORER) is None


def test_a_converter_split_table_row_verifies_loosely_and_its_numbers_are_not_skipped() -> None:
    assert judge.verify_how("With system mitigations only (prompt blocking and output blocking)", TABLE,
                            ANCHORER) == "loose"
    assert judge.verify_how("With system mitigations only (prompt blocking and output filtering)", TABLE,
                            ANCHORER) is None
    assert judge.verify_how("With system mitigations only 0.975", TABLE, ANCHORER) is None


@pytest.mark.parametrize(("quote", "how"), [
    ("|With system mitigations only (prompt|0.955|0.941||", "strict"),        # the row as written
    ("|With system mitigations only (prompt|0.955|0.947||", None),            # one number changed
    ("|With system mitigations only (prompt|0.941|0.955||", None),            # numbers swapped
    ("With system mitigations only (prompt blocking and output blocking) 0.955 0.941", "loose"),
    ("With system mitigations only (prompt blocking and output blocking) 0.955 0.948", None),
    ("With system mitigations only (prompt blocking and output blocking) 0.941", None),  # a figure skipped
])
def test_table_numbers_must_match_on_the_loose_tier(quote: str, how: str | None) -> None:
    """Review finding (round 2b): the loose tier dropped numeric table cells on both sides, so a table-row
    quote with a wrong number verified. Cells are kept now (a continuation row merges into its row), so
    the anchorer's digit-run rule sees every number of the quote, in order. Red under: numeric cells
    dropped again, or the continuation merge putting the numbers out of order."""
    assert judge.verify_how(quote, TABLE, ANCHORER) == how


@pytest.mark.parametrize(("quote", "how"), [
    ("The classifier [12] was trained using LoRA adapters", "strict"),  # spacing only differs from the text
    ("The classifier [13] was trained using LoRA adapters", None),      # a citation number changed
    ("Constitutional Classifiers Sharma et al. (2024) currently represent", None),
    ("Constitutional Classifiers Sharma et al. (2025) currently represent", "strict"),
])
def test_a_quote_that_carries_a_citation_keeps_its_numbers(quote: str, how: str | None) -> None:
    """Citations are removed from the text only when the quote has none: a quote that cites is compared
    with citations kept on both sides, so its citation numbers must match too."""
    text = CITED.replace("The classifier [12] was", "The classifier  [12]  was")
    assert judge.verify_how(quote, text, ANCHORER) == how


@pytest.mark.parametrize("case", [c for c in LOOSE if c["expect"] is not None],
                         ids=[c["doc_id"] for c in LOOSE if c["expect"] is not None])
def test_the_loose_projection_of_a_quote_keeps_its_digits_in_order(case: dict[str, Any]) -> None:
    import unicodedata

    q = case["quote"]
    cites = not judge.has_citation(q)
    assert re.findall(r"\d+", judge.loosen(q, citations=cites)) == re.findall(
        r"\d+", unicodedata.normalize("NFKC", q))


def test_a_loosely_verified_quote_is_recorded() -> None:
    text = "Constitutional Classifiers Sharma et al. (2025) currently represent the state of the art."
    out = normalize({"doc_kind": "paper", "techniques": [row(
        "dpo", {"mentioned": 1}, "Constitutional Classifiers currently represent the state of the art.")]}, text=text)
    assert out["techniques"][0]["quotes"][0]["verified"] is True
    assert "quote_verified_loose:dpo" in out["repairs"] and "quote_unverified:dpo" not in out["repairs"]


# ─────────────────────────── (e) registry, provenance ──────────────────────


def test_registry_entry() -> None:
    spec = REGISTRY["technique_roles"]
    assert (spec.version, spec.grain, spec.basis_kind, spec.evidence, spec.reads) == (2, "document", "text", True, ())
    # after model_roles (and its records: a record entry may sit between them)
    assert list(REGISTRY).index("technique_roles") > list(REGISTRY).index("model_roles")
    assert set(spec.text_bearing) == {"techniques[].quotes[].text", "techniques[].note",
                                      "extra_techniques[].quotes[].text", "extra_techniques[].gloss"}
    # a new typed field of the documents/corpus annotations block (D43 schema bump), after model_roles
    from stack.schemas.families import family

    ann = family("corpus").schema.field("annotations").type
    names = [ann.field(i).name for i in range(ann.num_fields)]
    assert names.index("technique_roles") > names.index("model_roles")


def _copy_inputs(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    here = tmp_path / "wf"
    shutil.copytree(WF_DIR / "prompts", here / "prompts")
    taxonomy = tmp_path / "taxonomy-v0.8.yaml"
    shutil.copy(TAXONOMY, taxonomy)
    return here, taxonomy


@pytest.mark.parametrize("edit", ["prompt", "rubric", "taxonomy"])
def test_prompt_rubric_and_taxonomy_are_in_the_config_digest(edit: str, tmp_path: pathlib.Path,
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    """Each prompt input's bytes change ``config_digest`` (and so every work key): the prompt file, its
    rubric include (the prompt file's digest) and the taxonomy (its sha256, and the menu it fills)."""
    from stack.annotate import config_digest

    llm = annotate_stage.stack_config().llm
    spec = REGISTRY["technique_roles"]
    pin = {"name": "technique-roles-v2", "role": "extract_main", "model": LUNA}
    real = main.prompt(pin, llm)
    assert real.name == "technique-roles-v2" and real.models[0] == LUNA
    assert real.template_digests[-2:] == (main.prompt_file(None).digest,
                                          hashlib.sha256(TAXONOMY.read_bytes()).hexdigest())
    here, taxonomy = _copy_inputs(tmp_path)
    monkeypatch.setattr(main, "HERE", here)
    monkeypatch.setattr(main, "TAXONOMY_PATH", taxonomy)
    before = main.prompt(pin, llm)
    assert before == real  # content, not location
    assert before == main.prompt(pin, annotate_stage.stack_config().llm)  # deterministic
    target = {"prompt": here / "prompts" / "technique-roles-v2.md", "rubric": here / "prompts" / "rubric-v0.4.0.md",
              "taxonomy": taxonomy}[edit]
    # a byte edit the judge never sees (a YAML comment) still counts: provenance is the file
    target.write_text(target.read_text() + ("\n# an edit\n" if edit == "taxonomy" else "<!-- an edit -->\n"))
    clear_cache()  # prompt files are read once per process
    after = main.prompt(pin, llm)
    assert config_digest(spec, pin, after) != config_digest(spec, pin, before)
    moved = [i for i, (x, y) in enumerate(zip(after.template_digests, before.template_digests, strict=True))
             if x != y]
    # the system template digests (luna, its fallback) move with what is sent: the prompt edit is in the
    # user part, the YAML comment is not sent; the file and taxonomy digests move with their bytes
    assert moved == {"prompt": [2], "rubric": [0, 1, 2], "taxonomy": [3]}[edit], moved


def test_the_pin_name_selects_the_prompt_file_and_bad_names_refuse(tmp_path: pathlib.Path,
                                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    """``name`` selects ``prompts/<name>.md`` (no name: ``technique-roles-v2``); a name with a path
    separator, with no file, or naming an include (no split line) is refused before any call."""
    from stack.annotate import config_digest

    llm = annotate_stage.stack_config().llm
    spec = REGISTRY["technique_roles"]
    assert main.prompt({"role": "extract_main"}, llm).name == main.DEFAULT_NAME == "technique-roles-v2"
    here, _ = _copy_inputs(tmp_path)
    v2 = (here / "prompts" / "technique-roles-v2.md").read_text("utf-8")
    (here / "prompts" / "technique-roles-v9.md").write_text(v2.replace("Now emit the JSON object", "Emit the JSON"))
    monkeypatch.setattr(main, "HERE", here)
    p1, p2 = ({"name": n, "model": LUNA} for n in ("technique-roles-v2", "technique-roles-v9"))
    pp1, pp2 = main.prompt(p1, llm), main.prompt(p2, llm)
    assert (pp1.name, pp2.name) == ("technique-roles-v2", "technique-roles-v9")
    v1 = main.prompt({"name": "technique-roles-v1", "model": LUNA}, llm)  # A/B: v1 is still selectable
    assert v1.name == "technique-roles-v1" and v1.template_digests[:2] != pp1.template_digests[:2]
    assert pp1.template_digests[:2] == pp2.template_digests[:2]  # the system part is the same
    assert pp1.template_digests[2] != pp2.template_digests[2]  # the file is not
    assert config_digest(spec, p1, pp1) != config_digest(spec, p2, pp2)
    assert main.user_message(main.prompt_file("technique-roles-v9"), "D").endswith("\nEmit the JSON described "
                                                                                     "above, in a ```json fenced "
                                                                                     "block, and nothing else.\n")
    with pytest.raises(StageError, match="is not a prompt name"):
        main.prompt({"name": "../../model_roles/prompts/model-roles-v1"}, llm)
    with pytest.raises(StageError, match="no prompt file"):
        main.prompt({"name": "technique-roles-v0"}, llm)
    with pytest.raises(StageError, match="exactly one"):  # the rubric include is no prompt
        main.prompt({"name": "rubric-v0.4.0"}, llm)


def test_unknown_pin_keys_and_roles_refuse() -> None:
    llm = annotate_stage.stack_config().llm
    with pytest.raises(StageError, match="plan pin keys"):
        main.prompt({"name": "x", "temperature": 0}, llm)
    with pytest.raises(StageError, match="not an llm.yaml role"):
        main.prompt({"name": "x", "role": "nonesuch"}, llm)


def test_ask_refuses_a_taxonomy_that_changed_since_the_pin(tmp_path: pathlib.Path,
                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    """The run's pin hashed one taxonomy; ``ask`` on another (edited mid-run) refuses before any call:
    the config digest would not describe the answers."""
    from stack.annotate import AskContext, AskItem

    cfg = annotate_stage.stack_config()
    pin = {"name": "technique-roles-v2", "model": LUNA}
    pp = main.prompt(pin, cfg.llm)

    class NoCall:
        def complete(self, **_kw: Any) -> Any:
            raise AssertionError("no call expected")

    item = AskItem(doc_id="d", source_id="d", basis_kind="text", basis_digest="b", fields=None, row={"doc_id": "d"},
                   license_class="restricted", attempt=1, load_text=lambda: DOC)
    actx = AskContext(run_id="r", stage=main.WORKFLOW_ID, store_root=None, pin=pp, plan_pin=pin, options={},
                      budget_usd=None, _config=lambda: cfg, _client=cast(Any, NoCall()))
    _, taxonomy = _copy_inputs(tmp_path)
    taxonomy.write_text(taxonomy.read_text() + "\n# an edit\n")
    monkeypatch.setattr(main, "TAXONOMY_PATH", taxonomy)
    with pytest.raises(StageError, match="changed during the run"):
        main.ask(item, actx)


# ─────────────────────────── (f) the stage on the fake model ───────────────


import stack.annotate.stage as annotate_stage  # noqa: E402
from stack.annotate.prompt_files import clear_cache  # noqa: E402
from stack.checks.verify import run_checks  # noqa: E402
from stack.stage_runner import StageError, run_stage  # noqa: E402
from tests.fakes.annotate import (  # noqa: E402
    AT_RUN,
    doc_with_text,
    fold_corpus,
    rows_of,
    write_documents,
    write_plan_file,
)
from tests.fakes.llm import FakeLlm, malformed, ok, prompt_text, truncated  # noqa: E402
from tests.synth.records import SERIES

LUNA = "openai/gpt-6-luna"
REPLY_A = {
    "doc_kind": "paper",
    "techniques": [
        row("dpo", {"mentioned": 4, "used": 4}, DPO_Q),
        row("ppo-for-alignment", {"used": 2}, "We compare against a PPO baseline trained on the same data"),
        row("rlhf", {"mentioned": 2}, "RLHF is the gold standard for alignment"),  # fabricated
        row("llm-as-judge", {"mentioned": 2, "used": 2}),                            # no quote: dropped
        row("dpo.not-a-variant", {"mentioned": 2}, DPO_Q),                           # not on the menu
    ],
    "extra_techniques": [],
    "flags": [],
}
REPLY_EMPTY = {"doc_kind": "news", "techniques": [], "extra_techniques": [], "flags": ["no_techniques_found"]}
#: claude-haiku-5.5's answer to the earlier open-map schema (AE run 20261013T132018Z_all_31a4e585).
REPLY_HOLE = {"doc_kind": "paper", "techniques": [row("dpo", {}, DPO_Q), row("ppo-for-alignment", {}, DPO_Q)],
              "extra_techniques": [{"name_as_written": "SteerLM", "roles": {}, "quotes": [DPO_Q]}], "flags": []}
TRUNCATED = '```json\n{"doc_kind": "paper", "techniques": [{"slug": "dpo", "roles": {"mentioned": 4, "us'


def _world(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, texts: list[str],
           options: dict[str, Any] | None = None) -> dict[str, Any]:
    fake = FakeLlm()
    fake.on("DOC_A", ok(REPLY_A), model=LUNA)
    fake.on("DOC_B", ok(REPLY_EMPTY), model=LUNA)
    fake.on("DOC_C", malformed(TRUNCATED), model=LUNA)
    fake.on("DOC_D", ok(REPLY_EMPTY), model=LUNA)
    fake.on("DOC_H", ok(REPLY_HOLE, cost=0.004), model=LUNA)
    monkeypatch.delenv("STACK_WORKFLOWS_ROOT", raising=False)  # the real workflows
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("STACK_PROGRESS", "0")
    monkeypatch.setattr(annotate_stage, "TRANSPORT_HOOK", lambda: fake)
    monkeypatch.setattr(annotate_stage, "SLEEP", lambda _s: None)
    monkeypatch.setattr(annotate_stage, "CLOCK", lambda: AT_RUN)
    monkeypatch.chdir(tmp_path)  # X6 finds the plan by name under ./configs/plans
    store = tmp_path / "store"
    docs = [doc_with_text(store, i, t) for i, t in enumerate(texts)]
    write_documents(store, docs, run=1)
    line: dict[str, Any] = {"stage": "annotate.technique_roles",
                            "prior": ["technique_roles", "technique_roles_evidence"]}
    if options:
        line["options"] = options
    plan = write_plan_file(
        tmp_path, "techniques-test",
        prompts={"annotate.technique_roles": {"name": "technique-roles-v2", "role": "extract_main", "model": LUNA}},
        stages=[{"stage": "fold.corpus"}, line, {"stage": "fold.build"}],
        folded=["technique_roles"])
    return {"store": store, "fake": fake, "plan": plan, "ids": [d["doc_id"] for d in docs]}


def _green(store: pathlib.Path) -> None:
    results = run_checks(store)
    red = {r.check: r.items for r in results if r.status not in ("ok", "skipped")}
    assert red == {}, red
    by = {r.check: r.status for r in results}
    names = ("V-wf", "V-evidence", "policy_pins", "basis_currency", "selection_recompute")
    assert {n: by[n] for n in names} == dict.fromkeys(names, "ok")


@pytest.mark.slow
def test_stage_grades_documents_and_fails_a_truncated_reply_visibly(tmp_path: pathlib.Path,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    w = _world(tmp_path, monkeypatch, ["DOC_A. " + DOC, "DOC_B. A recipe for sourdough bread.",
                                       "DOC_C. A long post the judge never finishes."])
    store, fake, plan, ids = w["store"], w["fake"], w["plan"], w["ids"]
    corpus = fold_corpus(store, plan)
    rec = run_stage(store, plan, "annotate.technique_roles", [corpus], series=SERIES)
    assert rec.status == "ok", rec.error
    assert fake.count(contains="DOC_A") == 1 and fake.count(contains="DOC_B") == 1
    assert fake.count(contains="DOC_C") == 3  # the client's schema retries, then the unit fails
    rows = {r["doc_id"]: r for r in rows_of(store, rec, "technique_roles")}
    a, b = rows[ids[0]], rows[ids[1]]
    assert (a["status"], a["origin"], a["basis_kind"]) == ("ok", "ai_check", "text")
    assert (b["status"], b["origin"]) == ("empty", "ai_check")  # an LLM answer with no rows
    assert b["techniques"] == [] and b["flags"] == ["no_techniques_found"] and b["doc_kind"] == "news"
    # the payload: taxonomy order, 12 explicit roles, verified quotes, the repairs
    by_slug = {t["slug"]: t for t in a["techniques"]}
    assert list(by_slug) == [s for s in main.menu().slugs if s in by_slug]
    assert set(by_slug) == {"dpo", "ppo-for-alignment", "rlhf"}
    assert list(by_slug["dpo"]["roles"]) == list(ROLE_NAMES) and by_slug["dpo"]["roles"]["critiqued"] == 0
    assert by_slug["ppo-for-alignment"]["roles"]["mentioned"] == 2  # clamped
    assert by_slug["dpo"]["quotes"] == [{"text": DPO_Q, "verified": True}]
    assert by_slug["rlhf"]["flags"] == ["quote_unverified"]
    assert [x["name_as_written"] for x in a["extra_techniques"]] == ["dpo.not-a-variant"]
    assert a["extra_techniques"][0]["hint_technique"] == "dpo"
    assert {"mentioned_clamped:ppo-for-alignment:0->2", "no_quote_dropped:llm-as-judge",
            "unknown_slug_to_extra:dpo.not-a-variant", "quote_unverified:rlhf"} <= set(a["repairs"])
    assert rec.counts["quote_unverified_rows"] == 1 and rec.counts["dropped_no_quote"] == 1
    # DOC_C's cut-off JSON came without finish_reason = length (malformed(), not truncated()), so not
    # the client's truncation retry (D273, the test below): unparseable on all three schema attempts,
    # a failed_permanent[schema_invalid] evidence row (the prior carry keeps it, not re-asked)
    ev = {e["doc_id"]: e for e in rows_of(store, rec, "technique_roles_evidence")}
    assert (ev[ids[2]]["status"], ev[ids[2]]["error_class"]) == ("failed_permanent", "schema_invalid")
    assert rows[ids[2]]["status"] == "failed_permanent" and rows[ids[2]]["techniques"] is None
    # the request: the prefix is the cached system block, the document the user message
    req = next(r for r in fake.requests if "DOC_A" in prompt_text(r))
    system = req.payload["messages"][0]["content"][0]
    assert system["cache_control"] == {"type": "ephemeral"} and "# THE MENU" in system["text"]
    assert "DOC_A" not in system["text"] and main.INPUT_MARK not in system["text"]
    assert "# THE DOCUMENT\n\nDOC_A. " in prompt_text(req) and req.payload["max_tokens"] == main.MAX_TOKENS
    # the evidence row names the pinned prompt and template
    pin = main.prompt(plan.prompts["annotate.technique_roles"], annotate_stage.stack_config().llm)
    assert ev[ids[0]]["prompt_name"] == "technique-roles-v2" and ev[ids[0]]["template_digest"] in pin.template_digests
    _green(store)


@pytest.mark.slow
def test_max_rows_and_carry_on_a_second_run(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = _world(tmp_path, monkeypatch, ["DOC_A. " + DOC, "DOC_B. bread.", "DOC_D. more bread."],
               options={"max_rows": 2})
    store, fake, plan, ids = w["store"], w["fake"], w["plan"], w["ids"]
    corpus = fold_corpus(store, plan)
    r1 = run_stage(store, plan, "annotate.technique_roles", [corpus], series=SERIES)
    assert r1.status == "ok", r1.error
    assert fake.count() == 2
    assert len(rows_of(store, r1, "technique_roles_evidence")) == 2
    left = [(r["status"], r["error_class"]) for r in rows_of(store, r1, "technique_roles")
            if r["status"] == "not_attempted"]
    assert left == [("not_attempted", "limit")]
    assert r1.counts["not_attempted.limit"] == 1 and not r1.scope_complete  # build reason `limit` (D709)

    def prior(rec: Any) -> list[str]:
        return [rec.outputs[f]["path"] for f in ("technique_roles", "technique_roles_evidence")]

    r2 = run_stage(store, plan, "annotate.technique_roles", [corpus, *prior(r1)], series=SERIES)
    assert r2.status == "ok", r2.error
    assert fake.count() == 3  # only the row the limit left out
    r3 = run_stage(store, plan, "annotate.technique_roles", [corpus, *prior(r2)], series=SERIES)
    assert r3.status == "ok", r3.error
    assert fake.count() == 3  # everything carried: no call
    rows = {r["doc_id"]: r["status"] for r in rows_of(store, r3, "technique_roles")}
    assert sorted(rows.values()) == ["empty", "empty", "ok"] and set(rows) == set(ids)
    _green(store)


@pytest.mark.slow
def test_a_reply_cut_off_twice_is_failed_permanent_truncated_carried_and_reasked_on_a_pin_change(
        tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``finish_reason = length`` on the call's cap and again on the client's one retry at twice
    the cap: ``failed_permanent[truncated]`` with both attempts' cost (not three schema attempts);
    the prior carry keeps it; a pin change (a new config digest, so a new work key) re-asks it."""
    w = _world(tmp_path, monkeypatch, ["DOC_E. A survey the judge never finishes."])
    store, fake, plan, ids = w["store"], w["fake"], w["plan"], w["ids"]
    fake.on("DOC_E", [truncated(TRUNCATED, cost=0.01, completion_tokens=main.MAX_TOKENS),
                      truncated(TRUNCATED, cost=0.02, completion_tokens=2 * main.MAX_TOKENS),
                      ok(REPLY_EMPTY)], model=LUNA)
    corpus = fold_corpus(store, plan)
    r1 = run_stage(store, plan, "annotate.technique_roles", [corpus], series=SERIES)
    assert r1.status == "ok", r1.error
    assert [r.payload["max_tokens"] for r in fake.requests] == [main.MAX_TOKENS, 2 * main.MAX_TOKENS]
    ev = rows_of(store, r1, "technique_roles_evidence")
    assert [(e["status"], e["error_class"]) for e in ev] == [("failed_permanent", "truncated")]
    assert ev[0]["cost_usd"] == pytest.approx(0.03) and "cut off at the output cap twice" in ev[0]["error"]
    assert rows_of(store, r1, "technique_roles")[0]["status"] == "failed_permanent"
    _green(store)

    def prior(rec: Any) -> list[str]:
        return [rec.outputs[f]["path"] for f in ("technique_roles", "technique_roles_evidence")]

    r2 = run_stage(store, plan, "annotate.technique_roles", [corpus, *prior(r1)], series=SERIES)
    assert r2.status == "ok", r2.error
    assert len(fake.requests) == 2  # carried, not re-asked
    assert [(r["status"], r["error_class"]) for r in rows_of(store, r2, "technique_roles")] == [
        ("failed_permanent", "truncated")]
    # a second prompt file, in a copy of the workflow (the pin name selects the file; the stage loads the copy)
    root = tmp_path / "wfroot"
    shutil.copytree(WF_DIR, root / "workflows" / "annotate" / "technique_roles",
                    ignore=shutil.ignore_patterns("__pycache__"))
    (root / "taxonomy").mkdir()
    shutil.copy(TAXONOMY, root / "taxonomy" / TAXONOMY.name)  # main.TAXONOMY_PATH is beside workflows/
    prompts = root / "workflows" / "annotate" / "technique_roles" / "prompts"
    v2 = (prompts / "technique-roles-v2.md").read_text("utf-8")
    (prompts / "technique-roles-v9.md").write_text(v2.replace("Now emit the JSON object", "V9: emit the JSON object"))
    monkeypatch.setenv("STACK_WORKFLOWS_ROOT", str(root / "workflows"))
    plan2 = write_plan_file(
        tmp_path, "techniques-test-2",
        prompts={"annotate.technique_roles": {"name": "technique-roles-v9", "role": "extract_main", "model": LUNA}},
        stages=[{"stage": "fold.corpus"}, {"stage": "annotate.technique_roles",
                                           "prior": ["technique_roles", "technique_roles_evidence"]},
                {"stage": "fold.build"}],
        folded=["technique_roles"])
    r3 = run_stage(store, plan2, "annotate.technique_roles", [fold_corpus(store, plan2), *prior(r2)],
                   series=SERIES)
    assert r3.status == "ok", r3.error
    assert len(fake.requests) == 3 and fake.requests[2].payload["max_tokens"] == main.MAX_TOKENS
    assert "\nV9: emit the JSON object described above" in prompt_text(fake.requests[2])  # v9's user part
    assert [(r["doc_id"], r["status"]) for r in rows_of(store, r3, "technique_roles")] == [(ids[0], "empty")]
    ev3 = rows_of(store, r3, "technique_roles_evidence")
    # the new call's evidence row names the selected prompt (the carried failure row is the old one)
    assert ("empty", "technique-roles-v9") in [(e["status"], e["prompt_name"]) for e in ev3]


@pytest.mark.slow
def test_an_all_empty_roles_answer_is_failed_not_an_empty_result(tmp_path: pathlib.Path,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    """R6 item 5: rows present, every roles map empty -> ``failed_permanent[schema_invalid]`` with the
    call's cost (one call, no retry); the prior carry keeps it; a genuine no-technique answer and a
    normal one in the same run are unchanged."""
    w = _world(tmp_path, monkeypatch, ["DOC_A. " + DOC, "DOC_B. A recipe for sourdough bread.",
                                       "DOC_H. " + DOC])
    store, fake, plan, ids = w["store"], w["fake"], w["plan"], w["ids"]
    corpus = fold_corpus(store, plan)
    r1 = run_stage(store, plan, "annotate.technique_roles", [corpus], series=SERIES)
    assert r1.status == "ok", r1.error
    assert fake.count(contains="DOC_H") == 1
    ev = {e["doc_id"]: e for e in rows_of(store, r1, "technique_roles_evidence")}
    h = ev[ids[2]]
    assert (h["status"], h["error_class"]) == ("failed_permanent", "schema_invalid")
    assert h["cost_usd"] == pytest.approx(0.004) and "empty roles map" in h["error"]
    rows = {r["doc_id"]: r for r in rows_of(store, r1, "technique_roles")}
    assert [rows[i]["status"] for i in ids] == ["ok", "empty", "failed_permanent"]
    assert rows[ids[2]]["techniques"] is None
    assert r1.counts["failed.schema_invalid"] == 1
    _green(store)
    prior = [r1.outputs[f]["path"] for f in ("technique_roles", "technique_roles_evidence")]
    r2 = run_stage(store, plan, "annotate.technique_roles", [corpus, *prior], series=SERIES)
    assert r2.status == "ok", r2.error
    assert fake.count() == 3  # all carried, the failure included
    assert [(r["status"], r["error_class"]) for r in rows_of(store, r2, "technique_roles")
            if r["doc_id"] == ids[2]] == [("failed_permanent", "schema_invalid")]


@pytest.mark.slow
def test_a_taxonomy_edit_reasks_every_document_from_the_llm_cache(tmp_path: pathlib.Path,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    """The taxonomy's sha256 is in the config digest, so an edited taxonomy file re-asks every document (a
    rerun on the same file carries everything); a byte edit the menu does not show (a YAML comment) leaves
    the requests byte-identical, so they are served from the LLM cache, no second call. Red under: the
    taxonomy sha256 dropped from ``prompt``'s digests (the edited run then carries both)."""
    w = _world(tmp_path, monkeypatch, ["DOC_A. " + DOC, "DOC_B. A recipe for sourdough bread."])
    store, fake, plan = w["store"], w["fake"], w["plan"]
    corpus = fold_corpus(store, plan)

    def prior(rec: Any) -> list[str]:
        return [rec.outputs[f]["path"] for f in ("technique_roles", "technique_roles_evidence")]

    r1 = run_stage(store, plan, "annotate.technique_roles", [corpus], series=SERIES)
    assert r1.status == "ok", r1.error
    r2 = run_stage(store, plan, "annotate.technique_roles", [corpus, *prior(r1)], series=SERIES)
    assert r2.status == "ok" and r2.counts["carried"] == 2 and not r2.counts.get("asked"), r2.counts
    root = tmp_path / "wfroot"
    shutil.copytree(WF_DIR, root / "workflows" / "annotate" / "technique_roles",
                    ignore=shutil.ignore_patterns("__pycache__"))
    (root / "taxonomy").mkdir()
    (root / "taxonomy" / TAXONOMY.name).write_text(TAXONOMY.read_text("utf-8") + "# an edit\n", "utf-8")
    monkeypatch.setenv("STACK_WORKFLOWS_ROOT", str(root / "workflows"))
    r3 = run_stage(store, plan, "annotate.technique_roles", [corpus, *prior(r2)], series=SERIES)
    assert r3.status == "ok", r3.error
    assert r3.config_digest != r1.config_digest
    assert r3.counts["asked"] == 2 and not r3.counts.get("carried"), r3.counts
    assert fake.count() == 2 and r3.counts["cache_hits"] == 2


REPLY_SWEEPS = {
    "sweeps": {"deployment_claims": [{"system": "GLM-5.3", "technique": "rlhf", "quote": RLHF_Q, "direction": "uses"}],
               "recommendations": [], "controls_discussed": [{"surface": "containment"}],
               "menu_gaps": ["safety cases", "incident reporting"]},
    "doc_kind": "commentary",
    "techniques": [row("rlhf", {"mentioned": 2, "suspected_use": 2}, RLHF_Q),
                   # the citation is dropped from the quote: verified on the loose tier only
                   row("dpo", {"mentioned": 1}, "We fine-tune Llama-3-8B with DPO on 60k preference pairs.")],
    "extra_techniques": [], "flags": []}


@pytest.mark.slow
def test_stage_stores_sweep_flags_and_counts_sweeps_and_loose_quotes(tmp_path: pathlib.Path,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    """Through ``ask`` and the stage: the sweeps' menu gaps are stored as ``menu_gap:`` flags, the
    sweeps themselves are not stored, a ``suspected_use`` row is kept, a citation-dropping quote is
    verified loosely and counted, and a reply without sweeps (DOC_B) is counted ``sweeps_absent``."""
    cited = "DOC_S. We fine-tune Llama-3-8B with DPO [12] on 60k preference pairs.\n" + RLHF_Q + ".\n"
    w = _world(tmp_path, monkeypatch, [cited, "DOC_B. A recipe for sourdough bread."])
    store, fake, plan, ids = w["store"], w["fake"], w["plan"], w["ids"]
    fake.on("DOC_S", ok(REPLY_SWEEPS), model=LUNA)
    rec = run_stage(store, plan, "annotate.technique_roles", [fold_corpus(store, plan)], series=SERIES)
    assert rec.status == "ok", rec.error
    rows = {r["doc_id"]: r for r in rows_of(store, rec, "technique_roles")}
    s = rows[ids[0]]
    assert s["flags"] == ["menu_gap:safety cases", "menu_gap:incident reporting"]
    assert [t["slug"] for t in s["techniques"]] == ["rlhf", "dpo"] and s["techniques"][0]["roles"]["suspected_use"] == 2
    assert [q["verified"] for t in s["techniques"] for q in t["quotes"]] == [True, True]
    assert "quote_verified_loose:dpo" in s["repairs"] and "sweeps" not in s
    assert rows[ids[1]]["flags"] == ["no_techniques_found"]
    assert rec.counts["loose_verified_quotes"] == 1 and rec.counts["menu_gap_flags"] == 2
    assert rec.counts["sweeps_absent"] == 1  # DOC_B's reply has no sweeps
    _green(store)
