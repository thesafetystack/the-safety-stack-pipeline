"""Freeze (or check) the input-only eval set technique_staged 2026-10-28a: the 100 documents of the held-out judge set
``evals/judge-2026-10-25j/technique_staged`` (same item ids jtec-0001..jtec-0100, same doc ids and texts), frozen for
``stack eval technique_staged`` so the effort-high arm can run on them (the confirmation step of the 2026-10-27
technique round, ``PROTOCOL.md``).

Run from the repo root (no LLM call, no network, the store read only)::

    uv run --no-sync python evals/technique_staged/2026-10-28a/scripts/freeze.py <STORE_DIR> <PRIVATE_EVALS_ROOT> [--write]

Only the judge set's ``items.jsonl`` is read (ids, doc ids, text sha256, characters, licence class, stratum, weight,
title): no label file of that set. The sample has a ``restricted`` text, so the texts never enter the repository: the
complete set (``items.jsonl``, ``manifest.json``, ``texts/jtec-NNNN.txt.gz``, an empty ``gold.jsonl``) is written under
``<PRIVATE_EVALS_ROOT>/technique_staged/2026-10-28a/``; the repository's set directory holds the same ``items.jsonl``
and ``manifest.json``. Without ``--write`` everything is rebuilt in memory and compared byte for byte with both copies
(exit 1 naming each difference).

Texts: the store blob of the judge item's ``input.text_sha256`` (the text production and the judges read), checked
against its sha256 and character count, decoded strictly as UTF-8. Built with the current production code as
2026-10-23a's freeze, but with the production prompt set and taxonomy (so ``stack eval`` checks the single user
message): ``input`` = title, basis_role, text_digest (the blob sha256), full_text_digest, full_text_chars and
``staged`` = ``main.freeze_info(text, name=PROMPT_SET, taxonomy_path=TAXONOMY)``; ``input_digest`` = sha256 of the
``single`` user message; ``stratum`` = the judge set's doc_type stratum (``group``) plus a length ``band``; ``weight``
= the judge set's design weight; every item on the ``test`` side.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path.cwd()
SET_ID = "2026-10-28a"
HERE = ROOT / "evals/technique_staged" / SET_ID
SOURCE = ROOT / "evals/judge-2026-10-25j/technique_staged/items.jsonl"
TAXONOMY = "taxonomy/techniques-1.0.0.yaml"
PROMPT_SET = "technique-staged-v5-a1.1"
RUBRIC = "rubric-v0.9-c"
BANDS = ((20_000, "<20k"), (60_000, "20-60k"), (100_000, "60-100k"))


class FreezeError(Exception):
    pass


def sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


def dumps(rows: list[dict]) -> str:
    return "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows)


def gz(text: str) -> bytes:
    return gzip.compress(text.encode("utf-8"), compresslevel=9, mtime=0)


def band(chars: int) -> str:
    return next((name for cap, name in BANDS if chars < cap), ">100k")


def load(name: str, rel: str):
    sys.path.insert(0, str(ROOT))
    from stack.stage_runner import load_path_module

    return load_path_module(name, ROOT / rel)


def texts_of(store: Path, source: list[dict]) -> dict[str, str]:
    texts, bad = {}, []
    for r in source:
        digest, iid = r["input"]["text_sha256"], r["item_id"]
        path = store / "blobs" / digest
        if not path.is_file():
            bad.append(f"{iid} (no blob)")
            continue
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest:
            bad.append(f"{iid} (blob sha256)")
            continue
        try:
            texts[iid] = raw.decode("utf-8")
        except UnicodeDecodeError:
            bad.append(f"{iid} (not UTF-8)")
            continue
        if len(texts[iid]) != r["input"]["chars"]:
            bad.append(f"{iid} (chars {len(texts[iid])} != judge item {r['input']['chars']})")
    if bad:
        raise FreezeError("texts refused: " + ", ".join(bad))
    return texts


def build(store: Path) -> dict[str, str | bytes]:
    """The private set's files by set-relative path."""
    source = read_jsonl(SOURCE)
    texts = texts_of(store, source)
    main = load("_freeze_28a_technique_staged_main", "workflows/annotate/technique_staged/main.py")
    tax = ROOT / TAXONOMY
    items = []
    for r in sorted(source, key=lambda r: r["item_id"]):
        iid, text = r["item_id"], texts[r["item_id"]]
        items.append({
            "item_id": iid, "doc_id": r["doc_id"], "source_item": f"judge-2026-10-25j:{iid}", "split": "test",
            "stratum": {"group": r["stratum"], "band": band(len(text))}, "weight": r["weight"],
            "license_class": r["license_class"], "production_asks": True,
            "input": {"title": r["metadata"]["title"], "basis_role": r["input"]["basis_role"],
                      "text_digest": r["input"]["text_sha256"], "full_text_digest": sha(text),
                      "full_text_chars": len(text),
                      "staged": main.freeze_info(text, name=PROMPT_SET, taxonomy_path=tax)},
            "input_digest": sha(main.user_message(text, mode="single", name=PROMPT_SET))})
    cat = main.catalogue(tax)
    prompts = {mode: {call: main.prompt_file(PROMPT_SET, call).digest for call in main.CALLS[mode]} for mode in main.MODES}
    rubric = main.HERE / "prompts" / f"{RUBRIC}.md"
    text = dumps(items)
    strata: dict[str, dict[str, int]] = {}
    for it in items:
        for k, v in it["stratum"].items():
            strata.setdefault(k, {})[v] = strata.get(k, {}).get(v, 0) + 1
    lic: dict[str, int] = {}
    for it in items:
        lic[it["license_class"]] = lic.get(it["license_class"], 0) + 1
    man = {"task": "technique_staged", "set_id": SET_ID, "items": len(items),
           "source": ("the 100 items of evals/judge-2026-10-25j/technique_staged (the judge-eval round: a self-weighting sample of "
                      "build ws1qpn8myzxah's luna technique rows); texts = store blobs of the judge items' text_sha256"),
           "source_items_sha256": hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
           "gold": None, "input_only": True,
           "items_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
           "splits": {"test": len(items)},
           "strata": {k: dict(sorted(v.items())) for k, v in strata.items()}, "license_class": dict(sorted(lic.items())),
           "frozen_with": {"taxonomy_path": TAXONOMY, "taxonomy_sha256": cat.sha256, "taxonomy_version": cat.version,
                           "rubric": RUBRIC, "rubric_digest": hashlib.sha256(rubric.read_bytes()).hexdigest(),
                           "prompt_set": PROMPT_SET, "prompt_digests": prompts,
                           "segmenter_version": main.segment.SEGMENTER_VERSION,
                           "lexical_version": main.lexical.LEXICAL_VERSION},
           "truncated_items": sorted(i["item_id"] for i in items if i["input"]["full_text_chars"] > main.DOC_CHAR_CAP),
           "doc_char_cap": main.DOC_CHAR_CAP}
    files: dict[str, str | bytes] = {"items.jsonl": text,
                                     "manifest.json": json.dumps(man, indent=2, ensure_ascii=False) + "\n",
                                     "gold.jsonl": ""}
    files.update({f"texts/{iid}.txt.gz": gz(t) for iid, t in sorted(texts.items())})
    return files


REPO_FILES = ("items.jsonl", "manifest.json")


def main_(argv: list[str]) -> int:
    write = "--write" in argv
    args = [a for a in argv if not a.startswith("--")]
    if len(args) != 2:
        print(__doc__)
        return 2
    store, private = Path(args[0]), Path(args[1])
    try:
        files = build(store)
    except FreezeError as exc:
        print(f"refused: {exc}")
        return 1
    diffs = []
    for dest, names in ((private / "technique_staged" / SET_ID, list(files)), (HERE, REPO_FILES)):
        for name in names:
            data = files[name]
            data = data.encode("utf-8") if isinstance(data, str) else data
            p = dest / name
            if write:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(data)
            elif not p.is_file() or p.read_bytes() != data:
                diffs.append(f"{'repo' if dest == HERE else 'private'} {name}")
    if not write:
        stale = sorted(p.name for p in (private / "technique_staged" / SET_ID / "texts").glob("*.txt.gz")
                       if f"texts/{p.name}" not in files)
        diffs += [f"private stale texts/{n}" for n in stale]
    for d in diffs:
        print("differs:", d)
    print(("written" if write else "checked"), len(files), "files;", len(diffs), "differences")
    return 1 if diffs else 0


if __name__ == "__main__":
    sys.exit(main_(sys.argv[1:]))
