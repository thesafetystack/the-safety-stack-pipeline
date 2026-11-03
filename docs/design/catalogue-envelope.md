# Catalogue envelope: one structure for the taxonomy files

Status: adopted by all three catalogues, 2026-10-29 (D738; the first adopter was orgs 0.2.0, the organisation-vocabulary work,
D737): techniques 1.1.0, models 1.1.0 and orgs 0.2.0 are files of the data repository (`data-repo/taxonomies/`),
pinned by plan `batch-2026-10a` since revision 9; since revision 11 (D778, D779) `annotate.affiliation_orgs`
reads orgs 0.2.2 (`data-repo/taxonomies/orgs/orgs-0.2.2.yaml`: 0.2.0 with the place names of 0.2.1, D760, and the
Chinese-script aliases, D756); models 1.1.0 still refers to orgs 0.2.0. Terms: `Dnnn` are `docs/DECISIONS.md`; "pin" and "config digest" are
the annotate stages' (`stack/annotate/stage.py`).

## 1. What there was

Before 1.1.0 the three catalogues shared no structure:

| | techniques 1.0.0 | models 1.0.0 | orgs 0.1.0 |
|---|---|---|---|
| version | `version`, plus `status`, `notes` at top level | `version` | `version` |
| meta | none (top-level `status`, `notes`) | `meta`: `schema_version`, `updated`, `makers`, `maker_facets` | `meta`: `schema_version`, `updated`, `description`, `sources`, lexicons |
| entries | two lists, `techniques` and `group_nodes` | a mapping `nodes` (id → node) | a list `orgs` |
| id field | `slug` | the mapping key | `id` |
| aliases | typed: `{synonym, instance, legacy}` | `mentions`, `weak_mentions`, `mentions_verbatim` | `aliases` plus side lists (`cased`, `segment_aliases`, `local_names`, `patterns`, `domains`) |
| external ids | none | `links` (URLs) | `ror` |
| JSON Schema | `techniques-<major>.schema.json` | `models-<major>.schema.json` | none |

Each had its own loader (`workflows/annotate/technique_staged/taxonomy.py`, `stack/modelvocab.py`, `stack/orgvocab.py`)
and the data repository's validator (`data-repo/tools/taxonomy-schema-validator.py`) a cross-reference check per
catalogue.

## 2. The envelope

```yaml
version: '1.1.0'                    # <major>.<minor>.<patch>: the CONTENT version; the file name carries it too
meta:
  schema: models-2                  # its JSON Schema: <catalogue>/schemas/<catalogue>-<n>.schema.json
  status: released                  # draft | released
  updated: '2026-10-28'
  description: ...
  sources: {...}                    # where the content came from
  references: {orgs: 0.2.0}         # the version of each catalogue this one's references resolve in
  ...                               # the catalogue's own keys (orgs: types, tags, countries, regions, word lists)
entries:                            # ONE list
  - id: gpt-1                       # core: id, name, parent, aliases, note, external_ids
    name: GPT-1
    parent: gpt
    maker: {id: openai, name: OpenAI}   # a reference into another catalogue: {id, name}
    ...                             # the catalogue's own fields
```

- **Core entry fields**: `id` (slug, lower case, `-` and `.` separators), `name`, `parent` (another entry's id, or
  absent), `aliases` (strings, or `{text, kind}` where a catalogue types them), `note`, `external_ids` (`{<scheme>:
  [ids]}`: `ror`, later `wikidata`, `openalex`, `hf_org`). Every catalogue has them with the same meaning, so a reader
  that only needs "what is this id called, what is it under, what else is it called" works on all of them. A
  catalogue may leave one out (models have no `aliases`: their `mentions` are matcher patterns, not names; they keep
  `notes` and `links`).
- **One entries container**: a list, not a mapping. A list keeps the id in the entry, which is what a reader joining
  on ids wants, and lets the schema say "unique ids" in one place. Its order is the catalogue's to define: orgs are
  ordered by id; techniques and models keep their 1.0.0 order, because what the stages render or match follows it
  (the technique slugs list, the model matcher's node order).
- **References between catalogues** are `{id, name}`: the id resolves in the version `meta.references` names, the
  name is the display string, so the file reads on its own (`catalogue-0.schema.json` `$defs/ref`).
- **Type-specific fields** stay in the entry (orgs: `type`, `country`, `tags`, the matcher's lists; models: `kind`,
  `gloss`, `maker`, `lab`, `mentions`, `lifecycle`, ...; techniques below).
- **Schemas** (the data repository's, the single source; `stack/catalogue.py` reads them from the subtree): one
  shared base, `taxonomies/schemas/catalogue-0.schema.json` (version, meta with `schema`, `status`, `updated`, one
  `entries` list, core entry fields, the reference shape; open to extension), and per catalogue
  `taxonomies/<catalogue>/schemas/<catalogue>-<n>.schema.json`, which refers to the base (`allOf`) and closes the shape
  (`additionalProperties: false`, its own fields and meta keys). The validator picks the schema from `meta.schema`
  (a file before the envelope: from the file name's major version).
- **One loader and validator path**: `stack/catalogue.py` validates a file against `meta.schema` (with every schema
  in one registry) and checks unique ids; each catalogue's module adds what a schema cannot say. The data
  repository's validator does the same and the cross-catalogue checks (below).

### Version semantics

`version` is the content version, `meta.schema` the structure version, counted on its own per catalogue: models 1.1.0
follows `models-2` (the second structure of the models files; `models-1` covered 0.9.0 and 1.0.0), techniques 1.1.0
`techniques-2`, orgs 0.2.0 `orgs-0`. A change of structure alone is a minor content version (decided 2026-10-29: no
release had used the earlier shapes): techniques 1.1.0 is 1.0.0's content, models 1.1.0 1.0.0's nodes with organisation
references. Ids stay frozen within a major content version as before.

### Techniques (schema `techniques-2`)

Four points where techniques' existing fields meet the core:

- **Group marker `node`**: `technique` | `group`. `kind` was already a rendered technique field (intervention,
  monitoring, assessment: the menu's `[kind]`), so the marker could not be `kind` as the first draft said.
- **Per-entry `status`** (`final` | `provisional`, rendered as "(provisional)") stays; it is the technique's own and
  distinct from `meta.status` (the file's draft / released).
- **Group `children`** stay an ordered list: the menu prints them in file order.
- **Aliases** are `[{text, kind}]` (`synonym`, `instance`, `legacy`; a group node has no `instance`), in file order:
  the menu prints the synonyms (aka) and instances (e.g.) each in file order.

The entries are the techniques, then the group nodes, each in 1.0.0's order (`all_slugs` order). The loader maps the
envelope onto the 1.0.0 shape (`taxonomy.from_envelope`), so both files give the same `Taxonomy`.

### Models (schema `models-2`)

`maker` (one reference, or a list for the 43 joint releases) and `lab` are `{id, name}` into orgs 0.2.0 (maker
`internlm` is `shanghai-ai-lab`; the names are 1.0.0's `meta.makers` strings and `lab` strings). `meta.makers` and
`meta.maker_facets` are gone: the menu shows the references' names, and a maker's country and type are its
organisation entry's (orgs 0.2.0 took over the facets: the tag `big-tech`, the facet notes, and the one conflict,
`arc-institute`, which was ARC Institute Indonesia's ROR record and is now the US Arc Institute). Four 1.0.0 makers
had no node (`deepmind`, `fast-ai`, `korea-university`, `nous-research`) and appear nowhere in 1.1.0; their org
entries keep the tag `model-maker`. `stack.modelvocab` reads a file before 1.1.0 through `from_legacy` (makers as
references with `meta.makers`' names), read-only.

## 3. Cost: what a new catalogue file re-asks, and what it cost here

A catalogue file's bytes are pinned. `annotate.technique_staged`'s config digest includes the taxonomy file's sha256
(its plan pin `taxonomy:`); `annotate.model_roles`'s includes `configs/models.yaml`'s sha256
(`stack.modelvocab.vocab_digest`; `model_regexp_matches` is a record of the same call). A re-shaped file is a new file:
a new config digest, every work key new, every document asked again. The recorded paid spend of the stored answers
(the 2026-10-24 runs, 39,269 documents per stage) is about $282 for technique_staged (single calls $219, gleaning
turns $63) and $175 for model_roles; an earlier version of this section quoted $217 / $119, the plan header's
per-1,000 estimates, not what was spent.

The LLM response cache does not include the config digest (`H([model, rendered_digest, basis_digest])`,
`stack/llm/cache.py`), so a re-asked document whose request is byte-identical is answered from it at $0 (D707, D591).
The envelope files are built so that every request is byte-identical (`tests/test_vendored_taxonomies.py`: the full
menu, the compact index, every sub-menu block, the lexical matcher's surfaces and regexes; every node's model menu
line, the matcher's inputs, regexes and prefilter; the catalogue-envelope work's sample of 50 stored documents rendered the same call
arguments and digests under 1.0.0 and 1.1.0). Measured on a store copy (D738; no key, a refusing transport): model_roles is
answered from the cache for every document with a stored answer (39,259; the 10 stored failures have no entry);
technique_staged's first call too (39,258), but **not its gleaning turn** (39,225 misses of 39,258), under 1.0.0 as
under 1.1.0. The gleaning turn's prior turns carry the first answer as `json.dumps(res.parsed)`, and a cache hit's `parsed`
comes back with sorted keys (the entry is written with `sort_keys`), so the second request differs from the one sent
live. That is a cache-replay defect, independent of the catalogue: any re-ask of a gleaning document served from the
cache pays for its gleaning turn again (about $63 here) and is answered anew. Restoring the key order from the stored
response body made all 50 sampled documents' gleaning turns hit (the catalogue-envelope work).

The fallback (D738) re-keys the stored evidence instead: a store migration that appends, per document, a copy of the
selected evidence row under the new config digest, pointing back to the original (`supersedes`), at no cost. It is
built and verified on a local store copy only (the store's checks pass, the next build carries every document);
using it on the production store is a separate decision. Without it, deploying revision 9 re-asks: model_roles from
the cache at $0, technique_staged's gleaning turns at about $63 (and 26 documents whose earlier calls failed).
Production took neither way: from revision 10 (D758) both stage lines accept the decided answers of their 1.0.0
configs (`accepted:`), which keep their 1.0.0 labels, and ask only documents without one.

- **No loader-level adapter for the new files**: the files themselves are in the envelope; each loader still reads
  its catalogue's earlier shape (eval sets and stored runs name those files).
- The eval sets' frozen sha256 (`evals/*/manifest.json` `frozen_with`) no longer match `configs/models.yaml`; their
  gold ids are all in the menu source, and an eval run warns that the digest moved.

## 4. Cross-references between catalogues

The models catalogue names organisations twice: `maker` and `lab`, both `{id, name}` since 1.1.0, with
`meta.references.orgs` the orgs version they resolve in. The data repository's validator checks that every maker and
lab id is an entry of that version, every maker tagged `model-maker` there, and one name per maker id;
`tests/test_vendored_taxonomies.py` checks the same against the vendored file.

Pipeline configs refer to organisations by slug too: `configs/registry.yaml` `orgs[].slug` (all 37 are organisation
ids) and `configs/channels.yaml` `institutions[].org` (ids since channels 2.4.0, D737). `tests/test_orgvocab.py` checks
that every registry slug, channel institution and models maker resolves.

## 5. Open

- Whether `meta.schema` should carry the catalogue name and number only (`models-2`, as here) or a URL.
- Whether `aliases` should be typed in every catalogue (orgs' side lists are matcher instructions rather than alias
  types; they stay type-specific fields), and whether models' `notes` and `links` should become the core `note` and
  `external_ids`.
- The tag `model-maker` on the four orgs whose maker has no node in models 1.1.0.
- The cache-replay defect of the gleaning turn (section 3): fix in the client (a hit's `parsed` in the response's key
  order) or in technique_staged (the prior turn from the response text), before the next catalogue change that
  re-asks.
