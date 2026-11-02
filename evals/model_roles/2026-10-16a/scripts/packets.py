"""Labeller packets for model_roles set 2026-10-16a (production code at HEAD; no LLM).

Run from the repo root: ``uv run --no-sync python evals/model_roles/2026-10-16a/scripts/packets.py <out-dir>
[item_id ...]`` (default items: the gold-gap items of ``manifest.json``). Writes per packet ``rubric.md`` (the rubric
the frozen prompt includes, as the judge sees it), ``vocab-index.tsv`` and per document ``docs/<item_id>/text.md`` and
``model-menu.md`` (the menu as the judge sees it, ``render.menu_text``), whose header lists the menu nodes without a
gold row and the gold rows no longer on the menu (``gold-gaps.jsonl``). The labelling protocol is 2026-10-13a's
(``evals/model_roles/2026-10-13a/labels/labelling-instructions.md``). The ``<out-dir>`` should be outside the repo.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path.cwd()
SET = ROOT / "evals/model_roles/2026-10-16a"


def main(out: Path, ids: list[str]) -> None:
    sys.path.insert(0, str(ROOT))
    import yaml

    from stack.evals.freeze import read_text_gz, text_path
    from stack.stage_runner import load_path_module

    mr = load_path_module("_packets_model_roles_main", ROOT / "workflows/annotate/model_roles/main.py")
    manifest = json.loads((SET / "manifest.json").read_text("utf-8"))
    pf = mr.prompt_file(manifest["frozen_with"]["model_roles_prompt"])
    c = mr.match.scanner_for(ROOT / "configs")
    if c.vocab.digest != manifest["frozen_with"]["models_yaml_sha256"]:
        raise SystemExit("configs/models.yaml is not the vocabulary the set was frozen with: the menus would differ")
    items = {json.loads(x)["item_id"]: json.loads(x) for x in (SET / "items.jsonl").read_text("utf-8").splitlines()}
    gaps = {json.loads(x)["item_id"]: json.loads(x) for x in (SET / "gold-gaps.jsonl").read_text("utf-8").splitlines()}
    ids = ids or list(manifest["gold"]["gap_items"])
    out.mkdir(parents=True, exist_ok=True)
    (out / "rubric.md").write_text("\n\n".join((pf.path.parent / inc).read_text("utf-8") for inc in pf.includes),
                                   encoding="utf-8")
    raw = yaml.safe_load((ROOT / "configs/models.yaml").read_text("utf-8"))
    lines = ["node_id\tkind\tname\tparent\tmaker\tmentions\tweak_mentions"]
    for nid, n in raw["nodes"].items():
        lines.append("\t".join([nid, str(n.get("kind", "")), str(n.get("name", "")), str(n.get("parent") or ""),
                                str(n.get("maker") or ""), "; ".join(n.get("mentions") or []),
                                "; ".join(n.get("weak_mentions") or [])]))
    (out / "vocab-index.tsv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    for iid in ids:
        it = items[iid]
        text = read_text_gz(text_path(SET, iid))
        scan = c.scanner.scan(text)
        menu, beyond, _fills = mr.fills(text, scan, c.vocab)
        if not it["production_asks"]:
            menu = []
        assert [e.node_id for e in menu] == [m["node_id"] for m in it["input"]["menu"]], iid
        g = gaps.get(iid, {})
        head = (f"# Model menu for {iid}\n\nTitle: {it['input']['title']}\nCharacters: {len(text)}\n"
                f"Menu nodes: {len(menu)}" + ("" if menu else " (no strong mention: production makes no call)") + "\n"
                f"Beyond the window (withheld): {', '.join(beyond) or '—'}\n"
                f"Menu nodes without a gold row: {', '.join(g.get('menu_not_in_gold') or []) or '—'}\n"
                f"Gold rows no longer on the menu: graded {', '.join(g.get('gold_off_menu_graded') or []) or '—'}; "
                f"all-zero {', '.join(g.get('gold_off_menu_all_zero') or []) or '—'}\n")
        d = out / "docs" / iid
        d.mkdir(parents=True, exist_ok=True)
        (d / "text.md").write_text(text, encoding="utf-8")
        (d / "model-menu.md").write_text(head + mr.render.menu_text(menu), encoding="utf-8")
        print(iid, len(text), len(menu))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    main(Path(sys.argv[1]), sys.argv[2:])
