"""T-SCALE item 1: gather's spanning walk over edges grouped by component equals the walk over
every edge (the grouping only removes the documents x edges scan, measurement S-17)."""

from __future__ import annotations

import random

from workflows.gather.union import Edge, components, edges_by_component, spanning_edges


def _graph(rng: random.Random, n: int) -> tuple[list[str], list[Edge]]:
    sources = [f"s{i:04d}" for i in range(n)]
    es: list[Edge] = []
    for _ in range(int(n * 0.9)):
        a, b = rng.sample(sources, 2)
        kind = rng.choice(["key:doi", "key:url", "match:paper_title_author", "human:merge"])
        key = (kind.split(":", 1)[1], f"v{rng.randrange(50)}") if kind.startswith("key:") else None
        es.append(Edge(a, b, kind, key, "o1" if kind == "human:merge" else None))
    return sources, es


def test_grouped_walk_equals_full_walk() -> None:
    for seed in range(40):
        rng = random.Random(seed)
        sources, es = _graph(rng, rng.randrange(2, 80))
        rank = {s: (rng.random(), s) for s in sources}

        def order(s: str, rank: dict[str, tuple[float, str]] = rank) -> tuple[object, ...]:
            return rank[s]

        docs = {c[0]: c for c in components(sources, es, order)}
        grouped = edges_by_component(docs, es)
        assert sum(len(v) for v in grouped.values()) == len(es)
        for d, members in docs.items():
            assert spanning_edges(members, d, grouped.get(d, []), order) == spanning_edges(members, d, es, order)
            assert all(e.a in members and e.b in members for e in grouped.get(d, []))


def test_group_keeps_edge_order() -> None:
    es = [Edge("a", "b", "key:doi", ("doi", "1"), None), Edge("c", "d", "key:doi", ("doi", "2"), None),
          Edge("b", "a", "match:paper_title_author", None, None), Edge("d", "c", "human:merge", None, "o")]
    grouped = edges_by_component({"a": ["a", "b"], "c": ["c", "d"]}, es)
    assert grouped == {"a": [es[0], es[2]], "c": [es[1], es[3]]}
