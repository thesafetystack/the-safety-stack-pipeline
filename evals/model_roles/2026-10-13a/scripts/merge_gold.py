"""Build gold.jsonl for evals/{model_roles,technique_roles}/2026-10-13a from the adjudicated labels.
usage (repo root): merge_gold.py <labels_dir>   reads opus-batch-*.jsonl, codex-batch-*.jsonl, adjudicated-batch-*.jsonl"""
import glob, json, sys
from pathlib import Path
L = Path(sys.argv[1]); ROOT = Path.cwd(); SET = "2026-10-13a"
def read(pat):
    out = {}
    for f in sorted(glob.glob(str(L / pat))):
        for line in open(f):
            if line.strip():
                r = json.loads(line); out[r["item_id"]] = r
    return out
A, B, B2, J = read("opus-batch-*.jsonl"), read("codex-batch-*.jsonl"), read("opus2-batch-*.jsonl"), read("adjudicated-batch-*.jsonl")
MR_KEYS = ("mentions", "mentions_out_of_vocab", "models", "off_menu_models")
TR_KEYS = ("techniques", "extra_techniques")
for task, keys, pref in (("model_roles", MR_KEYS, ("mention:", "model:")), ("technique_roles", TR_KEYS, ("technique:",))):
    d = ROOT / "evals" / task / SET
    items = [json.loads(l) for l in open(d / "items.jsonl")]
    out = []
    for it in items:
        i = it["item_id"]
        j = J.get(i)
        if j is None:
            raise SystemExit(f"{i}: no adjudicated label")
        contested = [c for c in j.get("contested") or [] if c.startswith(pref)]
        labelers = [{"who": who, "label": {k: src[i].get(k) for k in keys}, "rationale": src[i].get("rationale")}
                    for who, src in (("opus", A), ("codex-gpt-6-sol", B), ("opus2", B2)) if i in src]
        out.append({"item_id": i, "doc_id": it["doc_id"], "doc_kind": j.get("doc_kind"),
                    "label": {k: j.get(k) or [] for k in keys}, "labelers": labelers,
                    "contested": bool(contested), "contested_elements": contested,
                    "adjudication_note": j.get("adjudication_note"), "adjudicator": "opus"})
    with open(d / "gold.jsonl", "w", encoding="utf-8") as fh:
        for r in out:
            fh.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
    print(task, len(out), "contested", sum(r["contested"] for r in out))
