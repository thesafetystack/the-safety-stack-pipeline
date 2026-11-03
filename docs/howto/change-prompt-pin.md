# Change a prompt pin (re-run screen with a new prompt)

A plan's `prompts:` block pins, per LLM stage, the prompt template and model:

```yaml
prompts:
  triage.safety-relevance-v4: {name: safety-relevance-v4, model: openai/gpt-6-luna, role: classify_1k}
  annotate.screen: {name: screen-v12, model: openai/gpt-6-luna, adjudicate_model: openai/gpt-6-luna}
  annotate.affiliation: {name: affiliation-v1, role: extract_main}
```

`name` selects the template file next to the stage (`workflows/annotate/screen/screen-v12.md`); `model` or `role`
(a role of `configs/llm.yaml`) the model. Screen also takes `adjudicate_model` (tier 2), `tier2`, `tier1_chars`
and `tier2_chars` (what each tier reads: an int = the first N characters, `[head, tail]` = an excerpt, null = the
whole text), and `template_digest` (optional guard: a mismatch is refused).

Triage's title variant (D527) has no `prompts:` entry: under the triage option `title_basis: true` a question whose
template has a variant (`workflows/triage/ask.py` `SPECS`, the row's `title_variant`: `safety-relevance-v4` →
`safety-relevance-title-v2`) asks rows with no abstract and no excerpt with that file, at the question's model and
role. A new variant is a new file and a new `SPECS` row (plus `title_variant` on its question's row); it changes the asker name (the triage
`config_digest`), and title-basis evidence of another variant is not carried, so those rows are asked again.

## What a pin change does

An annotate stage's `config_digest` hashes the plan pin, the template's digest and the models (D128). Evidence
carries only under the same config digest; **under a new pin every row in scope is asked again**, and the response
cache does not help (the rendered prompt differs).

- Rows the run does not reach (its `max_rows`, its budget) are `not_attempted[limit]` or
  `not_attempted[budget]` in the new `screen` file. They do **not** keep their old verdict: until later passes reach
  them, the build has fewer screened rows than the one before.
- Under `batch-2026-10a` (its `require_complete` includes `screen`) such a build ends `incomplete`. It is still `latest`
  (the newest `ok` or `incomplete` build) for `stack diff latest` and `stack verify`'s reference build (D703), but
  `stack release` refuses its corpus without `--allow-incomplete`; `--passes N` continues it.
  Under `dev-v2-m4` (`require_complete` without `screen`) it ends `ok`, with the gap.
- Older builds' evidence shows as an X6 `warn` in `stack verify` (judged against the pin its run recorded).
- Readers that need one prompt version pin it by `config_digest` (`docs/CONSUME.md`, "Annotation records").
- A pin change that does not change the answers (the same request bytes, or a change not meant to be retroactive)
  need not re-ask: give the line a `target` and an `accepted:` list (D758; `docs/OPERATE.md` "Keeping answers across a
  config change"); the old answers stay, labelled with the old config.
- Never edit a template in place. For annotate stages an in-place edit changes the template digest, so the config
  digest, so every row is re-asked under the old name; for triage questions it changes only the asker's name and
  re-decides rows without asking (D157). Copy to a new name instead (`screen-v13.md`).
- Templates `screen-v8` to `screen-v11` (and `screen.md`, `screen-sr-v1`, `screen-stack-v1`) pull menus by `{{ref:enums}}`, which is outside the config digest: an
  `configs/enums.yaml` edit changes what they send without a new digest. `screen-v12` has its menus
  written in, so a menu change is a new template name.

## Steps

1. Copy the template to a new name and edit it: `workflows/annotate/screen/screen-v13.md`. Register the name with one
   row in the stage's `SPECS` table (screen: `workflows/annotate/screen/main.py`, schema, question mode, tier-1
   `max_tokens`, records, limits, confidence, inline menus, record lines; triage: `workflows/triage/ask.py`, record
   version and title variant). A file without a row is refused before any call.
2. Compare on the frozen eval sets (real calls, through the ledger), not on the store:

   ```bash
   STACK_ENV_FILE=.env uv run --no-sync stack --plan configs/plans/batch-2026-10a.yaml eval screen --set 2026-10-12a \
       --models openai/gpt-6-luna --tier2-model openai/gpt-6-luna --prompt screen-v13 --split dev --estimate  # cost only
   STACK_ENV_FILE=.env uv run --no-sync stack --plan configs/plans/batch-2026-10a.yaml eval screen --set 2026-10-12a \
       --models openai/gpt-6-luna --tier2-model openai/gpt-6-luna --prompt screen-v13 --split dev --max-usd 1
   uv run --no-sync stack eval compare <run dir A> <run dir B>       # paired accuracy, McNemar; no calls
   ```

   `--tier2-model` repeats the pin's `adjudicate_model`: `stack eval screen` does not read it from the plan and
   otherwise escalates to the `screen_adjudicate` role of `configs/llm.yaml` (D260). Run directories are under
   `v4/evals-store/` by default (gitignored; `--out`). The `2026-10-13-holdout` split was run once with
   `screen-v12` and is kept for the next pinned prompt only (D260); do not iterate on it.
3. Count what the new pin will ask: the rows of the newest build's `files.corpus_input` that the screen line's
   filter keeps (its path is `files.corpus_input.path` in the build record, read as in `docs/CONSUME.md`'s entry
   query; then a duckdb `count(*)`). `build --dry-run` does not estimate rows
   or cost; `stack eval screen --estimate` gives a per-1,000 cost on the eval set.
4. Change the pin in the plan (`annotate.screen: {name: screen-v13, …}`); give the line a budget that covers the
   count, or plan on `--passes`.
5. `stack build <plan> --dry-run`, then `stack build <plan> [--passes N]`. Screen's tier 2 is bounded per run by
   the line's `options` (`screen_usd_per_1k`, `tier2_max_usd`, `tier2_budget_share`, `tier2_max_share`; D256): a
   borderline whose tier-2 call does not fit is `not_attempted[budget]` (build reason `tier2_allowance`, D706) and
   asked again next run; `screen_usd_per_1k` admits a run's tier-2 call while nothing of tier 2 is spent or in
   flight, so a lone deferred borderline is not refused forever (D706).
6. Record the change and its eval in `docs/DECISIONS.md`. The run's record is its run record in the store and, for a
   scheduled cycle, the cycle's summary on the ops prefix (`docs/DEPLOY-railway.md`).
