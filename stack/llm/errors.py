"""The LLM client's error taxonomy, in its own module so ``stack.llm.ledger`` can raise
:class:`BudgetExceeded` without importing the client (which imports the ledger).

Every class carries the ``(status, error_class)`` pair (§2.6 vocabulary, ``stack.schemas.vocab``)
a stage writes for the item that raised it. The stage-side contract for the two budget refusals
is :class:`stack.llm.BudgetScope`: the refused item and every later item of the stage's scope
are ``not_attempted[budget]`` and are never sent.
"""

from __future__ import annotations

from collections.abc import Sequence


class LlmError(Exception):
    """Base class for every error the client raises."""

    status: str = "failed_permanent"
    error_class: str = "code_bug"


class TransportError(LlmError):
    """A network-level transport failure (connection refused, timeout) — distinct from an HTTP
    error status, which is returned as an ``LlmResponse`` for the client to classify."""

    status = "failed_transient"
    error_class = "network"


class MissingRef(LlmError):
    """A ``{{ref:<kind>}}`` placeholder in the template had no entry in ``refs``."""

    error_class = "config_missing"


class EgressRefused(LlmError):
    """The egress gate refused before any bytes left: every attempted model (incl. fallbacks)
    is gated out for this ``license_class``."""

    error_class = "license_gated"


class ContextTooLarge(LlmError):
    """HTTP 413, or a 400 naming the context window: the request cannot fit this model."""

    error_class = "too_large"


class BudgetExceeded(LlmError):
    """A reservation would take the run's committed + reserved cost above its cap (the stage's
    ``budget.max_usd`` or a per-run default); refused before the request (the transport is never called)."""

    status = "not_attempted"
    error_class = "budget"


class FatalLlmError(LlmError):
    """A fatal, resumable transport error: HTTP 401/402/403 (auth/credit) or ≥25 consecutive
    transport failures across the client's calls (then ``error_class`` is the tripping failure's:
    ``network``, ``http_5xx`` or ``rate_limit``; ``network`` for an unexpected status). The caller
    stops the stage; a rerun resumes."""

    status = "failed_transient"
    error_class = "provider_auth"


class ModelRetired(FatalLlmError):
    """The call names a model whose ``llm.yaml`` price entry is ``retired`` (a plan pin, or a
    caller's ``model=``; roles cannot name one, :class:`stack.config.LlmConfig`). Refused before the
    cache and before any reservation, nothing sent; fatal like an auth failure, so the stage stops
    instead of failing every item the same way. Permanent for this pin: the fix is a new pin."""

    status = "failed_permanent"
    error_class = "config_missing"


class RouteInvalid(FatalLlmError):
    """A model reference the client cannot route or its provider cannot take (D722): a qualifier naming no
    ``llm.providers`` entry, or a role setting the provider's dialect refuses (``temperature`` or effort ``minimal``
    on the Anthropic API). Raised before the cache and any reservation, nothing sent; fatal like
    :class:`ModelRetired`, since every item would fail the same way. Permanent for this pin."""

    status = "failed_permanent"
    error_class = "config_missing"


class ProviderRefused(LlmError):
    """The provider declined to answer (D722): an Anthropic reply with ``stop_reason: "refusal"`` (its safety
    classifiers), on the last model the call may try. The attempt is paid and settled; the document is
    ``failed_permanent[query_invalid]`` (the provider refuses the request), not schema-retried."""

    error_class = "query_invalid"


class AttemptsExhausted(LlmError):
    """One call's transport-level failures (network error, HTTP 5xx, 429, an unexpected status)
    reached the client's per-call cap (``max_call_failures``) without tripping the run-wide
    consecutive-failure breaker: this item fails, the run goes on. ``(status, error_class)`` is
    the last failure's: ``failed_transient`` with ``network`` / ``http_5xx`` / ``rate_limit``, or
    ``failed_permanent[query_invalid]`` when the provider kept refusing the request with an
    unexpected 4xx."""

    def __init__(self, message: str, *, error_class: str, status: str = "failed_transient") -> None:
        super().__init__(message)
        self.error_class = error_class
        self.status = status


class SchemaValidationError(LlmError):
    """The model failed to produce schema-valid JSON across every schema attempt. The last
    validator errors are on ``.errors``."""

    error_class = "schema_invalid"

    def __init__(self, message: str, errors: Sequence[str]) -> None:
        super().__init__(message)
        self.errors = list(errors)


class TruncatedOutput(LlmError):
    """The model's reply hit the output cap (``finish_reason = "length"``) twice: on the call's
    own ``max_tokens`` and on the one retry at double that cap. Permanent for this config: the
    item's evidence is ``failed_permanent[truncated]``, which the prior carry keeps until the work
    key changes (a higher cap in the workflow changes its template digest, hence the config
    digest). ``.cost_usd`` carries every attempt's spend, as for any :class:`LlmError`."""

    error_class = "truncated"


class Deferred(Exception):
    """Batch mode, collect pass (:mod:`stack.llm.batch`): the client recorded the request this call
    would have sent (or the call waits for the replay pass) and stops the item's ask here. Not an
    :class:`LlmError`, so a workflow's ``except LlmError`` lets it through to the batch driver."""


class BatchUnanswered(Exception):
    """Batch mode: this run asked the provider in a batch and had no answer when the passes
    ended (time cap, a failed/expired/lost batch, a submit error). The stage marks the item
    ``not_attempted[unanswered]``; the next run asks again. Not an :class:`LlmError`."""
