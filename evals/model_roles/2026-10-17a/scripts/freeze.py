"""Freeze the model_roles eval set 2026-10-17a (registry version 3: every document judged; prompt model-roles-v2).

Run from the repo root: ``uv run --no-sync python evals/model_roles/2026-10-17a/scripts/freeze.py [candidate models.yaml]``.
No LLM call. Items (37):

* an-0001..an-0025: set 2026-10-16a's items and whole texts (byte-identical, ``full_text_digest`` checked);
* an-0026..an-0030: real texts of ``evals/screen/2026-10-12a`` with no matcher hit and, read by the set's author, no
  named AI model (scr-0087, scr-0006, scr-0054, scr-0042, scr-0040; copied byte-identical);
* an-0031..an-0037: synthetic texts (``synthetic/*.md``, ``synthetic: true`` on the item): off-catalogue models with
  fictional names (syn-01..04) and family mentions the text does or does not concretize (ref-01..03).

Per item, with production code at HEAD and the current ``configs/models.yaml`` (v0.9 since 2026-10-18): ``input.model_mentions`` (the
scan, for ``stack eval model_mentions``), ``input.menu``/``input.beyond_window`` (the judge's menu), ``production_asks``
(always true since registry version 3), ``input.fills_digest`` (``stack.evals.annotators.fills_digest`` of
``main.fills``) and ``input_digest`` (sha256 of the user message under ``model-roles-v2``).

Gold (``gold.jsonl``): 2026-10-16a's (= 2026-10-13a's ``gold-vocab-v0.5.jsonl``) with ``gold-remap.jsonl`` applied to
the 8 items whose menus moved (each entry: move a row to the node the text names, add a row graded from the text, drop
an all-zero row no longer on the menu, or set a row's roles; every quote checked verbatim in the text), plus
``gold-new.jsonl`` for an-0026..an-0037 (the remap's ``section: v0.7`` / ``section: v0.9`` entries follow the catalogue v0.7 folds and
removals, D595, and the v0.9 removals, aliases and splits, D598 / D623). Every gold node is checked to exist in the current vocabulary and, when a candidate file is given,
in it too. With a candidate ``models.yaml`` argument (the next catalogue) the script also writes
``candidate-moves.jsonl``: the items whose menu would differ under it, in its nodes or only in its text (names,
glosses), i.e. the items the input check would refuse after that catalogue lands until this script is re-run.
"""
from __future__ import annotations

import copy
import gzip
import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path.cwd()
SRC = ROOT / "evals/model_roles/2026-10-16a"
SCREEN = ROOT / "evals/screen/2026-10-12a"
OUT = ROOT / "evals/model_roles/2026-10-17a"
SET_ID = "2026-10-17a"
PROMPT = "model-roles-v2"
REAL_EMPTY = ["scr-0087", "scr-0006", "scr-0054", "scr-0042", "scr-0040"]
SYNTHETIC = ["syn-01", "syn-02", "syn-03", "syn-04", "ref-01", "ref-02", "ref-03"]
ROLE_NAMES = ("mentioned", "cited_work", "discussed", "usage_experience", "built_on", "instrument", "modified",
              "evaluated_behavioral", "analyzed_internals", "introduced")


def sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows), "utf-8")


def _norm(s: str) -> str:
    return " ".join(s.replace(" ", " ").split())


def _rename_mentions(lab: dict, old: str, new: str) -> None:
    """Gold ``mentions`` naming ``old`` now name ``new`` (dropped when ``new`` is already a mention)."""
    have = {m["node_id"] for m in lab["mentions"]}
    lab["mentions"] = [({**m, "node_id": new} if m["node_id"] == old else m) for m in lab["mentions"]
                       if not (m["node_id"] == old and new in have)]


def apply_remap(gold: dict[str, dict], remap: list[dict], texts: dict[str, str]) -> None:
    """Apply ``gold-remap.jsonl`` in place (module docstring); a quote not in the text or a bad entry fails.
    Actions: ``move`` (a row to another node; its mentions follow), ``merge`` (a row into an existing row:
    roles by max, notes joined; its mentions follow), ``add``, ``drop`` (all-zero rows only), ``set`` (a row's
    roles), ``mention`` (a gold mention added or renamed, for ``stack eval model_mentions``), ``off_catalogue`` (a
    row whose node left the catalogue without an alias: the row and its mentions go, the name is kept as an
    out-of-vocabulary mention and an off-menu model)."""
    for e in remap:
        lab = gold[e["item_id"]]["label"]
        rows = {m["node_id"]: m for m in lab["models"]}
        tag = f"2026-10-17a {e['section']} remap" if e.get("section") else "2026-10-17a remap"
        if e["action"] == "move":
            row = rows.pop(e["from_node"])
            assert e["node_id"] not in rows, e
            row["node_id"] = e["node_id"]
            row["note"] = f"{row.get('note') or ''} [{tag} from {e['from_node']}: {e['why']}]".strip()
            rows[e["node_id"]] = row
            _rename_mentions(lab, e["from_node"], e["node_id"])
        elif e["action"] == "merge":
            row, into = rows.pop(e["from_node"]), rows[e["node_id"]]
            into["roles"] = {r: max(int(into["roles"].get(r, 0)), int(row["roles"].get(r, 0))) for r in ROLE_NAMES}
            into["quote"] = into.get("quote") or row.get("quote")
            into["note"] = f"{into.get('note') or ''} [{tag}, merged {e['from_node']}: {e['why']}]".strip()
            _rename_mentions(lab, e["from_node"], e["node_id"])
        elif e["action"] == "add":
            assert e["node_id"] not in rows, e
            rows[e["node_id"]] = {"node_id": e["node_id"], "quote": e.get("quote"),
                                  "roles": {r: int(e["roles"].get(r, 0)) for r in ROLE_NAMES},
                                  "note": f"[{tag}: {e['why']}]"}
        elif e["action"] == "drop":
            row = rows.pop(e["node_id"])
            assert not any(row["roles"].values()), e
        elif e["action"] == "set":
            row = rows[e["node_id"]]
            row["roles"] = {r: int(e["roles"].get(r, 0)) for r in ROLE_NAMES}
            if "quote" in e:
                row["quote"] = e["quote"]
            row["note"] = f"{row.get('note') or ''} [{tag}: {e['why']}]".strip()
        elif e["action"] == "off_catalogue":
            # the node left the catalogue (no node, no alias): the row and its mentions leave the in-vocabulary
            # gold; the model stays recorded as an out-of-vocabulary mention and an off-menu model (no node)
            rows.pop(e["node_id"])
            lab["mentions"] = [m for m in lab["mentions"] if m["node_id"] != e["node_id"]]
            lab.setdefault("mentions_out_of_vocab", []).append({"name": e["name"]})
            lab.setdefault("off_menu_models", []).append({"name": e["name"], "node_id": None})
        elif e["action"] == "mention":
            if e.get("from_node"):
                _rename_mentions(lab, e["from_node"], e["node_id"])
            else:
                assert e["node_id"] not in {m["node_id"] for m in lab["mentions"]}, e
                lab["mentions"].append({"node_id": e["node_id"], "surface": e["surface"]})
        else:
            raise SystemExit(f"unknown remap action: {e}")
        q = e.get("quote")
        assert q is None or _norm(q) in _norm(texts[e["item_id"]]), ("quote not in the text", e)
        lab["models"] = [rows[k] for k in sorted(rows)]


def text_file(set_dir: Path, iid: str) -> Path:
    """``iid``'s whole text: the set's ``texts/`` file, else the eval-text cache's (the texts are not in the
    repository, D790; ``tools/fetch_eval_texts.py`` fills the cache)."""
    from stack.evals.freeze import find_text, missing_text_message

    p = find_text(set_dir, iid)
    if p is None:
        raise SystemExit(missing_text_message(set_dir, iid))
    return p


def main(candidate: Path | None) -> None:
    sys.path.insert(0, str(ROOT))
    from stack.evals.annotators import fills_digest
    from stack.evals.freeze import gzip_text, read_text_gz, text_path
    from stack.stage_runner import load_path_module

    mr = load_path_module("_freeze17_model_roles_main", ROOT / "workflows/annotate/model_roles/main.py")
    c = mr.match.scanner_for(ROOT / "configs")
    pf = mr.prompt_file(PROMPT)
    # the items and their texts
    base, texts = [], {}
    for it in read_jsonl(SRC / "items.jsonl"):
        texts[it["item_id"]] = read_text_gz(text_file(SRC, it["item_id"]))
        assert sha(texts[it["item_id"]]) == it["input"]["full_text_digest"], it["item_id"]
        keep = {k: it["input"][k] for k in ("title", "text_digest", "basis_role", "full_text_digest", "full_text_chars")}
        base.append({**{k: it[k] for k in ("item_id", "doc_id", "source_item", "split", "stratum", "license_class")},
                     "input": keep})
    from stack.evals.freeze import items_file  # screen 2026-10-12a's items are archived (D795)

    screen = {i["item_id"]: i for i in read_jsonl(items_file(SCREEN))}
    n = len(base)
    for k, src_id in enumerate(REAL_EMPTY):
        it = screen[src_id]
        iid = f"an-{n + 1:04d}"
        n += 1
        texts[iid] = read_text_gz(text_file(SCREEN, src_id))
        assert sha(texts[iid]) == it["input"]["full_text_digest"], src_id
        base.append({"item_id": iid, "doc_id": it["doc_id"], "source_item": f"screen/2026-10-12a/{src_id}",
                     "split": "dev" if k % 2 == 0 else "test", "stratum": it["stratum"],
                     "license_class": it["license_class"],
                     "input": {"title": it["input"]["title"], "text_digest": it["input"]["text_digest"],
                               "basis_role": it["input"]["basis_role"],
                               "full_text_digest": it["input"]["full_text_digest"], "full_text_chars": len(texts[iid])}})
    for k, name in enumerate(SYNTHETIC):
        iid = f"an-{n + 1:04d}"
        n += 1
        text = (OUT / "synthetic" / f"{name}.md").read_text("utf-8")
        texts[iid] = text
        base.append({"item_id": iid, "doc_id": f"synthetic-{name}", "source_item": f"synthetic/{name}.md",
                     "split": "dev" if k % 2 == 0 else "test", "synthetic": True,
                     "stratum": {"channel_group": "synthetic"}, "license_class": "open",
                     "input": {"title": text.splitlines()[0], "text_digest": sha(text), "basis_role": "synthetic",
                               "full_text_digest": sha(text), "full_text_chars": len(text)}})
    # production inputs
    items = []
    for b in base:
        text = texts[b["item_id"]]
        scan = c.scanner.scan(text)
        menu, beyond, fills = mr.fills(text, scan, c.vocab)
        payload = mr.match.scan_payload(c.scanner, text)
        inp = {**b["input"], "model_mentions": {"status": "empty" if mr.match.is_empty(scan) else "ok", "payload": payload},
               "menu": [{"node_id": e.node_id, "weak": e.weak} for e in menu], "beyond_window": beyond,
               "fills_digest": fills_digest(fills)}
        items.append({**b, "production_asks": True, "input": inp, "input_digest": sha(pf.user(fills))})
    # gold
    gold = {g["item_id"]: g for g in read_jsonl(SRC / "gold.jsonl")}
    apply_remap(gold, read_jsonl(OUT / "gold-remap.jsonl"), texts)
    for g in read_jsonl(OUT / "gold-new.jsonl"):
        assert g["item_id"] not in gold
        for m in g["label"]["models"]:
            assert _norm(m["quote"]) in _norm(texts[g["item_id"]]), g["item_id"]
        gold[g["item_id"]] = g
    assert sorted(gold) == [i["item_id"] for i in items]
    current = c.vocab.nodes
    nodes06 = None
    tmp = None
    if candidate is not None:
        tmp = Path(tempfile.mkdtemp())
        shutil.copytree(ROOT / "configs", tmp / "configs", ignore=shutil.ignore_patterns("plans"))
        shutil.copyfile(candidate, tmp / "configs" / "models.yaml")
        c06 = mr.match.scanner_for(tmp / "configs")
        nodes06 = c06.vocab.nodes
    for g in gold.values():
        for m in [*g["label"]["models"], *g["label"]["mentions"]]:
            assert m["node_id"] in current, (g["item_id"], m["node_id"], "not in the current vocabulary")
            assert nodes06 is None or m["node_id"] in nodes06, (g["item_id"], m["node_id"], "not in the candidate")
    # write
    (OUT / "texts").mkdir(parents=True, exist_ok=True)
    for it in items:
        text_path(OUT, it["item_id"]).write_bytes(gzip_text(texts[it["item_id"]]))
    write_jsonl(OUT / "items.jsonl", items)
    write_jsonl(OUT / "gold.jsonl", [gold[k] for k in sorted(gold)])
    moves = []
    if candidate is not None:
        for it in items:
            text = texts[it["item_id"]]
            menu06, _b, fills06 = mr.fills(text, c06.scanner.scan(text), c06.vocab)
            now, then = [m["node_id"] for m in it["input"]["menu"]], [e.node_id for e in menu06]
            if now != then or fills_digest(fills06) != it["input"]["fills_digest"]:
                moves.append({"item_id": it["item_id"], "added": [x for x in then if x not in now],
                              "removed": [x for x in now if x not in then],
                              "menu_text_only": now == then})
        write_jsonl(OUT / "candidate-moves.jsonl", moves)
        shutil.rmtree(tmp)
    manifest = {
        "task": "model_roles", "set_id": SET_ID,
        "source": "evals/model_roles/2026-10-16a (an-0001..0025), evals/screen/2026-10-12a (an-0026..0030), "
                  "synthetic/ (an-0031..0037)",
        "items": len(items), "items_sha256": hashlib.sha256((OUT / "items.jsonl").read_bytes()).hexdigest(),
        "splits": {s: sum(r["split"] == s for r in items) for s in ("dev", "test")},
        "frozen_with": {"models_yaml_sha256": c.vocab.digest, "models_yaml_version": c.vocab.validated.version,
                        "model_roles_prompt": PROMPT, "model_roles_prompt_digest": pf.digest},
        "gold": {"file": "gold.jsonl", "sha256": hashlib.sha256((OUT / "gold.jsonl").read_bytes()).hexdigest(),
                 "from": "evals/model_roles/2026-10-16a/gold.jsonl + gold-remap.jsonl + gold-new.jsonl",
                 "remapped_items": sorted({e["item_id"] for e in read_jsonl(OUT / "gold-remap.jsonl")})},
    }
    if candidate is not None:
        manifest["candidate"] = {"models_yaml_sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
                                 "moved_items": [m["item_id"] for m in moves]}
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"{len(items)} items; gold {len(gold)}; candidate moves: "
          f"{[m['item_id'] for m in moves] if candidate else 'not computed'}")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else None)
