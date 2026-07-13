"""Experimental LLM-as-judge faithfulness estimate (Phase 10, D38).

This is a *secondary, disclosed-as-experimental* metric — it never gates
anything. It asks a Claude model to decompose a generated answer into atomic
claims and judge each claim against the retrieved context (supported /
unsupported / unclear), then reports a per-answer faithfulness = supported /
n_claims. Two honesty properties are built in:

- It is judged **per set, per answer** and reported **conditional on a
  non-refused answer** — a refusal has no claims to check, so refusals are
  never fed here.
- The judge is the *same model family* as the generator (``JUDGE_MODEL ==
  GENERATION_MODEL``); the report says so, because a same-family judge can
  share the generator's blind spots. This is a rough estimate, not an
  independent oracle.

Robustness: model JSON is parsed strictly (fence-strip + first-``{`` to
last-``}`` + schema check). API errors and parse errors are counted
*separately*, zero-claim answers are counted, and the aggregate mean is
**suppressed** (flagged invalid in the report) when the failure fraction
exceeds ``FAILURE_SUPPRESSION_THRESHOLD`` — so a run where the judge mostly
failed can never masquerade as a confident score.

Per D30, claim text is carried in per-item records only for a *gitignored*
local review dump; the committed report prints counts and the mean, never the
claims themselves.
"""

import json
import random
import re
from typing import Any, Callable, Dict, List, Optional

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate

from src.generator import GENERATION_MODEL, get_llm

# The judge runs on the same model as generation (D38): stated in the report so
# a reader weights the estimate accordingly (shared-family blind spots).
JUDGE_MODEL = GENERATION_MODEL

# Bump this string whenever the judge prompt changes, so a report's provenance
# pins exactly which judging rubric produced its numbers.
JUDGE_PROMPT_VERSION = "faithfulness-judge-v1"

# If more than this fraction of judged answers fail (API or parse errors), the
# aggregate mean is suppressed as unreliable rather than reported as if clean.
FAILURE_SUPPRESSION_THRESHOLD = 0.20

# The three verdicts the judge may assign a claim. Anything else the model
# emits is normalised to "unclear" (fail-safe: an unrecognised verdict never
# silently counts as support).
_VALID_VERDICTS = ("supported", "unsupported", "unclear")

# Prompt vars: {question}, {answer}, {context}. The model must return ONLY a
# JSON object; we still parse defensively (models add prose or fences).
JUDGE_PROMPT = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            """You are a strict faithfulness judge for a legal RAG system. You are \
given a QUESTION, an ANSWER produced by another model, and the CONTEXT passages \
that answer was supposed to rely on. Decompose the ANSWER into atomic factual \
claims (ignore pure citations, hedging, and restatements of the question). For \
each claim, decide whether the CONTEXT supports it:

- "supported": the context directly states or clearly entails the claim.
- "unsupported": the context contradicts the claim, or the claim is a \
substantive factual assertion the context does not contain.
- "unclear": you cannot tell from the context alone.

Judge ONLY against the provided context — never against your own legal \
knowledge. Return ONLY a JSON object of this exact shape, with no prose and no \
markdown fences:

{{"claims": [{{"claim": "<short paraphrase>", "verdict": "supported|unsupported|unclear"}}]}}

If the answer makes no checkable factual claim, return {{"claims": []}}.""",
        ),
        (
            "human",
            """QUESTION:
{question}

ANSWER:
{answer}

CONTEXT:
{context}""",
        ),
    ]
)


def _default_llm_fn(prompt_vars: Dict[str, str]) -> str:
    """Default judge LLM adapter: build the chain lazily and invoke it.

    Lazy so that importing this module — or injecting a fake ``llm_fn`` in
    tests — never constructs a real client or needs an API key. ``get_llm``
    raises without ``ANTHROPIC_API_KEY``, so this only works on a live run.
    """
    chain = JUDGE_PROMPT | get_llm() | StrOutputParser()
    return chain.invoke(prompt_vars)


def _extract_json_object(text: str) -> str:
    """Return the outermost ``{...}`` span of ``text``, fences stripped.

    Handles the two common ways a model wraps JSON: a ```` ```json ... ``` ````
    fence, and leading/trailing prose. Raises ``ValueError`` if no braces are
    found, which the caller treats as a parse error.
    """
    stripped = text.strip()
    # Drop a leading ```json / ``` fence and any trailing fence.
    stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
    stripped = re.sub(r"\s*```$", "", stripped)
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no JSON object found in judge output")
    return stripped[start : end + 1]


def _normalise_verdict(verdict: Any) -> str:
    """Map a raw verdict to one of ``_VALID_VERDICTS``; unknown -> 'unclear'."""
    if not isinstance(verdict, str):
        return "unclear"
    v = verdict.strip().lower()
    return v if v in _VALID_VERDICTS else "unclear"


def judge_answer(
    question: str,
    answer: str,
    context: str,
    llm_fn: Optional[Callable[[Dict[str, str]], str]] = None,
) -> Dict[str, Any]:
    """Judge one answer's faithfulness to its context.

    Args:
        question: The question the answer responds to.
        answer: The generated answer text to judge.
        context: The retrieved context the answer was meant to rely on
            (typically ``retriever.format_context`` output).
        llm_fn: ``Callable[[prompt_vars], raw_text]``. Defaults to a lazily
            built ``JUDGE_PROMPT | get_llm() | StrOutputParser()`` (live API).
            Tests inject a fake so nothing hits the network.

    Returns:
        A dict:
        ``{"ok", "error_type", "n_claims", "supported", "unsupported",
        "unclear", "faithfulness", "verdicts"}``.
        On success ``ok=True``, ``error_type=None``, ``faithfulness`` =
        supported / n_claims (``None`` when ``n_claims == 0``), and ``verdicts``
        is the list of ``{"claim", "verdict"}`` (kept for the gitignored review
        dump — never printed in the committed report, D30). On an ``llm_fn``
        exception ``ok=False, error_type="api"``; on unparseable/invalid JSON
        ``ok=False, error_type="parse"``. Both error cases zero the counts and
        set ``faithfulness=None``.
    """
    llm_fn = llm_fn or _default_llm_fn

    def _error(error_type: str) -> Dict[str, Any]:
        return {
            "ok": False,
            "error_type": error_type,
            "n_claims": 0,
            "supported": 0,
            "unsupported": 0,
            "unclear": 0,
            "faithfulness": None,
            "verdicts": [],
        }

    try:
        raw = llm_fn(
            {"question": question, "answer": answer, "context": context}
        )
    except Exception:
        # Any llm_fn failure (network, rate limit, auth) is an API error, held
        # apart from parse errors so the report can attribute failures.
        return _error("api")

    try:
        payload = json.loads(_extract_json_object(raw))
        claims = payload["claims"]
        if not isinstance(claims, list):
            raise ValueError("'claims' is not a list")
        for claim in claims:
            # Each element must be an object carrying a string "verdict". A bare
            # string, null, or a verdict-less object is a SCHEMA violation and is
            # counted as a parse error — not as a zero-faithfulness "success",
            # which would silently drag the mean down AND dodge the failure
            # counter that drives suppression.
            if not isinstance(claim, dict) or not isinstance(claim.get("verdict"), str):
                raise ValueError("malformed claim record")
    except Exception:
        return _error("parse")

    verdicts: List[Dict[str, str]] = []
    counts = {"supported": 0, "unsupported": 0, "unclear": 0}
    for claim in claims:
        # verdict is a string here (validated above); an unrecognised VALUE
        # (e.g. "maybe") still normalises to "unclear", never "supported".
        text = str(claim.get("claim", ""))
        verdict = _normalise_verdict(claim.get("verdict"))
        counts[verdict] += 1
        verdicts.append({"claim": text, "verdict": verdict})

    n_claims = len(verdicts)
    faithfulness = counts["supported"] / n_claims if n_claims else None

    return {
        "ok": True,
        "error_type": None,
        "n_claims": n_claims,
        "supported": counts["supported"],
        "unsupported": counts["unsupported"],
        "unclear": counts["unclear"],
        "faithfulness": faithfulness,
        "verdicts": verdicts,
    }


def judge_answers(
    items: List[Dict[str, str]],
    llm_fn: Optional[Callable[[Dict[str, str]], str]] = None,
    sample_n: Optional[int] = None,
    seed: int = 0,
) -> Dict[str, Any]:
    """Judge a list of answers and aggregate a faithfulness estimate.

    Args:
        items: One dict per answer to judge, each with ``question``, ``answer``,
            ``context`` keys. Callers pass ONLY non-refused, in-corpus answers
            (a refusal has no claims to judge). Judging is per set — the caller
            never pools two sets into one ``items`` list.
        llm_fn: Judge adapter, forwarded to ``judge_answer`` (default = live).
        sample_n: If given and smaller than ``len(items)``, judge a
            deterministic random sample of this size (``random.Random(seed)``);
            otherwise judge all. The sample size actually judged is reported.
        seed: Seed for the deterministic sample (default 0).

    Returns:
        A dict with the aggregate estimate and provenance:
        ``mean_faithfulness`` (mean over judgments that parsed AND had >=1
        claim; ``None`` if none), ``scored_n`` (how many that mean averaged
        over), ``attempted`` (answers judged), ``successful`` (parsed OK),
        ``api_errors``, ``parse_errors``, ``zero_claim`` (parsed but no
        claims), ``failure_rate``, ``suppressed`` (True when
        ``failure_rate > FAILURE_SUPPRESSION_THRESHOLD`` — the mean is then not
        to be trusted), ``sample_n``, ``seed``, ``judge_model``,
        ``prompt_version``, and ``per_item`` (full ``judge_answer`` records,
        including claim text, for the gitignored review dump only).
    """
    if sample_n is not None and sample_n < 0:
        raise ValueError(f"sample_n must be >= 0, got {sample_n}")

    # Deterministic sub-sample when asked for fewer than we have.
    to_judge = items
    if sample_n is not None and 0 <= sample_n < len(items):
        to_judge = random.Random(seed).sample(items, sample_n)

    per_item: List[Dict[str, Any]] = []
    for item in to_judge:
        record = judge_answer(
            item["question"], item["answer"], item["context"], llm_fn=llm_fn
        )
        # Carry the question through for the review dump (not the report).
        record = {"question": item["question"], **record}
        per_item.append(record)

    attempted = len(per_item)
    api_errors = sum(1 for r in per_item if r["error_type"] == "api")
    parse_errors = sum(1 for r in per_item if r["error_type"] == "parse")
    successful = sum(1 for r in per_item if r["ok"])
    zero_claim = sum(1 for r in per_item if r["ok"] and r["n_claims"] == 0)

    scored = [r["faithfulness"] for r in per_item if r["ok"] and r["faithfulness"] is not None]
    mean_faithfulness = sum(scored) / len(scored) if scored else None

    failures = api_errors + parse_errors
    failure_rate = failures / attempted if attempted else 0.0
    suppressed = failure_rate > FAILURE_SUPPRESSION_THRESHOLD

    return {
        "mean_faithfulness": mean_faithfulness,
        "scored_n": len(scored),
        "attempted": attempted,
        "successful": successful,
        "api_errors": api_errors,
        "parse_errors": parse_errors,
        "zero_claim": zero_claim,
        "failure_rate": failure_rate,
        "suppressed": suppressed,
        "sample_n": sample_n,
        "seed": seed,
        "judge_model": JUDGE_MODEL,
        "prompt_version": JUDGE_PROMPT_VERSION,
        "per_item": per_item,
    }
