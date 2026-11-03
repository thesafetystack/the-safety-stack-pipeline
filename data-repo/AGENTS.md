# AGENTS.md

The taxonomies of The Safety Stack (techniques, models, organisations): versioned YAML files, their JSON Schemas and
a validator; and the archive of the dataset's release formats (`formats/`) with the release checker. Humans start at
`README.md`; reports go through the issue forms (`CONTRIBUTING.md`).

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

## Release formats

- `formats/<major>.<minor>/` holds a format's schemas as the pipeline generates and strips them
  (`formats/README.md`: the compatibility rules and how a version is cut). It is written by the pipeline's
  `tools/release_format.py archive`, never by hand, and never edited once its version is tagged `format-<version>`
  and its `formats/CHANGELOG.md` entry dated (the maintainer does both in one step; CI refuses the edit). A format
  change starts in the pipeline; a new directory comes with its changelog entry (undated) and its digests in
  `FORMAT_DIGESTS` of `tools/release-validator.py` in the same commit.
- `tools/release-validator.py` is shipped byte-identical in every release and read by the pipeline (through its
  `data-repo/` subtree) on every export. A change to it is a change of every future release's checker: keep its
  checks within what the format promises, keep its tests (`tools/test_release_validator.py`, negative fixtures
  included) passing, and add a major to `SUPPORTED_MAJORS` only with that major's checks.
- Tags `format-<version>` and the date of a changelog entry are the maintainer's.

## Check

```bash
uv run tools/taxonomy-schema-validator.py --all     # every taxonomy file
uv run tools/test_release_validator.py              # the release checker's tests
uv run tools/release-validator.py --formats         # the archive of formats (CI adds --since <base>)
```

CI runs all three on every push and pull request.

Tags and pushes are made only on the maintainer's explicit instruction.
