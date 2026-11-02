"""The model-mention matcher of ``annotate.model_roles`` (``workflows/annotate/model_roles/match.py``;
ex-``annotate.model_mentions``, merged into model_roles by D591; round 5 T-ANN, D235): the matcher, the
vocabulary loader and the scan's payload shapes. The stage that runs it is tested in
``tests/test_annotate_model_roles.py``.

Ported from v3 ``tests/test_vocab.py`` and ``tests/test_annotate_model_mentions.py``
(``tomas/model-roles``, d011e5e), the DB-free parts:

(a) the matcher reproduces the 4,827-fixture suite (copied from v3's
    ``doc/model-collection/_build/*.fixtures.yaml`` to ``tests/fixtures/model_mentions/``; plus v4's
    ``10-v0.5.fixtures.yaml`` for the v0.5 additions, R6, and ``11-v0.6`` / ``12-v0.7`` / ``13-v0.8`` / ``14-v0.9.fixtures.yaml`` for v0.6 to v0.9) against
    ``configs/models.yaml``, with ``vocab_tool.py``'s ``run_fixtures`` check semantics (``node`` =
    among the resolved winners; ``node: none`` = no match at all; ``not_node`` = not a winner);
(b) ``stack.modelvocab.ModelVocab`` validates the shipped vocabulary and is strict; the generated
    ``spec/schemas/config/models.schema.json`` has not drifted; the loader stamps the file;
(c) the substring prefilter returns exactly the unfiltered result (synthetic texts; v3's
    corpus-sample variant needs the v1 export and is not ported);
(d) the scan's shapes: ``scan_payload`` (the eval's lists), ``payload_fields`` (``MODEL_ROLES``'
    ``matches`` / ``ambiguous``, writable as the registry type) and ``resolve_name`` (an extra model's
    ``node_id``).

The matcher is loaded by path, as the runner loads it.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
import yaml
from pydantic import ValidationError

from stack.modelvocab import ModelVocab, VocabNode, load_vocab, maker_refs, vocab_digest
from stack.schemas.payloads.registry import REGISTRY

REPO = Path(__file__).resolve().parents[1]
CONFIG_DIR = REPO / "configs"
VOCAB_PATH = CONFIG_DIR / "models.yaml"
FIXTURE_DIR = REPO / "tests" / "fixtures" / "model_mentions"
WF_PATH = REPO / "workflows" / "annotate" / "model_roles" / "match.py"

sys.path.insert(0, str(REPO / "tools"))  # gen_models_schema (the freshness check)


def _load_module(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


V = _load_module(WF_PATH, "_model_roles_match_under_test")


@pytest.fixture(scope="module")
def vocab() -> Any:
    return load_vocab(CONFIG_DIR)


@pytest.fixture(scope="module")
def regexes(vocab: Any) -> dict[str, re.Pattern[str]]:
    return V.compile_regexes(vocab.nodes)


# A Scanner compiles the whole vocabulary (~2 s) and is read-only after construction: shared per module.
@pytest.fixture(scope="module")
def scanner(vocab: Any) -> Any:
    return V.Scanner(vocab)


@pytest.fixture(scope="module")
def nova_scanner(vocab: Any) -> Any:
    """A scanner over the vocabulary with the cross-lineage weak collision of :func:`_with_nova_collision`."""
    return V.Scanner(_with_nova_collision(vocab))


# ─────────────────────────── (a) the fixture suite ─────────────────────────


def _fixture_files() -> list[Path]:
    return sorted(FIXTURE_DIR.glob("*.fixtures.yaml"))


def test_fixture_files_are_present() -> None:
    """A green suite that loaded no fixtures would be a silent pass — assert the corpus."""
    files = _fixture_files()
    assert len(files) >= 12, [p.name for p in files]
    total = sum(len(yaml.safe_load(p.read_text()) or []) for p in files)
    assert total >= 4827, total


def _check_fixture(nodes: dict[str, Any], regexes: dict[str, re.Pattern[str]], fx: dict[str, Any]) -> str | None:
    """``vocab_tool.run_fixtures``' per-fixture verdict: a failure string, or None if it passed."""
    text = fx["text"]
    winners = {c for h in V.resolve(nodes, regexes, text) for c in h["candidates"]}
    if "not_node" in fx:
        if fx["not_node"] in winners:
            return f"{text!r} -> must NOT resolve to {fx['not_node']}, but does"
        return None
    expected = fx["node"]
    if expected in (None, "none"):
        return f"{text!r} -> expected no match, got {sorted(winners)}" if winners else None
    if expected not in winners:
        return f"{text!r} -> expected {expected}, got {sorted(winners) or 'no match'}"
    return None


@pytest.mark.parametrize("path", _fixture_files(), ids=lambda p: p.name)
def test_mention_fixtures(path: Path, vocab: Any, regexes: dict[str, re.Pattern[str]]) -> None:
    """Every fixture string resolves as the vocabulary authors recorded (vocab_tool parity)."""
    fixtures = yaml.safe_load(path.read_text()) or []
    failures = [f for fx in fixtures if (f := _check_fixture(vocab.nodes, regexes, fx))]
    assert not failures, f"{len(failures)}/{len(fixtures)} fixtures failed:\n" + "\n".join(failures[:20])


def test_fixture_check_can_fail(vocab: Any, regexes: dict[str, re.Pattern[str]]) -> None:
    """The fixture check is not vacuous: run the SAME code on fixtures that must fail."""
    must_fail = [
        {"text": "GPT-4.5", "node": "claude-opus-4"},   # wrong expectation
        {"text": "GPT-4.5", "node": "none"},            # expects silence, gets a match
        {"text": "GPT-4.5", "not_node": "gpt-4.5"},     # forbids the node that wins
    ]
    for fx in must_fail:
        assert _check_fixture(vocab.nodes, regexes, fx) is not None, fx
    for fx in [{"text": "GPT-4.5", "node": "gpt-4.5"},
               {"text": "a plain sentence", "node": "none"},
               {"text": "GPT-4.5", "not_node": "claude-opus-4"}]:
        assert _check_fixture(vocab.nodes, regexes, fx) is None, fx


def test_schema_worked_examples(vocab: Any, regexes: dict[str, re.Pattern[str]]) -> None:
    """schema.md's worked-example table, run against the real vocabulary."""
    cases = [
        ("we used GPT-4.5 and gpt-4", {"gpt-4.5", "gpt-4"}),
        ("ChatGPT4o is fast", {"gpt-4o"}),
        ("anthropic.claude-opus-4-20250514-v1:0", {"claude-opus-4"}),
        ("a 70B model", set()),
    ]
    for text, expected in cases:
        got = {h["node"] for h in V.resolve(vocab.nodes, regexes, text)}
        assert expected <= got, f"{text!r}: {sorted(got)}"


# ─────────────────────────── (b) the typedef and the loader ────────────────


@pytest.mark.slow
def test_typedef_validates_shipped_vocabulary() -> None:
    """``configs/models.yaml`` validates against ``ModelVocab`` (and carries both versions)."""
    model = ModelVocab.model_validate(yaml.safe_load(VOCAB_PATH.read_text()))
    # models 1.1.0 (D738): 1.0.0's nodes (D707: v0.9.0's content, the 2026-10-17 review; v0.8: 1,809) in the envelope
    assert model.version == "1.1.0" and model.meta.schema_ == "models-2" and model.meta.references == {"orgs": "0.2.0"}
    assert len(model.nodes) == 1877  # D623: +4 (Vicuna-7B, Vicuna-13B, Alpaca-7B, Zephyr-7B)
    kinds = {k: sum(1 for n in model.nodes.values() if n.kind == k) for k in ("family", "group", "model")}
    assert kinds == {"family": 310, "group": 249, "model": 1318}
    refs = [m for n in model.nodes.values() for m in maker_refs(n)]
    assert refs and all(m.id and m.name for m in refs)  # every maker reference has an id and a display name


@pytest.mark.slow
def test_shipped_facets_are_filled_and_consistent() -> None:
    """Vocabulary 0.9 facets on the shipped file: every model node has a ``model_class`` (except the
    interpretability instruments, which the class vocabulary does not cover); a family / group's classes
    contain its children's (union semantics); every maker has facets; a folded model node carries no
    scalar params (its date is the earliest member's); ``class-only`` only on a node without children. (The maker
    facets of 1.0.0 are the organisation entries' since 1.1.0: tests/test_vendored_taxonomies.py.)"""
    model = ModelVocab.model_validate(yaml.safe_load(VOCAB_PATH.read_text()))
    nodes = model.nodes
    children: dict[str, list[str]] = {}
    for nid, n in nodes.items():
        if n.parent:
            children.setdefault(n.parent, []).append(nid)

    def instrument(nid: str | None) -> bool:
        while nid:
            if "instrument" in (nodes[nid].tags or []):
                return True
            nid = nodes[nid].parent
        return False

    missing = [k for k, n in nodes.items() if n.kind == "model" and not n.model_class and not instrument(k)]
    assert missing == [], missing
    for nid, kids in children.items():
        union = set().union(*(set(nodes[c].model_class or []) for c in kids))
        assert union <= set(nodes[nid].model_class or []), (nid, sorted(union - set(nodes[nid].model_class or [])))
    for nid, n in nodes.items():
        if n.representation_status == "folded" and n.kind == "model":
            assert n.params is None or n.params.total_m is None, nid
        if n.representation_status == "class-only":
            assert nid not in children, nid


def test_typedef_is_strict() -> None:
    """``extra="forbid"``: an unknown node field is an error, not silently carried."""
    with pytest.raises(ValidationError):
        VocabNode.model_validate({"id": "x", "kind": "model", "name": "X", "parmas": {"kind": "official"}})
    with pytest.raises(ValidationError):
        VocabNode.model_validate({"id": "x", "kind": "vendor", "name": "X"})
    with pytest.raises(ValidationError):
        VocabNode.model_validate({"id": "x", "kind": "model", "name": "X", "lifecycle": {"shipped": "2025"}})
    with pytest.raises(ValidationError):
        VocabNode.model_validate({"id": "x", "kind": "model", "name": "X", "confidence": "probably"})
    with pytest.raises(ValidationError):  # models 1.1.0: no node without its id
        VocabNode.model_validate({"kind": "model", "name": "X"})
    with pytest.raises(ValidationError):  # maker and lab are references {id, name}, not slugs or strings (D738)
        VocabNode.model_validate({"id": "x", "kind": "model", "name": "X", "maker": "openai"})
    with pytest.raises(ValidationError):
        VocabNode.model_validate({"id": "x", "kind": "model", "name": "X", "lab": "DeepMind"})
    assert VocabNode.model_validate({"id": "x", "kind": "model", "name": "X", "lab": {"id": "deepmind", "name": "D"},
                                     "maker": [{"id": "a", "name": "A"}, {"id": "b", "name": "B"}]}).lab is not None


def test_typedef_accepts_both_date_spellings() -> None:
    """YAML gives ``date`` for an unquoted full date and ``str`` for quoted/partial ones."""
    import datetime

    n = VocabNode.model_validate(yaml.safe_load(
        "id: x\nkind: model\nname: X\nlifecycle: {released: 2025-05-22, preview: '2025-05'}\n"))
    assert n.lifecycle is not None
    assert n.lifecycle.released == datetime.date(2025, 5, 22)
    assert n.lifecycle.preview == "2025-05"
    with pytest.raises(ValidationError):
        VocabNode.model_validate({"id": "x", "kind": "model", "name": "X", "lifecycle": {"released": "spring 2025"}})


def test_typedef_accepts_optional_tags() -> None:
    """``tags`` is an optional list of short labels; only the SHAPE is pinned here."""
    assert VocabNode.model_validate({"id": "x", "kind": "model", "name": "X", "tags": ["pre-llm"]}).tags == ["pre-llm"]
    assert VocabNode.model_validate({"id": "x", "kind": "model", "name": "X"}).tags is None
    with pytest.raises(ValidationError):  # a bare string is not a list of tags
        VocabNode.model_validate({"id": "x", "kind": "model", "name": "X", "tags": "pre-llm"})
    with pytest.raises(ValidationError):
        VocabNode.model_validate({"id": "x", "kind": "model", "name": "X", "tag": ["pre-llm"]})


def test_typedef_facets_shape() -> None:
    """Vocabulary 0.9 facets: closed value sets; the prepared ``weights_access`` and lifecycle
    stage validate when present and are optional (they are filled in a later pass)."""
    n = VocabNode.model_validate({
        "id": "x", "kind": "model", "name": "X", "model_class": ["language-model", "vision-language"],
        "specialization": ["reasoning"], "representation_status": "folded",
        "weights_access": {"status": "gated", "licence": "llama3.1", "evidence": "HF card",
                           "as_of": "2026-10"},
        "lifecycle": {"released": "2025", "status": "ga", "status_as_of": "2026-10",
                      "first_public": "2025-01", "ga": "2025-03"},
        "modalities": {"in": ["image", "text"], "out": ["action"]}})
    assert n.model_class == ["language-model", "vision-language"]
    assert n.weights_access is not None and n.weights_access.status == "gated"
    bare = VocabNode.model_validate({"id": "x", "kind": "model", "name": "X"})
    assert bare.weights_access is None and bare.model_class is None
    for bad in ({"model_class": ["llm"]}, {"model_class": "language-model"},
                {"specialization": ["math"]}, {"representation_status": "partial"},
                {"weights_access": {"status": "open"}}, {"weights_access": {"licence": "mit"}},
                {"lifecycle": {"status": "GA"}}):
        with pytest.raises(ValidationError):
            VocabNode.model_validate({"id": "x", "kind": "model", "name": "X", **bad})


def test_typedef_makers() -> None:
    """models 1.1.0 (D738): one name per maker id, unique entry ids; the file before it (``nodes``, ``meta.makers``)
    reads through ``from_legacy`` with the maker display names as the references' names (a slug without one shows
    as itself, as the menu did)."""
    from stack.modelvocab import from_legacy

    meta = {"schema": "models-2", "status": "draft", "updated": "2026-10-17", "references": {"orgs": "0.2.0"}}

    def entry(nid: str, maker: object) -> dict[str, object]:
        return {"id": nid, "kind": "model", "name": nid, "maker": maker}

    ok = ModelVocab.model_validate({"version": "1.1.0", "meta": meta, "entries": [
        entry("m1", {"id": "a", "name": "Lab A"}), entry("m2", [{"id": "a", "name": "Lab A"}, {"id": "b", "name": "B"}])]})
    assert list(ok.nodes) == ["m1", "m2"] and [m.name for m in maker_refs(ok.nodes["m2"])] == ["Lab A", "B"]
    with pytest.raises(ValidationError, match="several names"):
        ModelVocab.model_validate({"version": "1.1.0", "meta": meta, "entries": [
            entry("m1", {"id": "a", "name": "Lab A"}), entry("m2", {"id": "a", "name": "Lab a"})]})
    with pytest.raises(ValidationError, match="duplicate entry ids"):
        ModelVocab.model_validate({"version": "1.1.0", "meta": meta, "entries": [entry("m1", None), entry("m1", None)]})
    legacy = from_legacy({"version": "0.5", "meta": {"schema_version": "0.3", "updated": "2026-10-13",
                                                     "makers": {"a": "Lab A"}, "maker_facets": {"a": {"country": "FR"}}},
                          "nodes": {"m1": {"kind": "model", "name": "M", "maker": ["a", "z"], "lab": "Unit"}}})
    lv = ModelVocab.model_validate(legacy)
    assert lv.meta.schema_ == "models-1" and lv.nodes["m1"].lab is not None and lv.nodes["m1"].lab.id is None
    assert [(m.id, m.name) for m in maker_refs(lv.nodes["m1"])] == [("a", "Lab A"), ("z", "z")]


def test_generated_json_schema_is_fresh() -> None:
    """``spec/schemas/config/models.schema.json`` (what ``tools/check_configs.py`` validates the
    file against) still equals what ``tools/gen_models_schema.py`` generates from ``ModelVocab``."""
    import gen_models_schema

    committed = json.loads((REPO / "spec" / "schemas" / "config" / "models.schema.json").read_text())
    assert committed == gen_models_schema.build(), (
        "stale spec/schemas/config/models.schema.json — run tools/gen_models_schema.py")


@pytest.mark.slow
def test_config_yaml_carries_no_date_objects() -> None:
    """Dates in ``configs/models.yaml`` are quoted, so they load as strings (an unquoted date
    passes the typedef but fails the JSON-Schema gate)."""
    import datetime

    data = yaml.safe_load(VOCAB_PATH.read_text())
    offenders = [
        n["id"] for n in data["entries"]
        if isinstance(n.get("checked"), datetime.date)
        or any(isinstance(v, datetime.date) for v in (n.get("lifecycle") or {}).values())
    ]
    assert not offenders, offenders[:10]


def test_load_vocab_stamps_the_file(vocab: Any) -> None:
    """The loader produces the standard ``<semver>+<sha8>`` stamp and the file's full sha256."""
    sha = hashlib.sha256(VOCAB_PATH.read_bytes()).hexdigest()
    assert re.fullmatch(r"\d+\.\d+(\.\d+)?\+[0-9a-f]{8}", vocab.stamp), vocab.stamp
    assert vocab.stamp.endswith(sha[:8]) and vocab.digest == sha == vocab_digest(CONFIG_DIR)
    assert vocab.nodes is not vocab.validated.nodes  # raw mapping for the matcher, model for the shape


def test_vocab_nodes_are_the_raw_mapping(vocab: Any) -> None:
    """The matcher consumes plain dicts, not pydantic models (twin-parity requirement)."""
    node: Any = vocab.nodes["claude-opus-4"]
    assert isinstance(node, dict)
    assert node["kind"] == "model"


# ─────────────────────────── (c) prefilter equivalence ─────────────────────

SYNTHETIC = [
    "We evaluated GPT-4.5, Claude Opus 4.7, gemini-2.5-pro and Llama 3.1 70B.",
    "meta-llama/Llama-3.1-70B-Instruct beat anthropic.claude-opus-4-20250514-v1:0.",
    "A Magnum Opus of the 19th century; the nova was bright; BERT-style encoders.",
    "No models here at all, just prose about alignment and interpretability.",
    "ChatGPT4o, GPT_4o, gpt-4o and Chat GPT 4o are the same thing. Midjourney --v 6 too.",
    "Qwen3-32B, DeepSeek-R1, Mixtral 8x7B, phi-4, o3-mini, grok 4, Command A.",
    "gpt-40 is not a model and neither is opus 47 nor a bare 70B.",
    "İstanbul: a Turkish dotted capital I makes lowercasing change the length; GPT-4.5 still.",
]


@pytest.mark.slow
def test_prefilter_is_result_preserving_synthetic(vocab: Any, scanner: Any) -> None:
    fast = scanner  # prefilter=True, the default
    assert fast.prefilter
    slow = V.Scanner(vocab, prefilter=False)
    hits = 0
    for text in SYNTHETIC:
        got = fast.scan(text)
        assert got == slow.scan(text), text[:200]
        hits += bool(got["mentions"])
    assert hits >= 5  # the equivalence is over texts that do mention models


def test_prefilter_narrows_meaningfully(vocab: Any, regexes: dict[str, re.Pattern[str]]) -> None:
    """The prefilter must actually filter (v3's two loose floors)."""
    pf = V.Prefilter(vocab.nodes, regexes)
    assert len(pf.nodes_for("Prose about alignment and interpretability.")) < 30
    active = pf.nodes_for("We evaluated GPT-4.5 and Claude Opus 4.7 on a benchmark.")
    assert len(active) < len(regexes) / 4, len(active)
    assert "gpt-4.5" in active and "claude-opus-4.7" in active


def _with_nova_collision(vocab: Any) -> Any:
    """The vocabulary plus one cross-lineage weak collision. v0.9 (2026-10-17) removed the real one these
    tests used (bare "nova 1" on Amazon nova-1 and Deepgram deepgram-nova), and the shipped vocabulary has
    no other equal-span cross-lineage tie, so the ambiguous bucket is exercised on that collision re-added."""
    import copy
    import dataclasses

    nodes = dict(vocab.nodes)
    for nid in ("nova-1", "deepgram-nova"):
        node = copy.deepcopy(nodes[nid])
        node["weak_mentions"] = [*(node.get("weak_mentions") or []), "nova 1"]
        nodes[nid] = node
    return dataclasses.replace(vocab, nodes=nodes)


def test_scan_separates_strong_weak_and_ambiguous(nova_scanner: Any) -> None:
    """The three buckets carry what they claim, with duplicates kept as the occurrence count."""
    scanner = nova_scanner
    out = scanner.scan("GPT-4.5 and GPT-4.5 again. A Magnum Opus. Nova 1 is the ambiguous one.")
    assert out["mentions"]["gpt-4.5"] == ["GPT-4.5", "GPT-4.5"]  # duplicates kept
    assert out["weak"] == {"claude-opus": ["Opus"]}              # bare 'opus' is weak
    assert out["ambiguous"] == [
        {"text": "Nova 1", "candidates": ["deepgram-nova", "nova-1"], "strength": "weak"}
    ]
    assert "nova-1" not in out["mentions"]  # no winner invented


def test_strong_span_classification(vocab: Any) -> None:
    """``is_strong_span`` keys off the pattern list the winning alternative came from."""
    strong = V.compile_strong_regexes(vocab.nodes)
    assert V.is_strong_span(strong, "claude-opus", "Claude Opus")
    assert not V.is_strong_span(strong, "claude-opus", "Opus")


# ─────────────────────────── (d) the scan's payload shapes ─────────────────


def test_scan_payload_is_sorted_lists(nova_scanner: Any) -> None:
    """v3's maps become lists sorted by node_id; surfaces keep document order and duplicates (the shape
    ``stack eval model_mentions`` compares with the frozen sets)."""
    scanner = nova_scanner
    p = V.scan_payload(scanner, "Llama 3.1 70B then GPT-4.5, gpt-4.5 and a Magnum Opus. Nova 1.")
    ids = [m["node_id"] for m in p["mentions"]]
    assert ids == sorted(ids) and {"gpt-4.5", "llama-3.1-70b"} <= set(ids)
    assert next(m for m in p["mentions"] if m["node_id"] == "gpt-4.5")["surfaces"] == ["GPT-4.5", "gpt-4.5"]
    assert p["weak"] == [{"node_id": "claude-opus", "surfaces": ["Opus"]}]
    assert p["ambiguous"][0]["candidates"] == ["deepgram-nova", "nova-1"]
    assert V.is_empty(V.scan_payload(scanner, "A recipe for sourdough bread."))
    assert not V.is_empty(V.scan_payload(scanner, "Nova 1 only."))  # an ambiguous span is a hit


def test_payload_fields_are_strong_then_weak_and_writable(nova_scanner: Any) -> None:
    """``matches``: strong entries by node_id, then weak ones (a node with both has one of each);
    ``ambiguous`` in document order; both fit the record ``model_regexp_matches``' fields."""
    scanner = nova_scanner
    scan = scanner.scan("Opus first. GPT-4.5, Claude Opus 4.7 and Claude Opus, then GPT-4.5 again. Nova 1.")
    f = V.payload_fields(scan)
    assert [(m["node_id"], m["strength"]) for m in f["matches"]] == [
        ("claude-opus", "strong"), ("claude-opus-4.7", "strong"), ("gpt-4.5", "strong"), ("claude-opus", "weak")]
    assert f["matches"][2]["surfaces"] == ["GPT-4.5", "GPT-4.5"] and f["matches"][3]["surfaces"] == ["Opus"]
    assert f["ambiguous"] == [{"text": "Nova 1", "candidates": ["deepgram-nova", "nova-1"], "strength": "weak"}]
    schema = REGISTRY["model_regexp_matches"].payload_schema
    for name in ("matches", "ambiguous"):
        pa.array([f[name]], type=schema.field(name).type)  # fits the registry type
    assert V.payload_fields(scanner.scan("no models")) == {"matches": [], "ambiguous": []}


def test_resolve_name_needs_exactly_one_strong_node(scanner: Any) -> None:
    """An extra model's ``name_as_written`` resolves when its scan has exactly one strong node."""
    assert V.resolve_name(scanner, "Claude Opus 4.7") == "claude-opus-4.7"
    assert V.resolve_name(scanner, "gpt-4.5 (GPT-4.5)") == "gpt-4.5"  # one node, twice
    assert V.resolve_name(scanner, "Opus") is None                      # weak only
    assert V.resolve_name(scanner, "GPT-4.5 and Claude Opus 4.7") is None  # two nodes
    assert V.resolve_name(scanner, "FooLM-7B") is None                  # not in the vocabulary


@pytest.mark.slow
def test_scanner_for_compiles_once_per_digest(tmp_path: Path) -> None:
    """The scanner cache is keyed by the vocabulary's sha256: the same file shares one scanner, an
    edited copy gets its own."""
    a = V.scanner_for(CONFIG_DIR)
    assert V.scanner_for(CONFIG_DIR).scanner is a.scanner and a.vocab.digest == vocab_digest(CONFIG_DIR)
    cfg = tmp_path / "configs"
    cfg.mkdir()
    (cfg / "models.yaml").write_text(VOCAB_PATH.read_text("utf-8") + "# an edit\n", "utf-8")
    b = V.scanner_for(cfg)
    assert b.vocab.digest != a.vocab.digest and b.scanner is not a.scanner
