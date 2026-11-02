"""V7, V8, X6, X7, X8 (``stack.checks.annotate``) on a store the annotate framework wrote
(``tests/test_annotate.py``'s set-up: fake workflows on the fake model, through the real stage
runner): green on the store as written; X6 red on an off-pin template digest and X8's override
recomputation. A fault is a hand edit of one data file that keeps the run record's
``file_digest`` (``hand_edit``), so X1 stays green and only the check under test may go red. The
store holds two ``screen`` runs (the second a rerun with the first's outputs as its prior,
``--in``); X faults go into the second's
files. Each check red on exactly its own fault is ``tests/test_checks_fixtures.py``'s (the
clean-world fixtures are ``tests/fixtures/checks/annotate.py``).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import stack.annotate.stage as annotate_stage
from stack.checks.verify import run_checks
from stack.store.chain import resolve
from tests.fakes.annotate import (
    STAGES_ROOT,
    annotate,
    doc_with_text,
    fold_corpus,
    hand_edit,
    plan,
    write_documents,
)
from tests.fakes.llm import FakeLlm, ok
from tests.fixtures.checks import clean_world, fid, write_world
from tests.fixtures.checks.annotate import annotated_clean, triage_evidence, v8_triage
from tests.synth.records import sdir

pytestmark = [pytest.mark.slow, pytest.mark.e2e, pytest.mark.checks]  # runner-driven store per case

Edit = Callable[[list[dict[str, Any]]], None]


def _set(col: str, value: Any, i: int = 0) -> Edit:
    def edit(rows: list[dict[str, Any]]) -> None:
        rows[i][col] = value
    return edit


#: The checks of ``stack.checks.annotate`` (V7, V8, X6, X7, X8); each is red on exactly its own fixture
#: in ``tests/test_checks_fixtures.py`` (``tests/fixtures/checks/annotate.py``).
CHECKS = ("V-wf", "V-evidence", "policy_pins", "basis_currency", "selection_recompute")
EXTRA_X6: dict[str, Edit] = {"template": _set("template_digest", "d" * 64)}


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Any, Any]:
    fake = FakeLlm(default=ok({"verdict": "relevant", "confidence": 0.7}))
    monkeypatch.setenv("STACK_WORKFLOWS_ROOT", str(STAGES_ROOT))
    monkeypatch.setattr(annotate_stage, "TRANSPORT_HOOK", lambda: fake)
    root = tmp_path / "store"
    write_documents(root, [doc_with_text(root, i, f"Body of paper {i}.") for i in range(3)], run=1)
    p = plan()
    corpus = fold_corpus(root, p)
    r1 = annotate(root, p, "screen", corpus)
    r2 = annotate(root, p, "screen", corpus, prior=r1)
    assert fake.count() == 3
    return root, r1, r2


def red(root: Path) -> dict[str, list[str]]:
    return {r.check: r.items for r in run_checks(root) if r.status in ("failed", "warn")}


def test_clean(store: tuple[Path, Any, Any]) -> None:
    by = {r.check: r for r in run_checks(store[0])}
    assert {n: by[n].status for n in CHECKS} == dict.fromkeys(CHECKS, "ok")
    assert red(store[0]) == {}


def test_x6_template_off_pin(store: tuple[Path, Any, Any]) -> None:
    root, _, r2 = store
    hand_edit(root, r2, "screen_evidence", EXTRA_X6["template"])
    got = red(root)
    assert set(got) == {"policy_pins"} and "template_digest" in got["policy_pins"][0], got


def test_x8_recomputes_overrides(store: tuple[Path, Any, Any]) -> None:
    """X8 reselects with the overrides at the head the run pinned (design v4.1 §8): an override
    added after the run moves nothing; a file whose override was undone by hand is red (X8 only)."""
    root, _, r2 = store
    from tests.fakes.overrides import add_overrides
    from tests.synth.rows import sid

    def quarantine(oid: str, i: int) -> dict[str, Any]:
        return {"override_id": oid, "key": {"doc_id": sid(i)}, "scope": {"members": [sid(i)]}, "quarantine": True}

    add_overrides(root, "screen", [quarantine("o1", 0)])
    assert red(root) == {}  # r2 pinned no overrides: the later add does not move X8
    corpus = resolve(root, sdir("corpus")).head_key  # the one fold.corpus file r2 read
    assert resolve(root, sdir("corpus")).head == r2.inputs["corpus"]
    r3 = annotate(root, plan(), "screen", corpus, prior=r2, only=["select"])
    assert r3.inputs["overrides"] and red(root) == {}
    add_overrides(root, "screen", [quarantine("o2", 1)])
    assert red(root) == {}  # r3's pin predates o2

    def undo(rows: list[dict[str, Any]]) -> None:
        r = next(r for r in rows if r["doc_id"] == sid(0))
        assert r["status"] == "quarantined"
        r.update(status="ok", error_class=None, error=None, override_ids=[], origin="ai_check")

    hand_edit(root, r3, "screen", undo)
    got = red(root)
    assert set(got) == {"selection_recompute"} and "status" in got["selection_recompute"][0], got


def test_v7_an_ok_ai_row_without_evidence_needs_override_ids(store: tuple[Path, Any, Any]) -> None:
    """V7 (D717): an ``ok`` ``ai_check`` row without ``evidence_id`` is red unless it lists the override ids that
    supplied its payload (an override-supplied stand-in). Hand edits, so X8 is red too; only V7 is asserted."""
    root, _, r2 = store

    def drop_evidence(ids: list[str]) -> Edit:
        def edit(rows: list[dict[str, Any]]) -> None:
            assert (rows[0]["status"], rows[0]["origin"]) == ("ok", "ai_check")
            rows[0].update(evidence_id=None, override_ids=ids)
        return edit

    hand_edit(root, r2, "screen", drop_evidence([]))
    got = red(root)
    assert "without evidence or override_ids" in " ".join(got.get("V-wf", [])), got
    hand_edit(root, r2, "screen", drop_evidence(["o-standin"]))
    assert "V-wf" not in red(root)


def test_the_unbroken_annotated_pair_is_green(tmp_path: Path) -> None:
    """The control for X6-X8's fixtures (``tests/fixtures/checks/annotate.py``): the clean world
    plus the annotated pair (run 30) that each of them breaks one thing in, named by the build
    (``corpus_input`` too), is green on every check, and the five annotate checks ran on it
    (``ok``, not ``skipped``), so a fixture's red is its own break, not the pair's."""
    world = clean_world()
    annotated_clean(world)
    assert world.build is not None and world.build["files"]["screen"]["file_id"] == fid(30, "screen")
    write_world(world, tmp_path)
    assert world.refusals == []
    by = {r.check: r for r in run_checks(tmp_path)}
    assert {n: by[n].status for n in CHECKS} == dict.fromkeys(CHECKS, "ok")
    red = {n: r.items for n, r in by.items() if r.status not in ("ok", "skipped")}
    assert red == {}, red


def test_v8_covers_triage_evidence(tmp_path: Path) -> None:
    """D159 (4), D166 (C): V8 recomputes ``triage_evidence`` digests. The clean world plus its
    triage run's evidence file is green on every check; one answer edited after the fact turns
    exactly V8 red, naming that file."""
    clean = tmp_path / "clean"
    world = clean_world()
    triage_evidence(world)
    write_world(world, clean)
    red = {r.check: r.items for r in run_checks(clean) if r.status not in ("ok", "skipped")}
    assert red == {}, red

    broken = tmp_path / "broken"
    world = clean_world()
    v8_triage(world)
    write_world(world, broken)
    results = run_checks(broken)
    red = {r.check: r.items for r in results if r.status not in ("ok", "skipped")}
    assert set(red) == {"V-evidence"}, red
    assert all("triage_evidence" in item and "result_digest" in item for item in red["V-evidence"]), red


# ─────────────────────────── accepted rows (D758) ───────────────────────────


@pytest.fixture
def accepted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Any, Any, Any]:
    """Three documents answered under config A (r1), then a run under config B (r2: the same prompt, another
    pin) that accepts A's rows: r2 asks nothing and selects A's rows, labelled A's."""
    from tests.test_annotate import _accepting, _digest, _repinned

    fake = FakeLlm(default=ok({"verdict": "relevant", "confidence": 0.7}))
    monkeypatch.setenv("STACK_WORKFLOWS_ROOT", str(STAGES_ROOT))
    monkeypatch.setattr(annotate_stage, "TRANSPORT_HOOK", lambda: fake)
    root = tmp_path / "store"
    write_documents(root, [doc_with_text(root, i, f"Body of paper {i}.") for i in range(3)], run=1)
    pa_ = plan()
    corpus = fold_corpus(root, pa_)
    r1 = annotate(root, pa_, "screen", corpus)
    pb = _repinned(pa_, note="b")
    pb = _accepting(pb, _digest(pb), [r1.config_digest])
    r2 = annotate(root, pb, "screen", corpus, prior=r1)
    assert r2.counts["carried.accepted"] == 3 and r2.config_digest != r1.config_digest
    return root, r1, r2, pb


def test_accepted_rows_are_green(accepted: tuple[Path, Any, Any, Any]) -> None:
    by = {r.check: r for r in run_checks(accepted[0])}
    assert {n: by[n].status for n in CHECKS} == dict.fromkeys(CHECKS, "ok")
    assert red(accepted[0]) == {}


def _edit_selection(root: Path, run_id: str, edit: Any) -> None:
    from stack.runs import find_run_record
    from tests.synth.records import rewrite_record

    rec = find_run_record(root, run_id)
    assert rec is not None and rec.selection is not None
    rec.selection = edit(dict(rec.selection))
    rewrite_record(root, rec)


def test_an_accepted_config_removed_from_the_run_record_fails_x6_and_x8(accepted: tuple[Path, Any, Any, Any]) -> None:
    from stack.plan import selection_digest

    root, _, r2, _ = accepted
    # the list emptied and its digest recomputed (a consistent record): the rows are not the run's selection
    _edit_selection(root, r2.run_id, lambda s: {**s, "accepted": [], "digest": selection_digest(s["target"], [])})
    got = red(root)
    assert set(got) == {"policy_pins", "selection_recompute"}, got
    assert any("among the run's accepted configs" in m for m in got["policy_pins"]), got
    assert any("differs in" in m for m in got["selection_recompute"]), got


def test_a_selection_whose_digest_does_not_recompute_fails(accepted: tuple[Path, Any, Any, Any]) -> None:
    root, _, r2, _ = accepted
    _edit_selection(root, r2.run_id, lambda s: {**s, "accepted": [{**s["accepted"][0], "note": "edited later"}]})
    got = red(root)
    assert any("selection digest" in m and "does not recompute" in m for m in got.get("policy_pins", [])), got


def test_an_accepted_row_is_judged_by_its_own_runs_pin(accepted: tuple[Path, Any, Any, Any]) -> None:
    root, r1, r2, _ = accepted
    hand_edit(root, r2, "screen_evidence", EXTRA_X6["template"])  # an A row carried into r2's evidence file
    got = red(root)
    assert "policy_pins" in got and any(f"(run {r1.run_id}): template_digest" in m for m in got["policy_pins"]), got


def test_an_override_supplied_row_with_its_own_digest_passes(accepted: tuple[Path, Any, Any, Any]) -> None:
    from tests.fakes.overrides import add_overrides
    from tests.synth.rows import sid

    root, _, r2, pb = accepted
    add_overrides(root, "screen", [{"override_id": "o-row", "key": {"doc_id": sid(0)},
                                    "scope": {"members": [sid(0)]}, "row": True,
                                    "value": {"verdict": "not_relevant", "confidence": 0.9}}])
    corpus = resolve(root, sdir("corpus")).head_key
    r3 = annotate(root, pb, "screen", corpus, prior=r2, only=["select"])
    rows = {r["doc_id"]: r for r in read_rows(root, r3)}
    assert rows[sid(0)]["evidence_id"] is None and rows[sid(0)]["config_digest"] not in (r2.config_digest,)
    assert red(root) == {}


def read_rows(root: Path, rec: Any) -> list[dict[str, Any]]:
    from stack.store.parquet import read_family

    return read_family(root / rec.outputs["screen"]["path"])[0].to_pylist()
