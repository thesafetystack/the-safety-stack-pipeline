# Add an annotator

An annotator is an annotate stage `annotate.<wf>` ("workflow" in code): per document in scope it asks (usually one
LLM call) and writes `<wf>` (the selected result per document or item) and, with evidence, `<wf>_evidence` (every
attempt). `fold.build` folds `<wf>` into the corpus as `annotations.<wf>`. The driver's contract is the module
docstring of `stack/annotate/stage.py`; the registry's is `stack/schemas/payloads/registry.py`. Worked examples:
`workflows/annotate/screen/` (records, two tiers), `affiliation/` (item grain), `model_roles/` (an LLM-free matcher
then one call, prompt files selected by the pin name), `technique_roles/` (a taxonomy menu filled into the prompt
file's system part), `technique_staged/` (several dependent calls in one ask, chosen by a required pin key `mode`:
`single`, `sweep`, `staged`, `lexical`; a prompt set maps each call type to its file; `Answer.llm` merges the calls'
cost and digests, D602), `affiliation_orgs/` (D736: a `chained` annotator on
`affiliation` that asks no model, `Answer(llm=None)`, origin `computed`; its vocabulary file's sha256 is the pin's
template digest), `translation/` (D755: a `fields` basis, one small call on the title and abstract; its release kind
shows how a payload path follows the abstract policy, mapping.yaml `abstract_paths`).

## Checklist

1. **Payload schema.** A pyarrow struct in `stack/schemas/payloads/<wf>.py` (document grain: any struct; item grain:
   exactly one `list<struct>` whose struct carries `item_key`).
2. **Registry entry**, appended at the end of `REGISTRY` in `stack/schemas/payloads/registry.py`:
   `PayloadSpec(workflow, version, payload_schema, grain, text_bearing, evidence, basis_kind, produced_by="",
   reads=())`.
   - `basis_kind`: `text` (the document's current text), `fields` (metadata only) or `chained` (the text plus
     other annotations' results; then `reads` names them, each an earlier entry). A change in a read re-asks the
     dependent; no shipped entry is `chained` since D591 (`model_roles` read `model_mentions` this way before the
     two merged); `tests/test_annotate_inputs.py` exercises it on a stand-in.
   - `text_bearing`: payload paths holding text copied from the document (`quotes[].text`); releases cut them to
     an excerpt cap.
   - `produced_by=<other wf>` makes it a **record** of another workflow's call instead (as `doc_class` and
     `stack_relevance` are records of `screen`).
3. **This is a format change** (`docs/howto/change-family-format.md`). The registry order is the field order of the
   `annotations` block, so a new entry changes the `documents` and `corpus` formats (an `ADDITIVE` version each in
   `stack/schemas/upcasters.py`), and the new `<wf>` (and `<wf>_evidence`) families need their version 1; the
   registry test (`tests/test_family_format.py::test_registry_every_family_versioned`) prints the digests. Existing
   stores need nothing: older files read upcast, and the next writes are at the new version (OPERATE, "After a family format change").
4. **Module** `workflows/annotate/<wf>/main.py`:

   ```python
   from stack.annotate import Answer, AskContext, AskItem, OPTIONS, PromptPin, Workflow, run_annotate, SUBSTAGES, PURE_SUBSTAGES

   def prompt(pin, llm) -> PromptPin: ...          # the plan pin -> what is hashed into config_digest; no I/O
   def ask(item: AskItem, actx: AskContext) -> Answer:
       res = actx.client.complete(role=..., template=..., refs={...}, user=item.text, schema=..., license_class=item.license_class,
                                  prompt_name=actx.pin.name, basis_digest=item.basis_digest)
       return Answer(payload={...}, llm=res)       # status ok or empty
   WORKFLOW = Workflow("<wf>", ask=ask, prompt=prompt)
   def run(ctx): return run_annotate(ctx, WORKFLOW)
   ```

   The module's `OPTIONS` (here the imported set: `max_rows`, `workers`, the batch-mode keys) are the `options` keys
   its line may carry; the runner refuses any other key (D434), so add a key of your own to the set. The prompt
   template lives next to it (`<name>.md`; or a `prompts/<name>.md` holding system and user part, loaded with
   `stack.annotate.prompt_files`, as `model_roles`, `technique_roles` and `technique_staged` do); the client reserves every call in the
   ledger and caches schema-valid responses. A reply cut off at the output cap is retried once at twice the cap, then
   `failed_permanent[truncated]` (D273).
5. **Plan.** In `prompts:` a pin `annotate.<wf>: {name: <template name>, model: … | role: …}`; a stage line after
   `fold.corpus`:

   ```yaml
   - {stage: annotate.<wf>, prior: [<wf>, <wf>_evidence], reads: [stack_relevance],
      filter: "ann_verdict(annotations, 'stack_relevance') = 'relevant'", budget: {max_usd: 1}, options: {max_rows: 30, workers: 16}}
   ```

   `reads:` + `filter:` is a **scope**: which documents get annotated (here: screened relevant); it is not part of
   the basis, so a screen re-run that keeps a document in scope re-asks nothing. The loader orders lines so every
   read's producer runs first and refuses cycles. Add `<wf>` to `fold.build.annotations_folded` (a `chained`
   workflow's reads must be folded too). `configs/plans/ann-scratch.yaml` is the worked plan.
6. **Release.** The dataset release ships a kind only once `configs/releases/dataset/mapping.yaml` has it: a
   `fields:` entry `annotations.<wf>` with `keep`/`docs`, and a `meta.kind` enum value; capped and withheld payload
   paths are listed there too (`docs/howto/dataset-release.md`, "Changing the format"). The older release configs
   (`public-v1`, the plans' `fold.release`) drop payload text in `column_drops` (as
   `annotations.screen.payload.reasoning`) and a workflow they do not ship whole (`annotations.<wf>`, D705).
7. **Tests** against the fake model (`tests/fakes/llm.py`, `tests/fakes/annotate.py`); see
   `tests/test_annotate_model_roles.py` and `tests/test_annotate_inputs.py`. Then a small real run on a store copy
   with `max_rows` and a budget (D275).
8. **Eval** (optional): `stack eval` has frozen sets (`evals/<task>/<set>/`) for triage, screen, affiliation,
   model_roles (set `2026-10-17a`), technique_roles (`2026-10-13a`) and technique_staged (`2026-10-18c`, 120 documents
   on the 234-entry v0.9 taxonomy, D630/D632; earlier sets `2026-10-17a`/`17b`/`18a`/`18b`; `--pin mode=<mode>`);
   `stack eval model_mentions` scores model_roles' LLM-free matcher on the model_roles set. affiliation_orgs
   has no eval set: `tools/org_vocab.py` reports its coverage on a release corpus and draws precision samples;
   translation has none either (tens of documents a build; read its records by hand).

## Current state

`technique_staged` and `model_roles` are the production annotations since plan `batch-2026-10a` revision 7 (D710): on
the screen-relevant rows (`reads: [screen]`, `filter: screen_relevant`), technique_staged in mode `single` with the
gleaning turn, prompt set `technique-staged-v5-a1.1`, doc_cap 900,000, max_tokens 64,000 and
`taxonomy/techniques-1.0.0.yaml` (since revision 9 `techniques-1.1.0.yaml`, the same prompts, D738); model_roles with
`model-roles-v3b` and doc_cap 900,000 on `configs/models.yaml` (models 1.0.0, D707; since revision 9 models 1.1.0); both folded by `fold.build` (with `model_regexp_matches`) and in `require_complete`. Revision 7
started as a pilot (`max_rows: 1000` on both lines); the lines now set no `max_rows` (the full run, which carried the
pilot's answers).
The dataset release ships both (`public-v1` still drops them, D705). `technique_roles` runs only in
`ann-scratch.yaml` (D269). technique_staged's module defaults stay `technique-staged-v1` / `taxonomy-v0.9.yaml` (frozen eval
sets read them), so the plan pin is the switch. `doc_class` is a record of `screen-v12`, not an annotator of its own (D257).
