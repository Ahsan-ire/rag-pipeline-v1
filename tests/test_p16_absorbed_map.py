"""Phase 16A-1 (l) [C] parts: scripts/absorbed_map.py on the synthetic sample corpus, fake index."""

import json

import pytest

from scripts import absorbed_map
from scripts.sample_corpus import build_sample_corpus


def _sample():
    from src.chunker import chunk_handbook, production_chunk_ids

    clean, pages, meta = build_sample_corpus()
    docs = chunk_handbook(clean, pages, meta)
    return clean, pages, meta, production_chunk_ids(docs), [d.metadata["section_number"] for d in docs]


def test_map_and_inventory_on_fake_index():
    clean, pages, meta, ids, sections = _sample()
    absorbed, inventory = absorbed_map.build(clean, pages, meta, ids, sections)
    assert absorbed["chunk_count"] == 16
    assert all(k in ids for k in absorbed["map"])
    assert absorbed["map_sha256"] == absorbed_map.map_sha256(absorbed["map"])
    assert inventory["map_sha256"] == absorbed["map_sha256"]
    assert set(inventory["sections"]) == set(sections)
    aliases = {s for v in absorbed["map"].values() for s in v}
    assert set(inventory["aliases"]) == aliases and aliases
    assert inventory["sections"] == sorted(inventory["sections"])
    # chunk ids and section numbers only: no chunk text anywhere in either file
    blob = json.dumps(absorbed) + json.dumps(inventory)
    assert clean[:40] not in blob


def test_key_absent_from_index_fails():
    clean, pages, meta, ids, sections = _sample()
    absorbed, _ = absorbed_map.build(clean, pages, meta, ids, sections)
    key = next(iter(absorbed["map"]))
    with pytest.raises(absorbed_map.AbsorbedMapError):
        absorbed_map.build(clean, pages, meta, [i for i in ids if i != key], sections)


def test_inventory_validates_with_schema_loader():
    from src.eval_schema import inventory_sections

    clean, pages, meta, ids, sections = _sample()
    _, inventory = absorbed_map.build(clean, pages, meta, ids, sections)
    covered = inventory_sections(inventory)
    assert set(sections) <= covered


def test_v5_scoring_never_reads_the_map(monkeypatch, tmp_path):
    """The v1/v5 path never consults the absorbed map (v6 only)."""
    import src.evaluator as ev
    from tests.p16_capture import PROVENANCE, FakeRetrieval

    def _boom():
        raise AssertionError("v5 path read the absorbed map")

    monkeypatch.setattr(ev, "_load_absorbed_map", _boom)
    retrieval = FakeRetrieval({})
    ev.run_eval_matrix(
        [("golden", "eval/sample_golden_set.jsonl")], results_path=str(tmp_path / "r.md"),
        retrieve_fn_factory=retrieval.factory(6), provenance_fn=lambda: dict(PROVENANCE),
        privacy="public", skip_refusals=True, skip_completeness=True,
    )
