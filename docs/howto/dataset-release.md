# Make the dataset release (D719)

```
uv sync --frozen --group convert-pdf
stack export --store <root> --in data/corpus.build/<series>/ --config dataset-public   --out <dir>/public
stack export --store <root> --in data/corpus.build/<series>/ --config dataset-internal --out <dir>/internal [--blobs <dir of <sha256> files>]
```

Both read only local files: the corpus of an `ok` build (`--allow-incomplete` for an incomplete one), the build record,
the run records of the build's annotate runs, the build's `<wf>` and `<producer>_evidence` files, every part of the
overrides chain the build's runs read (one head), the repo's taxonomies, the judge eval set's scores, and for
`dataset-internal` the text blobs (the store's `blobs/`, or `--blobs`). The output directory must be absent or empty and outside the store;
nothing is recorded or pushed. The release id (and format id) is the export's UTC date unless `--release-id` names
one. About 100 s and 4.5 GB peak RSS for 39k rows (the internal variant adds ~30 s for 2.4 GB
of text). Every check in `mapping.yaml` runs over the written stage; any problem keeps nothing and names the problem.

The bundle (D725; D736 added the organisation vocabulary), the same 12 files in both variants: `README.md` (the card, with the quality table), `LICENSE`,
`FORMAT.md`, `VOCABULARY.md`, `schema.json` (the row's JSON Schema; each kind's record and payload inline),
`release.json` (every other file with size and sha256), `data/corpus.parquet`, `config/configs.json`,
`config/overrides.jsonl` (the active overrides, each with its run id), `taxonomies/{techniques,models}-<version>.yaml` (the files the records' pins name: 1.1.0 since D738) and
`taxonomies/orgs-0.2.2.yaml` (the organisation vocabulary the `annotate.affiliation_orgs` records pinned; the kind
`affiliation_orgs` ships those records, and `mapping.yaml` `org_fields` copies each affiliation's `org_ids` and
`org_status` and `org_candidates` from them into `affiliation`, NULL where a document has no record; `release.json` `orgs` counts the
affiliations by status).
The export refuses a bundle that is not exactly the mapping's `files:`.

Where things are: `stack/fold/dataset.py` (module docstring: inputs, row, variants, bundle), `configs/releases/
dataset-{public,internal}.yaml`, `configs/releases/dataset/` (`mapping.yaml`: the schema and its descriptions;
`overrides-public.yaml`: public author/method/note of the overrides; `vocab.yaml`, rendered as VOCABULARY.md; the card and FORMAT templates, LICENSE).
Tests: `tests/test_dataset_release.py`.

The card's quality table is computed at export time from the judge eval set the config names (`judge_eval`, a repo
directory such as `evals/judge-2026-10-25j`) and the release's own build (D728, `stack/evals/release_quality.py`): the
judges' stored labels (`<set>/labels-<judge>.jsonl`, model_roles' normalised `judge-normalized.jsonl`) are scored
against this build's production labels, read from the build's corpus, the `documents` file its fold.corpus run read
and that series' `triage_evidence` head (screen and triage samples include documents outside the release file). A
sampled document no longer in the build, outside the set's population there, or whose input changed since the judges
read it is not scored and is counted; triage_w's strata, N_h and weights are this build's. The table: production's
precision, recall and F1 with 95 % intervals and n for triage fetch (weighted), screen keep, affiliations and models
against the 3-judge majority and technique presence against Opus and against Sol; the relevant documents triage drops
(Wilson per stratum); the scored coverage; two caveats; the eval directory for the labels and the full tables of the
judged build. A missing eval file, key or interval refuses the export; `release.json` `quality.judge_eval` has the
table, the coverage per set, the store tables read and every eval file read with its sha256. `release.json`
`quality.triage_vs_screen` has triage's fetch against the screen's keep on every corpus document with a current
triage verdict (decision or D724 signal): P / R / F1 and the confusion table by kind, evidence basis and confidence
(agreement of two model stages, not accuracy); the cycle summary (`tools/scheduled_run.py`) repeats its headline.
`python -m stack.evals.release_quality rescore|triage-vs-screen --store ROOT --build ID` runs either on a local store.

## Changing the format

- A field: edit `mapping.yaml` (an `internal` path, or a `transform` named after its public path and implemented in
  `TRANSFORMS`); a new enum value needs a sentence in `vocab.yaml` (the build refuses otherwise).
- A new annotation kind: a registry entry, a `fields:` entry `annotations.<kind>` with `keep`/`docs`, and a `meta.kind`
  enum value; until it has one, a non-empty `annotations.other[]` is refused.
- A new overrides author label: an `authors:` entry in `overrides-public.yaml` (refused otherwise). A note the scrub
  cannot clean: `note_overrides`.
- A wrong stored title or doc_type (D768, which replaced D759's export-time corrections): a `fields` override in the
  store (`docs/howto/overrides.md`; the 2026-10-30 batch is `configs/overrides/data_quality_2026_10.yaml`). It changes
  the document's `fields_digest`: the document is triaged again (since D724 one triage call per document even where
  a gate rule or its fetched text decides) and re-pointed by acquire; no other annotation reads the bibliographic
  fields into its basis. The release ships the overridden value (`field_sources` origin `human`) and the override in
  `config/overrides.jsonl` (kind `metadata_fix`).
- A new shipped file: a line in `mapping.yaml` `files:` (a file without one, or a line without its file, is refused).
  There are no flat views (D723) and no configs table (D725); `data/corpus.parquet` is the card's only config, and
  FORMAT's recipes build flat tables.
- A config key of an earlier format (`views`, `prompts`, `examples`) is refused with what replaced it
  (`OBSOLETE_CONFIG_KEYS`); any other unknown key is refused too.
- A new judge eval set: point `judge_eval` in both configs at it; it needs the layout of `evals/judge-2026-10-25j`
  (items, released labels, sampling.json, model_roles' `judge-normalized.jsonl`); a renamed judgement needs the key
  tables (`SCREEN_KEEP`, `SET_UNITS`) updated.
- The one line on the internal variant in the public docs: `internal_variant_note` in the config (null drops it).

## Implemented, and what is not

- Implemented and run on builds `w0vt3g81xtaqj` and `ws1qpn8myzxah`: both variants, every file of the bundle, the
  checks.
- Stand-in overrides (whole payloads supplied by `ai:<model>` overrides, D717): implemented and unit-tested; the 21
  stand-ins of the current chain are in `config/overrides.jsonl` once a build has applied them (they need an annotate
  re-run and fold). A corpus folded before D717 with such a record is refused.
- Active overrides (D725): the effective rule of `stack.overrides` over the applied chain (revokes applied, one winner
  per group; revoke rows are not exported), minus a `<wf>` row the document's build record does not use; unit-tested
  on a synthetic chain. On build `ws1qpn8myzxah`'s chain nothing was superseded, revoked or stale (all 269 rows ship).
- Release-time re-scoring (D728): run on `ws1qpn8myzxah` (reproduces the committed scores; the public export there is
  byte-identical to the 2026-10-27 candidate but for README.md and release.json, whose quality table is unchanged) and
  on `w0vt3g81xtaqj`; drops, stratum re-assignment and the frame counts are unit-tested on synthetic builds
  (`tests/test_release_quality.py`). Not recomputed at release: the screen and affiliation window splits and the
  disagreement lists (they need the texts or describe the judged build). Triage vs screen: on `ws1qpn8myzxah` only
  decisions exist (8,546 corpus documents), so its recall is 1.000 by construction; signals appear once a cycle has
  run under D724.
- The title and doc_type fixes (D768) are `fields` overrides: the file is unit-tested (it loads through the add's
  reader, each value follows its rule against the stored value its note records, gather applies an entry); not yet
  added to a store or run through a build and an export.
- Not implemented: `annotations.other[]` payloads (refused while `json_kinds` is empty); a two-release stacking
  fixture; an end-to-end test on a synthetic store (the projection is unit-tested on synthetic rows; the whole export
  runs only on a real build); an upload by `stack export` (the scheduled cycle uploads both variants to Hugging Face:
  `tools/scheduled_run.py` `upload`, `docs/DEPLOY-railway.md`; nothing uploads to Zenodo or GitHub).
- `config/configs.json` names `defined_in` as a path without a commit: run records carry a code digest, not a git
  commit; `release.json` names the release code's commit. The prompt files are not shipped (D725); their digests are
  in each config's `components`, and the export no longer checks that the repo's prompt files reproduce them.
