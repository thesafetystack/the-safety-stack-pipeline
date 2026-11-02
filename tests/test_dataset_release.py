"""The dataset release (stack/fold/dataset.py, configs/releases/dataset/): the mapping and its schema, the quote cut,
the prose scrub, the per-kind metadata (provenance, config_id, annotator) and the status remap on synthetic corpus
rows, the overrides chain, the active overrides and their public notes, the slim bundle's file list and the card's
quality table. The end-to-end export runs on a real build (not here)."""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from stack.fold import dataset as ds
from stack.fold.dataset import DATASET_DIR, DatasetError, Inputs, Mapping_, Projection
from stack.fold.release import REPO_ROOT
from stack.overrides import Override, rows_digest
from stack.schemas.families import family
from stack.schemas.payloads.registry import REGISTRY

AT = dt.datetime(2026, 10, 20, 12, 0, tzinfo=dt.UTC)
TEXT = "a" * 64
D = {"screen": "1" * 64, "affiliation": "2" * 64, "technique_staged": "3" * 64, "model_roles": "4" * 64,
     "affiliation_orgs": "5" * 64, "translation": "6" * 64}


@pytest.fixture(scope="module")
def m() -> Mapping_:
    mp = Mapping_(DATASET_DIR / "mapping.yaml")
    ds.check_transforms(mp)
    return mp


# ---------------------------------------------------------------- the mapping
def test_mapping_schema_shape(m: Mapping_) -> None:
    ann = m.schema.field("annotations").type
    kinds = [f.name for f in ann]
    assert kinds == [*m.kinds, "other"]
    metas = {ann.field(k).type.field("metadata").type for k in m.kinds}
    assert len(metas) == 1, "metadata must be one struct type for every kind"
    assert ann.field("other").type.value_type.field("metadata").type == m.meta_type
    for f in m.schema:
        assert (f.metadata or {}).get(b"description"), f"top-level {f.name} has no description"
    st = ann.field("safety_techniques").type.field("payload").type
    quote = st.field("techniques").type.value_type.field("quotes").type.value_type
    assert quote.names == ["text", "anchored"]  # renamed from verified
    mnm = ann.field("model_name_matches").type.field("payload").type
    assert mnm.field("matches").type.value_type.names == ["node_id", "surfaces", "match_class"]
    assert "repairs" not in st.names and "prepass" not in st.names  # allow-list drops
    members = m.schema.field("identity").type.field("members").type.value_type
    assert "channel_class" in members.names and "license_class" not in members.names


def _mutated(tmp_path: Path, edit: Any) -> Path:
    import yaml

    raw = yaml.safe_load((DATASET_DIR / "mapping.yaml").read_text("utf-8"))
    edit(raw)
    p = tmp_path / "mapping.yaml"
    p.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), "utf-8")
    return p


def test_mapping_refuses_a_bad_internal_path(tmp_path: Path) -> None:
    def edit(raw: dict[str, Any]) -> None:
        next(e for e in raw["fields"] if e["public"] == "abstract")["internal"] = "metadata.abstrakt"  # title: a transform

    with pytest.raises(DatasetError, match="not in the documents schema"):
        Mapping_(_mutated(tmp_path, edit))


def test_mapping_refuses_a_kept_leaf_without_doc(tmp_path: Path) -> None:
    def edit(raw: dict[str, Any]) -> None:
        del next(e for e in raw["fields"] if e["public"] == "annotations.screen")["docs"]["reasoning"]

    with pytest.raises(DatasetError, match="no docs entry"):
        Mapping_(_mutated(tmp_path, edit))


def test_mapping_refuses_an_unmapped_workflow(tmp_path: Path) -> None:
    def edit(raw: dict[str, Any]) -> None:
        raw["dropped_workflows"] = ["triage"]

    with pytest.raises(DatasetError, match="technique_roles"):
        Mapping_(_mutated(tmp_path, edit))


def test_transforms_match_the_mapping(m: Mapping_, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(ds.TRANSFORMS, "unmapped.thing", lambda p, c: None)
    with pytest.raises(DatasetError, match="unmapped"):
        ds.check_transforms(m)


# ---------------------------------------------------------------- cutting and scrubbing
@pytest.mark.parametrize("s,cap,want", [
    ("short quote", 300, "short quote"),
    ("x" * 300, 300, "x" * 300),
    ("word " * 100, 300, ("word " * 60).rstrip() + "…"),  # the 60th word ends at 299: a boundary, kept
    ("y" * 400, 300, "y" * 299 + "…"),
    ("ab cd ef", 5, "ab…"),
    ("ab cd ef", 6, "ab cd…"),  # the character after cap-1 is a space: the prefix is kept whole
    ("ab cd ef", 7, "ab cd…"),
])
def test_cut_string(s: str, cap: int, want: str) -> None:
    got = ds.cut_string(s, cap)
    assert got == want
    assert len(got) <= cap


def test_scrub_rules(m: Mapping_) -> None:
    rules = m.prose_scrub[0]["rules"]
    s = "Specific models are graded on their menu nodes. Menu row X; its own menu entries too."
    out = ds._scrub(s, rules)
    assert out == "Specific models are graded on their catalogue entries. Catalogue entry X; its own catalogue entries too."


def test_walk_strings_paths() -> None:
    obj = {"techniques": [{"quotes": [{"text": "a"}, {"text": "b"}]}, {"quotes": None}],
           "matches": [{"surfaces": ["s1", "s2"]}]}
    got = [(p, v) for p, _h, _k, v in ds.walk_strings(obj, "techniques[].quotes[].text", "x")]
    assert got == [("x.techniques[0].quotes[0].text", "a"), ("x.techniques[0].quotes[1].text", "b")]
    got = [(p, v) for p, _h, _k, v in ds.walk_strings(obj, "matches[].surfaces[]")]
    assert got == [("matches[0].surfaces[0]", "s1"), ("matches[0].surfaces[1]", "s2")]


# ---------------------------------------------------------------- synthetic rows
def _blank(t: pa.DataType) -> Any:
    if pa.types.is_struct(t):
        return {f.name: _blank(f.type) for f in t}
    if pa.types.is_list(t):
        return []
    return None


def _rec(wf: str, *, status: str = "ok", origin: str = "ai_check", digest: str | None = None,
         payload: dict[str, Any] | None = None, evidence: str | None = "ev1", error_class: str | None = None) -> dict:
    spec = REGISTRY[wf]
    return {"workflow": wf, "version": spec.version, "status": status, "error_class": error_class, "basis_kind": "text",
            "basis_digest": TEXT, "config_digest": digest or D[spec.produced_by], "decided_at": AT, "evidence_id": evidence,
            "origin": origin, "is_basis_current": True,
            "payload": payload if payload is not None else _blank(spec.payload_schema)}


ROLES = {r: 0 for r in ("mentioned", "cited_work", "discussed", "used", "evaluated", "modified", "introduced", "analyzed",
                        "attested_use", "suspected_use", "considered", "critiqued")}


MODEL_ROLES = {r: 0 for r in ("mentioned", "cited_work", "discussed", "usage_experience", "built_on", "instrument",
                              "modified", "evaluated_behavioral", "analyzed_internals", "introduced")}


def _row(doc: str = "sdoc000000001", **over: dict[str, Any]) -> dict[str, Any]:
    ann: dict[str, Any] = {wf: None for wf in REGISTRY}
    for wf in ("screen", "doc_class", "stack_relevance", "sr_relevance", "text_usability", "technique_staged",
               "model_roles", "model_regexp_matches"):
        ann[wf] = _rec(wf)
    ann["technique_staged"]["payload"]["techniques"] = [
        {"slug": "rlhf", "roles": {**ROLES, "mentioned": 2, "used": 2},
         "quotes": [{"text": "word " * 80, "verified": True}], "flags": [], "note": None}]
    ann["model_roles"]["payload"]["models"] = [
        {"node_id": "gpt-4", "matched_node_id": "gpt-4", "roles": {**MODEL_ROLES, "mentioned": 1}, "evidence": [],
         "note": "graded on the menu node gpt-4", }]
    ann["other"] = []
    ann.update(over)
    return {"doc_id": doc, "state": "acquired", "annotations": ann,
            "content": {"current_text_sha256": TEXT}}


def _pin(wf: str, name: str, models: list[str], plan_pin: dict[str, Any]) -> dict[str, Any]:
    digests = ["t" * 64]
    if wf in ds.TAXONOMY_FILES:
        path = plan_pin.get(ds.TAXONOMY_PIN_KEYS.get(wf, "")) or ds.TAXONOMY_FILES[wf][1]
        digests.append(hashlib.sha256((REPO_ROOT / path).read_bytes()).hexdigest())
    return {"workflow": wf, "prompt_pin": {"workflow": wf, "prompt_name": name, "models": models, "plan_pin": plan_pin,
                                           "template_digests": digests, "registry_version": REGISTRY[wf].version},
            "run_id": "r1", "code_digest": "c" * 64}


def _inputs(overrides: list[Override], families: dict[str, dict[str, tuple[list[str], bool]]] | None = None,
            models: dict[str, str] | None = None) -> Inputs:
    pins = {D["screen"]: _pin("screen", "screen-v12", ["openai/gpt-6-luna"], {"name": "screen-v12",
                                                                             "model": "openai/gpt-6-luna"}),
            D["affiliation"]: _pin("affiliation", "affiliation-v1", ["openai/gpt-6-luna", "openai/gpt-5.2"],
                                   {"name": "affiliation-v1", "role": "extract_main"}),
            D["technique_staged"]: _pin("technique_staged", "technique-staged-v5-a1.1", ["openai/gpt-6-luna"],
                                        {"name": "technique-staged-v5-a1.1", "taxonomy": "taxonomy/techniques-1.0.0.yaml",
                                         "role": "extract_staged_openai"}),
            D["model_roles"]: _pin("model_roles", "model-roles-v3b", ["openai/gpt-6-luna"],
                                   {"name": "model-roles-v3b", "role": "extract_main_openai"}),
            D["affiliation_orgs"]: _pin("affiliation_orgs", "orgs-0.2.2", [],
                                        {"name": "orgs-0.2.2", "vocab": "taxonomy/orgs-0.2.2.yaml"}),
            D["translation"]: _pin("translation", "translate-short-v1", ["openai/gpt-6-luna"],
                                   {"name": "translate-short-v1", "role": "translate"})}
    ev = {p: {"ev1": models.get(p) if models else "openai/gpt-6-luna"} for p in ("screen", "affiliation", "technique_staged",
                                                                                "model_roles", "translation")}
    return Inputs(Path("."), {}, {}, {}, families or {}, ev, pins, "h.overrides", [], {o.override_id: o for o in overrides})


def _ov(oid: str, target: str, kind: str, *, field: str = "", value: Any = None, tier: str = "human",
        author: str = "tomas", note: str = "n", key: str = "sdoc000000001") -> Override:
    return Override(override_id=oid, target=target, key=key, item_key="", kind=kind, field=field,
                    payload={"value": value, "note": note}, tier=tier, created_at=AT, author=author)


def _proj(m: Mapping_, inp: Inputs, variant: str = "public") -> Projection:
    return Projection(m, inp, variant, "2026-10-31", m.variants[variant]["quote_cap_chars"])


def test_pipeline_records(m: Mapping_) -> None:
    p = _proj(m, _inputs([]))
    ann = p.annotations(_row())
    assert ann["affiliation"] is None  # not run
    s = ann["screen"]["metadata"]
    assert s["config_id"] == f"screen:screen-v12:gpt-6-luna:{D['screen'][:8]}"
    assert ann["doc_class"]["metadata"]["config_id"] == s["config_id"]  # one call, five records
    assert s["provenance"] == "pipeline" and s["annotator"] == "gpt-6-luna" and s["override_ids"] == []
    mnm = ann["model_name_matches"]["metadata"]
    assert mnm["annotator"] == "matcher"
    assert mnm["config_id"] == ann["model_roles"]["metadata"]["config_id"].replace("model_roles:", "model_roles:")
    assert set(p.configs) == {s["config_id"], ann["model_roles"]["metadata"]["config_id"],
                              ann["safety_techniques"]["metadata"]["config_id"]}
    assert sorted(p.configs[mnm["config_id"]].kinds) == ["model_name_matches", "model_roles"]
    assert ann["safety_techniques"]["payload"]["techniques"][0]["quotes"][0] == {"text": "word " * 80, "anchored": True}
    assert not p.problems and not p.remap


def test_annotator_is_the_evidence_model_and_computed_is_rule(m: Mapping_) -> None:
    row = _row()
    row["annotations"]["affiliation"] = _rec("affiliation", payload={"items": []})
    p = _proj(m, _inputs([], models={"affiliation": "openai/gpt-5.2"}))
    md = p.annotations(row)["affiliation"]["metadata"]
    assert md["annotator"] == "gpt-5.2"  # the fallback model answered; the config names luna first
    assert md["config_id"] == f"affiliation:affiliation-v1:gpt-6-luna:{D['affiliation'][:8]}"
    row["annotations"]["affiliation"]["origin"] = "computed"
    md = p.annotations(row)["affiliation"]["metadata"]
    assert md["annotator"] == "rule" and md["config_id"] == f"affiliation:affiliation-v1:rule:{D['affiliation'][:8]}"
    assert p.configs[md["config_id"]].model is None


def test_field_override_is_mixed(m: Mapping_) -> None:
    o1 = _ov("scr-x-verdict", "screen", "field", field="verdict", value="relevant")
    o2 = _ov("scr-x-otr", "screen", "field", field="off_topic_reason", value=None)
    row = _row()
    row["annotations"]["screen"]["origin"] = "human"
    p = _proj(m, _inputs([o1, o2], {"screen": {row["doc_id"]: (["scr-x-verdict", "scr-x-otr"], False)}}))
    md = p.annotations(row)["screen"]["metadata"]
    assert md["provenance"] == "mixed" and md["origin"] == "human"
    assert md["override_ids"] == ["scr-x-verdict", "scr-x-otr"]
    assert md["override_fields"] == ["verdict", "off_topic_reason"]
    assert md["config_id"].startswith("screen:screen-v12:")  # the base model answer's call


def _stand_in(m: Mapping_, payload: dict[str, Any], *, d717: bool = True) -> tuple[Projection, dict[str, Any]]:
    o = _ov("impl-ts-sdoc", "technique_staged", "row", value=payload, tier="ai_check", author="ai:claude-opus-5-5")
    row = _row()
    row["annotations"]["technique_staged"] = _rec("technique_staged", digest=rows_digest([o]) if d717 else None,
                                                  payload=copy.deepcopy(payload), evidence=None)
    p = _proj(m, _inputs([o], {"technique_staged": {row["doc_id"]: (["impl-ts-sdoc"], True)}}))
    return p, p.annotations(row)


def test_stand_in_override_provenance_and_empty_remap(m: Mapping_) -> None:
    empty = _blank(REGISTRY["technique_staged"].payload_schema)
    p, ann = _stand_in(m, empty)
    md = ann["safety_techniques"]["metadata"]
    assert md["provenance"] == "override" and md["config_id"] == "override:impl-ts-sdoc"
    assert md["annotator"] == "claude-opus-5-5"
    assert md["status"] == "empty"  # remapped: the producer's own empty rule
    assert p.remap == [{"release_id": "2026-10-31", "doc_id": "sdoc000000001", "kind": "safety_techniques",
                        "field": "status", "old": "ok", "new": "empty", "rule": "override_empty",
                        "reason": next(r["reason"] for r in m.remap_rules if r["id"] == "override_empty")}]
    assert p.configs["override:impl-ts-sdoc"].model == "claude-opus-5-5"


def test_stand_in_with_techniques_stays_ok(m: Mapping_) -> None:
    pay = _blank(REGISTRY["technique_staged"].payload_schema)
    pay["techniques"] = [{"slug": "rlhf", "roles": ROLES, "quotes": [], "flags": [], "note": None}]
    p, ann = _stand_in(m, pay)
    assert ann["safety_techniques"]["metadata"]["status"] == "ok" and not p.remap


def test_stand_in_folded_before_d717_is_refused(m: Mapping_) -> None:
    with pytest.raises(DatasetError, match="D717"):
        _stand_in(m, _blank(REGISTRY["technique_staged"].payload_schema), d717=False)


def test_matcher_record_of_a_failed_call(m: Mapping_) -> None:
    row = _row()
    a = row["annotations"]
    a["model_roles"] = _rec("model_roles", status="failed_permanent", error_class="query_invalid",
                            payload={**_blank(REGISTRY["model_roles"].payload_schema), "models": None})
    a["model_regexp_matches"] = _rec("model_regexp_matches", status="failed_permanent", error_class="query_invalid",
                                     payload={"matches": None, "ambiguous": None})
    p = _proj(m, _inputs([]))
    ann = p.annotations(row)
    assert ann["model_name_matches"]["metadata"]["error_class"] == "producer_call_failed"
    assert ann["model_name_matches"]["payload"] == {"matches": None, "ambiguous": None}
    assert ann["model_roles"]["metadata"]["error_class"] == "query_invalid"
    assert [(r["kind"], r["old"], r["new"]) for r in p.remap] == [("model_name_matches", "query_invalid",
                                                                   "producer_call_failed")]


def test_matcher_failure_without_a_failed_call_is_a_problem(m: Mapping_) -> None:
    row = _row()
    row["annotations"]["model_regexp_matches"] = _rec("model_regexp_matches", status="failed_permanent",
                                                      error_class="query_invalid")
    p = _proj(m, _inputs([]))
    p.annotations(row)
    assert p.problems and "without a failed producing call" in p.problems[0]


def test_basis_gate(m: Mapping_) -> None:
    row = _row()
    row["annotations"]["screen"]["is_basis_current"] = False
    p = _proj(m, _inputs([]))
    p.annotations(row)
    assert any("basis gate" in x for x in p.problems)


def test_unknown_other_kind_is_refused(m: Mapping_) -> None:
    row = _row()
    row["annotations"]["other"] = [{"workflow": "new_thing"}]
    with pytest.raises(DatasetError, match="json_kinds"):
        _proj(m, _inputs([])).annotations(row)


@pytest.mark.slow
def test_cap_cuts_and_counts(m: Mapping_) -> None:
    for variant, cut in (("public", True), ("internal", False)):
        p = _proj(m, _inputs([]), variant)
        ann = p.annotations(_row())
        ctx: dict[str, Any] = {"cuts": [], "quote_chars": 0}
        p.cap_and_scrub(ann, ctx)
        q = ann["safety_techniques"]["payload"]["techniques"][0]["quotes"][0]["text"]
        note = ann["model_roles"]["payload"]["models"][0]["note"]
        assert note == "graded on the catalogue entry gpt-4"  # scrubbed in both variants
        if cut:
            assert len(q) <= 300 and q.endswith("…")
            assert ctx["cuts"] == [{"path": "annotations.safety_techniques.payload.techniques[0].quotes[0].text",
                                    "chars": 400}]
            assert ctx["quote_chars"] == len(q)
        else:
            assert q == "word " * 80 and ctx["cuts"] == [] and ctx["quote_chars"] == 400


# ---------------------------------------------------------------- overrides: chain and public projection
def _write_part(path: Path, rows: list[Override], file_id: str, prior: list[str]) -> None:
    t = pa.Table.from_pylist([o.to_row() for o in rows], schema=family("overrides").schema)
    meta = {b"stack.family": b"overrides", b"stack.file_id": file_id.encode(), b"stack.prior": json.dumps(prior).encode()}
    pq.write_table(t.replace_schema_metadata(meta), path)


def test_overrides_chain_deltas_and_refusals(tmp_path: Path) -> None:
    d = tmp_path / "data" / "overrides" / "s"
    d.mkdir(parents=True)
    a, b = _ov("a1", "screen", "field", field="verdict"), _ov("b1", "screen", "field", field="verdict")
    _write_part(d / "1_ra.full.parquet", [a], "ra.overrides", [])
    _write_part(d / "2_rb.full.parquet", [a, b], "rb.overrides", ["ra.overrides"])
    chain = ds._overrides_chain(tmp_path, "rb.overrides")
    assert [[o.override_id for o in rows] for _p, _h, rows in chain] == [["a1"], ["b1"]]
    changed = _ov("a1", "screen", "field", field="verdict", value="irrelevant")
    _write_part(d / "2_rb.full.parquet", [changed, b], "rb.overrides", ["ra.overrides"])
    with pytest.raises(DatasetError, match="changes row a1"):
        ds._overrides_chain(tmp_path, "rb.overrides")
    (d / "1_ra.full.parquet").unlink()
    with pytest.raises(DatasetError, match="not local"):
        ds._overrides_chain(tmp_path, "rb.overrides")


@pytest.fixture(scope="module")
def policy() -> ds.OverridesPolicy:
    return ds.OverridesPolicy(DATASET_DIR / "overrides-public.yaml")


@pytest.mark.parametrize("note,kind,want", [
    ("ANTIDOTE (2009): robust-statistics defense against poisoning (ms-lo-antidote-2009; D664 step 2)",
     "scope_decision", "ANTIDOTE (2009): robust-statistics defense against poisoning."),
    ("Corrigibility (Soares, Fallenstein, Yudkowsky, Armstrong; AAAI-15 workshop): manual PDF ms-r2-lm-9 + S2 record",
     "identity_merge", "Corrigibility (Soares, Fallenstein, Yudkowsky, Armstrong; AAAI-15 workshop)."),
    ("MIRI announcement post 2013-03-22 (store document s1wdgdx71424g, channel miri_blog), flagged for collect.links.",
     "manual_source", "A source added by hand; the papers its page links to are collected."),
    ("Building safe artificial intelligence: specification, robustness, and assurance (2018-09-27). Ortega, Maini.",
     "manual_source", "Building safe artificial intelligence: specification, robustness, and assurance (2018-09-27)."),
    ("Batch 7: AI annotation standing in for gpt-6-luna's failed answer", "stand_in_annotation",
     "Annotation by claude-opus-5-5 standing in for a failed production annotation; quotes verified against the "
     "stored text."),
])
def test_public_notes(policy: ds.OverridesPolicy, note: str, kind: str, want: str) -> None:
    o = _ov("x", "screen", "field", note=note, author="ai:claude-opus-5-5")
    got = policy.note(o, kind)
    assert got == want
    assert not policy.marked(got)


def test_public_author_kind(policy: ds.OverridesPolicy) -> None:
    o = _ov("x", "technique_staged", "row", tier="ai_check", author="ai:claude-sonnet-5-5")
    assert policy.kind(o) == "stand_in_annotation"
    assert policy.author(o)[0] == "claude-sonnet-5-5"
    assert policy.author(_ov("y", "screen", "field", author="tomas")) == ("maintainers",
                                                                          "AI-prepared list, approved by gavento (2026-10-21)")
    with pytest.raises(DatasetError, match="no public author"):
        policy.author(_ov("z", "screen", "field", author="someone-new"))


def test_active_overrides_leave_out_superseded_revoked_and_stale(m: Mapping_, policy: ds.OverridesPolicy) -> None:
    """config/overrides.jsonl (D725): the effective rule over the chain (a revoke retires its row and is not a value;
    the later row of a group wins), minus a <wf> row the document's build record does not use; each line names its
    run."""
    later = AT + dt.timedelta(hours=1)
    a1 = _ov("a1", "screen", "field", field="verdict", value="irrelevant")
    a2 = dataclasses_replace(_ov("a2", "screen", "field", field="verdict", value="relevant"), created_at=later)
    b1 = _ov("b1", "screen", "field", field="off_topic_reason", value="x")
    r1 = Override(override_id="r1", target="screen", key="", item_key="", kind="revoke", field="", payload={},
                  tier="human", created_at=later, author="tomas", revokes="b1")
    st = _ov("st", "screen", "field", field="verdict", value="relevant", key="sdocstale0001")
    out = _ov("out", "screen", "field", field="verdict", value="irrelevant", key="sdocnotrun001")
    inp = _inputs([a1, a2, b1, r1, st, out], families={"screen": {"sdoc000000001": (["a2"], False),
                                                                  "sdocstale0001": ([], False)}})
    inp.chain = [(Path("p1"), {"run_id": "rone"}, [a1, b1, st, out]), (Path("p2"), {"run_id": "rtwo"}, [a2, r1])]
    act = ds.active_overrides(inp)
    assert [(o.override_id, run) for o, run in act.rows] == [("out", "rone"), ("a2", "rtwo")]
    assert (act.chain_rows, act.revoke_rows, act.revoked, act.superseded, act.stale) == (6, 1, ["b1"], ["a1"], ["st"])
    lines = ds.export_overrides(_proj(m, inp), policy, act)
    assert [list(r) for r in lines] == [list(ds.OVERRIDES_FIELDS)] * 2
    assert lines[1]["run_id"] == "rtwo" and lines[1]["value"] == "relevant" and lines[1]["kind"] == "scope_decision"
    dangling = Override(override_id="r2", target="screen", key="", item_key="", kind="revoke", field="", payload={},
                        tier="human", created_at=later, author="tomas", revokes="nothing")
    inp.chain = [(Path("p1"), {"run_id": "rone"}, [a1, dangling])]
    with pytest.raises(DatasetError, match="revokes the effective rule ignores"):
        ds.active_overrides(inp)


def dataclasses_replace(o: Override, **kw: Any) -> Override:
    import dataclasses

    return dataclasses.replace(o, **kw)


def test_stand_in_value_projected_and_cut(m: Mapping_) -> None:
    pay = _blank(REGISTRY["technique_staged"].payload_schema)
    pay["techniques"] = [{"slug": "rlhf", "roles": ROLES, "quotes": [{"text": "q " * 200, "verified": True}],
                          "flags": [], "note": None}]
    pay["repairs"] = ["internal"]
    o = _ov("impl-ts-s", "technique_staged", "row", value=pay, tier="ai_check", author="ai:claude-opus-5-5")
    value, cuts = ds.overrides_value(_proj(m, _inputs([o])), o)
    assert "repairs" not in value and value["techniques"][0]["quotes"][0]["anchored"] is True
    assert len(value["techniques"][0]["quotes"][0]["text"]) <= 300
    assert cuts == [{"path": "value.techniques[0].quotes[0].text", "chars": 400}]


# ---------------------------------------------------------------- organisations of the affiliations
LONG = "Lab of Things, " * 30 + "Stanford University"  # the organisation lies past the public cap
VOCAB = "taxonomy/orgs-0.2.2.yaml"


def _author(i: int, *surfaces: str) -> dict[str, Any]:
    return {"item_key": str(i), "author_name": f"a{i}", "author_surface": f"A{i}", "byline_index": i, "orcid": None,
            "resolution": "text", "affiliations": [{"surface": s, "org_slugs": []} for s in surfaces]}


def _affiliation_row(orgs: bool = True, stale: bool = False) -> dict[str, Any]:
    """A row with an affiliation record and, with ``orgs``, the stage's affiliation_orgs record of it (computed by
    the stage's own function over the real vocabulary, its chained basis over the affiliation record)."""
    from stack.fold import chained_basis
    from stack.orgvocab import affiliation_orgs, load

    pay = {"items": [_author(0, "Google DeepMind, London, UK", "Independent researcher"),
                     _author(1, "Northeastern University", "Department of Computer Science"),
                     _author(2, "Apollo Research & OpenAI", LONG),
                     _author(3, "Institute of Automation", "Chinese Academy of Sciences"),
                     _author(4)]}
    row = _row(affiliation=_rec("affiliation", payload=pay))
    if orgs:
        v = load(REPO_ROOT / VOCAB)
        rec = _rec("affiliation_orgs", origin="computed", evidence=None,
                   payload={**affiliation_orgs(pay, v.matcher), "vocab_version": v.version})
        rec["basis_kind"] = "chained"
        rec["basis_digest"] = "0" * 64 if stale else chained_basis(TEXT, row["annotations"], ("affiliation",))
        row["annotations"]["affiliation_orgs"] = rec
    return row


WANT = [[("matched", ["deepmind"]), ("non_org", [])],
        [("ambiguous", []), ("generic", [])],  # bare Northeastern: Boston or Shenyang, no default
        [("matched", ["apollo", "openai"]), ("matched", ["stanford"])],
        # a sub-unit's local name resolved by the same author's other affiliation
        [("matched", ["institute-of-automation"]), ("matched", ["chinese-academy-of-sciences"])],
        []]


def test_affiliation_org_fields_come_from_the_stored_record(m: Mapping_) -> None:
    for variant in ("public", "internal"):
        p = _proj(m, _inputs([]), variant)
        ann = p.annotations(_affiliation_row())
        p.cap_and_scrub(ann, {"cuts": [], "quote_chars": 0})
        assert not p.problems, p.problems
        pay = ann["affiliation"]["payload"]
        got = [[(a["org_status"], a["org_ids"]) for a in it["affiliations"]] for it in pay["items"]]
        assert got == WANT, variant
        long_surface = pay["items"][2]["affiliations"][1]["surface"]
        assert (long_surface == LONG) == (variant == "internal")  # cut in public; the stage read the whole surface
        assert dict(p.org_counts) == {"matched": 5, "non_org": 1, "ambiguous": 1, "generic": 1, "with_org_ids": 5}
        pa.array([pay], type=m.kinds["affiliation"].ptype)  # the projected payload has the release type
        orgs = ann["affiliation_orgs"]
        md = orgs["metadata"]
        assert md["config_id"] == f"affiliation_orgs:orgs-0.2.2:rule:{D['affiliation_orgs'][:8]}"
        assert (md["annotator"], md["origin"], md["provenance"]) == ("rule", "computed", "pipeline")
        assert orgs["payload"]["org_ids"][:2] == ["deepmind", "apollo"]
        assert orgs["payload"]["vocab_version"] == "0.2.2"
        assert set(orgs["payload"]) == {"org_ids", "vocab_version"}  # the per-item part is inline; no roots (D737)
        cands = [[a["org_candidates"] for a in it["affiliations"]] for it in pay["items"]]
        assert cands[1] == [[["northeastern-university", "northeastern-university-us"]], []]
        assert all(c == [] for row in cands[:1] + cands[2:] for c in row)
        cfg = p.configs[md["config_id"]]
        assert cfg.model is None and cfg.taxonomy["name"] == "orgs" and cfg.taxonomy["path"] == "taxonomies/orgs-0.2.2.yaml"
        assert json.loads(cfg.components)["method"].startswith("deterministic organisation matcher")
        assert json.loads(cfg.parameters)["role"] is None  # no LLM role for a computed workflow


def test_affiliation_org_fields_without_a_record_are_null(m: Mapping_) -> None:
    p = _proj(m, _inputs([]))
    ann = p.annotations(_affiliation_row(orgs=False))
    assert ann["affiliation_orgs"] is None and not p.problems
    affs = [a for it in ann["affiliation"]["payload"]["items"] for a in it["affiliations"]]
    assert affs and all(a["org_ids"] is None and a["org_status"] is None for a in affs)
    assert dict(p.org_counts) == {"no_record": 8, "with_org_ids": 0}
    assert ds.orgs_record(p)["by_status"]["no_record"] == 8


def test_a_skipped_no_basis_org_record_is_not_run(m: Mapping_) -> None:
    """The stage writes ``skipped`` (no_basis) where affiliation is not decided; the release ships no record."""
    row = _affiliation_row()
    rec = row["annotations"]["affiliation_orgs"]
    rec.update(status="skipped", error_class="no_basis", basis_digest=None, is_basis_current=None,
               payload=_blank(REGISTRY["affiliation_orgs"].payload_schema))
    p = _proj(m, _inputs([]))
    ann = p.annotations(row)
    assert ann["affiliation_orgs"] is None and not p.problems, p.problems
    assert p.counts["affiliation_orgs"]["not_run"] == 1
    assert all(a["org_status"] is None for it in ann["affiliation"]["payload"]["items"] for a in it["affiliations"])
    rec["error_class"] = "other"  # any other skipped record still meets the basis gate
    p = _proj(m, _inputs([]))
    p.annotations(row)
    assert any("affiliation_orgs: basis gate" in x for x in p.problems)


def test_affiliation_org_record_stale_or_misaligned_is_a_problem(m: Mapping_) -> None:
    p = _proj(m, _inputs([]))
    p.annotations(_affiliation_row(stale=True))
    assert any("affiliation_orgs: basis gate (basis_kind chained" in x for x in p.problems), p.problems
    row = _affiliation_row()
    row["annotations"]["affiliation_orgs"]["payload"]["items"][0]["affiliations"].pop()
    p = _proj(m, _inputs([]))
    p.annotations(row)
    assert any("affiliation_orgs item '0': 1 answers for 2 affiliations" in x for x in p.problems), p.problems


def test_affiliation_org_fields_of_a_failed_record_and_their_type(m: Mapping_) -> None:
    row = _row(affiliation=_rec("affiliation", status="failed_permanent", payload={"items": None}, error_class="x"))
    pay = _proj(m, _inputs([])).annotations(row)["affiliation"]["payload"]
    assert pay["items"] is None
    t = m.kinds["affiliation"].ptype
    aff = t.field("items").type.value_type.field("affiliations").type.value_type
    assert [(f.name, f.nullable) for f in aff][-3:] == [("org_ids", True), ("org_status", True),
                                                        ("org_candidates", True)]
    assert ds.with_orgs(None, None) == (None, None)


def test_mapping_org_fields_name_affiliation_and_affiliation_orgs(tmp_path: Path) -> None:
    def other_kind(raw: dict[str, Any]) -> None:
        raw["org_fields"]["kind"] = "screen"

    with pytest.raises(DatasetError, match="affiliation payload only"):
        Mapping_(_mutated(tmp_path, other_kind))

    def other_from(raw: dict[str, Any]) -> None:
        raw["org_fields"]["from"] = "model_roles"

    with pytest.raises(DatasetError, match="kind columns of affiliation and of affiliation_orgs"):
        Mapping_(_mutated(tmp_path, other_from))

    def bad_shape(raw: dict[str, Any]) -> None:
        raw["org_fields"] = {"kind": "affiliation"}

    with pytest.raises(DatasetError, match="org_fields must be"):
        Mapping_(_mutated(tmp_path, bad_shape))


def test_orgs_record_and_template_values(m: Mapping_) -> None:
    p = _proj(m, _inputs([]))
    p.annotations(_affiliation_row())
    rec = ds.orgs_record(p)
    assert rec == {"kind": "affiliation", "from": "affiliation_orgs", "affiliations": 8, "with_org_ids": 5,
                   "by_status": {"matched": 5, "ambiguous": 1, "non_org": 1, "generic": 1, "unmatched": 0,
                                 "no_record": 0}}
    doc = {"orgs": rec, "taxonomies": {"orgs": {"path": "taxonomies/orgs-0.1.0.yaml"}}}
    vals = ds._orgs_vals(doc)
    assert vals["orgs_file"] == "taxonomies/orgs-0.1.0.yaml" and "(62.5 % of 8 affiliations name at least one)" in vals["orgs_clause"]
    assert "5 (62.5 %)" in vals["orgs_coverage"] and "unmatched 0" in vals["orgs_coverage"]
    assert "no_record" not in vals["orgs_coverage"]  # shown only when some document lacks the record
    with pytest.raises(DatasetError, match="no org_fields"):
        ds._orgs_vals({"taxonomies": {}})


# ---------------------------------------------------------------- the vocabulary and the leak patterns
def test_vocab_covers_every_enum(m: Mapping_) -> None:
    import yaml

    v = yaml.safe_load((DATASET_DIR / "vocab.yaml").read_text("utf-8"))["fields"]
    for path, (vals, _open) in m.enums.items():
        assert path in v, path
        assert set(vals) <= {str(k) for k in v[path]["values"]}, path


def test_leak_patterns() -> None:
    import re

    pats = [re.compile(p) for p in ds.PATH_LEAKS + ds.STORE_LEAKS]
    hit = lambda s: any(p.search(s) for p in pats)  # noqa: E731
    home, priv = "/" + "ho" + "me/user/x", "mi" + "mir"  # built, so the repo carries no such literal
    assert hit(f"see {home}") and hit("s" + "3://b/x") and hit("path: ." + "local/x") and hit(f"in {priv} here")
    assert not hit("https://www.businesswire.com/news" + "/ho" + "me/2025") and not hit("screen buckets")


# ---------------------------------------------------------------- URL fields (D787)
EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/PMC0000001/fullTextXML"
OCI = "https://bucket.compat.objectstorage.us-ashburn-1.oraclecloud.com/files/abc"
SIGV4 = ("X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=AKIDEXAMPLE%2F20261101%2Fus-ashburn-1%2Fs3%2Faws4_request"
         "&X-Amz-Date=20261101T000000Z&X-Amz-Expires=60&X-Amz-SignedHeaders=host&X-Amz-Signature=deadbeef")


@pytest.mark.parametrize("url,want", [
    # contact parameters, encoded or not, alone or among others
    (f"{EPMC}?email=someone%40example.org", EPMC),
    (f"{EPMC}?email=someone@example.org", EPMC),
    ("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=pmc&id=1&retmode=xml&tool=t&email=someone%40example.org",
     "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=pmc&id=1&retmode=xml&tool=t"),
    ("https://api.example.org/works?filter=x&mailto=someone%40example.org&page=2",
     "https://api.example.org/works?filter=x&page=2"),
    (f"{EPMC}?EMAIL=someone%40example.org", EPMC),
    (f"{EPMC}?%65mail=someone%40example.org", EPMC),
    # AWS SigV4 (any case); the response override it signed is kept
    (f"{OCI}?response-content-disposition=attachment%3B%20filename%3Da.pdf&{SIGV4}",
     f"{OCI}?response-content-disposition=attachment%3B%20filename%3Da.pdf"),
    (f"{OCI}?{SIGV4.lower()}", OCI),
    (f"{OCI}?X-Amz-Security-Token=tok&X-Amz-Credential=c&X-Amz-Signature=s&v=2", f"{OCI}?v=2"),
    # GCS V4
    ("https://storage.example.org/b/o?X-Goog-Algorithm=GOOG4-RSA-SHA256&X-Goog-Credential=c&X-Goog-Date=d"
     "&X-Goog-Expires=60&X-Goog-SignedHeaders=host&X-Goog-Signature=s", "https://storage.example.org/b/o"),
    # CloudFront (Expires and Policy only beside Signature) and the legacy S3 form
    ("https://cdn.example.org/a.pdf?response-content-type=application%2Fpdf&Expires=1761955200&Signature=abc~&"
     "Key-Pair-Id=K2EXAMPLE", "https://cdn.example.org/a.pdf?response-content-type=application%2Fpdf"),
    ("https://cdn.example.org/a.pdf?Policy=eyJ&Signature=abc&Key-Pair-Id=K", "https://cdn.example.org/a.pdf"),
    ("https://b.example.org/k?AWSAccessKeyId=AKID&Expires=1&Signature=x%2By", "https://b.example.org/k"),
    # Azure SAS (only beside sig and sv)
    ("https://acct.blob.core.windows.net/c/a.pdf?sp=r&st=2026-11-01&se=2026-11-02&spr=https&sv=2022-11-02&sr=b"
     "&sig=abc%3D", "https://acct.blob.core.windows.net/c/a.pdf"),
    # fragment kept; a dropped last parameter takes its '&' along
    (f"{EPMC}?a=1&email=x#sec2", f"{EPMC}?a=1#sec2"),
    (f"{EPMC}?email=x&", EPMC),
])
def test_scrub_url_drops_contact_and_signature_parameters(url: str, want: str) -> None:
    assert ds.scrub_url(url) == want
    assert ds.scrub_url(want) == want


@pytest.mark.parametrize("url", [
    "https://arxiv.org/abs/2401.00001",
    "https://arxiv.org/pdf/2401.00001v2",
    "https://www.lesswrong.com/posts/abc/title?commentId=x#c1",
    "https://example.org/search?q=email%3Dx&page=2",          # 'email=' inside a value, not a parameter name
    "https://example.org/a?Expires=1&Policy=p",               # CloudFront names without a Signature
    "https://example.org/a?se=2026&sp=r&sv=1",                # SAS-like names without sig
    "https://example.org/a?sig=1",                            # sig without sv
    "https://example.org/a?x-amz-date=1&x-amz-expires=2",     # x-amz-* without a signature or credential
    "https://example.org/a?signed=1&key=v&token_type=a&utm_source=b",
    "https://example.org/a?",
    "https://example.org/a?b=%2F%20+x&&c",                    # odd encodings and empty pieces kept as written
])
def test_scrub_url_keeps_everything_else(url: str) -> None:
    assert ds.scrub_url(url) is url


def test_url_fields_are_the_rows_url_strings(m: Mapping_) -> None:
    """Every string leaf of the row whose name ends in ``url`` is scrubbed; a URL key is checked."""
    def leaves(t: pa.DataType, prefix: str) -> list[tuple[str, pa.DataType]]:
        out = []
        for f in t:
            p = f"{prefix}{f.name}"
            ft = f.type.value_type if pa.types.is_list(f.type) else f.type
            p += "[]" if pa.types.is_list(f.type) else ""
            out += leaves(ft, p + ".") if pa.types.is_struct(ft) else [(p, ft)]
        return out
    urls = {p for p, t in leaves(pa.struct(list(m.schema)), "") if p.endswith("url") and t == pa.string()}
    assert urls == set(ds.URL_FIELDS)
    assert set(ds.URL_CHECKED) == urls | {"identity.members[].key_value"}


def test_row_scrubs_its_url_fields(m: Mapping_) -> None:
    p = _proj(m, _inputs([]))
    mail, kept = f"{EPMC}?email=someone%40example.org", "https://example.org/k?email=x"
    projected = {"canonical_url": "https://example.org/landing", "content": {"source": {"url": mail}},
                 "identity": {"members": [{"canonical_url": f"{OCI}?{SIGV4}", "key_kind": "url", "key_value": kept},
                                          {"canonical_url": None, "key_kind": "doi", "key_value": "10.1/x"}]}}
    p._project = lambda n, obj, ctx: copy.deepcopy(projected)  # type: ignore[method-assign]
    out = p.row(_row())
    assert out["canonical_url"] == "https://example.org/landing"
    assert out["content"]["source"]["url"] == EPMC
    assert out["identity"]["members"][0] == {"canonical_url": OCI, "key_kind": "url", "key_value": kept}
    assert p.urls_scrubbed == 2


def test_url_sql_reads_every_checked_field(tmp_path: Path) -> None:
    """``check_bundle``'s URL check: each :data:`URL_CHECKED` query reads its field (a key only of kind url)."""
    import duckdb

    bad = f"{EPMC}?email=x"
    rows = [{"canonical_url": bad, "content": {"source": {"url": bad}},
             "identity": {"members": [{"canonical_url": bad, "key_kind": "url", "key_value": bad},
                                      {"canonical_url": None, "key_kind": "doi", "key_value": bad}]}}]
    pq.write_table(pa.Table.from_pylist(rows), tmp_path / "c.parquet")
    con = duckdb.connect()
    con.execute(f"CREATE VIEW d AS SELECT * FROM read_parquet('{tmp_path / 'c.parquet'}')")
    got = {path: [v for (v,) in con.execute(ds._url_sql(path)).fetchall()] for path in ds.URL_CHECKED}
    assert got == {path: [bad] for path in ds.URL_CHECKED}


def test_leak_allowance_is_the_exact_organisation_phrase() -> None:
    priv = "Mi" + "mir"  # built, as above
    name = f"{priv} Center for Long Term Futures Research"
    slug = "-".join(name.lower().split())
    assert ds.leak_problems("taxonomies/orgs-0.1.0.yaml", f"- id: {slug}\n  name: {name}\n") == []
    assert ds.leak_problems("README.md", f"{name} and {priv.lower()} here")  # the bare word still trips it
    assert ds.leak_problems("README.md", f"{slug[:12]}-x")  # so does a partial id
    assert ds.leak_problems("README.md", "s" + "3://b/x") and not ds.leak_problems("taxonomies/x.yaml", "s" + "3://b/x")


def test_row_json_schema_enums(m: Mapping_) -> None:
    js = ds.json_schema(pa.struct(list(m.schema)), False, None, m.enums)
    meta = js["properties"]["annotations"]["properties"]["screen"]["properties"]["metadata"]["properties"]
    assert "bogus" not in meta["status"]["enum"] and "ok" in meta["status"]["enum"]
    assert js["properties"]["published_precision"]["enum"] == ["day", "year_inferred", "unknown"]


def test_inline_text_body(m: Mapping_) -> None:
    text = "# A document\n\nbody text"
    good = hashlib.sha256(text.encode("utf-8")).hexdigest()
    row = {"doc_id": "sdoc000000001", "content": {"current_text_sha256": good}}
    p = _proj(m, _inputs([]), "internal")
    p.text_get = lambda d: text.encode("utf-8")
    assert p.text_body(row) == text
    assert _proj(m, _inputs([]), "public").text_body(row) is None  # public: NULL, same schema
    p.text_get = lambda d: b"other bytes"
    with pytest.raises(DatasetError, match="do not hash"):
        p.text_body(row)
    body = m.schema.field("content").type.field("text").type.field("body")
    assert body.type == pa.string() and body.nullable


# ---------------------------------------------------------------- review round: schemas, vocab, views
def test_payload_and_record_schemas_validate_their_own_targets(m: Mapping_, tmp_path: Path) -> None:
    """schema.json (D725): self-contained; a kind's record and payload subschemas validate their own targets only."""
    import jsonschema

    ds.write_row_schema(tmp_path, m)
    row_js = json.loads((tmp_path / "schema.json").read_text())
    assert row_js["$id"] == "schema.json" and "$ref" not in json.dumps(row_js)
    jsonschema.Draft202012Validator.check_schema(row_js)
    ann = _proj(m, _inputs([])).annotations(_row())
    for kind in ("screen", "safety_techniques", "model_roles", "model_name_matches"):
        rec = json.loads(json.dumps(ann[kind], default=ds._json_default))
        rs_js, ps_js = ds.kind_schemas(row_js, kind)
        ps, rs = jsonschema.Draft202012Validator(ps_js), jsonschema.Draft202012Validator(rs_js)
        assert ps.is_valid(rec["payload"]) and rs.is_valid(rec), kind
        assert not ps.is_valid(rec) and not rs.is_valid(rec["payload"]), kind


def test_shipped_vocab_keeps_only_occurring_values() -> None:
    vocab = {"fields": {"a": {"doc": "d", "values": {"x": "1", "y": "2"}}},
             "patterns": {"f": {"doc": "d", "values": {"menu_gap:<surface>": "g", "no_techniques_found": "n"}}}}
    out = ds.shipped_vocab(vocab, {"a": {"x"}, "f": {"menu_gap:red teaming"}})
    assert out["fields"]["a"] == {"doc": "d", "values": {"x": "1"}, "reserved": ["y"]}
    assert list(out["patterns"]["f"]["values"]) == ["menu_gap:<surface>"]
    assert out["patterns"]["f"]["reserved"] == ["no_techniques_found"]
    assert vocab["fields"]["a"]["values"] == {"x": "1", "y": "2"}  # the source is not changed


def test_values_sql_paths(m: Mapping_) -> None:
    assert ds.values_sql(m.schema, "doc_type") == "SELECT DISTINCT CAST(doc_type AS VARCHAR) FROM d"
    sql = ds.values_sql(m.schema, "identity.members[].channel_class")
    assert sql == ("SELECT DISTINCT CAST(x2.channel_class AS VARCHAR) FROM d, unnest(identity.members) AS u1(x1)"
                   .replace("x2", "x1"))
    assert "unnest(annotations.screen.payload.buckets)" in str(ds.values_sql(m.schema, "annotations.screen.payload.buckets"))
    assert ds.values_sql(m.schema, "identity.nothing") is None


def test_template_placeholders_must_be_filled() -> None:
    assert ds._fill("x {{a}} y", {"a": 1}) == "x 1 y"
    with pytest.raises(DatasetError, match="no value"):
        ds._fill("{{missing}}", {})


def test_overrides_summary_counts_by_kind() -> None:
    rows = [{"kind": "manual_source", "key": f"url=u{i}"} for i in range(3)] + [{"kind": "identity_merge", "key": "[]"}]
    s = ds.overrides_summary(rows)
    assert s.startswith("Of the 4 override rows applied to this release, 3 add a source by hand and 1 merge two records")
    s = ds.overrides_summary([{"kind": "scope_decision", "key": k} for k in ("a", "a", "b")])
    assert s.startswith("Of the 3 override rows applied to this release, 3 set screen or relevance fields (of 2 documents).")
    assert ds.overrides_summary([]) == "No overrides were applied to this release."
    assert ds.CONTACT["takedown"] == "mailto:info@thesafetystack.ai"


def test_overrides_chain_pulls_into_a_fresh_mirror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A container starts from an empty mirror of the S3 store (docs/DEPLOY-railway.md): the export's overrides chain
    is downloaded on read, as every other input, not taken only from files already local."""
    import textwrap

    from stack.overrides import run_add
    from stack.store.index import store_index
    from stack.store.remote import resolve_store, using_s3_client
    from tests.fakes.s3 import FAKE_ENV, FakeS3

    for k, v in FAKE_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("STACK_ENV_FILE", raising=False)
    a, b = tmp_path / "identity.yaml", tmp_path / "fields.yaml"
    a.write_text(textwrap.dedent("- {override_id: k1, value: mask, key: {source_id: sa}}\n"))
    b.write_text(textwrap.dedent("- {override_id: f1, doc_id: sa, field: title, value: T}\n"))
    with using_s3_client(FakeS3()):
        writer = resolve_store("s3://stack-data/stack-v4", mirror_base=tmp_path / "mirror-a")
        assert run_add(writer, a, "data/overrides/main/").status == "ok"
        head = run_add(writer, b, "data/overrides/main/").outputs["overrides"]["file_id"]
        fresh = resolve_store("s3://stack-data/stack-v4", mirror_base=tmp_path / "mirror-b")
        store_index(fresh, refresh=True)
        assert not list((fresh / "data").glob("overrides/*/*.parquet"))
        chain = ds._overrides_chain(fresh, head)
    assert [[o.override_id for o in rows] for _p, _h, rows in chain] == [["k1"], ["f1"]]


def test_git_commit_falls_back_to_the_image_build_arg(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_git(*_a: Any, **_k: Any) -> None:
        raise FileNotFoundError("git")

    monkeypatch.setattr(ds.subprocess, "run", no_git)
    monkeypatch.setenv("STACK_CODE_COMMIT", "0123456789abcdef0123456789abcdef01234567")
    assert ds._git_commit() == "0123456789ab+image"
    monkeypatch.setenv("STACK_CODE_COMMIT", "not a sha")
    assert ds._git_commit() == "unknown"


def test_prefetch_texts_fills_a_fresh_mirror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from stack.store.blobs import open_blobs
    from stack.store.remote import resolve_store, using_s3_client
    from tests.fakes.s3 import FAKE_ENV, FakeS3

    for k, v in FAKE_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("STACK_ENV_FILE", raising=False)
    with using_s3_client(FakeS3()) as fake:
        writer = resolve_store("s3://stack-data/stack-v4", mirror_base=tmp_path / "mirror-a")
        ds_ = [open_blobs(writer).put(f"text {i}".encode()) for i in range(40)]
        fresh = resolve_store("s3://stack-data/stack-v4", mirror_base=tmp_path / "mirror-b")
        assert ds.prefetch_texts(fresh, [*ds_, None, ds_[0]]) == 40
        assert all(open_blobs(fresh).local(d).read_bytes() == f"text {i}".encode() for i, d in enumerate(ds_))
        gets = len(fake.ops("get"))
        assert ds.prefetch_texts(fresh, ds_) == 0 and len(fake.ops("get")) == gets
    assert ds.prefetch_texts(tmp_path / "local-store", ds_) == 0


# ---------------------------------------------------------------- the slim bundle (D725); the card's quality table
JUDGE_EVAL = "evals/judge-2026-10-25j"
BUNDLE = {"README.md", "LICENSE", "FORMAT.md", "VOCABULARY.md", "schema.json", "release.json", "data/corpus.parquet",
          "config/configs.json", "config/overrides.jsonl"}
DROPPED = ("data/configs.parquet", "examples/", "prompts/", "schema/", "QUALITY.md", "CHANGELOG.md", "SHA256SUMS",
           "annotations.yaml", "remap.jsonl", "vocab.yaml", "VOCAB.md", "overrides/", "views/")


def test_configs_name_the_judge_eval_and_refuse_obsolete_keys() -> None:
    for name in ("dataset-public", "dataset-internal"):
        cfg = ds.load_dataset_config(name)
        assert cfg.judge_eval == JUDGE_EVAL and (REPO_ROOT / cfg.judge_eval).is_dir()
    raw = {"name": "x", "format": "dataset", "variant": "public", "hf_repo": "a/b", "judge_eval": JUDGE_EVAL}
    ds.DatasetConfig.model_validate(raw)
    for key, val in (("views", True), ("prompts", True), ("examples", ["s1"])):  # refused, not ignored
        with pytest.raises(ValueError, match=f"config key '{key}' is obsolete"):
            ds.DatasetConfig.model_validate({**raw, key: val})
    with pytest.raises(ValueError, match="bogus"):
        ds.DatasetConfig.model_validate({**raw, "bogus": 1})
    with pytest.raises(ValueError, match="judge_eval"):
        ds.DatasetConfig.model_validate({k: v for k, v in raw.items() if k != "judge_eval"})


def test_card_config_is_the_corpus_only() -> None:
    assert ds.HF_CONFIGS == ({"config_name": "corpus", "data_files": "data/corpus.parquet", "default": True},)


def test_mapping_files_are_the_slim_bundle() -> None:
    files = yaml_load(DATASET_DIR / "mapping.yaml")["files"]
    assert set(files) == BUNDLE | {"taxonomies/"}
    taxa = {"taxonomies/techniques-1.1.0.yaml", "taxonomies/models-1.1.0.yaml"}
    assert ds.bundle_files_problems(BUNDLE | taxa, files) == []
    assert ds.bundle_files_problems(BUNDLE | taxa | {"SHA256SUMS", "prompts/a.md"}, files) == [
        "files written that the mapping does not list: ['SHA256SUMS', 'prompts/a.md']"]
    assert ds.bundle_files_problems(BUNDLE - {"schema.json"} | taxa, files) == [
        "files of the mapping not written: ['schema.json']"]
    assert ds.bundle_files_problems(BUNDLE, files) == ["no file under taxonomies/"]


def test_no_template_names_a_dropped_file() -> None:
    texts = {p.name: p.read_text("utf-8") for p in DATASET_DIR.iterdir() if p.is_file() and p.name != "vocab.yaml"}
    assert not {"QUALITY.md", "CHANGELOG.md"} & set(texts)  # the templates of dropped files are gone
    hits = [(n, d) for n, t in texts.items() for d in DROPPED if re.search(r"(?<![\w/.])" + re.escape(d), t)]
    assert not hits
    assert "views" not in yaml_load(DATASET_DIR / "mapping.yaml")


def yaml_load(p: Path) -> Any:
    import yaml

    return yaml.safe_load(p.read_text("utf-8"))


def test_records_recipe_runs_on_the_release_schema(m: Mapping_) -> None:
    import duckdb

    con = duckdb.connect()
    con.register("d", pa.Table.from_pylist([], schema=m.schema))
    con.execute(ds.records_recipe_sql(list(m.kinds)))
    cols = [r[0] for r in con.execute("DESCRIBE records").fetchall()]
    assert cols == ["doc_id", *m.meta_type.names]
    assert con.execute("SELECT kind, status, count(*) FROM records GROUP BY ALL").fetchall() == []


def _scores(rel: str) -> Any:
    return json.loads((REPO_ROOT / JUDGE_EVAL / rel / "scores.json").read_text("utf-8"))


def _table(section: str) -> dict[tuple[str, str], list[str]]:
    """The quality table's rows: (judgement prefix, reference) -> [n, P, R, F1]."""
    out = {}
    for line in section.splitlines():
        if line.startswith("| ") and not line.startswith(("| Judgement", "|---")):
            c = [x.strip() for x in line.strip("|").split(" | ")]
            out[(c[0].split(":")[0], c[1])] = c[2:]
    return out


def _cell(e: dict[str, Any], ci: dict[str, Any], k: str, ck: str | None = None) -> str:
    lo, hi = ci[ck or k]
    return f"{e[k]:.2f} [{lo:.2f}, {hi:.2f}]"


def _rescored(build: str = "ws1qpn8myzxah", dropped: dict[str, dict[str, int]] | None = None) -> dict[str, Any]:
    """A re-scoring result (:func:`stack.evals.release_quality.rescore`) carrying the committed scores: re-scoring
    build ws1qpn8myzxah reproduces them (tests/test_release_quality.py)."""
    cov = {s: {"items": 200 if s == "triage_w" else 100, "scored": 200 if s == "triage_w" else 100, "dropped": {}}
           for s in ("triage", *ds.JUDGE_SETS)}
    for s, d in (dropped or {}).items():
        cov[s] = {"items": cov[s]["items"], "scored": cov[s]["items"] - sum(d.values()), "dropped": d}
    return {"judge_eval": JUDGE_EVAL, "judge_build": "ws1qpn8myzxah", "build": {"build": build, "documents": "d"},
            "sets": {s: _scores(s) for s in ds.JUDGE_SETS}, "coverage": cov,
            "files": [{"path": f"{JUDGE_EVAL}/screen/items.jsonl", "sha256": "0" * 64}]}


def test_judge_eval_table_is_the_scores() -> None:
    j = ds.judge_eval(JUDGE_EVAL, "ws1qpn8myzxah", _rescored())
    t = _table(j.section)
    maj = "majority of the 3 judges"
    assert list(t) == [("Triage", maj), ("Screen", maj), ("Affiliations", maj), ("Models", maj),
                       ("Techniques", "Claude Opus 5.5"), ("Techniques", "GPT Sol 6.1")]
    w = _scores("triage_w")["estimates"]["triage_w"]["majority"]
    assert t[("Triage", maj)] == [f"{w['n_scored']} of {w['n_items']} records, weighted",
                                  *[_cell(w["point"], w["ci95"], f"fetch_{k}") for k in ("precision", "recall", "f1")]]
    sk = _scores("screen")["scores"][ds.SCREEN_KEEP]["production_vs_majority"]
    assert t[("Screen", maj)] == [f"{sk['n']} documents", *[_cell(sk["per_class"]["keep"], sk["per_class_ci95"]["keep"], k)
                                                            for k in ("precision", "recall", "f1")]]
    for s, name, ref, refname in (("affiliation", "Affiliations", "majority", maj), ("model_roles", "Models", "majority", maj),
                                  ("technique_staged", "Techniques", "opus", "Claude Opus 5.5"),
                                  ("technique_staged", "Techniques", "sol", "GPT Sol 6.1")):
        e = _scores(s)["scores"][ds.SET_UNITS[s]][f"production_vs_{ref}"]
        assert t[(name, refname)] == [f"{e['n_items']} documents", *[_cell(e, e["ci95"], k) for k in
                                                                     ("precision", "recall", "f1")]], (s, ref)
    sec = " ".join(j.section.split())  # prose is wrapped
    mbs = _scores("triage_w")["missed_by_stratum"]
    for k in ("c<=0.94", "c0.95-0.98", "c0.99"):
        hi = mbs[f"majority|triage_w|not_relevant|{k}"]["missed_relevant_wilson95"][1]
        assert f"{round(hi):,} (confidence " in sec, k
    assert "of the 135,315 eligible records triage marked not relevant" in sec and "the estimate is 0;" in sec
    opus_sol = _scores("technique_staged")["scores"]["slug presence"]["opus_vs_sol"]["f1"]
    assert "not human annotators" in sec and f"between Opus and Sol: {opus_sol:.2f})" in sec
    assert "`evals/judge-2026-10-25j/` of the pipeline repository" in sec and "http" not in sec
    assert "who labelled samples of this release's build `ws1qpn8myzxah` blind" in sec
    assert "Every one of the 600 sampled documents is scored." in sec
    other = " ".join(ds.judge_eval(JUDGE_EVAL, "other", _rescored("other")).section.split())
    assert "labelled samples of build `ws1qpn8myzxah` blind" in other and "this release's build `other`" in other
    r = j.record
    assert r["path"] == JUDGE_EVAL and r["build"] == "ws1qpn8myzxah" and r["scored_build"] == "ws1qpn8myzxah"
    assert r["coverage"] == _rescored()["coverage"] and r["files"] == _rescored()["files"]
    assert [x["n"] for x in r["table"]] == [v[0] for v in t.values()]
    assert r["missed_relevant"]["not_relevant_records"] == 135315
    assert not any(re.search(p, j.section) for p in ds.PATH_LEAKS + ds.STORE_LEAKS)


def test_judge_eval_names_the_documents_it_could_not_score() -> None:
    j = ds.judge_eval(JUDGE_EVAL, "b2", _rescored("b2", {"screen": {"missing": 2, "screen_origin_human": 1},
                                                         "triage_w": {"input_changed": 4}}))
    sec = " ".join(j.section.split())
    assert ("7 of the 600 sampled documents are not scored (4 its input changed since the judges read it; "
            "2 no longer in the build; 1 no longer in the judged population).") in sec


def test_judge_eval_follows_an_edited_score() -> None:
    r = _rescored()
    r["sets"]["technique_staged"]["scores"]["slug presence"]["production_vs_sol"].update({"f1": 0.123})
    assert _table(ds.judge_eval(JUDGE_EVAL, "ws1qpn8myzxah", r).section)[("Techniques", "GPT Sol 6.1")][3].startswith(
        "0.12 [")
    r["sets"]["triage_w"]["missed_by_stratum"]["majority|triage_w|not_relevant|c0.99"].update({"relevant": 1})
    assert "the estimate is" not in " ".join(ds.judge_eval(JUDGE_EVAL, "ws1qpn8myzxah", r).section.split())


def test_judge_eval_refuses_a_missing_key_interval_another_build_or_set() -> None:
    r = _rescored("b")
    r["sets"]["screen"]["scores"][ds.SCREEN_KEEP]["production_vs_majority"].pop("per_class_ci95")
    with pytest.raises(DatasetError, match=r"screen \(re-scored on b\) has no .* > per_class_ci95"):
        ds.judge_eval(JUDGE_EVAL, "b", r)
    r = _rescored("b")
    r["sets"]["triage_w"]["missed_by_stratum"]["majority|triage_w|not_relevant|c0.99"].pop("missed_relevant_wilson95")
    with pytest.raises(DatasetError, match="missed_relevant_wilson95"):
        ds.judge_eval(JUDGE_EVAL, "b", r)
    with pytest.raises(DatasetError, match="re-scored on build 'b', the release is build 'c'"):
        ds.judge_eval(JUDGE_EVAL, "c", _rescored("b"))
    with pytest.raises(DatasetError, match="the config names 'evals/other'"):
        ds.judge_eval("evals/other", "b", _rescored("b"))
    r = _rescored("b")
    del r["coverage"]["model_roles"]
    with pytest.raises(DatasetError, match="no coverage for \\['model_roles'\\]"):
        ds.judge_eval(JUDGE_EVAL, "b", r)
    r = _rescored("b")
    del r["sets"]["affiliation"]
    with pytest.raises(DatasetError, match="affiliation \\(re-scored on b\\) has no scores"):
        ds.judge_eval(JUDGE_EVAL, "b", r)


def test_release_quality_refuses_a_build_without_its_documents(tmp_path: Path) -> None:
    with pytest.raises(DatasetError, match="corpus_input run"):
        ds.release_quality(tmp_path, {"build_id": "b", "files": {}}, pa.table({"doc_id": []}), JUDGE_EVAL)


def test_card_template_takes_the_quality_section() -> None:
    card = (DATASET_DIR / "README.card.md").read_text("utf-8")
    assert card.count("{{quality_section}}") == 1 and "## Annotation quality" in card
    assert "0.78" not in card and "0.92" not in card  # the reference-set (gold-v3) numbers left the release
    fmt = (DATASET_DIR / "FORMAT.head.md").read_text("utf-8")
    assert fmt.count("{{remap_sentence}}") == 1


def test_remap_sentence_and_runs_table(m: Mapping_) -> None:
    doc = {"remap": {"values": 12, "by_rule": {"producer_call_failed": 10, "override_empty": 2}}}
    s = ds.remap_sentence(m, doc)
    assert s.startswith("12 status values differ from the pipeline's (the status remap; release.json `remap`): ")
    assert "2 records get `status = empty`: " in s and "10 records get `error_class = producer_call_failed`: " in s
    assert s.endswith(".") and s.count(". ") == 0
    rows = [{"run_id": "r1", "kind": "manual_source", "target": "manual_sources", "author": "maintainers",
             "method": "m1"}] * 2 + [{"run_id": "r2", "kind": "stand_in_annotation", "target": "model_roles",
                                       "author": "claude-opus-5-5", "method": "m2"}]
    t = ds.runs_table(rows).splitlines()
    assert t[2] == "| `r1` | 2 | 2 manual_source (manual_sources) | maintainers | m1 |" and t[3].startswith("| `r2` | 1 |")


def test_vocabulary_glossary_note() -> None:
    out = ds.render_vocab({"glossary": {"stack": "the layers"}})
    i = out.index("**Glossary**")
    assert "Some of the terms used in the project" in out[i:] and "reasoning traces" in out[i:]
    assert out.index("- **stack**") > out.index("reasoning traces")


# ---------------------------------------------------------------- vocabularies by the pinned sha (D758)
def _sha(path: str) -> str:
    return hashlib.sha256((REPO_ROOT / path).read_bytes()).hexdigest()


def test_an_old_model_roles_config_exports_its_own_vocabulary(m: Mapping_, tmp_path: Path) -> None:
    """model_roles reads ``configs/models.yaml`` by name; a stored pin of models 1.0.0 resolves to the vendored
    ``taxonomy/models-1.0.0.yaml`` by its sha256 (it was refused while only today's file was looked at)."""
    inp = _inputs([])
    old = _pin("model_roles", "model-roles-v3b", ["openai/gpt-6-luna"], {"name": "model-roles-v3b",
                                                                         "role": "extract_main_openai"})
    old["prompt_pin"]["template_digests"] = ["t" * 64, _sha("taxonomy/models-1.0.0.yaml")]
    inp.pins[D["model_roles"]] = old
    p = _proj(m, inp)
    p.annotations(_row())
    (row,) = [c for c in p.configs.values() if c.stage == "annotate.model_roles"]
    assert row.taxonomy == {"name": "models", "version": "1.0.0", "sha256": _sha("taxonomy/models-1.0.0.yaml"),
                            "path": "taxonomies/models-1.0.0.yaml"}
    got = ds.write_taxonomies(tmp_path, p)
    assert (tmp_path / "taxonomies" / "models-1.0.0.yaml").read_bytes() == (
        REPO_ROOT / "taxonomy" / "models-1.0.0.yaml").read_bytes()
    assert got["models"] == [{"version": "1.0.0", "sha256": row.taxonomy["sha256"], "path": row.taxonomy["path"]}]
    inp.pins[D["model_roles"]]["prompt_pin"]["template_digests"] = ["t" * 64, "e" * 64]
    with pytest.raises(DatasetError, match="no file of .* hashes to a template digest the run pinned"):
        _proj(m, inp).annotations(_row())


def test_two_technique_configs_ship_both_taxonomy_versions(m: Mapping_, tmp_path: Path) -> None:
    """Rows of two technique_staged configs (1.0.0 accepted beside 1.1.0) ship one file per version, oldest first; the
    build's own config's version ships even when no row is under it; one name and version with two sha256 refuses."""
    old, new = "6" * 64, D["technique_staged"]
    inp = _inputs([])
    inp.pins[old] = _pin("technique_staged", "technique-staged-v5-a1.1", ["openai/gpt-6-luna"],
                         {"name": "technique-staged-v5-a1.1", "taxonomy": "taxonomy/techniques-1.0.0.yaml",
                          "role": "extract_staged_openai"})
    inp.pins[new] = _pin("technique_staged", "technique-staged-v5-a1.1", ["openai/gpt-6-luna"],
                         {"name": "technique-staged-v5-a1.1", "taxonomy": "taxonomy/techniques-1.1.0.yaml",
                          "role": "extract_staged_openai"})
    p = _proj(m, inp)
    p.annotations(_row("sdoc000000001", technique_staged=_rec("technique_staged", digest=old)))
    p.annotations(_row("sdoc000000002"))
    got = ds.write_taxonomies(tmp_path / "a", p)
    assert [t["version"] for t in got["techniques"]] == ["1.0.0", "1.1.0"]
    for t in got["techniques"]:
        assert hashlib.sha256((tmp_path / "a" / t["path"]).read_bytes()).hexdigest() == t["sha256"]
    assert ds.newest_taxonomy({"taxonomies": got}, "techniques") == got["techniques"][-1]
    # every row accepted from 1.0.0: the build's own 1.1.0 config still ships its file
    only_old = _proj(m, dataclasses_replace_inputs(inp, build_configs=[new]))
    only_old.annotations(_row("sdoc000000001", technique_staged=_rec("technique_staged", digest=old)))
    got = ds.write_taxonomies(tmp_path / "b", only_old)
    assert [t["version"] for t in got["techniques"]] == ["1.0.0", "1.1.0"]
    # two files claiming one name and version
    p.configs["x"] = copy.deepcopy(next(c for c in p.configs.values() if c.taxonomy and c.taxonomy["version"] == "1.0.0"))
    p.configs["x"].taxonomy = {**p.configs["x"].taxonomy, "sha256": "f" * 64}
    with pytest.raises(DatasetError, match="techniques 1.0.0 pinned with two sha256"):
        ds.write_taxonomies(tmp_path / "c", p)


def dataclasses_replace_inputs(inp: Inputs, **kw: Any) -> Inputs:
    import dataclasses

    return dataclasses.replace(inp, **kw)


def test_release_stages_record_and_sentence(m: Mapping_) -> None:
    """D758: ``release.json`` ``stages`` = per annotate stage of the build its run's selection (target, accepted list,
    selection digest), each config with its ``config_id`` and the shipped documents under it; FORMAT says one
    sentence per accepted config; a run recorded before D758 selects its own config only."""
    from types import SimpleNamespace

    from stack.plan import selection_digest

    old, new = "6" * 64, D["technique_staged"]
    entry = {"config": old, "name": "technique-staged-v5-a1.1@techniques-1.0.0", "decision": "D1",
             "note": "Same request bytes."}
    pin = {"prompt_name": "technique-staged-v5-a1.1", "template_digests": []}
    recs = [SimpleNamespace(stage="annotate.technique_staged", prompt_pin=pin, config_digest=new,
                            selection={"digest": selection_digest(new, [entry]), "target": new, "accepted": [entry]}),
            SimpleNamespace(stage="annotate.screen", prompt_pin={"prompt_name": "screen-v12"}, config_digest=D["screen"],
                            selection=None),
            SimpleNamespace(stage="fold.build", prompt_pin=None, config_digest="x", selection=None)]
    inp = _inputs([])
    inp.pins[old] = copy.deepcopy(inp.pins[new])
    inp.stages = ds.stage_selections(recs)
    assert set(inp.stages) == {"annotate.technique_staged", "annotate.screen"}
    p = _proj(m, inp)
    p.annotations(_row("sdoc000000001", technique_staged=_rec("technique_staged", digest=old)))
    p.annotations(_row("sdoc000000002", technique_staged=_rec("technique_staged", digest=old)))
    p.annotations(_row("sdoc000000003"))
    got = ds.stages_record(p)
    cid = {c.config_digest: c.config_id for c in p.configs.values()}
    assert got["annotate.technique_staged"] == {
        "selection_digest": selection_digest(new, [entry]),
        "target": {"config": new, "name": "technique-staged-v5-a1.1", "config_id": cid[new],
                   "config_ids": {cid[new]: 1}, "documents": 1},
        "accepted": [{**entry, "config_id": cid[old], "config_ids": {cid[old]: 2}, "documents": 2}]}
    assert got["annotate.screen"]["accepted"] == [] and got["annotate.screen"]["selection_digest"] == \
        selection_digest(D["screen"], [])
    text = ds.accepted_sentences({"stages": got})
    assert (f"- `annotate.technique_staged`: 2 documents carry an annotation of "
            f"`technique-staged-v5-a1.1@techniques-1.0.0` (`{cid[old]}`), kept for the target") in text
    assert "by D1. Same request bytes." in text
    assert "No annotation in this release was kept" in ds.accepted_sentences({"stages": {"annotate.screen": got[
        "annotate.screen"]}})


def test_role_parameters_come_from_the_recorded_pin(m: Mapping_) -> None:
    """D758: a config's LLM role parameters are the ones its run recorded (``prompt_pin.role_params``); a run that
    recorded none exports ``role: null`` with a note, never today's ``llm.yaml`` values under an old config."""
    recorded = {"name": "extract_main_openai", "model": "openai/gpt-0-recorded", "fallbacks": [], "max_tokens": 7,
                "temperature": None, "reasoning_effort": "low"}
    inp = _inputs([])
    inp.pins[D["model_roles"]]["prompt_pin"]["role_params"] = recorded
    p = _proj(m, inp)
    p.annotations(_row())
    rows = {c.stage: c for c in p.configs.values()}
    assert json.loads(rows["annotate.model_roles"].parameters)["role"] == recorded
    assert rows["annotate.model_roles"].note is None
    old = rows["annotate.screen"]  # its pin recorded no role_params
    assert json.loads(old.parameters)["role"] is None and old.note == ds.ROLE_UNRECORDED
    assert "max_tokens" not in old.parameters and "fallbacks" not in old.parameters  # nothing of today's role


def test_a_not_attempted_row_of_an_llm_workflow_is_not_a_computed_config(m: Mapping_) -> None:
    """An incomplete build's ``not_attempted`` row (no evidence; select stamps origin ``computed``) of an LLM workflow
    names its config like any row of it; it was taken for a computed workflow's and failed (KeyError)."""
    row = _row()
    for wf in ("model_roles", "model_regexp_matches"):
        row["annotations"][wf] = _rec(wf, status="not_attempted", origin="computed", evidence=None,
                                      error_class="budget", payload=None)
        row["annotations"][wf]["payload"] = None
    p = _proj(m, _inputs([]))
    ann = p.annotations(row)
    md = ann["model_roles"]["metadata"]
    assert md["config_id"] == f"model_roles:model-roles-v3b:gpt-6-luna:{D['model_roles'][:8]}"
    assert ann["model_name_matches"]["metadata"]["config_id"] == md["config_id"]


# ---------------------------------------------------------------- machine translations (D755)
def _translated_row() -> dict[str, Any]:
    rec = _rec("translation", payload={"from_lang": "zh", "title_en": "AI Safety Governance Framework",
                                       "abstract_en": "This framework sets out principles.", "method": "llm"})
    rec["basis_kind"] = "fields"
    rec["basis_digest"] = "f" * 64
    row = _row(translation=rec)
    row["metadata"] = {"fields_digest": "f" * 64}
    return row


def test_translation_ships_marked_as_machine_translation(m: Mapping_) -> None:
    """Both variants carry the kind with its model in the metadata; the public variant withholds abstract_en as it
    withholds the abstract (abstract_paths), the internal one keeps it; title_en ships in both."""
    assert "translation" in m.kinds and m.abstract_paths == ["annotations.translation.payload.abstract_en"]
    assert "annotations.translation.payload.title_en" in m.prose_paths
    for variant in ("public", "internal"):
        p = _proj(m, _inputs([]), variant)
        row = _translated_row()
        out = {"annotations": p.annotations(row)}
        assert not p.problems, p.problems
        tr = out["annotations"]["translation"]
        assert tr["payload"]["title_en"] == "AI Safety Governance Framework" and tr["payload"]["method"] == "llm"
        assert tr["metadata"]["kind"] == "translation"
        assert p.configs[tr["metadata"]["config_id"]].model == "gpt-6-luna"  # the release names models without the vendor
        if m.variants[variant]["abstract"] != "keep":
            ds.withhold_abstract_paths(out, m.abstract_paths)
        assert (tr["payload"]["abstract_en"] is None) == (variant == "public"), variant
        assert tr["payload"]["title_en"] is not None
    ds.withhold_abstract_paths({"annotations": {"translation": None}}, m.abstract_paths)  # no record: unchanged


def test_quote_unverified_sentence() -> None:
    s = ds.quote_unverified_sentence({"quote_unverified": [("paper", 1000, 27), ("system_card", 100, 5),
                                                           ("news", 50, 0)]})
    assert s == ("In this release 32 of 1,150 technique rows (2.8 %) carry it; by `doc_kind` (kinds with at least 100 "
                 "rows): paper 2.7 % of 1,000; system_card 5.0 % of 100.")


def test_release_macros_run_on_the_release_schema(m: Mapping_) -> None:
    import duckdb

    row = _blank(pa.struct(list(m.schema)))
    st = _blank(m.schema.field("annotations").type.field("safety_techniques").type)
    st["metadata"].update(kind="safety_techniques", status="ok", config_id="safety_techniques:p:m:12345678")
    st["payload"].update(doc_kind="paper", techniques=[
        {"slug": "rlhf", "roles": {**ROLES, "mentioned": 2, "used": 2}, "quotes": [{"text": "q1", "anchored": False}],
         "flags": ["quote_unverified"], "note": None},
        {"slug": "dpo", "roles": {**ROLES, "mentioned": 1}, "quotes": [{"text": "q2", "anchored": True}], "flags": [],
         "note": None}])
    sc = _blank(m.schema.field("annotations").type.field("stack_relevance").type)
    sc["metadata"].update(kind="stack_relevance", status="ok")
    sc["payload"]["verdict"] = "relevant"
    row.update(doc_id="ssepg4p9vmv5w", release_id="2026-10-31", published_precision="unknown")
    row["annotations"].update(safety_techniques=st, stack_relevance=sc, other=[])
    con = duckdb.connect()
    con.register("d", pa.Table.from_pylist([row], schema=m.schema))
    con.execute(ds.RELEASE_MACROS)
    assert con.execute("SELECT doc_id, slug, roles.used, quote_unverified, first_quote, doc_kind, config_id "
                       "FROM technique_rows() ORDER BY slug").fetchall() == [
        ("ssepg4p9vmv5w", "dpo", 0, False, "q2", "paper", "safety_techniques:p:m:12345678"),
        ("ssepg4p9vmv5w", "rlhf", 2, True, "q1", "paper", "safety_techniques:p:m:12345678")]
    assert con.execute("SELECT ann_verdict(annotations, 'stack_relevance'), ann_ok(annotations, 'screen'), "
                       "ann_status(annotations, 'safety_techniques'), "
                       "ann_pinned(annotations, 'safety_techniques', 'other') IS NULL, "
                       "ann_pinned(annotations, 'safety_techniques', 'safety_techniques:p:m:12345678').metadata.kind "
                       "FROM d").fetchall() == [("relevant", False, "ok", True, "safety_techniques")]
    for stmt in duckdb.extract_statements(ds.RELEASE_MACRO_EXAMPLES):
        con.execute(stmt).fetchall()
    assert con.execute("SELECT count(*) FROM model_rows()").fetchall() == [(0,)]
    head = (DATASET_DIR / "FORMAT.head.md").read_text("utf-8")
    assert "{{release_macros}}" in head and "{{release_macro_examples}}" in head and "{{quote_unverified_counts}}" in head


def test_public_reports_link_is_the_data_repository_issue_forms() -> None:
    """release.json ``contact.public_reports`` is the issue-form page the card and the data repository name (the
    2026-10-27 candidate pointed at ``thesafetystack/data``; the data repository is ``thesafetystack/the-safety-stack``,
    the subtree ``data-repo/`` pulls from)."""
    url = ds.CONTACT["public_reports"]
    assert url == "https://github.com/thesafetystack/the-safety-stack/issues/new/choose"
    assert url in (DATASET_DIR / "README.card.md").read_text("utf-8")
    assert url in (REPO_ROOT / "data-repo" / "README.md").read_text("utf-8")


def test_stage_config_id_is_the_one_most_documents_carry(m: Mapping_) -> None:
    """One config digest with a model id and a ``rule`` id (partly computed rows): the stage's ``config_id`` is the id
    most of its documents carry, whichever row came last; ``config_ids`` gives both."""
    from types import SimpleNamespace

    from stack.plan import selection_digest

    dg = D["affiliation"]
    inp = _inputs([])
    inp.stages = ds.stage_selections([SimpleNamespace(
        stage="annotate.affiliation", prompt_pin={"prompt_name": "affiliation-v1"}, config_digest=dg,
        selection={"digest": selection_digest(dg, []), "target": dg, "accepted": []})])
    p = _proj(m, inp)
    model = [p.config_id("affiliation", dg, False) for _ in range(3)][0]
    rule = p.config_id("affiliation", dg, True)  # last, as in the 2026-11-01 build
    t = ds.stages_record(p)["annotate.affiliation"]["target"]
    assert model.split(":")[2] == "gpt-6-luna" and rule.split(":")[2] == "rule"
    assert t["config_id"] == model and t["config_ids"] == {model: 3, rule: 1} and t["documents"] == 4


# ---------------------------------------------------------------- taxonomy files: counts and layouts
@pytest.mark.parametrize("name,version,want", [
    ("techniques", "1.0.0", {"techniques": 234, "groups": 9}), ("techniques", "1.1.0", {"techniques": 234, "groups": 9}),
    ("models", "1.0.0", {"models": 1877}), ("models", "1.1.0", {"models": 1877})])
def test_taxonomy_sizes_in_either_layout(name: str, version: str, want: dict[str, int]) -> None:
    import yaml

    path = f"taxonomy/{name}-{version}.yaml"
    assert ds.taxonomy_sizes(name, yaml.safe_load((REPO_ROOT / path).read_text("utf-8")), path) == want


def test_taxonomy_sizes_refuse_zero_and_unknown_layouts() -> None:
    with pytest.raises(DatasetError, match="0 groups"):
        ds.taxonomy_sizes("techniques", {"entries": [{"id": "a", "node": "technique"}]}, "t.yaml")
    with pytest.raises(DatasetError, match="0 models"):
        ds.taxonomy_sizes("models", {"entries": []}, "m.yaml")
    with pytest.raises(DatasetError, match="neither the catalogue envelope"):
        ds.taxonomy_sizes("models", {"version": "9", "models": [{"id": "x"}]}, "m.yaml")


def _tax_stage(tmp_path: Path, names: dict[str, list[str]]) -> dict[str, Any]:
    (tmp_path / "taxonomies").mkdir()
    doc: dict[str, Any] = {"taxonomies": {}}
    for name, versions in names.items():
        for v in versions:
            rel = f"taxonomies/{name}-{v}.yaml"
            (tmp_path / rel).write_bytes((REPO_ROOT / "taxonomy" / f"{name}-{v}.yaml").read_bytes())
            doc["taxonomies"].setdefault(name, []).append({"version": v, "path": rel})
    return doc


def test_taxonomy_layouts_describe_both_layouts(tmp_path: Path) -> None:
    doc = _tax_stage(tmp_path, {"techniques": ["1.0.0", "1.1.0"], "models": ["1.0.0", "1.1.0"], "orgs": ["0.2.2"]})
    s = ds.taxonomy_layouts(doc, tmp_path)
    assert s.startswith("This release ships 5 taxonomy files (`taxonomies/techniques-1.0.0.yaml`, ")
    assert "The newest version of each vocabulary is in the catalogue envelope" in s
    assert "`taxonomies/techniques-1.0.0.yaml` is in the older layout: top-level lists `techniques` and `group_nodes`" in s
    assert "`taxonomies/models-1.0.0.yaml` is in the older layout: top-level `nodes`" in s
    assert "share one structure" not in s and "1.1.0.yaml` is in" not in s
    (tmp_path / "b").mkdir()
    one = ds.taxonomy_layouts(_tax_stage(tmp_path / "b", {"orgs": ["0.2.2"]}), tmp_path / "b")
    assert one.startswith("This release ships 1 taxonomy file (") and "An earlier version" not in one


def test_taxonomy_layouts_refuse_an_older_layout_newest_file(tmp_path: Path) -> None:
    doc = _tax_stage(tmp_path, {"models": ["1.0.0"]})
    with pytest.raises(DatasetError, match="newest models file is not in the catalogue envelope"):
        ds.taxonomy_layouts(doc, tmp_path)


def test_a_shared_document_link_in_a_shipped_file_is_a_leak() -> None:
    # D796. Red under: leak_problems not applying SHARED_LINKS.
    link = "https://docs.google.com/" + "document/d/1" + "A" * 43 + "/edit"
    assert ds.leak_problems("README.md", f"notes at {link}")
    assert not ds.leak_problems("README.md", "fetched from docs.google.com")
