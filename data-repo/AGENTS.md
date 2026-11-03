# AGENTS.md

The taxonomies of The Safety Stack (techniques, models, organisations): versioned YAML files, their JSON Schemas and
a validator. Humans start at `README.md`; reports go through the issue forms (`CONTRIBUTING.md`).

## Rules

- A released file is never edited, not even for whitespace: the pipeline that builds the dataset
  (thesafetystack/the-safety-stack-pipeline) carries this repository as a git subtree and pins each file by sha256,
  and published annotations name the version they were made with. A change is a new file.
- Versioning is in `taxonomies/README.md#versioning`: ids frozen within a major version from 1.0.0 on; wording
  clarified in a minor or patch version; a structure change alone is a minor version; 0.x carries no guarantees.
- A new version is `taxonomies/<name>/<name>-<version>.yaml`, `<version>` being its `version` field
  (`<major>.<minor>.<patch>`; the 0.9 files predate this and say `0.9`), with `meta.schema` naming its schema; a `CHANGELOG.md` entry (what changed, the counts, the file's
  sha256); the counts in `taxonomies/<name>/README.md` and the root `README.md`; for techniques and models also
  `CITATION.cff`'s version. It gets an annotated tag named as the file without `.yaml`, with a one-line message.
- A schema change is a new `taxonomies/<name>/schemas/<name>-<n>.schema.json`; the shared envelope is
  `taxonomies/schemas/catalogue-0.schema.json`.
- Licences: everything outside `tools/` is CC0 1.0, `tools/` MIT.

## Check

```bash
uv run tools/taxonomy-schema-validator.py --all     # every file; CI runs the same on every push and pull request
```

Tags and pushes are made only on the maintainer's explicit instruction.
