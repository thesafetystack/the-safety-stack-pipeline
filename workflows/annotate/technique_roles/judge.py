"""``annotate.technique_roles`` — response parsing and post-processing repair (ported from the
prototype's ``tools/technique_roles_smoke.py`` parse + clamp code, ``tomas/technique-roles``
df411eb, which its docstring names "the seed of the eventual judge.py").

Pure functions of (judge response, menu slugs, document text, anchorer), unit-tested in
``tests/test_annotate_technique_roles.py``. :func:`extract_json` is kept for tests and tools: the
v4 client parses the reply itself (fence and prose recovery included, ``stack.llm``).

Every repair is RECORDED in the payload's ``repairs`` (as ``model_roles``): the repair rate is the
judge-health signal a corpus run is read against. Rules (:func:`normalize`), and where they differ
from the prototype:

* unknown ``doc_kind`` → ``other`` (``doc_kind_unknown:<x>``, model_roles' name);
* a ``techniques`` row whose slug is not on the menu is MOVED to ``extra_techniques``
  (``unknown_slug_to_extra:<slug>``; name = the slug as written, gloss = its note, hint = its
  dotted prefix when that is on the menu) — never minted; the prototype dropped it;
* a second row for the same slug is dropped (``duplicate_row:<slug>``; rubric §6);
* unknown role names and non-integer / out-of-range strengths are dropped (``unknown_role:``,
  ``bad_strength:``), ``0`` silently (rubric §6 expresses zero by omission); a row left with no
  nonzero role is dropped (``empty_roles_dropped:<label>``; the prototype kept such extras);
* ``mentioned`` is clamped up to max(other roles) (``mentioned_clamped:<label>:<a>-><b>``);
* quotes: NUL-stripped, capped at :data:`MAX_QUOTE_CHARS` (``quote_truncated:<label>``), empty ones
  skipped; **no quote, no row** (rubric §1): a row with no quote left is DROPPED
  (``no_quote_dropped:<label>``; the prototype kept it flagged);
* every quote is anchored against the WHOLE stored text with :mod:`stack.anchor` (the v4
  anchorer: exact → whitespace → punctuation fold → fuzzy at the policy's ``anchor_min_score``,
  digit runs exact) and stored with ``verified``; an elided quote (``...`` / ``…``) verifies when
  each of its ≥ 2 segments of ≥ :data:`MIN_ELISION_SEGMENT` chars anchors (the prototype's
  elision rule). When that fails, the same test runs on the :func:`loosen` projections of the
  quote and the text (inline citations, LaTeX markup, converter table cells, spacing
  around brackets and punctuation, case; 8 of the 9 unverified quotes of the v4 smoke run were
  such false negatives, the ninth a real paraphrase); a quote verified only there is recorded
  (``quote_verified_loose:<label>``). stack.anchor itself is unchanged (affiliation uses it).
  An unverified quote is KEPT (``quote_unverified:<label>``); a row none of whose
  quotes verified is KEPT with the row flag ``quote_unverified`` (open policy question; keeping is
  reversible, dropping is not). The prototype matched with model_roles' v3 normalisation ladder
  instead of the anchorer;
* an extra's ``hint_technique`` that is not on the menu is dropped (``unknown_hint:<x>``);
* ``techniques`` rows are ordered by taxonomy position (two runs over one document compare);
* document flags (:func:`_doc_flags`): ``menu_gap: x`` respelled ``menu_gap:x``; each
  ``sweeps.menu_gaps`` surface becomes a ``menu_gap:<surface>`` flag (deduplicated, capped);
  ``no_techniques_found`` is set exactly when ``techniques`` is empty (``no_techniques_found_added``
  / ``_dropped``);
* ``sweeps`` (rubric v0.4.0 §6/§7, optional on the wire): counted, not stored; a sweep entry naming
  a menu slug with no kept row is recorded (``sweep_without_row:<sweep>:<slug>``), never turned into
  a row.

THE OUTPUT SCHEMA SENT TO THE PROVIDER IS DELIBERATELY PERMISSIVE (:func:`output_schema`; D275
kept it when model_roles moved to ``strict_schema``): no ``additionalProperties: false``, no
strength bounds, only ``doc_kind`` and ``techniques`` required, every role an OPTIONAL key. The
client retries and then degrades on schema-validation failure, so a strict schema converts each
judge quirk above into extra paid calls where this module fixes it for free and says so; and
OpenAI's strict mode refuses this schema (400), so the pinned gpt-6-luna answers on the client's
degrade path (schema folded into the prompt), the condition D275 measured. ``roles`` names its
twelve keys as ``properties`` (R6 item 5): it was an open map (``additionalProperties: {type:
integer}``, keys only in the description), which the Anthropic route accepted without a 400 and
filled with ``{}`` on every row (claude-haiku-5.5, AE run ``20261013T132018Z_all_31a4e585``). Keys
stay optional, not dense-and-nullable as model_roles': the rubric and the prompt write a zero role
by omission (rubric §6), and the degrade-path prompt keeps that wording.

:func:`roles_degenerate` names the answer shape that hole produced (rows present, every roles map
empty, nothing kept); ``main.ask`` fails such an answer instead of storing it as ``empty``.
"""

from __future__ import annotations

import functools
import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any

from stack.schemas.payloads.technique_roles import DOC_KINDS, ROLE_NAMES

#: Rubric §6 says <= 200; the pipeline cap is looser (judges overshoot by a clause; the
#: prototype's and model_roles' value). Below the anchorer's 600-code-point cap.
MAX_QUOTE_CHARS = 300
#: Free-text caps (the prototype's).
MAX_NOTE_CHARS = 300
MAX_GLOSS_CHARS = 300
MAX_NAME_CHARS = 200
#: An elided quote's segments shorter than this are ignored (a 5-char fragment matches anywhere).
MIN_ELISION_SEGMENT = 20
#: The pipeline-written row flag: no quote of the row verified.
QUOTE_UNVERIFIED = "quote_unverified"

_JSON_FENCE = re.compile(r"```(?:json|JSON)?\s*\n(.*?)```", re.DOTALL)
_TRAILING_COMMA = re.compile(r",(\s*[}\]])")
_ELLIPSIS = re.compile(r"\.\.\.|…")


class JudgeParseError(ValueError):
    """The judge's response carried no JSON object this module could recover."""


# ── parsing ──────────────────────────────────────────────────────────────────────────────────


def extract_json(raw: str) -> dict[str, Any]:
    """The JSON object out of one judge response (the prototype's, = model_roles'): a ```json
    fenced block, then any fenced block, then the first ``{`` .. last ``}`` span; each parsed
    strictly, then with trailing commas removed. Anything else raises (a truncated reply too):
    a mangled response is a failed unit, not a silent empty grade."""
    if not raw or not raw.strip():
        raise JudgeParseError("empty judge response")
    candidates: list[str] = [m.group(1) for m in _JSON_FENCE.finditer(raw)]
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end > start:
        candidates.append(raw[start:end + 1])
    for candidate in candidates:
        for attempt in (candidate, _TRAILING_COMMA.sub(r"\1", candidate)):
            try:
                parsed = json.loads(attempt)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
            raise JudgeParseError(f"judge emitted a {type(parsed).__name__}, not an object")
    raise JudgeParseError(f"no parseable JSON object in response ({raw[:200]!r}…)")


def output_schema() -> dict[str, Any]:
    """The schema SENT to the provider: the rubric §6 shape, self-contained and permissive
    (module docstring). On the wire the client sends it as a strict ``response_format``; a
    provider that refuses it (OpenAI: no ``additionalProperties: false``, optional keys) answers
    400 and the client re-sends with the schema folded into the prompt (``stack.llm``'s
    degrade), the condition the prototype ran under (no structured output, a fenced JSON block).
    ``roles``: every role a named, optional integer property, no open map (module docstring)."""
    roles = {
        "type": "object",
        "description": "role -> strength 1..4 (rubric §2), zero-valued roles OMITTED",
        "properties": {name: {"type": "integer"} for name in ROLE_NAMES},
    }
    quotes = {"type": "array", "items": {"type": "string"}}
    flags = {"type": "array", "items": {"type": "string"}}
    text = {"type": "string"}

    def entries(**fields: dict[str, Any]) -> dict[str, Any]:
        return {"type": "array", "items": {"type": "object", "properties": fields}}

    return {
        "type": "object",
        "required": ["doc_kind", "techniques"],
        "properties": {
            # optional on the wire (D275: only doc_kind and techniques are required); the prompt
            # (rubric v0.4.0 §6) asks for it first. Read by normalize, not stored.
            "sweeps": {
                "type": "object",
                "description": "the four required sweeps (rubric §7), written before any row",
                "properties": {
                    "deployment_claims": entries(system=text, technique=text, quote=text, direction=text),
                    "recommendations": entries(technique=text, quote=text, direction=text),
                    "controls_discussed": entries(surface=text, technique=text),
                    "menu_gaps": {"type": "array", "items": text},
                },
            },
            "doc_kind": {"enum": list(DOC_KINDS)},
            "techniques": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["slug", "roles", "quotes"],
                    "properties": {
                        "slug": {"type": "string"},
                        "roles": roles,
                        "quotes": quotes,
                        "flags": flags,
                        "note": {"type": "string"},
                    },
                },
            },
            "extra_techniques": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["name_as_written", "roles", "quotes"],
                    "properties": {
                        "name_as_written": {"type": "string"},
                        "gloss": {"type": "string"},
                        "hint_technique": {"type": "string"},
                        "roles": roles,
                        "quotes": quotes,
                    },
                },
            },
            "flags": flags,
        },
    }


# ── normalization / repair ───────────────────────────────────────────────────────────────────


def normalize(obj: Mapping[str, Any], *, slugs: Sequence[str], text: str, anchorer: Any) -> dict[str, Any]:
    """One judge object -> the repaired payload (roles as sparse maps), repairs listed (module
    docstring). ``slugs`` = the menu in taxonomy order; ``text`` = the whole stored text the quotes
    are anchored in; ``anchorer`` = a :class:`stack.anchor.Anchorer`."""
    repairs: list[str] = []
    order = {s: i for i, s in enumerate(slugs)}

    doc_kind = obj.get("doc_kind")
    if not isinstance(doc_kind, str) or doc_kind not in DOC_KINDS:
        repairs.append(f"doc_kind_unknown:{_short(doc_kind)}")
        doc_kind = "other"

    rows: dict[str, dict[str, Any]] = {}
    moved: list[dict[str, Any]] = []
    for raw_row in _as_list(obj.get("techniques")):
        if not isinstance(raw_row, Mapping):
            repairs.append("bad_row")
            continue
        slug = _clean_str(str(raw_row.get("slug") or ""), repairs).strip()
        if slug not in order:
            # Never mint a slug (rubric §5): the row is an off-menu technique, filed wrongly.
            repairs.append(f"unknown_slug_to_extra:{_short(slug)}")
            prefix = slug.rsplit(".", 1)[0] if "." in slug else ""
            moved.append({"name_as_written": slug, "gloss": raw_row.get("note") or "",
                          "hint_technique": prefix if prefix in order else None,
                          "roles": raw_row.get("roles"), "quotes": raw_row.get("quotes")})
            continue
        if slug in rows:
            repairs.append(f"duplicate_row:{slug}")
            continue
        roles = _clamped_roles(raw_row.get("roles"), slug, repairs)
        if not roles:
            repairs.append(f"empty_roles_dropped:{slug}")
            continue
        quotes = _clean_quotes(raw_row.get("quotes"), slug, text, anchorer, repairs)
        if not quotes:
            repairs.append(f"no_quote_dropped:{slug}")
            continue
        flags = [f for f in (_clean_str(str(f), repairs).strip() for f in _as_list(raw_row.get("flags"))) if f]
        if not any(q["verified"] for q in quotes) and QUOTE_UNVERIFIED not in flags:
            flags.append(QUOTE_UNVERIFIED)
        note = raw_row.get("note")
        rows[slug] = {"slug": slug, "roles": roles, "quotes": quotes, "flags": flags,
                      "note": _clean_str(note.strip(), repairs)[:MAX_NOTE_CHARS]
                      if isinstance(note, str) and note.strip() else None}

    extras: list[dict[str, Any]] = []
    for raw_extra in _as_list(obj.get("extra_techniques")) + moved:
        if not isinstance(raw_extra, Mapping):
            repairs.append("bad_extra_row")
            continue
        name = _clean_str(str(raw_extra.get("name_as_written") or ""), repairs).strip()[:MAX_NAME_CHARS]
        if not name:
            repairs.append("extra_unnamed")
            continue
        label = f"extra:{name[:40]}"
        roles = _clamped_roles(raw_extra.get("roles"), label, repairs)
        if not roles:
            repairs.append(f"empty_roles_dropped:{label}")
            continue
        quotes = _clean_quotes(raw_extra.get("quotes"), label, text, anchorer, repairs)
        if not quotes:
            repairs.append(f"no_quote_dropped:{label}")
            continue
        hint = raw_extra.get("hint_technique")
        hint = _clean_str(hint, repairs).strip() if isinstance(hint, str) else ""
        if hint and hint not in order:
            repairs.append(f"unknown_hint:{_short(hint)}")
            hint = ""
        gloss = raw_extra.get("gloss")
        extras.append({"name_as_written": name,
                       "gloss": _clean_str(gloss.strip(), repairs)[:MAX_GLOSS_CHARS]
                       if isinstance(gloss, str) and gloss.strip() else None,
                       "hint_technique": hint or None, "roles": roles, "quotes": quotes,
                       "flags": [] if any(q["verified"] for q in quotes) else [QUOTE_UNVERIFIED]})

    techniques = [rows[s] for s in sorted(rows, key=lambda s: order[s])]
    sweeps = obj.get("sweeps")
    sweep_counts = _check_sweeps(sweeps, order, rows, repairs) if isinstance(sweeps, Mapping) else None
    gaps = _as_list(sweeps.get("menu_gaps")) if isinstance(sweeps, Mapping) else []
    return {
        "doc_kind": doc_kind,
        "techniques": techniques,
        "extra_techniques": extras,
        "flags": _doc_flags(_as_list(obj.get("flags")), gaps, bool(techniques), repairs),
        "repairs": repairs,
        "sweeps": sweep_counts,
    }


#: The sweeps whose entries name a technique (``technique``), checked against the rows.
SWEEP_LISTS = ("deployment_claims", "recommendations", "controls_discussed")
NO_TECHNIQUES = "no_techniques_found"
MENU_GAP = "menu_gap:"
#: At most this many ``menu_gap:`` flags per document, each surface at most :data:`MAX_GAP_CHARS`.
MAX_GAPS = 30
MAX_GAP_CHARS = 80


def _check_sweeps(sweeps: Mapping[str, Any], order: Mapping[str, int], rows: Mapping[str, Any],
                  repairs: list[str]) -> dict[str, int]:
    """Entry counts of the sweeps (rubric v0.4.0 §6, §7); a sweep entry naming a menu slug that has
    no kept ``techniques`` row is recorded (``sweep_without_row:<sweep>:<slug>``): the judge listed a
    claim, a recommendation or a control and then graded no row for it (or the row was dropped).
    Entries naming no menu slug (a surface, an extra) are not checked. Nothing is synthesized."""
    counts = {}
    for name in (*SWEEP_LISTS, "menu_gaps"):
        entries = _as_list(sweeps.get(name))
        counts[name] = len(entries)
        if name == "menu_gaps":
            continue
        for e in entries:
            slug = e.get("technique") if isinstance(e, Mapping) else None
            if isinstance(slug, str) and slug.strip() in order and slug.strip() not in rows:
                repairs.append(f"sweep_without_row:{name}:{slug.strip()}")
    return counts


def _doc_flags(raw: list[Any], gaps: list[Any], has_techniques: bool, repairs: list[str]) -> list[str]:
    """The document flags: the judge's (``menu_gap: x`` spelled ``menu_gap:x``), plus one
    ``menu_gap:<surface>`` per ``sweeps.menu_gaps`` entry not already flagged (case-insensitive,
    capped); ``no_techniques_found`` exactly when ``techniques`` is empty (added or dropped, recorded
    as ``no_techniques_found_added`` / ``_dropped``)."""
    out: list[str] = []
    seen: set[str] = set()

    def add(flag: str) -> None:
        if flag.lower() not in seen:
            seen.add(flag.lower())
            out.append(flag)

    for f in raw:
        flag = _clean_str(str(f), repairs).strip()
        if flag.startswith(MENU_GAP):
            flag = MENU_GAP + flag[len(MENU_GAP):].strip()[:MAX_GAP_CHARS]
            if flag == MENU_GAP:
                continue
        if flag:
            add(flag)
    for g in gaps:
        surface = _clean_str(g, repairs).strip()[:MAX_GAP_CHARS] if isinstance(g, str) else ""
        if surface and sum(x.startswith(MENU_GAP) for x in out) < MAX_GAPS:
            add(MENU_GAP + surface)
    if has_techniques and NO_TECHNIQUES in out:
        out.remove(NO_TECHNIQUES)
        repairs.append("no_techniques_found_dropped")
    elif not has_techniques and NO_TECHNIQUES not in out:
        out.append(NO_TECHNIQUES)
        repairs.append("no_techniques_found_added")
    return out


def roles_degenerate(normalized: Mapping[str, Any]) -> bool:
    """Is :func:`normalize`'s result the empty-roles answer (module docstring)? True when nothing
    was kept, at least one row was dropped for an empty roles map (``empty_roles_dropped``), and
    no row was dropped later for want of a quote (``no_quote_dropped``: that row HAD a role).
    Rows dropped before the roles test (``bad_row``, ``duplicate_row``, ``extra_unnamed``) do not
    count either way. An answer with no rows at all is a genuine ``empty``, not this."""
    if normalized["techniques"] or normalized["extra_techniques"]:
        return False
    repairs = normalized["repairs"]
    return (any(r.startswith("empty_roles_dropped:") for r in repairs)
            and not any(r.startswith("no_quote_dropped:") for r in repairs))


def to_payload(normalized: Mapping[str, Any]) -> dict[str, Any]:
    """:func:`normalize`'s result as the stored ``TECHNIQUE_ROLES`` payload: each ``roles`` map
    becomes the twelve-field struct with ``0`` for an omitted role."""

    def roles(value: Mapping[str, int]) -> dict[str, int]:
        return {r: int(value.get(r, 0)) for r in ROLE_NAMES}

    def quotes(value: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        return [{"text": q["text"], "verified": bool(q["verified"])} for q in value]

    return {
        "doc_kind": normalized["doc_kind"],
        "techniques": [{"slug": t["slug"], "roles": roles(t["roles"]), "quotes": quotes(t["quotes"]),
                        "flags": list(t["flags"]), "note": t.get("note")} for t in normalized["techniques"]],
        "extra_techniques": [{"name_as_written": x["name_as_written"], "gloss": x.get("gloss"),
                              "hint_technique": x.get("hint_technique"), "roles": roles(x["roles"]),
                              "quotes": quotes(x["quotes"]), "flags": list(x["flags"])}
                             for x in normalized["extra_techniques"]],
        "flags": list(normalized["flags"]),
        "repairs": list(normalized["repairs"]),
    }


def verify_quote(quote: str, text: str, anchorer: Any) -> bool:
    """Does ``quote`` anchor in ``text`` (module docstring: whole, or as an explicit elision; strictly
    or on the loose projection, :func:`verify_how`)?"""
    return verify_how(quote, text, anchorer) is not None


def verify_how(quote: str, text: str, anchorer: Any) -> str | None:
    """``"strict"`` when ``quote`` anchors in ``text`` with the anchorer as is (whole, or each of its
    ≥ 2 elided segments of ≥ :data:`MIN_ELISION_SEGMENT` chars); else ``"loose"`` when it anchors so
    on the :func:`loosen` projections of both; else ``None``. The anchorer's own rules (its fuzzy
    threshold, the hard digit-run rule) apply on both tiers, and the loose projection of a quote keeps
    every digit of the quote in order (:func:`loosen`), so a changed number never verifies: citations
    are removed from the text only when the quote carries none (a quote that does is compared with its
    citations, and the text's, kept)."""
    if _anchors(quote, text, anchorer):
        return "strict"
    cites = not has_citation(quote)
    if _anchors(loosen(quote, citations=cites), loosen(text, citations=cites), anchorer):
        return "loose"
    return None


def _anchors(quote: str, text: str, anchorer: Any) -> bool:
    if anchorer.anchor(quote, text) is not None:
        return True
    segments = [s.strip() for s in _ELLIPSIS.split(quote)]
    long_enough = [s for s in segments if len(s) >= MIN_ELISION_SEGMENT]
    if len(segments) <= 1 or len(long_enough) < 2:
        return False
    return all(anchorer.anchor(s, text) is not None for s in long_enough)


# ── the loose projection (quote verification only) ───────────────────────────────────────────

_NAME = r"[A-Z][\w'’\-]+"
_YEAR = r"(?:19|20)\d\d[a-z]?"
#: One author-year item: ``Smith, 2024`` / ``Smith et al., 2024`` / ``Smith and Lee 2024`` / ``Smith & Lee, 2024``.
_CITE_ITEM = rf"{_NAME}(?:\s+et\s+al\.?|\s+(?:and|&)\s+{_NAME})?,?\s+{_YEAR}"
_CITATIONS = (
    # numeric brackets: [12], [3, 7], [4-6], [2–5]
    re.compile(r"\[\s*\d+(?:\s*[,;–-]\s*\d+)*\s*\]"),
    # a parenthetical list of author-year items: (Rahman et al., 2025; Ren et al., 2024), (see Smith, 2023)
    re.compile(rf"\(\s*(?:(?:see|e\.g\.|cf\.),?\s+)?{_CITE_ITEM}(?:\s*;\s*{_CITE_ITEM})*\s*\)"),
    # narrative: Sharma et al. (2025), Sondej and Yang (2025)
    re.compile(rf"{_NAME}(?:\s+et\s+al\.|\s+(?:and|&)\s+{_NAME})\s+\(\s*{_YEAR}\s*\)"),
)
_GREEK = {name: chr(code) for name, code in (
    ("alpha", 0x3B1), ("beta", 0x3B2), ("gamma", 0x3B3), ("delta", 0x3B4), ("epsilon", 0x3B5),
    ("varepsilon", 0x3B5), ("zeta", 0x3B6), ("eta", 0x3B7), ("theta", 0x3B8), ("iota", 0x3B9),
    ("kappa", 0x3BA), ("lambda", 0x3BB), ("mu", 0x3BC), ("nu", 0x3BD), ("xi", 0x3BE), ("pi", 0x3C0),
    ("rho", 0x3C1), ("sigma", 0x3C3), ("tau", 0x3C4), ("upsilon", 0x3C5), ("phi", 0x3C6), ("varphi", 0x3C6),
    ("chi", 0x3C7), ("psi", 0x3C8), ("omega", 0x3C9), ("Gamma", 0x393), ("Delta", 0x394), ("Theta", 0x398),
    ("Lambda", 0x39B), ("Xi", 0x39E), ("Pi", 0x3A0), ("Sigma", 0x3A3), ("Phi", 0x3A6), ("Psi", 0x3A8),
    ("Omega", 0x3A9))}
_TEX_SYMBOLS = {"in": "∈", "le": "≤", "leq": "≤", "ge": "≥", "geq": "≥", "times": "×", "cdot": "·",
                "pm": "±", "dots": "...", "ldots": "...", "cdots": "...", "to": "→", "infty": "∞"}
_TEX_WRAPPER = re.compile(r"\\(?:mathrm|mathbf|mathit|mathcal|mathsf|text|textrm|textbf|textit|operatorname)\s*\{([^{}]*)\}")
_TEX_MACRO = re.compile(r"\\([A-Za-z]+)")
_TEX_SPACING = re.compile(r"\\[,;:! ]")
_TABLE_SEPARATOR = re.compile(r"^\s*\|?(?:\s*:?-{2,}:?\s*\|)+\s*:?-*:?\s*$")
_SPACE_BEFORE = re.compile(r"\s+([,.;:!?)\]])")
_SPACE_AFTER = re.compile(r"([(\[])\s+")


def has_citation(s: str) -> bool:
    """Does ``s`` hold an inline citation :func:`loosen` would remove?"""
    return any(p.search(s) for p in _CITATIONS)


@functools.lru_cache(maxsize=16)
def loosen(s: str, *, citations: bool = True) -> str:
    """The loose projection a quote and its text are compared on when the strict anchor fails (the
    smoke review's false negatives): NFKC; markdown tables read as running text (separator rows dropped,
    a continuation row — words in its first cell, every other cell empty, the converter's split of a
    wrapped cell — merged into the row above cell by cell, then every cell joined by spaces, numbers
    included; ``<br>`` and ``**`` removed); with ``citations``, inline citations removed (``[12]``,
    ``(Smith et al., 2024; Lee, 2023)``, ``Sharma et al. (2025)``); LaTeX read as text (``$`` dropped,
    ``\\mathrm{C}`` → ``C``, ``\\alpha`` → ``α``, braces, ``^`` and ``_`` dropped); whitespace
    collapsed, and dropped inside brackets and before punctuation; case folded (a quote that
    capitalises its first word mid-sentence). No digit is dropped except inside a removed citation,
    and :func:`verify_how` removes citations only when the quote has none, so the quote's numbers,
    in order, must be in the text (the anchorer's digit-run rule). A paraphrase, a dropped clause or a
    changed number still fails. Cached (the text of a document is loosened once for all its quotes)."""
    s = unicodedata.normalize("NFKC", s).replace("<br>", " ").replace("**", "")
    if "|" in s:
        s = _tables_as_text(s)
    if citations:
        for pattern in _CITATIONS:
            s = pattern.sub(" ", s)
    s = s.replace("$", "")
    for _ in range(3):  # nested wrappers: \mathbf{\mathrm{x}}
        s = _TEX_WRAPPER.sub(r"\1", s)
    s = _TEX_SPACING.sub("", s)
    s = _TEX_MACRO.sub(lambda m: _GREEK.get(m.group(1)) or _TEX_SYMBOLS.get(m.group(1)) or m.group(1), s)
    s = re.sub(r"[{}^_]", "", s)
    s = re.sub(r"\s+", " ", s)
    s = _SPACE_BEFORE.sub(r"\1", s)
    return _SPACE_AFTER.sub(r"\1", s).strip().casefold()


def _tables_as_text(s: str) -> str:
    """Markdown table rows as running text, every cell kept (:func:`loosen`)."""
    out: list[str] = []
    row: list[str] | None = None  # the current table row's cells, still open for a continuation

    def flush() -> None:
        nonlocal row
        if row is not None:
            out.append(" ".join(c for c in row if c))
            row = None

    for line in s.split("\n"):
        if not line.lstrip().startswith("|"):
            flush()
            out.append(line)
            continue
        if _TABLE_SEPARATOR.match(line):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        continuation = (row is not None and len(cells) <= len(row) and cells and cells[0]
                        and not any(cells[1:]))
        if continuation:
            assert row is not None
            row[0] = f"{row[0]} {cells[0]}"
        else:
            flush()
            row = cells
    flush()
    return "\n".join(out)


# ── helpers ──────────────────────────────────────────────────────────────────────────────────


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _short(value: Any) -> str:
    return str(value)[:60]


def _clean_str(value: str, repairs: list[str]) -> str:
    """Strip NUL bytes (the converter can leak them; the prototype's ``nul_stripped``)."""
    if "\x00" in value:
        if "nul_stripped" not in repairs:
            repairs.append("nul_stripped")
        return value.replace("\x00", "")
    return value


def _clamped_roles(value: Any, label: str, repairs: list[str]) -> dict[str, int]:
    """Clean a ``roles`` map and apply the rubric §3.1 ``mentioned >= max(others)`` clamp
    (arity-independent: it takes the max over every non-``mentioned`` role present)."""
    clean: dict[str, int] = {}
    if not isinstance(value, Mapping):
        if value is not None:
            repairs.append(f"bad_roles:{label}")
        value = {}
    for role, strength in value.items():
        name = str(role)
        if name not in ROLE_NAMES:
            repairs.append(f"unknown_role:{label}:{_short(name)}")
            continue
        if isinstance(strength, bool) or not isinstance(strength, int):
            repairs.append(f"bad_strength:{label}:{name}:{_short(strength)}")
            continue
        if strength == 0:
            continue  # rubric §6 expresses zero by omission: not a repair, the intent is clear
        if not 1 <= strength <= 4:
            repairs.append(f"bad_strength:{label}:{name}:{strength}")
            continue
        clean[name] = strength
    others = max((s for r, s in clean.items() if r != "mentioned"), default=0)
    mentioned = clean.get("mentioned", 0)
    if others > mentioned:
        repairs.append(f"mentioned_clamped:{label}:{mentioned}->{others}")
        clean["mentioned"] = others
    return {role: clean[role] for role in ROLE_NAMES if role in clean}  # rubric §3 order


def _clean_quotes(value: Any, label: str, text: str, anchorer: Any, repairs: list[str]) -> list[dict[str, Any]]:
    """Clean, cap and verify a row's quotes: ``[{text, verified}]`` in the judge's order. An
    unverified quote is kept (deleting it would hide the fabrication the check exposes)."""
    out: list[dict[str, Any]] = []
    for q in _as_list(value):
        if not isinstance(q, str):
            repairs.append(f"bad_quote:{label}")
            continue
        quote = _clean_str(q, repairs).strip()
        if not quote:
            continue
        if len(quote) > MAX_QUOTE_CHARS:
            repairs.append(f"quote_truncated:{label}")
            quote = quote[:MAX_QUOTE_CHARS]
        how = verify_how(quote, text, anchorer)
        if how is None:
            repairs.append(f"quote_unverified:{label}")
        elif how == "loose":
            repairs.append(f"quote_verified_loose:{label}")
        out.append({"text": quote, "verified": how is not None})
    return out
