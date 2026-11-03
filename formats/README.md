# Release formats

The format of a dataset release: which files a release has, the shape of each, and what a reader may rely on. A
format has a semantic version (`<major>.<minor>.<patch>`), from 1.0.0 on; each release names it in `release.json`
(`format_version`), in the parquet file's key-value metadata (`format_version`, beside `release_id`) and in the
dataset card's front matter. Many dated releases share one format: a new data release with the same format changes no
version.

## What is here

- `<major>.<minor>/`: the schemas of that format, as every release of it ships them, stripped of `description`,
  `title`, `examples` and `$comment` (prose, which a patch version may change). The paths mirror the release:
  `release.schema.json`, `data/corpus.schema.json`, `config/configs.schema.json`, `config/overrides.schema.json`.
- `CHANGELOG.md`: one entry per version ([Keep a Changelog](https://keepachangelog.com/en/1.1.0/)); an entry is
  dated when the maintainer tags the version (`format-<version>`).
- `../tools/release-validator.py`: the checker. Every release ships a byte-identical copy as
  `tools/release-validator.py`.

A directory is written once, by the pipeline (below), and never edited once its version is tagged and its changelog
entry dated. CI refuses such an edit: a directory must equal its tree at every tag `format-<major>.<minor>.<patch>`,
no file of a directory whose entry was dated at the base of a push or pull request may change, and a dated entry
may not lose or change its date. The checker carries the digests of every archived format's schemas
(`FORMAT_DIGESTS` in `tools/release-validator.py`), so a release's own copy, run anywhere, detects schemas that are
not their format's; CI checks them against the archive. A patch version changes prose only, so it has no directory of its own.

## The schemas

JSON Schema 2020-12, one file next to the file it describes. Each `$id` is
`urn:the-safety-stack:format:<major>.<minor>:<path>`: an identifier, not a URL, so it implies nothing that resolves.

- `release.schema.json`: the manifest. Strict on what a reader relies on (the release and format ids, the variant,
  the taxonomy pins, the file list with sizes and sha256, the licence); the diagnostic sections (`build`, `stages`,
  `quality`, ...) are free objects. The file list must contain the core files.
- `data/corpus.schema.json`: one row of the parquet file, `additionalProperties: false` on every object. Every node
  carries `x-arrow`, its exact Arrow type (`struct`, `list`, or a leaf type such as `timestamp[us, tz=UTC]`); a
  nullable field has `null` among its types.
- `config/configs.schema.json`: the envelope of `config/configs.json` (an array; string-encoded fields stay strings).
- `config/overrides.schema.json`: one line of `config/overrides.jsonl`: the envelope and the closed list of `kind`;
  the value per kind is free (FORMAT.md describes it).

**Closed and open lists.** A closed list is an `enum`: the reader may match it exhaustively, and it changes only in a
major version. An open list has no `enum` but `x-open: true` and its known values in `x-values`: values may be added
in a minor version, and a reader must expect values it does not know. Some strings with a documented vocabulary are
neither (the providers' `doc_type`, the channel names): their values are described in the release's
`VOCABULARY.md`, and new ones may appear in any release.

## Compatibility

Reader-centric semantic versioning. **Readers are expected to** select fields by name, tolerate fields and files they
do not know, and tolerate new values in open lists.

| Change | Version |
|---|---|
| a field or file removed or renamed | major |
| a JSON type or an `x-arrow` type changed | major |
| a field that may now be null (nullability loosened) | major |
| a field that is no longer required (it may be absent) | major |
| any change to a closed list (a value added, removed or renamed; a list opened or closed) | major |
| a value removed from an open list | major |
| a change in meaning (declared by hand, with a changelog entry) | major |
| a structural difference the classifier does not know | major |
| a field or file added | minor |
| a value added to an open list | minor |
| a new taxonomy key in `release.json` | minor |
| a field that may no longer be null (nullability tightened) | minor |
| a field that is now required | minor |
| prose and descriptions only | patch |

What is structural is exactly what the stripped schemas say: names, JSON types, `x-arrow`, nullability, `required`,
the closed lists, the open lists' known values, and any other keyword. Prose (descriptions, FORMAT.md,
VOCABULARY.md, the card) is not.

## Checking a release

```bash
uv run tools/release-validator.py <release directory>              # a seeded sample of 1000 rows
uv run tools/release-validator.py <release directory> --rows all   # every row
```

It compares the release's schemas with the archived ones wherever it runs (by the digests it carries, as above);
run from a checkout of this repository, it compares them with the archive here and also checks the shipped taxonomy
files with `tools/taxonomy-schema-validator.py`. Exit codes: 0 valid, 1 invalid, 2 usage,
3 unsupported format version (a major version the checker does not know, or a release from before 1.0.0, which has
no `format_version`; the checks that do not depend on the format still run).

## How a format version is cut

1. **The pipeline is changed** (thesafetystack/the-safety-stack-pipeline). Its `tools/release_format.py` generates the
   schemas from the export code, strips them, and classifies the difference against the highest version archived
   here below the version the pipeline declares (`format_version` in its `configs/releases/dataset/mapping.yaml`):
   it prints the bump needed and refuses a declared version below it. A change in meaning the classifier cannot see
   is declared by hand: a major version and a changelog entry saying what changed.
2. **The archive is written here.** `release_format.py archive --data-repo <checkout of this repository>` writes
   `formats/<major>.<minor>/` (run in the pipeline, pointed at a checkout of this repository; it refuses to change
   a dated or tagged directory, and changes an undated one only with `--replace`); add the `CHANGELOG.md` entry
   (undated) in the same commit, the schemas' digests (`--formats` prints them) in `FORMAT_DIGESTS` of
   `tools/release-validator.py`, and a new major version of the checker where the checks change
   (`SUPPORTED_MAJORS`). Run `uv run tools/release-validator.py --formats` and `uv run tools/test_release_validator.py`.
3. **The maintainer** reviews, pushes, tags the commit `format-<version>` (an annotated tag) and, in the same step,
   dates the changelog entry. These are the stop points: no tag, no date and no push without the maintainer. From
   then on the directory is never edited (a fix is a new version).
4. **The pipeline pulls** this repository's subtree (`git subtree pull --prefix=data-repo ... --squash`) and renews
   its lock of the archive (`tools/release_format.py lock`); its tests then find the archive equal to what it
   generates.
