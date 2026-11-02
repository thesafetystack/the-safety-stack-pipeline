"""Freeze the model_roles eval set 2026-10-16a: set 2026-10-13a re-frozen under the merged stage (D591).

Run from the repo root: ``uv run --no-sync python evals/model_roles/2026-10-16a/scripts/freeze.py``. No LLM call.
Same 25 items as ``evals/model_roles/2026-10-13a`` (same ``item_id``, ``doc_id``, split, stratum, licence; whole
texts copied byte-identical, ``full_text_digest`` checked). Per item, with production code at HEAD
(``workflows/annotate/model_roles/main.py``, the current ``configs/models.yaml``):

* ``input.model_mentions`` = ``match.scan_payload`` (status ``ok``/``empty``; what ``stack eval model_mentions``
  compares the scan with);
* ``input.menu`` = the judge's menu ``[{node_id, weak}]`` over the window, ``input.beyond_window`` = the nodes
  withheld as only past it (for reference and for the gold-gap table);
* ``production_asks`` = the scan has a strong mention; ``input.fills_digest`` = ``stack.evals.annotators.fills_digest``
  of ``main.fills`` (the prompt-independent part of the user message), ``input_digest`` = sha256 of the user message
  under the prompt ``model-roles-v1`` (both null when production makes no call).

Gold: ``gold.jsonl`` = 2026-10-13a's ``gold-vocab-v0.5.jsonl`` byte for byte (mentions aligned to vocabulary v0.5;
the model role grades are the 2026-10-13a labels over the v0.4 menus). ``gold-gaps.jsonl``: per item whose menu
is not the set of gold model rows, the menu nodes without a gold row and the gold rows no longer on the menu.
Writes ``items.jsonl``, ``manifest.json``, ``texts/``, ``gold.jsonl``, ``gold-gaps.jsonl``.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path

ROOT = Path.cwd()
SRC = ROOT / "evals/model_roles/2026-10-13a"
OUT = ROOT / "evals/model_roles/2026-10-16a"
SET_ID = "2026-10-16a"
PROMPT = "model-roles-v1"
GOLD_SRC = "gold-vocab-v0.5.jsonl"


def sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


def main() -> None:
    sys.path.insert(0, str(ROOT))
    from stack.evals.annotators import fills_digest
    from stack.evals.freeze import read_text_gz, text_path
    from stack.stage_runner import load_path_module

    mr = load_path_module("_freeze_model_roles_main", ROOT / "workflows/annotate/model_roles/main.py")
    c = mr.match.scanner_for(ROOT / "configs")
    pf = mr.prompt_file(PROMPT)
    gold = {g["item_id"]: g for g in read_jsonl(SRC / GOLD_SRC)}
    items, gaps = [], []
    for it in read_jsonl(SRC / "items.jsonl"):
        iid = it["item_id"]
        text = read_text_gz(text_path(SRC, iid))
        assert sha(text) == it["input"]["full_text_digest"], iid
        scan = c.scanner.scan(text)
        payload = mr.match.scan_payload(c.scanner, text)
        asks = bool(scan["mentions"])
        menu, beyond, fills = mr.fills(text, scan, c.vocab)
        user = pf.user(fills) if asks else None
        keep = {k: it["input"][k] for k in ("title", "text_digest", "basis_role", "full_text_digest", "full_text_chars")}
        inp = {**keep, "model_mentions": {"status": "empty" if mr.match.is_empty(scan) else "ok", "payload": payload},
               "menu": [{"node_id": e.node_id, "weak": e.weak} for e in menu] if asks else [],
               "beyond_window": beyond if asks else [],
               "fills_digest": fills_digest(fills) if asks else None}
        items.append({**{k: it[k] for k in ("item_id", "doc_id", "source_item", "split", "stratum", "license_class")},
                      "production_asks": asks, "input": inp, "input_digest": sha(user) if user is not None else None})
        on_menu = [m["node_id"] for m in inp["menu"]]
        rows = {m["node_id"]: m["roles"] for m in gold[iid]["label"].get("models") or []}
        new = [n for n in on_menu if n not in rows]
        gone = [n for n in rows if n not in on_menu]
        if new or gone:
            gaps.append({"item_id": iid, "split": it["split"], "production_asks": asks, "menu_not_in_gold": new,
                         "gold_off_menu_graded": [n for n in gone if any(v for v in rows[n].values())],
                         "gold_off_menu_all_zero": [n for n in gone if not any(v for v in rows[n].values())]})
    (OUT / "texts").mkdir(parents=True, exist_ok=True)
    for it in items:
        shutil.copyfile(text_path(SRC, it["item_id"]), text_path(OUT, it["item_id"]))
    with open(OUT / "items.jsonl", "w", encoding="utf-8") as fh:
        for r in items:
            fh.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
    shutil.copyfile(SRC / GOLD_SRC, OUT / "gold.jsonl")
    with open(OUT / "gold-gaps.jsonl", "w", encoding="utf-8") as fh:
        for g in gaps:
            fh.write(json.dumps(g, ensure_ascii=False, sort_keys=True) + "\n")
    manifest = {
        "task": "model_roles", "set_id": SET_ID,
        "source": "evals/model_roles/2026-10-13a (the same items and texts, re-frozen under the merged stage, D591)",
        "store_head": "rcprvcnepyb1g", "items": len(items),
        "items_sha256": hashlib.sha256((OUT / "items.jsonl").read_bytes()).hexdigest(),
        "splits": {s: sum(r["split"] == s for r in items) for s in ("dev", "test")},
        "frozen_with": {"models_yaml_sha256": c.vocab.digest, "model_roles_prompt": PROMPT,
                        "model_roles_prompt_digest": pf.digest},
        "gold": {"file": "gold.jsonl", "from": f"evals/model_roles/2026-10-13a/{GOLD_SRC}",
                 "sha256": hashlib.sha256((OUT / "gold.jsonl").read_bytes()).hexdigest(),
                 "gap_items": [g["item_id"] for g in gaps]},
    }
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"{len(items)} items ({sum(r['production_asks'] for r in items)} ask), {len(gaps)} with gold gaps: "
          f"{[g['item_id'] for g in gaps]}")


if __name__ == "__main__":
    main()
