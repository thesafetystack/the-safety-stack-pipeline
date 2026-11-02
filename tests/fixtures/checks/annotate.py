"""Failing fixtures of V7 ``V-wf``, V8 ``V-evidence``, X6 ``policy_pins``, X7 ``basis_currency``
and X8 ``selection_recompute`` (T4.3a, :mod:`stack.checks.annotate`), merged into
:data:`tests.fixtures.checks.FIXTURES`.

V7 and V8 edit the clean world's ``screen`` / ``screen_evidence`` files (run 7; the clean
world's evidence rows carry recomputing digests, ``stack.annotate.evidence.with_digests``). Those files name no
``corpus`` input and the clean build does not name them, so the X checks do not look at them.
V8 also covers ``triage_evidence`` (D166 (C)): :func:`triage_evidence` adds the clean world's
triage run's (F2, run 2) evidence file as the asker writes it, :func:`v8_triage` breaks one
row's ``result_digest`` (``tests/test_checks_annotate.py`` checks both).
X6-X8 add an **annotated** pair (run 30) the way the framework writes it: evidence from
:func:`stack.annotate.evidence.evidence_row` and ``screen`` rows from
:func:`stack.annotate.select.select` over a ``fold.corpus`` file of its own (run 29: ``has_text``
over the view at F5; doc 2, without text, is not in it) and the build record naming the pair and
that corpus (``corpus_input``), so the X checks take it; each fixture then breaks one thing. The
framework-store versions of these faults are ``tests/test_checks_annotate.py``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from stack.annotate.basis import basis_of
from stack.annotate.evidence import TRIAGE_EVIDENCE_SPEC, evidence_row
from stack.annotate.select import select
from stack.fold import WRITER as FOLD_WRITER
from stack.fold import filter_rows
from stack.query import HAS_TEXT
from stack.schemas.payloads.registry import payload
from tests.fixtures.checks import FileSpec, T, World, fid, view_rows
from tests.synth.rows import dg, eid, rid, sid, table

Row = dict[str, Any]
RUN = 30
#: The annotated pair's own ``fold.corpus`` file: ``has_text`` over the view at F5, header
#: ``inputs`` naming F5 (the clean corpus file names a placeholder input, so it cannot be the
#: build's ``corpus_input`` without X2/X9 findings).
CORPUS_RUN = 29
SCREEN = payload("screen")
#: The fixture header's ``config_digest`` (``tests.synth.rows.header`` fixes it), used as the
#: annotated pair's workflow config digest so X8's recomputation reads it back.
CONFIG = dg("config")


def _corpus_rows(root: Path) -> list[Row]:
    return filter_rows(table("corpus", view_rows(root, fid(5))), HAS_TEXT).to_pylist()


def _annotated_rows(root: Path) -> tuple[list[Row], list[Row]]:
    corpus = _corpus_rows(root)
    evidence = []
    for d in corpus:
        b = basis_of(d, "text")
        if b.digest is None:
            continue
        i = int(d["identity"]["members"][0]["key_value"].split(".")[1])
        evidence.append(evidence_row(
            SCREEN, doc_id=d["doc_id"], source_id=b.source_id, basis_digest=b.digest, config_digest=CONFIG,
            run_id=rid(RUN), now=T(RUN), status="ok", llm=None, origin="computed", attempts=1,
            first_attempt_at=None, supersedes=None, evidence_id=eid(100 + i),
            payload={"verdict": "relevant", "basis_role": "full_text", "buckets": ["evals"],
                     "quotes": [{"text": "we evaluate", "offset": 10}], "confidence": 0.8}))
    rows = select(SCREEN, corpus, evidence, [], config_digest=CONFIG, run_id=rid(RUN), now=T(RUN))
    return rows, evidence


def _annotate(w: World) -> tuple[FileSpec, FileSpec]:
    """Add the annotated pair (module docstring) and name it in the build."""
    cache: dict[str, tuple[list[Row], list[Row]]] = {}

    def rows_of(i: int) -> Callable[[Path], list[Row]]:
        def derive(root: Path) -> list[Row]:
            if "x" not in cache:
                cache["x"] = _annotated_rows(root)
            return cache["x"][i]
        return derive

    corpus = FileSpec("corpus@29", "corpus", CORPUS_RUN,
                      {"stage": "fold.corpus", "writer": FOLD_WRITER, "fold_config": "corpus",
                       "filter": HAS_TEXT, "annotations_folded": []},
                      derive=_corpus_rows, raw_header={"inputs": json.dumps({"documents": fid(5)})})
    inputs = {"inputs": json.dumps({"corpus": fid(CORPUS_RUN, "corpus")})}
    screen = FileSpec("screen@30", "screen", RUN, {"stage": "annotate.screen", "basis_kind": "text"},
                      derive=rows_of(0), raw_header=dict(inputs))
    evidence = FileSpec("screen_evidence@30", "screen_evidence", RUN, {"stage": "annotate.screen"},
                        derive=rows_of(1), raw_header=dict(inputs))
    w.files += [corpus, screen, evidence]
    assert w.build is not None
    w.build["files"]["screen"] = {"file_id": fid(RUN, "screen")}
    w.build["files"]["screen_evidence"] = {"file_id": fid(RUN, "screen_evidence")}
    w.build["files"]["corpus_input"] = {"file_id": fid(CORPUS_RUN, "corpus")}  # what it read (X2)
    return screen, evidence


def v7(w: World) -> None:  # `ok` without evidence (Appendix B's example)
    w.rows("screen")[0]["evidence_id"] = None


def v8(w: World) -> None:  # an evidence row edited after the fact: its result_digest is stale
    w.rows("screen_evidence")[0]["verdict"] = "not_relevant"


def x6(w: World) -> None:  # a dev prompt: an ai_check answer under another prompt than the plan pins
    screen, evidence = _annotate(w)

    def dev_prompt(rows: list[Row]) -> None:
        rows[0].update(origin="ai_check", prompt_name="screen-dev", template_digest=dg("dev template"))

    def ai_origin(rows: list[Row]) -> None:  # the <wf> row selects it: same origin (X8 stays green)
        rows[0]["origin"] = "ai_check"

    evidence.edits.append(dev_prompt)
    screen.edits.append(ai_origin)


def x7(w: World) -> None:  # current text marked not current
    screen, _ = _annotate(w)
    screen.edits.append(lambda rows: rows[0].update(is_basis_current=False))


def x8(w: World) -> None:  # a hand-edited screen row
    screen, _ = _annotate(w)
    screen.edits.append(lambda rows: rows[0].update(verdict="not_relevant"))


TRIAGE_RUN = 2  # the clean world's triage run (F2, a documents part)


def triage_evidence(w: World) -> FileSpec:
    """Add the clean world's triage run's ``triage_evidence`` file: one ``safety_relevant``
    answer per triaged document 0-1, built as ``workflows/triage/ask.py`` builds it
    (:func:`~stack.annotate.evidence.evidence_row` under ``TRIAGE_EVIDENCE_SPEC``, basis = the
    row's ``fields_digest``)."""
    rows = [evidence_row(
        TRIAGE_EVIDENCE_SPEC, doc_id=sid(i), source_id=sid(i),
        basis_digest=w.doc("F2", i)["metadata"]["fields_digest"],
        config_digest=dg(("question", "safety_relevant")), run_id=rid(TRIAGE_RUN), now=T(TRIAGE_RUN),
        status="ok", llm=None, origin="ai_check", attempts=1, first_attempt_at=None, supersedes=None,
        evidence_id=eid(200 + i),
        payload={"question": "safety_relevant", "verdict": "yes", "confidence": 0.9,
                 "reasoning": "mentions evaluations"}) for i in range(2)]
    spec = FileSpec("triage_evidence", "triage_evidence", TRIAGE_RUN,
                    {"stage": "triage", "blocks_written": ["annotations"]}, rows)
    w.files.append(spec)
    return spec


def v8_triage(w: World) -> None:  # a triage answer edited after the fact: its result_digest is stale
    triage_evidence(w).rows[0]["verdict"] = "no"  # type: ignore[index]


def annotated_clean(w: World) -> None:
    """Not a fixture: the annotated pair unbroken (``tests/test_checks_annotate.py`` checks it is
    green)."""
    _annotate(w)


#: Merged into ``tests.fixtures.checks.FIXTURES``.
FIXTURES_ANNOTATE: dict[str, Callable[[World], None]] = {
    "V-wf": v7,
    "V-evidence": v8,
    "policy_pins": x6,
    "basis_currency": x7,
    "selection_recompute": x8,
}
