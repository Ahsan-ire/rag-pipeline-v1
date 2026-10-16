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

import copy
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


#: The test worlds' (small) quota, and the pins ``prereg-check`` holds them to. ``eval/prereg_16a2.json`` has its own real
#: quota, pinned by ``sealed_tool.PINNED_VALUES``; ``World`` swaps that table for this one so the worlds' preregs, which are
#: valid but small, still pass ``prereg-check`` (``tests/test_p16a2b_b1a_r4_tool.py`` tests the real pins).
WORLD_QUOTA = {"sealed": 12, "spare": 4, "floor_sealed": 8, "floor_answerable": 5, "realistic_min": 4, "tuning_min": 8}
WORLD_PINS = (
    *((f"quota.{key}", value) for key, value in WORLD_QUOTA.items()),
    ("min_share.realistic.tutorial_or_colleague", 0.25),
)


def b1a_blocks() -> Dict[str, Any]:
    """The prereg's registered blocks (``mwe``, ``alpha``, ``k_max``, ``draws_r``, ``primary_metric``, ``collapse``,
    ``adoption``, ``controls``, ``slots``, ``drafting_rule``), complete, built from ``sealed_tool.REGISTERED_VALUES``:
    the one table ``prereg-check`` walks and ``eval/prereg_16a2.json`` must equal (a test walks the real file against it)."""
    out: Dict[str, Any] = {}
    for path, value in st.REGISTERED_VALUES:
        node = out
        *parents, last = path.split(".")
        for part in parents:
            node = node.setdefault(part, {})
        node[last] = copy.deepcopy(value)
    from src.judge import judge_identity

    for path, _name in st.COMPUTED_VALUES:  # the values the code computes (judge.config_hash), not typed in the table
        node = out
        *parents, last = path.split(".")
        for part in parents:
            node = node.setdefault(part, {})
        node[last] = judge_identity()["config_hash"]
    return out


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
        monkeypatch.setattr(st, "PINNED_VALUES", WORLD_PINS)
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
        quota = dict(WORLD_QUOTA)
        quota["examples"] = [{"pool_total": 24, "preassigned_realistic": 1, **st.compute_quotas(quota, 24, 1)}]
        power = {"alphas": [0.05, 0.0125], "discordance": [0.5], "planned_groups": [1] * 30 + [2] * 5, "draws": 60, "seed": 5}
        doc = {
            "version": 1, "branch": "main", "rounds": [1, 2],
            "seeds": {"split_r1": 11, "split_r2": 12, "repair": 13, "calibration": 14},
            "quota": quota, "band": {"min_pct": 25, "max_pct": 30},
            "min_share": {"realistic": {"tutorial_or_colleague": 0.25}}, "power": power,
            **b1a_blocks(),
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
