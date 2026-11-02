"""The real ``overrides.add`` under the fake workflows root (``STACK_WORKFLOWS_ROOT``), so the
annotate tests add overrides as ``stack overrides add`` does."""

from workflows.overrides.add.main import run

__all__ = ["run"]
