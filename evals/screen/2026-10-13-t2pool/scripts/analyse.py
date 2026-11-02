"""T-SCREEN tier-2 comparison: eval set 2026-10-12a (dev, test) + the P5b escalation pool; offline trigger variants."""
import json, sys
from pathlib import Path
from stack.evals.gold import question_verdicts
S = Path(sys.argv[1]); D = Path("v4/evals-store/screen/2026-10-12a")
RUNS = {"dev": {"none": "20261013T034623Z_dev_a7dff8ce", "terra_whole": "20261013T034559Z_dev_aac2d200",
                "lunapro_whole": "20261013T034601Z_dev_9aca1025", "terra_12k2k": "20261013T034629Z_dev_680051c1",
                "luna_whole": "20261013T035517Z_dev_750108a7", "luna_48k8k": "20261013T043614Z_dev_3c4ef584"},
        "test": {"none": "20261013T034737Z_test_7bdfdc35", "terra_whole": "20261013T034648Z_test_d5ec6f89",
                 "lunapro_whole": "20261013T034651Z_test_2a4114b3", "terra_12k2k": "20261013T034723Z_test_7c84633b",
                 "luna_whole": "20261013T035542Z_test_1449d10e", "luna_48k8k": "20261013T043625Z_test_cb831e45"}}
POOLF = {"terra_whole": None, "lunapro_whole": "t2-openai_gpt-6-luna-pro-null.json", "terra_12k2k": "t2-openai_gpt-5.6-terra-[12000_2000].json",
         "luna_whole": "t2-openai_gpt-6-luna-null.json", "luna_48k8k": "t2-openai_gpt-6-luna-[48000_8000].json",
         "luna_24k4k": "t2-openai_gpt-6-luna-[24000_4000].json"}
PRICE = {"terra_whole": (2.0, 12.0), "terra_12k2k": (2.0, 12.0), "lunapro_whole": (0.1, 0.5), "luna_whole": (0.1, 0.5),
         "luna_48k8k": (0.1, 0.5), "luna_24k4k": (0.1, 0.5)}
gold = {g["item_id"]: g["label"] for g in map(json.loads, open("evals/screen/2026-10-12a/gold-v2.jsonl"))}
P5B_T1_PER_1K = 0.69      # RUNLOG P5b: settled tier 1 per 1,000 screened
P5B_ESC = 15 / 367        # P5b escalation share

def trig(kind, t1):
    """t1: dict stack/sr verdicts + confidences of tier 1; returns whether tier 2 is asked."""
    if t1["verdict"] != "borderline":
        return False
    if kind == "all":
        return True
    if kind == "stack":
        return t1["stack"] == "borderline"
    if kind == "lowconf":  # a borderline answer at confidence <= 0.6
        return any(t1[q] == "borderline" and (t1.get(q + "_conf") or 0) <= 0.6 for q in ("stack", "sr"))
    raise ValueError(kind)

def load_eval(split):
    rows = {}
    base = {r["item_id"]: r for r in map(json.loads, open(D / RUNS[split]["none"] / "rows.jsonl"))}
    for it, r in base.items():
        rows[it] = {"gold": (gold[it]["stack_verdict"], gold[it]["sr_verdict"]),
                    "t1": {"verdict": r["verdict"], "stack": r["stack_verdict"], "sr": r["sr_verdict"],
                           "stack_conf": r["stack_confidence"], "sr_conf": r["sr_confidence"]},
                    "t1_cost": r["call_cost_usd"], "t2": {}}
    for cfg, run in RUNS[split].items():
        if cfg == "none": continue
        for r in map(json.loads, open(D / run / "rows.jsonl")):
            if r["tier2_attempted"]:
                rows[r["item_id"]]["t2"][cfg] = {"stack": r["stack_verdict"], "sr": r["sr_verdict"],
                                                 "cost": r["call_cost_usd"] - base[r["item_id"]]["call_cost_usd"]}
    return rows

def load_pool():
    pool = json.load(open(S / "pool/pool.json"))
    items = {json.loads(l)["doc_id"]: json.loads(l)["item_id"] for l in open(S / "pktA/items.jsonl")}
    a = {json.loads(l)["item_id"]: json.loads(l) for l in open(S / "pktA-opus.jsonl") if l.strip()}
    adj = {json.loads(l)["item_id"]: json.loads(l) for l in open(S / "adjA-out.jsonl") if l.strip()}
    rows = {}
    t2f = {k: {x["doc_id"]: x for x in json.load(open(S / "pool" / f))["rows"]} for k, f in POOLF.items() if f}
    for p in pool:
        iid = items[p["doc_id"]]; lab = adj.get(iid) or a[iid]
        g = question_verdicts(lab["verdict"], lab["scope"], lab["sr_category"])
        t1 = p["tier1"]
        r = {"gold": g, "t1": {"verdict": t1["verdict"], "stack": t1["stack"]["verdict"], "sr": t1["sr"]["verdict"],
                              "stack_conf": t1["stack"]["confidence"], "sr_conf": t1["sr"]["confidence"]},
             "t1_cost": t1["cost_usd"], "t2": {}, "chars": p["chars"]}
        r["t2"]["terra_whole"] = {"stack": p["tier2"]["stack"]["verdict"], "sr": p["tier2"]["sr"]["verdict"], "cost": p["tier2"]["cost_usd"],
                                  "nocache": (p["tier2"]["tokens_in"] * 2.0 + p["tier2"]["tokens_out"] * 12.0) / 1e6}
        for k, d in t2f.items():
            x = d[p["doc_id"]]; pi, po = PRICE[k]
            r["t2"][k] = {"stack": x["stack"], "sr": x["sr"], "cost": x["cost_usd"], "nocache": (x["tokens_in"] * pi + x["tokens_out"] * po) / 1e6}
        rows[iid] = r
    return rows

def score(rows, cfg, kind):
    n = len(rows); acc = {"stack": 0, "sr": 0}; esc = 0; cost2 = 0.0; ch = {"corr": 0, "worse": 0, "same": 0}; nc = 0.0; mx = 0.0
    t1c = sum(r["t1_cost"] for r in rows.values())
    for r in rows.values():
        final = {"stack": r["t1"]["stack"], "sr": r["t1"]["sr"]}
        if cfg != "none" and trig(kind, r["t1"]):
            t2 = r["t2"][cfg]; esc += 1; cost2 += t2["cost"]; nc += t2.get("nocache", t2["cost"]); mx = max(mx, t2.get("nocache", t2["cost"]))
            for qi, q in enumerate(("stack", "sr")):
                before, after = r["t1"][q] == r["gold"][qi], t2[q] == r["gold"][qi]
                ch["corr" if after and not before else "worse" if before and not after else "same"] += 1
            final = {"stack": t2["stack"], "sr": t2["sr"]}
        acc["stack"] += final["stack"] == r["gold"][0]; acc["sr"] += final["sr"] == r["gold"][1]
    return {"n": n, "stack": acc["stack"], "sr": acc["sr"], "esc": esc, "cost2": cost2, "t1c": t1c, "nc": nc, "mx": mx, **ch}

out = []
CFGS = ("none", "terra_whole", "terra_12k2k", "lunapro_whole", "luna_whole", "luna_48k8k", "luna_24k4k")
for name, rows in (("eval dev", load_eval("dev")), ("eval test", load_eval("test")), ("pool (P5b escalations)", load_pool())):
    out.append(f"\n### {name}: n={len(rows)}")
    out.append("| config | trigger | Stack acc | SR acc | escalated | corrected / worsened / unchanged (question answers) | tier-2 $ per escalation | tier-2 share (this set) | $/1k (this set) |")
    out.append("|---|---|---|---|---|---|---|---|---|")
    for cfg in CFGS:
        if not name.startswith("pool") and cfg == "luna_24k4k": continue
        for kind in (("all",) if cfg == "none" else ("all", "stack")):
            s = score(rows, cfg, kind)
            per = s["cost2"] / s["esc"] if s["esc"] else 0.0
            share = s["cost2"] / (s["t1c"] + s["cost2"]) if s["cost2"] else 0.0
            per1k = 1000 * (s["t1c"] + s["cost2"]) / s["n"] if not name.startswith("pool") else float("nan")
            out.append(f"| {cfg} | {kind if cfg!='none' else '-'} | {s['stack']}/{s['n']} | {s['sr']}/{s['n']} | {s['esc']} | {s['corr']} / {s['worse']} / {s['same']} | {per:.4f} | {share:.0%} | {per1k:.2f} |")
print("\n".join(out))
# projection to the P5b mix, pool costs per escalation, trigger fraction from the pool
pool = load_pool()
print("\n### projection to the P5b mix: tier 1 $0.69/1k, escalation 4.1 % (15/367); trigger fraction and $/escalation from the pool")
print("| config | trigger | escalated share | tier-2 $/escalation settled / no cache | max $/escalation (no cache) | tier-2 $/1k | total $/1k | tier-2 share of spend | total $/1k at no-cache tier 2 |")
print("|---|---|---|---|---|---|---|---|---|")
for cfg in CFGS:
    for kind in (("all",) if cfg == "none" else ("all", "stack")):
        s = score(pool, cfg, kind)
        frac = s["esc"] / len(pool)
        per = s["cost2"] / s["esc"] if s["esc"] else 0.0
        t2 = 1000 * P5B_ESC * frac * per
        pnc = s["nc"] / s["esc"] if s["esc"] else 0.0
        t2nc = 1000 * P5B_ESC * frac * pnc
        print(f"| {cfg} | {kind if cfg!='none' else '-'} | {P5B_ESC*frac:.1%} | {per:.4f} / {pnc:.4f} | {s['mx']:.4f} | {t2:.3f} | {P5B_T1_PER_1K+t2:.2f} | {t2/(P5B_T1_PER_1K+t2):.0%} | {P5B_T1_PER_1K+t2nc:.2f} |")
