"""Inter-labeller agreement (opus vs codex) and per-document diffs for the adjudicator.
usage: agree.py <labels_dir> <out_dir>   (reads opus-batch-*.jsonl, codex-batch-*.jsonl)"""
import json, sys, glob
from pathlib import Path
MR = ("mentioned", "cited_work", "discussed", "usage_experience", "built_on", "instrument", "modified",
      "evaluated_behavioral", "analyzed_internals", "introduced")
TR = ("mentioned", "cited_work", "discussed", "used", "evaluated", "modified", "introduced", "analyzed",
      "attested_use", "suspected_use", "considered", "critiqued")
L, O = Path(sys.argv[1]), Path(sys.argv[2]); O.mkdir(parents=True, exist_ok=True)
def read(pat):
    out = {}
    for f in sorted(glob.glob(str(L / pat))):
        for line in open(f):
            line = line.strip()
            if line:
                r = json.loads(line); out[r["item_id"]] = r
    return out
A = read("opus-batch-*.jsonl")
B = {}
for pat in (sys.argv[3] if len(sys.argv) > 3 else "codex-batch-*.jsonl").split(","):
    B.update(read(pat))
only = set(sys.argv[4].split(",")) if len(sys.argv) > 4 else None
ids = sorted(i for i in set(A) & set(B) if only is None or i in only)
def prf(a, b):
    tp = len(a & b); fp = len(b - a); fn = len(a - b)
    return tp, fp, fn
def kappa(pairs):
    n = len(pairs)
    if not n: return None
    po = sum(x == y for x, y in pairs) / n
    pa = sum(x for x, _ in pairs) / n; pb = sum(y for _, y in pairs) / n
    pe = pa * pb + (1 - pa) * (1 - pb)
    return None if pe == 1 else (po - pe) / (1 - pe)
def cells(rows, key, roles, thr):
    return {(r[key], role) for r in rows for role in roles if int((r.get("roles") or {}).get(role, 0) or 0) >= thr}
tot = {"mention": [0, 0, 0], "mr2": [0, 0, 0], "mr1": [0, 0, 0], "slug": [0, 0, 0], "tr2": [0, 0, 0]}
mr_pairs = {1: [], 2: []}
diffs = {}
for i in ids:
    a, b = A[i], B[i]
    ma = {m["node_id"] for m in a.get("mentions") or [] if m.get("node_id")}
    mb = {m["node_id"] for m in b.get("mentions") or [] if m.get("node_id")}
    menu = sorted({m["node_id"] for m in (a.get("models") or []) + (b.get("models") or [])})
    d = {"mentions_only_opus": sorted(ma - mb), "mentions_only_codex": sorted(mb - ma)}
    for k, (x, y) in {"mention": (ma, mb)}.items():
        t = prf(x, y); tot[k] = [u + v for u, v in zip(tot[k], t)]
    for thr in (1, 2):
        ca, cb = cells(a.get("models") or [], "node_id", MR, thr), cells(b.get("models") or [], "node_id", MR, thr)
        t = prf(ca, cb); tot[f"mr{thr}"] = [u + v for u, v in zip(tot[f"mr{thr}"], t)]
        for n in menu:
            for role in MR:
                mr_pairs[thr].append(((n, role) in ca, (n, role) in cb))
        if thr == 2:
            d["model_cells_ge2_only_opus"] = sorted(f"{n}:{r}" for n, r in ca - cb)
            d["model_cells_ge2_only_codex"] = sorted(f"{n}:{r}" for n, r in cb - ca)
    sa = {t["slug"] for t in a.get("techniques") or []}; sb = {t["slug"] for t in b.get("techniques") or []}
    t = prf(sa, sb); tot["slug"] = [u + v for u, v in zip(tot["slug"], t)]
    ta, tb = cells(a.get("techniques") or [], "slug", TR, 2), cells(b.get("techniques") or [], "slug", TR, 2)
    t = prf(ta, tb); tot["tr2"] = [u + v for u, v in zip(tot["tr2"], t)]
    d["techniques_only_opus"] = sorted(sa - sb); d["techniques_only_codex"] = sorted(sb - sa)
    d["technique_cells_ge2_only_opus"] = sorted(f"{s}:{r}" for s, r in ta - tb if s in sb)
    d["technique_cells_ge2_only_codex"] = sorted(f"{s}:{r}" for s, r in tb - ta if s in sa)
    diffs[i] = d
def f1(t):
    tp, fp, fn = t; return round(2 * tp / (2 * tp + fp + fn), 3) if tp + fp + fn else None
summary = {"docs": len(ids), "only_opus": sorted(set(A) - set(B)), "only_codex": sorted(set(B) - set(A)),
           "mentions_F1": f1(tot["mention"]), "mentions_counts(both,opus_only,codex_only)": [tot["mention"][0], tot["mention"][2], tot["mention"][1]],
           "model_cells_ge2_F1": f1(tot["mr2"]), "model_cells_ge2_kappa": round(kappa(mr_pairs[2]), 3) if mr_pairs[2] else None,
           "model_cells_ge1_F1": f1(tot["mr1"]), "model_cells_ge1_kappa": round(kappa(mr_pairs[1]), 3) if mr_pairs[1] else None,
           "model_cells_ge2_counts(both,opus_only,codex_only)": [tot["mr2"][0], tot["mr2"][2], tot["mr2"][1]],
           "technique_slug_F1": f1(tot["slug"]), "technique_slug_counts(both,opus_only,codex_only)": [tot["slug"][0], tot["slug"][2], tot["slug"][1]],
           "technique_cells_ge2_F1": f1(tot["tr2"]), "technique_cells_ge2_counts": [tot["tr2"][0], tot["tr2"][2], tot["tr2"][1]]}
json.dump(summary, open(O / "agreement.json", "w"), indent=1)
json.dump(diffs, open(O / "diffs.json", "w"), indent=1)
print(json.dumps(summary, indent=1))
