"""``annotate.affiliation_orgs`` — the organisations of each document's affiliations, resolved without a model
(D736; plan ``batch-2026-10a`` revision 8; the dataset release ships these records as the kind ``affiliation_orgs``
and copies each affiliation's answer into ``affiliation``, :mod:`stack.fold.dataset` ``ORG_FIELDS``).

A ``chained`` annotator reading ``affiliation`` (registry ``reads``): per document whose ``affiliation`` is decided,
:func:`stack.orgvocab.affiliation_orgs` maps every affiliation surface of every byline author to organisation ids
of the pinned organisation vocabulary (``taxonomy/orgs-<version>.yaml``), with each author's other affiliations as
context (:meth:`stack.orgvocab.OrgMatcher.match_author`). No LLM: the answer is ``origin = 'computed'``, so a run
costs no spend and a re-run is a recompute.

**Plan pin** ``prompts.annotate.affiliation_orgs``: ``vocab`` (repository-relative path of the vocabulary file;
default :data:`stack.orgvocab.DEFAULT_PATH`) and an optional ``name`` (written as ``prompt_name``). Other keys are
refused. The vocabulary file's sha256 and the matcher's version (:data:`stack.orgvocab.MATCHER_VERSION`, as
:func:`matcher_digest`; D737) are the pin's template digests, so they are in the ``config_digest`` and every work
key: a vocabulary edit or a matcher change recomputes every document on the next build, and a changed
``affiliation`` result recomputes that document (the ``chained`` basis covers the read's status and payload).

**Payload** (``AFFILIATION_ORGS``, document grain): ``items`` in the affiliation payload's order, each
``{item_key, byline_index, affiliations: [{status, org_ids, ambiguous}]}`` in the item's affiliation order (join
by ``item_key`` and position; no surface is repeated), ``org_ids`` (the document's distinct ids, first-seen order)
and ``vocab_version`` (D737 dropped ``root_org_ids``: the vocabulary's ``parent`` gives them). Status ``ok`` when the affiliation read is
``ok`` and has at least one affiliation, else ``empty``.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

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
import hashlib

from stack.orgvocab import DEFAULT_PATH, MATCHER_VERSION, REPO_ROOT, affiliation_orgs, load
from stack.stage_runner import StageContext, StageError, StageResult

if TYPE_CHECKING:
    from stack.config import LlmConfig

WORKFLOW_ID = "annotate.affiliation_orgs"
PIN_KEYS = frozenset({"name", "vocab"})


def vocab_path(pin: Mapping[str, Any]) -> Path:
    """The vocabulary file plan pin ``pin`` names (unknown keys refused)."""
    extra = sorted(set(pin) - PIN_KEYS)
    if extra:
        raise StageError(f"{WORKFLOW_ID}: plan pin keys {extra} not in {sorted(PIN_KEYS)}")
    rel = pin.get("vocab")
    if rel is None:
        return DEFAULT_PATH
    if not isinstance(rel, str) or Path(rel).is_absolute():
        raise StageError(f"{WORKFLOW_ID}: plan pin vocab must be a repository-relative path, got {rel!r}")
    return REPO_ROOT / rel


def matcher_digest() -> str:
    """The second template digest: sha256 of ``orgvocab-matcher:<MATCHER_VERSION>`` (D737)."""
    return hashlib.sha256(f"orgvocab-matcher:{MATCHER_VERSION}".encode()).hexdigest()


def prompt(pin: Mapping[str, Any], llm: LlmConfig) -> PromptPin:
    """The pin: the vocabulary's sha256 and the matcher version's digest as the template digests; no model."""
    return PromptPin(name=pin.get("name"), template_digests=(load(vocab_path(pin)).digest, matcher_digest()),
                     models=())


def ask(item: AskItem, actx: AskContext) -> Answer:
    """One document's organisations (module docstring)."""
    vocab = load(vocab_path(actx.plan_pin))
    if vocab.digest not in actx.pin.template_digests:
        raise StageError(f"{WORKFLOW_ID}: {vocab.path.name} changed during the run (sha256 {vocab.digest} is not "
                         f"among the pinned {list(actx.pin.template_digests)})")
    ann = (item.row.get("annotations") or {}).get("affiliation") or {}
    source = ann.get("payload") if ann.get("status") == "ok" else None
    out = affiliation_orgs(source, vocab.matcher)
    payload = {**out, "vocab_version": vocab.version}
    has_affs = any(a for it in out["items"] for a in it["affiliations"])
    counts: dict[str, int] = {}
    for it in out["items"]:
        for a in it["affiliations"]:
            counts[f"affiliations.{a['status']}"] = counts.get(f"affiliations.{a['status']}", 0) + 1
    return Answer(payload, llm=None, status="ok" if has_affs else "empty", counts=counts)


WORKFLOW = Workflow("affiliation_orgs", ask=ask, prompt=prompt)


def run(ctx: StageContext) -> StageResult:
    return run_annotate(ctx, WORKFLOW)


__all__ = ["OPTIONS", "PIN_KEYS", "PURE_SUBSTAGES", "SUBSTAGES", "WORKFLOW", "ask", "prompt", "run", "vocab_path"]
