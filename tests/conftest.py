"""Shared test fixtures for the legal RAG pipeline."""

import pytest
from langchain_core.documents import Document


@pytest.fixture(autouse=True)
def _no_live_api_key(monkeypatch):
    """Scrub environment leaked from a developer's .env for every test.

    `load_dotenv()` runs at import in src.generator (and src.query_rewrite),
    so on a keyed dev box os.environ carries a live ANTHROPIC_API_KEY even
    though CI is keyless — an unpatched live seam would silently make a real
    API call locally while passing green in CI. Deleting the key at test
    setup makes such a seam fail loudly instead (get_llm/get_rewrite_llm
    raise ValueError). Tests that need a key set a fake one explicitly.

    AUDIT_LOG_RAW_QUERIES is scrubbed for the same reason: it is a
    behaviour-changing opt-in that a dev box may set in .env (field-test
    query capture), and tests assert the default (off) audit event shape.
    Tests that exercise the opt-in set it explicitly via monkeypatch.

    EMBEDDING_MODEL is exported into the shell during a bake-off arm run: a
    leaked value would point spec/prompt assertions at the wrong model.
    ALLOW_CHUNK_TRUNCATION belonged to the index-time truncation guard (Phase
    15 WS3, reserved D56), which was designed but NOT landed — mooted by the
    D57 no-swap outcome (disposition in D59) — so nothing reads it today. The
    scrub is kept deliberately so a future guard cannot inherit shell state.
    Tests that exercise either set them explicitly via monkeypatch.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("AUDIT_LOG_RAW_QUERIES", raising=False)
    monkeypatch.delenv("EMBEDDING_MODEL", raising=False)
    monkeypatch.delenv("ALLOW_CHUNK_TRUNCATION", raising=False)


@pytest.fixture
def sample_document():
    """A sample legal document for testing."""
    return Document(
        page_content="""PART I
PRELIMINARY

Section 1.
This Act may be cited as the Succession Act, 1965.

Section 2.
In this Act, unless the context otherwise requires—
"the court" means the High Court;
"personal representative" means an executor or administrator for the time being of a deceased person.

PART II
WILLS

Section 77.
(1) Every person who has attained the age of eighteen years or is or has been married may make a valid will.
(2) No will made by a person who has not attained the age of eighteen years shall be valid.

Section 78.
A will shall be in writing and shall be executed in the following manner.""",
        metadata={
            "source": "/test/succession_act_1965.pdf",
            "title": "Succession Act 1965",
            "document_type": "legislation",
            "date": "1965",
        },
    )


@pytest.fixture
def sample_chunks():
    """Pre-chunked documents for testing."""
    return [
        Document(
            page_content="[From: Succession Act 1965, Section 77] Every person who has attained the age of eighteen years may make a valid will.",
            metadata={
                "source": "/test/succession_act_1965.pdf",
                "title": "Succession Act 1965",
                "document_type": "legislation",
                "section_number": "77",
                "parent_section": "PART II",
            },
        ),
        Document(
            page_content="[From: Succession Act 1965, Section 78] A will shall be in writing and shall be executed in the following manner.",
            metadata={
                "source": "/test/succession_act_1965.pdf",
                "title": "Succession Act 1965",
                "document_type": "legislation",
                "section_number": "78",
                "parent_section": "PART II",
            },
        ),
    ]


@pytest.fixture
def mock_retrieved_results(sample_chunks):
    """Mock retriever output."""
    return [
        {"document": sample_chunks[0], "score": 0.85, "metadata": sample_chunks[0].metadata},
        {"document": sample_chunks[1], "score": 0.72, "metadata": sample_chunks[1].metadata},
    ]


@pytest.fixture
def handbook_chunks():
    """Pre-chunked handbook documents, carrying the D21 in-text prefix and the
    Phase 2 metadata keys (section_number, page_start, page_end). The second
    chunk spans two pages to exercise the range-rendering path."""
    return [
        Document(
            page_content=(
                "[Conveyancing Handbook, Ch.14 Registration Of Title, para 14.8.5, p.412] "
                "The priority entry protects a purchaser pending completion of the sale."
            ),
            metadata={
                "source": "Conveyancing_Handbook.pdf",
                "title": "Conveyancing_Handbook.pdf",
                "document_type": "handbook",
                "section_number": "14.8.5",
                "page_start": 412,
                "page_end": 412,
            },
        ),
        Document(
            page_content=(
                "[Conveyancing Handbook, Ch.1 Introduction, para 1.2, pp.1–2] "
                "The objectives of the manual are set out for the practitioner."
            ),
            metadata={
                "source": "Conveyancing_Handbook.pdf",
                "title": "Conveyancing_Handbook.pdf",
                "document_type": "handbook",
                "section_number": "1.2",
                "page_start": 1,
                "page_end": 2,
            },
        ),
    ]


@pytest.fixture
def handbook_retrieved_results(handbook_chunks):
    """Mock retriever output for handbook chunks (fused RRF scores)."""
    return [
        {"document": handbook_chunks[0], "score": 0.03279, "metadata": handbook_chunks[0].metadata},
        {"document": handbook_chunks[1], "score": 0.01639, "metadata": handbook_chunks[1].metadata},
    ]


# ---------------------------------------------------------------------------
# Phase 16A-1 privacy plumbing (D65)
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _private_root_in_tmp(tmp_path, monkeypatch):
    """Every test's private root is ``tmp_path/eval/private``, never the repo's.

    ``src.eval_privacy.private_root()`` is the only way code finds the private
    root; this is the only place it moves (spec item 1).
    """
    root = tmp_path / "eval" / "private"
    monkeypatch.setattr("src.eval_privacy.private_root", lambda: root)
    return root


@pytest.fixture
def eval_registry(monkeypatch):
    """Register tmp eval sets for one test by patching the registry loader.

    Usage: ``eval_registry.add(path)`` registers ``path`` as public at its
    current sha256 (``privacy="private"`` for a private set). The committed
    ``eval/sets.json`` entries stay registered alongside.
    """
    import src.eval_sets as eval_sets

    real_loader = eval_sets.load_registry
    extra = []

    class _Registry:
        def add(self, path, *, privacy="public", role="fixture", name=None):
            from pathlib import Path

            p = Path(path).resolve()
            extra.append(
                eval_sets.SetEntry(
                    name=name or f"tmp-{len(extra)}-{p.name}",
                    path=str(p),
                    privacy=privacy,
                    role=role,
                    status="active",
                    sha256=eval_sets.sha256_file(p),
                )
            )
            return extra[-1]

        def add_tree(self, root):
            """Register every ``*.jsonl`` under ``root`` as public, re-scanned at
            each registry load (so a set written later in the test, at its final
            bytes, is registered too)."""
            trees.append(root)

        def clear(self):
            extra.clear()
            trees.clear()

    trees = []

    def _loader():
        from pathlib import Path

        dynamic = []
        for root in trees:
            for p in sorted(Path(root).rglob("*.jsonl")):
                if p.is_file() and not p.is_symlink() and not eval_sets.has_sealed_marker(p):
                    dynamic.append(
                        eval_sets.SetEntry(
                            name=f"tree-{len(dynamic)}-{p.name}", path=str(p.resolve()),
                            privacy="public", role="fixture", status="active",
                            sha256=eval_sets.sha256_file(p),
                        )
                    )
        return [*real_loader(), *extra, *dynamic]

    monkeypatch.setattr(eval_sets, "load_registry", _loader)
    return _Registry()
