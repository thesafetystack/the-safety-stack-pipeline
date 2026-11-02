"""``gather`` (§3.1, §6): document identity from all ``sources`` chains and the ``documents`` head;
writes a **full** ``documents`` file (D3). The stage entry is :mod:`workflows.gather.main`.

Modules, in data-flow order:

* :mod:`~workflows.gather.normalise` — one observation per ``source_id`` across the ``sources``
  chains, its merging keys per channel group (``channels.yaml``), observation stats;
* :mod:`~workflows.gather.overrides` — the ``identity`` overrides (``merge``, ``separate``,
  ``mask``; D74) as the operations ``union`` and ``main`` apply;
* :mod:`~workflows.gather.union` — connected components over (source, key) edges within a
  channel group, plus human ``merge`` edges; the spanning edge of each member;
* :mod:`~workflows.gather.anchor` — the anchor (earliest observed, masked included) and
  continuity against the ``documents`` head (merge, split, revive; ``former_doc_ids``,
  ``merged_into``);
* :mod:`~workflows.gather.merge` — the ``metadata`` merge over members (``fields`` overrides >
  provider rank > newest ``observed_at``, ``empty_is_absent``, author union);
* :mod:`~workflows.gather.carry` — ``content``/``annotations`` of a row: the head row's, plus
  D6's carry of a merged row's content and a split row's per-member entries;
* :mod:`~workflows.gather.main` — ``run(ctx)``: substages ``union``, ``merge``, ``carry`` (D32,
  all pure), row assembly and the byte-for-byte carry of unchanged rows (C6).
"""
