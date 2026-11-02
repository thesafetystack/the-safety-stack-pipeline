"""The failing fixture of X16 ``lookup_gate_current`` (T4.2), hooked into ``FIXTURES`` at the end
of ``tests/fixtures/checks/__init__.py``.

The clean store's build names ``coverage.arxiv``'s run-6 file; its rows carry ``gate_digest``
NULL (nothing excluded). The build record has no ``gate_digest``, so X16 takes the gate digest of
the plan it names (``dev-v2``).
"""

from __future__ import annotations

from collections.abc import Callable

from tests.fixtures.checks import World
from tests.synth.rows import dg


def x16(w: World) -> None:  # an exclusion made under an older gate (Appendix B's example)
    w.rows("coverage")[0]["gate_digest"] = dg("the gate before a new consumer")


#: Merged into ``tests.fixtures.checks.FIXTURES``.
FIXTURES_LOOKUP: dict[str, Callable[[World], None]] = {"lookup_gate_current": x16}
