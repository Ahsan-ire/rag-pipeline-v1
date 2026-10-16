"""Synthetic world for the ``scripts/sealed_tool.py`` tests (Phase 16A-2a a3, D72).

Every question, section label, tag and id here is invented and schema-valid; nothing reads a real eval
set, the corpus or ``eval/private/``. The private root is the per-test ``tmp_path/eval/private``
(``tests/conftest.py``); the out-of-tree ledger is a per-test file. Questions are built from tokens that
are unique to one family and phrasing (``q``), so a lexical overlap exists only where a test plants it,
and a leak of a question into stdout is a plain substring test.

The *standard world* (``build_standard``) is built so the split is **forced**: 12 sealed-eligible
drafts fill the sealed quota exactly, because the 8 other drafts are tied to public ids (sealed-ineligible)
and exactly fill the development quotas. So tests know which families end up sealed.
"""

from __future__ import annotations

import csv
import io
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import pytest

import src.eval_sets as eval_sets
from scripts import sealed_tool as st
from src import eval_schema
from tests.test_p16w_sp_launch import GitRepos, git

CANARY = "zqxcanary8841"
SECTIONS = [f"{ch}.{n}" for ch in range(1, 10) for n in range(1, 21)] + ["CANARYSEC.1", "CANARYSEC.2"]
INVENTORY = {"version": 1, "map_sha256": "a" * 64, "sections": SECTIONS, "aliases": []}


def b1a_blocks(*, mwe: float, draws_r: int) -> Dict[str, Any]:
    """The prereg blocks b1a requires (primary metric, collapse, adoption, controls, slots, drafting rule), as registered."""
    first_r = f"first_{draws_r}_recorded_development_draws"
    return {
        "primary_metric": {
            "cutoff": 6, "metric": "strict_hit", "mode": "hybrid+rewrite", "test": "exact_cluster_signflip", "sided": "two",
            "multiplicity": "holm",
        },
        "collapse": {"rule": "all"},
        "adoption": {
            "rule": "a", "development_family_effect_min": mwe, "critical_control_flips_max": 0, "sealed_point_estimate_min": 0,
            "sealed_holm_significant_loss": "forbidden",
            "confirmatory_batch": {
                "slots": ["1", "2", "3", "4"], "k_max": 4, "holm_k": 4, "replays": "slot0_canonical", "spent_slot_counts": True,
                "state": "refused_until_16b_sealed_comparison_gate", "modes": ["replay", "candidate"],
                "mode_declared_by": "batch_declaration",
                "candidate_identity": "sha256_canonical_pipeline_config_checked_at_gate_before_claim",
                "embargo_lifted_by": "16b_reviewed_code_change_with_identity_check",
                "requires": ["controls_record", "judge_on"],
            },
            "development_evidence": {
                "metric": "strict_hit", "cutoff": 6, "collapse": "all", "mode": "hybrid+rewrite",
                "sets": ["tuning", "realistic"], "versions": "development_freeze_records", "draws": draws_r,
                "statistic": "mean_over_draws_of_family_difference", "pairing": "by_draw_index", "incumbent_draws": first_r,
                "candidate_draws": f"replay_mode_replays_incumbent_draw_artifacts_else_first_{draws_r}_recorded_own_draws",
            },
            "unsupported_claims": {
                "unit": "claim", "where": "candidate_sealed_slot", "against": "incumbent",
                "reference": "canonical_slot_0_judged_sealed_claims", "max_excess_abs": 0,
            },
            "control_flip": {
                "from": "strict_hit", "to": "miss", "cutoff": 6, "mode": "hybrid+rewrite", "draws": f"any_of_first_{draws_r}_recorded",
                "versions": "development_freeze_records",
            },
        },
        "controls": {
            "rule": "c1", "rank_ceiling": 3, "modes": ["hybrid", "hybrid+rewrite"], "draws": first_r, "draws_r": draws_r,
            "collapse": "all", "committed": "ledger_controls_record_before_batch_declaration", "binds_to": "development_freeze_records",
        },
        "slots": {
            "0": {"expansion": "live"}, "0b": {"expansion": "live"},
            **{str(n): {"expansion": "batch", "artifact": "slot0_canonical", "role": "confirmatory", "requires": "batch_declaration"}
               for n in (1, 2, 3, 4)},
        },
        "drafting_rule": {
            "attempt_1": {
                "refusal_share_target": 0.275, "refusal_share_counts": "pool_including_preassigned", "realistic_lay_min_share": 0.5,
                "refusal_drafts": "max(round_half_up(0.275_x_pool_total)_minus_preassigned_refusals,sum_of_split_deficits_to_25pct)",
                "realistic_lay_share_of": "new_drafts_registered_lay_or_terse", "band_unreachable": "b2_owner_stop",
            },
            "next_attempt": {"max_round_1_attempts": 3, "after_cap": "split_infeasible_stop"},
            "round_2": {"max_attempts": 3, "after_cap": "freeze_infeasible_stop", "refusal_drafts": "same_formula_over_round_2_pool"},
            "paraphrase": {"t1_t7_cap": 3},
        },
    }


def q(tag: str) -> str:
    """A 9-token question whose tokens are unique to ``tag`` (no accidental shared 8-token window)."""
    return " ".join(f"{tag}w{i}" for i in range(9))


def fid_of(n: int) -> str:
    return f"f{n:08x}"


def public_id(n: int) -> str:
    return f"q:{n:012x}"


def make_row(fid: str, n: int, *, scope: str = "answer", evidence: Optional[List[List[str]]] = None, gaps: Optional[list] = None,
             register: str = "lay", source: str = "handbook", sealed: bool = True, question: Optional[str] = None,
             ambiguous: bool = False, **extra: Any) -> Dict[str, Any]:
    row: Dict[str, Any] = {
        "schema": 2, "id": f"{fid}-{n}", "family_id": fid, "question": question or q(f"{fid[-4:]}p{n}"), "scope": scope,
        "evidence": [] if scope == "refuse" else (evidence or [["1.1"]]), "register": register, "source": source,
        "ambiguous": ambiguous,
    }
    if scope == "partial":
        row["gaps"] = gaps or [{"id": "g1", "keywords": [f"gk{fid[-4:]}"]}]
    if sealed:
        row["sealed"] = True
    row.update(extra)
    return row


def write_rows(path: Path, rows: Iterable[Dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


def read_rows(path: Path) -> List[Dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").split("\n") if line.strip()]


@dataclass
class Spec:
    fid: str
    scope: str = "answer"
    phrasings: int = 2
    register: str = "lay"
    source: str = "handbook"
    section: str = ""
    tag: Optional[str] = None
    extra_groups: List[List[str]] = field(default_factory=list)
    gaps: Optional[list] = None
    first_question: Optional[str] = None

    def rows(self, *, sealed: bool, first_question: Optional[str] = None) -> List[Dict[str, Any]]:
        first_question = first_question or self.first_question
        evidence = [[self.section], *self.extra_groups]
        out = []
        for n in range(self.phrasings):
            out.append(make_row(self.fid, n, scope=self.scope, evidence=evidence, gaps=self.gaps, register=self.register,
                                source=self.source, sealed=sealed, question=first_question if (n == 0 and first_question) else None))
        return out


class World:
    """Paths, builders and a ``run`` that calls ``sealed_tool.main`` with captured, allowlisted output."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repos: GitRepos) -> None:
        self.tmp = tmp_path
        self.repos = repos
        self.repo = repos.work
        self.root = tmp_path / "eval" / "private"
        self.sealed = self.root / "sealed"
        self.dev = self.root / "dev"
        self.sp_dir = tmp_path / "sp_out"  # SP output staged outside the private root
        # `audit` refuses without a handbook dump (its exemption would silently be empty): a tiny synthetic one, no real text
        self.handbook_dump = tmp_path / "handbook_dump.jsonl"
        self.handbook_dump.write_text(json.dumps({"label": "ZZ.0", "page_start": 1, "page_end": 1, "text": "synthetic filler"}) + "\n", encoding="utf-8")
        self.prereg_path = self.repo / "eval" / "prereg_16a2.json"
        self._next = 0x100
        self._sec = 0
        self.tags: Dict[str, str] = {}
        self.pending: List[Spec] = []
        self.specs: Dict[str, Spec] = {}
        self.verdict_n = 0
        self.prereg_extra: Dict[str, Any] = {}  # extra prereg keys every `write_prereg` includes
        self.history: List[Tuple[List[str], int, str, str]] = []  # every run: (argv, exit code, stdout, stderr)
        monkeypatch.setenv("CC_SEALED_LEDGER", str(tmp_path / "state" / "sealed_ledger.jsonl"))
        inv = tmp_path / "_registry" / "section_inventory.json"
        inv.parent.mkdir()
        inv.write_text(json.dumps(INVENTORY), encoding="utf-8")
        monkeypatch.setattr(eval_schema, "INVENTORY_PATH", inv)
        reg = tmp_path / "_registry" / "sets.json"
        reg.write_text(json.dumps({"version": 1, "sets": []}), encoding="utf-8")
        monkeypatch.setattr(eval_sets, "REGISTRY_PATH", reg)
        monkeypatch.setattr(eval_sets, "LEGACY_PUBLIC_PATH", tmp_path / "eval" / "legacy_public.json")
        self.monkeypatch = monkeypatch
        self.sealed.mkdir(parents=True)
        (self.sealed / "work").mkdir()
        self.dev.mkdir(parents=True)

    # -- running -------------------------------------------------------------
    def run(self, *argv: str, **opts: str) -> Tuple[int, str, str]:
        import io as _io

        out, err = _io.StringIO(), _io.StringIO()
        glob = ["--repo", str(self.repo), "--prereg", str(self.prereg_path)]
        for key, value in {"handbook_dump": self.handbook_dump, **opts}.items():
            glob += [f"--{key.replace('_', '-')}", str(value)]
        code = st.main([*glob, *map(str, argv)], out=out, err=err)
        self.history.append((list(map(str, argv)), code, out.getvalue(), err.getvalue()))
        return code, out.getvalue(), err.getvalue()

    @staticmethod
    def fields(line: str) -> Dict[str, str]:
        """``kind k=v k=v`` -> {"_kind": kind, k: v}."""
        parts = line.strip().split(" ")
        out = {"_kind": parts[0]}
        out.update(dict(p.split("=", 1) for p in parts[1:]))
        return out

    def last(self, text: str, kind: str) -> Dict[str, str]:
        hits = [self.fields(ln) for ln in text.splitlines() if ln.startswith(kind + " ") or ln == kind]
        assert hits, f"no {kind!r} line in {text!r}"
        return hits[-1]

    def ledger(self) -> Any:
        return st.default_ctx(st.Printer(io.StringIO(), io.StringIO()), repo=self.repo, prereg_path=self.prereg_path).ledger()

    # -- families --------------------------------------------------------------
    def spec(self, scope: str = "answer", **kw: Any) -> Spec:
        fid = fid_of(self._next)
        self._next += 1
        if "section" not in kw:
            ch, n = self._sec % 9 + 1, self._sec // 9 + 1
            kw["section"] = f"{ch}.{n}"
            self._sec += 1
        spec = Spec(fid=fid, scope=scope, **kw)
        if spec.tag:
            self.tags[fid] = spec.tag
        self.specs[fid] = spec
        return spec

    def draft(self, scope: str = "answer", **kw: Any) -> str:
        spec = self.spec(scope, **kw)
        self.pending.append(spec)
        return spec.fid

    def flush_pool(self, name: str = "pool.jsonl") -> Path:
        rows = [r for s in self.pending for r in s.rows(sealed=True)]
        self.pending = []
        self.write_tags()
        return write_rows(self.sealed / "pool" / name, rows)

    def pinned(self, split: str, scope: str = "answer", file: Optional[str] = None, **kw: Any) -> str:
        spec = self.spec(scope, **kw)
        path = self.dev / (file or f"{split}_pinned.jsonl")
        existing = read_rows(path) if path.is_file() else []
        write_rows(path, [*existing, *spec.rows(sealed=False)])
        self.write_tags()
        return spec.fid

    def write_tags(self) -> None:
        (self.sealed / "work").mkdir(parents=True, exist_ok=True)
        (self.sealed / "work" / "tutorial_map.json").write_text(json.dumps(self.tags), encoding="utf-8")

    def verdict(self, pairs: Sequence[Tuple[str, str]] = (), name: Optional[str] = None) -> Path:
        self.verdict_n += 1
        path = self.sp_dir / (name or f"twins_{self.verdict_n}.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps({"a": a, "b": b, "verdict": "twin"}) + "\n" for a, b in pairs), encoding="utf-8")
        return path

    # -- prereg and git ---------------------------------------------------------
    def prereg_doc(self, **over: Any) -> Dict[str, Any]:
        quota = {"sealed": 12, "spare": 4, "floor_sealed": 8, "floor_answerable": 5, "realistic_min": 4, "tuning_min": 8}
        quota["examples"] = [{"pool_total": 24, "preassigned_realistic": 1, **st.compute_quotas(quota, 24, 1)}]
        power = {"alphas": [0.05, 0.0125], "discordance": [0.5], "planned_groups": [1] * 30 + [2] * 5, "draws": 60, "seed": 5}
        doc = {
            "version": 1, "branch": "main", "rounds": [1, 2],
            "seeds": {"split_r1": 11, "split_r2": 12, "repair": 13, "calibration": 14},
            "quota": quota, "band": {"min_pct": 25, "max_pct": 30}, "mwe": 0.1, "alpha": 0.05, "k_max": 4, "draws_r": 5,
            "min_share": {"realistic": {"tutorial_or_colleague": 0.25}}, "power": power,
            **b1a_blocks(mwe=0.1, draws_r=5),
        }
        doc.update(self.prereg_extra)
        doc.update(over)
        if "table" not in doc["power"]:
            doc["power"]["table"] = st.power_cells(doc["power"], doc["mwe"])
        if "family_table" not in doc["power"]:
            doc["power"]["family_table"] = st.family_cells(doc["power"], doc["mwe"])
        return doc

    def write_prereg(self, **over: Any) -> Dict[str, Any]:
        doc = self.prereg_doc(**over)
        self.prereg_path.parent.mkdir(parents=True, exist_ok=True)
        self.prereg_path.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        return doc

    def commit_prereg(self, push: bool = True) -> str:
        git(self.repo, "add", "eval/prereg_16a2.json")
        git(self.repo, "commit", "--quiet", "-m", "prereg")
        if push:
            git(self.repo, "push", "--quiet", "origin", "main")
        return git(self.repo, "rev-parse", "HEAD")

    # -- sheets ------------------------------------------------------------------
    def sheet_path(self, kind: str, name: str) -> Path:
        base = self.sealed / "work" if kind == "sealed" else self.root / "work"
        return base / "sheets" / f"{name}.csv"

    def mark_sheet(self, path: Path, marks: Dict[str, str], *, default: Optional[str] = None, extra: Optional[Dict[str, Dict[str, str]]] = None) -> Path:
        """Tick owner columns: ``marks`` maps row id or family id -> A/C/R/E; ``default`` ticks every other line."""
        with open(path, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            names, lines = list(reader.fieldnames or []), list(reader)
        for line in lines:
            mark = marks.get(line["row_id"]) or marks.get(line["family_id"]) or default
            if mark:
                line[mark] = "x"
            for key, value in ((extra or {}).get(line["row_id"]) or (extra or {}).get(line["family_id"]) or {}).items():
                line[key] = value
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=names, lineterminator="\n")
        writer.writeheader()
        writer.writerows(lines)
        path.write_text(buf.getvalue(), encoding="utf-8")
        return path

    def approve_all(self, kind: str, name: Optional[str] = None) -> None:
        name = name or f"all_{kind}_{self._next}"
        self._next += 1
        code, _, err = self.run("sheet", "--kind", kind, "--name", name)
        assert code == 0, err
        self.mark_sheet(self.sheet_path(kind, name), {}, default="A")
        code, _, err = self.run("apply", "--kind", kind, "--sheet", self.sheet_path(kind, name))
        assert code == 0, err

    # -- freeze plumbing -----------------------------------------------------------
    def recheck(self, split: str, twin_pairs: Sequence[Tuple[str, str]] = (), *, kind_ok: str = "recheck") -> Tuple[int, str, str]:
        """Run the freeze screen, fake the SP's recheck files, and run the freeze with them."""
        code, _, err = self.run("freeze", "--split", split)
        assert code == st.EXIT_REFUSED and "reason=recheck_missing" in err, err
        required = int(dict(p.split("=") for p in err.split()[1:] if "=" in p)["pairs_required"])
        verdict = self.verdict(twin_pairs, name=f"recheck_{split}_{self.verdict_n}.jsonl")
        ret = verdict.with_name(verdict.stem + ".return.json")
        ret.write_text(json.dumps({"status": "ok", "kind": kind_ok, "pairs_checked": required + 7, "twin_pairs": len(twin_pairs)}), encoding="utf-8")
        later = time.time() + 5
        for p in (verdict, ret):
            os.utime(p, (later, later))
        return self.run("freeze", "--split", split, "--recheck-verdict", verdict, "--recheck-return", ret)


@pytest.fixture
def repos(tmp_path, monkeypatch) -> GitRepos:
    empty = tmp_path / "empty.gitconfig"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for key, value in (("NAME", "t"), ("EMAIL", "t@example.invalid")):
        monkeypatch.setenv(f"GIT_AUTHOR_{key}", value)
        monkeypatch.setenv(f"GIT_COMMITTER_{key}", value)
    return GitRepos(tmp_path)


@pytest.fixture
def world(tmp_path, monkeypatch, repos) -> World:
    return World(tmp_path, monkeypatch, repos)


@dataclass
class Standard:
    """The forced standard world: which drafts must end sealed, which in development."""

    sealed: List[str]
    sealed_refuse: List[str]
    sealed_groups: List[List[str]]
    sealed_singletons: List[str]
    dev_drafts: List[str]
    pinned_tuning: List[str]
    pinned_realistic: List[str]
    verdict_pairs: List[Tuple[str, str]]


def _mark(w: World, fid: str, mark: str) -> None:
    """Put a canary into a family's free text: its first question (and its gap keywords if it is partial)."""
    spec = w.specs[fid]
    spec.first_question = f"{mark} {q(fid[-4:] + 'p0')}"
    if spec.scope == "partial":
        spec.gaps = [{"id": f"{mark}g", "keywords": [f"{mark}kw"]}]


def build_standard(w: World, sealed_mark: str = "", dev_mark: str = "") -> Standard:
    """24 families: 4 pinned, 12 forced-sealed drafts (3 refuse, three 2-groups, three singletons), 8 development drafts.

    With marks, every sealed-bound family carries ``sealed_mark`` and every development family ``dev_mark`` in
    its free text (questions, gap keywords and ids, tutorial tags, two section labels), for the leak tests.
    """
    pinned_t = [w.pinned("tuning", source="legacy", register="modelled") for _ in range(3)]
    pinned_r = [w.pinned("realistic", source="tutorial", register="lay", tag=f"{dev_mark.lower()}t01" if dev_mark else "t01")]
    refuse = [w.draft("refuse", register="modelled") for _ in range(3)]
    groups = []
    for g in range(3):
        tag = f"{sealed_mark.lower()}t{g}" if sealed_mark else f"t1{g}"
        groups.append([w.draft("answer", source="tutorial", tag=tag, register="modelled"),
                       w.draft("answer", source="tutorial", tag=tag, register="lay")])
    singles = []
    for i in range(3):
        kw = {"section": f"CANARYSEC.{i + 1}"} if sealed_mark and i < 2 else {}
        singles.append(w.draft("partial" if sealed_mark else "answer", register="terse" if i else "modelled", **kw))
    dev_refuse = [w.draft("refuse", register="lay") for _ in range(3)]
    dev_answer = [w.draft("answer", register="terse" if i % 2 else "lay") for i in range(5)]
    dev = dev_refuse + dev_answer
    flat_groups = [m for g in groups for m in g]
    sealed = refuse + flat_groups + singles
    if sealed_mark:
        for fid in sealed:
            _mark(w, fid, sealed_mark)
    if dev_mark:
        for fid in dev:
            _mark(w, fid, dev_mark)
    w.flush_pool()
    pairs = [(d, public_id(0xA00 + i)) for i, d in enumerate(dev)]
    return Standard(sealed, refuse, groups, singles, dev, pinned_t, pinned_r, pairs)


def standard_released(w: World, **marks: str) -> Standard:
    """Build the standard world, push the prereg, record the twins and release round 1."""
    std = build_standard(w, **marks)
    w.write_prereg()
    w.commit_prereg(push=True)
    verdict = w.verdict(std.verdict_pairs)
    code, out, err = w.run("twins", "--round", "1", "--verdict", verdict)
    assert code == 0, err
    code, out, err = w.run("split", "--round", "1")
    assert code == 0, err
    assert w.last(out, "split")["outcome"] == "released"
    return std
