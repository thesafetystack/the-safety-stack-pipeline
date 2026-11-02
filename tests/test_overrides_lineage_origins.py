"""The prepared ``configs/overrides/lineage_origins.yaml`` (D664): the curated origin list of the narrow-model
lineage, approved as prepared and added with ``stack overrides add`` at build time. A test file of
its own because the file spans three targets (``manual_sources`` and the screen's ``screen`` / ``stack_relevance``)
and is checked against the D6 file (``tests/test_collect_manual.py`` covers that one alone).

What is checked: the file loads through the add's own reader and checks; the manual sources are effective (no
entry shadows another, here or in the D6 file) and land on the intended channels and keys (the two present,
triage-skipped works on the DOI keys their documents have on the documents head of 2026-10-20); the screen
overrides are scoped, compose over a model answer of ``irrelevant`` into an S-relevant record that the frozen
payload types accept, and go stale on any other member set (``stack.annotate.select``).
"""

from __future__ import annotations

import datetime as dt
import pathlib
from collections import Counter
from typing import Any

from stack.annotate.select import _applies, _apply
from stack.overrides import MANUAL_SOURCES, check_add, effective, read_file
from stack.schemas.payloads.registry import REGISTRY

REPO = pathlib.Path(__file__).resolve().parent.parent
PREPARED = REPO / "configs" / "overrides" / "lineage_origins.yaml"
D6 = REPO / "configs" / "overrides" / "manual_sources.yaml"
NOW = dt.datetime(2026, 10, 21, tzinfo=dt.UTC)

#: The three present Distill documents and their live members on the documents head of 2026-10-20.
DISTILL = {"skdjf95venqh0": ["skdjf95venqh0"],                    # Feature Visualization (2017)
           "sa2cjatngtfey": ["s7my5nnqqmc3h", "sa2cjatngtfey"],   # The Building Blocks of Interpretability (2018)
           "spckkt48gz204": ["scvj9sgfa309q", "spckkt48gz204"]}   # Zoom In (2020)
#: Present but triage-skipped: the entry's DOI URL must carry the document's own DOI key so the flag lands on it.
JOINS = {"ms-lo-dwork-2006": "doi=10.29012/jpc.v7i3.405",              # doc sm52yhg55632e (JPC 2017 reprint)
         "ms-lo-lrp-2015": "doi=10.1371/journal.pone.0130140"}         # doc spwpxm0e1hs16


def _rows(path: pathlib.Path = PREPARED) -> list[Any]:
    return check_add(read_file(path, now=NOW), [])


def test_lineage_origins_load_with_the_expected_targets_and_flags() -> None:
    rows = _rows()
    assert all(o.kind in ("row", "field") for o in rows)
    assert Counter(o.target for o in rows) == {MANUAL_SOURCES: 25, "screen": 6, "stack_relevance": 3}
    prefix = {MANUAL_SOURCES: "ms-lo-", "screen": "scr-lo-", "stack_relevance": "srel-lo-"}
    for o in rows:
        assert o.override_id.startswith(prefix[o.target]), o.override_id
        assert o.note, f"{o.override_id}: the note carries title, year, ids and the technique"
    manual = [o for o in rows if o.target == MANUAL_SOURCES]
    assert all(o.payload["flags"] == ["accept"] for o in manual)
    eff = effective(manual, MANUAL_SOURCES)
    assert len(eff.rows) == len(manual), sorted(o.key for o in manual if [p.key for p in manual].count(o.key) > 1)
    # every manual note names a technique of the pinned taxonomy
    import yaml

    slugs = {t["slug"] for t in yaml.safe_load((REPO / "configs" / "taxonomy-v0.6.yaml").read_text())["techniques"]}
    for o in manual:
        assert any(s in o.note for s in slugs), f"{o.override_id}: no taxonomy-v0.6 technique id in the note"
    # D666: each manual source carries its metadata as fields; the date is the note's year (year-only form)
    for o in manual:
        assert o.payload["title"] and o.payload["authors"], o.override_id
        year = o.payload["published_at"]
        assert year.endswith("-01-01") and f"({year[:4]})" in o.note, o.override_id


def test_lineage_origins_and_the_d6_file_can_both_be_added() -> None:
    """Disjoint ids (the second add would be refused otherwise) and disjoint manual targets (an equal first
    typed key would silently leave only the later entry effective)."""
    mine, d6 = _rows(), _rows(D6)
    check_add(mine, d6)
    check_add(d6, mine)
    keys = {o.key for o in d6 if o.target == MANUAL_SOURCES}
    assert not [o.override_id for o in mine if o.target == MANUAL_SOURCES and o.key in keys]


def test_lineage_origins_land_on_the_intended_channels_and_keys() -> None:
    from stack.config import load
    from stack.urls import link_keys
    from workflows.collect.manual.main import channel_of

    channels = {c.id for c in load().channels.channels}
    manual = {o.override_id: o for o in _rows() if o.target == MANUAL_SOURCES}
    for oid, o in manual.items():
        target = link_keys(o.payload["url"])
        assert target is not None and channel_of(target) in channels, oid
        if "arxiv.org/" in o.payload["url"] or "doi.org/" in o.payload["url"]:
            assert target.group == "paper" and target.keys[0][0] in ("arxiv", "doi"), oid
        else:
            assert target.group == "url", oid
    for oid, key in JOINS.items():
        assert manual[oid].key == key, oid


def test_screen_overrides_retain_the_distill_origins_as_s_relevant() -> None:
    wf = [o for o in _rows() if o.target in ("screen", "stack_relevance")]
    assert {o.key for o in wf} == set(DISTILL)
    for o in wf:
        assert o.kind == "field" and o.item_key == "" and o.scope_members, o.override_id
        assert sorted(o.scope_members) == DISTILL[o.key], o.override_id
        live = DISTILL[o.key]
        assert _applies(o, live, "basis") and not _applies(o, [*live, "s-new-member"], "basis"), o.override_id
    model = {"screen": {"verdict": "irrelevant", "off_topic_reason": "general_ml — computer-vision image generation",
                        "reasoning": "kept", "adjudication_tier": 1, "tier1_verdict": "irrelevant"},
             "stack_relevance": {"verdict": "irrelevant", "confidence": 0.8, "prompt_name": "screen-v12"}}
    for target, base in model.items():
        spec = REGISTRY[target]
        base = {f.name: None for f in spec.payload_schema} | base
        for doc in DISTILL:
            mine = sorted((o for o in wf if o.target == target and o.key == doc), key=lambda o: o.field)
            assert [o.field for o in mine] == (["off_topic_reason", "verdict"] if target == "screen" else ["verdict"])
            pay, why = _apply(spec, base, mine)
            assert why is None and pay is not None, why
            assert pay["verdict"] == "relevant", (target, doc)
            assert pay.get("off_topic_reason") is None
            assert {k: v for k, v in pay.items() if k not in ("verdict", "off_topic_reason")} == {
                k: v for k, v in base.items() if k not in ("verdict", "off_topic_reason")}  # the model's rest stays
