# Changelog: techniques

## 1.1.0 (2026-10-29)

- Structure only: 1.0.0's 234 techniques and 9 group nodes, field for field, in the catalogue envelope (schema
  `techniques-2.schema.json` on `../schemas/catalogue-0.schema.json`; [../README.md](../README.md#the-catalogue-envelope)).
  Converted mechanically from 1.0.0.
  - `version`, `meta` (`schema`, `status` `released`, `updated` 2026-10-24 = 1.0.0's release, `description` with
    what 1.0.0's `notes` said, `sources`) and one `entries` list: the techniques, then the group nodes, each in
    1.0.0's order.
  - `slug` is `id`. `node` says what an entry is: `technique` or `group` (`kind` stays the technique's kind).
    A technique's `status` (final, provisional) is its own, not `meta.status`.
  - `aliases` is a list of `{text, kind}` (`synonym`, `instance`, `legacy`; a group node has no `instance`), each
    kind's texts in 1.0.0's order. Group `children` keep their order.
- The annotation prompts built from 1.1.0 are byte for byte those built from 1.0.0 (the menu, the compact index,
  every sub-menu block, the lexical matcher's surfaces): the definitions are the text the model was shown.
- sha256 `1bf1c50b08b816b9dfcaca35ae56eb8c3834e990451fa0d17e050e9e00954cc1`.

## 1.0.0 (2026-10-24)

- First release; content identical to 0.9.1: same 234 techniques and 9 group nodes, field for field.
- Header only: `version` `'1.0.0'` (was `'0.9'`), `status` `released` (was `draft`), and the first comment line.
- Schema `techniques-1.schema.json`: `techniques-0.schema.json` with the version pattern for major 1.
- sha256 `9957124474b6ab2e3328ee0231132449110e67b52f4eb0f17e1a0c5860a64d8b`.

## 0.9.1 (date unknown)

- 234 techniques (137 intervention, 32 monitoring, 65 assessment) and 9 group nodes; no families or facets.
- Identical to the pipeline's pinned `taxonomy-v0.9.1.yaml`, sha256
  `09fa7705b7ba6a750eb158fb3808b0d25f4d84764522a055f8abd7b1fe1827db`.
- The `version` (`'0.9'`) and `status` (`draft`) fields inside the file will be corrected at 1.0.0.
- 141 of the 292 0.8 slugs survive as slugs.

## History, not published in this repository

- 0.8 (date unknown): 292 techniques (243 techniques, 49 variants), 17 families, 10 facets.
- 0.6 (2026-07-23): 292 techniques (243 techniques, 49 variants), 17 families, 15 facets.
