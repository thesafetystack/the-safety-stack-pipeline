"""``stack.config`` load / validate / stamp / cross-check — 30-stack-module §3, 20-technical §3.

Exercises the shipped ``configs/`` registry through the conftest ``config``
fixture, the six config stamps, the typed accessors, and each fail-loud cross-check via a
mutated tmp copy of the config dir. No DB is touched.
"""

from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from stack.config import ConfigError, load

REPO = Path(__file__).resolve().parents[1]
CONFIG_DIR = REPO / "configs"

_STAMP_RE = re.compile(r"^\d+\.\d+(\.\d+)?\+[0-9a-f]{8}$")
_STAMP_KEYS = {"enums", "registry", "taxonomy", "channels", "llm", "policies"}


# ─────────────────────────── stamps ────────────────────────────────────────


def test_six_stamps_present_and_well_formed(config: Any) -> None:
    """All six config stamps exist and match '<semver>+<hash8>' (§3)."""
    assert set(config.stamps) == _STAMP_KEYS
    for name, stamp in config.stamps.items():
        assert _STAMP_RE.match(stamp), f"{name}: {stamp!r}"


def test_enums_stamp_recomputes_from_file_bytes(config: Any) -> None:
    """'<version>+<first 8 hex of sha256(enums.yaml bytes)>' reproduces stamps['enums'] (§3)."""
    raw = (CONFIG_DIR / "enums.yaml").read_bytes()
    assert f"{config.enums.version}+{hashlib.sha256(raw).hexdigest()[:8]}" == config.stamps["enums"]


# ─────────────────────────── accessors ─────────────────────────────────────


def test_screen_tier2_role_is_configured(config: Any) -> None:
    """annotate.screen's second tier reads a module-constant role (the workflow schema carries one
    llm_role); the role must exist in llm.yaml or every borderline would refuse at run time. That
    every role's model is priced is ``tools/check_configs.py``'s (cross-check 6)."""
    from workflows.annotate.screen.main import ADJUDICATE_ROLE

    assert ADJUDICATE_ROLE in config.llm.roles


def test_institutions_accessor(config: Any) -> None:
    """channels.institutions() ports v1 orgs.yaml `openalex_institutions`: curated OpenAlex
    institution ids, each with its `fields` volume guard (empty = unfiltered)."""
    insts = config.channels.institutions()
    assert insts and all(i.id.startswith("I") for i in insts)          # OpenAlex institution ids
    assert all(isinstance(i.fields, list) for i in insts)


def test_policy_file_is_the_anchoring_threshold(config: Any) -> None:
    """policies.yaml holds only ``anchoring`` (§6; v3's other policies are in v3's configs/)."""
    assert set(config.policy_file.model_dump()) == {"version", "anchoring"}
    assert isinstance(config.policy_file.anchoring.anchor_min_score, float)


def test_enums_vocab_and_validity(config: Any) -> None:
    """enums.vocab('ann_type') has the 11 record families; is_valid checks membership (§8)."""
    ann = config.enums.vocab("ann_type")
    assert len(ann) == 11          # 10 + author_affiliation (enums 1.1.0)
    assert config.enums.is_valid("ann_type", "claim") is True
    assert config.enums.is_valid("ann_type", "bogus") is False
    assert config.enums.is_valid("no_such_vocab", "x") is False


def test_taxonomy_pin_and_menu(config: Any) -> None:
    """The taxonomy pin resolves — taxonomy.version == registry.taxonomy.active — and facet
    menus/flags read through (§3 pin, §7.2)."""
    assert config.taxonomy.version == config.registry.taxonomy.active
    assert config.taxonomy.techniques
    assert {"data", "post-training", "inference"} <= config.taxonomy.menu("locus")
    assert config.taxonomy.facet("harm-area").empty_is_legal is True


# ─────────────────────────── failure cases ─────────────────────────────────


def _copy_config(tmp_path: Path) -> Path:
    """A fresh, writable copy of the example config dir."""
    dest = tmp_path / "config"
    shutil.copytree(CONFIG_DIR, dest)
    return dest


def _rewrite_yaml(path: Path, mutate) -> None:
    """Load YAML, apply ``mutate(data)`` in place, write it back."""
    data = yaml.safe_load(path.read_text())
    mutate(data)
    path.write_text(yaml.safe_dump(data))


def test_registry_taxonomy_active_mismatch(tmp_path: Path) -> None:
    """A registry taxonomy.active that disagrees with the pinned file version -> ConfigError."""
    d = _copy_config(tmp_path)

    def mut(data: dict) -> None:
        data["taxonomy"]["active"] = "0.9"

    _rewrite_yaml(d / "registry.yaml", mut)
    with pytest.raises(ConfigError):
        load(d)


# ─────────────────────────── taxonomy parsed on first use (F31-10) ─────────────


def _edit_taxonomy(tmp_path: Path, old: str, new: str) -> Path:
    d = _copy_config(tmp_path)
    tax = d / "taxonomy-v0.6.yaml"
    text = tax.read_text()
    assert text.count(old) == 1, old
    tax.write_text(text.replace(old, new))
    return d


def test_taxonomy_parsed_on_first_use_with_the_same_stamp(tmp_path: Path) -> None:
    """load() stamps the taxonomy from its bytes and `version:` line without parsing it; the
    first ``config.taxonomy`` parses it once."""
    cfg = load(CONFIG_DIR)
    raw = (CONFIG_DIR / cfg.registry.taxonomy.file).read_bytes()
    assert cfg.stamps["taxonomy"] == f"0.6+{hashlib.sha256(raw).hexdigest()[:8]}"
    assert cfg.taxonomy_file._parsed is None
    first = cfg.taxonomy
    assert first is cfg.taxonomy and first.version == "0.6" and len(first.techniques) == 292


def test_taxonomy_cross_check_9_raises_on_use(tmp_path: Path) -> None:
    """A taxonomy failing cross-check 9 loads (nothing reads it at load) and raises on use."""
    d = _edit_taxonomy(tmp_path, "  exclusive_with:\n  - - restriction\n",
                       "  exclusive_with:\n  - - no-such-value\n")
    cfg = load(d)
    with pytest.raises(ConfigError, match="exclusive_with references 'no-such-value'"):
        cfg.taxonomy


def test_taxonomy_yaml_and_schema_errors_raise_on_use(tmp_path: Path) -> None:
    d = _edit_taxonomy(tmp_path, "  exclusive_with:\n  - - restriction\n", "  exclusive_with: [[\n")
    with pytest.raises(ConfigError, match="taxonomy: YAML parse error"):
        load(d).taxonomy
    d2 = _edit_taxonomy(tmp_path / "b", "\nversion: '0.6'\n", "\nversion: '0.6'\nno_such_key: 1\n")
    with pytest.raises(ConfigError, match="failed schema validation"):
        load(d2).taxonomy


def test_taxonomy_version_line_is_required(tmp_path: Path) -> None:
    """The stamp's version comes from the one top-level `version:` line; none -> ConfigError at load."""
    d = _edit_taxonomy(tmp_path, "\nversion: '0.6'\n", "\nversion_: '0.6'\n")
    with pytest.raises(ConfigError, match="one top-level `version:` line"):
        load(d)


@pytest.mark.parametrize("filename,key,mutate", [
    ("channels.yaml", "schedule", lambda d: d["channels"][0].update(schedule="weekly")),
    ("channels.yaml", "recall_seeds", lambda d: d.update(recall_seeds=[])),
    ("llm.yaml", "per_sweep_usd", lambda d: d["budget"].update(per_sweep_usd=300)),
    ("policies.yaml", "quarantine_after", lambda d: d.update(quarantine_after=3)),
])
def test_keys_read_by_nothing_are_refused(tmp_path: Path, filename: str, key: str, mutate: Any) -> None:
    """v3 keys that no v4 code read were removed (D438); one written back is refused at
    load, not silently accepted as configuration."""
    d = _copy_config(tmp_path)
    _rewrite_yaml(d / filename, mutate)
    with pytest.raises(ConfigError, match=key):
        load(d)


# ─────────────────── store-pipeline channel keys (D13, D42) ──────────────────

CHANNELS_FIXTURE = REPO / "tests" / "fixtures" / "config" / "channels.yaml"


def test_channel_identity_keys_load_and_unknown_group_refused(tmp_path: Path) -> None:
    """The fixture's per-channel `channel_group`, `miss_means`, `non_merging_keys` and
    `record_projection` load; an unknown group is refused."""
    d = _copy_config(tmp_path)
    shutil.copyfile(CHANNELS_FIXTURE, d / "channels.yaml")
    ch = load(d).channels
    assert ch.channel("arxiv").channel_group == "paper"
    assert ch.channel("arxiv").miss_means == "unknown"  # the default
    assert ch.channel("x").miss_means == "withdrawn"
    assert "conversation_id" in ch.channel("x").non_merging_keys
    assert ch.channel("openalex").record_projection[0] == "title"
    assert "doi" in ch.channel_groups["paper"].merge_keys

    def bad_group(data: dict) -> None:
        data["channels"][0]["channel_group"] = "papers"

    _rewrite_yaml(d / "channels.yaml", bad_group)
    with pytest.raises(ConfigError, match="channel_group"):
        load(d)


def test_concept_group_s2_terms_override_loads_and_a_bad_value_is_refused(tmp_path: Path) -> None:
    """`s2_terms` (round 4, P): absent = `terms`, `[]` = skip the group on S2 (steerability,
    heuristic-arguments), a non-list is refused by the schema and the model."""
    d = _copy_config(tmp_path)
    groups = {g.id: g for g in load(d).channels.concept_groups}
    assert groups["steerability"].terms_for_s2() == [] and groups["steerability"].terms == ["steerability"]
    assert groups["heuristic-arguments"].terms_for_s2() == []
    assert groups["rlhf"].s2_terms is None and groups["rlhf"].terms_for_s2() == groups["rlhf"].terms

    def bad(data: dict) -> None:
        data["concept_groups"][0]["s2_terms"] = "steerability"

    _rewrite_yaml(d / "channels.yaml", bad)
    with pytest.raises(ConfigError, match="s2_terms"):
        load(d)


def test_concept_group_forum_terms_override_loads_and_a_bad_value_is_refused(tmp_path: Path) -> None:
    """`forum_terms` (the Chinese-coverage work): absent = `terms`, `[]` = no forum query for the group (llm-safety), a non-list is
    refused by the schema and the model."""
    d = _copy_config(tmp_path)
    groups = {g.id: g for g in load(d).channels.concept_groups}
    assert groups["llm-safety"].terms_for_forums() == [] and "honesty" in groups["llm-safety"].terms
    assert groups["rlhf"].forum_terms is None and groups["rlhf"].terms_for_forums() == groups["rlhf"].terms

    def bad(data: dict) -> None:
        data["concept_groups"][0]["forum_terms"] = "jailbreak"

    _rewrite_yaml(d / "channels.yaml", bad)
    with pytest.raises(ConfigError, match="forum_terms"):
        load(d)


def test_concept_group_needs_ai_context_flag_loads_and_a_misspelling_is_refused(
    tmp_path: Path,
) -> None:
    """`needs_ai_context` (SR scope, D-row A2.2) defaults false and loads when set; a misspelt
    flag is refused, not ignored (it would silently send another field's literature)."""
    d = _copy_config(tmp_path)
    groups = {g.id: g for g in load(d).channels.concept_groups}
    assert groups["decision-theory"].needs_ai_context and not groups["rlhf"].needs_ai_context

    def misspelt(data: dict) -> None:
        data["concept_groups"][0]["needs_ai_contxt"] = True

    _rewrite_yaml(d / "channels.yaml", misspelt)
    with pytest.raises(ConfigError, match="needs_ai_contxt"):
        load(d)


def test_leftover_workflows_yaml_is_refused(tmp_path: Path) -> None:
    """v3's workflows.yaml was retired at T4.5 (D42): a config dir still holding one is a stale
    copy and is refused by name, not silently ignored."""
    d = _copy_config(tmp_path)
    assert not (d / "workflows.yaml").exists()
    load(d)
    (d / "workflows.yaml").write_text("version: '1.0'\nworkflows: []\n")
    with pytest.raises(ConfigError, match="workflows.yaml was retired"):
        load(d)


def test_channel_vocabularies_agree() -> None:
    """The channel-group and miss_means words are spelled in config.py (standalone), the JSON
    Schema and tools/check_configs.py; they must equal each other and stack.schemas.vocab."""
    import ast
    import json
    from typing import get_args

    from stack.config import ChannelGroup, MissMeans
    from stack.schemas import vocab

    schema = json.loads((REPO / "spec/schemas/config/channels.schema.json").read_text())
    props = schema["$defs"]["channel"]["properties"]
    assert get_args(MissMeans) == vocab.MISS_MEANS == tuple(props["miss_means"]["enum"])
    assert get_args(ChannelGroup) == tuple(props["channel_group"]["enum"])
    tree = ast.parse((REPO / "tools/check_configs.py").read_text())
    consts = {
        t.id: ast.literal_eval(n.value)
        for n in tree.body
        if isinstance(n, ast.Assign)
        for t in n.targets
        if isinstance(t, ast.Name) and t.id in {"CHANNEL_GROUPS", "MISS_MEANS"}
    }
    assert consts == {"CHANNEL_GROUPS": get_args(ChannelGroup), "MISS_MEANS": vocab.MISS_MEANS}


def test_missing_file(tmp_path: Path) -> None:
    """A missing config file -> ConfigError, not a bare OSError (§3 fail-loud)."""
    d = _copy_config(tmp_path)
    (d / "llm.yaml").unlink()
    with pytest.raises(ConfigError):
        load(d)


def test_stack_config_dir_env_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """load(None) resolves the dir via STACK_CONFIG_DIR (§3 dir resolution)."""
    monkeypatch.setenv("STACK_CONFIG_DIR", str(CONFIG_DIR))
    cfg = load(None)
    assert cfg.taxonomy.version == "0.6"
    assert set(cfg.stamps) == _STAMP_KEYS


# ─────────────────────────── YAML strictness (D433) ─────────────────────────


def test_a_duplicate_key_in_a_config_file_is_refused(tmp_path: Path) -> None:
    """PyYAML keeps the last of two equal keys; the config loader refuses them, naming key and line."""
    d = _copy_config(tmp_path)
    text = (d / "llm.yaml").read_text()
    (d / "llm.yaml").write_text(text + "\nversion: 9.9.9\n")
    with pytest.raises(ConfigError, match=r"(?s)llm: YAML parse error in 'llm.yaml'.*duplicate key 'version'"):
        load(d)


@pytest.mark.parametrize("loader", ["c", "python"])
@pytest.mark.slow
def test_the_strict_loaders_read_the_shipped_yaml_as_safe_load_does(loader: str) -> None:
    """Both loaders (libyaml, and the pure-Python fallback) parse every shipped config and plan
    exactly as ``yaml.safe_load``, and both refuse a duplicate key."""
    from stack import yamlio

    cls = yamlio.StrictCSafeLoader if loader == "c" else yamlio.StrictSafeLoader
    if loader == "c":
        assert yaml.__with_libyaml__ and issubclass(cls, yaml.CSafeLoader)
    files = sorted(CONFIG_DIR.rglob("*.yaml"))
    assert len(files) >= 13
    for f in files:
        text = f.read_text("utf-8")
        assert yaml.load(text, Loader=cls) == yaml.safe_load(text), f
    with pytest.raises(yaml.YAMLError, match=r"duplicate key 'x' \(first at line 1, column 5\)"):
        yaml.load("a: {x: 1, x: 2}", Loader=cls)
    merged = "base: &b {x: 1, y: 2}\nc:\n  <<: *b\n  x: 3\n"  # an override of a merged key is no duplicate
    assert yaml.load(merged, Loader=cls)["c"] == {"x": 3, "y": 2}
