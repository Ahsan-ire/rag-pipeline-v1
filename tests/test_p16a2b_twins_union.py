"""16A-2b: ``sealed_tool twins --extra-verdict``: a pair flagged by ANY SP run counts.

The SP's all-pairs verdict varies between runs, so ``twins`` takes the main ``--verdict`` plus any number of ``--extra-verdict``
files (and the orchestrator's ``--dev-verdict``) and uses the union of their pairs. Every SP file goes through the same strict
parser, the in-pool refusal applies to the union of the SP files, and without extras the output and the stored record are
byte-for-byte what they were before the option existed (the golden below was captured from the tool at 61041d9, before it). A
with-extras record is stored under its own name (the pool digest plus ``_u`` and a digest of the SP union), so it can follow a
no-extras record for the same pool; the pointer moves to whichever record was stored last.

Synthetic fixtures only (``tests/p16w_sealed_world.py``); nothing here reads a real eval set, the corpus or ``eval/private/``.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

import pytest

from scripts import sealed_tool as st
from tests.p16w_sealed_world import CANARY, build_standard, fid_of, public_id, repos, world  # noqa: F401  (fixtures)
from tests.test_p16w_sealed_tool import assert_allowlisted
from tests.test_p16w_sealed_tool_split import assign_payload, twins_record

# --- the golden: the standard world's ``twins`` run with the main verdict only, as the tool printed and stored it at 61041d9 ---
GOLDEN_LINE = (
    "twins round=1 pool_families=20 twin_pairs=8 groups=3 ineligible=8 dropped=0 edges=3 "
    "sha256=9f12aee92d780be5c4b8d7a38e27d734f814f172bf6716736d32fea58cdbfeb2\n"
)
GOLDEN_SHA = "9f12aee92d780be5c4b8d7a38e27d734f814f172bf6716736d32fea58cdbfeb2"
GOLDEN_NAME = "twins_r1_9d8d1ebc7c070899"
GOLDEN_VERDICT_SHA = "7ab5c0330374c2432425704ae7e63a6f47b98a24ac74933e10556bc326b97bb1"
GOLDEN_KEYS = {
    "batches", "dropped", "edges", "families_digest", "groups", "ineligible", "pairs_covered", "pool_digest", "round",
    "twin_pairs", "verdict_sha256", "version",
}

Pair = Tuple[str, str]


# --- helpers --------------------------------------------------------------------------------------------------------------------
def sha_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def payload_dir() -> Path:
    return Path(os.environ["CC_SEALED_LEDGER"]).parent / "sealed_payloads"


def stored_twins() -> List[Path]:
    """The ``twins_r*`` payload files stored so far (the directory does not exist until something is stored)."""
    directory = payload_dir()
    return sorted(directory.glob("twins_r*.json")) if directory.is_dir() else []


def pointer(w) -> Dict[str, Any]:
    return json.loads((w.sealed / "work" / "twins_current_r1.json").read_text())


def verdict_file(w, pairs: Sequence[Pair], *, flip: bool = False) -> Path:
    """A verdict file; with ``flip`` the pairs are written in reverse order and each one with its ids swapped."""
    chosen = [(b, a) for a, b in reversed(pairs)] if flip else list(pairs)
    return w.verdict(chosen)


def twins(w, main: Path, *extras: Path, dev: Path = None, round_no: int = 1):  # type: ignore[assignment]
    argv: List[Any] = ["twins", "--round", str(round_no), "--verdict", main]
    for extra in extras:
        argv += ["--extra-verdict", extra]
    if dev is not None:
        argv += ["--dev-verdict", dev]
    return w.run(*argv)


def canonical(pairs: Sequence[Pair]) -> List[List[str]]:
    """What the record must hold: unordered pairs (each sorted), de-duplicated, sorted."""
    return [list(p) for p in sorted({tuple(sorted(p)) for p in pairs})]


# --- no extras: byte-identical to before ------------------------------------------------------------------------------------------
def test_without_extras_the_output_and_the_stored_record_are_byte_identical_to_before(world):
    std = build_standard(world)
    world.write_prereg()
    world.commit_prereg(push=True)
    verdict = world.verdict(std.verdict_pairs)
    code, out, err = twins(world, verdict)
    assert code == 0 and err == ""
    assert out == GOLDEN_LINE and "extra_verdict" not in out
    ptr = pointer(world)
    assert ptr["sha256"] == GOLDEN_SHA and ptr["payload"] == GOLDEN_NAME  # the name too: no ``_u`` suffix without extras
    stored = payload_dir() / f"{ptr['payload']}.json"
    assert sha_of(stored) == GOLDEN_SHA  # the very bytes the tool stored
    record = twins_record(world)
    assert set(record) == GOLDEN_KEYS and "extra_verdict_sha256s" not in record
    assert record["verdict_sha256"] == GOLDEN_VERDICT_SHA == sha_of(verdict)


def test_an_empty_extra_list_is_the_same_as_no_option(world):
    """``--extra-verdict`` absent is an empty list: nothing is added to the record or the line."""
    std = build_standard(world)
    code, out, err = twins(world, world.verdict(std.verdict_pairs))
    assert code == 0, err
    assert "extra_verdicts" not in out and "extra_verdict_sha256s" not in twins_record(world)


# --- union semantics -----------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("flip", [False, True], ids=["as_written", "reversed_and_swapped"])
def test_the_pairs_used_are_the_union_of_the_main_verdict_every_extra_and_the_dev_verdict(world, flip):
    p, p2 = world.pinned("tuning", source="legacy"), world.pinned("tuning", source="legacy")
    a, b, c, d, e = (world.draft() for _ in range(5))
    world.flush_pool()
    main_pairs = [(a, public_id(1)), (b, public_id(2))]
    extra1_pairs = [(public_id(2), b), (c, public_id(3))]  # (b, q2) again, ids swapped: one pair, not two
    extra2_pairs = [(d, p)]  # a twin of a pinned development family: the draft is sealed-ineligible
    dev_pairs = [(p, p2)]
    main, extra1 = verdict_file(world, main_pairs, flip=flip), verdict_file(world, extra1_pairs, flip=flip)
    extra2, dev = verdict_file(world, extra2_pairs, flip=flip), verdict_file(world, dev_pairs, flip=flip)

    code, out, err = twins(world, main, extra1, extra2, dev=dev)
    assert code == 0, err
    expected = canonical([*main_pairs, *extra1_pairs, *extra2_pairs, *dev_pairs])
    assert len(expected) == 5  # the duplicate collapsed
    record = twins_record(world)
    assert record["twin_pairs"] == expected  # every file's pairs present, sorted, unordered, de-duplicated
    assert record["ineligible"] == sorted([a, b, c, d]) and e not in record["ineligible"]  # each file's ties took effect
    assert record["verdict_sha256"] == sha_of(main)  # still the main file's
    assert record["extra_verdict_sha256s"] == sorted([sha_of(extra1), sha_of(extra2)])
    line = world.last(out, "twins")
    assert (line["twin_pairs"], line["ineligible"], line["extra_verdicts"]) == ("5", "4", "2")
    assert line["sha256"] == pointer(world)["sha256"]
    assert_allowlisted(out)


def test_the_order_of_the_extras_does_not_matter(world):
    a, b, c = (world.draft() for _ in range(3))
    world.flush_pool()
    main = world.verdict([(a, public_id(1))])
    extra1, extra2 = world.verdict([(b, public_id(2))]), world.verdict([(c, public_id(3))])
    code, first, err = twins(world, main, extra1, extra2)
    assert code == 0, err
    first_pointer, first_record = pointer(world), twins_record(world)
    # the same inputs in another order are the same record: an unchanged pool's payload is immutable, so this rerun would be
    # refused if the stored record differed in any byte
    code, second, err = twins(world, main, extra2, extra1)
    assert code == 0, err
    assert second == first and pointer(world) == first_pointer and twins_record(world) == first_record
    assert first_record["extra_verdict_sha256s"] == sorted([sha_of(extra1), sha_of(extra2)])


# --- the in-pool refusal ----------------------------------------------------------------------------------------------------------------
def test_a_twin_pair_inside_the_pool_in_an_extra_is_refused_and_nothing_is_stored(world):
    a, b, c = (world.draft() for _ in range(3))
    world.flush_pool()
    main, extra = world.verdict([(a, public_id(1))]), world.verdict([(b, c)])
    code, out, err = twins(world, main, extra)
    assert code == st.EXIT_REFUSED and "reason=twin_in_pool pairs=1" in err and out == ""
    assert not (world.sealed / "work" / "twins_current_r1.json").exists()  # no pointer
    assert stored_twins() == [] and world.ledger().records() == []  # no payload, no ledger record


def test_the_in_pool_refusal_counts_the_union_of_the_sp_files(world):
    a, b, c, d = (world.draft() for _ in range(4))
    world.flush_pool()
    same = world.verdict([(a, b)])
    code, out, err = twins(world, same, world.verdict([(b, a)]))  # the same pair in both files, ids swapped: one pair
    assert code == st.EXIT_REFUSED and "reason=twin_in_pool pairs=1" in err
    code, out, err = twins(world, same, world.verdict([(c, d)]), world.verdict([(a, c)]))  # three different pairs
    assert code == st.EXIT_REFUSED and "reason=twin_in_pool pairs=3" in err
    assert not (world.sealed / "work" / "twins_current_r1.json").exists() and stored_twins() == []


# --- every extra is parsed as strictly as the main verdict ------------------------------------------------------------------------
def test_a_malformed_extra_is_refused_exactly_like_a_malformed_main_verdict(world):
    a = world.draft()
    world.flush_pool()
    good = world.verdict([(a, public_id(1))])
    bad = world.sp_dir / "bad.jsonl"
    bad.write_text(json.dumps({"a": CANARY, "b": "x y", "verdict": "twin"}) + "\nnot json\n", encoding="utf-8")
    code_main, out_main, err_main = twins(world, bad)
    code_extra, out_extra, err_extra = twins(world, good, bad)
    assert (code_main, out_main) == (code_extra, out_extra) == (st.EXIT_USAGE, "")
    assert err_main == err_extra and "reason=verdict_invalid lines=2" in err_extra  # same refusal, same count
    assert CANARY not in out_extra + err_extra
    assert not (world.sealed / "work" / "twins_current_r1.json").exists() and stored_twins() == []
    assert world.ledger().records() == []


def test_a_missing_extra_is_refused_exactly_like_a_missing_main_verdict(world):
    a = world.draft()
    world.flush_pool()
    good = world.verdict([(a, public_id(1))])
    nope = world.sp_dir / "nope.jsonl"
    code_main, out_main, err_main = twins(world, nope)
    code_extra, out_extra, err_extra = twins(world, good, nope)
    assert (code_main, out_main, err_main) == (code_extra, out_extra, err_extra) == (st.EXIT_USAGE, "", "error reason=input_missing\n")
    assert stored_twins() == []


# --- an id known nowhere ---------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("where", ["main", "extra"])
def test_a_pair_naming_an_unknown_id_is_treated_alike_in_the_main_verdict_and_in_an_extra(world, where):
    """No crash: a draft paired with an id that is in no set is tied to a public family (sealed-ineligible), exactly as if
    it were paired with a public id; a pair of two unknown ids has no effect at all."""
    a, b = world.draft(), world.draft()
    world.flush_pool()
    unknown_family, unknown_public = fid_of(0xFFF0), public_id(0xFFF1)
    pairs = [(a, unknown_family), (unknown_family, unknown_public), (unknown_public, fid_of(0xFFF2))]
    carrier, empty = world.verdict(pairs), world.verdict()
    code, out, err = twins(world, carrier) if where == "main" else twins(world, empty, carrier)
    assert code == 0, err
    record = twins_record(world)
    assert record["twin_pairs"] == canonical(pairs)
    assert record["ineligible"] == [a] and b not in record["ineligible"]
    assert record["edges"] == [] and record["groups"] == {} and record["dropped"] == []
    line = world.last(out, "twins")
    assert (line["twin_pairs"], line["ineligible"], line["edges"], line["dropped"]) == ("3", "1", "0", "0")
    assert ("extra_verdicts" in line) == (where == "extra")


# --- the payload name: a union record can follow a no-extras record for the same pool ----------------------------------------------
def union_name(pool_digest: str, pairs: Sequence[Pair]) -> str:
    """The name a with-extras record must be stored under, computed here independently of the tool: the pool digest's first 16 hex,
    then ``_u`` and the first 16 hex of the sha256 of the canonical JSON of the sorted union of the SP pairs (no dev verdict)."""
    text = json.dumps(canonical(pairs), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return f"twins_r1_{pool_digest[:16]}_u{hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]}"


def test_a_union_record_follows_a_no_extras_record_for_the_same_pool_and_the_pointer_moves(world):
    a, b, c = (world.draft() for _ in range(3))
    world.flush_pool()
    main_pairs, extra_pairs, extra2_pairs = [(a, public_id(1))], [(b, public_id(2))], [(c, public_id(3))]
    main, extra, extra2 = world.verdict(main_pairs), world.verdict(extra_pairs), world.verdict(extra2_pairs)

    code, out, err = twins(world, main)  # the no-extras record, as the real pool already has
    assert code == 0, err
    ptr1, record1 = pointer(world), twins_record(world)
    assert ptr1["payload"] == f"twins_r1_{ptr1['pool_digest'][:16]}"  # the name is unchanged without extras
    assert record1["ineligible"] == [a]

    code, out, err = twins(world, main, extra)  # same pool, now with an extra: no refusal
    assert code == 0 and err == "", err
    ptr2, record2 = pointer(world), twins_record(world)
    assert ptr2["payload"] == union_name(ptr1["pool_digest"], [*main_pairs, *extra_pairs])
    assert ptr2["pool_digest"] == ptr1["pool_digest"] and ptr2["sha256"] != ptr1["sha256"]  # the pointer moved
    assert record2["ineligible"] == sorted([a, b]) and record2["extra_verdict_sha256s"] == [sha_of(extra)]
    assert world.last(out, "twins")["sha256"] == ptr2["sha256"]
    # the first record is untouched and still loads: nothing was overwritten
    assert world.ledger().load_payload(ptr1["payload"], ptr1["sha256"]) == record1
    assert [f.stem for f in stored_twins()] == sorted([ptr1["payload"], ptr2["payload"]])

    code, again, err = twins(world, main, extra)  # an identical rerun is idempotent
    assert code == 0, err
    assert again == out and pointer(world) == ptr2 and twins_record(world) == record2
    assert len(stored_twins()) == 2

    code, out3, err = twins(world, main, extra, extra2)  # a different union on the same pool gets a different name: allowed
    assert code == 0, err
    ptr3 = pointer(world)
    assert ptr3["payload"] == union_name(ptr1["pool_digest"], [*main_pairs, *extra_pairs, *extra2_pairs])
    assert ptr3["payload"] not in (ptr1["payload"], ptr2["payload"]) and twins_record(world)["ineligible"] == sorted([a, b, c])
    assert len(stored_twins()) == 3
    assert world.ledger().load_payload(ptr2["payload"], ptr2["sha256"]) == record2  # still intact

    code, back, err = twins(world, main)  # and the no-extras command is still idempotent: the pointer returns to its record
    assert code == 0, err
    assert pointer(world) == ptr1 and twins_record(world) == record1 and len(stored_twins()) == 3


def test_the_name_is_the_sp_union_so_other_files_or_a_dev_verdict_for_the_same_union_are_refused_and_nothing_moves(world):
    """The name is derived from the union of the SP pairs, not the files and not ``--dev-verdict``, and a payload is immutable:
    the same name with different content (another split of the same pairs into files, or a dev verdict added) is refused."""
    a, b = world.draft(), world.draft()
    world.flush_pool()
    pairs = [(a, public_id(1)), (b, public_id(2))]
    main, extra = world.verdict(pairs[:1]), world.verdict(pairs[1:])
    code, out, err = twins(world, main, extra)
    assert code == 0, err
    first_pointer, first_record = pointer(world), twins_record(world)
    assert first_pointer["payload"] == union_name(first_pointer["pool_digest"], pairs)

    both, empty = world.verdict(pairs), world.verdict()
    code, out, err = twins(world, both, empty)  # the same union through other files
    assert code == st.EXIT_REFUSED and "reason=ledger_protocol" in err and out == ""
    code, out, err = twins(world, main, extra, dev=world.verdict([(a, b)]))  # the same union plus a dev pair
    assert code == st.EXIT_REFUSED and "reason=ledger_protocol" in err and out == ""
    assert pointer(world) == first_pointer and twins_record(world) == first_record and len(stored_twins()) == 1


# --- a following split uses the union --------------------------------------------------------------------------------------------------
def test_diagnose_and_split_use_the_union_record_once_the_pointer_moves_more_ineligible_drafts_than_the_main_verdict_alone(world):
    """The standard world (8 development drafts, all forced out of sealed by a twin with a public id), whose 8 verdict pairs are
    split: the main verdict holds 6 and an extra the last 2. The no-extras record is recorded first, as in the real pool."""
    std = build_standard(world)
    world.write_prereg()
    world.commit_prereg(push=True)
    main, extra = world.verdict(std.verdict_pairs[:6]), world.verdict(std.verdict_pairs[6:])
    code, out, err = twins(world, main)
    assert code == 0, err
    ptr1 = pointer(world)
    assert twins_record(world)["ineligible"] == sorted(std.dev_drafts[:6]) and "extra_verdicts" not in out
    code, out, err = world.run("split", "--round", "1", "--diagnose")  # the main verdict alone
    assert code in (0, st.EXIT_SPLIT_INFEASIBLE), err
    assert "diagnose_drafts class=sealed_ineligible families=6 answer=3 partial=0 refuse=3" in out
    assert "diagnose_drafts class=sealed_eligible families=14 answer=11 partial=0 refuse=3" in out

    code, out, err = twins(world, main, extra)  # now the union, on the same pool
    assert code == 0, err
    ptr2 = pointer(world)
    assert ptr2["payload"] == union_name(ptr2["pool_digest"], std.verdict_pairs) and ptr2 != ptr1
    record = twins_record(world)
    assert record["ineligible"] == sorted(std.dev_drafts) and record["extra_verdict_sha256s"] == [sha_of(extra)]
    assert world.last(out, "twins")["extra_verdicts"] == "1"
    before = world.ledger().records()
    code, out, err = world.run("split", "--round", "1", "--diagnose")  # the staleness check passes: no twins_stale
    assert code == 0 and err == ""
    assert "diagnose_drafts class=sealed_ineligible families=8 answer=5 partial=0 refuse=3" in out
    assert "diagnose_drafts class=sealed_eligible families=12 answer=9 partial=0 refuse=3" in out
    assert world.ledger().records() == before  # a diagnosis records nothing
    code, out, err = world.run("split", "--round", "1")
    assert code == 0, err
    assert world.last(out, "split")["outcome"] == "released"
    payload = assign_payload(world)
    assert sorted(payload["ineligible"]) == sorted(std.dev_drafts)  # all 8, as the union says
    assert (payload["twins_payload"], payload["twins_sha256"]) == (ptr2["payload"], ptr2["sha256"])  # the release names the union record
