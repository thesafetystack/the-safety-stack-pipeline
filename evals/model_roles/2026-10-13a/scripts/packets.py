"""Labeller packets for set 2026-10-13a: 4 batches balanced by characters; per doc the frozen text and the
production model menu (render.build_menu over the window, `_menu_line` as the judge sees it)."""
import gzip, json, sys, importlib.util, shutil
from pathlib import Path
import yaml
ROOT = Path.cwd(); sys.path.insert(0, str(ROOT))
OUT = Path(sys.argv[1])
def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m
from stack.modelvocab import load_vocab
mr = load("p_mr", "workflows/annotate/model_roles/main.py")
tr = load("p_tr", "workflows/annotate/technique_roles/main.py")
nodes, makers = mr.render.vocab_view(load_vocab(ROOT / "configs").validated)
items = [json.loads(l) for l in open(ROOT / "evals/model_roles/2026-10-13a/items.jsonl")]
# greedy balance by chars into 4 batches
batches = [[] for _ in range(4)]; load_ = [0] * 4
for it in sorted(items, key=lambda r: -r["input"]["full_text_chars"]):
    k = load_.index(min(load_)); batches[k].append(it); load_[k] += it["input"]["full_text_chars"]
print("batch chars", load_)
# shared files
shared = OUT / "_shared"; shared.mkdir(parents=True, exist_ok=True)
shutil.copyfile(ROOT / "workflows/annotate/model_roles/rubric.md", shared / "rubric-model-roles.md")
shutil.copyfile(ROOT / "workflows/annotate/technique_roles/rubric.md", shared / "rubric-technique-roles.md")
tax = tr.render.load_taxonomy(tr.TAXONOMY_PATH)
(shared / "technique-menu.md").write_text(tr.render.full_menu(tax), encoding="utf-8")
raw = yaml.safe_load(open(ROOT / "configs/models.yaml"))
lines = ["node_id\tkind\tname\tparent\tmaker\tmentions\tweak_mentions"]
for nid, n in raw["nodes"].items():
    lines.append("\t".join([nid, str(n.get("kind", "")), str(n.get("name", "")), str(n.get("parent", "") or ""),
                            str(n.get("maker", "") or ""), "; ".join(n.get("mentions") or []), "; ".join(n.get("weak_mentions") or [])]))
(shared / "vocab-index.tsv").write_text("\n".join(lines) + "\n", encoding="utf-8")
manifest = {}
for b, its in enumerate(batches, 1):
    bd = OUT / f"batch-{b:02d}"
    if bd.exists(): shutil.rmtree(bd)
    shutil.copytree(shared, bd)  # each packet is self-contained (Codex -C <packet>)
    for it in sorted(its, key=lambda r: r["item_id"]):
        iid = it["item_id"]; dd = bd / "docs" / iid; dd.mkdir(parents=True)
        text = gzip.open(ROOT / f"evals/model_roles/2026-10-13a/texts/{iid}.txt.gz", "rt", encoding="utf-8").read()
        (dd / "text.md").write_text(text, encoding="utf-8")
        status, mentions = mr.mentions_of({"annotations": {"model_mentions": it["input"]["model_mentions"]}})
        window = text[: mr.DOC_CHAR_CAP]
        in_window, beyond = mr.render.window_split(mentions, text, mr.DOC_CHAR_CAP)
        menu = mr.render.build_menu(in_window, nodes, makers, window) if mentions["mentions"] else []
        head = (f"# Model menu for {iid}\n\nTitle: {it['input']['title']}\nCharacters: {len(text)}\n"
                f"Menu nodes: {len(menu)}" + (" (no strong matcher hit: production makes no model_roles call for this document;"
                " still label mentions and techniques)" if not menu else "") + "\n")
        (dd / "model-menu.md").write_text(head + "".join(mr.render._menu_line(e, False, True) for e in menu), encoding="utf-8")
        manifest.setdefault(f"batch-{b:02d}", []).append({"item_id": iid, "chars": len(text), "menu": [e.node_id for e in menu]})
json.dump(manifest, open(OUT / "manifest.json", "w"), indent=1)
for b, v in manifest.items(): print(b, [(x["item_id"], x["chars"], len(x["menu"])) for x in v])
