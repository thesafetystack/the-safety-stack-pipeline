"""Freeze (or check) the technique_staged eval set 2026-10-18b, gold v2, from its staged inputs (no LLM call, no
network).

Run from the repo root: ``uv run --no-sync python evals/technique_staged/2026-10-18b/scripts/freeze.py [--write]
[--allow-partial]``.

Inputs, all in this set (staged by ``scripts/stage.py``): ``sources/mirror-docs.jsonl`` and ``texts/g2-NNNN.txt.gz``
(each text checked against the store's ``text_digest`` and the labellers' copy, ``adjudication/texts.sha256``),
``sources/sampling.json``, ``adjudication/final.jsonl`` (the labels of record), ``STAGED.json`` (which labels those
are, which items the reconciler covered), ``verify-sample.json``, ``estimates.json`` and ``packet.json``.

Built with the current production code (``workflows/annotate/technique_staged/main.py``) and :data:`TAXONOMY`:

* ``items.jsonl``: per item ``input`` = title, basis_role, text_digest, full_text_digest, full_text_chars and
  ``staged`` = ``main.freeze_info(text, name=PROMPT_SET, taxonomy_path=TAXONOMY)``; ``input_digest`` = sha256 of
  the prompt set's ``single`` user message (the harness's input check under that prompt set); ``production_asks``
  true; ``source_item`` ``mirror:<doc_id>``; ``stratum`` {channel, channel_group, doc_type, group, band} (band from
  the frozen text's length, which must equal the selection's); split: dev / test alternating by length within the
  group (items sorted by (group, chars, item_id), the first of each group dev; deterministic, no seed).
* ``gold.jsonl``: per labelled item ``{item_id, doc_id, doc_kind, label{techniques[{slug, roles, quote, confidence,
  sources, alt_slugs, level_alternatives, implemented_with, note[, dont_care][, verification][, quote_check]}],
  extra_techniques, menu_gaps}, labelers, adjudicator, adjudication{doc_note, dropped, disagreement_note[,
  verification]}}``. ``sources``: X (L1) -> ``A``, Y (L2) -> ``B``; ``reconciler``, ``opus`` (a row the verification
  added from the Opus labels) and ``verification`` (a better slug the verification named) kept (the scorer's
  ``both`` variant reads A and B); a stand-in L1 / L2 row has none and gets ``["A"]`` / ``["B"]``. ``dont_care``
  (a verification ``borderline``) is kept: the scorer leaves such a row out of every variant (D611). ``note`` = the
  row's ``decision``, else its ``reason``. ``adjudicator``: ``astra-reconciler`` on the items in
  ``STAGED.json`` ``reconciled_items``, else ``merge-rule`` (checked against the label's own ``adjudicator``).
  ``labelers`` = astra-L1, astra-L2, plus ``opus`` and ``sonnet-adjudicator`` on the verified items when the labels
  of record are ``final``. Refused: a slug or level alternative that is not a technique or group node of
  :data:`TAXONOMY`, a ``sources`` entry outside :data:`SOURCES`, a label of record without ``sources`` (R, final), an
  adjudicator that contradicts the reconciled-items list, a verified item without its ``verification`` record (or
  the reverse). Reported in
  ``manifest.json`` ``gold.checks`` and not refused: quotes not found in the text (whitespace-normalised; the row
  gets ``quote_check`` ``case_insensitive`` or ``not_found``), rows with a role above ``mentioned``, level
  alternatives not structurally linked to the row's slug, alt_slugs outside the taxonomy, unknown ``doc_kind``.
* ``manifest.json``: task, set_id, items, items_sha256, splits, strata counts, gold {file, sha256, counts, checks},
  frozen_with {taxonomy, rubric and prompt digests of :data:`PROMPT_SET`, segmenter and lexical versions, the
  labelling packet}, sampling (``sources/sampling.json``), staging (``STAGED.json``).

A partial staging (labels missing for some items) or labels of record other than ``final`` (a dry run on stand-in
labels) is refused unless ``--allow-partial``; the manifest then records ``dry_run``. Without ``--write`` it rebuilds
everything in memory and compares it with the files byte for byte (exit 1 on any difference, naming it). With
``--write`` it writes ``items.jsonl``, ``gold.jsonl`` and ``manifest.json``. Any digest mismatch refuses in both modes.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path.cwd()
OUT = ROOT / "evals/technique_staged/2026-10-18b"
SET_ID = "2026-10-18b"
IDS = [f"g2-{n:04d}" for n in range(1, 121)]
TAXONOMY = "taxonomy/taxonomy-v0.9-234.yaml"
PROMPT_SET = "technique-staged-v3"
RUBRIC = "rubric-v0.9-c"  # the single call's rubric of PROMPT_SET; its grade call (staged, lexical) reads 0.9-b
SOURCES = {"X": "A", "Y": "B", "reconciler": "reconciler", "opus": "opus", "verification": "verification"}
STAND_IN_SOURCES = {"L1": ["A"], "L2": ["B"]}
LABELERS = ["astra-L1", "astra-L2"]
VERIFIERS = ["opus", "sonnet-adjudicator"]
RECONCILER, MERGE_RULE = "astra-reconciler", "merge-rule"
DOC_KINDS = ("paper", "blogpost", "commentary", "model_card", "system_card", "technical_report", "eval_report", "news",
             "policy", "other")


class FreezeError(Exception):
    """A digest mismatch or a malformed input: nothing is written."""


def sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


def dumps(rows: list[dict]) -> str:
    return "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows)


def gz(text: str) -> bytes:
    return gzip.compress(text.encode("utf-8"), compresslevel=9, mtime=0)


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def load_main():
    sys.path.insert(0, str(ROOT))
    from stack.stage_runner import load_path_module

    return load_path_module("_freeze_18b_technique_staged_main", ROOT / "workflows/annotate/technique_staged/main.py")


def texts_and_meta(out: Path) -> tuple[dict[str, str], dict[str, dict]]:
    """``(item_id -> text, item_id -> staged metadata)``, every digest checked."""
    from stack.evals.technique_staged import length_band

    mirror = {m["item_id"]: m for m in read_jsonl(out / "sources" / "mirror-docs.jsonl")}
    if sorted(mirror) != IDS:
        raise FreezeError(f"sources/mirror-docs.jsonl: expected {IDS[0]}..{IDS[-1]}")
    sums = dict(reversed(line.split("  ", 1)) for line in (out / "adjudication" / "texts.sha256")
                .read_text("utf-8").splitlines() if line.strip())
    texts: dict[str, str] = {}
    bad: list[str] = []
    for iid in IDS:
        m = mirror[iid]
        path = text_file(out, iid)  # staged by scripts/stage.py
        text = gzip.decompress(path.read_bytes()).decode("utf-8")
        if sha(text) != m["text_digest"]:
            bad.append(f"{iid}: staged text does not hash to the store's text_digest")
        if path.read_bytes() != gz(text):
            bad.append(f"{iid}: staged text is not the deterministic gzip of itself")
        if sums.get(f"{iid}.txt") != sha(text):
            bad.append(f"{iid}: the labellers' text (adjudication/texts.sha256) differs from the frozen text")
        if len(text) != m["chars"] or length_band(len(text)) != m["band"]:
            bad.append(f"{iid}: {len(text)} chars / band {length_band(len(text))}, selection {m['chars']} / "
                       f"{m['band']}")
        texts[iid] = text
    if bad:
        raise FreezeError("digest mismatch: " + "; ".join(bad))
    return texts, mirror


def splits(mirror: dict[str, dict]) -> dict[str, str]:
    """dev / test alternating by length within the group (module docstring)."""
    out, seen = {}, {}
    for iid in sorted(IDS, key=lambda i: (mirror[i]["group"], mirror[i]["chars"], i)):
        g = mirror[iid]["group"]
        out[iid] = "dev" if seen.get(g, 0) % 2 == 0 else "test"
        seen[g] = seen.get(g, 0) + 1
    return out


def build_items(main, texts: dict[str, str], mirror: dict[str, dict]) -> list[dict]:
    sp = splits(mirror)
    tax = ROOT / TAXONOMY
    items = []
    for iid in IDS:
        m, text = mirror[iid], texts[iid]
        items.append({
            "item_id": iid, "doc_id": m["doc_id"], "source_item": f"mirror:{m['doc_id']}", "split": sp[iid],
            "stratum": {"channel": m["channel"], "channel_group": m["channel_group"], "doc_type": m["doc_type"],
                        "group": m["group"], "band": m["band"]},
            "license_class": m["license_class"], "production_asks": True,
            "input": {"title": m["title"], "basis_role": m["basis_role"], "text_digest": m["text_digest"],
                      "full_text_digest": sha(text), "full_text_chars": len(text),
                      "staged": main.freeze_info(text, name=PROMPT_SET, taxonomy_path=tax)},
            "input_digest": sha(main.user_message(text, mode="single", name=PROMPT_SET))})
    return items


def build_gold(out: Path, items: list[dict], texts: dict[str, str], staged: dict) -> tuple[list[dict], dict]:
    from stack.evals.technique_staged import taxonomy_structure

    kind, groups, links = taxonomy_structure(ROOT / TAXONOMY)
    slugs = set(kind) | groups
    source = staged["labels_of_record"]
    rec = {r["item_id"]: r for r in read_jsonl(out / "adjudication" / "final.jsonl")}
    reconciled = set(staged.get("reconciled_items") or [])
    verified = {x["item_id"] for x in json.loads((out / "adjudication" / "verify-sample.json").read_text("utf-8"))}
    bad: list[str] = []
    checks: dict[str, list[str]] = {"quote_case_insensitive": [], "quote_not_found": [], "role_above_mentioned": [],
                                    "level_alternative_not_linked": [], "alt_slug_unknown": [], "doc_kind_unknown": []}
    gold = []
    for it in items:
        iid = it["item_id"]
        r = rec.get(iid)
        if r is None:
            continue
        text = norm(texts[iid])
        if r.get("doc_kind") not in DOC_KINDS:
            checks["doc_kind_unknown"].append(f"{iid}:{r.get('doc_kind')}")
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
            if "sources" in t:
                unknown = [s for s in t["sources"] if s not in SOURCES]
                if unknown:
                    bad.append(f"{where} (sources {unknown})")
                srcs = [SOURCES[s] for s in t["sources"] if s in SOURCES]
            elif source in STAND_IN_SOURCES:
                srcs = list(STAND_IN_SOURCES[source])
            else:
                bad.append(f"{where} (no sources)")
                srcs = []
            roles = {str(k): int(v) for k, v in (t.get("roles") or {}).items()}
            if any(v > roles.get("mentioned", 0) for k, v in roles.items() if k != "mentioned"):
                checks["role_above_mentioned"].append(where)
            row = {"slug": slug, "roles": roles, "quote": t.get("quote"), "confidence": t.get("confidence"),
                   "sources": srcs, "alt_slugs": list(t.get("alt_slugs") or []), "level_alternatives": alts,
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
               "disagreement_note": r.get("disagreement_note")}
        if r.get("verification") is not None:
            adj["verification"] = r["verification"]
        adjudicator = RECONCILER if iid in reconciled else MERGE_RULE
        if source == "final":
            if r.get("adjudicator", RECONCILER if iid in reconciled else None) != adjudicator:
                bad.append(f"{iid} (adjudicator {r.get('adjudicator')!r}, reconciled items say {adjudicator})")
            if (iid in verified) != (r.get("verification") is not None):
                bad.append(f"{iid} (verification record {r.get('verification') is not None}, sample {iid in verified})")
        gold.append({"item_id": iid, "doc_id": it["doc_id"], "doc_kind": r.get("doc_kind"),
                     "label": {"techniques": techniques, "extra_techniques": list(r.get("extra_techniques") or []),
                               "menu_gaps": list(r.get("menu_gaps") or [])},
                     "labelers": LABELERS + (VERIFIERS if source == "final" and iid in verified else []),
                     "adjudicator": adjudicator, "adjudication": adj})
    if bad:
        raise FreezeError(f"gold not valid against {TAXONOMY} / the label schema: {', '.join(bad)}")
    return gold, checks


def frozen_with(main, packet: dict) -> dict:
    cat = main.catalogue(ROOT / TAXONOMY)
    prompts = {mode: {call: main.prompt_file(PROMPT_SET, call).digest for call in main.CALLS[mode]}
               for mode in main.MODES}
    options = {call: main.prompt_file(PROMPT_SET, call).digest for call in ("window", "glean")
               if call in main.PROMPT_SETS[PROMPT_SET]}
    rubric = main.HERE / "prompts" / f"{RUBRIC}.md"
    return {"taxonomy_path": TAXONOMY, "taxonomy_sha256": cat.sha256, "taxonomy_version": cat.version,
            "rubric": RUBRIC, "rubric_digest": hashlib.sha256(rubric.read_bytes()).hexdigest(),
            "prompt_set": PROMPT_SET, "prompt_digests": prompts, "option_prompt_digests": options,
            "segmenter_version": main.segment.SEGMENTER_VERSION, "lexical_version": main.lexical.LEXICAL_VERSION,
            "labelling_packet": packet}


def manifest(items_text: str, items: list[dict], gold_text: str, gold: list[dict], checks: dict, fw: dict,
             sampling: dict, staged: dict, groups: frozenset[str], estimates: dict | None) -> dict:
    rows = [t for g in gold for t in g["label"]["techniques"]]
    scored = [t for t in rows if t["slug"] not in groups and not t.get("dont_care")]
    sources: dict[str, int] = {}
    for t in rows:
        k = "+".join(t["sources"])
        sources[k] = sources.get(k, 0) + 1
    strata: dict[str, dict[str, int]] = {}
    for k in ("group", "band", "channel_group"):
        for it in items:
            v = str(it["stratum"][k])
            strata.setdefault(k, {})[v] = strata.get(k, {}).get(v, 0) + 1
    out = {
        "task": "technique_staged", "set_id": SET_ID,
        "source": ("a read-only store copy (sources/mirror-docs.jsonl, texts staged by scripts/stage.py; sampling in "
                   "sources/sampling.json and scripts/sampling/); gold v2 labelled and reconciled outside the "
                   "repository (adjudication/)"),
        "items": len(items), "items_sha256": hashlib.sha256(items_text.encode("utf-8")).hexdigest(),
        "splits": {s: sum(i["split"] == s for i in items) for s in ("dev", "test")},
        "strata": {k: dict(sorted(v.items())) for k, v in strata.items()},
        "gold": {"file": "gold.jsonl", "sha256": hashlib.sha256(gold_text.encode("utf-8")).hexdigest(),
                 "from": f"adjudication/final.jsonl (labels of record: {staged['labels_of_record']})",
                 "items": len(gold), "rows": len(rows),
                 "dont_care_rows": sum(bool(t.get("dont_care")) for t in rows),
                 "group_node_rows": sum(t["slug"] in groups and not t.get("dont_care") for t in rows),
                 "scored_rows": len(scored), "scored_high": sum(t["confidence"] == "high" for t in scored),
                 "scored_both_labellers": sum({"A", "B"} <= set(t["sources"]) for t in scored),
                 "level_alternative_rows": sum(bool(t["level_alternatives"]) for t in rows),
                 "verified_items": sum("opus" in g["labelers"] for g in gold),
                 "adjudicators": {a: sum(g["adjudicator"] == a for g in gold) for a in (RECONCILER, MERGE_RULE)},
                 "row_sources": dict(sorted(sources.items())),
                 "extra_techniques": sum(len(g["label"]["extra_techniques"]) for g in gold),
                 "menu_gaps": sum(len(g["label"]["menu_gaps"]) for g in gold),
                 "checks": {k: v for k, v in checks.items()}},
        "frozen_with": fw,
        "sampling": sampling,
        "verification": {"sample": "adjudication/verify-sample.json", "verifier": "opus (blind labels)",
                         "adjudicator": "sonnet (disputed slugs, labellers blinded as P / Q)",
                         "estimates": estimates},
        "staging": staged,
    }
    if staged["partial"] or staged["labels_of_record"] != "final":
        out["dry_run"] = {"labels_of_record": staged["labels_of_record"], "partial": staged["partial"],
                          "note": "not the final gold: built for testing the scripts (--allow-partial)"}
    return out


def build(out: Path = OUT, allow_partial: bool = False) -> tuple[dict[str, str], dict[str, bytes]]:
    """``(text files by set-relative path, binary files by path)``: what the set should hold."""
    staged = json.loads((out / "adjudication" / "STAGED.json").read_text("utf-8"))
    if (staged["partial"] or staged["labels_of_record"] != "final") and not allow_partial:
        raise FreezeError(f"the staging is partial ({staged['partial']}) or its labels of record are "
                          f"{staged['labels_of_record']!r}, not 'final': pass --allow-partial for a dry run")
    main = load_main()
    texts, mirror = texts_and_meta(out)
    items = build_items(main, texts, mirror)
    gold, checks = build_gold(out, items, texts, staged)
    if len(gold) != len(IDS) and not allow_partial:
        raise FreezeError(f"gold for {len(gold)} of {len(IDS)} items: pass --allow-partial for a dry run")
    from stack.evals.technique_staged import taxonomy_kinds

    _kind, groups = taxonomy_kinds(ROOT / TAXONOMY)
    items_text, gold_text = dumps(items), dumps(gold)
    packet = json.loads((out / "adjudication" / "packet.json").read_text("utf-8"))
    sampling = json.loads((out / "sources" / "sampling.json").read_text("utf-8"))
    est = out / "adjudication" / "estimates.json"
    estimates = json.loads(est.read_text("utf-8")) if est.is_file() else None
    man = manifest(items_text, items, gold_text, gold, checks, frozen_with(main, packet), sampling, staged, groups,
                   estimates)
    files = {"items.jsonl": items_text, "gold.jsonl": gold_text,
             "manifest.json": json.dumps(man, indent=2, ensure_ascii=False) + "\n"}
    return files, {}


def text_file(set_dir: Path, iid: str) -> Path:
    """``iid``'s whole text: the set's ``texts/`` file, else the eval-text cache's (the texts are not in the
    repository, D790; ``tools/fetch_eval_texts.py`` fills the cache)."""
    from stack.evals.freeze import find_text, missing_text_message

    p = find_text(set_dir, iid)
    if p is None:
        raise FreezeError(missing_text_message(set_dir, iid))
    return p


def main(write: bool, out: Path = OUT, allow_partial: bool = False) -> int:
    try:
        files, _blobs = build(out, allow_partial)
    except FreezeError as exc:
        print(f"refused: {exc}")
        return 1
    if write:
        for rel, text in files.items():
            (out / rel).write_text(text, encoding="utf-8")
        print(f"wrote {', '.join(files)}")
        return 0
    differ = [rel for rel, text in files.items()
              if not (out / rel).is_file() or (out / rel).read_text("utf-8") != text]
    if differ:
        print(f"differs: {', '.join(differ)}")
        return 1
    print(f"reproduced: {', '.join(files)} byte for byte, {len(IDS)} texts checked")
    return 0


if __name__ == "__main__":
    raise SystemExit(main("--write" in sys.argv[1:], allow_partial="--allow-partial" in sys.argv[1:]))
