"""Shared synthetic world for the Phase 16A-2a a5 slot-run tests (not a test module).

One sealed set (8 families, 2 phrasings each), two development sets, the
freeze/approval/twins/exclusion records, a prereg and a ledger, all invented and
schema-valid, all under ``tmp_path``. Every text-bearing value (question, gap
keyword, evidence label, rewrite, intent, answer, judge claim) carries a
``P16-CANARY`` marker built from seeded invented words, so a leak is a plain
window test (``tests/p16_canary.assert_no_leak``). The fake models never touch
a network: ``World.run`` injects fakes at the evaluator's seams, and a test can
add a socket spy to prove it.
"""

from __future__ import annotations

import json
import random
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import pytest
from langchain_core.documents import Document

import src.evaluator as ev
from src import eval_privacy, eval_schema, eval_sets, sealed_run
from src.eval_schema import row_hash
from src.eval_sealed_read import load_sealed
from src.query_rewrite import REWRITE_MODEL, STATUS_LIVE, Expansion
from src.sealed_ledger import SealedLedger
from tests.p16_capture import ANSWER, PARTIAL, PROVENANCE, REFUSE, FakeGeneration
from tests.test_p16w_spend import make_meter

SECTIONS = ["901.1", "901.2", "902.7.3"]
HELD = "held-out"
SHEET = "a" * 64
_CONS, _VOW = "bcdfghjklmnpqrstvwxz", "aeiou"
TRACING_VARS = ("LANGSMITH_TRACING_V2", "LANGCHAIN_TRACING_V2", "LANGSMITH_TRACING", "LANGCHAIN_TRACING")

#: family suffix -> (scope, source, (register of phrasing 0, register of phrasing 1))
SEALED_SPEC: List[Tuple[str, str, Tuple[str, str]]] = [
    ("answer", "tutorial", ("lay", "terse")),
    ("answer", "handbook", ("modelled", "modelled")),
    ("answer", "handbook", ("lay", "lay")),
    ("answer", "colleague", ("terse", "lay")),
    ("answer", "tutorial", ("modelled", "lay")),
    ("partial", "handbook", ("lay", "terse")),
    ("refuse", "handbook", ("lay", "lay")),
    ("refuse", "tutorial", ("terse", "terse")),
]
#: twins edges among sealed families: groups {0,1} and {2,3}; the other four are singletons.
SEALED_EDGES = [(0, 1), (2, 3)]


class SlotCanaries:
    """Seeded invented words and ``P16-CANARY-<field>-<word>`` markers."""

    def __init__(self, seed: int = 1620) -> None:
        self._rng = random.Random(seed)
        self.fields = {
            name: f"P16-CANARY-{name}-{self.word()}"
            for name in ("gap", "evidence", "rewrite", "intent", "answer", "claim")
        }
        self.questions: List[str] = []

    def word(self, pairs: int = 3) -> str:
        return "".join(self._rng.choice(_CONS) + self._rng.choice(_VOW) for _ in range(pairs))

    def question(self, tag: str) -> str:
        words = " ".join(self.word(2) for _ in range(9))
        text = f"P16-CANARY-q-{tag}-{self.word()} {words} P16-CANARY-end-{self.word()}?"
        self.questions.append(text)
        return text

    def secrets(self) -> List[str]:
        return [*self.fields.values(), *self.questions]


class SlotRetrieval:
    """Ranked synthetic chunks; ``hit(mode, question)`` decides whether the expected section is at rank 1."""

    def __init__(self, section_of: Dict[str, str], hit: Optional[Callable[[str, str], bool]] = None) -> None:
        self.section_of = section_of
        self.hit = hit or (lambda mode, question: True)

    def results(self, mode: str, question: str, top_k: int) -> List[Dict[str, Any]]:
        ranked = [f"800.{i}" for i in range(top_k)]
        section = self.section_of.get(question)
        if section is not None and self.hit(mode, question):
            ranked[0] = section
        out = []
        for rank, sec in enumerate(ranked):
            meta = {
                "section_number": sec, "chapter_number": sec.split(".")[0], "chapter_title": "Synthetic chapter",
                "heading": "Synthetic heading", "page_start": 10 + rank, "page_end": 10 + rank,
                "source": "synthetic.pdf", "title": "Synthetic Handbook", "document_type": "handbook",
            }
            doc = Document(page_content=f"Synthetic chunk {sec}.", metadata=meta, id=f"syn-{sec}-{rank}")
            out.append({"document": doc, "score": 1.0 - 0.05 * rank, "metadata": meta})
        return out

    def factory(self, top_k_default: int = 6) -> Callable[[str], Callable[..., List[Dict[str, Any]]]]:
        def _factory(mode: str) -> Callable[..., List[Dict[str, Any]]]:
            def _fn(question: str, top_k: int = top_k_default) -> List[Dict[str, Any]]:
                return self.results(mode, question, top_k)

            return _fn

        return _factory


def isolate_env(monkeypatch: Any, *names: str) -> None:
    """Leave ``names`` unset now, and restore them to unset at teardown even if the test writes
    ``os.environ`` directly (as ``scrub_tracing_env`` and ``load_dotenv`` do)."""
    for name in names:
        monkeypatch.setenv(name, "placeholder")
        monkeypatch.delenv(name)


def _write_jsonl(path: Path, rows: List[Dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


def sha_of(path: Path) -> str:
    return eval_sets.sha256_file(path)


@dataclass
class World:
    root: Path
    private: Path
    canaries: SlotCanaries
    sealed_path: Path
    golden_path: Path
    realistic_path: Path
    inventory_path: Path
    records_dir: Path
    prereg_path: Path
    sealed_rows: List[Dict[str, Any]]
    dev_rows: List[Dict[str, Any]]
    section_of: Dict[str, str]
    refusal_questions: set
    partial_questions: set
    families: List[str]
    group_of: Dict[str, str]
    prereg: Dict[str, Any] = field(default_factory=dict)

    # -- records ----------------------------------------------------------------

    def ledger(self) -> SealedLedger:
        """A fresh view of the epoch's ledger (every call is a 'restarted process')."""
        return sealed_run.default_ledger()

    def write_prereg(self, slots: Optional[Dict[str, Any]] = None, floor: Tuple[int, int] = (6, 4)) -> Path:
        self.prereg = {
            "version": 1,
            "floor": {"families": floor[0], "eligible": floor[1]},
            "slots": slots if slots is not None else {"0": {"expansion": "live"}, "0b": {"expansion": "live"}},
        }
        self.prereg_path.write_text(json.dumps(self.prereg), encoding="utf-8")
        return self.prereg_path

    def write_records(self, *, approve: bool = True, exclusions: Optional[List[str]] = None) -> None:
        rows = load_sealed(self.sealed_path)
        approved = {row_hash(r): SHEET for r in rows} if approve else {}
        self.records_dir.mkdir(parents=True, exist_ok=True)
        (self.records_dir / "approvals.json").write_text(json.dumps({"version": 1, "approved": approved}))
        edges = [[self.families[a], self.families[b]] for a, b in SEALED_EDGES]
        (self.records_dir / "twins.json").write_text(json.dumps({"edges": edges, "batches": {}}))
        (self.records_dir / "exclusions.json").write_text(json.dumps({"version": 1, "groups": exclusions or []}))

    def freeze(self) -> None:
        self.ledger().record_freeze(
            split="sealed", set_sha256=sha_of(self.sealed_path), totals={"families": len(self.families)},
            twins_sha256=sha_of(self.records_dir / "twins.json"),
            exclusions_sha256=sha_of(self.records_dir / "exclusions.json"),
        )

    # -- fakes ------------------------------------------------------------------

    def retrieval(self, hit: Optional[Callable[[str, str], bool]] = None) -> SlotRetrieval:
        return SlotRetrieval(self.section_of, hit)

    def generator(self, retrieval: SlotRetrieval, *, on_call: Optional[Callable[[str], None]] = None) -> Callable:
        def pick(question: str, is_refusal_row: bool) -> str:
            return REFUSE if is_refusal_row else PARTIAL if question in self.partial_questions else ANSWER

        base = FakeGeneration(retrieval, pick)
        base.refusal_qs = set(self.refusal_questions)

        def fn(question: str) -> Dict[str, Any]:
            if on_call is not None:
                on_call(question)
            out = base(question)
            if question in self.refusal_questions:
                return out
            # score_partial reads grounded citations as {"para": ...} dicts (the real shape).
            out["citation_check"] = {k: [{"para": x} for x in v] for k, v in out["citation_check"].items()}
            out["answer"] = f"{self.canaries.fields['answer']} {out['answer']}"
            if question in self.partial_questions:
                out["answer"] += f" The handbook does not say {self.canaries.fields['gap']}."
            return out

        return fn

    def judge(self) -> Callable[[Dict[str, str]], str]:
        claim = self.canaries.fields["claim"]

        def fn(prompt_vars: Dict[str, str]) -> str:
            unsupported = sum(map(ord, prompt_vars["question"])) % 3 == 0
            claims = [
                {"claim": f"{claim} one", "verdict": "supported"},
                {"claim": f"{claim} two", "verdict": "unsupported" if unsupported else "supported"},
            ]
            return json.dumps({"claims": claims})

        return fn

    def expand(self) -> Callable[..., Expansion]:
        rewrite, intent = self.canaries.fields["rewrite"], self.canaries.fields["intent"]

        def fn(question: str, *args: Any, **kwargs: Any) -> Expansion:
            return Expansion(question, (f"{rewrite} one", f"{rewrite} two"), REWRITE_MODEL, STATUS_LIVE, intent)

        return fn

    # -- running ----------------------------------------------------------------

    @property
    def specs(self) -> List[Tuple[str, str]]:
        return [("tuning", str(self.golden_path)), (HELD, str(self.sealed_path)), ("realistic", str(self.realistic_path))]

    def token(self, slot: Any = 0, *, expansion: str = "live", rewrite_candidate: Optional[str] = None,
              meter: Any = "default", baseline_run: Optional[str] = None, ledger: Optional[SealedLedger] = None):
        return sealed_run.acquire_slot_token(
            slot, ledger=ledger or self.ledger(), set_path=self.sealed_path, prereg_path=self.prereg_path,
            records_dir=self.records_dir, expansion=expansion, rewrite_candidate=rewrite_candidate,
            meter=make_meter() if meter == "default" else meter, baseline_run=baseline_run,
        )

    def run(self, slot: Any = 0, *, token: Any = None, modes: Optional[List[str]] = None, top_k: int = 6,
            skip_refusals: bool = False, skip_completeness: bool = False, judge: bool = True,
            judge_sample: Optional[int] = None, hit: Optional[Callable[[str, str], bool]] = None,
            scan: Optional[Callable[[str], Any]] = None, generate: Optional[Callable] = None,
            judge_fn: Optional[Callable] = None, out: Callable[[str], None] = print, expansion: str = "live",
            rewrite_candidate: Optional[str] = None, baseline_run: Optional[str] = None,
            specs: Optional[List[Tuple[str, str]]] = None, meter: Any = "default",
            matrix_extra: Optional[Dict[str, Any]] = None) -> sealed_run.SlotOutcome:
        """Gate + claim + run + store + finish for one slot, with every model faked."""
        meter_obj = make_meter() if meter == "default" else meter
        tok = token or self.token(slot, expansion=expansion, rewrite_candidate=rewrite_candidate,
                                  meter=meter_obj, baseline_run=baseline_run)
        retrieval = self.retrieval(hit)
        kwargs: Dict[str, Any] = {
            "retrieve_fn_factory": retrieval.factory(6),
            "generate_fn": generate or self.generator(retrieval),
            "judge_fn": judge_fn or self.judge(),
            "provenance_fn": lambda: dict(PROVENANCE),
        }
        kwargs.update(matrix_extra or {})
        return sealed_run.run_slot(
            tok, ledger=self.ledger(), records_dir=self.records_dir, set_specs=specs or self.specs,
            modes=modes or list(ev.EVAL_MODES), top_k=top_k, skip_refusals=skip_refusals,
            skip_completeness=skip_completeness, judge=judge, judge_sample=judge_sample,
            persist_directory="unused", meter=meter_obj, expansion=expansion, matrix_kwargs=kwargs,
            scan=scan, out=out,
        )

    # -- inspection -------------------------------------------------------------

    def run_dir(self, run_id: str) -> Path:
        return eval_privacy.sealed_run_dir(run_id, create=False)

    def stored_doc(self, slot: Any) -> Dict[str, Any]:
        rec = self.ledger().recover(slot)
        return json.loads((self.run_dir(rec.run_id) / sealed_run.RESULTS_FILE).read_text())

    def ids(self) -> List[str]:
        """Every opaque id a sealed row can carry: row ids, family ids (= group ids)."""
        return sorted({r["id"] for r in self.sealed_rows} | set(self.families))

    def files_outside_sealed(self, *, skip_ledger: bool = False) -> List[Path]:
        """Every file in the tmp world outside ``eval/private/sealed/`` except the test's own inputs."""
        sealed_root = (self.private / "sealed").resolve()
        inputs = {self.golden_path, self.realistic_path, self.inventory_path}
        found = []
        for p in sorted(self.root.rglob("*")):
            if not p.is_file() or p in inputs or sealed_root in p.resolve().parents:
                continue
            if skip_ledger and "state" in p.relative_to(self.root).parts:
                continue
            found.append(p)
        return found


def make_world(root: Path, monkeypatch: Any) -> World:
    """Build the synthetic world under ``root`` (the suite's private root is ``root/eval/private``)."""
    private = eval_privacy.private_root()
    canaries = SlotCanaries()
    reg = root / "_registry" / "sets.json"
    reg.parent.mkdir(parents=True, exist_ok=True)
    reg.write_text(json.dumps({"version": 1, "sets": []}), encoding="utf-8")
    monkeypatch.setattr(eval_sets, "REGISTRY_PATH", reg)
    monkeypatch.setattr(eval_sets, "LEGACY_PUBLIC_PATH", root / "eval" / "legacy_public.json")
    inventory = root / "_registry" / "section_inventory.json"
    inventory.write_text(json.dumps({
        "version": 1, "map_sha256": "0" * 64, "sections": [*SECTIONS, canaries.fields["evidence"]], "aliases": [],
    }), encoding="utf-8")
    monkeypatch.setattr(eval_schema, "INVENTORY_PATH", inventory)
    monkeypatch.setattr(ev, "_load_absorbed_map", lambda: (None, None))
    isolate_env(monkeypatch, *TRACING_VARS)
    try:
        from langsmith import utils as ls_utils

        ls_utils.get_env_var.cache_clear()
    except ImportError:  # pragma: no cover
        pass

    families = [f"f1000000{i}" for i in range(len(SEALED_SPEC))]
    sealed_rows: List[Dict[str, Any]] = []
    section_of: Dict[str, str] = {}
    refusal_q, partial_q = set(), set()
    for i, (scope, source, registers) in enumerate(SEALED_SPEC):
        for n in (0, 1):
            question = canaries.question(f"s{i}{n}")
            row: Dict[str, Any] = {
                "schema": 2, "id": f"{families[i]}-{n}", "family_id": families[i], "question": question,
                "scope": scope, "evidence": [], "register": registers[n], "source": source,
            }
            if scope != "refuse":
                section = canaries.fields["evidence"] if i == 0 else SECTIONS[i % len(SECTIONS)]
                row["evidence"] = [[section]]
                section_of[question] = section
            else:
                refusal_q.add(question)
            if scope == "partial":
                row["gaps"] = [{"id": "g1", "keywords": [canaries.fields["gap"]]}]
                partial_q.add(question)
            sealed_rows.append(row)
    sealed_path = _write_jsonl(private / "sealed" / "frozen.jsonl", sealed_rows)

    dev_rows: List[Dict[str, Any]] = []
    paths = {}
    for name, prefix in (("golden", "f2000000"), ("realistic", "f3000000")):
        rows = []
        for i in range(4):
            question = canaries.question(f"{name}{i}")
            refuse = i == 3
            row = {
                "schema": 2, "id": f"{prefix}{i}-0", "family_id": f"{prefix}{i}", "question": question,
                "scope": "refuse" if refuse else "answer", "evidence": [] if refuse else [[SECTIONS[i % 3]]],
            }
            if refuse:
                refusal_q.add(question)
            else:
                section_of[question] = SECTIONS[i % 3]
            rows.append(row)
        dev_rows.extend(rows)
        paths[name] = _write_jsonl(root / "dev" / f"{name}.jsonl", rows)

    dump = root / "data" / "research" / "handbook_sections.jsonl"
    dump.parent.mkdir(parents=True, exist_ok=True)
    dump.write_text(json.dumps({"section": "1.1", "pages": [1, 1], "text": "General law of the synthetic handbook."}) + "\n")

    group_of = {}
    for a, b in SEALED_EDGES:
        group_of[families[a]] = group_of[families[b]] = families[min(a, b)]
    for fam in families:
        group_of.setdefault(fam, fam)
    world = World(
        root=root, private=private, canaries=canaries, sealed_path=sealed_path, golden_path=paths["golden"],
        realistic_path=paths["realistic"], inventory_path=inventory, records_dir=private / "sealed" / "work",
        prereg_path=root / "prereg.json", sealed_rows=sealed_rows, dev_rows=dev_rows, section_of=section_of,
        refusal_questions=refusal_q, partial_questions=partial_q, families=families, group_of=group_of,
    )
    monkeypatch.setattr(ev, "expand_query", world.expand())
    return world


def kinds(world: "World") -> List[str]:
    """The ledger's record kinds, oldest first."""
    return [r["kind"] for r in world.ledger().records()]


def gate_of(call: Callable[[], Any]) -> str:
    """The gate a call refuses with (the call must raise ``GateRefused``)."""
    with pytest.raises(sealed_run.GateRefused) as info:
        call()
    return info.value.gate


class Crash(BaseException):
    """A simulated process death: nothing in the run may catch or convert it."""


# ---------------------------------------------------------------------------
# Fixtures shared by the test_p16w_slot_run*.py modules (imported by name)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _langsmith_env_cache():
    """langsmith caches the tracing environment; never let one test's cached 'true' reach another."""
    from langsmith import utils as ls_utils

    ls_utils.get_env_var.cache_clear()
    yield
    ls_utils.get_env_var.cache_clear()


@pytest.fixture
def bare(tmp_path, monkeypatch):
    """The synthetic world with no prereg, records or freeze yet."""
    return make_world(tmp_path, monkeypatch)


@pytest.fixture
def world(bare):
    bare.write_prereg()
    bare.write_records()
    bare.freeze()
    return bare


@pytest.fixture
def socket_spy(monkeypatch):
    """A spy transport: every outbound connection attempt is recorded and refused."""
    attempts = []

    def spy(self, address):
        attempts.append(address)
        raise ConnectionRefusedError("spy transport")

    monkeypatch.setattr(socket.socket, "connect", spy)
    monkeypatch.setattr(socket.socket, "connect_ex", lambda self, address: attempts.append(address) or 111)
    return attempts


@pytest.fixture
def no_calls(monkeypatch):
    """Record any expansion, matrix run or model call; a refusal before any call leaves it empty."""
    calls = []
    monkeypatch.setattr(ev, "expand_query", lambda *a, **k: calls.append("expand"))
    real = ev.run_eval_matrix
    monkeypatch.setattr(ev, "run_eval_matrix", lambda *a, **k: calls.append("run") or real(*a, **k))
    return calls
