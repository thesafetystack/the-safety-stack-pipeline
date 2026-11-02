"""Union: connected components over (source, key) edges within a channel group, plus human
``merge`` edges (§6). REWRITE of v3 ``stack/core/writer.py`` ``register_source :783-943``
(mint-time hard-key dedup, matched **globally** on ``external_id``) and ``mark_duplicate
:2016-2064`` (star groups with an elected representative); here identity is recomputed from
keys on every gather, there are no stored pointers and no election (the anchor rule is
:mod:`workflows.gather.anchor`).

**Edges.** Two sources share a key edge iff they are in the same channel group and carry the
same **merging** key ``(kind, value)`` (:mod:`~workflows.gather.normalise`); no edge crosses
groups. Per key the edges form a star from the earliest source, unless a ``separate`` names a
pair of that key's sources: then every non-separated pair of the key's sources gets an edge
(so the separate removes exactly the pair's direct edges). Human ``merge`` edges
(``human:merge``) are added after, star from the entry's earliest source.

**Detectors** (:mod:`stack.identity`, round 5 T-ID). A source's bridge keys (a key that
``CROSS_GROUP_KEYS`` lets join another group) take part in that group's key edges. Matcher
edges ``match:<name>`` (:func:`stack.identity.match_pairs`) join the matched pairs; a
``separate`` removes them as it removes key edges.

**Spanning edges.** :func:`spanning_edges` walks a component breadth-first from its anchor,
neighbours in anchor order, key edges before human edges; each member records the edge it was
first reached by: ``MEMBER.edge_kind ∈ {anchor, key:<kind>, match:<name>, human:merge}``,
``edge_to_source_id``, the key ``(kind, value)`` of a key edge and the override id of a human
edge. Key edges are preferred to match edges, match edges to human edges.

**Match buckets count works.** :func:`stack.identity.match_pairs` gets each source's cluster over
the key edges (after the ``separate``s), so a title bucket is crowded by more than
``MAX_BUCKET`` works, not sources (S25-19).
"""

from __future__ import annotations

import itertools
from collections import defaultdict
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass

from stack import identity

from workflows.gather.normalise import Observation
from workflows.gather.overrides import IdentityOps

OrderKey = Callable[[str], tuple[object, ...]]


@dataclass(frozen=True)
class Edge:
    a: str
    b: str
    kind: str                       # 'key:<kind>', 'match:<name>' or 'human:merge'
    key: tuple[str, str] | None     # (kind, value) of a key edge
    override_id: str | None         # the merge entry of a human edge


@dataclass(frozen=True)
class Reach:
    """How a member joined its component (``MEMBER.edge_*``)."""

    edge_kind: str
    edge_to: str | None
    key: tuple[str, str] | None
    override_id: str | None


class _DSU:
    def __init__(self, items: Sequence[str]) -> None:
        self.parent = {s: s for s in items}

    def find(self, x: str) -> str:
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def edges(obs: Mapping[str, Observation], ops: IdentityOps, order: OrderKey,
          counts: MutableMapping[str, int] | None = None) -> list[Edge]:
    """Every key edge, match edge and human edge (module docstring). ``counts`` (if given) gets
    ``bridge_ambiguous`` (bridge keys of two or more sources, joining nothing), ``match_pairs_<name>``
    per matcher and ``match_crowded_<class>`` (title buckets skipped)."""
    by_key: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    bridging: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    for s in sorted(obs, key=order):
        o = obs[s]
        for k in dict.fromkeys(o.merge_keys):
            by_key[(o.group, *k)].append(s)
        for g, kind, value in o.bridge_keys:
            bridging[(g, kind, value)].append(s)
    ambiguous = 0
    for bk, bs in bridging.items():
        # two linkposts of one page: the page joins neither (stack.identity, 2.); one post's two
        # members (an LW/AF crosspost, one fm_post) are one linkpost
        if len({obs[s].merge_keys[:1] or (s,) for s in bs}) > 1:
            ambiguous += 1
            continue
        by_key[bk] = sorted([*by_key[bk], *bs], key=order)
    sep = ops.separated_pairs
    sep_sources = {s for p in sep for s in p}
    out: list[Edge] = []
    for (_, kind, value), ss in by_key.items():
        if len(ss) < 2:
            continue
        pairs = (itertools.combinations(ss, 2) if sep_sources.intersection(ss)
                 else ((ss[0], s) for s in ss[1:]))
        out += [Edge(a, b, f"key:{kind}", (kind, value), None)
                for a, b in pairs if frozenset((a, b)) not in sep]
    dsu = _DSU(sorted(obs))  # key clusters: a match bucket's crowding counts works, not sources
    for e in out:
        dsu.union(e.a, e.b)
    matched, stats = identity.match_pairs(obs, order, {s: dsu.find(s) for s in obs})
    out += [Edge(a, b, f"match:{name}", None, None) for a, b, name in matched
            if frozenset((a, b)) not in sep]
    if counts is not None:
        counts["bridge_ambiguous"] = ambiguous
        counts.update({f"match_pairs_{n}": v for n, v in stats.pairs.items()})
        counts.update({f"match_crowded_{k}": v for k, v in stats.crowded.items()})
    for oid, named in ops.merges:
        first, *rest = sorted(named, key=order)
        out += [Edge(first, s, "human:merge", None, oid) for s in rest]
    return out


def components(sources: Sequence[str], es: Sequence[Edge], order: OrderKey) -> list[list[str]]:
    """Connected components, each in anchor order, listed by their first member."""
    dsu = _DSU(sorted(sources))
    for e in es:
        dsu.union(e.a, e.b)
    comps: dict[str, list[str]] = defaultdict(list)
    for s in sorted(sources, key=order):
        comps[dsu.find(s)].append(s)
    return sorted(comps.values(), key=lambda c: order(c[0]))


def edges_by_component(docs: Mapping[str, Sequence[str]], es: Sequence[Edge]) -> dict[str, list[Edge]]:
    """``es`` grouped by component in one pass (``docs``: anchor → members, as
    :func:`components` partitions them), each group in ``es`` order, so
    :func:`spanning_edges` of a component sees only its own edges; the walk is the same as over
    all of ``es`` (an edge joins two members of one component). Without it gather's merge step is
    documents x edges (T-SCALE, measurement S-17: 175 s of 223 s at 150k documents)."""
    comp_of = {s: d for d, members in docs.items() for s in members}
    out: dict[str, list[Edge]] = defaultdict(list)
    for e in es:
        out[comp_of[e.a]].append(e)
    return dict(out)


#: Spanning-walk preference by edge family: a key edge, then a matcher's, then a human merge.
_EDGE_RANK = {"key": 0, "match": 1, "human": 2}


def spanning_edges(component: Sequence[str], anchor: str, es: Sequence[Edge],
                   order: OrderKey) -> dict[str, Reach]:
    """Member → :class:`Reach` by a breadth-first walk from ``anchor`` (module docstring)."""
    members = set(component)
    adj: dict[str, list[tuple[tuple[object, ...], str, Edge]]] = defaultdict(list)
    for e in es:
        if e.a in members and e.b in members:
            rank = _EDGE_RANK[e.kind.split(":", 1)[0]]
            adj[e.a].append(((rank, *order(e.b)), e.b, e))
            adj[e.b].append(((rank, *order(e.a)), e.a, e))
    reach = {anchor: Reach("anchor", None, None, None)}
    queue = [anchor]
    while queue:
        cur = queue.pop(0)
        for _, nxt, e in sorted(adj[cur], key=lambda t: t[0]):
            if nxt not in reach:
                reach[nxt] = Reach(e.kind, cur, e.key, e.override_id)
                queue.append(nxt)
    missing = members - set(reach)
    if missing:  # components() and spanning_edges() disagree: a bug, never data
        raise AssertionError(f"component of {anchor}: unreachable members {sorted(missing)}")
    return reach


__all__ = ["Edge", "Reach", "components", "edges", "edges_by_component", "spanning_edges"]
