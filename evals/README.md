# Eval sets

An eval set is a frozen sample of documents with reference labels, in `evals/<task>/<set_id>/`: `items.jsonl` (each
item's input exactly as production built it, with its digests), `manifest.json` (how and from what it was frozen,
`items_sha256`) and gold labels (`gold*.jsonl`, LLM-made by two labellers and an adjudicator unless the README says
otherwise). `stack eval <task> --set <set_id>` runs production's own code on the frozen items and scores the answers
(`stack eval --help`; `stack/evals/`); an input check refuses a set when production no longer builds the frozen input.
A frozen set changes only by a recorded amendment or re-freeze (`manifest.json` says so); a gold change is normally a
new set (a "gold revision"). Tests pin set paths, so a superseded set stays where it is. Each set's README says what it is, how it was frozen and labelled, and its result.

## Texts are not in the repository

Whole texts (`texts/<item_id>.txt.gz`) and the files holding long third-party excerpts (eight sets' `items.jsonl`,
labeller disagreement dumps, `triage/sr2025-recall/source/papers.csv`) left the tree (D790, D793, D795, D796).
`tools/fetch_eval_texts.py` fetches them from the project's archive (store credentials, or `--from-tar` with a local
copy of the archive) into a local cache (`$STACK_EVAL_TEXTS_DIR`, else `~/.cache/stack/eval-texts`), checked against
the sets' pins; readers look in the set, then in the cache. Without the cache the tests that need texts skip with an
`EvalTextsMissing` warning. The sets frozen from 2026-10-23 on (model_roles and technique_staged 2026-10-23a,
technique_staged 2026-10-28a, the judge sets) never had texts here: they include restricted texts, kept in a private
directory outside the repository.

## Redacted for redistribution

Email addresses in the eval files are replaced by `[email]`, the public dataset's marker (D808). Digests recorded at
freezing name the files as frozen: the judge set's `guides/affiliation.md` no longer matches its `guides_sha256`
(manifests), `task_files` (items) and `guide_sha256` (labels); `affiliation/manifest.json` `production_sha256` is
renewed.

## Sets

Status: **current** (in use as a measure or reference), **current (non-production stage)** (the set of an annotator
stage that is in the code but not in the production plan), **superseded** (kept as the record of a decision or for
the tests), **protocol** (a gate or confirmation run once under a protocol fixed in advance; closed). Where a README
keeps the measurements recorded at the time, they are under "Record (as written …)", below its current status.

| task | set | status | what |
|---|---|---|---|
| (all) | `judge-2026-10-25j` | current | Production's LLM judgements on one build, checked by three judges (Opus, Sonnet, Sol); the release quality table |
| triage | `2026-10-12a` | superseded by `judge-2026-10-25j/triage`, `triage_w` | 241 records, gold v1/v2; chose production's triage model and fetch rule (D201) |
| triage | `sr2025-recall` | current | 150 Shallow Review 2025 inclusions; recall of the SR scope (positives only) |
| triage | `2026-10-21-p4gate` | protocol (KEEP OFF) | Promotion gate of the technique-provenance clause (D657) |
| screen | `2026-10-12a` | superseded by `judge-2026-10-25j/screen` | 100 documents, gold v1/v2; chose `screen-v12` (D255, D260); `doc-class/` gold |
| screen | `2026-10-13-holdout` | superseded by `judge-2026-10-25j/screen` | 60 fresh documents; one check of the `screen-v12` pin |
| screen | `2026-10-13-t2pool` | superseded by `judge-2026-10-25j/screen` | 15 real tier-1 escalations; the tier-2 choice |
| screen | `2026-10-21-p4gate` | protocol (KEEP OFF) | Screen half of the technique-provenance gate |
| affiliation | `2026-10-12a` | superseded by `2026-10-12b-bylines`, `judge-2026-10-25j/affiliation` | 30 papers with clear bylines |
| affiliation | `2026-10-12b-bylines` | superseded by `judge-2026-10-25j/affiliation` | 15 hard bylines; moved `extract_main` to luna (D201) |
| model_roles | `2026-10-13a` | superseded by `2026-10-17a` | 25 documents; the labelling record; `stack eval model_mentions` |
| model_roles | `2026-10-16a` | superseded by `2026-10-17a` | 2026-10-13a re-frozen at the D591 merge |
| model_roles | `2026-10-17a` | current | 37 items with gold; the scored model_roles set |
| model_roles | `2026-10-23a` | current (inputs only) | Gold v3 sample (100 documents); `model-roles-v3b` chosen on it (D708) |
| technique_roles | `2026-10-13a` | current (non-production stage) | 25 documents; the technique_roles stage's own set |
| technique_staged | `2026-10-17a`, `-17b`, `-18a` | superseded by `2026-10-18c`, `2026-10-23a` | Gold v1 (36 documents) and its revisions |
| technique_staged | `2026-10-18b` | superseded by `2026-10-18c` | Gold v2, 120 documents |
| technique_staged | `2026-10-18c` | superseded by `2026-10-23a` | Gold v2 revised; held-out set of D708 |
| technique_staged | `2026-10-23a` | current (inputs only) | Gold v3 sample; adjudicated reference `adjudication/reference-v3x.jsonl`; production prompts chosen on it (D708) |
| technique_staged | `2026-10-28a` | protocol (closed, D735) | The judge set's technique documents frozen for the effort-high confirmation |

**Release quality.** Every dataset release's quality table comes from `judge-2026-10-25j`
(`configs/releases/dataset-*.yaml` `judge_eval`): `stack.evals.release_quality` re-scores the stored judge labels
against the release's own build, offline. That set is not run by `stack eval`; its scripts are in its `scripts/`.

## Other files

- `rubrics/`: the labelling rubrics the gold labellers of the 2026-10 triage, screen and affiliation sets followed
  (`triage.md` → `-v2` → `-v3`, `screen.md` → `-v2` → `-v3`, `affiliation.md`). The judge set uses the production
  prompts instead.
- `archived.sha256`: the sha256 and path (under `evals/`) of each file that left the tree for the archive (D793,
  D795, D796); `tools/fetch_eval_texts.py` checks every fetched file against it.
- `egress-overlay.yaml`: eval-only provider routes that `stack eval` applies to an in-memory copy of `configs/llm.yaml`
  (D173); production egress is unchanged.
- `recall/`: lists of documents the corpus should hold, made outside this project, for measuring collection recall
  (not `stack eval` sets).
