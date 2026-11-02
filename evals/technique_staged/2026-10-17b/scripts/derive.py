"""Derive (or check) the technique_staged eval set 2026-10-17b from 2026-10-17a and the blind gold audit (no LLM
call, no network).

Run from the repo root: ``uv run --no-sync python evals/technique_staged/2026-10-17b/scripts/derive.py [--write]``.

Inputs, all in the repository: ``evals/technique_staged/2026-10-17a`` (``gold.jsonl``, ``items.jsonl``,
``texts/``, ``manifest.json``; never written), and this set's ``adjudication/audit/``: ``key.jsonl`` (audit id ->
pool, item, slug, judge, 17a gold confidence and quote, the arms that predicted the slug), ``out-astra.txt`` /
``out-sol.txt`` (the two judges' answers: one JSON list ``{id, verdict yes|borderline|no, quote, reason}``) and
``arm-roles.jsonl`` (``scripts/arm_roles.py``: the roles the audited arm gave each pool-B slug, per repeat).

Rules (techpipe D610), per audit item:

* pool ``A`` (a 17a gold row no run predicted in any repeat): ``no`` -> the row is removed; ``yes`` -> kept
  unchanged; ``borderline`` -> the row gets ``dont_care: true`` and an ``audit`` record (D611: out of every
  denominator, a prediction of the slug on that document neither a true nor a false positive);
* pools ``B-staged`` / ``B-single`` (a slug the audited arm predicted that 17a's gold lacks): ``yes`` -> a gold row is
  added; ``borderline`` -> the same row with ``dont_care: true``; ``no`` -> nothing (it stays a false positive).
  Added row: ``slug``, ``roles`` = the audited arm's modal roles over its repeats (the most frequent roles dict; a
  tie goes to the higher top strength, then the earlier repeat; ``{"used": 1}`` if the arm gave none, which this
  audit never needs), ``quote`` = the judge's quote, ``confidence: med``, ``sources: ["audit"]``,
  ``source: blind-audit-2026-10-17``, ``alt_slugs`` / ``implemented_with`` empty, ``note`` = judge, verdict and
  reason, ``audit`` = {id, pool, judge, verdict}; appended after the item's 17a rows in audit-id order;
* pool ``C`` (controls, predicted by every run): no change.

Every changed item also gets ``audit_actions`` (one ``{id, slug, action}`` per change). Refuses when an audit item
does not match the 17a gold (a pool-A row missing or duplicated, a pool-B slug already in the gold or not a v0.9
technique), when a verdict is missing or malformed, or when 17b's ``items.jsonl`` / ``texts/`` differ from 17a's.
Judge quotes not found verbatim (whitespace-normalised) in the document text are listed in the manifest
(``derivation.quotes_not_found``), not refused.

Without ``--write`` it rebuilds ``gold.jsonl`` and ``manifest.json`` in memory and compares them, ``items.jsonl`` and
``texts/`` with the files byte for byte (exit 1 on any difference, naming it). With ``--write`` it copies
``items.jsonl`` and ``texts/`` from 17a and writes ``gold.jsonl`` and ``manifest.json``.
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
SRC = ROOT / "evals/technique_staged/2026-10-17a"
OUT = ROOT / "evals/technique_staged/2026-10-17b"
SET_ID = "2026-10-17b"
SOURCE = "blind-audit-2026-10-17"
JUDGES = ("astra", "sol")
VERDICTS = ("yes", "borderline", "no")
POOLS = ("A", "B-staged", "B-single", "C")
BIAS = ("Only false positives of the two arms sampled in the audit (staged gpt-6-luna with prompt set "
        "technique-staged-v1, single gpt-6-luna) could be credited: 17b raises those arms' precision slightly "
        "relative to any other arm, whose distinct false positives were never audited. Pool A removals and "
        "dont_care rows apply to every arm alike.")


class DeriveError(Exception):
    """An audit item that does not fit the 17a gold, a malformed verdict, or items / texts that differ."""


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text("utf-8").splitlines() if x.strip()]


def dumps(rows: list[dict]) -> str:
    return "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verdicts(audit: Path) -> dict[str, dict]:
    """Audit id -> the judge's answer (the one ``json`` block of each judge's output)."""
    out: dict[str, dict] = {}
    for judge in JUDGES:
        text = (audit / f"out-{judge}.txt").read_text("utf-8")
        m = re.search(r"```json\s*(\[.*?\])\s*```", text, re.S)
        if m is None:
            raise DeriveError(f"out-{judge}.txt: no ```json block")
        for a in json.loads(m.group(1)):
            if a.get("verdict") not in VERDICTS or a["id"] in out:
                raise DeriveError(f"out-{judge}.txt: {a.get('id')}: verdict {a.get('verdict')!r} or a repeated id")
            out[a["id"]] = {**a, "judge": judge}
    return out


def modal_roles(reps: list[dict]) -> dict[str, int]:
    """The most frequent roles dict over the repeats; a tie: the higher top strength, then the earlier repeat."""
    if not reps:
        return {"used": 1}
    keys = [json.dumps(r["roles"], sort_keys=True) for r in reps]
    count = Counter(keys)
    best = min(range(len(reps)), key=lambda i: (-count[keys[i]], -max(reps[i]["roles"].values()), i))
    return dict(reps[best]["roles"])


def norm(s: str) -> str:
    return " ".join(s.split())


def technique_slugs() -> set[str]:
    from stack.evals.technique_staged import taxonomy_kinds

    manifest = json.loads((SRC / "manifest.json").read_text("utf-8"))
    kind, _groups = taxonomy_kinds(ROOT / manifest["frozen_with"]["taxonomy_path"])
    return set(kind)


def build_gold(out: Path) -> tuple[list[dict], dict]:
    audit = out / "adjudication" / "audit"
    gold = read_jsonl(SRC / "gold.jsonl")
    by_item = {g["item_id"]: g for g in gold}
    key = sorted(read_jsonl(audit / "key.jsonl"), key=lambda k: k["id"])
    ans = verdicts(audit)
    roles = {r["id"]: r for r in read_jsonl(audit / "arm-roles.jsonl")}
    missing = sorted({k["id"] for k in key} - set(ans))
    if missing:
        raise DeriveError(f"no verdict for {', '.join(missing)}")
    techniques = technique_slugs()
    texts = {iid: norm(gzip.decompress(text_file(SRC, iid).read_bytes()).decode("utf-8"))
             for iid in by_item}
    removed: set[tuple[str, str]] = set()
    marked: dict[tuple[str, str], dict] = {}
    added: dict[str, list[dict]] = {}
    actions: dict[str, list[dict]] = {}
    counts: Counter[str] = Counter()
    not_found = []
    fallback = []
    for k in key:
        a, pool, iid, slug = ans[k["id"]], k["pool"], k["item"], k["slug"]
        if pool not in POOLS or iid not in by_item:
            raise DeriveError(f"{k['id']}: pool {pool!r} / item {iid!r}")
        counts[f"{pool}:{a['verdict']}"] += 1
        rows = [t for t in by_item[iid]["label"]["techniques"] if t["slug"] == slug]
        record = {"id": k["id"], "pool": pool, "judge": a["judge"], "verdict": a["verdict"]}
        if pool in ("A", "C"):
            if len(rows) != 1 or rows[0].get("candidate"):
                raise DeriveError(f"{k['id']}: pool {pool} expects one scored 17a gold row {iid}:{slug}, found {len(rows)}")
            if pool == "C":
                continue
            if a["verdict"] == "no":
                removed.add((iid, slug))
                actions.setdefault(iid, []).append({"id": k["id"], "slug": slug, "action": "removed"})
            elif a["verdict"] == "borderline":
                marked[(iid, slug)] = record
                actions.setdefault(iid, []).append({"id": k["id"], "slug": slug, "action": "dont_care"})
            continue
        if rows:
            raise DeriveError(f"{k['id']}: pool {pool} slug {iid}:{slug} is already in the 17a gold")
        if slug not in techniques:
            raise DeriveError(f"{k['id']}: {slug} is not a v0.9 technique")
        if a["verdict"] == "no":
            continue
        quote = str(a.get("quote") or "")
        if not quote or norm(quote) not in texts[iid]:
            not_found.append(k["id"])
        reps = roles[k["id"]]["repeats"] if k["id"] in roles else []
        if not reps:
            fallback.append(k["id"])
        row = {"slug": slug, "roles": modal_roles(reps), "quote": quote, "confidence": "med", "sources": ["audit"],
               "source": SOURCE, "alt_slugs": [], "implemented_with": [],
               "note": f"Blind audit {k['id']} ({a['judge']}, pool {pool}): {a['verdict']}. {a.get('reason', '')}".strip(),
               "audit": record}
        if a["verdict"] == "borderline":
            row["dont_care"] = True
        added.setdefault(iid, []).append(row)
        actions.setdefault(iid, []).append({"id": k["id"], "slug": slug,
                                            "action": "added" if a["verdict"] == "yes" else "added_dont_care"})
    out_gold = []
    for g in gold:
        iid = g["item_id"]
        rows = []
        for t in g["label"]["techniques"]:
            if (iid, t["slug"]) in removed:
                continue
            if (iid, t["slug"]) in marked:
                t = {**t, "dont_care": True, "audit": marked[(iid, t["slug"])]}
            rows.append(t)
        rows += added.get(iid, [])
        g2 = {**g, "label": {**g["label"], "techniques": rows}}
        if iid in actions:
            g2["audit_actions"] = actions[iid]
        out_gold.append(g2)
    n_add = sum(r.get("dont_care") is None for rs in added.values() for r in rs)
    n_add_dc = sum(bool(r.get("dont_care")) for rs in added.values() for r in rs)
    derivation = {"from_set": "2026-10-17a", "from_gold_sha256": sha((SRC / "gold.jsonl").read_bytes()),
                  "audit": SOURCE, "audit_items": len(key), "verdicts": dict(sorted(counts.items())),
                  "rows_removed": len(removed), "rows_dont_care": len(marked), "rows_added": n_add,
                  "rows_added_dont_care": n_add_dc, "roles_fallback": fallback, "quotes_not_found": not_found,
                  "known_bias": BIAS}
    return out_gold, derivation


def manifest(items_text: str, gold_text: str, gold: list[dict], derivation: dict) -> dict:
    src = json.loads((SRC / "manifest.json").read_text("utf-8"))
    from stack.evals.technique_staged import taxonomy_kinds

    _kind, groups = taxonomy_kinds(ROOT / src["frozen_with"]["taxonomy_path"])
    rows = [t for g in gold for t in g["label"]["techniques"]]
    scored = [t for t in rows if not t.get("candidate") and not t.get("dont_care") and t["slug"] not in groups]
    return {
        "task": "technique_staged", "set_id": SET_ID,
        "source": ("evals/technique_staged/2026-10-17a (items.jsonl and texts byte-identical; gold derived by "
                   "scripts/derive.py from 17a's gold and the blind audit in adjudication/audit/)"),
        "items": src["items"], "items_sha256": sha(items_text.encode("utf-8")), "splits": src["splits"],
        "gold": {"file": "gold.jsonl", "sha256": sha(gold_text.encode("utf-8")),
                 "from": "2026-10-17a gold.jsonl + adjudication/audit/", "rows": len(rows),
                 "candidate_rows": sum(bool(t.get("candidate")) for t in rows),
                 "dont_care_rows": sum(bool(t.get("dont_care")) for t in rows),
                 "group_node_rows": sum(t["slug"] in groups and not t.get("candidate") and not t.get("dont_care")
                                        for t in rows),
                 "scored_rows": len(scored), "scored_high": sum(t.get("confidence") == "high" for t in scored),
                 "scored_both_labellers": sum({"A", "B"} <= set(t.get("sources") or []) for t in scored),
                 "extra_techniques": sum(len(g["label"]["extra_techniques"]) for g in gold),
                 "menu_gaps": sum(len(g["label"]["menu_gaps"]) for g in gold)},
        "derivation": derivation,
        "frozen_with": src["frozen_with"],
    }


def build(out: Path = OUT) -> tuple[dict[str, str], dict[str, bytes]]:
    items_text = (SRC / "items.jsonl").read_text("utf-8")
    if sha(items_text.encode("utf-8")) != json.loads((SRC / "manifest.json").read_text("utf-8"))["items_sha256"]:
        raise DeriveError("2026-10-17a items.jsonl does not hash to its manifest's items_sha256")
    gold, derivation = build_gold(out)
    gold_text = dumps(gold)
    man = manifest(items_text, gold_text, gold, derivation)
    files = {"items.jsonl": items_text, "gold.jsonl": gold_text,
             "manifest.json": json.dumps(man, indent=2, ensure_ascii=False) + "\n"}
    blobs = {f"texts/{iid}.txt.gz": text_file(SRC, iid).read_bytes()
             for iid in sorted(i["item_id"] for i in read_jsonl(SRC / "items.jsonl"))}
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
    differ += [f"texts/{x} (not in 2026-10-17a)" for x in extra]
    if differ:
        print(f"differs: {', '.join(differ)}")
        return 1
    print(f"reproduced: {', '.join(files)} byte for byte, {len(blobs)} texts identical")
    return 0


if __name__ == "__main__":
    raise SystemExit(main("--write" in sys.argv[1:]))
