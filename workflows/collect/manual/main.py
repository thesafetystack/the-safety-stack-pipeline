"""``collect.manual`` — sources added by hand: the ``manual_sources`` rows of the ``overrides``
family (round 4, track L: a way to add source URLs and sources-to-link-collect by hand, each with flags to
collect its links and to be accepted in triage; D336-D338).

**The entries** are added with ``stack overrides add manual_sources.yaml`` (``docs/howto/overrides.md``;
validated there, :class:`stack.overrides.ManualSource`), a YAML list ::

    - override_id: ms-0001                 # unique in the overrides chain; what a later entry revokes
      url: https://example.org/some/page   # an absolute http(s) URL
      note: why it is here                 # kept in the source's record
      flags: [collect_links, accept]       # optional; values: collect_links, accept
      title: Some Title                    # optional (D666): the source's metadata, as a `fields` override's
      authors: [A. Author, B. Author]      #   value: names or {name, orcid, affiliation_raw}
      published_at: 2015-01-01             #   an ISO date or time; a year alone is <year>-01-01
      added_by: tomas
      added_at: 2026-10-12
    - {override_id: ms-0005, revokes: ms-0001}   # retire an entry (or `supersedes:` with a new url/flags)

An unknown flag, a flag listed twice, a URL that is not http(s) or an ill-typed field is refused by the add. The stage
reads the pinned ``overrides`` input (``--in data/overrides/<series>/``; given none it reads none
and retracts what it emitted before, D377; :func:`stack.overrides.input_rows`) and takes the effective rows
(:func:`stack.overrides.effective`): two entries naming the same target (equal first typed key,
the row's ``key``) are one group, and the later one wins (D337).

**Rows.** Each effective entry's URL is typed by :func:`stack.urls.link_keys` → its identity group
→ channel ``manual_<group>`` (``manual_url``, ``manual_paper``, ``manual_forum``,
``manual_social``; ``configs/channels.yaml``; the v1 drop-folder channel ``manual`` is not read
here). One ``sources`` row on ``sources.manual``: ``external_id`` = the URL as written in the file
(so a URL-rule change re-mints nothing), ``canonical_url`` = the link's fetch URL (an entry URL that pins one
Wayback capture's original bytes, ``web.archive.org/web/<timestamp>id_/<url>``, :func:`stack.urls.wayback_capture`,
is fetched as written: the live host answers scripted clients with another page, so the lane must not ask it; the
keys stay the live page's), keys = the
target's typed keys plus one non-merging **``flag``** key per flag
(:data:`stack.scholarly_records.FLAGS`), so the flags reach ``documents.identity.keys`` through
``gather`` like ``relay`` keys and rules read them in SQL (``stack/rules.py`` ``Match.keys``).
``metadata.origin = human``; ``title``, ``authors`` and ``published_at`` from the entry's fields when it
gives them (D666; typed by :func:`stack.overrides.field_value`, as a ``fields`` override's value), else NULL
(the paper lookups, acquire and annotate may supply them); gather merges them like any member's metadata
(``workflows/gather/merge.py``: the manual channels rank after every collected channel, before only the
link channels, so a manual value fills only what no collected member has; a ``fields`` override wins over
all); ``doc_type`` ``forum_post`` / ``tweet`` for the forum / social groups,
else NULL. The record = ``{url, note, flags, override_id, added_by, added_at, keys}`` plus the given fields
(``added_at`` the override's ``created_at``, ISO) (the row's ``provider_record`` in-band entry, D522),
observed with ``skip_unchanged``: an unchanged entry writes no row; a changed note, flag list or field
writes a new one (a removed flag disappears from the document at the next ``gather``).

**Retraction.** A source this stage observed earlier whose entry is no longer effective (revoked,
or replaced by a later entry with another URL) gets a miss row; the channels
say ``miss_means: withdrawn`` and the stage line sets ``miss_cap: 1``, so the row is
``retracted`` at once (its member is withdrawn at gather). The miss row carries the target keys
without the flags, so a retracted source's flags leave the document. A store with no effective
manual source retracts every manual source (counted ``entries_gone``; a ``missing`` row under a larger
``miss_cap`` is re-missed each processed run until the cap retracts it).

**Coverage.** One ``coverage`` row per touched channel and entry set: ``query`` =
``{"target": "manual_sources", "digest": <digest of the effective rows>}``, window ``[now, now]``;
an unchanged set is not processed again within ``relist_after_h`` (D226), and a changed set
(new digest) always is. No network.

Imports only ``stack.*`` and the stdlib.
"""

from __future__ import annotations

import json
from typing import Any

from stack import scholarly_records as sr
from stack.collect.base import RELIST_OPTIONS, Collector
from stack.overrides import (
    MANUAL_FIELDS,
    MANUAL_SOURCES,
    Override,
    OverrideError,
    effective,
    field_value,
    input_rows,
)
from stack.stage_runner import StageContext, StageError, StageResult
from stack.urls import LinkTarget, link_keys, wayback_capture

#: The ``options`` keys this stage's code reads; any other is refused (D434)
OPTIONS: frozenset[str] = RELIST_OPTIONS

CHANNEL_PREFIX = "manual_"
#: The coverage ``query_id`` (``metadata.matched_by``) of every row.
QUERY_ID = f"manual:{MANUAL_SOURCES}"
#: ``doc_type`` known from the group alone (enums.yaml); the url and paper groups leave it NULL.
DOC_TYPE_BY_GROUP = {"forum": "forum_post", "social": "tweet"}


def channel_of(target: LinkTarget) -> str:
    return f"{CHANNEL_PREFIX}{target.group}"


def entry_keys(target: LinkTarget, flags: list[str]) -> list[tuple[str, str]]:
    """The row's keys: the target's typed keys plus one ``flag`` key per flag (normalised and
    ordered by :func:`stack.scholarly_records.keys_for`)."""
    return [(k["kind"], k["value"]) for k in sr.keys_for([*target.keys, *(("flag", f) for f in flags)])]


def entry_url(e: Override) -> str:
    return str(e.payload["url"])


def entry_flags(e: Override) -> list[str]:
    return list(e.payload.get("flags") or [])


def entry_fields(e: Override) -> dict[str, Any]:
    """The entry's given :data:`~stack.overrides.MANUAL_FIELDS`, as written (JSON data)."""
    return {f: e.payload[f] for f in MANUAL_FIELDS if e.payload.get(f) is not None}


def entry_metadata(e: Override, target: LinkTarget) -> dict[str, Any]:
    """The source's ``metadata``: ``origin = human``, the group's ``doc_type``, the entry's fields typed."""
    try:
        typed = {f: field_value(f, v) for f, v in entry_fields(e).items()}
    except ValueError as exc:  # overrides.add refuses such a value
        raise StageError(f"collect.manual: manual source {e.override_id}: {exc}") from None
    return {"origin": "human", "doc_type": DOC_TYPE_BY_GROUP.get(target.group)} | typed


def entry_record(e: Override, target: LinkTarget) -> dict[str, Any]:
    """The source's record; the fields only when given, so an entry without them keeps its earlier record."""
    return {"url": entry_url(e), "note": e.note, "flags": sorted(entry_flags(e)), "override_id": e.override_id,
            "added_by": e.author, "added_at": e.created_at.isoformat(),
            "keys": [list(k) for k in target.keys]} | entry_fields(e)


def _prior_rows(ctx: StageContext) -> list[dict[str, Any]]:
    """The own chain's latest row per source (empty on a first run)."""
    if ctx.prior.get("sources") is None and ctx.inputs.get("sources") is None:
        return []
    rows: list[dict[str, Any]] = ctx.view("sources").to_pylist()
    return rows


def run(ctx: StageContext) -> StageResult:
    c = Collector(ctx)
    try:
        eff = effective(input_rows(ctx), MANUAL_SOURCES)
    except OverrideError as exc:
        raise StageError(str(exc)) from None
    query = {"target": MANUAL_SOURCES, "digest": eff.digest}
    relist = c.relist_after()

    # Effective entries by channel, in override_id order.
    by_channel: dict[str, list[tuple[Override, LinkTarget]]] = {}
    active: set[tuple[str, str]] = set()
    for e in sorted(eff.rows, key=lambda o: o.override_id):
        target = link_keys(entry_url(e))
        if target is None or not target.keys:  # overrides.add refuses such a URL
            raise StageError(f"{ctx.stage}: manual source {e.override_id}: url {entry_url(e)!r} is not http(s)")
        ch = channel_of(target)
        c.channel(ch)  # an unconfigured group fails loudly here
        by_channel.setdefault(ch, []).append((e, target))
        active.add((ch, entry_url(e)))
    # Earlier manual sources whose entry is gone: retracted (module docstring).
    gone: dict[str, list[dict[str, Any]]] = {}
    for r in _prior_rows(ctx):
        ident = r["identity"]
        if r["state"] != "retracted" and (ident["channel"], ident["external_id"]) not in active:
            gone.setdefault(ident["channel"], []).append(r)

    tally = {"entries": len(active), "flags": 0, "sources_new": 0, "entries_gone": 0}
    for ch in sorted(set(by_channel) | set(gone)):
        if not c.due(ch, query, relist_after=relist):
            continue  # this file version was processed within relist_after_h (D226)
        with c.window(ch, query, c.now, c.now) as w:
            for e, target in by_channel.get(ch, []):
                url, flags = entry_url(e), entry_flags(e)
                w.seen += 1
                tally["flags"] += len(flags)
                new = c.prior(ch, url) is None
                record = entry_record(e, target)
                row = c.observe(
                    ch, url, record, keys=entry_keys(target, flags),
                    canonical_url=wayback_capture(url) or target.fetch_url,
                    metadata=entry_metadata(e, target),
                    provider_record=record,  # the curated entry is the source's record (D522)
                    skip_unchanged=True, query=query, query_id=QUERY_ID)
                if row is not None and new:
                    w.kept += 1
                    tally["sources_new"] += 1
            for r in gone.get(ch, []):
                keys = [(k["kind"], k["value"]) for k in r["identity"]["keys"] if k["kind"] != "flag"]
                c.miss(ch, r["identity"]["external_id"], query=query, keys=keys,
                       query_id=QUERY_ID)
                tally["entries_gone"] += 1
            w.complete()
    c.counts.update(tally)
    return c.finish()
