# tools/

Scripts run by hand (or by the production container), not imported by the pipeline. Each module docstring is the
full usage; run them with `uv run --no-sync python tools/<name>.py --help` from the repository root.

| Tool | What it does |
|---|---|
| **Checks** | |
| `check_all.py` | The gate: config checks, mypy on one file, the full suite (`uv run --no-sync tools/check_all.py --no-sync`). There is no CI. |
| `check_configs.py` | Validates `configs/*.yaml` against `spec/schemas/config/`, the screen payload schemas and their examples, and cross-checks. |
| `devtest.py` | The dev test tier: the tests for the paths you touched, plus the smoke set (`tests/smoke.txt`, `tests/modules.toml`). |
| `fetch_eval_texts.py` | Fills the local eval-text cache with the frozen eval sets' texts, which are not in the repository (D790; needs the maintainers' archive or `--from-tar`). |
| `gen_models_schema.py` | Regenerates `spec/schemas/config/models.schema.json` from the model vocabulary's code. |
| **Production** | |
| `scheduled_run.py` | The weekly production cycle and its operator commands (`cycle`, `upload`, `update-website`, `pause`/`resume`, `report`, `unlock`, `watchdog`; `docs/DEPLOY-railway.md`). |
| `build_website_summary.py` | Reviewed image copy of the website repo's `tools/build_summary.py`, run by `scheduled_run.py` to generate `summary.json` and `summary.js` (D810). |
| `memlog.py` | The cycle's memory log: the container's memory and its largest processes, every few minutes (D783). |
| `s3_smoke.py` | Put, get, list and delete under a test prefix of an S3 store, to check its credentials and endpoint. |
| **Probes and one-offs** | |
| `probe_collect.py` | One collect query against the live provider, nothing written. |
| `probe_filters.py` | How many items each collector filter would let through. |
| `llm_smoke.py` | One LLM call through the client, then the same call again, which must be a cache hit at no cost. |
| `list_configs.py` | The config digests a stage recorded in a store, for a plan line's `target` and `accepted:` (D758; `docs/OPERATE.md`). |
| `org_vocab.py` | Applies the organisation vocabulary to a release corpus and reports its coverage. |
| `recall_concordia.py` | Recall of the corpus against the Concordia AI Chinese Technical AI Safety Database. |
| `resolve_openalex_authors.py` | OpenAlex author ids for the researcher roster (`configs/rosters/`), by a written rule; metered. |
