"""Stage the inputs of the technique_staged eval set 2026-10-17a (one-time import; no LLM call).

Run from the repo root::

    uv run --no-sync python evals/technique_staged/2026-10-17a/scripts/stage.py <GOLD_DIR> <STORE_DIR> [--write]

``GOLD_DIR`` is the labelling workspace (outside the repository): ``reconciled/an-00NN.json`` (36 final labels),
``adj-A/`` and ``adj-B/`` (the two independent labellings), ``texts/an-00NN.txt`` (the exact texts the labellers
read), ``new-items.tsv`` (an-0026..an-0036 -> doc_id), ``GOLD-GUIDE.md`` (v1.1) and ``ADJ-NOTES.md`` (the rulings).
``STORE_DIR`` is a read-only copy of a production store (``<STORE_DIR>/data/documents``, ``<STORE_DIR>/blobs``);
it is only read (duckdb over the parquet files, the text blobs by digest). Neither path is written into the set.

What it writes under the set directory (``--write``; without it, it only checks and reports):

* ``sources/mirror-docs.jsonl``: per new item (an-0026..an-0036) the document's metadata as the store holds it
  (newest row of the doc): ``title``, ``channel`` (the anchor member's) and ``channel_group`` (``channels.yaml``),
  ``license_class`` (the member that supplies the current text, as the annotate stage hands it to the client),
  ``basis_role`` (``content.current_role``), ``text_digest`` (``content.current_text_digest``), ``doc_type``;
* ``texts/an-0026..an-0036.txt.gz``: the texts (deterministic gzip), which must hash to the store's digest and be
  byte-identical to the labellers' copy in ``GOLD_DIR/texts``; an-0001..an-0025 are not staged here (their texts are
  the technique_roles 2026-10-13a set's; ``freeze.py`` reads them there and checks the labellers' copies against them
  through ``adjudication/texts.sha256``);
* ``adjudication/reconciled.jsonl``, ``adj-A.jsonl``, ``adj-B.jsonl``: the label files verbatim, one JSON object per
  item (sorted keys), with ``labeler`` added to adj-B rows (:data:`B_LABELER`);
* ``adjudication/texts.sha256``: sha256 of each labeller text (``GOLD_DIR/texts``), for the freeze's identity check;
* ``adjudication/GUIDE.md`` and ``adjudication/RULINGS.md``: the guide and the rulings, with a provenance header.

``freeze.py`` then builds ``items.jsonl``, ``gold.jsonl`` and ``manifest.json`` from these staged files alone.
"""
from __future__ import annotations

import csv
import gzip
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path.cwd()
OUT = ROOT / "evals/technique_staged/2026-10-17a"
NEW = [f"an-{n:04d}" for n in range(26, 37)]
ALL = [f"an-{n:04d}" for n in range(1, 37)]
#: Labeller of each adj-B file: Codex gpt-6-sol on batches B1, B3, B5, B7, Codex gpt-6-astra on B2, B4, B6, B8, a
#: second Opus on the three abstract-only items (an-0001, an-0002, an-0025; public status not verified, so not sent to
#: an external labeller). Batches as run (labelling log): B1 an-0001,0006,0021 | B2 0003,0026,0027 |
#: B3 0019,0020,0022 | B4 0024,0029,0033,0036 | B5 0002,0014,0015,0030,0031 | B6 0005,0017,0018,0032 |
#: B7 0012,0013,0016,0025,0028,0035 | B8 0004,0007,0008,0009,0010,0011,0023,0034.
BATCHES = {"B1": (1, 6, 21), "B2": (3, 26, 27), "B3": (19, 20, 22), "B4": (24, 29, 33, 36),
           "B5": (2, 14, 15, 30, 31), "B6": (5, 17, 18, 32), "B7": (12, 13, 16, 25, 28, 35),
           "B8": (4, 7, 8, 9, 10, 11, 23, 34)}
SOL, ASTRA = ("B1", "B3", "B5", "B7"), ("B2", "B4", "B6", "B8")
OPUS_B = ("an-0001", "an-0002", "an-0025")
B_LABELER = {f"an-{n:04d}": ("opus-b" if f"an-{n:04d}" in OPUS_B else "codex-sol" if b in SOL else "codex-astra")
             for b, ns in BATCHES.items() for n in ns}
GUIDE_HEADER = ("<!-- Copied verbatim from the labelling workspace (guide v1.1) for provenance. The menu file it names\n"
                "(menu-v0.9-gold.md: taxonomy v0.9 + group nodes + a 20-entry `candidate:` appendix) and the rubric\n"
                "file it names (rubric-v0.4.0.md) are not part of this set. -->\n\n")
RULINGS_HEADER = ("<!-- Copied verbatim from the labelling workspace for provenance: ambiguities the labellers reported,\n"
                  "with the rulings ([R]) the reconciler applied as binding. -->\n\n")


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def gz(text: str) -> bytes:
    return gzip.compress(text.encode("utf-8"), compresslevel=9, mtime=0)


def dumps(rows: list[dict]) -> str:
    return "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows)


def store_rows(store: Path, doc_ids: list[str]) -> dict[str, dict]:
    """The newest ``documents`` row of each doc (read only; no timestamp column is fetched)."""
    import duckdb

    con = duckdb.connect()
    con.execute("SET memory_limit = '2GB'; SET threads = 2")
    glob = str(store / "data" / "documents" / "*" / "*.parquet")
    rows = con.execute(
        f"""select doc_id, metadata.title as title, metadata.doc_type as doc_type,
                   identity.anchor_source_id as anchor,
                   [{{'source_id': m.source_id, 'channel': m.channel, 'license_class': m.license_class}}
                    for m in identity.members] as members,
                   content.current_text_digest as dig, content.current_role as basis_role,
                   content.current_source_id as cur_src
            from (select *, row_number() over (partition by doc_id order by updated_at desc) as rn
                  from read_parquet('{glob}', union_by_name=true) where doc_id in (select unnest(?)))
            where rn = 1""", [doc_ids]).fetchall()
    cols = ["doc_id", "title", "doc_type", "anchor", "members", "dig", "basis_role", "cur_src"]
    return {r[0]: dict(zip(cols, r, strict=True)) for r in rows}


def main(gold_dir: Path, store: Path, write: bool) -> int:
    sys.path.insert(0, str(ROOT))
    from stack import gate

    groups = gate.channel_groups()
    with (gold_dir / "new-items.tsv").open(encoding="utf-8") as fh:
        doc_of = {r[0]: r[1] for r in csv.reader(fh, delimiter="\t") if r}
    assert sorted(doc_of) == NEW, sorted(doc_of)
    found = store_rows(store, [doc_of[i] for i in NEW])
    problems: list[str] = []
    meta, texts = [], {}
    for iid in NEW:
        did = doc_of[iid]
        r = found.get(did)
        if r is None:
            problems.append(f"{iid}: doc {did} not in the store")
            continue
        raw = (store / "blobs" / r["dig"]).read_bytes()
        if sha(raw) != r["dig"]:
            problems.append(f"{iid}: blob does not hash to its digest")
        labeller = (gold_dir / "texts" / f"{iid}.txt").read_bytes()
        if labeller != raw:
            problems.append(f"{iid}: the labellers' text differs from the store's current text")
        members, anchor = r["members"] or [], r["anchor"]
        channel = next(str(m["channel"]) for m in members if m["source_id"] == anchor)
        supplier = next((m for m in members if m["source_id"] == r["cur_src"]), None)
        lic = (supplier or next(m for m in members if m["source_id"] == anchor))["license_class"]
        meta.append({"item_id": iid, "doc_id": did, "title": r["title"], "channel": channel,
                     "channel_group": groups[channel], "license_class": lic, "basis_role": r["basis_role"],
                     "doc_type": r["doc_type"], "text_digest": r["dig"]})
        texts[iid] = raw.decode("utf-8")
    labels = {}
    for kind in ("reconciled", "adj-A", "adj-B"):
        rows = []
        for iid in ALL:
            d = json.loads((gold_dir / kind / f"{iid}.json").read_text("utf-8"))
            if d.get("item_id") != iid:
                problems.append(f"{kind}/{iid}.json: item_id {d.get('item_id')!r}")
            if kind == "adj-B":
                d["labeler"] = B_LABELER[iid]
            rows.append(d)
        labels[kind] = rows
    text_sums = "".join(f"{sha((gold_dir / 'texts' / f'{i}.txt').read_bytes())}  {i}.txt\n" for i in ALL)
    if problems:
        print("\n".join(problems))
        return 1
    print(f"checked: {len(meta)} store documents (digests, labeller copies identical); "
          f"{sum(len(v) for v in labels.values())} label files")
    if not write:
        return 0
    (OUT / "sources").mkdir(parents=True, exist_ok=True)
    (OUT / "texts").mkdir(parents=True, exist_ok=True)
    (OUT / "adjudication").mkdir(parents=True, exist_ok=True)
    (OUT / "sources" / "mirror-docs.jsonl").write_text(dumps(meta), encoding="utf-8")
    for iid, text in texts.items():
        (OUT / "texts" / f"{iid}.txt.gz").write_bytes(gz(text))
    for kind, rows in labels.items():
        (OUT / "adjudication" / f"{kind}.jsonl").write_text(dumps(rows), encoding="utf-8")
    (OUT / "adjudication" / "texts.sha256").write_text(text_sums, encoding="utf-8")
    (OUT / "adjudication" / "GUIDE.md").write_text(
        GUIDE_HEADER + (gold_dir / "GOLD-GUIDE.md").read_text("utf-8"), encoding="utf-8")
    (OUT / "adjudication" / "RULINGS.md").write_text(
        RULINGS_HEADER + (gold_dir / "ADJ-NOTES.md").read_text("utf-8"), encoding="utf-8")
    print(f"wrote sources/mirror-docs.jsonl, {len(texts)} texts, adjudication/ ({', '.join(labels)})")
    return 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--write"]
    if len(args) != 2:
        raise SystemExit(__doc__)
    raise SystemExit(main(Path(args[0]), Path(args[1]), "--write" in sys.argv[1:]))
