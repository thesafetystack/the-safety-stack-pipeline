"""Derive (or check) the technique_staged eval set 2026-10-18c, a gold revision of 2026-10-18b (gold v2 with the
Astra reconciliation completed for all 120 items and a second verification round; no LLM call, no network).

Run from the repo root: ``uv run --no-sync python evals/technique_staged/2026-10-18c/scripts/derive.py [--write]``.

Inputs, all in the repository: ``evals/technique_staged/2026-10-18b`` (``items.jsonl``, ``texts/``, ``manifest.json``,
``adjudication/verify-sample.json``, ``adjudication/pre-verification.jsonl``; never written) and this set's
``adjudication/`` as ``scripts/stage.py`` staged it (``final.jsonl``, ``astra-reconciler.jsonl``,
``reconciliation-rounds.json``, ``verification2.jsonl``, ``estimates2.json``).

* ``items.jsonl`` and ``texts/``: byte copies of 2026-10-18b's, whose ``items.jsonl`` must hash to
  :data:`ITEMS_SHA256` (refused otherwise), so every 18b run scores here (``--set-dir``).
* ``gold.jsonl``: 2026-10-18b's row and item shape (``freeze.py`` there): ``sources`` X -> ``A``, Y -> ``B``,
  ``reconciler``, ``opus``, ``verification`` kept; ``dont_care`` and the row's ``verification`` note kept; ``note`` =
  the row's ``decision``, else its ``reason``; ``adjudicator`` ``astra-reconciler`` on every item (refused if a label
  names another); ``adjudication`` {doc_note, dropped, disagreement_note, reconciliation_round[, verification]};
  ``labelers`` astra-L1, astra-L2, plus opus, sonnet-adjudicator and codex-sol-adjudicator on the 30 verified items.
  Refused: a slug or level alternative outside the 234-entry taxonomy, a ``sources`` entry outside :data:`SOURCES`,
  a verified item without its ``verification`` record or the reverse. Reported in ``manifest.json`` ``gold.checks``:
  quotes not found verbatim (row ``quote_check``), roles above ``mentioned``, level alternatives not linked to the
  row's slug, alt_slugs outside the taxonomy, unknown ``doc_kind``.
* ``manifest.json``: 18b's shape (counts, checks, its ``frozen_with`` and ``sampling``) with ``derivation``: the
  reconciliation rounds, the merge-rule -> Astra change on the round-2 items (18b's pre-verification gold vs
  ``astra-reconciler.jsonl``), the round-2 verification counts, and the gold change against 18b.

Without ``--write`` it rebuilds everything in memory and compares it with the files byte for byte (exit 1 on any
difference, naming it). With ``--write`` it copies ``items.jsonl`` and ``texts/`` from 18b and writes ``gold.jsonl``
and ``manifest.json``.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path.cwd()
SRC = ROOT / "evals/technique_staged/2026-10-18b"
OUT = ROOT / "evals/technique_staged/2026-10-18c"
SET_ID = "2026-10-18c"
ITEMS_SHA256 = "fa4912e8a1a0a7dca03451de01c57d9504579c3cbdf0cdfeaea5e2ec4559ce78"
TAXONOMY = "taxonomy/taxonomy-v0.9-234.yaml"
SOURCES = {"X": "A", "Y": "B", "reconciler": "reconciler", "opus": "opus", "verification": "verification"}
LABELERS = ["astra-L1", "astra-L2"]
VERIFIERS = ["opus", "sonnet-adjudicator", "codex-sol-adjudicator"]
ADJUDICATOR = "astra-reconciler"
DOC_KINDS = ("paper", "blogpost", "commentary", "model_card", "system_card", "technical_report", "eval_report", "news",
             "policy", "other")


class DeriveError(Exception):
    """A digest mismatch or a gold row that does not validate: nothing is written."""


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text("utf-8").splitlines() if x.strip()]


def dumps(rows: list[dict]) -> str:
    return "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows)


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def structure() -> tuple[set[str], frozenset[str], frozenset[tuple[str, str]]]:
    sys.path.insert(0, str(ROOT))
    from stack.evals.technique_staged import taxonomy_structure

    kind, groups, links = taxonomy_structure(ROOT / TAXONOMY)
    return set(kind) | groups, groups, links


def build_gold(out: Path, items: list[dict], texts: dict[str, str]) -> tuple[list[dict], dict]:
    slugs, _groups, links = structure()
    rec = {r["item_id"]: r for r in read_jsonl(out / "adjudication" / "final.jsonl")}
    rounds = json.loads((out / "adjudication" / "reconciliation-rounds.json").read_text("utf-8"))
    round_of = {iid: int(r) for r, info in rounds.items() for iid in info["items"]}
    verified = {x["item_id"] for x in json.loads((SRC / "adjudication" / "verify-sample.json").read_text("utf-8"))}
    bad: list[str] = []
    checks: dict[str, list[str]] = {"quote_case_insensitive": [], "quote_not_found": [], "role_above_mentioned": [],
                                    "level_alternative_not_linked": [], "alt_slug_unknown": [], "doc_kind_unknown": []}
    gold = []
    for it in items:
        iid = it["item_id"]
        r = rec.get(iid)
        if r is None:
            raise DeriveError(f"adjudication/final.jsonl has no {iid}")
        text = norm(texts[iid])
        if r.get("doc_kind") not in DOC_KINDS:
            checks["doc_kind_unknown"].append(f"{iid}:{r.get('doc_kind')}")
        if r.get("adjudicator", ADJUDICATOR) != ADJUDICATOR:
            bad.append(f"{iid} (adjudicator {r.get('adjudicator')!r})")
        if (iid in verified) != (r.get("verification") is not None):
            bad.append(f"{iid} (verification record {r.get('verification') is not None}, sample {iid in verified})")
        techniques = []
        for t in r["techniques"]:
            slug = str(t["slug"])
            where = f"{iid}:{slug}"
            if slug not in slugs:
                bad.append(f"{where} (slug)")
            alts = list(t.get("level_alternatives") or [])
            for x in alts:
                if x not in slugs:
                    bad.append(f"{where} (level alternative {x})")
                elif (slug, x) not in links and (x, slug) not in links:
                    checks["level_alternative_not_linked"].append(f"{where}->{x}")
            checks["alt_slug_unknown"] += [f"{where}->{x}" for x in t.get("alt_slugs") or [] if x not in slugs]
            unknown = [s for s in t.get("sources") or [] if s not in SOURCES]
            if unknown or not t.get("sources"):
                bad.append(f"{where} (sources {t.get('sources')})")
            roles = {str(k): int(v) for k, v in (t.get("roles") or {}).items()}
            if any(v > roles.get("mentioned", 0) for k, v in roles.items() if k != "mentioned"):
                checks["role_above_mentioned"].append(where)
            row = {"slug": slug, "roles": roles, "quote": t.get("quote"), "confidence": t.get("confidence"),
                   "sources": [SOURCES[s] for s in t.get("sources") or [] if s in SOURCES],
                   "alt_slugs": list(t.get("alt_slugs") or []), "level_alternatives": alts,
                   "implemented_with": list(t.get("implemented_with") or []),
                   "note": t.get("decision") or t.get("reason")}
            if t.get("dont_care"):
                row["dont_care"] = True
            if t.get("verification") is not None:
                row["verification"] = t["verification"]
            q = norm(str(t.get("quote") or ""))
            if q and q not in text:
                row["quote_check"] = "case_insensitive" if q.lower() in text.lower() else "not_found"
                checks[f"quote_{row['quote_check']}"].append(where)
            techniques.append(row)
        adj = {"doc_note": r.get("doc_note"), "dropped": list(r.get("dropped") or []),
               "disagreement_note": r.get("disagreement_note"), "reconciliation_round": round_of[iid]}
        if r.get("verification") is not None:
            adj["verification"] = r["verification"]
        gold.append({"item_id": iid, "doc_id": it["doc_id"], "doc_kind": r.get("doc_kind"),
                     "label": {"techniques": techniques, "extra_techniques": list(r.get("extra_techniques") or []),
                               "menu_gaps": list(r.get("menu_gaps") or [])},
                     "labelers": LABELERS + (VERIFIERS if iid in verified else []),
                     "adjudicator": ADJUDICATOR, "adjudication": adj})
    if bad:
        raise DeriveError(f"gold not valid against {TAXONOMY} / the label schema: {', '.join(bad)}")
    return gold, checks


def merge_to_astra(out: Path, groups: frozenset[str], items: list[str]) -> dict:
    """18b's pre-verification gold (the merge rule on these items) vs the Astra reconciliation."""
    pre = {r["item_id"]: r for r in read_jsonl(SRC / "adjudication" / "pre-verification.jsonl")}
    ast = {r["item_id"]: r for r in read_jsonl(out / "adjudication" / "astra-reconciler.jsonl")}
    c: Counter[str] = Counter()
    for iid in items:
        m = {t["slug"]: t for t in pre[iid]["techniques"]}
        a = {t["slug"]: t for t in ast[iid]["techniques"]}
        c["rows_removed"] += len(set(m) - set(a))
        c["rows_added"] += len(set(a) - set(m))
        for s in set(m) & set(a):
            c["confidence_changed"] += m[s]["confidence"] != a[s]["confidence"]
            rm = {k: v for k, v in m[s]["roles"].items() if v}
            ra = {k: v for k, v in a[s]["roles"].items() if v}
            c["rows_roles_changed"] += rm != ra
            c["role_cells_changed"] += sum(rm.get(k, 0) != ra.get(k, 0) for k in set(rm) | set(ra))
        ms, as_ = {s for s in m if s not in groups}, {s for s in a if s not in groups}
        c["tp"] += len(ms & as_)
        c["fp"] += len(ms - as_)
        c["fn"] += len(as_ - ms)
    f1 = round(2 * c["tp"] / (2 * c["tp"] + c["fp"] + c["fn"]), 3)
    return {"items": len(items), "group_nodes_in_row_counts": True,
            **{k: c[k] for k in ("rows_removed", "rows_added", "confidence_changed", "rows_roles_changed",
                                 "role_cells_changed")},
            "slug_f1_merge_vs_astra_no_group_nodes": f1}


def gold_change(gold: list[dict]) -> dict:
    """Slug rows added, removed and kept against 18b's gold (dont_care flags counted apart)."""
    old = {(g["item_id"], t["slug"]): t for g in read_jsonl(SRC / "gold.jsonl") for t in g["label"]["techniques"]}
    new = {(g["item_id"], t["slug"]): t for g in gold for t in g["label"]["techniques"]}
    return {"rows_added": len(set(new) - set(old)), "rows_removed": len(set(old) - set(new)),
            "rows_kept": len(set(new) & set(old)),
            "dont_care_18b": sum(bool(t.get("dont_care")) for t in old.values()),
            "dont_care_18c": sum(bool(t.get("dont_care")) for t in new.values())}


def manifest(items_text: str, items: list[dict], gold_text: str, gold: list[dict], checks: dict, out: Path) -> dict:
    _slugs, groups, _links = structure()
    src = json.loads((SRC / "manifest.json").read_text("utf-8"))
    rows = [t for g in gold for t in g["label"]["techniques"]]
    scored = [t for t in rows if t["slug"] not in groups and not t.get("dont_care")]
    sources = Counter("+".join(t["sources"]) for t in rows)
    rounds = json.loads((out / "adjudication" / "reconciliation-rounds.json").read_text("utf-8"))
    v2 = read_jsonl(out / "adjudication" / "verification2.jsonl")
    reasons = Counter(str(d["to_codex"]) for v in v2 for d in v["disputes"] if d["to_codex"])
    answers = Counter(f"{d['by']}:{x['answer']}" for v in v2 for d, x in
                      zip([d for d in v["disputes"] if d["to_codex"]], v["verdicts"], strict=True))
    estimates = json.loads((out / "adjudication" / "estimates2.json").read_text("utf-8"))
    return {
        "task": "technique_staged", "set_id": SET_ID,
        "source": ("evals/technique_staged/2026-10-18b (items.jsonl and texts byte-identical; gold derived by "
                   "scripts/derive.py from adjudication/, staged by scripts/stage.py: the Astra reconciliation of all "
                   "120 items with two verification rounds applied)"),
        "items": len(items), "items_sha256": sha(items_text.encode("utf-8")), "splits": src["splits"],
        "strata": src["strata"],
        "gold": {"file": "gold.jsonl", "sha256": sha(gold_text.encode("utf-8")),
                 "from": "adjudication/final.jsonl", "items": len(gold), "rows": len(rows),
                 "dont_care_rows": sum(bool(t.get("dont_care")) for t in rows),
                 "group_node_rows": sum(t["slug"] in groups and not t.get("dont_care") for t in rows),
                 "scored_rows": len(scored), "scored_high": sum(t["confidence"] == "high" for t in scored),
                 "scored_both_labellers": sum({"A", "B"} <= set(t["sources"]) for t in scored),
                 "level_alternative_rows": sum(bool(t["level_alternatives"]) for t in rows),
                 "verified_items": sum("opus" in g["labelers"] for g in gold),
                 "adjudicators": dict(Counter(g["adjudicator"] for g in gold)),
                 "row_sources": dict(sorted(sources.items())),
                 "extra_techniques": sum(len(g["label"]["extra_techniques"]) for g in gold),
                 "menu_gaps": sum(len(g["label"]["menu_gaps"]) for g in gold), "checks": checks},
        "derivation": {
            "from_set": "2026-10-18b", "from_gold_sha256": sha((SRC / "gold.jsonl").read_bytes()),
            "reconciliation_rounds": {r: {k: v for k, v in info.items() if k != "items"} | {"items": len(info["items"])}
                                      for r, info in rounds.items()},
            "merge_to_astra_round2": merge_to_astra(out, groups, rounds["2"]["items"]),
            "verification_round2": {"items": len(v2), "disputed_slugs": sum(len(v["disputes"]) for v in v2),
                                    "to_codex": sum(reasons.values()), "to_codex_why": dict(sorted(reasons.items())),
                                    "codex_answers_by_side": dict(sorted(answers.items())),
                                    "kept_sonnet": sum(not d["to_codex"] for v in v2 for d in v["disputes"])},
            "gold_vs_18b": gold_change(gold)},
        "frozen_with": src["frozen_with"],
        "sampling": src["sampling"],
        "verification": {"sample": "../2026-10-18b/adjudication/verify-sample.json",
                         "verifier": "opus (blind labels, ../2026-10-18b/adjudication/opus.jsonl)",
                         "adjudicators": "sonnet (round 1, ../2026-10-18b/adjudication/verification.jsonl); codex "
                                         "gpt-6-sol (round 2, adjudication/verification2.jsonl)",
                         "estimates": estimates},
    }


def build(out: Path = OUT) -> tuple[dict[str, str], dict[str, bytes]]:
    items_bytes = (SRC / "items.jsonl").read_bytes()
    if sha(items_bytes) != ITEMS_SHA256:
        raise DeriveError(f"2026-10-18b items.jsonl hashes to {sha(items_bytes)[:12]}, not {ITEMS_SHA256[:12]}")
    items_text = items_bytes.decode("utf-8")
    items = read_jsonl(SRC / "items.jsonl")
    blobs = {f"texts/{it['item_id']}.txt.gz": text_file(SRC, it["item_id"]).read_bytes() for it in items}
    texts = {}
    for it in items:
        text = gzip.decompress(blobs[f"texts/{it['item_id']}.txt.gz"]).decode("utf-8")
        if sha(text.encode("utf-8")) != it["input"]["full_text_digest"]:
            raise DeriveError(f"{it['item_id']}: 2026-10-18b text does not hash to its full_text_digest")
        texts[it["item_id"]] = text
    gold, checks = build_gold(out, items, texts)
    gold_text = dumps(gold)
    man = manifest(items_text, items, gold_text, gold, checks, out)
    files = {"items.jsonl": items_text, "gold.jsonl": gold_text,
             "manifest.json": json.dumps(man, indent=2, ensure_ascii=False) + "\n"}
    return files, blobs


def text_file(set_dir: Path, iid: str) -> Path:
    """``iid``'s whole text: the set's ``texts/`` file, else the eval-text cache's (the texts are not in the
    repository, D790; ``tools/fetch_eval_texts.py`` fills the cache)."""
    from stack.evals.freeze import find_text, missing_text_message

    p = find_text(set_dir, iid)
    if p is None:
        raise DeriveError(missing_text_message(set_dir, iid))
    return p


def text_bytes(set_dir: Path, rel: str) -> bytes | None:
    """The bytes of ``texts/<iid>.txt.gz`` of ``set_dir`` (its own file, else the cache's), None when neither."""
    from stack.evals.freeze import find_text

    p = find_text(set_dir, rel.removeprefix("texts/").removesuffix(".txt.gz"))
    return p.read_bytes() if p is not None else None


def main(write: bool, out: Path = OUT) -> int:
    try:
        files, blobs = build(out)
    except DeriveError as exc:
        print(f"refused: {exc}")
        return 1
    if write:
        (out / "texts").mkdir(parents=True, exist_ok=True)
        for rel, data in blobs.items():
            (out / rel).write_bytes(data)
        for rel, text in files.items():
            (out / rel).write_text(text, encoding="utf-8")
        print(f"wrote {', '.join(files)} and {len(blobs)} texts")
        return 0
    differ = [rel for rel, text in files.items()
              if not (out / rel).is_file() or (out / rel).read_text("utf-8") != text]
    differ += [rel for rel, data in blobs.items() if text_bytes(out, rel) != data]
    extra = sorted(p.name for p in (out / "texts").glob("*") if f"texts/{p.name}" not in blobs)
    differ += [f"texts/{x} (not in 2026-10-18b)" for x in extra]
    if differ:
        print(f"differs: {', '.join(differ)}")
        return 1
    print(f"reproduced: {', '.join(files)} byte for byte, {len(blobs)} texts identical")
    return 0


if __name__ == "__main__":
    raise SystemExit(main("--write" in sys.argv[1:]))
