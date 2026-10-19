"""Employee data access repository."""

import logging
import re
from dataclasses import dataclass
from typing import Optional

from app.data_access.database import get_database

logger = logging.getLogger(__name__)


@dataclass
class Employee:
    """Employee record from the database."""

    employee_id: str
    full_name: str
    email: str
    phone: str
    region: str
    join_date: str
    level: str
    role: str
    manager_id: Optional[str]

    @property
    def masked_email(self) -> str:
        """Email with first char + *** + domain. e.g. r***@veltrix.example"""
        local, domain = self.email.split("@", 1)
        return f"{local[0]}***@{domain}"

    @property
    def masked_phone(self) -> str:
        """Phone numbers are NEVER shown."""
        return "[REDACTED]"


class EmployeeRepository:
    """Repository for employee queries."""

    def get_by_id(self, employee_id: str) -> Optional[Employee]:
        """Get an employee by their ID."""
        db = get_database()
        row = db.execute_one(
            "SELECT * FROM employees WHERE employee_id = ?",
            (employee_id,),
        )
        if row is None:
            return None
        return Employee(**dict(row))

    def get_direct_reports(self, manager_id: str) -> list[Employee]:
        """Get all direct reports of a manager."""
        db = get_database()
        rows = db.execute(
            "SELECT * FROM employees WHERE manager_id = ?",
            (manager_id,),
        )
        return [Employee(**dict(row)) for row in rows]

    def get_direct_report_ids(self, manager_id: str) -> list[str]:
        """Get employee IDs of direct reports."""
        db = get_database()
        rows = db.execute(
            "SELECT employee_id FROM employees WHERE manager_id = ?",
            (manager_id,),
        )
        return [row["employee_id"] for row in rows]

    def exists(self, employee_id: str) -> bool:
        """Check if an employee exists."""
        db = get_database()
        row = db.execute_one(
            "SELECT 1 FROM employees WHERE employee_id = ?",
            (employee_id,),
        )
        return row is not None

    def find_by_name(self, name: str) -> list[Employee]:
        """Employees whose full name equals ``name`` or starts/ends with it as a word.

        Callers MUST filter the result through the permission checks before
        revealing anything (names are personal data, Section 7.4).
        """
        needle = " ".join((name or "").split()).lower()
        if not needle:
            return []
        db = get_database()
        rows = db.execute(
            """
            SELECT * FROM employees
            WHERE lower(full_name) = ? OR lower(full_name) LIKE ? OR lower(full_name) LIKE ?
            ORDER BY employee_id
            """,
            (needle, f"{needle} %", f"% {needle}"),
        )
        return [Employee(**dict(row)) for row in rows]

    def get_safe_name(self, employee_id: str, requester: Employee) -> Optional[str]:
        """Get employee name only if requester is allowed to see it.

        Rules:
        - Employee can see their own name
        - Manager can see their direct reports' names
        - HR can see any employee's name (one at a time)
        """
        target = self.get_by_id(employee_id)
        if target is None:
            return None

        if employee_id == requester.employee_id:
            return target.full_name

        if requester.role == "manager":
            report_ids = self.get_direct_report_ids(requester.employee_id)
            if employee_id in report_ids:
                return target.full_name

        if requester.role == "hr":
            return target.full_name

        return None
