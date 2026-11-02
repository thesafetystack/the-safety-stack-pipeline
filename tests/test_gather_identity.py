"""``gather`` identity (T2.7a, §6): union of typed keys within a channel group, anchor = earliest
observed member (masked included), continuity against the ``documents`` head (merge, split,
revive), the ``identity`` overrides (``merge``, ``separate``, ``mask``; added through ``overrides.add``), the dedup decision
table mined from v3's ``test_dedup`` and the checks over the resulting store.

Every case runs the real stage through ``stack.stage_runner.run_stage`` on a temp store, over
synthetic ``sources`` chains built with ``tests/synth/rows.py`` (D46).
"""

from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from stack.checks.verify import run_checks
from stack.plan import load_plan
from stack.runs import RunRecord, write_final_record
from stack.stage_runner import run_stage
from stack.store.chain import (
    HeaderFields,
    Published,
    head_of,
    read_view,
    resolve,
)
from tests.fakes.overrides import add_overrides, overrides_input
from tests.synth.records import sdir, write_series
from tests.synth.rows import AT, dg, rid, sid, source_row, table

REPO = Path(__file__).resolve().parents[1]
DEV_PLAN = REPO / "configs" / "plans" / "dev-v2.yaml"
FIXTURE = REPO / "tests" / "fixtures" / "v3_scenarios" / "dedup.yaml"
#: chain → channel; arxiv/openalex are the `paper` group, lesswrong `forum`, openai_blog `url`.
CHAINS = {"sources.arxiv_sweep": "arxiv", "sources.openalex": "openalex",
          "sources.forums": "lesswrong", "sources.blogs": "openai_blog"}

Row = dict[str, Any]


# ---------------------------------------------------------------- helpers
def src(i: int, keys: list[tuple[str, str]], *, t: int | None = None, channel: str = "arxiv",
        run: int = 1, **over: Any) -> Row:
    """A ``sources`` row for ``sid(i)`` with ``keys``, observed ``t`` minutes after AT (default ``i``)."""
    row = source_row(i, run=run, observed_at=AT + dt.timedelta(minutes=i if t is None else t), **over)
    row["identity"].update(channel=channel, keys=[{"kind": k, "value": v} for k, v in keys])
    return row


class Store:
    """A temp store with ``sources`` chains, an override file and ``gather`` runs."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.plan = load_plan(DEV_PLAN)
        self.runs = 0
        self.chains: list[str] = []

    def add(self, chain_id: str, rows: list[Row], *, full: bool = False) -> None:
        """Publish ``rows`` on ``chain_id`` (first file full, later ones parts unless ``full``: a
        full file superseding the head, as a compaction writes), with an ok record."""
        self.runs += 1
        n = 100 + self.runs
        for r in rows:
            r["run_id"] = r["inband"]["run_id"] = rid(n)
        fields = HeaderFields(stage=f"collect.{chain_id.removeprefix('sources.')}", run_id=rid(n),
                              config_digest=dg("cfg"), code_digest=dg("code"), plan="dev-v2")
        at = AT + dt.timedelta(minutes=n)

        def hook(pubs: list[Published]) -> None:
            write_final_record(self.root, RunRecord(
                run_id=rid(n), stage=fields.stage, writer=fields.stage, plan="dev-v2", status="ok",
                started_at=at, heartbeat_at=at, finished_at=at, code_digest=dg("code"),
                config_digest=dg("cfg"), scope_complete=True,
                version={"rows_touched": len(rows)},
                outputs={"sources": pubs[0].as_output()}))

        kind = "full" if not head_of(self.root, f"data/{chain_id}/main") or full else "part"
        write_series(self.root, chain_id, "sources", table("sources", rows), fields, kind=kind, at=at,
                     finished_at=at, on_published=hook)
        if chain_id not in self.chains:
            self.chains.append(chain_id)

    def compact(self, chain_id: str) -> None:
        """A full file of the chain's view (one row per source): the files before it leave the
        resolved version, as after ``stack migrate upcast``."""
        self.add(chain_id, read_view(resolve(self.root, sdir(chain_id))).to_pylist(), full=True)

    def overrides(self, entries: list[Row]) -> None:
        """The identity overrides now ``entries``: the ones the chain lacks are added (a test
        retires one with a revoke, never by leaving it out)."""
        add_overrides(self.root, "identity", entries, only_new=True)

    def gather(self) -> RunRecord:
        ins = [sdir(c) for c in self.chains]
        if head_of(self.root, "data/documents/main"):
            ins.append(sdir("documents"))
        ins += overrides_input(self.root)
        rec = run_stage(self.root, self.plan, "gather", ins, series="main")
        assert rec.status == "ok", rec.error
        return rec

    def docs(self) -> dict[str, Row]:
        return {r["doc_id"]: r for r in read_view(resolve(self.root, sdir("documents"))).to_pylist()}


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path)


def members(doc: Row) -> list[str]:
    return [m["source_id"] for m in doc["identity"]["members"]]


def member(doc: Row, s: str) -> Row:
    return next(m for m in doc["identity"]["members"] if m["source_id"] == s)


def merge(oid: str, *ss: str) -> Row:
    return {"override_id": oid, "value": "merge", "key": {"source_ids": list(ss)}}


def separate(oid: str, *ss: str) -> Row:
    return {"override_id": oid, "value": "separate", "key": {"source_ids": list(ss)}}


def mask(oid: str, s: str, reason: str | None = None) -> Row:
    e: Row = {"override_id": oid, "value": "mask", "key": {"source_id": s}}
    if reason is not None:
        e["mask_reason"] = reason
    return e


# ---------------------------------------------------------------- union and anchor
def test_doi_shared_in_a_group_merges(store: Store) -> None:
    store.add("sources.arxiv_sweep", [src(1, [("arxiv", "2610.00001"), ("doi", "10.1/x")])])
    store.add("sources.openalex", [src(2, [("openalex", "W2"), ("doi", "10.1/x")], channel="openalex")])
    store.gather()
    docs = store.docs()
    assert list(docs) == [sid(1)]
    d = docs[sid(1)]
    assert d["state"] == "identified" and members(d) == [sid(1), sid(2)]
    assert d["identity"]["anchor_source_id"] == sid(1)
    assert d["identity"]["channels"] == ["arxiv", "openalex"]
    m2 = member(d, sid(2))
    assert (m2["edge_kind"], m2["edge_to_source_id"], m2["key_kind"], m2["key_value"]) == \
        ("key:doi", sid(1), "doi", "10.1/x")
    assert member(d, sid(1))["edge_kind"] == "anchor"


def test_one_af_post_with_a_source_id_per_collector_chain_is_one_document(store: Store) -> None:
    # C32 id continuity is per own chain: collect.forums and collect.af_graphql each mint a
    # source_id for the same (alignmentforum, post id); the real dev store has one such pair
    # (T2.x-f2, D-row "AF source ids"). The forum group's fm_post key joins them.
    lw, af_f, af_g = (src(i, [("fm_post", "p")], channel=ch)
                      for i, ch in ((1, "lesswrong"), (2, "alignmentforum"), (3, "alignmentforum")))
    for r in (lw, af_f, af_g):
        r["identity"]["external_id"] = "p"
    store.add("sources.forums", [lw, af_f])
    store.add("sources.af_graphql", [af_g])
    store.gather()
    docs = store.docs()
    assert list(docs) == [sid(1)]
    d = docs[sid(1)]
    assert members(d) == [sid(1), sid(2), sid(3)]
    assert sorted(member(d, s)["edge_kind"] for s in (sid(2), sid(3))) == ["key:fm_post", "key:fm_post"]


def test_doi_shared_across_groups_does_not_merge(store: Store) -> None:
    # v3 matched external ids globally (stack/core/writer.py:928-931); §6 keeps edges in a group.
    store.add("sources.arxiv_sweep", [src(1, [("doi", "10.1/x")])])
    store.add("sources.blogs", [src(2, [("url", "https://o.ai/p"), ("doi", "10.1/x")], channel="openai_blog")])
    store.add("sources.forums", [src(3, [("fm_post", "p3"), ("doi", "10.1/x")], channel="lesswrong")])
    store.gather()
    assert sorted(store.docs()) == [sid(1), sid(2), sid(3)]


def test_a_kind_merging_in_two_groups_still_joins_within_a_group_only(
        store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The shipped config has no kind merging in two groups; give `paper` the `url` kind as well,
    # so only the group boundary keeps a paper and a blog post with one URL apart.
    cfg = tmp_path / "configs"
    shutil.copytree(REPO / "configs", cfg)
    text = (cfg / "channels.yaml").read_text()
    assert "paper:  { merge_keys: [doi," in text
    (cfg / "channels.yaml").write_text(text.replace("paper:  { merge_keys: [doi,", "paper:  { merge_keys: [url, doi,"))
    monkeypatch.setenv("STACK_CONFIG_DIR", str(cfg))
    url = ("url", "https://example.org/p")
    store.add("sources.arxiv_sweep", [src(1, [("arxiv", "a1"), url]), src(2, [("arxiv", "a2"), url])])
    store.add("sources.blogs", [src(3, [url], channel="openai_blog"), src(4, [url], channel="openai_blog")])
    store.gather()
    assert {d: members(r) for d, r in store.docs().items()} == {sid(1): [sid(1), sid(2)], sid(3): [sid(3), sid(4)]}


def test_non_merging_kind_never_joins(store: Store) -> None:
    store.add("sources.arxiv_sweep", [src(1, [("arxiv", "a1"), ("related", "r")]),
                                      src(2, [("arxiv", "a2"), ("related", "r")])])
    store.gather()
    assert sorted(store.docs()) == [sid(1), sid(2)]


def test_anchor_is_earliest_observed_not_newest(store: Store) -> None:
    # sid(5) observed before sid(1): it anchors although its id sorts later; a re-observation of
    # it later (a new part file) does not move the anchor.
    store.add("sources.arxiv_sweep", [src(1, [("doi", "d")], t=10), src(5, [("doi", "d")], t=1)])
    store.gather()
    store.add("sources.arxiv_sweep", [src(5, [("doi", "d")], t=50)])
    store.gather()
    docs = store.docs()
    assert list(docs) == [sid(5)]
    m5 = member(docs[sid(5)], sid(5))
    assert m5["observation_count"] == 2
    assert m5["first_observed_at"] == AT + dt.timedelta(minutes=1)
    assert m5["last_observed_at"] == AT + dt.timedelta(minutes=50)


def test_observation_stats_survive_a_compaction(store: Store) -> None:
    # Observed at t=5, 20, 30; a compaction keeps only the t=30 row. The head carries the count
    # (D261): the gather after the compaction changes nothing, a later observation adds one.
    store.add("sources.arxiv_sweep", [src(5, [("arxiv", "a5")], t=5)])
    store.add("sources.arxiv_sweep", [src(5, [("arxiv", "a5")], t=20)])
    store.add("sources.arxiv_sweep", [src(5, [("arxiv", "a5")], t=30)])
    store.gather()
    before = store.docs()[sid(5)]
    assert member(before, sid(5))["observation_count"] == 3
    store.compact("sources.arxiv_sweep")
    assert len(resolve(store.root, sdir("sources.arxiv_sweep")).paths) == 1
    rec = store.gather()
    assert rec.counts["changed"] == 0 and store.docs()[sid(5)] == before
    store.add("sources.arxiv_sweep", [src(5, [("arxiv", "a5")], t=40)])
    store.gather()
    m = member(store.docs()[sid(5)], sid(5))
    assert (m["first_observed_at"], m["last_observed_at"], m["observation_count"]) == (
        AT + dt.timedelta(minutes=5), AT + dt.timedelta(minutes=40), 4)
    assert store.gather().counts["changed"] == 0


def test_a_miss_after_an_observation_keeps_the_member(store: Store) -> None:
    # R3-1: the newest rows of sid(1) (the anchor) and sid(2) are non-retracting misses (miss_cap
    # > 1, or miss_means: unknown); their older observed rows still speak, so both stay members
    # and gather neither fails ("member(s) ... absent", before the fix) nor changes the document.
    # A source seen only as a miss joins nothing.
    store.add("sources.arxiv_sweep", [src(1, [("arxiv", "a1")], t=1), src(2, [("arxiv", "a1")], t=2)])
    store.gather()
    before = store.docs()
    miss = {"state": "missing", "record_format": "miss", "status": "empty"}
    store.add("sources.arxiv_sweep", [src(1, [], t=10, **miss), src(2, [], t=10, **miss),
                                      src(3, [("arxiv", "a1")], t=11, **miss)])
    rec = store.gather()
    assert (rec.counts["changed"], rec.counts["misses_only"]) == (0, 1)
    docs = store.docs()
    assert docs == before and members(docs[sid(1)]) == [sid(1), sid(2)]
    m2 = member(docs[sid(1)], sid(2))
    assert (m2["edge_kind"], m2["last_observed_at"], m2["withdrawn_at"]) == (
        "key:arxiv", AT + dt.timedelta(minutes=2), None)


# ---------------------------------------------------------------- continuity and overrides
def test_merge_override_retires_the_absorbed_id(store: Store) -> None:
    store.add("sources.arxiv_sweep", [src(1, [("arxiv", "a1")]), src(2, [("arxiv", "a2")])])
    store.gather()
    created = store.docs()[sid(2)]["created_at"]
    store.overrides([merge("ov-m", sid(2), sid(1))])
    store.gather()
    docs = store.docs()
    surv, gone = docs[sid(1)], docs[sid(2)]
    assert members(surv) == [sid(1), sid(2)] and surv["state"] == "identified"
    assert surv["identity"]["former_doc_ids"] == [sid(2)]
    assert surv["identity"]["override_ids"] == ["ov-m"]
    m2 = member(surv, sid(2))
    assert (m2["edge_kind"], m2["edge_to_source_id"], m2["override_id"]) == ("human:merge", sid(1), "ov-m")
    assert gone["state"] == "retired" and gone["identity"]["merged_into"] == sid(1)
    assert gone["created_at"] == created  # X17


def test_a_gather_without_the_series_overrides_is_refused(store: Store) -> None:
    """A9 / S25-31 (D495): a by-hand gather into a series whose ``data/overrides/<series>/`` has a file, without
    ``--in`` of it, would split every merge until the next build: refused before any record, unless
    ``no_overrides`` (``--no-overrides``), which then undoes the merge as asked."""
    from stack.runs import read_run_records
    from stack.stage_runner import StageError

    store.add("sources.arxiv_sweep", [src(1, [("arxiv", "a1")]), src(2, [("arxiv", "a2")])])
    store.gather()
    store.overrides([merge("ov-m", sid(2), sid(1))])
    store.gather()
    ins = [sdir(c) for c in store.chains] + [sdir("documents")]
    runs = len(read_run_records(store.root))
    with pytest.raises(StageError, match=r"gather: data/overrides/main/ has overrides .*--no-overrides"):
        run_stage(store.root, store.plan, "gather", ins, series="main")
    assert len(read_run_records(store.root)) == runs
    rec = run_stage(store.root, store.plan, "gather", ins, series="main", no_overrides=True)
    assert rec.status == "ok", rec.error
    assert store.docs()[sid(2)]["state"] == "identified"  # the merge undone, on purpose


def test_two_documents_join_the_earliest_anchor_survives(store: Store) -> None:
    store.add("sources.arxiv_sweep", [src(3, [("arxiv", "a3")], t=1), src(1, [("arxiv", "a1")], t=2)])
    store.gather()
    store.add("sources.openalex", [src(7, [("arxiv", "a1"), ("arxiv", "a3")], channel="openalex")])
    store.gather()
    docs = store.docs()
    assert docs[sid(3)]["state"] == "identified" and members(docs[sid(3)]) == [sid(3), sid(1), sid(7)]
    assert docs[sid(3)]["identity"]["former_doc_ids"] == [sid(1)]
    assert docs[sid(1)]["state"] == "retired" and docs[sid(1)]["identity"]["merged_into"] == sid(3)


def test_separate_splits_and_both_parts_list_the_old_id(store: Store) -> None:
    # Old doc sid(2) = {2, 3} (shared doi). A merge of 1 with 2 and a separate of 2 from 3 spread
    # its members over two documents: sid(2) is retired with merged_into NULL (a split) and both
    # parts list it.
    store.add("sources.arxiv_sweep", [src(1, [("arxiv", "a1")]), src(2, [("doi", "d")]),
                                      src(3, [("doi", "d")])])
    store.gather()
    assert members(store.docs()[sid(2)]) == [sid(2), sid(3)]
    store.overrides([merge("ov-m", sid(1), sid(2)), separate("ov-s", sid(2), sid(3))])
    rec = store.gather()
    docs = store.docs()
    assert docs[sid(2)]["state"] == "retired" and docs[sid(2)]["identity"]["merged_into"] is None
    assert members(docs[sid(1)]) == [sid(1), sid(2)] and members(docs[sid(3)]) == [sid(3)]
    assert docs[sid(1)]["identity"]["former_doc_ids"] == [sid(2)]
    assert docs[sid(3)]["identity"]["former_doc_ids"] == [sid(2)]
    assert rec.counts["split"] == 1


def test_separate_of_the_anchor_keeps_the_id_then_revoke_and_revive(store: Store) -> None:
    store.add("sources.arxiv_sweep", [src(1, [("doi", "d")]), src(2, [("doi", "d")])])
    store.gather()
    # separate: the part holding the old anchor keeps sid(1); sid(2) is a new document.
    store.overrides([separate("ov-s", sid(1), sid(2))])
    store.gather()
    docs = store.docs()
    assert members(docs[sid(1)]) == [sid(1)] and members(docs[sid(2)]) == [sid(2)]
    assert docs[sid(2)]["state"] == "identified" and docs[sid(1)]["state"] == "identified"
    assert docs[sid(2)]["identity"]["former_doc_ids"] == []  # a live id is never an alias
    # revoke the separate: sid(2) joins sid(1) again and becomes a tombstone ...
    store.overrides([separate("ov-s", sid(1), sid(2)), {"override_id": "ov-r", "revokes": "ov-s"}])
    store.gather()
    docs = store.docs()
    assert docs[sid(2)]["state"] == "retired" and docs[sid(2)]["identity"]["merged_into"] == sid(1)
    created = docs[sid(2)]["created_at"]
    # ... and a new separate revives it (retired -> identified, §2.5).
    store.overrides([separate("ov-s2", sid(1), sid(2))])
    rec = store.gather()
    docs = store.docs()
    assert docs[sid(2)]["state"] == "identified" and docs[sid(2)]["identity"]["merged_into"] is None
    assert docs[sid(2)]["created_at"] == created and rec.counts["revived"] == 1
    assert docs[sid(1)]["identity"]["former_doc_ids"] == []


def test_masked_anchor_stays_the_anchor(store: Store) -> None:
    store.add("sources.arxiv_sweep", [src(1, [("doi", "d")]), src(2, [("doi", "d")])])
    store.overrides([mask("ov-k", sid(1))])
    store.gather()
    d = store.docs()[sid(1)]
    assert d["identity"]["anchor_source_id"] == sid(1) and members(d) == [sid(1), sid(2)]
    assert member(d, sid(1))["is_live"] is False and member(d, sid(1))["override_id"] == "ov-k"
    assert d["identity"]["is_masked"] is False  # a live member remains
    store.overrides([mask("ov-k", sid(1)), mask("ov-k2", sid(2), "review_required")])
    store.gather()
    d = store.docs()[sid(1)]
    assert d["identity"]["is_masked"] is True and d["identity"]["mask_reason"] == "operator"


def test_bridged_separate_masks_for_review(store: Store) -> None:
    # 1 and 2 share a doi and are separated, but 3 carries the doi as well: the pair stays joined.
    store.add("sources.arxiv_sweep", [src(1, [("doi", "d")]), src(2, [("doi", "d")])])
    store.add("sources.openalex", [src(3, [("doi", "d")], channel="openalex")])
    store.overrides([separate("ov-s", sid(1), sid(2))])
    store.gather()
    d = store.docs()[sid(1)]
    assert members(d) == [sid(1), sid(2), sid(3)]
    assert (d["identity"]["is_masked"], d["identity"]["mask_reason"]) == (True, "review_required")
    assert d["identity"]["override_ids"] == ["ov-s"]


def test_unknown_source_in_override_fails_the_run(store: Store) -> None:
    store.add("sources.arxiv_sweep", [src(1, [("doi", "d")])])
    store.overrides([merge("ov-m", sid(1), sid(99))])
    rec = run_stage(store.root, store.plan, "gather", [sdir("sources.arxiv_sweep"), sdir("overrides")], series="main")
    assert rec.status == "failed" and "name source ids no sources input" in (rec.error or "")


# removed with the implicit overrides pin (D377, D338): a gather no longer refuses a missing
# overrides input (a stage reads only its --in paths); the pin is the plan line's `in:`.
def test_gather_records_the_overrides_file_it_read(store: Store) -> None:
    store.add("sources.arxiv_sweep", [src(1, [("doi", "d")]), src(2, [("doi", "d")])])
    store.overrides([mask("ov-k", sid(1))])
    rec = store.gather()
    head_run = head_of(store.root, "data/overrides/main").split("_")[1].split(".")[0]
    assert rec.inputs["overrides"] == f"{head_run}.overrides"  # provenance: the run of the read head


def test_later_identity_row_of_one_key_wins(store: Store) -> None:
    """D337: a merge and a later separate of the same pair are one group; the later one wins
    (before the family both applied and the bridged pair was masked for review)."""
    store.add("sources.arxiv_sweep", [src(1, [("arxiv", "a1")]), src(2, [("arxiv", "a2")])])
    add_overrides(store.root, "identity", [{**merge("ov-m", sid(1), sid(2)), "created_at": "2026-01-01T00:00:00Z"}])
    add_overrides(store.root, "identity", [{**separate("ov-s", sid(2), sid(1)), "created_at": "2026-01-02T00:00:00Z"}])
    store.gather()
    docs = store.docs()
    assert members(docs[sid(1)]) == [sid(1)] and members(docs[sid(2)]) == [sid(2)]
    assert docs[sid(1)]["identity"]["override_ids"] == ["ov-s"] and not docs[sid(1)]["identity"]["is_masked"]


def test_regather_over_unchanged_sources_changes_nothing(store: Store) -> None:
    store.add("sources.arxiv_sweep", [src(1, [("doi", "d")]), src(2, [("doi", "d")]), src(3, [("arxiv", "a")])])
    store.add("sources.openalex", [src(4, [("arxiv", "a")], channel="openalex")])
    store.overrides([mask("ov-k", sid(3))])
    store.gather()
    before = store.docs()
    rec = store.gather()
    assert store.docs() == before  # every row, stamps included, carried byte for byte (C6)
    assert rec.counts["changed"] == 0 and rec.version is not None and rec.version["rows_touched"] == 0


# ---------------------------------------------------------------- v3 dedup decision table
#: v4 outcome per fixture row: label → the label of the document holding it, and the note
#: stating why it differs from v3 (None: v3's grouping and representative are reproduced).
DEDUP_V4: dict[str, tuple[dict[str, str], str | None]] = {
    "external_id_wins": ({"A": "A", "B": "A"}, None),
    "path_compression": ({"A": "A", "B": "A", "C": "A"}, None),
    "merge_rep_with_satellites": ({s: "A" for s in "ABCDE"}, None),
    "election_text_quality": (
        {"A": "A", "B": "A"},
        ("EXCEPTION: v3 elected B for its better text_quality; v4 anchors on the earliest observed "
         "member and acquisition never feeds identity (§6)")),
    "election_discovered_at": ({"A": "A", "B": "A"}, None),
    "election_id_tiebreak": ({"I1": "I1", "I2": "I1"}, None),
    "humans_stick": ({"A": "A", "B": "B", "C": "C"}, None),
    "machine_confirm_preserves_human_origin": ({"A": "A", "B": "A"}, None),
    "repoint_promote_satellite": (
        {"A": "A", "B": "A", "C": "A"},
        "EXCEPTION: v3 repointed the representative to B; v4 has no repoint, doc_id is the anchor"),
    "repoint_rejects_non_rep_old_rep": ({"A": "A", "B": "A"}, None),
    "repoint_rejects_third_group": ({"A": "A", "B": "A", "D": "D", "E": "D"}, None),
    "repoint_machine_over_human_stuck": ({"A": "A", "B": "A", "C": "A"}, None),
    "clear_returns_satellite_to_corpus": ({"A": "A", "S": "S"}, None),
    "mask_reelects_representative": (
        {"R": "R", "S1": "R", "S2": "R"},
        "EXCEPTION: v3 re-elected S1 when R was masked; v4 keeps a masked anchor (§6)"),
}


def _dedup_rows() -> list[Row]:
    return yaml.safe_load(FIXTURE.read_text())["rows"]


def _translate(row: Row) -> tuple[dict[str, str], list[Row]]:
    """Fixture sources → source ids (list order = observation order; ``discovered_at`` honoured),
    ops → ``identity.yaml`` entries. Every v3 duplicate pointer (``mark_duplicate``, ``elect``),
    machine (``method: content``) or human, becomes a ``merge``: v4 merges no content
    automatically (C29), a human merges on ``same_raw_doc_ids``. A v3 op it refused
    (``expect.flagged``, ``raises``) and ``repoint`` have no v4 counterpart; ``clear_duplicate``
    by a human revokes the merges naming the source; ``mask``/``unmask`` add/revoke a mask."""
    labels = {lb: sid(i + 1) for i, lb in enumerate(row["sources"])}
    entries: list[Row] = []
    for k, op in enumerate(row["ops"]):
        kind = op["op"]
        if (op.get("expect") or {}).get("flagged") or kind == "repoint":
            continue
        if kind in ("mark_duplicate", "elect"):
            ss = [labels[op["a"]], labels[op["b"]]] if kind == "mark_duplicate" else [labels[m] for m in op["members"]]
            entries.append(merge(f"ov-{k}", *ss))
        elif kind == "clear_duplicate" and op["info"] == "human":
            s = labels[op["a"]]
            for e in [e for e in entries if e.get("value") == "merge" and s in e["key"]["source_ids"]]:
                entries.append({"override_id": f"ov-{k}-{e['override_id']}", "revokes": e["override_id"]})
        elif kind == "mask":
            entries.append(mask(f"ov-{k}", labels[op["a"]]))
        elif kind == "unmask":
            m = next(e for e in entries if e.get("value") == "mask" and e["key"]["source_id"] == labels[op["a"]])
            entries.append({"override_id": f"ov-{k}", "revokes": m["override_id"]})
    return labels, entries


def test_dedup_table_covers_the_fixture() -> None:
    assert sorted(r["id"] for r in _dedup_rows()) == sorted(DEDUP_V4)


@pytest.mark.parametrize("row", _dedup_rows(), ids=lambda r: r["id"])
def test_dedup_table(store: Store, row: Row) -> None:
    """v3's merges reproduced as identity overrides. No fixture row matches across groups (all
    sources are one channel), so the cross-group exception lives in
    :func:`test_doi_shared_across_groups_does_not_merge`; automatic content merge (C29) is the
    translation itself (module of :func:`_translate`)."""
    labels, entries = _translate(row)
    rows = []
    for i, (lb, spec) in enumerate(row["sources"].items()):
        at = spec.get("discovered_at")
        t = (dt.datetime.fromisoformat(at) - AT).total_seconds() / 60 if at else i
        rows.append(src(i + 1, [("arxiv", f"x.{row['id']}.{lb}")], t=int(t)))
    store.add("sources.arxiv_sweep", rows)
    store.overrides(entries)
    store.gather()
    expected, _note = DEDUP_V4[row["id"]]
    holder = {s: d for d, doc in store.docs().items() if doc["state"] != "retired" for s in members(doc)}
    assert {lb: holder[labels[lb]] for lb in labels} == {lb: labels[d] for lb, d in expected.items()}


# ---------------------------------------------------------------- checks over the store
@pytest.mark.slow
def test_checks_over_a_gathered_store(store: Store) -> None:
    store.add("sources.arxiv_sweep", [src(1, [("arxiv", "a1")]), src(2, [("doi", "d")]),
                                      src(3, [("doi", "d")]), src(4, [("arxiv", "a4")])])
    store.add("sources.forums", [src(5, [("fm_post", "p")], channel="lesswrong"),
                                 src(6, [("fm_post", "p")], channel="lesswrong")])
    store.gather()
    store.overrides([merge("ov-m", sid(1), sid(2)), separate("ov-s", sid(2), sid(3)),
                     merge("ov-m4", sid(1), sid(4)), mask("ov-k", sid(6))])
    store.add("sources.arxiv_sweep", [src(4, [("arxiv", "a4")], t=60)])
    store.gather()
    results = {r.code: r for r in run_checks(store.root)}
    wanted = [c for c in results if c[0] == "S"] + ["V3", "V4", "X4", "X17", "X22"]
    bad = {c: (results[c].status, results[c].error_class, results[c].items) for c in wanted
           if results[c].status != "ok"}
    assert bad == {}
    assert results["X18"].status in ("ok", "skipped"), results["X18"]


# ---------------------------------------------------------------- detectors (stack.identity; round 5, T-ID)
def _titled(row: Row, title: str, author: str | None, days: int, doc_type: str) -> Row:
    md = row["metadata"]
    md.update(title=title, doc_type=doc_type, published_at=AT + dt.timedelta(days=days),
              authors=[{"name": author, "orcid": None, "affiliation_raw": None}] if author else [])
    return row


def test_paper_matcher_joins_preprint_and_journal_version(store: Store) -> None:
    title = "Weak-to-Strong Generalization in Practice"
    store.add("sources.arxiv_sweep", [_titled(src(1, [("arxiv", "2601.00001")]), title, "Collin Burns", 0, "preprint")])
    store.add("sources.openalex", [
        _titled(src(2, [("openalex", "W2"), ("doi", "10.1/journal")], channel="openalex"), title.upper(),
                "C. Burns", 200, "journal"),
        _titled(src(3, [("openalex", "W3")], channel="openalex"), title, "Other Person", 10, "journal")])
    rec = store.gather()
    d = store.docs()
    assert members(d[sid(1)]) == [sid(1), sid(2)] and members(d[sid(3)]) == [sid(3)]
    m2 = member(d[sid(1)], sid(2))
    assert (m2["edge_kind"], m2["edge_to_source_id"]) == ("match:paper_title_author", sid(1))
    assert rec.counts["match_pairs_paper_title_author"] == 1
    assert rec.counts["joined_by_match_paper_title_author"] == 1
    again = store.gather()  # deterministic, idempotent
    assert again.counts["changed"] == 0


def test_a_matched_paper_stays_merged_when_its_title_bucket_grows_past_max_bucket_sources(store: Store) -> None:
    """S25-19 / L3-7: a preprint and its journal version joined by the title matcher in one build stay one
    document when a later build adds more same-title records of the same work (key-joined to either):
    a bucket counts works, not sources."""
    title = "Scaling Laws for Reward Model Overoptimization"
    store.add("sources.arxiv_sweep", [_titled(src(1, [("arxiv", "2210.10760")]), title, "Leo Gao", 0, "preprint")])
    store.add("sources.openalex", [_titled(src(2, [("openalex", "W2"), ("doi", "10.5555/icml.2")], channel="openalex"),
                                           title, "Leo Gao", 200, "journal")])
    store.gather()
    assert members(store.docs()[sid(1)]) == [sid(1), sid(2)]
    more = [_titled(src(10 + k, [("openalex", f"W{10 + k}"), ("doi", "10.5555/icml.2")], channel="openalex",
                        t=500 + k), title, "Leo Gao", 200, "journal") for k in range(4)]
    more += [_titled(src(20 + k, [("arxiv", "2210.10760")], t=600 + k), title, "Leo Gao", 0, "preprint")
             for k in range(4)]
    store.add("sources.openalex", more[:4])
    store.add("sources.arxiv_sweep", more[4:])
    rec = store.gather()
    live = {k: members(v) for k, v in store.docs().items() if v["state"] != "retired"}
    assert list(live) == [sid(1)] and len(live[sid(1)]) == 10
    assert (rec.counts["match_crowded_paper"], rec.counts["split"], rec.counts["moved_out"]) == (0, 0, 0)


def test_a_member_leaving_a_continuing_document_is_counted_moved_out(store: Store) -> None:
    """S25-19: past :data:`stack.identity.MAX_BUCKET` works a title is generic and the earlier match goes (by
    design), but no longer silently: the member that left a document keeping its id is ``moved_out``."""
    title = "Scaling Laws for Reward Model Overoptimization"
    store.add("sources.arxiv_sweep", [_titled(src(1, [("arxiv", "2210.10760")]), title, "Leo Gao", 0, "preprint")])
    store.add("sources.openalex", [_titled(src(2, [("openalex", "W2"), ("doi", "10.5555/icml.2")], channel="openalex"),
                                           title, "Leo Gao", 200, "journal")])
    store.gather()
    store.add("sources.openalex", [
        _titled(src(10 + k, [("openalex", f"W{10 + k}"), ("doi", f"10.5555/x.{k}")], channel="openalex", t=500 + k),
                title, f"Other Person{k}", 200, "journal") for k in range(8)])
    rec = store.gather()
    d = store.docs()
    assert members(d[sid(1)]) == [sid(1)] and members(d[sid(2)]) == [sid(2)]
    assert (rec.counts["match_crowded_paper"], rec.counts["split"], rec.counts["moved_out"]) == (1, 0, 1)


def test_post_matcher_joins_crosspost_across_groups_and_never_a_paper(store: Store) -> None:
    title = "The Curve Bends You Toward Nothing"
    store.add("sources.forums", [_titled(src(1, [("fm_post", "p1")], channel="lesswrong"), title, "Zvi", 0,
                                         "forum_post")])
    store.add("sources.blogs", [_titled(src(2, [("url", "https://thezvi.substack.com/p/x")], channel="openai_blog"),
                                        title, "Zvi Mowshowitz", 1, "lab_blog")])
    store.add("sources.arxiv_sweep", [_titled(src(3, [("arxiv", "2601.00003")]), title, "Zvi Mowshowitz", 0,
                                              "preprint")])
    store.gather()
    d = store.docs()
    assert members(d[sid(1)]) == [sid(1), sid(2)] and members(d[sid(3)]) == [sid(3)]
    assert member(d[sid(1)], sid(2))["edge_kind"] == "match:post_title_author"


def test_linkpost_url_key_bridges_forum_and_url_groups(store: Store) -> None:
    url = "https://blog.redwoodresearch.org/p/paper-distillation"
    store.add("sources.blogs", [src(1, [("url", url)], channel="openai_blog")])
    store.add("sources.forums", [src(2, [("fm_post", "p2"), ("url", url)], channel="lesswrong"),
                                 src(3, [("fm_post", "p2")], channel="lesswrong", t=30)])
    store.gather()
    d = store.docs()
    assert members(d[sid(1)]) == [sid(1), sid(2), sid(3)]
    m2 = member(d[sid(1)], sid(2))
    assert (m2["edge_kind"], m2["key_kind"], m2["key_value"]) == ("key:url", "url", url)
    assert member(d[sid(1)], sid(3))["edge_kind"] == "key:fm_post"
    assert sorted(d[sid(1)]["identity"]["channels"]) == ["lesswrong", "openai_blog"]


def test_url_key_on_a_paper_source_does_not_bridge(store: Store) -> None:
    url = "https://blog.example.org/p/x"
    store.add("sources.blogs", [src(1, [("url", url)], channel="openai_blog")])
    store.add("sources.arxiv_sweep", [src(2, [("arxiv", "2601.00002"), ("url", url)])])
    store.gather()
    assert members(store.docs()[sid(1)]) == [sid(1)]


def test_separate_undoes_a_match_and_merge_forces_one(store: Store) -> None:
    title = "Sleeper Agents Training Deceptive Models"
    store.add("sources.arxiv_sweep", [_titled(src(1, [("arxiv", "2601.00001")]), title, "Evan Hubinger", 0,
                                              "preprint"),
                                      _titled(src(2, [("arxiv", "2601.00002")]), "Unrelated Other Title Here",
                                              "X Y", 0, "preprint")])
    store.add("sources.openalex", [_titled(src(3, [("openalex", "W3")], channel="openalex"), title,
                                           "Evan Hubinger", 30, "journal")])
    store.gather()
    assert members(store.docs()[sid(1)]) == [sid(1), sid(3)]
    store.overrides([separate("ov-s", sid(1), sid(3)), merge("ov-m", sid(1), sid(2))])
    store.gather()
    d = store.docs()
    assert members(d[sid(1)]) == [sid(1), sid(2)] and members(d[sid(3)]) == [sid(3)]
    assert not d[sid(1)]["identity"]["is_masked"]
    assert member(d[sid(1)], sid(2))["edge_kind"] == "human:merge"


def test_two_linkposts_of_one_page_join_nothing(store: Store) -> None:
    url = "https://blog.example.org/p/a-post"
    store.add("sources.blogs", [src(1, [("url", url)], channel="openai_blog")])
    store.add("sources.forums", [src(2, [("fm_post", "p2"), ("url", url)], channel="lesswrong"),
                                 src(3, [("fm_post", "p3"), ("url", url)], channel="lesswrong")])
    rec = store.gather()
    d = store.docs()
    assert [members(d[s]) for s in (sid(1), sid(2), sid(3))] == [[sid(1)], [sid(2)], [sid(3)]]
    assert rec.counts["bridge_ambiguous"] == 1


def test_one_linkpost_seen_on_lw_and_af_still_bridges(store: Store) -> None:
    url = "https://blog.example.org/p/a-post"
    store.add("sources.blogs", [src(1, [("url", url)], channel="openai_blog")])
    store.add("sources.forums", [src(2, [("fm_post", "p2"), ("url", url)], channel="lesswrong"),
                                 src(3, [("fm_post", "p2"), ("url", url)], channel="alignmentforum")])
    rec = store.gather()
    assert members(store.docs()[sid(1)]) == [sid(1), sid(2), sid(3)] and rec.counts["bridge_ambiguous"] == 0
