"""Tests for src/spend.py — the eval API spend meter (Phase 16A-1 item 8, D70).

Acceptance (m), the meter's own parts. Synthetic data only: fake chat models
returning AIMessage with ``usage_metadata``, faked anthropic SDK exceptions
(httpx Request/Response objects built locally, nothing is sent), and a
per-test tmp ledger (tests/conftest.py sets CC_SPEND_LEDGER, autouse).
"""

from __future__ import annotations

import json
import multiprocessing
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, List, Optional

import anthropic
import httpx
import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.output_parsers import StrOutputParser
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.prompts import ChatPromptTemplate
from pydantic import Field

from src import spend
from src.spend import (
    LedgerRefused,
    MeteredChatModel,
    SpendLimitReached,
    SpendMeter,
    SpendMeterRequired,
    UnpricedModel,
    check_ledger_path,
    default_ledger_path,
    iso_week,
    load_prices,
    resolve_ledger_path,
    retry_decision,
)

PRICES_PATH = Path(__file__).parent / "fixtures" / "p16w_prices.toml"
MODELS = {"generation": "fake-gen", "rewrite": "fake-rewrite", "judge": "fake-judge"}
# A Wednesday in 2026-W41.
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
USAGE = {
    "input_tokens": 100,
    "output_tokens": 50,
    "total_tokens": 150,
    "input_token_details": {"cache_read": 20, "cache_creation": 10},
}
MESSAGES = [SystemMessage(content="sys"), HumanMessage(content="hi")]
# 3 + 2 bytes + 2 * 64 per-message overhead.
MESSAGES_WORST_INPUT = 133


# ---------------------------------------------------------------------------
# Fakes and helpers
# ---------------------------------------------------------------------------


class FakeChat(BaseChatModel):
    """Fake chat model: scripted failures, then an AIMessage with usage."""

    model: str = "fake-gen"
    max_tokens: int = 1000
    script: List[Any] = Field(default_factory=list)  # exceptions (or None = succeed), consumed in order
    usage: Optional[dict] = None
    reply: str = "ok"
    sent: List[str] = Field(default_factory=list)
    sent_log: Optional[str] = None  # cross-process record of sends
    crash: bool = False
    delay: float = 0.0  # seconds "in flight", keeps a reservation open

    @property
    def _llm_type(self) -> str:
        return "fake-chat"

    def _generate(self, messages: List[BaseMessage], stop=None, run_manager=None, **kwargs: Any) -> ChatResult:
        text = "\n".join(str(m.content) for m in messages)
        self.sent.append(text)
        if self.sent_log:
            fd = os.open(self.sent_log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            os.write(fd, b"sent\n")
            os.close(fd)
        if self.crash:
            os._exit(17)
        if self.delay:
            time.sleep(self.delay)
        if self.script:
            step = self.script.pop(0)
            if step is not None:
                raise step
        msg = AIMessage(
            content=self.reply,
            usage_metadata=self.usage,
            response_metadata={"stop_reason": "end_turn", "model": self.model},
        )
        return ChatResult(generations=[ChatGeneration(message=msg)])


def _usage() -> dict:
    return json.loads(json.dumps(USAGE))


def make_fakes(**overrides: Any) -> dict:
    """One FakeChat per kind; overrides apply to all three."""
    params = {"usage": _usage(), **overrides}
    return {kind: FakeChat(model=model, **params) for kind, model in MODELS.items()}


def make_meter(fakes: Optional[dict] = None, **kwargs: Any) -> SpendMeter:
    fakes = fakes if fakes is not None else make_fakes()
    kwargs.setdefault("clock", lambda: NOW)
    kwargs.setdefault("sleep", lambda s: None)
    kwargs.setdefault("rand", lambda: 0.0)
    return SpendMeter(
        load_prices(PRICES_PATH),
        kwargs.pop("ledger", None),
        kwargs.pop("run_limit_eur", None),
        models=kwargs.pop("models", MODELS),
        inner_factory=lambda kind: fakes[kind],
        **kwargs,
    )


def ledger_path() -> Path:
    return Path(os.environ["CC_SPEND_LEDGER"])


def ledger_lines() -> List[dict]:
    path = ledger_path()
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def seed(eur: float, *, week: str = "2026-W41", run_id: str = "seed-run", settled: bool = True) -> None:
    """Append a synthetic reservation (settled at ``eur``, or open at ``eur``)."""
    rid = uuid.uuid4().hex
    base = {"v": 1, "rid": rid, "week": week, "run_id": run_id, "model": "fake-gen", "kind": "generation"}
    lines = [{**base, "event": "reserve", "eur": eur + (1.0 if settled else 0.0)}]
    if settled:
        lines.append({**base, "event": "settle", "eur": eur})
    path = ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        for line in lines:
            fh.write(json.dumps(line) + "\n")


def gen_worst_eur(max_tokens: int, input_tokens: int = MESSAGES_WORST_INPUT) -> float:
    # fake-gen: max(input 1.0, cache_write 2.0) = 2.0 $/MTok in, 10 $/MTok out; usd_per_eur 2.
    return (input_tokens * 2.0 + max_tokens * 10.0) / 1_000_000 / 2.0


# fake-gen: uncached 70 * 1 + cache write 10 * 2 + cache read 20 * 0.5 + out 50 * 10 = 600 -> $0.0006
GEN_SETTLED_EUR = 600 / 1_000_000 / 2.0

_REQ = httpx.Request("POST", "https://api.invalid/v1/messages")


def status_error(code: int, headers: Optional[dict] = None) -> anthropic.APIStatusError:
    response = httpx.Response(code, headers=headers or {}, request=_REQ)
    return anthropic.APIStatusError(f"status {code}", response=response, body=None)


def conn_error() -> anthropic.APIConnectionError:
    return anthropic.APIConnectionError(request=_REQ)


# ---------------------------------------------------------------------------
# Prices and refusal of unpriced models
# ---------------------------------------------------------------------------


def test_load_prices_parses_fixture():
    prices = load_prices(PRICES_PATH)
    assert prices.price_list_date == "2026-01-01"
    assert prices.usd_per_eur == 2.0 and prices.weekly_cap_eur == 40.0
    assert prices.for_model("fake-judge").cache_write == 5.0
    assert prices.usd_to_eur(3.0) == 1.5


def test_load_prices_rejects_missing_keys(tmp_path):
    bad = tmp_path / "p16w_bad.toml"
    bad.write_text('price_list_date = "2026-01-01"\nusd_per_eur = 1.0\n[models.m]\ninput = 1\n')
    with pytest.raises(ValueError):
        load_prices(bad)


def test_unpriced_model_refused_at_meter_build():
    with pytest.raises(UnpricedModel):
        make_meter(models={**MODELS, "judge": "fake-unpriced"})
    assert not ledger_path().exists()


def test_default_models_unpriced_in_fixture_are_refused():
    """The production model ids are not in the synthetic price file -> refused."""
    with pytest.raises(UnpricedModel):
        SpendMeter(load_prices(PRICES_PATH), None, None)


def test_inner_model_mismatch_refused_at_client_build():
    fakes = make_fakes()
    fakes["generation"] = FakeChat(model="fake-unpriced", usage=_usage())
    meter = make_meter(fakes)
    with pytest.raises(ValueError):
        meter.generation_llm()
    assert fakes["generation"].sent == []


def test_inner_with_sdk_retries_refused():
    class RetryingFake(FakeChat):
        max_retries: int = 3

    fakes = make_fakes()
    fakes["generation"] = RetryingFake(model="fake-gen", usage=_usage())
    with pytest.raises(ValueError, match="max_retries"):
        make_meter(fakes).generation_llm()


# ---------------------------------------------------------------------------
# All three kinds settle; drop-in compatibility
# ---------------------------------------------------------------------------


def test_all_three_kinds_settle_through_production_chain_shapes():
    from src.generator import PROMPT_TEMPLATE
    from src.judge import JUDGE_PROMPT
    from src.query_rewrite import _invoke_rewrite

    fakes = make_fakes()
    meter = make_meter(fakes)

    # generation: `PROMPT_TEMPLATE | llm` -> AIMessage, stop_reason read off it.
    message = (PROMPT_TEMPLATE | meter.generation_llm()).invoke({"question": "q?", "context": "ctx"})
    assert isinstance(message, AIMessage)
    assert message.response_metadata["stop_reason"] == "end_turn"
    assert message.usage_metadata["output_tokens"] == 50

    # rewrite: the real `_invoke_rewrite` seam (TEMPLATE | llm | StrOutputParser()).
    assert _invoke_rewrite(meter.rewrite_llm(), "q?") == "ok"

    # judge: `JUDGE_PROMPT | llm | StrOutputParser()`.
    judged = (JUDGE_PROMPT | meter.judge_llm() | StrOutputParser()).invoke(
        {"question": "q", "answer": "a", "context": "c"}
    )
    assert judged == "ok"

    lines = ledger_lines()
    assert [(l["event"], l["kind"]) for l in lines] == [
        ("reserve", "generation"), ("settle", "generation"),
        ("reserve", "rewrite"), ("settle", "rewrite"),
        ("reserve", "judge"), ("settle", "judge"),
    ]
    for reserve, settle in zip(lines[::2], lines[1::2]):
        assert reserve["rid"] == settle["rid"]
        assert settle["input_tokens"] == 100 and settle["output_tokens"] == 50
        assert settle["cache_read_tokens"] == 20 and settle["cache_write_tokens"] == 10
        assert settle["eur"] < reserve["eur"]
        assert settle["eur"] == pytest.approx(settle["usd"] / 2.0)
    assert lines[1]["eur"] == pytest.approx(GEN_SETTLED_EUR)
    # rewrite: 70*1 + 10*1 + 20*0.1 + 50*10 = 582; judge: 70*4 + 10*5 + 20*0.4 + 50*20 = 1338
    assert lines[3]["eur"] == pytest.approx(582 / 1e6 / 2)
    assert lines[5]["eur"] == pytest.approx(1338 / 1e6 / 2)
    assert meter.run_total_eur == pytest.approx((600 + 582 + 1338) / 1e6 / 2)
    assert meter.week_total_eur == pytest.approx(meter.run_total_eur)


def test_direct_invoke_returns_inner_message_unchanged():
    fakes = make_fakes()
    meter = make_meter(fakes)
    llm = meter.generation_llm()
    assert isinstance(llm, MeteredChatModel)
    msg = llm.invoke(MESSAGES)
    assert msg.content == "ok"
    assert msg.response_metadata == {"stop_reason": "end_turn", "model": "fake-gen"}
    assert msg.usage_metadata["input_tokens"] == 100
    assert meter.generation_llm() is llm  # one client per kind


def test_reserve_worst_case_formula():
    fakes = make_fakes(max_tokens=1000)
    meter = make_meter(fakes)
    meter.generation_llm().invoke(MESSAGES)
    reserve = ledger_lines()[0]
    assert reserve["input_tokens"] == MESSAGES_WORST_INPUT
    assert reserve["output_tokens"] == 1000
    assert reserve["eur"] == pytest.approx(gen_worst_eur(1000))


def test_missing_usage_leaves_worst_case_charged():
    fakes = make_fakes(usage=None, max_tokens=1000)
    meter = make_meter(fakes)
    meter.generation_llm().invoke(MESSAGES)
    assert [l["event"] for l in ledger_lines()] == ["reserve"]
    assert meter.run_total_eur == pytest.approx(gen_worst_eur(1000))


# ---------------------------------------------------------------------------
# Metered retries
# ---------------------------------------------------------------------------


def test_two_transient_failures_then_success_make_three_reservations():
    fakes = make_fakes(max_tokens=1000)
    fakes["generation"].script = [conn_error(), status_error(529), None]
    sleeps: List[float] = []
    meter = make_meter(fakes, sleep=sleeps.append)
    msg = meter.generation_llm().invoke(MESSAGES)
    assert msg.content == "ok"
    lines = ledger_lines()
    reserves = [l for l in lines if l["event"] == "reserve"]
    settles = [l for l in lines if l["event"] == "settle"]
    assert len(reserves) == 3 and len(settles) == 1
    assert [r["attempt"] for r in reserves] == [0, 1, 2]
    assert settles[0]["rid"] == reserves[2]["rid"]
    assert len(fakes["generation"].sent) == 3
    assert sleeps == [0.5, 1.0]  # min(0.5 * 2**n, 8) with U = 0
    assert meter.run_total_eur == pytest.approx(2 * gen_worst_eur(1000) + GEN_SETTLED_EUR)


def test_retries_exhaust_after_four_attempts():
    fakes = make_fakes()
    fakes["generation"].script = [conn_error() for _ in range(5)]
    sleeps: List[float] = []
    meter = make_meter(fakes, sleep=sleeps.append)
    with pytest.raises(anthropic.APIConnectionError):
        meter.generation_llm().invoke(MESSAGES)
    assert len(fakes["generation"].sent) == 4
    assert [l["event"] for l in ledger_lines()] == ["reserve"] * 4
    assert sleeps == [0.5, 1.0, 2.0]


def test_non_retryable_error_raises_once_with_worst_case_charged():
    fakes = make_fakes(max_tokens=1000)
    fakes["generation"].script = [status_error(400)]
    meter = make_meter(fakes)
    with pytest.raises(anthropic.APIStatusError):
        meter.generation_llm().invoke(MESSAGES)
    assert len(fakes["generation"].sent) == 1
    assert meter.run_total_eur == pytest.approx(gen_worst_eur(1000))


# ---------------------------------------------------------------------------
# Limits: never sent; latch; BaseException
# ---------------------------------------------------------------------------


def test_spend_limit_reached_is_base_exception():
    assert issubclass(SpendLimitReached, BaseException)
    assert not issubclass(SpendLimitReached, Exception)
    assert issubclass(SpendMeterRequired, Exception)
    with pytest.raises(ValueError):
        SpendLimitReached("month")
    caught = None
    try:
        try:
            raise SpendLimitReached("week")
        except Exception:  # noqa: BLE001 — the broad handler this guards against
            caught = "swallowed"
    except SpendLimitReached as exc:
        caught = exc.kind
    assert caught == "week"


def test_call_crossing_week_ceiling_is_never_sent_and_latches():
    seed(38.0)  # 2 € left of the 40 € week
    fakes = make_fakes(max_tokens=1_000_000)  # worst case ~5 €
    meter = make_meter(fakes)
    with pytest.raises(SpendLimitReached) as info:
        meter.generation_llm().invoke(MESSAGES)
    assert info.value.kind == "week"
    assert fakes["generation"].sent == []
    assert [l["event"] for l in ledger_lines()] == ["reserve", "settle"]  # seed only
    assert meter.latched == "week"

    # Latched: even a tiny call on another client raises before anything happens.
    path = ledger_path()
    path.write_text("")
    with pytest.raises(SpendLimitReached) as again:
        meter.rewrite_llm().invoke(MESSAGES)
    assert again.value.kind == "week"
    assert fakes["rewrite"].sent == []
    assert path.read_text() == ""


def test_exact_fit_is_allowed():
    worst = gen_worst_eur(1_000_000)
    seed(40.0 - worst)
    fakes = make_fakes(max_tokens=1_000_000)
    make_meter(fakes).generation_llm().invoke(MESSAGES)
    assert len(fakes["generation"].sent) == 1


def test_call_crossing_run_limit_is_never_sent():
    seed(20.0, run_id="other-run")  # other runs count for the week, not this run
    fakes = make_fakes(max_tokens=1_000_000)  # worst ~5 €
    fakes["generation"].script = [status_error(400)]  # first call fails: worst case stays charged
    meter = make_meter(fakes, run_limit_eur=6.0, run_id="this-run")
    with pytest.raises(anthropic.APIStatusError):
        meter.generation_llm().invoke(MESSAGES)
    assert meter.run_total_eur == pytest.approx(gen_worst_eur(1_000_000))
    with pytest.raises(SpendLimitReached) as info:
        meter.generation_llm().invoke(MESSAGES)
    assert info.value.kind == "run"
    assert len(fakes["generation"].sent) == 1
    assert meter.latched == "run"


def test_week_is_checked_before_run():
    seed(38.0)
    meter = make_meter(make_fakes(max_tokens=1_000_000), run_limit_eur=1.0)
    with pytest.raises(SpendLimitReached) as info:
        meter.generation_llm().invoke(MESSAGES)
    assert info.value.kind == "week"


def test_owner_approval_lifts_ceiling_and_is_logged_on_every_line():
    seed(38.0)
    fakes = make_fakes(max_tokens=1_000_000)
    meter = make_meter(fakes, owner_approved_eur=50.0, approval_ref="owner-approval-test-1")
    meter.generation_llm().invoke(MESSAGES)
    assert len(fakes["generation"].sent) == 1
    new = ledger_lines()[2:]
    assert [l["event"] for l in new] == ["reserve", "settle"]
    for line in new:
        assert line["approval_ref"] == "owner-approval-test-1"
        assert line["owner_approved_eur"] == 50.0
        assert line["ceiling_eur"] == 50.0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"owner_approved_eur": 50.0},
        {"approval_ref": "ref-only"},
        {"owner_approved_eur": 40.0, "approval_ref": "not-a-lift"},
        {"run_limit_eur": 0},
    ],
)
def test_bad_limit_arguments_refused(kwargs):
    with pytest.raises(ValueError):
        make_meter(**kwargs)


# ---------------------------------------------------------------------------
# Cross-process: race and crash
# ---------------------------------------------------------------------------


def _race_child(barrier: Any, queue: Any, sent_log: str) -> None:
    # Widen the read-sum -> append window inside _reserve (this process only)
    # so that, without the flock, both children would read the same totals.
    original = spend.ledger_totals

    def slow_totals(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        time.sleep(0.5)
        return result

    spend.ledger_totals = slow_totals
    fakes = make_fakes(max_tokens=1_000_000, sent_log=sent_log, delay=2.0)
    meter = make_meter(fakes)
    llm = meter.generation_llm()
    barrier.wait(timeout=30)
    try:
        llm.invoke(MESSAGES)
        queue.put("sent")
    except SpendLimitReached as exc:
        queue.put(f"refused:{exc.kind}")


def test_two_processes_racing_for_one_worst_case_exactly_one_sends(tmp_path):
    worst = gen_worst_eur(1_000_000)
    seed(40.0 - 1.5 * worst)  # room for one worst case, not two
    ctx = multiprocessing.get_context("fork")
    barrier = ctx.Barrier(2)
    queue = ctx.Queue()
    sent_log = str(tmp_path / "sent.log")
    procs = [ctx.Process(target=_race_child, args=(barrier, queue, sent_log)) for _ in range(2)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)
        assert p.exitcode == 0
    outcomes = sorted(queue.get(timeout=10) for _ in procs)
    assert outcomes == ["refused:week", "sent"]
    assert Path(sent_log).read_text().count("sent") == 1
    assert [l["event"] for l in ledger_lines()].count("reserve") == 2  # seed + the winner


def _crash_child(run_id: str) -> None:
    fakes = make_fakes(max_tokens=1000, crash=True)
    make_meter(fakes, run_id=run_id).generation_llm().invoke(MESSAGES)


def test_unsettled_crashed_reservation_counts_in_a_new_process():
    ctx = multiprocessing.get_context("fork")
    proc = ctx.Process(target=_crash_child, args=("crashed-run",))
    proc.start()
    proc.join(timeout=60)
    assert proc.exitcode == 17  # died mid-request, after reserving
    assert [l["event"] for l in ledger_lines()] == ["reserve"]

    fresh = make_meter(run_id="crashed-run")
    assert fresh.week_total_eur == pytest.approx(gen_worst_eur(1000))
    assert fresh.run_total_eur == pytest.approx(gen_worst_eur(1000))
    other = make_meter(run_id="another-run")
    assert other.week_total_eur == pytest.approx(gen_worst_eur(1000))
    assert other.run_total_eur == 0.0


def test_torn_tail_is_ignored_and_not_glued_onto():
    seed(1.0)
    with ledger_path().open("a") as fh:
        fh.write('{"event": "reserve", "rid": "torn"')  # crash mid-write, no newline
    meter = make_meter()
    assert meter.week_total_eur == pytest.approx(1.0)
    meter.generation_llm().invoke(MESSAGES)
    assert meter.week_total_eur == pytest.approx(1.0 + GEN_SETTLED_EUR)
    assert '"torn"' not in ledger_path().read_text()  # cut off under the lock
    assert [l["event"] for l in ledger_lines()] == ["reserve", "settle"] * 2


def test_corrupt_line_fails_closed():
    ledger_path().write_text("not json\n")
    fakes = make_fakes()
    with pytest.raises(spend.LedgerCorrupt):
        make_meter(fakes).generation_llm().invoke(MESSAGES)
    assert fakes["generation"].sent == []


# ---------------------------------------------------------------------------
# ISO weeks
# ---------------------------------------------------------------------------


def test_iso_week_keys_including_year_boundaries():
    assert iso_week(datetime(2026, 12, 31, tzinfo=timezone.utc)) == "2026-W53"
    assert iso_week(datetime(2027, 1, 2, tzinfo=timezone.utc)) == "2026-W53"
    assert iso_week(datetime(2027, 1, 4, tzinfo=timezone.utc)) == "2027-W01"
    assert iso_week(datetime(2024, 12, 30, tzinfo=timezone.utc)) == "2025-W01"
    # UTC, not local: 00:30 Monday at +02:00 is still Sunday in UTC.
    local = datetime(2027, 1, 4, 0, 30, tzinfo=timezone(timedelta(hours=2)))
    assert iso_week(local) == "2026-W53"


def test_week_totals_count_only_the_current_iso_week_across_year_boundary():
    seed(20.0, week="2026-W52")
    seed(10.0, week="2026-W53")
    seed(15.0, week="2027-W01")
    sat = make_meter(clock=lambda: datetime(2027, 1, 2, 9, tzinfo=timezone.utc))
    assert sat.week_total_eur == pytest.approx(10.0)
    mon = make_meter(clock=lambda: datetime(2027, 1, 4, 9, tzinfo=timezone.utc))
    assert mon.week_total_eur == pytest.approx(15.0)


def test_previous_week_spend_does_not_block_this_week():
    seed(39.9, week="2026-W40")
    fakes = make_fakes(max_tokens=1_000_000)
    meter = make_meter(fakes)  # NOW is 2026-W41
    meter.generation_llm().invoke(MESSAGES)
    assert len(fakes["generation"].sent) == 1
    assert ledger_lines()[-1]["week"] == "2026-W41"


# ---------------------------------------------------------------------------
# No text in the ledger
# ---------------------------------------------------------------------------


def test_ledger_lines_hold_no_prompt_or_answer_text():
    canary = f"P16W-CANARY-{uuid.uuid4()}"
    fakes = make_fakes(reply=f"answer {canary}")
    fakes["rewrite"].script = [conn_error(), None]
    fakes["judge"].script = [status_error(400)]
    meter = make_meter(fakes)
    prompt = ChatPromptTemplate.from_messages([("system", "sys " + canary), ("human", "{q}")])
    assert canary in (prompt | meter.generation_llm()).invoke({"q": canary}).content
    (prompt | meter.rewrite_llm() | StrOutputParser()).invoke({"q": canary})
    with pytest.raises(anthropic.APIStatusError):
        meter.judge_llm().invoke([HumanMessage(content=canary)])
    text = ledger_path().read_text()
    assert len(ledger_lines()) == 6  # gen r+s, rewrite r+r+s, judge r
    assert canary not in text
    assert "CANARY" not in text
    expected = {
        "v", "event", "rid", "ts", "week", "run_id", "project", "model", "kind", "attempt",
        "input_tokens", "output_tokens", "cache_write_tokens", "cache_read_tokens", "usd", "eur",
        "ceiling_eur", "run_limit_eur", "owner_approved_eur", "approval_ref", "price_list_date",
    }
    for line in ledger_lines():
        assert set(line) == expected
        assert line["project"] == "rag-pipeline-v1"
        assert line["ts"].endswith("Z")


# ---------------------------------------------------------------------------
# Retry predicate (mirrors anthropic 0.116.0)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        conn_error(),
        anthropic.APITimeoutError(request=_REQ),
        anthropic.RetryableError("retry me"),
        *(status_error(c) for c in (408, 409, 429, 500, 501, 502, 503, 529)),
        status_error(400, {"x-should-retry": "true"}),
    ],
    ids=lambda e: f"{type(e).__name__}-{getattr(e, 'status_code', '')}",
)
def test_retry_predicate_retries(exc):
    retry, wait = retry_decision(exc, 0, rand=lambda: 0.0)
    assert retry is True and wait == 0.5


@pytest.mark.parametrize("code", [400, 401, 403, 404, 413, 422])
def test_retry_predicate_does_not_retry_client_errors(code):
    assert retry_decision(status_error(code), 0) == (False, 0.0)


@pytest.mark.parametrize("code", [408, 429, 500, 503, 529])
def test_x_should_retry_false_takes_precedence(code):
    assert retry_decision(status_error(code, {"x-should-retry": "false"}), 0) == (False, 0.0)


def test_retry_predicate_walks_cause_chain():
    def wrapped(inner: BaseException) -> RuntimeError:
        try:
            try:
                raise inner
            except BaseException as e:
                raise RuntimeError("wrapper") from e
        except RuntimeError as outer:
            return outer

    assert retry_decision(wrapped(status_error(503)), 0, rand=lambda: 0.0) == (True, 0.5)
    assert retry_decision(wrapped(conn_error()), 0, rand=lambda: 0.0) == (True, 0.5)
    assert retry_decision(wrapped(status_error(400)), 0) == (False, 0.0)
    assert retry_decision(ValueError("not an SDK error"), 0) == (False, 0.0)
    assert retry_decision(SpendLimitReached("run"), 0) == (False, 0.0)


@pytest.mark.parametrize(
    "headers,expected",
    [
        ({"retry-after": "30"}, 30.0),
        ({"retry-after": "60"}, 60.0),
        ({"retry-after": "0.25"}, 0.25),
        ({"retry-after-ms": "1500"}, 1.5),
        ({"retry-after-ms": "1500", "retry-after": "30"}, 1.5),
        ({"retry-after": "61"}, 0.5),  # > 60 s: ignored, backoff
        ({"retry-after": "0"}, 0.5),  # <= 0: ignored, backoff
        ({"retry-after": "-5"}, 0.5),
        ({"retry-after-ms": "70000", "retry-after": "5"}, 0.5),  # ms wins, then out of range (as the SDK)
        ({"retry-after": "soon"}, 0.5),
    ],
)
def test_retry_after_honoured_only_in_0_to_60_seconds(headers, expected):
    retry, wait = retry_decision(status_error(429, headers), 0, rand=lambda: 0.0)
    assert retry is True
    assert wait == pytest.approx(expected)


def test_retry_after_http_date(monkeypatch):
    import email.utils

    monkeypatch.setattr(spend.time, "time", lambda: 1_000_000_000.0)
    header = email.utils.formatdate(1_000_000_000.0 + 20, usegmt=True)
    retry, wait = retry_decision(status_error(503, {"retry-after": header}), 0)
    assert retry and wait == pytest.approx(20.0)


def test_backoff_schedule_jitter_and_cap():
    exc = conn_error()
    assert [retry_decision(exc, n, rand=lambda: 0.0)[1] for n in range(3)] == [0.5, 1.0, 2.0]
    assert retry_decision(exc, 0, rand=lambda: 1.0)[1] == pytest.approx(0.375)  # x (1 - 0.25)
    assert retry_decision(exc, 10, max_attempts=100, rand=lambda: 0.0)[1] == 8.0
    assert retry_decision(exc, 3) == (False, 0.0)  # 4th attempt failed: no 5th


# ---------------------------------------------------------------------------
# Ledger location rules
# ---------------------------------------------------------------------------


def test_conftest_points_suite_at_tmp_ledger():
    path = resolve_ledger_path()
    assert path == ledger_path()
    assert path != default_ledger_path()
    assert os.environ.get("PYTEST_CURRENT_TEST")


def test_cc_spend_ledger_outside_pytest_is_refused(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    with pytest.raises(LedgerRefused):
        resolve_ledger_path()
    with pytest.raises(LedgerRefused):
        check_ledger_path(ledger_path())
    with pytest.raises(LedgerRefused):
        SpendMeter(load_prices(PRICES_PATH), None, None, models=MODELS)


def test_outside_pytest_only_the_default_ledger(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("CC_SPEND_LEDGER", raising=False)
    expected = tmp_path / "home" / ".local" / "state" / "claudecode" / "anthropic_spend.jsonl"
    assert default_ledger_path() == expected
    assert resolve_ledger_path() == expected
    with pytest.raises(LedgerRefused):
        check_ledger_path(tmp_path / "elsewhere.jsonl")
    assert not expected.exists()  # resolving never creates it


def test_default_ledger_refused_under_pytest(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # belt and braces: never the real home
    with pytest.raises(LedgerRefused):
        check_ledger_path(default_ledger_path())
    with pytest.raises(LedgerRefused):
        make_meter(ledger=default_ledger_path())
    monkeypatch.setenv("CC_SPEND_LEDGER", str(default_ledger_path()))
    with pytest.raises(LedgerRefused):
        resolve_ledger_path()
    monkeypatch.delenv("CC_SPEND_LEDGER")
    with pytest.raises(LedgerRefused):
        resolve_ledger_path()
    assert not default_ledger_path().exists()


# ---------------------------------------------------------------------------
# Default inner factory: today's ChatAnthropic, max_retries=0 (built, never invoked)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["generation", "rewrite", "judge"])
def test_default_inner_factory_matches_production_builders_with_zero_retries(monkeypatch, kind):
    from src.generator import get_llm
    from src.query_rewrite import get_rewrite_llm

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake-for-tests")
    base = get_rewrite_llm() if kind == "rewrite" else get_llm()
    inner = spend.default_inner_factory(kind)
    assert base.max_retries == 3  # production stays as it is
    assert inner.max_retries == 0
    assert inner._client_params["max_retries"] == 0  # what the SDK client would be built with
    for attr in ("model", "max_tokens", "default_request_timeout", "thinking"):
        assert getattr(inner, attr) == getattr(base, attr)


def test_default_inner_factory_needs_a_key():
    with pytest.raises(ValueError):
        spend.default_inner_factory("generation")
