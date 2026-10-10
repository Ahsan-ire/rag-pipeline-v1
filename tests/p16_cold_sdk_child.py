"""Cold-interpreter child for the SDK logging-boundary regression (16A-1 merge gate round 3).

Not a test module. ``tests/test_p16w_spend.py`` runs it as
``python -m tests.p16_cold_sdk_child`` in a FRESH interpreter, because the
defect only exists there: httpx imports httpcore lazily, inside the first
``httpx.HTTPTransport()``, so in a cold process httpcore's loggers
(``httpcore.http11`` ...) are created *inside* the first metered call. A
boundary that only filters loggers existing at entry misses them, and
httpcore logs ``repr(exception)`` on a failed trace, where h11's
"illegal chunk header" error quotes the received bytes, i.e. response content.

What it does (nothing leaves the process: httpcore's ``MockBackend`` serves
the bytes):

1. one metered call -- a real ``SpendMeter`` / ``MeteredChatModel`` over the
   real anthropic SDK, ``httpx.HTTPTransport`` and httpcore's HTTP/1.1 parser
   -- whose prompt carries one canary and whose malformed chunked response
   carries another; every log record of the call is written to
   ``metered.log``;
2. a positive control: the same request *outside* any metered call, logged
   to ``control.log`` -- it must quote the body canary, which proves the
   parser path really logs response content and that the boundary is gone.

The inner model is a plain object, not a ``BaseChatModel``: importing
``langchain_core.language_models`` loads langsmith, which imports httpcore
and would warm the process. It drives the LangChain callbacks itself, so the
meter still reserves before the request.

Environment (set by the parent): ``P16_COLD_BODY_CANARY``,
``P16_COLD_PROMPT_CANARY``, ``P16_COLD_OUT`` (output dir), ``P16_COLD_PRIME``
(``"0"`` disables the priming layer, leaving only the handler filter) and the
per-test ``CC_SPEND_LEDGER``. Prints one JSON object of flags and logger
names on stdout -- never a canary.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, List, Tuple

BODY_CANARY = os.environ["P16_COLD_BODY_CANARY"]
PROMPT_CANARY = os.environ["P16_COLD_PROMPT_CANARY"]
OUT = Path(os.environ["P16_COLD_OUT"])
PRIME = os.environ.get("P16_COLD_PRIME", "1") != "0"
PRICES = Path(__file__).resolve().parent / "fixtures" / "p16w_prices.toml"
MODELS = {"generation": "fake-gen", "rewrite": "fake-rewrite", "judge": "fake-judge"}

# A malformed chunked response: the chunk-size line is the canary, so h11
# raises "illegal chunk header: b'<canary>'" and httpcore's
# ``receive_response_body.failed`` trace logs that exception's repr.
WIRE = (
    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
    b"Transfer-Encoding: chunked\r\n\r\n" + BODY_CANARY.encode("ascii") + b"\r\n"
)

RECORDS: List[Tuple[str, str]] = []


class _Capture(logging.Handler):
    """Root handler keeping every record's logger name and formatted text."""

    def emit(self, record: logging.LogRecord) -> None:
        RECORDS.append((record.name, self.format(record)))


def main() -> None:
    """Run the metered call and the control call; print the flags."""
    cold_at_start = "httpcore" not in sys.modules
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(_Capture())

    import anthropic
    import httpx
    from langchain_core.callbacks import CallbackManager
    from langchain_core.messages import AIMessage, HumanMessage
    from langchain_core.outputs import ChatGeneration, LLMResult

    from src import spend

    # This child belongs to a pytest test (CC_SPEND_LEDGER is that test's tmp
    # ledger), but pytest is not imported here, so the meter would refuse
    # the variable.
    spend._under_pytest = lambda: True
    if not PRIME:
        spend._SDK_PRIME_MODULES = ()

    seen: dict = {}

    class RawSDKChat:
        """The real anthropic SDK behind ``MeteredChatModel``'s inner interface."""

        model = "fake-gen"
        max_tokens = 64
        max_retries = 0

        def invoke(self, messages: List[Any], config: Any = None, **kwargs: Any) -> AIMessage:
            manager = CallbackManager.configure((config or {}).get("callbacks"))
            [run] = manager.on_chat_model_start(
                {}, [messages], invocation_params={"max_tokens": self.max_tokens}
            )
            seen.setdefault("httpcore_loaded_in_call", "httpcore" in sys.modules)
            transport = httpx.HTTPTransport()  # cold: this line imports httpcore
            from httpcore._backends.mock import MockBackend

            transport._pool._network_backend = MockBackend([WIRE])
            try:
                with anthropic.Client(
                    api_key="sk-ant-fake-for-tests",
                    base_url="https://api.anthropic.invalid",
                    max_retries=0,
                    http_client=httpx.Client(transport=transport, trust_env=False),
                ) as client:
                    reply = client.messages.create(
                        model=self.model,
                        max_tokens=self.max_tokens,
                        messages=[{"role": "user", "content": str(messages[-1].content)}],
                    )
            except BaseException as exc:
                run.on_llm_error(exc)
                raise
            message = AIMessage(content=reply.content[0].text)
            run.on_llm_end(LLMResult(generations=[[ChatGeneration(message=message)]]))
            return message

    meter = spend.SpendMeter(
        spend.load_prices(PRICES), None, 1.0, models=MODELS,
        inner_factory=lambda kind: RawSDKChat(), sleep=lambda s: None, max_attempts=1,
    )
    llm = meter.generation_llm()
    cold_at_invoke = "httpcore" not in sys.modules

    metered_error = None
    try:
        llm.invoke([HumanMessage(content=f"question {PROMPT_CANARY}")])
    except Exception as exc:  # the malformed body must fail the call
        metered_error = type(exc).__name__
    metered = list(RECORDS)
    depth_after = spend._sdk_drop_depth

    control_error = None
    try:
        RawSDKChat().invoke([HumanMessage(content="control")])
    except Exception as exc:
        control_error = type(exc).__name__
    control = RECORDS[len(metered):]

    (OUT / "metered.log").write_text("\n".join(text for _, text in metered), encoding="utf-8")
    (OUT / "control.log").write_text("\n".join(text for _, text in control), encoding="utf-8")
    ledger = Path(os.environ["CC_SPEND_LEDGER"])
    events = [json.loads(line)["event"] for line in ledger.read_text().splitlines() if line.strip()]
    print(json.dumps({
        "cold_at_start": cold_at_start,
        "cold_at_invoke": cold_at_invoke,
        "httpcore_loaded_in_call": seen.get("httpcore_loaded_in_call"),
        "metered_error": metered_error,
        "control_error": control_error,
        "control_body_loggers": sorted({name for name, text in control if BODY_CANARY in text}),
        "depth_after": depth_after,
        "ledger_events": events,
    }))


if __name__ == "__main__":
    main()
