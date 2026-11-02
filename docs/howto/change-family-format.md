# Change a family's format

A family's **format** is its pyarrow schema (`stack/schemas/families/`); its **versions** 1, 2, … number the formats
it has had. `stack/schemas/upcasters.py` holds each family's history (`VERSIONS`): per version its number, the
family's `schema_digest` at it, the **step** from the previous version and a one-line note (what changed, which
decision). Every file's footer says its version (`stack.family_version`; a legacy file, `stack.schema_version = "1"`,
is mapped by its `schema_digest`), and every stack reader reads every file **at the current version**
(`stack.store.parquet.read_family`): an older file goes through the steps from its version on. A chain may mix
versions; writers write current-version files, full or part, on top of older ones. Nothing has to be migrated,
except for a change no read-time step can make (below).

## Steps

1. **Edit the schema** (`stack/schemas/families/*.py`, `stack/schemas/blocks.py`, `stack/schemas/structs.py`, or a
   payload in `stack/schemas/payloads/`).
2. **Run the registry test**: `uv run --no-sync pytest -q tests/test_family_format.py::test_registry_every_family_versioned`.
   It fails and prints, per changed family, its new digest. Until the version is registered the package imports but
   `write_family` refuses every file of that family (S7 names the family, the digest and this page).
3. **Append the version** to the family's tuple in `VERSIONS`: `Version(<n+1>, "<new digest>", "<D…: what changed>",
   <step>)`, the step being one of
   - `ADDITIVE`: the version only adds nullable fields (top level, inside structs, inside `list<struct>` items) or
     relaxes non-null to nullable. Check it: `stack.schemas.upcasters.schema_delta(old_schema, new_schema).problems`
     is empty.
   - a function `pa.Table -> pa.Table` (version n → n+1), for a rename, a type change, a split. Contract (module
     docstring): a field added by an earlier `ADDITIVE` step may be absent (= all NULL); **key columns never change**
     (a key change is a new family, or `Incompatible`); one additive conform to the current schema runs after the last
     step, and the result must equal the current schema exactly (else `UpcastBug`). A step that must refuse a file's
     data (it would discard values) raises `StepRefused`: readers get `UpcastRefused` naming the file, and `stack
     verify` reports it as that file's S1 finding (D648; D591's `documents_drop_model_mentions` was the example,
     retired by D693).
   - `Incompatible("<D…: why; the remedy: re-run <stage> / stack migrate …>")`: older files then fail loudly with that
     reason (`IncompatibleFormat`), in every reader and in `stack verify` (S1, V11).
4. **A payload change** (`stack/schemas/payloads/`, a new annotator) is a format change of **every family that embeds
   it**: `documents` and `corpus` (`annotations.<wf>`), `<wf>` (flat payload columns) and `<producer>_evidence`
   (payload columns or the record's struct column). Each gets its own version and step; a new annotator's `<wf>` and
   `<wf>_evidence` families start at version 1. `PayloadSpec.version` is not a file-format version and does not move.
5. **Tests.** For a function step, a test that writes a file at the old version and reads it at the new one (the test
   family in `tests/test_family_format.py` is the pattern); for a legacy-style reconstruction, add the fields to
   `ADDED_SINCE` there. Then the fast suite.
6. **Optional: `stack migrate upcast --family <F> [--file <id> | --in data/<family>/<series>/ ...] [--dry-run]`** materialises the current version
   (chains: a `fold.compact` full file; a chain of one: a `migrate.upcast` full file; `corpus` is never migrated). The
   dry run lists per file `v<k> → v<N>` and the step kinds. Run it to drop the per-read upcast cost of a large old
   version or to hand duckdb consumers current files (the old files stay; nothing deletes data files).

## A change that needs data outside the file

A step is a function of the file alone. A change that needs data the file does not hold cannot be a read-time step:
D690 records each blob reference's size, which older files lack for converted texts and in-band bodies and which only
a LIST of the store's `blobs/` gives. Such a change is done once, on disk (D691 is the example; its tool and
module were removed by D693):

- Register the new version behind `Incompatible("<D…: why; the remedy: tools/<one-off tool>>")`, so every reader
  refuses an older file and names the tool.
- The one-off tool reads each older file through the earlier steps up to the last old version (D691 gave
  `upcasters.upcast` an `end=` parameter for that, removed with it), then applies its own mapping with the outside
  data and writes the file in place
  (same key, file id and header except `schema_digest`/`family_version`); it patches every record naming a rewritten
  file (`file_digest`, and `schema_digest`/`family_version` where present).
- Run it in a window with no build: a dry run first, an archive of the target files and records before anything is
  written (on a mirror), then the rewrite, then `stack verify --full` (`docs/OPERATE.md`, "Blob-reference upcast").
- Once every store is upcast, delete the tool and its old types and step, and retire the older function steps to
  `Incompatible` (no path can reach them past the new version's step); keep the older version entries, so an older
  file is refused by name rather than as an unknown digest (D693).

## What readers see

- Stack readers (stages, folds, `release`, `verify`, `diff`): the current version. `stack ls` and `stack head` read
  only run records, so they show no format version (their "version" is a chain's head files); `stack migrate upcast
  --dry-run` lists each file's `v<k> → v<N>`. `stack release` and `stack fold build` print `note: <file> is corpus
  v2 (current v3, upcast on read)` on stderr for an older corpus.
- A file **newer** than the code (`family_version` above the current one, or an unknown `schema_version`) is refused
  (`NewerFormat`: update the code); a legacy file whose digest no version has is `UnknownFormat`; a file whose digest
  is not its declared version's is a `SchemaMismatch`.
- A consumer reading `data/` with raw duckdb sees each file's own version (`stack.family_version` in the footer):
  read through `read_family` instead, or use a release (`docs/CONSUME.md`).
