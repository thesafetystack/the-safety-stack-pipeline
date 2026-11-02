"""Gold labels of an eval set from two independent labelers (track E; design: evals/<task>/<set>/README.md).

``python -m stack.evals.gold merge <task> --set <id>`` reads ``labels/opus-*.jsonl`` and ``labels/codex-*.jsonl``,
reports agreement and Cohen's κ on the primary field, and writes ``labels/disagreements.jsonl`` (item input + both
labels and rationales) for the adjudicator. ``finalize`` writes ``gold.jsonl`` from the agreements plus
``labels/adjudication.jsonl``. ``calibration`` writes the markdown slice for a human labeler.
Primary field: triage and screen ``verdict``; affiliation the per-author organisation sets, normalised
(:func:`affiliation_key`: per author the H-S12 name key, the roster-id set and the set of
:func:`norm_org` strings); ``merge`` also reports per-author agreement on each of the two sets.

**Version 2** (``merge|finalize <task> --set <id> --version v2``; triage and screen; rubrics
``evals/rubrics/{triage,screen}-v2.md``, the Shallow Review scope): labels from ``labels-v2/``
(``opus-*``, ``codex-*``, ``adjudication.jsonl``, and ``tomas-*`` reference labels where present),
gold to ``gold-v2.jsonl``; ``labels/`` and ``gold.jsonl`` (v1) are untouched. Label part: triage
``{verdict, fetch, scope}``, screen ``{verdict, buckets, scope, sr_category, sr_agenda}``. A
disagreement (→ ``labels-v2/disagreements.jsonl``, with the differing ``fields``) is a differing
``verdict`` or ``scope`` or (screen) ``sr_category``; ``sr_agenda`` is never adjudicated (gold: the
adjudicator's if given, else the common value, else null; both labelers' values in
``sr_agenda_labels``). An item of ``items.jsonl`` without v2 labels carries its v1 gold row
(``carried_from: "v1"``, scope fields null). Where a reference label exists (``tomas-*``: triage
so far) gold ``verdict``/``fetch`` are the reference's (``reference: "tomas"``), the scope comes
from the labelers' agreement or the adjudication, and the would-be gold is kept as
``label_without_reference``. Details: :func:`merge_v2`, :func:`finalize_v2`.

**Per-question screen gold** (track E2, D???(E)). Every v2 screen gold label also carries
``stack_verdict`` and ``sr_verdict`` (the two relevance questions of ``screen-v10`` /
``screen-stack-v1`` / ``screen-sr-v1``), derived by :func:`question_verdicts`: Stack = the
verdict if scope is ``core``, else ``irrelevant``; SR = the verdict if scope is ``sr_only``, or
``core`` with an ``sr_category`` other than ``none``/null, else ``irrelevant``; both null where
the scope is null (rows carried from v1). A screen reference label (``tomas-*``: ``{verdict,
stack_verdict, sr_verdict}``) overrides those three fields; the derived ones stay in
``label_without_reference``. A conductor override (``labels-v2/conductor-*.jsonl``: ``{item_id,
sr_category, note}``; :data:`CONDUCTOR_FIELDS`) is applied after the adjudication and before the
derivation, recorded on the row as ``conductor_override`` ``{note, fields: {<field>: {from,
to}}}``; the reference still decides the verdict fields.

:func:`affiliation_runner` loads the v3 affiliation runner (``workflows/annotate/affiliation/main.py``)
by path, as the stage loader does; its ``ex`` is the pure ``extract.py`` (``norm_author_name``,
``header_window``, ``parse_authors``, ``align_to_roster``, ``resolve``, ...). The freezer, the harness
and the scorer share this one loaded copy.
"""

from __future__ import annotations

import argparse
import functools
import json
import random
import unicodedata
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType
from typing import Any

Row = dict[str, Any]
REPO = Path(__file__).resolve().parents[2]
#: The repository's eval sets whatever the cwd (S25-96).
ROOT = REPO / "evals"
AFFILIATION_MAIN = REPO / "workflows" / "annotate" / "affiliation" / "main.py"


@functools.cache
def affiliation_runner() -> ModuleType:
    """The v3 affiliation runner module, loaded by path once (``workflows/`` is not a package)."""
    from stack.stage_runner import load_path_module

    module = load_path_module("_eval_affiliation__main", AFFILIATION_MAIN)  # registered in sys.modules
    if module is None:
        raise RuntimeError(f"cannot load {AFFILIATION_MAIN}")
    return module


def affiliation_extract() -> ModuleType:
    """``extract.py`` as the runner loaded it (``main.ex``)."""
    return affiliation_runner().ex  # type: ignore[no-any-return]


_ORG_EDGE = " \t\n,;.:"


def norm_org(raw: str) -> str:
    """An organisation string for comparison: NFKC, casefolded, whitespace collapsed, leading and
    trailing punctuation (``,;.:``) dropped (the rubric's copy rule drops them too)."""
    s = " ".join(unicodedata.normalize("NFKC", raw or "").casefold().split()).strip(_ORG_EDGE)
    return s[4:] if s.startswith("the ") else s  # "the X Laboratory …" as printed in running text = "X Laboratory …"


def affiliation_key(lab: Row) -> tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...]:
    """The affiliation label's comparison key: per author (sorted by name key)
    ``(norm_author_name(name), sorted roster ids, sorted norm_org(org))``."""
    norm_name = affiliation_extract().norm_author_name
    return tuple(sorted(
        (norm_name(str(a.get("name") or "")),
         tuple(sorted(set(a.get("roster_ids") or []))),
         tuple(sorted({norm_org(o) for o in a.get("orgs") or [] if norm_org(o)})))
        for a in lab.get("authors") or []))


def affiliation_agreement(pairs: list[tuple[Row, Row]]) -> Row:
    """Per-author agreement between two labelers' affiliation labels: authors matched by name key;
    shares of matched authors whose roster-id sets / normalised org-string sets / both are equal."""
    authors = matched = ids = orgs = both = 0
    for a, b in pairs:
        ka = {name: (i, o) for name, i, o in affiliation_key(a)}
        kb = {name: (i, o) for name, i, o in affiliation_key(b)}
        authors += len(set(ka) | set(kb))
        for name in set(ka) & set(kb):
            matched += 1
            ids += ka[name][0] == kb[name][0]
            orgs += ka[name][1] == kb[name][1]
            both += ka[name] == kb[name]
    return {"authors": authors, "authors_matched": matched,
            "roster_ids_agree": ids / matched if matched else None,
            "orgs_agree": orgs / matched if matched else None,
            "both_agree": both / matched if matched else None}


def read_jsonl(path: Path) -> list[Row]:
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


def write_jsonl(path: Path, rows: list[Row]) -> None:
    path.write_text("".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows), "utf-8")


def labels_of(set_dir: Path, who: str, labels_dir: str = "labels") -> dict[str, Row]:
    out: dict[str, Row] = {}
    for f in sorted((set_dir / labels_dir).glob(f"{who}-*.jsonl")):
        for r in read_jsonl(f):
            if r["item_id"] in out:
                raise SystemExit(f"{f}: item {r['item_id']} labelled twice by {who}")
            out[r["item_id"]] = r
    return out


def primary(task: str, lab: Row) -> Any:
    if task == "affiliation":
        return affiliation_key(lab)
    return lab["verdict"]


def label_part(task: str, lab: Row) -> Row:
    if task == "triage":
        return {"verdict": lab["verdict"], "fetch": lab["verdict"] != "not_relevant"}
    if task == "screen":
        return {"verdict": lab["verdict"], "buckets": sorted(lab.get("buckets") or [])}
    return {"authors": lab.get("authors") or []}


def kappa(pairs: list[tuple[Any, Any]]) -> float | None:
    """Cohen's κ over paired categorical labels."""
    n = len(pairs)
    if n == 0:
        return None
    po = sum(a == b for a, b in pairs) / n
    ca, cb = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    pe = sum(ca[k] * cb.get(k, 0) for k in ca) / (n * n)
    return 1.0 if pe == 1 else (po - pe) / (1 - pe)


def input_text(item: Row) -> str:
    inp = item["input"]
    return inp.get("record") or inp.get("text") or inp.get("window") or inp.get("byline_text") or ""


def verbatim(quote: str, text: str) -> bool:
    return bool(quote) and " ".join(quote.split()) in " ".join(text.split())


def merge(task: str, set_dir: Path) -> Row:
    items = {i["item_id"]: i for i in read_jsonl(_items_file(set_dir))}
    a, b = labels_of(set_dir, "opus"), labels_of(set_dir, "codex")
    both = sorted(set(a) & set(b) & set(items))
    pairs = [(primary(task, a[i]), primary(task, b[i])) for i in both]
    dis = [i for i, (x, y) in zip(both, pairs) if x != y]
    rep: Row = {"items": len(items), "opus": len(a), "codex": len(b), "both": len(both),
                "missing_opus": sorted(set(items) - set(a)), "missing_codex": sorted(set(items) - set(b)),
                "agree": len(both) - len(dis), "disagree": len(dis)}
    if task != "affiliation":
        rep["kappa"] = kappa(pairs)
        rep["confusion"] = {f"{x}|{y}": n for (x, y), n in sorted(Counter(pairs).items())}
        for split in ("dev", "test"):
            sp = [(primary(task, a[i]), primary(task, b[i])) for i in both if items[i]["split"] == split]
            rep[f"kappa_{split}"] = kappa(sp)
    if task == "affiliation":
        rep["per_author"] = affiliation_agreement([(a[i], b[i]) for i in both])
    if task == "triage":
        fp = [(a[i]["verdict"] != "not_relevant", b[i]["verdict"] != "not_relevant") for i in both]
        rep["fetch_agree"] = sum(x == y for x, y in fp)
        rep["kappa_fetch"] = kappa(fp)
    write_jsonl(set_dir / "labels" / "disagreements.jsonl",
                [{"item_id": i, "input": items[i]["input"], "labeler_A": a[i], "labeler_B": b[i]} for i in dis])
    return rep


def _gold_row(task: str, item: Row, a: Row, b: Row | None, adj: Row | None) -> Row:
    text = input_text(item)
    if b is None:  # second labeler checked only a sample (affiliation: 30 %): gold = the first labeler's
        return {"item_id": item["item_id"], "doc_id": item["doc_id"], "label": label_part(task, a),
                "confidence": a.get("confidence"), "rationale": a.get("rationale"),
                "quote": a.get("quote", "") if verbatim(a.get("quote", ""), text) else "",
                "labelers": [{"who": "opus", "label": label_part(task, a), "confidence": a.get("confidence"),
                              "rationale": a.get("rationale"), "quote": a.get("quote", "")}],
                "agreement": None, "contested": False, "adjudication_note": None}
    labelers = [{"who": who, "label": label_part(task, lab), "confidence": lab.get("confidence"),
                 "rationale": lab.get("rationale"), "quote": lab.get("quote", "")}
                for who, lab in (("opus", a), ("codex-gpt-6-sol", b))]
    agree = primary(task, a) == primary(task, b)
    if agree:
        src, conf = a, round(((a.get("confidence") or 0) + (b.get("confidence") or 0)) / 2, 3)
        quote = next((q for q in (a.get("quote", ""), b.get("quote", "")) if verbatim(q, text)), "")
        note = None
    else:
        if adj is None:
            raise SystemExit(f"{item['item_id']}: disagreement without adjudication")
        src, conf, quote, note = adj, adj.get("confidence"), adj.get("quote", ""), adj.get("adjudication_note")
    return {"item_id": item["item_id"], "doc_id": item["doc_id"], "label": label_part(task, src),
            "confidence": conf, "rationale": src.get("rationale"), "quote": quote, "labelers": labelers,
            "agreement": agree, "contested": not agree, "adjudication_note": note}


def finalize(task: str, set_dir: Path) -> Row:
    items = read_jsonl(_items_file(set_dir))
    a, b = labels_of(set_dir, "opus"), labels_of(set_dir, "codex")
    adj_path = set_dir / "labels" / "adjudication.jsonl"
    adj = {r["item_id"]: r for r in read_jsonl(adj_path)} if adj_path.exists() else {}
    gold = [_gold_row(task, i, a[i["item_id"]], b.get(i["item_id"]), adj.get(i["item_id"])) for i in items]
    write_jsonl(set_dir / "gold.jsonl", gold)
    base: Counter[str] = Counter()
    for g, i in zip(gold, items):
        if task != "affiliation":
            base[f"{i['split']}:{g['label']['verdict']}"] += 1
    return {"gold": len(gold), "contested": sum(g["contested"] for g in gold), "base_rates": dict(sorted(base.items()))}


# ── version 2: the Shallow Review scope (module docstring) ──────────────────────────────────

V2_LABELS = "labels-v2"
V2_GOLD = "gold-v2.jsonl"
#: The labelers' ``who`` in gold rows (as v1 writes them).
WHO_A, WHO_B = "opus", "codex-gpt-6-sol"
#: Fields whose difference makes a v2 disagreement (``sr_agenda`` is never adjudicated).
V2_FIELDS = {"triage": ("verdict", "scope"), "screen": ("verdict", "scope", "sr_category")}
#: Verdicts that keep a document (fetch at triage, kept at screen).
KEPT = {"triage": ("relevant", "borderline"), "screen": ("relevant", "borderline")}


def question_verdicts(verdict: Any, scope: Any, sr_category: Any) -> tuple[Any, Any]:
    """``(stack_verdict, sr_verdict)`` derived from a screen verdict and its scope fields (module
    docstring); ``(None, None)`` when ``scope`` is None (unknown, e.g. carried from v1)."""
    if scope is None:
        return None, None
    stack = verdict if scope == "core" else "irrelevant"
    sr = verdict if scope == "sr_only" or (scope == "core" and sr_category not in ("none", None)) else "irrelevant"
    return stack, sr


def _with_questions(task: str, label: Row) -> Row:
    """A screen label with its derived ``stack_verdict`` / ``sr_verdict``; other tasks unchanged."""
    if task == "screen":
        label["stack_verdict"], label["sr_verdict"] = question_verdicts(
            label.get("verdict"), label.get("scope"), label.get("sr_category"))
    return label


#: The fields a reference label sets: triage verdict and fetch; screen verdict and the two questions.
REFERENCE_FIELDS = {"triage": ("verdict", "fetch"), "screen": ("verdict", "stack_verdict", "sr_verdict")}


def _reference_part(task: str, ref: Row) -> Row:
    """The gold fields reference label ``ref`` overrides (:data:`REFERENCE_FIELDS`); a missing
    field refuses."""
    missing = [f for f in REFERENCE_FIELDS[task] if f not in ref]
    if missing:
        raise SystemExit(f"{ref.get('item_id')}: reference label lacks {missing}")
    return {"verdict": ref["verdict"], "fetch": bool(ref["fetch"])} if task == "triage" else {
        f: ref[f] for f in REFERENCE_FIELDS[task]}


#: The label fields a conductor override (``labels-v2/conductor-*.jsonl``) may set, per task.
CONDUCTOR_FIELDS: dict[str, tuple[str, ...]] = {"screen": ("sr_category",)}


def _conductor_overrides(task: str, set_dir: Path, items: Mapping[str, Row]) -> dict[str, Row]:
    """The conductor override rows of a v2 set by item; unknown keys, unknown items or a row
    setting no field refuse."""
    rows = labels_of(set_dir, "conductor", V2_LABELS)
    allowed = CONDUCTOR_FIELDS.get(task, ())
    for iid, r in rows.items():
        extra = sorted(set(r) - {"item_id", "note", *allowed})
        if extra or not set(r) & set(allowed):
            raise SystemExit(f"{set_dir / V2_LABELS}: conductor override {iid}: fields {sorted(set(r) - {'item_id', 'note'})}"
                             f" (allowed for {task}: {list(allowed)})")
        if iid not in items:
            raise SystemExit(f"{set_dir / V2_LABELS}: conductor override for {iid}, not in items.jsonl")
    return rows


def _apply_conductor(task: str, row: Row, over: Row | None) -> None:
    """Apply override ``over`` to ``row["label"]`` in place, record it, re-derive the questions."""
    if over is None:
        return
    lab = row["label"]
    fields = {f: {"from": lab.get(f), "to": over[f]} for f in CONDUCTOR_FIELDS[task] if f in over}
    lab.update({f: v["to"] for f, v in fields.items()})
    row["conductor_override"] = {"note": over.get("note"), "fields": fields}
    _with_questions(task, lab)


def label_part_v2(task: str, lab: Row) -> Row:
    """A v2 label's gold part: triage ``{verdict, fetch, scope}``, screen ``{verdict, buckets,
    scope, sr_category, sr_agenda}``; absent keys are None."""
    if task == "triage":
        return {"verdict": lab["verdict"], "fetch": lab["verdict"] != "not_relevant", "scope": lab.get("scope")}
    if task == "screen":
        return {"verdict": lab["verdict"], "buckets": sorted(lab.get("buckets") or []), "scope": lab.get("scope"),
                "sr_category": lab.get("sr_category"), "sr_agenda": lab.get("sr_agenda")}
    raise SystemExit(f"--version v2: task {task!r} has no v2 labels (triage, screen)")


def differing_fields(task: str, a: Row, b: Row) -> list[str]:
    return [f for f in V2_FIELDS[task] if a.get(f) != b.get(f)]


def _v2_inputs(task: str, set_dir: Path) -> tuple[dict[str, Row], dict[str, Row], dict[str, Row], dict[str, Row],
                                                    dict[str, Row]]:
    """(items, opus, codex, adjudication, reference) of a v2 set."""
    if task not in V2_FIELDS:
        raise SystemExit(f"--version v2: task {task!r} has no v2 labels (triage, screen)")
    items = {i["item_id"]: i for i in read_jsonl(_items_file(set_dir))}
    a, b = labels_of(set_dir, "opus", V2_LABELS), labels_of(set_dir, "codex", V2_LABELS)
    adj_path = set_dir / V2_LABELS / "adjudication.jsonl"
    adj = {r["item_id"]: r for r in read_jsonl(adj_path)} if adj_path.exists() else {}
    ref = labels_of(set_dir, "tomas", V2_LABELS)
    for who, labs in (("opus", a), ("codex", b), ("adjudication", adj), ("tomas", ref)):
        stray = sorted(set(labs) - set(items))
        if stray:
            raise SystemExit(f"{set_dir / V2_LABELS}: {who} labels for items not in items.jsonl: {stray[:5]}")
    return items, a, b, adj, ref


def _field_agreement(pairs: list[tuple[Any, Any]]) -> Row:
    return {"n": len(pairs), "agree": sum(x == y for x, y in pairs),
            "raw": sum(x == y for x, y in pairs) / len(pairs) if pairs else None, "kappa": kappa(pairs)}


def _transitions(pairs: list[tuple[str, str]]) -> Row:
    """v1 → v2 verdict transitions: counts per ``"v1->v2"`` and the number of changed verdicts."""
    return {"n": len(pairs), "flips": sum(x != y for x, y in pairs),
            "table": {f"{x}->{y}": n for (x, y), n in sorted(Counter(pairs).items())}}


def _would_be(task: str, a: Row, b: Row, adj: Row | None) -> Row | None:
    """The gold label part without a reference: the agreed label (opus's part), or the
    adjudication's on a disagreement; None while a disagreement waits for its adjudication."""
    la, lb = label_part_v2(task, a), label_part_v2(task, b)
    if not differing_fields(task, la, lb):
        return la
    return label_part_v2(task, adj) if adj is not None else None


def merge_v2(task: str, set_dir: Path) -> Row:
    """Agreement of the two v2 labelers (raw and κ on verdict, scope and, screen, sr_category over
    items both kept), disagreement counts per field, v1 → v2 verdict transitions (per labeler and
    for the agreed items) on items with v1 gold, and each labeler's (and the would-be gold's)
    agreement with the reference labels; writes ``labels-v2/disagreements.jsonl``."""
    items, a, b, adj, ref = _v2_inputs(task, set_dir)
    both = sorted(set(a) & set(b))
    la = {i: label_part_v2(task, a[i]) for i in a}
    lb = {i: label_part_v2(task, b[i]) for i in b}
    diff = {i: differing_fields(task, la[i], lb[i]) for i in both}
    dis = [i for i in both if diff[i]]
    rep: Row = {"version": "v2", "items": len(items), "opus": len(a), "codex": len(b), "both": len(both),
                "only_opus": sorted(set(a) - set(b)), "only_codex": sorted(set(b) - set(a)),
                "unlabelled_carried": len(set(items) - set(a) - set(b)),
                "agree": len(both) - len(dis), "disagree": len(dis),
                "disagree_by_field": {f: sum(f in diff[i] for i in both) for f in V2_FIELDS[task]}}
    rep["verdict"] = _field_agreement([(la[i]["verdict"], lb[i]["verdict"]) for i in both])
    rep["verdict"]["confusion"] = {f"{x}|{y}": n for (x, y), n in
                                   sorted(Counter((la[i]["verdict"], lb[i]["verdict"]) for i in both).items())}
    rep["scope"] = _field_agreement([(la[i]["scope"], lb[i]["scope"]) for i in both])
    if task == "screen":
        kept = [i for i in both if la[i]["verdict"] in KEPT[task] and lb[i]["verdict"] in KEPT[task]]
        rep["sr_category_both_kept"] = _field_agreement([(la[i]["sr_category"], lb[i]["sr_category"]) for i in kept])
    gold_v1_path = set_dir / "gold.jsonl"
    v1 = {g["item_id"]: g for g in read_jsonl(gold_v1_path)} if gold_v1_path.exists() else {}
    rep["v1_to_v2"] = {
        "opus": _transitions([(v1[i]["label"]["verdict"], la[i]["verdict"]) for i in sorted(a) if i in v1]),
        "codex": _transitions([(v1[i]["label"]["verdict"], lb[i]["verdict"]) for i in sorted(b) if i in v1]),
        "agreed": _transitions([(v1[i]["label"]["verdict"], la[i]["verdict"]) for i in both
                                if i in v1 and not diff[i]]),
    }
    if ref:
        # Agreement with the reference on each field it sets (triage verdict, fetch; screen verdict
        # and the derived per-question verdicts).
        rparts = {i: _reference_part(task, r) for i, r in ref.items()}

        def vs_ref(parts: dict[str, Row], ids: list[str]) -> Row:
            return {f: _field_agreement([(parts[i][f], rparts[i][f]) for i in ids]) for f in REFERENCE_FIELDS[task]}

        out: Row = {"items": len(ref)}
        for name, labs in (("opus", la), ("codex", lb)):
            ids = [i for i in sorted(ref) if i in labs]
            out[name] = vs_ref({i: _with_questions(task, dict(labs[i])) for i in ids}, ids)
        wb = {i: _would_be(task, a[i], b[i], adj.get(i)) for i in sorted(ref) if i in a and i in b}
        ids = [i for i, w in wb.items() if w is not None]
        out["would_be_gold"] = {**vs_ref({i: _with_questions(task, dict(wb[i])) for i in ids}, ids),  # type: ignore[arg-type]
                                "pending_adjudication": sorted(i for i, w in wb.items() if w is None)}
        out["without_v2_labels"] = sorted(i for i in ref if i not in a or i not in b)
        rep["reference_tomas"] = out
    write_jsonl(set_dir / V2_LABELS / "disagreements.jsonl",
                [{"item_id": i, "input": items[i]["input"], "fields": diff[i], "labeler_A": a[i], "labeler_B": b[i]}
                 for i in dis])
    return rep


def _labeler_entry(who: str, task: str, lab: Row) -> Row:
    return {"who": who, "label": label_part_v2(task, lab), "confidence": lab.get("confidence"),
            "rationale": lab.get("rationale"), "quote": lab.get("quote", "")}


def _carried_v1(task: str, item: Row, g1: Row | None) -> Row:
    if g1 is None:
        raise SystemExit(f"{item['item_id']}: no v2 labels and no v1 gold row to carry")
    label = {**g1["label"], "scope": None}
    if task == "screen":
        label.update(sr_category=None, sr_agenda=None)
    return {**g1, "label": _with_questions(task, label), "carried_from": "v1"}


def _gold_row_v2(task: str, item: Row, a: Row | None, b: Row | None, adj: Row | None, ref: Row | None,
                 g1: Row | None, over: Row | None = None) -> Row:
    iid = item["item_id"]
    if a is None and b is None:
        row = _carried_v1(task, item, g1)
        _apply_conductor(task, row, over)
        if ref is not None:
            row["label_without_reference"] = dict(row["label"])
            row["label"] = {**row["label"], **_reference_part(task, ref)}
            row["reference"] = "tomas"
            row["reference_note"] = ref.get("note")
        return row
    if a is None or b is None:
        raise SystemExit(f"{iid}: v2 labels from one labeler only ({'opus' if a is not None else 'codex'})")
    text = input_text(item)
    la, lb = label_part_v2(task, a), label_part_v2(task, b)
    fields = differing_fields(task, la, lb)
    labelers = [_labeler_entry(WHO_A, task, a), _labeler_entry(WHO_B, task, b)]
    # A referenced item needs the adjudicator only when the scope fields differ (the reference
    # decides verdict and fetch).
    needs_adj = [f for f in fields if not (ref is not None and f == "verdict")]
    if adj is not None and fields:
        labelers.append(_labeler_entry("adjudicator", task, adj))
    if not fields:
        src, label = a, dict(la)
        conf = round(((a.get("confidence") or 0) + (b.get("confidence") or 0)) / 2, 3)
        quote = next((q for q in (a.get("quote", ""), b.get("quote", "")) if verbatim(q, text)), "")
        note = None
    elif adj is None:
        if needs_adj:
            raise SystemExit(f"{iid}: disagreement on {needs_adj} without adjudication")
        src, label = a, dict(la)  # verdict-only disagreement on a referenced item: the reference decides it
        conf, quote, note = None, next((q for q in (a.get("quote", ""), b.get("quote", "")) if verbatim(q, text)), ""), None
    else:
        src, label = adj, label_part_v2(task, adj)
        conf, quote, note = adj.get("confidence"), adj.get("quote", ""), adj.get("adjudication_note")
    row: Row = {"item_id": iid, "doc_id": item["doc_id"], "label": label, "confidence": conf,
                "rationale": src.get("rationale"), "quote": quote, "labelers": labelers,
                "agreement": not fields, "contested": bool(fields), "disagreement_fields": fields,
                "adjudication_note": note}
    if task == "screen":
        given = adj.get("sr_agenda") if adj is not None and fields else None
        label["sr_agenda"] = given if given else (la["sr_agenda"] if la["sr_agenda"] == lb["sr_agenda"] else None)
        row["sr_agenda_labels"] = {WHO_A: la["sr_agenda"], WHO_B: lb["sr_agenda"]}
    _with_questions(task, label)
    _apply_conductor(task, row, over)
    if ref is not None:
        row["label_without_reference"] = None if (fields and adj is None) else dict(label)
        label.update(_reference_part(task, ref))
        row["reference"] = "tomas"
        row["reference_note"] = ref.get("note")
    return _mark_scope_conflict(task, row)


def _mark_scope_conflict(task: str, row: Row) -> Row:
    """Flag a referenced row whose reference verdict and labelers' scope disagree on in/out (the
    rubric's ``out`` ⇔ negative verdict); counted by :func:`finalize_v2`, not repaired."""
    lab = row["label"]
    if row.get("reference") and lab.get("scope") is not None:
        row["reference_scope_conflict"] = (lab["verdict"] in KEPT[task]) == (lab["scope"] == "out")
    return row


def finalize_v2(task: str, set_dir: Path) -> Row:
    """``gold-v2.jsonl`` (module docstring): per item of ``items.jsonl`` its v2 gold row, or its
    v1 row carried over; reports contested counts per field, base rates, scope counts and the
    v1 → v2 verdict transitions on relabeled items with v1 gold."""
    items, a, b, adj, ref = _v2_inputs(task, set_dir)
    gold_v1_path = set_dir / "gold.jsonl"
    v1 = {g["item_id"]: g for g in read_jsonl(gold_v1_path)} if gold_v1_path.exists() else {}
    over = _conductor_overrides(task, set_dir, items)
    gold = [_gold_row_v2(task, it, a.get(i), b.get(i), adj.get(i), ref.get(i), v1.get(i), over.get(i))
            for i, it in items.items()]
    write_jsonl(set_dir / V2_GOLD, gold)
    relabeled = [g for g in gold if "carried_from" not in g]
    base: Counter[str] = Counter(f"{items[g['item_id']]['split']}:{g['label']['verdict']}" for g in gold)
    return {"version": "v2", "gold": len(gold), "relabeled": len(relabeled),
            "carried_from_v1": len(gold) - len(relabeled), "new_items": sum(g["item_id"] not in v1 for g in relabeled),
            "contested": sum(bool(g.get("contested")) for g in relabeled),
            "contested_by_field": {f: sum(f in (g.get("disagreement_fields") or []) for g in relabeled)
                                   for f in V2_FIELDS[task]},
            "reference_tomas": sum(g.get("reference") == "tomas" for g in gold),
            "conductor_overrides": {g["item_id"]: g["conductor_override"]["fields"] for g in gold
                                    if g.get("conductor_override")},
            "reference_scope_conflicts": sorted(g["item_id"] for g in gold if g.get("reference_scope_conflict")),
            "reference_overrode_verdict": sum(
                g.get("reference") == "tomas" and (g.get("label_without_reference") or {}).get("verdict")
                not in (None, g["label"]["verdict"]) for g in gold),
            "scope": dict(sorted(Counter(str(g["label"]["scope"]) for g in gold).items())),
            "base_rates": dict(sorted(base.items())),
            "v1_to_v2": _transitions([(v1[g["item_id"]]["label"]["verdict"], g["label"]["verdict"])
                                      for g in relabeled if g["item_id"] in v1])}


def calibration(task: str, set_dir: Path, out: Path, n: int = 30, n_contested: int = 20,
                seed: str = "track-e-calibration") -> Row:
    """``n_contested`` contested items (all of them when fewer) plus seeded random agreed items up to ``n``, as one
    readable markdown file with a blank per item for a human label."""
    items = {i["item_id"]: i for i in read_jsonl(_items_file(set_dir))}
    gold = read_jsonl(set_dir / "gold.jsonl")
    rng = random.Random(seed)
    contested = [g for g in gold if g["contested"]]
    rest = [g for g in gold if not g["contested"]]
    rng.shuffle(contested)
    rng.shuffle(rest)
    take_c = contested[:n_contested]
    pick = sorted(take_c + rest[:max(0, n - len(take_c))], key=lambda g: g["item_id"])
    lines = [f"# Calibration slice: {task} set {set_dir.name}", "",
             f"{len(pick)} items ({sum(g['contested'] for g in pick)} contested, the rest random). For each item read "
             f"the input as the model sees it, the two labelers' calls, and write your label after **Tomáš:** "
             f"(`relevant` / `borderline` / `not_relevant`{' / `irrelevant`' if task == 'screen' else ''}, "
             f"optionally a note). Rubric: `evals/rubrics/{task}.md`.", ""]
    for g in pick:
        it = items[g["item_id"]]
        text = input_text(it)
        lines += [f"## {g['item_id']} ({it['stratum'].get('channel')}, {it['split']}"
                  f"{', contested' if g['contested'] else ''})", "", "```text", text[:3000]
                  + ("\n…[cut for this slice]" if len(text) > 3000 else ""), "```", ""]
        for lab in g["labelers"]:
            lines.append(f"- **{lab['who']}**: `{json.dumps(lab['label'], ensure_ascii=False)}` "
                         f"(conf {lab['confidence']}) — {lab['rationale']}")
        if g["contested"]:
            lines.append(f"- **adjudicator → gold**: `{json.dumps(g['label'], ensure_ascii=False)}` — "
                         f"{g['adjudication_note'] or g['rationale']}")
        lines += ["", "**Tomáš:** ", "", "---", ""]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), "utf-8")
    return {"path": str(out), "items": len(pick), "contested": sum(g["contested"] for g in pick)}


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m stack.evals.gold")
    ap.add_argument("action", choices=["merge", "finalize", "calibration"])
    ap.add_argument("task", choices=["triage", "screen", "affiliation"])
    ap.add_argument("--set", required=True)
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--n-contested", type=int, default=20)
    ap.add_argument("--version", choices=["v1", "v2"], default="v1",
                    help="v1: labels/ → gold.jsonl; v2: labels-v2/ → gold-v2.jsonl (merge, finalize)")
    args = ap.parse_args()
    set_dir = ROOT / args.task / args.set
    if args.action == "merge":
        rep = merge_v2(args.task, set_dir) if args.version == "v2" else merge(args.task, set_dir)
    elif args.action == "finalize":
        rep = finalize_v2(args.task, set_dir) if args.version == "v2" else finalize(args.task, set_dir)
    else:
        rep = calibration(args.task, set_dir, ROOT / "calibration" / f"{args.task}-{args.set}.md", n=args.n, n_contested=args.n_contested)
    print(json.dumps(rep, indent=1, default=str))


if __name__ == "__main__":
    main()


def _items_file(set_dir: Path) -> Path:
    """The set's ``items.jsonl``, else the eval-text cache's (:func:`stack.evals.freeze.items_file`, D795)."""
    from stack.evals.freeze import items_file

    return items_file(Path(set_dir))
