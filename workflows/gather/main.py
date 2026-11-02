"""Stage ``gather`` (§3.1, §6; D3, D6, D7, D32): identity from every ``sources`` chain and the
``documents`` head, written as a **full** ``documents`` file (``blocks_written = identity,
metadata, inband``, plus ``content`` on a run that carries content, D6).

Inputs: every ``sources`` chain (``--in data/sources.<suffix>/<series>/`` per chain, D92; keyed
``sources/<family dir>`` when more than one) and the ``documents`` head (absent on the first
gather; if the ``documents`` chain has a head it must be passed, else the full file would drop
its ids, X22). Overrides: the ``identity`` (:mod:`~workflows.gather.overrides`) and ``fields``
(:mod:`~workflows.gather.merge`) rows of the pinned ``overrides`` input (``--in
data/overrides/<series>/``; required when the store has an ``overrides`` chain,
:func:`stack.overrides.input_rows`). ``config_digest`` = ``H`` of the stage line's digest, the
digests of the effective identity and fields rows (``None`` when there are none) and the
provider rank's digest.

Substages (D32; all pure, always run):

* ``union`` — observations (:mod:`~workflows.gather.normalise`), identity overrides, key and human
  edges, components (:mod:`~workflows.gather.union`), anchors and continuity against the head
  (:mod:`~workflows.gather.anchor`);
* ``merge`` — per document the ``identity`` block (:func:`identity_payload`; its
  ``override_ids`` also list the ``fields`` overrides the metadata used; its ``channels`` and ``keys`` also hold
  what a member's channel implies, :data:`IMPLIED_CHANNELS`), the ``inband``
  block (union of the members' entries) and the ``metadata`` block
  (:func:`workflows.gather.merge.metadata_payload`: ``fields`` overrides > provider rank > newest
  ``observed_at``, a year-only ``published_at`` after every other date, ``empty_is_absent``, author union);
* ``carry`` — row assembly against the head: a block whose content (stamps aside) equals the
  head's is carried with its stamps; a row with no block and no state change is the head row
  itself (C6); ``annotations`` come from the head row, ``content`` too except where members moved
  (D6 merge carry and split distribution, :mod:`workflows.gather.carry`); a new row gets D7's
  initial blocks.

**States** (§2.5): a new row and a revived tombstone are ``identified``; an existing row moves to
``identified`` when its ``members_digest`` or ``fields_digest`` changes, else keeps its state
(masking is orthogonal and keeps progress); a tombstone is ``retired`` with its other blocks
frozen and its members not live. ``created_at`` is set once and carried (X17); a changed row
restamps the flat block's ``run_id``/``updated_at``.

A full gather over unchanged sources and overrides therefore writes every row byte for byte as
the head has it, stamps included (C6; ``tests/test_gather_merge.py``).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from typing import Any

import pyarrow as pa

from stack import urls
from stack.checks import digests
from stack.overrides import OverrideError, input_rows
from stack.schemas.families import family
from stack.stage_runner import StageContext, StageResult
from workflows.gather import merge as field_merge
from workflows.gather import overrides as identity_overrides
from workflows.gather.anchor import Continuity, anchor_of, continuity
from workflows.gather.carry import carried_content, donors, empty_annotations
from workflows.gather.normalise import (
    GatherError,
    Observation,
    channel_index,
    observations,
)
from workflows.gather.overrides import IdentityOps
from workflows.gather.union import (
    Reach,
    components,
    edges,
    edges_by_component,
    spanning_edges,
)

SUBSTAGES = ("union", "merge", "carry")
#: gather reads no ``options``: any key on its line is refused (D434)
OPTIONS: frozenset[str] = frozenset()
PURE_SUBSTAGES = SUBSTAGES
GATHER_BLOCKS = ["identity", "metadata", "inband"]
DOCUMENTS_CHAIN = "documents"
STAMPS = ("run_id", "updated_at")

Row = dict[str, Any]

#: A member channel that implies another (D770): the Alignment Forum and LessWrong serve one ForumMagnum database, so
#: every Alignment Forum post is also at ``lesswrong.com/posts/<id>`` (checked on 2026-10-30 for a sample of posts held
#: only as Alignment Forum members, legacy 24-hex ids of the old agent-foundations forum included). A document with a
#: member of the key channel lists the implied channel in ``identity.channels`` and, per such member, the implied
#: channel's ``url`` key of its post (the ForumMagnum alternate of :func:`stack.urls.alternate_url_keys`) in
#: ``identity.keys`` with that member's ``source_id``. Nothing else changes: these are not observations, so the union,
#: the members, ``members_digest`` and ``fields_digest`` (no state change, no annotation basis) are as before.
IMPLIED_CHANNELS: Mapping[str, str] = {"alignmentforum": "lesswrong"}
_IMPLIED_HOSTS: Mapping[str, str] = {"lesswrong": "https://lesswrong.com/"}


def implied_keys(o: Observation) -> list[tuple[str, str]]:
    """The ``url`` keys a member's channel implies (:data:`IMPLIED_CHANNELS`): for each ``fm_post`` key of an Alignment
    Forum member, its post's URL key on LessWrong."""
    to = IMPLIED_CHANNELS.get(o.channel)
    if to is None:
        return []
    return [("url", alt) for kind, value in o.keys if kind == "fm_post"
            for alt in urls.alternate_url_keys(f"https://alignmentforum.org/posts/{value}")
            if alt.startswith(_IMPLIED_HOSTS[to])]


def _member_key(o: Observation, reach: Reach) -> tuple[str, str]:
    """``MEMBER.key_kind/key_value``: the key of the edge it joined by; else its first merging
    key, first key, canonical URL, or its ``source_id``."""
    if reach.key is not None:
        return reach.key
    if o.merge_keys:
        return o.merge_keys[0]
    if o.keys:
        return o.keys[0]
    url = (o.row["identity"] or {}).get("canonical_url")
    return ("url", url) if url else ("source_id", o.source_id)


def _inband_entries(o: Observation) -> list[Row]:
    return list((o.row["inband"] or {}).get("entries") or [])


def _entry_key(e: Row) -> tuple[str, str, str, str]:
    raw, text = e["raw"], e["text"]
    return (e["source_id"], e["content_role"], raw["sha256"] if raw else "", text["sha256"] if text else "")


def inband_payload(members: Sequence[str], obs: Mapping[str, Observation]) -> Row:
    """Union of the members' in-band entries, unique per (source, role, raw and text sha256), sorted."""
    entries: dict[tuple[str, str, str, str], Row] = {}
    for s in members:
        for e in _inband_entries(obs[s]):
            entries.setdefault(_entry_key(e), e)
    return {"entries": [entries[k] for k in sorted(entries)]}


def identity_payload(doc_id: str, members: Sequence[str], obs: Mapping[str, Observation],
                     reach: Mapping[str, Reach], first: Mapping[str, dt.datetime],
                     ops: IdentityOps, former: Sequence[str]) -> Row:
    """The ``identity`` block without its stamps (A.3)."""
    mems: list[Row] = []
    keys: set[tuple[str, str, str]] = set()
    for s in members:
        o, r = obs[s], reach[s]
        md, ident = o.row["metadata"] or {}, o.row["identity"] or {}
        mask = ops.masks.get(s)
        kind, value = _member_key(o, r)
        mems.append({
            "source_id": s, "channel": o.channel, "key_kind": kind, "key_value": value,
            "canonical_url": ident.get("canonical_url"), "title": md.get("title"),
            "published_at": md.get("published_at"), "doc_type": md.get("doc_type"),
            "license_class": md.get("license_class"), "is_live": mask is None,
            "edge_kind": r.edge_kind, "edge_to_source_id": r.edge_to,
            "override_id": mask.override_id if mask is not None else r.override_id,
            "first_observed_at": first[s], "last_observed_at": o.last_observed_at,
            "observation_count": o.observation_count, "withdrawn_at": o.withdrawn_at,
            "withdrawn_reason": "withdrawn" if o.withdrawn_at is not None else None,
        })
        keys.update((k, v, s) for k, v in [*o.keys, *implied_keys(o)])
    bridged = ops.bridged(members)
    live = [s for s in members if s not in ops.masks]
    masked = bool(bridged) or not live
    reason = "review_required" if bridged else (ops.masks[doc_id].reason if not live else None)
    return {
        "anchor_source_id": doc_id, "members": mems, "member_ids": list(members),
        "keys": [{"kind": k, "value": v, "source_id": s} for k, v, s in sorted(keys)],
        "channels": sorted({c for s in members for c in (obs[s].channel, IMPLIED_CHANNELS.get(obs[s].channel)) if c}),
        "former_doc_ids": list(former), "merged_into": None, "is_masked": masked,
        "mask_reason": reason, "override_ids": ops.touching(members),
        "members_digest": digests.members_digest(members),
    }


def _settle(payload: Row, head_block: Row | None, run_id: str, now: dt.datetime) -> tuple[Row, bool]:
    """The head's block (stamps included) when ``payload`` equals it stamps aside; else
    ``payload`` stamped by this run. Returns ``(block, changed)``."""
    if head_block is not None and {k: v for k, v in head_block.items() if k not in STAMPS} == payload:
        return head_block, False
    return {**payload, "run_id": run_id, "updated_at": now}, True


def _live_row(doc_id: str, head: Row | None, ident: Row, meta: Row, inband: Row,
              content: Row | None, run_id: str, now: dt.datetime) -> tuple[Row, bool]:
    """``content``: the carried block without stamps (:mod:`~workflows.gather.carry`), or
    ``None`` to keep the head row's."""
    identity, ch_i = _settle(ident, head and head["identity"], run_id, now)
    metadata, ch_m = _settle(meta, head and head["metadata"], run_id, now)
    inb, ch_b = _settle(inband, head and head["inband"], run_id, now)
    own = head["content"] if head is not None else None
    cont, ch_c = _settle(content, own, run_id, now) if content is not None else (own, False)
    if (head is None or head["state"] == "retired"
            or ident["members_digest"] != head["identity"]["members_digest"]
            or meta["fields_digest"] != head["metadata"]["fields_digest"]):
        state = "identified"
    else:
        state = head["state"]
    if head is not None and not (ch_i or ch_m or ch_b or ch_c) and state == head["state"]:
        return head, False
    return {
        "doc_id": doc_id, "state": state, "run_id": run_id,
        "created_at": head["created_at"] if head is not None else now, "updated_at": now,
        "identity": identity, "metadata": metadata, "inband": inb, "content": cont,
        "annotations": head["annotations"] if head is not None else empty_annotations(),
    }, True


def _tombstone(head: Row, merged_into: str | None, run_id: str, now: dt.datetime) -> tuple[Row, bool]:
    hid = head["identity"]
    payload = {k: v for k, v in hid.items() if k not in STAMPS}
    payload.update(merged_into=merged_into,
                   members=[{**m, "is_live": False} for m in hid["members"]])
    identity, changed = _settle(payload, hid, run_id, now)
    if not changed and head["state"] == "retired":
        return head, False
    return {**head, "state": "retired", "run_id": run_id, "updated_at": now, "identity": identity}, True


def _inputs(ctx: StageContext) -> tuple[list[str], str | None]:
    src = [k for k, ch in ctx.inputs.items() if ch.family == "sources"]
    docs = [k for k, ch in ctx.inputs.items() if ch.family == "documents"]
    if not src:
        raise GatherError("gather: no sources input (pass every sources chain: --in data/sources.<suffix>/<series>/)")
    if len(docs) > 1:
        raise GatherError(f"gather: more than one documents input {docs}")
    return src, docs[0] if docs else None


def run(ctx: StageContext) -> StageResult:
    src_keys, doc_key = _inputs(ctx)
    chains = {ctx.inputs[k].chain_id: ctx.inputs[k] for k in src_keys}
    views = {ctx.inputs[k].chain_id: ctx.view(k) for k in src_keys}
    head: dict[str, Row] = ({r["doc_id"]: r for r in ctx.view(doc_key).to_pylist()}
                            if doc_key is not None else {})
    now = dt.datetime.now(dt.UTC)

    with ctx.substage("union"):
        obs, misses_only = observations(views, chains, channel_index(), head)  # stats carried from the head
        first = {s: o.first_observed_at for s, o in obs.items()}

        def order(s: str) -> tuple[dt.datetime, str]:
            return (first[s], s)

        try:
            override_rows = input_rows(ctx)
        except OverrideError as exc:
            raise GatherError(str(exc)) from None
        ops = identity_overrides.from_rows(override_rows)
        identity_overrides.check_known(ops, obs)
        fops = field_merge.field_ops(override_rows)
        fops.check_known(obs)
        ctx.config_digest = digests.H({
            "stage": ctx.config_digest, "identity": ops.config_digest,
            "fields": fops.config_digest, "provider_rank": field_merge.rank_digest()})
        match_counts: dict[str, int] = {}
        es = edges(obs, ops, order, match_counts)
        docs = {anchor_of(c, order): c for c in components(list(obs), es, order)}
        cont: Continuity = continuity(docs, head)
        ctx.heartbeat()

    with ctx.substage("merge"):
        doc_override = {t: f for f, t in fops.resolve_docs(docs, cont.former).items()}
        payloads: dict[str, tuple[Row, Row, Row]] = {}
        comp_edges = edges_by_component(docs, es)
        for d, members in docs.items():
            reach = spanning_edges(members, d, comp_edges.get(d, []), order)
            inband = inband_payload(members, obs)
            ident = identity_payload(d, members, obs, reach, first, ops, cont.former[d])
            live_ids = {m["source_id"] for m in ident["members"] if m["is_live"]}
            meta, used = field_merge.metadata_payload(d, members, obs, live_ids, fops, doc_override.get(d),
                                                      inband["entries"])
            if used:
                ident["override_ids"] = sorted({*ident["override_ids"], *used})
            payloads[d] = (ident, meta, inband)

    with ctx.substage("carry"):
        rows: list[Row] = []
        changed = n_content = 0
        gives = donors(docs, head, cont.merged_into)
        absorbed: dict[str, list[str]] = {}
        for f, t in cont.merged_into.items():
            if t is not None:
                absorbed.setdefault(t, []).append(f)
        for d, (ident, meta, inband) in payloads.items():
            h = head.get(d)
            content = (carried_content(d, h["content"] if h is not None else None, gives[d], head,
                                       absorbed.get(d, [])) if d in gives else None)
            row, ch = _live_row(d, h, ident, meta, inband, content, ctx.run_id, now)
            rows.append(row)
            changed += ch
            n_content += row["content"] is not None and (h is None or row["content"] is not h["content"])
        for f, target in cont.merged_into.items():
            row, ch = _tombstone(head[f], target, ctx.run_id, now)
            rows.append(row)
            changed += ch
        rows.sort(key=lambda r: r["doc_id"])
        table = pa.Table.from_pylist(rows, schema=family("documents").schema)
        blocks = GATHER_BLOCKS + (["content"] if n_content else [])
        ctx.emit_full("documents", table, blocks,
                      chain_id=None if doc_key is not None else DOCUMENTS_CHAIN)

    live = [p[0] for p in payloads.values()]
    ctx.counts.update(
        sources=len(obs), misses_only=misses_only, documents=len(docs),
        multi_member=sum(len(m) > 1 for m in docs.values()), retired=len(cont.merged_into),
        merged=cont.merges, split=cont.splits, moved_out=cont.moved_out, revived=len(cont.revived),
        masked=sum(i["is_masked"] for i in live),
        review_required=sum(i["mask_reason"] == "review_required" for i in live),
        content_carried=n_content, changed=changed, carried=len(rows) - changed, **match_counts)
    for i in live:  # members joined per edge family (the anchor aside): key:<kind>, match:<name>, human:merge
        for m in i["members"]:
            if m["edge_kind"] != "anchor":
                k = f"joined_by_{m['edge_kind'].replace(':', '_')}"
                ctx.counts[k] = ctx.counts.get(k, 0) + 1
    return StageResult(scope_complete=True, rows_touched=changed)
