"""Phase 16A-2a gate fix A9: exclusions name their member families.

``exclude(group_id, reason, members)`` stores the members, ``excluded_families()`` returns them, the
slot gate resolves them against the freeze-bound groups at run time (so a group renamed by a later
rejection still resolves, and a stale id is not a permanent refusal), and ``record_freeze`` computes
``exclusions_sha256`` inside its own transaction when it is not given one (a given one must equal it).
"""

import json

import pytest

from src import sealed_run
from src.sealed_ledger import LedgerCorrupt, ProtocolError, exclusions_digest
from tests.p16_slot import (  # noqa: F401 - the fixtures are used by name
    _langsmith_env_cache,
    bare,
    gate_of,
    kinds,
    world,
)
from tests.test_p16w_sealed_ledger import (
    DIGEST_A,
    DIGEST_B,
    DIGEST_C,
    NO_EXCLUSIONS,
    lines_of,
    make,
    mirror_file,
)
from tests.test_p16w_slot_run_protocol import leaky

# --- the ledger ------------------------------------------------------------------------------------------------------


def test_an_exclusion_records_its_members_and_excluded_families_unions_them(tmp_path):
    led = make(tmp_path)
    led.exclude("fa", "s_hit", members=["fb", "fa", "fb"])  # stored sorted and distinct
    led.exclude("fz", "owner")  # no members given: the group's own name
    first, second = led.records("exclusion")
    assert first["members"] == ["fa", "fb"] and second["members"] == ["fz"]
    assert led.excluded_families() == frozenset({"fa", "fb", "fz"})
    assert led.exclusions() == frozenset({"fa", "fz"})  # the group ids are unchanged
    assert make(tmp_path).excluded_families() == frozenset({"fa", "fb", "fz"})  # durable
    mirror = [json.loads(line) for line in lines_of(mirror_file(tmp_path))]
    assert [set(m) for m in mirror] == [{"seq", "kind", "digest"}] * 2  # the mirror: kinds and line digests only


def test_a_record_written_before_members_existed_counts_as_its_group_name(tmp_path):
    led = make(tmp_path)
    with led._txn() as txn:  # an old record: no "members" key
        txn.append("exclusion", group_id="fold", reason="owner")
    assert led.excluded_families() == frozenset({"fold"})


def test_malformed_members_in_the_ledger_fail_closed(tmp_path):
    led = make(tmp_path)
    with led._txn() as txn:
        txn.append("exclusion", group_id="fbad", reason="owner", members=[3])
    with pytest.raises(LedgerCorrupt):
        led.excluded_families()


@pytest.mark.parametrize("members", [[], "fa", b"fa", [""], ["has space"], [3], ["x" * 65], [None]])
def test_bad_members_are_refused_and_write_nothing(tmp_path, members):
    led = make(tmp_path)
    with pytest.raises(ValueError):
        led.exclude("fa", "owner", members=members)
    assert led.records() == []


def test_the_sealed_freeze_digests_the_exclusions_inside_its_transaction(tmp_path):
    led = make(tmp_path)
    led.exclude("grp-1", "exposure", members=["grp-1", "grp-9"])
    led.exclude("grp-2", "owner")
    led.record_freeze(split="sealed", set_sha256=DIGEST_A, twins_sha256=DIGEST_B, totals={})  # exclusions_sha256 omitted
    frozen = led.freeze_record("sealed")
    assert frozen["exclusions_sha256"] == exclusions_digest({"grp-1", "grp-2"})  # of the group ids, as before
    assert led.exclusions_at_freeze() == frozenset({"grp-1", "grp-2"})
    assert json.loads(lines_of(mirror_file(tmp_path))[-1])["digests"]["exclusions_sha256"] == frozen["exclusions_sha256"]
    other = make(tmp_path)
    other.record_freeze(split="tuning", set_sha256=DIGEST_C, totals={})  # the other splits still bind none
    assert "exclusions_sha256" not in other.freeze_record("tuning")


def test_a_given_exclusions_digest_must_equal_the_ledgers_own(tmp_path):
    led = make(tmp_path)
    led.exclude("grp-1", "owner")
    with pytest.raises(ProtocolError):
        led.record_freeze(split="sealed", set_sha256=DIGEST_A, twins_sha256=DIGEST_B, exclusions_sha256=NO_EXCLUSIONS, totals={})
    with pytest.raises(ProtocolError):
        led.record_freeze(split="tuning", set_sha256=DIGEST_A, exclusions_sha256=DIGEST_C, totals={})
    assert led.freeze_record("sealed") is None and led.freeze_record("tuning") is None
    led.record_freeze(
        split="sealed", set_sha256=DIGEST_A, twins_sha256=DIGEST_B, exclusions_sha256=exclusions_digest(["grp-1"]), totals={}
    )
    assert led.freeze_record("sealed")["exclusions_sha256"] == exclusions_digest(["grp-1"])


def test_no_exclusion_can_slip_in_between_a_digest_and_the_freeze(tmp_path):
    """A digest computed outside the transaction goes stale if another process excludes meanwhile: refused,
    and the omitted form is computed under the lock, so it is never stale."""
    mine, theirs = make(tmp_path), make(tmp_path)
    mine.exclude("grp-1", "owner")
    stale = exclusions_digest(mine.exclusions())
    theirs.exclude("grp-2", "s_hit")
    with pytest.raises(ProtocolError):
        mine.record_freeze(split="sealed", set_sha256=DIGEST_A, twins_sha256=DIGEST_B, exclusions_sha256=stale, totals={})
    mine.record_freeze(split="sealed", set_sha256=DIGEST_A, twins_sha256=DIGEST_B, totals={})
    assert mine.freeze_record("sealed")["exclusions_sha256"] == exclusions_digest({"grp-1", "grp-2"})
    assert mine.exclusions_at_freeze() == frozenset({"grp-1", "grp-2"})


# --- the gate -----------------------------------------------------------------------------------------------------------


def test_a_group_renamed_by_a_later_rejection_still_resolves_at_the_gate(bare):
    """Group {f0, f1} is excluded under its name f0; f0 is then rejected out of the sealed set, so the group is
    named f1. The exclusion's members resolve it; before, the id f0 was unknown and every slot was refused."""
    f = bare.families
    rows = [r for r in bare.sealed_rows if r["family_id"] != f[0]]
    bare.sealed_path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    bare.write_prereg(floor=(6, 4))
    bare.write_records(exclusions={f[0]: [f[0], f[1]]})
    bare.freeze()
    assert bare.ledger().exclusions() == frozenset({f[0]})  # the recorded name is stale
    token = bare.token(0)
    assert token.groups[f[1]] == f[1]  # the group is now named by its smallest surviving member
    assert token.excluded == frozenset({f[1]})
    token.release()


def test_a_renamed_group_stays_excluded_through_the_run_and_the_report(bare):
    f = bare.families
    rows = [r for r in bare.sealed_rows if r["family_id"] != f[0]]
    bare.sealed_path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    bare.write_prereg(floor=(6, 4))
    bare.write_records(exclusions={f[0]: [f[0], f[1]]})
    bare.freeze()
    outcome = bare.run(0)
    assert outcome.canonical
    text = sealed_run.results_v6_path().read_text()
    assert "excluded groups: 1 of 6 (1 families dropped" in text
    assert "eligible answerable families (answer or partial, not excluded): 4" in text
    assert not [i for i in bare.ids() if i in text]


def test_a_stale_exclusion_is_no_longer_a_permanent_refusal(bare):
    bare.write_prereg()
    bare.write_records(exclusions={"f99999999": ["f99999999", "f99999998"]})  # none of its families is in the set
    bare.freeze()
    token = bare.token(0)
    assert token.excluded == frozenset()
    token.release()


def test_an_exclusion_from_a_scan_hit_records_the_whole_group_as_its_members(world, monkeypatch):
    f = world.families
    render, _leaks = leaky(world, [f[1]])  # f1 is the larger name of the group {f0, f1}
    monkeypatch.setattr(sealed_run, "format_sealed_report", render)
    outcome = world.run(0)
    assert outcome.canonical and outcome.scan_exclusions == 1
    [record] = world.ledger().records("exclusion")
    assert (record["group_id"], record["reason"], record["members"]) == (f[0], "s_hit", [f[0], f[1]])
    assert world.ledger().excluded_families() == frozenset({f[0], f[1]})


def test_a_scan_that_reports_several_families_for_one_match_excludes_every_families_group(world):
    """The scanner can return one S-hit per sealed family sharing a matched question: every group goes."""
    f = world.families
    calls = []

    def scan(text):
        calls.append(text)
        return [f[4], f[5], f[4]] if len(calls) == 1 else []  # two families (one repeated), then clean

    outcome = world.run(0, scan=scan)
    assert outcome.canonical and outcome.scan_exclusions == 2 and len(calls) == 2
    assert sorted((r["group_id"], r["members"]) for r in world.ledger().records("exclusion")) == [
        (f[4], [f[4]]), (f[5], [f[5]]),
    ]
    text = sealed_run.results_v6_path().read_text()
    assert "excluded groups: 2 of 6 (2 families dropped" in text
