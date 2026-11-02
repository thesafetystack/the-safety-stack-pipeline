# Changelog: models

## 1.1.0 (2026-10-29)

- The catalogue envelope (schema `models-2.schema.json` on `../schemas/catalogue-0.schema.json`;
  [../README.md](../README.md#the-catalogue-envelope)): `version`, `meta` (`schema`, `status` `released`, `updated`
  2026-10-28, `description`, `references: {orgs: 0.2.0}`) and one `entries` list, 1.0.0's 1,877 nodes in its order,
  each with its key as `id`. `meta.schema_version` is gone (`meta.schema` names the structure).
- Organisation references: `maker` (one, or a list on the 43 joint releases) and `lab` are `{id, name}`, `id` an
  entry of `../orgs/orgs-0.2.0.yaml`, `name` the display string (a maker's from 1.0.0's `meta.makers`, a lab's its
  `lab` string). The validator checks every id resolves there.
  - Maker `internlm` is `shanghai-ai-lab` (name unchanged, "Shanghai AI Laboratory (InternLM)") on 31 nodes; the
    node id `internlm` (the model family) is unchanged. The other maker keys were already organisation ids.
  - `lab` ids (128 nodes): DeepMind → `deepmind`, Microsoft Research → `microsoft-research`, ByteDance Seed and
    Seed → `bytedance-seed`, OpenBMB → `openbmb`, FAIR → `meta-ai`, Google Research → `google-research`, OpenMOSS →
    `openmoss`, Microsoft AI → `microsoft-ai`.
- `meta.makers` and `meta.maker_facets` are gone. A maker's name is on its references; its country and type are its
  organisation entry's (orgs 0.2.0 took over the facets: `country`, the tag `big-tech`, the notes). Four 1.0.0
  makers had no node and so appear nowhere in 1.1.0: `deepmind` (DeepMind models have maker `google`, lab
  `deepmind`), `fast-ai`, `korea-university`, `nous-research`; 134 makers remain.
- No node, name, gloss or mention changes: the annotation menu built from 1.1.0 is byte for byte the one built from
  1.0.0 (maker names, glosses, denotations, the matcher's patterns).
- A reader keyed on the maker `internlm` must use `shanghai-ai-lab`; a reader of `meta.makers` reads the references.
- sha256 `bd731a85743c86d9c0fe2a909ecf35c5713edb353ecc5d4b3056d968194ec911` (an untagged draft of 1.1.0 with
  `lab_id` beside `lab`, sha256 `c10a6dace9c7…`, preceded it and was never used).

## 1.0.0 (2026-10-24)

- First release; content identical to 0.9.0: same 1,877 nodes and 138 makers, field for field; `meta.updated` stays
  `2026-10-17`, the date of the content.
- Header only: `version` `'1.0.0'` (was `'0.9'`). The file has no `status` field; its schema has none.
- Schema `models-1.schema.json`: `models-0.schema.json` with the version pattern for major 1.
- sha256 `d7f1bae80e5b1dd9a978cecad8d78cba7be7adebd0fa5bdc99692b731aa4acb5`.

## 0.9.0 (2026-10-17, `meta.updated`)

- 1,877 nodes: 310 families, 249 groups (162 generations, 59 lines, 28 series), 1,318 models; 138 makers.
- Identical to the pipeline's pinned `configs/models.yaml`, sha256
  `ce35fb95729991de3a5cea8d29829d22ed202b236f6a57308140cc23a9643315`.
- The `version` field inside the file (`'0.9'`) will be corrected at 1.0.0.
