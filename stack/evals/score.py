"""Score a finished ``stack eval`` run: ``summary.json`` + ``summary.md`` in the run directory
(``stack eval score --run <dir>`` re-scores, e.g. after gold changes; ``--summary NAME`` writes ``NAME.json`` /
``NAME.md`` instead, e.g. when scoring on another revision of the set with ``--set-dir``).

**Rows scored.** Each row is one (model, item, repeat). ``skipped`` (egress) and
``not_attempted`` (budget, halted) rows are counted but leave every denominator; a failed row
(``failed_*``: schema-invalid, transport, …) stays in and counts as *no answer*: not fetched, not
relevant, wrong. Accuracy-type metrics pool all repeats (each (item, repeat) one observation);
repeat agreement compares repeats per item; McNemar pairs repeat-1 rows of the same item.

**Gold file.** The run's ``run.json`` ``gold`` (the file the harness chose: ``--gold``, else
``gold-v2.jsonl`` when the set has it, else ``gold.jsonl``); a run without the key (before E2) is
scored on ``gold.jsonl``; ``stack eval score --gold <file>`` overrides. A name without a path
separator is a file in the set directory. A named file that does not exist refuses; the default
``gold.jsonl`` missing means no gold.

**Without gold** (no gold file in the set): failures, verdict distribution, cost and latency.

**triage** (gold ``label.verdict``, ``label.fetch``): the model fetches under ``fetch_on =
[relevant]`` (std-v2, primary) and under ``[relevant, borderline]``; recall of gold fetch
(primary), precision of fetch, skip rate, 3-way accuracy and Cohen's κ vs gold (no answer is its
own category), for all items, ``production_asks`` items and the title-only probe separately; the
gold-fetch items each model would skip. McNemar on per-item fetch correctness. When a gold row
has a ``label.scope`` (gold v2), recall of gold fetch is also given per gold scope (``core``,
``sr_only``, ``null`` = carried v1 rows). A **recall-only set** (no gold row with fetch false, e.g.
a set of known Shallow Review items) reports recall (both ``fetch_on``), skip rate, the verdict
distribution and the skipped list; precision and κ are not applicable there (None, marked
``not_applicable``; the markdown says so instead of printing them).

**screen** (gold ``label.verdict`` relevant|borderline|irrelevant, ``label.buckets``): verdict
accuracy and κ, relevant recall/precision, kept (relevant ∪ borderline) recall/precision, bucket
accuracy = mean Jaccard on gold-kept answered items (two empty sets = 1), quote verbatim rate of
the model under test (tier 1: anchored / returned; tier 2's separately), tier-2 share (escalated
/ answered) and what tier 2 changed (tier-1 → final, and correct-vs-gold before and after).
McNemar on per-item verdict correctness. When gold rows carry ``label.scope`` (gold v2, rubric
``screen-v2.md``) the ``scope`` block adds, from the rows' ``scope`` / ``sr_category`` (harness rows
of a prompt in question mode ``scope``): scope accuracy and 3-way κ on answered rows whose answer
and gold both have a scope; kept recall (relevant or borderline) per gold scope ``core`` /
``sr_only``; sr_category accuracy on rows both gold and the model kept with a category; and
export membership precision / recall vs gold — *SR export* = kept and (scope ``sr_only``, or
``core`` with an ``sr_category`` other than ``none``; a null category does not count), *Stack
export* = kept and scope ``core`` — over rows whose gold has a scope (failed rows: not a member).
Rows without scope fields (v7/v8 prompts) make these metrics n/a; the other metrics are unchanged.
**Per question** (D???(E); rows with ``question_mode``, gold with ``label.stack_verdict`` /
``label.sr_verdict``, :func:`question_block`): for question S (Stack) and R (Shallow Review), over
scored rows whose gold has that question's verdict: n, answered (rows with an answer to it),
3-way accuracy and Cohen's κ (a failed row or one without that answer is *no answer*), kept
recall / precision (kept = relevant or borderline), relevant recall / precision, mean quotes
returned per kept answer, verbatim rate (kept / returned quotes of that question), the share of
kept answers with ≥ 1 kept quote, mean confidence on correct and on incorrect answers; for R the
category accuracy on rows both gold and the model keep for R with a category on both sides. A
question the run does not ask (the other side of a single-question prompt) is n/a. The *overall*
line scores the keep decision (the row's ``verdict``) against gold ``verdict`` the same way and
carries cost per 1,000 documents (settled and no cache, as :func:`cache_block`) and latency
p50 over rows that made calls (sequential = parallel for one call).
:func:`stack.evals.combine.pair_screens` writes the same table for two single-question runs.
**Text usability** (rows with ``usability_verdict``, ``screen-v11``; :func:`usability_block`): per
model the final answers' usability verdict counts (``none`` = an answered row without one) and
issue counts over answered rows, with the mean document characters tier 1 showed; no gold.
**Compare** (:func:`compare_runs`, ``stack eval compare A B``, no calls): two screen runs of the
same set, split and items with a model in common, paired per (model, item) on repeat 1; per
question (S, R, overall keep, each 3-way against the gold this module reads: ``label.stack_verdict``,
``label.sr_verdict``, ``label.verdict``) the accuracy of each, the discordant pairs ``b`` (A right,
B wrong) and ``c`` (B right, A wrong), the exact McNemar p, the discordant items with gold and
both answers; mean prompt tokens per row and settled USD per 1,000 documents of each.

**affiliation** (gold ``label.authors``: ``[{name, orgs, roster_ids}]``): authors are matched by
the H-S12 name key (``norm_author_name``), then production's ``align_to_roster`` over the gold
names for the rest. Per author two org sets: *tracked* = roster ids (the model's through
production's ``resolve``/``Canon``, gold's as labelled) and *all* = the tracked ids plus every org
string no roster entry matches (``Canon.orgs_in`` empty), as :func:`stack.evals.gold.norm_org`.
Pair-level (author, org) precision / recall / F1 for both (v3's ``tools/eval_affiliations.py``'s
``score_pairs`` unit), pooled over (item, repeat); a predicted author no gold author matches
contributes false positives. Byline coverage = gold authors with a matched predicted record /
gold authors; org coverage = gold authors with >= 1 predicted org / gold authors. Broadcast: in
one answered row of a paper with >= 2 gold authors, an *all*-set org the model attaches to >= 80 %
of the gold authors while gold attaches it to < 50 % (``MODEL-AB-LUNA-VS-HAIKU.md``); counted per
(row, org). Anchor void rate = surfaces production's anchoring would void / surfaces returned.
McNemar unit: (item, gold author), correct = the *all* set equals gold's. Repeat agreement compares
the per-author *all* sets.

**model_roles** (gold ``label.models[{node_id, roles{10 ROLE_NAMES: 0-4}}]``, one entry per menu
node; all-zero roles = not a real mention; :func:`model_roles_block`): over scored rows whose item
has gold (a failed row and an item production answers without a call count with the empty
prediction), pooled over (item, repeat): (node_id, role) cell micro precision / recall / F1 at
strength ≥ 2 (primary) and ≥ 1; mean per-document Jaccard of the ≥ 2 cells (both sides empty = 1);
node-level P/R/F1 of "graded as a real mention" (any role ≥ 1); mean |strength difference| over
cells nonzero on either side; and over answered rows the repairs, ``quote_unverified`` repairs,
repaired documents and no-call rows.

**model_roles extras** (:func:`extras_block`, beside the cell metrics, which it does not change): over
scored rows whose gold item has ``label.expected_extras`` (``[{name, aliases?}]``; items whose gold says
which off-catalogue models the judge should report, e.g. the synthetic and no-model items of set
2026-10-17a): extras precision / recall / F1, a predicted ``extra_models[].name_as_written`` matching an
expected name or alias loosely (casefolded, letters and digits only; equal, or one containing the other
when both keys have at least 4 characters), each expected name matched at most once; and
``menu_spurious``, the items where a graded menu row (any role ≥ 1) is a node gold does not grade.

**technique_roles** (gold ``label.techniques[{slug, roles{12 ROLE_NAMES: 0-4}, quote}]``,
``label.extra_techniques``; :func:`technique_roles_block`): technique (slug) micro P/R/F1 (a slug is
present when its row exists, on either side), (slug, role) cell micro P/R/F1 at ≥ 2, the same
pooling as model_roles; over answered rows the quote verification rate (verified / all quotes of
``techniques[].quotes`` and ``extra_techniques[].quotes``), rows flagged ``quote_unverified`` and
extra techniques per document.

**technique_staged** (techpipe D607): scored by :mod:`stack.evals.technique_staged` (slug presence without
group nodes and ``candidate:`` rows, gold variants, breakdowns, length weighting, stage-wise candidate recall,
standalone cost, repeats and their union); ``stack eval staged-compare`` and ``staged-table`` compare runs.

**model_mentions** (no run rows: :func:`stack.evals.annotators.run_mentions`; :func:`mentions_block`):
gold ``label.mentions[].node_id`` vs the scan's strong set and strong ∪ weak set, micro P/R/F1 over
(item, node); a lenient variant counts a predicted node as matched when it equals or is an
ancestor or descendant of a gold node (P = matched predicted / predicted, R = matched gold / gold);
per-item FP/FN, gold ``mentions_out_of_vocab`` counted.

**all**: settled cost per item over rows that made calls (cache hits cost $0 and are left out;
``n/a`` when every row was a hit) and per 1,000 items, latency median/p90 over the same rows,
parse/schema failures, other failures by class, repeat agreement (share of items with identical
verdicts across repeats; κ between repeats 1 and 2), and the paired exact McNemar test against
the baseline model (default: the first model of the run).

v3 reuse: the pairwise-agreement / borderline-rate reading of ``tools/eval_screen.py``
(``summarize``) and the P/R/F of v3's ``tools/eval_affiliations.py`` (``prf``) are the model for the
rates here; those scripts are DB-bound (psycopg, sqlite) and are not imported.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from stack.evals.gold import V2_GOLD, affiliation_extract, kappa, norm_org

Row = dict[str, Any]

NO_ANSWER = "no_answer"
FETCH_ON_PRIMARY = ("relevant",)
FETCH_ON_WIDE = ("relevant", "borderline")
SCREEN_KEPT = ("relevant", "borderline")
#: ``truncated``: a reply cut off at the output cap twice (D273, ``failed_permanent[truncated]``).
PARSE_FAILURES = ("schema_invalid", "parse_error", "truncated")


def read_jsonl(path: Path) -> list[Row]:
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


# ── statistics ───────────────────────────────────────────────────────────────────────────────


def ratio(num: float, den: float) -> float | None:
    return num / den if den else None


def quantile(values: Sequence[float], q: float) -> float | None:
    """Linear-interpolated quantile (numpy's default); None for no values."""
    if not values:
        return None
    xs = sorted(values)
    pos = (len(xs) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def mcnemar_exact(b: int, c: int) -> float | None:
    """Two-sided exact McNemar p-value: discordant pairs ``b`` (baseline right, model wrong) and
    ``c`` (model right, baseline wrong) under Binomial(b + c, 1/2). None when there are no
    discordant pairs."""
    n = b + c
    if n == 0:
        return None
    k = min(b, c)
    tail: float = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    sa, sb = set(a), set(b)
    return 1.0 if not sa and not sb else len(sa & sb) / len(sa | sb)


# ── per-row readings ─────────────────────────────────────────────────────────────────────────


def scored(rows: Iterable[Row]) -> list[Row]:
    """Rows in the denominators: not skipped (egress) and not not_attempted (budget, halted)."""
    return [r for r in rows if r["status"] not in ("skipped", "not_attempted")]


def answer(r: Row) -> str:
    return r["verdict"] if r["status"] == "ok" and r.get("verdict") else NO_ANSWER


def fetches(r: Row, fetch_on: Sequence[str]) -> bool:
    return answer(r) in fetch_on


def _triage_correct(r: Row, gold: Row) -> bool:
    return fetches(r, FETCH_ON_PRIMARY) == bool(gold["label"]["fetch"])


def _screen_correct(r: Row, gold: Row) -> bool:
    return bool(answer(r) == gold["label"]["verdict"])


# ── provider prompt cache: per-call usage, per-row fields, backfill ─────────────────────────


#: Per-row usage fields (harness rows since E11a; older rows get them from :func:`backfill_usage`).
USAGE_FIELDS = ("prompt_tokens", "cached_tokens", "cache_write_tokens", "completion_tokens", "reasoning_tokens",
                "call_cost_usd", "cost_nocache_usd", "usage_source", "usage_missing")


def _n(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def entry_usage(entry: Mapping[str, Any], price: Any) -> Row:
    """One answered call's usage from its response-cache entry, i.e. from the provider response
    body as OpenRouter returned it (``body.usage``): ``prompt_tokens``, ``cached_tokens``
    (``prompt_tokens_details.cached_tokens``; Anthropic-native ``cache_read_input_tokens`` as a
    fallback), ``cache_write_tokens`` (``prompt_tokens_details.cache_write_tokens``; native
    ``cache_creation_input_tokens``), ``completion_tokens`` (reasoning included),
    ``reasoning_tokens`` (``completion_tokens_details.reasoning_tokens``), ``cost_usd`` (``usage.cost``,
    else the entry's settled ``cost_usd``) and ``cost_nocache_usd`` = prompt x table in-price +
    completion x table out-price (``price``: the ``llm.yaml`` entry of the answering model)."""
    body = entry.get("body")
    usage = body.get("usage") if isinstance(body, Mapping) else None
    u: Mapping[str, Any] = usage if isinstance(usage, Mapping) else {}
    raw_pd, raw_cd = u.get("prompt_tokens_details"), u.get("completion_tokens_details")
    pd: Mapping[str, Any] = raw_pd if isinstance(raw_pd, Mapping) else {}
    cd: Mapping[str, Any] = raw_cd if isinstance(raw_cd, Mapping) else {}
    # an Anthropic-native body's input_tokens is the uncached remainder only: the prompt is the three fields' sum (D722)
    native = _n(u.get("input_tokens")) + _n(u.get("cache_read_input_tokens")) + _n(u.get("cache_creation_input_tokens"))
    prompt = _n(u.get("prompt_tokens")) or native or _n(entry.get("tokens_in"))
    completion = _n(u.get("completion_tokens")) or _n(u.get("output_tokens")) or _n(entry.get("tokens_out"))
    cost = u.get("cost")
    if not isinstance(cost, (int, float)) or isinstance(cost, bool):
        cost = entry.get("cost_usd") or 0.0
    return {"prompt_tokens": prompt,
            "cached_tokens": _n(pd.get("cached_tokens")) or _n(u.get("cache_read_input_tokens")),
            "cache_write_tokens": _n(pd.get("cache_write_tokens")) or _n(u.get("cache_creation_input_tokens")),
            "completion_tokens": completion, "reasoning_tokens": _n(cd.get("reasoning_tokens")),
            "cost_usd": float(cost), "cost_nocache_usd": price.cost(prompt, completion)}


def row_usage(calls: Sequence[tuple[Mapping[str, Any] | None, bool]], settled_fresh_usd: float,
              price_for: Callable[[str], Any]) -> Row:
    """A row's usage fields from its answered calls ``[(cache entry or None, replayed?)]`` (every
    tier) and ``settled_fresh_usd``, the row's settled cost (every attempt the row made: retries,
    failed tiers; a replayed call costs $0 there).

    ``call_cost_usd`` describes the call, not the replay: the settled cost plus, for each replayed
    call, the original call's cost from its entry. ``cost_nocache_usd`` = table price of every
    answered call + the settled cost of whatever is not an answered call (retries, failed tiers:
    no token counts are kept for those, so they stay at their billed amount). ``usage_source``:
    ``response`` (all calls made now), ``cache_entry`` (all replayed: the original call's
    numbers), ``mixed``, or ``none`` (no answered call). A call whose entry is missing makes the
    two costs None (``usage_missing`` counts the calls); its tokens are left out."""
    out: Row = {k: 0 for k in USAGE_FIELDS[:5]}
    missing = 0
    replayed_cost = fresh_body_cost = nocache = 0.0
    for entry, replayed in calls:
        if entry is None:
            missing += 1
            continue
        u = entry_usage(entry, price_for(str(entry.get("model"))))
        for k in USAGE_FIELDS[:5]:
            out[k] += u[k]
        nocache += u["cost_nocache_usd"]
        if replayed:
            replayed_cost += u["cost_usd"]
        else:
            fresh_body_cost += u["cost_usd"]
    overhead = max(0.0, settled_fresh_usd - fresh_body_cost)  # retries and failed tiers, at the billed amount
    kinds = {replayed for _, replayed in calls}
    out.update({
        "call_cost_usd": None if missing else settled_fresh_usd + replayed_cost,
        "cost_nocache_usd": None if missing else nocache + overhead,
        "usage_source": "none" if not calls else ("mixed" if len(kinds) > 1 else
                                                  ("cache_entry" if True in kinds else "response")),
        "usage_missing": missing,
    })
    return out


def _cache_index(directory: Path) -> list[Row]:
    from stack.llm.cache import ResponseCache

    cache = ResponseCache.for_store(directory)
    if not cache.dir.is_dir():
        return []
    return [e for p in sorted(cache.dir.glob("*.json.zst")) if (e := cache.get(p.name.split(".")[0])) is not None]


def _item_bases(item: Row) -> set[str]:
    """The digests a call about ``item`` was cached under (the client's ``basis_digest``: triage
    the fields digest = ``input_digest``; screen the text digest; affiliation the cache input
    digest of the user message)."""
    from stack.core import hashes

    inp = item.get("input") or {}
    out = {item.get("input_digest"), inp.get("text_digest"), inp.get("full_text_digest")}
    for k in ("user", "record"):
        if isinstance(inp.get(k), str):
            out.add(hashes.input_digest(inp[k]))
    return {x for x in out if x}


def backfill_usage(run: Row, rows: list[Row], items: Mapping[str, Row], price_for: Callable[[str], Any]) -> Row:
    """Usage fields for rows written before the harness recorded them, from the response-cache
    entries under ``run['cache_root']`` (read only; ``rows.jsonl`` is not rewritten). Per row the
    entries in ``<cache_root>/<model slug>/r<repeat>`` of this item (basis digest), whose template
    digest is one the run pinned (triage ``template_digest``, screen ``template_digests``;
    affiliation pins none and has one template) and whose model is the row's answer model (tier 1)
    or, when tier 2 answered, the tier-2 model. An entry another run wrote is a replay here when
    the row is a cache hit or when the call was not made by this run (``entry.run_id``). More than
    one candidate is counted ambiguous and treated as missing. Returns counts."""
    from stack.evals.harness import model_slug

    root = Path(run.get("cache_root") or "")
    pins = run.get("pins") or {}
    indexes: dict[tuple[str, int], list[Row]] = {}
    counts = Counter[str]()
    for r in rows:
        if all(k in r for k in USAGE_FIELDS):
            counts["recorded"] += 1
            continue
        if r["status"] in ("skipped", "not_attempted"):
            continue
        if r["status"] != "ok":  # no answered call: the settled cost is all there is
            r.update(row_usage([], float(r.get("cost_usd") or 0.0), price_for))
            counts["backfilled"] += 1
            continue
        key = (r["model"], int(r["repeat"]))
        if key not in indexes:
            indexes[key] = _cache_index(root / model_slug(r["model"]) / f"r{key[1]}")
        pin = pins.get(r["model"]) or {}
        allowed = set(pin.get("template_digests") or ([pin["template_digest"]] if pin.get("template_digest") else []))
        bases = _item_bases(items.get(r["item_id"]) or {})
        wanted = [r.get("answer_model") or r["model"]]
        if r.get("tier2_attempted") and not r.get("tier2_error_class") and r.get("tier2_model"):
            wanted.append(r["tier2_model"])
        calls: list[tuple[Row | None, bool]] = []
        for m in wanted:
            cand = [e for e in indexes[key] if e.get("model") == m and e.get("basis_digest") in bases
                    and (not allowed or e.get("template_digest") in allowed)]
            if len(cand) != 1:
                counts["ambiguous" if cand else "missing"] += 1
                calls.append((None, bool(r.get("cache_hit"))))
                continue
            e = cand[0]
            calls.append((e, bool(r.get("cache_hit")) or e.get("run_id") != run.get("run_id")))
        r.update(row_usage(calls, float(r.get("cost_usd") or 0.0), price_for))
        counts["backfilled"] += 1
    return dict(counts)


def cache_block(rows: list[Row]) -> Row:
    """Provider prompt-cache share and cost per 1,000 documents with and without it, over the
    scored rows (each row one (item, repeat) call; a replayed row carries the original call's
    numbers): cached share = Σ cached / Σ prompt tokens; ``usd_per_1000_settled`` = 1,000 x mean
    ``call_cost_usd`` (the provider-cache-discounted price OpenRouter settled);
    ``usd_per_1000_nocache`` = 1,000 x mean ``cost_nocache_usd`` (tokens at the ``llm.yaml``
    table price). Rows whose usage is missing are left out of both and counted."""
    sc = [r for r in scored(rows) if "call_cost_usd" in r]
    costed = [r for r in sc if r.get("call_cost_usd") is not None and r.get("cost_nocache_usd") is not None]
    prompt = sum(int(r.get("prompt_tokens") or 0) for r in sc)
    calls = sum(r.get("usage_source") not in (None, "none") for r in sc)
    return {
        "rows": len(sc), "rows_costed": len(costed),
        "rows_usage_missing": sum(int(r.get("usage_missing") or 0) > 0 for r in sc),
        "rows_replayed": sum(r.get("usage_source") in ("cache_entry", "mixed") for r in sc),
        "prompt_tokens": prompt, "cached_tokens": sum(int(r.get("cached_tokens") or 0) for r in sc),
        "cache_write_tokens": sum(int(r.get("cache_write_tokens") or 0) for r in sc),
        "completion_tokens": sum(int(r.get("completion_tokens") or 0) for r in sc),
        "reasoning_tokens": sum(int(r.get("reasoning_tokens") or 0) for r in sc),
        "cached_share": ratio(sum(int(r.get("cached_tokens") or 0) for r in sc), prompt),
        "prompt_tokens_per_row": ratio(prompt, calls),
        "reasoning_tokens_per_row": ratio(sum(int(r.get("reasoning_tokens") or 0) for r in sc), calls),
        "usd_per_1000_settled": (ratio(sum(float(r["call_cost_usd"]) for r in costed), len(costed)) or 0.0) * 1000
        if costed else None,
        "usd_per_1000_nocache": (ratio(sum(float(r["cost_nocache_usd"]) for r in costed), len(costed)) or 0.0) * 1000
        if costed else None,
    }


# ── blocks ───────────────────────────────────────────────────────────────────────────────────


def general_block(rows: list[Row], key: Callable[[Row], Any] = answer, categorical: bool = True) -> Row:
    """Failures, distribution, cost, latency, repeat agreement: no gold needed. ``key`` is a row's
    answer for repeat agreement; ``categorical`` False (affiliation: the answer is a structure)
    leaves out the verdict distribution and the between-repeat κ."""
    sc = scored(rows)
    status = Counter(f"{r['status']}[{r['error_class']}]" if r.get("error_class") else r["status"] for r in rows)
    failed = [r for r in sc if r["status"] != "ok"]
    calls = [r for r in sc if not r.get("cache_hit")]
    lat = [float(r["latency_ms"]) for r in calls if r["status"] == "ok" and not r.get("no_call")]
    cost_calls = sum(float(r.get("cost_usd") or 0.0) for r in calls)
    per_item = ratio(cost_calls, len(calls))
    by_item: dict[str, dict[int, str]] = defaultdict(dict)
    for r in sc:
        if r["status"] == "ok":
            by_item[r["item_id"]][int(r["repeat"])] = key(r)
    multi = [v for v in by_item.values() if len(v) >= 2]
    pairs12 = [(v[1], v[2]) for v in by_item.values() if 1 in v and 2 in v]
    return {
        "rows": len(rows), "scored": len(sc), "answered": sum(r["status"] == "ok" for r in sc),
        "status": dict(status),
        "egress_skipped": sum(r["status"] == "skipped" for r in rows),
        "not_attempted": sum(r["status"] == "not_attempted" for r in rows),
        "parse_schema_failures": sum(r.get("error_class") in PARSE_FAILURES for r in failed),
        "other_failures": dict(Counter(r.get("error_class") or "?" for r in failed
                                       if r.get("error_class") not in PARSE_FAILURES)),
        "schema_degraded": sum(bool(r.get("schema_degraded")) for r in sc),
        "verdicts": dict(Counter(answer(r) for r in sc)) if categorical else None,
        "cache_hits": sum(bool(r.get("cache_hit")) for r in sc),
        "settled_usd": sum(float(r.get("cost_usd") or 0.0) for r in rows),
        "cost_per_item_usd": per_item,
        "cost_per_1000_usd": per_item * 1000 if per_item is not None else None,
        "latency_ms_median": quantile(lat, 0.5), "latency_ms_p90": quantile(lat, 0.9),
        "repeat_items": len(multi),
        "repeat_agreement": ratio(sum(len(set(v.values())) == 1 for v in multi), len(multi)),
        "repeat_kappa_1_2": kappa(pairs12) if pairs12 and categorical else None,
    }


#: Gold scope values the per-scope breakdowns report (None: rows carried from v1 gold).
SCOPES = ("core", "sr_only", None)


def gold_has_scope(gold: Mapping[str, Row]) -> bool:
    return any(g["label"].get("scope") is not None for g in gold.values())


def recall_only(gold: Mapping[str, Row]) -> bool:
    """A recall-only triage set: gold with no fetch-false row (precision and κ undefined)."""
    return bool(gold) and all(bool(g["label"]["fetch"]) for g in gold.values())


def triage_block(rows: list[Row], gold: dict[str, Row], items: dict[str, Row]) -> Row:
    sc = [r for r in scored(rows) if r["item_id"] in gold]
    only_recall = recall_only(gold)

    def subset(rs: list[Row]) -> Row:
        out: Row = {"n": len(rs), "gold_fetch": sum(bool(gold[r["item_id"]]["label"]["fetch"]) for r in rs)}
        for tag, fetch_on in (("", FETCH_ON_PRIMARY), ("_rb", FETCH_ON_WIDE)):
            g = [bool(gold[r["item_id"]]["label"]["fetch"]) for r in rs]
            m = [fetches(r, fetch_on) for r in rs]
            tp = sum(a and b for a, b in zip(g, m, strict=True))
            out["recall_fetch" + tag] = ratio(tp, sum(g))
            out["precision_fetch" + tag] = ratio(tp, sum(m))
            out["skip_rate" + tag] = ratio(len(rs) - sum(m), len(rs))
        pairs = [(answer(r), gold[r["item_id"]]["label"]["verdict"]) for r in rs]
        out["accuracy3"] = ratio(sum(a == b for a, b in pairs), len(pairs))
        out["kappa3"] = kappa(pairs) if pairs else None
        if only_recall:
            out.update(precision_fetch=None, precision_fetch_rb=None, kappa3=None,
                       not_applicable=["precision_fetch", "precision_fetch_rb", "kappa3"])
        return out

    skipped = sorted({r["item_id"] for r in sc
                      if gold[r["item_id"]]["label"]["fetch"] and not fetches(r, FETCH_ON_PRIMARY)})
    skip_counts = Counter(r["item_id"] for r in sc
                          if gold[r["item_id"]]["label"]["fetch"] and not fetches(r, FETCH_ON_PRIMARY))
    reps = Counter(r["item_id"] for r in sc)
    extra: Row = {"recall_only": only_recall}
    if only_recall:
        extra["verdicts"] = dict(sorted(Counter(answer(r) for r in sc).items()))
    if gold_has_scope(gold):
        by: Row = {}
        for sv in SCOPES:
            rs = [r for r in sc if gold[r["item_id"]]["label"].get("scope") == sv and gold[r["item_id"]]["label"]["fetch"]]
            by["null" if sv is None else sv] = {
                "gold_fetch": len(rs), "recall_fetch": ratio(sum(fetches(r, FETCH_ON_PRIMARY) for r in rs), len(rs)),
                "recall_fetch_rb": ratio(sum(fetches(r, FETCH_ON_WIDE) for r in rs), len(rs))}
        extra["recall_by_scope"] = by
    return {
        **extra,
        "all": subset(sc),
        "production_asks": subset([r for r in sc if items.get(r["item_id"], {}).get("production_asks")]),
        "title_only": subset([r for r in sc if items.get(r["item_id"], {}).get("production_asks") is False]),
        "skipped_gold_fetch": [{"item_id": i, "title": (items.get(i, {}).get("input") or {}).get("title"),
                                "repeats_skipped": skip_counts[i], "repeats": reps[i]} for i in skipped],
    }


def screen_block(rows: list[Row], gold: dict[str, Row]) -> Row:
    sc_all = scored(rows)
    ok = [r for r in sc_all if r["status"] == "ok"]
    returned1 = sum(int(r.get("quotes_kept") or 0) + int(r.get("quotes_voided") or 0) for r in ok)
    returned2 = sum(int(r.get("tier2_quotes_kept") or 0) + int(r.get("tier2_quotes_voided") or 0) for r in ok)
    escalated = [r for r in ok if r.get("tier2_attempted")]
    out: Row = {
        "quote_verbatim_rate": ratio(sum(int(r.get("quotes_kept") or 0) for r in ok), returned1),
        "quotes_returned": returned1,
        "quotes_voided": sum(int(r.get("quotes_voided") or 0) for r in ok),
        "tier2_quote_verbatim_rate": ratio(sum(int(r.get("tier2_quotes_kept") or 0) for r in ok), returned2),
        "tier2_share": ratio(len(escalated), len(ok)),
        "tier2_escalated": len(escalated),
        "tier2_failed": sum(bool(r.get("tier2_error_class")) for r in escalated),
        "tier2_changes": dict(Counter(f"{r['tier1_verdict']}->{r['verdict']}" for r in escalated)),
    }
    if usability := usability_block(ok):
        out["usability"] = usability
    if not gold:
        return out
    sc = [r for r in sc_all if r["item_id"] in gold]
    pairs = [(answer(r), gold[r["item_id"]]["label"]["verdict"]) for r in sc]

    def pr(positive: Sequence[str]) -> tuple[float | None, float | None]:
        g = [b in positive for _, b in pairs]
        m = [a in positive for a, _ in pairs]
        tp = sum(x and y for x, y in zip(g, m, strict=True))
        return ratio(tp, sum(g)), ratio(tp, sum(m))

    rr, rp = pr(("relevant",))
    kr, kp = pr(SCREEN_KEPT)
    kept_answered = [r for r in sc if r["status"] == "ok" and gold[r["item_id"]]["label"]["verdict"] in SCREEN_KEPT]
    esc_g = [r for r in escalated if r["item_id"] in gold]
    out.update({
        "n": len(sc), "accuracy": ratio(sum(a == b for a, b in pairs), len(pairs)),
        "kappa": kappa(pairs) if pairs else None,
        "relevant_recall": rr, "relevant_precision": rp, "kept_recall": kr, "kept_precision": kp,
        "bucket_jaccard": ratio(sum(jaccard(r.get("buckets") or [], gold[r["item_id"]]["label"].get("buckets") or [])
                                    for r in kept_answered), len(kept_answered)),
        "tier2_tier1_correct": sum(r["tier1_verdict"] == gold[r["item_id"]]["label"]["verdict"] for r in esc_g),
        "tier2_final_correct": sum(r["verdict"] == gold[r["item_id"]]["label"]["verdict"] for r in esc_g),
        "tier2_confusion": dict(Counter(f"{r['tier1_verdict']}->{r['verdict']} (gold {gold[r['item_id']]['label']['verdict']})"
                                        for r in esc_g)),
    })
    if gold_has_scope(gold):
        out["scope"] = scope_block(sc, gold)
    if gold_has_questions(gold) and any(r.get("question_mode") for r in sc):
        out["questions"] = questions_block(sc, gold)
        out["questions_mode"] = "/".join(sorted({str(r["question_mode"]) for r in sc if r.get("question_mode")}))
    return out


QUESTIONS = ("stack", "sr")
QUESTION_LABEL = {"stack": "S (Stack)", "sr": "R (Shallow Review)", "overall": "overall keep"}
USABILITY_VERDICTS = ("usable", "degraded", "unusable")


def usability_block(ok_rows: Sequence[Row]) -> Row | None:
    """Usability verdict and issue counts over answered rows (module docstring); None when no row
    carries a usability answer (a prompt without question U)."""
    if not any(r.get("usability_verdict") for r in ok_rows):
        return None
    verdicts = Counter(str(r.get("usability_verdict") or "none") for r in ok_rows)
    issues = Counter(str(i) for r in ok_rows for i in r.get("usability_issues") or [])
    sent = [float(r["chars_sent_tier1"]) for r in ok_rows if isinstance(r.get("chars_sent_tier1"), (int, float))]
    return {"answered": len(ok_rows), "verdicts": {k: verdicts.get(k, 0) for k in (*USABILITY_VERDICTS, "none")},
            "issues": dict(sorted(issues.items(), key=lambda kv: (-kv[1], kv[0]))),
            "notes": sum(bool(r.get("usability_note")) for r in ok_rows),
            "chars_sent_tier1_mean": _mean(sent)}


def gold_has_questions(gold: Mapping[str, Row]) -> bool:
    return any(g["label"].get(f"{q}_verdict") is not None for g in gold.values() for q in QUESTIONS)


def verdict_metrics(pairs: Sequence[tuple[str, str]]) -> Row:
    """3-way accuracy, κ, kept and relevant recall / precision of ``(model, gold)`` verdict pairs
    (the model's may be :data:`NO_ANSWER`)."""

    def pr(positive: Sequence[str]) -> tuple[float | None, float | None]:
        g = [b in positive for _, b in pairs]
        m = [a in positive for a, _ in pairs]
        tp = sum(x and y for x, y in zip(g, m, strict=True))
        return ratio(tp, sum(g)), ratio(tp, sum(m))

    kr, kp = pr(SCREEN_KEPT)
    rr, rp = pr(("relevant",))
    return {"n": len(pairs), "accuracy": ratio(sum(a == b for a, b in pairs), len(pairs)),
            "kappa": kappa(list(pairs)) if pairs else None,
            "kept_recall": kr, "kept_precision": kp, "relevant_recall": rr, "relevant_precision": rp}


def _mean(xs: Sequence[float]) -> float | None:
    return ratio(sum(xs), len(xs))


def question_block(rows: Sequence[Row], gold: Mapping[str, Row], q: str) -> Row:
    """Question ``q``'s metrics (module docstring, *per question*) over scored rows ``rows``."""
    key = f"{q}_verdict"
    sc = [r for r in scored(rows) if r["item_id"] in gold and gold[r["item_id"]]["label"].get(key) is not None]

    def ans(r: Row) -> str:
        v = r.get(key)
        return str(v) if r["status"] == "ok" and v else NO_ANSWER

    answered = [r for r in sc if ans(r) != NO_ANSWER]
    out: Row = {"question": q, "applicable": bool(answered), "rows": len(sc), "answered": len(answered),
                "parse_failures": sum(r["status"] != "ok" and r.get("error_class") in PARSE_FAILURES for r in sc)}
    if not answered:  # the run does not ask this question
        return {**out, **dict.fromkeys(("n", "accuracy", "kappa", "kept_recall", "kept_precision", "relevant_recall",
                                        "relevant_precision", "quotes_per_kept", "verbatim_rate", "kept_quoted_share",
                                        "confidence_correct", "confidence_incorrect", "category_n",
                                        "category_accuracy"))}
    gl = {r["item_id"]: gold[r["item_id"]]["label"] for r in sc}
    out.update(verdict_metrics([(ans(r), str(gl[r["item_id"]][key])) for r in sc]))
    kept = [r for r in answered if ans(r) in SCREEN_KEPT]
    quoted = [r for r in answered if r.get(f"{q}_quotes_returned") is not None]
    kept_q = [r for r in kept if r.get(f"{q}_quotes_returned") is not None]
    conf = [(float(r[f"{q}_confidence"]), ans(r) == gl[r["item_id"]][key]) for r in answered
            if isinstance(r.get(f"{q}_confidence"), (int, float))]
    out.update(
        quotes_per_kept=_mean([float(r[f"{q}_quotes_returned"]) for r in kept_q]),
        verbatim_rate=ratio(sum(int(r[f"{q}_quotes_kept"] or 0) for r in quoted),
                            sum(int(r[f"{q}_quotes_returned"] or 0) for r in quoted)),
        kept_quoted_share=ratio(sum(int(r[f"{q}_quotes_kept"] or 0) > 0 for r in kept_q), len(kept_q)),
        confidence_correct=_mean([c for c, ok_ in conf if ok_]),
        confidence_incorrect=_mean([c for c, ok_ in conf if not ok_]),
        category_n=None, category_accuracy=None)
    if q == "sr":
        cat = [(r.get("sr_category"), gl[r["item_id"]].get("sr_category")) for r in kept
               if gl[r["item_id"]][key] in SCREEN_KEPT and r.get("sr_category") is not None
               and gl[r["item_id"]].get("sr_category") is not None]
        out.update(category_n=len(cat), category_accuracy=ratio(sum(a == b for a, b in cat), len(cat)))
    return out


def overall_block(rows: Sequence[Row], gold: Mapping[str, Row]) -> Row:
    """The keep decision (row ``verdict``) vs gold ``verdict``, with cost per 1,000 documents and
    latency (module docstring, *per question*); one call per row: sequential = parallel."""
    sc = [r for r in scored(rows) if r["item_id"] in gold]
    calls = [r for r in sc if not r.get("cache_hit") and r["status"] == "ok"]
    c = cache_block(list(sc))
    lat = quantile([float(r["latency_ms"]) for r in calls], 0.5)
    return {"question": "overall", "applicable": True, "rows": len(sc),
            "answered": sum(r["status"] == "ok" for r in sc),
            "parse_failures": sum(r["status"] != "ok" and r.get("error_class") in PARSE_FAILURES for r in sc),
            **verdict_metrics([(answer(r), str(gold[r["item_id"]]["label"]["verdict"])) for r in sc]),
            "usd_per_1000_settled": c["usd_per_1000_settled"], "usd_per_1000_nocache": c["usd_per_1000_nocache"],
            "latency_rows": len(calls), "latency_ms_p50_sequential": lat, "latency_ms_p50_parallel": lat}


def questions_block(rows: Sequence[Row], gold: Mapping[str, Row]) -> Row:
    return {"stack": question_block(rows, gold, "stack"), "sr": question_block(rows, gold, "sr"),
            "overall": overall_block(rows, gold)}


QUESTION_HEAD = ["model / design", "question", "n", "answered", "parse fail", "acc", "κ", "kept recall", "kept prec",
                 "rel recall", "rel prec", "quotes / kept ans", "verbatim", "kept ans quoted", "conf right / wrong",
                 "R category acc", "USD/1k settled / no cache", "latency p50 seq / par"]


def question_rows(label: str, qb: Mapping[str, Row]) -> list[list[str]]:
    """The per-question table rows of one model or design (:data:`QUESTION_HEAD`)."""
    out = []
    for q in (*QUESTIONS, "overall"):
        b = qb[q]
        if not b["applicable"]:
            out.append([label, QUESTION_LABEL[q], str(b["rows"]), "0", str(b["parse_failures"]),
                        *(["n/a"] * (len(QUESTION_HEAD) - 5))])
            continue
        cat = (f"{_pct(b['category_accuracy'])} (n {b['category_n']})" if q == "sr" else "—")
        cost = (f"{_usd(b['usd_per_1000_settled'])} / {_usd(b['usd_per_1000_nocache'])}" if q == "overall" else "—")
        lat = (f"{_f(b['latency_ms_p50_sequential'], 0)} / {_f(b['latency_ms_p50_parallel'], 0)}"
               if q == "overall" else "—")
        per_q = q != "overall"
        out.append([label, QUESTION_LABEL[q], str(b["n"]), str(b["answered"]), str(b["parse_failures"]),
                    _pct(b["accuracy"]), _f(b["kappa"]), _pct(b["kept_recall"]), _pct(b["kept_precision"]),
                    _pct(b["relevant_recall"]), _pct(b["relevant_precision"]),
                    _f(b["quotes_per_kept"], 2) if per_q else "—", _pct(b["verbatim_rate"]) if per_q else "—",
                    _pct(b["kept_quoted_share"]) if per_q else "—",
                    f"{_f(b['confidence_correct'], 2)} / {_f(b['confidence_incorrect'], 2)}" if per_q else "—",
                    cat, cost, lat])
    return out


QUESTION_NOTE = ("S = Stack relevance, R = Shallow Review relevance (gold: `label.stack_verdict` / "
                 "`label.sr_verdict`); overall = the keep decision vs gold `verdict`. A failed row or one without "
                 "that answer is no answer. Quotes per question: returned per kept answer, verbatim = anchored / "
                 "returned. USD per 1,000 documents settled (provider cache) / table price without cache; latency "
                 "over rows that made calls.")


def in_sr(kept: bool, scope: Any, category: Any) -> bool:
    """SR-export membership: kept and scope ``sr_only``, or ``core`` with a category other than ``none``."""
    return kept and (scope == "sr_only" or (scope == "core" and category not in (None, "none")))


def in_stack(kept: bool, scope: Any) -> bool:
    """Stack-export membership: kept and scope ``core``."""
    return kept and scope == "core"


def _pr(pairs: Sequence[tuple[bool, bool]]) -> Row:
    """Precision / recall of the model's membership (second) against gold's (first)."""
    tp = sum(g and m for g, m in pairs)
    return {"gold": sum(g for g, _ in pairs), "model": sum(m for _, m in pairs), "tp": tp,
            "precision": ratio(tp, sum(m for _, m in pairs)), "recall": ratio(tp, sum(g for g, _ in pairs))}


def scope_block(sc: list[Row], gold: dict[str, Row]) -> Row:
    """The screen's SR-scope metrics (module docstring) over scored rows ``sc`` with gold."""
    lab = {r["item_id"]: gold[r["item_id"]]["label"] for r in sc}
    with_gold_scope = [r for r in sc if lab[r["item_id"]].get("scope") is not None]
    model_has = any(r["status"] == "ok" and r.get("scope") is not None for r in sc)
    out: Row = {"model_has_scope": model_has, "gold_scope_rows": len(with_gold_scope)}
    kept_by: Row = {}
    for sv in ("core", "sr_only"):
        rs = [r for r in with_gold_scope if lab[r["item_id"]]["scope"] == sv]
        kept_by[sv] = {"n": len(rs), "kept_recall": ratio(sum(answer(r) in SCREEN_KEPT for r in rs), len(rs))}
    out["kept_recall_by_scope"] = kept_by
    if not model_has:
        out.update(scope_n=0, scope_accuracy=None, scope_kappa=None, sr_category_n=0, sr_category_accuracy=None,
                   sr_export=None, stack_export=None)
        return out
    pairs = [(r["scope"], lab[r["item_id"]]["scope"]) for r in with_gold_scope
             if r["status"] == "ok" and r.get("scope") is not None]
    cat = [(r["sr_category"], lab[r["item_id"]]["sr_category"]) for r in with_gold_scope
           if answer(r) in SCREEN_KEPT and lab[r["item_id"]]["verdict"] in SCREEN_KEPT
           and r.get("sr_category") is not None and lab[r["item_id"]].get("sr_category") is not None]

    def member(r: Row, fn: Callable[..., bool], *keys: str) -> tuple[bool, bool]:
        g = lab[r["item_id"]]
        ok_ = r["status"] == "ok"
        return (fn(g["verdict"] in SCREEN_KEPT, *(g.get(k) for k in keys)),
                ok_ and fn(answer(r) in SCREEN_KEPT, *(r.get(k) for k in keys)))

    out.update(
        scope_n=len(pairs), scope_accuracy=ratio(sum(a == b for a, b in pairs), len(pairs)),
        scope_kappa=kappa(pairs) if pairs else None,
        scope_confusion={f"{a}|{b}": n for (a, b), n in sorted(Counter(pairs).items())},
        sr_category_n=len(cat), sr_category_accuracy=ratio(sum(a == b for a, b in cat), len(cat)),
        sr_export=_pr([member(r, in_sr, "scope", "sr_category") for r in with_gold_scope]),
        stack_export=_pr([member(r, in_stack, "scope") for r in with_gold_scope]),
    )
    return out


BROADCAST_PRED = 0.8
BROADCAST_GOLD = 0.5


def _all_set(roster_ids: Iterable[str], untracked: Iterable[str]) -> frozenset[str]:
    return frozenset({f"id:{x}" for x in roster_ids} | {f"str:{n}" for n in map(norm_org, untracked) if n})


def pred_sets(author: Row) -> tuple[frozenset[str], frozenset[str]]:
    """A predicted author's (tracked, all) sets: roster ids from production's ``resolve``
    (``org_slugs``), plus the strings of affiliations that resolved to no id."""
    affs = author.get("affiliations") or []
    ids = {x for a in affs for x in a.get("org_slugs") or []}
    return frozenset(ids), _all_set(ids, (a["surface"] for a in affs if not a.get("org_slugs")))


def gold_sets(author: Row, canon: Any) -> tuple[frozenset[str], frozenset[str]]:
    """A gold author's (tracked, all) sets: the labelled roster ids, plus the labelled org strings
    that ``Canon.orgs_in`` maps to no roster entry."""
    ids = set(author.get("roster_ids") or [])
    return frozenset(ids), _all_set(ids, (o for o in author.get("orgs") or [] if not canon.orgs_in(o)))


def match_authors(pred: Sequence[Row], gold_names: Sequence[str]) -> dict[str, int]:
    """gold name key -> index into ``pred``: equal ``norm_author_name`` first, then production's
    ``align_to_roster`` (its looser unique-match tiers) for the rest."""
    ex = affiliation_extract()
    keys = [ex.norm_author_name(n) for n in gold_names]
    out: dict[str, int] = {}
    used: set[int] = set()
    for k in keys:
        hit = [i for i, p in enumerate(pred) if i not in used and ex.norm_author_name(p["name"]) == k]
        if len(hit) == 1 and k not in out:
            out[k] = hit[0]
            used.add(hit[0])
    rest_g = [n for n, k in zip(gold_names, keys, strict=True) if k not in out]
    rest_p = [i for i in range(len(pred)) if i not in used]
    if rest_g and rest_p:
        # the predicted index rides in ``email``, which align_to_roster copies onto the record it maps
        stubs = [ex.RawAuthor(pred[i]["name"], email=str(i)) for i in rest_p]
        aligned, _ = ex.align_to_roster(stubs, rest_g)
        for name, a in zip(rest_g, aligned, strict=True):
            if a.email:
                out[ex.norm_author_name(name)] = int(a.email)
    return out


def affiliation_units(r: Row, gold: Row, canon: Any) -> list[Row]:
    """Per gold author of a scored row: name key, matched?, gold and predicted (tracked, all) sets;
    plus one unit per predicted author no gold author matched (``gold_author`` None)."""
    pred = (r.get("authors") or []) if r["status"] == "ok" else []
    g_authors = gold["label"].get("authors") or []
    ex = affiliation_extract()
    m = match_authors(pred, [a["name"] for a in g_authors])
    units = []
    for a in g_authors:
        k = ex.norm_author_name(a["name"])
        gt, ga = gold_sets(a, canon)
        i = m.get(k)
        pt, pa = pred_sets(pred[i]) if i is not None else (frozenset(), frozenset())
        units.append({"author": k, "gold_author": True, "matched": i is not None,
                      "gold_tracked": gt, "gold_all": ga, "pred_tracked": pt, "pred_all": pa})
    for i in sorted(set(range(len(pred))) - set(m.values())):
        pt, pa = pred_sets(pred[i])
        units.append({"author": ex.norm_author_name(pred[i]["name"]), "gold_author": False, "matched": False,
                      "gold_tracked": frozenset(), "gold_all": frozenset(), "pred_tracked": pt, "pred_all": pa})
    return units


def _prf(tp: int, fp: int, fn: int) -> Row:
    p, r = ratio(tp, tp + fp), ratio(tp, tp + fn)
    f1 = None if p is None or r is None else (2 * p * r / (p + r) if p + r else 0.0)
    return {"tp": tp, "fp": fp, "fn": fn, "precision": p, "recall": r, "f1": f1}


def broadcasts(units: Sequence[Row]) -> list[Row]:
    """Orgs of one row attached to >= :data:`BROADCAST_PRED` of the gold authors by the model and to
    < :data:`BROADCAST_GOLD` by gold (papers with >= 2 gold authors)."""
    ga = [u for u in units if u["gold_author"]]
    n = len(ga)
    if n < 2:
        return []
    out = []
    for org in sorted({o for u in ga for o in u["pred_all"]}):
        ps = sum(org in u["pred_all"] for u in ga) / n
        gs = sum(org in u["gold_all"] for u in ga) / n
        if ps >= BROADCAST_PRED and gs < BROADCAST_GOLD:
            out.append({"org": org, "pred_share": ps, "gold_share": gs})
    return out


def affiliation_block(rows: list[Row], gold: dict[str, Row], canon: Any) -> Row:
    sc_all = scored(rows)
    ok = [r for r in sc_all if r["status"] == "ok"]
    quoted = sum(int(r.get("anchor_quoted") or 0) for r in ok)
    out: Row = {
        "anchor_void_rate": ratio(sum(int(r.get("anchor_voided") or 0) for r in ok), quoted),
        "anchor_quoted": quoted,
        "roster_mismatch_rows": sum(bool(r.get("roster_mismatch")) for r in ok),
        "production_uses_llm_rows": sum(bool(r.get("production_uses_llm")) for r in sc_all),
    }
    if not gold:
        return out
    sc = [r for r in sc_all if r["item_id"] in gold]
    tot = {"tracked": [0, 0, 0], "all": [0, 0, 0]}
    g_n = matched = covered = 0
    bc: list[Row] = []
    for r in sc:
        units = affiliation_units(r, gold[r["item_id"]], canon)
        for u in units:
            for kind in ("tracked", "all"):
                g, p = u[f"gold_{kind}"], u[f"pred_{kind}"]
                tot[kind][0] += len(g & p)
                tot[kind][1] += len(p - g)
                tot[kind][2] += len(g - p)
            if u["gold_author"]:
                g_n += 1
                matched += u["matched"]
                covered += bool(u["pred_all"])
        bc += [{"item_id": r["item_id"], "repeat": r["repeat"], **b} for b in broadcasts(units)]
    out.update({
        "n": len(sc), "gold_authors": g_n,
        "tracked": _prf(*tot["tracked"]), "all": _prf(*tot["all"]),
        "byline_coverage": ratio(matched, g_n), "org_coverage": ratio(covered, g_n),
        "broadcast": len(bc), "broadcasts": bc,
    })
    return out


def affiliation_answer(r: Row) -> Any:
    """A row's answer for repeat agreement: per predicted author (name key) the *all* set."""
    if r["status"] != "ok":
        return NO_ANSWER
    return tuple(sorted((a.get("author") or "", tuple(sorted(pred_sets(a)[1]))) for a in r.get("authors") or []))


def mcnemar_block(task: str, rows: list[Row], gold: dict[str, Row], model: str, baseline: str,
                  canon: Any = None) -> Row | None:
    if not gold or model == baseline or task in ANNOTATOR_TASKS:  # no per-item correct/incorrect there
        return None

    def first(m: str) -> dict[Any, bool]:
        mine = [r for r in scored(rows) if r["model"] == m and int(r["repeat"]) == 1 and r["item_id"] in gold]
        if task == "affiliation":
            return {(r["item_id"], u["author"]): u["pred_all"] == u["gold_all"]
                    for r in mine for u in affiliation_units(r, gold[r["item_id"]], canon) if u["gold_author"]}
        correct = _triage_correct if task == "triage" else _screen_correct
        return {r["item_id"]: correct(r, gold[r["item_id"]]) for r in mine}

    a, b_ = first(baseline), first(model)
    both = sorted(set(a) & set(b_))
    b = sum(a[i] and not b_[i] for i in both)
    c = sum(b_[i] and not a[i] for i in both)
    return {"baseline": baseline, "pairs": len(both), "baseline_only_correct": b, "model_only_correct": c,
            "p_value": mcnemar_exact(b, c)}


def summarize(run: Row, rows: list[Row], items: dict[str, Row], gold: dict[str, Row],
              baseline: str | None = None, usage: Row | None = None, gold_file: str | None = None) -> Row:
    task = run["task"]
    models = list(run["models"])
    baseline = baseline or run.get("baseline") or models[0]
    if baseline not in models:
        raise ValueError(f"baseline {baseline!r} is not a model of the run ({models})")
    canon = None
    if task in ANNOTATOR_TASKS:
        return _annotator_summary(run, rows, gold, baseline, usage, gold_file)
    if task == "affiliation":
        from stack.annotate.stage import stack_config
        from stack.canon import Canon

        canon = Canon.from_config(stack_config())
    per: dict[str, Row] = {}
    for m in models:
        mr = [r for r in rows if r["model"] == m]
        block: Row = ({"general": general_block(mr, affiliation_answer, categorical=False)}
                      if task == "affiliation" else {"general": general_block(mr)})
        if task == "triage" and gold:
            block["triage"] = triage_block(mr, gold, items)
        if task == "screen":
            block["screen"] = screen_block(mr, gold)
        if task == "affiliation":
            block["affiliation"] = affiliation_block(mr, gold, canon)
        block["mcnemar"] = mcnemar_block(task, rows, gold, m, baseline, canon)
        block["cache"] = cache_block(mr)
        per[m] = block
    out = {"run_id": run["run_id"], "task": task, "set": run["set"], "split": run["split"],
           "models": models, "baseline": baseline, "gold": bool(gold), "gold_items": len(gold), "gold_file": gold_file,
           "per_model": per, "usage": usage or {}}
    out["markdown"] = markdown(run, out)
    return out


# ── annotators: model_roles, technique_roles, model_mentions ─────────────────────────────────

ANNOTATOR_TASKS = ("model_roles", "technique_roles")
#: Each annotator's payload list of graded rows and its id key (prediction and gold alike).
ANNOTATOR_ROWS = {"model_roles": ("models", "node_id"), "technique_roles": ("techniques", "slug")}


def _payload(r: Row) -> Row:
    """The row's stored payload; a failed row (no answer) is the empty prediction."""
    return (r.get("payload") or {}) if r["status"] == "ok" else {}


def strengths(graded: Iterable[Mapping[str, Any]] | None, id_key: str) -> dict[tuple[str, str], int]:
    """``(id, role) -> strength`` of the nonzero cells of graded rows (a repeated id: the maximum)."""
    out: dict[tuple[str, str], int] = {}
    for g in graded or []:
        for role, v in (g.get("roles") or {}).items():
            if v:
                key = (str(g[id_key]), str(role))
                out[key] = max(out.get(key, 0), int(v))
    return out


def _at(cells: Mapping[tuple[str, str], int], t: int) -> set[tuple[str, str]]:
    return {c for c, v in cells.items() if v >= t}


def _counts(pred: set[Any], gold: set[Any]) -> tuple[int, int, int]:
    return len(pred & gold), len(pred - gold), len(gold - pred)


def _sum_prf(triples: Iterable[tuple[int, int, int]]) -> Row:
    tp = fp = fn = 0
    for a, b, c in triples:
        tp, fp, fn = tp + a, fp + b, fn + c
    return _prf(tp, fp, fn)


def cells_key(task: str) -> Callable[[Row], Any]:
    """A row's answer for repeat agreement: its ≥ 2 cells (an empty prediction for a failed row)."""
    pool, id_key = ANNOTATOR_ROWS[task]
    return lambda r: json.dumps(sorted(_at(strengths(_payload(r).get(pool), id_key), 2)))


def _graded_pairs(task: str, rows: Sequence[Row], gold: Mapping[str, Row]) -> list[tuple[Row, Row, dict[Any, int],
                                                                                      dict[Any, int]]]:
    """``(row, gold, predicted cells, gold cells)`` over scored rows whose item has gold."""
    pool, id_key = ANNOTATOR_ROWS[task]
    return [(r, gold[r["item_id"]], strengths(_payload(r).get(pool), id_key),
             strengths(gold[r["item_id"]]["label"].get(pool), id_key))
            for r in scored(rows) if r["item_id"] in gold]


def model_roles_block(rows: list[Row], gold: Mapping[str, Row]) -> Row:
    """model_roles metrics of one model's rows (module docstring)."""
    pairs = _graded_pairs("model_roles", rows, gold)
    jac, diffs = [], []
    for _, _, p, g in pairs:
        jac.append(jaccard({f"{n}|{r}" for n, r in _at(p, 2)}, {f"{n}|{r}" for n, r in _at(g, 2)}))
        diffs += [abs(p.get(c, 0) - g.get(c, 0)) for c in set(p) | set(g)]
    answered = [r for r in scored(rows) if r["status"] == "ok"]
    repairs = [str(x) for r in answered for x in (_payload(r).get("repairs") or [])]
    return {
        "n": len(pairs), "answered": sum(r["status"] == "ok" for r, *_ in pairs),
        "cells_ge2": _sum_prf(_counts(_at(p, 2), _at(g, 2)) for *_, p, g in pairs),
        "cells_ge1": _sum_prf(_counts(_at(p, 1), _at(g, 1)) for *_, p, g in pairs),
        "jaccard_ge2": _mean(jac),
        "nodes": _sum_prf(_counts({n for n, _ in p}, {n for n, _ in g}) for *_, p, g in pairs),
        "strength_abs_diff": _mean(diffs), "strength_cells": len(diffs),
        "repairs": len(repairs), "quote_unverified": sum(x.startswith("quote_unverified:") for x in repairs),
        "repaired_docs": sum(bool(_payload(r).get("repairs")) for r in answered),
        "no_call": sum(bool(r.get("no_call")) for r in answered),
        "called_without_production_ask": sum(r.get("production_asks") is False and not r.get("no_call")
                                             for r in answered),
    }


_NAME_KEY = re.compile(r"[^0-9a-z]+")


def _name_key(name: str) -> str:
    return _NAME_KEY.sub("", name.casefold())


def _name_matches(pred: str, names: Sequence[str]) -> bool:
    p = _name_key(pred)
    for n in names:
        g = _name_key(n)
        if p == g or (len(p) >= 4 and len(g) >= 4 and (p in g or g in p)):
            return True
    return False


def extras_block(rows: list[Row], gold: Mapping[str, Row]) -> Row:
    """model_roles off-catalogue extras and spurious menu grades (module docstring)."""
    tp = fp = fn = 0
    spurious: list[str] = []
    per: list[Row] = []
    n = 0
    for r in scored(rows):
        g = gold.get(r["item_id"])
        if g is None or "expected_extras" not in g["label"]:
            continue
        n += 1
        expected = [[str(e["name"]), *[str(a) for a in e.get("aliases") or []]] for e in g["label"]["expected_extras"]]
        predicted = [str(x.get("name_as_written") or "") for x in _payload(r).get("extra_models") or []]
        left = list(range(len(expected)))
        hit, extra = [], []
        for name in predicted:
            k = next((i for i in left if _name_matches(name, expected[i])), None)
            if k is None:
                extra.append(name)
            else:
                left.remove(k)
                hit.append(name)
        tp, fp, fn = tp + len(hit), fp + len(extra), fn + len(left)
        graded = {str(m["node_id"]) for m in _payload(r).get("models") or [] if any((m.get("roles") or {}).values())}
        want = {str(m["node_id"]) for m in g["label"].get("models") or [] if any((m.get("roles") or {}).values())}
        wrong = sorted(graded - want)
        if wrong:
            spurious.append(r["item_id"])
        per.append({"item_id": r["item_id"], "repeat": r.get("repeat"), "matched": hit, "unexpected": extra,
                    "missed": [expected[i][0] for i in left], "menu_spurious": wrong})
    return {"n": n, "extras": _prf(tp, fp, fn), "menu_spurious": len(spurious),
            "menu_spurious_items": sorted(set(spurious)), "per_item": per}


def technique_roles_block(rows: list[Row], gold: Mapping[str, Row]) -> Row:
    """technique_roles metrics of one model's rows (module docstring)."""
    pairs = _graded_pairs("technique_roles", rows, gold)

    def slugs(graded: Iterable[Mapping[str, Any]] | None) -> set[str]:
        return {str(t["slug"]) for t in graded or []}

    answered = [r for r in scored(rows) if r["status"] == "ok"]
    graded = [t for r in answered for pool in ("techniques", "extra_techniques") for t in _payload(r).get(pool) or []]
    quotes = [q for t in graded for q in t.get("quotes") or []]
    return {
        "n": len(pairs), "answered": sum(r["status"] == "ok" for r, *_ in pairs),
        "techniques": _sum_prf(_counts(slugs(_payload(r).get("techniques")), slugs(g["label"].get("techniques")))
                               for r, g, _, _ in pairs),
        "cells_ge2": _sum_prf(_counts(_at(p, 2), _at(gc, 2)) for *_, p, gc in pairs),
        "jaccard_ge2": _mean([jaccard({f"{s}|{r}" for s, r in _at(p, 2)}, {f"{s}|{r}" for s, r in _at(gc, 2)})
                              for *_, p, gc in pairs]),
        "quotes": len(quotes), "quotes_verified": sum(bool(q.get("verified")) for q in quotes),
        "quote_verified_rate": ratio(sum(bool(q.get("verified")) for q in quotes), len(quotes)),
        "rows": len(graded), "rows_quote_unverified": sum("quote_unverified" in (t.get("flags") or []) for t in graded),
        "extra_per_doc": _mean([float(len(_payload(r).get("extra_techniques") or [])) for r in answered]),
        "repairs": sum(len(_payload(r).get("repairs") or []) for r in answered),
    }


def _annotator_summary(run: Row, rows: list[Row], gold: dict[str, Row], baseline: str, usage: Row | None,
                       gold_file: str | None) -> Row:
    task = run["task"]
    block_of = model_roles_block if task == "model_roles" else technique_roles_block
    per: dict[str, Row] = {}
    for m in run["models"]:
        mr = [r for r in rows if r["model"] == m]
        per[m] = {"general": general_block(mr, cells_key(task), categorical=False), task: block_of(mr, gold),
                  "mcnemar": None, "cache": cache_block(mr)}
        if task == "model_roles":
            per[m]["extras"] = extras_block(mr, gold)
    out = {"run_id": run["run_id"], "task": task, "set": run["set"], "split": run["split"],
           "models": list(run["models"]), "baseline": baseline, "gold": bool(gold), "gold_items": len(gold),
           "gold_file": gold_file, "per_model": per, "usage": usage or {}, "warnings": list(run.get("warnings") or [])}
    out["markdown"] = markdown(run, out)
    return out


def _lenient(pred: set[str], gold: set[str], related: Callable[[str, str], bool]) -> tuple[int, int]:
    """``(matched predicted, matched gold)``: equal, or one the other's ancestor (``related(a, b)``:
    a is an ancestor of b)."""
    def match(p: str, g: str) -> bool:
        return p == g or related(p, g) or related(g, p)

    return sum(any(match(p, g) for g in gold) for p in pred), sum(any(match(p, g) for p in pred) for g in gold)


def mentions_block(rows: Sequence[Row], gold: Mapping[str, Row], related: Callable[[str, str], bool]) -> Row:
    """model_mentions metrics (module docstring) over scan rows ``{item_id, strong, weak, ambiguous,
    frozen_differs}``; ``related(a, b)``: node a is an ancestor of node b in the vocabulary."""
    graded = [(r, gold[r["item_id"]]["label"]) for r in rows if r["item_id"] in gold]
    out: Row = {"n": len(rows), "gold_items": len(graded),
                "frozen_differs": sum(bool(r["frozen_differs"]) for r in rows),
                "frozen_differs_items": [r["item_id"] for r in rows if r["frozen_differs"]],
                "ambiguous": sum(int(r.get("ambiguous") or 0) for r in rows),
                "gold_oov": sum(len(lab.get("mentions_out_of_vocab") or []) for _, lab in graded)}
    for key, weak in (("strong", False), ("strong_weak", True)):
        tp = fp = fn = mp = mg = np_ = ng = 0
        for r, lab in graded:
            pred = set(r["strong"]) | (set(r["weak"]) if weak else set())
            g = {str(m["node_id"]) for m in lab.get("mentions") or []}
            a, b, c = _counts(pred, g)
            tp, fp, fn = tp + a, fp + b, fn + c
            x, y = _lenient(pred, g, related)
            mp, mg, np_, ng = mp + x, mg + y, np_ + len(pred), ng + len(g)
        out[key] = {**_prf(tp, fp, fn), "pred": tp + fp, "gold": tp + fn}
        p, rc = ratio(mp, np_), ratio(mg, ng)
        out[f"{key}_lenient"] = {"pred": np_, "gold": ng, "pred_matched": mp, "gold_matched": mg, "precision": p,
                                 "recall": rc, "f1": None if p is None or rc is None else
                                 (2 * p * rc / (p + rc) if p + rc else 0.0)}
    out["per_item"] = []
    for r, lab in graded:
        g = {str(m["node_id"]) for m in lab.get("mentions") or []}
        pred = set(r["strong"])
        out["per_item"].append({"item_id": r["item_id"], "fp": sorted(pred - g), "fn": sorted(g - pred),
                                "weak": sorted(set(r["weak"])),
                                "oov": [str(x.get("name")) for x in lab.get("mentions_out_of_vocab") or []]})
    return out


# ── markdown ─────────────────────────────────────────────────────────────────────────────────


def _f(x: float | None, digits: int = 3) -> str:
    return "n/a" if x is None else f"{x:.{digits}f}"


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{100 * x:.1f}%"


def _table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    return ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|",
            *("| " + " | ".join(r) + " |" for r in rows)]


def _common_cells(b: Row) -> list[str]:
    g = b["general"]
    mc = b["mcnemar"]
    fails = f"{g['parse_schema_failures']}/{sum(g['other_failures'].values())}"
    return [fails, str(g["egress_skipped"]), str(g["not_attempted"]),
            "n/a" if g["cost_per_1000_usd"] is None else f"{g['cost_per_1000_usd']:.3f}",
            f"{_f(g['latency_ms_median'], 0)}/{_f(g['latency_ms_p90'], 0)}",
            _pct(g["repeat_agreement"]), "—" if mc is None else _f(mc["p_value"])]


COMMON = ["parse/other fail", "egress skip", "not attempted", "USD / 1,000 docs", "latency ms p50/p90",
          "repeat agree", "McNemar p"]


def markdown(run: Row, s: Row) -> str:
    task = s["task"]
    lines = [f"# stack eval {task}: {s['set']} / {s['split']}", "",
             (f"run `{s['run_id']}`; models {', '.join(s['models'])}; repeats {run.get('repeat', 1)}; "
              f"baseline {s['baseline']}; gold {'yes (' + str(s['gold_items']) + ' items, ' + str(s.get('gold_file')) + ')' if s['gold'] else 'none'}."),
             ("Skipped (egress) and not-attempted rows are outside every denominator; a failed row counts as no "
              "answer. Cost is settled USD over rows that made calls (cache hits excluded)."), ""]
    lines += [f"**Warning:** {w}" for w in run.get("warnings") or []] + ([""] if run.get("warnings") else [])
    per = s["per_model"]
    if task == "triage" and s["gold"]:
        only_recall = any(b["triage"].get("recall_only") for b in per.values())
        na = "not applicable"
        for key, title in (("all", "all items"), ("production_asks", "production asks (abstract or excerpt)"),
                           ("title_only", "title-only probe (production does not ask)")):
            lines += [f"## triage — {title}", ""]
            if only_recall:
                lines += ["Recall-only set (every gold row is fetch): precision and κ are not applicable.", ""]
            rows = []
            for m, b in per.items():
                t = b["triage"][key]
                rows.append([m, str(t["n"]), _pct(t["recall_fetch"]), na if only_recall else _pct(t["precision_fetch"]),
                             _pct(t["skip_rate"]), _pct(t["recall_fetch_rb"]), _pct(t["accuracy3"]),
                             na if only_recall else _f(t["kappa3"]), *(_common_cells(b) if key == "all" else [])])
            header = ["model", "n", "recall fetch", "precision", "skip rate", "recall (rel+bl)", "3-way acc", "κ"]
            lines += _table(header + (COMMON if key == "all" else []), rows) + [""]
        if only_recall:
            lines += ["### verdicts (recall-only set)", ""]
            lines += [f"- **{m}**: {json.dumps(b['triage'].get('verdicts') or {}, sort_keys=True)}" for m, b in per.items()]
            lines.append("")
        if any("recall_by_scope" in b["triage"] for b in per.values()):
            lines += ["### recall of gold fetch by gold scope (fetch_on [relevant] / [relevant, borderline])", ""]
            rows = [[m, *(f"{_pct(b['triage']['recall_by_scope'][k]['recall_fetch'])} / "
                          f"{_pct(b['triage']['recall_by_scope'][k]['recall_fetch_rb'])} "
                          f"(n {b['triage']['recall_by_scope'][k]['gold_fetch']})"
                          for k in ("core", "sr_only", "null"))] for m, b in per.items()]
            lines += _table(["model", "core", "sr_only", "null (carried v1)"], rows) + [""]
        lines += ["### gold-fetch items each model would skip (fetch_on = [relevant])", ""]
        for m, b in per.items():
            sk = b["triage"]["skipped_gold_fetch"]
            lines.append(f"- **{m}** ({len(sk)}): " + ("; ".join(
                f"{x['item_id']} {x['title']!r}" + (f" [{x['repeats_skipped']}/{x['repeats']}]" if x["repeats"] > 1 else "")
                for x in sk) or "none"))
        lines.append("")
    elif task == "screen":
        lines += ["## screen", ""]
        rows = []
        for m, b in per.items():
            sb = b["screen"]
            gold_cells = ([str(sb["n"]), _pct(sb["accuracy"]), _f(sb["kappa"]), _pct(sb["relevant_recall"]),
                           _pct(sb["relevant_precision"]), _pct(sb["kept_recall"]), _pct(sb["kept_precision"]),
                           _f(sb["bucket_jaccard"])] if s["gold"] else [])
            rows.append([m, *gold_cells, _pct(sb["quote_verbatim_rate"]), _pct(sb["tier2_share"]),
                         *_common_cells(b)])
        gold_head = ["n", "acc", "κ", "rel recall", "rel prec", "kept recall", "kept prec", "bucket J"] if s["gold"] else []
        lines += _table(["model", *gold_head, "quote verbatim", "tier-2 share", *COMMON], rows) + [""]
        if s["gold"] and any("scope" in b["screen"] for b in per.values()):
            lines += ["### Shallow Review scope (gold v2)", "",
                      ("Scope acc/κ on answered rows with a scope; kept recall per gold scope; category accuracy on "
                       "rows both kept with a category; SR export = kept and (sr_only, or core with a category other "
                       "than none); Stack export = kept and core. n/a: the model's answers carry no scope (v7/v8)."), ""]
            rows = []
            for m, b in per.items():
                sp = b["screen"]["scope"]
                kb = sp["kept_recall_by_scope"]

                def pr(x: Row | None) -> str:
                    return "n/a" if x is None else f"{_pct(x['precision'])} / {_pct(x['recall'])}"

                rows.append([m, str(sp["scope_n"]), _pct(sp["scope_accuracy"]), _f(sp["scope_kappa"]),
                             f"{_pct(kb['core']['kept_recall'])} (n {kb['core']['n']})",
                             f"{_pct(kb['sr_only']['kept_recall'])} (n {kb['sr_only']['n']})",
                             f"{_pct(sp['sr_category_accuracy'])} (n {sp['sr_category_n']})",
                             pr(sp["sr_export"]), pr(sp["stack_export"])])
            lines += _table(["model", "scope n", "scope acc", "scope κ", "kept recall core", "kept recall sr_only",
                             "category acc", "SR export P / R", "Stack export P / R"], rows) + [""]
        if s["gold"] and any("questions" in b["screen"] for b in per.values()):
            lines += ["### per question", "", QUESTION_NOTE, ""]
            rows = [row for m, b in per.items() if "questions" in b["screen"]
                    for row in question_rows(f"{m} ({b['screen']['questions_mode']})", b["screen"]["questions"])]
            lines += _table(QUESTION_HEAD, rows) + [""]
        if any("usability" in b["screen"] for b in per.values()):
            lines += ["### text usability (question U)", "",
                      ("Final answers over answered rows: verdict counts (`none` = no usability answer), issue codes "
                       "(a row may carry several), rows with a note; chars shown = mean document characters tier 1 "
                       "sent. Not scored: there is no usability gold."), ""]
            rows = []
            for m, b in per.items():
                u = b["screen"].get("usability")
                if u is None:
                    continue
                v = u["verdicts"]
                rows.append([m, str(u["answered"]), *(str(v[k]) for k in (*USABILITY_VERDICTS, "none")),
                             ", ".join(f"{k} {n}" for k, n in u["issues"].items()) or "none", str(u["notes"]),
                             _f(u["chars_sent_tier1_mean"], 0)])
            lines += _table(["model", "answered", *USABILITY_VERDICTS, "none", "issues", "notes", "chars shown"],
                            rows) + [""]
        lines += ["### what tier 2 changed", ""]
        for m, b in per.items():
            sb = b["screen"]
            extra = (f"; correct vs gold before {sb['tier2_tier1_correct']}, after {sb['tier2_final_correct']}; "
                     f"{sb['tier2_confusion']}") if s["gold"] else ""
            lines.append(f"- **{m}**: escalated {sb['tier2_escalated']} (failed {sb['tier2_failed']}); "
                         f"{sb['tier2_changes'] or 'no changes'}{extra}")
        lines.append("")
    elif task == "affiliation":
        lines += ["## affiliation", "",
                  "Pairs (author, org): *tracked* = roster ids; *all* = roster ids plus untracked org strings "
                  "(normalised). Broadcast = an org on >= 80 % of a paper's authors that gold gives < 50 %.", ""]
        rows = []
        for m, b in per.items():
            ab = b["affiliation"]
            gold_cells = ([str(ab["n"]), str(ab["gold_authors"]),
                           *(f"{_f(ab[k]['precision'])}/{_f(ab[k]['recall'])}/{_f(ab[k]['f1'])}"
                             for k in ("tracked", "all")),
                           _pct(ab["byline_coverage"]), _pct(ab["org_coverage"]), str(ab["broadcast"])]
                          if s["gold"] else [])
            rows.append([m, *gold_cells, _pct(ab["anchor_void_rate"]), str(ab["roster_mismatch_rows"]),
                         str(b["general"]["schema_degraded"]), *_common_cells(b)])
        gold_head = (["n", "gold authors", "tracked P/R/F1", "all P/R/F1", "byline cov", "org cov", "broadcast"]
                     if s["gold"] else [])
        lines += _table(["model", *gold_head, "anchor void", "roster mismatch", "schema degraded", *COMMON],
                        rows) + [""]
        if s["gold"]:
            lines += ["### broadcasts", ""]
            for m, b in per.items():
                bc = b["affiliation"]["broadcasts"]
                lines.append(f"- **{m}** ({len(bc)}): " + ("; ".join(
                    f"{x['item_id']} r{x['repeat']} {x['org']} ({x['pred_share']:.0%} vs gold {x['gold_share']:.0%})"
                    for x in bc) or "none"))
            lines.append("")
    elif task == "model_roles":
        lines += ["## model_roles", "",
                  ("Cells = (menu node, role) at strength ≥ 2 (primary) / ≥ 1, micro over (item, repeat); a failed row "
                   "and an item production answers without a call count with the empty prediction. J = mean "
                   "per-document Jaccard of the ≥ 2 cells; nodes = graded as a real mention (any role ≥ 1); |Δ| = "
                   "mean |strength difference| over cells nonzero on either side."), ""]
        rows = []
        for m, b in per.items():
            x = b["model_roles"]
            rows.append([m, str(x["n"]), str(x["answered"]), _prf_cell(x["cells_ge2"]), _prf_cell(x["cells_ge1"]),
                         _f(x["jaccard_ge2"]), _prf_cell(x["nodes"]), _f(x["strength_abs_diff"], 2),
                         f"{x['repairs']} ({x['repaired_docs']} docs)", str(x["quote_unverified"]), str(x["no_call"]),
                         str(b["general"]["schema_degraded"]), *_common_cells(b)])
        lines += _table(["model", "n", "answered", "cells≥2 P/R/F1", "cells≥1 P/R/F1", "J≥2", "nodes P/R/F1", "|Δ|",
                         "repairs", "quote unverified", "no call", "schema degraded", *COMMON], rows) + [""]
        if any(b.get("extras", {}).get("n") for b in per.values()):
            lines += ["### off-catalogue extras", "",
                      ("Items whose gold lists the expected extra models (`label.expected_extras`): extras P/R/F1 by "
                       "loose name match; menu spurious = items with a graded menu row gold does not grade."), ""]
            lines += _table(["model", "n", "extras P/R/F1", "menu spurious"],
                            [[m, str(b["extras"]["n"]), _prf_cell(b["extras"]["extras"]),
                              f"{b['extras']['menu_spurious']}" + (f" ({', '.join(b['extras']['menu_spurious_items'])})"
                                                                     if b["extras"]["menu_spurious_items"] else "")]
                             for m, b in per.items()]) + [""]
    elif task == "technique_roles":
        lines += ["## technique_roles", "",
                  ("Techniques = slug present (a row exists), cells = (slug, role) at strength ≥ 2, micro over (item, "
                   "repeat); a failed row counts as the empty prediction. Quotes: verified / all quotes of techniques "
                   "and extra techniques (answered rows); rows flagged `quote_unverified`."), ""]
        rows = []
        for m, b in per.items():
            x = b["technique_roles"]
            rows.append([m, str(x["n"]), str(x["answered"]), _prf_cell(x["techniques"]), _prf_cell(x["cells_ge2"]),
                         _f(x["jaccard_ge2"]), f"{_pct(x['quote_verified_rate'])} ({x['quotes']})",
                         f"{x['rows_quote_unverified']}/{x['rows']}", _f(x["extra_per_doc"], 2),
                         str(b["general"]["schema_degraded"]), *_common_cells(b)])
        lines += _table(["model", "n", "answered", "techniques P/R/F1", "cells≥2 P/R/F1", "J≥2", "quotes verified",
                         "rows unverified", "extra / doc", "schema degraded", *COMMON], rows) + [""]
    else:
        lines += [f"## {task} (no gold: cost, latency, failures only)", ""]
        rows = [[m, str(b["general"]["scored"]), json.dumps(b["general"]["verdicts"], sort_keys=True),
                 *_common_cells(b)] for m, b in per.items()]
        lines += _table(["model", "n", "verdicts", *COMMON], rows) + [""]
    lines += cache_markdown(s)
    return "\n".join(lines)


def _prf_cell(x: Row) -> str:
    return f"{_f(x['precision'])}/{_f(x['recall'])}/{_f(x['f1'])}"


def _usd(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.3f}"


def cache_markdown(s: Row) -> list[str]:
    """The provider prompt-cache table (:func:`cache_block` per model)."""
    us = s.get("usage") or {}
    lines = ["## provider prompt cache and USD per 1,000 documents", "",
             ("Over scored rows, each one (item, repeat) call (screen: tier 2 included); a row replayed from the "
              "local response cache carries the original call's tokens and cost (from its cache entry). "
              "*settled* = OpenRouter's `usage.cost` (provider cache discounts and write premiums included); "
              "*no cache* = prompt x in-price + completion x out-price at the `llm.yaml` table "
              f"(version {us.get('llm_yaml_version', '?')}); for deepseek via DeepInfra the table holds the listing "
              "price, above the endpoint's. Retries and failed tiers count at their billed amount in both."
              + (f" Usage backfilled from cache entries: {us.get('backfill')}." if us.get("backfill") else "")), ""]
    rows = []
    for m, b in s["per_model"].items():
        c = b["cache"]
        rows.append([m, str(c["rows"]), _f(c["prompt_tokens_per_row"], 0), _pct(c["cached_share"]),
                     str(c["cache_write_tokens"]), _f(c["reasoning_tokens_per_row"], 0),
                     _usd(c["usd_per_1000_settled"]), _usd(c["usd_per_1000_nocache"]),
                     str(c["rows_replayed"]), str(c["rows_usage_missing"])])
    return lines + _table(["model", "rows", "prompt tok/row", "cached share", "cache-write tok", "reasoning tok/row",
                   "USD/1k settled (provider cache)", "USD/1k no cache (table)", "rows from cache entry",
                   "usage missing"], rows) + [""]


def _llm_config() -> Any:
    from stack.annotate.stage import stack_config

    return stack_config().llm


DEFAULT_GOLD = "gold.jsonl"


def default_gold(set_dir: Path) -> str:
    """The gold file a new run (or a combine) of ``set_dir`` uses without ``--gold``: ``gold-v2.jsonl``
    (:data:`stack.evals.gold.V2_GOLD`) when the set has it, else :data:`DEFAULT_GOLD`."""
    return V2_GOLD if (set_dir / V2_GOLD).is_file() else DEFAULT_GOLD


def items_digest_problem(run: Row, set_dir: Path) -> str | None:
    """Why ``run``'s rows cannot be joined with ``set_dir``'s current ``items.jsonl`` (the run's
    ``set_items_digest``, the sha256 of the file it ran on, differs from the file's now, or the run
    recorded none), else None. combine, pair-screens and compare refuse on it (S-39)."""
    path = _items_file(set_dir)
    now = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
    ran = run.get("set_items_digest")
    if ran is not None and ran == now:
        return None
    return (f"set_items_digest {ran[:12] if ran else 'unrecorded'} (the items.jsonl it ran on) differs from "
            f"{path}'s {now[:12] if now else 'missing'}: its rows belong to another version of the set")


def gold_path(set_dir: Path, name: str) -> Path:
    """A gold file name (no path separator: a file in ``set_dir``) or path."""
    return set_dir / name if "/" not in name and "\\" not in name else Path(name)


def load_gold(set_dir: Path, name: str | None) -> tuple[str, dict[str, Row]]:
    """``(file, gold by item_id)``: ``name`` (must exist), else :data:`DEFAULT_GOLD` (may be absent:
    no gold)."""
    file = name or DEFAULT_GOLD
    path = gold_path(set_dir, file)
    if not path.is_file():
        if name is not None and name != DEFAULT_GOLD:
            raise FileNotFoundError(f"gold file {path} does not exist")
        return file, {}
    return file, {g["item_id"]: g for g in read_jsonl(path)}


def score_run(run_dir: Path, *, set_dir: Path | None = None, baseline: str | None = None,
              gold: str | None = None, summary: str = "summary") -> Row:
    """Score ``run_dir`` (its ``run.json`` names the set directory and the gold file) and write the
    summaries ``<summary>.json`` / ``<summary>.md`` in it; ``gold`` overrides the run's gold file (module
    docstring). A technique_staged run scored on another ``set_dir`` must have run on that set's
    ``items.jsonl`` (techpipe D611)."""
    if not summary or "/" in summary or "\\" in summary or summary.startswith("."):
        raise ValueError(f"--summary {summary!r}: a plain file name stem")
    run = json.loads((run_dir / "run.json").read_text("utf-8"))
    if run.get("status") == "estimate":
        raise ValueError(f"{run_dir}: an --estimate record has no rows to score")
    sd = set_dir if set_dir is not None else Path(run["set_dir"])
    items = {i["item_id"]: i for i in read_jsonl(_items_file(sd))}
    gold_file, gold_rows = load_gold(sd, gold or run.get("gold"))
    rows = read_jsonl(run_dir / "rows.jsonl")
    llm = _llm_config()
    usage = {"backfill": backfill_usage(run, rows, items, llm.price_for), "llm_yaml_version": llm.version}
    if run["task"] == "technique_staged":  # several calls per row, its own metrics (techpipe D607)
        from stack.evals import technique_staged

        if set_dir is not None and (problem := items_digest_problem(run, set_dir)) is not None:
            raise ValueError(f"{run_dir}: {problem}")

        try:
            s = technique_staged.summarize(run, rows, gold_rows, sd, gold_file, usage)
        except technique_staged.AnalysisError as exc:
            raise ValueError(str(exc)) from exc
    else:
        s = summarize(run, rows, items, gold_rows, baseline, usage, gold_file=gold_file)
    (run_dir / f"{summary}.json").write_text(
        json.dumps({k: v for k, v in s.items() if k != "markdown"}, indent=2, sort_keys=True) + "\n", "utf-8")
    (run_dir / f"{summary}.md").write_text(s["markdown"] + "\n", "utf-8")
    return s


# ── compare two screen runs ──────────────────────────────────────────────────────────────────


def _load_screen_run(d: Path) -> tuple[Row, list[Row]]:
    if not (d / "run.json").is_file() or not (d / "rows.jsonl").is_file():
        raise ValueError(f"{d}: not a finished run (run.json and rows.jsonl)")
    run = json.loads((d / "run.json").read_text("utf-8"))
    if run.get("task") != "screen":
        raise ValueError(f"{d}: task {run.get('task')!r}, not screen")
    problem = items_digest_problem(run, Path(run["set_dir"]))
    if problem is not None:
        raise ValueError(f"{d}: {problem}")
    return run, read_jsonl(d / "rows.jsonl")


def _question_answer(r: Row, q: str) -> str:
    """Row ``r``'s answer to question ``q`` (``stack``, ``sr``; ``overall`` = the keep verdict)."""
    if q == "overall":
        return answer(r)
    v = r.get(f"{q}_verdict")
    return str(v) if r["status"] == "ok" and v else NO_ANSWER


#: Gold label key per compared question.
COMPARE_GOLD = {"stack": "stack_verdict", "sr": "sr_verdict", "overall": "verdict"}


def compare_runs(run_a: Path, run_b: Path, *, gold: str | None = None, price_for: Any = None,
                 write: bool = True) -> Row:
    """Paired comparison of two screen runs (module docstring, *Compare*); written to
    ``compare-<stamp>.md`` beside run A. Refuses runs of different sets, splits or item lists, or
    with no model in common."""
    price_for = price_for or _llm_config().price_for
    ra, rows_a = _load_screen_run(run_a)
    rb, rows_b = _load_screen_run(run_b)
    for k in ("set_dir", "split"):
        if ra.get(k) != rb.get(k):
            raise ValueError(f"run A {k} {ra.get(k)!r} differs from run B {k} {rb.get(k)!r}")
    if sorted(ra.get("items") or []) != sorted(rb.get("items") or []):
        raise ValueError("run A and run B ran different items (compare needs the same items)")
    models = [m for m in ra["models"] if m in rb["models"]]
    if not models:
        raise ValueError(f"no model in common (A {ra['models']}, B {rb['models']})")
    set_dir = Path(ra["set_dir"])
    items = {i["item_id"]: i for i in read_jsonl(_items_file(set_dir))}
    backfill = {"a": backfill_usage(ra, rows_a, items, price_for), "b": backfill_usage(rb, rows_b, items, price_for)}
    gold_file, gold_rows = load_gold(set_dir, gold or ra.get("gold"))
    if not gold_rows:
        raise ValueError(f"gold {gold_file}: no gold rows")

    def first(rows: list[Row], m: str) -> dict[str, Row]:
        return {r["item_id"]: r for r in scored(rows) if r["model"] == m and int(r["repeat"]) == 1}

    per: dict[str, Row] = {}
    for m in models:
        a, b = first(rows_a, m), first(rows_b, m)
        both = sorted(set(a) & set(b))
        qs: dict[str, Row] = {}
        for q, key in COMPARE_GOLD.items():
            pairs = [i for i in both if i in gold_rows and gold_rows[i]["label"].get(key) is not None]
            if not pairs:
                qs[q] = {"n": 0, "applicable": False}
                continue
            ga = {i: _question_answer(a[i], q) for i in pairs}
            gb = {i: _question_answer(b[i], q) for i in pairs}
            g = {i: str(gold_rows[i]["label"][key]) for i in pairs}
            ok_a = {i: ga[i] == g[i] for i in pairs}
            ok_b = {i: gb[i] == g[i] for i in pairs}
            nb = sum(ok_a[i] and not ok_b[i] for i in pairs)
            nc = sum(ok_b[i] and not ok_a[i] for i in pairs)
            qs[q] = {"n": len(pairs), "applicable": True,
                     "accuracy_a": ratio(sum(ok_a.values()), len(pairs)),
                     "accuracy_b": ratio(sum(ok_b.values()), len(pairs)),
                     "answered_a": sum(ga[i] != NO_ANSWER for i in pairs),
                     "answered_b": sum(gb[i] != NO_ANSWER for i in pairs),
                     "b": nb, "c": nc, "p_value": mcnemar_exact(nb, nc),
                     "discordant": [{"item_id": i, "gold": g[i], "a": ga[i], "b": gb[i]}
                                    for i in pairs if ok_a[i] != ok_b[i]]}
        ca = cache_block(list(a.values()))
        cb = cache_block(list(b.values()))
        per[m] = {"pairs": len(both), "only_a": len(set(a) - set(b)), "only_b": len(set(b) - set(a)),
                  "questions": qs,
                  "prompt_tokens_per_row_a": ca["prompt_tokens_per_row"],
                  "prompt_tokens_per_row_b": cb["prompt_tokens_per_row"],
                  "usd_per_1000_settled_a": ca["usd_per_1000_settled"],
                  "usd_per_1000_settled_b": cb["usd_per_1000_settled"]}
    out: Row = {"run_a": str(run_a), "run_b": str(run_b), "set": ra.get("set"), "split": ra.get("split"),
                "prompt_a": ra.get("prompt"), "prompt_b": rb.get("prompt"),
                "pin_overrides_a": ra.get("pin_overrides") or {}, "pin_overrides_b": rb.get("pin_overrides") or {},
                "gold_file": gold_file, "backfill": backfill, "per_model": per}
    out["markdown"] = compare_markdown(out)
    path = None
    if write:
        import datetime as dt

        stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
        path = run_a.parent / f"compare-{stamp}.md"
        n = 1
        while path.exists():
            n += 1
            path = run_a.parent / f"compare-{stamp}-{n}.md"
        path.write_text(out["markdown"] + "\n", "utf-8")
    return {**out, "path": str(path) if path else None}


def compare_markdown(c: Row) -> str:
    def label(prompt: Any, pins: Mapping[str, Any]) -> str:
        extra = ", ".join(f"{k}={json.dumps(v)}" for k, v in sorted(pins.items()))
        return f"{prompt}" + (f" ({extra})" if extra else "")

    lines = [f"# stack eval compare: {c['set']} / {c['split']}", "",
             (f"A = `{Path(c['run_a']).name}` ({label(c['prompt_a'], c['pin_overrides_a'])}); "
              f"B = `{Path(c['run_b']).name}` ({label(c['prompt_b'], c['pin_overrides_b'])}); gold {c['gold_file']}."),
             ("Paired per (model, item), repeat 1, scored rows of both runs; each question 3-way against its gold "
              "(S `stack_verdict`, R `sr_verdict`, overall keep `verdict`); a failed row or a missing answer is no "
              "answer. b = A right and B wrong, c = B right and A wrong; exact McNemar p (n/a: no discordant pair)."),
             ""]
    for m, b in c["per_model"].items():
        lines += [f"## {m}", "",
                  (f"pairs {b['pairs']} (only in A {b['only_a']}, only in B {b['only_b']}); prompt tokens per row "
                   f"A {_f(b['prompt_tokens_per_row_a'], 0)} / B {_f(b['prompt_tokens_per_row_b'], 0)}; USD per 1,000 "
                   f"documents settled A {_usd(b['usd_per_1000_settled_a'])} / B {_usd(b['usd_per_1000_settled_b'])}."),
                  ""]
        rows = []
        for q, x in b["questions"].items():
            if not x["applicable"]:
                rows.append([QUESTION_LABEL[q], "0", *(["n/a"] * 6)])
                continue
            rows.append([QUESTION_LABEL[q], str(x["n"]), f"{x['answered_a']} / {x['answered_b']}",
                         _pct(x["accuracy_a"]), _pct(x["accuracy_b"]), str(x["b"]), str(x["c"]), _f(x["p_value"])])
        lines += _table(["question", "n", "answered A / B", "acc A", "acc B", "b (A only right)", "c (B only right)",
                         "McNemar p"], rows) + [""]
        for q, x in b["questions"].items():
            if x.get("discordant"):
                lines.append(f"- discordant {QUESTION_LABEL[q]}: " + "; ".join(
                    f"{d['item_id']} gold {d['gold']}, A {d['a']}, B {d['b']}" for d in x["discordant"]))
        lines.append("")
    return "\n".join(lines)


__all__ = ["affiliation_block", "backfill_usage", "broadcasts", "cache_block", "entry_usage", "general_block",
           "in_sr", "in_stack", "jaccard", "load_gold", "match_authors", "mcnemar_exact", "quantile",
           "question_block", "question_rows", "questions_block", "recall_only", "row_usage", "scope_block",
           "score_run", "screen_block", "summarize", "triage_block", "usability_block", "verdict_metrics",
           "compare_runs", "compare_markdown", "default_gold", "items_digest_problem"]


def _items_file(set_dir: Path) -> Path:
    """The set's ``items.jsonl``, else the eval-text cache's (:func:`stack.evals.freeze.items_file`, D795)."""
    from stack.evals.freeze import items_file

    return items_file(Path(set_dir))
