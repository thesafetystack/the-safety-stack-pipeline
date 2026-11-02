"""Plan files as written: YAML strictness (D433) and the shipped plans (``configs/plans/*.yaml``).

No store, no network: plans are loaded from tmp copies of the shipped ones.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from stack.build import BuildFailed, preflight
from stack.cli import app
from stack.plan import PlanError, load_plan
from stack.stage_runner import load_stage_module
from workflows.annotate.screen.main import TIER2_OPTIONS

REPO = Path(__file__).resolve().parents[1]
PLANS = REPO / "configs" / "plans"
STD_V3 = PLANS / "std-v3.yaml"

#: std-v3's acquire line spans two lines, ``options:`` on the second (L6-17's scenario)
_ACQUIRE_HEAD = "  - {stage: acquire, out: {kind: part},"


def test_a_second_options_key_on_a_plan_line_is_refused_naming_key_and_line(tmp_path: Path) -> None:
    """The natural one-line edit of std-v3's acquire line adds ``options:`` to its first line, next
    to the one on its second: PyYAML kept the last mapping and dropped ``order`` silently (D433)."""
    text = STD_V3.read_text("utf-8")
    assert text.count(_ACQUIRE_HEAD) == 1
    edited = text.replace(_ACQUIRE_HEAD, _ACQUIRE_HEAD + " options: {order: newest_first},")
    head_line = edited.splitlines().index(next(l for l in edited.splitlines() if l.startswith(_ACQUIRE_HEAD))) + 1
    p = tmp_path / "std-v3-dup.yaml"
    p.write_text(edited, "utf-8")
    with pytest.raises(PlanError) as err:
        load_plan(p)
    msg = str(err.value)
    assert str(p) in msg
    assert "duplicate key 'options'" in msg
    assert f"first at line {head_line}," in msg
    assert f"line {head_line + 1}," in msg


def test_an_annotate_line_naming_a_record_is_refused_at_load(tmp_path: Path) -> None:
    """``annotate.doc_class`` is a record screen writes (registry ``produced_by``), not a workflow: the
    plan loaded and the build failed at that line (no module); the loader refuses it (L1-8, S25-92)."""
    text = STD_V3.read_text("utf-8")
    head = "  - {stage: fold.build"
    assert text.count(head) == 1
    p = tmp_path / "std-v3-doc-class.yaml"
    p.write_text(text.replace(head, "  - {stage: annotate.doc_class}\n" + head), "utf-8")
    with pytest.raises(PlanError, match=r"annotate\.doc_class: 'doc_class' is a record of annotate\.screen"):
        load_plan(p)


# ─────────────────────────── stage options (D434) ───────────────────────────

SHIPPED = sorted(PLANS.glob("*.yaml"))


@pytest.mark.parametrize("path", SHIPPED, ids=lambda p: p.stem)
def test_every_shipped_line_carries_only_options_its_stage_reads(path: Path) -> None:
    """Every line's stage module declares ``OPTIONS`` (D434), and every shipped option is in it."""
    plan = load_plan(path)
    for line in plan.stages:
        known = getattr(load_stage_module(line.stage), "OPTIONS", None)
        assert known is not None, f"{line.stage}: no OPTIONS"
        assert set(line.options) <= known, (line.stage, sorted(set(line.options) - known))


@pytest.mark.parametrize(("stage", "key", "hint"), [
    ("triage", "worker", "workers"),
    ("annotate.screen", "screen_usd_per1k", "screen_usd_per_1k"),
    ("annotate.affiliation", "max_row", "max_rows"),
    ("collect.openalex", "max_record", "max_records"),
    ("collect.lw_graphql", "tag", "tags"),
    ("gather", "workers", None),
    ("fold.corpus", "workers", None),
])
def test_a_build_refuses_an_option_its_stage_does_not_read_at_its_start(
        tmp_path: Path, stage: str, key: str, hint: str | None) -> None:
    """A misspelt option on a non-acquire line did nothing (the e2e's annotate options); the build's
    preflight now ends it ``stage_refused`` before any run, naming the closest known key."""
    plan = load_plan(STD_V3)
    lines = [ln.model_copy(update={"options": {**ln.options, key: 1}}) if ln.stage == stage else ln
             for ln in plan.stages]
    assert any(ln.stage == stage for ln in plan.stages)
    with pytest.raises(BuildFailed) as err:
        preflight(tmp_path, plan.model_copy(update={"stages": lines}))
    assert err.value.error_class == "stage_refused"
    want = f"{stage}: unknown options {key!r}" + (f" (did you mean {hint!r}?)" if hint else "")
    assert want in str(err.value), str(err.value)


_COLLECT_SRC = [*(REPO / "workflows" / "collect").rglob("*.py"), *(REPO / "workflows" / "fetch").rglob("*.py"),
                *(REPO / "stack" / "collect").rglob("*.py"), REPO / "stack" / "lookup" / "card_date_backfill.py"]
_OPTION_CALL = re.compile(r"\.option\(\s*[\"']([a-z_]+)[\"']")


def _collect_options() -> dict[str, frozenset[str]]:
    return {f"collect.{p.parent.name}": load_stage_module(f"collect.{p.parent.name}").OPTIONS
            for p in sorted((REPO / "workflows" / "collect").glob("*/main.py"))}


def test_every_collect_option_read_is_declared() -> None:
    """``Collector.option("…")`` reads (D434): each literal key read in a collector's own ``main.py``
    is in its ``OPTIONS``, and every key read anywhere in the collect code is in some stage's."""
    declared = _collect_options()
    for stage, known in declared.items():
        main = REPO / "workflows" / "collect" / stage.removeprefix("collect.") / "main.py"
        read = set(_OPTION_CALL.findall(main.read_text()))
        assert read <= known, (stage, sorted(read - known))
    read_anywhere = {k for p in _COLLECT_SRC for k in _OPTION_CALL.findall(p.read_text())}
    read_anywhere |= {"fraction", "max_queries"}  # sampling loops: c.option(name, ...) over these names
    assert read_anywhere <= frozenset().union(*declared.values()), sorted(read_anywhere - frozenset().union(*declared.values()))


def test_every_annotate_and_triage_option_read_is_declared() -> None:
    """The framework (``stack/annotate``, ``stack/llm/batch.py``), the annotate workflows and triage
    read only declared keys (a grep of ``options``/``opts`` ``.get("…")``, ``["…"]`` and ``"…" in``)."""
    pat = re.compile(r"\b(?:options|opts|stage_line\.options|line\.options)(?:\.get\(|\[)\"([a-z_0-9]+)\"|"
                     r"\"([a-z_0-9]+)\" in (?:options|opts)\b")
    def reads(*roots: Path) -> set[str]:
        out: set[str] = set()
        for root in roots:
            for p in ([root] if root.is_file() else root.rglob("*.py")):
                out |= {a or b for a, b in pat.findall(p.read_text())}
        return out
    framework = reads(REPO / "stack" / "annotate", REPO / "stack" / "llm" / "batch.py")
    assert framework, "the grep found nothing: the pattern is stale"
    for wf in ("affiliation", "model_roles", "screen", "technique_roles"):
        known = load_stage_module(f"annotate.{wf}").OPTIONS
        own = reads(REPO / "workflows" / "annotate" / wf) | (set(TIER2_OPTIONS) if wf == "screen" else set())
        assert framework | own <= known, (wf, sorted((framework | own) - known))
    assert reads(REPO / "workflows" / "triage") | framework <= load_stage_module("triage").OPTIONS


# ─────────────────────────── the shipped plans dry-run (S25-78) ─────────────────────────


@pytest.mark.parametrize("path", SHIPPED, ids=lambda p: p.stem)
def test_every_shipped_plan_dry_runs_offline_on_an_empty_store(tmp_path: Path, path: Path) -> None:
    """``stack build <plan> --dry-run`` on an empty local store (no network, nothing written): the
    plan loads, every line resolves its inputs and options, the line table lists every stage
    (L6-18: a plan edit the build refuses was found only when the weekly build started)."""
    store = tmp_path / "store"
    store.mkdir()
    res = CliRunner().invoke(app, ["build", str(path), "--dry-run", "--json", "--store", str(store)])
    if res.exception is not None and not isinstance(res.exception, SystemExit):
        raise res.exception
    assert res.exit_code == 0, res.output
    d = json.loads(res.output)
    # on an empty store the stages reading the documents head (lookups, links) have nothing yet:
    # warned "not run" and left out of the table; nothing else is
    not_run = [w.split(":")[0] for w in d["warnings"] if w.endswith("not run")]
    assert all("has no file yet (first build)" in w for w in d["warnings"] if w.endswith("not run")), d["warnings"]
    listed = list(dict.fromkeys(s["stage"] for s in d["stages"]))
    assert listed == [ln.stage for ln in load_plan(path).stages if ln.stage not in not_run]
    assert [s for s in d["stages"] if s.get("error")] == []
    assert sorted(p.name for p in store.rglob("*")) == []  # a dry run writes nothing


# ─────────────────────────── accepted configs on a plan line (D758) ───────────────────────────

BATCH = PLANS / "batch-2026-10a.yaml"
A, B, C = "a" * 64, "b" * 64, "c" * 64


def _batch(edit: Any = None) -> dict[str, Any]:
    import yaml

    data = yaml.safe_load(BATCH.read_text("utf-8"))
    for line in data["stages"]:  # the shipped lists off: each test states its own
        line.pop("target", None)
        line.pop("accepted", None)
    if edit is not None:
        edit(data)
    return data


def _line(data: dict[str, Any], stage: str) -> dict[str, Any]:
    return next(s for s in data["stages"] if s["stage"] == stage)


def _accept(stage: str, target: Any, accepted: list[dict[str, Any]]) -> Any:
    def edit(data: dict[str, Any]) -> None:
        _line(data, stage).update(target=target, accepted=accepted)
    return edit


def _entry(config: str = A, **kw: Any) -> dict[str, Any]:
    return {"config": config, "name": "technique-staged-v5-a1.1@techniques-1.0.0", "decision": "D1", **kw}


def test_accepted_lists_are_in_the_plan_digest_not_in_any_config_digest() -> None:
    """Without ``target``/``accepted`` the shipped plan digests as before; with them the plan digest moves, every
    line digest (so every config digest) stays, and the line's selection digest names target and list."""
    from stack.plan import Plan, selection_digest

    bare = Plan.model_validate(_batch())
    assert all("target" not in s and "accepted" not in s for s in bare.to_json()["stages"])
    with_acc = Plan.model_validate(_batch(_accept("annotate.technique_staged", B, [_entry(note="same bytes")])))
    assert with_acc.plan_digest != bare.plan_digest
    assert [s.digest() for s in with_acc.stages] == [s.digest() for s in bare.stages]
    line = with_acc.stage_line("annotate.technique_staged")
    assert line is not None and line.accepted_configs() == (A,) and line.target_of() == B
    sel = line.selection(B)
    assert sel == {"digest": selection_digest(B, sel["accepted"]), "target": B, "accepted": [_entry(note="same bytes")]}
    other = Plan.model_validate(_batch(_accept("annotate.technique_staged", B, [_entry(note="edited")])))
    assert other.stage_line("annotate.technique_staged").selection(B)["digest"] != sel["digest"]  # type: ignore[union-attr]
    assert bare.stage_line("annotate.technique_staged").selection(B)["digest"] == selection_digest(B, [])  # type: ignore[union-attr]


@pytest.mark.parametrize(("edit", "match"), [
    (_accept("annotate.technique_staged", None, [_entry()]), "needs `target`"),
    (_accept("annotate.technique_staged", B, []), "needs an `accepted` list"),
    (_accept("annotate.technique_staged", "B" * 64, [_entry()]), "not a full config digest"),
    (_accept("annotate.technique_staged", B, [_entry("x" * 64)]), "String should match pattern"),
    (_accept("annotate.technique_staged", B, [_entry(B)]), "is the target itself"),
    (_accept("annotate.technique_staged", B, [_entry(), _entry()]), "listed twice"),
    (_accept("annotate.technique_staged", B, [_entry(question="safety_relevance")]), "on the triage line only"),
    (_accept("annotate.technique_staged", {"q": B}, [_entry()]), "one config digest on an annotate line"),
    (_accept("annotate.technique_staged", B, [_entry(decision="pending")]), "String should match pattern"),
    (_accept("annotate.technique_staged", B, [_entry(rejudge="all")]), "Extra inputs are not permitted"),
    (_accept("fold.build", B, [_entry()]), "is for triage and annotate"),
    (_accept("triage", B, [_entry(question="safety_relevance")]), "mapping question -> question digest"),
    (_accept("triage", {"safety_relevance": B}, [_entry()]), "on the triage line only"),
    (_accept("triage", {"nosuch": B}, [_entry(question="nosuch")]), "the gate does not ask"),
    (_accept("triage", {"safety_relevance": B, "other": C}, [_entry(question="safety_relevance")]), "no accepted"),
])
def test_accepted_lists_refused_at_load(tmp_path: Path, edit: Any, match: str) -> None:
    import yaml

    p = tmp_path / "plans" / BATCH.name
    p.parent.mkdir()
    p.write_text(yaml.safe_dump(_batch(edit), sort_keys=False), "utf-8")
    with pytest.raises(PlanError, match=match):
        load_plan(p)


def test_the_shipped_plan_accepts_the_1_0_0_configs_of_its_catalogue_stages() -> None:
    """Revision 10 (D758): technique_staged and model_roles name their revision-9 config as target (what their pins
    give: no build-start refusal) and accept their 1.0.0 configs."""
    from stack.build import target_refusals

    plan = load_plan(BATCH)
    assert target_refusals(plan) == []
    assert plan.stage_line("annotate.technique_staged").accepted_configs() == (  # type: ignore[union-attr]
        "afc8e6f6e2d638c68d762d9101884aba6b0e3d0001f83a610646b1b6b107af35",)
    assert plan.stage_line("annotate.model_roles").accepted_configs() == (  # type: ignore[union-attr]
        "bb1fcb36642568f1cd82c97e147b223f0665e010b4957c0135069fe34fe66c0a",)


def test_a_target_that_is_not_the_pins_config_refuses_the_build(tmp_path: Path) -> None:
    from stack import gate
    from stack.build import dry_run, target_refusals
    from stack.plan import Plan

    bare = Plan.model_validate(_batch())
    (q,) = gate.questions(bare)
    plan = Plan.model_validate(_batch(lambda d: (
        _accept("annotate.technique_staged", B, [_entry()])(d),
        _accept("triage", {q.name: q.config_digest}, [_entry(question=q.name, name="safety-relevance-v3")])(d))))
    got = target_refusals(plan)
    assert len(got) == 1 and got[0].startswith("annotate.technique_staged: target bbbbbbbbbbbb… is not the config")
    assert got[0] in dry_run(tmp_path / "store", plan)["preflight"]
    assert target_refusals(plan, ["annotate.technique_staged"]) == [] and target_refusals(bare) == []
