"""Triage's bounded memory (D780): the stage holds the ``documents`` head as Arrow and turns it into Python rows
:data:`stack.gate.BATCH_ROWS` at a time (:func:`stack.gate.row_batches`, which ``stack.gate.gate_view`` uses too);
only the rows the asker is sent leave Arrow whole. A gate edit makes every row config-stale, so every row is scoped (the
2026-10-31 production cycle: a rule's compiled SQL changed and the run held two Python copies of the whole head).

The gate-edit case runs with two-row batches and checks that no batch is larger, that every row is scoped and none
asked, and that decisions, states and question rows are those of the run before the edit. The store cases of the
triage tests then run again with one-row batches: their assertions hold whatever the batch size.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from stack import gate
from tests import test_triage_questions as tq
from tests import test_triage_signals as tsig
from tests import test_triage_standing as tst
from tests.fakes.llm import FakeLlm
from tests.synth.rows import sid
from tests.test_gather_identity import Store
from tests.test_triage_questions import (
    Q,
    acquired_row,
    fake,
    plan_with,
    run_triage,
    title,
    verdict,
)
from tests.test_triage_rules import ann, paper
from tests.test_triage_standing import no_asker

__all__ = ["fake", "no_asker"]  # the fixtures, imported for this module's tests

Row = dict[str, Any]


def _outcome(row: Row) -> tuple[Any, ...]:
    t = ann(row)
    qs = tuple((q["question"], q["status"], q["error_class"], q.get("verdict"), q["evidence_id"])
               for q in t["payload"]["questions"])
    return (row["state"], t["status"], t["error_class"], t["origin"], t["payload"]["decision"],
            tuple(t["payload"]["wanted_by"]), qs)


@pytest.mark.slow
def test_a_gate_edit_scopes_every_row_in_bounded_batches(tmp_path: Path, fake: FakeLlm,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    store = Store(tmp_path / "store")
    acquired_row(store, 7)
    store.add("sources.arxiv_sweep", [paper(i) for i in range(1, 6)])
    store.gather()
    for i in (1, 2, 3, 4, 5, 7):
        fake.on(title(i), verdict("relevant" if i % 2 else "not_relevant"))
    gate_ = {"screen": [{**Q, "fetch_on": ["relevant"]}]}
    plan, _ = plan_with(tmp_path, "a", gate_)
    first = run_triage(store, plan)
    before = store.docs()
    assert first.counts["asked_pairs"] == len(before) == 6

    sizes: list[int] = []
    real = gate.row_batches

    def counted(table: pa.Table) -> Iterator[list[Row]]:
        for rows in real(table):
            sizes.append(len(rows))
            yield rows

    monkeypatch.setattr(gate, "BATCH_ROWS", 2)
    monkeypatch.setattr(gate, "row_batches", counted)
    # an edit that matches nothing: a new sql clause moves the gate digest, so every row is config-stale
    edited = {"screen": [{"sql": "doc_id = 'no-such-document'"}, {**Q, "fetch_on": ["relevant"]}]}
    plan2, _ = plan_with(tmp_path, "b", edited)
    rec = run_triage(store, plan2)
    after = store.docs()
    assert rec.config_digest != first.config_digest
    assert (rec.counts["head"], rec.counts["scoped"], rec.counts["asked_pairs"]) == (6, 6, 0)
    assert len(fake.requests) == 6  # nothing asked again: every answer carries
    assert sizes and max(sizes) <= 2
    for d, r in after.items():
        assert ann(r)["config_digest"] == rec.config_digest
        assert _outcome(r) == _outcome(before[d]), d
    assert rec.scope_complete is True
    assert sorted(after) == sorted(before) and sid(7) in after


#: Store cases of the triage tests (gather + triage over synthetic sources): the first one checks the standing
#: rows the stage appends after the head's order, the others the ask, the prior evidence, title basis and signals.
CASES: list[Callable[..., None]] = [
    tst.test_gate_edit_carries_acquired_rows_and_x14_fold_agree,
    tst.test_merge_carries_the_absorbed_fetch_over_the_survivors_skip,
    tq.test_question_arms_evidence_and_prior_rerun,
    tq.test_due_transient_is_reasked_without_a_gate_edit_and_evidence_carries,
    tq.test_title_basis_asks_no_basis_rows_with_topics_labels_and_reasks_on_an_abstract,
    tsig.test_fetched_documents_are_asked_and_decide_as_before,
]


@pytest.mark.slow
@pytest.mark.parametrize("case", CASES, ids=[c.__name__ for c in CASES])
def test_store_cases_hold_with_one_row_batches(case: Callable[..., None], request: pytest.FixtureRequest,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    sizes: list[int] = []
    real = gate.row_batches

    def counted(table: pa.Table) -> Iterator[list[Row]]:
        for rows in real(table):
            sizes.append(len(rows))
            yield rows

    monkeypatch.setattr(gate, "BATCH_ROWS", 1)
    monkeypatch.setattr(gate, "row_batches", counted)
    case(**{name: request.getfixturevalue(name) for name in inspect.signature(case).parameters})
    assert sizes and set(sizes) == {1}  # the stage ran, a row at a time
