"""Phase 16A-2a (j), `scripts/handbook_sections.py` on a synthetic chunk store (a8, b0).

Chunks are invented ``Document`` lists or the real chunker run over the wholly
synthetic sample corpus (``scripts/sample_corpus.py``). Nothing here reads
`data/` or a Chroma index.
"""

import json
import os

import pytest
from langchain_core.documents import Document

from scripts import absorbed_map, handbook_sections
from scripts.sample_corpus import build_sample_corpus

CANARY = "P16-CANARY"


def _chunk(label, text, start=None, end=None):
    return Document(page_content=text, metadata={"section_number": label, "page_start": start, "page_end": end})


def _inventory(sections, aliases=()):
    return {"version": 1, "map_sha256": "a" * 64, "sections": sorted(sections), "aliases": sorted(aliases)}


def test_groups_chunks_by_section_in_order_with_page_span():
    chunks = [
        _chunk("", f"{CANARY} chapter intro", 1, 1),                 # no label: not dumped
        _chunk("1.1", "first A", 5, 5),
        _chunk("1.2", "second", 6, 7),
        _chunk("1.1", "first B", 5, 6),                              # same label, later chunk
        _chunk("1.3", "no pages", None, None),
        _chunk("1.4", "not inventoried", 9, 9),                      # label outside the inventory
        _chunk("1.2", "second B", None, 9),                          # a None page is ignored
    ]
    rows = handbook_sections.build_rows(chunks, _inventory(["1.1", "1.2", "1.3"], aliases=["1.1.1"]))
    assert [r["label"] for r in rows] == ["1.1", "1.2", "1.3"]       # first-appearance order
    assert rows[0] == {"label": "1.1", "page_start": 5, "page_end": 6, "text": "first A\n\nfirst B"}
    assert rows[1] == {"label": "1.2", "page_start": 6, "page_end": 9, "text": "second\n\nsecond B"}
    assert rows[2] == {"label": "1.3", "page_start": None, "page_end": None, "text": "no pages"}


def test_missing_inventoried_sections_are_listed_by_label_only():
    chunks = [_chunk("1.1", f"{CANARY} body text", 1, 1)]
    with pytest.raises(handbook_sections.MissingSectionsError) as info:
        handbook_sections.build_rows(chunks, _inventory(["1.1", "2.5", "1.9.3"]))
    assert info.value.missing == ["1.9.3", "2.5"]
    assert "1.9.3" in str(info.value) and "2.5" in str(info.value)
    assert CANARY not in str(info.value)
    assert len(handbook_sections.build_rows(chunks, _inventory(["1.1"]))) == 1  # control: the success path builds


def test_aliases_and_ancestors_are_not_required():
    chunks = [_chunk("1.7.1", "child text", 3, 3)]
    inv = _inventory(["1.7.1"], aliases=["1.7.1.1", "1.7.2"])  # 1.7 is a dotted ancestor too
    assert [r["label"] for r in handbook_sections.build_rows(chunks, inv)] == ["1.7.1"]


def test_malformed_inventory_is_refused():
    from src.eval_schema import InventoryError

    with pytest.raises(InventoryError):
        handbook_sections.build_rows([_chunk("1.1", "x", 1, 1)], {"sections": ["1.1"]})


def test_dump_is_jsonl_with_exactly_the_four_keys():
    rows = handbook_sections.build_rows([_chunk("1.1", f"{CANARY} text é", 2, 3)], _inventory(["1.1"]))
    lines = handbook_sections.dump_rows(rows).splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert set(parsed) == {"label", "page_start", "page_end", "text"}
    assert parsed["text"] == f"{CANARY} text é" and isinstance(parsed["page_start"], int)


# --- the real chunker over the synthetic sample corpus ----------------------------
def _sample_chunks_and_inventory():
    from src.chunker import chunk_handbook, production_chunk_ids

    clean, pages, meta = build_sample_corpus()
    chunks = chunk_handbook(clean, pages, meta)
    _, inventory = absorbed_map.build(
        clean, pages, meta, production_chunk_ids(chunks), [c.metadata["section_number"] for c in chunks]
    )
    return chunks, inventory


def test_sample_corpus_yields_every_inventoried_section():
    chunks, inventory = _sample_chunks_and_inventory()
    rows = handbook_sections.build_rows(chunks, inventory)
    assert {r["label"] for r in rows} == set(inventory["sections"]) and rows
    assert all(isinstance(r["page_start"], int) and r["page_start"] <= r["page_end"] for r in rows)
    by_label = {r["label"]: r for r in rows}
    for chunk in chunks:  # each section's text holds every one of its chunks, verbatim
        label = chunk.metadata["section_number"]
        if label in by_label:
            assert chunk.page_content in by_label[label]["text"]


def test_cli_writes_the_dump_and_fails_on_a_missing_label(tmp_path, capsys):
    _, inventory = _sample_chunks_and_inventory()
    inv_path = tmp_path / "inventory.json"
    inv_path.write_text(json.dumps(inventory), encoding="utf-8")
    out = tmp_path / "dump" / "handbook_sections.jsonl"
    assert handbook_sections.main(["--sample", "--inventory", str(inv_path), "--out", str(out)]) == 0
    lines = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert sorted(r["label"] for r in lines) == inventory["sections"]
    assert all(set(r) == {"label", "page_start", "page_end", "text"} for r in lines)
    capsys.readouterr()

    inventory["sections"] = sorted(inventory["sections"] + ["98.7.6"])
    inv_path.write_text(json.dumps(inventory), encoding="utf-8")
    out2 = tmp_path / "second.jsonl"
    assert handbook_sections.main(["--sample", "--inventory", str(inv_path), "--out", str(out2)]) == 1
    captured = capsys.readouterr()
    assert "98.7.6" in captured.err
    sample_text = lines[0]["text"]
    assert sample_text[:40] not in captured.out + captured.err  # labels only, never text
    assert not out2.exists()


def test_cli_inventory_problems_and_argument_errors(tmp_path):
    bad = tmp_path / "inventory.json"
    bad.write_text("{}", encoding="utf-8")
    assert handbook_sections.main(["--sample", "--inventory", str(bad), "--out", str(tmp_path / "o.jsonl")]) == 2
    assert handbook_sections.main(["--sample", "--inventory", str(tmp_path / "absent.json"), "--out", str(tmp_path / "o.jsonl")]) == 2
    with pytest.raises(SystemExit):
        handbook_sections.main(["--out", str(tmp_path / "o.jsonl")])  # neither a PDF nor --sample


def test_output_inside_the_repo_must_be_under_data():
    repo = handbook_sections.REPO
    assert handbook_sections._out_allowed(os.path.join(repo, "data", "research", "handbook_sections.jsonl"))
    assert handbook_sections._out_allowed(handbook_sections.DEFAULT_OUT)
    assert not handbook_sections._out_allowed(os.path.join(repo, "eval", "handbook_sections.jsonl"))
    assert not handbook_sections._out_allowed(os.path.join(repo, "handbook_sections.jsonl"))
    assert handbook_sections._out_allowed("/tmp/anywhere/outside/the/repo.jsonl")
    assert handbook_sections.main(["--sample", "--out", os.path.join(repo, "eval", "a8b_refused.jsonl")]) == 2
    assert not os.path.exists(os.path.join(repo, "eval", "a8b_refused.jsonl"))
