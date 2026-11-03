# Organisations

Files `orgs-0.2.2.yaml` (the current one) and `orgs-0.2.0.yaml` (the version models 1.1.0 refers to), drafts: version
0.x, ids may still change. 0.2.2 has 4,841 organisations, schema
`schemas/orgs-0.schema.json` (the catalogue envelope, [../README.md](../README.md#the-catalogue-envelope)). They are
the organisations named in the author affiliations of the dataset's documents and the makers of the models taxonomy,
which refers to them by id (`maker`, `lab`). The pipeline resolves each affiliation string to these ids with a
deterministic matcher (the dataset's `affiliation_orgs` annotation and each affiliation's `org_ids`).

Top level:
- `version`, `meta`: `schema`, `status` `draft`, `updated`, `description`, `sources` (ROR v2.15, the pipeline's
  registry, the models makers, the corpus's affiliation strings), `types` and `tags` (glosses), and the matcher's
  word lists: `countries`, `regions` (US states), `generic_words`, `unit_words`, `non_org`, and (0.2.2) `places`
  (city names that, after an organisation's name, are its address).
- `entries`: 4,841 organisations in 0.2.2 (4,840 in 0.2.0), ordered by id.

Entry fields:
- `id`, `name`, `type` (0.2.2: `university` 2,020, `company` 1,155, `institute` 781, `nonprofit` 368, `government` 244,
  `hospital` 176, `other` 97), `country` (ISO 3166-1 alpha-2), `parent` (the organisation a unit belongs to:
  `deepmind` under `google`).
- `aliases`, and for the matcher `cased`, `segment_aliases`, `local_names`, `patterns`, `domains`.
- `external_ids.ror` (ROR ids), `other_ids` (the slugs other vocabularies use for the same organisation).
- `tags`: `model-maker` (138: the makers of the models taxonomy 1.0.0), `big-tech` (32: its makers of type big-tech),
  `ai-safety-org` (16), `frontier-lab` (7).
- `note`: curation remarks, among them the models makers' facet notes.
