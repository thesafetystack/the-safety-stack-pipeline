"""doc_class as a separate call (T-SCREEN eval): 60 items of 2026-10-12a, one model per run, through the production
client (ledger, cache, egress gate). Usage: dc_sep.py <pktB dir> <model> <max_usd> [--estimate]"""
import gzip, json, math, sys, datetime as dt
from pathlib import Path
from stack.annotate.stage import stack_config
from stack.config import ModelPrice
from stack.evals import overlay as egress_overlay
from stack.llm import open_client, LlmError, strict_schema, estimate_tokens, RESERVE_OVERHEAD_TOKENS, RESERVE_SAFETY_FACTOR
import importlib.util
spec = importlib.util.spec_from_file_location("screen_main", "workflows/annotate/screen/main.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
pkt, model, max_usd = Path(sys.argv[1]), sys.argv[2], float(sys.argv[3])
SET = Path("evals/screen/2026-10-12a")
#: prices from one keyless fetch of openrouter.ai/api/v1/models (2026-10-13), in-memory for this eval only
EXTRA = {"openai/gpt-5-nano": (0.05, 0.4), "google/gemini-2.5-flash-lite": (0.1, 0.4)}
base = stack_config()
cfg = egress_overlay.overlaid_config(base, egress_overlay.load_overlay(Path("evals/egress-overlay.yaml")))
prices = dict(cfg.llm.pricing.models)
for k, (i, o) in EXTRA.items():
    prices.setdefault(k, ModelPrice(in_usd_per_m=i, out_usd_per_m=o))
llm = cfg.llm.model_copy(update={"pricing": cfg.llm.pricing.model_copy(update={"models": prices})})
import dataclasses; cfg = dataclasses.replace(cfg, llm=llm)
tmpl = (SET / "doc-class/prompt-separate-v1.md").read_text()
schema = strict_schema({"type": "object", "additionalProperties": False, "required": ["doc_type", "self_published"],
                        "properties": {"doc_type": {"type": "string", "enum": list(m.DOC_TYPES)},
                                       "self_published": {"type": "boolean"},
                                       "primary_lang": {"type": "string", "maxLength": 16}}})
items = {json.loads(l)["item_id"]: json.loads(l) for l in open(SET / "items.jsonl")}
pick = [json.loads(l)["item_id"] for l in open(pkt / "items.jsonl")]
MAXTOK = 1024
users, est = {}, 0.0
for iid in pick:
    it = items[iid]
    text = gzip.decompress((SET / "texts" / f"{iid}.txt.gz").read_bytes()).decode()
    users[iid] = f"Title: {it['input']['title'] or 'Unknown'}\n\nText:\n" + m.screening_text(text, (6000, 1000))[0]
    body = json.dumps([tmpl, users[iid], schema])
    est += llm.price_for(model).cost(math.ceil((estimate_tokens(body) + RESERVE_OVERHEAD_TOKENS) * RESERVE_SAFETY_FACTOR), MAXTOK)
print(f"{model}: reservation estimate ${est:.4f} for {len(pick)} calls (max {max_usd})")
if "--estimate" in sys.argv or est > max_usd:
    sys.exit(0)
run_id = "eval-doc_class-" + dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
client = open_client(cfg, run_id=run_id, stage="eval.doc_class", store_root=Path("v4/evals-store/doc_class/cache") / model.replace("/", "_"),
                     budget_usd=max_usd)
out = []
for iid in pick:
    it = items[iid]
    try:
        res = client.complete(role="classify", model=model, template=tmpl, refs={}, user=users[iid], schema=schema,
                              license_class=it["license_class"] or "restricted", max_tokens=MAXTOK,
                              prompt_name="doc-class-separate-v1", basis_digest=it["input"]["text_digest"])
        out.append({"item_id": iid, **{k: res.parsed.get(k) for k in ("doc_type", "self_published", "primary_lang")},
                    "cost_usd": res.cost_usd, "tokens_in": res.tokens_in, "tokens_out": res.tokens_out, "latency_ms": res.latency_ms})
    except LlmError as exc:
        out.append({"item_id": iid, "error": f"{type(exc).__name__}: {str(exc)[:160]}", "cost_usd": getattr(exc, "cost_usd", 0.0) or 0.0})
p = Path(sys.argv[1]).parent / f"dcsep-{model.replace('/', '_')}.json"
p.write_text(json.dumps({"run_id": run_id, "model": model, "rows": out}, indent=1))
print(p, "settled $%.4f" % sum(x["cost_usd"] or 0 for x in out), "errors", sum("error" in x for x in out))
