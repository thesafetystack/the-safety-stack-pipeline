"""Freeze (or check) the technique_staged eval set 2026-10-17a from its staged inputs (no LLM call, no network).

Run from the repo root: ``uv run --no-sync python evals/technique_staged/2026-10-17a/scripts/freeze.py [--write]``.

Inputs, all in the repository:

* an-0001..an-0025: ``evals/technique_roles/2026-10-13a`` (``items.jsonl``: doc_id, source_item, split, stratum,
  license_class, title, basis_role, text_digest; ``texts/``), each text checked against its ``full_text_digest``;
* an-0026..an-0036: ``sources/mirror-docs.jsonl`` and ``texts/an-00NN.txt.gz`` of this set (staged by
  ``scripts/stage.py`` from a read-only store copy), each text checked against the store's ``text_digest``;
* ``adjudication/texts.sha256``: the labellers' copies of all 36 texts must hash to the texts frozen here;
* ``adjudication/reconciled.jsonl`` (the final labels) and ``adjudication/adj-B.jsonl`` (``labeler`` per item) for
  ``gold.jsonl``.

Built with the current production code (``workflows/annotate/technique_staged/main.py``):

* ``items.jsonl``: per item ``input`` = title, basis_role, text_digest, full_text_digest, full_text_chars and
  ``staged`` = ``main.freeze_info(text)`` (segmenter / lexical versions, taxonomy sha256, segmentation and lexical
  digests, ``staged_fills_digest``, chunk count, and the client ``input_digest`` of the user message of ``single``,
  ``lexical`` and every P chunk); ``input_digest`` = sha256 of the ``single`` user message (the harness's input
  check); ``production_asks`` true. Split: the 25 keep 2026-10-13a's; the 11 new alternate dev/test by length within
  channel group (sorted by (channel_group, chars, item_id)). Results are reported over all 36 (``--split all``).
* ``gold.jsonl``: per item ``{item_id, doc_id, doc_kind, label{techniques[{slug, roles, quote, confidence, sources,
  alt_slugs, implemented_with, note[, candidate]}], extra_techniques, menu_gaps}, labelers, adjudicator,
  adjudication{dropped, remap_decisions, doc_note}}``; ``note`` = the reconciler's ``decision`` text, else the row's
  ``reason`` (rows the reconciler kept as one labeller wrote them carry that labeller's reason); a ``candidate:<slug>``
  row (an entry proposed for the taxonomy, not admitted) carries ``candidate: true`` and is left out of slug scoring.
  Every other gold slug must be a v0.9 technique or group node.
* ``manifest.json``: task, set_id, items, items_sha256, splits, gold {file, sha256, counts}, frozen_with {taxonomy,
  rubric and prompt digests, segmenter and lexical versions}.

Without ``--write`` it rebuilds everything in memory and compares it with the files byte for byte (exit 1 on any
difference, naming it). With ``--write`` it writes ``items.jsonl``, ``gold.jsonl``, ``manifest.json`` and the texts of
an-0001..an-0025. Any digest mismatch refuses in both modes.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path.cwd()
OUT = ROOT / "evals/technique_staged/2026-10-17a"
OLD = ROOT / "evals/technique_roles/2026-10-13a"
SET_ID = "2026-10-17a"
OLD_IDS = [f"an-{n:04d}" for n in range(1, 26)]
NEW_IDS = [f"an-{n:04d}" for n in range(26, 37)]
CANDIDATE = "candidate:"


class FreezeError(Exception):
    """A digest mismatch or a malformed input: nothing is written."""


def sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


def dumps(rows: list[dict]) -> str:
    return "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows)


def gz(text: str) -> bytes:
    return gzip.compress(text.encode("utf-8"), compresslevel=9, mtime=0)


def ungz(path: Path) -> str:
    return gzip.decompress(path.read_bytes()).decode("utf-8")


def load_main():  # noqa: ANN201 - the workflow module
    sys.path.insert(0, str(ROOT))
    from stack.stage_runner import load_path_module

    return load_path_module("_freeze_technique_staged_main", ROOT / "workflows/annotate/technique_staged/main.py")


def texts_and_meta(out: Path) -> tuple[dict[str, str], dict[str, dict]]:
    """``(item_id -> text, item_id -> base item)`` from the staged inputs, every digest checked."""
    old = {i["item_id"]: i for i in read_jsonl(OLD / "items.jsonl")}
    if sorted(old) != OLD_IDS:
        raise FreezeError(f"{OLD}/items.jsonl: expected {OLD_IDS[0]}..{OLD_IDS[-1]}")
    texts: dict[str, str] = {}
    base: dict[str, dict] = {}
    bad: list[str] = []
    for iid in OLD_IDS:
        it = old[iid]
        text = ungz(text_file(OLD, iid))
        if sha(text) != it["input"]["full_text_digest"]:
            bad.append(f"{iid}: 2026-10-13a text does not hash to its full_text_digest")
        texts[iid] = text
        base[iid] = {"item_id": iid, "doc_id": it["doc_id"], "source_item": it["source_item"], "split": it["split"],
                     "stratum": it["stratum"], "license_class": it["license_class"],
                     "title": it["input"]["title"], "basis_role": it["input"]["basis_role"],
                     "text_digest": it["input"]["text_digest"]}
    mirror = {m["item_id"]: m for m in read_jsonl(out / "sources" / "mirror-docs.jsonl")}
    if sorted(mirror) != NEW_IDS:
        raise FreezeError(f"sources/mirror-docs.jsonl: expected {NEW_IDS[0]}..{NEW_IDS[-1]}")
    for iid in NEW_IDS:
        m = mirror[iid]
        path = text_file(out, iid)  # staged by scripts/stage.py
        text = ungz(path)
        if sha(text) != m["text_digest"]:
            bad.append(f"{iid}: staged text does not hash to the store's text_digest")
        if path.read_bytes() != gz(text):
            bad.append(f"{iid}: staged text is not the deterministic gzip of itself")
        texts[iid] = text
        base[iid] = {"item_id": iid, "doc_id": m["doc_id"], "source_item": f"mirror:{m['doc_id']}", "split": None,
                     "stratum": {"channel": m["channel"], "channel_group": m["channel_group"], "proxy": None},
                     "license_class": m["license_class"], "title": m["title"], "basis_role": m["basis_role"],
                     "text_digest": m["text_digest"]}
    sums = dict(reversed(line.split("  ", 1)) for line in (out / "adjudication" / "texts.sha256")
                .read_text("utf-8").splitlines() if line.strip())
    for iid, text in texts.items():
        if sums.get(f"{iid}.txt") != sha(text):
            bad.append(f"{iid}: the labellers' text (adjudication/texts.sha256) differs from the frozen text")
    if bad:
        raise FreezeError("digest mismatch: " + "; ".join(bad))
    return texts, base


def new_splits(base: dict[str, dict], texts: dict[str, str]) -> dict[str, str]:
    """dev/test for the new items: alternate by length within channel group."""
    order = sorted(NEW_IDS, key=lambda i: (base[i]["stratum"]["channel_group"], len(texts[i]), i))
    out, seen = {}, {}
    for iid in order:
        g = base[iid]["stratum"]["channel_group"]
        out[iid] = "dev" if seen.get(g, 0) % 2 == 0 else "test"
        seen[g] = seen.get(g, 0) + 1
    return out


def build_items(main, texts: dict[str, str], base: dict[str, dict]) -> list[dict]:  # noqa: ANN001
    splits = new_splits(base, texts)
    items = []
    for iid in [*OLD_IDS, *NEW_IDS]:
        b, text = base[iid], texts[iid]
        items.append({
            "item_id": iid, "doc_id": b["doc_id"], "source_item": b["source_item"],
            "split": b["split"] or splits[iid], "stratum": b["stratum"], "license_class": b["license_class"],
            "production_asks": True,
            "input": {"title": b["title"], "basis_role": b["basis_role"], "text_digest": b["text_digest"],
                      "full_text_digest": sha(text), "full_text_chars": len(text), "staged": main.freeze_info(text)},
            "input_digest": sha(main.user_message(text, mode="single"))})
    return items


def build_gold(out: Path, items: list[dict], slugs: set[str]) -> list[dict]:
    rec = {r["item_id"]: r for r in read_jsonl(out / "adjudication" / "reconciled.jsonl")}
    labeler_b = {r["item_id"]: r["labeler"] for r in read_jsonl(out / "adjudication" / "adj-B.jsonl")}
    bad: list[str] = []
    gold = []
    for it in items:
        iid = it["item_id"]
        r = rec[iid]
        techniques = []
        for t in r["techniques"]:
            slug = str(t["slug"])
            row = {"slug": slug, "roles": dict(t["roles"]), "quote": t["quote"], "confidence": t["confidence"],
                   "sources": list(t.get("sources") or []), "alt_slugs": list(t.get("alt_slugs") or []),
                   "implemented_with": list(t.get("implemented_with") or []),
                   "note": t.get("decision") or t.get("reason")}
            if slug.startswith(CANDIDATE):
                row["candidate"] = True
            elif slug not in slugs:
                bad.append(f"{iid}:{slug}")
            techniques.append(row)
        gold.append({"item_id": iid, "doc_id": it["doc_id"], "doc_kind": r["doc_kind"],
                     "label": {"techniques": techniques, "extra_techniques": list(r.get("extra_techniques") or []),
                               "menu_gaps": list(r.get("menu_gaps") or [])},
                     "labelers": ["opus-a", labeler_b[iid]], "adjudicator": "opus-reconciler",
                     "adjudication": {"dropped": list(r.get("dropped") or []),
                                      "remap_decisions": list(r.get("remap_decisions") or []),
                                      "doc_note": r.get("doc_note")}})
    if bad:
        raise FreezeError(f"gold slugs not in the v0.9 taxonomy (techniques + group nodes): {', '.join(bad)}")
    return gold


def frozen_with(main) -> dict:  # noqa: ANN001
    cat = main.catalogue(main.REPO / main.DEFAULT_TAXONOMY)
    name = main.DEFAULT_NAME
    prompts = {mode: {call: main.prompt_file(name, call).digest for call in main.CALLS[mode]} for mode in main.MODES}
    rubric = main.HERE / "prompts" / "rubric-v0.9-a.md"
    return {"taxonomy_path": main.DEFAULT_TAXONOMY, "taxonomy_sha256": cat.sha256, "taxonomy_version": cat.version,
            "rubric": rubric.name.removesuffix(".md"),
            "rubric_digest": hashlib.sha256(rubric.read_bytes()).hexdigest(),
            "prompt_set": name, "prompt_digests": prompts,
            "segmenter_version": main.segment.SEGMENTER_VERSION, "lexical_version": main.lexical.LEXICAL_VERSION}


def manifest(items_text: str, items: list[dict], gold_text: str, gold: list[dict], fw: dict) -> dict:
    rows = [t for g in gold for t in g["label"]["techniques"]]
    return {
        "task": "technique_staged", "set_id": SET_ID,
        "source": ("evals/technique_roles/2026-10-13a (an-0001..0025, texts byte-identical); a read-only store copy "
                   "(an-0026..0036: sources/mirror-docs.jsonl, staged by scripts/stage.py)"),
        "items": len(items), "items_sha256": hashlib.sha256(items_text.encode("utf-8")).hexdigest(),
        "splits": {s: sum(i["split"] == s for i in items) for s in ("dev", "test")},
        "gold": {"file": "gold.jsonl", "sha256": hashlib.sha256(gold_text.encode("utf-8")).hexdigest(),
                 "from": "adjudication/reconciled.jsonl", "rows": len(rows),
                 "candidate_rows": sum(bool(t.get("candidate")) for t in rows),
                 "extra_techniques": sum(len(g["label"]["extra_techniques"]) for g in gold),
                 "menu_gaps": sum(len(g["label"]["menu_gaps"]) for g in gold)},
        "frozen_with": fw,
    }


def build(out: Path = OUT) -> tuple[dict[str, str], dict[str, bytes]]:
    """``(text files by set-relative path, binary files by path)``: what the set should hold."""
    main = load_main()
    texts, base = texts_and_meta(out)
    items = build_items(main, texts, base)
    cat = main.catalogue(main.REPO / main.DEFAULT_TAXONOMY)
    gold = build_gold(out, items, set(cat.slugs))
    items_text, gold_text = dumps(items), dumps(gold)
    man = manifest(items_text, items, gold_text, gold, frozen_with(main))
    files = {"items.jsonl": items_text, "gold.jsonl": gold_text,
             "manifest.json": json.dumps(man, indent=2, ensure_ascii=False) + "\n"}
    blobs = {f"texts/{iid}.txt.gz": gz(texts[iid]) for iid in OLD_IDS}
    return files, blobs


def text_file(set_dir: Path, iid: str) -> Path:
    """``iid``'s whole text: the set's ``texts/`` file, else the eval-text cache's (the texts are not in the
    repository, D790; ``tools/fetch_eval_texts.py`` fills the cache)."""
    from stack.evals.freeze import find_text, missing_text_message

    p = find_text(set_dir, iid)
    if p is None:
        raise FreezeError(missing_text_message(set_dir, iid))
    return p


def text_bytes(set_dir: Path, rel: str) -> bytes | None:
    """The bytes of ``texts/<iid>.txt.gz`` of ``set_dir`` (its own file, else the cache's), None when neither."""
    from stack.evals.freeze import find_text

    p = find_text(set_dir, rel.removeprefix("texts/").removesuffix(".txt.gz"))
    return p.read_bytes() if p is not None else None


def main(write: bool, out: Path = OUT) -> int:
    try:
        files, blobs = build(out)
    except FreezeError as exc:
        print(f"refused: {exc}")
        return 1
    if write:
        (out / "texts").mkdir(parents=True, exist_ok=True)
        for rel, data in blobs.items():
            (out / rel).write_bytes(data)
        for rel, text in files.items():
            (out / rel).write_text(text, encoding="utf-8")
        print(f"wrote {', '.join(files)} and {len(blobs)} texts")
        return 0
    differ = [rel for rel, text in files.items()
              if not (out / rel).is_file() or (out / rel).read_text("utf-8") != text]
    differ += [rel for rel, data in blobs.items() if text_bytes(out, rel) != data]
    if differ:
        print(f"differs: {', '.join(differ)}")
        return 1
    print(f"reproduced: {', '.join(files)} byte for byte, {len(blobs) + len(NEW_IDS)} texts identical")
    return 0


if __name__ == "__main__":
    raise SystemExit(main("--write" in sys.argv[1:]))
