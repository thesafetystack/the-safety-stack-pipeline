# Retrying failed OpenAlex searches

To run only the searches whose coverage row from a particular finished run is a failure
(`failed_transient`, including a budget halt's `rate_limit`, or `failed_permanent`):

```bash
STACK_OPENALEX_RETRY_FAILED_RUN=<run_id> uv run --no-sync stack run collect.openalex --plan <plan> \
    --in data/sources.openalex/<series>/ --in data/coverage.openalex/<series>/ --part
```

(or the stage option `retry_failed_run: <run_id>` in the plan line). This creates a new
attempt using the current query configuration and ordinary watermarks. It does not
repeat complete or capped (`partial`) searches from that run, and does not include
queries that were never attempted (a halted run's unreached queries have no row; a normal
run picks them up). Sources are matched on the `sources.openalex` directory given as `--in`, so
no `source_id` is re-minted. A failed query starts pagination again, not at its failed
page. The per-query result cap still applies.

Explicit failed-only retries ignore `fraction` / `max_queries` (`STACK_COLLECT_FRACTION`,
`STACK_COLLECT_MAX_QUERIES`) so all selected failures are attempted. The run is read from
the current rows of `coverage.openalex`: a run id with no current row there (unknown,
unfinished — rows are written at a run's end — or every query it ran has been re-run
since, which replaced its rows) or a failed query no longer configured fails closed. A run
with no failed queries makes no OpenAlex calls.

Rate-limit/budget handling still applies; this option does not bypass API limits. The
stage options and the coverage-query format are in the module docstring of `main.py`.
