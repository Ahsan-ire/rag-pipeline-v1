"""Tests for src/render.py (H2 uncited hint, H3 render contract, H5 source label)
and for query()'s use of it (leak tests, audit values per path).

No network or model calls: generation is a patched return value, retrieval is a
patched list, and the audit log is pointed at tmp_path.
"""

import json
from unittest.mock import patch

import pytest
from langchain_core.documents import Document

from src.generator import CAVEAT_PREFIX, REFUSAL_PHRASE
from src.grounding import (
    ANSWER_TRUNCATED,
    CITATIONS_UNVERIFIED,
    CITATIONS_VERIFIED,
    GENERATION_INCOMPLETE,
    MODEL_DECLINED,
    PARTIALLY_VERIFIED,
    REFUSAL,
    UNKNOWN_STATUS_NOTICE,
    WITHHELD_NOTICES,
)
from scripts.sample_corpus import TITLE as SAMPLE_TITLE
from src.pipeline import query
from src.query_rewrite import REWRITE_MODEL, STATUS_DISABLED, Expansion
from src.render import (
    BLOCKED_NOTICE,
    DISCLAIMER,
    NO_RESULTS_MESSAGE,
    UNCITED_HEADING,
    RenderFlags,
    prettify_title,
    render,
    source_titles,
    uncited_statements,
)

PUBLIC_KEYS = {
    "answer",
    "gate_outcome",
    "citations",
    "sources",
    "citation_check",
    "source_documents",
    "answer_chars",
    "generation_status",
    "stop_reason",
    "uncited_count",
}
CITED = "The vendor must deliver title [Handbook, para 3.2, p.5]."
SENTINEL_DRAFT = "SENTINELDRAFT the draft says something confidential here."
SENTINEL_UNCITED = "SENTINELUNCITED this sentence has no citation at all."
TERMINALS = {
    "truncated": ANSWER_TRUNCATED,
    "declined": MODEL_DECLINED,
    "incomplete": GENERATION_INCOMPLETE,
}
TERMINAL_ACTIONS = {
    ANSWER_TRUNCATED: "withheld_truncated",
    MODEL_DECLINED: "withheld_declined",
    GENERATION_INCOMPLETE: "withheld_incomplete",
}


def _cite(para="3.2", page="5"):
    return {"para": para, "page": page, "raw": f"Handbook, para {para}, p.{page}"}


def _doc(section="3.2", p0=5, p1=5, title="Conveyancing_Handbook.pdf"):
    meta = {"section_number": section, "page_start": p0, "page_end": p1}
    if title is not None:
        meta["title"] = title
    return Document(page_content="chunk text", metadata=meta)


def _retrieved(*docs):
    return [{"document": d, "score": 0.1} for d in docs]


def _result(answer, outcome, status="complete", grounded=(), ungrounded=(), docs=None):
    cites = list(grounded) + list(ungrounded)
    return {
        "answer": answer,
        "citations": cites,
        "sources": [c["raw"] for c in cites],
        "source_documents": docs if docs is not None else [_doc()],
        "citation_check": {"grounded": list(grounded), "ungrounded": list(ungrounded)},
        "gate_outcome": outcome,
        "generation_status": status,
        "stop_reason": "end_turn" if status == "complete" else None,
    }


# --- H2: uncited-statement hint ---------------------------------------------
class TestUncitedStatements:
    def test_cited_sentence_not_flagged(self):
        assert uncited_statements(CITED) == []

    def test_uncited_sentence_flagged(self):
        text = f"{CITED} The purchaser may then raise requisitions on title."
        assert uncited_statements(text) == [
            "The purchaser may then raise requisitions on title."
        ]

    def test_heading_not_flagged(self):
        text = f"## Delivery of title and related obligations\n{CITED}"
        assert uncited_statements(text) == []

    def test_short_unit_not_flagged(self):
        assert uncited_statements(f"{CITED} See above.") == []

    def test_list_lead_in_not_flagged(self):
        text = "The following documents must be furnished by the vendor:\n- a map [Handbook, para 3.2, p.5]."
        assert uncited_statements(text) == []

    def test_narrow_gap_statement_exempt(self):
        text = f"{CITED} The handbook does not address stamp duty on this transaction."
        assert uncited_statements(text) == []

    def test_d32_hedge_flagged(self):
        hedge = "This is not covered in the source material, but the likely answer is 20 days."
        assert uncited_statements(hedge) == [hedge]

    def test_hedge_word_matched_as_whole_word(self):
        # "butter"/"usually"-free: 'but' inside another word must not defeat the exemption
        text = "The handbook does not mention butterfly easements in this chapter."
        assert uncited_statements(text) == []

    def test_warranty_sentence_flagged(self):
        s = "Defects not covered by the warranty remain the vendor's risk."
        assert uncited_statements(s) == [s]

    def test_leading_caveat_stripped(self):
        text = f"{CAVEAT_PREFIX} {CITED}"
        assert uncited_statements(text) == []

    def test_repeated_caveat_flagged(self):
        text = f"{CAVEAT_PREFIX} {CITED}\n{CAVEAT_PREFIX} {CITED}"
        assert uncited_statements(text) == ["repeated caveat"]

    def test_refusal_flags_nothing(self):
        assert uncited_statements(REFUSAL_PHRASE) == []

    def test_lowercase_start_is_a_documented_miss(self):
        # split_sentences does not split before a lowercase letter, so the
        # uncited second sentence rides on the first sentence's citation.
        text = "The vendor delivers title [Handbook, para 3.2, p.5]. then the purchaser pays the deposit in full."
        assert uncited_statements(text) == []


# --- H5: helpers ------------------------------------------------------------
class TestSourceLabel:
    @pytest.mark.parametrize(
        "raw,pretty",
        [
            ("Conveyancing_Handbook.pdf", "Conveyancing Handbook"),
            ("Land_and_Conveyancing_Law_Reform_Act_2009.html", "Land and Conveyancing Law Reform Act 2009"),
            ("sample_conveyancing.txt", "sample conveyancing"),
            ("Succession Act 1965", "Succession Act 1965"),
        ],
    )
    def test_prettify(self, raw, pretty):
        assert prettify_title(raw) == pretty

    def test_titles_come_from_verified_citation_chunks(self):
        handbook = _doc("3.2", 5, 5, "Conveyancing_Handbook.pdf")
        other = _doc("9.9", 90, 90, "Other_Source.pdf")
        titles = source_titles(
            {"grounded": [_cite("3.2", "5")], "ungrounded": []}, _retrieved(handbook, other)
        )
        assert titles == ["Conveyancing Handbook"]

    def test_titles_sorted_unique(self):
        a = _doc("3.2", 5, 5, "B_Source.pdf")
        b = _doc("3.2", 5, 5, "A_Source.pdf")
        c = _doc("3.2", 5, 5, "A_Source.pdf")
        titles = source_titles(
            {"grounded": [_cite()], "ungrounded": []}, _retrieved(a, b, c)
        )
        assert titles == ["A Source", "B Source"]

    def test_falls_back_to_retrieved_chunks_when_none_match(self):
        a = _doc("9.9", 90, 90, "Act_2009.html")
        b = _doc("8.8", 80, 80, "Handbook.pdf")
        titles = source_titles(
            {"grounded": [_cite("3.2", "5")], "ungrounded": []}, _retrieved(a, b)
        )
        assert titles == ["Act 2009", "Handbook"]

    def test_legislation_and_sample_index_fixtures(self):
        leg = _doc("77", 1, 1, "Succession_Act_1965.html")
        sample = _doc("1.1", 1, 1, SAMPLE_TITLE)  # the sample index stores no extension
        titles = source_titles(
            {"grounded": [_cite("77", "1"), _cite("1.1", "1")], "ungrounded": []},
            _retrieved(leg, sample),
        )
        assert titles == ["Sample Conveyancing Handbook", "Succession Act 1965"]

    def test_chunk_without_title_adds_nothing(self):
        titles = source_titles(
            {"grounded": [_cite()], "ungrounded": []}, _retrieved(_doc(title=None))
        )
        assert titles == []


class TestH5Display:
    def _shown(self, outcome, **kw):
        grounded = [_cite()]
        ungrounded = [_cite("7.7", "70")] if outcome == PARTIALLY_VERIFIED else []
        res = _result(CITED, outcome, grounded=grounded, ungrounded=ungrounded)
        return render(res, RenderFlags(retrieved=_retrieved(_doc()), **kw)), res

    @pytest.mark.parametrize("outcome", [CITATIONS_VERIFIED, PARTIALLY_VERIFIED])
    def test_label_and_disclaimer_on_verified_and_partial(self, outcome):
        r, res = self._shown(outcome)
        assert "Source: Conveyancing Handbook" in r.display_text
        assert r.display_text.rstrip().endswith(DISCLAIMER)
        # Display-only: nothing leaks into the public answer or its length.
        assert r.public_result["answer"] == CITED
        assert r.public_result["answer_chars"] == len(CITED)
        assert DISCLAIMER not in json.dumps(r.public_result, default=str)
        assert "Source:" not in r.public_result["answer"]

    def test_override_draft_has_disclaimer_but_no_source_label(self):
        res = _result("A draft claim without any citation here.", CITATIONS_UNVERIFIED)
        r = render(res, RenderFlags(show_unverified=True, retrieved=_retrieved(_doc())))
        assert "UNVERIFIED DRAFT" in r.display_text
        assert "Source:" not in r.display_text
        assert DISCLAIMER in r.display_text

    @pytest.mark.parametrize("status", ["truncated", "declined", "incomplete"])
    def test_absent_on_terminal(self, status):
        res = _result(CITED, CITATIONS_VERIFIED, status=status, grounded=[_cite()])
        r = render(res, RenderFlags(retrieved=_retrieved(_doc())))
        assert "Source:" not in r.display_text and DISCLAIMER not in r.display_text

    def test_absent_on_refusal_blocked_legacy_and_no_results(self):
        docs = _retrieved(_doc())
        for res, flags in [
            (_result(REFUSAL_PHRASE, REFUSAL), RenderFlags(retrieved=docs)),
            (_result("Uncited draft claim about completion dates.", CITATIONS_UNVERIFIED), RenderFlags(retrieved=docs)),
            (_result(CITED, None, status="unknown"), RenderFlags(retrieved=docs)),
            (None, RenderFlags(no_results=True)),
        ]:
            text = render(res, flags).display_text
            assert "Source:" not in text and DISCLAIMER not in text

    def test_refusal_matching_unchanged_by_decoration(self):
        from src.generator import is_refusal

        r = render(_result(REFUSAL_PHRASE, REFUSAL), RenderFlags())
        assert r.public_result["answer"] == REFUSAL_PHRASE
        assert is_refusal(r.public_result["answer"])


# --- H3: render matrix (spec amendment 4) -----------------------------------
def _cases():
    """Every reachable state, per amendment 4's table."""
    cases = [("no_results", None, None, False, None)]
    cases.append(("legacy", None, None, False, None))
    for status in ("complete", "unknown"):
        cases.append(("refusal", status, None, False, None))
        for outcome in (CITATIONS_VERIFIED, PARTIALLY_VERIFIED):
            for uncited in (True, False):
                cases.append((outcome, status, uncited, False, None))
        for override in (False, True):
            cases.append(("blocked", status, None, override, None))
    for term in TERMINALS:
        for override in (False, True):
            cases.append(("terminal", term, None, override, None))
    return cases


def _build(kind, status, uncited):
    """Return (result, flags_kwargs) for one matrix row."""
    if kind == "legacy":
        res = _result(SENTINEL_DRAFT, None, status="unknown", grounded=[_cite()])
        del res["generation_status"]
        return res
    if kind == "refusal":
        return _result(REFUSAL_PHRASE, REFUSAL, status=status)
    if kind in (CITATIONS_VERIFIED, PARTIALLY_VERIFIED):
        answer = CITED + (f" {SENTINEL_UNCITED}" if uncited else "")
        ung = [_cite("7.7", "70")] if kind == PARTIALLY_VERIFIED else []
        return _result(answer, kind, status=status, grounded=[_cite()], ungrounded=ung)
    if kind == "blocked":
        return _result(SENTINEL_DRAFT, CITATIONS_UNVERIFIED, status=status, ungrounded=[_cite("7.7", "70")])
    return _result(SENTINEL_DRAFT, CITATIONS_UNVERIFIED, status=status, grounded=[_cite()])


@pytest.mark.parametrize("kind,status,uncited,override,_", _cases())
def test_render_matrix(kind, status, uncited, override, _):
    if kind == "no_results":
        r = render(None, RenderFlags(no_results=True))
        assert r.action == "no_results"
        assert r.public_result["answer"] == NO_RESULTS_MESSAGE
        assert r.public_result["generation_status"] == "not_run"
        assert r.public_result["uncited_count"] is None
        assert r.public_result["answer_chars"] == 0
        assert set(r.public_result) == PUBLIC_KEYS
        return

    res = _build(kind, status, uncited)
    flags = RenderFlags(show_unverified=override, retrieved=_retrieved(_doc()))
    r = render(res, flags)
    pub, text = r.public_result, r.display_text
    assert set(pub) == PUBLIC_KEYS
    assert pub["answer_chars"] == len(res["answer"])

    if kind == "legacy":
        assert r.action == "shown"
        assert pub["gate_outcome"] is None and pub["uncited_count"] is None
        assert pub["generation_status"] == "unknown"
        assert pub["answer"] == SENTINEL_DRAFT
        assert text.startswith(f"\nAnswer:\n{SENTINEL_DRAFT}")
        assert UNKNOWN_STATUS_NOTICE not in text  # legacy never shows it
        assert "Source:" not in text and DISCLAIMER not in text
        return

    if kind == "terminal":
        outcome = TERMINALS[status]
        notice = WITHHELD_NOTICES[outcome]
        assert r.action == TERMINAL_ACTIONS[outcome]
        assert text == f"\n{notice}"  # no sources, no override hint, nothing else
        assert pub["answer"] == notice and pub["gate_outcome"] == outcome
        assert pub["generation_status"] == status
        assert pub["uncited_count"] is None
        assert SENTINEL_DRAFT not in text + json.dumps(pub, default=str)
        return

    unknown_shown = UNKNOWN_STATUS_NOTICE in text
    if kind == "blocked":
        if override:
            assert r.action == "shown_unverified_override"
            assert SENTINEL_DRAFT in text and "UNVERIFIED DRAFT" in text
            assert pub["answer"] == SENTINEL_DRAFT
            assert isinstance(pub["uncited_count"], int)
            assert DISCLAIMER in text and "Source:" not in text
            assert unknown_shown == (status == "unknown")
        else:
            assert r.action == "blocked_unverified"
            assert "BLOCKED" in text and "--show-unverified" in text
            assert SENTINEL_DRAFT not in text + json.dumps(pub, default=str)
            assert pub["answer"] == BLOCKED_NOTICE
            assert pub["uncited_count"] is None
            assert not unknown_shown  # blocked is not a shown outcome
        assert pub["gate_outcome"] == CITATIONS_UNVERIFIED
        return

    # refusal / verified / partial: shown outcomes
    assert unknown_shown == (status == "unknown")
    assert pub["generation_status"] == status
    assert pub["answer"] == res["answer"]
    if kind == "refusal":
        assert r.action == "refusal_shown" and pub["uncited_count"] is None
        assert UNCITED_HEADING not in text
        return
    assert r.action == ("shown" if kind == CITATIONS_VERIFIED else "shown_with_warning")
    assert pub["uncited_count"] == (1 if uncited else 0)
    assert (UNCITED_HEADING in text) == uncited
    assert (SENTINEL_UNCITED in text) == uncited
    assert DISCLAIMER in text and "Source: Conveyancing Handbook" in text


def test_unknown_notice_is_in_display_not_in_public_answer():
    r = render(_result(CITED, CITATIONS_VERIFIED, status="unknown", grounded=[_cite()]), RenderFlags(retrieved=_retrieved(_doc())))
    assert UNKNOWN_STATUS_NOTICE in r.display_text
    assert UNKNOWN_STATUS_NOTICE not in r.public_result["answer"]


def test_status_complete_never_shows_unknown_line():
    r = render(_result(CITED, CITATIONS_VERIFIED, grounded=[_cite()]), RenderFlags(retrieved=_retrieved(_doc())))
    assert UNKNOWN_STATUS_NOTICE not in r.display_text


def test_legacy_display_is_exact_v1():
    """(d) The legacy path keeps D35's exact v1 display: answer, citations,
    zero-citation warning, ungrounded warning — nothing added."""
    res = _result("A bare claim.", None)
    del res["generation_status"]
    r = render(res, RenderFlags())
    assert r.display_text == (
        "\nAnswer:\nA bare claim."
        "\n\n⚠ WARNING: this answer contains no citations and could not be "
        "verified\n  against the retrieved sources — treat it as unverified."
    )
    res2 = _result("Claim [Handbook, para 7.7, p.70].", None, ungrounded=[_cite("7.7", "70")])
    del res2["generation_status"]
    assert render(res2, RenderFlags()).display_text == (
        "\nAnswer:\nClaim [Handbook, para 7.7, p.70].\n\nCitations found:"
        "\n  - Handbook, para 7.7, p.70"
        "\n\n⚠ Ungrounded citations (not matched to any retrieved chunk):"
        "\n  - Handbook, para 7.7, p.70"
    )


def test_blocked_public_result_follows_allowlist():
    res = _result(SENTINEL_DRAFT, CITATIONS_UNVERIFIED, ungrounded=[_cite("7.7", "70")])
    res["secret_future_key"] = SENTINEL_DRAFT
    pub = render(res, RenderFlags()).public_result
    assert "secret_future_key" not in pub
    assert pub["citation_check"] == res["citation_check"]
    assert pub["citations"] == res["citations"]


def test_explicit_none_status_is_unknown_and_shows_notice():
    res = _result(CITED, CITATIONS_VERIFIED, status=None, grounded=[_cite()])
    r = render(res, RenderFlags(retrieved=_retrieved(_doc())))
    assert UNKNOWN_STATUS_NOTICE in r.display_text
    assert r.public_result["generation_status"] == "unknown"
    assert r.action == "shown"


def test_legacy_path_with_none_status_never_shows_unknown_notice():
    res = _result("Claim.", None, status=None)
    r = render(res, RenderFlags())
    assert UNKNOWN_STATUS_NOTICE not in r.display_text


@pytest.mark.parametrize("status", ["", 0, False])
@pytest.mark.parametrize("outcome", [None, CITATIONS_VERIFIED, CITATIONS_UNVERIFIED])
@pytest.mark.parametrize("override", [False, True])
def test_falsey_unrecognised_status_withholds_draft(status, outcome, override):
    """A falsey, unrecognised status is not `unknown`: render withholds the draft."""
    res = _result(SENTINEL_DRAFT, outcome, status=status, grounded=[_cite()])
    r = render(res, RenderFlags(show_unverified=override, retrieved=_retrieved(_doc())))
    assert r.action == "withheld_incomplete"
    assert r.public_result["gate_outcome"] == GENERATION_INCOMPLETE
    assert r.public_result["answer"] == WITHHELD_NOTICES[GENERATION_INCOMPLETE]
    assert SENTINEL_DRAFT not in r.display_text
    assert SENTINEL_DRAFT not in json.dumps(r.public_result, default=str)
    assert r.public_result["generation_status"] == "incomplete"  # closed vocabulary


def test_terminal_beats_refusal_sentence_and_override():
    res = _result(REFUSAL_PHRASE, REFUSAL, status="truncated")
    r = render(res, RenderFlags(show_unverified=True))
    assert r.public_result["gate_outcome"] == ANSWER_TRUNCATED
    assert r.action == "withheld_truncated"


# --- (c) leak tests + (i) audit values, through query() ---------------------
class TestQueryLeaksAndAudit:
    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        self.log = tmp_path / "audit_log.jsonl"
        monkeypatch.setenv("AUDIT_LOG_PATH", str(self.log))
        monkeypatch.setattr("src.audit._git_sha", lambda: "t3st5ha")
        monkeypatch.setattr(
            "src.pipeline.load_retrieval_context", lambda *a, **k: (object(), object())
        )
        monkeypatch.setattr(
            "src.pipeline.expand_query",
            lambda q, *, enabled=True: Expansion(q, (), REWRITE_MODEL, STATUS_DISABLED),
        )

    def _run(self, generated, retrieved=True, **kw):
        results = _retrieved(_doc()) if retrieved else []
        with patch("src.pipeline.retrieve", return_value=results), patch(
            "src.pipeline.generate_with_sources", return_value=generated
        ):
            return query("a client question", **kw)

    def _events(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    @pytest.mark.parametrize("raw_queries", [False, True])
    @pytest.mark.parametrize("show_unverified", [False, True])
    @pytest.mark.parametrize("status", ["truncated", "declined", "incomplete"])
    def test_terminal_leaks_nothing(self, capsys, monkeypatch, status, show_unverified, raw_queries):
        if raw_queries:
            monkeypatch.setenv("AUDIT_LOG_RAW_QUERIES", "1")
        gen = _result(
            f"{SENTINEL_DRAFT} {SENTINEL_UNCITED}", CITATIONS_VERIFIED, status=status,
            grounded=[_cite()],
        )
        gen["stop_reason"] = "max_tokens"
        out_dict = self._run(gen, show_unverified=show_unverified)
        out = capsys.readouterr().out
        audit = self.log.read_text()
        for sentinel in ("SENTINELDRAFT", "SENTINELUNCITED"):
            assert sentinel not in out
            assert sentinel not in json.dumps(out_dict, default=str)
            assert sentinel not in audit
        assert "--show-unverified" not in out and "Retrieved sources" not in out
        events = self._events()
        assert len(events) == 1
        assert events[0]["action"] == TERMINAL_ACTIONS[TERMINALS[status]]
        assert events[0]["generation_status"] == status
        assert events[0]["stop_reason"] == "max_tokens"
        assert events[0]["uncited_count"] is None  # not computed -> null
        # The audit length is the DRAFT's, not the (shorter) withheld notice's.
        assert events[0]["answer_chars"] == len(gen["answer"])
        assert events[0]["answer_chars"] != len(out_dict["answer"])

    @pytest.mark.parametrize("raw_queries", [False, True])
    @pytest.mark.parametrize(
        "case", ["verified", "partial", "override", "refusal"]
    )
    def test_shown_outcomes_never_put_text_in_audit(self, monkeypatch, case, raw_queries):
        if raw_queries:
            monkeypatch.setenv("AUDIT_LOG_RAW_QUERIES", "1")
        draft = f"{CITED} {SENTINEL_UNCITED} {SENTINEL_DRAFT}"
        kw = {}
        if case == "verified":
            gen = _result(draft, CITATIONS_VERIFIED, grounded=[_cite()])
            action = "shown"
        elif case == "partial":
            gen = _result(
                draft, PARTIALLY_VERIFIED,
                grounded=[_cite()], ungrounded=[_cite("7.7", "70")],
            )
            action = "shown_with_warning"
        elif case == "override":
            gen = _result(draft, CITATIONS_UNVERIFIED)
            kw["show_unverified"] = True
            action = "shown_unverified_override"
        else:
            draft = f"{REFUSAL_PHRASE} SENTINELDRAFT SENTINELUNCITED"
            gen = _result(draft, REFUSAL)
            action = "refusal_shown"
        # The fixture really contains the sentinels the audit must not carry.
        assert "SENTINELDRAFT" in gen["answer"] and "SENTINELUNCITED" in gen["answer"]
        pub = self._run(gen, **kw)
        audit = self.log.read_text()
        assert "SENTINELUNCITED" not in audit and "SENTINELDRAFT" not in audit
        assert "Conveyancing" not in audit  # titles are display-only too
        ev = self._events()[0]
        assert ev["action"] == action
        assert ev["answer_chars"] == len(draft)
        if case == "refusal":
            assert ev["uncited_count"] is None and pub["uncited_count"] is None
        else:
            assert isinstance(ev["uncited_count"], int) and ev["uncited_count"] >= 1
            assert ev["uncited_count"] == pub["uncited_count"]
        assert ev["generation_status"] == "complete" and ev["stop_reason"] == "end_turn"

    def test_blocked_audit_answer_chars_is_draft_length(self):
        gen = _result(SENTINEL_DRAFT, CITATIONS_UNVERIFIED, ungrounded=[_cite("7.7", "70")])
        pub = self._run(gen)
        ev = self._events()[0]
        assert ev["answer_chars"] == len(SENTINEL_DRAFT) != len(pub["answer"])
        assert pub["answer_chars"] == len(SENTINEL_DRAFT)

    def test_blocked_default_does_not_leak_stdout_or_return(self, capsys):
        gen = _result(SENTINEL_DRAFT, CITATIONS_UNVERIFIED, ungrounded=[_cite("7.7", "70")])
        out_dict = self._run(gen)
        assert "SENTINELDRAFT" not in capsys.readouterr().out
        assert "SENTINELDRAFT" not in json.dumps(out_dict, default=str)
        assert self._events()[0]["action"] == "blocked_unverified"
        assert self._events()[0]["uncited_count"] is None

    def test_override_audit_counts_uncited_without_text(self, capsys):
        gen = _result(
            "An unverified draft sentence SENTINELUNCITED with no locator.",
            CITATIONS_UNVERIFIED,
        )
        out_dict = self._run(gen, show_unverified=True)
        out = capsys.readouterr().out
        assert "SENTINELUNCITED" in out  # the operator asked to see the draft
        assert out_dict["uncited_count"] == 1
        ev = self._events()[0]
        assert ev["action"] == "shown_unverified_override" and ev["uncited_count"] == 1
        assert "SENTINELUNCITED" not in self.log.read_text()

    def test_no_results_audit_values(self, capsys):
        out_dict = self._run({}, retrieved=False)
        assert out_dict["answer"] == NO_RESULTS_MESSAGE
        assert set(out_dict) == PUBLIC_KEYS
        ev = self._events()[0]
        assert ev["action"] == "no_results"
        assert ev["generation_status"] == "not_run"
        assert ev["stop_reason"] is None and ev["uncited_count"] is None
        assert ev["answer_chars"] == 0

    def test_refusal_and_legacy_audit_values(self):
        pub = self._run(_result(REFUSAL_PHRASE, REFUSAL))
        assert pub["uncited_count"] is None
        legacy = _result("Claim.", None)
        del legacy["generation_status"]
        self._run(legacy)
        refusal_ev, legacy_ev = self._events()
        assert refusal_ev["action"] == "refusal_shown" and refusal_ev["uncited_count"] is None
        assert legacy_ev["uncited_count"] is None
        assert legacy_ev["action"] == "shown"
        assert legacy_ev["generation_status"] == "unknown"
        assert legacy_ev["gate_outcome"] is None

    def test_exactly_one_event_per_query(self):
        self._run(_result(CITED, CITATIONS_VERIFIED, grounded=[_cite()]))
        assert len(self._events()) == 1

    def test_verbose_chunk_scores_print_before_answer(self, capsys):
        self._run(_result(CITED, CITATIONS_VERIFIED, grounded=[_cite()]), verbose=True)
        out = capsys.readouterr().out
        assert out.index("Retrieved chunks (by fused RRF score)") < out.index("Answer:")
