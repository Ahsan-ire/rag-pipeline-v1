"""Phase 16A-2a (j), `scripts/controls.py` under both rules (a8). Everything is synthetic.

Draw sidecars are built with ``src.eval_cohort`` (the shape the evaluator
writes); development sets are tmp v2 files with invented ``P16-CANARY``
questions. Rank tables are invented; no model, index or `data/` is touched.
"""

import json

import pytest

from scripts import bakeoff_report, controls
from src.eval_cohort import SCORER_VERSION, build_sidecar, cohort_block
from src.eval_schema import load_inventory
from src.eval_sets import sha256_file

CANARY = "P16-CANARY"
SHA = "a" * 64
RAW, REWRITE = "hybrid", "hybrid+rewrite"
EXPANSION = {"kind": "live"}


def _rid(fam, n=0):
    return f"{fam}-{n}"


def _sidecar(set_sha, table, *, scorer=SCORER_VERSION, refusal=(), drop_mode=None, drop_row=None):
    """A C4 sidecar. ``table``: ``{row_id: (raw rank, hybrid+rewrite rank)}`` (``None`` = miss).

    A third mode (bm25, every rank a miss) is always present: controls must ignore it.
    """
    rows, cohort_rows = [], []
    for rid, (raw, rewrite) in table.items():
        cohort_rows.append((rid, rid.rsplit("-", 1)[0], [["901.1"]], "answer"))
        for mode, rank in ((RAW, raw), (REWRITE, rewrite), ("bm25", None)):
            if mode == drop_mode or rid == drop_row:
                continue
            rows.append(
                {"set_sha256": set_sha, "id": rid, "mode": mode, "strict_rank": rank,
                 "related_rank": rank, "completion_rank": rank}
            )
    for fam in refusal:  # refusal rows are in the cohort but never in the sidecar rows
        cohort_rows.append((_rid(fam), fam, [], "refuse"))
    block = cohort_block(path="synthetic", privacy="private", schema=2, sha256=set_sha, rows=cohort_rows)
    doc = build_sidecar(privacy="private", cohorts=[block], rows=rows, expansion=EXPANSION)
    doc["scorer_version"] = scorer
    return doc


# Family -> per-draw {row id: (raw, rewrite)}; 3 draws.
OK, RANK6, FLIP, MISSDRAW, SEVEN, MIXED = (f"f000{c}aaaa" for c in "123456")
TUTORIAL = "f0009aaaa"


def _tables():
    draws = []
    for d in range(3):
        t = {
            _rid(OK, 0): (1, 2), _rid(OK, 1): (3, 3),                   # always <= 3
            _rid(RANK6): (6, 6),                                        # rank 6 on every draw
            _rid(FLIP): (2, 5 if d == 2 else 2),                        # one flip: rank 5 once, rewrite mode
            _rid(MISSDRAW): (None if d == 1 else 1, 1),                 # a miss on one draw
            _rid(SEVEN): (7, 7),
            _rid(MIXED, 0): (2, 2), _rid(MIXED, 1): (4, 2),             # one phrasing past rank 3
            _rid(TUTORIAL): (1, 1),                                     # perfect, but not public lineage
        }
        draws.append(t)
    return draws


def _draws(**kw):
    return [_sidecar(SHA, t, refusal=("f0008aaaa",), **kw) for t in _tables()]


FAMILIES = {SHA: {rid: rid.rsplit("-", 1)[0] for rid in _tables()[0] if not rid.startswith(TUTORIAL)}}


def _ids(doc):
    (entry,) = doc["controls"]
    assert entry["set_sha256"] == SHA
    return entry["ids"]


# --- the two rules -------------------------------------------------------------
def test_c1_keeps_only_families_within_rank_3_on_every_draw():
    doc = controls.compute_controls(_draws(), FAMILIES, "c1")
    assert _ids(doc) == [_rid(OK, 0), _rid(OK, 1)]  # rank-6, one-flip, miss, rank-7, mixed excluded
    assert (doc["rule"], doc["rank_ceiling"], doc["draws"], doc["families"]) == ("c1", 3, 3, 1)


def test_c2_keeps_every_family_that_hits_at_6_in_the_incumbents_run():
    # D57 as written: the incumbent's own run (draw 1, the development artifact) decides
    doc = controls.compute_controls(_draws(), FAMILIES, "c2")
    assert _ids(doc) == sorted([_rid(OK, 0), _rid(OK, 1), _rid(RANK6), _rid(FLIP), _rid(MISSDRAW),
                                _rid(MIXED, 0), _rid(MIXED, 1)])
    assert (doc["rule"], doc["rank_ceiling"], doc["families"]) == ("c2", 6, 5)
    # MISSDRAW misses only on draw 2, which C2 never consults; rank 7 (SEVEN) fails D57's HIT
    assert _rid(SEVEN) not in _ids(doc)


def test_c2_ignores_later_draws_but_a_draw_1_miss_fails():
    t1, t2 = {_rid(OK): (1, 1), _rid(SEVEN): (None, 1)}, {_rid(OK): (9, 9), _rid(SEVEN): (1, 1)}
    fam = {SHA: {_rid(OK): OK, _rid(SEVEN): SEVEN}}
    assert _ids(controls.compute_controls([_sidecar(SHA, t1), _sidecar(SHA, t2)], fam, "c2")) == [_rid(OK)]


def test_a_one_flip_family_fails_c1_and_only_c1():
    draws = _draws()
    only_flip = {SHA: {rid: fam for rid, fam in FAMILIES[SHA].items() if fam in (OK, FLIP)}}
    assert _ids(controls.compute_controls(draws, only_flip, "c1")) == [_rid(OK, 0), _rid(OK, 1)]
    assert _rid(FLIP) in _ids(controls.compute_controls(draws, only_flip, "c2"))


def test_every_draw_counts_not_just_the_first():
    # F is rank 1 on draw 1 and rank 4 on draw 2: a single flip drops it from C1
    t1, t2 = {_rid(OK): (1, 1), _rid("f0007aaaa"): (1, 1)}, {_rid(OK): (1, 1), _rid("f0007aaaa"): (4, 1)}
    fam = {SHA: {_rid(OK): OK, _rid("f0007aaaa"): "f0007aaaa"}}
    doc = controls.compute_controls([_sidecar(SHA, t1), _sidecar(SHA, t2)], fam, "c1")
    assert _ids(doc) == [_rid(OK)]


def test_raw_hybrid_alone_fails_a_family_too():
    t = {_rid(OK): (1, 1), _rid(RANK6): (5, 1)}  # rewrite mode fine, raw hybrid at 5
    fam = {SHA: {_rid(OK): OK, _rid(RANK6): RANK6}}
    assert _ids(controls.compute_controls([_sidecar(SHA, t)], fam, "c1")) == [_rid(OK)]


def test_output_is_bakeoff_controls_format_and_holds_no_text():
    doc = controls.compute_controls(_draws(), FAMILIES, "c2")
    parsed = bakeoff_report.parse_controls(doc)
    assert parsed == [(SHA, rid) for rid in _ids(doc)]
    assert set(doc) == {"version", "rule", "rank_ceiling", "draws", "families", "controls"}
    assert CANARY not in json.dumps(doc)


# --- refusals -------------------------------------------------------------------
def test_unknown_rule_no_draw_no_family_and_empty_result_refuse():
    with pytest.raises(controls.ControlsError):
        controls.compute_controls(_draws(), FAMILIES, "c3")
    with pytest.raises(controls.ControlsError):
        controls.compute_controls([], FAMILIES, "c1")
    with pytest.raises(controls.ControlsError):
        controls.compute_controls(_draws(), {}, "c1")
    nothing = {SHA: {_rid(SEVEN): SEVEN}}
    with pytest.raises(controls.ControlsError, match="no family qualifies"):
        controls.compute_controls(_draws(), nothing, "c2")


def test_missing_mode_row_set_or_agreement_is_an_error_not_a_miss():
    fam = {SHA: {_rid(OK, 0): OK}}
    with pytest.raises(controls.ControlsError, match="no rows for mode"):
        controls.compute_controls(_draws(drop_mode=REWRITE), fam, "c1")
    ghost = {SHA: {**fam[SHA], "f0007aaaa-0": "f0007aaaa"}}  # a family row no draw scored
    with pytest.raises(controls.ControlsError, match="missing evidence is not a MISS"):
        controls.compute_controls(_draws(), ghost, "c1")
    other = {"b" * 64: fam[SHA]}
    with pytest.raises(controls.ControlsError, match="no cohort block"):
        controls.compute_controls(_draws(), other, "c1")
    draws = _draws()
    draws[1]["scorer_version"] = "something-else"
    with pytest.raises(controls.ControlsError, match="scorer_version"):
        controls.compute_controls(draws, fam, "c1")


def test_a_malformed_sidecar_is_refused_by_the_c4_validator():
    draws = _draws(drop_row=_rid(OK, 0))  # the cohort still lists the row: roster mismatch
    with pytest.raises(bakeoff_report.C4Error):
        controls.compute_controls(draws, {SHA: {_rid(OK, 1): OK}}, "c1")
    with pytest.raises(bakeoff_report.C4Error):
        controls.compute_controls([{"version": 1}], FAMILIES, "c1")


# --- lineage and the CLI --------------------------------------------------------
LABEL = load_inventory()["sections"][0]


def _v2(fam, n, source, scope="answer", **over):
    row = {
        "schema": 2, "id": f"{fam}-{n}", "family_id": fam,
        "question": f"{CANARY} invented question {fam}-{n}",
        "scope": scope, "evidence": [] if scope == "refuse" else [[LABEL]], "source": source,
    }
    row.update(over)
    return row


def _dev_set(tmp_path, rows, name="dev.jsonl"):
    path = tmp_path / name
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


def _rows():
    return [
        _v2(OK, 0, "legacy"), _v2(OK, 1, "legacy"),
        _v2(MIXED, 0, "legacy"), _v2(MIXED, 1, "legacy"),
        _v2(RANK6, 0, "legacy"),
        _v2(TUTORIAL, 0, "tutorial"),                      # perfect ranks, not public lineage
        _v2("f0008aaaa", 0, "legacy", scope="refuse"),     # public lineage but a refusal
    ]


def test_load_lineage_takes_legacy_families_and_drops_refusals_and_other_sources(tmp_path):
    path = _dev_set(tmp_path, _rows() + [_v2(OK, 2, "handbook")])  # a non-legacy paraphrase of a legacy family
    lineage = controls.load_lineage([str(path)])
    assert set(lineage) == {sha256_file(path)}
    assert set(lineage[sha256_file(path)]) == {
        _rid(OK, 0), _rid(OK, 1), _rid(OK, 2), _rid(MIXED, 0), _rid(MIXED, 1), _rid(RANK6)
    }


def _cli_draws(tmp_path, sha, *, tweak=None):
    paths = []
    for d, table in enumerate(_tables()):
        table = {rid: ranks for rid, ranks in table.items() if rid.split("-")[0] in (OK, MIXED, RANK6, TUTORIAL)}
        if tweak:
            tweak(d, table)
        doc = _sidecar(sha, table, refusal=("f0008aaaa",))
        p = tmp_path / f"draw{d}.rows.json"
        p.write_text(json.dumps(doc), encoding="utf-8")
        paths.append(str(p))
    return paths


def _realistic_set(tmp_path):
    """The realistic development set of these tests: one non-legacy family, so it brings no control (and no draw rows)."""
    return _dev_set(tmp_path, [_v2("f0007bbbb", 0, "tutorial")], name="realistic.jsonl")


def _freeze(dev, realistic, prereg):
    """Record both development splits' freezes (the versions the controls bind to) under ``prereg`` (each once; the
    first record carrying a prereg sha registers the epoch's prereg)."""
    from src.sealed_run import default_ledger

    ledger = default_ledger()
    for split, path in (("tuning", dev), ("realistic", realistic)):
        if ledger.freeze_record(split) is None:
            ledger.record_freeze(
                split=split, set_sha256=sha256_file(path), totals={"families": 1}, prereg_sha256=sha256_file(prereg),
            )


def _prereg(tmp_path, rule, draws_r, ceiling=None):
    path = tmp_path / f"prereg_{rule}.json"
    path.write_text(json.dumps({"controls": {
        "rule": rule, "rank_ceiling": controls.RANK_CEILING[rule] if ceiling is None else ceiling, "draws_r": draws_r,
    }}), encoding="utf-8")
    return path


def _argv(rule, draws, dev, out, *, freeze=True, prereg=None, dev_sets=None):
    """The CLI arguments: the rule comes from a prereg file written next to ``out``; both development sets are frozen
    first (``dev`` is the tuning set, the realistic one is a second file) and ``dev_sets`` overrides which are passed."""
    prereg = prereg or _prereg(out.parent, rule, len(draws))
    realistic = _realistic_set(out.parent)
    if freeze:
        _freeze(dev, realistic, prereg)
    argv = ["--prereg", str(prereg), "--list", str(out)]
    for d in dev_sets if dev_sets is not None else [dev, realistic]:
        argv += ["--dev-set", str(d)]
    for d in draws:
        argv += ["--draw", d]
    return argv


def _ledger():
    from src.sealed_run import default_ledger

    return default_ledger()


def test_cli_writes_records_then_verifies_and_refuses_a_differing_list(tmp_path, capsys):
    dev = _dev_set(tmp_path, _rows())
    sha = sha256_file(dev)
    draws = _cli_draws(tmp_path, sha)
    out = tmp_path / "controls_v1.json"
    prereg = _prereg(tmp_path, "c2", len(draws))

    assert controls.main(_argv("c2", draws, dev, out)) == 0
    written = out.read_text(encoding="utf-8")
    doc = json.loads(written)
    assert doc["rule"] == "c2" and doc["controls"][0]["set_sha256"] == sha
    assert doc["controls"][0]["ids"] == sorted(
        [_rid(OK, 0), _rid(OK, 1), _rid(MIXED, 0), _rid(MIXED, 1), _rid(RANK6)]
    )
    assert _rid(TUTORIAL) not in written
    # the ledger holds the controls record: the file's sha, the draw files' digest, the prereg's sha
    import hashlib

    record = _ledger().controls_record()
    assert record["controls_sha256"] == hashlib.sha256(out.read_bytes()).hexdigest()
    shas = sorted(hashlib.sha256(open(d, "rb").read()).hexdigest() for d in draws)
    assert record["draws_sha256"] == hashlib.sha256(json.dumps(shas, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert record["prereg_sha256"] == hashlib.sha256(prereg.read_bytes()).hexdigest()
    assert len(_ledger().records("controls")) == 1

    assert controls.main(_argv("c2", draws, dev, out)) == 0  # same inputs: verifies, no rewrite
    assert "matches the committed list" in capsys.readouterr().out
    assert out.read_text(encoding="utf-8") == written and len(_ledger().records("controls")) == 1

    changed = _cli_draws(tmp_path, sha, tweak=lambda d, t: t.update({_rid(OK, 0): (1, 5)}) if d == 2 else None)
    assert controls.main(_argv("c2", changed, dev, out)) == 0  # rank 5 is still a HIT@6: same list
    assert controls.main(_argv("c2", _cli_draws(tmp_path, sha, tweak=lambda d, t: t.update({_rid(OK, 0): (1, 9)})), dev, out)) == (
        controls.EXIT_DIFFERS
    )
    assert out.read_text(encoding="utf-8") == written  # a refusal writes nothing
    captured = capsys.readouterr()
    assert CANARY not in captured.out + captured.err
    assert "differs from the committed list" in captured.err
    # another rule needs another prereg, which is not this epoch's registered one: unusable, not a quiet difference
    assert controls.main(_argv("c1", draws, dev, out)) == controls.EXIT_UNUSABLE
    assert "not this epoch's registered prereg" in capsys.readouterr().err
    assert out.read_text(encoding="utf-8") == written


def test_cli_c1_list_content_and_text_free_output(tmp_path, capsys):
    dev = _dev_set(tmp_path, _rows())
    out = tmp_path / "c1.json"
    assert controls.main(_argv("c1", _cli_draws(tmp_path, sha256_file(dev)), dev, out)) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["controls"][0]["ids"] == [_rid(OK, 0), _rid(OK, 1)]
    captured = capsys.readouterr()
    assert CANARY not in captured.out + captured.err + out.read_text(encoding="utf-8")


def test_the_controls_are_computed_once_a_deleted_or_unrecorded_or_altered_file_is_never_recomputed(tmp_path, capsys):
    dev = _dev_set(tmp_path, _rows())
    draws = _cli_draws(tmp_path, sha256_file(dev))
    out = tmp_path / "controls_v1.json"
    assert controls.main(_argv("c2", draws, dev, out)) == 0
    written = out.read_text(encoding="utf-8")
    capsys.readouterr()
    # a deleted file does not allow a recompute: the record stands
    out.unlink()
    assert controls.main(_argv("c2", draws, dev, out)) == controls.EXIT_DIFFERS
    assert "controls already recorded" in capsys.readouterr().err and not out.exists()
    assert len(_ledger().records("controls")) == 1
    # a file with the right content but other bytes is not the recorded file
    out.write_text(json.dumps(json.loads(written), indent=4, sort_keys=True) + "\n", encoding="utf-8")
    assert controls.main(_argv("c2", draws, dev, out)) == controls.EXIT_DIFFERS
    assert "not the recorded one" in capsys.readouterr().err
    # the very bytes again: verifies
    out.write_text(written, encoding="utf-8")
    assert controls.main(_argv("c2", draws, dev, out)) == 0


def test_a_controls_file_with_no_ledger_record_is_refused(tmp_path, capsys):
    dev = _dev_set(tmp_path, _rows())
    draws = _cli_draws(tmp_path, sha256_file(dev))
    out = tmp_path / "controls_v1.json"
    out.write_text("{}\n", encoding="utf-8")  # a file nobody recorded
    assert controls.main(_argv("c2", draws, dev, out)) == controls.EXIT_DIFFERS
    assert "controls file not recorded" in capsys.readouterr().err
    assert out.read_text(encoding="utf-8") == "{}\n" and _ledger().controls_record() is None


def test_the_ledger_records_controls_once(tmp_path):
    from src.sealed_ledger import ProtocolError

    ledger = _ledger()
    ledger.record_controls(controls_sha256="a" * 64, draws_sha256="b" * 64, prereg_sha256="c" * 64)
    with pytest.raises(ProtocolError):
        ledger.record_controls(controls_sha256="d" * 64, draws_sha256="b" * 64, prereg_sha256="c" * 64)
    assert _ledger().controls_record()["controls_sha256"] == "a" * 64
    mirror = [json.loads(line) for line in (tmp_path / "eval" / "sealed_ledger.jsonl").read_text().splitlines()]
    assert mirror[-1]["digests"] == {"controls_sha256": "a" * 64, "draws_sha256": "b" * 64, "prereg_sha256": "c" * 64}


def test_cli_exit_codes_for_unusable_and_sealed_input(tmp_path, capsys):
    dev = _dev_set(tmp_path, _rows())
    good = _cli_draws(tmp_path, sha256_file(dev))
    out = tmp_path / "x.json"
    bad_json = tmp_path / "bad.rows.json"
    bad_json.write_text("{not json", encoding="utf-8")
    assert controls.main(_argv("c1", [str(bad_json)], dev, out)) == controls.EXIT_UNUSABLE
    other = _dev_set(tmp_path, [_v2("f0001bbbb", 0, "legacy")], name="other.jsonl")  # set not in the draws
    assert controls.main(_argv("c1", good, other, out)) == controls.EXIT_UNUSABLE
    sealed = _dev_set(tmp_path, [_v2("f0001cccc", 0, "legacy", sealed=True)], name="s.jsonl")
    assert controls.main(_argv("c1", good, sealed, out)) == controls.EXIT_SEALED
    assert not out.exists()
    captured = capsys.readouterr()
    assert CANARY not in captured.out + captured.err


def test_cli_reads_rule_ceiling_and_draw_count_from_the_prereg(tmp_path, capsys):
    dev = _dev_set(tmp_path, _rows())
    draws = _cli_draws(tmp_path, sha256_file(dev))
    out = tmp_path / "o.json"
    # rank ceiling that is not the rule's
    assert controls.main(_argv("c1", draws, dev, out, prereg=_prereg(tmp_path, "c1", len(draws), ceiling=6))) == controls.EXIT_UNUSABLE
    assert "rank ceiling" in capsys.readouterr().err
    # c1: the number of draw files must be draws_r
    assert controls.main(_argv("c1", draws, dev, out, prereg=_prereg(tmp_path, "c1", len(draws) + 1))) == controls.EXIT_UNUSABLE
    assert "draws_r" in capsys.readouterr().err
    # no --rule flag any more
    with pytest.raises(SystemExit):
        controls.main(["--rule", "c1", "--dev-set", str(dev), "--draw", draws[0]])


def test_c2_does_not_count_draws(tmp_path):
    dev = _dev_set(tmp_path, _rows())
    draws = _cli_draws(tmp_path, sha256_file(dev))
    assert controls.main(_argv("c2", draws, dev, tmp_path / "o.json", prereg=_prereg(tmp_path, "c2", 99))) == 0


def test_cli_refuses_a_dev_set_that_is_not_its_frozen_version(tmp_path, capsys):
    dev = _dev_set(tmp_path, _rows())
    draws = _cli_draws(tmp_path, sha256_file(dev))
    out = tmp_path / "o.json"
    assert controls.main(_argv("c1", draws, dev, out, freeze=False)) == controls.EXIT_UNUSABLE  # never frozen
    assert "not its frozen version" in capsys.readouterr().err
    assert not out.exists()
    assert controls.main(_argv("c1", draws, dev, out)) == 0  # frozen now (the realistic one is the second dev set)
    changed = _dev_set(tmp_path, _rows() + [_v2("f0002dddd", 0, "legacy")], name="changed.jsonl")  # a later version
    other = _cli_draws(tmp_path, sha256_file(changed))
    assert controls.main(_argv("c1", other, changed, tmp_path / "p.json", freeze=False)) == controls.EXIT_UNUSABLE
    assert "not its frozen version" in capsys.readouterr().err
    assert not (tmp_path / "p.json").exists()


def test_both_development_splits_are_required_each_exactly_once(tmp_path, capsys):
    dev = _dev_set(tmp_path, _rows())
    draws = _cli_draws(tmp_path, sha256_file(dev))
    realistic = _realistic_set(tmp_path)
    out = tmp_path / "o.json"
    assert controls.main(_argv("c1", draws, dev, out, dev_sets=[dev])) == controls.EXIT_UNUSABLE  # realistic omitted
    assert "both development splits" in capsys.readouterr().err
    assert controls.main(_argv("c1", draws, dev, out, dev_sets=[realistic])) == controls.EXIT_UNUSABLE  # tuning omitted
    assert "both development splits" in capsys.readouterr().err
    assert controls.main(_argv("c1", draws, dev, out, dev_sets=[dev, dev, realistic])) == controls.EXIT_UNUSABLE  # a split twice
    assert "given twice" in capsys.readouterr().err
    assert not out.exists() and _ledger().controls_record() is None
    assert controls.main(_argv("c1", draws, dev, out)) == 0


def test_the_prereg_must_be_the_epochs_registered_one(tmp_path, capsys):
    dev = _dev_set(tmp_path, _rows())
    draws = _cli_draws(tmp_path, sha256_file(dev))
    out = tmp_path / "o.json"
    registered = _prereg(tmp_path, "c1", len(draws))
    argv = _argv("c1", draws, dev, out, prereg=registered)  # freezes both splits under `registered`
    replacement = tmp_path / "replacement.json"
    replacement.write_text(registered.read_text(encoding="utf-8") + "\n", encoding="utf-8")  # other bytes, same meaning
    swapped = [str(replacement) if a == str(registered) else a for a in argv]
    assert controls.main(swapped) == controls.EXIT_UNUSABLE
    assert "not this epoch's registered prereg" in capsys.readouterr().err
    assert not out.exists() and _ledger().controls_record() is None
    assert controls.main(argv) == 0  # the registered one passes


def test_a_ledger_with_no_registered_prereg_refuses(tmp_path, capsys):
    dev = _dev_set(tmp_path, _rows())
    draws = _cli_draws(tmp_path, sha256_file(dev))
    realistic = _realistic_set(tmp_path)
    ledger = _ledger()
    for split, path in (("tuning", dev), ("realistic", realistic)):  # frozen, but under no recorded prereg
        ledger.record_freeze(split=split, set_sha256=sha256_file(path), totals={"families": 1})
    assert ledger.epoch_prereg_sha256() is None
    out = tmp_path / "o.json"
    assert controls.main(_argv("c1", draws, dev, out, freeze=False)) == controls.EXIT_UNUSABLE
    assert "not this epoch's registered prereg" in capsys.readouterr().err and not out.exists()
