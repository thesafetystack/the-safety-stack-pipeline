"""One LLM call through the client, the cost ledger and the response cache, then the identical
call again (PLAN T4.1 done criterion): the second must be a cache hit at $0 with no ledger line.

    uv run --no-sync python tools/llm_smoke.py --fake --store <tmp>
    uv run --no-sync python tools/llm_smoke.py [model]            # REAL call: costs money

The real mode reads the key from ``$OPENROUTER_API_KEY`` or, preferred, from the env file
``$STACK_ENV_FILE`` names (``STACK_ENV_FILE=../repo/.env uv run --no-sync python
tools/llm_smoke.py``: a path on the command line, never a value; D160 (C)) and never prints it; ``max_tokens`` is 16 and the prompt one line, so a call costs well under a cent
(the run's budget is ``--max-usd``, default $0.05, held by the run's in-memory ledger).
``--fake`` uses the fake model from ``tests/fakes/llm.py`` (no network).
Exit 0 when the second call was a cache hit at $0, else 1.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from stack.config import RoleConfig
from stack.config import load as load_config
from stack.llm import (
    Ledger,
    LlmClient,
    ResponseCache,
    Transport,
    make_default_transport,
    provider_key,
)

PROMPT = 'Reply with exactly the JSON object {"ok": true} and nothing else.'
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["ok"],
    "properties": {"ok": {"type": "boolean"}},
}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("model", nargs="?", default="openai/gpt-6-luna")
    ap.add_argument("--fake", action="store_true", help="fake transport, no network")
    ap.add_argument("--store", type=Path, default=REPO / "v4" / "store-dev",
                    help="store whose cache/llm-responses/ holds the cache (default v4/store-dev)")
    ap.add_argument("--max-usd", type=float, default=0.05, help="this run's budget")
    args = ap.parse_args(argv)

    config = load_config()
    llm = config.llm.model_copy(update={
        "roles": {**config.llm.roles, "smoke": RoleConfig(model=args.model, max_tokens=16)}})
    transport: Transport
    if args.fake:
        from tests.fakes.llm import FakeLlm, ok

        transport = FakeLlm(default=ok({"ok": True}, cost=0.00004, prompt_tokens=30,
                                       completion_tokens=5))
    else:
        if not provider_key(llm.provider.api_key_env):
            print(f"${llm.provider.api_key_env} is not set and not in $STACK_ENV_FILE "
                  "(pass the env file by path: STACK_ENV_FILE=../repo/.env)", file=sys.stderr)
            return 2
        transport = make_default_transport(llm)

    ledger = Ledger()
    run_id = "smoke" + dt.datetime.now(dt.UTC).strftime("%Y%m%dt%H%M%S")
    client = LlmClient(config=llm, transport=transport, ledger=ledger, run_id=run_id,
                       stage="tools.llm_smoke", cache=ResponseCache.for_store(args.store),
                       budget_usd=args.max_usd)
    print(f"mode={'fake' if args.fake else 'REAL'} model={args.model} run_id={run_id}")
    print(f"cache={client.cache.dir if client.cache else None}")

    results = []
    for i in (1, 2):
        r = client.complete(role="smoke", template=PROMPT, refs={}, user="smoke test",
                            schema=SCHEMA, license_class="open", prompt_name="llm-smoke-v1")
        results.append(r)
        print(f"call {i}: is_cache_hit={r.is_cache_hit} cost_usd=${r.cost_usd:.6f} "
              f"tokens_in={r.tokens_in} tokens_out={r.tokens_out} latency_ms={r.latency_ms} "
              f"attempts={len(r.attempts)} parsed={dict(r.parsed)}")
        print(f"        ledger total=${ledger.total():.6f} lines={len(ledger.lines())}")
    first, second = results
    good = second.is_cache_hit and second.cost_usd == 0.0
    print(("OK" if good else "FAIL") + ": second identical call "
          + ("was a cache hit at $0" if good else "was NOT a free cache hit")
          + (" (the first was itself a hit: an earlier smoke cached it)" if first.is_cache_hit else ""))
    return 0 if good else 1


if __name__ == "__main__":
    raise SystemExit(main())
