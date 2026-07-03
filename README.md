# Legal RAG Pipeline

A retrieval-augmented question-answering pipeline for long, structurally-numbered
legal reference documents (built and evaluated against an ~800-page Irish
conveyancing handbook, but corpus-agnostic — bring your own manual). The
pipeline chunks on the document's own numbering structure (chapter / section /
paragraph), retrieves with hybrid BM25 + vector search, and answers strictly
from retrieved context — every answer carries a chapter/paragraph/page
citation, and out-of-corpus questions are explicitly refused rather than
guessed at.

**Status:** under active development — submission 12 July 2026.

## Quickstart

```
# TODO (Phase 6): fresh-clone quickstart, verified end-to-end.
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python -m src.pipeline index ./data/your-document.pdf --type handbook
python -m src.pipeline query "..." --top-k 6
```

See `IMPLEMENTATION_PLAN.md` for the build roadmap and `docs/decisions.md`
for design rationale.
