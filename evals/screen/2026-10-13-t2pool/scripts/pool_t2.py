"""Counterfactual tier 2 on P5b's 15 real escalations: P5b's tier-1 answer fixed (its rationale in the
preamble, as production's ask builds it), tier 2 re-asked under another (model, tier2 limit) through the
production client (ledger, cache, egress gate). Usage: pool_t2.py <pool dir> <model> <limit-json> <max_usd> [--estimate]"""
import importlib.util, json, sys, datetime as dt
from pathlib import Path
from stack.annotate.stage import stack_config
from stack.evals import overlay as egress_overlay
from stack.llm import open_client, LlmError

pool_dir, model, limit_json, max_usd = Path(sys.argv[1]), sys.argv[2], sys.argv[3], float(sys.argv[4])
estimate_only = "--estimate" in sys.argv
limit = json.loads(limit_json)
limit = tuple(limit) if isinstance(limit, list) else limit
spec = importlib.util.spec_from_file_location("screen_main", "workflows/annotate/screen/main.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
base = stack_config()
cfg = egress_overlay.overlaid_config(base, egress_overlay.load_overlay(Path("evals/egress-overlay.yaml")))
name = "screen-v11"
tmpl, schema = m.template(name), m.schema_for(name)
refs = {"enums": m._render_menu(cfg, m.MENUS)}
pool = json.loads((pool_dir / "pool.json").read_text())
tag = f"{m.__name__}-{model.replace('/', '_')}-{limit_json.replace(' ', '')}"
cache = Path("v4/evals-store/screen/t2pool/cache") / model.replace("/", "_") / limit_json.replace(" ", "").replace(",", "_")
est_total = 0.0
for r in pool:
    text = (pool_dir / "texts" / f"{r['doc_id']}.txt").read_text()
    pre = m._TIER2_PREAMBLE if limit is None else m._TIER2_PREAMBLE_LIMITED
    r["_user"] = (pre.format(rationale=str(r["tier1"]["rationale"] or "")[:400]) + f"Title: {r['title'] or 'Unknown'}\n\nScreening text:\n"
                  + m.screening_text(text, limit)[0])
    est_total += m.tier2_estimate(cfg.llm, model, template=tmpl, refs=refs, user=r["_user"], schema=schema)
print(f"{model} {limit_json}: reservation-formula estimate ${est_total:.4f} for {len(pool)} calls (max {max_usd})")
if estimate_only or est_total > max_usd:
    sys.exit(0 if estimate_only else 2)
run_id = "eval-screen-t2pool-" + dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
client = open_client(cfg, run_id=run_id, stage="eval.screen", store_root=cache, budget_usd=max_usd)
out = []
for r in pool:
    try:
        res = client.complete(role=m.ADJUDICATE_ROLE, model=model, template=tmpl, refs=refs, user=r["_user"], schema=schema,
                              license_class=r["license_class"] or "restricted", prompt_name=name,
                              basis_digest=r["text_digest"], attempt_kind="adjudicate")
        p = res.parsed
        out.append({"doc_id": r["doc_id"], "verdict": p["verdict"], "stack": p["stack"]["verdict"], "sr": p["sr"]["verdict"],
                    "stack_conf": p["stack"].get("confidence"), "sr_conf": p["sr"].get("confidence"),
                    "cost_usd": res.cost_usd, "tokens_in": res.tokens_in, "tokens_out": res.tokens_out, "cache_hit": res.is_cache_hit})
    except LlmError as exc:
        out.append({"doc_id": r["doc_id"], "error": f"{type(exc).__name__}: {str(exc)[:200]}", "cost_usd": getattr(exc, "cost_usd", 0.0)})
outp = pool_dir / f"t2-{model.replace('/', '_')}-{limit_json.replace(' ', '').replace(',', '_')}.json"
outp.write_text(json.dumps({"run_id": run_id, "model": model, "limit": limit_json, "rows": out}, indent=1))
print(outp, "settled $%.4f" % sum(x.get("cost_usd") or 0 for x in out), "errors", sum("error" in x for x in out))
