"""Tests for the synthetic sample corpus and its index builder (Phase 11 / D40).

All offline: the corpus/chunking tests need no models at all, and the build test
injects a Chroma store over ``FakeEmbeddings`` so no MiniLM download happens. The
guard tests deliberately monkeypatch the writer to fail, proving the "never touch
the real ./chroma_db" guard fires before any store is constructed.
"""

import os
from pathlib import Path

import pytest

from src.bm25_index import build_bm25_index, load_bm25_index, search_bm25
from src.chunker import _page_string, chunk_handbook
from src.embedder import (
    EMBEDDING_MODEL,
    EMBEDDING_MODEL_MANIFEST,
    compute_chunk_id,
    get_vector_store,
)
from src.evaluator import load_golden_set
from tests.test_embedder import FakeEmbeddings

from scripts.build_sample_index import DEFAULT_SAMPLE_DIR, build_sample_index
from scripts.sample_corpus import (
    FRONT_MATTER_SENTINEL,
    POST_INDEX_SENTINEL,
    SOURCE,
    TITLE,
    build_sample_corpus,
)

# The frozen contract: the exact set of section numbers the corpus must chunk
# into. Any prose edit that trips a runt/oversize merge changes this and fails
# test_exact_section_set loudly (that is the point).
EXPECTED_SECTIONS = {
    "1.1", "1.2", "1.3", "1.3.1", "1.4",
    "2.1", "2.2", "2.2.1", "2.2.1.1", "2.3", "2.4",
    "3.1", "3.2", "3.2.1", "3.3", "APPENDIX 3.1",
}

GOLDEN_PATH = Path(__file__).resolve().parents[1] / "eval" / "sample_golden_set.jsonl"


@pytest.fixture(scope="module")
def corpus():
    """The ``(clean_text, page_map, metadata)`` triple, built once."""
    return build_sample_corpus()


@pytest.fixture(scope="module")
def docs(corpus):
    """The chunked sample corpus, built once (no embeddings)."""
    clean_text, page_map, metadata = corpus
    return chunk_handbook(clean_text, page_map, metadata)


@pytest.fixture(scope="module")
def by_section(docs):
    """Map section_number -> chunk (section numbers are unique here)."""
    return {d.metadata["section_number"]: d for d in docs}


# --- Corpus / page-map shape --------------------------------------------------

def test_page_map_invariant(corpus):
    """15 contiguous spans covering the whole clean text, first starting at 0."""
    clean_text, page_map, _ = corpus
    assert len(page_map) == 15
    assert page_map[0].char_start == 0
    assert page_map[-1].char_end == len(clean_text)
    for prev, nxt in zip(page_map, page_map[1:]):
        # Pages join with a single "\n", so the next span starts one char on.
        assert nxt.char_start == prev.char_end + 1
    # Each span's slice must round-trip to the authored page text.
    for span in page_map:
        assert clean_text[span.char_start:span.char_end]  # non-empty, in range


def test_front_matter_has_no_printed_page(corpus):
    """Page 1 is front matter with no printed page; body pages are numbered."""
    _, page_map, _ = corpus
    assert page_map[0].printed_page is None
    assert all(span.printed_page == span.page_number for span in page_map[1:])


def test_metadata_contract(docs):
    """Every chunk carries the synthetic (non-/data/) source and handbook type."""
    for d in docs:
        assert d.metadata["document_type"] == "handbook"
        assert d.metadata["source"] == SOURCE
        assert "/data/" not in d.metadata["source"]
        assert d.metadata["title"] == TITLE


# --- Chunking expectations ----------------------------------------------------

def test_exact_section_set(docs):
    """The corpus chunks into exactly the frozen 16-section set."""
    assert {d.metadata["section_number"] for d in docs} == EXPECTED_SECTIONS


def test_one_chunk_per_section(docs):
    """No oversize splits: one chunk per section number."""
    section_numbers = [d.metadata["section_number"] for d in docs]
    assert len(section_numbers) == len(set(section_numbers)) == 16


def test_exactly_one_appendix_chunk(by_section):
    """The appendix is first-class: exactly one, verbatim locator, with a page."""
    appendix = [s for s in by_section if s.startswith("APPENDIX")]
    assert appendix == ["APPENDIX 3.1"]
    chunk = by_section["APPENDIX 3.1"]
    assert chunk.metadata["heading"] == "Blackthorn Conditions of Sale"
    assert chunk.metadata["page_start"] is not None


def test_trap_line_stays_in_prose(by_section):
    """The 'Part I of the folio' false-positive trap is body text of 1.2, not a
    heading (the legislation-vs-handbook routing bug class)."""
    assert "Part I of the folio" in by_section["1.2"].page_content


def test_designed_runt_merge(docs, by_section):
    """2.4.1 is a sub-600 trailing runt: it merges into 2.4 and is not its own
    section, but its heading text survives inside 2.4's chunk."""
    assert "2.4.1" not in {d.metadata["section_number"] for d in docs}
    assert "Peppercorn Adjustment" in by_section["2.4"].page_content


def test_excluded_text_never_chunked(docs):
    """Front matter (before CHAPTER 1) and the post-INDEX tail are excluded."""
    joined = "\n".join(d.page_content for d in docs)
    assert FRONT_MATTER_SENTINEL not in joined
    assert POST_INDEX_SENTINEL not in joined


def test_page_citations(by_section):
    """A single-page section cites one page; the straddling 1.1 cites a range."""
    kestrel = by_section["1.4"]
    assert kestrel.metadata["page_start"] == kestrel.metadata["page_end"] == 5

    verdigris = by_section["1.1"]  # intro + 1.1 body straddle pages 2-3
    assert verdigris.metadata["page_start"] == 2
    assert verdigris.metadata["page_end"] == 3
    assert _page_string(2, 3) in verdigris.page_content  # "pp.2–3"


# --- Golden-set self-consistency ---------------------------------------------

def test_golden_set_shape():
    """Exactly 7 answerable questions (4 direct + 3 exact_token), no refusals,
    with the required appendix expectation."""
    golden = load_golden_set(str(GOLDEN_PATH))
    assert len(golden) == 7
    types = [q["type"] for q in golden]
    assert types.count("direct") == 4
    assert types.count("exact_token") == 3
    assert types.count("refusal") == 0
    all_expected = {s for q in golden for s in q["expected_sections"]}
    assert "APPENDIX 3.1" in all_expected


def test_golden_sections_exist_in_corpus(docs):
    """Every section the golden set expects is actually produced by the chunker."""
    built = {d.metadata["section_number"] for d in docs}
    golden = load_golden_set(str(GOLDEN_PATH))
    for q in golden:
        for section in q["expected_sections"]:
            assert section in built, f"golden expects {section!r}, not in corpus"


def test_golden_questions_retrievable_via_bm25(docs):
    """The platform-independent floor under the CI smoke: BM25 alone (pure Python,
    deterministic) ranks each expected section in its top 3, so vector-float
    jitter across macOS/ubuntu cannot flip a top-6 hybrid hit."""
    ids = [compute_chunk_id(SOURCE, d.page_content) for d in docs]
    index = build_bm25_index(ids, docs)
    section_by_id = {i: d.metadata["section_number"] for i, d in zip(ids, docs)}
    golden = load_golden_set(str(GOLDEN_PATH))
    for q in golden:
        results = search_bm25(index, q["question"], top_k=3, document_type="handbook")
        ranked = [section_by_id[r[0]] for r in results]
        assert q["expected_sections"][0] in ranked, (
            f"{q['expected_sections'][0]!r} not in BM25 top-3 {ranked} "
            f"for {q['question']!r}"
        )


# --- build_sample_index guard -------------------------------------------------

@pytest.mark.parametrize("target", ["./chroma_db", "chroma_db", "CHROMA_DB"])
def test_guard_refuses_real_chroma_db(target, monkeypatch):
    """The builder refuses any directory that resolves to the real corpus store,
    and does so BEFORE calling the writer (proved by making the writer fail)."""
    def _boom(*a, **k):
        pytest.fail("sync_documents was reached — the guard did not fire first")

    monkeypatch.setattr("scripts.build_sample_index.sync_documents", _boom)
    with pytest.raises(ValueError):
        build_sample_index(persist_dir=target)


def test_guard_refuses_tmp_chroma_db(tmp_path, monkeypatch):
    """A chroma_db anywhere on disk (not just cwd-relative) is refused."""
    monkeypatch.setattr(
        "scripts.build_sample_index.sync_documents",
        lambda *a, **k: pytest.fail("guard did not fire"),
    )
    with pytest.raises(ValueError):
        build_sample_index(persist_dir=str(tmp_path / "chroma_db"))


def test_guard_resolves_symlink_alias(tmp_path, monkeypatch):
    """A symlink pointing at a chroma_db directory is resolved and refused."""
    real = tmp_path / "chroma_db"
    real.mkdir()
    alias = tmp_path / "sneaky"
    os.symlink(real, alias)
    monkeypatch.setattr(
        "scripts.build_sample_index.sync_documents",
        lambda *a, **k: pytest.fail("guard did not fire"),
    )
    with pytest.raises(ValueError):
        build_sample_index(persist_dir=str(alias))


# --- build_sample_index end-to-end (offline, injected store) -----------------

def _fail_if_model_loaded(monkeypatch):
    """Make any attempt to build the real embedding function explode, so a test
    that "passes" cannot be silently loading MiniLM behind our backs."""
    def _boom():
        raise AssertionError("real embedding model was loaded; injection bypassed")

    monkeypatch.setattr("src.embedder.get_embedding_function", _boom)


def test_build_sample_index_offline(tmp_path, monkeypatch):
    """Injecting a FakeEmbeddings store builds 16 chunks and writes both sidecars,
    with no model download."""
    _fail_if_model_loaded(monkeypatch)
    persist = str(tmp_path / "sample_chroma")
    store = get_vector_store(
        embedding_function=FakeEmbeddings(), persist_directory=persist
    )

    counts = build_sample_index(persist_dir=persist, vector_store=store)
    assert counts["added"] == 16
    assert counts["chunks"] == 16

    manifest = Path(persist) / EMBEDDING_MODEL_MANIFEST
    assert manifest.read_text().strip() == EMBEDDING_MODEL

    bm25 = load_bm25_index(persist)
    assert bm25 is not None
    assert len(bm25.ids) == 16
    assert len(bm25.documents) == 16


def test_build_sample_index_idempotent(tmp_path, monkeypatch):
    """A second build against the same store is a pure no-op (converged)."""
    _fail_if_model_loaded(monkeypatch)
    persist = str(tmp_path / "sample_chroma")
    store = get_vector_store(
        embedding_function=FakeEmbeddings(), persist_directory=persist
    )

    first = build_sample_index(persist_dir=persist, vector_store=store)
    assert first["added"] == 16

    second = build_sample_index(persist_dir=persist, vector_store=store)
    assert second["added"] == 0
    assert second["updated"] == 0
    assert second["deleted"] == 0

    stored = store.get(where={"source": SOURCE})
    assert len(stored["ids"]) == 16


def test_default_sample_dir_is_not_chroma_db():
    """Sanity: the default persist dir is the gitignored sample dir, never real."""
    assert DEFAULT_SAMPLE_DIR == "./sample_chroma_db"
    assert os.path.basename(DEFAULT_SAMPLE_DIR).casefold() != "chroma_db"
