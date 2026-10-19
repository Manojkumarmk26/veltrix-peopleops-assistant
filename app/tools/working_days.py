"""Working day calculator — excludes weekends and regional public holidays.

Holidays come from the holiday calendar document that applies to the requester
under Section 5 (role can read it, region matches, effective on the leave
dates; precedence picks the winner). The dates themselves are extracted
automatically at ingestion into the ``holidays`` table.
"""

import logging
from datetime import date, timedelta
from typing import Iterable, Optional

from app.data_access.leave_repo import HolidayRecord, LeaveRepository
from app.retrieval.precedence import get_accessible_levels, resolve_winners

logger = logging.getLogger(__name__)


def select_applicable_calendars(
    records: Iterable[HolidayRecord],
    role: str,
    reference_date: date,
) -> list[str]:
    """Pick the calendar document(s) that apply on ``reference_date``.

    Returns the winning source_doc_id(s). Several IDs are returned only if
    precedence cannot decide between them (their holidays are then combined).
    """
    accessible = get_accessible_levels(role)
    docs: dict[str, dict] = {}
    for r in records:
        if r.source_doc_id in docs:
            continue
        docs[r.source_doc_id] = {
            "doc_id": r.source_doc_id,
            "doc_type": "calendar",
            "regions": ["GLOBAL"] if r.region == "GLOBAL" else [r.region],
            "effective_date": r.doc_effective_date,
            "access_level": r.doc_access_level,
        }

    eligible = [
        m
        for m in docs.values()
        if m["access_level"] in accessible
        and date.fromisoformat(m["effective_date"]) <= reference_date
    ]
    winners = resolve_winners(eligible)
    if len(winners) > 1:
        logger.warning(
            "Calendar precedence tie between %s; combining their holidays",
            [w["doc_id"] for w in winners],
        )
    return [w["doc_id"] for w in winners]


def get_applicable_holidays(
    region: str,
    start_date: date,
    end_date: date,
    role: str = "employee",
) -> set[date]:
    """Holidays for the requester's region from the applicable calendar(s)."""
    repo = LeaveRepository()
    holidays: set[date] = set()
    for year in range(start_date.year, end_date.year + 1):
        records = repo.get_holiday_records([region, "GLOBAL"], year)
        if not records:
            logger.warning("No holiday calendar found for region=%s year=%d", region, year)
            continue
        # A calendar applies to the leave dates it covers: use the last leave
        # date in this year as the reference date for eligibility.
        reference = min(end_date, date(year, 12, 31))
        winners = set(select_applicable_calendars(records, role, reference))
        for r in records:
            if r.source_doc_id in winners:
                try:
                    holidays.add(date.fromisoformat(r.holiday_date))
                except ValueError:
                    logger.warning("Invalid holiday date in DB: %s", r.holiday_date)
    return holidays


def count_working_days(
    start_date: date,
    end_date: date,
    region: str,
    holidays: Optional[list[str]] = None,
    role: str = "employee",
) -> int:
    """Count working days in a date range, excluding weekends and holidays.

    Working days = Monday-Friday minus public holidays for the region.

    Args:
        start_date: Inclusive start date
        end_date: Inclusive end date
        region: Employee's region (IN, US, DE)
        holidays: Optional list of holiday date strings (YYYY-MM-DD).
                  If None, fetched from the applicable calendar in the database.
        role: Requester's role (decides which calendars they may read)

    Returns:
        Number of working days
    """
    return len(get_working_days_list(start_date, end_date, region, holidays, role))


def get_working_days_list(
    start_date: date,
    end_date: date,
    region: str,
    holidays: Optional[list[str]] = None,
    role: str = "employee",
) -> list[date]:
    """Get a list of working day dates in a range.

    Args:
        start_date: Inclusive start date
        end_date: Inclusive end date
        region: Employee's region
        holidays: Optional list of holiday date strings
        role: Requester's role

    Returns:
        List of working day date objects
    """
    if start_date > end_date:
        return []

    if holidays is None:
        holiday_set = get_applicable_holidays(region, start_date, end_date, role)
    else:
        holiday_set = set()
        for h in holidays:
            try:
                holiday_set.add(date.fromisoformat(h))
            except ValueError:
                logger.warning("Invalid holiday date: %s", h)

    working_days = []
    current = start_date
    while current <= end_date:
        # Monday=0, ..., Friday=4, Saturday=5, Sunday=6
        if current.weekday() < 5 and current not in holiday_set:
            working_days.append(current)
        current += timedelta(days=1)

    return working_days
