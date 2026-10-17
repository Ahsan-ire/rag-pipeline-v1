"""The one switch for test-only injection seams (16A-2b b1a r7, T1; D74).

A sealed slot run, a recorded development draw and the matrix runner accept injected adapters so the test suite can run them
with fakes: ``run_slot(scan=...)``, ``recover_slot(scan=...)``, ``finish_slot(scan=..., render=...)``, ``matrix_kwargs`` (the
retrieval, generation, judge and provenance adapters), ``run_eval_matrix(retrieve_fn_factory=, generate_fn=, judge_fn=,
provenance_fn=, record_draw=)`` and ``SpendMeter(inner_factory=...)``. Every one of them can change what a run returns while
every identity hash stays the same (a scanner that picks which groups are excluded, a judge that is not the registered one),
so none of them is honoured outside the suite: :data:`TEST_SEAMS` is ``False`` and each entry point refuses a non-default
seam before any claim, ledger write or call (``GateRefused("seam")`` at the slot and evaluator entry points, which the CLI
reports as exit 4; ``SpendMeterError`` from the meter).

``tests/conftest.py`` sets :data:`TEST_SEAMS` to ``True`` for the suite. **No module under ``src/`` or ``scripts/`` may assign
it** (an AST test holds this): the only way to switch the seams on is code that is not shipped. This is a guard against
accidental misuse, like the hooks, not a security boundary against code that edits the source.
"""

from __future__ import annotations

#: True only while the test suite runs (``tests/conftest.py``). Read at call time (``seams.TEST_SEAMS``), never copied.
TEST_SEAMS = False


def enabled() -> bool:
    """True when the test-only seams are honoured (only under the test suite)."""
    return TEST_SEAMS
