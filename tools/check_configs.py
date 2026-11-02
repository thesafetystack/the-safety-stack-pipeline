#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6", "jsonschema>=4.21", "referencing>=0.30"]
# ///
"""Validate the live configs (``configs/*.yaml`` against ``spec/schemas/config/``), the payload
schemas the screen workflow still loads (``spec/schemas/payloads/``: ``screen_verdict``,
``doc_class`` and the ``_core`` definition they ``$ref``) with their example instances, and cross-check mutual
consistency (10-design §8, 20-technical §10): x-enum tags against ``configs/enums.yaml``, license
classes, priced LLM roles, channel groups and key kinds, taxonomy ``exclusive_with``/``absorbs``,
``spec/types/interfaces.py`` Literals, plan and stage names.

Run from the repo root:  uv run tools/check_configs.py
Exits nonzero with readable errors on any schema violation or cross-check failure.

Post-graduation layout: live configs in ``configs/``, prompts co-located in
``workflows/<phase>/<name>/<role>.md``; the config and payload JSON Schemas, payload example
instances, and ``types/interfaces.py`` deliberately remain under ``spec/``. v3's other payload
schemas, its aggregate schemas, its ``workflows.schema.json``, the v3 half of ``_core``, their
examples and the enums.yaml vocabularies only they named were retired with v3 (2026-10-14) and
are not checked here.

v3's ``workflows.yaml`` and its lints (llm_role/ann_type/requires/policy/prompt/payload-schema
resolution, ``bump_on`` ⊆ ``reads``, ``anchoring.per_workflow`` keys registered) were retired at
T4.5 (D42): stage registration lives in the stage modules and ``configs/plans/``; its schema is
v3's ``spec/schemas/config/workflows.schema.json``.
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

REPO = Path(__file__).resolve().parent.parent
SPEC = REPO / "spec"  # prose contract + the reference schemas/examples that did NOT graduate
CONFIGS = REPO / "configs"  # the live YAML registry (graduated from spec/examples/config)
SCHEMAS = SPEC / "schemas" / "config"
PAYLOAD_SCHEMAS = SPEC / "schemas" / "payloads"
SCHEMA_DIRS = [SCHEMAS, PAYLOAD_SCHEMAS]
PAYLOAD_EXAMPLES = SPEC / "examples" / "payloads"
WORKFLOWS_DIR = REPO / "workflows"  # prompts co-located: workflows/<phase>/<name>/<role>.md
INTERFACES = SPEC / "types" / "interfaces.py"

# example-file stem -> schema stem (default: identity)
SCHEMA_STEM = {"taxonomy-sample": "taxonomy", "taxonomy-v0.6": "taxonomy"}

# x-enum vocabularies whose ORDER is normative (E-NIT-5). Others compare as sets. (v3's one,
# evidence_tier, is in v3's configs/enums.yaml with the _core definition that tagged it.)
ORDERED_ENUMS: set[str] = set()

# The F-S4 scope-SQL blessed-view lint went with `workflows.yaml`'s `scope:` keys (D42): stage
# scopes live in the plan files now.

# Store vocabularies the channels carry (design §2.6, §6; D13). Not enums.yaml domains: they belong
# to the store pipeline (`stack/schemas/vocab.py`), and this script stays import-free of `stack`.
CHANNEL_GROUPS = ("paper", "forum", "url", "social", "record")
MISS_MEANS = ("withdrawn", "unknown")
# Key kinds that never merge identities (§3.5, §5.6): cross-links (`related`), links relayed by a
# post (`relay`), the X context keys, the manual sources' `flag`s (collect.manual, round 4 (L)), and the
# v3 import's `v3_meta_id` (D524). Disjoint from every group's `merge_keys` by lint.
NON_MERGING_KEY_KINDS = frozenset(
    {"related", "relay", "conversation_id", "reply_to_id", "quote_of", "repost_of", "flag", "v3_meta_id"}
)
STORE_VOCAB = REPO / "stack" / "schemas" / "vocab.py"

# Enum value-sets that are legitimately STRUCTURAL (not an enums.yaml domain), so they need no
# x-enum tag (E-S3). Each is named by its source. Anything else untagged is drift.
STRUCTURAL_ENUM_ALLOWLIST = {
    frozenset({"search", "scrape", "watch", "manual", "feed"}),            # channels.channel.kind
    frozenset(CHANNEL_GROUPS),                                             # channels channel_group (store vocab)
    frozenset(MISS_MEANS),                                                 # channels channel.miss_means (store vocab)
    frozenset({"publisher", "self_publication"}),                          # channels keyword_cuts.drop_sources (stack/rules.py SOURCE_CUT_CLASSES)
    frozenset({"deny", "allow"}),                                          # llm egress.data_collection
    frozenset({"minimal", "low", "medium", "high"}),                       # llm role.reasoning_effort (OpenRouter's values; D612)
    frozenset({"openrouter", "openai", "anthropic"}),                      # llm provider.dialect (D714, D722)
    frozenset({"single", "single+", "multi"}),                            # taxonomy facet.cardinality
    frozenset({"AI-scored", "human-set"}),                                 # taxonomy facet.population
    frozenset({"low", "medium", "high"}),                                 # taxonomy facet.difficulty
    frozenset({"technique", "standard", "source"}),                       # taxonomy edge.target
    frozenset({"technique", "variant"}),                                   # taxonomy node.type
    # models.yaml (ported from v3 tomas/model-roles; doc/model-collection/schema.md v0.3): the
    # mention vocabulary's own structural domains (the SHAPE of a vocabulary entry, typed in
    # stack/modelvocab.py), not operational annotation vocabularies.
    frozenset({"family", "group", "model"}),                               # models node.kind
    frozenset({"line", "series", "generation"}),                           # models group.level
    frozenset({"official", "estimate", "undisclosed"}),                    # models params.kind
    frozenset({"verified", "weak", "stub"}),                               # models node.confidence
    frozenset({"text", "image", "audio", "video", "music", "embedding", "code", "3d", "action"}),  # models modalities
    # models.yaml facets (vocabulary 0.9): classification vocabularies of the catalogue itself
    frozenset({"language-model", "vision-language", "text-encoder", "embedding", "reranker",
               "guard-classifier", "reward-model", "judge-model", "image-gen", "video-gen",
               "speech-recognition", "speech-generation", "music-gen", "robot-policy",
               "world-model", "vision-backbone", "detector-segmenter", "rl-agent",
               "science", "interpretability-tool"}),                                                # models node.model_class
    frozenset({"reasoning", "code", "computer-use", "cyber", "bio", "medical"}),  # models node.specialization
    frozenset({"expanded", "folded", "class-only", "pointer"}),            # models node.representation_status
    frozenset({"downloadable", "gated", "not-distributed", "unknown"}),    # models weights_access.status
    frozenset({"announced", "preview", "ga", "restricted", "retired", "unknown"}),  # models lifecycle.status
    frozenset({"draft", "released"}),                                      # models meta.status (catalogue envelope, D738)
}

errors: list[str] = []
loaded: dict[str, dict] = {}  # schema-stem -> parsed YAML


def load_yaml(path: Path) -> dict:
    with path.open() as fh:
        return yaml.safe_load(fh)


def validate_file(example: Path) -> None:
    stem = SCHEMA_STEM.get(example.stem, example.stem)
    schema_path = SCHEMAS / f"{stem}.schema.json"
    if not schema_path.exists():
        errors.append(f"{example.name}: no schema at {schema_path.relative_to(REPO)}")
        return
    try:
        data = load_yaml(example)
    except yaml.YAMLError as exc:
        errors.append(f"{example.name}: YAML parse error: {exc}")
        return
    schema = json.loads(schema_path.read_text())
    validator = Draft202012Validator(schema)
    found = sorted(validator.iter_errors(data), key=lambda e: list(e.path))
    if found:
        for err in found:
            loc = "/".join(str(p) for p in err.path) or "<root>"
            errors.append(f"{example.name}: [{loc}] {err.message}")
    else:
        loaded[stem] = data
        print(f"  ok   {example.name}  ->  {schema_path.name}")


def cross_checks() -> None:
    enums = (loaded.get("enums") or {}).get("enums", {})
    license_classes = set(enums.get("license_class", {}).get("values", {}))

    llm = loaded.get("llm") or {}

    # 2. channel license_class values are in enums.license_class.
    for ch in (loaded.get("channels") or {}).get("channels", []):
        lc = ch.get("license_class")
        if lc is not None and lc not in license_classes:
            errors.append(f"channels.yaml: channel '{ch.get('id')}' license_class '{lc}' not in enums.license_class")

    # 3c. registry taxonomy pin is RESOLVABLE (G-N2/item-12): registry.taxonomy.file must exist
    #     under configs/, and registry.taxonomy.active must equal the pinned file's version.
    reg_tax = (loaded.get("registry") or {}).get("taxonomy", {})
    tax_file = reg_tax.get("file")
    if tax_file is not None:
        tax_path = CONFIGS / tax_file
        if not tax_path.exists():
            errors.append(f"registry.yaml: taxonomy.file '{tax_file}' does not resolve under {CONFIGS.relative_to(REPO)}/")
        else:
            pinned = load_yaml(tax_path)
            active = reg_tax.get("active")
            if active is not None and pinned.get("version") != active:
                errors.append(
                    f"registry.yaml: taxonomy.active '{active}' != pinned file version "
                    f"'{pinned.get('version')}' ({tax_file})")

    # 4. taxonomy sample slugs referenced anywhere (channels concept_groups) exist.
    tax = loaded.get("taxonomy") or {}
    tax_slugs = {t["slug"] for t in tax.get("techniques", []) if "slug" in t}
    for cg in (loaded.get("channels") or {}).get("concept_groups", []):
        for slug in cg.get("techniques", []):
            if slug not in tax_slugs:
                errors.append(
                    f"channels.yaml: concept_group '{cg.get('id')}' references technique "
                    f"'{slug}' absent from the taxonomy sample {sorted(tax_slugs)}"
                )

    # 5. license_class key-sets are single-sourced from enums.license_class (F-S8):
    #    llm.yaml egress.by_license_class keys == the vocabulary.
    llm_egress = set((llm.get("egress") or {}).get("by_license_class", {}))
    if llm_egress and license_classes and llm_egress != license_classes:
        errors.append(
            f"llm.yaml: egress.by_license_class keys {sorted(llm_egress)} != "
            f"enums.license_class {sorted(license_classes)}"
        )

    # 6. every llm role's model and fallbacks are priced (D178): an unpriced model is
    #    reserved and charged at pricing.fallback, not its own price, so a cap or a ledger is wrong.
    priced = set((llm.get("pricing") or {}).get("models", {}))
    for role, spec in (llm.get("roles") or {}).items():
        for model in [spec.get("model"), *(spec.get("fallbacks") or [])]:
            if model is not None and model not in priced:
                errors.append(f"llm.yaml: role '{role}' model '{model}' not in pricing.models")


def _walk(node: object, path: str):
    """Yield (json-path, dict-node) for every object node in a schema."""
    if isinstance(node, dict):
        yield path, node
        for k, v in node.items():
            yield from _walk(v, f"{path}/{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk(v, f"{path}/{i}")


def schema_checks() -> None:
    """The checks that would have caught MUST-1/-3/-7, S5, S7, S9 (20-tech §10)."""
    enums = (loaded.get("enums") or {}).get("enums", {})
    vocab = {name: set(v.get("values", {})) for name, v in enums.items()}
    vocab_ordered = {name: list(v.get("values", {})) for name, v in enums.items()}

    # A. Every JSON Schema under schemas/ is itself valid (metaschema); every x-enum field's enum
    #    list EQUALS its enums.yaml vocabulary (the single-source guarantee, C-S5/NIT-3), order-
    #    sensitive for ORDERED_ENUMS (E-NIT-5); and every `enum` node is x-enum-tagged or on the
    #    named structural allowlist (E-S3 — else dropping a tag while drifting is a silent pass).
    for sdir in SCHEMA_DIRS:
        if not sdir.is_dir():
            continue
        for sp in sorted(sdir.glob("*.schema.json")):
            try:
                schema = json.loads(sp.read_text())
            except json.JSONDecodeError as exc:
                errors.append(f"{sp.relative_to(REPO)}: invalid JSON: {exc}")
                continue
            try:
                Draft202012Validator.check_schema(schema)
            except Exception as exc:  # noqa: BLE001 — surface any metaschema failure
                errors.append(f"{sp.relative_to(REPO)}: not a valid JSON Schema: {exc}")

            # enum nodes accounted for by an x-enum tag (the tag's own node or its `items` child).
            covered: set[int] = set()
            for _jp, node in _walk(schema, sp.name):
                if "x-enum" in node:
                    if "enum" in node:
                        covered.add(id(node))
                    items = node.get("items")
                    if isinstance(items, dict) and "enum" in items:
                        covered.add(id(items))

            for jp, node in _walk(schema, sp.name):
                if "x-enum" in node:
                    name = node["x-enum"]
                    enum_list = node.get("enum")
                    if enum_list is None and isinstance(node.get("items"), dict):
                        enum_list = node["items"].get("enum")
                    if enum_list is None:
                        errors.append(f"{jp}: x-enum '{name}' but no enum/items.enum list")
                    elif name not in vocab:
                        errors.append(f"{jp}: x-enum '{name}' has no enums.yaml vocabulary")
                    elif name in ORDERED_ENUMS:
                        if list(enum_list) != vocab_ordered[name]:
                            errors.append(
                                f"{jp}: x-enum '{name}' (order-normative) {list(enum_list)} != "
                                f"enums.{name} {vocab_ordered[name]}"
                            )
                    elif set(enum_list) != vocab[name]:
                        errors.append(
                            f"{jp}: x-enum '{name}' enum {sorted(enum_list)} != "
                            f"enums.{name} {sorted(vocab[name])}"
                        )
                # E-S3: an enum node with no x-enum (own or parent) must be structurally allowlisted.
                if "enum" in node and id(node) not in covered:
                    if frozenset(str(x) for x in node["enum"]) not in STRUCTURAL_ENUM_ALLOWLIST:
                        errors.append(
                            f"{jp}: untagged enum {sorted(map(str, node['enum']))} — add an x-enum "
                            f"tag or extend the named structural allowlist"
                        )


def _store_vocab_tuple(name: str) -> tuple[str, ...] | None:
    """A tuple constant of ``stack/schemas/vocab.py`` read by AST (no import of ``stack``)."""
    if not STORE_VOCAB.exists():
        return None
    for node in ast.parse(STORE_VOCAB.read_text()).body:
        if isinstance(node, ast.AnnAssign | ast.Assign):
            targets = [node.target] if isinstance(node, ast.AnnAssign) else node.targets
            if any(isinstance(t, ast.Name) and t.id == name for t in targets) and node.value:
                value = ast.literal_eval(node.value)
                return tuple(value) if isinstance(value, tuple) else None
    return None


def channel_identity_checks() -> None:
    """The store-pipeline channel keys (design §3.5, §6; D13, D42): `channel_group` and
    `miss_means` in their vocabularies, every group's merging key kinds declared once and never
    also non-merging, `non_merging_keys` drawn from the known non-merging kinds, and
    `record_projection` a list of distinct non-empty field names."""
    store_miss = _store_vocab_tuple("MISS_MEANS")
    if store_miss is not None and tuple(store_miss) != MISS_MEANS:
        errors.append(f"check_configs MISS_MEANS {MISS_MEANS} != stack/schemas/vocab.py {store_miss}")

    if "channels" not in loaded:
        return  # channels.yaml failed its JSON Schema; that error is already reported
    channels = loaded["channels"]
    groups = channels.get("channel_groups") or {}
    if set(groups) != set(CHANNEL_GROUPS):
        errors.append(
            f"channels.yaml: channel_groups keys {sorted(groups)} != groups {sorted(CHANNEL_GROUPS)}"
        )
    owner: dict[str, str] = {}
    for g, spec in groups.items():
        for kind in (spec or {}).get("merge_keys", []):
            if kind in owner:
                errors.append(
                    f"channels.yaml: merge key kind '{kind}' in both groups '{owner[kind]}' and '{g}'"
                )
            owner.setdefault(kind, g)
            if kind in NON_MERGING_KEY_KINDS:
                errors.append(f"channels.yaml: group '{g}' merges on non-merging key kind '{kind}'")

    for ch in channels.get("channels", []):
        cid = ch.get("id", "<no-id>")
        group = ch.get("channel_group")
        if group not in CHANNEL_GROUPS:
            errors.append(f"channels.yaml: channel '{cid}' channel_group {group!r} not in {list(CHANNEL_GROUPS)}")
        miss = ch.get("miss_means", "unknown")
        if miss not in MISS_MEANS:
            errors.append(f"channels.yaml: channel '{cid}' miss_means {miss!r} not in {list(MISS_MEANS)}")
        nmk = ch.get("non_merging_keys") or []
        for kind in nmk:
            if kind not in NON_MERGING_KEY_KINDS:
                hint = f" (a merge key of group '{owner[kind]}')" if kind in owner else ""
                errors.append(
                    f"channels.yaml: channel '{cid}' non_merging_keys kind {kind!r} unknown{hint} "
                    f"(known: {sorted(NON_MERGING_KEY_KINDS)})"
                )
        if len(set(nmk)) != len(nmk):
            errors.append(f"channels.yaml: channel '{cid}' non_merging_keys has duplicates {nmk}")
        proj = ch.get("record_projection") or []
        for name in proj:
            if not isinstance(name, str) or not name.strip():
                errors.append(f"channels.yaml: channel '{cid}' record_projection entry {name!r} is not a field name")
        if len(set(map(str, proj))) != len(proj):
            errors.append(f"channels.yaml: channel '{cid}' record_projection has duplicates {proj}")


def taxonomy_metadata_checks() -> None:
    """H-M2: per-facet value-combination hygiene metadata (`exclusive_with` mutually-exclusive
    groups; `absorbs` catch-all/dominant absorption) must reference real menu values of that facet.
    Annotators apply these repairs pre-write, reducers at union (§7.2)."""
    tax = loaded.get("taxonomy") or {}
    for facet in tax.get("facets", []):
        slug = facet.get("slug", "<facet>")
        menu = {v["slug"] for v in facet.get("values", []) if "slug" in v}
        for group in (facet.get("exclusive_with") or []):
            for v in group:
                if v not in menu:
                    errors.append(
                        f"taxonomy: facet '{slug}' exclusive_with references '{v}' absent from its "
                        f"menu {sorted(menu)}")
        for keeper, absorbed in (facet.get("absorbs") or {}).items():
            if keeper not in menu:
                errors.append(
                    f"taxonomy: facet '{slug}' absorbs key '{keeper}' absent from its menu {sorted(menu)}")
            for v in absorbed:
                if v not in menu:
                    errors.append(
                        f"taxonomy: facet '{slug}' absorbs['{keeper}'] references '{v}' absent from "
                        f"its menu {sorted(menu)}")


def _literal_domains(path: Path) -> dict[str, list[str]]:
    """Parse `Name = Literal["a", "b", ...]` module assignments out of interfaces.py."""
    out: dict[str, list[str]] = {}
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)):
            continue
        val = node.value
        if (isinstance(val, ast.Subscript) and isinstance(val.value, ast.Name)
                and val.value.id == "Literal"):
            sl = val.slice
            elts = sl.elts if isinstance(sl, ast.Tuple) else [sl]
            out[node.targets[0].id] = [
                e.value for e in elts if isinstance(e, ast.Constant) and isinstance(e.value, str)
            ]
    return out


def interfaces_checks() -> None:
    """F-NIT-10: interfaces.py Literal domains for RunStatus/Phase/LicenseClass mirror enums.yaml.
    (Its ReceiptOutcome mirrored v3's receipt.schema.json, in v3's spec; not checked.)"""
    if not INTERFACES.exists():
        errors.append("types/interfaces.py: missing")
        return
    try:
        lits = _literal_domains(INTERFACES)
    except SyntaxError as exc:
        errors.append(f"types/interfaces.py: syntax error: {exc}")
        return
    enums = (loaded.get("enums") or {}).get("enums", {})
    for lit_name, enum_name in [("RunStatus", "run_status"), ("Phase", "phase"),
                                ("LicenseClass", "license_class")]:
        want = set(enums.get(enum_name, {}).get("values", {}))
        got = set(lits.get(lit_name, []))
        if not got:
            errors.append(f"interfaces.py: Literal {lit_name} not found")
        elif want and got != want:
            errors.append(f"interfaces.py: {lit_name} {sorted(got)} != enums.{enum_name} {sorted(want)}")


def validate_payloads() -> None:
    """F-M5: validate one example instance per payload schema, resolving cross-file
    $refs (payload → _core.schema.json) through a `referencing` registry — doubling as the missing
    usage example for the 'normative gate' schema layer."""
    from referencing import Registry, Resource

    schema_files = sorted(PAYLOAD_SCHEMAS.glob("*.schema.json"))
    by_stem: dict[str, dict] = {}
    resources: list[tuple[str, Any]] = []
    for sp in schema_files:
        sch = json.loads(sp.read_text())
        by_stem[sp.name[: -len(".schema.json")]] = sch
        sid = sch.get("$id")
        if sid:
            resources.append((sid, Resource.from_contents(sch)))
    registry = Registry().with_resources(resources)

    if not PAYLOAD_EXAMPLES.is_dir():
        errors.append(f"no payload examples dir at {PAYLOAD_EXAMPLES.relative_to(REPO)}")
        return
    for ex in sorted(PAYLOAD_EXAMPLES.glob("*.json")):
        stem = ex.stem
        schema = by_stem.get(stem)
        if schema is None:
            errors.append(f"examples/payloads/{ex.name}: no matching schema for stem '{stem}'")
            continue
        try:
            data = json.loads(ex.read_text())
        except json.JSONDecodeError as exc:
            errors.append(f"examples/payloads/{ex.name}: invalid JSON: {exc}")
            continue
        validator = Draft202012Validator(schema, registry=registry)
        found = sorted(validator.iter_errors(data), key=lambda e: list(e.path))
        if found:
            for err in found:
                loc = "/".join(str(p) for p in err.path) or "<root>"
                errors.append(f"examples/payloads/{ex.name}: [{loc}] {err.message}")
        else:
            print(f"  ok   payloads/{ex.name}  ->  {stem}.schema.json")
    for stem in by_stem:  # every schema needs an instance (the layer must be exercised)
        if not (PAYLOAD_EXAMPLES / f"{stem}.json").exists():
            errors.append(f"examples/payloads: no example instance for {stem}.schema.json")


# The job-name grammar of the S3 store (stack/store/layout.py RESERVED_SUFFIXES, PLAN_RE,
# STAGE_RE; D320): a plan name names build directories and a stage names run files, so neither may
# end in a job file's suffix. Copied here because this script stays import-free of `stack`.
RESERVED_SUFFIXES = (".json.zst", ".log.zst", "-running.json")
PLAN_NAME_RE = r"[a-z0-9-]+"
STAGE_NAME_RE = r"[a-z][a-z0-9_]*(?:\.[a-z0-9][a-z0-9_-]*)?"


def plan_name_checks() -> None:
    """Plan and stage names in ``configs/plans/*.yaml`` follow the job-name grammar."""
    import re

    for path in sorted((CONFIGS / "plans").glob("*.yaml")):
        data = load_yaml(path) or {}
        names = [("plan", data.get("plan"), PLAN_NAME_RE)]
        names += [("stage", line.get("stage"), STAGE_NAME_RE)
                  for line in data.get("stages") or [] if isinstance(line, dict)]
        for what, name, pattern in names:
            if not isinstance(name, str):
                errors.append(f"plans/{path.name}: {what} name missing or not a string")
            elif any(name.endswith(suf) for suf in RESERVED_SUFFIXES):
                errors.append(f"plans/{path.name}: {what} {name!r} ends in a reserved suffix {RESERVED_SUFFIXES}")
            elif not re.fullmatch(pattern, name):
                errors.append(f"plans/{path.name}: {what} {name!r} does not match {pattern}")


def main() -> int:
    if not CONFIGS.is_dir():
        print(f"no configs dir at {CONFIGS}", file=sys.stderr)
        return 2
    print(f"validating {CONFIGS.relative_to(REPO)}/*.yaml against {SCHEMAS.relative_to(REPO)}/")
    for example in sorted(CONFIGS.glob("*.yaml")):
        validate_file(example)
    print(f"validating {PAYLOAD_EXAMPLES.relative_to(REPO)}/*.json against payload schemas")
    validate_payloads()
    cross_checks()
    schema_checks()
    channel_identity_checks()
    taxonomy_metadata_checks()
    interfaces_checks()
    plan_name_checks()

    if errors:
        print(f"\nFAILED with {len(errors)} error(s):", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    print("\nall configs + payloads + schemas valid and mutually consistent "
          "(x-enum tags+order, channel groups/key kinds, taxonomy "
          "exclusive_with/absorbs, license classes, priced roles, interfaces Literals, $ref payloads)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
