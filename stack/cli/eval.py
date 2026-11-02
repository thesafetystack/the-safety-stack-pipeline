"""``stack eval``: LLM workflow evaluations on frozen sets (track E; :mod:`stack.evals.harness`).

    stack eval triage --set 2026-10-12a --split dev --models anthropic/claude-haiku-5.5,openai/gpt-6-luna \\
        [--repeat 2] [--max-usd 1] [--prompt safety-relevance-v1] [--workers 64] [--estimate] \\
        [--items tri-0001,…] [--limit N] [--baseline <model>] [--gold <file>]
    stack eval screen  … [--tier2-model <id>] [--no-tier2] [--pin 'tier1_chars=[12000,2000]' …]
    stack eval affiliation …          # the v3 runner's LLM path (extract.md, role extract_cheap)
    stack eval model_roles --set 2026-10-17a … [--split all] [--prompt model-roles-v2]   # production main.ask (D591)
    stack eval technique_roles … [--split all] [--prompt technique-roles-v2]
    stack eval technique_staged --set 2026-10-17a --split all --models openai/gpt-6-luna --pin mode=sweep \
        [--pin prepass_model=anthropic/claude-haiku-5.5] [--pin select_model=…] [--repeat 2]   # techpipe D607
    stack eval model_mentions --set 2026-10-17a [--split all]   # no LLM: production scan vs frozen + gold
    stack eval score --run <run-dir> [--baseline <model>] [--gold <file>] [--set-dir <dir>] [--summary <name>]
    stack eval staged-compare <source-a> <source-b> [--gold-variant full] [--set-dir <dir>]   # technique_staged
    stack eval staged-table <run-dir> … [--unions] [--set-dir <dir>]        # technique_staged: all runs, one table
    stack eval spend
    stack eval combine triage --set 2026-10-12a --split dev --spec vote:gpt-6-luna:3:safety-relevance-v2 \\
        --spec 'escalate:gpt-6-luna@safety-relevance-v2:claude-sonnet-5.5:borderline' [--runs <dir>,…] [--gold <file>]
    stack eval pair-screens --stack-run <dir> --sr-run <dir> [--gold <file>]   # two single-question screens
    stack eval compare <run-a> <run-b> [--gold <file>]   # two screen runs, same items and model: paired
    stack eval audit-prompts --set 2026-10-12a [--model <id>]

The verbs that run a task or render its requests (triage, screen, affiliation, model_roles,
technique_roles, technique_staged, audit-prompts) need a plan (``--plan`` or ``STACK_PLAN``; there is no default plan).
The prompt pin's name comes from ``--prompt``, else the plan's pin, else the task's default; triage's
pin is the one the plan's gate question clause names (a plan with none needs ``--prompt``).
``--evals``, ``--out`` and ``--overlay`` default to the repository's ``evals/``, ``v4/evals-store/``
and ``evals/egress-overlay.yaml`` whatever the cwd (S25-96); a relative path given is the cwd's.
The gold file (``--gold``; a bare name is a file in the set directory) defaults to
``gold-v2.jsonl`` when the set has it, else ``gold.jsonl``; the run records it in ``run.json``
and ``score`` uses that unless ``--gold`` overrides (runs before E2: ``gold.jsonl``); ``combine``
takes the same default. Real runs pass the key file by path: ``STACK_ENV_FILE=../repo/.env uv run
--no-sync stack eval …`` (D160). Errors a verb expects print ``error: …`` and exit 1.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Annotated, Any

import typer

from stack.cli.common import PlanOpt, fail, load_plan_or_fail, resolve_globals
from stack.evals import harness
from stack.evals.overlay import DEFAULT_OVERLAY

app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="LLM workflow evaluations on frozen sets (evals/<task>/<set>/).")

SetOpt = Annotated[str, typer.Option("--set", help="Set id (evals/<task>/<set-id>/).")]
SplitOpt = Annotated[str, typer.Option("--split", help="dev, test or holdout (annotator tasks also: all).")]
ModelsOpt = Annotated[str, typer.Option("--models", help="Comma-separated model ids (priced in llm.yaml).")]
RepeatOpt = Annotated[int, typer.Option("--repeat", help="Repeats per (model, item); each has its own cache.")]
MaxUsdOpt = Annotated[float, typer.Option("--max-usd", help="Run budget (ledger) and estimate ceiling, USD.")]
PromptOpt = Annotated[str | None, typer.Option("--prompt", help="Prompt pin name (default: the plan's).")]
WorkersOpt = Annotated[int, typer.Option("--workers", help=f"Concurrent calls (1-{harness.MAX_WORKERS}).")]
EstimateOpt = Annotated[bool, typer.Option("--estimate", help="Estimate cost only; no calls.")]
ItemsOpt = Annotated[str | None, typer.Option("--items", help="Comma-separated item ids (within the split).")]
LimitOpt = Annotated[int | None, typer.Option("--limit", help="Only the first N items of the split.")]
BaselineOpt = Annotated[str | None, typer.Option("--baseline", help="McNemar baseline (default: first model).")]
EvalsOpt = Annotated[Path, typer.Option("--evals", help="Eval sets root.")]
OutOpt = Annotated[Path, typer.Option("--out", help="Eval scratch store root (gitignored).")]
OverlayOpt = Annotated[Path, typer.Option("--overlay", help="Eval-only egress overlay (D173 (E)).")]
GoldOpt = Annotated[str | None, typer.Option(
    "--gold", help="Gold file (name in the set dir, or a path); default gold-v2.jsonl if present, else gold.jsonl.")]


#: A ``--pin`` value that is not JSON but matches this is taken as a string (``mode=sweep``,
#: ``prepass_model=anthropic/claude-haiku-5.5``).
_BARE = re.compile(r"[A-Za-z][A-Za-z0-9_.:/-]*")


def _pins(values: list[str] | None) -> dict[str, Any]:
    """``--pin KEY=JSON`` values as a dict (the value parsed as JSON: ``8000``, ``[12000,2000]``,
    ``null``, ``"sweep"``; a bare token that is not JSON, such as ``sweep`` or ``openai/gpt-6-luna``,
    is the string itself); a malformed one or a repeated key fails."""
    out: dict[str, Any] = {}
    for v in values or []:
        key, sep, raw = v.partition("=")
        key = key.strip()
        if not sep or not key:
            fail(f"--pin {v!r}: expected KEY=JSON")
        try:
            out_value = json.loads(raw)
        except json.JSONDecodeError as exc:
            if not _BARE.fullmatch(raw.strip()):
                fail(f"--pin {v!r}: the value is not JSON nor a bare token ({exc})")
            out_value = raw.strip()
        if key in out:
            fail(f"--pin {key}: given twice")
        out[key] = out_value
    return out


def _split_csv(value: str | None) -> list[str] | None:
    if value is None:
        return None
    return [v.strip() for v in value.split(",") if v.strip()]


def _run(ctx: typer.Context, task: str, plan: Path | None, **kw: Any) -> None:
    g = resolve_globals(ctx, None, plan)
    opts = harness.EvalOptions(
        task=task, set_id=str(kw["set_id"]), split=str(kw["split"]), models=_split_csv(str(kw["models"])) or [],
        repeat=int(kw["repeat"]), max_usd=float(kw["max_usd"]),
        prompt=kw["prompt"], workers=int(kw["workers"]), estimate_only=bool(kw["estimate"]),
        items=_split_csv(kw["items"]), limit=kw["limit"], baseline=kw["baseline"],
        tier2=bool(kw.get("tier2", True)), tier2_model=kw.get("tier2_model"), gold=kw.get("gold"),
        pin_overrides=_pins(kw.get("pin")),
        evals_root=Path(kw["evals"]), out_root=Path(kw["out"]), overlay=Path(kw["overlay"]),
        plan=load_plan_or_fail(g.plan), argv={"verb": f"eval {task}", **{k: str(v) for k, v in ctx.params.items()}},
    )
    try:
        harness.run_eval(opts, echo=typer.echo, warn=lambda m: typer.echo(f"warning: {m}", err=True))
    except harness.EvalError as exc:
        fail(str(exc))


@app.command("triage")
def triage(
    ctx: typer.Context, set_id: SetOpt, models: ModelsOpt, split: SplitOpt = "dev", repeat: RepeatOpt = 1,
    max_usd: MaxUsdOpt = harness.DEFAULT_MAX_USD, prompt: PromptOpt = None, workers: WorkersOpt = harness.DEFAULT_WORKERS,
    estimate: EstimateOpt = False, items: ItemsOpt = None, limit: LimitOpt = None, baseline: BaselineOpt = None,
    gold: GoldOpt = None, evals: EvalsOpt = harness.DEFAULT_EVALS, out: OutOpt = harness.DEFAULT_OUT,
    overlay: OverlayOpt = DEFAULT_OVERLAY, plan: PlanOpt = None,
) -> None:
    """Triage question (workflows/triage/ask.py) on a frozen triage set."""
    _run(ctx, "triage", plan, set_id=set_id, models=models, split=split, repeat=repeat, max_usd=max_usd,
         prompt=prompt, workers=workers, estimate=estimate, items=items, limit=limit, baseline=baseline,
         gold=gold, evals=evals, out=out, overlay=overlay)


@app.command("screen")
def screen(
    ctx: typer.Context, set_id: SetOpt, models: ModelsOpt, split: SplitOpt = "dev", repeat: RepeatOpt = 1,
    max_usd: MaxUsdOpt = harness.DEFAULT_MAX_USD, prompt: PromptOpt = None, workers: WorkersOpt = harness.DEFAULT_WORKERS,
    estimate: EstimateOpt = False, items: ItemsOpt = None, limit: LimitOpt = None, baseline: BaselineOpt = None,
    tier2: Annotated[bool, typer.Option("--tier2/--no-tier2", help="Escalate borderlines to tier 2.")] = True,
    tier2_model: Annotated[str | None, typer.Option(
        "--tier2-model", help="Tier-2 model (default: llm.yaml role screen_adjudicate).")] = None,
    pin: Annotated[list[str] | None, typer.Option(
        "--pin", help="KEY=JSON plan-pin override for every model, e.g. 'tier1_chars=8000' (repeatable).")] = None,
    gold: GoldOpt = None, evals: EvalsOpt = harness.DEFAULT_EVALS, out: OutOpt = harness.DEFAULT_OUT,
    overlay: OverlayOpt = DEFAULT_OVERLAY, plan: PlanOpt = None,
) -> None:
    """Relevance screen (workflows/annotate/screen) on a frozen screen set; the model is tier 1."""
    _run(ctx, "screen", plan, set_id=set_id, models=models, split=split, repeat=repeat, max_usd=max_usd,
         prompt=prompt, workers=workers, estimate=estimate, items=items, limit=limit, baseline=baseline,
         tier2=tier2, tier2_model=tier2_model, pin=pin, gold=gold, evals=evals, out=out, overlay=overlay)


@app.command("affiliation")
def affiliation(
    ctx: typer.Context, set_id: SetOpt, models: ModelsOpt, split: SplitOpt = "dev", repeat: RepeatOpt = 1,
    max_usd: MaxUsdOpt = harness.DEFAULT_MAX_USD, prompt: PromptOpt = None, workers: WorkersOpt = harness.DEFAULT_WORKERS,
    estimate: EstimateOpt = False, items: ItemsOpt = None, limit: LimitOpt = None, baseline: BaselineOpt = None,
    evals: EvalsOpt = harness.DEFAULT_EVALS, out: OutOpt = harness.DEFAULT_OUT,
    overlay: OverlayOpt = DEFAULT_OVERLAY, plan: PlanOpt = None,
) -> None:
    """Byline affiliations (workflows/annotate/affiliation, LLM path; the XML tier is not run)."""
    _run(ctx, "affiliation", plan, set_id=set_id, models=models, split=split, repeat=repeat, max_usd=max_usd,
         prompt=prompt, workers=workers, estimate=estimate, items=items, limit=limit, baseline=baseline,
         evals=evals, out=out, overlay=overlay)


@app.command("model_roles")
def model_roles(
    ctx: typer.Context, set_id: SetOpt, models: ModelsOpt, split: SplitOpt = "dev", repeat: RepeatOpt = 1,
    max_usd: MaxUsdOpt = harness.DEFAULT_MAX_USD, prompt: PromptOpt = None, workers: WorkersOpt = harness.DEFAULT_WORKERS,
    estimate: EstimateOpt = False, items: ItemsOpt = None, limit: LimitOpt = None, baseline: BaselineOpt = None,
    pin: Annotated[list[str] | None, typer.Option(
        "--pin", help="KEY=JSON plan-pin key for every model: doc_cap (repeatable).")] = None,
    gold: GoldOpt = None, evals: EvalsOpt = harness.DEFAULT_EVALS, out: OutOpt = harness.DEFAULT_OUT,
    overlay: OverlayOpt = DEFAULT_OVERLAY, plan: PlanOpt = None,
) -> None:
    """Model roles (workflows/annotate/model_roles, production ask on the frozen whole text; set 2026-10-17a)."""
    _run(ctx, "model_roles", plan, set_id=set_id, models=models, split=split, repeat=repeat, max_usd=max_usd,
         prompt=prompt, workers=workers, estimate=estimate, items=items, limit=limit, baseline=baseline,
         pin=pin, gold=gold, evals=evals, out=out, overlay=overlay)


@app.command("technique_roles")
def technique_roles(
    ctx: typer.Context, set_id: SetOpt, models: ModelsOpt, split: SplitOpt = "dev", repeat: RepeatOpt = 1,
    max_usd: MaxUsdOpt = harness.DEFAULT_MAX_USD, prompt: PromptOpt = None, workers: WorkersOpt = harness.DEFAULT_WORKERS,
    estimate: EstimateOpt = False, items: ItemsOpt = None, limit: LimitOpt = None, baseline: BaselineOpt = None,
    gold: GoldOpt = None, evals: EvalsOpt = harness.DEFAULT_EVALS, out: OutOpt = harness.DEFAULT_OUT,
    overlay: OverlayOpt = DEFAULT_OVERLAY, plan: PlanOpt = None,
) -> None:
    """Technique roles (workflows/annotate/technique_roles, production ask on the frozen whole text)."""
    _run(ctx, "technique_roles", plan, set_id=set_id, models=models, split=split, repeat=repeat, max_usd=max_usd,
         prompt=prompt, workers=workers, estimate=estimate, items=items, limit=limit, baseline=baseline,
         gold=gold, evals=evals, out=out, overlay=overlay)


@app.command("technique_staged")
def technique_staged(
    ctx: typer.Context, set_id: SetOpt, models: ModelsOpt, split: SplitOpt = "all", repeat: RepeatOpt = 1,
    max_usd: MaxUsdOpt = harness.DEFAULT_MAX_USD, prompt: PromptOpt = None, workers: WorkersOpt = harness.DEFAULT_WORKERS,
    estimate: EstimateOpt = False, items: ItemsOpt = None, limit: LimitOpt = None, baseline: BaselineOpt = None,
    pin: Annotated[list[str] | None, typer.Option(
        "--pin", help="KEY=VALUE plan-pin key for every model: mode (required), prepass_model, select_model, role, "
                      "taxonomy (repeatable; VALUE is JSON or a bare token).")] = None,
    gold: GoldOpt = None, evals: EvalsOpt = harness.DEFAULT_EVALS, out: OutOpt = harness.DEFAULT_OUT,
    overlay: OverlayOpt = DEFAULT_OVERLAY, plan: PlanOpt = None,
) -> None:
    """Staged technique roles (workflows/annotate/technique_staged; --models = the grading model; techpipe D607)."""
    _run(ctx, "technique_staged", plan, set_id=set_id, models=models, split=split, repeat=repeat, max_usd=max_usd,
         prompt=prompt, workers=workers, estimate=estimate, items=items, limit=limit, baseline=baseline,
         pin=pin, gold=gold, evals=evals, out=out, overlay=overlay)


@app.command("model_mentions")
def model_mentions(
    set_id: SetOpt, split: SplitOpt = "dev", items: ItemsOpt = None, limit: LimitOpt = None, gold: GoldOpt = None,
    evals: EvalsOpt = harness.DEFAULT_EVALS, out: OutOpt = harness.DEFAULT_OUT,
) -> None:
    """Model mentions (no LLM): production scan of the model_roles set's whole texts vs frozen payload and gold."""
    from stack.evals import annotators

    try:
        annotators.run_mentions(set_id, split, items=_split_csv(items), limit=limit, gold=gold, evals_root=evals,
                                out_root=out, echo=typer.echo)
    except harness.EvalError as exc:
        fail(str(exc))


@app.command("score")
def score(
    run: Annotated[Path, typer.Option("--run", help="A finished run directory.")],
    baseline: BaselineOpt = None,
    set_dir: Annotated[Path | None, typer.Option("--set-dir", help="Set directory (default: run.json's).")] = None,
    gold: Annotated[str | None, typer.Option(
        "--gold", help="Gold file (name in the set dir, or a path); default: run.json's, else gold.jsonl.")] = None,
    summary: Annotated[str, typer.Option(
        "--summary", help="Name of the summary files written in the run dir (<name>.json, <name>.md); e.g. "
                          "summary-2026-10-17b when scoring on another revision of the set.")] = "summary",
) -> None:
    """Re-score a finished run (e.g. after gold changes): rewrites <summary>.json and <summary>.md."""
    from stack.evals import score as scorer

    if not (run / "run.json").is_file():
        fail(f"{run}: no run.json")
    try:
        s = scorer.score_run(run, set_dir=set_dir, baseline=baseline, gold=gold, summary=summary)
    except (ValueError, FileNotFoundError) as exc:
        fail(str(exc))
    typer.echo(s["markdown"])


SharesOpt = Annotated[str, typer.Option(
    "--length-shares", help="Production length-band shares: screen_relevant (default), all_text or brief.")]
StagedSetDirOpt = Annotated[Path | None, typer.Option(
    "--set-dir", help="Score on this set directory (another revision of the runs' set, same items.jsonl) instead "
                      "of the runs' own; --gold is then a file in it.")]


@app.command("staged-compare")
def staged_compare(
    source_a: Annotated[str, typer.Argument(help="Source A: RUN_DIR[@rK][#model][,…] or union:PART,PART.")],
    source_b: Annotated[str, typer.Argument(help="Source B (same syntax).")],
    variant: Annotated[str, typer.Option("--gold-variant", help="full (default), both or high.")] = "full",
    resamples: Annotated[int, typer.Option("--resamples", help="Bootstrap resamples.")] = 10_000,
    seed: Annotated[int, typer.Option("--seed", help="Bootstrap seed.")] = 20261017,
    gold: Annotated[str | None, typer.Option("--gold", help="Gold file (default: gold.jsonl of the set).")] = None,
    set_dir: StagedSetDirOpt = None,
    length_shares: SharesOpt = "screen_relevant",
    out: OutOpt = harness.DEFAULT_OUT,
) -> None:
    """technique_staged: paired document bootstrap of B - A (slug recall, precision, F1, weighted recall, cells)."""
    from stack.evals import technique_staged as ts

    try:
        a, b = ts.load_source(source_a), ts.load_source(source_b)
        ctx = ts.scoring_context([*a.runs, *b.runs], set_dir, gold, length_shares)
        res = ts.compare(a, b, ctx, variant=variant, resamples=resamples, seed=seed)
    except ts.AnalysisError as exc:
        fail(str(exc))
    res["markdown"] = ts.compare_markdown(res)
    typer.echo(res["markdown"])
    set_id = set_dir.name if set_dir is not None else str(a.runs[0]["set"])
    path = ts.write_analysis(out / "technique_staged" / set_id / "analysis", "compare", res)
    typer.echo(f"written: {path}")


@app.command("staged-table")
def staged_table(
    runs: Annotated[list[Path], typer.Argument(help="Finished technique_staged run directories.")],
    source: Annotated[list[str] | None, typer.Option(
        "--source", help="Extra source spec (e.g. union:RUN_A,RUN_B), repeatable.")] = None,
    unions: Annotated[bool, typer.Option("--unions/--no-unions", help="Add the union of each run's repeats.")] = True,
    gold: Annotated[str | None, typer.Option("--gold", help="Gold file (default: gold.jsonl of the set).")] = None,
    set_dir: StagedSetDirOpt = None,
    length_shares: SharesOpt = "screen_relevant",
    out: OutOpt = harness.DEFAULT_OUT,
) -> None:
    """technique_staged: one results table over many runs (no calls)."""
    from stack.evals import technique_staged as ts

    try:
        res = ts.results_table(runs, unions=unions, gold_file=gold, shares=length_shares,
                               extra_sources=source or (), set_dir=set_dir)
    except ts.AnalysisError as exc:
        fail(str(exc))
    typer.echo(res["markdown"])
    set_id = (set_dir.name if set_dir is not None else
              json.loads((runs[0] / "run.json").read_text("utf-8"))["set"] if runs else "set")
    path = ts.write_analysis(out / "technique_staged" / str(set_id) / "analysis", "table", res)
    typer.echo(f"written: {path}")


@app.command("combine")
def combine(
    task: Annotated[str, typer.Argument(help="triage or screen.")],
    set_id: SetOpt,
    spec: Annotated[list[str], typer.Option("--spec", help=(
        "single:<model>[:<variant>] | vote:<model>:<k>[:<variant>] | escalate:<first>:<second>:borderline|conf<0.X "
        "(repeatable; <model>@<variant> pins a variant)"))],
    split: SplitOpt = "dev",
    runs: Annotated[str | None, typer.Option("--runs", help="Comma-separated run dirs (default: all of the split).")] = None,
    gold: GoldOpt = None, out: OutOpt = harness.DEFAULT_OUT,
) -> None:
    """Score vote / escalation scaffolds over collected rows (no calls): quality and USD per 1,000 docs."""
    from stack.evals import combine as comb

    try:
        res = comb.combine(task, set_id, split, spec, runs=_split_csv(runs), out_root=out, gold=gold)
    except comb.CombineError as exc:
        fail(str(exc))
    typer.echo(res["markdown"])
    typer.echo(f"written: {res['path']}")


@app.command("pair-screens")
def pair_screens(
    stack_run: Annotated[Path, typer.Option("--stack-run", help="A finished screen-stack-v1 run directory.")],
    sr_run: Annotated[Path, typer.Option("--sr-run", help="A finished screen-sr-v1 run directory.")],
    gold: Annotated[str | None, typer.Option(
        "--gold", help="Gold file (name in the set dir, or a path); default: the Stack run's.")] = None,
) -> None:
    """Score two single-question screen runs as one two-call design (no calls), per question."""
    from stack.evals import combine as comb

    try:
        res = comb.pair_screens(stack_run, sr_run, gold=gold)
    except (comb.CombineError, FileNotFoundError) as exc:
        fail(str(exc))
    typer.echo(res["markdown"])
    typer.echo(f"written: {res['path']}")


@app.command("compare")
def compare(
    run_a: Annotated[Path, typer.Argument(help="A finished screen run directory (A).")],
    run_b: Annotated[Path, typer.Argument(help="A finished screen run directory (B), same items and model.")],
    gold: Annotated[str | None, typer.Option(
        "--gold", help="Gold file (name in the set dir, or a path); default: run A's.")] = None,
) -> None:
    """Paired comparison of two screen runs per question (S, R, overall keep): accuracy, McNemar (no calls)."""
    from stack.evals import score as scorer

    try:
        res = scorer.compare_runs(run_a, run_b, gold=gold)
    except (ValueError, FileNotFoundError) as exc:
        fail(str(exc))
    typer.echo(res["markdown"])
    typer.echo(f"written: {res['path']}")


@app.command("audit-prompts")
def audit_prompts(
    ctx: typer.Context, set_id: SetOpt,
    model: Annotated[str, typer.Option("--model", help="Model the requests are rendered for.")] = harness.AUDIT_MODEL,
    evals: EvalsOpt = harness.DEFAULT_EVALS, out: OutOpt = harness.DEFAULT_OUT,
    overlay: OverlayOpt = DEFAULT_OVERLAY, plan: PlanOpt = None,
) -> None:
    """Request structure of each eval prompt (prefix, document position, cache_control); no calls."""
    g = resolve_globals(ctx, None, plan)
    try:
        res = harness.audit_prompts(set_id=set_id, model=model, evals_root=evals, out_root=out, overlay=overlay,
                                    plan=load_plan_or_fail(g.plan))
    except harness.EvalError as exc:
        fail(str(exc))
    typer.echo(res["markdown"])
    typer.echo(f"written: {res['path']}")


@app.command("spend")
def spend(out: OutOpt = harness.DEFAULT_OUT) -> None:
    """Track E's total settled spend from the runs' records."""
    s = harness.spend(out)
    typer.echo(f"eval runs: {s['runs']}; settled ${s['settled_usd']:.6f} (records in {out / harness.SPEND_FILE})")
    for k, v in sorted(s["by_task"].items()):
        typer.echo(f"  task {k:<12} ${v:.6f}")
    for k, v in sorted(s["by_model"].items()):
        typer.echo(f"  model {k:<36} ${v:.6f}")
