"""The taxonomy files of the taxonomies data repository (``taxonomy/vendored.lock.yaml``): each pipeline path is a
relative symlink into the data repository's subtree (``data-repo/``, the single source of the bytes), each
file's bytes match the lock's sha256, its ``version`` field the tag's version, techniques 1.0.0 is 0.9.1's
content, and the catalogue-envelope files (D738: techniques 1.1.0, models 1.1.0) give the annotation stages exactly
the prompt material and matcher inputs of 1.0.0, so the LLM response cache answers their calls."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
LOCK = yaml.safe_load((REPO / "taxonomy" / "vendored.lock.yaml").read_text("utf-8"))
_TAG = re.compile(r"(?P<name>techniques|models|orgs)-(?P<version>\d+\.\d+\.\d+)")
_VERSION_LINE = re.compile(r"^version: '(?P<v>[^']+)'$", re.M)


def test_lock_shape() -> None:
    assert set(LOCK) == {"source_repository", "subtree", "files"} and LOCK["files"]
    assert (REPO / LOCK["subtree"] / "taxonomies").is_dir()
    for path, e in LOCK["files"].items():
        assert set(e) == {"tag", "source", "sha256"}, path
        m = _TAG.fullmatch(e["tag"])
        assert m and e["source"] == f"taxonomies/{m['name']}/{e['tag']}.yaml", path
        assert re.fullmatch(r"[0-9a-f]{64}", e["sha256"]), path


@pytest.mark.parametrize("path", sorted(LOCK["files"]))
def test_vendored_file_matches_lock(path: str) -> None:
    e = LOCK["files"][path]
    blob = (REPO / path).read_bytes()
    assert hashlib.sha256(blob).hexdigest() == e["sha256"], f"{path} differs from {e['tag']} (lock)"
    # 0.9.x files carry the two-component version '0.9'; a released file its full version
    version = _VERSION_LINE.search(blob.decode("utf-8"))
    tag_version = _TAG.fullmatch(e["tag"])["version"]
    assert version and tag_version.startswith(version["v"]) and (
        version["v"] == tag_version or tag_version.startswith("0.")), (path, version and version["v"])


@pytest.mark.parametrize("path", sorted(LOCK["files"]))
def test_vendored_path_is_a_symlink_into_the_subtree(path: str) -> None:
    """A pipeline path is a relative symlink to ``<subtree>/<source>`` (a relative link resolves the same in a clone,
    a ``git archive`` and the image), never a second copy of the bytes."""
    p, e = REPO / path, LOCK["files"][path]
    assert p.is_symlink(), f"{path} is a copy, not a symlink into {LOCK['subtree']}/"
    target = os.readlink(p)
    assert not os.path.isabs(target), (path, target)
    assert p.resolve() == (REPO / LOCK["subtree"] / e["source"]).resolve(), (path, target)
    assert (REPO / LOCK["subtree"] / e["source"]).is_file() and not (REPO / LOCK["subtree"] / e["source"]).is_symlink()


def test_production_pins_read_the_vendored_files() -> None:
    plan = yaml.safe_load((REPO / "configs" / "plans" / "batch-2026-10a.yaml").read_text("utf-8"))
    assert plan["prompts"]["annotate.technique_staged"]["taxonomy"] == "taxonomy/techniques-1.1.0.yaml"
    assert plan["prompts"]["annotate.affiliation_orgs"]["vocab"] == "taxonomy/orgs-0.2.2.yaml"
    assert LOCK["files"]["configs/models.yaml"]["tag"] == "models-1.1.0"
    assert {"taxonomy/techniques-1.1.0.yaml", "taxonomy/orgs-0.2.2.yaml"} <= set(LOCK["files"])


def test_techniques_1_0_0_is_0_9_1_content() -> None:
    """Only the header moved: the same entries and group nodes, so the same menus (the model-visible text)."""
    from workflows.annotate.technique_staged import taxonomy as t

    old, new = (t.load_taxonomy(REPO / "taxonomy" / f) for f in ("taxonomy-v0.9.1.yaml", "techniques-1.0.0.yaml"))
    assert (old.version, old.status, new.version, new.status) == ("0.9", "draft", "1.0.0", "released")
    assert new.techniques == old.techniques and new.group_nodes == old.group_nodes
    assert t.menu_text(new) == t.menu_text(old) and t.compact_index(new) == t.compact_index(old)


# ── the catalogue envelope (D738): 1.1.0 renders as 1.0.0 ────────────────────────────────────

MODELS = REPO / "data-repo" / "taxonomies" / "models"
#: A pointer of every key, and a note (``pointer_line``'s two forms), for the sub-menu blocks.
_POINTERS = ({"source": "P", "section": "s2", "surface": "DP-SGD", "snippet": "we train with DP-SGD", "stance": "used"},
             {"note": "3 more mentions"})


def test_techniques_1_1_0_renders_as_1_0_0() -> None:
    """Every rendering of the stage: the full menu, the compact index, every sub-menu block (with and without
    pointers) and a ranked sub-menu; the slugs in their order; the lexical matcher's surfaces, regexes, keys and
    dropped surfaces."""
    from workflows.annotate.technique_staged import lexical
    from workflows.annotate.technique_staged import taxonomy as t

    old, new = (t.load_taxonomy(REPO / "taxonomy" / f) for f in ("techniques-1.0.0.yaml", "techniques-1.1.0.yaml"))
    assert (new.version, new.status) == ("1.1.0", "released")
    assert new.techniques == old.techniques and new.group_nodes == old.group_nodes
    assert t.all_slugs(new) == t.all_slugs(old)
    assert t.menu_text(new) == t.menu_text(old) and t.compact_index(new) == t.compact_index(old)
    for slug in t.all_slugs(old):
        for ptrs in ((), _POINTERS):
            assert t.submenu_block(new.get(slug), ptrs) == t.submenu_block(old.get(slug), ptrs), slug  # type: ignore[arg-type]
    ranked = list(reversed(t.all_slugs(old)))
    assert t.submenu_text(new, ranked, {ranked[0]: _POINTERS}) == t.submenu_text(old, ranked, {ranked[0]: _POINTERS})
    lo, ln = lexical.Lexicon(old), lexical.Lexicon(new)
    assert list(ln.surfaces.items()) == list(lo.surfaces.items()) and ln.dropped == lo.dropped
    assert {k: r.pattern for k, r in ln._regex.items()} == {k: r.pattern for k, r in lo._regex.items()}
    assert ln._keys == lo._keys


def test_techniques_envelope_is_validated() -> None:
    from workflows.annotate.technique_staged import taxonomy as t

    raw = yaml.safe_load((REPO / "taxonomy" / "techniques-1.1.0.yaml").read_text("utf-8"))
    group = next(e for e in raw["entries"] if e["node"] == "group" and e.get("aliases"))
    bad = {**raw, "entries": [e if e is not group else {**e, "aliases": [{"text": "x", "kind": "instance"}]}
                              for e in raw["entries"]]}
    with pytest.raises(ValueError, match="schema problem"):
        t.parse_taxonomy(bad)
    with pytest.raises(ValueError, match="duplicate"):
        t.parse_taxonomy({**raw, "entries": raw["entries"] + raw["entries"][:1]})


def _menu_everything(vocab: object) -> list[object]:
    """``build_menu`` with every node on the menu, strong and (a second menu) weak, over a text naming each node's
    first mention (location notes), and its text as the prompt shows it."""
    from workflows.annotate.model_roles import render

    nodes = render.vocab_view(vocab)  # type: ignore[arg-type]
    forms = {nid: list(n.mentions or n.weak_mentions or [nid])[:2] for nid, n in nodes.items()}
    text = " ".join(f[0] for f in forms.values()) + "\n\nReferences\n" + " ".join(f[-1] for f in forms.values())
    strong = render.build_menu({"mentions": forms, "weak": {}}, nodes, text)
    weak = render.build_menu({"mentions": {}, "weak": forms}, nodes, text)
    return [strong, weak, render.menu_text(strong), render.menu_text(weak)]


def test_models_1_1_0_renders_as_1_0_0() -> None:
    """model_roles' prompt material and matcher: the menu of every node (name, maker display, denotation, gloss),
    the matcher's raw inputs per node in file order, its compiled regexes and the prefilter's index."""
    from stack.modelvocab import load_vocab
    from workflows.annotate.model_roles import match

    old, new = load_vocab(MODELS, "models-1.0.0.yaml"), load_vocab(REPO / "configs")
    assert (old.validated.version, new.validated.version, new.validated.meta.schema_) == ("1.0.0", "1.1.0", "models-2")
    assert list(new.nodes) == list(old.nodes)
    for nid in old.nodes:
        for k in ("mentions", "weak_mentions", "mentions_verbatim", "parent", "kind"):
            assert new.nodes[nid].get(k) == old.nodes[nid].get(k), (nid, k)
    assert _menu_everything(new.validated) == _menu_everything(old.validated)
    so, sn = match.Scanner(old), match.Scanner(new)
    assert [(k, r.pattern, r.flags) for k, r in sn.regexes.items()] == [(k, r.pattern, r.flags) for k, r in so.regexes.items()]
    assert [(k, r.pattern) for k, r in sn.strong_regexes.items()] == [(k, r.pattern) for k, r in so.strong_regexes.items()]
    assert sn._pf.by_token == so._pf.by_token and sn._pf.always == so._pf.always


def test_models_references_resolve_in_the_referenced_orgs() -> None:
    """Every maker and lab id is an entry of the organisation vocabulary version the file names (the pipeline's
    vendored ``taxonomy/orgs-<version>.yaml``), every maker tagged model-maker there."""
    from stack import orgvocab
    from stack.modelvocab import load_vocab, maker_refs

    v = load_vocab(REPO / "configs").validated
    orgs = orgvocab.load(REPO / "taxonomy" / f"orgs-{v.meta.references['orgs']}.yaml").matcher.entries
    makers = {m.id for e in v.entries for m in maker_refs(e)}
    labs = {e.lab.id for e in v.entries if e.lab is not None}
    # 1.0.0's meta.makers listed 138; four had no node (deepmind, fast-ai, korea-university, nous-research)
    assert len(makers) == 134 and len(labs) == 8
    assert makers <= set(orgs) and labs <= set(orgs), (makers - set(orgs), labs - set(orgs))
    assert all("model-maker" in orgs[m].tags for m in makers)
