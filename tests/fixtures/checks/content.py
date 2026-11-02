"""V6 ``V-content``'s failing fixture (T3.1), hooked into ``FIXTURES`` at the end of
``tests/fixtures/checks/__init__.py``: Appendix B's example, ``convert_failed`` with a digest."""

from __future__ import annotations

from tests.fixtures.checks import World


def v6(w: World) -> None:  # `convert_failed` with a digest (doc 0 of the acquire part F3)
    w.doc("F3", 0)["content"]["current_text_status"] = "convert_failed"


FIXTURES_CONTENT = {"V-content": v6}
