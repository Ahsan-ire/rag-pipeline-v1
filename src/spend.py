"""API spend meter for eval runs (Phase 16A-1 item 8, D70; D64's weekly cap).

What this module does
---------------------
Every Anthropic call an *eval* run makes (generation, Haiku query rewrite,
judge) goes through a ``SpendMeter``. The meter keeps one append-only JSONL
ledger per user, outside every checkout, and enforces two hard limits:

* the **week** ceiling — €40 per UTC ISO week by default (``weekly_cap_eur``
  in ``config/api_prices.toml``), lifted only by an owner approval
  (``owner_approved_eur`` + ``approval_ref``), and
* the **run** limit — an optional lower cap for this one run
  (``run_limit_eur``, the CLI's ``--approved-eur``); without it the run's
  limit is simply what is left of the week.

Reserve / settle
----------------
Each *attempt* (not each logical call — retries are attempts too) is metered
by a LangChain callback handler with ``raise_error=True``:

1. ``on_chat_model_start`` fires *before* the request is sent. Under an
   exclusive ``fcntl.flock`` on the ledger the meter computes the attempt's
   worst case — prompt UTF-8 bytes + 64 per message as input tokens, priced
   at the higher of the input and cache-write rates, plus ``max_tokens``
   output tokens — and sums the ledger. If this week's charge + worst case
   exceeds the week ceiling (checked first), or this run's charge + worst
   case exceeds the run limit, it raises ``SpendLimitReached`` and the
   request is never sent. Otherwise it appends a ``reserve`` line and fsyncs
   it, then releases the lock and the request goes out.
2. ``on_llm_end`` appends a ``settle`` line carrying the real cost computed
   from the response's ``usage_metadata``; from then on that cost replaces
   the reservation's worst case.

An error, a timeout, a response without ``usage_metadata`` or a crash leaves
the reservation open, and an open reservation is charged at its worst case —
by every process that ever reads the ledger. "Charge" below therefore means:
settled cost if a ``settle`` line exists for the reservation id, else the
reserve line's worst case.

A reservation belongs to the UTC ISO week in which it was *reserved* (its
``week`` field, e.g. ``"2026-W53"``); a settle line never moves it.

Ledger line schema (one JSON object per line, ``sort_keys``; NEVER any prompt
or answer text — only counts, money and identifiers)::

    v                 1 (schema version)
    event             "reserve" | "settle"
    rid               reservation id (uuid4 hex), shared by a reserve and its settle
    ts                UTC time, ISO 8601 with "Z"
    week              UTC ISO week of the reservation, "YYYY-Www"
    run_id            the run's id
    project           e.g. "rag-pipeline-v1"
    model             model id
    kind              "generation" | "rewrite" | "judge"
    attempt           0-based attempt index within the logical call
    input_tokens      reserve: worst-case estimate; settle: usage_metadata total
    output_tokens     reserve: max_tokens; settle: actual
    cache_write_tokens / cache_read_tokens   (0 on reserve lines)
    usd, eur          cost of this line (worst case on reserve, actual on settle)
    ceiling_eur       the week ceiling in force for this meter
    run_limit_eur     the run limit, or null (= the week's remainder)
    owner_approved_eur, approval_ref   the owner approval lifting the ceiling, or null
    price_list_date   from the price file

Retries
-------
The inner ``ChatAnthropic`` is built with ``max_retries=0`` so the SDK never
retries on its own (an SDK-internal retry would be an unmetered request).
``MeteredChatModel`` runs the retry loop itself, 4 attempts as in D52
(``get_llm``'s ``max_retries=3``), with ``retry_decision`` mirroring
anthropic 0.116.0 ``_base_client.py``: ``_should_retry_exception``
(lines 877–900: walk ``__cause__``; ``RetryableError`` and
``APIConnectionError`` — ``APITimeoutError`` is its subclass — retry; an
``APIStatusError`` goes to ``_should_retry``), ``_should_retry``
(842–875: ``x-should-retry: true|false`` obeyed first, else 408, 409, 429 and
>= 500), ``_parse_retry_after_header`` (784–816: ``retry-after-ms``, then
``retry-after`` as seconds, then as an HTTP date) and
``_calculate_retry_timeout`` (818–840: a parsed value in (0, 60] s is used
as-is, else ``min(0.5 * 2**n, 8) * (1 - 0.25 * U)`` where ``n`` is the number
of retries already taken, i.e. the 0-based index of the failed attempt —
``_base_client.py:1132–1133`` and ``:1324–1332``).

Ledger location
---------------
``~/.local/state/claudecode/anthropic_spend.jsonl`` — one per user, outside
every checkout, so worktrees, branch switches and ``git clean`` cannot reset
it. ``CC_SPEND_LEDGER`` overrides it **only under pytest**
(``PYTEST_CURRENT_TEST`` set); outside pytest the variable is refused, and
under pytest the default path is refused (``tests/conftest.py`` sets the
variable per test, autouse). The same rules apply to an explicit ``ledger=``
argument: outside pytest only the default path is accepted.

Unpriced models
---------------
Refused twice: when the meter is built (the declared model of every kind —
by default ``GENERATION_MODEL``, ``REWRITE_MODEL`` and ``JUDGE_MODEL`` — must
be in the price file) and when a client is built (the inner model's actual
id must equal the declared one, hence be priced).

Latch
-----
Once a limit is hit the meter latches: every later call through any of its
clients raises ``SpendLimitReached`` of the same kind immediately, without
reading the ledger or sending anything.

``SpendLimitReached`` subclasses ``BaseException`` so the broad
``except Exception`` handlers in the eval path cannot turn it into an api
error row, a fallback, or a retry.

Logging boundary
----------------
While any metered attempt is in flight, every record from the SDK and
transport logger families (``anthropic``, ``httpx``, ``httpcore``,
``langchain_anthropic``, children included) is dropped, at every level, by a
filter on those loggers (:func:`_sdk_logging_dropped`). The anthropic SDK logs
each request's options -- the messages, i.e. the question, its context and,
for the judge, the answer -- at DEBUG (``_base_client._build_request``), which
``ANTHROPIC_LOG=debug`` or any DEBUG-level root logger turns on; report
sanitisation cannot reach that output (16A-1 merge gate, Codex #1).

The filter is reference-counted under a lock: the first attempt to enter
installs it, the last to leave removes it, so overlapping calls (threads,
``Runnable.batch``) cannot reopen the SDK's logging while another call is still
sending. No other logger and not the process-wide ``logging.disable``
threshold is touched (merge gate round 2, Codex #1). Every live eval call goes
through a meter (``SpendMeterRequired`` otherwise), so this covers every class.
"""

from __future__ import annotations

import email.utils
import fcntl
import json
import logging
import math
import os
import pwd
import random
import sys
import threading
import time
import tomllib
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Tuple

from langchain_core.callbacks import BaseCallbackHandler, BaseCallbackManager
from langchain_core.messages import BaseMessage
from langchain_core.outputs import LLMResult
from langchain_core.runnables import Runnable, RunnableConfig

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Kinds of metered client, one per eval call site.
KINDS: Tuple[str, ...] = ("generation", "rewrite", "judge")

#: Total attempts per logical call (D52: the SDK's max_retries=3 + 1).
MAX_ATTEMPTS = 4

#: Per-message token overhead added to the prompt-bytes worst case.
PER_MESSAGE_OVERHEAD_TOKENS = 64

#: Mirrors anthropic._constants INITIAL_RETRY_DELAY / MAX_RETRY_DELAY (0.116.0).
INITIAL_RETRY_DELAY = 0.5
MAX_RETRY_DELAY = 8.0

DEFAULT_PROJECT = "rag-pipeline-v1"
LEDGER_ENV = "CC_SPEND_LEDGER"
LEDGER_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class SpendLimitReached(BaseException):
    """A metered call would cross the week ceiling or the run limit.

    Subclasses ``BaseException`` (not ``Exception``) on purpose: broad
    ``except Exception`` handlers in the eval path must not swallow it.

    Attributes:
        kind: ``"week"`` (the D64 stop) or ``"run"`` (the run's own limit).
    """

    def __init__(self, kind: str, message: str = "") -> None:
        if kind not in ("week", "run"):
            raise ValueError(f"SpendLimitReached kind must be 'week' or 'run', got {kind!r}")
        self.kind = kind
        super().__init__(message or f"spend limit reached ({kind})")


class SpendMeterError(BaseException):
    """Base for meter failures that must stop an eval run, never degrade it.

    Subclasses ``BaseException`` like :class:`SpendLimitReached` (16A-1 gate
    round 5): a broad ``except Exception`` anywhere in the eval path (rewrite,
    judge, generation, or code added later) cannot turn a corrupt or refused
    ledger into a silent fallback, API-error or error row. The CLI maps it to
    exit 2, printing its type only on a non-public run.
    """


class SpendMeterRequired(SpendMeterError):
    """A live eval path with a usable API key was started without a meter."""


class LedgerRefused(SpendMeterError):
    """The ledger path breaks the location rules (see module docstring)."""


class LedgerCorrupt(SpendMeterError):
    """A ledger line could not be parsed; the meter fails closed."""


class UnpricedModel(SpendMeterError):
    """A model has no entry in the price file; the meter refuses to build."""


# ---------------------------------------------------------------------------
# Prices
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelPrice:
    """USD per million tokens for one model."""

    input: float
    output: float
    cache_write: float
    cache_read: float


@dataclass(frozen=True)
class Prices:
    """The parsed price file."""

    models: Mapping[str, ModelPrice]
    price_list_date: str
    usd_per_eur: float
    weekly_cap_eur: float

    def for_model(self, model: str) -> ModelPrice:
        """Return the model's rates, or raise ``UnpricedModel``."""
        try:
            return self.models[model]
        except KeyError:
            raise UnpricedModel(
                f"model {model!r} is not priced in the price file "
                f"(priced: {sorted(self.models)})"
            ) from None

    def usd_to_eur(self, usd: float) -> float:
        """Convert USD to EUR with the file's ``usd_per_eur``."""
        return usd / self.usd_per_eur


def _positive(value: Any, name: str, *, allow_zero: bool = False) -> float:
    """Validate a TOML number; bools are refused (they are ints in Python)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number, got {value!r}")
    v = float(value)
    # NaN/inf would switch a ceiling off: every `total + eur > nan` is False.
    if not math.isfinite(v):
        raise ValueError(f"{name} must be finite, got {v}")
    if v < 0 or (v == 0 and not allow_zero):
        raise ValueError(f"{name} must be {'>= 0' if allow_zero else '> 0'}, got {v}")
    return v


def load_prices(path: os.PathLike[str] | str) -> Prices:
    """Load and validate the price file (``config/api_prices.toml``).

    Expected shape::

        price_list_date = 2026-10-01      # TOML date or string
        usd_per_eur = 1.08
        weekly_cap_eur = 40

        [models."claude-sonnet-5"]        # USD per million tokens
        input = 3.0
        output = 15.0
        cache_write = 3.75
        cache_read = 0.30

    Raises:
        ValueError: a key is missing or a value is not a valid number.
    """
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    for key in ("price_list_date", "usd_per_eur", "weekly_cap_eur", "models"):
        if key not in data:
            raise ValueError(f"price file {path}: missing {key!r}")
    pld = data["price_list_date"]
    if isinstance(pld, (date, datetime)):
        pld = pld.isoformat()
    if not isinstance(pld, str) or not pld:
        raise ValueError(f"price file {path}: price_list_date must be a date or string")
    models_raw = data["models"]
    if not isinstance(models_raw, dict) or not models_raw:
        raise ValueError(f"price file {path}: [models] must be a non-empty table")
    models: Dict[str, ModelPrice] = {}
    for name, rates in models_raw.items():
        if not isinstance(rates, dict):
            raise ValueError(f"price file {path}: models.{name} must be a table")
        try:
            models[name] = ModelPrice(
                **{
                    f: _positive(rates[f], f"models.{name}.{f}", allow_zero=True)
                    for f in ("input", "output", "cache_write", "cache_read")
                }
            )
        except KeyError as exc:
            raise ValueError(f"price file {path}: models.{name} missing {exc.args[0]!r}") from None
    return Prices(
        models=models,
        price_list_date=pld,
        usd_per_eur=_positive(data["usd_per_eur"], "usd_per_eur"),
        weekly_cap_eur=_positive(data["weekly_cap_eur"], "weekly_cap_eur"),
    )


# ---------------------------------------------------------------------------
# Ledger location
# ---------------------------------------------------------------------------


def default_ledger_path() -> Path:
    """The one per-user ledger: ``~/.local/state/claudecode/anthropic_spend.jsonl``."""
    # Keyed on the passwd entry, not $HOME: HOME=/other would otherwise reset the cap.
    return Path(pwd.getpwuid(os.getuid()).pw_dir) / ".local" / "state" / "claudecode" / "anthropic_spend.jsonl"


def _under_pytest() -> bool:
    """True only inside a pytest process.

    ``PYTEST_CURRENT_TEST`` alone can be exported by any shell, which would let
    a plain process swap the per-owner ledger for an empty one via
    ``CC_SPEND_LEDGER``; requiring the ``pytest`` module to be loaded as well
    closes that. (A process that imports pytest on purpose to spoof it remains
    an instruction-enforced residual, D70.)
    """
    return bool(os.environ.get("PYTEST_CURRENT_TEST")) and "pytest" in sys.modules


def _same_path(a: Path, b: Path) -> bool:
    return os.path.abspath(os.path.realpath(a)) == os.path.abspath(os.path.realpath(b))


def resolve_ledger_path() -> Path:
    """Return the ledger path the meter must use, enforcing the location rules.

    * outside pytest: ``CC_SPEND_LEDGER`` set -> ``LedgerRefused``; else the
      default per-user path;
    * under pytest: ``CC_SPEND_LEDGER`` must be set (the autouse conftest
      fixture does) and must not be the default path -> else ``LedgerRefused``.
    """
    override = os.environ.get(LEDGER_ENV)
    if not _under_pytest():
        if override:
            raise LedgerRefused(
                f"{LEDGER_ENV} is honoured only under pytest; unset it to use "
                f"the per-user ledger {default_ledger_path()}"
            )
        return default_ledger_path()
    if not override:
        raise LedgerRefused(
            f"under pytest the default ledger is refused and {LEDGER_ENV} is "
            "not set (tests/conftest.py should set it, autouse)"
        )
    path = Path(override)
    if _same_path(path, default_ledger_path()):
        raise LedgerRefused(f"under pytest {LEDGER_ENV} must not point at the default ledger")
    return path


def check_ledger_path(ledger: os.PathLike[str] | str | None) -> Path:
    """Validate an explicit ``ledger`` argument (``None`` -> ``resolve_ledger_path``).

    Outside pytest only the default path is accepted (an arbitrary path would
    reset the per-owner cap); under pytest the default path is refused.
    """
    if ledger is None:
        return resolve_ledger_path()
    path = Path(ledger)
    is_default = _same_path(path, default_ledger_path())
    if _under_pytest():
        if is_default:
            raise LedgerRefused("under pytest the default ledger is refused")
    else:
        if os.environ.get(LEDGER_ENV):
            raise LedgerRefused(f"{LEDGER_ENV} is honoured only under pytest")
        if not is_default:
            raise LedgerRefused(
                f"outside pytest the ledger must be the per-user ledger {default_ledger_path()}"
            )
    return path


# ---------------------------------------------------------------------------
# Retry predicate (mirrors anthropic 0.116.0 _base_client.py:784-900)
# ---------------------------------------------------------------------------


def _parse_retry_after(headers: Any) -> Optional[float]:
    """Mirror ``_parse_retry_after_header``: seconds to wait, or None."""
    if headers is None:
        return None
    try:
        return float(headers.get("retry-after-ms", None)) / 1000
    except (TypeError, ValueError):
        pass
    retry_header = headers.get("retry-after")
    try:
        return float(retry_header)
    except (TypeError, ValueError):
        pass
    if retry_header is None:
        return None
    retry_date_tuple = email.utils.parsedate_tz(retry_header)
    if retry_date_tuple is None:
        return None
    return float(email.utils.mktime_tz(retry_date_tuple) - time.time())


def _status_should_retry(response: Any) -> bool:
    """Mirror ``_should_retry``: x-should-retry first, then 408/409/429/>=500."""
    header = response.headers.get("x-should-retry")
    if header == "true":
        return True
    if header == "false":
        return False
    status = response.status_code
    return status in (408, 409, 429) or status >= 500


def _should_retry_exception(err: BaseException) -> Tuple[bool, Any]:
    """Mirror ``_should_retry_exception``: walk ``__cause__``; (retry?, response)."""
    import anthropic

    seen: set[int] = set()
    current: Optional[BaseException] = err
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, anthropic.RetryableError):
            return True, None
        if isinstance(current, anthropic.APIStatusError):
            return _status_should_retry(current.response), current.response
        if isinstance(current, anthropic.APIConnectionError):
            return True, None
        current = current.__cause__
    return False, None


def retry_decision(
    exc: BaseException,
    attempt: int,
    *,
    max_attempts: int = MAX_ATTEMPTS,
    rand: Callable[[], float] = random.random,
) -> Tuple[bool, float]:
    """Decide whether a failed attempt is retried, and how long to wait first.

    Args:
        exc: the exception the attempt raised.
        attempt: 0-based index of the attempt that failed (= retries taken so far).
        max_attempts: total attempts allowed (4, as D52).
        rand: uniform [0, 1) source for the jitter (injectable for tests).

    Returns:
        ``(retry, wait_seconds)``; ``wait_seconds`` is 0.0 when not retrying.
    """
    if not isinstance(exc, Exception):  # SpendLimitReached, KeyboardInterrupt, ...
        return False, 0.0
    if attempt + 1 >= max_attempts:
        return False, 0.0
    retry, response = _should_retry_exception(exc)
    if not retry:
        return False, 0.0
    retry_after = _parse_retry_after(response.headers if response is not None else None)
    if retry_after is not None and 0 < retry_after <= 60:
        return True, retry_after
    sleep_seconds = min(INITIAL_RETRY_DELAY * pow(2.0, min(attempt, 1000)), MAX_RETRY_DELAY)
    wait = sleep_seconds * (1 - 0.25 * rand())
    return True, wait if wait >= 0 else 0.0


# ---------------------------------------------------------------------------
# Cost helpers
# ---------------------------------------------------------------------------


def _content_bytes(content: Any) -> int:
    """UTF-8 byte length of a message's content (str or list of blocks)."""
    if isinstance(content, str):
        return len(content.encode("utf-8"))
    total = 0
    for block in content or []:
        if isinstance(block, str):
            total += len(block.encode("utf-8"))
        else:
            total += len(json.dumps(block, ensure_ascii=False, default=str).encode("utf-8"))
    return total


def worst_case_input_tokens(messages: List[BaseMessage]) -> int:
    """Prompt UTF-8 bytes + ``PER_MESSAGE_OVERHEAD_TOKENS`` per message."""
    return sum(_content_bytes(m.content) + PER_MESSAGE_OVERHEAD_TOKENS for m in messages)


def worst_case_usd(price: ModelPrice, input_tokens: int, max_tokens: int) -> float:
    """Input at max(input, cache_write) + ``max_tokens`` output, in USD."""
    return (input_tokens * max(price.input, price.cache_write) + max_tokens * price.output) / 1_000_000


def usage_cost_usd(price: ModelPrice, usage: Mapping[str, Any]) -> Tuple[float, Dict[str, int]]:
    """Cost of a LangChain ``usage_metadata`` dict, plus the token breakdown.

    ``input_tokens`` from langchain_anthropic already *includes* cache reads
    and writes (``_create_usage_metadata``), so they are subtracted back out
    and priced at their own rates. Cache writes are the generic
    ``cache_creation`` plus the ``ephemeral_5m``/``ephemeral_1h`` keys (the
    integration zeroes the generic key when the specific ones exist), all at
    the single ``cache_write`` rate.
    """
    details = usage.get("input_token_details") or {}
    cache_read = int(details.get("cache_read") or 0)
    cache_write = int(
        (details.get("cache_creation") or 0)
        + (details.get("ephemeral_5m_input_tokens") or 0)
        + (details.get("ephemeral_1h_input_tokens") or 0)
    )
    input_total = int(usage.get("input_tokens") or 0)
    output = int(usage.get("output_tokens") or 0)
    uncached = max(input_total - cache_read - cache_write, 0)
    usd = (
        uncached * price.input
        + cache_write * price.cache_write
        + cache_read * price.cache_read
        + output * price.output
    ) / 1_000_000
    return usd, {
        "input_tokens": input_total,
        "output_tokens": output,
        "cache_write_tokens": cache_write,
        "cache_read_tokens": cache_read,
    }


def iso_week(moment: datetime) -> str:
    """UTC ISO week key, e.g. ``"2026-W53"`` (ISO year, not calendar year)."""
    year, week, _ = moment.astimezone(timezone.utc).isocalendar()
    return f"{year}-W{week:02d}"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Ledger IO
# ---------------------------------------------------------------------------


@contextmanager
def _locked(path: Path, *, exclusive: bool) -> Iterator[int]:
    """Open (creating) the ledger and hold an flock on it for the block."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield fd
    finally:
        os.close(fd)  # closing the descriptor releases the flock


_MONEY_FIELDS = (
    "usd", "eur", "input_tokens", "output_tokens", "cache_write_tokens", "cache_read_tokens",
)


def _read_lines(fd: int) -> Tuple[List[Dict[str, Any]], int]:
    """Parse the ledger; return (records, length of the complete-line prefix).

    A final line without a newline is a write cut off by a crash before its
    fsync returned — the request it would have reserved was never sent — so
    it is ignored here and cut off by the next ``_append`` (under LOCK_EX).
    Any other unparsable line raises ``LedgerCorrupt`` (fail closed).
    """
    os.lseek(fd, 0, os.SEEK_SET)
    chunks = []
    while True:
        chunk = os.read(fd, 1 << 20)
        if not chunk:
            break
        chunks.append(chunk)
    data = b"".join(chunks)
    complete_len = data.rfind(b"\n") + 1  # 0 when there is no full line
    records = []
    for i, raw in enumerate(data[:complete_len].split(b"\n")[:-1], 1):
        if not raw.strip():
            continue
        try:
            rec = json.loads(raw)
        except ValueError:
            raise LedgerCorrupt(f"ledger line {i} is not JSON") from None
        if not isinstance(rec, dict) or rec.get("event") not in ("reserve", "settle") or not rec.get("rid"):
            raise LedgerCorrupt(f"ledger line {i} is not a reserve/settle record")
        if rec["event"] == "reserve":
            for key in ("week", "run_id", "eur"):
                if key not in rec:
                    raise LedgerCorrupt(f"ledger line {i}: reserve without {key!r}")
        elif "eur" not in rec:
            raise LedgerCorrupt(f"ledger line {i}: settle without 'eur'")
        for key in _MONEY_FIELDS:
            if key in rec:
                val = rec[key]
                if (
                    isinstance(val, bool)
                    or not isinstance(val, (int, float))
                    or not math.isfinite(val)
                    or val < 0
                ):
                    raise LedgerCorrupt(f"ledger line {i}: {key!r} must be a finite number >= 0")
        records.append(rec)
    return records, complete_len


def _append(fd: int, record: Mapping[str, Any], *, complete_len: int) -> None:
    """Append one JSON line in a single write and fsync it (caller holds LOCK_EX).

    A torn tail past ``complete_len`` is truncated first, so a new line is
    never glued onto a crashed writer's partial line.
    """
    if os.fstat(fd).st_size != complete_len:
        os.ftruncate(fd, complete_len)
    line = json.dumps(record, sort_keys=True, ensure_ascii=True) + "\n"
    os.write(fd, line.encode("utf-8"))  # O_APPEND: lands at the (new) end
    os.fsync(fd)


def _charges(records: List[Dict[str, Any]]) -> Dict[str, Tuple[Dict[str, Any], float]]:
    """Map rid -> (reserve record, charge in EUR: settled cost or worst case)."""
    reserves: Dict[str, Dict[str, Any]] = {}
    settled: Dict[str, float] = {}
    for rec in records:
        if rec["event"] == "reserve":
            reserves[rec["rid"]] = rec
        else:
            settled[rec["rid"]] = float(rec["eur"])
    return {
        rid: (rec, settled.get(rid, float(rec["eur"]))) for rid, rec in reserves.items()
    }


def ledger_totals(records: List[Dict[str, Any]], *, week: str, run_id: str) -> Tuple[float, float]:
    """(week charge, run charge) in EUR for the given ISO week and run id."""
    week_total = 0.0
    run_total = 0.0
    for rec, charge in _charges(records).values():
        if rec["week"] == week:
            week_total += charge
        if rec["run_id"] == run_id:
            run_total += charge
    return week_total, run_total


# ---------------------------------------------------------------------------
# The meter
# ---------------------------------------------------------------------------


def _default_models() -> Dict[str, str]:
    """Declared model per kind, read from the modules that own them (lazy)."""
    from src.generator import GENERATION_MODEL
    from src.judge import JUDGE_MODEL
    from src.query_rewrite import REWRITE_MODEL

    return {"generation": GENERATION_MODEL, "rewrite": REWRITE_MODEL, "judge": JUDGE_MODEL}


def default_inner_factory(kind: str) -> Any:
    """Build today's client for ``kind`` with ``max_retries=0``.

    Reuses the production builders (``get_llm`` for generation and judge,
    ``get_rewrite_llm`` for rewrite) so model id, ``max_tokens``, timeout and
    ``thinking`` cannot drift from what ``pipeline query`` uses, then copies
    the instance with ``max_retries=0``. The copy drops any cached SDK client
    so the new value is the one the SDK client is built with. Raises
    ``ValueError`` without an API key, exactly as the builders do.
    """
    if kind in ("generation", "judge"):
        from src.generator import get_llm

        base = get_llm()
    elif kind == "rewrite":
        from src.query_rewrite import get_rewrite_llm

        base = get_rewrite_llm()
    else:
        raise ValueError(f"unknown client kind {kind!r}")
    inner = base.model_copy(update={"max_retries": 0})
    for cached in ("_client_params", "_client", "_async_client"):
        inner.__dict__.pop(cached, None)
    return inner


class _MeterCallback(BaseCallbackHandler):
    """Per-attempt callback: reserve on start, settle on end (raise_error=True)."""

    raise_error = True
    run_inline = True

    def __init__(self, meter: "SpendMeter", client: "MeteredChatModel", attempt: int) -> None:
        self._meter = meter
        self._client = client
        self._attempt = attempt
        self._open: Dict[Any, Tuple[str, str]] = {}  # langchain run_id -> (rid, week)

    def on_chat_model_start(
        self,
        serialized: Dict[str, Any],
        messages: List[List[BaseMessage]],
        *,
        run_id: Any,
        **kwargs: Any,
    ) -> None:
        """Reserve the worst case before the request is sent (may raise)."""
        params = kwargs.get("invocation_params") or {}
        max_tokens = params.get("max_tokens") or self._client.max_tokens
        input_tokens = sum(worst_case_input_tokens(batch) for batch in messages)
        self._open[run_id] = self._meter._reserve(
            model=self._client.model,
            kind=self._client.kind,
            attempt=self._attempt,
            input_tokens=input_tokens,
            max_tokens=int(max_tokens),
        )

    def on_llm_end(self, response: LLMResult, *, run_id: Any, **kwargs: Any) -> None:
        """Settle with the real usage; missing usage leaves the worst case."""
        opened = self._open.pop(run_id, None)
        if opened is None:
            return
        usage = None
        for gens in response.generations:
            for gen in gens:
                msg = getattr(gen, "message", None)
                if msg is not None and getattr(msg, "usage_metadata", None):
                    usage = msg.usage_metadata
                    break
            if usage:
                break
        if not usage:
            return
        rid, week = opened
        self._meter._settle(
            rid=rid, week=week, model=self._client.model, kind=self._client.kind,
            attempt=self._attempt, usage=usage,
        )


# The logger families whose records can carry request content. In the pinned
# versions only ``anthropic._base_client`` logs the request options (the
# prompt, DEBUG); httpx and httpcore log the URL, status and headers, and
# langchain_anthropic defines no logger. All four families are covered so a
# version bump that adds a body dump is covered too.
_SDK_LOGGER_FAMILIES = ("anthropic", "httpx", "httpcore", "langchain_anthropic")


class _DropAll(logging.Filter):
    """A logger filter that rejects every record."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 - logging's API name
        return False


_SDK_DROP = _DropAll()
_sdk_drop_lock = threading.Lock()
_sdk_drop_depth = 0  # metered attempts currently in flight; guarded by _sdk_drop_lock


def _sdk_loggers() -> List[logging.Logger]:
    """Every existing logger in ``_SDK_LOGGER_FAMILIES`` (placeholders skipped).

    A logger filter applies only to records created on that logger, not to
    records its children propagate, so each child is listed itself.
    """
    out = []
    for name, obj in list(logging.root.manager.loggerDict.items()):
        if isinstance(obj, logging.Logger) and any(
            name == family or name.startswith(family + ".") for family in _SDK_LOGGER_FAMILIES
        ):
            out.append(obj)
    return out


@contextmanager
def _sdk_logging_dropped() -> Iterator[None]:
    """Drop every SDK/transport log record while any metered attempt is in flight.

    Reference-counted under a lock, so concurrent and nested attempts share
    one boundary: each entry attaches :data:`_SDK_DROP` to every SDK-family
    logger that exists by then (``addFilter`` is idempotent), and the last
    exit detaches it from all of them. Only those loggers are touched; other
    loggers and ``logging.disable`` keep whatever the caller set.
    """
    global _sdk_drop_depth
    with _sdk_drop_lock:
        _sdk_drop_depth += 1
        for logger in _sdk_loggers():
            logger.addFilter(_SDK_DROP)
    try:
        yield
    finally:
        with _sdk_drop_lock:
            _sdk_drop_depth -= 1
            if _sdk_drop_depth == 0:
                for logger in _sdk_loggers():
                    logger.removeFilter(_SDK_DROP)


def _with_handler(config: Optional[RunnableConfig], handler: BaseCallbackHandler) -> RunnableConfig:
    """Return a copy of ``config`` with ``handler`` added to its callbacks."""
    cfg: Dict[str, Any] = dict(config or {})
    callbacks = cfg.get("callbacks")
    if callbacks is None:
        cfg["callbacks"] = [handler]
    elif isinstance(callbacks, BaseCallbackManager):
        manager = callbacks.copy()
        manager.add_handler(handler, inherit=True)
        cfg["callbacks"] = manager
    else:
        cfg["callbacks"] = [*callbacks, handler]
    return cfg  # type: ignore[return-value]


class MeteredChatModel(Runnable[Any, BaseMessage]):
    """Drop-in for a ChatAnthropic: metered attempts in the meter's retry loop.

    Works in ``PROMPT | llm`` and ``PROMPT | llm | StrOutputParser()`` chains
    and with ``llm.invoke(messages)``; returns the inner model's ``AIMessage``
    unchanged (``response_metadata["stop_reason"]`` and ``usage_metadata``
    preserved). Async/stream/batch use Runnable's defaults, which route
    through ``invoke``.
    """

    def __init__(self, meter: "SpendMeter", kind: str, inner: Any, model: str, max_tokens: int) -> None:
        self._meter = meter
        self.kind = kind
        self.inner = inner
        self.model = model
        self.max_tokens = max_tokens

    def invoke(self, input: Any, config: Optional[RunnableConfig] = None, **kwargs: Any) -> BaseMessage:
        """Run up to ``MAX_ATTEMPTS`` metered attempts of the inner model."""
        meter = self._meter
        attempt = 0
        while True:
            meter._check_latch()
            handler = _MeterCallback(meter, self, attempt)
            try:
                # No SDK/transport log record during the call: the SDK's DEBUG
                # request dump carries the prompt (module docstring).
                with _sdk_logging_dropped():
                    return self.inner.invoke(input, _with_handler(config, handler), **kwargs)
            except Exception as exc:  # SpendLimitReached is BaseException: never here
                retry, wait = retry_decision(
                    exc, attempt, max_attempts=meter.max_attempts, rand=meter._rand
                )
                if not retry:
                    raise
                meter._sleep(wait)
                attempt += 1


class SpendMeter:
    """Locked reserve/settle meter over a per-user JSONL ledger.

    Args:
        prices: parsed price file (``load_prices``).
        ledger: ledger path, or None for ``resolve_ledger_path()``; validated
            by ``check_ledger_path``.
        run_limit_eur: this run's limit in EUR (the CLI's ``--approved-eur``),
            or None, meaning the run may use the week's remainder.
        run_id: identifies this run's lines (default: a fresh uuid4 hex).
        project: written on every line.
        approval_ref, owner_approved_eur: an owner approval lifting the week
            ceiling to ``owner_approved_eur`` (must exceed the cap; both or
            neither).
        models: declared model per kind (default: the production constants);
            every one must be priced or ``UnpricedModel`` is raised here.
        inner_factory: ``kind -> chat model``; default builds ChatAnthropic
            via ``default_inner_factory``. Tests inject fakes.
        sleep, rand, clock: injectable for tests.
        max_attempts: attempts per logical call (4).
    """

    def __init__(
        self,
        prices: Prices,
        ledger: os.PathLike[str] | str | None = None,
        run_limit_eur: Optional[float] = None,
        *,
        run_id: Optional[str] = None,
        project: str = DEFAULT_PROJECT,
        approval_ref: Optional[str] = None,
        owner_approved_eur: Optional[float] = None,
        models: Optional[Mapping[str, str]] = None,
        inner_factory: Optional[Callable[[str], Any]] = None,
        sleep: Callable[[float], None] = time.sleep,
        rand: Callable[[], float] = random.random,
        clock: Callable[[], datetime] = _utcnow,
        max_attempts: int = MAX_ATTEMPTS,
    ) -> None:
        self.prices = prices
        self.ledger = check_ledger_path(ledger)
        if run_limit_eur is not None:
            run_limit_eur = _positive(run_limit_eur, "run_limit_eur")
        self.run_limit_eur = run_limit_eur
        if (owner_approved_eur is None) != (approval_ref is None or approval_ref == ""):
            raise ValueError("owner_approved_eur and approval_ref must be given together")
        if owner_approved_eur is not None:
            owner_approved_eur = _positive(owner_approved_eur, "owner_approved_eur")
            if owner_approved_eur <= prices.weekly_cap_eur:
                raise ValueError(
                    f"owner_approved_eur ({owner_approved_eur}) must exceed the weekly cap "
                    f"({prices.weekly_cap_eur}); use run_limit_eur to lower a run's limit"
                )
        self.owner_approved_eur = owner_approved_eur
        self.approval_ref = approval_ref
        self.ceiling_eur = owner_approved_eur if owner_approved_eur is not None else prices.weekly_cap_eur
        self.run_id = run_id or uuid.uuid4().hex
        self.project = project
        declared = dict(models) if models is not None else _default_models()
        missing = [k for k in KINDS if k not in declared]
        if missing:
            raise ValueError(f"models must declare every kind; missing {missing}")
        for kind in KINDS:
            prices.for_model(declared[kind])  # UnpricedModel if absent
        self.models = declared
        self._inner_factory = inner_factory or default_inner_factory
        self._sleep = sleep
        self._rand = rand
        self._clock = clock
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self.max_attempts = max_attempts
        self._latched: Optional[str] = None
        self._clients: Dict[str, MeteredChatModel] = {}

    # -- clients -----------------------------------------------------------

    def _client(self, kind: str) -> MeteredChatModel:
        if kind not in self._clients:
            inner = self._inner_factory(kind)
            model = getattr(inner, "model", None) or getattr(inner, "model_name", None)
            if model != self.models[kind]:
                raise ValueError(
                    f"{kind} client model {model!r} != declared {self.models[kind]!r}"
                )
            self.prices.for_model(model)
            if getattr(inner, "max_retries", 0) != 0:
                raise ValueError(
                    f"{kind} inner client has max_retries={inner.max_retries}; the "
                    "meter's own loop must own every retry (max_retries=0)"
                )
            max_tokens = getattr(inner, "max_tokens", None)
            if not isinstance(max_tokens, int) or max_tokens <= 0:
                raise ValueError(f"{kind} inner client has no usable max_tokens ({max_tokens!r})")
            self._clients[kind] = MeteredChatModel(self, kind, inner, model, max_tokens)
        return self._clients[kind]

    def generation_llm(self) -> MeteredChatModel:
        """Metered drop-in for ``get_llm()`` used by generation."""
        return self._client("generation")

    def rewrite_llm(self) -> MeteredChatModel:
        """Metered drop-in for ``get_rewrite_llm()`` (``expand_query(llm=)``)."""
        return self._client("rewrite")

    def judge_llm(self) -> MeteredChatModel:
        """Metered drop-in for the judge's ``get_llm()``."""
        return self._client("judge")

    # -- totals ------------------------------------------------------------

    @property
    def latched(self) -> Optional[str]:
        """The limit kind that latched this meter, or None."""
        return self._latched

    def _totals(self) -> Tuple[float, float]:
        with _locked(self.ledger, exclusive=False) as fd:
            records, _ = _read_lines(fd)
        return ledger_totals(records, week=iso_week(self._clock()), run_id=self.run_id)

    @property
    def week_total_eur(self) -> float:
        """This UTC ISO week's charge in EUR across every run and process."""
        return self._totals()[0]

    @property
    def run_total_eur(self) -> float:
        """This run's charge in EUR (settled costs + open worst cases)."""
        return self._totals()[1]

    # -- reserve / settle --------------------------------------------------

    def _check_latch(self) -> None:
        if self._latched is not None:
            raise SpendLimitReached(self._latched, f"spend meter latched ({self._latched})")

    def _base_line(self, *, event: str, rid: str, week: str, model: str, kind: str, attempt: int) -> Dict[str, Any]:
        return {
            "v": LEDGER_SCHEMA_VERSION,
            "event": event,
            "rid": rid,
            "ts": self._clock().astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "week": week,
            "run_id": self.run_id,
            "project": self.project,
            "model": model,
            "kind": kind,
            "attempt": attempt,
            "ceiling_eur": self.ceiling_eur,
            "run_limit_eur": self.run_limit_eur,
            "owner_approved_eur": self.owner_approved_eur,
            "approval_ref": self.approval_ref,
            "price_list_date": self.prices.price_list_date,
        }

    def _reserve(self, *, model: str, kind: str, attempt: int, input_tokens: int, max_tokens: int) -> Tuple[str, str]:
        """Check both limits and append a reserve line, all under LOCK_EX."""
        self._check_latch()
        price = self.prices.for_model(model)
        usd = worst_case_usd(price, input_tokens, max_tokens)
        eur = self.prices.usd_to_eur(usd)
        week = iso_week(self._clock())
        with _locked(self.ledger, exclusive=True) as fd:
            records, complete_len = _read_lines(fd)
            week_total, run_total = ledger_totals(records, week=week, run_id=self.run_id)
            if week_total + eur > self.ceiling_eur:
                self._latched = "week"
                raise SpendLimitReached(
                    "week",
                    f"week {week}: €{week_total:.4f} charged + €{eur:.4f} worst case "
                    f"> ceiling €{self.ceiling_eur:.2f}",
                )
            if self.run_limit_eur is not None and run_total + eur > self.run_limit_eur:
                self._latched = "run"
                raise SpendLimitReached(
                    "run",
                    f"run {self.run_id}: €{run_total:.4f} charged + €{eur:.4f} worst case "
                    f"> run limit €{self.run_limit_eur:.2f}",
                )
            rid = uuid.uuid4().hex
            line = self._base_line(event="reserve", rid=rid, week=week, model=model, kind=kind, attempt=attempt)
            line.update(
                input_tokens=input_tokens, output_tokens=max_tokens,
                cache_write_tokens=0, cache_read_tokens=0, usd=usd, eur=eur,
            )
            _append(fd, line, complete_len=complete_len)
        return rid, week

    def _settle(self, *, rid: str, week: str, model: str, kind: str, attempt: int, usage: Mapping[str, Any]) -> None:
        """Append a settle line with the real cost of ``usage``."""
        price = self.prices.for_model(model)
        usd, tokens = usage_cost_usd(price, usage)
        line = self._base_line(event="settle", rid=rid, week=week, model=model, kind=kind, attempt=attempt)
        line.update(tokens, usd=usd, eur=self.prices.usd_to_eur(usd))
        with _locked(self.ledger, exclusive=True) as fd:
            _, complete_len = _read_lines(fd)
            _append(fd, line, complete_len=complete_len)
