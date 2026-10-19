"""Permission checks for tool access control.

Permission checks run INSIDE the tool code using the employee_id
from the request header. They MUST NOT depend on the prompt or
on arguments chosen by the LLM (Section 6.1).
"""

import logging
from typing import Optional

from app.data_access.employee_repo import Employee, EmployeeRepository

logger = logging.getLogger(__name__)

# Fixed refusal message — same whether employee exists or not (Section 7.3)
ACCESS_DENIED_MESSAGE = "I can't share information about that employee."


class PermissionError(Exception):
    """Raised when a permission check fails."""

    def __init__(self, message: str = ACCESS_DENIED_MESSAGE):
        super().__init__(message)
        self.message = message


def check_leave_data_access(
    requester: Employee,
    target_employee_id: str,
) -> None:
    """Check if the requester can view the target employee's leave data.

    Rules (Section 3.1 & 7.3):
    - employee: can only see their own data
    - manager: can see their own + direct reports' data
    - hr: can see any ONE employee at a time

    Raises PermissionError with a fixed message if denied.
    """
    # Self-access is always allowed
    if requester.employee_id == target_employee_id:
        return

    if requester.role == "employee":
        raise PermissionError()

    if requester.role == "manager":
        repo = EmployeeRepository()
        report_ids = repo.get_direct_report_ids(requester.employee_id)
        if target_employee_id not in report_ids:
            raise PermissionError()
        return

    if requester.role == "hr":
        # HR can view any ONE employee at a time — individual access is fine
        return

    raise PermissionError()


def check_team_leave_access(requester: Employee) -> list[str]:
    """Check if the requester can use get_team_leave.

    Only managers can use this tool.

    Returns:
        List of direct report employee_ids

    Raises PermissionError if not a manager.
    """
    if requester.role != "manager":
        raise PermissionError(
            "Only managers can view team leave."
        )

    repo = EmployeeRepository()
    report_ids = repo.get_direct_report_ids(requester.employee_id)

    if not report_ids:
        raise PermissionError(
            "You don't have any direct reports."
        )

    return report_ids


def check_self_only(requester: Employee, target_employee_id: str) -> None:
    """Check that the action is for the requester themselves only.

    Used for apply_leave and cancel_leave (Section 6.1).
    """
    if requester.employee_id != target_employee_id:
        raise PermissionError(
            "You can only apply for or cancel your own leave."
        )
