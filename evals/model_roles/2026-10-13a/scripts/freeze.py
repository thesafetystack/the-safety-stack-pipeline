"""Freeze the annotator eval set 2026-10-13a (model_mentions, model_roles, technique_roles).

Run from the repo root: ``uv run --no-sync python evals/model_roles/2026-10-13a/scripts/freeze.py``.
Source: the frozen whole texts of screen set ``evals/screen/2026-10-12a`` (each text is the string
``stack.annotate.stage._text`` returned at that freeze, store head ``rcprvcnepyb1g``), restricted to items whose
``gold-v2.jsonl`` verdict is ``relevant`` (production scopes the annotators to stack_relevance = relevant, D269).
The texts are copied byte-identical (same ``full_text_digest``). Writes
``evals/model_roles/2026-10-13a/{items.jsonl,manifest.json,texts/}`` and the same for ``technique_roles``.
Per item the production inputs are computed with production code: model_mentions' ``scan_payload``
(the model_roles basis read), model_roles' user message (``render.render_prompt`` over the window menu)
and technique_roles' user message (``render.render_user``); ``input_digest`` is the sha256 of that user
message (null for a model_roles item without a strong mention: production answers ``empty`` without a call).
"""
from __future__ import annotations

import gzip
import hashlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path

ROOT = Path.cwd()
SRC = ROOT / "evals/screen/2026-10-12a"
SET_ID = "2026-10-13a"
#: 21 relevant items with >= 1 strong model mention (all but scr-0077, the 564k-char system card: beyond
#: model_roles' 300k window and too long for the labellers) + 4 without one (mention recall, technique rows).
PICK = ["scr-0024", "scr-0022", "scr-0053", "scr-0096", "scr-0058", "scr-0079", "scr-0082", "scr-0090",
        "scr-0059", "scr-0063", "scr-0074", "scr-0097", "scr-0019", "scr-0052", "scr-0047", "scr-0080",
        "scr-0089", "scr-0094", "scr-0066", "scr-0095", "scr-0068",
        "scr-0076", "scr-0028", "scr-0016", "scr-0001"]


def load(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def main() -> None:
    sys.path.insert(0, str(ROOT))
    from stack.modelvocab import load_vocab

    mm = load("ae_mm", "workflows/annotate/model_mentions/main.py")
    mr = load("ae_mr", "workflows/annotate/model_roles/main.py")
    tr = load("ae_tr", "workflows/annotate/technique_roles/main.py")
    scanner = mm.scanner_for(ROOT / "configs")
    nodes, makers = mr.render.vocab_view(load_vocab(ROOT / "configs").validated)
    from stack.evals.freeze import items_file  # screen 2026-10-12a's items are archived (D795)

    src_items = {json.loads(line)["item_id"]: json.loads(line) for line in open(items_file(SRC))}
    gold = {json.loads(line)["item_id"]: json.loads(line) for line in open(SRC / "gold-v2.jsonl")}
    rows = []
    for src_id in PICK:
        it, g = src_items[src_id], gold[src_id]
        assert g["label"]["verdict"] == "relevant", src_id
        text = gzip.open(SRC / "texts" / f"{src_id}.txt.gz", "rt", encoding="utf-8").read()
        assert sha(text) == it["input"]["full_text_digest"], src_id
        rows.append((src_id, it, text))
    # dev/test alternate within channel group by length (stable, documented in the README)
    rows.sort(key=lambda r: (r[1]["stratum"]["channel_group"], len(r[2]), r[0]))
    split_of = {}
    by_group: dict[str, int] = {}
    for src_id, it, _ in rows:
        k = by_group.get(it["stratum"]["channel_group"], 0)
        split_of[src_id] = "dev" if k % 2 == 0 else "test"
        by_group[it["stratum"]["channel_group"]] = k + 1
    out_mr, out_tr = [], []
    for n, src_id in enumerate(PICK, 1):
        it, text = next((r[1], r[2]) for r in rows if r[0] == src_id)
        iid = f"an-{n:04d}"
        base = {"item_id": iid, "doc_id": it["doc_id"], "source_item": f"screen/2026-10-12a/{src_id}",
                "split": split_of[src_id], "stratum": it["stratum"], "license_class": it["license_class"]}
        inp = {"title": it["input"]["title"], "text_digest": it["input"]["text_digest"],
               "basis_role": it["input"]["basis_role"], "full_text_digest": it["input"]["full_text_digest"],
               "full_text_chars": len(text)}
        payload = mm.scan_payload(scanner, text)
        status = "empty" if mm.is_empty(payload) else "ok"
        _, mentions = mr.mentions_of({"annotations": {"model_mentions": {"status": status, "payload": payload}}})
        asks = bool(mentions["mentions"])
        user = None
        if asks:
            window = text[: mr.DOC_CHAR_CAP]
            in_window, _beyond = mr.render.window_split(mentions, text, mr.DOC_CHAR_CAP)
            menu = mr.render.build_menu(in_window, nodes, makers, window)
            user = mr.render.render_prompt(text, menu, doc_char_cap=mr.DOC_CHAR_CAP)
        out_mr.append({**base, "production_asks": asks,
                       "input": {**inp, "model_mentions": {"status": status, "payload": payload}},
                       "input_digest": sha(user) if user is not None else None})
        tuser = tr.render.render_user(text, tr.DOC_CHAR_CAP)
        out_tr.append({**base, "production_asks": True, "input": dict(inp), "input_digest": sha(tuser)})
    vocab_sha = hashlib.sha256((ROOT / "configs/models.yaml").read_bytes()).hexdigest()
    for task, items in (("model_roles", out_mr), ("technique_roles", out_tr)):
        d = ROOT / "evals" / task / SET_ID
        (d / "texts").mkdir(parents=True, exist_ok=True)
        with open(d / "items.jsonl", "w", encoding="utf-8") as fh:
            for r in items:
                fh.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
        for r in items:
            src_id = r["source_item"].rsplit("/", 1)[1]
            shutil.copyfile(SRC / "texts" / f"{src_id}.txt.gz", d / "texts" / f"{r['item_id']}.txt.gz")
        items_sha = hashlib.sha256((d / "items.jsonl").read_bytes()).hexdigest()
        manifest = {"task": task, "set_id": SET_ID, "source": "evals/screen/2026-10-12a (gold-v2 relevant)",
                    "store_head": "rcprvcnepyb1g", "items": len(items), "items_sha256": items_sha,
                    "splits": {s: sum(r["split"] == s for r in items) for s in ("dev", "test")},
                    "frozen_with": {"models_yaml_sha256": vocab_sha,
                                    "model_roles_rubric_digest": mr.rubric_digest(),
                                    "technique_roles_rubric_digest": tr.rubric_digest()}}
        (d / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        print(task, len(items), items_sha[:12])


if __name__ == "__main__":
    main()
