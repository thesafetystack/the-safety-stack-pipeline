import json, sys
from pathlib import Path
S = Path(sys.argv[1]); D = Path("v4/evals-store/screen/2026-10-12a")
a = {json.loads(l)["item_id"]: json.loads(l) for l in open(S / "pktB-opus1.jsonl") if l.strip()}
b = {json.loads(l)["item_id"]: json.loads(l) for l in open(S / "pktB-opus2.jsonl") if l.strip()}
adj = {json.loads(l)["item_id"]: json.loads(l) for l in open(S / "pktB-adj.jsonl") if l.strip()}
gold = {i: {"doc_type": adj[i]["doc_type"] if i in adj else a[i]["doc_type"], "self_published": a[i]["self_published"],
            "primary_lang": a[i]["primary_lang"]} for i in a}
ct = json.load(open("evals/screen/2026-10-12a/collector-doc-types.json"))["doc_types"]
llm_items = {i for i in gold if ct[i] not in ("forum_post", "lab_blog", "system_card", "framework_policy", "model_card", "tweet", "social_post")}
coarse = lambda t: "paper|preprint" if t in ("paper", "preprint") else t
v12 = {}
for run in ("20261013T035245Z_dev_9440d144", "20261013T035350Z_test_fe4924d2"):
    for r in map(json.loads, open(D / run / "rows.jsonl")):
        v12[r["item_id"]] = r
systems = {"integrated v12 (record: channel rule + model)": {i: {"doc_type": v12[i]["doc_type"], "self_published": v12[i]["self_published"], "primary_lang": v12[i]["primary_lang"]} for i in gold},
           "integrated v12 (model answer only)": {i: {"doc_type": v12[i]["doc_type_model"], "self_published": v12[i]["self_published"], "primary_lang": v12[i]["primary_lang"]} for i in gold},
           "collector metadata.doc_type only": {i: {"doc_type": {"journal": "paper", "conference": "paper"}.get(ct[i], ct[i]), "self_published": None, "primary_lang": None} for i in gold}}
cost = {}
for m in ("openai_gpt-6-luna", "openai_gpt-5-nano", "google_gemini-2.5-flash-lite", "anthropic_claude-haiku-5.5"):
    d = json.load(open(S / f"dcsep-{m}.json"))
    rows = {r["item_id"]: r for r in d["rows"]}
    ans = {}
    for i in gold:
        r = rows[i]
        if "error" in r: ans[i] = None; continue
        ans[i] = {"doc_type": ct[i] if i not in llm_items and ct[i] in ("forum_post", "lab_blog", "system_card") else r["doc_type"],
                  "self_published": r["self_published"], "primary_lang": r["primary_lang"]}
    systems[f"separate {d['model']} (+ channel rule)"] = ans
    ok = [r for r in d["rows"] if "error" not in r]
    cost[f"separate {d['model']} (+ channel rule)"] = (1000 * sum(r["cost_usd"] for r in ok) / len(ok), sum(r["latency_ms"] for r in ok) / len(ok), len(d["rows"]) - len(ok))
print(f"gold: 60 items (two Opus labelers, doc_type agree 54/60, 6 adjudicated); LLM-decided items (collector type paper/preprint/journal): {len(llm_items)}")
print("| system | doc_type exact (60) | exact on LLM-decided | paper/preprint merged, LLM-decided | self_published | primary_lang | $/1k docs | errors |")
print("|---|---|---|---|---|---|---|---|")
for name, ans in systems.items():
    def acc(items, f, key="doc_type"):
        got = [ans[i] is not None and f(ans[i][key]) == f(gold[i][key]) for i in items]
        return f"{sum(got)}/{len(got)}"
    idf = lambda x: x
    sp = acc(gold, idf, "self_published") if "collector" not in name else "-"
    pl = acc(gold, idf, "primary_lang") if "collector" not in name else "-"
    c = cost.get(name)
    print(f"| {name} | {acc(gold, idf)} | {acc(llm_items, idf)} | {acc(llm_items, coarse)} | {sp} | {pl} | {('%.3f' % c[0]) if c else ('+0.10 (v12 vs v11 tier 1)' if 'integrated' in name else '0')} | {c[2] if c else 0} |")
from collections import Counter
print("gold doc_type on LLM-decided:", Counter(gold[i]["doc_type"] for i in llm_items))
for name, ans in systems.items():
    print(name, "confusions:", Counter((gold[i]["doc_type"], ans[i]["doc_type"]) for i in llm_items if ans[i] and ans[i]["doc_type"] != gold[i]["doc_type"]).most_common(6))
