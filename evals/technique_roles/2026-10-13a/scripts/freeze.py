"""Freeze (or check) the technique_roles eval set 2026-10-13a against the current production code.

Run from the repo root: ``uv run --no-sync python evals/technique_roles/2026-10-13a/scripts/freeze.py [--write]``.
No LLM call. The set was first frozen on 2026-10-13 by ``evals/model_roles/2026-10-13a/scripts/freeze.py`` together
with model_roles' set (historical; it imports code removed by D591). This script is the technique half of it,
against HEAD:

* the same 25 items in the same order (``PICK``), item ids ``an-0001``.., the same dev/test split rule (alternate by
  length within channel group), and the whole texts of ``evals/screen/2026-10-12a`` (``gold-v2.jsonl`` verdict
  ``relevant``), checked against their ``full_text_digest``;
* ``input_digest`` = sha256 of production's user message, ``main.user_message(main.prompt_file(PROMPT), text)``
  (the prompt file's user part with the ``document`` / ``truncation`` fills; technique-roles-v1 and -v2 have the
  same user part, so the digest holds for both).

Without ``--write`` it rebuilds ``items.jsonl`` in memory and compares it with the set's byte for byte (and each
``texts/<item_id>.txt.gz`` with its source), printing what differs; exit 1 on a difference. With ``--write`` it writes
``items.jsonl`` and the texts and refreshes ``manifest.json``: ``items_sha256``, ``splits`` and, in ``frozen_with``,
the menu source (``taxonomy_path``, ``taxonomy_sha256``, ``taxonomy_version``); no prompt is recorded, since
the user part is the same in every technique prompt so far (a prompt whose user part differs needs a new set, or a
``frozen_with`` prompt key and the adapter support model_roles has); the other manifest fields (the 2026-10-13 freeze's provenance) are kept. ``gold.jsonl`` is never
touched; every gold slug (``label.techniques[].slug``, ``label.extra_techniques[].hint_technique``) is checked to be
on the current menu.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path.cwd()
SRC = ROOT / "evals/screen/2026-10-12a"
OUT = ROOT / "evals/technique_roles/2026-10-13a"
SET_ID = "2026-10-13a"
PROMPT = "technique-roles-v2"
#: The 2026-10-13 freeze's selection, in item-id order (see the model_roles 2026-10-13a README).
PICK = ["scr-0024", "scr-0022", "scr-0053", "scr-0096", "scr-0058", "scr-0079", "scr-0082", "scr-0090",
        "scr-0059", "scr-0063", "scr-0074", "scr-0097", "scr-0019", "scr-0052", "scr-0047", "scr-0080",
        "scr-0089", "scr-0094", "scr-0066", "scr-0095", "scr-0068",
        "scr-0076", "scr-0028", "scr-0016", "scr-0001"]


def sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


def dumps(rows: list[dict]) -> str:
    return "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows)


def build(tr) -> tuple[list[dict], dict[str, Path]]:
    """The items (and each item's source text file), computed with production code."""
    from stack.evals.freeze import items_file  # screen 2026-10-12a's items are archived (D795)

    src_items = {it["item_id"]: it for it in read_jsonl(items_file(SRC))}
    gold = {g["item_id"]: g for g in read_jsonl(SRC / "gold-v2.jsonl")}
    pf = tr.prompt_file(PROMPT)
    rows = []
    for src_id in PICK:
        it, g = src_items[src_id], gold[src_id]
        assert g["label"]["verdict"] == "relevant", src_id
        text = gzip.decompress(text_file(SRC, src_id).read_bytes()).decode("utf-8")
        assert sha(text) == it["input"]["full_text_digest"], src_id
        rows.append((src_id, it, text))
    order = sorted(rows, key=lambda r: (r[1]["stratum"]["channel_group"], len(r[2]), r[0]))
    split_of: dict[str, str] = {}
    by_group: dict[str, int] = {}
    for src_id, it, _ in order:
        k = by_group.get(it["stratum"]["channel_group"], 0)
        split_of[src_id] = "dev" if k % 2 == 0 else "test"
        by_group[it["stratum"]["channel_group"]] = k + 1
    items, sources = [], {}
    for n, (src_id, it, text) in enumerate(rows, 1):
        iid = f"an-{n:04d}"
        items.append({
            "item_id": iid, "doc_id": it["doc_id"], "source_item": f"screen/2026-10-12a/{src_id}",
            "split": split_of[src_id], "stratum": it["stratum"], "license_class": it["license_class"],
            "production_asks": True,
            "input": {"title": it["input"]["title"], "text_digest": it["input"]["text_digest"],
                      "basis_role": it["input"]["basis_role"], "full_text_digest": it["input"]["full_text_digest"],
                      "full_text_chars": len(text)},
            "input_digest": sha(tr.user_message(pf, text))})
        sources[iid] = text_file(SRC, src_id)
    return items, sources


def text_file(set_dir: Path, iid: str) -> Path:
    """``iid``'s whole text: the set's ``texts/`` file, else the eval-text cache's (the texts are not in the
    repository, D790; ``tools/fetch_eval_texts.py`` fills the cache)."""
    from stack.evals.freeze import find_text, missing_text_message

    p = find_text(set_dir, iid)
    if p is None:
        raise SystemExit(missing_text_message(set_dir, iid))
    return p


def gold_slugs_missing(slugs: set[str]) -> list[str]:
    out = []
    for g in read_jsonl(OUT / "gold.jsonl"):
        lab = g["label"]
        named = [t["slug"] for t in lab.get("techniques") or []]
        named += [x["hint_technique"] for x in lab.get("extra_techniques") or [] if x.get("hint_technique")]
        out += [f"{g['item_id']}:{s}" for s in named if s not in slugs]
    return out


def main(write: bool) -> int:
    sys.path.insert(0, str(ROOT))
    from stack.stage_runner import load_path_module

    tr = load_path_module("_freeze_technique_roles_main", ROOT / "workflows/annotate/technique_roles/main.py")
    items, sources = build(tr)
    menu = tr.menu()
    missing = gold_slugs_missing(set(menu.slugs))
    if missing:
        print(f"gold slugs not on the current menu ({tr.TAXONOMY_PATH.name}): {', '.join(missing)}")
        return 1
    new = dumps(items)
    old = (OUT / "items.jsonl").read_text("utf-8") if (OUT / "items.jsonl").is_file() else ""
    from stack.evals.freeze import find_text

    texts_differ = [iid for iid, src in sources.items()
                    if (have := find_text(OUT, iid)) is None
                    or gzip.decompress(have.read_bytes()) != gzip.decompress(src.read_bytes())]
    if not write:
        if new == old and not texts_differ:
            print(f"reproduced: items.jsonl byte for byte ({len(items)} items, sha256 "
                  f"{hashlib.sha256(new.encode('utf-8')).hexdigest()[:12]}), texts identical")
            return 0
        olds = {json.loads(x)["item_id"]: x for x in old.splitlines() if x.strip()}
        for line in new.splitlines():
            iid = json.loads(line)["item_id"]
            if olds.get(iid) != line:
                print(f"differs: {iid}")
        print(f"texts differ: {texts_differ}" if texts_differ else "texts identical")
        return 1
    (OUT / "texts").mkdir(parents=True, exist_ok=True)
    for iid, src in sources.items():
        (OUT / "texts" / f"{iid}.txt.gz").write_bytes(src.read_bytes())
    (OUT / "items.jsonl").write_text(new, encoding="utf-8")
    path = OUT / "manifest.json"
    manifest = json.loads(path.read_text("utf-8")) if path.is_file() else {
        "task": "technique_roles", "set_id": SET_ID, "source": "evals/screen/2026-10-12a (gold-v2 relevant)",
        "store_head": "rcprvcnepyb1g"}
    manifest.update({"items": len(items), "items_sha256": hashlib.sha256(new.encode("utf-8")).hexdigest(),
                     "splits": {s: sum(r["split"] == s for r in items) for s in ("dev", "test")}})
    manifest["frozen_with"] = {**manifest.get("frozen_with", {}), **menu_source(tr, menu)}
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(items)} items; manifest frozen_with: {manifest['frozen_with']}")
    return 0


def menu_source(tr, menu) -> dict[str, str]:
    """The manifest's menu-source fields (what ``stack eval technique_roles`` compares)."""
    return {"taxonomy_path": str(tr.TAXONOMY_PATH.relative_to(ROOT)), "taxonomy_sha256": menu.taxonomy_sha256,
            "taxonomy_version": menu.taxonomy_version}


if __name__ == "__main__":
    raise SystemExit(main("--write" in sys.argv[1:]))
