"""X16 ``lookup_gate_current`` (Appendix B; D20; T4.2): a lookup's exclusion of triage-``skip``
documents holds only under the build's gate.

A lookup (``collect.<provider>-lookup``) may leave out documents whose triage decision is
``skip`` (§3.1's collect row: "optional exclusion of ``skip`` under X16"); the batch's
``coverage`` row then records in ``gate_digest`` the gate those decisions were made under (D20;
NULL = nothing was excluded). Lookups run before ``triage`` in a build, so they read decisions of
the previous gate: after a ``gate:`` edit (a new consumer, a wider ``fetch_on``) a ``skip`` may no
longer be one, and an exclusion made under the old gate is stale (Appendix B's failing example).

**Rule.** Every ``coverage`` row written by the reference build's runs that has a non-NULL
``gate_digest`` carries the build's gate digest: the build record's ``gate_digest``, else the
``gate_digest`` of the plan file the record names (:func:`stack.gate.resolve_plan`). "Written by
the build's runs" = ``run_id`` in the record's ``runs``; a record without ``runs`` (a hand-made
one) uses the runs that wrote the ``coverage`` heads it names (``files``, D17).
``skipped[no_builds]`` without a build record, ``skipped[no_gate]`` when neither gives a digest.

Status (2026-10-12): no lookup excludes yet (``workflows/fetch/metadata/main.py`` writes
``gate_digest`` NULL), so on real stores the check holds vacuously; the rule is pinned by
fixtures (``tests/fixtures/checks/lookup.py``, ``tests/test_triage_questions.py``).
"""

from __future__ import annotations

from stack import gate
from stack.checks import Finding, register
from stack.checks.chain import SkipCheck, StoreView

_EXAMPLES = 5


def _build_gate_digest(view: StoreView) -> str:
    assert view.build is not None
    want = view.build.get("gate_digest")
    if want:
        return str(want)
    plan = gate.resolve_plan(str(view.build.get("plan") or ""))
    if plan is None:
        raise SkipCheck("no_gate")
    return plan.gate_digest


@register("X16", "lookup_gate_current", scope="store")
def lookup_gate_current(view: StoreView) -> list[Finding]:
    """X16 (module docstring)."""
    heads = view.build_heads()  # SkipCheck('no_builds')
    want = _build_gate_digest(view)
    assert view.build is not None
    runs = set(view.build.get("runs") or ())
    if not runs:
        for fid in heads.values():
            df = view.file(fid)
            if df is not None and df.spec is not None and df.spec.kind == "coverage":
                runs.add(df.header.get("run_id", ""))
    bad: list[str] = []
    for df in view.files:
        if df.spec is None or df.spec.kind != "coverage":
            continue
        tbl = view.table(df)
        if tbl is None:
            continue  # S1's
        for r in tbl.select(["channel", "query_digest", "run_id", "gate_digest"]).to_pylist():
            if r["run_id"] in runs and r["gate_digest"] is not None and r["gate_digest"] != want:
                bad.append(f"{df.file_id}: {r['channel']} batch {r['query_digest'][:12]} excluded "
                           f"under gate {r['gate_digest'][:12]}, build gate {want[:12]}")
    if not bad:
        return []
    return [Finding("lookup_gate_current", "hard",
                    f"{len(bad)} lookup batch(es) excluded documents under another gate",
                    len(bad), tuple(bad[:_EXAMPLES]))]
