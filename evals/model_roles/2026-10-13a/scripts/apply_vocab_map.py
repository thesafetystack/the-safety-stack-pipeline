"""Build gold-vocab-v0.5.jsonl from gold.jsonl and vocab-v0.5-oov-map.jsonl (T-R6 item 6): each gold
`mentions_out_of_vocab` name the map gives a v0.5 node becomes a `label.mentions` entry {node_id, surface: name}
(unless the item already has that node); unmapped names (node_id null, reason given) stay out of vocabulary.
Nothing else changes. usage (repo root): apply_vocab_map.py"""
import json
from pathlib import Path

D = Path("evals/model_roles/2026-10-13a")
M = {(m["item_id"], m["name"]): m["node_id"]
     for m in map(json.loads, (D / "vocab-v0.5-oov-map.jsonl").read_text("utf-8").splitlines())}
out, seen = [], set()
for r in map(json.loads, (D / "gold.jsonl").read_text("utf-8").splitlines()):
    lab, keep = r["label"], []
    for o in lab.get("mentions_out_of_vocab") or []:
        key = (r["item_id"], o["name"]); seen.add(key)
        node = M[key]
        if node is None:
            keep.append(o)
        elif node not in {m["node_id"] for m in lab["mentions"]}:
            lab["mentions"].append({"node_id": node, "surface": o["name"]})
    lab["mentions_out_of_vocab"] = keep
    out.append(json.dumps(r, ensure_ascii=False, sort_keys=True))
if seen != set(M):
    raise SystemExit(f"map and gold disagree: {sorted(set(M) ^ seen)}")
(D / "gold-vocab-v0.5.jsonl").write_text("\n".join(out) + "\n", "utf-8")
print(f"{sum(v is not None for v in M.values())} mapped, {sum(v is None for v in M.values())} left out of vocabulary")
