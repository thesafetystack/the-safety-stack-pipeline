"""Validate a labels JSONL against a packet: parse, menu coverage, slugs, vocab ids, verbatim quotes."""
import json, sys, re
from pathlib import Path
lab, pk = Path(sys.argv[1]), Path(sys.argv[2])
slugs = set(re.findall(r"^- ([a-z0-9][a-z0-9_.\-]*) — ", (pk / "technique-menu.md").read_text(), re.M))
vocab = {l.split("\t")[0] for l in (pk / "vocab-index.tsv").read_text().splitlines()[1:]}
def norm(s): return re.sub(r"\s+", " ", s).strip()
for n, line in enumerate(open(lab), 1):
    if not line.strip(): continue
    r = json.loads(line); i = r["item_id"]
    text = (pk / "docs" / i / "text.md").read_text(); nt = norm(text)
    menu = re.findall(r"^- `([^`]+)`", (pk / "docs" / i / "model-menu.md").read_text(), re.M)
    got = [m["node_id"] for m in r.get("models") or []]
    probs = []
    if sorted(got) != sorted(menu): probs.append(f"menu cover {sorted(set(menu)^set(got))}")
    bad_slug = [t["slug"] for t in r.get("techniques") or [] if t["slug"] not in slugs]
    if bad_slug: probs.append(f"bad slugs {bad_slug}")
    bad_v = [m["node_id"] for m in r.get("mentions") or [] if m.get("node_id") not in vocab]
    if bad_v: probs.append(f"bad vocab {bad_v}")
    q = [x.get("quote") for x in (r.get("models") or []) + (r.get("techniques") or []) if x.get("quote")]
    nv = [x for x in q if norm(x) not in nt]
    print(f"{i}: mentions {len(r.get('mentions') or [])} oov {len(r.get('mentions_out_of_vocab') or [])} menu {len(menu)} "
          f"models>=2 {sum(any(v>=2 for v in (m.get('roles') or {}).values()) for m in r.get('models') or [])} "
          f"tech {len(r.get('techniques') or [])} extra {len(r.get('extra_techniques') or [])} quotes {len(q)} nonverbatim {len(nv)} {probs}")
