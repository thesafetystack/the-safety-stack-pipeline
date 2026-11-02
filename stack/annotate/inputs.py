"""The inputs of an ``annotate.<wf>`` run besides ``corpus`` (D235): annotations joined into the
corpus rows before anything else reads them.

The joined set is :func:`stack.schemas.payloads.registry.joined_reads`: the workflow's registry
``reads`` (its *basis* reads: a ``chained`` basis covers their results) then the stage line's
``reads:`` (scope reads: visible to the run's filter, not in the basis). Each is passed as
``--in <this build's file>`` (:func:`stack.build.stage_inputs`) and joined with
:func:`stack.fold.join`, mapping M included, so ``AskItem.row["annotations"][<read>]``, the scope
filter (``ann_verdict(annotations, 'stack_relevance') = 'relevant'``), ``select`` and the ``chained``
basis all see the reads exactly as ``fold.build`` would fold them. The checks (X7, X8) rebuild the
same joined rows from the ``<wf>`` file's header ``inputs`` with :func:`join_reads`
(:func:`header_reads` names them).
"""

from __future__ import annotations

from collections.abc import Mapping

import pyarrow as pa

from stack.fold import join
from stack.schemas.families import CHAIN_WORKFLOWS
from stack.schemas.payloads.registry import REGISTRY, joined_reads


def join_reads(corpus: pa.Table, reads: Mapping[str, pa.Table]) -> pa.Table:
    """``corpus`` with each read's ``<name>`` table joined into ``annotations`` (in the given
    order) and mapping M applied (:func:`stack.fold.join`); no reads = ``corpus`` as is."""
    if not reads:
        return corpus
    out, _ = join(corpus, dict(reads))
    return out


def header_reads(inputs: Mapping[str, str]) -> dict[str, str]:
    """The joined reads among a ``<wf>`` file's header ``inputs``: every input other than
    ``corpus`` that names a registered annotation (``name → file id``, in the given order)."""
    return {k: v for k, v in inputs.items()
            if k != "corpus" and k in REGISTRY and k not in CHAIN_WORKFLOWS}


__all__ = ["header_reads", "join_reads", "joined_reads"]
