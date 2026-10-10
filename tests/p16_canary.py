"""Shared leak check for the Phase 16A-1 canary tests (acceptance (b)).

``assert_no_leak(secrets, *texts)`` fails if any secret, or any 8-token
window of it, appears in the texts after normalisation (escape-decoded,
NFKC, casefolded, every non-alphanumeric run collapsed to one space), so an
escaped, re-cased, re-spaced or punctuation-altered copy is caught; a
whitespace-deleted copy is caught by a second, space-free comparison.
Secrets shorter than 8 tokens are checked whole. Not a test module.
"""

import codecs
import re
import unicodedata
from typing import Iterable, List

WINDOW = 8


def normalise(text: str) -> str:
    """Escape-decode (best effort), NFKC, casefold, punctuation -> single spaces."""
    try:
        decoded = codecs.decode(text, "unicode_escape")
    except Exception:  # noqa: BLE001 - keep the raw text when it does not decode
        decoded = text
    out = []
    for variant in (text, decoded):
        v = unicodedata.normalize("NFKC", variant).casefold()
        out.append(re.sub(r"[^0-9a-z]+", " ", v).strip())
    return " | ".join(out)


def windows(secret: str, n: int = WINDOW) -> List[str]:
    """The normalised secret whole, plus every ``n``-token window of it."""
    tokens = normalise(secret).split(" | ")[0].split()
    if len(tokens) <= n:
        return [" ".join(tokens)]
    return [" ".join(tokens)] + [" ".join(tokens[i:i + n]) for i in range(len(tokens) - n + 1)]


def assert_no_leak(secrets: Iterable[str], *texts: str, where: str = "") -> None:
    """Fail if any secret (or 8-token window of one) appears in ``texts``."""
    hay = " " + normalise("\n".join(t for t in texts if t)) + " "
    squeezed = hay.replace(" ", "")
    for secret in secrets:
        for w in windows(secret):
            if not w:
                continue
            assert f" {w} " not in hay, f"secret window leaked {where}"
            assert w.replace(" ", "") not in squeezed, f"secret window leaked (re-spaced) {where}"
