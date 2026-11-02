"""Two passes of one plan (``stack build <plan> --passes 2``; round 4, D221: the phases are idempotent on
successful entries): the real stages over an empty store under
``tests/synth/plans/synth-passes.yaml``, every transport faked.

* ``collect.arxiv_sweep``: one roster author, the arXiv Atom API answered by :class:`tests.fakes.http.FakeHttp`
  at the collector harness seam (``stack.collect.base.urllib_send``);
* ``gather``, ``fold.corpus``, ``fold.build``: real, no transport;
* ``triage`` (one question), ``annotate.screen`` (``screen-v11``, P3 borderline on tier 1 and adjudicated on
  tier 2), ``annotate.affiliation``: the model is
  :class:`StageFake` through ``stack.annotate.stage.TRANSPORT_HOOK`` (the provider key unset, the ledger a tmp
  file, the response cache removed before each pass so a repeated call reaches the transport and is counted);
* ``acquire``: the ``arxiv`` lane is a :class:`tests.fakes.lanes.FakeLane` serving a plain-text full text
  (the convert substage passes plain text through; no child process).

Pass 2 must make no collector request, no LLM call and no lane request, and both build records end ``ok``.
The fault variants plant one transient failure in pass 1 (a triage call, a screen call, a lane fetch) and
check that pass 2 retries exactly that row and ends with it ``ok`` (the first retry is due at once, D-row
"first retry"); the per-pass summary (:func:`stack.build.pass_summary`) is what ``--passes`` prints.
"""

from __future__ import annotations

import datetime as dt
import time
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

import stack.annotate.stage as annotate_stage
from stack import gate
from stack.build import BuildRecord, pass_summary, run_passes
from stack.cli import app
from stack.llm import LlmRequest, LlmResponse, ResponseCache
from stack.plan import load_plan
from stack.store.chain import read_view, resolve
from stack.store.parquet import read_family
from tests.fakes.http import FakeHttp
from tests.fakes.llm import FakeLlm, ok, prompt_text, server_error
from tests.fakes.overrides import add_overrides
from tests.synth.records import sdir
from workflows.acquire import lanes
from workflows.acquire.lanes import FetchOutcome, FetchRequest, LaneEnv

pytestmark = [pytest.mark.slow, pytest.mark.e2e]  # real stages, two builds per case

REPO = Path(__file__).resolve().parents[1]
PLAN_PATH = REPO / "tests" / "synth" / "plans" / "synth-passes.yaml"
ARXIV_API = "https://export.arxiv.org/api/query"
AUTHOR = "Ethan Perez"
#: The three papers the sweep finds: P1 and P3 relevant (fetched, screened), P2 not (skipped by triage).
PAPERS = {
    "P1": {"id": "2610.00101", "title": "MARK_P1 Debate as scalable oversight", "relevant": True},
    "P2": {"id": "2610.00102", "title": "MARK_P2 Protein folding benchmarks", "relevant": False},
    "P3": {"id": "2610.00103", "title": "MARK_P3 Reward hacking in RL agents", "relevant": True},
}
#: One stage marker per prompt template (the system text each stage sends).
STAGE_MARKERS = {"triage": "triage gate for a research corpus", "affiliation": "transcribe author affiliations",
                 "screen": "screen-v11"}
#: The plan pin's ``adjudicate_model`` (screen tier 2).
TIER2 = "openai/gpt-5.6-terra"
runner = CliRunner()


def full_text(label: str) -> bytes:
    p = PAPERS[label]
    body = (f"{p['title']}\n\n{AUTHOR}\nAnthropic, San Francisco\n\nAbstract. {label} TEXT_{label}. "
            + "We study how language models behave under oversight and report what we found in detail. " * 40)
    return body.encode()


def triage_reply(relevant: bool) -> LlmResponse:
    v = "relevant" if relevant else "not_relevant"
    return ok({"verdict": v, "confidence": 0.9, "reasoning": f"because {v}"})


def screen_reply(verdict: str = "relevant") -> LlmResponse:
    return ok({"verdict": verdict, "rationale": f"overall {verdict}", "buckets": ["paper"], "off_topic_reason": None,
               "stack": {"verdict": verdict, "quotes": [], "confidence": 0.8},
               "sr": {"verdict": "irrelevant", "quotes": [], "confidence": 0.6, "category": "none", "agenda": None},
               "usability": {"verdict": "usable", "issues": [], "note": None}})


def affiliation_reply() -> LlmResponse:
    return ok({"authors": [{"author_surface": AUTHOR, "affiliations": [{"surface": "Anthropic, San Francisco"}]}]})


class StageFake(FakeLlm):
    """The model for every LLM stage: answers by (stage marker, paper mark), counts requests per
    ``(stage, paper)``, and serves queued faults (``fail(stage, label, n)``) before the answer."""

    def __init__(self) -> None:
        super().__init__()
        self.seen: list[tuple[str, str]] = []
        self.faults: dict[tuple[str, str], int] = {}

    def fail(self, stage: str, label: str, times: int) -> None:
        self.faults[(stage, label)] = times

    def post(self, request: LlmRequest) -> LlmResponse:
        text = prompt_text(request)
        stage = next((s for s, m in STAGE_MARKERS.items() if m in text), None)
        label = next((k for k in PAPERS if f"MARK_{k}" in text), None)
        if stage is None or label is None:
            raise AssertionError(f"unscripted request (stage {stage}, paper {label}): {text[:200]!r}")
        with self._lock:
            self.requests.append(request)
            self.seen.append((stage, label))
            left = self.faults.get((stage, label), 0)
            if left:
                self.faults[(stage, label)] = left - 1
        if left:
            return server_error()
        if stage == "triage":
            return triage_reply(bool(PAPERS[label]["relevant"]))
        if stage == "screen":  # P3 is borderline on tier 1, so it is adjudicated on tier 2
            return screen_reply("borderline" if label == "P3" and request.model != TIER2 else "relevant")
        return affiliation_reply()

    def by_stage(self, since: int = 0) -> dict[str, int]:
        out: dict[str, int] = {}
        for s, _ in self.seen[since:]:
            out[s] = out.get(s, 0) + 1
        return out


class PaperLane:
    """The ``arxiv`` acquire lane: the paper's plain-text full text (by the arXiv id in the member's
    keys), after any queued faults (``fail(label, n)``: ``failed_transient[http_5xx]``); every request kept."""

    name = "arxiv"

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.faults: dict[str, int] = {}

    def fail(self, label: str, times: int) -> None:
        self.faults[label] = times

    def fetch(self, request: FetchRequest, env: LaneEnv) -> FetchOutcome:
        label = next(k for k, p in PAPERS.items() if str(p["id"]) in str(request.member))
        self.calls.append(label)
        url = f"https://arxiv.org/pdf/{PAPERS[label]['id']}"
        if self.faults.get(label):
            self.faults[label] -= 1
            return FetchOutcome("failed_transient", "http_5xx", f"{url}: 503", role_patch={"tried_urls": [url]})
        body = full_text(label)
        snap = {"digest": env.blobs.put(body), "bytes": len(body), "media_type": "text/plain",
                "url": url, "final_url": url, "http_status": 200, "via": "live"}
        return FetchOutcome("ok", snapshots=[snap], role_patch={"tried_urls": [url]})


def atom(entries: list[dict[str, Any]], day: str) -> str:
    parts = ['<?xml version="1.0"?>', '<feed xmlns="http://www.w3.org/2005/Atom">']
    for e in entries:
        parts.append(f"<entry><id>http://arxiv.org/abs/{e['id']}v1</id><title>{e['title']}</title>"
                     f"<summary>Abstract of {e['title']}: oversight of language models.</summary>"
                     f"<published>{day}T00:00:00Z</published><author><name>{AUTHOR}</name></author></entry>")
    return "".join([*parts, "</feed>"])


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The faked world: the arXiv API, the model, the arxiv lane; the plan written to
    ``<tmp>/configs/plans/`` and the test run from ``<tmp>`` (X6 finds the plan by name)."""
    from workflows.collect.arxiv_sweep import main as arxiv

    for var in ("STACK_ARXIV_ROSTER", "STACK_COLLECT_LIMIT", "STACK_COLLECT_FRACTION",
                "STACK_COLLECT_MAX_QUERIES", "STACK_ARXIV_FROM", "STACK_WORKFLOWS_ROOT", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    roster = tmp_path / "roster.yaml"
    roster.write_text(f'names:\n  - "{AUTHOR}"\n', encoding="utf-8")
    frm = (dt.datetime.now(dt.UTC).date() - dt.timedelta(days=5))
    day = (frm + dt.timedelta(days=1)).isoformat()
    http = FakeHttp()
    search = f"au:Perez_Ethan AND {arxiv._CAT_CLAUSE}"
    http.add(ARXIV_API, {"search_query": f"{search} AND submittedDate:[{frm:%Y%m%d}0000 TO {arxiv.ARXIV_DATE_UPPER}]",
                         "start": 0, "max_results": 10, "sortBy": "submittedDate", "sortOrder": "ascending"},
             body=atom(list(PAPERS.values()), day))
    monkeypatch.setattr("stack.collect.base.urllib_send", http)
    monkeypatch.setattr(time, "sleep", lambda _s: None)

    fake = StageFake()
    monkeypatch.setenv("STACK_PROGRESS", "0")
    monkeypatch.setattr(gate, "ASKER_HOOK", None)
    monkeypatch.setattr(annotate_stage, "TRANSPORT_HOOK", lambda: fake)
    monkeypatch.setattr(annotate_stage, "SLEEP", lambda _s: None)
    monkeypatch.setattr(ResponseCache, "for_store", classmethod(lambda _cls, _root: None))  # every call counted

    lane = PaperLane()
    data = yaml.safe_load(PLAN_PATH.read_text("utf-8"))
    for line in data["stages"]:
        if line["stage"] == "collect.arxiv_sweep":
            line["options"].update(roster=str(roster), **{"from": frm.isoformat()})
    plan_file = tmp_path / "configs" / "plans" / "synth-passes.yaml"
    plan_file.parent.mkdir(parents=True)
    plan_file.write_text(yaml.safe_dump(data, sort_keys=False), "utf-8")
    monkeypatch.chdir(tmp_path)
    with lanes.using({"arxiv": lane}):
        yield {"store": tmp_path / "store", "plan": load_plan(plan_file), "plan_file": plan_file,
               "http": http, "llm": fake, "lane": lane}


def calls_now(env: dict[str, Any]) -> tuple[int, int, int]:
    """(collector HTTP requests, LLM requests, lane fetches) so far."""
    return len(env["http"].calls), len(env["llm"].seen), len(env["lane"].calls)


def docs(store: Path) -> dict[str, dict[str, Any]]:
    """The ``documents`` head view by paper label (title mark)."""
    out = {}
    for r in read_view(resolve(store, sdir("documents"))).to_pylist():
        label = next(k for k in PAPERS if f"MARK_{k}" in str(r["metadata"]["title"]))
        out[label] = r
    return out


def summary(builds: list[BuildRecord]) -> dict[str, list[tuple[int, int, int]]]:
    return {s["stage"]: [(p["calls"], p["carried"], p["new_rows"]) for p in s["passes"]] for s in pass_summary(builds)}


def test_second_pass_makes_no_calls_and_the_third_is_not_run(env: dict[str, Any]) -> None:
    """``stack build synth-passes --passes 3`` from an empty store: pass 1 collects, triages, fetches,
    screens and reads affiliations; pass 2 carries everything (no collector request, no LLM call, no
    lane fetch, no changed row) and the loop stops there, ``converged``. Both records ``ok``, with
    ``pass`` and ``first_pass_build_id``; the per-stage summary is the run records' counts."""
    store = env["store"]
    marks: list[tuple[int, int, int]] = []

    def mark(_b: BuildRecord) -> None:
        marks.append(calls_now(env))

    res = run_passes(store, env["plan"], 3, on_pass=mark)
    assert [b.status for b in res.builds] == ["ok", "ok"], [b.data.get("error") for b in res.builds]
    assert res.stopped == "converged"
    assert marks[0] == (1, 8, 2)  # 1 Atom page; 3 triage + 3 screen (P3 on both tiers) + 2 affiliation
    assert marks[1] == marks[0]  # pass 2: nothing at all
    b1, b2 = res.builds
    assert (b1.data["pass"], b2.data["pass"]) == (1, 2)
    assert b1.data["first_pass_build_id"] == b2.data["first_pass_build_id"] == b1.build_id
    assert "prior_build_id" not in b2.data  # pass 2 continues from the heads pass 1 left (D373)
    got = summary(res.builds)
    assert got["collect.arxiv_sweep"] == [(1, 0, 3), (0, 1, 0)]  # pass 2: the query already covered
    assert got["gather"] == [(0, 0, 3), (0, 3, 0)]
    assert got["triage"] == [(3, 0, 3), (0, 3, 0)]
    assert got["acquire"] == [(2, 1, 3), (0, 3, 0)]  # pass 2 not_due: P2 idle, P1 and P3 nothing due
    assert got["annotate.screen"] == [(3, 0, 0), (0, 2, 0)]
    assert got["annotate.affiliation"] == [(2, 0, 0), (0, 2, 0)]
    assert {d: (r["annotations"]["triage"]["payload"]["decision"], r["state"]) for d, r in docs(store).items()} == {
        "P1": ("fetch", "acquired"), "P2": ("skip", "acquired"), "P3": ("fetch", "acquired")}  # P2: its in-band abstract


def test_cli_passes_prints_a_line_per_pass_and_the_summary(env: dict[str, Any]) -> None:
    no = runner.invoke(app, ["build", "synth-passes", "--store", str(env["store"]), "--passes", "2"])
    assert no.exit_code == 1 and "no store at" in no.output and not env["store"].exists()  # R6-5
    out = runner.invoke(app, ["build", "synth-passes", "--store", str(env["store"]), "--passes", "2", "--create"])
    assert out.exit_code == 0, out.output
    lines = out.stdout.splitlines()
    assert lines[0].startswith("pass 1/2\t") and "\tok\tcalls 11\t" in lines[0]
    assert lines[1].startswith("pass 2/2\t") and "\tok\tcalls 0\t" in lines[1] and lines[1].endswith("new_rows 0")
    assert lines[2].startswith("stage\tpass1 calls/carried/new_rows\tpass2")
    assert "triage\t3/0/3\t0/3/0" in lines
    assert lines[-1] == "final\tok"
    dry = runner.invoke(app, ["build", "synth-passes", "--store", str(env["store"]), "--passes", "2", "--dry-run"])
    assert dry.exit_code == 0, dry.output
    assert dry.stdout.splitlines()[0].startswith("passes\t2 (below: pass 1;")


@pytest.mark.parametrize("fault", ["triage", "annotate.screen", "acquire"])
def test_a_transient_failure_in_pass_1_is_retried_in_pass_2(env: dict[str, Any], fault: str) -> None:
    """One transient failure in pass 1 (a triage call of P3, a screen call of P1, the lane fetch of
    P3; an LLM call fails after the client's four tries) is due at once, so pass 2 retries exactly
    that row (and does the work it unblocks for that paper only) and ends with it ``ok``."""
    store, llm, lane = env["store"], env["llm"], env["lane"]
    label = "P1" if fault == "annotate.screen" else "P3"
    if fault == "acquire":
        lane.fail(label, 1)
    else:
        llm.fail(fault.removeprefix("annotate."), label, 4)
    ends: list[tuple[int, int]] = []
    res = run_passes(store, env["plan"], 2, on_pass=lambda _b: ends.append((len(llm.seen), len(lane.calls))))
    assert [b.status for b in res.builds] == ["ok", "ok"], [b.data.get("error") for b in res.builds]
    got = summary(res.builds)
    calls2 = {st: p[1][0] for st, p in got.items() if p[1][0]}
    if fault == "triage":
        assert calls2 == {"triage": 1, "acquire": 1, "annotate.screen": 2, "annotate.affiliation": 1}
        assert docs(store)["P3"]["annotations"]["triage"]["status"] == "ok"
    elif fault == "annotate.screen":
        assert calls2 == {"annotate.screen": 1}
        screen = {r["doc_id"]: r for r in read_family(store / res.builds[1].files["screen"]["path"])[0].to_pylist()}
        assert screen[docs(store)["P1"]["doc_id"]]["status"] == "ok"
    else:
        assert calls2 == {"acquire": 1, "annotate.screen": 2, "annotate.affiliation": 1}
        role = docs(store)["P3"]["content"]["roles"][0]
        assert (role["status"], role["attempts"]) == ("ok", 0)
    # pass 2's calls are about that paper only
    assert {lab for _st, lab in llm.seen[ends[0][0]:]} == {label}
    assert set(lane.calls[ends[0][1]:]) <= {label}


def test_a_lookup_on_a_store_without_documents_is_not_run(tmp_path: Path) -> None:
    """A first build on an empty store reaches the lookups before gather has written ``documents``:
    the line is not run (a warning), instead of the lookup refusing the run for its missing input
    (found by the round-4 real 2-pass check on a fresh scratch store)."""
    from stack.build import StageCall, stage_inputs
    from stack.plan import Plan

    plan = Plan.model_validate({"plan": "p", "schema_version": 2, "series": "main", "stages": [
        {"stage": "collect.openalex-lookup", "prior": ["sources", "coverage"], "keys": ["doi"]},
        {"stage": "collect.arxiv_sweep", "prior": ["sources", "coverage"]},
        {"stage": "gather"}]})
    notes: list[str] = []
    lookup, sweep = plan.stages[0], plan.stages[1]
    assert stage_inputs(tmp_path, plan, lookup, {}, notes.append) == []
    assert notes and f"{sdir('documents')} has no file yet" in notes[0]
    # the sweep's own (empty) series dirs: the empty prior of a first run (D373)
    assert stage_inputs(tmp_path, plan, sweep, {}, notes.append) == [StageCall(
        ins=(sdir("sources.arxiv_sweep"), sdir("coverage.arxiv_sweep")), series="main")]
    assert len(notes) == 1


def test_a_row_gather_resets_keeps_its_content_stamps_in_pass_2(env: dict[str, Any]) -> None:
    """Pass 2 changes an acquired paper's metadata (here a ``fields`` override of ``published_at`` for P1, added between the passes; in the
    real dev build, a blog member re-observed under another dated URL): gather moves P1 back to
    ``identified``, triage decides again on the new basis (``triaged``; its decision carried as its
    standing; D724: one call, the signal on the new basis, which decides nothing), and acquire re-points it with its
    ``content`` block unchanged. That block keeps pass 1's ``run_id``/``updated_at`` (C6), so pass 2's
    verify passes X17 ``created_preserved`` (round-4 real 2-pass check, P5: pass 2 ``failed[verify]``)."""
    store = env["store"]
    before: dict[str, Any] = {}

    def between(b: BuildRecord) -> None:
        if b.data["pass"] != 1:
            return
        before.update(docs(store))
        add_overrides(store, "fields", [{"override_id": "f1", "doc_id": before["P1"]["doc_id"],
                                         "field": "published_at", "value": "2026-01-02"}])

    res = run_passes(store, env["plan"], 2, on_pass=between)
    assert [b.status for b in res.builds] == ["ok", "ok"], [b.data.get("error") for b in res.builds]
    after = docs(store)
    p1, was = after["P1"], before["P1"]
    assert p1["metadata"]["published_at"] == dt.datetime(2026, 1, 2, tzinfo=dt.UTC)
    assert p1["state"] == "acquired" and p1["run_id"] != was["run_id"]  # re-pointed in pass 2
    assert {k: p1["content"][k] for k in ("run_id", "updated_at")} == \
        {k: was["content"][k] for k in ("run_id", "updated_at")}
    assert p1["content"] == was["content"]
    got = summary(res.builds)
    assert {st: p[1][0] for st, p in got.items() if p[1][0]} == {"triage": 1}  # P1's signal (D724); no fetch
    assert got["gather"][1][2] == got["triage"][1][2] == got["acquire"][1][2] == 1  # P1 only, in each


# ---------------------------------------------------------------- incomplete builds (round 5, R6-4)
def capped(env: dict[str, Any], *, max_usd: float | None = None, max_rows: int | None = None) -> Any:
    """synth-passes with ``screen`` in ``require_complete`` and the screen line capped (a budget or
    a row limit), written next to the original so ``stack build <file>`` finds it."""
    data = yaml.safe_load(env["plan_file"].read_text("utf-8"))
    for line in data["stages"]:
        if line["stage"] == "annotate.screen":
            if max_usd is not None:
                line["budget"] = {"max_usd": max_usd}
            if max_rows is not None:
                line["options"] = {**line.get("options", {}), "max_rows": max_rows}
    data["require_complete"] = [*data["require_complete"], "screen"]
    path = env["plan_file"].with_name(f"capped-{max_usd}-{max_rows}.yaml")
    path.write_text(yaml.safe_dump(data, sort_keys=False), "utf-8")
    return load_plan(path), path


def test_a_budget_capped_screen_ends_incomplete_and_the_next_build_asks_the_rest(
        env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """The screen line stops on its budget: the build runs the later lines (affiliation, fold.build) and
    verify, and ends ``incomplete`` with the stage and reason ``budget``. The next build (budget lifted)
    continues from the series heads it left: the screen run's priors are that build's files, and it asks
    only the rest.
    (Every screen answer is ``relevant`` here, one call per paper, so a $0.0018 budget admits exactly one
    $0.001 call whichever paper comes first.)"""
    import sys

    from stack.build import run_build

    reply = screen_reply
    monkeypatch.setattr(sys.modules[__name__], "screen_reply", lambda verdict="relevant": reply("relevant"))
    store, llm = env["store"], env["llm"]
    tight, _ = capped(env, max_usd=0.0018)
    b1 = run_build(store, tight)
    assert b1.status == "incomplete", b1.data.get("error")
    screen1 = next(rid for rid, c in b1.data["counts"].items() if c["stage"] == "annotate.screen")
    assert b1.data["incomplete"] == [{"stage": "annotate.screen", "run_id": screen1, "reasons": ["budget"]}]
    stages = [c["stage"] for c in b1.data["counts"].values()]
    assert stages[-2:] == ["annotate.affiliation", "fold.build"]  # the lines after the cap ran
    assert b1.data["verify"]["status"] == "ok" and "corpus" in b1.files
    s1 = {r["doc_id"]: r["status"] for r in read_family(store / b1.files["screen"]["path"])[0].to_pylist()}
    left = {d for d, st in s1.items() if st == "not_attempted"}
    done = {d for d, st in s1.items() if st == "ok"}
    assert len(left) == 1 and len(done) == 1, s1
    mark = len(llm.seen)
    loose, _ = capped(env, max_usd=1.0)
    b2 = run_build(store, loose)
    assert b2.status == "ok", b2.data.get("error")
    screen2 = next(rid for rid, c in b2.data["counts"].items() if c["stage"] == "annotate.screen")
    from stack.runs import find_run_record

    rec2 = find_run_record(store, screen2)
    assert rec2 is not None and rec2.prior == [b1.files["screen"]["file_id"], b1.files["screen_evidence"]["file_id"]]
    asked = {lab for st, lab in llm.seen[mark:] if st == "screen"}
    assert asked == {lab for lab, r in docs(store).items() if r["doc_id"] in left}  # only the rest
    s2 = {r["doc_id"]: r for r in read_family(store / b2.files["screen"]["path"])[0].to_pylist()}
    assert all(s2[d]["status"] == "ok" for d in left | done)
    assert all(s2[d]["run_id"] == screen1 for d in done)  # carried, not asked again


def test_passes_continue_after_an_incomplete_pass_and_stop_on_no_progress(env: dict[str, Any]) -> None:
    """A screen budget too small for any call: pass 1 ends ``incomplete`` and pass 2 still runs (from pass
    1's series heads); pass 2 makes no call and changes no row, so the loop stops ``no_progress`` before
    pass 3. The CLI prints ``stopped: no progress`` and ``final incomplete`` and exits 3."""
    store = env["store"]
    plan, path = capped(env, max_usd=0.001)
    res = run_passes(store, plan, 3)
    assert [b.status for b in res.builds] == ["incomplete", "incomplete"], [b.data.get("error") for b in res.builds]
    assert res.stopped == "no_progress" and res.final_status == "incomplete"
    assert [e["reasons"] for e in res.builds[1].data["incomplete"]] == [["budget"]]
    out = runner.invoke(app, ["build", str(path), "--store", str(store), "--passes", "3"])
    assert out.exit_code == 3, out.output
    assert "stopped: no progress\tafter pass 1" in out.stdout and out.stdout.splitlines()[-1] == "final\tincomplete"
    assert "incomplete: annotate.screen run" in out.output


def test_passes_resume_a_row_limit_until_complete(env: dict[str, Any]) -> None:
    """``options.max_rows: 1`` on screen: pass 1 screens one paper and ends ``incomplete``; pass 2 screens
    the other and ends ``ok``; pass 3 converges, so pass 4 is not run."""
    plan, _ = capped(env, max_rows=1)
    res = run_passes(env["store"], plan, 4)
    assert [b.status for b in res.builds] == ["incomplete", "ok", "ok"], [b.data.get("error") for b in res.builds]
    assert res.stopped == "converged"
    assert res.builds[0].data["incomplete"][0]["stage"] == "annotate.screen"
