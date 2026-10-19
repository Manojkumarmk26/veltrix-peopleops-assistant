"""Working-day calculation and holiday extraction (Section 6.2)."""

from datetime import date

import pytest

from app.data_access.leave_repo import HolidayRecord, LeaveRepository
from app.tools.working_days import count_working_days, select_applicable_calendars


@pytest.mark.parametrize(
    "region,start,end,expected",
    [
        # Ground truth from the provided data (stored working_days of real requests)
        ("IN", "2026-11-09", "2026-11-13", 4),
        ("US", "2026-12-21", "2026-12-31", 7),
        ("DE", "2026-12-21", "2026-12-31", 6),
        ("IN", "2026-12-21", "2026-12-31", 8),
    ],
)
def test_working_days_match_ground_truth(client, region, start, end, expected):
    assert count_working_days(date.fromisoformat(start), date.fromisoformat(end), region) == expected


def test_stored_working_days_of_all_requests_match(client):
    """Every request in the provided DB agrees with our calendar logic."""
    from app.data_access.database import get_database

    rows = get_database().execute(
        "SELECT r.start_date, r.end_date, r.working_days, e.region FROM leave_requests r "
        "JOIN employees e ON e.employee_id = r.employee_id"
    )
    assert rows
    for r in rows:
        got = count_working_days(
            date.fromisoformat(r["start_date"]), date.fromisoformat(r["end_date"]), r["region"]
        )
        assert got == r["working_days"], dict(r)


def test_weekend_only_range_has_zero_working_days(client):
    assert count_working_days(date(2026, 10, 3), date(2026, 10, 4), "IN") == 0


def test_explicit_holiday_list_is_respected():
    # Mon 2026-10-05 .. Fri 2026-10-09 with Wednesday as a holiday -> 4
    assert count_working_days(date(2026, 10, 5), date(2026, 10, 9), "IN", holidays=["2026-10-07"]) == 4


def test_end_before_start_is_zero():
    assert count_working_days(date(2026, 10, 9), date(2026, 10, 5), "IN", holidays=[]) == 0


def test_indian_restricted_holidays_are_not_closures(client):
    repo = LeaveRepository()
    records = repo.get_holiday_records(["IN"], 2026)
    in_2026 = [r for r in records if r.source_doc_id == "CAL-HOL-IN-2026"]
    assert len(in_2026) == 12  # the 12 public holidays; restricted list excluded


@pytest.mark.parametrize("region,doc_id,count", [("US", "CAL-HOL-US-2026", 10),
                                                 ("DE", "CAL-HOL-DE-2026", 12)])
def test_calendar_extraction_counts(client, region, doc_id, count):
    records = LeaveRepository().get_holiday_records([region], 2026)
    assert len([r for r in records if r.source_doc_id == doc_id]) == count


def _rec(doc_id, effective, region="IN", access="all"):
    return HolidayRecord(region, "2026-01-26", "x", 2026, doc_id, effective, access)


def test_calendar_selection_uses_precedence_and_effective_date():
    records = [_rec("CAL-2025", "2025-01-01"), _rec("CAL-2026", "2026-01-01")]
    assert select_applicable_calendars(records, "employee", date(2026, 6, 1)) == ["CAL-2026"]
    # Before the 2026 calendar takes effect, only the 2025 one is eligible.
    assert select_applicable_calendars(records, "employee", date(2025, 12, 1)) == ["CAL-2025"]


def test_calendar_selection_respects_access_level():
    records = [_rec("CAL-PUBLIC", "2026-01-01"), _rec("CAL-HR", "2026-02-01", access="hr")]
    assert select_applicable_calendars(records, "employee", date(2026, 6, 1)) == ["CAL-PUBLIC"]
    assert select_applicable_calendars(records, "hr", date(2026, 6, 1)) == ["CAL-HR"]
