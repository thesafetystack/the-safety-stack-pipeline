"""The record lines a triage or screen ask carries after the title (triage record version 2,
``workflows/triage/ask.py``; the screen's v12-like names, ``workflows/annotate/screen/main.py``):
one renderer, so the two stages cannot drift apart (stages do not import each other)."""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from typing import Any


def record_lines(row: Mapping[str, Any]) -> list[str]:
    """``Published: <year>`` of ``metadata.published_at`` and ``Source: <channels>`` of
    ``identity.channels`` (comma-separated, in stored order); a NULL date or no channels leaves its
    line out. A date that is not a date (a string, say) raises: the store types it, so that is a
    code bug."""
    out: list[str] = []
    pub = (row.get("metadata") or {}).get("published_at")
    if pub is not None:
        if not isinstance(pub, dt.date):  # a datetime is a date
            raise TypeError(f"record lines: metadata.published_at {pub!r} is not a date")
        out.append(f"Published: {pub.year}")
    channels = [str(c) for c in (row.get("identity") or {}).get("channels") or [] if c]
    if channels:
        out.append("Source: " + ", ".join(channels))
    return out


__all__ = ["record_lines"]
