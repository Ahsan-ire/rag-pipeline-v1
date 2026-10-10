"""Phase 16A-1 item 7 [W]: the chunker's absorption side channel (acceptance (l)).

All synthetic and offline: invented handbook-style text built into the same
``(clean_text, page_map, metadata)`` contract ``ingest.extract_pdf`` produces,
plus the committed synthetic sample corpus (scripts/sample_corpus.py). No model,
no Chroma, no network.

What is proven here:

* a runt merge (intro adoption, descendant forward, trailing backward, chained)
  and an appendix stub are recorded and map to the absorbing chunk;
* across an oversize re-split only a sub-chunk wholly containing the absorbed
  span is credited; a span split across sub-chunks credits none; a sub-chunk
  produced by the find-miss path is credited nothing;
* chunk output is byte-identical with and without the side channel, AND equal
  to digests pinned from the pre-16A chunker (commit 7e59e29) — several
  synthetic documents including oversize re-splits, a forced find-miss, and the
  16-chunk sample corpus.
"""

import hashlib
import json

import pytest
from langchain_text_splitters import RecursiveCharacterTextSplitter

from scripts.sample_corpus import build_sample_corpus
from src.chunker import (
    KIND_APPENDIX_STUB,
    KIND_RUNT_MERGE,
    ORIGIN_FIND_MISS,
    ORIGIN_OVERSIZE_SPLIT,
    ORIGIN_SEGMENT,
    AbsorptionLog,
    absorbed_sections_by_chunk,
    chunk_handbook,
    chunk_handbook_with_absorption,
    production_chunk_ids,
)
from src.embedder import compute_chunk_id
from src.ingest import PageSpan


# --- Synthetic fixtures -------------------------------------------------------

def _clauses(n: int, tag: str) -> str:
    """``n`` distinct ~79-char clauses (non-repeating, so sub-chunk location is
    unambiguous — repeated phrasing would be a fixture artefact)."""
    return "".join(
        f"Clause {tag}{i} in this part sets out a rule of conveyancing practice in detail. "
        for i in range(n)
    )


def _handbook(pages, title: str = "Synthetic_Handbook.pdf"):
    """Build ``(clean_text, page_map, metadata)``; printed page = raw index + 10."""
    parts, page_map, pos = [], [], 0
    for i, page in enumerate(pages, start=1):
        text, printed = page if isinstance(page, tuple) else (page, i + 10)
        start = pos
        parts.append(text)
        pos += len(text)
        page_map.append(PageSpan(i, printed, start, pos))
        if i < len(pages):
            parts.append("\n")
            pos += 1
    metadata = {
        "source": "/synthetic/" + title,
        "title": title,
        "document_type": "handbook",
        "date": "",
    }
    return "".join(parts), page_map, metadata


def doc_merges():
    """Intro adoption x2, a descendant runt merge (3.2 <- 3.2.1), an appendix stub
    (3.3 <- APPENDIX 3.1), a trailing runt (3.4 <- 3.5) and a sibling runt (4.2)
    that stays standalone. No oversize segment."""
    return _handbook([
        "Front matter that precedes the first chapter marker.",
        "CHAPTER 3\nSALE AGREEMENTS\nA short chapter note.\n"
        f"3.1 Scope of the agreement\n{_clauses(12, 'a')}\n"
        f"3.2 Deposits\n{_clauses(1, 'b')}\n"
        f"3.2.1 Holding the deposit\n{_clauses(10, 'c')}\n",
        f"3.3 Completion\n{_clauses(10, 'd')}\n"
        "APPENDIX 3.1 Form of Notice\n"
        f"3.4 Requisitions\n{_clauses(10, 'e')}\n"
        f"3.5 Closing note\n{_clauses(2, 'f')}",
        "CHAPTER 4\nTITLE MATTERS\n"
        f"4.1 Root of title\n{_clauses(10, 'g')}\n"
        f"4.2 Searches short\n{_clauses(2, 'h')}\n"
        f"4.3 Sibling section\n{_clauses(10, 'i')}",
    ])


def doc_oversize_tail():
    """5.1 (oversize) absorbs an appendix stub and a trailing runt 5.2; the
    composite re-splits into three sub-chunks. 5.2 sits wholly in the last
    one; the stub sits in the overlap of the last two."""
    return _handbook([
        "CHAPTER 5\nOVERSIZE MATTERS\n"
        f"5.1 Big section with a tail\n{_clauses(30, 'p')}",
        f"{_clauses(30, 'pp')}\n"
        "APPENDIX 5.1 Site Plan\n"
        f"5.2 Short tail\n{_clauses(3, 'q')}",
    ])


def doc_split_across():
    """Runt 6.1 absorbs its long child 6.1.1; the composite re-splits and 6.1.1's
    own span runs across every sub-chunk."""
    return _handbook([
        "CHAPTER 6\nSPLIT MATTERS\n"
        f"6.1 Parent heading\n{_clauses(2, 'r')}\n"
        f"6.1.1 Long child\n{_clauses(40, 's')}",
        f"{_clauses(30, 'ss')}\n"
        f"6.2 Sibling\n{_clauses(10, 't')}",
    ])


def doc_chain():
    """Chained merges: 7.2 absorbs 7.2.1 forward, then (still a runt, last in its
    chapter) is absorbed backward by 7.1 — both labels resolve to 7.1."""
    return _handbook([
        "CHAPTER 7\nCHAIN MATTERS\n"
        f"7.1 Substantial section\n{_clauses(10, 'u')}\n"
        f"7.2 Parent runt\n{_clauses(1, 'v')}\n"
        f"7.2.1 Child runt\n{_clauses(2, 'w')}",
        "CHAPTER 8\nNEXT MATTERS\n"
        f"8.1 Next chapter section\n{_clauses(10, 'x')}",
    ])


def _digest(docs) -> str:
    """sha256 over every chunk's exact text and metadata, in order."""
    payload = [
        [d.page_content, sorted(d.metadata.items(), key=lambda kv: kv[0])] for d in docs
    ]
    blob = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


# Digests of chunk_handbook's output computed on the PRE-16A chunker (commit
# 7e59e29, before any side-channel code existed). Equality proves the chunks'
# text, metadata and order — and so their ids — are unchanged by this item.
PINNED = {
    "merges": "ea1aa2eccdc43cf446f602e33f6aa7a913d7f6517f73a9e590342b744a40f0a2",
    "oversize": "8c6b45903c8938f1f2a5c47a423d4a6d2f9065f053d46d9cf478ffe2fa3bafe3",
    "split": "f566d95b0b9bc6430649eedd648abf43d97ee35c9f5e88f8e1132e3ceeaba5c4",
    "chain": "f6bbc65de83539e1f1cbf822a9adb146532c5caa8b66c7e41d70439a8b988d6c",
    "sample": "c277961f3b419afe5282e3c243525fdc2c3e8a0a374d0918318501ba275333e7",
    "findmiss": "04059bf0d0f3b8048e92525cae6f2fa61f9983f8fe71c15e9826eac40e880f68",
}

BUILDERS = {
    "merges": doc_merges,
    "oversize": doc_oversize_tail,
    "split": doc_split_across,
    "chain": doc_chain,
    "sample": build_sample_corpus,
}


def _run(builder):
    clean_text, page_map, metadata = builder()
    docs, log = chunk_handbook_with_absorption(clean_text, page_map, metadata)
    return clean_text, docs, log


def _map_by_section(docs, log):
    """The alias map keyed by production id, re-keyed by (index, section) for
    readable assertions."""
    ids = production_chunk_ids(docs)
    by_id = absorbed_sections_by_chunk(log, ids)
    return {
        (i, d.metadata["section_number"]): by_id[cid]
        for i, (d, cid) in enumerate(zip(docs, ids))
        if cid in by_id
    }


@pytest.fixture
def force_find_miss(monkeypatch):
    """Make the oversize splitter emit a last piece that is not a verbatim
    substring of clean_text, driving src/chunker.py's find-miss branch."""
    original = RecursiveCharacterTextSplitter.split_text

    def patched(self, text):
        pieces = original(self, text)
        return pieces[:-1] + ["UNLOCATABLE " + pieces[-1]]

    monkeypatch.setattr(RecursiveCharacterTextSplitter, "split_text", patched)


# --- Byte identity ------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(BUILDERS))
def test_chunks_byte_identical_with_and_without_side_channel(name):
    clean_text, page_map, metadata = BUILDERS[name]()
    plain = chunk_handbook(clean_text, page_map, metadata)
    logged, _ = chunk_handbook_with_absorption(clean_text, page_map, metadata)
    assert [(d.page_content, d.metadata) for d in plain] == [
        (d.page_content, d.metadata) for d in logged
    ]
    assert production_chunk_ids(plain) == production_chunk_ids(logged)
    assert _digest(plain) == _digest(logged) == PINNED[name]


def test_find_miss_path_byte_identical(force_find_miss):
    clean_text, page_map, metadata = doc_oversize_tail()
    plain = chunk_handbook(clean_text, page_map, metadata)
    logged, log = chunk_handbook_with_absorption(clean_text, page_map, metadata)
    assert _digest(plain) == _digest(logged) == PINNED["findmiss"]
    assert [c.origin for c in log.chunks][-1] == ORIGIN_FIND_MISS


def test_sample_corpus_is_the_16_chunk_corpus_and_unchanged():
    _, docs, log = _run(build_sample_corpus)
    assert len(docs) == 16
    assert _digest(docs) == PINNED["sample"]
    assert len(log.chunks) == 16


def test_chunk_handbook_still_returns_a_plain_list():
    clean_text, page_map, metadata = doc_merges()
    assert isinstance(chunk_handbook(clean_text, page_map, metadata), list)


# --- Chunk spans --------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(BUILDERS))
def test_chunk_spans_are_aligned_and_locate_each_body(name):
    clean_text, docs, log = _run(BUILDERS[name])
    assert [c.chunk_index for c in log.chunks] == list(range(len(docs)))
    for doc, span in zip(docs, log.chunks):
        assert span.section == doc.metadata["section_number"]
        assert span.origin in (ORIGIN_SEGMENT, ORIGIN_OVERSIZE_SPLIT)
        body = clean_text[span.start:span.end]
        # The body is the chunk's text after its "[...] " citation prefix.
        assert body and doc.page_content.endswith("] " + body)


def test_oversize_sub_chunks_share_one_segment():
    _, docs, log = _run(doc_oversize_tail)
    assert [c.origin for c in log.chunks] == [ORIGIN_OVERSIZE_SPLIT] * 3
    assert {c.segment_index for c in log.chunks} == {0}


# --- Absorption records -------------------------------------------------------

def _labelled(log):
    """(kind, rule, absorbed, absorbing) per record, label-bearing ones sorted."""
    return sorted(
        (a.kind, a.rule, a.absorbed_section, a.absorbing_section)
        for a in log.absorptions
    )


def test_merges_records_every_absorption():
    clean_text, docs, log = _run(doc_merges)
    assert [d.metadata["section_number"] for d in docs] == [
        "3.1", "3.2", "3.3", "3.4", "4.1", "4.2", "4.3",
    ]
    assert _labelled(log) == [
        (KIND_APPENDIX_STUB, "appendix_backward", "APPENDIX 3.1", "3.3"),
        (KIND_RUNT_MERGE, "descendant_forward", "3.2.1", "3.2"),
        (KIND_RUNT_MERGE, "intro_adopt", "", "3.1"),
        (KIND_RUNT_MERGE, "intro_adopt", "", "4.1"),
        (KIND_RUNT_MERGE, "trailing_backward", "3.5", "3.4"),
    ]
    spans = {a.absorbed_section: clean_text[a.start:a.end] for a in log.absorptions}
    # The span is the absorbed label's OWN text, trimmed of whitespace.
    assert spans["APPENDIX 3.1"] == "APPENDIX 3.1 Form of Notice"
    assert spans["3.2.1"].startswith("3.2.1 Holding the deposit\nClause c0 ")
    assert spans["3.2.1"].endswith(
        "Clause c9 in this part sets out a rule of conveyancing practice in detail."
    )
    assert spans["3.5"].startswith("3.5 Closing note\n") and "Clause f1 " in spans["3.5"]
    assert spans["3.5"] == spans["3.5"].strip()


def test_merges_map_names_the_absorbing_chunk():
    _, docs, log = _run(doc_merges)
    assert _map_by_section(docs, log) == {
        (1, "3.2"): ["3.2.1"],
        (2, "3.3"): ["APPENDIX 3.1"],
        (3, "3.4"): ["3.5"],
    }


def test_sibling_runt_is_not_an_absorption():
    _, docs, log = _run(doc_merges)
    assert "4.2" in [d.metadata["section_number"] for d in docs]
    assert all(a.absorbed_section != "4.2" for a in log.absorptions)


def test_chained_merges_resolve_to_the_final_absorber():
    clean_text, docs, log = _run(doc_chain)
    assert [d.metadata["section_number"] for d in docs] == ["7.1", "8.1"]
    by_label = {a.absorbed_section: a for a in log.absorptions if a.absorbed_section}
    assert by_label["7.2.1"].rule == "descendant_forward"
    assert by_label["7.2"].rule == "trailing_backward"
    assert by_label["7.2.1"].absorbing_section == by_label["7.2"].absorbing_section == "7.1"
    # 7.2's own span excludes the 7.2.1 text it had absorbed first.
    assert "7.2.1" not in clean_text[by_label["7.2"].start:by_label["7.2"].end]
    assert _map_by_section(docs, log) == {(0, "7.1"): ["7.2", "7.2.1"]}


def test_sample_corpus_map():
    _, docs, log = _run(build_sample_corpus)
    assert _map_by_section(docs, log) == {(10, "2.4"): ["2.4.1"]}


# --- Oversize crediting -------------------------------------------------------

def test_only_containing_oversize_sub_chunk_is_credited():
    _, docs, log = _run(doc_oversize_tail)
    assert _map_by_section(docs, log) == {
        # The stub's span lies in the splitter overlap, i.e. wholly inside BOTH
        # of the last two sub-chunks — each one contains it, so each is credited.
        (1, "5.1"): ["APPENDIX 5.1"],
        # The trailing runt 5.2 lies only in the last sub-chunk.
        (2, "5.1"): ["5.2", "APPENDIX 5.1"],
    }


def test_span_split_across_sub_chunks_credits_none():
    _, docs, log = _run(doc_split_across)
    assert [a.absorbed_section for a in log.absorptions if a.absorbed_section] == ["6.1.1"]
    assert sum(1 for d in docs if d.metadata["section_number"] == "6.1") == 4
    assert _map_by_section(docs, log) == {}


def test_find_miss_sub_chunk_gets_none(force_find_miss):
    clean_text, page_map, metadata = doc_oversize_tail()
    docs, log = chunk_handbook_with_absorption(clean_text, page_map, metadata)
    miss = log.chunks[-1]
    assert miss.origin == ORIGIN_FIND_MISS and miss.start is None and miss.end is None
    # The find-miss body still holds 5.2's text verbatim — it is the position
    # that is unknown, so it earns no credit; the overlap sibling keeps the stub.
    assert "5.2 Short tail" in docs[-1].page_content
    assert _map_by_section(docs, log) == {(1, "5.1"): ["APPENDIX 5.1"]}


# --- Map function contract ----------------------------------------------------

def test_map_is_keyed_by_the_production_chunk_id():
    _, docs, log = _run(doc_merges)
    expected = [compute_chunk_id(d.metadata["source"], d.page_content) for d in docs]
    assert production_chunk_ids(docs) == expected
    by_id = absorbed_sections_by_chunk(log, expected)
    assert by_id == {
        expected[1]: ["3.2.1"],
        expected[2]: ["APPENDIX 3.1"],
        expected[3]: ["3.5"],
    }


def test_shared_ids_pool_and_output_is_sorted_and_deduped():
    _, docs, log = _run(doc_merges)
    ids = ["same"] * len(docs)
    assert absorbed_sections_by_chunk(log, ids) == {"same": ["3.2.1", "3.5", "APPENDIX 3.1"]}


def test_misaligned_ids_are_refused():
    _, docs, log = _run(doc_merges)
    with pytest.raises(ValueError):
        absorbed_sections_by_chunk(log, ["x"] * (len(docs) - 1))


def test_a_log_records_one_run_only():
    clean_text, page_map, metadata = doc_merges()
    _, log = chunk_handbook_with_absorption(clean_text, page_map, metadata)
    assert isinstance(log, AbsorptionLog)
    with pytest.raises(RuntimeError):
        log._begin(clean_text, [])
