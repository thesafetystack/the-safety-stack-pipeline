"""Stack pipeline v4 — the repo's only shared module.

Design: ``docs/DESIGN.md`` (code layout §12); v3's module contract is v3's ``spec/30-stack-module.md``.
Workflows import ``stack.*`` and not each other (one exception, D92); cross-workflow data flows only
through the file store (``stack.store``), published by the stage runner.
"""
