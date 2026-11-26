# License classes of channels (project decision D8)

Status: adopted 2026-07 (v3), unchanged in v4. The "D8" cited in `configs/channels.yaml` is this decision of the v3
planning round, not row D8 of `docs/DECISIONS.md` (in-band snapshots).

## What a license class is for

Every channel in `configs/channels.yaml` carries one `license_class` (`configs/enums.yaml`: `open`, `public_web`,
`restricted`, `shadow`, `api_tos`); a source row takes its channel's class. The class is read at use time, not
stored as a verdict about a document:

- **LLM egress.** `configs/llm.yaml` `egress.by_license_class` names the providers a call may go to for each class
  (`stack/llm/__init__.py`); a basis row without a class is sent as `restricted` (`stack/annotate/stage.py`).
  `open` and `public_web` may also go to Google Vertex; `restricted` and `api_tos` to Anthropic and OpenAI only;
  `shadow` to OpenAI only.
- **Releases.** The dataset release records each member's class (`configs/releases/dataset/mapping.yaml`) and
  handles `shadow`-anchored rows specially (`stack/fold/dataset.py`).

Because a wrong `open` lets text go where it may not, misclassing upwards is an egress violation, not a cosmetic
default. Classes are therefore assigned conservatively.

## The rule

- **`open`**: channels whose content carries an explicit open license: arXiv (its license is explicit; the enum's
  `open` gloss names it), LessWrong and the Alignment Forum (posts default to CC BY).
- **`public_web`**: self-published, publicly reachable content without an explicit open license: lab blogs, model
  and system cards, policy documents, newsletters. Not `open`, even where the content is meant to be read freely.
- **`restricted`**: paper full texts found through DOI-based discovery (OpenAlex, Europe PMC, Semantic Scholar and
  the other paper-search channels). acquire's oa lane fetches them only through a fetchpdf build that can reach no
  non-open-access source: it refuses to run otherwise (`stack/oa_adapter.py` `_require_open_access_only`). The
  licence of a particular copy is still not known at channel level, so they are classed `restricted`, never `open`.
- **`shadow`**: no channel carries this class. It is kept so that a record of such origin, if one is ever imported,
  is dropped from every release (`licence_rules.shadow` in `configs/releases/*.yaml`; the dataset mapping drops
  `shadow_anchor` rows).
- **`api_tos`**: content obtained through an API whose terms constrain redistribution.

## Open

One class per channel cannot tell two copies in one channel apart. A per-copy classification, from where each
copy was acquired, could sharpen `restricted` later; nothing does it yet.
