"""16A-2b b1a closure round r6, the slot-gate parts: the stored results and the slot's rows sidecar carry the computed scorer
identity (C3), next to the scorer-version label (the controls computation's refusal of draws from different scorers is in
``tests/test_p16a2_controls.py``).

Synthetic data only (``tests/p16_slot.py``).
"""

from __future__ import annotations

import json

from src import scoring_identity
from src.eval_cohort import SCORER_VERSION
from tests.p16_slot import _langsmith_env_cache, bare, world  # noqa: F401 - the fixtures are used by name


def test_the_stored_results_and_the_slot_sidecar_carry_the_scorer_identity(world):
    assert world.run(0).canonical is True
    doc = world.stored_doc(0)
    rows = json.loads((world.run_dir(doc["run_id"]) / "rows.json").read_text())
    assert doc["scorer_identity"] == rows["scorer_identity"] == scoring_identity.scorer_identity()
    assert len(doc["scorer_identity"]) == 64
    assert doc["scorer_version"] == rows["scorer_version"] == SCORER_VERSION  # the label stays, as a label


def test_the_scorer_identity_in_a_stored_result_moves_with_the_scoring_data(world, monkeypatch, tmp_path):
    import shutil

    for relative in scoring_identity.SCORING_DATA_FILES:
        (tmp_path / "data" / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(scoring_identity.DATA_ROOT / relative, tmp_path / "data" / relative)
    (tmp_path / "data" / scoring_identity.SCORING_DATA_FILES[0]).write_text('{"map": {}}\n', encoding="utf-8")
    before = scoring_identity.scorer_identity()
    monkeypatch.setattr(scoring_identity, "DATA_ROOT", tmp_path / "data")
    assert scoring_identity.scorer_identity() != before
