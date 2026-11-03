# Taxonomies

- `techniques/`: AI-safety techniques (intervention, monitoring, assessment) and group nodes.
- `models/`: AI model families, groups and models, with their makers.
- `orgs/`: organisations (affiliations, model makers); a 0.x draft.
- Layout: `<name>/<name>-<major>.<minor>.<patch>.yaml` per version, `<name>/CHANGELOG.md`, `<name>/README.md`.
- Schemas: `<name>/schemas/<name>-<n>.schema.json`. A file in the catalogue envelope (below) names its schema in
  `meta.schema`; for an older file the validator takes `<n>` from the file name's major version.

## The catalogue envelope

From techniques 1.1.0, models 1.1.0 and orgs 0.2.0 on, every file has the same structure:

- `version`: the content version (also in the file name).
- `meta`: `schema` (the JSON Schema it follows: `techniques-2`, `models-2`, `orgs-0`), `status` (`draft` |
  `released`), `updated`, `description`, `sources`, `references` (for each other vocabulary this one refers to, the
  version its ids resolve in: models 1.1.0 has `orgs: 0.2.0`), plus the vocabulary's own keys.
- `entries`: one list. Every entry has `id` and `name`, and where they apply `parent` (another entry's id),
  `aliases` (strings, or `{text, kind}`), `note` and `external_ids` (`{<scheme>: [ids]}`, e.g. `ror`); then the
  vocabulary's own fields.
- A reference to another vocabulary's entry is `{id, name}`: the id resolves there, the name is the display string,
  so the file reads on its own.

The shared schema is `schemas/catalogue-0.schema.json`; each vocabulary's schema refers to it and closes the shape.
A schema's number counts structure versions and is independent of the content version: models 1.1.0 follows
`models-2`, the second structure of the models files. The validator also checks what a schema cannot say: unique
ids, parents and children that resolve, and every reference to another vocabulary resolving in the version
`meta.references` names.

## Versioning

- A released file is never edited; a change is a new file with a CHANGELOG entry. Each file has an annotated tag,
  its file name without `.yaml`.
- From 1.0.0 on, identifiers (technique and group-node slugs, model node ids) are frozen within a major version:
  none is removed, merged, split or given a different meaning. A minor version may add new identifiers.
- Definition wording (technique and group-node `definition`, model `gloss`) may be clarified in a minor or patch
  version without changing what it means. Anything that changes the meaning of an existing identifier needs a new
  major version.
- The definitions and glosses in a released file are the text the annotation model was shown when annotating with
  that version. A dataset release records which version of each taxonomy it used, with the file's sha256.
- A change of structure alone (a new schema) is a minor version: techniques 1.1.0 is 1.0.0's content in the
  envelope, and the annotation prompts built from either are the same text.
- 0.x versions (techniques 0.9.1, models 0.9.0, orgs 0.2.0 and 0.2.2) carry none of these guarantees; the 0.9 files
  stay for reference.
