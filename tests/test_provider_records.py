"""Provider records as in-band entries (D522): the role ``provider_record`` is metadata, not content.

The vocabulary keeps it apart (``RECORD_ROLES``, in ``NEVER_TEXT_ROLES``, not in ``CONTENT_ROLES`` or
``POINTER_RANK``); ``mint_entry`` accepts it as a raw blob only; ``acquire.wants`` refuses it. Every
consumer of in-band entries ignores it: a row with a record next to its content gives the same answer
as the row without one — acquire (``mint_inband``, the scope, carry, pointer, convert, quality), the
gate and triage excerpt (``inband_excerpt_sha256``, ``fields_digest``), the content check V6, the
screen/affiliation basis, and release's licence rules.
"""

from __future__ import annotations

import copy
import datetime as dt
from pathlib import Path
from typing import Any

import pytest

from stack.checks.content import _content_problems
from stack.checks.digests import inband_excerpt_sha256, row_fields_digest
from stack.collect.inband import RECORD_ROLE, mint_entry, record_bytes
from stack.fold.release import ReleaseConfig, apply_rules
from stack.annotate.basis import basis_of
from stack.gate import gate_columns
from stack.plan import Plan
from stack.schemas.vocab import (
    CONTENT_ROLES,
    INBAND_ROLES,
    NEVER_TEXT_ROLES,
    POINTER_RANK,
    RECORD_ROLES,
    STRUCTURED_ROLES,
)
from stack.store.blobs import open_blobs
from tests.fakes.lanes import TrapLane
from tests.synth.rows import AT, CONVERT_PIN, document_row, rid, sid
from workflows.acquire import lanes
from workflows.acquire.inband import mint_inband
from workflows.acquire.lanes import LaneEnv
from workflows.acquire.main import acquire_rows
from workflows.acquire.scope import reasons

Row = dict[str, Any]
NOW = AT + dt.timedelta(hours=30)
RECORD = {"id": "https://openalex.org/W1", "cited_by_count": 7, "title": "Paper 1",
          "abstract_inverted_index": {"Scalable": [0], "oversight": [1]}}


def with_record(row: Row, blob: bytes = b"") -> Row:
    """``row`` (a documents row) with a ``provider_record`` entry for its anchor appended."""
    out = copy.deepcopy(row)
    data = blob or record_bytes(RECORD)
    out["inband"]["entries"].append({
        "source_id": out["identity"]["anchor_source_id"], "content_role": RECORD_ROLE,
        "raw": {"sha256": _digest(data), "bytes": len(data), "media_type": "application/json"}, "text": None,
        "captured_at": AT})
    return out


def _digest(data: bytes) -> str:
    from stack.core.hashes import raw_digest

    return raw_digest(data)


def sans_inband(rows: list[Row]) -> list[Row]:
    return [{k: v for k, v in r.items() if k != "inband"} for r in rows]


# ---------------------------------------------------------------- the vocabulary and the writers' seam
def test_record_roles_are_apart_from_content() -> None:
    assert RECORD_ROLES == ("provider_record",)
    assert not set(RECORD_ROLES) & (set(CONTENT_ROLES) | set(POINTER_RANK) | set(STRUCTURED_ROLES))
    assert set(RECORD_ROLES) <= NEVER_TEXT_ROLES
    assert INBAND_ROLES == CONTENT_ROLES + RECORD_ROLES


def test_mint_entry_takes_a_record_as_raw_only(tmp_path: Path) -> None:
    blobs = open_blobs(tmp_path)
    raw = record_bytes(RECORD)
    assert raw == (b'{"abstract_inverted_index":{"Scalable":[0],"oversight":[1]},"cited_by_count":7,'
                   b'"id":"https://openalex.org/W1","title":"Paper 1"}')  # sorted keys, compact
    assert record_bytes(dict(reversed(list(RECORD.items())))) == raw  # key order does not matter
    e = mint_entry(blobs, sid(1), RECORD_ROLE, raw=raw, media_type="application/json", captured_at=AT)
    assert e is not None and e["text"] is None and blobs.get(e["raw"]["sha256"]) == raw
    assert e["raw"] == {"sha256": _digest(raw), "bytes": len(raw), "media_type": "application/json"}
    with pytest.raises(ValueError, match="no text"):
        mint_entry(blobs, sid(1), RECORD_ROLE, text="x" * 100, media_type="text/plain", captured_at=AT)
    with pytest.raises(ValueError, match="not in"):
        mint_entry(blobs, sid(1), "record", raw=raw, media_type="application/json", captured_at=AT)


def test_wants_refuses_a_record_role() -> None:
    base = {"plan": "t", "schema_version": 2, "series": "main", "stages": [{"stage": "gather"}]}
    Plan.model_validate({**base, "acquire": {"wants": {"openalex": ["full_text"]}}})
    with pytest.raises(ValueError, match="record role, not content"):
        Plan.model_validate({**base, "acquire": {"wants": {"openalex": ["full_text", "provider_record"]}}})


# ---------------------------------------------------------------- acquire
def env(tmp_path: Path) -> LaneEnv:
    return LaneEnv(run_id=rid(50), now=NOW, blobs=open_blobs(tmp_path), convert_pin=CONVERT_PIN, counts={})


def test_mint_inband_and_scope_skip_a_record(tmp_path: Path) -> None:
    """With its blob present, a record would mint a snapshot and a role if it were content."""
    e = env(tmp_path)
    e.blobs.put(record_bytes(RECORD))
    row = with_record(document_row(1, state="triaged"))
    content = {"snapshots": [], "roles": []}
    assert mint_inband(row, content, e, lambda: "x") == 0
    assert content == {"snapshots": [], "roles": []} and "inband_minted" not in e.counts
    acquired = document_row(1, state="acquired")
    acquired["inband"]["entries"] = []  # no content entry: nothing to mint
    assert "inband" not in reasons(acquired, NOW, True)
    assert reasons(with_record(acquired), NOW, True) == reasons(acquired, NOW, True)


@pytest.mark.parametrize("state", ["triaged", "acquired"])
def test_acquire_answers_the_same_with_a_record(tmp_path: Path, state: str) -> None:
    """The whole acquire body (carry, inband, quality, point; no fetch: the lanes trap) gives the
    same rows and counts for a row with a record as for the row without one."""
    row = document_row(1, state=state)
    row["identity"]["members"][0]["channel"] = "openalex"  # not in wants: no fetch role, no lane
    out = []
    for rows in ([row], [with_record(row)]):
        e = env(tmp_path)
        e.blobs.put(record_bytes(RECORD))
        with lanes.using({n: TrapLane(n) for n in lanes.FETCH_LANES}):
            res = acquire_rows(rows, e, wants={"arxiv": ["full_text"]}, channel_group={"openalex": "paper"}.get)
        out.append((sans_inband(res.rows), e.counts))
    assert out[0] == out[1]
    assert out[1][1].get("carry.no_wants") == (1 if state == "triaged" else out[0][1].get("carry.no_wants"))


# ---------------------------------------------------------------- gate, triage, checks, annotate, release
def test_excerpt_fields_digest_and_gate_ignore_a_record() -> None:
    row = document_row(1, state="acquired")
    rec = with_record(row)
    anchor = row["identity"]["anchor_source_id"]
    assert inband_excerpt_sha256(anchor, rec["inband"]["entries"]) == \
        inband_excerpt_sha256(anchor, row["inband"]["entries"]) is not None
    alone = with_record(document_row(2))
    assert inband_excerpt_sha256(alone["identity"]["anchor_source_id"], alone["inband"]["entries"]) is None
    assert row_fields_digest(rec) == row_fields_digest(row)
    assert gate_columns(rec, {"arxiv": "paper"}) == gate_columns(row, {"arxiv": "paper"})


def test_content_check_and_annotate_basis_ignore_a_record() -> None:
    row = document_row(1, state="acquired")
    assert _content_problems(with_record(row)) == _content_problems(row) == []
    no_text = document_row(2, state="acquired")
    c = no_text["content"]
    c.update(current_snapshot_id=None, current_text_status="no_raw", current_text_sha256=None,
             current_source_id=None, current_role=None, snapshots=[])
    no_text["inband"]["entries"] = []
    assert _content_problems(with_record(no_text)) == []  # a record is not text left behind
    for kind in ("text", "fields"):
        assert basis_of(with_record(row), kind) == basis_of(row, kind)


def test_release_rules_ignore_a_record() -> None:
    rules = {c: {"rows": "keep", "text": "ship", "abstract": "keep", "excerpt_cap_chars": 0}
             for c in ("open", "public_web", "restricted", "api_tos", "api_abstract", "shadow")}
    cfg = ReleaseConfig.model_validate({"name": "t", "licence_rules": rules})
    row = document_row(1, state="acquired")
    kept, dropped, ship = apply_rules([with_record(row)], cfg)
    kept0, dropped0, ship0 = apply_rules([row], cfg)
    assert (sans_inband(kept), dropped, ship) == (sans_inband(kept0), dropped0, ship0)
    assert ship == [row["content"]["current_text_sha256"]]  # the record's blob never ships
    assert "inband" in cfg.column_drops
