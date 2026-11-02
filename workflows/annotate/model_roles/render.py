"""The judge's menu for ``annotate.model_roles`` (ported from v3 ``render.py``, ``tomas/model-roles``
d011e5e).

Turns the matcher's scan (``match.Scanner.scan``: ``{"mentions": {node: [surfaces]}, "weak": {...}}``)
+ the document text + the vocabulary into the menu (:func:`build_menu`, :func:`window_split`) and the
menu's text (:func:`menu_text`), which ``main.py`` fills into the pinned prompt file's ``{{menu}}``
(``prompts/<name>.md``; D591: the rest of the user message, v3's ``prompts/v1.md.j2``, which v4
re-implemented here in Python, is now that file, byte for byte the same text).

Design decisions (v3, 2026-08-02, grounded in the 20-doc experience pass):

* **Menu entry** = identity (node id, name) · denotation phrase derived from kind/level
  ("model" / "product line" / "release series" / "generation" / "model family") · maker
  display name (the ``maker`` reference's ``name``, models 1.1.0; ``meta.makers`` before; + parent-chain
  inheritance) · gloss (``gloss`` field,
  falling back to the first sentence of ``notes``) · distinct matched surface forms ·
  weak-match flag · a body-vs-references location note.
* **Occurrence counts are OFF by default** (``show_counts`` flag): raw counts anchor the
  judge toward typographic footprint, against the rubric's §2 functional-load rule. The
  distinct forms locate the model; the document itself carries prominence.
* **Location note is ON by default** (``show_locations``): "references-only" vs "body" is
  deterministic mechanical support for the bibliography deletion-test precedent (§4.15),
  computed here because the matcher stores no offsets.
* **Prompt order** (now the prompt file's): task header → document → rubric (§1–§7)
  → menu → output spec. Document-early, instructions-after long-context practice;
  validated by the experience pass.

No dataclass (written when the stage loader ran ``main.py``, and so this sibling, outside
``sys.modules``; registered since D435): :class:`MenuEntry` is a ``NamedTuple``.
"""

from __future__ import annotations

import json
import re
from typing import Any, NamedTuple

from stack.modelvocab import ModelVocab, VocabNode, maker_refs

_REFS_HEADING = re.compile(
    r"^(?:#{1,6}\s*(?:\d+\.?\s*)?|\*\*)?(references|bibliography|works cited)\b",
    re.IGNORECASE | re.MULTILINE,
)

_DENOTATION: dict[tuple[str, str], str] = {
    ("group", "line"): "product line",
    ("group", "series"): "release series",
    ("group", "generation"): "generation",
}


class MenuEntry(NamedTuple):
    node_id: str
    name: str
    maker: str
    denotation: str
    gloss: str
    forms: list[str]
    count: int
    location: str  # body | references-only | body+references | not-found | ""
    weak: bool
    #: the matched text as written: each form extended rightward over a model-name run (:func:`written_forms`)
    written: tuple[str, ...] = ()


#: At most this many distinct written strings per menu entry (the rest are counted, "+N more").
MAX_WRITTEN = 6


def written_forms(forms: list[str], text: str) -> list[str]:
    """The matched forms as written in ``text``: every guarded occurrence of each form (no letter or
    digit right before or after it, as the matcher's guards) extended rightward over ``-_.``-joined
    letter/digit runs, so a pattern that matched a prefix shows what the document wrote ("gpt-5" in
    "gpt-5-thinking", "LLaMA-3-8B" in "LLaMA-3-8B-IT"). Distinct, in document order."""
    seen: dict[str, int] = {}
    for form in forms:
        start = 0
        while (i := text.find(form, start)) != -1:
            start = i + len(form)
            end = i + len(form)
            if (i > 0 and text[i - 1].isalnum()) or (end < len(text) and text[end].isalnum()):
                continue  # inside a longer word: not an occurrence the matcher would take
            while end + 1 < len(text) and text[end] in "-_." and text[end + 1].isalnum():
                end += 1
                while end < len(text) and text[end].isalnum():
                    end += 1
            seen.setdefault(text[i:end], i)
    return sorted(seen, key=lambda w: seen[w])


def vocab_view(vocab: ModelVocab) -> dict[str, VocabNode]:
    """The nodes by id through the TYPED model (v3 ``load_vocab``: typed on purpose, after
    the pilot crash rr91swn997nez, so a schema widening such as vocabulary 0.4's
    ``maker: list[str]`` breaks this renderer at type-check time)."""
    return vocab.nodes


def maker_display(nodes: dict[str, VocabNode], node_id: str) -> str:
    """The maker names of the node or its nearest ancestor with a maker, `` / ``-joined (a joint release: LLaVA is
    uw-madison + microsoft, vocabulary 0.4), else ``maker unknown``."""
    seen: set[str] = set()
    nid: str | None = node_id
    while nid and nid not in seen:
        seen.add(nid)
        node = nodes.get(nid)
        if node is None:
            break
        refs = maker_refs(node)
        if refs:
            return " / ".join(r.name for r in refs)
        nid = node.parent
    return "maker unknown"


def denotation(node: VocabNode, nodes: dict[str, VocabNode], node_id: str) -> str:
    if node.kind == "model":
        return "model"
    if node.kind == "group":
        return _DENOTATION.get((node.kind, node.level or ""), "model group")
    n_children = sum(1 for n in nodes.values() if n.parent == node_id)
    return "brand (one model)" if n_children <= 1 else "model family"


def gloss(node: VocabNode) -> str:
    g = (node.gloss or "").strip()
    if g:
        return g
    notes = (node.notes or "").strip()
    return notes.split(". ")[0][:200] if notes else ""


def _references_start(text: str) -> int | None:
    m = _REFS_HEADING.search(text)
    return m.start() if m else None


def _locate(forms: list[str], text: str, refs_at: int | None) -> tuple[int, str]:
    """Count occurrences of the matched forms and classify body vs references.

    Approximate by design: plain substring counting of the forms the matcher reported,
    good enough for a qualitative location note (the matcher stores no offsets)."""
    body = refs = 0
    for form in forms:
        start = 0
        while (i := text.find(form, start)) != -1:
            if refs_at is not None and i >= refs_at:
                refs += 1
            else:
                body += 1
            start = i + len(form)
    total = body + refs
    if total == 0:
        return 0, "not-found"
    if refs_at is None:
        return total, ""
    if refs == 0:
        return total, "body"
    if body == 0:
        return total, "references-only"
    return total, "body+references"


def build_menu(payload: dict[str, Any], nodes: dict[str, VocabNode], text: str) -> list[MenuEntry]:
    refs_at = _references_start(text)
    strong = payload.get("mentions", {})
    weak = {k: v for k, v in payload.get("weak", {}).items() if k not in strong}
    entries = []
    for pool, is_weak in ((strong, False), (weak, True)):
        for nid, surfaces in sorted(pool.items()):
            node = nodes.get(nid)
            forms = sorted(set(surfaces))
            count, location = _locate(forms, text, refs_at)
            written = written_forms(forms, text) or forms
            entries.append(MenuEntry(
                node_id=nid,
                # a payload node absent from the CURRENT vocabulary still gets a menu row —
                # id-as-name, no gloss
                name=node.name if node else nid,
                maker=maker_display(nodes, nid),
                denotation=denotation(node, nodes, nid) if node else "model",
                gloss=gloss(node) if node else "",
                forms=forms,
                count=count,
                location=location,
                weak=is_weak,
                written=tuple(written),
            ))
    return entries


def window_split(
    payload: dict[str, Any], text: str, doc_char_cap: int
) -> tuple[dict[str, Any], list[str]]:
    """Split a mentions payload by what the judge will actually SEE (``text[:doc_char_cap]``).

    Returns ``(payload_in_window, beyond)``. A node is BEYOND the window when its matched forms
    occur in the document but nowhere in the first ``doc_char_cap`` characters — the judge is
    shown a menu entry for a model it cannot find, and the pilot showed what it does with that:
    it reasons the model must be bibliographic and flags ``bib_only`` (doc nr43qcgn4d5fv, 854k
    chars: bloom / efficientnet / gemma / luna all first occur past 631k, all spuriously
    ``bib_only``). Asking a question about invisible evidence does not produce a missing answer,
    it produces a confident wrong one, so the entry is withheld instead and the row is recorded
    as ungraded (``beyond_window:<node>``) downstream.

    A node found nowhere at ALL — including in the full text — is NOT beyond the window; it is a
    matcher artifact (a form the regex matched but a plain substring search cannot re-find), it
    stays on the menu as today, and the rubric's ``matcher_fp`` / ``not_found`` flags cover it.

    Documents at or under the cap take the identity path: no split, no behaviour change.
    """
    if len(text) <= doc_char_cap:
        return payload, []
    head = text[:doc_char_cap]
    forms_by_node: dict[str, set[str]] = {}
    for pool in ("mentions", "weak"):
        for nid, forms in (payload.get(pool) or {}).items():
            forms_by_node.setdefault(nid, set()).update(forms)
    beyond = sorted(
        nid
        for nid, forms in forms_by_node.items()
        if any(f in text for f in forms) and not any(f in head for f in forms)
    )
    if not beyond:
        return payload, []
    dropped = set(beyond)
    in_window = dict(payload)
    for pool in ("mentions", "weak"):
        if payload.get(pool):
            in_window[pool] = {k: v for k, v in payload[pool].items() if k not in dropped}
    return in_window, beyond


def _tojson(value: str) -> str:
    """jinja2's ``tojson`` filter: ``json.dumps`` (ASCII-escaped, sort_keys) with ``<``, ``>``,
    ``&`` and ``'`` escaped as ``\\u003c`` … (``jinja2.utils.htmlsafe_json_dumps``)."""
    return (json.dumps(value, sort_keys=True).replace("<", "\\u003c").replace(">", "\\u003e")
            .replace("&", "\\u0026").replace("'", "\\u0027"))


def _menu_line(e: MenuEntry, show_counts: bool, show_locations: bool) -> str:
    out = f"\n- `{e.node_id}` — **{e.name}** ({e.maker}; {e.denotation})"
    if e.weak:
        out += " [WEAK MATCH — confirm before grading]"
    out += "\n"
    if e.gloss:
        out += f"  {e.gloss}\n"
    shown = e.written or tuple(e.forms)
    out += "  matched text: " + ", ".join(_tojson(w) for w in shown[:MAX_WRITTEN])
    if len(shown) > MAX_WRITTEN:
        out += f" (+{len(shown) - MAX_WRITTEN} more)"
    if show_counts:
        out += f" ({e.count} occurrences)"
    if show_locations and e.location:
        out += f"; found in: {e.location}"
    return out + "\n"


def menu_text(menu: list[MenuEntry], *, show_counts: bool = False, show_locations: bool = True) -> str:
    """The menu as the prompt's ``{{menu}}`` fill: one entry per :func:`_menu_line`, each opening with
    a blank line (v1's shipped flags: counts off, locations on)."""
    return "".join(_menu_line(e, show_counts, show_locations) for e in menu)
