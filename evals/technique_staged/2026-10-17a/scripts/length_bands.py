"""Production length-band shares for length-weighted scores (read-only; no LLM call).

Run from the repo root::

    uv run --no-sync python evals/technique_staged/2026-10-17a/scripts/length_bands.py <STORE_DIR> [--json]

``STORE_DIR`` is a read-only copy of a production store (``data/documents``, ``data/screen``, ``blobs``). The
population is the newest ``documents`` row of each doc with a current text (``content.current_text_digest`` set,
``content.current_text_status = 'ok'``); the text length is its character count (UTF-8 decoded blob; a blob under
20,000 bytes has under 20,000 characters and is not decoded; a document whose blob the store copy lacks is
counted in ``blob_missing`` and left out). Three populations are reported:

* ``stack_relevant``: those whose ``annotations.stack_relevance`` verdict is ``relevant`` (the documents
  ``annotate.technique_staged`` would read in production), when the store carries that annotation;
* ``all_text``: every document with a text (the fallback when no ``stack_relevance`` verdict is present);
* ``screen_relevant``: those whose newest ``screen`` row's verdict is ``relevant`` (the older relevance screen; a
  sensitivity check).

Bands: ``<20k``, ``20-60k``, ``60-100k``, ``>100k`` characters (upper bounds exclusive), the bands of
:data:`stack.evals.technique_staged.LENGTH_BANDS`.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

BANDS = (("<20k", 20_000), ("20-60k", 60_000), ("60-100k", 100_000), (">100k", None))


def band(chars: int) -> str:
    return next(name for name, hi in BANDS if hi is None or chars < hi)


def main(store: Path, as_json: bool) -> int:
    import duckdb

    con = duckdb.connect()
    con.execute("SET memory_limit = '2GB'; SET threads = 2")
    docs = str(store / "data" / "documents" / "*" / "*.parquet")
    rows = con.execute(f"""
        select doc_id, dig, sr from (
          select doc_id, content.current_text_digest as dig, content.current_text_status as st,
                 annotations.stack_relevance.payload.verdict as sr,
                 row_number() over (partition by doc_id order by updated_at desc) as rn
          from read_parquet('{docs}', union_by_name=true))
        where rn = 1 and dig is not null and st = 'ok'""").fetchall()
    screen = {r[0] for r in con.execute(f"""
        select doc_id from (
          select doc_id, verdict, row_number() over (partition by doc_id order by decided_at desc) as rn
          from read_parquet('{store / "data" / "screen" / "*" / "*.parquet"}', union_by_name=true))
        where rn = 1 and verdict = 'relevant'""").fetchall()}
    pops: dict[str, dict[str, int]] = {p: dict.fromkeys((b for b, _ in BANDS), 0)
                                       for p in ("stack_relevant", "all_text", "screen_relevant")}
    decoded = 0
    missing = Counter[str]()
    for doc_id, dig, sr in rows:
        path = store / "blobs" / dig
        try:
            size = path.stat().st_size
        except FileNotFoundError:  # a partial store copy: counted, left out
            missing["all_text"] += 1
            missing["stack_relevant"] += sr == "relevant"
            missing["screen_relevant"] += doc_id in screen
            continue
        if size < 20_000:
            chars = size
        else:
            chars = len(path.read_bytes().decode("utf-8", errors="replace"))
            decoded += 1
        b = band(chars)
        pops["all_text"][b] += 1
        if sr == "relevant":
            pops["stack_relevant"][b] += 1
        if doc_id in screen:
            pops["screen_relevant"][b] += 1
    out = {"documents_with_text": len(rows), "decoded": decoded, "blob_missing": dict(missing),
           "stack_relevance_present": any(sr is not None for _, _, sr in rows), "populations": {}}
    for p, counts in pops.items():
        n = sum(counts.values())
        out["populations"][p] = {"n": n, "counts": counts,
                                 "shares": {b: round(c / n, 4) for b, c in counts.items()} if n else None}
    if as_json:
        print(json.dumps(out, indent=2))
        return 0
    print(f"documents with text: {out['documents_with_text']} (decoded {decoded}; blob missing, left out: "
          f"{dict(missing)}); stack_relevance verdicts present: {out['stack_relevance_present']}")
    for p, v in out["populations"].items():
        shares = ", ".join(f"{b} {s:.3f}" for b, s in (v["shares"] or {}).items())
        print(f"  {p:<16} n={v['n']:>6}  {shares}")
    return 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--json"]
    if len(args) != 1:
        raise SystemExit(__doc__)
    raise SystemExit(main(Path(args[0]), "--json" in sys.argv[1:]))
