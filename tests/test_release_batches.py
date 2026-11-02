"""T-SCALE item 5: ``run_release`` applies the release rules per record batch; the release is the
same whatever the batch size (rows, order, dropped counts, shipped texts)."""

from __future__ import annotations

import json
from typing import Any

import pyarrow.parquet as pq
import pytest

import stack.fold.release as rel
from stack.fold.release import load_release_config, resolve_destination, run_release
from tests.test_fold_build import N, build, rid, world  # noqa: F401 - the fixture


@pytest.mark.parametrize("batch", [1, 3])
def test_release_batched_equals_one_batch(world: dict[str, Any], monkeypatch: pytest.MonkeyPatch,  # noqa: F811
                                          batch: int) -> None:
    store = world["store"]
    b = build(world)
    one = run_release(store, run_id=rid(30), inputs={"corpus": b.file_id}, release="public-v1",
                      dest=resolve_destination(store, load_release_config("public-v1"), tag="one"), allow_unbuilt=True)
    monkeypatch.setattr(rel, "RELEASE_BATCH_ROWS", batch)
    many = run_release(store, run_id=rid(31), inputs={"corpus": b.file_id}, release="public-v1",
                       dest=resolve_destination(store, load_release_config("public-v1"), tag="many"), allow_unbuilt=True)
    assert N > batch
    t1 = pq.read_table(one.corpus_path).replace_schema_metadata(None)
    t2 = pq.read_table(many.corpus_path).replace_schema_metadata(None)
    assert t1.equals(t2) and t1.num_rows == one.rows_out == many.rows_out
    assert one.dropped == many.dropped and list(one.dropped) == list(many.dropped)
    d1, d2 = (json.loads((r.path / "release.json").read_text()) for r in (one, many))
    run_keys = ("tag", "run_id", "file_id")  # file_id = <run_id>.release (D647)
    assert {k: v for k, v in d1.items() if k not in run_keys} == {k: v for k, v in d2.items() if k not in run_keys}
