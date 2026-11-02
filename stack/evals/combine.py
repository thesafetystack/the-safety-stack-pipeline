"""``stack eval combine <task> --set S --split dev|test --spec <spec> [--spec …] [--runs <dir,…>]``:
score scaffolds over rows already collected (no new calls) — majority votes over repeats, a cheap
first pass escalating uncertain items to a second model — each with its quality and its cost per
1,000 documents, with and without the provider prompt cache.

**Rows.** Every finished run of the set and split under ``<out>/<task>/<set>/`` (or ``--runs``),
indexed by (model, variant, repeat, item). The *variant* is the run's prompt pin name (``run.json``
``prompt``), for screen with ``+t2`` when the run escalated borderlines to tier 2 (its verdict is
then the tier-2 one): ``screen-v7``, ``screen-v7+t2``, ``safety-relevance-v2``. When two runs hold
the same key (a rerun replayed from the response cache, a repeat-3 run over repeats 1-2), the
later run's row wins unless it failed and the earlier one answered. ``skipped`` /
``not_attempted`` rows are absent. Usage and cost fields come from the rows, backfilled from the
response-cache entries for rows written before the harness recorded them
(:func:`stack.evals.score.backfill_usage`).

**Specs** (a model is its full id or a unique suffix, e.g. ``gpt-6-luna``; ``<model>@<variant>``
pins the variant anywhere; without one the model must have exactly one variant in the rows):

* ``single:<model>[:<variant>]`` — repeat 1, for reference;
* ``vote:<model>:<k>[:<variant>]`` — majority over repeats 1..k; a failed repeat casts no vote
  (all failed: no answer); ties go to the more inclusive verdict (triage relevant > borderline >
  not_relevant, screen relevant > borderline > irrelevant); the quotes are those of the first
  repeat that gave the winning verdict; cost = the k calls;
* ``escalate:<first>:<second>:<rule>`` — repeat 1 of ``first`` on every item; where it matches
  the rule (``borderline``: answered borderline; ``conf<0.X``: confidence below 0.X, triage only)
  or gave no answer, the answer is ``second``'s repeat 1; cost = first on every item + second on
  the escalated items; also the escalation share.

An item is *covered* when every row the spec needs is present (vote: repeats 1..k; escalate:
first, and second where escalated); metrics are over covered gold items and the coverage is
reported. Gold: ``--gold`` (a name in the set directory, or a path), else ``gold-v2.jsonl`` when the
set has it, else ``gold.jsonl`` (:func:`stack.evals.score.default_gold`, the harness's rule); a set
with no gold refuses. A run whose ``set_items_digest`` is not the sha256 of the set's
``items.jsonl`` now (a set changed since the run, or a run that recorded none) refuses, naming both
digests (:func:`stack.evals.score.items_digest_problem`; pair-screens and compare check the same).
Quality per task as :mod:`stack.evals.score` computes it on the combined answers:
triage recall of gold fetch under ``fetch_on = [relevant]`` and ``[relevant, borderline]``,
precision under both, 3-way accuracy and κ; screen accuracy, κ, relevant recall / precision and
the share of relevant/borderline answers carrying at least one kept (anchored) quote. Cost per
1,000 documents = 1,000 x the mean over covered items of the summed ``call_cost_usd`` (settled,
provider cache included; a replayed row carries the original call's cost) and of
``cost_nocache_usd`` (``llm.yaml`` table price). Output: a markdown table on stdout and in
``<out>/<task>/<set>/combine-<stamp>.md``.

``stack eval pair-screens --stack-run <dir> --sr-run <dir> [--gold <file>]`` (:func:`pair_screens`,
D???(E)): a ``screen-stack-v1`` run and a ``screen-sr-v1`` run scored as one two-call design, in
the per-question table ``score`` writes for a ``screen-v10`` run.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from stack.evals import score
from stack.evals.harness import DEFAULT_OUT

Row = dict[str, Any]

#: Verdicts from most to least inclusive (vote tie-break).
INCLUSIVE = {"triage": ("relevant", "borderline", "not_relevant"),
             "screen": ("relevant", "borderline", "irrelevant")}


class CombineError(ValueError):
    """A spec or a run selection that cannot be scored (named in the message)."""


@dataclass(frozen=True)
class Arm:
    model: str
    variant: str

    def label(self) -> str:
        return f"{self.model}@{self.variant}"


@dataclass(frozen=True)
class Spec:
    text: str
    kind: str
    arms: tuple[Arm, ...]
    k: int = 1
    threshold: float | None = None  # escalate conf<X; None = the borderline rule


# ── rows ─────────────────────────────────────────────────────────────────────────────────────


def variant_of(run: Row) -> str:
    t2 = run["task"] == "screen" and any(bool((p or {}).get("tier2")) for p in (run.get("pins") or {}).values())
    return str(run["prompt"]) + ("+t2" if t2 else "")


@dataclass
class Pool:
    """The indexed rows of a set and split (module docstring)."""

    task: str
    split: str
    set_dir: Path
    runs: list[str]
    rows: dict[tuple[str, str, int, str], Row]
    items: dict[str, Row]
    gold: dict[str, Row]
    conflicts: int
    backfill: Row
    gold_file: str = score.DEFAULT_GOLD

    def variants(self, model: str) -> list[str]:
        return sorted({v for (m, v, _, _) in self.rows if m == model})

    def models(self) -> list[str]:
        return sorted({m for (m, _, _, _) in self.rows})


def run_dirs(out_root: Path, task: str, set_id: str, split: str, runs: Sequence[str] | None) -> list[Path]:
    base = out_root / task / set_id
    if runs:
        dirs = [Path(r) if Path(r).is_absolute() or Path(r).exists() else base / r for r in runs]
    else:
        dirs = sorted(p for p in base.iterdir() if p.is_dir() and p.name != "cache") if base.is_dir() else []
    out = []
    for d in dirs:
        rj = d / "run.json"
        if not rj.is_file():
            if runs:
                raise CombineError(f"--runs {d}: no run.json")
            continue
        run = json.loads(rj.read_text("utf-8"))
        if not (d / "rows.jsonl").is_file():  # an --estimate record, or a run still going
            if runs:
                raise CombineError(f"--runs {d}: no rows.jsonl (status {run.get('status')!r})")
            continue
        if (run["task"], run["set"], run["split"]) != (task, set_id, split):
            if runs:
                raise CombineError(f"--runs {d}: is {run['task']}/{run['set']}/{run['split']}, "
                                   f"not {task}/{set_id}/{split}")
            continue
        out.append(d)
    if not out:
        raise CombineError(f"no finished {task}/{set_id}/{split} runs under {base}")
    return sorted(out, key=lambda p: p.name)


def load_pool(out_root: Path, task: str, set_id: str, split: str, runs: Sequence[str] | None = None,
              *, price_for: Any = None, gold: str | None = None) -> Pool:
    if task not in INCLUSIVE:
        raise CombineError(f"combine: task {task!r} has no verdict to vote on (triage, screen)")
    price_for = price_for or score._llm_config().price_for
    dirs = run_dirs(out_root, task, set_id, split, runs)
    index: dict[tuple[str, str, int, str], Row] = {}
    conflicts = 0
    backfill: Counter[str] = Counter()
    set_dir: Path | None = None
    items: dict[str, Row] = {}
    for d in dirs:
        run = json.loads((d / "run.json").read_text("utf-8"))
        sd = Path(run["set_dir"])
        if set_dir is None:
            set_dir = sd
            items = {i["item_id"]: i for i in score.read_jsonl(_items_file(sd))}
        elif sd != set_dir:
            raise CombineError(f"{d}: set_dir {sd} differs from {set_dir}")
        problem = score.items_digest_problem(run, sd)
        if problem is not None:
            raise CombineError(f"{d}: {problem}")
        rows = score.read_jsonl(d / "rows.jsonl")
        backfill.update(score.backfill_usage(run, rows, items, price_for))
        var = variant_of(run)
        for r in rows:
            if r["status"] in ("skipped", "not_attempted"):
                continue
            key = (r["model"], var, int(r["repeat"]), r["item_id"])
            old = index.get(key)
            if old is not None and old["status"] == "ok":
                if r["status"] != "ok":
                    continue
                if score.answer(old) != score.answer(r):
                    conflicts += 1
            index[key] = r
    assert set_dir is not None
    try:
        gold_file, gold_rows = score.load_gold(set_dir, gold or score.default_gold(set_dir))
    except FileNotFoundError as exc:
        raise CombineError(str(exc)) from None
    if not gold_rows:
        raise CombineError(f"{set_dir}: no gold ({gold_file})")
    return Pool(task, split, set_dir, [d.name for d in dirs], index, items, gold_rows, conflicts, dict(backfill),
                gold_file)


# ── specs ────────────────────────────────────────────────────────────────────────────────────


def _arm(pool: Pool, token: str, variant: str | None = None) -> Arm:
    if "@" in token:
        token, at = token.split("@", 1)
        if variant is not None and variant != at:
            raise CombineError(f"{token}: two variants given ({at!r}, {variant!r})")
        variant = at
    models = pool.models()
    hits = [m for m in models if m == token] or [m for m in models if m.split("/", 1)[-1] == token
                                                 or m.endswith("/" + token)]
    if len(hits) != 1:
        raise CombineError(f"model {token!r}: {'ambiguous: ' + ', '.join(hits) if hits else 'not in the rows'} "
                           f"(models: {', '.join(models)})")
    model = hits[0]
    have = pool.variants(model)
    if variant is None:
        if len(have) != 1:
            raise CombineError(f"{model} has variants {have} in the rows: name one ({model}@<variant> or :<variant>)")
        variant = have[0]
    elif variant not in have:
        raise CombineError(f"{model}: no rows of variant {variant!r} (have {have})")
    return Arm(model, variant)


_CONF = re.compile(r"conf<(0?\.\d+|1(?:\.0*)?)")


def parse_spec(pool: Pool, text: str) -> Spec:
    parts = text.split(":")
    kind = parts[0]
    if kind == "single" and len(parts) in (2, 3):
        return Spec(text, kind, (_arm(pool, parts[1], parts[2] if len(parts) == 3 else None),))
    if kind == "vote" and len(parts) in (3, 4):
        try:
            k = int(parts[2])
        except ValueError:
            raise CombineError(f"{text}: k {parts[2]!r} is not an integer") from None
        if k < 1:
            raise CombineError(f"{text}: k must be >= 1")
        return Spec(text, kind, (_arm(pool, parts[1], parts[3] if len(parts) == 4 else None),), k=k)
    if kind == "escalate" and len(parts) == 4:
        rule = parts[3]
        threshold = None
        if rule != "borderline":
            m = _CONF.fullmatch(rule)
            if m is None:
                raise CombineError(f"{text}: rule {rule!r} is neither 'borderline' nor 'conf<0.X'")
            if pool.task != "triage":
                raise CombineError(f"{text}: conf<X is triage only (the {pool.task} rows carry no confidence)")
            threshold = float(m.group(1))
        return Spec(text, kind, (_arm(pool, parts[1]), _arm(pool, parts[2])), threshold=threshold)
    raise CombineError(f"spec {text!r}: single:<model>[:<variant>] | vote:<model>:<k>[:<variant>] | "
                       f"escalate:<first>:<second>:borderline|conf<0.X")


# ── evaluation ───────────────────────────────────────────────────────────────────────────────


def final_quotes(r: Row) -> int:
    """Kept (anchored) quotes of the row's final answer (tier 2's when tier 2 answered)."""
    return int(r.get("tier2_quotes_kept") or 0) if r.get("adjudication_tier") == 2 else int(r.get("quotes_kept") or 0)


def majority(task: str, rows: Sequence[Row]) -> Row:
    """The row carrying the majority verdict of ``rows`` (failed rows cast no vote; ties → the
    more inclusive verdict; the first row with that verdict); all failed: the first row."""
    votes = Counter(score.answer(r) for r in rows if score.answer(r) != score.NO_ANSWER)
    if not votes:
        return rows[0]
    order = INCLUSIVE[task]
    top = max(votes.values())
    best = min((v for v, c in votes.items() if c == top), key=lambda v: order.index(v) if v in order else len(order))
    return next(r for r in rows if score.answer(r) == best)


def _escalates(spec: Spec, first: Row) -> bool:
    a = score.answer(first)
    if a == score.NO_ANSWER:
        return True
    if spec.threshold is None:
        return a == "borderline"
    conf = first.get("confidence")
    return conf is not None and float(conf) < spec.threshold


def combine_rows(pool: Pool, spec: Spec) -> tuple[list[Row], Row]:
    """One synthetic row per covered gold item (the final answer's row, relabelled, with the
    spec's summed costs) and the coverage / escalation / cost counts."""
    ids = sorted(i for i, it in pool.items.items() if i in pool.gold and it["split"] == pool.split)
    out: list[Row] = []
    escalated = uncovered = 0
    for iid in ids:
        def get(arm: Arm, k: int) -> Row | None:
            return pool.rows.get((arm.model, arm.variant, k, iid))  # noqa: B023

        used: list[Row]
        if spec.kind == "single":
            r = get(spec.arms[0], 1)
            if r is None:
                uncovered += 1
                continue
            final, used = r, [r]
        elif spec.kind == "vote":
            rs = [get(spec.arms[0], k) for k in range(1, spec.k + 1)]
            if any(r is None for r in rs):
                uncovered += 1
                continue
            used = [r for r in rs if r is not None]
            final = majority(pool.task, used)
        else:
            first = get(spec.arms[0], 1)
            if first is None:
                uncovered += 1
                continue
            final, used = first, [first]
            if _escalates(spec, first):
                second = get(spec.arms[1], 1)
                if second is None:
                    uncovered += 1
                    continue
                final, used = second, [first, second]
                escalated += 1
        costs = [r.get("call_cost_usd") for r in used]
        nocache = [r.get("cost_nocache_usd") for r in used]
        out.append({**final, "model": spec.text, "repeat": 1,
                    "spec_cost_usd": None if any(c is None for c in costs) else sum(float(c) for c in costs if c is not None),
                    "spec_cost_nocache_usd": (None if any(c is None for c in nocache)
                                              else sum(float(c) for c in nocache if c is not None)),
                    "spec_calls": len(used)})
    return out, {"items": len(ids), "covered": len(out), "uncovered": uncovered, "escalated": escalated}


def evaluate(pool: Pool, spec: Spec) -> Row:
    rows, counts = combine_rows(pool, spec)
    costed = [r for r in rows if r["spec_cost_usd"] is not None and r["spec_cost_nocache_usd"] is not None]
    out: Row = {"spec": spec.text, "kind": spec.kind, "arms": [a.label() for a in spec.arms], "k": spec.k,
                **counts,
                "escalation_share": score.ratio(counts["escalated"], counts["covered"]) if spec.kind == "escalate" else None,
                "calls_per_item": score.ratio(sum(r["spec_calls"] for r in rows), len(rows)),
                "cost_missing": len(rows) - len(costed),
                "usd_per_1000_settled": 1000 * sum(r["spec_cost_usd"] for r in costed) / len(costed) if costed else None,
                "usd_per_1000_nocache": (1000 * sum(r["spec_cost_nocache_usd"] for r in costed) / len(costed)
                                         if costed else None)}
    if pool.task == "triage":
        t = score.triage_block(rows, pool.gold, pool.items)["all"]
        out.update({k: t[k] for k in ("recall_fetch", "recall_fetch_rb", "precision_fetch", "precision_fetch_rb",
                                      "accuracy3", "kappa3")})
    else:
        s = score.screen_block(rows, pool.gold)
        kept = [r for r in rows if score.answer(r) in score.SCREEN_KEPT]
        out.update({k: s.get(k) for k in ("accuracy", "kappa", "relevant_recall", "relevant_precision")})
        out["quoted_share"] = score.ratio(sum(final_quotes(r) > 0 for r in kept), len(kept))
    return out


# ── report ───────────────────────────────────────────────────────────────────────────────────


def markdown(pool: Pool, set_id: str, split: str, results: Sequence[Row]) -> str:
    p, f, u = score._pct, score._f, score._usd
    lines = [f"# stack eval combine {pool.task}: {set_id} / {split}", "",
             (f"Runs ({len(pool.runs)}): {', '.join(pool.runs)}. Rows of the same (model, variant, repeat, item) in "
              f"two runs that disagree: {pool.conflicts}. Usage backfilled from cache entries: {pool.backfill}. "
              f"Gold: {pool.gold_file}."),
             ("Metrics over covered gold items (every needed row present); cost = 1,000 x mean summed cost of the "
              "calls each item takes (settled with the provider cache / at the llm.yaml table price)."), ""]
    if pool.task == "triage":
        head = ["spec", "covered", "escalated", "calls/item", "recall [rel]", "recall [rel+bl]", "prec [rel]",
                "prec [rel+bl]", "3-way acc", "κ", "USD/1k settled", "USD/1k no cache"]
        body = [[r["spec"], f"{r['covered']}/{r['items']}", p(r["escalation_share"]), f(r["calls_per_item"], 2),
                 p(r["recall_fetch"]), p(r["recall_fetch_rb"]), p(r["precision_fetch"]), p(r["precision_fetch_rb"]),
                 p(r["accuracy3"]), f(r["kappa3"]), u(r["usd_per_1000_settled"]), u(r["usd_per_1000_nocache"])]
                for r in results]
    else:
        head = ["spec", "covered", "escalated", "calls/item", "acc", "κ", "rel recall", "rel prec",
                "quoted (rel+bl)", "USD/1k settled", "USD/1k no cache"]
        body = [[r["spec"], f"{r['covered']}/{r['items']}", p(r["escalation_share"]), f(r["calls_per_item"], 2),
                 p(r["accuracy"]), f(r["kappa"]), p(r["relevant_recall"]), p(r["relevant_precision"]),
                 p(r["quoted_share"]), u(r["usd_per_1000_settled"]), u(r["usd_per_1000_nocache"])] for r in results]
    lines += score._table(head, body) + [""]
    missing = [r for r in results if r["cost_missing"]]
    if missing:
        lines += [f"- cost unknown (usage missing) on {r['cost_missing']} covered items of `{r['spec']}`"
                  for r in missing] + [""]
    return "\n".join(lines)


def combine(task: str, set_id: str, split: str, specs: Sequence[str], *, runs: Sequence[str] | None = None,
            out_root: Path = DEFAULT_OUT, price_for: Any = None, write: bool = True,
            gold: str | None = None) -> Row:
    if split not in ("dev", "test"):
        raise CombineError(f"--split {split!r}: dev or test")
    if not specs:
        raise CombineError("--spec: at least one")
    pool = load_pool(out_root, task, set_id, split, runs, price_for=price_for, gold=gold)
    parsed = [parse_spec(pool, s) for s in specs]
    results = [evaluate(pool, s) for s in parsed]
    md = markdown(pool, set_id, split, results)
    path = None
    if write:
        stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
        path = out_root / task / set_id / f"combine-{stamp}.md"
        n = 1
        while path.exists():
            n += 1
            path = out_root / task / set_id / f"combine-{stamp}-{n}.md"
        path.write_text(md + "\n", "utf-8")
    return {"results": results, "markdown": md, "path": str(path) if path else None, "runs": pool.runs,
            "gold": pool.gold_file}


# ── two single-question screen runs as one design (D???(E)) ──────────────────────────────────


def _load_run(d: Path, mode: str, price_for: Any) -> tuple[Row, dict[tuple[str, str, int], Row], Row]:
    """``(run.json, scored rows by (model, item, repeat), backfill counts)`` of a finished screen
    run whose rows all carry ``question_mode`` ``mode``; anything else refuses."""
    if not (d / "run.json").is_file() or not (d / "rows.jsonl").is_file():
        raise CombineError(f"{d}: not a finished run (run.json and rows.jsonl)")
    run = json.loads((d / "run.json").read_text("utf-8"))
    if run["task"] != "screen":
        raise CombineError(f"{d}: task {run['task']!r}, not screen")
    problem = score.items_digest_problem(run, Path(run["set_dir"]))
    if problem is not None:
        raise CombineError(f"{d}: {problem}")
    rows = score.read_jsonl(d / "rows.jsonl")
    modes = sorted({str(r["question_mode"]) for r in rows if r.get("question_mode")})  # failed rows carry none
    if modes != [mode]:
        raise CombineError(f"{d}: rows have question_mode {modes} (prompt {run.get('prompt')!r}); "
                           f"the {mode} side needs {mode!r} rows (screen-{mode}-v1, run since D???(E))")
    items = {i["item_id"]: i for i in score.read_jsonl(_items_file(Path(run["set_dir"])))}
    backfill = score.backfill_usage(run, rows, items, price_for)
    return run, {(r["model"], r["item_id"], int(r["repeat"])): r for r in score.scored(rows)}, backfill


def _inclusive(a: Row, b: Row) -> str:
    """The more inclusive answer of two rows (screen order); no answer only when both have none."""
    answers = [score.answer(r) for r in (a, b) if score.answer(r) != score.NO_ANSWER]
    return min(answers, key=INCLUSIVE["screen"].index) if answers else score.NO_ANSWER


def pair_overall(pairs: Sequence[tuple[Row, Row]], gold: dict[str, Row]) -> Row:
    """The keep decision of a (Stack row, SR row) design (the more inclusive verdict) vs gold
    ``verdict``; cost per 1,000 documents = both calls (settled / no cache); latency p50 over keys
    where both rows made calls: sequential = the sum, parallel = the max."""
    ps = [(a, b) for a, b in pairs if a["item_id"] in gold]
    costed = [(a, b) for a, b in ps if all(r.get(k) is not None for r in (a, b)
                                         for k in ("call_cost_usd", "cost_nocache_usd"))]
    timed = [(a, b) for a, b in ps if all(r["status"] == "ok" and not r.get("cache_hit") for r in (a, b))]
    verdicts = [(_inclusive(a, b), str(gold[a["item_id"]]["label"]["verdict"])) for a, b in ps]
    return {"question": "overall", "applicable": True, "rows": len(ps),
            "answered": sum(v != score.NO_ANSWER for v, _ in verdicts),
            "parse_failures": sum(any(r["status"] != "ok" and r.get("error_class") in score.PARSE_FAILURES
                                      for r in (a, b)) for a, b in ps),
            **score.verdict_metrics(verdicts),
            "cost_missing": len(ps) - len(costed),
            "usd_per_1000_settled": (1000 * sum(float(a["call_cost_usd"]) + float(b["call_cost_usd"])
                                                for a, b in costed) / len(costed)) if costed else None,
            "usd_per_1000_nocache": (1000 * sum(float(a["cost_nocache_usd"]) + float(b["cost_nocache_usd"])
                                                for a, b in costed) / len(costed)) if costed else None,
            "latency_rows": len(timed),
            "latency_ms_p50_sequential": score.quantile([float(a["latency_ms"]) + float(b["latency_ms"])
                                                         for a, b in timed], 0.5),
            "latency_ms_p50_parallel": score.quantile([max(float(a["latency_ms"]), float(b["latency_ms"]))
                                                       for a, b in timed], 0.5)}


def pair_screens(stack_run: Path, sr_run: Path, *, gold: str | None = None, price_for: Any = None,
                 write: bool = True) -> Row:
    """Score a ``screen-stack-v1`` run and a ``screen-sr-v1`` run of the same set and split as one
    two-call design: rows joined by (model, item, repeat); question S from the Stack run's rows,
    R from the SR run's (:func:`stack.evals.score.question_block`), the overall keep decision and
    cost / latency by :func:`pair_overall`; the markdown table is the one ``score`` writes per
    question for a ``screen-v10`` run (:func:`stack.evals.score.question_rows`). Gold: ``gold``,
    else the Stack run's ``run.json`` gold; it must carry the per-question verdicts. Written to
    ``pair-<stamp>.md`` beside the Stack run."""
    price_for = price_for or score._llm_config().price_for
    run_s, rows_s, bf_s = _load_run(stack_run, "stack", price_for)
    run_r, rows_r, bf_r = _load_run(sr_run, "sr", price_for)
    for k in ("set_dir", "split"):
        if run_s[k] != run_r[k]:
            raise CombineError(f"--stack-run {k} {run_s[k]!r} differs from --sr-run {k} {run_r[k]!r}")
    gold_file, gold_rows = score.load_gold(Path(run_s["set_dir"]), gold or run_s.get("gold"))
    if not score.gold_has_questions(gold_rows):
        raise CombineError(f"gold {gold_file}: no label.stack_verdict / label.sr_verdict (gold v2 finalize, D???(E))")
    keys = sorted(set(rows_s) & set(rows_r))
    per: dict[str, Row] = {}
    for m in sorted({k[0] for k in keys}):
        mk = [k for k in keys if k[0] == m]
        per[m] = {"stack": score.question_block([rows_s[k] for k in mk], gold_rows, "stack"),
                  "sr": score.question_block([rows_r[k] for k in mk], gold_rows, "sr"),
                  "overall": pair_overall([(rows_s[k], rows_r[k]) for k in mk], gold_rows), "joined": len(mk)}
    out: Row = {"stack_run": str(stack_run), "sr_run": str(sr_run), "set": run_s["set"], "split": run_s["split"],
                "gold_file": gold_file, "joined": len(keys), "only_stack": len(set(rows_s) - set(rows_r)),
                "only_sr": len(set(rows_r) - set(rows_s)), "backfill": {"stack": bf_s, "sr": bf_r}, "per_model": per}
    lines = [f"# stack eval pair-screens: {run_s['set']} / {run_s['split']}", "",
             (f"Stack run `{stack_run.name}` (prompt {run_s.get('prompt')}), SR run `{sr_run.name}` (prompt "
              f"{run_r.get('prompt')}); gold {gold_file}. Rows joined by (model, item, repeat): {len(keys)}; only in "
              f"the Stack run {out['only_stack']}, only in the SR run {out['only_sr']} (left out)."),
             ("Overall keep = the more inclusive of the two answers (no answer only when both calls failed); cost = "
              "both calls; latency over joined rows where both made calls: sequential = sum, parallel = max."), "",
             score.QUESTION_NOTE, ""]
    lines += score._table(score.QUESTION_HEAD, [row for m, b in per.items()
                                                for row in score.question_rows(f"{m} (stack + sr)", b)]) + [""]
    missing = {m: b["overall"]["cost_missing"] for m, b in per.items() if b["overall"]["cost_missing"]}
    if missing:
        lines += [f"- cost unknown (usage missing) on {n} joined rows of {m}" for m, n in missing.items()] + [""]
    md = "\n".join(lines)
    path = None
    if write:
        stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
        path = stack_run.parent / f"pair-{stamp}.md"
        n = 1
        while path.exists():
            n += 1
            path = stack_run.parent / f"pair-{stamp}-{n}.md"
        path.write_text(md + "\n", "utf-8")
    return {**out, "markdown": md, "path": str(path) if path else None}


__all__ = ["CombineError", "Spec", "combine", "combine_rows", "evaluate", "load_pool", "majority", "pair_overall",
           "pair_screens", "parse_spec"]


def _items_file(set_dir: Path) -> Path:
    """The set's ``items.jsonl``, else the eval-text cache's (:func:`stack.evals.freeze.items_file`, D795)."""
    from stack.evals.freeze import items_file

    return items_file(Path(set_dir))
