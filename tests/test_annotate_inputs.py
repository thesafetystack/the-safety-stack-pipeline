"""Annotate inputs (round 5 T-ANN, D235): chained annotators and a scope over annotation records.

* the plan loader: joined reads (registry ``reads`` ∪ the line's ``reads:``) must be registered
  and produced by an ``annotate.<producer>`` line; the stages are ordered producer before reader
  (stable: listed order kept otherwise); a cycle is refused naming it; a folded ``chained``
  workflow needs its reads folded; ``reads`` is omitted from dumps when empty (digests of plans
  without reads unchanged);
* ``stack.fold``: the ``chained`` basis and mapping M's second pass;
* the build: ``stage_inputs`` passes this build's file of every joined read, refuses a missing one;
  ``refuse_unproduced_records`` refuses a read the producer's pin does not produce;
* the stage: refuses an input outside the joined set; the scope filter sees a joined screen
  record (``ann_verdict(annotations, 'stack_relevance') = 'relevant'``);
* end to end (``stack build``, real screen and model_roles workflows on the fake model): build 1
  asks; build 2 with nothing changed asks nothing; a screen re-run under a new pin that keeps the
  verdicts re-asks no model_roles (a scope read is not in the basis); a changed text re-asks exactly
  that document's model_roles; a vocabulary edit re-asks every in-scope document, the unchanged menus
  from the LLM cache; ``fold.build`` maps nothing stale and every check (V9, X7, X8 included) is green.

**No shipped workflow has a ``chained`` basis since D591** (``model_roles`` read ``model_mentions``;
the two merged). The chained machinery (registry ``reads``, the basis, mapping M's second pass, the
build's inputs) is tested on a stand-in: the :func:`chained` fixture makes ``technique_roles`` a
``chained`` entry reading ``model_roles`` (binding techniques to models is the registry's named later
work) for the test's duration. Not covered any more: a whole build whose upstream re-run with the
same result carries the chained reader.
"""

from __future__ import annotations

import dataclasses
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
import yaml

import stack.annotate.stage as annotate_stage
from stack.build import (
    BuildFailed,
    dry_run,
    refuse_unproduced_records,
    run_build,
    stage_inputs,
)
from stack.checks.verify import run_checks
from stack.fold import chained_basis, join
from stack.plan import Plan, StageLine
from stack.schemas.families import family
from stack.schemas.payloads.model_roles import ROLE_NAMES
from stack.schemas.payloads.registry import REGISTRY, joined_reads
from stack.stage_runner import run_stage
from stack.store.parquet import read_family
from tests.fakes.annotate import (
    AT_RUN,
    doc_with_text,
    fold_corpus,
    revise,
    rows_of,
    write_documents,
    write_plan_file,
)
from tests.fakes.llm import FakeLlm, ok
from tests.synth.records import SERIES, sdir

pytestmark = pytest.mark.e2e  # area marker(s), not a tier (pyproject markers; docs/SETUP.md §7)

LUNA = "openai/gpt-6-luna"
REPO = Path(__file__).resolve().parents[1]


def plan_of(stages: list[dict[str, Any]], folded: list[str] = (), **extra: Any) -> Plan:  # type: ignore[assignment]
    return Plan.model_validate({"plan": "t", "schema_version": 2, "stages": stages,
                                "fold": {"build": {"annotations_folded": list(folded)}}, **extra})


def names(p: Plan) -> list[str]:
    return [s.stage for s in p.stages]


@pytest.fixture
def chained() -> Iterator[None]:
    """``technique_roles`` as a ``chained`` entry reading ``model_roles`` (module docstring), restored after."""
    spec = REGISTRY["technique_roles"]
    REGISTRY["technique_roles"] = dataclasses.replace(spec, basis_kind="chained", reads=("model_roles",))
    try:
        yield
    finally:
        REGISTRY["technique_roles"] = spec


# ---------------------------------------------------------------- the plan loader
def test_the_only_chained_entry_is_affiliation_orgs() -> None:
    """D591: model_roles' basis is its text. The one entry reading another annotation as its basis is
    ``affiliation_orgs`` (D736), on ``affiliation``."""
    assert REGISTRY["model_roles"].basis_kind == "text" and REGISTRY["model_roles"].reads == ()
    assert [(w, spec.reads) for w, spec in REGISTRY.items() if spec.reads or spec.basis_kind == "chained"] == [
        ("affiliation_orgs", ("affiliation",))]
    assert "model_mentions" not in REGISTRY


def test_joined_reads_are_registry_then_line(chained: None) -> None:
    assert REGISTRY["technique_roles"].reads == ("model_roles",)
    assert REGISTRY["technique_roles"].basis_kind == "chained"
    assert joined_reads("technique_roles", ["stack_relevance", "model_roles"]) == ("model_roles", "stack_relevance")
    assert joined_reads("model_roles", ["stack_relevance"]) == ("stack_relevance",)
    assert joined_reads("screen") == ()
    assert joined_reads("unregistered", ["stack_relevance"]) == ("stack_relevance",)


def test_a_reader_listed_before_its_producer_moves_after_it(chained: None) -> None:
    p = plan_of([{"stage": "fold.corpus"},
                 {"stage": "annotate.technique_roles", "reads": ["stack_relevance"]},
                 {"stage": "annotate.affiliation"},
                 {"stage": "annotate.model_roles"},
                 {"stage": "annotate.screen"},
                 {"stage": "fold.build"}])
    # technique_roles waits for model_roles (registry read) and screen (line read); the others keep
    # their listed order
    assert names(p) == ["fold.corpus", "annotate.affiliation", "annotate.model_roles", "annotate.screen",
                        "annotate.technique_roles", "fold.build"]
    listed = plan_of([{"stage": "fold.corpus"}, {"stage": "annotate.screen"}, {"stage": "annotate.model_roles"},
                      {"stage": "annotate.affiliation"}, {"stage": "annotate.technique_roles"}, {"stage": "fold.build"}])
    assert names(listed) == ["fold.corpus", "annotate.screen", "annotate.model_roles", "annotate.affiliation",
                             "annotate.technique_roles", "fold.build"]  # already ordered: unchanged
    # the resolved order is what the plan digest covers
    assert p.to_json()["stages"][4]["stage"] == "annotate.technique_roles"


def test_a_line_read_orders_too() -> None:
    p = plan_of([{"stage": "fold.corpus"}, {"stage": "annotate.model_roles", "reads": ["stack_relevance"]},
                 {"stage": "annotate.screen"}])
    assert names(p) == ["fold.corpus", "annotate.screen", "annotate.model_roles"]


def test_a_cycle_is_refused_naming_it() -> None:
    with pytest.raises(ValueError, match=r"cycle: annotate\.(model|technique)_roles -> .* -> annotate\."):
        plan_of([{"stage": "annotate.model_roles", "reads": ["technique_roles"]},
                 {"stage": "annotate.technique_roles", "reads": ["model_roles"]}])
    with pytest.raises(ValueError, match=r"cycle: annotate\.screen -> annotate\.screen"):
        plan_of([{"stage": "annotate.screen", "reads": ["stack_relevance"]}])


def test_a_read_without_its_producer_line_is_refused(chained: None) -> None:
    with pytest.raises(ValueError, match="no annotate.model_roles line"):
        plan_of([{"stage": "annotate.technique_roles"}])
    with pytest.raises(ValueError, match="no annotate.screen line"):
        plan_of([{"stage": "annotate.model_roles", "reads": ["stack_relevance"]}])
    with pytest.raises(ValueError, match="not a registered annotation"):
        plan_of([{"stage": "annotate.model_roles", "reads": ["nonesuch"]}])
    with pytest.raises(ValueError, match="not a registered annotation"):
        plan_of([{"stage": "annotate.model_roles", "reads": ["model_mentions"]}])  # merged away (D591)
    with pytest.raises(ValueError, match="annotate.<wf> lines only"):
        plan_of([{"stage": "fold.corpus", "reads": ["stack_relevance"]}])


def test_a_folded_chained_workflow_needs_its_reads_folded(chained: None) -> None:
    stages = [{"stage": "annotate.model_roles"}, {"stage": "annotate.technique_roles"}]
    with pytest.raises(ValueError, match="without its reads"):
        plan_of(stages, folded=["technique_roles"])
    assert plan_of(stages, folded=["model_roles", "technique_roles"]).fold.build.annotations_folded == (
        "model_roles", "technique_roles")


def test_reads_are_omitted_from_dumps_when_empty() -> None:
    """A plan without reads digests as before the field existed."""
    line = StageLine(stage="annotate.screen")
    assert "reads" not in line.model_dump() and "reads" not in line.model_dump(mode="json")
    assert StageLine(stage="annotate.screen", reads=["x"]).model_dump()["reads"] == ["x"]
    assert line.digest() == StageLine.model_validate({"stage": "annotate.screen", "reads": []}).digest()


def test_ann_scratch_plan_loads() -> None:
    from stack.build import find_plan
    from stack.plan import load_plan

    p = load_plan(find_plan("ann-scratch"))
    assert any(line.reads for line in p.stages)  # the plan exercises ``reads`` (its stage names are config)
    assert p.stage_line("annotate.technique_roles").reads == ["stack_relevance"]
    assert p.stage_line("annotate.model_roles").reads == ["stack_relevance"]
    assert {"model_roles", "model_regexp_matches", "technique_roles"} <= set(p.fold.build.annotations_folded)
    assert "model_mentions" not in p.fold.build.annotations_folded and "annotate.model_mentions" not in p.prompts
    refuse_unproduced_records(p)


# ---------------------------------------------------------------- the chained basis
def _ann(wf: str, status: str, basis: str | None, payload: dict[str, Any]) -> dict[str, Any]:
    spec = REGISTRY[wf]
    return {"workflow": wf, "version": spec.version, "status": status, "error_class": None,
            "basis_kind": spec.basis_kind, "basis_digest": basis, "config_digest": "c", "decided_at": AT_RUN,
            "origin": "computed", "is_basis_current": None, "payload": payload, "run_id": "r",
            "evidence_id": None, "result_digest": None}


ROLES = {"doc_kind": "paper", "variant_models": [], "extra_models": [], "flags": [], "repairs": [],
         "models": [{"node_id": "gpt-4.5", "roles": dict.fromkeys(ROLE_NAMES, 0) | {"mentioned": 1}, "evidence": [],
                     "note": None, "matched_node_id": "gpt-4.5"}]}
TECHNIQUES = {"doc_kind": "paper", "techniques": [], "extra_techniques": [], "flags": [], "repairs": []}


def test_chained_basis_reads_text_status_and_payload() -> None:
    mr = {"status": "ok", "payload": ROLES}
    base = chained_basis("t1", {"model_roles": mr}, ("model_roles",))
    assert base is not None
    assert chained_basis("t2", {"model_roles": mr}, ("model_roles",)) != base  # the text
    other = {"status": "ok", "payload": {**ROLES, "models": []}}
    assert chained_basis("t1", {"model_roles": other}, ("model_roles",)) != base  # the payload
    assert chained_basis("t1", {"model_roles": {**mr, "status": "empty"}}, ("model_roles",)) != base
    assert chained_basis(None, {"model_roles": mr}, ("model_roles",)) is None
    assert chained_basis("t1", {"model_roles": None}, ("model_roles",)) is None
    for undecided in ("skipped", "failed_transient", "not_attempted"):
        assert chained_basis("t1", {"model_roles": {**mr, "status": undecided}}, ("model_roles",)) is None


def _corpus_with(ann: dict[str, Any], text: str) -> pa.Table:
    """A one-row corpus table whose current text digest is ``text`` and annotations ``ann``."""
    from tests.synth.rows import document_row, with_digests

    row = with_digests(document_row(0, 1, state="acquired"))
    row["content"]["current_text_sha256"] = text
    for k, v in ann.items():
        row["annotations"][k] = v
    return pa.Table.from_pylist([row], schema=family("corpus").schema)


def _wf_table(ann: dict[str, Any], doc_id: str) -> pa.Table:
    row = {k: v for k, v in ann.items() if k != "payload"} | dict(ann["payload"]) | {
        "doc_id": doc_id, "item_key": "", "source_id": doc_id, "override_ids": [], "override_stale_ids": [],
        "attempts": 1, "error": None}
    return pa.Table.from_pylist([row], schema=family(ann["workflow"]).schema)


def test_mapping_m_second_pass_over_the_mapped_reads(chained: None) -> None:
    corpus = _corpus_with({}, "t1")
    doc = corpus.column("doc_id")[0].as_py()
    mr = _ann("model_roles", "ok", "t1", ROLES)
    current = chained_basis("t1", {"model_roles": {"status": "ok", "payload": ROLES}}, ("model_roles",))
    cases = {
        "current": (mr, current, ("ok", True)),
        "read payload changed": (_ann("model_roles", "ok", "t1", {**ROLES, "models": []}), current,
                                 ("skipped", False)),
        "read stale by text": (_ann("model_roles", "ok", "t0", ROLES), current, ("skipped", None)),
    }
    for why, (mr_ann, tech_basis, want) in cases.items():
        tr = _ann("technique_roles", "ok", tech_basis, TECHNIQUES)
        out, folded = join(corpus, {"technique_roles": _wf_table(tr, doc), "model_roles": _wf_table(mr_ann, doc)})
        a = out.column("annotations")[0].as_py()
        assert folded == ["technique_roles", "model_roles"]
        assert (a["technique_roles"]["status"], a["technique_roles"]["is_basis_current"]) == want, why
        if want[0] == "skipped":
            assert a["technique_roles"]["error_class"] == "stale_basis"


# ---------------------------------------------------------------- the build's inputs
def test_stage_inputs_pass_every_joined_read(tmp_path: Path, chained: None) -> None:
    p = plan_of([{"stage": "fold.corpus"}, {"stage": "annotate.screen"}, {"stage": "annotate.model_roles"},
                 {"stage": "annotate.technique_roles", "reads": ["stack_relevance"]}], series=SERIES)
    line = p.stage_line("annotate.technique_roles")
    built = {k: {"path": f"{sdir(d)}20261001T120000Z_r{k[:4]}.full.parquet"} for k, d in
             (("corpus_input", "corpus"), ("model_roles", "model_roles"), ("stack_relevance", "screen"))}
    (call,) = stage_inputs(tmp_path, p, line, built, lambda _m: None)
    assert tuple(call.ins) == (*(built[k]["path"] for k in ("corpus_input", "model_roles", "stack_relevance")),
                               sdir("technique_roles"), sdir("technique_roles_evidence"))
    with pytest.raises(BuildFailed, match="model_roles") as exc:
        stage_inputs(tmp_path, p, line, {k: v for k, v in built.items() if k != "model_roles"},
                     lambda _m: None)
    assert exc.value.error_class == "missing_input"


def test_a_read_record_the_pin_does_not_produce_is_refused() -> None:
    p = plan_of([{"stage": "annotate.screen"}, {"stage": "annotate.model_roles", "reads": ["text_usability"]}],
                prompts={"annotate.screen": {"name": "screen-v10", "model": LUNA}})
    with pytest.raises(BuildFailed, match="annotate.model_roles reads 'text_usability'") as exc:
        refuse_unproduced_records(p)
    assert exc.value.error_class == "missing_input"


# ---------------------------------------------------------------- the stage, the scope, end to end
def dual(verdict: str) -> Any:
    """A screen-v10 reply with the same verdict on both questions."""
    return ok({"verdict": verdict, "rationale": f"overall {verdict}", "buckets": ["paper"], "off_topic_reason": None,
               "stack": {"quotes": [], "confidence": 0.8, "verdict": verdict},
               "sr": {"quotes": [], "confidence": 0.6, "category": "none", "agenda": None, "verdict": verdict}})


TEXTS = [
    "DOC_REL1. We fine-tune Llama 3.1 70B with LoRA; responses were scored by GPT-4.5 acting as judge.",
    "DOC_REL2. We evaluated GPT-4.5 and Claude Opus 4.7 on the refusal benchmark.",
    "DOC_IRR. GPT-4.5 in a recipe for sourdough bread.",
    "DOC_REL3. A study of reward hacking with no model names at all.",
]
#: grades nothing: every model_roles answer is ``empty`` (called, nothing graded; registry version 3)
ROLES_REPLY = {"doc_kind": "paper", "models": [], "flags": []}
SCOPE = "ann_verdict(annotations, 'stack_relevance') = 'relevant'"


def roles_plan(cwd: Path, name: str, screen_pin: dict[str, Any] | None = None) -> Plan:
    """The plan file ``name`` (screen, model_roles), on the test world's series."""
    return write_plan_file(
        cwd, name,
        prompts={"annotate.screen": screen_pin or {"name": "screen-v10", "model": LUNA},
                 "annotate.model_roles": {"name": "model-roles-v2", "role": "extract_main", "model": LUNA}},
        # listed reader-first on purpose: the loader orders them
        stages=[{"stage": "fold.corpus"},
                {"stage": "annotate.model_roles", "prior": ["model_roles", "model_roles_evidence"],
                 "reads": ["stack_relevance"], "filter": SCOPE},
                {"stage": "annotate.screen", "prior": ["screen", "screen_evidence"]},
                {"stage": "fold.build"}],
        folded=["screen", "stack_relevance", "model_roles"]).model_copy(update={"series": SERIES})


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    fake = FakeLlm()
    fake.on("# THE MENU", ok(ROLES_REPLY), model=LUNA)  # model_roles' user turn (before the doc rules)
    for marker, verdict in (("DOC_REL", "relevant"), ("DOC_IRR", "irrelevant")):
        fake.on(marker, dual(verdict), model=LUNA)
    monkeypatch.delenv("STACK_WORKFLOWS_ROOT", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("STACK_PROGRESS", "0")
    monkeypatch.setattr(annotate_stage, "TRANSPORT_HOOK", lambda: fake)
    monkeypatch.setattr(annotate_stage, "SLEEP", lambda _s: None)
    monkeypatch.setattr(annotate_stage, "CLOCK", lambda: AT_RUN)
    monkeypatch.chdir(tmp_path)
    store = tmp_path / "store"
    docs = [doc_with_text(store, i, t) for i, t in enumerate(TEXTS)]
    write_documents(store, docs, run=1)
    return {"store": store, "fake": fake, "cwd": tmp_path, "ids": [d["doc_id"] for d in docs],
            "monkeypatch": monkeypatch}


@pytest.mark.slow
def test_stage_refuses_inputs_outside_its_joined_set_and_scopes_on_a_joined_record(env: dict[str, Any]) -> None:
    store, cwd, ids = env["store"], env["cwd"], env["ids"]
    p = roles_plan(cwd, "inputs-stage")
    # the dry run names this build's files of the reads as placeholders, in the resolved order
    dry = {(st["stage"], st["call"]): st for st in dry_run(store, p)["stages"]}
    ins = [i["path"] for i in dry[("annotate.model_roles", 0)]["inputs"]]
    assert ins == ["<this build: corpus_input>", "<this build: stack_relevance>",
                   sdir("model_roles"), sdir("model_roles_evidence")]
    corpus = fold_corpus(store, p)
    scr = run_stage(store, p, "annotate.screen", [corpus], series=SERIES)
    assert scr.status == "ok", scr.error
    rel = scr.outputs["stack_relevance"]["path"]
    # model_roles joins stack_relevance (its line read): without it, or with an extra input, refused
    missing = run_stage(store, p, "annotate.model_roles", [corpus], series=SERIES)
    assert missing.status == "failed" and "stack_relevance" in (missing.error or "")
    extra = run_stage(store, p, "annotate.model_roles", [corpus, rel, scr.outputs["screen"]["path"]],
                      series=SERIES)
    assert extra.status == "failed" and "not read: ['screen']" in (extra.error or "")
    mr = run_stage(store, p, "annotate.model_roles", [corpus, rel], series=SERIES)
    assert mr.status == "ok", mr.error
    got = {r["doc_id"]: r["status"] for r in rows_of(store, mr, "model_roles")}
    assert got == {ids[0]: "empty", ids[1]: "empty", ids[3]: "empty"}  # DOC_IRR is out of scope
    assert mr.counts["scope.filtered_out"] == 1


@pytest.mark.slow  # five builds through the runner
def test_build_carries_and_reasks_exactly_the_changed_document(env: dict[str, Any]) -> None:
    store, fake, cwd, ids = env["store"], env["fake"], env["cwd"], env["ids"]

    def roles_calls() -> int:
        return fake.count(contains="# THE MENU")

    p = roles_plan(cwd, "inputs-e2e")
    assert names(p)[1:3] == ["annotate.screen", "annotate.model_roles"]
    b1 = run_build(store, p)
    assert b1.status == "ok", b1.data.get("error")
    assert roles_calls() == 3  # every in-scope document is judged (DOC_REL3 names no model); DOC_IRR is out of scope
    roles1 = read_family(store / b1.files["model_roles"]["path"])[0].to_pylist()
    assert sorted((r["doc_id"], r["status"]) for r in roles1) == sorted(
        [(ids[0], "empty"), (ids[1], "empty"), (ids[3], "empty")])
    built = read_family(store / b1.files["corpus"]["path"])[0].to_pylist()
    for row in built:
        a = row["annotations"]["model_roles"]
        if a is not None:  # fold.build maps none stale
            assert (a["status"], a["is_basis_current"]) in (("ok", True), ("empty", True)), row["doc_id"]
    assert sum(r["annotations"]["model_roles"] is not None for r in built) == 3

    def green() -> None:
        results = run_checks(store)
        red = {r.check: r.items for r in results if r.status not in ("ok", "skipped")}
        assert red == {}, red
        by = {r.check: r.status for r in results}
        assert {n: by[n] for n in ("V-fold", "basis_currency", "selection_recompute")} == dict.fromkeys(
            ("V-fold", "basis_currency", "selection_recompute"), "ok")

    green()

    b2 = run_build(store, p)  # nothing changed: everything carried
    assert b2.status == "ok", b2.data.get("error")
    assert roles_calls() == 3

    # screen re-runs under a new pin (new config digest, same verdicts): the scope read changes
    # file, not the basis, so model_roles carries
    p3 = roles_plan(cwd, "inputs-e2e", screen_pin={"name": "screen-v10", "model": LUNA, "tier1_chars": 100000})
    b3 = run_build(store, p3)
    assert b3.status == "ok", b3.data.get("error")
    rel2 = read_family(store / b3.files["stack_relevance"]["path"])[0].to_pylist()
    rel1 = read_family(store / b1.files["stack_relevance"]["path"])[0].to_pylist()
    assert {r["config_digest"] for r in rel2}.isdisjoint({r["config_digest"] for r in rel1})
    assert roles_calls() == 3
    ev3 = read_family(store / b3.files["model_roles_evidence"]["path"])[0].to_pylist()
    assert len(ev3) == 3  # no new model_roles evidence either

    # a changed text of DOC_REL2: screen and exactly that model_roles re-ask
    revise(store, 1, TEXTS[1] + " We also used GPT-4.5 again.", run=50)
    b4 = run_build(store, p3)
    assert b4.status == "ok", b4.data.get("error")
    assert roles_calls() == 4
    ev = read_family(store / b4.files["model_roles_evidence"]["path"])[0].to_pylist()
    new = [e for e in ev if e["run_id"] not in {e3["run_id"] for e3 in ev3}]
    assert [e["doc_id"] for e in new] == [ids[1]]
    green()

    # the vocabulary changes (in model_roles' config digest): every in-scope document is re-asked; only
    # DOC_REL2's menu changes (no ``claude-opus-4.7`` node patterns any more), so only it costs a call,
    # DOC_REL1's identical request is served from the LLM cache
    cfg = cwd / "cfg-vocab-edit"
    shutil.copytree(REPO / "configs", cfg, ignore=shutil.ignore_patterns("plans"))
    vocab = yaml.safe_load((cfg / "models.yaml").read_text("utf-8"))
    node = next(e for e in vocab["entries"] if e["id"] == "claude-opus-4.7")
    node["mentions"] = []
    node.pop("weak_mentions", None), node.pop("mentions_verbatim", None)  # models-2 has no null pattern lists
    (cfg / "models.yaml").write_text(yaml.safe_dump(vocab, sort_keys=False, allow_unicode=True), "utf-8")
    env["monkeypatch"].setenv("STACK_CONFIG_DIR", str(cfg))
    b5 = run_build(store, p3)
    assert b5.status == "ok", b5.data.get("error")
    mr4 = {r["doc_id"]: r for r in read_family(store / b4.files["model_roles"]["path"])[0].to_pylist()}
    mr5 = {r["doc_id"]: r for r in read_family(store / b5.files["model_roles"]["path"])[0].to_pylist()}
    assert {d for d in mr5 if mr5[d]["config_digest"] != mr4[d]["config_digest"]} == {ids[0], ids[1], ids[3]}
    rx4 = {r["doc_id"]: r for r in read_family(store / b4.files["model_regexp_matches"]["path"])[0].to_pylist()}
    rx5 = {r["doc_id"]: r for r in read_family(store / b5.files["model_regexp_matches"]["path"])[0].to_pylist()}
    assert [d for d in rx5 if rx5[d]["matches"] != rx4[d]["matches"]] == [ids[1]]
    assert "claude-opus-4.7" not in [m["node_id"] for m in rx5[ids[1]]["matches"]]
    assert roles_calls() == 5
    green()


def test_payloads_round_trip_through_the_corpus_schema() -> None:
    """The ``documents``/``corpus`` schema's typed fields: model_roles (with D591's ``matches``) then
    technique_roles; no ``model_mentions`` (D591)."""
    ann = family("corpus").schema.field("annotations").type
    names = [ann.field(i).name for i in range(ann.num_fields)]
    assert names[-1] == "other" and names[names.index("model_roles") + 1] == "model_regexp_matches"
    assert names[names.index("model_regexp_matches") + 1] == "technique_roles" and "model_mentions" not in names
    pa.array([ROLES], type=REGISTRY["model_roles"].payload_schema)


def test_every_annotate_stage_is_named_where_the_docs_list_them() -> None:
    """Every ``workflows/annotate/<wf>/`` stage appears in the how-tos that list the annotators (worked
    examples and eval sets of add-annotator, the ``workers`` row of concurrency) and in OPERATE's stage list
    and ``eval --plan`` row. Red when a stage is added without them (technique_staged was missing)."""
    stages = sorted(p.name for p in (REPO / "workflows" / "annotate").iterdir() if (p / "main.py").is_file())
    assert "technique_staged" in stages
    add = (REPO / "docs" / "howto" / "add-annotator.md").read_text("utf-8")
    examples, evals = add.split("## Checklist", 1)[0], add.split("8. **Eval**", 1)[1].split("## ", 1)[0]
    workers = next(ln for ln in (REPO / "docs" / "howto" / "concurrency.md").read_text("utf-8").splitlines()
                   if ln.startswith("| `annotate.<wf>`"))
    operate = (REPO / "docs" / "OPERATE.md").read_text("utf-8")
    eval_row = next(ln for ln in operate.splitlines() if ln.startswith("| `eval …`"))
    stage_list = " ".join(operate.split("`annotate.*` (", 1)[1].split(")", 1)[0].split())
    for wf in stages:
        assert f"{wf}/`" in examples, f"add-annotator's worked examples lack {wf}"
        assert wf in evals, f"add-annotator's eval step lacks {wf}"
        assert wf in workers or (wf.startswith("model_") and "model_*" in workers), f"concurrency lacks {wf}"
        assert f"`{wf}`" in eval_row and wf in stage_list, f"OPERATE lacks {wf}"
