"""Shared collect-phase machinery (10-design §5.1).

Helpers that more than one collect workflow needs. Workflows may import only ``stack.*`` and the
stdlib (the cardinal rule in each workflow's docstring), so anything two of them share has to
live here rather than in one of them.
"""
