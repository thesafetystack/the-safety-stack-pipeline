"""``annotate.<wf>``: the workflow-independent half of an annotation stage (§3.1, A.1, A.4, A.5).

A stage reads the build's ``fold.corpus`` file, asks a workflow per **work key**
(``work_digest = H([source_id, basis_digest, config_digest, item_key])``, D19) and writes
``<wf>_evidence`` (one row per invocation, cumulative over the prior evidence) and ``<wf>`` (the
selected result per ``(doc_id, item_key)``, full, a chain of one, D14). Modules:

* :mod:`stack.annotate.scope` — the rows in scope (plan filter, masked/retired guard,
  ``options.max_rows``);
* :mod:`stack.annotate.basis` — ``basis_kind`` → current basis digest, supplying member, text or
  fields; the work key; ``is_basis_current`` (D99's function);
* :mod:`stack.annotate.evidence` — evidence rows from a workflow's answer (EVI from the
  :class:`~stack.llm.LlmResult`), attempts, cap 3, backoff, ``result_digest``;
* :mod:`stack.annotate.select` — the pure selection (evidence by work key, then ``<wf>.yaml``
  overrides), which X8 reruns;
* :mod:`stack.annotate.stage` — the driver :func:`run_annotate` and the plug-in types
  (:class:`Workflow`, :class:`AskItem`, :class:`Answer`, :class:`AskContext`,
  :class:`PromptPin`).

Checks V7, V8, X6, X7, X8 are :mod:`stack.checks.annotate`.
"""

from __future__ import annotations

from stack.annotate.stage import (
    OPTIONS,
    PURE_SUBSTAGES,
    SUBSTAGES,
    Answer,
    AskContext,
    AskItem,
    PromptPin,
    Workflow,
    config_digest,
    resolve_pin,
    run_annotate,
)

__all__ = ["OPTIONS", "PURE_SUBSTAGES", "SUBSTAGES", "Answer", "AskContext", "AskItem", "PromptPin", "Workflow",
           "config_digest", "resolve_pin", "run_annotate"]
