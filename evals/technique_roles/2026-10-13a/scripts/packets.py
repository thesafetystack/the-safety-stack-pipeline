"""Labeller packets for the technique_roles set 2026-10-13a (the technique half of
``evals/model_roles/2026-10-13a/scripts/packets.py``, which imports code removed by D591), against HEAD.

Run from the repo root: ``uv run --no-sync python evals/technique_roles/2026-10-13a/scripts/packets.py <out dir>
[--rubric-of <prompt name>] [item_id ...]`` (no ids: every item). No LLM call. Writes ``<out>/rubric.md`` (the
judge-visible rubric the prompt includes, then its ``.notes.md``: metadata, changelog, design notes; default the
rubric of ``technique-roles-v1``, v0.3.1, which the set's gold was labelled against), ``<out>/technique-menu.md``
(the menu exactly as the judge sees it, ``render.full_menu`` of the current taxonomy file; refused when that file is
not the one in ``manifest.json`` ``frozen_with.taxonomy_sha256``) and ``<out>/docs/<item_id>/text.md`` (the frozen
whole text).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path.cwd()
SET = ROOT / "evals/technique_roles/2026-10-13a"
DEFAULT_RUBRIC_OF = "technique-roles-v1"


def main(out: Path, ids: list[str], rubric_of: str) -> None:
    sys.path.insert(0, str(ROOT))
    from stack.evals.freeze import read_text_gz, text_path
    from stack.stage_runner import load_path_module

    tr = load_path_module("_packets_technique_roles_main", ROOT / "workflows/annotate/technique_roles/main.py")
    frozen = json.loads((SET / "manifest.json").read_text("utf-8"))["frozen_with"]
    menu = tr.menu()
    if menu.taxonomy_sha256 != frozen.get("taxonomy_sha256"):
        raise SystemExit(f"{tr.TAXONOMY_PATH} is not the taxonomy the set was frozen with "
                         f"(sha256 {menu.taxonomy_sha256[:12]}, frozen {str(frozen.get('taxonomy_sha256'))[:12]})")
    pf = tr.prompt_file(rubric_of)
    out.mkdir(parents=True, exist_ok=True)
    parts = []
    for inc in pf.includes:
        parts.append((pf.path.parent / inc).read_text("utf-8"))
        notes = pf.path.parent / inc.replace(".md", ".notes.md")
        if notes.is_file():
            parts.append(notes.read_text("utf-8"))
    (out / "rubric.md").write_text("\n\n".join(parts), encoding="utf-8")
    (out / "technique-menu.md").write_text(menu.text + "\n", encoding="utf-8")
    items = [json.loads(x) for x in (SET / "items.jsonl").read_text("utf-8").splitlines() if x.strip()]
    chosen = [it for it in items if not ids or it["item_id"] in ids]
    unknown = sorted(set(ids) - {it["item_id"] for it in items})
    if unknown:
        raise SystemExit(f"not items of the set: {unknown}")
    for it in chosen:
        d = out / "docs" / it["item_id"]
        d.mkdir(parents=True, exist_ok=True)
        text = read_text_gz(text_path(SET, it["item_id"]))
        (d / "text.md").write_text(text, encoding="utf-8")
        print(it["item_id"], len(text))


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        raise SystemExit(__doc__)
    rubric_of = DEFAULT_RUBRIC_OF
    if "--rubric-of" in args:
        k = args.index("--rubric-of")
        rubric_of = args[k + 1]
        del args[k:k + 2]
    main(Path(args[0]), args[1:], rubric_of)
