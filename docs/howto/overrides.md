# Overrides: add, revoke, what wins

Human corrections (and, later, generated ones) live in the store as the `overrides` family: one chain, `overrides`,
parquet files under `data/` like any other family (D336-D338; code `stack/overrides.py`). Nothing is
edited in place: every change is a new row added by one recorded run of the stage `overrides.add`.

## Add

Write a file and add it:

```
uv run --no-sync stack --store <store> overrides add my-fixes.yaml --out data/overrides/<series>/ [--comment fix-masks] [--dry-run]
```

- The command validates the file against the chain first; a refusal prints `error: …` and nothing runs. It also
  refuses an `identity` or `fields` entry naming an id the series does not know (a `source_id` no
  `sources.*/<series>/` chain has; a `doc_id` neither a `documents/<series>/` head document nor a `former_doc_ids`
  entry: a source not gathered yet has no document to name), since gather would then refuse every run until a
  revoke (D440). `stack run overrides.add` does not run this check. `--dry-run` stops after the check and prints
  the rows. Otherwise one `overrides.add` run writes one file: a `full` file for
  the first add on a store, a `part` after that.
- **YAML** keeps the entry format of the old `<store>/overrides/*.yaml` files. Either a list whose target is the
  file's stem (`identity.yaml`, `fields.yaml`, `manual_sources.yaml`, `<wf>.yaml` such as `screen.yaml`), or a
  mapping of targets:

```yaml
identity:
- {override_id: sep-2026-10-14-1, value: separate, key: {source_ids: [s0h3jnptj7a30, s1xyz…]}, author: tomas,
   note: two LW users posted the same title}
- {override_id: mask-2026-10-14-1, value: mask, key: {source_id: s…}, mask_reason: operator}
fields:
- {override_id: title-2026-10-14-1, doc_id: s…, field: title, value: The Right Title}
screen:
- override_id: scr-2026-10-14-1
  key: {doc_id: s…}               # item-grain workflows also name item_key
  scope: {members: [s…]}          # the members it was decided on; required (else review_required)
  field: verdict                  # or `row: true` + a mapping value, or `quarantine: true`
  value: relevant
  origin: human                   # or ai_check
manual_sources:
- {override_id: ms-0007, url: 'https://example.org/page', flags: [accept], note: why, added_by: tomas, added_at: 2026-10-14,
   title: The Page's Title, authors: [A. Author], published_at: 2015-01-01}   # the three optional (D666)
```

- **Prepared files in the repo:** `configs/overrides/manual_sources.yaml` holds the manual sources prepared for the
  pre-2020 scope review (D658: venue-gap landmarks and MIRI papers with `accept`, the MIRI announcement posts
  with `collect_links`); no stage reads it from there, it is added like any file (its header has the commands and
  the plan lines it needs), and `tests/test_collect_manual.py` validates it. A URL entry that is not an arXiv or DOI
  URL never joins an OpenAlex/S2 record of the same work.
- **A manual source's metadata** (D666): optional `title`, `authors` and `published_at` on the entry, in the forms
  a `fields` override's value takes (one validator, `stack.overrides.field_value`: an ISO date or time, a year alone
  written `<year>-01-01`; names or `{name, orcid, affiliation_raw}`), refused at the add when ill-typed.
  `collect.manual` writes them into its `sources` row's `metadata`, and gather merges them like any member's. The
  manual channels rank after every collected channel (before only the link channels), so a manual value only
  fills what no collected member of the document has (a blog's, card's or OpenAlex record's title wins); a
  `fields` override is the way to force a value.
  A stored entry gets fields by a new entry `supersedes:` it (same URL, the fields added): `collect.manual` then
  writes a new row for that source and gather updates the document.
  `configs/overrides/lineage_origins.yaml` (D664, approved as prepared on 2026-10-21; the operator
  imports it at build time) is a mapping file with three targets: `manual_sources` (`accept`) for the curated origin works of
  the narrow-model lineage before the 2015 floor or in venue gaps, and `screen` / `stack_relevance` field
  overrides (`verdict: relevant`) that keep the present Distill origins S-relevant whatever the screen says; R
  (`sr_relevance`) is left to the model. `tests/test_overrides_lineage_origins.py` validates it.
  `configs/overrides/data_quality_2026_10.yaml` (D768, approved 2026-10-30; imported the same way) holds the
  data-quality fixes from the review of release 2026-10-27: `fields` fixes of card titles and doc types (each
  note records the rule and the stored value), `identity` merges of blog posts with their forum copies, and
  `technique_staged` row overrides that drop technique rows (a document-grain workflow has no item override: the
  entry is the model's payload without those rows); `tests/test_overrides_data_quality.py` validates it.
- **Screen overrides for documents that do not exist yet.** A `<wf>` override needs the document's `doc_id` and
  its live members (`scope.members`), and a manual source's `source_id` is minted when `collect.manual` first
  sees it, so the screen overrides for newly added manual sources are a second file, written after the first
  build that gathered them: per entry, the documents head's row whose `identity.keys` hold the entry's key (the
  `key` column of the `--dry-run` output, `<kind>=<value>`) gives `doc_id` and the live members
  (`identity.members[].source_id` where `is_live`). A later member (a lookup's record, a merge) makes the override
  stale: it is then listed in the row's `override_stale_ids` and must be re-scoped.
- **CSV** uses the family's columns: `override_id, target, key, item_key, kind, field, payload, scope_members,
  scope_basis_digest, tier, author, created_at, revokes`. `payload` is JSON (`{"value": …}`, plus `mask_reason`,
  `url`, `flags`, `title`, `authors`, `published_at`, `note`), `scope_members` a JSON list. Each row is turned back
  into its target's entry and validated as the YAML is.
- `override_id` is unique in the whole chain; reusing one is refused. `created_at` defaults to the time of the add
  (a time without a zone is UTC); `tier` is `human` unless a `<wf>` entry says `origin: ai_check`.
- An unknown target (a misspelt workflow) is refused.

## Who reads it

`gather` (targets `identity`, `fields`), `collect.manual` (`manual_sources`) and `annotate.<wf>` (`<wf>`, and a
record's own name for an annotation record). Each pins the chain head like any other input: `stack build` passes
`--in data/overrides/<series>/` to them when that directory has a file, and their run record's `inputs.overrides`
names the exact file they read. A standalone run of one of them (`stack run gather`, `stack run annotate.<wf>`,
`stack run collect.manual`) into a series whose `data/overrides/<series>/` has a file, without `--in` of it, is
refused before it starts: it would silently undo every merge, mask, field and row override (and retract every
manual source) of the series until the next build re-applies them. Pass the directory, or `--no-overrides` to run
without them on purpose (D495). By hand: `stack run gather --in data/sources.<p>/<series>/ … --in
data/overrides/<series>/`.

## Provenance of an overridden annotation

A `<wf>` row or field override sets the payload; the row's `origin` is the highest tier used (`human` or
`ai_check`) and `override_ids` (in the `<wf>` file, not the corpus) lists the rows used. When the overrides supply the
whole payload (a document-grain `row: true`; at item grain, every item), the overrides are the decision (D717):
`decided_at` is the newest `created_at` of the rows used, `config_digest` the fingerprint of those rows
(`stack.overrides.rows_digest`, not a plan pin, so `ann_pinned` with a pin digest does not match it), and there is
no `evidence_id`. A field override over a model's answer keeps the answer's provenance. The `author` and `note` stay
on the override row (the ANN struct has no place for them): give an AI stand-in answer `origin: ai_check`, an
`author` naming the model (`ai:<model>`) and a `note` saying how it was produced. An annotation record (registry
`produced_by`, e.g. `model_regexp_matches`) takes only its own overrides: a row override of its producer leaves the
record as the producer's evidence left it.

## Revoke and replace

- **Revoke:** `{override_id: r-…, revokes: <id>}` (an `ai_check` revoke in a `<wf>` file says `origin: ai_check`).
  A revoke needs a tier at least that of the row it names; a revoke of a revoke, an under-tier revoke, one naming
  another target's row and one naming no row (of the chain or the same file) are refused. A revoke cannot wait for a
  later row: kept, it would silently cancel whatever is added under that id later.
- **Replace:** `supersedes: <id>` on a new entry adds two rows, a revoke (`<new id>.supersedes`) and the new row.
- A revoke is final: to restore a revoked override, add it again under a new id.

## What wins

1. Revoked rows are dropped.
2. Per target, key, item key and field (a quarantine is its own group) the row that sorts last by (tier, `created_at`,
   `override_id`) wins: `human` above `ai_check`, then the later time, then the larger id. Two rows for one
   document field, two masks of one source, or a merge and a separate of the same source set: the later wins.
3. `<wf>`: rows whose `scope_members` (and `scope_basis_digest`, when set) no longer match the document are stale
   (`override_stale_ids`) and left out *before* step 2, so an older matching row still applies. The winning row-level
   row applies first; the winning field rows compose over it.
4. With no row left, the stage's own value stands.

## Provenance

- Which overrides a result used: the row's `override_ids` (`<wf>`), `identity.override_ids` (`documents`), the
  manual source's record (`override_id`).
- Which version of the overrides a run read: its run record's `inputs.overrides` (the head file id). `stack verify`
  X8 reselects a `<wf>` file with the overrides at that head, so a later add never turns an older file red.
- The chain itself: `stack ls data/overrides/<series>/` (its files in order) and `stack head data/overrides/<series>/`.
