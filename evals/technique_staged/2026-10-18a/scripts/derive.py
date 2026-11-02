"""Derive (or check) the technique_staged eval set 2026-10-18a from 2026-10-17b: the gold's ``candidate:<slug>`` rows
mapped to the entries the 234-entry v0.9 taxonomy admitted (no LLM call, no network).

Run from the repo root: ``uv run --no-sync python evals/technique_staged/2026-10-18a/scripts/derive.py [--write]``.

Inputs, all in the repository: ``evals/technique_staged/2026-10-17b`` (``gold.jsonl``, ``items.jsonl``, ``texts/``,
``manifest.json``; never written) and :data:`TAXONOMY` (``taxonomy/taxonomy-v0.9-234.yaml``).

Rules (techpipe D627), per gold row of 17b:

* a row flagged ``candidate`` (slug ``candidate:<slug>``): ``slug`` becomes ``<slug>``, the ``candidate`` flag is
  dropped and ``remapped_from: "candidate:<slug>"`` is added; ``<slug>`` must be a technique of :data:`TAXONOMY`
  (else refused). Everything else in the row is unchanged (``alt_slugs`` included);
* if the document then has two rows with one slug, the higher-confidence row is kept (high > med > low; a tie keeps
  the row that was not remapped, then the earlier) with a ``note`` naming the dropped row, and the pair is listed in
  the manifest (``derivation.duplicates``); 17b has none;
* every other row and every item field: unchanged. Every gold slug (techniques and group nodes) must be in
  :data:`TAXONOMY`, and no ``candidate:`` slug may remain (else refused).

Without ``--write`` it rebuilds ``gold.jsonl`` and ``manifest.json`` in memory and compares them, ``items.jsonl`` and
``texts/`` with the files byte for byte (exit 1 on any difference, naming it). With ``--write`` it copies
``items.jsonl`` and ``texts/`` from 17b and writes ``gold.jsonl`` and ``manifest.json``.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path.cwd()
SRC = ROOT / "evals/technique_staged/2026-10-17b"
OUT = ROOT / "evals/technique_staged/2026-10-18a"
SET_ID = "2026-10-18a"
TAXONOMY = "taxonomy/taxonomy-v0.9-234.yaml"
PREFIX = "candidate:"
RANK = {"high": 3, "med": 2, "low": 1}
INPUT_NOTE = ("The items' input.staged digests (taxonomy_sha256, lexical_digest, staged_fills_digest, "
              "user_digests.lexical) were frozen against taxonomy/taxonomy-v0.9.yaml (the 214-entry file), as were "
              "frozen_with.prompt_digests; runs here pin --pin taxonomy=taxonomy/taxonomy-v0.9-234.yaml (the gold's "
              "remapped slugs are not in the 214-entry file, so a run on it is refused). The input check then "
              "refuses staged runs (lexical_digest moves on 4 items, under every prompt set) and lexical runs under "
              "the frozen prompt set technique-staged-v1 (the lexical user message moves on 8 items); single and "
              "sweep runs pass (their checked digests, the single and P user messages and the segmentation, do not "
              "depend on the taxonomy), and user digests are not compared for prompt sets other than v1.")


class DeriveError(Exception):
    """A candidate slug the taxonomy lacks, a gold slug outside it, or items / texts that differ from 17b's."""


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text("utf-8").splitlines() if x.strip()]


def dumps(rows: list[dict]) -> str:
    return "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def taxonomy() -> tuple[set[str], frozenset[str], str, str]:
    """``(technique slugs, group-node slugs, sha256, version)`` of :data:`TAXONOMY`."""
    from stack.evals.technique_staged import taxonomy_kinds
    from stack.stage_runner import load_path_module

    path = ROOT / TAXONOMY
    kind, groups = taxonomy_kinds(path)
    tx = load_path_module("_technique_staged__taxonomy", ROOT / "workflows/annotate/technique_staged/taxonomy.py")
    assert tx is not None
    return set(kind), groups, sha(path.read_bytes()), str(tx.load_taxonomy(path).version)


def _keep_first(a: dict, b: dict) -> bool:
    """Whether row ``a`` (earlier in the item) wins over ``b`` (module docstring)."""
    ra, rb = RANK.get(a.get("confidence"), 0), RANK.get(b.get("confidence"), 0)
    if ra != rb:
        return ra > rb
    return not ("remapped_from" in a and "remapped_from" not in b)


def build_gold(techniques: set[str], groups: frozenset[str]) -> tuple[list[dict], dict]:
    gold = read_jsonl(SRC / "gold.jsonl")
    remapped: list[dict] = []
    duplicates: list[dict] = []
    out = []
    for g in gold:
        iid = g["item_id"]
        rows: list[dict] = []
        for t in g["label"]["techniques"]:
            if t.get("candidate"):
                slug = str(t["slug"])
                if not slug.startswith(PREFIX) or slug[len(PREFIX):] not in techniques:
                    raise DeriveError(f"{iid}: candidate row {slug!r} names no technique of {TAXONOMY}")
                new = slug[len(PREFIX):]
                t = {**{k: v for k, v in t.items() if k != "candidate"}, "slug": new, "remapped_from": slug}
                remapped.append({"item": iid, "from": slug, "to": new})
            rows.append(t)
        kept: list[dict] = []
        for t in rows:
            same = next((k for k in kept if k["slug"] == t["slug"]), None)
            if same is None:
                kept.append(t)
                continue
            win, lose = (same, t) if _keep_first(same, t) else (t, same)
            dropped = lose.get("remapped_from") or lose["slug"]
            win = {**win, "note": (f"{win.get('note') or ''} [18a: kept over a duplicate row {dropped} "
                                   f"(confidence {lose.get('confidence')}).]").strip()}
            kept[kept.index(same)] = win
            duplicates.append({"item": iid, "slug": t["slug"], "kept": win.get("remapped_from") or win["slug"],
                               "dropped": dropped})
        for t in kept:
            s = str(t["slug"])
            if s.startswith(PREFIX) or (s not in techniques and s not in groups):
                raise DeriveError(f"{iid}: gold slug {s!r} is not in {TAXONOMY}")
        out.append({**g, "label": {**g["label"], "techniques": kept}})
    derivation = {"from_set": "2026-10-17b", "from_gold_sha256": sha((SRC / "gold.jsonl").read_bytes()),
                  "rule": "candidate:<slug> rows -> <slug> (remapped_from), candidate flag dropped",
                  "remapped_rows": len(remapped), "remapped": remapped, "duplicates": duplicates,
                  "input_check": INPUT_NOTE}
    return out, derivation


def manifest(items_text: str, gold_text: str, gold: list[dict], derivation: dict, groups: frozenset[str],
             tax_sha: str, tax_version: str) -> dict:
    src = json.loads((SRC / "manifest.json").read_text("utf-8"))
    rows = [t for g in gold for t in g["label"]["techniques"]]
    scored = [t for t in rows if not t.get("candidate") and not t.get("dont_care") and t["slug"] not in groups]
    frozen_with = {**src["frozen_with"], "taxonomy_path": TAXONOMY, "taxonomy_sha256": tax_sha,
                   "taxonomy_version": tax_version,
                   "staged_inputs_taxonomy_sha256": src["frozen_with"]["taxonomy_sha256"]}
    return {
        "task": "technique_staged", "set_id": SET_ID,
        "source": ("evals/technique_staged/2026-10-17b (items.jsonl and texts byte-identical; gold derived by "
                   "scripts/derive.py: the candidate: rows mapped to the entries of taxonomy-v0.9-234)"),
        "items": src["items"], "items_sha256": sha(items_text.encode("utf-8")), "splits": src["splits"],
        "gold": {"file": "gold.jsonl", "sha256": sha(gold_text.encode("utf-8")),
                 "from": "2026-10-17b gold.jsonl", "rows": len(rows),
                 "candidate_rows": sum(bool(t.get("candidate")) for t in rows),
                 "remapped_rows": sum("remapped_from" in t for t in rows),
                 "dont_care_rows": sum(bool(t.get("dont_care")) for t in rows),
                 "group_node_rows": sum(t["slug"] in groups and not t.get("candidate") and not t.get("dont_care")
                                        for t in rows),
                 "scored_rows": len(scored), "scored_high": sum(t.get("confidence") == "high" for t in scored),
                 "scored_both_labellers": sum({"A", "B"} <= set(t.get("sources") or []) for t in scored),
                 "extra_techniques": sum(len(g["label"]["extra_techniques"]) for g in gold),
                 "menu_gaps": sum(len(g["label"]["menu_gaps"]) for g in gold)},
        "derivation": derivation,
        "frozen_with": frozen_with,
    }


def build() -> tuple[dict[str, str], dict[str, bytes]]:
    items_text = (SRC / "items.jsonl").read_text("utf-8")
    if sha(items_text.encode("utf-8")) != json.loads((SRC / "manifest.json").read_text("utf-8"))["items_sha256"]:
        raise DeriveError("2026-10-17b items.jsonl does not hash to its manifest's items_sha256")
    techniques, groups, tax_sha, tax_version = taxonomy()
    gold, derivation = build_gold(techniques, groups)
    gold_text = dumps(gold)
    man = manifest(items_text, gold_text, gold, derivation, groups, tax_sha, tax_version)
    files = {"items.jsonl": items_text, "gold.jsonl": gold_text,
             "manifest.json": json.dumps(man, indent=2, ensure_ascii=False) + "\n"}
    blobs = {f"texts/{iid}.txt.gz": text_file(SRC, iid).read_bytes()
             for iid in sorted(i["item_id"] for i in read_jsonl(SRC / "items.jsonl"))}
    return files, blobs


def text_file(set_dir: Path, iid: str) -> Path:
    """``iid``'s whole text: the set's ``texts/`` file, else the eval-text cache's (the texts are not in the
    repository, D790; ``tools/fetch_eval_texts.py`` fills the cache)."""
    from stack.evals.freeze import find_text, missing_text_message

    p = find_text(set_dir, iid)
    if p is None:
        raise DeriveError(missing_text_message(set_dir, iid))
    return p


def text_bytes(set_dir: Path, rel: str) -> bytes | None:
    """The bytes of ``texts/<iid>.txt.gz`` of ``set_dir`` (its own file, else the cache's), None when neither."""
    from stack.evals.freeze import find_text

    p = find_text(set_dir, rel.removeprefix("texts/").removesuffix(".txt.gz"))
    return p.read_bytes() if p is not None else None


def main(write: bool, out: Path = OUT) -> int:
    try:
        files, blobs = build()
    except DeriveError as exc:
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
    extra = sorted(p.name for p in (out / "texts").glob("*") if f"texts/{p.name}" not in blobs)
    differ += [f"texts/{x} (not in 2026-10-17b)" for x in extra]
    if differ:
        print(f"differs: {', '.join(differ)}")
        return 1
    print(f"reproduced: {', '.join(files)} byte for byte, {len(blobs)} texts identical")
    return 0


if __name__ == "__main__":
    raise SystemExit(main("--write" in sys.argv[1:]))
