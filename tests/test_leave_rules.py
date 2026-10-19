"""apply_leave / cancel_leave business rules (Section 6.2) — tools called directly."""

import uuid
from datetime import date

import pytest

from app.data_access.database import get_database
from app.data_access.leave_repo import LeaveRepository
from app.tools.leave_tools import REQUEST_NOT_FOUND_MESSAGE, apply_leave, cancel_leave
from helpers import balance

pytestmark = pytest.mark.usefixtures("restore_leave_data")


def _error(result, fragment):
    assert result.status == "error", result
    assert fragment.lower() in result.error_message.lower(), result.error_message


# ── apply_leave validation ──────────────────────────────────────────────────

def test_invalid_leave_type(employees):
    _error(apply_leave(employees("E2004"), "casual", "2026-10-05", "2026-10-05"), "invalid leave type")


def test_dates_must_be_in_2026(employees):
    _error(apply_leave(employees("E2004"), "sick", "2027-01-05", "2027-01-06"), "2026")


def test_start_cannot_be_in_the_past(employees):
    _error(apply_leave(employees("E2004"), "sick", "2026-09-01", "2026-09-02"), "in the past")


def test_start_today_is_allowed(employees):
    assert apply_leave(employees("E2004"), "sick", "2026-10-01", "2026-10-01").status == \
        "pending_confirmation"


def test_end_before_start(employees):
    _error(apply_leave(employees("E2004"), "sick", "2026-10-08", "2026-10-06"), "before the start")


def test_zero_working_days_weekend(employees):
    _error(apply_leave(employees("E2004"), "sick", "2026-10-10", "2026-10-11"), "zero working days")


def test_zero_working_days_public_holiday(employees):
    rows = get_database().execute(
        "SELECT holiday_date FROM holidays WHERE region='IN' AND source_doc_id='CAL-HOL-IN-2026' "
        "AND holiday_date > '2026-10-01' ORDER BY holiday_date"
    )
    weekday = next(r["holiday_date"] for r in rows if date.fromisoformat(r["holiday_date"]).weekday() < 5)
    _error(apply_leave(employees("E2004"), "sick", weekday, weekday), "zero working days")


def test_insufficient_balance(employees):
    # E2004 has 3.5 annual days; Mon-Fri next week is 5 working days.
    _error(apply_leave(employees("E2004"), "annual", "2026-10-05", "2026-10-09"), "insufficient")


def test_overlap_with_pending_request(employees):
    # E2004 already has pending annual leave 2026-11-09..13.
    _error(apply_leave(employees("E2004"), "sick", "2026-11-12", "2026-11-12"), "overlaps")


# ── apply_leave two-phase write ─────────────────────────────────────────────

def test_proposal_does_not_write_and_confirmation_does(employees):
    me = employees("E2004")
    before_sick = balance("E2004", "sick")
    before_count = len(LeaveRepository().get_requests("E2004"))

    proposal = apply_leave(me, "sick", "2026-10-05", "2026-10-07")
    assert proposal.status == "pending_confirmation"
    assert proposal.data["working_days"] == 3
    assert balance("E2004", "sick") == before_sick
    assert len(LeaveRepository().get_requests("E2004")) == before_count

    done = apply_leave(me, "sick", "2026-10-05", "2026-10-07", confirmed=True)
    assert done.status == "success"
    request_id = done.data["request_id"]
    assert uuid.UUID(request_id).version == 4
    stored = LeaveRepository().get_request_by_id(request_id)
    assert (stored.status, stored.working_days, stored.employee_id) == ("pending", 3, "E2004")
    assert balance("E2004", "sick") == before_sick - 3


def test_execution_rechecks_overlap(employees):
    me = employees("E2004")
    assert apply_leave(me, "sick", "2026-10-05", "2026-10-05", confirmed=True).status == "success"
    _error(apply_leave(me, "sick", "2026-10-05", "2026-10-05", confirmed=True), "overlaps")


# ── cancel_leave ────────────────────────────────────────────────────────────

def test_cancel_restores_balance(employees):
    me = employees("E2004")
    created = apply_leave(me, "sick", "2026-10-12", "2026-10-13", confirmed=True)
    after_apply = balance("E2004", "sick")

    proposal = cancel_leave(me, created.data["request_id"])
    assert proposal.status == "pending_confirmation"
    assert balance("E2004", "sick") == after_apply  # nothing changed yet

    done = cancel_leave(me, created.data["request_id"], confirmed=True)
    assert done.status == "success"
    assert LeaveRepository().get_request_by_id(created.data["request_id"]).status == "cancelled"
    assert balance("E2004", "sick") == after_apply + 2


def test_cannot_cancel_someone_elses_request(employees):
    other = LeaveRepository().get_cancellable_requests("E2005", date(2026, 10, 1))[0]
    _error(cancel_leave(employees("E2004"), other.request_id), REQUEST_NOT_FOUND_MESSAGE)
    # A manager cannot cancel on behalf of a report either (writes are self-only).
    _error(cancel_leave(employees("E2002"), other.request_id), REQUEST_NOT_FOUND_MESSAGE)


def test_unknown_request_id_gets_same_message(employees):
    _error(cancel_leave(employees("E2004"), str(uuid.uuid4())), REQUEST_NOT_FOUND_MESSAGE)


def test_cannot_cancel_already_cancelled(employees):
    row = get_database().execute_one(
        "SELECT request_id, employee_id FROM leave_requests WHERE status='cancelled' LIMIT 1")
    _error(cancel_leave(employees(row["employee_id"]), row["request_id"]), "already cancelled")


def test_cannot_cancel_request_that_started(employees):
    row = get_database().execute_one(
        "SELECT request_id, employee_id FROM leave_requests "
        "WHERE status IN ('pending','approved') AND start_date <= '2026-10-01' LIMIT 1")
    _error(cancel_leave(employees(row["employee_id"]), row["request_id"]), "not after today")


def test_cannot_cancel_request_starting_today(employees):
    me = employees("E2004")
    created = apply_leave(me, "sick", "2026-10-01", "2026-10-01", confirmed=True)
    _error(cancel_leave(me, created.data["request_id"]), "not after today")
