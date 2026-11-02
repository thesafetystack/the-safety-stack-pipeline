"""``stack build`` overrides and ``--dry-run`` (T5.5; ``stack/build.py``: :class:`BuildOverrides`,
:func:`apply_overrides`, :func:`dry_run`).

The validation cases run :func:`apply_overrides` on the synthetic M5 plan alone (fast). The build
cases run :func:`run_build` over the synthetic M5 world with the stand-in stages, as
``tests/test_scenario_m5.py`` does (``slow``): ``--skip``, ``--filter`` ANDed with the line's filter,
``--max-usd`` as the annotate run sees it, a refused override written as a ``failed`` record, and
``--dry-run`` (library and CLI) writing nothing. The build-start preflight (REH F1, :func:`preflight`):
the stand-ins with the real acquire's ``preflight`` refuse a plan whose acquire line names an
``options.archive`` this environment cannot open, ``failed[environment]`` before any run, and
``--dry-run`` prints it; ``--skip``ped lines and plans without the option are unaffected.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from stack.annotate import stage as annotate_stage
from stack.build import (
    AppliedOverrides,
    BuildFailed,
    BuildOverrides,
    StageCall,
    apply_overrides,
    dry_run,
    placeholder,
    preflight,
    run_build,
    run_passes,
)
from stack.cli import app
from stack.plan import Plan, StageLine
from stack.query import NAMED_FILTERS
from stack.runs import read_run_records
from stack.stage_runner import WORKFLOWS_ROOT_ENV, StageError, new_series_refusal
from stack.store.parquet import read_header
from tests.test_convert_pin_guard import fake_tools
from tests.synth.records import run_line
from tests.synth.world import build_world

pytestmark = pytest.mark.e2e  # area marker(s), not a tier (pyproject markers; docs/SETUP.md §7)

REPO = Path(__file__).resolve().parents[1]
STAND_INS = REPO / "tests" / "synth" / "stages"
PLAN_PATH = REPO / "tests" / "synth" / "plans" / "synth-m5.yaml"
runner = CliRunner()


def synth_plan(name: str = "synth-m5", **lines: dict[str, Any]) -> Plan:
    """The synthetic M5 plan, renamed, with keys merged into the named stage lines."""
    data = yaml.safe_load(PLAN_PATH.read_text("utf-8"))
    data["plan"] = name
    for line in data["stages"]:
        line.update(lines.get(line["stage"], {}))
    return Plan.model_validate(data)


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv(WORKFLOWS_ROOT_ENV, str(STAND_INS))
    root = tmp_path / "store"
    build_world(root)
    return root


def runs_by_stage(store: Path, rec: dict[str, Any]) -> dict[str, Any]:
    recs = {r.run_id: r for r in read_run_records(store)}
    return {recs[r].stage: recs[r] for r in rec["runs"]}


def listing(store: Path) -> list[str]:
    return sorted(p.relative_to(store).as_posix() for d in ("data", "runs", "blobs")
                  for p in (store / d).rglob("*"))


def refused(plan: Plan, ov: BuildOverrides, root: Path) -> BuildFailed:
    with pytest.raises(BuildFailed) as exc:
        apply_overrides(root, plan, ov)
    return exc.value


# ---------------------------------------------------------------- validation (fast)
def test_override_refusals(tmp_path: Path) -> None:
    plan = synth_plan(**{"annotate.screen": {"budget": {"max_usd": 3}}})
    e = refused(plan, BuildOverrides(skip=("collect.*", "colect.*")), tmp_path)
    assert e.error_class == "override_refused" and "'colect.*' matches no stage" in str(e)
    e = refused(plan, BuildOverrides(max_usd={"triage": 1.0}), tmp_path)
    assert e.error_class == "override_refused" and "config_digest" in str(e) and "rescope" in str(e)
    e = refused(plan, BuildOverrides(max_usd={"annotate.screen": 5.0}), tmp_path)
    assert "raises the plan's budget.max_usd 3" in str(e)
    e = refused(plan, BuildOverrides(max_usd={"acquire": 1.0}), tmp_path)
    assert "only annotate.<wf>" in str(e)
    e = refused(plan, BuildOverrides(filters={"fold.corpus": "true"}), tmp_path)
    assert "plan config" in str(e)
    e = refused(plan, BuildOverrides(filters={"annotate.affiliation": "true"}), tmp_path)
    assert "not a stage of plan" in str(e)
    e = refused(plan, BuildOverrides(filters={"triage": "true"}), tmp_path)
    assert e.error_class == "override_refused"
    # no --prior refusals: the option is gone with the prior build (D373)
    # allowed: lowering, setting where the line has none, a skip pattern that matches
    ok = apply_overrides(tmp_path, plan, BuildOverrides(skip=("collect.*",), max_usd={"annotate.screen": 0.5}))
    assert ok.skipped == ("collect.arxiv",) and ok.lines["annotate.screen"].budget.max_usd == 0.5
    assert ok.record == {"skip": ["collect.arxiv"], "skip_patterns": ["collect.*"], "filter": {},
                         "max_usd": {"annotate.screen": 0.5}}
    assert apply_overrides(tmp_path, synth_plan(), BuildOverrides(max_usd={"annotate.screen": 2})).lines
    assert apply_overrides(tmp_path, plan, BuildOverrides()).record is None


def test_filter_is_anded_with_the_line_filter() -> None:
    applied = AppliedOverrides(filters={"annotate.screen": "doc_id IS NULL"})
    line = StageLine(stage="annotate.screen", filter="has_text")
    got = applied.call_of(line, StageCall(ins=("data/corpus/main/",)))
    assert got.filter == f"({NAMED_FILTERS['has_text']}) AND (doc_id IS NULL)" and got.ins == ("data/corpus/main/",)
    assert applied.call_of(StageLine(stage="annotate.screen"), StageCall()).filter == "doc_id IS NULL"
    other = StageLine(stage="gather", filter="x")
    assert applied.call_of(other, StageCall()) == StageCall()  # no override: the call as stage_inputs made it


# ---------------------------------------------------------------- builds on the synthetic world (slow)
# removed with --prior / newest_prior_build: test_seeded_prior_reaches_triage_and_annotate_on_a_first_build
# (D373); every line reads its series' heads now.


@pytest.mark.slow
def test_skip_filter_and_max_usd(store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[float | None] = []
    real = annotate_stage.AskContext

    def recording(*args: Any, **kw: Any) -> Any:
        actx = real(*args, **kw)
        seen.append(actx.budget_usd)
        return actx

    monkeypatch.setattr(annotate_stage, "AskContext", recording)
    plan = synth_plan(**{"annotate.screen": {"budget": {"max_usd": 3}, "filter": "metadata.title IS NOT NULL"}})
    b1 = run_build(store, plan)
    assert b1.status == "ok", b1.data.get("error")
    assert seen == [3.0]
    ov = BuildOverrides(skip=("collect.*",), filters={"annotate.screen": "doc_id IS NULL"},
                        max_usd={"annotate.screen": 0.25})
    b2 = run_build(store, plan, overrides=ov)
    assert b2.status == "ok", b2.data.get("error")
    assert "skipped by --skip: collect.arxiv" in b2.data["warnings"]
    assert b2.data["overrides"] == {"skip": ["collect.arxiv"], "skip_patterns": ["collect.*"],
                                    "filter": {"annotate.screen": "doc_id IS NULL"},
                                    "max_usd": {"annotate.screen": 0.25}}
    by1, by2 = runs_by_stage(store, b1.data), runs_by_stage(store, b2.data)
    assert "collect.arxiv" not in by2 and "gather" in by2
    ann = by2["annotate.screen"]
    assert ann.filter == "(metadata.title IS NOT NULL) AND (doc_id IS NULL)"
    corpus_rows = int(read_header(store / b2.files["corpus_input"]["path"])["row_count"])
    assert corpus_rows > 0 and ann.counts["scope.filtered_out"] == corpus_rows  # doc_id IS NULL: none annotated
    assert seen == [3.0, 0.25]  # the run's AskContext (its LLM client's budget) saw the override
    # annotate's config_digest excludes the stage line: the budget override does not rescope
    assert ann.config_digest == by1["annotate.screen"].config_digest
    # a refused override is a failed build with a record and no run
    b3 = run_build(store, plan, overrides=BuildOverrides(max_usd={"triage": 0.5}))
    assert (b3.status, b3.error_class, b3.data["runs"]) == ("failed", "override_refused", [])


@pytest.mark.slow
def test_dry_run_writes_nothing_and_lists_every_line(store: Path) -> None:
    plan = synth_plan()
    before = listing(store)
    d = dry_run(store, plan)
    assert listing(store) == before
    assert d["series"] == "main" and d["overrides"] is None
    assert [s["stage"] for s in d["stages"]] == ["collect.arxiv", "gather", "triage", "acquire", "fold.corpus",
                                                 "fold.corpus", "annotate.screen", "fold.build"]
    assert all("error" not in s for s in d["stages"])
    by = {(s["stage"], s["call"]): s for s in d["stages"]}
    assert by[("acquire", 0)]["repeat"] == 2 and by[("annotate.screen", 0)]["max_rows"] == 6
    assert by[("fold.corpus", 1)]["filter"] == "everything" and by[("fold.corpus", 1)]["repeat"] is None
    ann = by[("annotate.screen", 0)]
    assert ann["inputs"][0] == {"path": placeholder("corpus_input"), "file": placeholder("corpus_input")}
    assert [i["path"] for i in by[("fold.build", 0)]["inputs"]] == [placeholder("corpus_input"), placeholder("screen")]
    docs = by[("triage", 0)]["inputs"][0]
    assert docs["path"] == "data/documents/main/" and docs["file"].endswith(".full.parquet")
    assert by[("triage", 0)]["kind"] == "part" and by[("collect.arxiv", 0)]["kind"] == "part"
    assert [i["path"] for i in by[("collect.arxiv", 0)]["inputs"]] == ["data/sources.arxiv/main/",
                                                                          "data/coverage.arxiv/main/"]

    b1 = run_build(store, plan)
    assert b1.status == "ok", b1.data.get("error")
    before = listing(store)
    d = dry_run(store, plan, overrides=BuildOverrides(skip=("collect.*",), max_usd={"annotate.screen": 1}))
    assert listing(store) == before
    by = {(s["stage"], s["call"]): s for s in d["stages"]}
    assert by[("collect.arxiv", None)]["skipped"] is True
    assert by[("annotate.screen", 0)]["max_usd"] == 1.0
    # the inputs are the series' heads now (the last file of each directory)
    assert by[("triage", 0)]["inputs"][0]["file"] == Path(b1.files["documents"]["path"]).name
    assert d["compaction"]["due"] is True
    with pytest.raises(BuildFailed):
        dry_run(store, plan, overrides=BuildOverrides(skip=("nope",)))
    assert listing(store) == before


@pytest.mark.slow
def test_cli_dry_run(store: Path) -> None:
    before = listing(store)
    res = runner.invoke(app, ["--store", str(store), "build", str(PLAN_PATH), "--dry-run", "--skip", "collect.*"])
    assert res.exit_code == 0, res.output
    lines = res.stdout.splitlines()
    assert "series\tmain" in lines
    rows = [ln.split("\t") for ln in lines[lines.index("stage\tcall\trun\tinputs\touts\tkind\tfilter\tmax_usd\tmax_rows\trepeat") + 1:]]
    assert [r[0] for r in rows] == ["collect.arxiv", "gather", "triage", "acquire", "fold.corpus", "fold.corpus",
                                    "annotate.screen", "fold.build"]
    assert rows[0][2] == "skipped" and rows[6][3].startswith(placeholder("corpus_input"))
    res = runner.invoke(app, ["--store", str(store), "build", str(PLAN_PATH), "--dry-run", "--json"])
    assert res.exit_code == 0 and len(json.loads(res.stdout)["stages"]) == 8
    res = runner.invoke(app, ["--store", str(store), "build", str(PLAN_PATH), "--dry-run", "--skip", "colect.*"])
    assert res.exit_code == 1 and "matches no stage" in res.output
    assert listing(store) == before


# ---------------------------------------------------------------- build-start preflight (REH F1)
#: A plan-level convert config whose tool the fake venv lacks: acquire's preflight (the convert pin
#: guard, R4-1) refuses it (the archive lane, the other refusal source, is gone: T-STORE D329).
CONVERT = {"config": "pymupdf4llm-v1"}


def convert_plan(name: str = "synth-pin") -> Plan:
    data = yaml.safe_load(PLAN_PATH.read_text("utf-8"))
    data["plan"], data["acquire"] = name, {**(data.get("acquire") or {}), "convert": CONVERT}
    return Plan.model_validate(data)


@pytest.fixture
def checked(store: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The stand-ins, their ``acquire`` given the real acquire's ``preflight`` (its run stays the
    stand-in's); the venv faked without ``pymupdf4llm``. The stand-in's own option ``complete_after``
    is not a real acquire option, which the real preflight refuses (D391, e2e-fix): the graft drops it."""
    stages = tmp_path / "stages"
    shutil.copytree(STAND_INS, stages)
    with (stages / "acquire" / "main.py").open("a", encoding="utf-8") as f:
        f.write("\nfrom workflows.acquire.main import preflight as _real_preflight  # noqa: E402 (REH F1 test copy)\n"
                "\n\ndef preflight(plan, line, root):  # the real preflight on the line without the stand-in's option\n"
                "    opts = {k: v for k, v in line.options.items() if k != 'complete_after'}\n"
                "    return _real_preflight(plan, line.model_copy(update={'options': opts}), root)\n")
    monkeypatch.setenv(WORKFLOWS_ROOT_ENV, str(stages))
    fake_tools(monkeypatch, "pymupdf4llm")
    return store


def test_build_refuses_an_absent_convert_tool_before_any_run(checked: Path) -> None:
    runs = len(read_run_records(checked))
    data = listing(checked)
    b = run_build(checked, convert_plan())
    assert (b.status, b.error_class, b.data["runs"]) == ("failed", "environment", []), b.data.get("error")
    assert b.data["error"].startswith("build-start preflight: acquire: ")
    assert "needs ['pymupdf4llm']" in b.data["error"]
    assert len(read_run_records(checked)) == runs  # collect.arxiv, the first line, did not run
    assert listing(checked) == data  # the build record is under builds/, outside the listing (D344)


def test_dry_run_prints_the_preflight_and_skip_lifts_it(checked: Path) -> None:
    plan = convert_plan()
    before = listing(checked)
    d = dry_run(checked, plan)
    assert len(d["preflight"]) == 1 and "needs ['pymupdf4llm']" in d["preflight"][0]
    assert dry_run(checked, plan, overrides=BuildOverrides(skip=("acquire",)))["preflight"] == []
    assert preflight(checked, plan, ["acquire"]) == [] and preflight(checked, plan) == d["preflight"]
    path = checked.parent / "synth-pin.yaml"
    path.write_text(yaml.safe_dump(plan.to_json()), "utf-8")
    res = runner.invoke(app, ["--store", str(checked), "build", str(path), "--dry-run"])
    assert res.exit_code == 0, res.output
    assert f"preflight\terror: environment: {d['preflight'][0]}" in res.stdout.splitlines()
    assert listing(checked) == before
    # a plan without a convert config: nothing to refuse
    assert dry_run(checked, synth_plan())["preflight"] == []
    res = runner.invoke(app, ["--store", str(checked), "build", str(PLAN_PATH), "--dry-run"])
    assert res.exit_code == 0 and "preflight\t-" in res.stdout.splitlines(), res.output


@pytest.mark.slow
def test_a_build_whose_preflight_passes_runs(checked: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every convert tool present: the preflight is empty and the build runs."""
    fake_tools(monkeypatch)
    b = run_build(checked, convert_plan())
    assert b.status == "ok", b.data.get("error")
    assert "acquire" in runs_by_stage(checked, b.data)


# ---------------------------------------------------------------- series guard (A1, D491)
#: Every line skipped: the build starts, writes its record and runs nothing (cheap).
SKIP_ALL = BuildOverrides(skip=("*",))


def builds(store: Path) -> list[str]:
    return sorted(p.name for p in (store / "builds").glob("*")) if (store / "builds").exists() else []


def test_build_into_a_new_series_beside_documents_is_refused_before_any_record(store: Path) -> None:
    plan = synth_plan().model_copy(update={"series": "batch-import"})
    before = listing(store), builds(store)
    with pytest.raises(BuildFailed) as exc:
        run_build(store, plan, overrides=SKIP_ALL)
    assert exc.value.error_class == "new_series"
    assert "'batch-import' has no documents while 'main' has" in str(exc.value) and "--new-series" in str(exc.value)
    with pytest.raises(BuildFailed):
        run_passes(store, plan, 2, overrides=SKIP_ALL)
    assert (listing(store), builds(store)) == before  # no record, no run, no file
    d = dry_run(store, plan)
    assert d["new_series"]["allowed"] is False and "'main'" in d["new_series"]["why"]
    assert dry_run(store, plan, new_series=True)["new_series"]["allowed"] is True


def test_new_series_flag_starts_the_build(store: Path) -> None:
    plan = synth_plan().model_copy(update={"series": "batch-import"})
    b = run_build(store, plan, overrides=SKIP_ALL, new_series=True)
    assert b.error_class != "new_series" and b.data["series"] == "batch-import" and b.path.exists()


def test_no_refusal_into_a_series_with_documents_or_in_an_empty_store(store: Path, tmp_path: Path) -> None:
    assert new_series_refusal(store, "main") is None and dry_run(store, synth_plan())["new_series"] is None
    b = run_build(store, synth_plan(), overrides=SKIP_ALL)  # series main: has documents
    assert b.error_class != "new_series" and b.path.exists()
    empty = tmp_path / "empty"
    empty.mkdir()
    assert new_series_refusal(empty, "batch-import") is None
    b = run_build(empty, synth_plan().model_copy(update={"series": "batch-import"}), overrides=SKIP_ALL)
    assert b.error_class != "new_series" and b.path.exists()


def test_standalone_collector_run_into_a_new_series_is_refused(store: Path) -> None:
    """A collector run with no ``sources`` prior of its own mints every ``source_id`` afresh: the same guard."""
    plan = synth_plan()
    runs = len(read_run_records(store))
    with pytest.raises(StageError, match="collect.arxiv: series 'batch-import' has no documents"):
        run_line(store, plan, "collect.arxiv", series="batch-import")
    assert len(read_run_records(store)) == runs
    # --new-series: past the guard (the stand-in then fails on its empty chain itself: a recorded run)
    rec = run_line(store, plan, "collect.arxiv", series="batch-import", new_series=True)
    assert rec.status == "failed" and "stand-in collect.arxiv extends" in (rec.error or "")
    # a series with documents, or a non-collector stage: not guarded
    assert run_line(store, plan, "collect.arxiv").status == "ok"


def test_cli_new_series(store: Path) -> None:
    args = ["--store", str(store), "build", str(PLAN_PATH), "--series", "batch-import", "--skip", "*"]
    before = builds(store)
    res = runner.invoke(app, args)
    assert res.exit_code == 1 and "[new_series]" in res.output and "'main'" in res.output, res.output
    assert builds(store) == before
    res = runner.invoke(app, [*args, "--dry-run"])
    assert res.exit_code == 0 and any(ln.startswith("new_series\terror: refused:") for ln in res.stdout.splitlines())
    res = runner.invoke(app, [*args, "--new-series"])
    assert res.exit_code == 0, res.output
    assert len(builds(store)) == len(before) + 1
    res = runner.invoke(app, ["--store", str(store), "--plan", str(PLAN_PATH), "run", "collect.arxiv",
                              "--series", "batch-import"])
    assert res.exit_code == 1 and "--new-series" in res.output, res.output
