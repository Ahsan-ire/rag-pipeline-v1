"""Shared synthetic world for the Phase 16A-2a a5 slot-run tests (not a test module).

One sealed set (8 families, 2 phrasings each), two development sets, the
freeze/approval/twins/exclusion records, a prereg and a ledger, all invented and
schema-valid, all under ``tmp_path``. The records are in the formats ``sealed_tool``
writes and the slot gate reads: ``approvals.json`` as ``apply`` writes it, the group
record as the freeze-bound ``twins_sealed`` ledger payload, exclusions as ledger records. Every text-bearing value (question, gap
keyword, evidence label, rewrite, intent, answer, judge claim) carries a
``P16-CANARY`` marker built from seeded invented words, so a leak is a plain
window test (``tests/p16_canary.assert_no_leak``). The fake models never touch
a network: ``World.run`` injects fakes at the evaluator's seams, and a test can
add a socket spy to prove it.
"""

from __future__ import annotations

import hashlib
import json
import random
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import pytest
from langchain_core.documents import Document

import src.evaluator as ev
from src import eval_privacy, eval_schema, eval_sets, sealed_ledger, sealed_run
from src.eval_schema import row_hash
from src.eval_sealed_read import load_sealed
from src.query_rewrite import REWRITE_MODEL, STATUS_LIVE, Expansion
from src.sealed_ledger import SealedLedger, _dumps
from tests.p16_capture import ANSWER, PARTIAL, PROVENANCE, REFUSE, FakeGeneration
from tests.test_p16w_spend import make_meter

SECTIONS = ["901.1", "901.2", "902.7.3"]
HELD = "held-out"
SHEET = "a" * 64
BOUND_COMMIT, BOUND_PREREG_SHA = "c" * 40, "d" * 64  # what the tests' patched ``sealed_run.bind_prereg`` returns
CANDIDATES = [c * 64 for c in "1234"]  # a four-candidate batch (slot i runs candidate i)
BATCH = [{"config_hash": c, "expansion": "replay"} for c in CANDIDATES]  # ... all retrieval-only (replay slot 0's artifact)
BATCH_SLOTS = {  # the prereg's declaration of slots 1-4: the mode comes from the ledger's batch entry
    str(n): {"expansion": "batch", "artifact": "slot0_canonical", "role": "confirmatory", "requires": "batch_declaration"}
    for n in (1, 2, 3, 4)
}


def candidate_entry(config_hash: str, rewrite_config_hash: str) -> Dict[str, str]:
    """A rewrite-changing batch entry: the candidate's pipeline config hash and its rewrite identity hash."""
    return {"config_hash": config_hash, "expansion": "candidate", "rewrite_config_hash": rewrite_config_hash}


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
    pre_freeze_exclusions: Dict[str, List[str]] = field(default_factory=dict)
    twins_payload: Dict[str, Any] = field(default_factory=dict)

    # -- records ----------------------------------------------------------------

    def ledger(self) -> SealedLedger:
        """A fresh view of the epoch's ledger (every call is a 'restarted process')."""
        return sealed_run.default_ledger()

    def write_prereg(self, slots: Optional[Dict[str, Any]] = None, floor: Tuple[int, int] = (6, 4)) -> Path:
        self.prereg = {
            "version": 1,
            "quota": {"floor_sealed": floor[0], "floor_answerable": floor[1]},
            "slots": slots if slots is not None else {"0": {"expansion": "live"}, "0b": {"expansion": "live"}, **BATCH_SLOTS},
        }
        self.prereg_path.write_text(json.dumps(self.prereg), encoding="utf-8")
        return self.prereg_path

    def record_controls(self) -> None:
        """The ledger's ``controls`` record and the committed list it binds (a batch needs both), once."""
        ledger = self.ledger()
        if ledger.controls_record() is not None:
            return
        path = sealed_run.controls_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"version": 1}\n', encoding="utf-8")
        ledger.record_controls(controls_sha256=sha_of(path), draws_sha256="e" * 64, prereg_sha256=BOUND_PREREG_SHA)

    def declare_batch(self, n: int = 4, entries: Optional[List[Dict[str, str]]] = None) -> str:
        """Declare the first ``n`` of :data:`BATCH` (or ``entries``) as the confirmatory batch (after the freeze and
        the controls record)."""
        self.record_controls()
        return self.ledger().record_batch_declaration(entries if entries is not None else BATCH[:n])

    def ensure_batch(self, slot: Any) -> None:
        """Declare the default batch before a confirmatory slot (1-4) is first used, unless one exists."""
        if str(slot) in ("1", "2", "3", "4") and self.ledger().batch_declaration() is None:
            self.declare_batch()

    def write_records(self, *, approve: bool = True, exclusions: Optional[Any] = None,
                      twins: Optional[Dict[str, Any]] = None) -> None:
        """``approvals.json`` as ``sealed_tool apply`` writes it; stage the group record and the exclusions
        that ``freeze`` puts in the ledger (``twins`` replaces the group-record payload). ``exclusions`` is a
        list of group ids, or a ``{group id: [member families]}`` mapping (a list means the id alone)."""
        rows = load_sealed(self.sealed_path)
        approved = {r["id"]: {"row_hash": row_hash(r), "sheet_sha256": SHEET} for r in rows} if approve else {}
        self.records_dir.mkdir(parents=True, exist_ok=True)
        (self.records_dir / "approvals.json").write_text(json.dumps({
            "version": 1, "approved": approved, "pending_confirm": {}, "rejected": [], "escalated": [],
        }))
        edges = [[self.families[a], self.families[b]] for a, b in SEALED_EDGES]
        self.twins_payload = twins if twins is not None else {"version": 1, "edges": edges}
        self.pre_freeze_exclusions = dict(exclusions) if isinstance(exclusions, dict) else {g: [g] for g in exclusions or []}

    def freeze_dev(self, controls: bool = True) -> None:
        """The two development freezes, as ``sealed_tool freeze`` records them (once each): the sets a slot run's
        private inputs must be, bound to the same prereg as the sealed freeze. The critical-controls record, which
        every slot (0 and 0b included) needs, is made with them (``controls.py`` runs before slot 0)."""
        ledger = self.ledger()
        for split, path in (("tuning", self.golden_path), ("realistic", self.realistic_path)):
            if ledger.freeze_record(split) is None:
                ledger.record_freeze(
                    split=split, set_sha256=sha_of(path), totals={"families": 4}, prereg_sha256=BOUND_PREREG_SHA,
                )
        if controls:
            self.record_controls()

    def freeze(self, *, set_path: Optional[Path] = None, exclusions_sha256: Optional[str] = None,
               twins_names: Tuple[str, ...] = ("twins_sealed",), dev: bool = True, controls: bool = True) -> None:
        """The sealed freeze, as ``sealed_tool freeze`` records it: the group record is stored as a payload and
        bound by its sha256; the exclusions recorded so far are bound by digest. ``twins_names`` are the payload
        names it is stored under (``{sha16}`` stands for the first 16 hex digits of its sha256: the content name
        ``sealed_tool`` uses; the plain ``twins_sealed`` is the alias older readers load)."""
        if dev:
            self.freeze_dev(controls=controls)
        ledger = self.ledger()
        for group, members in self.pre_freeze_exclusions.items():
            ledger.exclude(group, "owner", members=members)
        twins_sha = hashlib.sha256(_dumps(self.twins_payload)).hexdigest()
        for name in twins_names:
            assert ledger.store_payload(name.format(sha16=twins_sha[:16]), self.twins_payload) == twins_sha
        ledger.record_freeze(  # exclusions_sha256=None: the ledger digests the exclusions inside its transaction
            split="sealed", set_sha256=sha_of(set_path or self.sealed_path), totals={"families": len(self.families)},
            twins_sha256=twins_sha, exclusions_sha256=exclusions_sha256, prereg_sha256=BOUND_PREREG_SHA,
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
              meter: Any = "default", baseline_run: Optional[str] = None, ledger: Optional[SealedLedger] = None,
              claim: bool = True, top_k: Optional[int] = 6, declare: bool = True):
        """The slot gate. ``claim=False`` mints the token unclaimed, as the CLI does (``run_slot`` claims it
        after its preflight). The gate itself never claims (``claim=True`` is refused), so this helper, unless told
        ``claim=False``, claims the minted token the way ``run_slot`` does after its preflight (``SlotToken.claim``),
        releasing the slot's lock if the claim is refused: tests that need a claimed, not yet run slot use it. A
        confirmatory slot gets the default four-candidate batch declared first unless ``declare=False``."""
        if declare:
            self.ensure_batch(slot)
        token = sealed_run.acquire_slot_token(
            slot, ledger=ledger or self.ledger(), set_path=self.sealed_path, prereg_path=self.prereg_path,
            records_dir=self.records_dir, expansion=expansion, rewrite_candidate=rewrite_candidate,
            meter=make_meter() if meter == "default" else meter, baseline_run=baseline_run, top_k=top_k,
        )
        if claim:
            try:
                token.claim()
            except BaseException:
                token.release()
                raise
        return token

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
                                  meter=meter_obj, baseline_run=baseline_run, top_k=top_k)
        retrieval = self.retrieval(hit)
        kwargs: Dict[str, Any] = {
            "retrieve_fn_factory": retrieval.factory(6),
            "generate_fn": generate or self.generator(retrieval),
            "judge_fn": judge_fn or self.judge(),
            "provenance_fn": lambda: dict(PROVENANCE),
        }
        kwargs.update(matrix_extra or {})
        return sealed_run.run_slot(
            tok, ledger=self.ledger(), set_specs=specs or self.specs,
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
    # the 16B embargo and candidate-identity check are code-level; these worlds exercise the batch and the confirmatory
    # slots behind them (each is tested by putting it back: the embargo True, the identity check the real stub)
    monkeypatch.setattr(sealed_ledger, "CONFIRMATORY_EMBARGO", False)
    monkeypatch.setattr(sealed_run, "check_candidate_identity", lambda *args, **kwargs: None)
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
    monkeypatch.setattr(sealed_run, "bind_prereg", lambda path: (BOUND_COMMIT, BOUND_PREREG_SHA))
    return world


def kinds(world: "World") -> List[str]:
    """The ledger's record kinds, oldest first (the two development freezes and the controls record the world adds are left out)."""
    return [
        r["kind"] for r in world.ledger().records()
        if not (r["kind"] == "freeze" and r["split"] != "sealed") and r["kind"] != "controls"
    ]


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
