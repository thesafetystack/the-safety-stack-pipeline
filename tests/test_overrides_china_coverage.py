"""The prepared ``configs/overrides/china_coverage.yaml`` (the Chinese-coverage work): the Concordia-database arXiv papers absent from
the 2026-10-27 release and the Chinese frameworks, standards, statements and reports of the Chinese-coverage review,
added with ``stack overrides add`` at build time.

What is checked: the file loads through the add's own reader and checks; every entry is an effective manual source
with a title and a date, the documents ``accept``, the Concordia papers without a flag (D754); it can be added beside the two other prepared files (no shared id, no shared
target); the arXiv entries land on ``manual_paper`` with the arXiv key (the 15 present documents join it), the documents on ``manual_url``; the Chinese-only documents carry Chinese titles and keep their identity
query parameters (the SAMR record page).
"""

from __future__ import annotations

import datetime as dt
import pathlib
import re
from collections import Counter
from typing import Any

from stack.overrides import MANUAL_SOURCES, check_add, effective, read_file

REPO = pathlib.Path(__file__).resolve().parent.parent
PREPARED = REPO / "configs" / "overrides" / "china_coverage.yaml"
OTHERS = [REPO / "configs" / "overrides" / "manual_sources.yaml", REPO / "configs" / "overrides" / "lineage_origins.yaml"]
NOW = dt.datetime(2026, 10, 30, tzinfo=dt.UTC)


def _rows(path: pathlib.Path = PREPARED) -> list[Any]:
    return check_add(read_file(path, now=NOW), [])


def test_china_coverage_loads_as_accepted_manual_sources() -> None:
    rows = _rows()
    assert {o.target for o in rows} == {MANUAL_SOURCES}
    kinds = Counter(o.override_id.split("-")[2] for o in rows)
    assert kinds == {"cdb": 107, "doc": 16}
    assert all(o.note and o.payload["title"] for o in rows)
    # D754: the frameworks and reports are accepted; the Concordia papers go through triage and the screen
    assert all(o.payload["flags"] == ["accept"] for o in rows if o.override_id.startswith("ms-cn-doc-"))
    assert not any(o.payload.get("flags") for o in rows if o.override_id.startswith("ms-cn-cdb-"))
    assert all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(o.payload["published_at"])) for o in rows)
    eff = effective(rows, MANUAL_SOURCES)
    assert len(eff.rows) == len(rows)  # no entry shadows another


def test_china_coverage_and_the_other_prepared_files_can_all_be_added() -> None:
    mine = _rows()
    for other in OTHERS:
        theirs = [o for o in check_add(read_file(other, now=NOW), []) if o.target == MANUAL_SOURCES]
        check_add(mine, theirs)
        assert not {o.key for o in mine} & {o.key for o in theirs}, other.name


def test_china_coverage_lands_on_the_intended_channels_and_keys() -> None:
    from stack.config import load
    from stack.urls import link_keys
    from workflows.collect.manual.main import channel_of

    channels = {c.id for c in load().channels.channels}
    for o in _rows():
        target = link_keys(o.payload["url"])
        assert target is not None and channel_of(target) in channels, o.override_id
        if o.override_id.startswith("ms-cn-cdb-"):
            assert target.group == "paper" and ("arxiv", o.override_id.removeprefix("ms-cn-cdb-")) in target.keys
        else:
            assert target.group == "url", o.override_id
    by_id = {o.override_id: o for o in _rows()}
    samr = link_keys(by_id["ms-cn-doc-gbt-46347-2025"].payload["url"])
    assert samr is not None and samr.keys == (
        ("url", "https://openstd.samr.gov.cn/bzgk/std/newGbInfo?hcno=120779016BBA61E4408DCECE3EAD92ED"),)
    assert re.search(r"[一-鿿]", by_id["ms-cn-doc-tc260-ai-safety-standards-system"].payload["title"])


def test_every_prepared_override_file_projects_to_a_public_row() -> None:
    """The dataset export refuses an override whose author has no public author/method in
    `configs/releases/dataset/overrides-public.yaml`, or whose public note keeps an internal marker or runs past
    `max_note_chars`: every prepared file's every entry passes that projection, so adding a file at build time
    cannot refuse the next release. The notes are one line (an institution list pasted with line breaks reads
    badly in the shipped note)."""
    from stack.fold.dataset import OverridesPolicy

    policy = OverridesPolicy(REPO / "configs" / "releases" / "dataset" / "overrides-public.yaml")
    cap = int(policy.raw["max_note_chars"])
    for path in [PREPARED, *OTHERS]:
        for o in read_file(path, now=NOW):
            if o.kind == "revoke":
                continue
            author, method = policy.author(o)
            assert author and method, (path.name, o.override_id)
            note = policy.note(o, policy.kind(o))
            assert not policy.marked(note) and len(note) <= cap and "\n" not in note, (path.name, o.override_id)
            assert "\n" not in (o.note or ""), (path.name, o.override_id)


def test_no_wechat_article_is_prepared() -> None:
    """mp.weixin.qq.com answers this pipeline's client with an interactive captcha page whatever the headers, so a
    WeChat URL yields that page, never the article: the four such entries were dropped (2026-10-30). One may come back
    only as another copy (a pinned Wayback capture) or a hand-made text, never as the WeChat URL."""
    from stack.urls import link_keys

    for o in _rows():
        target = link_keys(o.payload["url"])
        assert "weixin.qq.com" not in o.payload["url"] and target is not None, o.override_id
        assert not any("weixin.qq.com" in v for _, v in target.keys), o.override_id


def test_tc260_pdfs_are_pinned_wayback_captures() -> None:
    """tc260.org.cn answers scripted clients with its homepage: its two PDFs are pinned captures of the original
    bytes, which collect.manual keeps as the fetch URL (stack.urls.wayback_capture); the key is the live URL's."""
    from stack.urls import link_keys, wayback_capture

    tc260 = [o for o in _rows() if "tc260.org.cn" in o.payload["url"]]
    assert len(tc260) == 2
    for o in tc260:
        assert wayback_capture(o.payload["url"]) == o.payload["url"], o.override_id
        target = link_keys(o.payload["url"])
        assert target is not None and target.keys[0][1].startswith("https://tc260.org.cn/upload/"), o.override_id
