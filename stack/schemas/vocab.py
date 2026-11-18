"""Closed vocabularies of the store (design §2.6, D9, D12, D13), as plain tuples and frozensets.

One module so the write-time S-checks (``stack.checks.schema``) and the later V/X checks read the
same words. Deliberately independent of v3's ``stack.types.enums`` / ``configs/enums.yaml``,
which are tied to the database era.
"""

from __future__ import annotations

# Status of a role, a snapshot's conversion, an ANN entry, a <wf>/evidence row, a run output
# (frozen, §2.6). `ok` = a decided result with a payload; `empty` = examined, nothing found.
STATUS: tuple[str, ...] = (
    "ok",
    "empty",
    "partial",
    "failed_transient",
    "failed_permanent",
    "quarantined",
    "skipped",
    "not_attempted",
)

#: Statuses whose `error_class` must be NULL (S4).
STATUS_NO_ERROR: frozenset[str] = frozenset({"ok", "empty"})

#: error_class sets per status (§2.6; `budget` only under not_attempted, D12; permanent = v4c's
#: twelve + `too_short`; transient `forbidden` = an HTTP 403 before the fetch cap, D150). A status
#: listed here requires a non-NULL error_class from its set (S4).
ERROR_CLASSES: dict[str, frozenset[str]] = {
    "failed_transient": frozenset(
        {"rate_limit", "timeout", "network", "http_5xx", "provider_auth", "crashed", "forbidden"}
    ),
    "failed_permanent": frozenset(
        {
            "dead",
            "blocked",
            "paywalled",
            "license_gated",
            "too_large",
            "no_fetchable_url",
            "parse_error",
            "corrupted",
            "schema_invalid",
            "query_invalid",
            "provider_plan",  # D583: the provider refuses what the account's plan lacks (OpenAlex "Plan upgrade required")
            "truncated",  # round 5, T-ANN: the reply hit the output cap twice (stack.llm TruncatedOutput)
            "config_missing",
            "code_bug",
            "too_short",
            # round 3, Q: a text the quality gate fails (stack/conversion/quality.py FAIL_CLASSES)
            "quality_garbled",
            "quality_stub",
            "quality_too_short",
            "quality_references_only",
            "quality_abstract_only",
            "quality_markup",
        }
    ),
    "quarantined": frozenset({"cap_reached", "operator"}),
    "skipped": frozenset(
        {
            "review_required",
            "stale_basis",
            "no_text",
            "masked",
            "not_relevant",
            "undecided",
            "lapsed_scope",
            "no_basis",
            "withdrawn",
            "gated",
            "gate_failed",
            "not_needed",
            "not_requested",
        }
    ),
    "not_attempted": frozenset(
        {
            "halted",
            "budget",
            "limit",
            # batch mode (T-BATCH, D-entry pending): the run asked in a batch and had no answer when it
            # ended (time cap, failed/expired/lost batch, submit error); the next run asks again
            "unanswered",
            # a drain (D570): the run stopped admitting work, or abandoned the call in flight at the
            # end of its bounded wait; the next run asks again
            "stopped",
        }
    ),
}

#: Every listed error_class (a `partial` row may carry any of these, or none).
ALL_ERROR_CLASSES: frozenset[str] = frozenset().union(*ERROR_CLASSES.values())

DOCUMENT_STATES: tuple[str, ...] = ("identified", "triaged", "acquired", "retired")
SOURCE_STATES: tuple[str, ...] = ("observed", "missing", "retracted")

#: content_role in pointer rank order where ranked (§2.6); `xml`, `html`, `archive` are never text by
#: default. `html` (round 5, T-RUN B8): arXiv's LaTeXML rendering of the paper, fetched by the arXiv lane.
CONTENT_ROLES: tuple[str, ...] = (
    "full_text",
    "xml",
    "html",
    "archive",
    "post",
    "page",
    "readme",
    "abstract",
    "fields",
)
POINTER_RANK: tuple[str, ...] = ("full_text", "post", "page", "readme", "abstract", "fields")
#: In-band roles that are not content (D522): ``provider_record`` = the source's record of the item as
#: received (a JSON API's item as canonical JSON, arXiv's Atom ``<entry>``), a raw blob only. Never a
#: wanted role, never a snapshot, never text; collectors write it, ``acquire`` skips it.
RECORD_ROLES: tuple[str, ...] = ("provider_record",)
#: Every role an ``inband.entries[]`` item may carry.
INBAND_ROLES: tuple[str, ...] = CONTENT_ROLES + RECORD_ROLES
NEVER_TEXT_ROLES: frozenset[str] = frozenset({"xml", "html", "archive", *RECORD_ROLES})
#: Structured representations of a paper (round 5, T-RUN B8): JATS (`xml`, OA lane) and arXiv's LaTeXML
#: HTML (`html`). Never text unless the acquire line's `options.structured` enables them; an enabled one
#: whose text converted ok stands in for its source's `full_text` (`workflows/acquire/point.py`).
STRUCTURED_ROLES: tuple[str, ...] = ("xml", "html")

#: ``snapshots[].quality.bucket`` and ``content.current_text_quality`` (round 3, Q).
TEXT_QUALITY_BUCKETS: tuple[str, ...] = ("ok", "flagged", "failed")
#: ``chained`` (D235): the current text plus the typed results of the registry ``reads``.
BASIS_KINDS: tuple[str, ...] = ("fields", "text", "chained")
ANN_ORIGINS: tuple[str, ...] = ("computed", "human", "ai_check", "import", "rule")
SOURCE_ORIGINS: tuple[str, ...] = ("provider", "human", "import")
VERSION_KINDS: tuple[str, ...] = ("full", "part")
FOLD_CONFIGS: tuple[str, ...] = ("corpus", "build", "release", "compact")
LICENSE_CLASSES: tuple[str, ...] = (
    "open",
    "public_web",
    "restricted",
    "api_tos",
    "api_abstract",
    "shadow",
)
#: How strict each licence class is, loosest first (D807): the acquire pointer keeps a same-text supplier
#: unless the new one is strictly looser, and an annotation answer is reused for a new supplier of the same
#: text only when that supplier is no stricter than the one it was asked for. Tied ranks have the same LLM
#: egress (``configs/llm.yaml``; ``tests/test_config.py`` checks that a looser rank never permits
#: fewer providers). A missing class reads as ``restricted``, as the annotate ask and the release read it.
LICENSE_STRICTNESS: dict[str, int] = {"open": 0, "public_web": 1, "api_abstract": 2, "restricted": 3,
                                      "api_tos": 3, "shadow": 4}


def license_rank(license_class: str | None) -> int:
    """:data:`LICENSE_STRICTNESS` of a class (NULL: ``restricted``); an unknown class is the strictest."""
    return LICENSE_STRICTNESS.get(license_class or "restricted", max(LICENSE_STRICTNESS.values()))


MISS_MEANS: tuple[str, ...] = ("withdrawn", "unknown")
MASK_REASONS: tuple[str, ...] = ("operator", "review_required")
TRIAGE_DECISIONS: tuple[str, ...] = ("fetch", "skip", "pending")
RUN_STATUS: tuple[str, ...] = ("running", "ok", "failed", "cancelled")
GRAINS: tuple[str, ...] = ("document", "item")
