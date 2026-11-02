"""Stage the inputs of the technique_staged eval set 2026-10-18b, gold v2 (one-time import; no LLM call, no network).

Run from the repo root::

    uv run --no-sync python evals/technique_staged/2026-10-18b/scripts/stage.py <WORKSPACE> <STORE_DIR>
        [--labels final|R|L1|L2] [--allow-partial] [--check-pool] [--write]

``WORKSPACE`` is the labelling workspace (outside the repository), read only:

* ``selection/selected.jsonl`` (120 rows g2-0001..g2-0120: doc_id, title, doc_type, group, band, chars, channel,
  license_class, basis_role, text_digest, relevance_conf, text_quality), ``selection/pool.jsonl`` (the candidate
  pool) and ``scripts/pool.py`` / ``scripts/select.py`` (the sampling procedure);
* ``texts/g2-NNNN.txt``: the exact texts the labellers read;
* ``labels/L1/``, ``labels/L2/`` (two independent labellings: L1 document-first, L2 inventory-first; = the
  reconciler's X and Y), ``labels/R/`` with ``labels/R-items.json`` (the reconciler's labels, for the items it
  covered), ``labels/G0/`` (the pre-verification gold: R where it exists, else the merge rule's L1 + L2 merge) and
  the labels of record: ``labels/final/`` (G0 with the verification applied; the default) or, for a dry run,
  ``--labels R``, ``L1`` or ``L2`` (a stand-in);
* ``verify/sample.json`` (the verified items), ``verify/opus/<item>.json`` (blind Opus labels), ``verify/key.json``
  (per item the disputed slugs, who gave each, and the P / Q blinding swap), ``verify/adj/<item>.json`` (the
  adjudicator's verdicts), ``verify/adj-s1-first/`` (a first adjudication run of one batch, kept for the
  self-consistency check) and ``verify/estimates.json``;
* ``scripts/merge.py``, ``verify_sample.py``, ``disputes.py``, ``verify_apply.py`` (the merge rule and the
  verification procedure);
* ``packet-common/`` (``GOLD-GUIDE.md``, ``RULINGS.md``, ``rubric-v0.4.0.md``, ``menu-v0.9-234.md``) and
  ``prompts/*.md`` (the labelling, reconciliation and verification prompts).

``STORE_DIR`` is a read-only copy of a production store; only the four head snapshots :data:`HEADS` under
``data/<table>/batch-import/`` (duckdb) and the text blobs (``blobs/<digest>``) are read. Neither path is written into
the set.

Checks (any failure refuses, nothing is written): ``select.py`` run on ``pool.jsonl`` reproduces ``selected.jsonl``
byte for byte; each selected document's store row (corpus, stack_relevance, text_usability and doc_class heads, read
as ``pool.py`` reads them) matches its selection row (title, doc_type, channel, licence, basis role, text digest,
text quality, relevance confidence, chars; relevant, usable, text ok); the blob hashes to its digest, decodes as
UTF-8 and equals the labellers' copy; no selected document is a 2026-10-17a document; every label file names its
item; every verified item's verdicts follow its disputes in order; the packet menu is ``# <header>`` + the
production ``menu_text`` of the 234-entry taxonomy. Without ``--allow-partial`` every item must have an L1, an L2, a
pre-verification and a final label, every reconciled item an R label, and every verified item an Opus label and,
when it has disputes, verdicts.
``--check-pool`` also re-runs ``pool.py`` on the store and compares its output with ``pool.jsonl`` (reads ~11k
blobs). A head that is no longer the newest snapshot of its table is reported (``pool.py`` takes the newest).

What it writes under the set directory (``--write``; without it, it only checks and reports):

* ``sources/mirror-docs.jsonl``: per item the store's metadata (doc_id, title, doc_type, group, band, channel,
  channel_group (``gate.channel_groups``), license_class, basis_role, text_digest, chars, relevance_conf,
  text_quality); ``sources/sampling.json``: seed, quotas, bands and group map (parsed from ``select.py``), the pool's
  row count and sha256, the selection's sha256, the store heads, the exclusion;
* ``scripts/sampling/pool.py`` and ``select.py``: byte copies (the pool itself is copied only below 2 MB);
* ``texts/g2-NNNN.txt.gz``: the texts (deterministic gzip);
* ``adjudication/adj-X.jsonl`` (L1), ``adj-Y.jsonl`` (L2), ``astra-reconciler.jsonl`` (R), ``pre-verification.jsonl``
  (G0), ``final.jsonl`` (the labels of record), ``opus.jsonl``: the label files verbatim, one JSON object per item
  (sorted keys); ``verification.jsonl``: per verified item the blinding key (``swap``: gold shown as Q) and disputes
  with the adjudicator's verdicts; ``verification-first-run.jsonl``; ``verify-sample.json``, ``estimates.json``;
  ``adjudication/STAGED.json``: which labels are of record, the reconciled items, whether the staging is partial;
* ``adjudication/scripts/``: the merge rule and verification scripts, byte copies except lines naming workspace
  paths or a person (replaced by a marker; ``packet.json`` keeps both sha256);
* ``adjudication/texts.sha256``: sha256 of each labeller text; ``adjudication/packet.json``: sha256 of the guide,
  rulings, rubric, menu and prompts as the labellers had them;
* ``adjudication/GUIDE.md``, ``RULINGS.md`` and ``adjudication/prompts/<name>.md``, with a provenance header.

``freeze.py`` then builds ``items.jsonl``, ``gold.jsonl`` and ``manifest.json`` from these staged files alone.
"""
from __future__ import annotations

import ast
import gzip
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path.cwd()
OUT = ROOT / "evals/technique_staged/2026-10-18b"
IDS = [f"g2-{n:04d}" for n in range(1, 121)]
TAXONOMY = "taxonomy/taxonomy-v0.9-234.yaml"
EXCLUDED_SET = "evals/technique_staged/2026-10-17a"
#: The store snapshots the pool was drawn from (``pool.py``: the newest ``*.full.parquet`` of each table under
#: ``data/<table>/batch-import/`` when it ran, 2026-10-18).
HEADS = {"corpus": "20261017T175503Z_rz3vvhk92wt9r.full.parquet",
         "stack_relevance": "20261017T175521Z_rah6b8pyfs0e9.full.parquet",
         "text_usability": "20261017T175521Z_rah6b8pyfs0e9.full.parquet",
         "doc_class": "20261017T175521Z_rah6b8pyfs0e9.full.parquet"}
LABEL_SOURCES = ("final", "R", "L1", "L2")
POOL_COPY_LIMIT = 2_000_000
SELECTION_FIELDS = ("title", "doc_type", "channel", "license_class", "basis_role", "text_digest", "text_quality",
                    "relevance_conf", "chars")
MENU_HEADER = "# Technique taxonomy v0.9 (annotator view, 234 entries + 9 group nodes)\n\n"
#: Workspace or absolute paths a copied sampling script must not carry (spelled in pieces so that this file passes
#: the set-files path check itself).
WORKSPACE_MARKERS = re.compile("|".join(re.escape(a + b) for a, b in (("/ho", "me/"), ("/t", "mp/"), ("im", "pl/")))
                               + "|(?i:" + "mi" + "mir)")
GUIDE_HEADER = ("<!-- Copied verbatim from the labelling workspace (guide v2.0) for provenance. The menu file it\n"
                "names (menu-v0.9-234.md) is the production menu text of taxonomy/taxonomy-v0.9-234.yaml under a\n"
                "one-line title; the rubric file it names (rubric-v0.4.0.md) is not part of this set (its sha256 is\n"
                "in packet.json). -->\n\n")
RULINGS_HEADER = ("<!-- Copied verbatim from the labelling workspace for provenance: the binding rulings the\n"
                  "labellers and the reconciler applied (1-40 from 2026-10-17a's labelling, 41-46 new for gold\n"
                  "v2). -->\n\n")
PROMPT_HEADER = ("<!-- Copied verbatim from the labelling workspace for provenance: the task prompt of one labelling\n"
                 "pass (L1, L2), the reconciliation (RECONCILE) or the verification adjudication (VERIFY-ADJ:\n"
                 "written for a Codex adjudicator; used, with an agent preamble, by a Sonnet subagent because Codex\n"
                 "was out of credits); {{ITEMS}} was filled per batch. -->\n\n")
LABELLING_SCRIPTS = ("merge.py", "verify_sample.py", "disputes.py", "verify_apply.py")
#: Lines of a copied labelling script that name a workspace path or a person (pieces, as above) are replaced.
REDACT_LINE = re.compile(WORKSPACE_MARKERS.pattern + "|" + "Tom" + "[a\u00e1]\u0161|(?i:" + "gaven" + ")")
REDACTED = "# [line removed at staging: it names workspace paths or a person; packet.json has the original sha256]"


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def gz(text: str) -> bytes:
    return gzip.compress(text.encode("utf-8"), compresslevel=9, mtime=0)


def dumps(rows: list[dict]) -> str:
    return "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows)


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text("utf-8").splitlines() if x.strip()]


def select_constants(path: Path) -> dict:
    """``SEED``, ``QUOTA``, ``BANDS`` and ``GROUP`` of ``select.py`` (literal assignments, not executed)."""
    out = {}
    for node in ast.parse(path.read_text("utf-8")).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name in ("SEED", "QUOTA", "BANDS", "GROUP"):
                out[name] = ast.literal_eval(node.value)
    missing = {"SEED", "QUOTA", "BANDS", "GROUP"} - set(out)
    if missing:
        raise SystemExit(f"select.py: no literal {sorted(missing)}")
    return out


def run_to_stdout(script: Path, *args: str) -> list[str]:
    """Run a workspace script whose last argument is its output file, with ``/dev/stdout`` as that file."""
    res = subprocess.run([sys.executable, "-I", str(script), *args, "/dev/stdout"], capture_output=True, text=True,
                         check=True)
    return res.stdout.splitlines()


def redact(text: str) -> str:
    return "".join((REDACTED + "\n") if REDACT_LINE.search(line) else line for line in text.splitlines(True))


def store_rows(store: Path, doc_ids: list[str]) -> dict[str, dict]:
    """Per doc_id the four heads' fields, read as ``pool.py`` reads them (read only)."""
    import duckdb

    con = duckdb.connect()
    con.execute("SET memory_limit = '2GB'; SET threads = 2")
    p = {t: str(store / "data" / t / "batch-import" / name) for t, name in HEADS.items()}
    rows = con.execute(f"""
        with c as (select doc_id, metadata.title as title, identity.anchor_source_id as anchor,
               [{{'source_id': m.source_id, 'channel': m.channel, 'license_class': m.license_class}}
                for m in identity.members] as members,
               content.current_text_digest as dig, content.current_role as role, content.current_source_id as cur_src,
               content.current_text_status as tst, content.current_text_quality as tq
               from read_parquet('{p['corpus']}') where doc_id in (select unnest(?))),
         r as (select doc_id, verdict, confidence from read_parquet('{p['stack_relevance']}') where status = 'ok'),
         u as (select doc_id, verdict as usab from read_parquet('{p['text_usability']}') where status = 'ok'),
         d as (select doc_id, doc_type from read_parquet('{p['doc_class']}') where status = 'ok')
        select c.*, r.verdict, r.confidence, u.usab, d.doc_type
        from c left join r using (doc_id) left join u using (doc_id) left join d using (doc_id)""",
                       [doc_ids]).fetchall()
    cols = [x[0] for x in con.description]
    out: dict[str, dict] = {}
    for row in rows:
        r = dict(zip(cols, row, strict=True))
        if r["doc_id"] in out:
            raise SystemExit(f"corpus head has two rows for {r['doc_id']}")
        out[r["doc_id"]] = r
    return out


def head_notes(store: Path) -> list[str]:
    notes = []
    for t, name in HEADS.items():
        d = store / "data" / t / "batch-import"
        if not (d / name).is_file():
            raise SystemExit(f"store head {t}/batch-import/{name} is missing")
        newest = max(x.name for x in d.glob("*.full.parquet"))
        if newest != name:
            notes.append(f"{t}: a newer snapshot {newest} exists (pool.py would now read it)")
    return notes


def main(ws: Path, store: Path, labels: str, allow_partial: bool, check_pool: bool, write: bool) -> int:
    sys.path.insert(0, str(ROOT))
    from stack import gate
    from stack.stage_runner import load_path_module

    problems: list[str] = []
    notes = head_notes(store)
    sel_path = ws / "selection" / "selected.jsonl"
    pool_path = ws / "selection" / "pool.jsonl"
    selected = read_jsonl(sel_path)
    if [r["item_id"] for r in selected] != IDS:
        raise SystemExit(f"{sel_path.name}: expected {IDS[0]}..{IDS[-1]} in order")
    sampling_scripts = {name: (ws / "scripts" / name).read_bytes() for name in ("pool.py", "select.py")}
    for name, data in sampling_scripts.items():
        if WORKSPACE_MARKERS.search(data.decode("utf-8")):
            problems.append(f"scripts/{name} names a workspace path")
    redo = run_to_stdout(ws / "scripts" / "select.py", str(pool_path))
    rows_out = [x for x in redo if x.startswith("{")]
    if "".join(x + "\n" for x in rows_out) != sel_path.read_text("utf-8"):
        problems.append("select.py on pool.jsonl does not reproduce selected.jsonl")
    pool_bytes = pool_path.read_bytes()
    pool_rows = sum(1 for x in pool_bytes.decode("utf-8").splitlines() if x.strip())
    if check_pool:
        out = run_to_stdout(ws / "scripts" / "pool.py", str(store), str(ROOT / EXCLUDED_SET / "items.jsonl"))
        again = [x for x in out if x.startswith("{") and not x.startswith('{"heads"')]
        if "".join(x + "\n" for x in again).encode("utf-8") != pool_bytes:
            problems.append("pool.py on the store does not reproduce pool.jsonl")
    excluded = {r["doc_id"] for r in read_jsonl(ROOT / EXCLUDED_SET / "items.jsonl")}
    groups = gate.channel_groups()
    found = store_rows(store, [r["doc_id"] for r in selected])
    meta, texts = [], {}
    for s in selected:
        iid, did = s["item_id"], s["doc_id"]
        if did in excluded:
            problems.append(f"{iid}: doc {did} is a {EXCLUDED_SET} document")
        r = found.get(did)
        if r is None:
            problems.append(f"{iid}: doc {did} not in the corpus head")
            continue
        if (r["verdict"], r["usab"], r["tst"]) != ("relevant", "usable", "ok"):
            problems.append(f"{iid}: store verdicts {(r['verdict'], r['usab'], r['tst'])}")
        raw = (store / "blobs" / str(r["dig"])).read_bytes()
        if sha(raw) != r["dig"]:
            problems.append(f"{iid}: blob does not hash to its digest")
        if (ws / "texts" / f"{iid}.txt").read_bytes() != raw:
            problems.append(f"{iid}: the labellers' text differs from the store's current text")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            problems.append(f"{iid}: the text is not UTF-8")
            continue
        members = r["members"] or []
        anchor = next((m for m in members if m["source_id"] == r["anchor"]), {})
        supplier = next((m for m in members if m["source_id"] == r["cur_src"]), None) or anchor
        now = {"title": r["title"], "doc_type": r["doc_type"], "channel": anchor.get("channel"),
               "license_class": supplier.get("license_class"), "basis_role": r["role"], "text_digest": r["dig"],
               "text_quality": r["tq"], "relevance_conf": r["confidence"], "chars": len(text)}
        diff = [k for k in SELECTION_FIELDS if now[k] != s[k]]
        if diff:
            problems.append(f"{iid}: store differs from the selection in {diff}")
        if now["channel"] not in groups:
            problems.append(f"{iid}: channel {now['channel']!r} has no channel group")
            continue
        meta.append({"item_id": iid, "doc_id": did, **now, "channel_group": groups[now["channel"]],
                     "group": s["group"], "band": s["band"]})
        texts[iid] = text
    lab: dict[str, list[dict]] = {}
    missing: dict[str, list[str]] = {}
    r_list = ws / "labels" / "R-items.json"
    r_items = sorted(json.loads(r_list.read_text("utf-8"))) if r_list.is_file() else []
    for kind, sub, ids in (("adj-X", "L1", IDS), ("adj-Y", "L2", IDS), ("astra-reconciler", "R", r_items),
                           ("pre-verification", "G0", IDS), ("final", labels, IDS)):
        rows = []
        for iid in ids:
            f = ws / "labels" / sub / f"{iid}.json"
            if not f.is_file():
                missing.setdefault(kind, []).append(iid)
                continue
            d = json.loads(f.read_text("utf-8"))
            if d.get("item_id") != iid:
                problems.append(f"labels/{sub}/{iid}.json: item_id {d.get('item_id')!r}")
            rows.append(d)
        lab[kind] = rows
    sample = json.loads((ws / "verify" / "sample.json").read_text("utf-8"))
    sample_ids = [x["item_id"] for x in sample]
    opus = []
    for iid in sample_ids:
        f = ws / "verify" / "opus" / f"{iid}.json"
        if f.is_file():
            opus.append(json.loads(f.read_text("utf-8")))
        else:
            missing.setdefault("opus", []).append(iid)
    key = json.loads((ws / "verify" / "key.json").read_text("utf-8")) if (ws / "verify" / "key.json").is_file() else {}
    verification, first_run = [], []
    for iid in sample_ids:
        k = key.get(iid)
        if k is None:
            missing.setdefault("verification", []).append(iid)
            continue
        f = ws / "verify" / "adj" / f"{iid}.json"
        a = json.loads(f.read_text("utf-8")) if f.is_file() else None
        if k["disputes"] and a is None:
            missing.setdefault("verification", []).append(iid)
            continue
        verdicts = (a or {}).get("verdicts") or []
        if [v["slug"] for v in verdicts] != [d["slug"] for d in k["disputes"]]:
            problems.append(f"verify/adj/{iid}.json: verdicts do not follow the disputes of key.json")
        verification.append({"item_id": iid, "swap": k["swap"], "disputes": k["disputes"],
                             "adjudicator": (a or {}).get("adjudicator"), "verdicts": verdicts})
        f1 = ws / "verify" / "adj-s1-first" / f"{iid}.json"
        if f1.is_file():
            first_run.append(json.loads(f1.read_text("utf-8")))
    est_path = ws / "verify" / "estimates.json"
    estimates = est_path.read_bytes() if est_path.is_file() else None
    lscripts = {n: (ws / "scripts" / n).read_bytes() for n in LABELLING_SCRIPTS if (ws / "scripts" / n).is_file()}
    if (len(lscripts) < len(LABELLING_SCRIPTS) or estimates is None) and not allow_partial:
        problems.append("verify/estimates.json or a labelling script (merge, verify_sample, disputes, verify_apply) "
                        "is missing")
    if missing and not allow_partial:
        problems += [f"labels missing ({kind}): {len(v)} items, e.g. {', '.join(v[:5])}" for kind, v in missing.items()]
    tx = load_path_module("_stage_18b_taxonomy", ROOT / "workflows/annotate/technique_staged/taxonomy.py")
    menu = (ws / "packet-common" / "menu-v0.9-234.md").read_bytes()
    if menu.decode("utf-8") != MENU_HEADER + tx.menu_text(tx.load_taxonomy(ROOT / TAXONOMY)):
        problems.append(f"packet-common/menu-v0.9-234.md is not the menu text of {TAXONOMY}")
    if problems:
        print("\n".join(problems))
        return 1
    consts = select_constants(ws / "scripts" / "select.py")
    prompts = {p.stem: p.read_bytes() for p in sorted((ws / "prompts").glob("*.md"))}
    sampling = {"seed": consts["SEED"], "bands": list(consts["BANDS"]),
                "quotas": {g: list(q) for g, q in consts["QUOTA"].items()}, "doc_type_groups": consts["GROUP"],
                "pool": {"rows": pool_rows, "sha256": sha(pool_bytes), "bytes": len(pool_bytes),
                         "copied": len(pool_bytes) < POOL_COPY_LIMIT, "reproduced_from_store": check_pool},
                "selected": {"rows": len(selected), "sha256": sha(sel_path.read_bytes()),
                             "reproduced_by_select_py": True},
                "store_heads": {t: name.removesuffix(".full.parquet") for t, name in HEADS.items()},
                "exclusion": f"the documents of {EXCLUDED_SET} (items.jsonl doc_id) and their same_raw_doc_ids",
                "procedure": "scripts/sampling/pool.py (candidate pool) then scripts/sampling/select.py (stratified "
                             "sample); both take paths as arguments"}
    packet = {"guide": {"file": "GOLD-GUIDE.md", "sha256": sha((ws / "packet-common" / "GOLD-GUIDE.md").read_bytes())},
              "rulings": {"file": "RULINGS.md", "sha256": sha((ws / "packet-common" / "RULINGS.md").read_bytes())},
              "rubric": {"file": "rubric-v0.4.0.md",
                         "sha256": sha((ws / "packet-common" / "rubric-v0.4.0.md").read_bytes())},
              "menu": {"file": "menu-v0.9-234.md", "sha256": sha(menu),
                       "is": f"MENU_HEADER + menu_text({TAXONOMY})"},
              "prompts": {name: sha(data) for name, data in prompts.items()}}
    packet["labelling_scripts"] = {
        n: {"sha256": sha(b), "staged_sha256": sha(redact(b.decode("utf-8")).encode("utf-8"))}
        for n, b in lscripts.items()}
    staged = {"labels_of_record": labels, "partial": bool(missing), "reconciled_items": r_items,
              "missing": {k: v for k, v in sorted(missing.items())},
              "counts": {k: len(v) for k, v in lab.items()} | {"opus": len(opus), "verification": len(verification),
                                                               "verification_first_run": len(first_run)}}
    print(f"checked: {len(meta)} store documents (heads, digests, labeller copies identical); select.py reproduces "
          f"the selection; labels of record {labels!r}: {staged['counts']}"
          + (f"; partial, missing {({k: len(v) for k, v in missing.items()})}" if missing else ""))
    for n in notes:
        print(f"note: {n}")
    if not write:
        return 0
    for d in ("sources", "texts", "adjudication/prompts", "adjudication/scripts", "scripts/sampling"):
        (OUT / d).mkdir(parents=True, exist_ok=True)
    (OUT / "sources" / "mirror-docs.jsonl").write_text(dumps(meta), encoding="utf-8")
    (OUT / "sources" / "sampling.json").write_text(json.dumps(sampling, indent=2, ensure_ascii=False) + "\n",
                                                   encoding="utf-8")
    for name, data in sampling_scripts.items():
        (OUT / "scripts" / "sampling" / name).write_bytes(data)
    if sampling["pool"]["copied"]:
        (OUT / "sources" / "pool.jsonl").write_bytes(pool_bytes)
    for iid, text in texts.items():
        (OUT / "texts" / f"{iid}.txt.gz").write_bytes(gz(text))
    for kind, rows in lab.items():
        (OUT / "adjudication" / f"{kind}.jsonl").write_text(dumps(rows), encoding="utf-8")
    (OUT / "adjudication" / "opus.jsonl").write_text(dumps(opus), encoding="utf-8")
    (OUT / "adjudication" / "verify-sample.json").write_text(json.dumps(sample, indent=2, ensure_ascii=False) + "\n",
                                                             encoding="utf-8")
    (OUT / "adjudication" / "verification.jsonl").write_text(dumps(verification), encoding="utf-8")
    (OUT / "adjudication" / "verification-first-run.jsonl").write_text(dumps(first_run), encoding="utf-8")
    if estimates is not None:
        (OUT / "adjudication" / "estimates.json").write_bytes(estimates)
    for n, b in lscripts.items():
        (OUT / "adjudication" / "scripts" / n).write_text(redact(b.decode("utf-8")), encoding="utf-8")
    (OUT / "adjudication" / "STAGED.json").write_text(json.dumps(staged, indent=2) + "\n", encoding="utf-8")
    (OUT / "adjudication" / "packet.json").write_text(json.dumps(packet, indent=2) + "\n", encoding="utf-8")
    (OUT / "adjudication" / "texts.sha256").write_text(
        "".join(f"{sha((ws / 'texts' / f'{i}.txt').read_bytes())}  {i}.txt\n" for i in IDS), encoding="utf-8")
    (OUT / "adjudication" / "GUIDE.md").write_text(
        GUIDE_HEADER + (ws / "packet-common" / "GOLD-GUIDE.md").read_text("utf-8"), encoding="utf-8")
    (OUT / "adjudication" / "RULINGS.md").write_text(
        RULINGS_HEADER + (ws / "packet-common" / "RULINGS.md").read_text("utf-8"), encoding="utf-8")
    for name, data in prompts.items():
        (OUT / "adjudication" / "prompts" / f"{name}.md").write_text(PROMPT_HEADER + data.decode("utf-8"),
                                                                     encoding="utf-8")
    print(f"wrote sources/ (mirror-docs, sampling), scripts/sampling/, {len(texts)} texts, adjudication/ "
          f"({', '.join(lab)}, opus, verification, verify-sample, estimates, packet, STAGED, GUIDE, RULINGS, "
          f"{len(prompts)} prompts, {len(lscripts)} scripts)")
    return 0


if __name__ == "__main__":
    argv = sys.argv[1:]
    flags = {a for a in argv if a in ("--write", "--allow-partial", "--check-pool")}
    labels = "final"
    if "--labels" in argv:
        k = argv.index("--labels")
        labels = argv[k + 1] if k + 1 < len(argv) else ""
        argv = argv[:k] + argv[k + 2:]
    args = [a for a in argv if a not in flags]
    if len(args) != 2 or labels not in LABEL_SOURCES:
        raise SystemExit(__doc__)
    raise SystemExit(main(Path(args[0]), Path(args[1]), labels, "--allow-partial" in flags, "--check-pool" in flags,
                          "--write" in flags))
