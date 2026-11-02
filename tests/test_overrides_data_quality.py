"""The prepared ``configs/overrides/data_quality_2026_10.yaml`` (D768, D772, D774): the data-quality fixes from the review of
release 2026-10-27, added with ``stack overrides add`` at build time.

What is checked: the file loads through the add's own reader and checks, under the author label the release maps to a
public author; every public note is the note's first sentence and passes the release's scrub. ``fields``: each value
follows the rule its note names against the stored value the note records (the rules D759 tested its export-time
corrections by), ``doc_type`` values are of the release's enum, and gather's field merge takes every entry.
``identity``: crosspost merges, each naming two or three documents (their anchors' source ids) that no other entry
names, with the rule in the note; gather takes every merge; no merged document carries a field fix (a fix naming a
merged-away id would resolve to its survivor, and two fixes of one field there are refused by gather).
``technique_staged``: row overrides, scoped to members and text, whose payloads the frozen type accepts, keep no
quote of the rubric's worked example and keep the flags consistent; select applies them in scope and leaves them
stale out of it.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import re
from collections import Counter
from typing import Any

import pytest

from stack.annotate.select import _applies, _apply
from stack.fold.dataset import DATASET_DIR, DatasetConfig, Mapping_, OverridesPolicy
from stack.overrides import FIELDS, IDENTITY, Override, check_add, effective, read_file
from stack.schemas.payloads.registry import REGISTRY
from workflows.gather import overrides as identity_overrides
from workflows.gather.merge import OVERRIDABLE, field_ops

REPO = pathlib.Path(__file__).resolve().parent.parent
PREPARED = REPO / "configs" / "overrides" / "data_quality_2026_10.yaml"
NOW = dt.datetime(2026, 10, 30, tzinfo=dt.UTC)
AUTHOR = "data-quality-2026-10"
#: target -> override_id prefixes
PREFIX = {FIELDS: ("title-2026-10-30-", "doctype-2026-10-30-"), IDENTITY: ("merge-2026-10-30-",), "technique_staged": ("ts-2026-10-30-",)}

_NOTE = re.compile(r"^(?P<public>.+?) Rule: (?P<rule>[a-z_, ]+)\. Stored: (?P<stored>.*)\.$")
_LEAK_NOTE = re.compile(r"^(?P<public>.+?) Dropped: (?P<slugs>[a-z0-9, -]+)\. Model answer: .*\.$")
_MERGE_NOTE = re.compile(r"^(?P<public>.+?) Rule: (?P<rule>[a-z_, ]+)\.(?: Text containment .*?: (?P<cont>[0-9., and;]+)\.)?"
                         r"(?: Former URL: (?P<moved>.+)\.)?$")


def _rows() -> list[Override]:
    return check_add(read_file(PREPARED, now=NOW), [])


def _of(target: str) -> list[Override]:
    return [o for o in _rows() if o.target == target]


def test_the_file_loads_with_its_targets_author_and_public_notes() -> None:
    rows = _rows()
    assert Counter(o.target for o in rows) == {FIELDS: 85, IDENTITY: 152, "technique_staged": 4}
    policy = OverridesPolicy(DATASET_DIR / "overrides-public.yaml")
    for o in rows:
        assert o.override_id.startswith(PREFIX[o.target]), o.override_id
        assert o.author == AUTHOR and o.tier == "human", o.override_id
        assert policy.author(o)[0] == "maintainers"
        kind = policy.kind(o)
        note = policy.note(o, kind)
        assert not policy.marked(note) and len(note) <= policy.raw["max_note_chars"], (o.override_id, note)
        m = {FIELDS: _NOTE, IDENTITY: _MERGE_NOTE}.get(o.target, _LEAK_NOTE).match(o.note or "")
        if m is not None:  # the public note is the first sentence; the rule and its data stay internal
            assert note == m.group("public"), (o.override_id, note)
    assert len(effective(rows).rows) == len(rows), "no entry shadows another"


def _undotted(s: str) -> str:
    return re.sub(r"(?<=\d)\.(?=\d)", " ", s)


def _rule(field: str, why: set[str], stored: Any, value: Any) -> None:
    """D759's rules: each reason says how the value is made from the stored one."""
    if why == {"version_dots"}:
        assert value != stored and _undotted(value) == stored
    elif why <= {"version_dots", "doc_word_twice"}:
        assert any(stored == _undotted(value) + w for w in (" System Card", " Model Card", " Policy"))
        assert ("version_dots" in why) == (_undotted(value) != value)
    elif why == {"listing_run_in"}:
        date = re.match(r"^[A-Z][a-z]{2} \d{2}, \d{4}", stored)
        assert date is not None and stored.startswith(date.group(0) + value)
        rest = stored[len(date.group(0) + value):]  # the run-in description starts a sentence
        assert rest == "" or (rest[0].isupper() and not value[-1].isspace())
    elif why == {"model_card"}:
        assert (field, stored, value) == ("doc_type", "system_card", "model_card")
    else:
        assert (field, stored) == ("doc_type", None) and why in ({"forum_post_url"}, {"arxiv_preprint"})
        assert value == {"forum_post_url": "forum_post", "arxiv_preprint": "preprint"}[next(iter(why))]


def test_each_field_fix_follows_the_rule_its_note_names() -> None:
    doc_types = Mapping_(DATASET_DIR / "mapping.yaml").enums["doc_type"][0]
    reasons: Counter[str] = Counter()
    for o in _of(FIELDS):
        assert o.field in ("title", "doc_type") and o.field in OVERRIDABLE and o.item_key == "doc_id", o.override_id
        assert o.override_id == f"{'title' if o.field == 'title' else 'doctype'}-2026-10-30-{o.key}"
        m = _NOTE.match(o.note or "")
        assert m is not None, f"{o.override_id}: the note ends `Rule: <reasons>. Stored: <JSON>.`"
        why = set(m.group("rule").split(", "))
        stored = json.loads(m.group("stored"))
        if o.field == "doc_type":
            assert o.value in doc_types, o.override_id
        try:
            _rule(o.field, why, stored, o.value)
        except AssertionError:
            pytest.fail(f"{o.override_id}: value {o.value!r} does not follow {sorted(why)} from {stored!r}")
        reasons.update(why)
    assert reasons == {"version_dots": 42, "doc_word_twice": 7, "listing_run_in": 9, "model_card": 8,
                       "forum_post_url": 17, "arxiv_preprint": 3}


def test_the_rule_check_refuses_a_value_that_breaks_its_rule() -> None:
    with pytest.raises(AssertionError):
        _rule("title", {"version_dots"}, "Gpt 4 5 System Card", "GPT-4.5 System Card")
    with pytest.raises(AssertionError):
        _rule("doc_type", {"forum_post_url"}, None, "preprint")


def test_gather_takes_every_field_fix() -> None:
    fops = field_ops(_rows())
    assert sum(len(v) for v in fops.by_doc.values()) == 85 and not fops.by_source
    assert len(fops.by_doc) == 84  # one document has its title and its doc_type fixed


def test_the_export_time_corrections_key_is_refused() -> None:
    with pytest.raises(ValueError, match="obsolete"):
        DatasetConfig.model_validate({"name": "x", "format": "dataset", "variant": "public", "hf_repo": "a/b",
                                      "judge_eval": "evals/x", "corrections": "dataset/corrections.yaml"})


def test_each_merge_names_two_or_three_documents_once_with_its_rule() -> None:
    merges = _of(IDENTITY)
    named: Counter[str] = Counter()
    rules: Counter[str] = Counter()
    for o in merges:
        assert o.kind == "row" and o.value == "merge", o.override_id
        ids = json.loads(o.key)
        assert 2 <= len(ids) <= 3 and ids == sorted(set(ids)), o.override_id
        assert o.override_id.removeprefix("merge-2026-10-30-") in ids
        assert all(re.fullmatch(r"s[0-9a-z]{12}", i) for i in ids), o.override_id
        named.update(ids)
        m = _MERGE_NOTE.match(o.note or "")
        assert m is not None, f"{o.override_id}: the note ends `Rule: <rules>.` (+ containment, former URL)"
        why = m.group("rule").split(", ")
        assert set(why) <= {"crosspost", "linkpost_moved_url"} and why == sorted(why), o.override_id
        assert ("crosspost" in why) == (m.group("cont") is not None), o.override_id
        assert ("linkpost_moved_url" in why) == (m.group("moved") is not None), o.override_id
        rules[", ".join(why)] += 1
        if len(ids) == 3:
            assert why == ["crosspost", "linkpost_moved_url"], o.override_id
    assert max(named.values()) == 1, [d for d, n in named.items() if n > 1]
    assert len(named) == 305
    assert rules == {"crosspost": 132, "linkpost_moved_url": 14, "crosspost, linkpost_moved_url": 6}
    fixed = {o.key for o in _of(FIELDS)}
    assert not fixed & set(named), sorted(fixed & set(named))


def test_gather_takes_every_merge() -> None:
    ops = identity_overrides.from_rows(_rows())
    assert len(ops.merges) == 152 and not ops.separates and not ops.masks
    assert sum(len(ss) for _oid, ss in ops.merges) == 305


#: The rubric's worked-example quote (its start), which the model copied into these documents' technique rows.
LEAK = "If I had a research budget I would put all of it on monitoring reasoni"


def test_the_technique_rows_with_the_rubric_example_are_dropped() -> None:
    spec = REGISTRY["technique_staged"]
    dropped: Counter[str] = Counter()
    for o in _of("technique_staged"):
        assert o.kind == "row" and o.item_key == "" and o.override_id == f"ts-2026-10-30-{o.key}"
        assert o.scope_members and o.key in o.scope_members and re.fullmatch(r"[0-9a-f]{64}", o.scope_basis_digest or "")
        pay, why = _apply(spec, None, [o])
        assert why is None and pay is not None, (o.override_id, why)
        quotes = [q["text"] for t in pay["techniques"] + pay["extra_techniques"] for q in t["quotes"]]
        quotes += [q["text"] for g in pay["candidate_gaps"] for q in g["quotes"]]
        assert not any(LEAK in q for q in quotes), o.override_id
        empty = not pay["techniques"] and not pay["extra_techniques"]
        assert empty == ("no_techniques_found" in pay["flags"]), o.override_id
        m = _LEAK_NOTE.match(o.note or "")
        assert m is not None, o.override_id
        slugs = m.group("slugs").split(", ")
        assert not {t["slug"] for t in pay["techniques"]} & set(slugs), o.override_id
        dropped.update(slugs)
        # select applies it while the members and the text are the scoped ones, and not otherwise
        live = sorted(o.scope_members)
        assert _applies(o, live, o.scope_basis_digest)
        assert not _applies(o, live, "0" * 64) and not _applies(o, [*live, "sz" + "0" * 11], o.scope_basis_digest)
    assert dropped == {"chain-of-thought-monitoring": 4, "rlhf": 1}
