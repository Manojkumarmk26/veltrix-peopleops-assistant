"""Leave data access repository."""

import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Iterable, Optional

from app.data_access.database import get_database

logger = logging.getLogger(__name__)


class LeaveWriteError(Exception):
    """Raised when a leave write is rejected during its final in-transaction check."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass
class HolidayRecord:
    """A holiday extracted from a calendar document."""

    region: str
    holiday_date: str
    holiday_name: str
    year: int
    source_doc_id: str
    doc_effective_date: str
    doc_access_level: str


@dataclass
class LeaveBalance:
    """Leave balance record."""

    employee_id: str
    leave_type: str
    balance_days: float


@dataclass
class LeaveRequest:
    """Leave request record."""

    request_id: str
    employee_id: str
    leave_type: str
    start_date: str
    end_date: str
    working_days: int
    status: str
    created_at: str


class LeaveRepository:
    """Repository for leave balance and request queries."""

    # ── Read Operations ─────────────────────────────────

    def get_balance(
        self, employee_id: str, leave_type: Optional[str] = None
    ) -> list[LeaveBalance]:
        """Get leave balance(s) for an employee."""
        db = get_database()
        if leave_type:
            rows = db.execute(
                "SELECT * FROM leave_balances WHERE employee_id = ? AND leave_type = ?",
                (employee_id, leave_type),
            )
        else:
            rows = db.execute(
                "SELECT * FROM leave_balances WHERE employee_id = ?",
                (employee_id,),
            )
        return [LeaveBalance(**dict(row)) for row in rows]

    def get_requests(
        self, employee_id: str, status: Optional[str] = None
    ) -> list[LeaveRequest]:
        """Get leave requests for an employee, optionally filtered by status."""
        db = get_database()
        if status:
            rows = db.execute(
                "SELECT * FROM leave_requests WHERE employee_id = ? AND status = ? "
                "ORDER BY start_date",
                (employee_id, status),
            )
        else:
            rows = db.execute(
                "SELECT * FROM leave_requests WHERE employee_id = ? "
                "ORDER BY start_date",
                (employee_id,),
            )
        return [LeaveRequest(**dict(row)) for row in rows]

    def get_request_by_id(self, request_id: str) -> Optional[LeaveRequest]:
        """Get a single leave request by ID."""
        db = get_database()
        row = db.execute_one(
            "SELECT * FROM leave_requests WHERE request_id = ?",
            (request_id,),
        )
        if row is None:
            return None
        return LeaveRequest(**dict(row))

    def get_team_leave(
        self, report_ids: list[str], start_date: str, end_date: str
    ) -> list[LeaveRequest]:
        """Get pending and approved leave for direct reports overlapping a date range."""
        if not report_ids:
            return []

        db = get_database()
        placeholders = ",".join("?" * len(report_ids))
        rows = db.execute(
            f"""
            SELECT * FROM leave_requests
            WHERE employee_id IN ({placeholders})
              AND status IN ('pending', 'approved')
              AND start_date <= ?
              AND end_date >= ?
            ORDER BY start_date
            """,
            (*report_ids, end_date, start_date),
        )
        return [LeaveRequest(**dict(row)) for row in rows]

    def has_overlapping_requests(
        self, employee_id: str, start_date: str, end_date: str
    ) -> bool:
        """Check if employee has any pending/approved requests overlapping the date range."""
        db = get_database()
        row = db.execute_one(
            """
            SELECT 1 FROM leave_requests
            WHERE employee_id = ?
              AND status IN ('pending', 'approved')
              AND start_date <= ?
              AND end_date >= ?
            """,
            (employee_id, end_date, start_date),
        )
        return row is not None

    def get_cancellable_requests(self, employee_id: str, today: date) -> list[LeaveRequest]:
        """Pending/approved requests of an employee that start after today."""
        db = get_database()
        rows = db.execute(
            """
            SELECT * FROM leave_requests
            WHERE employee_id = ? AND status IN ('pending', 'approved') AND start_date > ?
            ORDER BY start_date
            """,
            (employee_id, today.isoformat()),
        )
        return [LeaveRequest(**dict(row)) for row in rows]

    # ── Write Operations ────────────────────────────────

    def create_leave_request(
        self,
        employee_id: str,
        leave_type: str,
        start_date: str,
        end_date: str,
        working_days: int,
    ) -> str:
        """Create a new leave request and subtract from balance.

        Executes the final overlap/balance checks, the insert and the balance
        update in a single IMMEDIATE transaction so concurrent requests cannot
        both pass validation. Returns the generated request_id.

        Raises:
            LeaveWriteError: if the request is no longer valid.
        """
        request_id = str(uuid.uuid4())
        created_at = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")

        db = get_database()
        with db.transaction(immediate=True) as conn:
            overlap = conn.execute(
                """
                SELECT 1 FROM leave_requests
                WHERE employee_id = ? AND status IN ('pending', 'approved')
                  AND start_date <= ? AND end_date >= ?
                LIMIT 1
                """,
                (employee_id, end_date, start_date),
            ).fetchone()
            if overlap is not None:
                raise LeaveWriteError(
                    "You already have a pending or approved leave request that "
                    "overlaps with the selected dates."
                )

            row = conn.execute(
                "SELECT balance_days FROM leave_balances WHERE employee_id = ? AND leave_type = ?",
                (employee_id, leave_type),
            ).fetchone()
            if row is None:
                raise LeaveWriteError(f"No {leave_type} leave balance found.")
            if working_days > row["balance_days"]:
                raise LeaveWriteError(
                    f"Insufficient {leave_type} leave balance. Requested {working_days} "
                    f"working days but only {_fmt_days(row['balance_days'])} days are available."
                )

            # Create the request with status 'pending'
            conn.execute(
                """
                INSERT INTO leave_requests
                    (request_id, employee_id, leave_type, start_date, end_date,
                     working_days, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    request_id,
                    employee_id,
                    leave_type,
                    start_date,
                    end_date,
                    working_days,
                    created_at,
                ),
            )

            # Subtract working days from balance
            conn.execute(
                """
                UPDATE leave_balances
                SET balance_days = balance_days - ?
                WHERE employee_id = ? AND leave_type = ?
                """,
                (working_days, employee_id, leave_type),
            )

        logger.info(
            "Leave request created: request_id=%s employee=%s type=%s days=%d",
            request_id,
            employee_id,
            leave_type,
            working_days,
        )
        return request_id

    def cancel_leave_request(
        self, request_id: str, employee_id: str, today: date
    ) -> LeaveRequest:
        """Cancel a leave request and restore the balance.

        Re-checks ownership, status and start date, then sets the status and
        restores the balance, all in a single IMMEDIATE transaction.

        Returns:
            The request as it was before cancellation.

        Raises:
            LeaveWriteError: if the request can no longer be cancelled.
        """
        db = get_database()
        with db.transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT * FROM leave_requests WHERE request_id = ?", (request_id,)
            ).fetchone()
            if row is None or row["employee_id"] != employee_id:
                raise LeaveWriteError(
                    "I couldn't find a leave request with that ID among your requests."
                )
            request = LeaveRequest(**dict(row))
            if request.status not in ("pending", "approved"):
                raise LeaveWriteError(
                    f"This request is already {request.status}. Only pending or "
                    "approved requests can be cancelled."
                )
            if date.fromisoformat(request.start_date) <= today:
                raise LeaveWriteError(
                    f"This request starts on {request.start_date}, which is not after "
                    f"today ({today.isoformat()}). Only requests that start in the "
                    "future can be cancelled."
                )

            # Set status to cancelled
            conn.execute(
                """
                UPDATE leave_requests
                SET status = 'cancelled'
                WHERE request_id = ?
                """,
                (request_id,),
            )

            # Add working days back to balance
            conn.execute(
                """
                UPDATE leave_balances
                SET balance_days = balance_days + ?
                WHERE employee_id = ? AND leave_type = ?
                """,
                (request.working_days, request.employee_id, request.leave_type),
            )

        logger.info(
            "Leave request cancelled: request_id=%s employee=%s days=%d restored",
            request_id,
            request.employee_id,
            request.working_days,
        )
        return request

    # ── Holiday Operations ──────────────────────────────

    def replace_holidays(self, records: Iterable[HolidayRecord]) -> int:
        """Replace all extracted holidays with a fresh set (one transaction).

        Called at ingestion so the table always mirrors the current corpus.
        """
        rows = [
            (
                r.region,
                r.holiday_date,
                r.holiday_name,
                r.year,
                r.source_doc_id,
                r.doc_effective_date,
                r.doc_access_level,
            )
            for r in records
        ]
        db = get_database()
        with db.transaction() as conn:
            conn.execute("DELETE FROM holidays")
            conn.executemany(
                """
                INSERT OR IGNORE INTO holidays
                    (region, holiday_date, holiday_name, year, source_doc_id,
                     doc_effective_date, doc_access_level)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        return len(rows)

    def get_holiday_records(self, regions: list[str], year: int) -> list[HolidayRecord]:
        """Get every extracted holiday for the given regions and year."""
        if not regions:
            return []
        db = get_database()
        placeholders = ",".join("?" * len(regions))
        rows = db.execute(
            f"""
            SELECT region, holiday_date, holiday_name, year, source_doc_id,
                   doc_effective_date, doc_access_level
            FROM holidays
            WHERE region IN ({placeholders}) AND year = ?
            ORDER BY holiday_date
            """,
            (*regions, year),
        )
        return [HolidayRecord(**dict(row)) for row in rows]

    def count_holidays(self) -> int:
        """Number of extracted holiday rows (used for readiness checks)."""
        row = get_database().execute_one("SELECT COUNT(*) AS n FROM holidays")
        return int(row["n"]) if row else 0


def _fmt_days(value: float) -> str:
    """Format a day count without a trailing .0 for whole numbers."""
    return str(int(value)) if float(value).is_integer() else f"{value:g}"
