"""Convert the taxonomy v0.9 draft TSVs into ``taxonomy/taxonomy-v0.9.yaml``.

The v0.9 draft is authored outside this repository as two tab-separated files: the technique
table (214 rows in the 2026-10-17 draft; columns :data:`TECHNIQUE_COLUMNS`) and the group-node
table (9 rows; columns :data:`GROUP_COLUMNS`). Both are written with standard CSV quoting (a field
holding a ``"`` is quoted and the quote doubled), so they are read with :mod:`csv`, not split on
tabs. The YAML is read by ``workflows/annotate/technique_staged/taxonomy.py``.

Usage (from the repo root)::

    uv run --no-sync python taxonomy/sources/convert_v09.py TECHNIQUES.tsv GROUPS.tsv \\
        --out taxonomy/taxonomy-v0.9.yaml

Mapping:

* list-valued columns: ``aliases_synonym``, ``aliases_instance``, ``aliases_legacy`` and the group
  ``aliases`` are `` | ``-separated; ``ml_methods`` and the group ``children`` are comma-separated;
* a group alias ending in ``(legacy)`` is a former name: it goes to ``aliases.legacy`` with the
  marker removed and is never shown to an annotator; the group ``family`` column (a v0.8 view) is
  dropped;
* empty values are omitted; ``scope_change`` must be empty (no mapping is defined for it, so a
  value there stops the conversion instead of being dropped);
* row order is the TSVs' order; list order is the cell's order;
* a provenance reference to one of the draft's review-round folders (``impl``, a slash, a number)
  followed by a comment id (``C`` and digits) is written ``tax-review C<nnn>`` (the taxonomy
  review's comment ids, e.g. ``origin: v0.9 add (D7; tax-review C464)``); any other such folder
  reference, with the round-internal item label that follows it (e.g. ``K12``, ``B06n``), is removed:
  internal round labels mean nothing outside the authoring workspace (``(pass 4; <ref> K12)`` becomes
  ``(pass 4)``); a bare folder reference becomes ``a review round``. Leftover empty brackets and
  separators are tidied. No path or round label of the authoring workspace enters the repository.
  The count of rewrites is printed. (Before 2026-10-18 such references were kept as ``impl-<n>``;
  ``taxonomy-v0.9-234.yaml`` was generated then and is kept byte for byte for its frozen sets.)
* ``--redact REGEX`` (repeatable, operator-supplied, never stored here) deletes matching text from
  every cell, e.g. a personal attribution inside a draft ``note``; the header records how many
  redactions were applied (not the patterns), so a rebuild must pass the same patterns.

The output is deterministic: the same two input files give the same bytes. The header records the
inputs' file names and sha256 (no paths). The script validates what the loader also checks
(unique slugs over techniques and group nodes, ``kind`` and ``status`` enums, ``parent`` and
``children`` resolve) and exits non-zero naming the first problem.

Stdlib + PyYAML only.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import re
import sys
from pathlib import Path
from typing import Any

import yaml

VERSION = "0.9"
STATUS = "draft"
KINDS = ("intervention", "monitoring", "assessment")
ENTRY_STATUSES = ("final", "provisional")

TECHNIQUE_COLUMNS = (
    "slug", "name", "v08_name", "kind", "parent", "origin", "definition", "scope_change",
    "aliases_synonym", "aliases_instance", "aliases_legacy", "absorbed", "ml_methods", "risk_note",
    "source_papers", "note", "status",
)
GROUP_COLUMNS = ("slug", "name", "family", "definition", "children", "aliases")

ALIAS_SEP = " | "
WORKSPACE_REF = re.compile(r"\bimpl[/](\d+)")
#: A folder reference followed by a round-internal item label (``K12``, ``B06n``, ``R5``): removed together.
LABELLED_REF = re.compile(r"\bimpl[/]\d+\s+[A-Z]{1,2}\d+[a-z]?\b")
#: A review comment reference inside a workspace reference: ``<folder ref> C464`` -> ``tax-review C464``.
REVIEW_REF = re.compile(r"\bimpl[/]\d+\s+(C\d+)\b")
LEGACY_MARK = "(legacy)"

NOTES = (
    "Generated from the v0.9 draft TSVs by taxonomy/sources/convert_v09.py; do not edit by hand. "
    "Alias types: `synonym` and `instance` aliases are shown to annotators (the menu's aka / e.g.); "
    "`legacy` aliases are former names, kept for lookup and migration only and never shown. "
    "Group nodes are not techniques of their own: they are the targets of generic mentions "
    "(a bare field or umbrella name) and list their children. `ml_methods` are typical "
    "implementations, not annotation options; `risk_note` is informal."
)


class ConvertError(ValueError):
    """A TSV the converter cannot map faithfully."""


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_tsv(path: Path, columns: tuple[str, ...]) -> list[dict[str, str]]:
    """Rows of a tab-separated file with exactly ``columns`` as its header (CSV quoting)."""
    with path.open(encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f, delimiter="\t"))
    if not rows:
        raise ConvertError(f"{path.name}: empty file")
    header, body = rows[0], rows[1:]
    if tuple(header) != columns:
        raise ConvertError(f"{path.name}: header {header} != expected {list(columns)}")
    out: list[dict[str, str]] = []
    for i, row in enumerate(body, start=2):
        if not row:
            continue
        if len(row) != len(columns):
            raise ConvertError(f"{path.name}: line {i}: {len(row)} fields, expected {len(columns)}")
        out.append({c: v.strip() for c, v in zip(columns, row)})
    return out


def tidy(value: str) -> str:
    """Tidy what a removed reference leaves: empty brackets, a separator before a closing bracket or
    after an opening one, doubled spaces."""
    for pat, rep in ((r"\(\s*[;,]?\s*\)", ""), (r"[;,]\s*\)", ")"), (r"\(\s*[;,]\s*", "("), (r"\s+([;,.])", r"\1"),
                     (r"\s{2,}", " ")):
        value = re.sub(pat, rep, value)
    return value.strip()


def scrub_refs(rows: list[dict[str, str]]) -> int:
    """Rewrite workspace references in place (module docstring): a review comment reference
    (:data:`REVIEW_REF`) to ``tax-review C<nnn>``; a labelled one (:data:`LABELLED_REF`) removed; any other
    (:data:`WORKSPACE_REF`) to ``a review round``; then :func:`tidy`;
    the number of rewrites."""
    n = 0
    for row in rows:
        for key, value in row.items():
            value, k1 = REVIEW_REF.subn(r"tax-review \1", value)
            value, k2 = LABELLED_REF.subn("", value)
            value, k3 = WORKSPACE_REF.subn("a review round", value)
            if k2 or k3:
                value = tidy(value)
            row[key] = value
            n += k1 + k2 + k3
    return n


def split_list(cell: str, sep: str) -> list[str]:
    parts = [p.strip() for p in cell.split(sep)] if cell else []
    if any(not p for p in parts):
        raise ConvertError(f"empty item in list cell {cell!r}")
    return parts


def technique_entry(row: dict[str, str]) -> dict[str, Any]:
    slug = row["slug"]
    if row["scope_change"]:
        raise ConvertError(f"{slug}: `scope_change` is set ({row['scope_change']!r}); no mapping is defined")
    for key in ("slug", "name", "kind", "definition", "status", "origin"):
        if not row[key]:
            raise ConvertError(f"{slug or '<no slug>'}: empty `{key}`")
    if row["kind"] not in KINDS:
        raise ConvertError(f"{slug}: kind {row['kind']!r} not in {KINDS}")
    if row["status"] not in ENTRY_STATUSES:
        raise ConvertError(f"{slug}: status {row['status']!r} not in {ENTRY_STATUSES}")
    aliases = {
        "synonym": split_list(row["aliases_synonym"], ALIAS_SEP),
        "instance": split_list(row["aliases_instance"], ALIAS_SEP),
        "legacy": split_list(row["aliases_legacy"], ALIAS_SEP),
    }
    entry: dict[str, Any] = {
        "slug": slug,
        "name": row["name"],
        "kind": row["kind"],
        "parent": row["parent"],
        "status": row["status"],
        "origin": row["origin"],
        "definition": row["definition"],
        "aliases": {k: v for k, v in aliases.items() if v},
        "absorbed": row["absorbed"],
        "ml_methods": split_list(row["ml_methods"], ","),
        "risk_note": row["risk_note"],
        "source_papers": row["source_papers"],
        "v08_name": row["v08_name"],
        "note": row["note"],
    }
    return {k: v for k, v in entry.items() if v}


def group_entry(row: dict[str, str]) -> dict[str, Any]:
    slug = row["slug"]
    for key in ("slug", "name", "definition", "children"):
        if not row[key]:
            raise ConvertError(f"group {slug or '<no slug>'}: empty `{key}`")
    synonym: list[str] = []
    legacy: list[str] = []
    for alias in split_list(row["aliases"], ALIAS_SEP):
        if alias.endswith(LEGACY_MARK):
            legacy.append(alias[: -len(LEGACY_MARK)].rstrip())
        elif LEGACY_MARK in alias:
            raise ConvertError(f"group {slug}: {LEGACY_MARK!r} inside alias {alias!r}, not at its end")
        else:
            synonym.append(alias)
    aliases = {k: v for k, v in (("synonym", synonym), ("legacy", legacy)) if v}
    entry: dict[str, Any] = {
        "slug": slug,
        "name": row["name"],
        "definition": row["definition"],
        "children": split_list(row["children"], ","),
        "aliases": aliases,
    }
    return {k: v for k, v in entry.items() if v}


def validate(techniques: list[dict[str, Any]], groups: list[dict[str, Any]]) -> None:
    seen: set[str] = set()
    for e in [*techniques, *groups]:
        if e["slug"] in seen:
            raise ConvertError(f"duplicate slug {e['slug']!r} (unique over techniques and group nodes)")
        seen.add(e["slug"])
    for t in techniques:
        if "parent" in t and t["parent"] not in seen:
            raise ConvertError(f"{t['slug']}: parent {t['parent']!r} does not resolve")
        if t.get("parent") == t["slug"]:
            raise ConvertError(f"{t['slug']}: is its own parent")
    for g in groups:
        missing = [c for c in g["children"] if c not in seen]
        if missing:
            raise ConvertError(f"group {g['slug']}: children {missing} do not resolve")


def redact(rows: list[dict[str, str]], patterns: list[re.Pattern[str]]) -> int:
    """Delete every match of ``patterns`` from every cell, in place; the number of deletions."""
    n = 0
    for row in rows:
        for key in row:
            for pat in patterns:
                row[key], k = pat.subn("", row[key])
                n += k
    return n


def convert(techniques_tsv: Path, groups_tsv: Path,
            redactions: list[re.Pattern[str]] | None = None) -> tuple[str, int]:
    """``(yaml_text, rewritten_refs)`` for the two TSVs (deterministic for the same inputs and
    ``redactions``)."""
    t_rows = read_tsv(techniques_tsv, TECHNIQUE_COLUMNS)
    g_rows = read_tsv(groups_tsv, GROUP_COLUMNS)
    redacted = redact(t_rows, redactions or []) + redact(g_rows, redactions or [])
    rewritten = scrub_refs(t_rows) + scrub_refs(g_rows)
    techniques = [technique_entry(r) for r in t_rows]
    groups = [group_entry(r) for r in g_rows]
    validate(techniques, groups)
    doc = {"version": VERSION, "status": STATUS, "notes": NOTES,
           "techniques": techniques, "group_nodes": groups}
    header = (
        "# Taxonomy v0.9 (draft), generated by taxonomy/sources/convert_v09.py; do not edit by hand.\n"
        f"# source {techniques_tsv.name} sha256 {sha256_file(techniques_tsv)}\n"
        f"# source {groups_tsv.name} sha256 {sha256_file(groups_tsv)}\n"
        f"# {len(techniques)} techniques, {len(groups)} group nodes\n"
        + (f"# {redacted} operator redaction(s) applied (--redact)\n" if redacted else "")
    )
    body = yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=10**9, default_flow_style=False)
    return header + body, rewritten


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("techniques_tsv", type=Path)
    ap.add_argument("groups_tsv", type=Path)
    ap.add_argument("--out", type=Path, default=None, help="output YAML (default: stdout)")
    ap.add_argument("--redact", action="append", default=[], metavar="REGEX",
                    help="delete text matching REGEX from every cell (repeatable)")
    args = ap.parse_args(argv)
    try:
        text, rewritten = convert(args.techniques_tsv, args.groups_tsv,
                                  [re.compile(r) for r in args.redact])
    except (ConvertError, re.error) as e:
        print(f"convert_v09: {e}", file=sys.stderr)
        return 1
    print(f"convert_v09: {rewritten} workspace references rewritten or removed", file=sys.stderr)
    if args.out is None:
        sys.stdout.write(text)
    else:
        args.out.write_text(text, encoding="utf-8")
        print(f"convert_v09: wrote {args.out} ({len(text.encode('utf-8'))} bytes)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
