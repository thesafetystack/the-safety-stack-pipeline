"""One observation per ``source_id`` across every ``sources`` chain (§3.1 gather row, C30), and
the keys it merges on (§6, D56).

**Which row speaks for a source.** Each chain is read through its view (``ctx.view``: the newest
row per ``source_id`` within that chain, §7.3). Across chains the row with the latest
``observed_at`` wins; ties go to the larger ``chain_id`` (deterministic, no meaning). Rows with
``state = 'missing'`` (a miss: ``record_format = 'miss'``) never speak for a source: a miss says
nothing about identity. Where a chain's view row is a miss, that chain's newest **non-miss** row
in its resolved version speaks for it instead (the last in chain order, as the view would have
chosen it; R3-1, D262), so a non-retracting miss after an observation (``miss_cap`` > 1, ``miss_means:
unknown``) neither drops the source nor fails gather. A ``source_id`` with no non-miss row in any
input is not a member of any document (counted as ``misses_only``). A ``sources`` compaction
keeps the observed row under a miss (D264: the full file holds it, a ``migrate.restate`` part the
miss). A
``retracted`` row does speak (its keys still join); its member gets ``withdrawn_at`` = that row's
``observed_at``, ``withdrawn_reason = 'withdrawn'``.

**Observation stats** (``MEMBER.first_observed_at``, ``last_observed_at``,
``observation_count``) are the min, max and count of distinct ``observed_at`` over **every**
non-miss row of every file of each input chain's resolved version (the newest full file and its
parts, not only the view), so the anchor rule "earliest observed" does not drift when a source is
re-observed, and a row that restates an observation under its own ``observed_at`` (``stack migrate
rekey``, url-normalisation.md §3) is not a new one. A full file that compacts a chain (``stack
migrate upcast``'s ``fold.compact``, any later ``fold.sources``) keeps one row per source, so the
files before it no longer count; the ``documents`` head carries what they said (D261,
:func:`carry_stats`): ``first`` is lowered and ``last`` raised to the head member's values, and
``count`` = max(the recount, the head's count + the distinct ``observed_at`` later than the head's
``last``). Over an uncompacted chain this equals the recount; a gather over unchanged inputs
reproduces the head's values (idempotent). Not covered: an observation compacted away before any
gather saw it, or one appended after a compaction with an ``observed_at`` at or before the
head's ``last`` (a back-dated import); both are undercounted.

**Keys.** Collectors type keys (``KEY(kind, value)``; T2.1b's ``stack/scholarly_records.py``);
gather does not re-type them. A key merges iff its kind is in the source's channel group's
``merge_keys`` (``channels.yaml`` ``channel_groups``) and not in the channel's
``non_merging_keys``. An unknown channel is an error (fail loudly). A key of another group's kind that
:data:`stack.identity.CROSS_GROUP_KEYS` names (a linkpost's ``url`` key on a forum source) is a
**bridge key**: it joins that group's edges (``Observation.bridge_keys``).
"""

from __future__ import annotations

import datetime as dt
import functools
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc

from stack import config as stack_config
from stack import duck, identity
from stack.store.chain import Chain
from stack.store.parquet import read_family

Row = dict[str, Any]


class GatherError(Exception):
    """Inputs ``gather`` refuses: no ``sources`` input, an unknown channel, an override naming an
    unseen source, a head member absent from every ``sources`` chain."""


@dataclass(frozen=True)
class ChannelIndex:
    """Channel → group, group → merging key kinds, channel → non-merging kinds."""

    group: Mapping[str, str]
    merge_keys: Mapping[str, frozenset[str]]
    non_merging: Mapping[str, frozenset[str]]

    def group_of(self, channel: str) -> str:
        try:
            return self.group[channel]
        except KeyError:
            raise GatherError(f"channel {channel!r} is not in channels.yaml") from None

    def merging_kinds(self, channel: str) -> frozenset[str]:
        return self.merge_keys.get(self.group_of(channel), frozenset()) - self.non_merging[channel]

    def bridge_keys(self, channel: str, keys: tuple[tuple[str, str], ...]) -> tuple[tuple[str, str, str], ...]:
        """``(group, kind, value)`` of the keys that join another group (:func:`stack.identity.bridge_keys`)."""
        return identity.bridge_keys(self.group_of(channel), keys, self.non_merging[channel])


def channel_index(config_dir: Path | None = None) -> ChannelIndex:
    """The :class:`ChannelIndex` of ``channels.yaml`` (``stack.config.load``: the dir, else
    ``STACK_CONFIG_DIR``, else the repo's ``configs/``; cached per resolved choice)."""
    return _channel_index(str(config_dir) if config_dir is not None else os.environ.get("STACK_CONFIG_DIR"))


@functools.cache
def _channel_index(config_dir: str | None) -> ChannelIndex:
    cfg = stack_config.load(Path(config_dir) if config_dir is not None else None).channels
    return ChannelIndex(
        group={c.id: c.channel_group for c in cfg.channels},
        merge_keys={g: frozenset(d.merge_keys) for g, d in cfg.channel_groups.items()},
        non_merging={c.id: frozenset(c.non_merging_keys) for c in cfg.channels},
    )


@dataclass(frozen=True)
class Observation:
    """The row that speaks for one source, its group, keys and stats."""

    source_id: str
    chain_id: str
    row: Row
    channel: str
    group: str
    keys: tuple[tuple[str, str], ...]           # every key, in the row's order
    merge_keys: tuple[tuple[str, str], ...]     # the merging subset
    first_observed_at: dt.datetime
    last_observed_at: dt.datetime
    observation_count: int
    bridge_keys: tuple[tuple[str, str, str], ...] = ()  # (group, kind, value) joining another group

    @property
    def withdrawn_at(self) -> dt.datetime | None:
        return self.row["observed_at"] if self.row["state"] == "retracted" else None


def latest_rows(views: Mapping[str, pa.Table], chains: Mapping[str, Chain] | None = None,
                ) -> tuple[dict[str, tuple[str, Row]], int]:
    """Per ``source_id`` the speaking row and its chain (module docstring), and the number of
    ``source_id``s seen only as misses. ``chains`` (``chain_id`` → resolved version) supplies the
    older non-miss row of a source whose view row is a miss; without it such a source is silent."""
    best: dict[str, tuple[str, Row]] = {}
    missed: set[str] = set()

    def offer(chain_id: str, r: Row) -> None:
        cur = best.get(r["source_id"])
        if cur is None or (r["observed_at"], chain_id) >= (cur[1]["observed_at"], cur[0]):
            best[r["source_id"]] = (chain_id, r)

    for chain_id in sorted(views):
        missed_here: set[str] = set()
        for r in views[chain_id].to_pylist():
            if r["state"] == "missing":
                missed_here.add(r["source_id"])
            else:
                offer(chain_id, r)
        if missed_here and chains is not None and chain_id in chains:
            for r in last_observed(chains[chain_id], missed_here).values():
                offer(chain_id, r)
        missed |= missed_here
    return best, len(missed - set(best))


def last_observed(chain: Chain, source_ids: set[str]) -> dict[str, Row]:
    """Per ``source_id`` in ``source_ids`` its last non-miss row in ``chain``'s resolved version, in
    chain order (later file wins, as in the view); sources with none are absent."""
    out: dict[str, Row] = {}
    wanted = pa.array(sorted(source_ids), pa.string())
    for cf in chain.files:
        tbl = read_family(cf.path)[0]
        keep = pc.and_(pc.is_in(tbl["source_id"], value_set=wanted), pc.not_equal(tbl["state"], "missing"))
        for r in tbl.filter(keep).to_pylist():
            out[r["source_id"]] = r
    return out


def observation_stats(chains: Mapping[str, Chain]) -> dict[str, list[dt.datetime]]:
    """``source_id`` → the sorted distinct ``observed_at`` of every non-miss row of every file of
    ``chains`` (module docstring). Each file is read through the family reader (its three columns
    at the current format version, D295), the aggregation runs in duckdb over the Arrow table."""
    cols = ["source_id", "state", "observed_at"]
    parts = [read_family(p, cols)[0] for ch in chains.values() for p in ch.paths]
    if not parts:
        return {}
    con = duck.connect()
    try:
        con.execute("SET TimeZone = 'UTC'")
        con.register("obs", pa.concat_tables(parts))
        rows = con.execute(  # through arrow: duckdb's own timestamptz → Python needs pytz
            "SELECT source_id AS s, list(DISTINCT observed_at ORDER BY observed_at) AS ts "
            "FROM obs WHERE state <> 'missing' GROUP BY source_id").to_arrow_table().to_pylist()
    finally:
        con.close()
    return {r["s"]: r["ts"] for r in rows}


Stats = tuple[dt.datetime, dt.datetime, int]


def head_stats(head: Mapping[str, Row]) -> dict[str, Stats]:
    """``source_id`` → ``(first, last, count)`` as the ``documents`` head's members record them;
    a source listed in several head rows (a live row and a tombstone's frozen copy) takes the
    earliest ``first`` and the entry with the latest ``(last, count)``."""
    out: dict[str, Stats] = {}
    for r in head.values():
        for m in r["identity"]["members"]:
            f, la, n = m["first_observed_at"], m["last_observed_at"], m["observation_count"]
            if f is None or la is None or n is None:
                continue
            cur = out.get(m["source_id"])
            if cur is None:
                out[m["source_id"]] = (f, la, n)
            else:
                out[m["source_id"]] = (min(cur[0], f), *max(cur[1:], (la, n)))
    return out


def carry_stats(times: list[dt.datetime], held: Stats | None) -> Stats:
    """``(first, last, count)`` from the distinct observation times in the chains and the head's
    stats for the source (module docstring, D261)."""
    first, last, n = times[0], times[-1], len(times)
    if held is None:
        return first, last, n
    hf, hl, hn = held
    return min(first, hf), max(last, hl), max(n, hn + sum(t > hl for t in times))


def observations(views: Mapping[str, pa.Table], chains: Mapping[str, Chain],
                 channels: ChannelIndex, head: Mapping[str, Row] | None = None,
                 ) -> tuple[dict[str, Observation], int]:
    """Every source that speaks (module docstring), by ``source_id``, and the misses-only count.
    ``head`` (``doc_id`` → ``documents`` row) carries observation stats across compactions."""
    best, misses_only = latest_rows(views, chains)
    stats = observation_stats(chains)
    held = head_stats(head or {})
    out: dict[str, Observation] = {}
    for s, (chain_id, r) in best.items():
        ident = r["identity"] or {}
        channel = ident.get("channel")
        if channel is None:
            raise GatherError(f"source {s} in {chain_id}: identity.channel is NULL")
        group = channels.group_of(channel)
        kinds = channels.merging_kinds(channel)
        keys = tuple((k["kind"], k["value"]) for k in ident.get("keys") or [])
        first, last, n = carry_stats(stats.get(s) or [r["observed_at"]], held.get(s))
        out[s] = Observation(
            source_id=s, chain_id=chain_id, row=r, channel=channel, group=group, keys=keys,
            merge_keys=tuple(k for k in keys if k[0] in kinds), first_observed_at=first,
            last_observed_at=last, observation_count=n, bridge_keys=channels.bridge_keys(channel, keys))
    return out, misses_only


__all__ = ["ChannelIndex", "GatherError", "Observation", "carry_stats", "channel_index", "head_stats",
           "last_observed", "latest_rows", "observation_stats", "observations"]
