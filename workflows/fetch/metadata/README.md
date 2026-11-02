# Scholarly lookups (`collect.openalex-lookup`, `collect.s2-lookup`)

Status as of T2.3b (2026-10-12); both lookups also ran in the round-4 passes (D226) and are in the plans. `main.py` here is the shared code of the two lookup
stages (`workflows/collect/{openalex,s2}-lookup/main.py`, D92); its module docstring is the
contract (due keys from the `documents` head, one `sources` row per looked-up key, one `coverage`
row per batch, pacing and halts).

```bash
uv run --no-sync stack --store v4/store-dev --plan configs/plans/dev-v2-m4.yaml \
    run collect.openalex-lookup --in data/sources.openalex-lookup/<series>/ \
    --in data/coverage.openalex-lookup/<series>/ --in data/documents/<series>/ --part
```

- **Implemented:** OpenAlex filter batches (doi, pmid, openalex; arXiv ids by their DataCite DOI)
  and the S2 paper batch, on the collector harness; the miss/hit rows; `miss_cap`; self-skip;
  S2's contradiction rule; 413/414 halving. Tests: `tests/test_collect_lookup.py`, the pure cases
  of `tests/test_fetch_metadata.py`.
- **S2 scope and 429s (D520, D521):** `collect.s2-lookup` asks only for documents without a usable
  abstract (`metadata.abstract` under 100 chars; counted `docs_with_abstract`); the stage option
  `only_missing_abstract: false` restores the full scope. A 429 within 60 s is waited out (when S2
  names no `Retry-After`, the Gate's backoff: 5 s doubling to 60 s, reset by a 200; D550) and
  re-sent up to 6 times (`http_429_waits`); a longer `Retry-After` halts the run; a seventh 429
  fails the batch and ends the run with its keys still due. S2 is paced at the `semantic_scholar`
  channel's `politeness.rate_rps` (0.4: one request per 2.5 s, D550).
- **Real runs:** first on 2026-10-12 11:23 UTC on `v4/store-dev` (the 2026-10 run log, M2.T2.8:
  openalex-lookup 152 rows; s2-lookup 50 rows, capped at 50 of 304 due keys).
- **Retired at T4.5:** v3's database-bound multi-provider enrichment and abstract driver
  (`stack/lookup/metadata_pipeline.py` `run_v5`; `abstract_workflow.py` `seed`, `prepare`,
  `run_abstract`, `_seed_records`). **Deleted in the dead-code pass of 2026-10-14:** the pure remainder, `stack/lookup/{metadata_enrichment,metadata_scheduler,metadata_io,
  abstract_workflow,abstract_recovery}.py`. The lookups'
  pacing (OpenAlex 0.15 s, S2 1.05 s) became `main.py`'s `OPENALEX_DELAY_S`/`S2_DELAY_S` (S2's is the channel's since D550). In the
  same pass `main.py` lost its 64 test-only v3 names (the Crossref / Europe PMC / arXiv Atom / OAI /
  Zenodo fetchers on v3's `http_request` seam, abstract waterfall, venue and withdrawal parsing,
  circuit breaker). A v4 abstract stage is not decided (T2.3b slot 2).

v3's `fetch.metadata` documentation (stages `fetch.metadata` / `fetch.history`, `tools/benchmark_metadata.py`,
the `STACK_HISTORY_*` knobs) was removed from this file on 2026-10-13.
Environment knobs (`.env.example`): `STACK_METADATA_LIMIT` (the lookups' per-run cap, `main.py`) and
`STACK_METADATA_MAILTO` (v3's contact name, read before `STACK_MAILTO` by `stack/provider_transport.py`'s pacing gate
and response journal; leave it unset). The lookups write no response journal: `STACK_METADATA_SPOOL` is the journal
directory of a caller without a store, and only `python -m stack.evals.sr` uses one (it sets the variable itself).
