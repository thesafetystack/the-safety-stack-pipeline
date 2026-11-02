"""In-band capture (``stack/collect/inband.py``) against v3's decision table, mined into
``tests/fixtures/v3_scenarios/collect_inband.yaml`` (15 rows; v3 ran them against Postgres).

What carries over unchanged: the noise floor (``MIN_CHARS`` after normalisation), the
paragraph-preserving normalisation, idempotent re-offers, and the sink's tallies and breaker.
What changes with the store (each row's test says how): a mint is a blob plus an
``INBAND_ENTRY`` instead of a ``partial`` snapshot row, so ``refused_when_full_snapshot_exists``
becomes "a mint never touches another role's entry" (role before currency, §5.5); the
``fetch_meta.via`` validation becomes the ``content_role`` vocabulary; the DB probe
``sources_without_snapshots`` is retired and fails loudly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from stack.collect import inband as inband_mod
from stack.collect.inband import (
    MIN_CHARS,
    InbandSink,
    mint_entry,
    normalize,
    offer_digests,
    record_bytes,
)
from stack.core.hashes import text_digest
from stack.store.blobs import BlobStore, open_blobs

TABLE = yaml.safe_load((Path(__file__).parent / "fixtures" / "v3_scenarios" / "collect_inband.yaml")
                       .read_text())
SID = "s0000000000ab"
LONG = "A sufficiently long in-band abstract about scalable oversight."


def rows(section: str) -> list[Any]:
    return [pytest.param(r, id=r["id"]) for r in TABLE[section]]


def test_table_has_fifteen_rows() -> None:
    assert sum(len(TABLE[s]) for s in ("mint", "normalization", "batch_prefilter", "import_via",
                                       "sink")) == 15


@pytest.fixture
def blobs(tmp_path: Path) -> BlobStore:
    return open_blobs(tmp_path)


@pytest.mark.parametrize("row", rows("mint"))
def test_mint(row: dict[str, Any], blobs: BlobStore) -> None:
    sink = InbandSink(blobs)
    text = row.get("text", LONG).replace("<nonce>", "n1")
    if row["id"] == "fresh_source_minted":
        e = sink.offer(SID, "abstract", text=text, media_type="text/plain",
                       captured_at=row["observed_at"])
        assert e is not None and e["raw"] is None  # raw group null (C33)
        assert e["text"] == {"sha256": text_digest(normalize(text)), "bytes": len(normalize(text).encode()),
                             "media_type": "text/plain"}
        assert blobs.get(e["text"]["sha256"]).decode() == normalize(text)
        assert e["captured_at"].isoformat().startswith("2024-01-15")
        assert sink.counts["inband_minted"] == 1
    elif row["id"] == "refused_when_full_snapshot_exists":
        # v4: the body's entry is never displaced; the abstract gets its own role entry
        body = mint_entry(blobs, SID, "post", raw=b"<p>" + LONG.encode() * 3 + b"</p>",
                          media_type="text/html", captured_at="2024-01-01")
        assert body is not None
        carried = {"post": dict(body)}
        e = sink.offer(SID, "abstract", text=text, media_type="text/plain", carried=carried,
                       captured_at=row["observed_at"])
        assert e is not None and e["content_role"] == "abstract"
        assert carried == {"post": body}
    elif row["id"] == "second_call_is_noop":
        first = sink.offer(SID, "abstract", text=LONG, media_type="text/plain")
        assert first is not None
        second = sink.offer(SID, "abstract", text=LONG, media_type="text/plain",
                            carried={"abstract": first})
        assert second is first  # the carried entry stands, first captured_at kept
        assert (sink.counts["inband_minted"], sink.counts["inband_skipped"]) == (1, 1)
    elif row["id"] == "short_text_rejected":
        assert len(text) < MIN_CHARS
        assert sink.offer(SID, "abstract", text=text, media_type="text/plain") is None
        assert sink.counts["inband_minted"] == 0
        assert not (blobs.store.root / "blobs").exists()
    else:
        pytest.fail(f"unmapped row {row['id']}")


@pytest.mark.parametrize("row", rows("normalization"))
def test_normalization(row: dict[str, Any]) -> None:
    assert normalize(row["input"]) == row["expect"]


def test_surrogates_in_a_collectors_text_and_record(blobs: BlobStore) -> None:
    """D586: a collector's text or JSON record holding a lone surrogate (a provider's ``\\ud83d``
    escape without its partner) mints with U+FFFD in its place instead of raising
    ``UnicodeEncodeError``; the text's digest is that of the repaired text, and a text without
    surrogates normalises as before."""
    lone = LONG + " \ud83d done"
    entry = mint_entry(blobs, SID, "abstract", text=lone, media_type="text/plain", captured_at="2026-10-15")
    assert entry is not None
    assert blobs.get(entry["text"]["sha256"]).decode("utf-8") == LONG + " \ufffd done"
    assert offer_digests(lone, None) == (None, entry["text"]["sha256"])
    assert normalize(LONG) == LONG
    assert record_bytes({"title": "x \udc00 y"}) == '{"title":"x \ufffd y"}'.encode()
    assert record_bytes({"title": "x"}) == b'{"title":"x"}'


def test_raw_and_text_entry_names_the_texts_media_type(blobs: BlobStore) -> None:
    """D690: an entry's ``raw`` and ``text`` are BLOBs with their own sizes and media types; an offer of
    both must name the text's (``text_media_type``), else the mint refuses."""
    raw = b'{"id": "x", "text": "' + LONG.encode() + b'"}'
    with pytest.raises(ValueError, match="text_media_type"):
        mint_entry(blobs, SID, "post", text=LONG, raw=raw, media_type="application/json", captured_at="2026-10-15")
    e = mint_entry(blobs, SID, "post", text=LONG, raw=raw, media_type="application/json",
                   text_media_type="text/markdown", captured_at="2026-10-15")
    assert e is not None
    assert e["raw"] == {"sha256": blobs.put(raw), "bytes": len(raw), "media_type": "application/json"}
    assert e["text"] == {"sha256": text_digest(LONG), "bytes": len(LONG.encode()), "media_type": "text/markdown"}


@pytest.mark.parametrize("row", rows("batch_prefilter"))
def test_db_prefilter_is_retired(row: dict[str, Any]) -> None:
    """The prefilter answered "which of these sources have no snapshot" over SQL; the harness
    carries each source's prior entries instead. The v3 names (``sources_without_snapshots``,
    ``store_inband_text``) went with their last caller, ``run_v5``, at T4.5."""
    assert "batch" in row  # the v3 fixture rows stay mapped
    assert not hasattr(inband_mod, "sources_without_snapshots")
    assert not hasattr(inband_mod, "store_inband_text")


@pytest.mark.parametrize("row", rows("import_via"))
def test_role_vocabulary(row: dict[str, Any], blobs: BlobStore) -> None:
    """v3 validated ``fetch_meta.via``; an entry has no ``via`` (acquire stamps ``inband`` on
    the snapshot), so the gate moves to the ``content_role`` vocabulary."""
    role = {"inband_accepted": "abstract"}.get(row["id"], row["fetch_meta"]["via"])
    if row["expect"] == "accepted":
        assert mint_entry(blobs, SID, role, text=LONG, media_type="text/plain",
                          captured_at="2024-01-01") is not None
    else:
        with pytest.raises(ValueError, match="content_role"):
            mint_entry(blobs, SID, role, text=LONG, media_type="text/plain",
                       captured_at="2024-01-01")


class ScriptedBlobs:
    """A blob store whose ``put`` follows a script of ``error <msg>`` / ``ok <digest>`` steps
    (``ok`` returns the scripted digest for every put of that offer)."""

    def __init__(self, script: list[str]) -> None:
        self.script = list(script)

    def put(self, data: bytes) -> str:
        step = self.script.pop(0) if self.script else "ok " + "0" * 64
        kind, _, arg = step.partition(" ")
        if kind == "error":
            raise OSError(arg.strip("\"") or "blob store down")
        return arg if len(arg) == 64 else "f" * 64


@pytest.mark.parametrize("row", rows("sink"))
def test_sink(row: dict[str, Any], blobs: BlobStore) -> None:
    rid = row["id"]
    keys = ("minted", "backfill", "skipped", "absent", "errors")

    def counts(sink: InbandSink) -> dict[str, int]:
        return {k: sink.counts[f"inband_{k}"] for k in keys}

    if rid in ("mint_path_counts", "absent_text", "skip_when_snapshot_exists"):
        sink = InbandSink(blobs)
        carried: dict[str, dict[str, Any]] = {}
        for off in row["offers"]:
            text = {"long": LONG}.get(off["text"], off["text"])
            source = SID if off["source"] in ("s", "fresh_a") else "s0000000000ac"
            e = sink.offer(source, "abstract", text=text, media_type="text/plain",
                           carried=carried if source == SID else None,
                           backfill=bool(off.get("backfill")))
            if off["expect"] == "minted":
                assert e is not None
                if source == SID:
                    carried["abstract"] = e
            elif rid == "skip_when_snapshot_exists":
                assert e is carried["abstract"]  # skipped: the carried entry, nothing minted
            else:
                assert e is None
        assert counts(sink) == row["expect_counts"]
        assert sink.disabled is bool(row.get("expect_disabled", False))
    elif rid == "breaker_trips_on_third_consecutive_error":
        sink = InbandSink(ScriptedBlobs(row["store_script"]))
        for off in row["offers"]:
            text = {"long": LONG}.get(off["text"], off["text"])
            assert sink.offer(SID, "abstract", text=text, media_type="text/plain") is None
            if "expect_counts" in off:
                assert counts(sink) == off["expect_counts"]
            if "expect_disabled" in off:
                assert sink.disabled and sink.first_error == off["expect_disabled"]
        assert (sink.errors, sink.absent, sink.first_error) == (3, 0, row["expect_final"]["disabled"])
    elif rid == "success_resets_consecutive_errors":
        script = [s if not s.startswith("ok") else "ok " + "a" * 64 for s in row["store_script"]]
        sink = InbandSink(ScriptedBlobs(script))
        got = [sink.offer(SID, "abstract", text=LONG, media_type="text/plain")
               for _ in row["expect_returns"]]
        assert [g is not None for g in got] == [r is not None for r in row["expect_returns"]]
        assert {k: counts(sink)[k] for k in row["expect_counts_subset"]} == row["expect_counts_subset"]
        assert sink.disabled is row["expect_disabled"]
    else:
        pytest.fail(f"unmapped row {rid}")
