"""Check a gold v3 label file (stdlib only; run from a labelling session directory).

    python3 check_labels.py techniques  out/g3-0001.json [more files]
    python3 check_labels.py model_roles out/g3-0001.json [more files]

Checks: the file against `schema.json` (the subset of JSON Schema it uses), the item id against the file name, the
role invariant (`mentioned` >= every other role), every quote verbatim in `docs/<item_id>/text.txt` (whitespace
normalised, case-sensitive), and the ids: techniques - every slug, alt slug and level alternative a slug of
`menu-v0.9.1.md`, one row per slug; model_roles - exactly one `models` row per `model-menu.md` entry, node ids in
`vocab-index.tsv`, a quote on every row with a role >= 1, a flag on every all-zero menu row.
Prints `OK <file>` or one line per problem; exit status 1 when any file has a problem.
"""
import json
import re
import sys
from pathlib import Path

HERE = Path.cwd()


def norm(s):
    return re.sub(r"\s+", " ", s).strip()


def validate(v, s, path, out):
    if "anyOf" in s:
        for alt in s["anyOf"]:
            sub = []
            validate(v, alt, path, sub)
            if not sub:
                return
        out.append(f"{path}: matches no allowed form")
        return
    if "enum" in s and v not in s["enum"]:
        out.append(f"{path}: {v!r} not one of {s['enum']}")
        return
    t = s.get("type")
    types = {"object": dict, "array": list, "string": str, "integer": int, "null": type(None)}
    if t and not (isinstance(v, types[t]) and not (t == "integer" and isinstance(v, bool))):
        out.append(f"{path}: expected {t}")
        return
    if t == "object":
        for k in s.get("required", []):
            if k not in v:
                out.append(f"{path}: missing key {k!r}")
        props = s.get("properties", {})
        for k, x in v.items():
            if k in props:
                validate(x, props[k], f"{path}.{k}", out)
            elif s.get("additionalProperties") is False:
                out.append(f"{path}: unexpected key {k!r}")
        if len(v) < s.get("minProperties", 0):
            out.append(f"{path}: needs at least {s['minProperties']} keys")
    elif t == "array":
        if len(v) < s.get("minItems", 0):
            out.append(f"{path}: needs at least {s['minItems']} items")
        for i, x in enumerate(v):
            validate(x, s.get("items", {}), f"{path}[{i}]", out)
    elif t == "string":
        if len(v) < s.get("minLength", 0) or len(v) > s.get("maxLength", 10**9):
            out.append(f"{path}: length {len(v)} outside [{s.get('minLength', 0)}, {s.get('maxLength', '-')}]")
        if "pattern" in s and not re.search(s["pattern"], v):
            out.append(f"{path}: {v!r} does not match {s['pattern']}")
    elif t == "integer":
        if v < s.get("minimum", -10**9) or v > s.get("maximum", 10**9):
            out.append(f"{path}: {v} outside [{s.get('minimum')}, {s.get('maximum')}]")


def roles_ok(roles, path, out):
    m = roles.get("mentioned", 0)
    if any(x > m for x in roles.values()):
        out.append(f"{path}: a role is above mentioned ({roles})")


def quote_ok(q, text, path, out):
    if q is not None and norm(q) not in text:
        hint = " (found case-insensitively: fix the case)" if norm(q).lower() in text.lower() else ""
        out.append(f"{path}: quote not found verbatim in text.txt{hint}: {q[:80]!r}")


def check_techniques(d, text, out):
    slugs = set(re.findall(r"^- ([a-z0-9][a-z0-9.-]*) \[", (HERE / "menu-v0.9.1.md").read_text("utf-8"), re.M))
    seen = set()
    for i, r in enumerate(d.get("techniques") or []):
        p = f"techniques[{i}]"
        s = r.get("slug")
        if s not in slugs:
            out.append(f"{p}: slug {s!r} is not on the menu")
        if s in seen:
            out.append(f"{p}: slug {s!r} has two rows")
        seen.add(s)
        for k in ("alt_slugs", "level_alternatives"):
            for x in r.get(k) or []:
                if x not in slugs:
                    out.append(f"{p}.{k}: {x!r} is not on the menu")
        roles_ok(r.get("roles") or {}, p, out)
        quote_ok(r.get("quote"), text, p, out)
    for i, r in enumerate(d.get("extra_techniques") or []):
        p = f"extra_techniques[{i}]"
        if r.get("nearest_slug") is not None and r["nearest_slug"] not in slugs:
            out.append(f"{p}: nearest_slug {r['nearest_slug']!r} is not on the menu")
        roles_ok(r.get("roles") or {}, p, out)
        quote_ok(r.get("quote"), text, p, out)


def check_model_roles(d, text, iid, out):
    menu = re.findall(r"^- `([^`]+)` — ", (HERE / "docs" / iid / "model-menu.md").read_text("utf-8"), re.M)
    vocab = {ln.split("\t")[0] for ln in (HERE / "vocab-index.tsv").read_text("utf-8").splitlines()[1:] if ln}
    rows = [r.get("node_id") for r in d.get("models") or []]
    for n in sorted(set(menu) - set(rows)):
        out.append(f"models: menu node {n!r} has no row")
    for n in sorted(set(rows) - set(menu)):
        out.append(f"models: {n!r} is not on this document's menu")
    for n in sorted({n for n in rows if rows.count(n) > 1}):
        out.append(f"models: {n!r} has more than one row")
    for i, r in enumerate(d.get("models") or []):
        p = f"models[{i}]"
        roles = r.get("roles") or {}
        roles_ok(roles, p, out)
        if any(roles.values()):
            if r.get("quote") is None:
                out.append(f"{p}: a row with a role needs a quote")
            if r.get("confidence") is None:
                out.append(f"{p}: a row with a role needs a confidence")
            if r.get("flag") is not None:
                out.append(f"{p}: a flag only on an all-zero row")
        elif r.get("flag") is None:
            out.append(f"{p}: an all-zero row needs a flag")
        if r.get("refined_node_id") is not None and r["refined_node_id"] not in vocab:
            out.append(f"{p}: refined_node_id {r['refined_node_id']!r} not in vocab-index.tsv")
        quote_ok(r.get("quote"), text, p, out)
    for k, key in (("variant_models", "base_node_id"), ("extra_models", "node_id"), ("mentions", "node_id")):
        for i, r in enumerate(d.get(k) or []):
            p = f"{k}[{i}]"
            if r.get(key) is not None and r[key] not in vocab:
                out.append(f"{p}: {key} {r[key]!r} not in vocab-index.tsv")
            if k == "variant_models" and r.get(key) not in menu:
                out.append(f"{p}: base_node_id {r.get(key)!r} is not on this document's menu")
            if "roles" in r:
                roles_ok(r["roles"], p, out)
                if any(r["roles"].values()) and r.get("quote") is None:
                    out.append(f"{p}: a row with a role needs a quote")
                quote_ok(r.get("quote"), text, p, out)
            if k == "mentions" and norm(r.get("surface") or "") not in text:
                out.append(f"{p}: surface {r.get('surface')!r} not found verbatim in text.txt")


def main(argv):
    if len(argv) < 2 or argv[0] not in ("techniques", "model_roles"):
        print(__doc__)
        return 2
    task, files = argv[0], argv[1:]
    schema = json.loads((HERE / "schema.json").read_text("utf-8"))
    bad = 0
    for f in files:
        out = []
        try:
            d = json.loads(Path(f).read_text("utf-8"))
        except (OSError, ValueError) as e:
            print(f"{f}: cannot read as JSON: {e}")
            bad += 1
            continue
        validate(d, schema, "$", out)
        iid = d.get("item_id") if isinstance(d, dict) else None
        if iid and Path(f).stem != iid:
            out.append(f"$.item_id {iid!r} differs from the file name")
        tp = HERE / "docs" / str(iid) / "text.txt"
        if not tp.is_file():
            out.append(f"no document docs/{iid}/text.txt")
        elif isinstance(d, dict):
            text = norm(tp.read_text("utf-8"))
            (check_techniques(d, text, out) if task == "techniques" else check_model_roles(d, text, iid, out))
        for o in out:
            print(f"{f}: {o}")
        if out:
            bad += 1
        else:
            print(f"OK {f}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
