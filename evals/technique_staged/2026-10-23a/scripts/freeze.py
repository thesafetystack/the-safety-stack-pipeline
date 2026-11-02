"""Freeze (or check) the input-only eval sets technique_staged 2026-10-23a and model_roles 2026-10-23a (gold v3, no
gold yet): the 100 documents of ``sources/sample.jsonl``, shared by both tasks (same item ids g3-0001..g3-0100).

Run from the repo root (no LLM call, no network, the store read only)::

    uv run --no-sync python evals/technique_staged/2026-10-23a/scripts/freeze.py <STORE_DIR> <FRAME_PARQUET> <PRIVATE_EVALS_ROOT> [--write]

The sample covers every licence class (restricted included), so the texts never enter the repository: the complete
sets (``items.jsonl``, ``manifest.json``, ``texts/g3-NNNN.txt.gz``, an empty ``gold.jsonl``) are written under
``<PRIVATE_EVALS_ROOT>/<task>/2026-10-23a/`` and run with ``stack eval <task> --evals <PRIVATE_EVALS_ROOT>``; the
repository's set directories hold the same ``items.jsonl`` and ``manifest.json`` (no texts, no gold). Without
``--write`` everything is rebuilt in memory and compared byte for byte with both copies (exit 1 naming each
difference).

Texts: the store blob of the sample row's ``text_sha256`` (the corpus row's current text), decoded strictly as UTF-8
(a blob that does not decode is refused). Built with the current production code:

* technique_staged ``items.jsonl``: ``input`` = title, basis_role, text_digest (the blob sha256), full_text_digest,
  full_text_chars and ``staged`` = ``main.freeze_info(text, name=PROMPT_SET, taxonomy_path=TAXONOMY)``;
  ``input_digest`` = sha256 of the ``single`` user message; ``stratum`` {channel, channel_group, doc_type, group,
  band, year_bucket, mention_bucket} plus ``weight`` (the design weight); split dev / test alternating by length
  within the group (items sorted by (group, chars, item_id)), as 2026-10-18b.
* model_roles ``items.jsonl``: the same base plus ``input.model_mentions`` (``match.scan_payload``), ``menu``,
  ``beyond_window``, ``fills_digest`` and ``input_digest`` = sha256 of the ``model-roles-v2`` user message, as
  2026-10-17a's freeze.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path.cwd()
SET_ID = "2026-10-23a"
HERE = ROOT / "evals/technique_staged" / SET_ID
MR_HERE = ROOT / "evals/model_roles" / SET_ID
TAXONOMY = "taxonomy/taxonomy-v0.9.1.yaml"
PROMPT_SET = "technique-staged-v3"
RUBRIC = "rubric-v0.9-c"
MR_PROMPT = "model-roles-v2"
GROUP = {"paper": "paper", "forum": "forum", "blog": "blog", "card": "card", "report": "report"}


class FreezeError(Exception):
    pass


def sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


def dumps(rows: list[dict]) -> str:
    return "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows)


def gz(text: str) -> bytes:
    return gzip.compress(text.encode("utf-8"), compresslevel=9, mtime=0)


def load(name: str, rel: str):
    sys.path.insert(0, str(ROOT))
    from stack.stage_runner import load_path_module

    return load_path_module(name, ROOT / rel)


def splits(sample: list[dict]) -> dict[str, str]:
    out, seen = {}, {}
    for r in sorted(sample, key=lambda r: (r["source_kind"], r["chars"], r["item_id"])):
        g = r["source_kind"]
        out[r["item_id"]] = "dev" if seen.get(g, 0) % 2 == 0 else "test"
        seen[g] = seen.get(g, 0) + 1
    return out


def texts_of(store: Path, sample: list[dict]) -> dict[str, str]:
    texts, bad = {}, []
    for r in sample:
        raw = (store / "blobs" / r["text_sha256"]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != r["text_sha256"]:
            bad.append(f"{r['item_id']} (blob sha256)")
            continue
        try:
            texts[r["item_id"]] = raw.decode("utf-8")
        except UnicodeDecodeError:
            bad.append(f"{r['item_id']} (not UTF-8)")
            continue
        if len(texts[r["item_id"]]) != r["chars"]:
            bad.append(f"{r['item_id']} (chars {len(texts[r['item_id']])} != sample {r['chars']})")
    if bad:
        raise FreezeError("texts refused: " + ", ".join(bad))
    return texts


def base_items(sample, texts, titles, channel_groups) -> list[dict]:
    sp = splits(sample)
    out = []
    for r in sample:
        iid, text = r["item_id"], texts[r["item_id"]]
        out.append({
            "item_id": iid, "doc_id": r["doc_id"], "source_item": f"mirror:{r['doc_id']}", "split": sp[iid],
            "stratum": {"channel": r["channel"], "channel_group": channel_groups.get(r["channel"], "url"),
                        "doc_type": r["doc_type"], "group": GROUP[r["source_kind"]], "band": r["band"],
                        "year_bucket": r["year_bucket"], "mention_bucket": r["mention_bucket"]},
            "weight": r["weight"], "license_class": r["license_class"], "production_asks": True,
            "input": {"title": titles[r["doc_id"]], "basis_role": r["basis_role"], "text_digest": r["text_sha256"],
                      "full_text_digest": sha(text), "full_text_chars": len(text)}})
    return out


def counts(items: list[dict]) -> dict:
    strata: dict[str, dict[str, int]] = {}
    for k in ("group", "band", "channel_group", "year_bucket", "mention_bucket"):
        for it in items:
            v = str(it["stratum"][k])
            strata.setdefault(k, {})[v] = strata.get(k, {}).get(v, 0) + 1
    lic: dict[str, int] = {}
    for it in items:
        lic[it["license_class"]] = lic.get(it["license_class"], 0) + 1
    return {"splits": {s: sum(i["split"] == s for i in items) for s in ("dev", "test")},
            "strata": {k: dict(sorted(v.items())) for k, v in strata.items()}, "license_class": dict(sorted(lic.items()))}


def build(store: Path, frame: Path) -> dict[str, dict[str, str | bytes]]:
    """Files by set (``technique_staged`` / ``model_roles``) and set-relative path."""
    import duckdb

    from stack import gate
    from stack.evals.annotators import fills_digest

    sample = read_jsonl(HERE / "sources" / "sample.jsonl")
    sampling = json.loads((HERE / "sources" / "sampling.json").read_text("utf-8"))
    if hashlib.sha256((HERE / "sources" / "sample.jsonl").read_bytes()).hexdigest() != sampling["sample_sha256"]:
        raise FreezeError("sources/sample.jsonl does not match sampling.json's sample_sha256")
    ids = [r["doc_id"] for r in sample]
    titles = dict(duckdb.sql(f"select doc_id, title from '{frame}'").fetchall())
    missing = [d for d in ids if d not in titles]
    if missing:
        raise FreezeError(f"doc_ids not in the frame: {missing}")
    texts = texts_of(store, sample)
    base = base_items(sample, texts, titles, gate.channel_groups())
    common = {"items": len(base), "source": ("gold v3 sample (sources/sample.jsonl, scripts/sampling/sample.py) of the "
                                             "2026-10-23 build's screen_relevant documents; texts = store blobs"),
              "sampling": {k: sampling[k] for k in ("seed", "n", "frame", "eligible", "excluded", "allocation",
                                                    "weights", "sample_sha256", "mentions_sha256",
                                                    "models_yaml_sha256")},
              "gold": None, "input_only": True}
    # technique_staged
    main = load("_freeze_23a_technique_staged_main", "workflows/annotate/technique_staged/main.py")
    tax = ROOT / TAXONOMY
    ts_items = []
    for b in base:
        text = texts[b["item_id"]]
        ts_items.append({**b, "input": {**b["input"], "staged": main.freeze_info(text, name=PROMPT_SET, taxonomy_path=tax)},
                         "input_digest": sha(main.user_message(text, mode="single", name=PROMPT_SET))})
    cat = main.catalogue(tax)
    prompts = {mode: {call: main.prompt_file(PROMPT_SET, call).digest for call in main.CALLS[mode]} for mode in main.MODES}
    rubric = main.HERE / "prompts" / f"{RUBRIC}.md"
    ts_text = dumps(ts_items)
    ts_man = {"task": "technique_staged", "set_id": SET_ID, **common,
              "items_sha256": hashlib.sha256(ts_text.encode("utf-8")).hexdigest(), **counts(ts_items),
              "frozen_with": {"taxonomy_path": TAXONOMY, "taxonomy_sha256": cat.sha256, "taxonomy_version": cat.version,
                              "rubric": RUBRIC, "rubric_digest": hashlib.sha256(rubric.read_bytes()).hexdigest(),
                              "prompt_set": PROMPT_SET, "prompt_digests": prompts,
                              "segmenter_version": main.segment.SEGMENTER_VERSION,
                              "lexical_version": main.lexical.LEXICAL_VERSION},
              "truncated_items": sorted(b["item_id"] for b in base if b["input"]["full_text_chars"] > main.DOC_CHAR_CAP),
              "doc_char_cap": main.DOC_CHAR_CAP}
    # model_roles
    mr = load("_freeze_23a_model_roles_main", "workflows/annotate/model_roles/main.py")
    c = mr.match.scanner_for(ROOT / "configs")
    pf = mr.prompt_file(MR_PROMPT)
    mr_items = []
    for b in base:
        text = texts[b["item_id"]]
        scan = c.scanner.scan(text)
        menu, beyond, fills = mr.fills(text, scan, c.vocab)
        payload = mr.match.scan_payload(c.scanner, text)
        inp = {**b["input"], "model_mentions": {"status": "empty" if mr.match.is_empty(scan) else "ok", "payload": payload},
               "menu": [{"node_id": e.node_id, "weak": e.weak} for e in menu], "beyond_window": beyond,
               "fills_digest": fills_digest(fills)}
        mr_items.append({**b, "input": inp, "input_digest": sha(pf.user(fills))})
    mr_text = dumps(mr_items)
    mr_man = {"task": "model_roles", "set_id": SET_ID, **common,
              "items_sha256": hashlib.sha256(mr_text.encode("utf-8")).hexdigest(), **counts(mr_items),
              "frozen_with": {"models_yaml_sha256": c.vocab.digest, "models_yaml_version": c.vocab.validated.version,
                              "model_roles_prompt": MR_PROMPT, "model_roles_prompt_digest": pf.digest},
              "menu": {"empty": sum(not i["input"]["menu"] for i in mr_items),
                       "weak_only": sum(bool(i["input"]["menu"]) and all(e["weak"] for e in i["input"]["menu"])
                                        for i in mr_items),
                       "entries": sum(len(i["input"]["menu"]) for i in mr_items),
                       "beyond_window_items": sum(bool(i["input"]["beyond_window"]) for i in mr_items)},
              "truncated_items": sorted(b["item_id"] for b in base if b["input"]["full_text_chars"] > mr.DOC_CHAR_CAP),
              "doc_char_cap": mr.DOC_CHAR_CAP}
    text_files = {f"texts/{iid}.txt.gz": gz(t) for iid, t in texts.items()}
    return {"technique_staged": {"items.jsonl": ts_text, "manifest.json": json.dumps(ts_man, indent=2, ensure_ascii=False) + "\n",
                                 "gold.jsonl": "", **text_files},
            "model_roles": {"items.jsonl": mr_text, "manifest.json": json.dumps(mr_man, indent=2, ensure_ascii=False) + "\n",
                            "gold.jsonl": "", **text_files}}


REPO_FILES = ("items.jsonl", "manifest.json")


def main_(argv: list[str]) -> int:
    write = "--write" in argv
    args = [a for a in argv if not a.startswith("--")]
    if len(args) != 3:
        print(__doc__)
        return 2
    store, frame, private = Path(args[0]), Path(args[1]), Path(args[2])
    files = build(store, frame)
    diffs = []
    for task, fs in files.items():
        for dest, names in ((private / task / SET_ID, list(fs)), (ROOT / "evals" / task / SET_ID, REPO_FILES)):
            for name in names:
                data = fs[name]
                data = data.encode("utf-8") if isinstance(data, str) else data
                p = dest / name
                if write:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(data)
                elif not p.is_file() or p.read_bytes() != data:
                    diffs.append(f"{task}: {'repo' if dest.is_relative_to(ROOT) else 'private'} {name}")
        if not write:
            stale = sorted(p.name for p in (private / task / SET_ID / "texts").glob("*.txt.gz")
                           if f"texts/{p.name}" not in fs)
            diffs += [f"{task}: private stale texts/{n}" for n in stale]
    for d in diffs:
        print("differs:", d)
    print(("written" if write else "checked"), {t: len(f) for t, f in files.items()}, "files;", len(diffs), "differences")
    return 1 if diffs else 0


if __name__ == "__main__":
    sys.exit(main_(sys.argv[1:]))
