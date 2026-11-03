#!/usr/bin/env python3
"""``tools/release_format.py`` — the release format's schemas: generate, strip, classify a change, archive (D803).

The export writes four schema files beside the files they describe (:func:`stack.fold.dataset.format_schemas`:
``release.schema.json``, ``data/corpus.schema.json``, ``config/configs.schema.json``,
``config/overrides.schema.json``); the data repository archives each format version's schemas, stripped of prose
(description, title, examples, $comment), in ``formats/<major>.<minor>/`` (``data-repo/formats/README.md``: the rules).
The version the pipeline declares is ``format_version`` in ``configs/releases/dataset/mapping.yaml``.

    uv run --no-sync python tools/release_format.py check                 # the default: is the declared version right?
    uv run --no-sync python tools/release_format.py generate --out DIR    # the four schemas as an export writes them
    uv run --no-sync python tools/release_format.py strip --out DIR       # the same, stripped (what the archive holds)
    uv run --no-sync python tools/release_format.py archive --data-repo PATH   # write formats/<major>.<minor>/ there

``check``: when ``formats/<major>.<minor>/`` of the declared version exists, the generated stripped schemas must equal
it (compared structurally: object key order ignored, ``required``, ``enum``, ``type`` and ``x-values`` as sets);
a difference means the format changed, which needs a new version. When it does not exist, the generated schemas are
classified against the highest archived version below the declared one (:func:`classify`, on the flattened path
table of :func:`flatten`): the declared bump must be at least the needed one, and ``formats/CHANGELOG.md`` must have
an entry for the declared version; then ``archive`` writes the directory into a data-repository checkout (step 2 of
``data-repo/formats/README.md``). Exit 0 when the archive matches, 1 otherwise (the message says what to do).

``archive`` refuses to replace a directory whose changelog entry is dated, and writes nothing else.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from stack import yamlio  # noqa: E402
from stack.fold import dataset as ds  # noqa: E402

LEVELS = ("none", "patch", "minor", "major")
#: Keywords :func:`flatten` reads into a row's columns or walks into; any other keyword lands in the row's ``rest``,
#: whose change is a difference the classifier does not know (major).
KNOWN = frozenset({"type", "enum", "x-arrow", "x-open", "x-values", "properties", "required", "items",
                   "additionalProperties", "$schema", "$id"})
VERSION_PATTERN = "<format_version pattern>"


def rv() -> Any:
    return ds.release_validator()


def generated(m: ds.Mapping_ | None = None, cfg: ds.DatasetConfig | None = None) -> dict[str, dict[str, Any]]:
    """The four schemas as an export writes them, from the repository's mapping and the public config (the schemas
    are the same for every variant)."""
    m = m or ds.Mapping_(ds.DATASET_DIR / "mapping.yaml")
    return ds.format_schemas(m, cfg or ds.load_dataset_config("dataset-public"))


def stripped(schemas: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {rel: rv().strip_schema(js) for rel, js in schemas.items()}


# ---------------------------------------------------------------- the flattened path table and the classifier
@dataclass(frozen=True)
class Row:
    """One node of a schema file (or one required file of the release, ``kind == "file"``)."""

    kind: str  # node | file
    types: tuple[str, ...]  # JSON types without null
    arrow: str | None
    nullable: bool
    enum: tuple[str, ...] | None  # a closed list's values (without null)
    open: bool
    values: tuple[str, ...]  # an open list's known values
    required: bool
    rest: str  # canonical JSON of the other keywords


def _norm(v: Any) -> tuple[str, ...]:
    return tuple(sorted(json.dumps(x, sort_keys=True) if not isinstance(x, str) else x for x in v))


def flatten(schemas: dict[str, dict[str, Any]]) -> dict[str, Row]:
    """The path table of stripped schemas: ``<file>:<dotted path>`` (``[]`` a list level, ``*`` the values of a map)
    per node, and ``file:<path>`` per file ``release.schema.json`` requires (its ``files`` ``contains``)."""
    out: dict[str, Row] = {}

    def walk(rel: str, js: Any, path: str, required: bool) -> None:
        if not isinstance(js, dict):
            out[f"{rel}:{path}"] = Row("node", (), None, False, None, False, (), required, json.dumps(js))
            return
        t = js.get("type")
        types = [t] if isinstance(t, str) else list(t or [])
        enum = js.get("enum")
        rest = {k: v for k, v in js.items() if k not in KNOWN}
        if rel == ds.RELEASE_SCHEMA_FILE and path == "files" and "allOf" in rest:
            for c in rest.pop("allOf"):
                p = (((c.get("contains") or {}).get("properties") or {}).get("path") or {}).get("const")
                if p is None:
                    rest.setdefault("allOf", []).append(c)
                else:
                    out[f"file:{p}"] = Row("file", (), None, False, None, False, (), True, "")
        if rel == ds.RELEASE_SCHEMA_FILE and path == "format_version" and rest.get("pattern") == version_pattern(mm):
            rest["pattern"] = VERSION_PATTERN  # the format's own version: not a difference between versions
        if js.get("additionalProperties") not in (None, False, True):
            walk(rel, js["additionalProperties"], f"{path}.*", True)
        elif "additionalProperties" in js:
            rest["additionalProperties"] = js["additionalProperties"]
        out[f"{rel}:{path}"] = Row(
            "node", tuple(sorted(x for x in types if x != "null")), js.get("x-arrow"),
            "null" in types or (enum is not None and None in enum),
            None if enum is None else _norm(x for x in enum if x is not None), bool(js.get("x-open")),
            _norm(js.get("x-values") or []), required, json.dumps(rv().canonical(rest), sort_keys=True))
        req = set(js.get("required") or [])
        for name, sub in (js.get("properties") or {}).items():
            walk(rel, sub, f"{path}.{name}" if path else name, name in req)
        if "items" in js:
            walk(rel, js["items"], path + "[]", True)

    rid = str((schemas.get(ds.RELEASE_SCHEMA_FILE) or {}).get("$id", ""))
    mm = rid.split(":")[-2] if rid.count(":") >= 2 else ""
    for rel, js in sorted(schemas.items()):
        walk(rel, js, "", True)
    return out


def version_pattern(mm: str) -> str:
    """``release.schema.json``'s ``format_version`` pattern of format ``<major>.<minor>`` (the generator's)."""
    major, _, minor = mm.partition(".")
    return f"^{major}\\.{minor}\\.(0|[1-9][0-9]*)$"


def classify(old: dict[str, Row], new: dict[str, Row]) -> list[tuple[str, str]]:
    """The differences of two path tables as ``(level, what)`` (``data-repo/formats/README.md``, Compatibility);
    a difference of any other keyword is ``major`` (a kind the classifier does not know)."""
    out: list[tuple[str, str]] = []
    for p in sorted(set(old) | set(new)):
        a, b = old.get(p), new.get(p)
        what = "file" if p.startswith("file:") else "field"
        if b is None:
            out.append(("major", f"{p}: {what} removed or renamed"))
            continue
        if a is None:
            out.append(("minor", f"{p}: {what} added"))
            continue
        if a.types != b.types:
            out.append(("major", f"{p}: JSON type {list(a.types)} -> {list(b.types)}"))
        if a.arrow != b.arrow:
            out.append(("major", f"{p}: x-arrow {a.arrow} -> {b.arrow}"))
        if a.nullable != b.nullable:
            out.append(("major", f"{p}: may now be null") if b.nullable else ("minor", f"{p}: no longer null"))
        if a.required != b.required:
            out.append(("major", f"{p}: no longer required") if a.required else ("minor", f"{p}: now required"))
        if a.enum != b.enum:
            out.append(("major", f"{p}: no longer a closed list" if b.enum is None else
                        f"{p}: now a closed list {list(b.enum)}" if a.enum is None else
                        f"{p}: closed list {list(a.enum)} -> {list(b.enum)}"))
        if a.open != b.open:
            out.append(("major", f"{p}: now an open list" if b.open else
                        f"{p}: no longer an open list ({'closed' if b.enum is not None else 'a plain string'})"))
        elif a.open:
            gone, added = set(a.values) - set(b.values), set(b.values) - set(a.values)
            if gone:
                out.append(("major", f"{p}: open-list values removed: {sorted(gone)}"))
            if added:
                out.append(("minor", f"{p}: open-list values added: {sorted(added)}"))
        if a.rest != b.rest:
            out.append(("major", f"{p}: other keywords changed ({a.rest[:60]} -> {b.rest[:60]})"))
    return out


def needed(changes: list[tuple[str, str]]) -> str:
    return max((lvl for lvl, _w in changes), key=LEVELS.index, default="none")


def parse(v: str) -> tuple[int, int, int]:
    m = ds.FORMAT_VERSION.match(v)
    if m is None:
        raise ValueError(f"not a <major>.<minor>.<patch> version: {v!r}")
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def bump(base: tuple[int, int], declared: tuple[int, int, int]) -> str:
    """The bump from archived ``base`` (major, minor) to the ``declared`` version; ``below`` when it is lower."""
    if declared[0] != base[0]:
        return "major" if declared[0] > base[0] else "below"
    if declared[1] != base[1]:
        return "minor" if declared[1] > base[1] else "below"
    return "patch"


def archived(formats: Path) -> list[tuple[int, int]]:
    return sorted((int(a), int(b)) for p in formats.glob("*") if p.is_dir()
                  for a, b in [p.name.split(".")] if re.fullmatch(r"\d+\.\d+", p.name))


def read_dir(d: Path) -> dict[str, dict[str, Any]]:
    return {rel: json.loads((d / rel).read_text("utf-8")) for rel in ds.FORMAT_SCHEMA_FILES if (d / rel).is_file()}


# ---------------------------------------------------------------- the check
@dataclass
class Verdict:
    """:func:`check`'s answer: ``ok`` (the archive of the declared version equals the generated schemas and the
    changelog has its entry); the classified ``changes``, the ``needed`` level, the declared ``bump`` against the
    base and whether it is ``enough`` (None where there was nothing to classify against)."""

    ok: bool
    lines: list[str]
    changes: list[tuple[str, str]]
    needed: str
    bump: str | None = None
    enough: bool | None = None
    has_entry: bool = False


def check(formats: Path, version: str, schemas: dict[str, dict[str, Any]]) -> Verdict:
    """``check`` (module docstring) of the ``version`` the pipeline declares and the stripped ``schemas`` it
    generates, against the archive in ``formats``."""
    v = parse(version)
    mm = f"{v[0]}.{v[1]}"
    d = formats / mm
    entries = rv().changelog_entries((formats / "CHANGELOG.md").read_text("utf-8")) \
        if (formats / "CHANGELOG.md").is_file() else {}
    has_entry = version in entries
    no_entry = [] if has_entry else [f"formats/CHANGELOG.md has no entry [{version}]"]
    if d.is_dir():
        arch = read_dir(d)
        diffs = {rel: rv().schema_diff(rv().canonical(arch.get(rel)), rv().canonical(js))
                 for rel, js in schemas.items()}
        diffs = {rel: x for rel, x in diffs.items() if x}
        if not diffs:
            return Verdict(has_entry, [f"ok: the generated schemas equal formats/{mm}/ (format_version {version})",
                                       *no_entry], [], "none", has_entry=has_entry)
        changes = classify(flatten(arch), flatten(schemas))
        need = needed(changes)
        lines = [f"the generated schemas differ from formats/{mm}/, the archive of the declared format_version "
                 f"{version}: an archived format does not change; declare a new version ({need} change)"]
        lines += [f"  {lvl}: {w}" for lvl, w in changes] or [f"  {rel}: {x}" for rel, xs in diffs.items() for x in xs]
        return Verdict(False, lines + no_entry, changes, need, "patch", False, has_entry)
    lower = [a for a in archived(formats) if a < (v[0], v[1])]
    then = (f"then: tools/release_format.py archive --data-repo <checkout of the data repository> (writes "
            f"formats/{mm}/), add the changelog entry, commit there, and pull the subtree here")
    if not lower:
        return Verdict(False, [f"no formats/{mm}/ and no archived version below it: {version} is the first format",
                               *no_entry, then], [], "none", has_entry=has_entry)
    base = lower[-1]
    changes = classify(flatten(read_dir(formats / f"{base[0]}.{base[1]}")), flatten(schemas))
    need, have = needed(changes), bump(base, v)
    enough = have != "below" and LEVELS.index(have) >= LEVELS.index(need)
    lines = [f"no formats/{mm}/ yet; against formats/{base[0]}.{base[1]}/ the change is {need}, the declared "
             f"{version} a {have} bump{'' if enough else ': not enough'}"]
    lines += [f"  {lvl}: {w}" for lvl, w in changes]
    if not enough:
        lines.append(f"declare at least a {need} version above {base[0]}.{base[1]} (mapping.yaml format_version)")
    return Verdict(False, lines + no_entry + [then], changes, need, have, enough, has_entry)


def write_archive(data_repo: Path, version: str, schemas: dict[str, dict[str, Any]], *, replace: bool = False) -> Path:
    """Write ``formats/<major>.<minor>/`` into the data-repository checkout ``data_repo``. An existing directory with
    the same schemas is left as it is; one with other schemas is replaced only with ``replace`` and only while the
    version is neither dated in ``formats/CHANGELOG.md`` nor tagged ``format-<major>.<minor>.*`` there."""
    v = parse(version)
    mm = f"{v[0]}.{v[1]}"
    formats = data_repo / "formats"
    d = formats / mm
    if not formats.is_dir():
        raise SystemExit(f"{formats}: not a checkout of the data repository (no formats/)")
    if d.is_dir():
        have = read_dir(d)
        if {rel: rv().canonical(js) for rel, js in have.items()} == \
                {rel: rv().canonical(js) for rel, js in schemas.items()} and \
                {p.relative_to(d).as_posix() for p in d.rglob("*") if p.is_file()} == set(schemas):
            return d
        dated = {k.rsplit(".", 1)[0] for k, dd in rv().changelog_entries(
            (formats / "CHANGELOG.md").read_text("utf-8")).items() if dd} if (formats / "CHANGELOG.md").is_file() \
            else set()
        try:
            tags = subprocess.run(["git", "tag", "-l", f"format-{mm}.*"], cwd=data_repo, check=True,
                                  capture_output=True, text=True).stdout.split()
        except (subprocess.CalledProcessError, OSError):
            tags = []
        if mm in dated or tags:
            raise SystemExit(f"formats/{mm}/: {'tagged ' + ', '.join(tags) if tags else 'its changelog entry is dated'}"
                             "; an archived format is never edited (declare a new version)")
        if not replace:
            raise SystemExit(f"formats/{mm}/ exists with other schemas; it is not dated or tagged yet, so --replace "
                             "may overwrite it (review the difference: tools/release_format.py check --formats ...)")
        shutil.rmtree(d)
    for rel, js in schemas.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(js, indent=2, ensure_ascii=False) + "\n", "utf-8")
    return d


# ---------------------------------------------------------------- the pipeline's lock of the vendored archive
LOCK = ds.DATASET_DIR / "formats.lock.yaml"


def archive_digests(formats: Path) -> dict[str, str]:
    """sha256 of every file of every ``formats/<major>.<minor>/`` (paths relative to ``formats/``)."""
    return {p.relative_to(formats).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for d in sorted(formats.glob("*")) if d.is_dir() for p in sorted(d.rglob("*")) if p.is_file()}


def lock_problems(formats: Path, lock: Path = LOCK) -> list[str]:
    """The vendored archive against the pipeline's lock (D805): a subtree pull that changes, adds or removes an
    archived file fails until the lock is renewed (``tools/release_format.py lock``), so the change is seen."""
    if not lock.is_file():
        return [f"{lock.name}: missing (tools/release_format.py lock)"]
    want = (yamlio.load(lock.read_text("utf-8")) or {}).get("files") or {}
    have = archive_digests(formats)
    out = []
    for p in sorted(set(want) | set(have)):
        if want.get(p) != have.get(p):
            why = "not in the lock" if p not in want else "not in the archive" if p not in have else \
                "sha256 differs from the lock"
            out.append(f"data-repo/formats/{p}: {why}")
    return out


def write_lock(formats: Path, lock: Path = LOCK) -> None:
    lines = ["# sha256 of every archived format file the pipeline vendors (data-repo/formats/<major>.<minor>/, D805).",
             "# tests/test_release_format.py checks the vendored files against it; after a subtree pull that changes",
             "# them, review the change and renew it: uv run --no-sync python tools/release_format.py lock", "files:"]
    lines += [f"  {p}: {h}" for p, h in archive_digests(formats).items()]
    lock.write_text("\n".join(lines) + "\n", "utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("command", nargs="?", default="check", choices=("check", "generate", "strip", "archive", "lock"))
    ap.add_argument("--out", type=Path, help="generate, strip: the directory to write the schemas into")
    ap.add_argument("--data-repo", type=Path, help="archive: a checkout of the data repository")
    ap.add_argument("--replace", action="store_true",
                    help="archive: overwrite a directory with other schemas (refused once dated or tagged)")
    ap.add_argument("--formats", type=Path, default=ds.DATA_REPO / "formats",
                    help="check: the archive (default: the vendored data-repo/formats)")
    a = ap.parse_args(argv)
    m = ds.Mapping_(ds.DATASET_DIR / "mapping.yaml")
    gen = generated(m)
    if a.command in ("generate", "strip"):
        if a.out is None:
            ap.error(f"{a.command}: --out DIR")
        for rel, js in (gen if a.command == "generate" else stripped(gen)).items():
            (a.out / rel).parent.mkdir(parents=True, exist_ok=True)
            (a.out / rel).write_text(json.dumps(js, indent=2, ensure_ascii=False) + "\n", "utf-8")
        print(f"wrote {len(gen)} schemas of format {m.format_version} into {a.out}")
        return 0
    if a.command == "archive":
        if a.data_repo is None:
            ap.error("archive: --data-repo PATH")
        d = write_archive(a.data_repo, m.format_version, stripped(gen), replace=a.replace)
        print(f"wrote {d}; add its formats/CHANGELOG.md entry [{m.format_version}] in the same commit")
        return 0
    if a.command == "lock":
        write_lock(a.formats)
        print(f"wrote {LOCK.relative_to(REPO)} ({len(archive_digests(a.formats))} files)")
        return 0
    verdict = check(a.formats, m.format_version, stripped(gen))
    locked = lock_problems(a.formats) if a.formats == ds.DATA_REPO / "formats" else []
    print("\n".join(verdict.lines + locked))
    return 0 if verdict.ok and not locked else 1


if __name__ == "__main__":
    sys.exit(main())
