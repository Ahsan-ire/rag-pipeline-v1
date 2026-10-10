"""Tests for src/text_utils.py (16A-1 item 3): the move, is_gap_statement, and
render's list-item gap fix. Synthetic text only; no IO, no models.
"""

import ast
import pathlib

import pytest

from src.generator import CAVEAT_PREFIX
from src.render import uncited_statements
from src.text_utils import _GAP_STARTS, _HEDGE_RE, is_gap_statement, split_sentences

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"


def _imported_modules(path: pathlib.Path) -> set:
    """Every module name a source file imports (top level or nested)."""
    names = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return names


class TestMoveAndNoCycle:
    def test_render_no_longer_imports_evaluator(self):
        assert "src.evaluator" not in _imported_modules(SRC / "render.py")

    def test_text_utils_imports_nothing_from_the_project(self):
        assert not any(m.startswith("src") for m in _imported_modules(SRC / "text_utils.py"))

    def test_evaluator_reexports_the_same_function(self):
        from src import evaluator

        assert evaluator.split_sentences is split_sentences

    def test_gap_starts_and_hedge_unchanged(self):
        assert _GAP_STARTS == (
            "The extracts do not",
            "The source material does not",
            "This is not covered",
            "The handbook does not",
        )
        assert _HEDGE_RE.search("it is LIKELY so")
        assert not _HEDGE_RE.search("butter and buttress")


GAP = "The handbook does not address widget easements at all."


class TestIsGapStatement:
    @pytest.mark.parametrize(
        "unit",
        [
            GAP,
            f"- {GAP}",
            f"* {GAP}",
            f"• {GAP}",
            f"1. {GAP}",
            f"12) {GAP}",
            f"(a) {GAP}",
            f"(iv) {GAP}",
            f"**{GAP}**",
            f"_{GAP}_",
            f"- **{GAP}**",
            f"**1.** {GAP}",
            "  The extracts do not mention the widget register.",
            "This is not covered by the extracts provided.",
        ],
    )
    def test_gap_statements(self, unit):
        assert is_gap_statement(unit)

    @pytest.mark.parametrize(
        "unit",
        [
            "The handbook does not address it, but it is likely 20 days.",
            "- The handbook does not say; however the period is 20 days.",
            "**The extracts do not** cover it; probably 10 days.",
            "The purchaser must register the widget.",
            "- The purchaser must register the widget.",
            "the handbook does not address it.",  # case-sensitive start, as H2
            "-The handbook does not address it.",  # marker needs a space
            "Notably, the handbook does not address it.",
            "",
        ],
    )
    def test_not_gap_statements(self, unit):
        assert not is_gap_statement(unit)


class TestRenderListItemGapFix:
    def test_bullet_gap_statement_no_longer_flagged(self):
        text = (
            "Widgets must be registered [para 3.2, p.10].\n"
            f"- {GAP}\n"
            f"* **{GAP}**\n"
            f"(a) {GAP}"
        )
        assert uncited_statements(text) == []

    def test_other_list_items_still_flagged(self):
        text = (
            "Widgets must be registered [para 3.2, p.10].\n"
            "- The vendor must also deliver the widget certificate.\n"
            f"- {GAP}\n"
            "(b) The purchaser pays the widget levy on completion."
        )
        assert uncited_statements(text) == [
            "- The vendor must also deliver the widget certificate.",
            "(b) The purchaser pays the widget levy on completion.",
        ]

    def test_hedged_list_gap_still_flagged(self):
        unit = "- The handbook does not say, but it is probably 20 days."
        assert uncited_statements(f"Intro claim [para 3.2, p.10].\n{unit}") == [unit]

    def test_caveat_handling_unchanged(self):
        text = f"{CAVEAT_PREFIX} Widgets need a licence [para 3.2, p.10]."
        assert uncited_statements(text) == []
