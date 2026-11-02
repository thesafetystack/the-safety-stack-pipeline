#!/usr/bin/env python3
"""``tools/org_vocab.py`` — apply the organisation vocabulary to a release corpus and report what it covers.

The matcher is :mod:`stack.orgvocab` (D736: run by the stage ``annotate.affiliation_orgs``). This tool reads a
dataset release's ``data/corpus.parquet`` (public or internal variant; the internal one has uncapped surfaces), runs
every author's affiliation surfaces through the matcher (:meth:`~stack.orgvocab.OrgMatcher.match_author`: a local
name such as "Institute of Automation" resolves against the same author's other affiliations) and writes into
``--out``:

* ``summary.json``: mentions (one per (document, author, affiliation)), distinct surfaces and documents by matcher
  status; coverage by mentions (a mention is covered when the matcher named at least one organisation) and by
  documents (a document with affiliation mentions is covered when at least one of them is, and *fully* covered when
  every author with an affiliation has a covered one); both split by the affiliation's ``resolution`` tier; the
  most frequent organisations, by documents;
* ``review-unmatched.tsv`` / ``review-ambiguous.tsv`` / ``review-generic.tsv``: the most frequent unmatched,
  ambiguous and generic surfaces with mention and document counts (the ambiguous ones with their candidate sets,
  the generic ones with the local-name entries they could be), for a human to add aliases;
* ``sample-mentions.tsv`` / ``sample-surfaces.tsv`` (``--sample N``): N random matched (surface, organisation)
  pairs drawn by mention (what a reader of the data meets) and N drawn by distinct surface (the long tail), with
  the organisation's name, type and country and an empty ``verdict`` column, for a precision check;
* ``matches.parquet``: one row per distinct surface: status, org ids, open candidate sets, mentions, documents.

    uv run --no-sync python tools/org_vocab.py --corpus <release>/data/corpus.parquet --out <dir> [--sample 200]

Reads nothing else; writes only ``--out``.
"""

from __future__ import annotations

import argparse
import collections
import json
import random
import sys
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from stack import duck
from stack.orgvocab import STATUSES, load

MENTIONS_SQL = """
with it as (select doc_id, unnest(annotations.affiliation.payload.items) as i from '{corpus}'
            where annotations.affiliation.metadata.status = 'ok'),
af as (select doc_id, i.byline_index as bi, i.resolution as res, unnest(i.affiliations) as a from it)
select doc_id, bi, res, a.surface as surface from af
"""


def _tsv(path: Path, header: list[str], rows: list[list[Any]]) -> None:
    def cell(v: Any) -> str:
        return str("" if v is None else v).replace("\t", " ").replace("\r", "\\r").replace("\n", "\\n")
    with path.open("w", encoding="utf-8") as f:
        f.write("\t".join(header) + "\n")
        for r in rows:
            f.write("\t".join(cell(v) for v in r) + "\n")


def _pct(a: int, b: int) -> float:
    return round(100.0 * a / b, 2) if b else 0.0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tools/org_vocab.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("--corpus", required=True, type=Path, help="a release's data/corpus.parquet")
    ap.add_argument("--vocab", type=Path, default=None, help="vocabulary file (default: stack.orgvocab.DEFAULT_PATH)")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--top", type=int, default=300, help="rows in each review list")
    ap.add_argument("--sample", type=int, default=0, help="matched pairs per precision sample (0: none)")
    ap.add_argument("--seed", type=int, default=20261027)
    a = ap.parse_args(argv)

    vocab = load(a.vocab)
    m = vocab.matcher
    a.out.mkdir(parents=True, exist_ok=True)
    con = duck.connect()  # D732: spills under the scratch directory
    con.execute("SET memory_limit='2GB'")
    rows = con.execute(MENTIONS_SQL.format(corpus=str(a.corpus).replace("'", "''"))).fetchall()
    got = con.execute(f"select count(*) from '{a.corpus}' where annotations.affiliation.metadata.status = 'ok'"
                      ).fetchone()
    docs_ok = got[0] if got else 0

    surf_mentions: collections.Counter[str] = collections.Counter()
    surf_docs: dict[str, set[str]] = collections.defaultdict(set)
    for doc, _bi, _res, s in rows:
        surf_mentions[s or ""] += 1
        surf_docs[s or ""].add(doc)
    result = {s: m.match(s) for s in surf_mentions}
    # per mention, with the author's other affiliations as context (OrgMatcher.match_author)
    authors: dict[tuple[str, int], list[int]] = collections.defaultdict(list)
    for i, (doc, bi, _res, _s) in enumerate(rows):
        authors[(doc, bi)].append(i)
    mres: list[Any] = [None] * len(rows)
    for idxs in authors.values():
        for i, r in zip(idxs, m.match_author([rows[i][3] or "" for i in idxs])):
            mres[i] = r
    surf_status: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    for (_d, _b, _r, s), r in zip(rows, mres):
        surf_status[s or ""][r.status] += 1

    by_status = {st: {"mentions": 0, "surfaces": 0} for st in STATUSES}
    by_res: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    doc_any: dict[str, bool] = {}
    author_cov: dict[tuple[str, int], bool] = {}
    org_docs: dict[str, set[str]] = collections.defaultdict(set)
    root_docs: dict[str, set[str]] = collections.defaultdict(set)
    for (doc, bi, res, s), r in zip(rows, mres):
        by_status[r.status]["mentions"] += 1
        covered = bool(r.orgs)
        by_res[res or "?"]["mentions"] += 1
        by_res[res or "?"]["covered"] += int(covered)
        by_res[res or "?"][r.status] += 1
        doc_any[doc] = doc_any.get(doc, False) or covered
        author_cov[(doc, bi)] = author_cov.get((doc, bi), False) or covered
        for o in r.orgs:
            org_docs[o].add(doc)
            root_docs[m.root(o)].add(doc)
    for s, c in surf_status.items():
        by_status[c.most_common(1)[0][0]]["surfaces"] += 1  # a surface counts under its most frequent status
    doc_full: dict[str, bool] = {}
    for (doc, _bi), cov in author_cov.items():
        doc_full[doc] = doc_full.get(doc, True) and cov
    n_mentions = len(rows)
    covered_mentions = sum(1 for r in mres if r.orgs)
    used = set(org_docs)
    summary = {
        "corpus": a.corpus.name,
        "vocab": {"version": vocab.version, "sha256": vocab.digest, "orgs": len(m.entries),
                  "orgs_matched_in_corpus": len(used),
                  "by_type": dict(collections.Counter(e.type for e in m.entries.values())),
                  "aliases": sum(len(e.aliases) for e in m.entries.values()),
                  "cased": sum(len(e.cased) for e in m.entries.values()),
                  "patterns": sum(len(e.patterns) for e in m.entries.values())},
        "documents_affiliation_ok": docs_ok,
        "documents_with_mentions": len(doc_any),
        "mentions": n_mentions,
        "distinct_surfaces": len(surf_mentions),
        "by_status": {st: {**v, "mentions_pct": _pct(v["mentions"], n_mentions),
                           "surfaces_pct": _pct(v["surfaces"], len(surf_mentions))} for st, v in by_status.items()},
        "coverage": {
            "mentions_covered": covered_mentions,
            "mentions_covered_pct": _pct(covered_mentions, n_mentions),
            "mentions_covered_pct_excl_non_org": _pct(covered_mentions, n_mentions - by_status["non_org"]["mentions"]),
            "documents_covered": sum(doc_any.values()),
            "documents_covered_pct": _pct(sum(doc_any.values()), len(doc_any)),
            "documents_fully_covered": sum(doc_full.values()),
            "documents_fully_covered_pct": _pct(sum(doc_full.values()), len(doc_full)),
            "authors_covered_pct": _pct(sum(author_cov.values()), len(author_cov)),
        },
        "by_resolution": {k: {**dict(v), "covered_pct": _pct(v["covered"], v["mentions"])}
                          for k, v in sorted(by_res.items(), key=lambda kv: -kv[1]["mentions"])},
        "top_orgs_by_documents": [
            {"id": o, "name": m.entries[o].name, "type": m.entries[o].type, "documents": len(d)}
            for o, d in sorted(org_docs.items(), key=lambda kv: -len(kv[1]))[:60]],
        "top_roots_by_documents": [
            {"id": o, "name": m.entries[o].name, "documents": len(d)}
            for o, d in sorted(root_docs.items(), key=lambda kv: -len(kv[1]))[:40]],
    }
    (a.out / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", "utf-8")

    def ranked(status: str) -> list[str]:
        return sorted((s for s, c in surf_status.items() if c[status]),
                      key=lambda s: (-surf_status[s][status], -len(surf_docs[s]), s))[:a.top]
    _tsv(a.out / "review-unmatched.tsv", ["surface", "mentions", "documents"],
         [[s, surf_status[s]["unmatched"], len(surf_docs[s])] for s in ranked("unmatched")])
    _tsv(a.out / "review-generic.tsv", ["surface", "mentions", "documents", "local_candidates"],
         [[s, surf_status[s]["generic"], len(surf_docs[s]), ",".join(result[s].local[:8])] for s in ranked("generic")])
    _tsv(a.out / "review-ambiguous.tsv", ["surface", "mentions", "documents", "resolved", "open"],
         [[s, surf_status[s]["ambiguous"], len(surf_docs[s]), ",".join(result[s].orgs),
           " | ".join(",".join(c) for c in result[s].ambiguous)] for s in ranked("ambiguous")])

    tbl = pa.table({
        "surface": list(result),
        "status": [r.status for r in result.values()],
        "org_ids": [list(r.orgs) for r in result.values()],
        "open": [[list(c) for c in r.ambiguous] for r in result.values()],
        "via": [[f"{h.via}{':' + h.resolved_by if h.resolved_by else ''}" for h in r.hits] for r in result.values()],
        "mentions": [surf_mentions[s] for s in result],
        "documents": [len(surf_docs[s]) for s in result],
    })
    pq.write_table(tbl, a.out / "matches.parquet")

    if a.sample:
        rng = random.Random(a.seed)
        mention_pairs = [(row[3] or "", o) for row, r in zip(rows, mres) for o in r.orgs]
        by_mention = rng.sample(mention_pairs, k=min(a.sample, len(mention_pairs)))
        pairs = sorted(set(mention_pairs))
        by_surface = rng.sample(pairs, k=min(a.sample, len(pairs)))
        head = ["n", "surface", "org_id", "org_name", "type", "country", "parent", "mentions", "verdict", "note"]
        for name, pick in (("sample-mentions.tsv", by_mention), ("sample-surfaces.tsv", by_surface)):
            _tsv(a.out / name, head, [[i + 1, s, o, m.entries[o].name, m.entries[o].type, m.entries[o].country,
                                       m.entries[o].parent, surf_mentions[s], "", ""]
                                      for i, (s, o) in enumerate(pick)])
    print(json.dumps({k: summary[k] for k in ("mentions", "distinct_surfaces", "by_status", "coverage")}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
