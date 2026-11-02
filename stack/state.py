"""Host-wide state directory outside every store and every checkout.

The cost ledgers that lived here (D44, R6-2) are retired (T-STORE): a run's spend is in its own
record (``RunRecord.spend``), and no run writes here. What remains is :func:`state_dir`, the
default location of operator tools' caches (``tools/resolve_openalex_authors.py``):
``$STACK_STATE_DIR``, else ``$XDG_STATE_HOME/stack``, else ``~/.local/state/stack``.
"""

from __future__ import annotations

import os
from pathlib import Path

STATE_DIR_ENV = "STACK_STATE_DIR"


def state_dir() -> Path:
    """``$STACK_STATE_DIR``, else ``$XDG_STATE_HOME/stack``, else ``~/.local/state/stack``
    (not created here)."""
    env = os.environ.get(STATE_DIR_ENV)
    if env:
        return Path(env).expanduser()
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg).expanduser() / "stack"
    return Path.home() / ".local" / "state" / "stack"


__all__ = ["STATE_DIR_ENV", "state_dir"]
