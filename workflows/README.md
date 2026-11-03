# workflows/ — independent pipeline units, phase-first

Layout: `workflows/<phase>/<name>/` — one directory per stage, where `<phase>/<name>` maps to
the stage id `<phase>.<name>` (e.g. `annotate.screen` → `workflows/annotate/screen/main.py`; a
bare `<name>` → `workflows/<name>/main.py`). A stage is its module plus its line in a plan file
(`configs/plans/*.yaml`; the line's keys are `stage, prior, in, out, budget, repeat, filter, reads,
keys, miss_cap, require_complete, options`; `bump_on` is refused when not empty, D437, see `configs/plans/README.md`; prompt and model pins are the plan's
`prompts:` block). There is no stage registry (v3's `configs/workflows.yaml` was retired, D42).

**Dispatch convention** (D73): each stage's `main.py` exposes `run(ctx) -> StageResult`
(optional `SUBSTAGES` / `PURE_SUBSTAGES`; optional `OPTIONS`, the `options` keys its code reads: any other key of
its line is refused before the run and at a build's start, D434; optional `preflight(plan, line, store_root) ->
list[str]`, which `stack build` asks before any line runs and refuses `failed[environment]` on any message: no
network, no row reads; acquire's checks its convert tools), loaded *by path* by `stack.stage_runner`. `fold.*`
dispatches to `stack/fold/stage.py`. `STACK_WORKFLOWS_ROOT` points the loader at another tree
(the tests' stand-ins in `tests/synth/stages/`).

Prompt templates are co-located with their stage. For triage (`workflows/triage/<name>.md`), `annotate.screen`
(`workflows/annotate/screen/<name>.md`; `screen-v7` is `screen.md`) and `collect.links`
(`workflows/collect/links/<name>.md`) the plan's `prompts:` pin names the template a run sends, and a changed prompt
gets a new file name (`docs/howto/change-prompt-pin.md`). `annotate.affiliation` and `annotate.translation` read a
fixed file (their pin's `name` is a label); `annotate.model_roles`, `annotate.technique_roles` and
`annotate.technique_staged` select their files by the pin's `name` (below). An edit to a file a pin sends changes the
pin's template digests, so every document in scope is asked again.

## Writing a stage

The contract is in three module docstrings; read the one that fits:

| Kind | Contract | Task page |
|---|---|---|
| any stage | `stack/stage_runner.py` ("Stage protocol"): `StageContext` gives `run_id`, `stage`, `plan`, `plan_stage` (the stage line), `store_root`, `inputs`/`prior` (pinned chains), `view(key)`, `filter`, `only`, `counts`, `config_digest`, `blobs`, `emit_part`/`emit_full`, `heartbeat()`, `substage(name)`; `run` returns `StageResult(scope_complete, counts, rows_touched)`. A planned stop (a cap) is `scope_complete=False` with status `ok`; any exception fails the run and leaves `data/` untouched. | `docs/OPERATE.md` |
| collector `collect.<suffix>` | `stack/collect/base.py`: the `Collector` harness (options, pacing, windows, `observe`, in-band, `finish`) | `docs/howto/add-collector.md`, `docs/howto/add-feed.md` |
| annotator `annotate.<wf>` | `stack/annotate/stage.py`: `Workflow(name, ask, prompt)`, `run_annotate`; `stack/schemas/payloads/registry.py`: the registry entry | `docs/howto/add-annotator.md` |

A stage never writes into `data/` itself: it emits tables and the runner publishes them at the end of a successful
run. Test a stage on the synthetic world (`tests/synth/world.py`) or with the fakes in `tests/fakes/` (LLM,
HTTP, lanes, S3); no test touches the network.

## What is here

- `collect/<name>/`: one collector per provider or site family (`arxiv_sweep`, `openalex`, `semantic_scholar`,
  `forums`, `af_graphql`, `lw_graphql`, `blogs`, `cards`, `feeds`, `manual`, `links`, `v3_import`) and the
  lookups (`openalex-lookup`, `s2-lookup`, `card-dates-lookup`, `x-lookup`).
- `gather/`, `triage/`, `acquire/` (scope, fetch lanes, convert, quality, point, carry, history, in-band bodies;
  the archive step was removed, D329).
- `annotate/screen/` (relevance screen, two tiers; its records `stack_relevance`, `sr_relevance`, `text_usability`,
  `doc_class`), `annotate/affiliation/` (byline affiliations, item grain), `annotate/model_roles/` (the LLM-free
  matcher over `configs/models.yaml`, then one judge call per document with a strong mention; the former
  `annotate/model_mentions/` merged in, D591), `annotate/technique_roles/` (one judge call per document against the
  technique taxonomy `taxonomy/taxonomy-v0.8.yaml`). The last two are ports of v3 branches (round 5, T-ANN;
  D266-D268); each module docstring and `NOTES.md` say what was not ported. model_roles runs in production
  (`batch-2026-10a`, D710), technique_roles only in `configs/plans/ann-scratch.yaml`; the production technique
  annotator is `annotate/technique_staged/` (staged or single-call extraction against the technique catalogue; its
  `NOTES.md`). `annotate/translation/` (English versions of non-English titles and abstracts, D755) and
  `annotate/affiliation_orgs/` (affiliations matched to an organisation vocabulary, no model, D736). `doc_class` is a
  record of `screen-v12` (D257), no longer a stand-alone annotator.
- `overrides/add/`: the stage `overrides.add` (`stack overrides add <file>`; one human-edited file into the
  `overrides` series, D338).
- `fetch/metadata/`: not a stage; the shared code of `collect.openalex-lookup` and `collect.s2-lookup`.
- `echo_test/`: a test stage.

Rules: workflows import `stack.*` and **never each other** (one exception: `collect.openalex-lookup` and
`collect.s2-lookup` import their shared code from `workflows/fetch/metadata/main.py`, D92) —
cross-workflow data flows only through the store; thin per-service clients are duplicated first,
graduated deliberately (no `workflows/common/`). Some `stack/` modules import a stage module by name
(`stack/gate.py` the triage asker, `stack/migrate.py` gather helpers, `stack/cli/acquire.py` acquire's
`filtered_ids`, `stack/evals/` the triage, screen and collect modules); that works because the editable install puts
the repo root on the path.

## Prompts in the tree

Plans (`configs/plans/README.md`): `batch-2026-10a` is the production plan, `std-v3` and `std-v2` the production
plans before it, `dev-v2-m4` the dev store's plan, `ann-scratch` and `links-scratch` scratch plans, `dev-v2` the test worlds' plan.
Any template can be run on a frozen eval set with `stack eval <task> --prompt <name>`; `stack eval audit-prompts`
renders the five of `AUDIT_PROMPTS` (`stack/evals/harness.py`).

| Template (under `workflows/`) | Pin name | Pinned by | Otherwise |
|---|---|---|---|
| `triage/safety-relevance-v4.md` (title variant `safety-relevance-title-v2.md`, D656) | `safety-relevance-v4` | `batch-2026-10a` (with `title_basis`, D527) | |
| `triage/safety-relevance-v4-p4.md` (title variant `safety-relevance-title-v2-p4.md`) | `safety-relevance-v4-p4` | none | eval only (D657) |
| `triage/safety-relevance-v3.md` (title variant `safety-relevance-title-v1.md`) | `safety-relevance-v3` | `std-v3`, `std-v2`, `dev-v2-m4`, both scratch plans | |
| `triage/safety-relevance-v2.md` | `safety-relevance-v2` | none | eval only (`AUDIT_PROMPTS`, `stack eval combine`) |
| `triage/safety-relevance-v1.md` | `safety-relevance-v1` | `dev-v2` (its sql-only gate asks no question) | `AUDIT_PROMPTS`; tests |
| `annotate/screen/screen-v13.md`, `screen-v13-p4.md` | same | none | staged for the screen's next change (D656, D663); `-p4` D657 |
| `annotate/screen/screen-v12.md` | `screen-v12` | `batch-2026-10a`, `std-v3`, `std-v2`, `dev-v2-m4` | |
| `annotate/screen/screen-v11.md` | `screen-v11` | `ann-scratch`, `links-scratch` | |
| `annotate/screen/screen-v10.md` | `screen-v10` | none | eval only |
| `annotate/screen/screen-v9.md` | `screen-v9` | none | eval only (Shallow Review scope fields) |
| `annotate/screen/screen-v8.md` | `screen-v8` | none | eval only (`AUDIT_PROMPTS`) |
| `annotate/screen/screen.md` | `screen-v7` (also no name) | `dev-v2` | `AUDIT_PROMPTS`; tests |
| `annotate/screen/screen-stack-v1.md`, `screen-sr-v1.md` | same | none | eval only (one question each; `stack eval pair-screens`) |
| `collect/links/link-select-v1.md` | `link-select-v1` | `batch-2026-10a`, `links-scratch` | |
| `annotate/affiliation/extract.md` | `affiliation-v1` (label) | `batch-2026-10a`, `std-v3`, `std-v2`, `dev-v2-m4`, `ann-scratch`, `dev-v2` | `AUDIT_PROMPTS` |
| `annotate/model_roles/prompts/model-roles-v3b.md` (+ `rubric-v0.6.0.md`) | `model-roles-v3b` (selects the file) | `batch-2026-10a` | `v3a`, `v4a`, `v4f`: eval only (`prompts/README.md`) |
| `annotate/model_roles/prompts/model-roles-v2.md` (+ `rubric-v0.6.0.md`; `model-roles-v1.md` kept) | `model-roles-v2` (selects the file, D591) | `ann-scratch` | `stack eval model_roles` |
| `annotate/technique_staged/prompts/technique-staged-{single,glean}-v5-a1.1.md` (+ `rubric-v0.9-c.md`) | `technique-staged-v5-a1.1` (a prompt set: `PROMPT_SETS` in `main.py`) | `batch-2026-10a` (D708) | the other sets: eval only (`prompts/README.md`) |
| `annotate/translation/translate-short-v1.md` | `translate-short-v1` (label) | `batch-2026-10a` (D755) | |
| `annotate/technique_roles/prompts/technique-roles-v2.md` (+ `rubric-v0.4.0.md`) | `technique-roles-v2` (also no name; selects the file, D591) | `ann-scratch` | `stack eval technique_roles --prompt technique-roles-v2` |
| `annotate/technique_roles/prompts/technique-roles-v1.md` (+ `rubric-v0.3.1.md`) | `technique-roles-v1` | none | eval only (A/B; `stack eval technique_roles` default) |

`annotate.model_roles`' and `annotate.technique_roles`' prompt files (`prompts/README.md` in each;
`stack/annotate/prompt_files.py`) hold the system part and the user part in one file; the config digest also covers
model_roles' vocabulary `configs/models.yaml` and technique_roles' taxonomy `taxonomy/taxonomy-v0.8.yaml`, whose menu
fills the system part (D591). `annotate.affiliation_orgs` has no template: its pin (`orgs-0.2.2` in `batch-2026-10a`,
D778) names the organisation vocabulary the matcher reads.
