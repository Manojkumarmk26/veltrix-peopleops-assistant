"""Section 5: eligibility and precedence (authority -> scope -> recency)."""

import json
from datetime import date
from pathlib import Path

import pytest

from app.config import get_settings
from app.retrieval.precedence import (
    compare_precedence,
    get_allowed_doc_ids,
    is_document_eligible,
    resolve_winners,
    upcoming_outranked_by,
)

TODAY = date(2026, 10, 1)


@pytest.fixture(scope="module")
def meta():
    entries = json.loads(Path(get_settings().manifest_path).read_text(encoding="utf-8"))
    return {e["doc_id"]: e for e in entries}


def test_authority_beats_scope_and_recency(meta):
    # FAQ is newer than V2 but faq/wiki rank below policy.
    result, test = compare_precedence(meta["POL-LEAVE-GLOBAL-V2"], meta["FAQ-LEAVE"])
    assert (result, test) == (-1, "authority")


def test_regional_scope_beats_global(meta):
    result, test = compare_precedence(meta["ADD-LEAVE-DE"], meta["POL-LEAVE-GLOBAL-V2"])
    assert (result, test) == (-1, "scope")


def test_regional_memo_beats_global_policy(meta):
    result, test = compare_precedence(meta["MEMO-HYBRID-2026"], meta["POL-REMOTE-WORK"])
    assert (result, test) == (-1, "scope")


def test_recency_decides_when_authority_and_scope_tie(meta):
    result, test = compare_precedence(meta["POL-LEAVE-GLOBAL-V1"], meta["POL-LEAVE-GLOBAL-V2"])
    assert (result, test) == (1, "recency")


def test_full_tie_is_an_unresolved_conflict(meta):
    # V2 and its summary: same type tier, scope and effective date.
    result, test = compare_precedence(meta["POL-LEAVE-GLOBAL-V2"], meta["POL-LEAVE-GLOBAL-V2-SUMMARY"])
    assert (result, test) == (0, None)
    winners = resolve_winners([meta["POL-LEAVE-GLOBAL-V2"], meta["POL-LEAVE-GLOBAL-V2-SUMMARY"],
                               meta["FAQ-LEAVE"], meta["POL-LEAVE-GLOBAL-V1"]])
    assert {w["doc_id"] for w in winners} == {"POL-LEAVE-GLOBAL-V2", "POL-LEAVE-GLOBAL-V2-SUMMARY"}


def test_eligibility_rules(meta):
    assert is_document_eligible(meta["ADD-LEAVE-DE"], "employee", "DE", TODAY)
    assert not is_document_eligible(meta["ADD-LEAVE-DE"], "employee", "IN", TODAY)  # region
    assert not is_document_eligible(meta["HB-MANAGER"], "employee", "IN", TODAY)  # access
    assert is_document_eligible(meta["HB-MANAGER"], "manager", "IN", TODAY)
    assert not is_document_eligible(meta["HR-COMP-BANDS-2026"], "manager", "IN", TODAY)
    assert is_document_eligible(meta["HR-COMP-BANDS-2026"], "hr", "IN", TODAY)
    assert not is_document_eligible(meta["POL-LEAVE-GLOBAL-V3"], "employee", "IN", TODAY)  # date
    # A question about a past date uses that date as the reference.
    assert not is_document_eligible(meta["POL-LEAVE-GLOBAL-V2"], "employee", "IN", date(2025, 12, 31))
    assert not is_document_eligible(meta["MEMO-HYBRID-2026"], "employee", "IN", date(2026, 6, 30))


def test_upcoming_documents_are_separated(meta):
    eligible, upcoming = get_allowed_doc_ids(meta, "employee", "DE", TODAY)
    assert "POL-LEAVE-GLOBAL-V3" in upcoming and "POL-LEAVE-GLOBAL-V3" not in eligible
    assert "ADD-LEAVE-IN" not in eligible | upcoming


def test_worked_example_de_employee_not_told_about_v3(meta):
    """DE carry-forward is set by ADD-LEAVE-DE, which still beats V3 on scope."""
    assert upcoming_outranked_by(meta["POL-LEAVE-GLOBAL-V3"], [meta["ADD-LEAVE-DE"]]) == ["ADD-LEAVE-DE"]
    # Where only the global V2 states the rule, V3 would win on its date -> mention it.
    assert upcoming_outranked_by(meta["POL-LEAVE-GLOBAL-V3"], [meta["POL-LEAVE-GLOBAL-V2"]]) == []


def test_retrieval_applies_precedence_and_upcoming_rule(client, employees):
    from app.main import state

    retriever = state.chat_handler.retriever
    result = retriever.retrieve_context("How many days can I carry forward?", employees("E4004"))
    doc_ids = [p.doc_id for p in result.passages]
    assert "ADD-LEAVE-DE" in doc_ids
    assert not {"ADD-LEAVE-IN", "ADD-LEAVE-US"} & set(doc_ids)
    # Passages are presented in precedence order: the DE addendum before global docs.
    assert doc_ids.index("ADD-LEAVE-DE") < doc_ids.index("POL-LEAVE-GLOBAL-V2")
    for up in result.upcoming:
        assert up.doc_id == "POL-LEAVE-GLOBAL-V3"
        assert "ADD-LEAVE-DE" in up.outranked_by
    assert result.corpus_tokens <= 3000


def test_past_reference_date_excludes_later_documents(client, employees):
    from app.main import state

    result = state.chat_handler.retriever.retrieve_context(
        "sick leave days per year", employees("E2004"), reference_date=date(2025, 12, 31)
    )
    doc_ids = {p.doc_id for p in result.passages}
    assert "POL-LEAVE-GLOBAL-V1" in doc_ids
    assert not doc_ids & {"POL-LEAVE-GLOBAL-V2", "POL-LEAVE-GLOBAL-V2-SUMMARY", "FAQ-LEAVE"}
