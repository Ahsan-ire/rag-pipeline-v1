"""Phase 16A-2a, the whole sealed chain end to end (the integration of a1-a8, D72/D73).

One synthetic, schema-valid world in tmp dirs, a fake ``claude`` executable standing in for the sealed
processor (SP), fake retrieval, generation, judge and query expansion, a spy transport that refuses every
connection, and nothing read from the corpus, ``eval/private/`` or ``.env``. The chain, in order:

1. ``sp_launch``: a ``probe`` dispatch and a ``data`` ``draft`` dispatch (the fake SP writes the pool, the
   tutorial map and its return file); the launcher releases ``out/`` to ``sealed/work/sp_out/<id>/``;
2. ``sealed_tool intake`` (pool and tutorial tags), a ``twins`` dispatch and ``sealed_tool twins``;
3. ``sealed_tool split --round 1`` against a prereg committed and pushed to a bare origin;
4. ``sheet`` / ``apply``: the owner's approvals (``work/approvals.json``);
5. ``freeze --split sealed`` in two calls around a ``recheck`` dispatch;
6. ``pipeline eval --sealed-slot 0``: canonical v6, ``results_v6.md`` in the redirected eval directory;
7. ``audit``: clean; then a sealed question planted in a fake transcript is one hit with a family id,
   ``exclude`` records the whole group in the ledger, and a slot-1 gate sees that exclusion.

Each tool reads what the one before wrote, with no file renamed or converted by the test in between except
what the SP's contract says the SP writes. A canary is carried in the sealed families' free text (questions,
gap keywords, two section labels), in the development families' text, in the fake SP's stdout and stderr and in
every fake model output: none may reach a captured stdout or stderr, the log, ``results_v6.md`` or any file
outside ``eval/private/sealed/``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List

import pytest

import src.evaluator as ev
from scripts import sealed_tool as st
from scripts import sp_launch as sp
from src import eval_privacy, pipeline_config, sealed_ledger, sealed_run
from src.eval_sealed_read import load_sealed
from src.sealed_ledger import RecoveryAction
from tests import p16_slot as slot
from tests.p16_canary import assert_no_leak
from tests.p16_capture import PROVENANCE
from tests.p16_slot import TRACING_VARS, _langsmith_env_cache, isolate_env, socket_spy  # noqa: F401  (fixtures)
from tests.p16w_sealed_world import World, build_standard, read_rows, repos  # noqa: F401  (fixture)
from tests.test_p16w_sealed_tool import assert_allowlisted
from tests.test_p16w_sealed_tool_fuzz import assert_clean
from tests.test_p16w_sp_launch import env  # noqa: F401  (fixture)
from tests.test_p16w_spend import make_meter

ROOT = Path(__file__).resolve().parent.parent
SEALED_MARK = "SEALEDCANARY"
DEV_MARK = "DEVCANARY"
SP_CANARY = "SPSTREAMCANARY-4417-pelvanth"
PROBE_ENUMS = ["step_read", "step_mcp", "step_memory"]
RETURN_ENUMS = [*st.RETURN_ENUMS, *PROBE_ENUMS]
FAMILY = re.compile(r"\bf[0-9a-f]{8}\b")


class Chain(World):
    """The ``sealed_tool`` world, with the tool on the same ledger mirror as the launcher and ``pipeline eval``.

    The launcher and ``pipeline eval`` derive the mirror from the private root (``<eval dir>/sealed_ledger.jsonl``);
    ``sealed_tool`` derives it from ``--repo``. In production both are ``eval/sealed_ledger.jsonl``; here the repo is
    a clone elsewhere, so the tool is pointed at the same mirror explicitly.
    """

    def run(self, *argv: Any, **opts: Any):
        opts.setdefault("mirror", self.tmp / "eval" / "sealed_ledger.jsonl")
        return super().run(*argv, **opts)

    def ledger(self):
        return sealed_run.default_ledger()


@pytest.fixture
def chain(tmp_path, monkeypatch, repos) -> Chain:
    return Chain(tmp_path, monkeypatch, repos)


def pool_text(rows: List[Dict[str, Any]]) -> str:
    return "".join(json.dumps(r) + "\n" for r in rows)


def sha_text(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def slot_fakes(w: Chain, frozen: Path, golden: Path, realistic: Path) -> slot.World:
    """The slot-run test world's fakes (retrieval, generation, judge, expansion) over this chain's three sets."""
    section_of: Dict[str, str] = {}
    refusal, partial = set(), set()
    sealed_rows = load_sealed(frozen)
    dev_rows: List[Dict[str, Any]] = []
    for path in (golden, realistic):
        dev_rows += read_rows(path)
    for row in [*sealed_rows, *dev_rows]:
        if row["scope"] == "refuse":
            refusal.add(row["question"])
        else:
            section_of[row["question"]] = row["evidence"][0][0]
            if row["scope"] == "partial":
                partial.add(row["question"])
    return slot.World(
        root=w.tmp, private=w.root, canaries=slot.SlotCanaries(), sealed_path=frozen, golden_path=golden,
        realistic_path=realistic, inventory_path=w.tmp / "_registry" / "section_inventory.json",
        records_dir=w.sealed / "work", prereg_path=w.prereg_path, sealed_rows=sealed_rows, dev_rows=dev_rows,
        section_of=section_of, refusal_questions=refusal, partial_questions=partial,
        families=sorted({r["family_id"] for r in sealed_rows}), group_of={},
    )


def files_outside_sealed(tmp_path: Path, sealed_root: Path, skip: List[Path]) -> List[Path]:
    """Every file of the tmp world that is not under ``eval/private/sealed/`` (its ledger copy excepted), ``.git``,
    the planted transcript directory or one of ``skip`` (the fake SP's own scripted output)."""
    found = []
    for path in sorted(tmp_path.rglob("*")):
        if not path.is_file() or ".git" in path.parts or "claude_projects" in path.parts or path in skip:
            continue
        resolved = path.resolve()
        if sealed_root in resolved.parents and (sealed_root / "ledger") not in resolved.parents:
            continue  # sealed material may live here
        found.append(path)
    return found


@pytest.mark.filterwarnings("ignore:invalid escape sequence:DeprecationWarning")  # tests/p16_canary.py decodes raw file text
def test_the_whole_sealed_chain_runs_end_to_end_and_leaks_nothing(chain, env, repos, tmp_path, monkeypatch, capfd, caplog, socket_spy):
    caplog.set_level(logging.DEBUG)
    w = chain
    seen: List[str] = []  # every stdout / stderr text the chain printed, apart from sealed_tool's own history
    ids_counter = iter(range(0x7000, 0x8000))
    monkeypatch.setattr(st, "new_family_id", lambda: f"f{next(ids_counter):08x}")
    fake_sp_config = tmp_path / "fake_claude.json"  # the test's stand-in for the model holds its scripted output

    def sp_run(kind: str, ret: Dict[str, Any], files: Dict[str, str] = None, inputs=(), **kw) -> str:
        """One fake-SP dispatch through ``sp_launch.launch``; returns the dispatch id (read from the dispatch log)."""
        env.make_claude(canary=SP_CANARY, files=files or {}, ret=ret)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        outcome = env.launch(kind, inputs=list(inputs), enums=RETURN_ENUMS, **kw)
        seen.extend(t for t in (outcome.line, outcome.message) if t)
        assert outcome.exit_status == sp.EXIT_OK and outcome.line not in (None, sp.INVALID), outcome
        log = env.log("sp_dispatch.jsonl" if kind == "data" else "sp_probe.jsonl")
        assert log[-1]["exit_status"] == 0
        return log[-1]["dispatch_id"]

    def sp_out(dispatch: str) -> Path:
        return w.sealed / "work" / "sp_out" / dispatch

    # --- the world: prereg pushed first (a data dispatch needs it on the remote) --------------------------------
    std = build_standard(w, sealed_mark=SEALED_MARK, dev_mark=DEV_MARK)
    w.write_prereg()
    prereg_sha = w.commit_prereg(push=True)
    prereg_file_sha = hashlib.sha256(w.prereg_path.read_bytes()).hexdigest()
    code, out, err = w.run("prereg-check", "--committed")
    assert code == 0, err

    # the drafts the SP "writes" are the standard world's pool, under ids issued by `sealed_tool ids`
    ids_file = tmp_path / "inputs" / "ids.txt"
    code, out, err = w.run("ids", "--count", "24", "--out", ids_file)
    assert code == 0 and w.last(out, "ids")["count"] == "24", err
    issued = ids_file.read_text().split()
    pool_path = w.sealed / "pool" / "pool.jsonl"
    rows = read_rows(pool_path)
    pool_path.unlink()  # the standard world staged it in sealed/pool; in this chain only the SP and intake put it there
    drafts = sorted({r["family_id"] for r in rows})
    assert len(drafts) == 20
    new = dict(zip(drafts, issued))
    renamed = []
    for row in rows:
        fam = new[row["family_id"]]
        renamed.append({**row, "family_id": fam, "id": f"{fam}-{row['id'].rsplit('-', 1)[1]}"})
    draft_text = pool_text(renamed)
    sp_tags = {new[fid]: f"T1{g}" for g, group in enumerate(std.sealed_groups) for fid in group}
    tag_file = w.sealed / "work" / "tutorial_map.json"
    pinned_tags = {fid: tag for fid, tag in json.loads(tag_file.read_text()).items() if fid not in new}
    tag_file.write_text(json.dumps(pinned_tags), encoding="utf-8")  # the pinned (development) tags were there before
    assert pinned_tags  # the pinned realistic family's tag stays; the SP's tags arrive only through intake
    sealed_new = [new[f] for f in std.sealed]
    groups_new = [[new[f] for f in g] for g in std.sealed_groups]

    # --- 1. a probe dispatch, through the command line: only the validated line reaches stdout ------------------
    env.make_claude(canary=SP_CANARY, ret={"status": "ok", "kind": "probe", "step_read": "denied", "step_mcp": "absent", "step_memory": "absent"})
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert env.main(*[a for word in RETURN_ENUMS for a in ("--enum", word)], kind="probe") == sp.EXIT_OK
    captured = capfd.readouterr()
    seen += [captured.out, captured.err]
    assert json.loads(captured.out) == {"kind": "probe", "status": "ok", "step_mcp": "absent", "step_memory": "absent", "step_read": "denied"}
    assert captured.err == ""
    [probe] = env.log("sp_probe.jsonl")
    assert (w.sealed / "work" / "sp_out" / probe["dispatch_id"] / "return.json").is_file()
    assert w.ledger().records("dispatch") == []  # a probe is not an epoch event

    # --- 1b. the data `draft` dispatch: pool, tutorial map and return file ------------------------------------------
    def tally(key: str) -> Dict[str, int]:  # families by scope / register / source, as the brief's return file counts them
        firsts = [r[key] for r in renamed if r["id"].endswith("-0")]
        return {value: firsts.count(value) for value in sorted(set(firsts))}

    draft = sp_run(
        "data",
        {"status": "ok", "kind": "draft", "families": 20, "rows": len(renamed), "by_scope": tally("scope"),
         "by_register": tally("register"), "by_source": tally("source"), "sha256": sha_text(draft_text)},
        {"pool.jsonl": draft_text, "tutorial_map.json": json.dumps(sp_tags)},
        inputs=[ids_file], prereg=prereg_sha, branch="main", repo=repos.work,
    )
    assert re.fullmatch(r"[0-9a-f]{8}", draft)
    assert (sp_out(draft) / "pool.jsonl").read_text() == draft_text  # released where intake looks
    assert (w.sealed / "work" / f"{draft}.stdout.log").read_text().count(SP_CANARY) == 1  # the SP did talk; only to a log
    assert "ids.txt" in env.records()[-1]["tree"]  # and it was given exactly the ids as input

    # --- 2. intake, then the `twins` dispatch over the intaken pool, then `sealed_tool twins` -------------------------
    code, out, err = w.run("intake", "--dispatch", draft)
    assert code == 0 and out == f"intake families=20 rows={len(renamed)} tags=6\n", (out, err)
    assert not (sp_out(draft) / "pool.jsonl").exists() and (w.sealed / "pool" / f"{draft}.jsonl").read_text() == draft_text
    assert json.loads(tag_file.read_text()) == {**pinned_tags, **{f: t.lower() for f, t in sp_tags.items()}}
    verdict_pairs = [(new[d], pub) for d, pub in std.verdict_pairs]
    twins_text = "".join(json.dumps({"a": a, "b": b, "verdict": "twin"}) + "\n" for a, b in verdict_pairs)
    twins = sp_run(
        "data",
        {"status": "ok", "kind": "twins", "pairs_checked": 20 * 19 // 2 + 20 * 4, "twin_pairs": len(verdict_pairs),
         "pool_families": 20, "sha256": sha_text(twins_text)},
        {"twins.jsonl": twins_text}, inputs=[w.sealed / "pool", w.dev], prereg=prereg_sha, branch="main", repo=repos.work,
    )
    assert f"pool/{draft}.jsonl" in env.records()[-1]["tree"]  # the SP's next task is fed from the intaken pool
    code, out, err = w.run("twins", "--round", "1", "--verdict", sp_out(twins) / "twins.jsonl")
    assert code == 0, err
    assert w.last(out, "twins")["pool_families"] == "20" and w.last(out, "twins")["twin_pairs"] == "8"

    # --- 3. split round 1 against the pushed prereg ---------------------------------------------------------------
    code, out, err = w.run("split", "--round", "1")
    assert code == 0, err
    split = w.last(out, "split")
    assert split["outcome"] == "released" and (split["sealed"], split["tuning"], split["realistic"]) == ("12", "8", "4")
    assert {r["family_id"] for r in read_rows(w.sealed / "sealed_r1.jsonl")} == set(sealed_new)

    # --- 4. the owner's approvals: sheet, tick, apply ----------------------------------------------------------------
    w.approve_all("sealed")
    approvals = json.loads((w.sealed / "work" / "approvals.json").read_text())
    assert len(approvals["approved"]) == 24 and all(len(v["row_hash"]) == 64 for v in approvals["approved"].values())

    # --- 5. freeze --split sealed in two calls, with a fake SP's recheck between them --------------------------------
    code, out, err = w.run("freeze", "--split", "sealed")
    assert code == st.EXIT_REFUSED and "reason=recheck_missing" in err
    required = int(re.search(r"pairs_required=(\d+)", err).group(1))
    recheck = sp_run(
        "data", {"status": "ok", "kind": "recheck", "pairs_checked": required, "twin_pairs": 0},
        {"twins.jsonl": ""}, inputs=[w.sealed / "sealed_r1.jsonl", w.dev], prereg=prereg_sha, branch="main", repo=repos.work,
    )
    code, out, err = w.run(
        "freeze", "--split", "sealed", "--recheck-verdict", sp_out(recheck) / "twins.jsonl",
        "--recheck-return", sp_out(recheck) / "return.json",
    )
    assert code == 0, err
    frozen_line = w.last(out, "freeze")
    assert frozen_line["status"] == "frozen" and (frozen_line["families"], frozen_line["rows"]) == ("12", "24")
    frozen = w.sealed / "heldout-v2.jsonl"
    ledger = w.ledger()
    freeze = ledger.freeze_record("sealed")
    assert freeze["set_sha256"] == frozen_line["sha256"] and freeze["twins_sha256"] == frozen_line["twins_sha256"]
    assert not (w.sealed / "work" / "twins.json").exists() and not (w.sealed / "work" / "exclusions.json").exists()

    # --- 5b. the two development splits freeze too: a slot run's development inputs must be these versions (b1a) -----
    w.approve_all("development")
    for split in ("tuning", "realistic"):
        code, out, err = w.recheck(f"{split}-v2")
        assert code == 0, err
        assert w.last(out, "freeze")["status"] == "frozen"

    # --- 5c. the critical-controls list is recorded before slot 0 (r3 F6: every slot needs it; controls.py runs first) ---
    monkeypatch.setattr(pipeline_config, "index_identity", lambda persist_directory: "1" * 64)  # an index of fixed identity
    controls_bytes = b'{"version": 1}\n'  # a batch needs the controls recorded in the ledger and the file unchanged
    for controls_file in (w.repo / "eval" / "controls_v1.json", tmp_path / "eval" / "controls_v1.json"):  # tool / gate
        controls_file.write_bytes(controls_bytes)
    w.ledger().record_controls(
        controls_sha256=hashlib.sha256(controls_bytes).hexdigest(), draws_sha256="e" * 64,
        prereg_sha256=hashlib.sha256(w.prereg_path.read_bytes()).hexdigest(),
        incumbent_config_hash=pipeline_config.pipeline_config_hash("unused", top_k=6),  # this world's pipeline (index patched)
    )

    # --- 6. pipeline eval --sealed-slot 0, every model faked ---------------------------------------------------------
    golden, realistic = w.dev / "tuning-v2.jsonl", w.dev / "realistic-v2.jsonl"
    fakes = slot_fakes(w, frozen, golden, realistic)
    retrieval = fakes.retrieval()
    matrix_kwargs = {
        "retrieve_fn_factory": retrieval.factory(6), "generate_fn": fakes.generator(retrieval),
        "judge_fn": fakes.judge(), "provenance_fn": lambda: dict(PROVENANCE),
    }
    import src.pipeline as pipeline  # imported only now: it loads .env, which a dispatch must not see

    monkeypatch.setattr(ev, "_load_absorbed_map", lambda: (None, None))
    monkeypatch.setattr(ev, "expand_query", fakes.expand())
    isolate_env(monkeypatch, *TRACING_VARS)
    try:
        from langsmith import utils as ls_utils

        ls_utils.get_env_var.cache_clear()
    except ImportError:  # pragma: no cover
        pass
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-test-key")
    real_run_slot = sealed_run.run_slot
    monkeypatch.setattr(sealed_run, "run_slot", lambda token, **kw: real_run_slot(token, matrix_kwargs=matrix_kwargs, **kw))
    monkeypatch.setattr("sys.argv", [
        "prog", "eval", "--golden", str(golden), "--heldout", str(frozen), "--realistic", str(realistic),
        "--sealed-slot", "0", "--prereg", str(w.prereg_path), "--judge",
    ])  # no --slot-records: the default records directory is where `apply` wrote the approvals
    capfd.readouterr()
    eval_before = {p: p.stat().st_mtime_ns for p in (ROOT / "eval").rglob("*") if p.is_file()}
    code = 0
    try:
        pipeline.main()
    except SystemExit as exc:
        code = exc.code or 0
    captured = capfd.readouterr()
    seen += [captured.out, captured.err]
    assert code == 0, captured.err
    assert "[eval] sealed slot 0: canonical; published results_v6.md" in captured.out
    assert eval_before == {p: p.stat().st_mtime_ns for p in (ROOT / "eval").rglob("*") if p.is_file()}  # the repo's eval/ is untouched
    report_path = sealed_run.results_v6_path()
    assert report_path == tmp_path / "eval" / "results_v6.md" and report_path.is_file()
    assert not (tmp_path / "eval" / "results.md").exists()
    report = report_path.read_text()
    assert "canonical" in report.lower() and len(report) > 500
    assert ledger.recover(0).action is RecoveryAction.CONSUMED
    assert w.ledger().recover(0).run_id and (w.sealed / "runs" / w.ledger().recover(0).run_id / "results.json").is_file()

    # --- 7. audit: clean, then a sealed question in a fake transcript is one hit -----------------------------------------
    projects = tmp_path / "claude_projects"
    transcript = projects / st.mangle_path(w.repo) / "session.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(json.dumps({"content": "an unrelated note about a house purchase"}) + "\n", encoding="utf-8")
    code, out, err = w.run("audit", claude_projects=projects, staging_root=env.root)
    assert code == 0, out
    assert w.last(out, "audit")["status"] == "clean" and w.last(out, "audit")["hits"] == "0"
    assert int(w.last(out, "audit")["s_needles"]) > 0  # the sealed set really was a needle source: the scan ran
    scanned = {ln.split()[1].split("=")[1]: int(ln.split()[2].split("=")[1]) for ln in out.splitlines() if ln.startswith("audit_target")}
    assert scanned["transcript"] == 1 and scanned["dispatch"] == 2 and scanned["tree"] > 0  # twins.jsonl was not a source: no exit 2

    target_group = sorted(groups_new[0])
    question = next(r["question"] for r in read_rows(w.sealed / "sealed_r1.jsonl") if r["family_id"] == target_group[1])
    transcript.write_text(json.dumps({"content": question}) + "\n", encoding="utf-8")
    code, out, err = w.run("audit", claude_projects=projects, staging_root=env.root)
    assert code == st.EXIT_HIT
    assert f"audit_hit target=transcript family={target_group[1]} count=1" in out
    assert w.last(out, "audit")["status"] == "hits" and w.last(out, "audit")["hits"] == "1"
    code, out, err = w.run("exclude", "--family", target_group[1], "--reason", "s_hit")
    assert code == 0, err
    assert w.last(out, "exclude")["outcome"] == "ok" and w.last(out, "exclude")["members"] == "2"
    assert w.ledger().exclusions() == frozenset({target_group[0]})  # the whole group, named by its smallest family

    # a slot-1 gate (a confirmatory slot, paired with slot 0's stored run) sees the exclusion
    cand = "7" * 64
    monkeypatch.setattr(sealed_ledger, "CONFIRMATORY_EMBARGO", False)  # the 16B embargo is code-level; this chain goes behind it
    monkeypatch.setattr(sealed_run, "check_candidate_identity", lambda *a, **k: None)  # ... and behind 16B's identity check (a stub)
    from tests.test_p16a2b_b1a_tool import record_draws

    record_draws(w.ledger(), cand)  # r5: the batch's candidate has its development draws recorded
    code, out, err = w.run("declare-batch", "--candidate", f"{cand}:replay")  # slot 1 needs the batch declaration (b1a)
    assert code == 0 and w.last(out, "batch_declared")["candidates"] == "1", err
    baseline_id = w.ledger().recover(0).run_id
    artifact = eval_privacy.sealed_run_dir(baseline_id, create=False) / "expansion_artifact.json"  # slot 0's, replayed
    token = sealed_run.acquire_slot_token(
        1, ledger=w.ledger(), set_path=frozen, prereg_path=w.prereg_path, records_dir=w.sealed / "work",
        expansion=str(artifact), meter=make_meter(), baseline_run=baseline_id, top_k=6,
    )
    # the gate never claims (r3 F3): run_slot preflights a minted token and claims it with the receipt; done by hand here (r4 C)
    token.claim(sealed_run.preflight_slot(
        token, set_specs=[("golden", str(golden)), ("held-out", str(frozen)), ("realistic", str(realistic))],
        modes=list(ev.EVAL_MODES), top_k=6, skip_refusals=False, skip_completeness=False, judge=True, judge_sample=None,
        persist_directory="./chroma_db", meter=make_meter(), expansion=str(artifact), matrix_kwargs=matrix_kwargs,
    ))
    assert token.slot == "1" and token.excluded == frozenset({target_group[0]})
    assert token.candidate_config_hash == cand and token.prereg_sha256 == hashlib.sha256(w.prereg_path.read_bytes()).hexdigest()  # the real prereg binding
    assert token.prereg_commit == prereg_sha
    assert token.groups[target_group[1]] == target_group[0] == token.groups[target_group[0]]
    survivors = {r["family_id"] for r in read_rows(w.sealed / "sealed_r1.jsonl")}
    assert set(token.groups.values()) == set(st.sealed_group_index(w.ledger(), survivors))  # exclude's ids are the gate's ids
    assert token.records_still_bound()

    # --- the ledger: every record kind in order, dispatches included; the mirror carries kinds and digests only ---------
    records = w.ledger().records()
    assert [r["kind"] for r in records] == [
        "dispatch", "dispatch", "split_attempt", "release", "dispatch", "freeze", "freeze", "freeze", "controls",
        "slot_claim", "slot_stored", "publication_intent", "slot_consumed", "exclusion",
        *["dev_draw"] * 5,  # r5: the batch candidate's five recorded development draws
        "batch_declaration", "slot_claim",
    ]
    dispatches = [r for r in records if r["kind"] == "dispatch"]
    assert [d["dispatch_id"] for d in dispatches] == [d["dispatch_id"] for d in env.log("sp_dispatch.jsonl")] == [draft, twins, recheck]
    assert {d["dispatch_kind"] for d in dispatches} == {"data"}
    mirror = [json.loads(line) for line in (tmp_path / "eval" / "sealed_ledger.jsonl").read_text().splitlines()]
    assert [m["kind"] for m in mirror] == [r["kind"] for r in records]
    assert all(set(m) <= {"seq", "kind", "digest", "digests", "split"} for m in mirror)
    # a dispatch's mirror line carries the kind, its line digest and the prereg's sha256 (r3 F7), nothing else; the first
    # dispatch registered the epoch's prereg
    assert all(set(m) == {"seq", "kind", "digest", "digests"} for m in mirror if m["kind"] == "dispatch")
    assert all(m["digests"] == {"prereg_sha256": prereg_file_sha} for m in mirror if m["kind"] == "dispatch")
    assert records[0]["kind"] == "dispatch" and w.ledger().epoch_prereg_sha256() == prereg_file_sha

    # --- the validator accepts what the SP brief names: family ids and row ids ------------------------------------------
    ret = tmp_path / "ret_with_ids.json"
    ret.write_text(json.dumps({"status": "ok", "kind": "twins", "sha256": "0" * 64, "families": sealed_new[:2], "rows": [f"{sealed_new[0]}-1"]}))
    code, out, err = w.run("return", ret)
    assert code == 0 and json.loads(out)["families"] == sealed_new[:2]
    assert sp.main(["--sweep"]) == sp.EXIT_OK
    seen += list(capfd.readouterr())

    # --- nothing leaked ----------------------------------------------------------------------------------------------------
    sealed_questions = [r["question"] for r in read_rows(w.sealed / "sealed_r1.jsonl")]
    sealed_secrets = [
        *sealed_questions, SEALED_MARK, SP_CANARY, *fakes.canaries.secrets(),
        *[kw for r in read_rows(w.sealed / "sealed_r1.jsonl") for g in r.get("gaps", []) for kw in g["keywords"]],
    ]
    dev_questions = [r["question"] for path in (golden, realistic) for r in read_rows(path)]
    everything = [*sealed_secrets, *dev_questions, DEV_MARK]  # what no stdout, stderr or log line may carry

    assert_clean(w.history, caplog)  # sealed_tool: no canary, no opaque id off the audit lines, no unlisted value
    outputs = [(argv, out + err) for argv, _code, out, err in w.history if argv[0] != "return"]  # `return` prints a validated line
    for argv, text in outputs:
        assert_allowlisted(text, ids_ok=argv[0] == "audit")
    assert_no_leak(everything, *seen, caplog.text, *[text for _argv, text in outputs], where="(captured output)")
    joined = "\n".join([*seen, caplog.text, *[text for argv, text in outputs if argv[0] != "audit"]])
    for mark in (SEALED_MARK, DEV_MARK, SP_CANARY):
        assert mark.lower() not in joined.lower()
    assert not [i for i in [*sealed_new, *drafts] if i in joined]  # no family id in any output but audit's hit lines
    assert not FAMILY.findall(joined)
    assert socket_spy == []  # not one connection attempt

    sealed_root = w.sealed.resolve()
    outside = files_outside_sealed(tmp_path, sealed_root, skip=[fake_sp_config])
    rel = {p.relative_to(tmp_path).as_posix() for p in outside}
    for expected in ("eval/results_v6.md", "eval/sealed_ledger.jsonl", "state/sealed_ledger.jsonl", "state/sealed_payloads/twins_sealed.json",
                     "eval/private/sp_dispatch.jsonl", "eval/private/sp_probe.jsonl", "eval/private/dev/tuning_r1.jsonl"):
        assert expected in rel, expected  # the places a leak would be most likely are among the files checked
    assert len(outside) >= 12
    for path in outside:  # development files hold development text, so only sealed text is excluded everywhere
        assert_no_leak(sealed_secrets, path.read_bytes().decode("utf-8", errors="replace"), where=path.relative_to(tmp_path).as_posix())
    for path in (sealed_root / "ledger").rglob("*"):  # the private ledger copy and payloads are content-free too
        if path.is_file():
            assert_no_leak([*sealed_secrets, *dev_questions, DEV_MARK], path.read_text(encoding="utf-8"), where="sealed/ledger")
    # positive controls: the secrets really were inside sealed/
    sealed_text = "\n".join(p.read_text(errors="replace") for p in w.sealed.rglob("*") if p.is_file())
    assert SEALED_MARK in sealed_text and SP_CANARY in sealed_text and sealed_questions[0] in sealed_text
