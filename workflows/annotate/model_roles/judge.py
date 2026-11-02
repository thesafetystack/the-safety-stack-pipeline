"""``annotate.model_roles`` — response parsing and post-processing repair (ported from v3
``judge.py``, ``tomas/model-roles`` d011e5e; the two-judge merge is not ported: v4 runs v3's
shipped single-judge mode only).

Pure functions of (judge response, menu, document text), unit-tested in
``tests/test_annotate_model_roles.py``. v4 changes: :func:`normalize` no longer stamps ``judge``,
``low_confidence`` or ``vocab_stamp`` (the evidence row and the config digest carry that
provenance), and :func:`to_payload` maps its result onto the stored ``MODEL_ROLES`` shape (every
role an explicit integer, ``0`` for an omitted one). :func:`extract_json` is kept for tests and
tools: the v4 client parses the reply itself.

WHY THE CLAMPS LIVE HERE. The rubric (``prompts/rubric-v0.5.3.md``) tells the judge what to emit; two rounds of
5-judge calibration (kept in the author's notes, not in this repository) then measured what
judges ACTUALLY emit, and the answer was: the schema is followed, three or four rules are not.
Every arm violated the ``> 200 chars`` quote cap; several dropped the ``mentioned >= max(others)``
invariant; two omitted menu rows entirely; one emitted a trailing comma. The calibration's
standing recommendation is "clamp before scoring in production; enforce the quote cap in the
pipeline, not the prompt" — because a rule the judge must remember costs prompt tokens and fails
silently, while a rule the pipeline enforces costs nothing and is auditable.

Auditable is the operative word: every repair is RECORDED in the payload's ``repairs`` list rather
than applied silently. A repaired row must not be indistinguishable from a clean one — the repair
rate is the judge-health signal a corpus run is read against, and it is also the tripwire that
says a model swap has degraded output. The one thing never repaired away is a quote the pipeline
cannot re-find: it is kept and marked ``quote_unverified``, because deleting it would hide exactly
the fabrication the check exists to expose.

THE OUTPUT SCHEMA CONSTRAINS SHAPE, NOT CONTENT (:func:`output_schema`): no strength bounds, no
menu enum, almost nothing required. The client retries on schema-validation failure
(``stack.llm``), so a content constraint converts each of the above judge quirks into extra paid
calls and possibly a failed unit — where this module fixes it for free and says so. The SHAPE is
OpenAI strict mode's (``main.SCHEMA`` = ``stack.llm.strict_schema`` of it, round 5 T-ANN; the open
``roles`` map it had drew an HTTP 400 on every call before the client's degrade): ``roles`` is a
fixed-key object, every role present, ``null`` (or ``0``) for a zero-valued role; the keys that
were optional are present and ``null``. :func:`normalize` reads ``null`` as absent, so a
strict-shaped answer and the permissive one it stands for give the same payload.
"""

from __future__ import annotations

import html
import json
import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from stack.schemas.payloads.model_roles import DOC_KINDS, ROLE_NAMES

# Rubric §6 says <= 200; the pipeline cap is deliberately looser at 300. The violation is
# universal and benign (judges overshoot by a clause, not by a paragraph), and a truncation is
# lossy — 300 keeps the sentence that carries the evidence while still bounding the payload.
MAX_QUOTE_CHARS = 300

# An elided quote ("... the model was fine-tuned ... on 12 tasks ...") is a real span the judge
# shortened, not a fabrication. Segments shorter than this are ignored when checking an elision:
# a 5-character fragment matches somewhere in any document and would verify nothing.
MIN_ELISION_SEGMENT = 20

#: The maker's standard release suffixes that denote the node itself (rubric 0.6 grain rule):
#: "Llama-3-8B-Instruct", "LLaMA-3-8B-IT", "Qwen2.5-7B-Chat", "Llama-3-8B-Base", "-hf" checkpoints.
RELEASE_SUFFIXES = ("instruct", "it", "chat", "base", "hf", "instructhf", "chathf")

_JSON_FENCE = re.compile(r"```(?:json|JSON)?\s*\n(.*?)```", re.DOTALL)
_TRAILING_COMMA = re.compile(r",(\s*[}\]])")
_WS = re.compile(r"\s+")
_MD_ESCAPE = re.compile(r"\\([^a-zA-Z0-9])")  # docling's \_ \* \[ … around identifiers
_LOOSE_MARKS = re.compile(r"[-\[\]()*_|`#]")  # OCR hyphens + markdown syntax (see _loose)
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")  # [text](target) -> text, target and all
_TERMINAL_PUNCT = ".,;:!?\"'"

# Typographic folding for quote re-finding. NFKC already handles NBSP and ligatures; these are the
# pairs it leaves alone and that converters and judges disagree about. The ellipsis maps to "..."
# so the elision check below has one form to split on.
_PUNCT = str.maketrans({
    "‘": "'", "’": "'", "‛": "'",
    "“": '"', "”": '"',
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "―": "-",
    "−": "-",
    "…": "...",
})


class JudgeParseError(ValueError):
    """The judge's response carried no JSON object this module could recover."""


# ── parsing ───────────────────────────────────────────────────────────────────────────────────


def extract_json(raw: str) -> dict[str, Any]:
    """The JSON object out of one judge response, tolerating the wrappers judges actually emit.

    In order: a ```json fenced block (the prompt asks for one), any fenced block, then the first
    ``{`` .. last ``}`` span — which is what handles a bare object with prose before or after it.
    Each candidate is parsed strictly first; only on failure does a LIGHT repair pass run (trailing
    commas before ``}``/``]``, measured once in 110 calibration outputs). Anything still unparseable
    raises rather than being guessed at: a mangled response is a failed unit, not a silent empty
    grade.
    """
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
    """The rubric §6 shape, self-contained and content-permissive; ``main.SCHEMA`` (what is sent
    and validated) is ``stack.llm.strict_schema`` of it.

    Self-contained because ``stack.llm.validate_json`` is a deliberately minimal validator with no
    ``$ref`` resolution (the same reason ``annotate.screen`` inlines its one ``$ref``). Permissive
    in content because every constraint here is a retry trigger — see the module docstring. The
    STORED payload is typed (``MODEL_ROLES``, :func:`to_payload`) after this module has repaired
    the difference. ``roles`` is a fixed-key object (strict mode admits no open map): every role a
    key, none required, so strict mode makes each a nullable integer the model must emit. v3
    calibrated without structured output (``structured_output: false``); the rubric's "omit
    zero-valued roles" becomes ``null`` here (the ``description`` says so, the rubric is unchanged).
    """
    roles = {
        "type": "object",
        "description": (
            "role -> strength 1..4 (rubric §2); every key present, null for a zero-valued role "
            "(the rubric's 'omit')"
        ),
        "properties": {name: {"type": "integer"} for name in ROLE_NAMES},
    }
    return {
        "type": "object",
        "required": ["doc_kind", "models"],
        "properties": {
            "doc_kind": {"enum": list(DOC_KINDS)},
            "models": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["node_id", "roles"],
                    "properties": {
                        "node_id": {"type": "string"},
                        "roles": roles,
                        "evidence": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "required": ["roles", "quote"],
                                "properties": {
                                    "roles": {"type": "array", "items": {"type": "string"}},
                                    "quote": {"type": "string"},
                                },
                            },
                        },
                        "note": {"type": "string"},
                        "refined_name": {"type": "string"},
                    },
                },
            },
            "variant_models": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["base_node_id", "names_as_written", "quote", "roles"],
                    "properties": {
                        "base_node_id": {"type": "string"},
                        "names_as_written": {"type": "array", "items": {"type": "string"}},
                        "quote": {"type": "string"},
                        "roles": roles,
                    },
                },
            },
            "extra_models": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["name_as_written", "quote", "roles"],
                    "properties": {
                        "name_as_written": {"type": "string"},
                        "quote": {"type": "string"},
                        "roles": roles,
                    },
                },
            },
            "flags": {"type": "array", "items": {"type": "string"}},
        },
    }


# ── normalization / repair ────────────────────────────────────────────────────────────────────


def normalize(
    obj: Mapping[str, Any],
    *,
    menu_nodes: Sequence[str],
    menu_names: Mapping[str, str] | None = None,
    menu_kinds: Mapping[str, str] | None = None,
    text: str,
    beyond_window: Sequence[str] = (),
    refine: Callable[[str, str], tuple[str | None, str]] | None = None,
) -> dict[str, Any]:
    """One judge object -> the stored ``model_roles`` payload, repaired and with the repairs listed.

    ``menu_nodes`` is the menu in the order it was shown; the emitted ``models`` array follows it,
    so two runs over the same document produce byte-comparable rows regardless of the order the
    judge chose. Rules applied, each recorded in ``repairs``:

    * unknown ``doc_kind`` -> ``other``;
    * a row for a node that was not on the menu is DROPPED (the menu is the vocabulary contract;
      an invented node id has no referent) — a genuinely missed model belongs in ``extra_models``;
    * a second row for the same node is dropped (rubric §6: each model appears exactly once);
    * unknown role names and non-integer / out-of-range strengths are dropped, ``0`` and ``null``
      being dropped silently since the rubric expresses zero by omission (strict mode by ``null``);
    * ``mentioned`` is clamped up to max(other roles) — rubric §3.1's invariant, the one the
      calibration measured every judge dropping;
    * quotes are NUL-stripped, capped at :data:`MAX_QUOTE_CHARS`, and re-found in the document
      whitespace-, entity- and typography-normalized; a quote that cannot be re-found is KEPT and
      marked ``quote_unverified``;
    * an ``extra_models`` row naming something already on the menu is dropped (rubric §6 forbids
      double-listing) — the menu row is the one that carries the grade;
    * an ``extra_models`` row that is a NAMED DERIVATIVE of a menu node (:func:`_derives_from`) is
      moved into ``variant_models`` under that base, and variant rows sharing a base are merged
      into one; a variant row whose base is not on the menu is demoted back to ``extra_models``;
    * every menu node missing a row gets an all-zero one;
    * a variant row whose every name is the base node's own Instruct/IT/Chat/Base release
      (:func:`_is_release_grain`) is folded back onto the base row (rubric 0.6 grain rule: the node
      covers those releases), roles merged by max, its ``subsumed:<base>:<name>`` flags dropped
      (``instruct_folded:<base>``);
    * a row's ``refined_name`` (rubric 0.6: the specific model the document determines behind a family,
      weak or product mention) is passed to ``refine(menu node, name)``, which returns the node it
      resolves to within the menu node's lineage or ``(None, why)``: accepted, the row grades that node
      and ``matched_node_id`` keeps the menu node (``refined:<menu>-><node>``); refused, the row stays
      on the menu node (``refinement_dropped:<menu>:<why>``); rows that end on the same node are merged
      in menu order, roles by max, evidence and notes concatenated (``refinement_merged:<node>:<menu>``).
      Without ``refine`` a ``refined_name`` is ignored (``refinement_dropped:<menu>:no_resolver``).
    """
    repairs: list[str] = []
    norm_text = _normalize(text)
    order = {nid: i for i, nid in enumerate(menu_nodes)}
    names = {str(v).strip().casefold() for v in (menu_names or {}).values() if str(v).strip()}
    names |= {nid.casefold() for nid in menu_nodes}

    doc_kind = obj.get("doc_kind")
    if not isinstance(doc_kind, str) or doc_kind not in DOC_KINDS:
        repairs.append(f"doc_kind_unknown:{_short(doc_kind)}")
        doc_kind = "other"

    rows: dict[str, dict[str, Any]] = {}
    for raw_row in _as_list(obj.get("models")):
        if not isinstance(raw_row, Mapping):
            repairs.append("bad_row")
            continue
        node_id = _clean_str(str(raw_row.get("node_id", "")), repairs).strip()
        if node_id not in order:
            repairs.append(f"unknown_node:{_short(node_id)}")
            continue
        if node_id in rows:
            repairs.append(f"duplicate_row:{node_id}")
            continue
        roles = _clamped_roles(raw_row.get("roles"), node_id, repairs)
        row: dict[str, Any] = {
            "node_id": node_id,
            "roles": roles,
            "evidence": _clean_evidence(raw_row.get("evidence"), node_id, norm_text, repairs),
        }
        note = raw_row.get("note")
        if isinstance(note, str) and note.strip():
            row["note"] = _clean_str(note.strip(), repairs)[:400]
        refined = raw_row.get("refined_name")
        if isinstance(refined, str) and refined.strip():
            row["refined_name"] = _clean_str(refined.strip(), repairs)[:200]
        rows[node_id] = row

    for node_id in menu_nodes:  # rubric §6: every menu node gets a row, graded or all-zero
        if node_id not in rows:
            repairs.append(f"row_missing:{node_id}")
            rows[node_id] = {"node_id": node_id, "roles": {}, "evidence": []}

    # Nodes withheld from the menu because they occur only past the prompt's character cap
    # (render.window_split). They get an all-zero row like any ungraded node, but the repair says
    # UNGRADED rather than absent: the model is in the document, the judge was never shown it, and
    # a reader must not take the zero for a judgement. They are exempt from the all-zero-row flag
    # rule below for the same reason — no flag the rubric defines is true of them.
    for node_id in beyond_window:
        if node_id not in rows:
            repairs.append(f"beyond_window:{node_id}")
            rows[node_id] = {"node_id": node_id, "roles": {}, "evidence": []}

    # ── variants and extras ───────────────────────────────────────────────────────────────────
    # Order matters: variant rows are collected first so that an extra_models candidate found to
    # be a derivative of a menu base MERGES into an existing variant row rather than opening a
    # second one for the same base.
    variants: dict[str, dict[str, Any]] = {}
    demoted: list[dict[str, Any]] = []
    for raw_variant in _as_list(obj.get("variant_models")):
        if not isinstance(raw_variant, Mapping):
            repairs.append("bad_variant_row")
            continue
        base = _clean_str(str(raw_variant.get("base_node_id", "")), repairs).strip()
        variant_names = [
            _clean_str(str(n), repairs).strip()[:200]
            for n in _as_list(raw_variant.get("names_as_written"))
            if isinstance(n, str) and n.strip()
        ]
        if not variant_names:
            repairs.append(f"variant_unnamed:{_short(base)}")
            continue
        roles = _clamped_roles(raw_variant.get("roles"), f"variant:{base or '?'}", repairs)
        quote = raw_variant.get("quote")
        if base not in order:
            # A derivative of something not on the menu is not a variant of anything this row can
            # name; it is an off-menu model, and extra_models is where those live.
            repairs.append(f"variant_demoted:{_short(base)}")
            for name in variant_names:
                demoted.append({"name_as_written": name, "quote": quote, "roles": roles})
            continue
        _merge_variant(variants, base, variant_names, quote, roles, norm_text, repairs)

    extras: list[dict[str, Any]] = []
    for raw_extra in _as_list(obj.get("extra_models")) + demoted:
        if not isinstance(raw_extra, Mapping):
            repairs.append("bad_extra_row")
            continue
        name = _clean_str(str(raw_extra.get("name_as_written", "")), repairs).strip()[:200]
        if not name:
            repairs.append("extra_unnamed")
            continue
        if name.casefold() in names:
            repairs.append(f"extra_on_menu:{_short(name)}")
            continue
        derived_from = _derives_from(name, menu_nodes, menu_names, menu_kinds)
        if derived_from is not None:
            # A named derivative of a menu model belongs in variant_models, joined to its base:
            # as an extra it would be severed from the lineage the census counts.
            repairs.append(f"variant_from_extra:{_short(name)}:{derived_from}")
            _merge_variant(
                variants, derived_from, [name], raw_extra.get("quote"),
                _clamped_roles(raw_extra.get("roles"), f"variant:{derived_from}", repairs),
                norm_text, repairs,
            )
            continue
        label = f"extra:{name}"
        quote = raw_extra.get("quote")
        if not isinstance(quote, str) or not quote.strip():
            repairs.append(f"quote_missing:{label}")
            cleaned = ""
        else:
            cleaned = _clean_quote(quote, label, norm_text, repairs)
        extras.append({
            "name_as_written": name,
            "quote": cleaned,
            "roles": _clamped_roles(raw_extra.get("roles"), label, repairs),
        })

    flags: list[str] = []
    for flag in _as_list(obj.get("flags")):
        if isinstance(flag, str) and flag.strip():
            flags.append(_clean_str(flag.strip(), repairs)[:200])
        else:
            repairs.append(f"bad_flag:{_short(flag)}")
    flags = _canonical_flag_targets(flags, rows, variants, repairs)
    flags = _fold_release_variants(rows, variants, flags, menu_names or {}, repairs)
    _require_flag_on_all_zero_rows(flags, rows, set(beyond_window), repairs)

    ordered = [rows[nid] for nid in sorted(rows, key=lambda n: order.get(n, len(order)))]
    return {
        "doc_kind": doc_kind,
        "models": _refined_rows(ordered, refine, repairs),
        "variant_models": [variants[b] for b in sorted(variants, key=lambda n: order[n])],
        "extra_models": extras,
        "flags": flags,
        "repairs": repairs,
    }


def _refined_rows(rows: list[dict[str, Any]], refine: Callable[[str, str], tuple[str | None, str]] | None,
                  repairs: list[str]) -> list[dict[str, Any]]:
    """Apply each row's ``refined_name`` (:func:`normalize`'s docstring) and merge rows that end on the
    same node; every row gets ``matched_node_id`` (its menu node)."""
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        menu_node = row["node_id"]
        row["matched_node_id"] = menu_node
        name = row.pop("refined_name", None)
        if name is not None:
            target, why = refine(menu_node, name) if refine is not None else (None, "no_resolver")
            if target is None:
                repairs.append(f"refinement_dropped:{menu_node}:{_short(why)}")
            elif target != menu_node:
                row["node_id"] = target
                repairs.append(f"refined:{menu_node}->{target}")
        first = out.get(row["node_id"])
        if first is None:
            out[row["node_id"]] = row
            continue
        repairs.append(f"refinement_merged:{row['node_id']}:{menu_node}")
        merged = {r: max(first["roles"].get(r, 0), row["roles"].get(r, 0)) for r in {*first["roles"], *row["roles"]}}
        first["roles"] = _clamped_roles(merged, row["node_id"], repairs)
        first["evidence"] = [*first["evidence"], *row["evidence"]]
        notes = [n for n in (first.get("note"), row.get("note")) if n]
        if notes:
            first["note"] = " | ".join(notes)[:400]
    return list(out.values())


def to_payload(normalized: Mapping[str, Any]) -> dict[str, Any]:
    """:func:`normalize`'s result as the stored ``MODEL_ROLES`` payload: each ``roles`` map
    becomes the ten-field struct with ``0`` for an omitted role; ``note`` is NULL when absent."""

    def roles(value: Mapping[str, int]) -> dict[str, int]:
        return {r: int(value.get(r, 0)) for r in ROLE_NAMES}

    return {
        "doc_kind": normalized["doc_kind"],
        "models": [{"node_id": m["node_id"], "roles": roles(m["roles"]),
                    "evidence": [{"roles": list(e["roles"]), "quote": e["quote"]} for e in m["evidence"]],
                    "note": m.get("note"), "matched_node_id": m.get("matched_node_id", m["node_id"])}
                   for m in normalized["models"]],
        "variant_models": [{"base_node_id": v["base_node_id"], "names_as_written": list(v["names_as_written"]),
                            "quote": v["quote"], "roles": roles(v["roles"])} for v in normalized["variant_models"]],
        "extra_models": [{"name_as_written": x["name_as_written"], "quote": x["quote"], "roles": roles(x["roles"])}
                         for x in normalized["extra_models"]],
        "flags": list(normalized["flags"]),
        "repairs": list(normalized["repairs"]),
    }


# ── helpers ───────────────────────────────────────────────────────────────────────────────────


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _short(value: Any) -> str:
    return str(value)[:60]


def _clean_str(value: str, repairs: list[str]) -> str:
    """Strip NUL bytes (v3: Postgres rejected them in TEXT; the converter can leak them through)."""
    if "\x00" in value:
        if "nul_stripped" not in repairs:
            repairs.append("nul_stripped")
        return value.replace("\x00", "")
    return value


def _clamped_roles(value: Any, label: str, repairs: list[str]) -> dict[str, int]:
    """Clean a ``roles`` map and apply the rubric §3.1 ``mentioned >= max(others)`` clamp."""
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
        if strength is None:
            continue  # strict mode's zero (the rubric's omission): not a repair
        if isinstance(strength, bool) or not isinstance(strength, int):
            repairs.append(f"bad_strength:{label}:{name}:{_short(strength)}")
            continue
        if strength == 0:
            continue  # rubric §6 expresses zero by omission — not a repair, the intent is clear
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


def _flag_targets(flag: str) -> list[str]:
    """The node ids / names a rubric §6 flag points at: everything after the flag word.

    ``matcher_fp:<node>`` and ``bib_only:<node>`` and ``not_found:<node>`` name one;
    ``subsumed:<family>:<specific>`` names two, and both are legitimately "flagged".
    """
    return [part for part in flag.split(":")[1:] if part]


def _canonical_flag_targets(
    flags: list[str],
    rows: Mapping[str, dict[str, Any]],
    variants: Mapping[str, dict[str, Any]],
    repairs: list[str],
) -> list[str]:
    """Rewrite flag targets to the casing the payload actually uses.

    ``subsumed:<family>:<specific>`` may name a VARIANT by its written name (rubric 0.5), and the
    pilot caught the judge writing that name in a different case from its own ``variant_models``
    entry two lines earlier. A consumer joining flags to rows by string equality would silently
    miss those, so the case is normalized here — to the payload's own spelling, not to lowercase,
    because the variant name is verbatim document text and must stay readable as such.
    """
    known: dict[str, str] = {nid.casefold(): nid for nid in rows}
    for row in variants.values():
        for name in row["names_as_written"]:
            known.setdefault(name.casefold(), name)
    out: list[str] = []
    for flag in flags:
        parts = flag.split(":")
        fixed = [parts[0]] + [known.get(p.casefold(), p) for p in parts[1:]]
        if fixed != parts:
            repairs.append(f"flag_recased:{flag[:60]}")
        out.append(":".join(fixed))
    return out


def _require_flag_on_all_zero_rows(
    flags: list[str],
    rows: Mapping[str, dict[str, Any]],
    exempt: set[str],
    repairs: list[str],
) -> None:
    """Rubric 0.5.2: an all-zero menu row must say WHY it is all-zero, via exactly one flag.

    A bare all-zero row is unreadable — "the matcher was wrong", "it is only in the bibliography"
    and "I could not find it" are different facts with different consequences downstream, and the
    pilot left 52 of 496 such rows bare. The missing flag is synthesized as ``not_found``, the
    weakest of the three (it claims only that the judge did not locate the model), and the
    synthesis is recorded so nobody reads it as the judge's own call.

    Mutates ``flags`` in place; rows in ``exempt`` (withheld from the menu) are skipped.
    """
    flagged: dict[str, int] = {}
    for flag in flags:
        for target in _flag_targets(flag):
            flagged[target] = flagged.get(target, 0) + 1
    for node_id, row in rows.items():
        if row["roles"] or node_id in exempt:
            continue
        count = flagged.get(node_id, 0)
        if count == 0:
            flags.append(f"not_found:{node_id}")
            repairs.append(f"flag_synthesized:not_found:{node_id}")
        elif count > 1:
            # Left as the judge wrote it: two flags on one row is over-specified, not wrong, and
            # dropping one would be the pipeline choosing which fact about the row survives.
            repairs.append(f"flag_ambiguous:{node_id}")


_ALNUM = re.compile(r"[^a-z0-9]+")

# render.denotation()'s two SINGLE-MODEL outputs; everything else it emits ("model family",
# "product line", "release series", "generation", "model group") denotes a set of models, under
# which a numbered name is a member rather than a sibling. Default when the caller passes no
# denotations: single, the conservative reading (it only ever declines to move a row).
_SINGLE_MODEL_DENOTATIONS = frozenset({"model", "brand (one model)"})


def _key(name: str) -> str:
    """Comparison key for model names: casefolded, punctuation removed ("Llama-3.1 70B" ->
    "llama3170b"). The separators are exactly what the mention matcher already treats as noise."""
    return _ALNUM.sub("", name.casefold())


def _derives_from(
    name: str,
    menu_nodes: Sequence[str],
    menu_names: Mapping[str, str] | None,
    menu_kinds: Mapping[str, str] | None = None,
) -> str | None:
    """Which menu node does ``name`` sit UNDER, if any — a derivative or an unlisted family member?

    Rubric §5 (v0.5) puts both in ``variant_models``: a third-party fine-tune of a menu model
    ("Llama-3.1-70B-Instruct") and a maker's own member the vocabulary happens to lack
    ("Qwen2.5-Math-7B" under a ``qwen2.5`` node). ``extra_models`` is then cleanly "no menu
    ancestor at all".

    A deliberately narrow, mechanical test: the candidate must START with a menu node's id or
    name; the LONGEST matching base wins, so a more specific menu entry takes precedence. One
    guard, and it is where the two cases differ: when the base is a SINGLE MODEL, a digit right
    after the base means a numbered sibling, not a descendant — "GPT-4.5" is not a variant of
    "GPT-4". When the base is a family/line/series (``menu_kinds`` carries the menu's denotation),
    a numbered continuation is exactly what a member looks like ("Llama 3.3 70B" under ``llama``),
    so the guard is lifted.

    It catches only the name-sharing half of the problem, which is the half a string test CAN
    settle: Zephyr-7B shares no substring with Mistral-7B and is invisible here. That half is the
    JUDGE's job (rubric §5) — this function exists to fix the case where the judge reported the
    model but filed it under extra_models, not to discover derivations on its own.
    """
    key = _key(name)
    best: str | None = None
    best_len = 0
    for node_id in menu_nodes:
        single = (menu_kinds or {}).get(node_id, "model") in _SINGLE_MODEL_DENOTATIONS
        for candidate in (node_id, (menu_names or {}).get(node_id, "")):
            base = _key(candidate)
            if not base or len(base) >= len(key) or not key.startswith(base):
                continue
            if single and key[len(base)].isdigit():
                continue  # a numbered sibling of a single model, not a descendant of it
            if len(base) > best_len:
                best, best_len = node_id, len(base)
    return best


def _merge_variant(
    variants: dict[str, dict[str, Any]],
    base: str,
    names: Sequence[str],
    quote: Any,
    roles: dict[str, int],
    norm_text: str,
    repairs: list[str],
) -> None:
    """Fold one derivative claim into the single variant row for its base.

    One row per base is the contract (see the payload schema): derivatives of the same base share
    a role vector and are told apart only by their names. Merging takes the MAX strength per role
    — the row says what this document does with the lineage, so a role exercised on any of its
    checkpoints is exercised — and then re-applies the ``mentioned`` clamp over the result.
    """
    label = f"variant:{base}"
    cleaned = (
        _clean_quote(quote, label, norm_text, repairs)
        if isinstance(quote, str) and quote.strip()
        else ""
    )
    row = variants.get(base)
    if row is None:
        if not cleaned:
            repairs.append(f"quote_missing:{label}")
        variants[base] = {
            "base_node_id": base,
            "names_as_written": list(dict.fromkeys(names)),
            "quote": cleaned,
            "roles": roles,
        }
        return
    repairs.append(f"variant_merged:{base}")
    row["names_as_written"] = list(dict.fromkeys([*row["names_as_written"], *names]))
    row["quote"] = row["quote"] or cleaned
    merged = {r: max(row["roles"].get(r, 0), roles.get(r, 0)) for r in {*row["roles"], *roles}}
    row["roles"] = _clamped_roles(merged, label, repairs)


def _is_release_grain(name: str, base_keys: set[str]) -> bool:
    """``name`` is a base's own standard release: its key is a base key plus one of
    :data:`RELEASE_SUFFIXES` ("Llama-3-8B-Instruct" over "Llama 3 8B")."""
    k = _key(name)
    return any(k == b + suf for b in base_keys for suf in RELEASE_SUFFIXES)


def _fold_release_variants(
    rows: dict[str, dict[str, Any]],
    variants: dict[str, dict[str, Any]],
    flags: list[str],
    menu_names: Mapping[str, str],
    repairs: list[str],
) -> list[str]:
    """Fold variant rows that only name the base's own Instruct/IT/Chat/Base releases onto the base
    row (module docstring of :func:`normalize`); returns the flags without the dropped ``subsumed``
    ones. A row naming any other derivative stays a variant row (folding would merge a real fine-tune
    into its base)."""
    dropped: set[tuple[str, str]] = set()
    for base in list(variants):
        row = variants[base]
        keys = {_key(base)} | ({_key(menu_names[base])} if base in menu_names else set())
        if not row["names_as_written"] or not all(_is_release_grain(n, keys) for n in row["names_as_written"]):
            continue
        target = rows[base]
        merged = {r: max(target["roles"].get(r, 0), row["roles"].get(r, 0)) for r in {*target["roles"], *row["roles"]}}
        target["roles"] = _clamped_roles(merged, base, repairs)
        if row["quote"]:
            strong = [r for r, v in row["roles"].items() if v >= 2]
            if strong:
                target["evidence"].append({"roles": strong, "quote": row["quote"]})
        dropped |= {(base.casefold(), n.casefold()) for n in row["names_as_written"]}
        del variants[base]
        repairs.append(f"instruct_folded:{base}")
    if not dropped:
        return flags
    out = []
    for flag in flags:
        parts = flag.split(":")
        if parts[0] == "subsumed" and len(parts) == 3 and (parts[1].casefold(), parts[2].casefold()) in dropped:
            continue
        out.append(flag)
    return out


def _clean_evidence(
    value: Any, label: str, norm_text: str, repairs: list[str]
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for entry in _as_list(value):
        if not isinstance(entry, Mapping):
            repairs.append(f"evidence_dropped:{label}")
            continue
        quote = entry.get("quote")
        if not isinstance(quote, str) or not quote.strip():
            repairs.append(f"evidence_dropped:{label}")
            continue
        roles = [
            _clean_str(str(r), repairs)[:40]
            for r in _as_list(entry.get("roles"))
            if isinstance(r, str) and r.strip()
        ]
        out.append({"roles": roles, "quote": _clean_quote(quote, label, norm_text, repairs)})
    return out


def _clean_quote(quote: str, label: str, norm_text: str, repairs: list[str]) -> str:
    """NUL-strip, cap, and verify one quote against the document. Never deletes."""
    cleaned = _clean_str(quote.strip(), repairs)
    if len(cleaned) > MAX_QUOTE_CHARS:
        repairs.append(f"quote_truncated:{label}")
        cleaned = cleaned[:MAX_QUOTE_CHARS]
    if not _found_in(cleaned, norm_text):
        repairs.append(f"quote_unverified:{label}")
    return cleaned


def _normalize(value: str) -> str:
    """The comparison form for quote re-finding: entities resolved, NFKC, typography folded,
    whitespace collapsed. Case is NOT folded — a judge that changes case has changed the span."""
    folded = unicodedata.normalize("NFKC", html.unescape(value)).translate(_PUNCT)
    return _WS.sub(" ", folded).strip()


def _loose(value: str) -> str:
    """The most permissive comparison form: :func:`_squeeze`, case-folded, with the marks a judge
    routinely adds or drops while quoting removed — hyphens (OCR line-break joins), markdown
    bracket/emphasis syntax, and table pipes.

    Calibrated against the 100-doc pilot, where 91 of 856 quotes (10.6 %) were marked unverified
    and hand-audit put real fabrication at 1-2 %. The false marks were all presentational:
    sentence-initial recapitalization, a trailing period the judge added, an OCR hyphen, a
    markdown link's brackets, a dropped footnote marker. None of them changes which span of the
    document is being pointed at, and a `quote_unverified` rate dominated by them is a signal
    nobody can read.

    It stays a CONTIGUITY test, which is what still catches the thing worth catching: a splice of
    two distant sentences, or a compressed paraphrase, matches under none of these forms.
    """
    return _LOOSE_MARKS.sub("", _squeeze(_MD_LINK.sub(r"\1", value)).casefold())


def _squeeze(value: str) -> str:
    """The lenient comparison form: whitespace removed entirely, markdown escapes undone.

    Both are CONVERSION artifacts the judge silently repairs while quoting, measured on the
    round-2 calibration corpus: docling emits ``ranking\\_logic\\_hard`` for an underscored
    identifier, and PDF extraction loses word spaces (``Wefirst evaluate``). A judge quoting the
    readable form of either is quoting the document, and flagging it would spend the
    ``quote_unverified`` signal on converter noise — which is the one thing that would make the
    signal useless, since it is meant to surface fabrication.

    Dropping whitespace can only make matching MORE permissive, never less; over a span of tens
    of characters it does not make a fabricated sentence match.
    """
    return _MD_ESCAPE.sub(r"\1", _WS.sub("", value))


def _found_in(quote: str, norm_text: str) -> bool:
    """Is this quote a real span of the document, or an explicit elision of one?"""
    norm_quote = _normalize(quote)
    if not norm_quote:
        return False
    if norm_quote in norm_text or _squeeze(norm_quote) in _squeeze(norm_text):
        return True
    # Terminal punctuation is stripped from the QUOTE only: a judge closing a sentence it quoted
    # mid-sentence is not pointing anywhere else.
    if _loose(norm_quote).rstrip(_TERMINAL_PUNCT) in _loose(norm_text):
        return True
    segments = [s.strip() for s in norm_quote.split("...")]
    long_enough = [s for s in segments if len(s) >= MIN_ELISION_SEGMENT]
    if len(segments) <= 1 or len(long_enough) < 2:
        return False
    loose_text = _loose(norm_text)
    return all(_loose(s).rstrip(_TERMINAL_PUNCT) in loose_text for s in long_enough)
