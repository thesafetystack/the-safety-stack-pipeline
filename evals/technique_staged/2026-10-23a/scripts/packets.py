"""Build the gold v3 labelling packets (blind, agentic labellers) for technique_staged and model_roles 2026-10-23a.

Run from the repo root (no LLM call, no network)::

    uv run --no-sync python evals/technique_staged/2026-10-23a/scripts/packets.py <PRIVATE_EVALS_ROOT> <FRAME_PARQUET> <OUT>

Reads the frozen sets (``<PRIVATE_EVALS_ROOT>/<task>/2026-10-23a``: items and texts) and writes under OUT (a private
directory: the texts are third-party content):

* ``docs/<item_id>/text.txt`` (the frozen text, byte-identical to the store blob), ``meta.md`` (title, source kind,
  year, channel, length, text quality) and ``model-menu.md`` (the production model_roles menu text,
  ``render.menu_text`` of ``main.fills``, under a short header);
* ``shared/techniques/`` - INSTRUCTIONS.md, GUIDE.md (guide v2.1 blind), RULINGS.md (the 2026-10-18 round's, as in
  2026-10-18b), rubric-v0.4.0.md, menu-v0.9.1.md (production ``menu_text`` of taxonomy v0.9.1 under a title line),
  schema.json, check_labels.py; ``shared/model_roles/`` - INSTRUCTIONS.md, GUIDE.md, rubric-v0.6.0.md (the rubric
  ``model-roles-v2`` includes), vocab-index.tsv (configs/models.yaml), schema.json, check_labels.py;
* ``sessions/<task>-<labeller>-r<k>/`` for labellers opus, sonnet, sol and repeats 1, 2: a self-contained copy of the
  task's shared files and of ``docs/`` (model-menu.md only for model_roles), an empty ``out/``, ``batches.json`` and
  one session prompt per batch in ``prompts/<batch>.md``. Batches: documents first-fit packed by characters
  (<= CAP characters and <= MAXN documents, a longer document alone) in an order shuffled with seed SEED + repeat, so
  the two repeats group documents differently;
* ``packet.json`` - sha256 of every shared file and document file, the batches, token estimates per document.

Refused: a shared file containing a private marker (a person's name, a home path, the repository's private names).
"""
from __future__ import annotations

import gzip
import hashlib
import json
import random
import re
import shutil
import statistics
import sys
from pathlib import Path

ROOT = Path.cwd()
SET_ID = "2026-10-23a"
HERE = ROOT / "evals/technique_staged" / SET_ID
MR_HERE = ROOT / "evals/model_roles" / SET_ID
TAXONOMY = "taxonomy/taxonomy-v0.9.1.yaml"
MENU_HEADER = "# Technique taxonomy v0.9.1 (annotator view, 234 entries + 9 group nodes)\n\n"
RULINGS_SHA256 = "d08458edd77b70aaa364a8d61fa6b2942f9a8f3116d591ab5815c39d938e56be"  # 2026-10-18b packet.json
RUBRIC_SHA256 = "858092e8ed156baaf7ca3e87aa0550bbc81021889d795030e4375d5237d666e8"
LABELLERS = ("opus", "sonnet", "sol")
REPEATS = (1, 2)
SEED = 20261023
CAP, MAXN = 200_000, 5
LEAK = re.compile(r"mimir|/home/|/tmp/|Tom[aá]š|Gaven[cč]iak|impl/\d|stack-store|\.env\b", re.I)
TASK_NAMES = {"techniques": "technique_staged", "model_roles": "model_roles"}


def sha_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def read_jsonl(p: Path) -> list[dict]:
    return [json.loads(x) for x in p.read_text("utf-8").splitlines() if x.strip()]


def load(name: str, rel: str):
    sys.path.insert(0, str(ROOT))
    from stack.stage_runner import load_path_module

    return load_path_module(name, ROOT / rel)


def strip_comment(s: str) -> str:
    return re.sub(r"\A<!--.*?-->\n\n", "", s, flags=re.S)


def shared_files() -> dict[str, dict[str, bytes]]:
    import yaml

    tx = load("_p23a_tax", "workflows/annotate/technique_staged/taxonomy.py")
    rulings = strip_comment((ROOT / "evals/technique_staged/2026-10-18b/adjudication/RULINGS.md").read_text("utf-8")).encode()
    rubric = (ROOT / "workflows/annotate/technique_roles/prompts/rubric-v0.4.0.md").read_bytes()
    if sha_bytes(rulings) != RULINGS_SHA256 or sha_bytes(rubric) != RUBRIC_SHA256:
        raise SystemExit("RULINGS.md or rubric-v0.4.0.md differ from the 2026-10-18 round's packet")
    menu = (MENU_HEADER + tx.menu_text(tx.load_taxonomy(ROOT / TAXONOMY))).encode()
    check = (HERE / "labelling/check_labels.py").read_bytes()
    raw = yaml.safe_load((ROOT / "configs/models.yaml").read_text("utf-8"))
    lines = ["node_id\tkind\tname\tparent\tmaker\tmentions\tweak_mentions"]
    for nid, n in raw["nodes"].items():
        lines.append("\t".join([nid, str(n.get("kind", "")), str(n.get("name", "")), str(n.get("parent", "") or ""),
                                str(n.get("maker", "") or ""), "; ".join(n.get("mentions") or []),
                                "; ".join(n.get("weak_mentions") or [])]))
    return {
        "techniques": {"INSTRUCTIONS.md": (HERE / "labelling/INSTRUCTIONS.md").read_bytes(),
                       "GUIDE.md": (HERE / "labelling/GUIDE.md").read_bytes(), "RULINGS.md": rulings,
                       "rubric-v0.4.0.md": rubric, "menu-v0.9.1.md": menu,
                       "schema.json": (HERE / "labelling/schema.json").read_bytes(), "check_labels.py": check},
        "model_roles": {"INSTRUCTIONS.md": (MR_HERE / "labelling/INSTRUCTIONS.md").read_bytes(),
                        "GUIDE.md": (MR_HERE / "labelling/GUIDE.md").read_bytes(),
                        "rubric-v0.6.0.md": (ROOT / "workflows/annotate/model_roles/prompts/rubric-v0.6.0.md").read_bytes(),
                        "vocab-index.tsv": ("\n".join(lines) + "\n").encode(),
                        "schema.json": (MR_HERE / "labelling/schema.json").read_bytes(), "check_labels.py": check}}


def doc_files(private: Path, frame: Path) -> tuple[dict[str, dict[str, bytes]], dict[str, dict]]:
    import duckdb

    from stack.llm import estimate_tokens

    ts = {r["item_id"]: r for r in read_jsonl(private / "technique_staged" / SET_ID / "items.jsonl")}
    mri = {r["item_id"]: r for r in read_jsonl(private / "model_roles" / SET_ID / "items.jsonl")}
    sample = {r["item_id"]: r for r in read_jsonl(HERE / "sources" / "sample.jsonl")}
    meta_rows = {d: (t, y) for d, t, y in duckdb.sql(f"select doc_id, title, year from '{frame}'").fetchall()}
    mr = load("_p23a_mr", "workflows/annotate/model_roles/main.py")
    c = mr.match.scanner_for(ROOT / "configs")
    files, info = {}, {}
    for iid, it in sorted(ts.items()):
        text = gzip.open(private / "technique_staged" / SET_ID / "texts" / f"{iid}.txt.gz", "rt", encoding="utf-8").read()
        if sha_bytes(text.encode()) != it["input"]["full_text_digest"]:
            raise SystemExit(f"{iid}: frozen text digest mismatch")
        s = sample[iid]
        title, year = meta_rows[it["doc_id"]]
        meta = (f"# {iid}\n\n- title: {title}\n- source kind: {s['source_kind']} ({s['doc_type']})\n"
                f"- published: {year if year is not None else 'unknown'}\n- channel: {s['channel']}\n"
                f"- text: {len(text):,} characters, {text.count(chr(10)) + 1:,} lines; text quality: {s['usability']}"
                f" (pipeline basis: {it['input']['basis_role']})\n")
        scan = c.scanner.scan(text)
        menu, beyond, fills = mr.fills(text, scan, c.vocab)
        if [{"node_id": e.node_id, "weak": e.weak} for e in menu] != mri[iid]["input"]["menu"]:
            raise SystemExit(f"{iid}: menu differs from the frozen model_roles item")
        head = (f"# Model menu for {iid}\n\nCandidate catalogue models the production matcher found in this document "
                f"({len(menu)} entries" + (", none: the menu is empty" if not menu else "") + ").\n")
        if beyond:
            head += (f"The production judge sees only the first {mr.DOC_CHAR_CAP:,} characters; matcher hits only past "
                     f"that point (not on this menu): {', '.join(beyond)}. Label the whole document regardless.\n")
        files[iid] = {"text.txt": text.encode("utf-8"), "meta.md": meta.encode("utf-8"),
                      "model-menu.md": (head + fills["menu"]).encode("utf-8")}
        info[iid] = {"chars": len(text), "tokens_est": estimate_tokens(text), "menu": len(menu), "beyond_window": beyond}
    return files, info


def batches(info: dict[str, dict], repeat: int) -> list[list[str]]:
    rng = random.Random(SEED + repeat)
    ids = sorted(info)
    rng.shuffle(ids)
    bins: list[list[str]] = []
    for iid in ids:
        for b in bins:
            if len(b) < MAXN and sum(info[x]["chars"] for x in b) + info[iid]["chars"] <= CAP:
                b.append(iid)
                break
        else:
            bins.append([iid])
    rng.shuffle(bins)
    return [sorted(b) for b in bins]


def session_prompt(task: str, label: str, items: list[str]) -> str:
    what = "AI-safety techniques" if task == "techniques" else "AI models and their roles"
    return (f"You are gold labeller `{label}` for {what}. Your working directory is this session directory; work only "
            f"in it. Read INSTRUCTIONS.md and follow it exactly (it points you to GUIDE.md and the other files).\n\n"
            f"Label these documents, each into out/<item_id>.json with \"labeller\": \"{label}\": {', '.join(items)}\n\n"
            f"Run `python3 check_labels.py {task} out/<item_id>.json` on every file you write and fix every problem it "
            f"reports. Do not stop before every listed document has a checked file.\n")


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2
    private, frame, out = Path(argv[0]), Path(argv[1]), Path(argv[2])
    shared = shared_files()
    for task, fs in shared.items():
        for name, data in fs.items():
            if LEAK.search(data.decode("utf-8", "replace")):
                raise SystemExit(f"private marker in shared/{task}/{name}")
    docs, info = doc_files(private, frame)
    for sub in ("docs", "shared", "sessions"):
        if (out / sub).exists():
            shutil.rmtree(out / sub)
    for iid, fs in docs.items():
        for name, data in fs.items():
            p = out / "docs" / iid / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
    for task, fs in shared.items():
        for name, data in fs.items():
            p = out / "shared" / task / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
    plan = {r: batches(info, r) for r in REPEATS}
    for task, fs in shared.items():
        for lab in LABELLERS:
            for r in REPEATS:
                label = f"{lab}-r{r}"
                d = out / "sessions" / f"{task}-{label}"
                for name, data in fs.items():
                    (d / name).parent.mkdir(parents=True, exist_ok=True)
                    (d / name).write_bytes(data)
                for iid, dfs in docs.items():
                    for name, data in dfs.items():
                        if name == "model-menu.md" and task != "model_roles":
                            continue
                        (d / "docs" / iid).mkdir(parents=True, exist_ok=True)
                        (d / "docs" / iid / name).write_bytes(data)
                (d / "out").mkdir()
                (d / "prompts").mkdir()
                bs = {f"B{k:02d}": b for k, b in enumerate(plan[r], 1)}
                (d / "batches.json").write_text(json.dumps(bs, indent=1) + "\n", "utf-8")
                for b, items in bs.items():
                    (d / "prompts" / f"{b}.md").write_text(session_prompt(task, label, items), "utf-8")
    tok = sorted(v["tokens_est"] for v in info.values())
    packet = {
        "set_id": SET_ID, "labellers": list(LABELLERS), "repeats": list(REPEATS), "seed": SEED, "cap_chars": CAP,
        "max_docs_per_batch": MAXN,
        "shared": {t: {n: sha_bytes(b) for n, b in fs.items()} for t, fs in shared.items()},
        "docs": {iid: {n: sha_bytes(b) for n, b in fs.items()} for iid, fs in docs.items()},
        "batches": {f"r{r}": {f"B{k:02d}": b for k, b in enumerate(plan[r], 1)} for r in REPEATS},
        "tokens_est": {"min": tok[0], "median": statistics.median(tok), "max": tok[-1], "sum": sum(tok),
                       "per_doc": {iid: v["tokens_est"] for iid, v in sorted(info.items())}},
        "menu_entries": {iid: v["menu"] for iid, v in sorted(info.items())},
    }
    (out / "packet.json").write_text(json.dumps(packet, indent=1) + "\n", "utf-8")
    print(json.dumps({"docs": len(docs), "batches": {r: len(plan[r]) for r in REPEATS},
                      "tokens_est": {k: packet["tokens_est"][k] for k in ("min", "median", "max", "sum")}}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
