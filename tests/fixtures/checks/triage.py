"""The failing fixtures of the triage checks (T2.9): V5, X13, X14, X19, X20, hooked into
``FIXTURES`` at the end of ``tests/fixtures/checks/__init__.py``.

The clean store's F2 triage part holds docs 0-3 (``fetch``, ``wanted_by = [screen]``); doc 3 is
in F2 only (not in the F3 acquire part), so editing it there leaves X15 green. X14 recomputes
under the build's plan (``dev-v2``), whose M2-M3 gate fetches every row with an abstract.
"""

from __future__ import annotations

import copy

from stack.checks.digests import row_fields_digest
from tests.fixtures.checks import World, _stamp
from tests.synth.rows import _ann


def v5(w: World) -> None:  # `screen` in documents (Appendix B's example), in the triage part
    w.doc("F2", 3)["annotations"]["screen"] = _ann("screen", {
        "verdict": "relevant", "basis_role": "full_text", "buckets": [], "quotes": [],
        "off_topic_reason": None, "confidence": 0.9, "reasoning": "r", "adjudication_tier": 1,
        "tier1_verdict": "relevant", "tier1_model": "m"}, basis_kind="text", run=2)


def x13(w: World) -> None:  # a fetched `skip` row (Appendix B's example)
    # Doc 0 lost its abstract before triage, so the dev gate skips it (X14 stays green), yet the
    # F3 acquire part fetched its full_text. The same fields in F1, F2, F3 (X15, X17 green).
    for key in ("F1", "F2", "F3"):
        r = w.doc(key, 0)
        r["metadata"]["abstract"] = None
        r["metadata"]["fields_digest"] = row_fields_digest(r)
        t = r["annotations"]["triage"]
        if t is not None:
            t["basis_digest"] = r["metadata"]["fields_digest"]
            t["payload"].update(decision="skip", wanted_by=[], questions=[])


def x13_gated(w: World, *, mark: str | None = None) -> None:
    """Not a fixture (T3.6-f4): doc 0 as :func:`x13` (triage ``skip``), but F3 carries it the way
    ``carry`` writes a gated row (D108): ``full_text`` and ``xml`` roles ``skipped[gated]``,
    ``attempts = 0``, ``last_attempt_at`` NULL, no snapshot, state ``acquired``. Clean for X13.
    ``mark`` makes the ``full_text`` role record a fetch anyway: ``"attempt"`` sets
    ``last_attempt_at``, ``"snapshot"`` points it at a raw snapshot minted in F3 (both X13-red)."""
    x13(w)
    c = w.doc("F3", 0)["content"]
    snap = c["snapshots"][0]
    gated = {"status": "skipped", "error_class": "gated", "error": "triage: skip", "attempts": 0,
             "first_attempt_at": None, "last_attempt_at": None, "last_outcome": None,
             "tried_urls": [], "last_source_id": None, "snapshot_id": None, "lane": "oa"}
    role = c["roles"][0]
    role.update(gated)
    c["roles"].append({**copy.deepcopy(role), "content_role": "xml"})
    c["snapshots"] = []
    c.update(current_snapshot_id=None, current_source_id=None, current_role=None,
             current_text_sha256=None, current_text_status="no_raw", current_text_media=None,
             current_revision=None, revisions=[])
    if mark == "attempt":
        role.update(first_attempt_at=snap["captured_at"], last_attempt_at=snap["captured_at"])
    elif mark == "snapshot":
        c["snapshots"] = [snap]
        role["snapshot_id"] = snap["snapshot_id"]
    elif mark is not None:
        raise ValueError(f"unknown mark {mark!r}")


def x14(w: World) -> None:  # `wanted_by` without a positive clause (Appendix B's example)
    w.doc("F2", 3)["annotations"]["triage"]["payload"]["wanted_by"] = ["ghost", "screen"]


def x19(w: World) -> None:  # `acquired → triaged` (Appendix B's example), by the F5 gather part
    row = _stamp(copy.deepcopy(w.doc("F3", 0)), 5)
    row["state"] = "triaged"
    w.rows("F5").append(row)


def x20(w: World) -> None:  # a triaged row that says its basis is not current
    w.doc("F2", 3)["annotations"]["triage"]["is_basis_current"] = False


FIXTURES_TRIAGE = {"V-annotations": v5, "gate_obeyed": x13, "gate_union": x14,
                   "state_monotone": x19, "triage_current": x20}
