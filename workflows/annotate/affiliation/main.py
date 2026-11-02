"""``annotate.affiliation`` — byline affiliations per (document, author) on the annotate framework
(§3.1, A.4 ``AFFILIATION``); the lab-attribution map.

One invocation per document of the build's ``fold.corpus`` file, on its current text
(``basis_kind = 'text'``, ``content.current_text_sha256``): ONE call of the plan pin's role over
the front-matter window → roster alignment → org resolution → anchoring of every text-tier
surface. The framework (:mod:`stack.annotate`) does scope, prior-evidence carry, attempts, budget,
the ``affiliation`` / ``affiliation_evidence`` files and ``select``; this module is the
per-document ``ask`` and the prompt pin. ADAPT of v3's ``main.py`` (9312c8c ``run``; the DB
loop, receipts and ``Services`` went into the framework); every extraction step stays in the
pure sibling ``extract.py`` (COPY, unchanged), so ``tests/test_affiliation.py`` and the eval
harness run it without a store.

**Plan pin** ``prompts.annotate.affiliation`` (part of the config digest, so of the work key):
``name`` (the ``prompt_name`` written), ``role`` (``llm.yaml`` role, default
:data:`DEFAULT_ROLE`), ``model`` (replaces the role's primary; default the role's model; the
role's ``max_tokens`` and fallbacks stay) and the framework's optional ``template_digest``.
Other keys are refused (a typo must not pass silently). The template is ``extract.md`` (one
template; ``name`` does not select it).

**Inputs of one ask** (v3 → v4):

* the **roster** (the authoritative author list) is the row's ``metadata.authors[].name``
  (A.2 ``AUTHOR``) where v3 read ``effective_meta.extra["authors"]``, through the same
  :func:`_roster` normalisation; none → the extractor's discovery mode;
* the **OpenAlex tier** reads ``metadata.authors[].affiliation_raw``: ``collect.openalex``
  writes there the authorship's institution display names joined with ``"; "`` (else its raw
  affiliation strings), the data v3 kept as ``authors_detail[].institutions``; ``collect.openalex-lookup``
  writes its institution names the same way, and a ``fields`` override of ``authors`` may set it;
* the **window** is ``extract.header_window`` over the current text (v1's 8k head + 2k tail);
* the **XML tier** (v3 D-RAW1): when the row has a ``content.roles[]`` entry ``xml`` with
  status ``ok``, the ``raw`` blob of its snapshot is parsed with ``extract.parse_xml_byline``;
  a byline found there IS the answer and no model is called (``origin = 'computed'``, no
  LLM columns). A missing raw blob degrades to the text tier (v3's ``read.raw`` → None),
  counted ``xml_raw_missing``. The raw XML is not part of the basis: a row that gains an
  ``xml`` role keeps its text-tier answer under the prior carry until the text changes.

**Payload** (``AFFILIATION``, item grain, one item per author ``extract`` returns: with a roster
one per roster name in roster order, else per author the model or the XML read):
``item_key`` = ``str(byline_index)``; ``byline_index`` = the author's position in that list;
``author_name`` = v3's ``author`` (the H-S12 normalised name key, ``extract.norm_author_name``);
``author_surface`` = v3's ``author_surface`` (the roster spelling, else as read);
``affiliations`` = ``{surface, org_slugs}`` per affiliation left after anchoring; ``orcid`` (XML
tier only, as v3); ``resolution`` = ``text`` | ``xml`` | ``openalex`` | ``email_domain`` |
``unknown``. Dropped (no ``AFFILIATION`` field): ``marker``, ``email``, the grounding envelope
(``grounding_idx``, spans, scores), the voided quotes, the per-document flags (``xml_byline``,
``vision_candidate``, ``ambiguous_assignment``, ``roster_mismatch``, ``fuzzy_name_matches``,
``discovery_failed``, the LLM provenance flags). The flags survive as run counts
(:data:`COUNTS`), not per row.

**Status**: ``ok`` when there is at least one item, ``empty`` when there is none (no roster and
the model, or the XML, found no author). With a roster every name yields an item, so an answer
in which no author aligned is ``ok`` with every item ``resolution = 'unknown'`` (v3 wrote these
rows and an ``ok`` receipt; the per-author ``unknown`` is the record), counted ``roster_mismatch``.

Failures are the framework's: a budget refusal → ``not_attempted[budget]``; a fatal client
error halts the run; any other :class:`~stack.llm.LlmError` of the one call is that document's
evidence row and the run goes on. Imports ``stack.*`` only, never another workflow; no
dataclass here (written when the stage loader ran this file outside ``sys.modules``; registered
since D435).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, NamedTuple, cast

from stack.anchor import Anchorer
from stack.annotate import (
    OPTIONS,
    PURE_SUBSTAGES,
    SUBSTAGES,
    Answer,
    AskContext,
    AskItem,
    PromptPin,
    Workflow,
    run_annotate,
)
from stack.canon import Canon
from stack.core.hashes import template_hash
from stack.llm import LicenseClass, LlmResult
from stack.stage_runner import StageContext, StageError, StageResult, load_path_module
from stack.store.blobs import BlobNotFound, open_blobs

if TYPE_CHECKING:
    from stack.config import LlmConfig

HERE = Path(__file__).resolve().parent


def _load_sibling(name: str) -> Any:
    """Import a sibling module by PATH. ``workflows/`` is not a package (the runner loads
    ``main.py`` the same way), so a plain ``import`` would not resolve."""
    path = HERE / f"{name}.py"
    module = load_path_module(f"_affiliation__{name}", path)  # registered in sys.modules (L1-8)
    if module is None:  # pragma: no cover - packaging accident
        raise RuntimeError(f"annotate.affiliation: cannot load sibling module {path}")
    return module


ex = _load_sibling("extract")

WORKFLOW_ID = "annotate.affiliation"
#: ``llm.yaml`` role when the plan pin names none (v3's registration said ``extract_cheap``,
#: reserved and unused in llm.yaml 1.8.x; the M4 plans pin ``extract_main``).
DEFAULT_ROLE = "extract_main"
PIN_KEYS = frozenset({"name", "role", "model", "template_digest"})
TEMPLATE = (HERE / "extract.md").read_text("utf-8")
SCHEMA: dict[str, Any] = ex.RESPONSE_SCHEMA
#: Run counts an ask may add (``Answer.counts``): the per-document flags v3 kept on its rows.
COUNTS = ("xml_tier", "xml_raw_missing", "surfaces_voided", "roster_mismatch", "fuzzy_name_matches",
          "discovery_failed", "vision_candidate", "ambiguous_assignment")


# ── the plan pin ─────────────────────────────────────────────────────────────────────────────


class Pin(NamedTuple):
    """What a plan pin resolves to: the ``llm.yaml`` role and the model replacing its primary.
    (No dataclass here: written when the stage loader ran this file outside ``sys.modules``.)"""

    role: str
    model: str


def pin_of(pin: Mapping[str, Any], llm: LlmConfig) -> Pin:
    """The role and model of plan pin ``pin`` (module docstring); unknown keys or role refuse."""
    extra = sorted(set(pin) - PIN_KEYS)
    if extra:
        raise StageError(f"{WORKFLOW_ID}: plan pin keys {extra} not in {sorted(PIN_KEYS)}")
    role = pin.get("role") or DEFAULT_ROLE
    if not isinstance(role, str) or role not in llm.roles:
        raise StageError(f"{WORKFLOW_ID}: plan pin role {role!r} is not an llm.yaml role")
    model = pin.get("model") or llm.roles[role].model
    if not isinstance(model, str):
        raise StageError(f"{WORKFLOW_ID}: plan pin model must be a string, got {model!r}")
    return Pin(role=role, model=model)


def prompt(pin: Mapping[str, Any], llm: LlmConfig) -> PromptPin:
    """The prompt pin (no I/O): ``extract.md``'s template digest for the pinned model and each
    of the role's fallbacks, as :func:`stack.llm.render_call` computes ``template_digest``."""
    p = pin_of(pin, llm)
    role = llm.roles[p.role]
    pairs = []
    for m in dict.fromkeys([p.model, *role.fallbacks]):
        params: dict[str, Any] = {"model": m, "max_tokens": role.max_tokens}
        if role.temperature is not None:
            params["temperature"] = role.temperature
        if role.reasoning_effort is not None:
            params["reasoning_effort"] = role.reasoning_effort
        pairs.append((template_hash(TEMPLATE, SCHEMA, params), m))
    return PromptPin(name=pin.get("name"), template_digests=tuple(dict.fromkeys(d for d, _ in pairs)),
                     models=tuple(m for _, m in pairs))


# ── the inputs of one document ───────────────────────────────────────────────────────────────


def roster_of(row: Mapping[str, Any]) -> list[str]:
    """The roster of a ``corpus`` row: ``metadata.authors[].name`` through :func:`_roster`."""
    authors = row["metadata"].get("authors") or []
    return _roster(SimpleNamespace(extra={"authors": [a.get("name") for a in authors if a]}))


def openalex_of(row: Mapping[str, Any]) -> dict[str, list[str]]:
    """``{author name -> [institution names]}`` from ``metadata.authors[].affiliation_raw``
    (``collect.openalex`` joins them with ``"; "``) — the OpenAlex tier, used ONLY for an author
    the text left unresolved, and not copied onto the payload beyond what ``resolve`` keeps."""
    out: dict[str, list[str]] = {}
    for a in row["metadata"].get("authors") or []:
        name = str((a or {}).get("name") or "").strip()
        raw = (a or {}).get("affiliation_raw")
        if name and isinstance(raw, str):
            named = [s.strip() for s in raw.split("; ") if s.strip()]
            if named:
                out[name] = named
    return out


def xml_snapshot(row: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The snapshot of the row's ``xml`` role with status ``ok`` (newest ``captured_at`` when
    several), or None."""
    content = row.get("content") or {}
    ok_ids = {r["snapshot_id"] for r in content.get("roles") or []
              if r["content_role"] == "xml" and r["status"] == "ok" and r.get("snapshot_id")}
    snaps = [s for s in content.get("snapshots") or [] if s["snapshot_id"] in ok_ids]
    return max(snaps, key=lambda s: (str(s.get("captured_at") or ""), s["snapshot_id"]), default=None)


def _raw_xml(row: Mapping[str, Any], store_root: Any) -> tuple[bytes | None, bool]:
    """``(raw XML bytes or None, blob missing)`` for the XML tier."""
    snap = xml_snapshot(row)
    if snap is None:
        return None, False
    try:
        return open_blobs(Path(store_root)).get(snap["raw"]["sha256"]), False
    except BlobNotFound:
        return None, True


# ── ask ──────────────────────────────────────────────────────────────────────────────────────


def ask(item: AskItem, actx: AskContext) -> Answer:
    """Extract one document's byline affiliations (module docstring)."""
    p = pin_of(actx.plan_pin, actx.config.llm)
    text = item.text or ""
    roster = roster_of(item.row)
    raw_xml, xml_missing = _raw_xml(item.row, actx.store_root)
    calls: list[LlmResult] = []

    def llm_call(authors_block: str, window: str) -> Mapping[str, Any] | None:
        res = actx.client.complete(
            role=p.role, model=p.model, template=TEMPLATE, refs={},
            user=_user_message(authors_block, window), schema=SCHEMA,
            license_class=cast(LicenseClass, item.license_class), prompt_name=actx.pin.name,
            basis_digest=item.basis_digest)
        calls.append(res)
        return cast("Mapping[str, Any] | None", res.parsed)

    payloads, flags = ex.extract(text=text, roster=roster, canon=Canon.from_config(actx.config), llm_call=llm_call,
                                 openalex=openalex_of(item.row), raw_xml=raw_xml)
    anchorer = Anchorer.from_config(actx.config, workflow=WORKFLOW_ID)
    anchored, voided_n, quoted_n = [], 0, 0
    for payload in payloads:
        payload, envelope, voided = ex.anchor_payload(payload, anchorer, text, item.basis_digest)
        quoted_n += len(envelope) + len(voided)
        voided_n += len(voided)
        anchored.append(payload)

    xml_tier = bool(flags.get("xml_byline"))
    counts = {
        "xml_tier": int(xml_tier),
        "xml_raw_missing": int(xml_missing),
        "surfaces_voided": voided_n,
        "roster_mismatch": int("roster_mismatch" in flags),
        "fuzzy_name_matches": int(flags.get("fuzzy_name_matches") or 0),
        "discovery_failed": int(bool(flags.get("discovery_failed"))),
        # Diagnoses of a mangled converted text, which the XML tier never read (v3).
        "vision_candidate": int(not xml_tier and ex.needs_vision_fallback(payloads, voided=voided_n,
                                                                          quoted=quoted_n)),
        "ambiguous_assignment": int(not xml_tier and ex.ambiguous_assignment(payloads)),
    }
    items = [_item(pl) for pl in anchored]
    return Answer({"items": items}, llm=calls[-1] if calls else None, status="ok" if items else "empty",
                  counts={k: v for k, v in counts.items() if v})


def _item(payload: Mapping[str, Any]) -> dict[str, Any]:
    """One ``AFFILIATION`` item from v3's per-author payload (module docstring)."""
    index = int(payload["byline_index"])
    return {
        "item_key": str(index),
        "author_name": payload["author"],
        "author_surface": payload["author_surface"],
        "byline_index": index,
        "affiliations": [{"surface": a["surface"], "org_slugs": list(a.get("org_slugs") or [])}
                         for a in payload.get("affiliations") or []],
        "orcid": payload.get("orcid") or None,
        "resolution": payload["resolution"],
    }


# ── v3 helpers the eval harness and freezer call by name (stack/evals/{harness,freeze}.py) ───


def _user_message(authors_block: str, window: str) -> str:
    """The per-document fill for the template's two fenced blocks. Kept out of ``refs`` so the
    rendered PREFIX is identical across every document — that is what makes the client's
    response cache and the provider's prefix cache work on a 20k-document run."""
    return f"```authors\n{authors_block}\n```\n\n```document\n{window}\n```"


def _roster(meta: Any) -> list[str]:
    """The authoritative author list from merged metadata (``meta.extra["authors"]``, names), or
    [] to put the extractor in discovery mode. :func:`roster_of` feeds it a v4 row's names."""
    authors = (meta.extra or {}).get("authors")
    if not isinstance(authors, list):
        return []
    return [str(a) for a in authors if isinstance(a, (str, int, float)) and str(a).strip()]


WORKFLOW = Workflow("affiliation", ask=ask, prompt=prompt)


def run(ctx: StageContext) -> StageResult:
    return run_annotate(ctx, WORKFLOW)


__all__ = ["OPTIONS", "COUNTS", "DEFAULT_ROLE", "PIN_KEYS", "PURE_SUBSTAGES", "SCHEMA", "SUBSTAGES", "TEMPLATE",
           "WORKFLOW", "Pin", "ask", "openalex_of", "pin_of", "prompt", "roster_of", "run", "xml_snapshot"]
