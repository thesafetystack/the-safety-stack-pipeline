"""TEST-ONLY stand-in ``triage`` (T5.1a build scenario): every unmasked ``identified`` row of the
pinned ``documents`` head becomes ``triaged`` with decision ``skip`` (verdict ``irrelevant``, basis
= ``fields_digest``) in a ``documents`` part, plus one ``triage_evidence`` row each. Nothing in
scope: no output. Records how many prior ``triage_evidence`` rows (``--in data/triage_evidence/<series>/``)
it saw (``counts.prior_evidence``)."""

from __future__ import annotations

import copy
import datetime as dt

from stack.annotate.evidence import TRIAGE_EVIDENCE_SPEC, with_digests
from stack.stage_runner import StageContext, StageError, StageResult
from tests.synth.rows import table, triage_evidence_row
from tests.synth.world import SOURCES, _triage

_I = {s.source_id: s.i for s in SOURCES.values()}


def run(ctx: StageContext) -> StageResult:
    if "documents" not in ctx.inputs:
        raise StageError("stand-in triage needs --input documents")
    ctx.counts["prior_evidence"] = ctx.view("triage_evidence").num_rows if "triage_evidence" in ctx.prior else 0
    now = dt.datetime.now(dt.UTC)
    rows, evs = [], []
    for r in ctx.view("documents").to_pylist():
        if r["state"] != "identified" or r["identity"]["is_masked"]:
            continue
        i = _I[r["doc_id"]]
        out = copy.deepcopy(r)
        ann = _triage("skip", "irrelevant", i)
        ann.update(basis_digest=r["metadata"]["fields_digest"], decided_at=now)
        out["state"], out["annotations"]["triage"] = "triaged", ann
        out["run_id"], out["updated_at"] = ctx.run_id, now
        rows.append(out)
        # digests recomputed: V8 checks triage_evidence (D166 (C))
        evs.append(with_digests(triage_evidence_row(i, 0, doc_id=r["doc_id"], source_id=r["doc_id"],
                                                    basis_digest=ann["basis_digest"], decided_at=now,
                                                    run_id=ctx.run_id, verdict="irrelevant"),
                                TRIAGE_EVIDENCE_SPEC))
    if rows:
        ctx.emit_part("documents", table("documents", rows), ["annotations"])
        ctx.emit_full("triage_evidence", table("triage_evidence", evs), ["annotations"])
    ctx.counts["triaged"] = len(rows)
    return StageResult(scope_complete=True, rows_touched=len(rows))
