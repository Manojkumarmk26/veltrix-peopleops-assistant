"""Access control per role, for documents and leave data (Sections 3.1, 7.2-7.4)."""

import pytest

from app.tools.leave_tools import (
    get_leave_balance,
    get_team_leave,
    list_leave_requests,
)
from app.tools.permissions import ACCESS_DENIED_MESSAGE, PermissionError, check_leave_data_access

PROBE_QUERIES = [
    "What are the salary bands for each level?",
    "How should a performance improvement plan be run?",
    "What should managers do when approving leave?",
    "compensation bands base salary",
    "PIP guidelines for HR",
]
LEVEL = {"employee": {"all"}, "manager": {"all", "managers"}, "hr": {"all", "managers", "hr"}}


@pytest.mark.parametrize("employee_id,role", [("E2004", "employee"), ("E2002", "manager"),
                                              ("E1001", "hr")])
def test_documents_filtered_by_role_before_llm(client, employees, employee_id, role):
    from app.main import state

    requester = employees(employee_id)
    assert requester.role == role
    meta = state.chat_handler.retriever.vector_store.doc_metadata
    for query in PROBE_QUERIES:
        result = state.chat_handler.retriever.retrieve_context(query, requester)
        for p in result.passages + result.upcoming:
            assert meta[p.doc_id]["access_level"] in LEVEL[role], (query, p.doc_id)


def test_hr_can_retrieve_hr_documents(client, employees):
    from app.main import state

    result = state.chat_handler.retriever.retrieve_context(
        "What are the salary bands?", employees("E1001"))
    assert "HR-COMP-BANDS-2026" in {p.doc_id for p in result.passages}


def test_manager_can_retrieve_manager_handbook(client, employees):
    from app.main import state

    result = state.chat_handler.retriever.retrieve_context(
        "manager handbook approving leave for my team", employees("E2002"))
    assert "HB-MANAGER" in {p.doc_id for p in result.passages}


# ── Leave data ──────────────────────────────────────────────────────────────

def test_employee_sees_only_own_leave(client, employees):
    me = employees("E2004")
    assert get_leave_balance(me, "E2004").status == "success"
    for other in ("E2005", "E2002", "E9999"):
        result = get_leave_balance(me, other)
        assert result.status == "error" and result.error_message == ACCESS_DENIED_MESSAGE
        result = list_leave_requests(me, other)
        assert result.status == "error" and result.error_message == ACCESS_DENIED_MESSAGE


def test_manager_sees_direct_reports_only(client, employees):
    manager = employees("E2002")
    from app.data_access.employee_repo import EmployeeRepository

    reports = EmployeeRepository().get_direct_report_ids("E2002")
    assert reports
    for rid in reports:
        assert get_leave_balance(manager, rid).status == "success"
    outsider = next(e for e in ("E3005", "E4004", "E2004") if e not in reports)
    assert get_leave_balance(manager, outsider).error_message == ACCESS_DENIED_MESSAGE


def test_hr_sees_any_single_employee(client, employees):
    hr = employees("E1001")
    for target in ("E2004", "E3005", "E4004"):
        assert get_leave_balance(hr, target).status == "success"
    # Unknown employee: same fixed message, no hint about existence.
    assert get_leave_balance(hr, "E9999").error_message == ACCESS_DENIED_MESSAGE


def test_same_message_whether_or_not_employee_exists(client, employees):
    me = employees("E2004")
    with pytest.raises(PermissionError) as exists:
        check_leave_data_access(me, "E2005")
    with pytest.raises(PermissionError) as missing:
        check_leave_data_access(me, "E9999")
    assert exists.value.message == missing.value.message == ACCESS_DENIED_MESSAGE


def test_team_leave_is_manager_only(client, employees):
    assert get_team_leave(employees("E2004"), "2026-12-01", "2026-12-31").status == "error"
    assert get_team_leave(employees("E1001"), "2026-12-01", "2026-12-31").status == "error"
    result = get_team_leave(employees("E2002"), "2026-12-01", "2026-12-31")
    assert result.status == "success"
    from app.data_access.employee_repo import EmployeeRepository

    reports = set(EmployeeRepository().get_direct_report_ids("E2002"))
    assert all(r["employee_id"] in reports for r in result.data["team_leave"])
    # Names of direct reports are visible to their manager; no emails/phones.
    for r in result.data["team_leave"]:
        assert "email" not in r and "phone" not in r
