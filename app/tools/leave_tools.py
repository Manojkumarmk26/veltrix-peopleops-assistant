"""Leave management tools — the 5 required tools for the Veltrix PeopleOps Assistant.

Tool names and argument names match exactly what the automated tests expect
(Section 6.1 of TASK.pdf).

Every permission check runs INSIDE these functions using the requester
resolved from the ``X-Employee-Id`` header — never from the prompt or from
arguments a model might choose. Write tools (``apply_leave``,
``cancel_leave``) always act on the requester only and are two-phase:
``confirmed=False`` validates and proposes, ``confirmed=True`` executes.
"""

import logging
from dataclasses import dataclass
from datetime import date
from typing import Any, Optional

from app.config import get_settings
from app.data_access.employee_repo import Employee, EmployeeRepository
from app.data_access.leave_repo import LeaveRepository, LeaveWriteError, _fmt_days
from app.tools.permissions import (
    ACCESS_DENIED_MESSAGE,
    PermissionError,
    check_leave_data_access,
    check_self_only,
    check_team_leave_access,
)
from app.tools.working_days import count_working_days

logger = logging.getLogger(__name__)

VALID_LEAVE_TYPES = ("annual", "sick")
VALID_STATUSES = ("pending", "approved", "cancelled", "rejected")

# Same message whether the request does not exist or belongs to someone else,
# so request IDs of other employees cannot be probed.
REQUEST_NOT_FOUND_MESSAGE = "I couldn't find a leave request with that ID among your requests."


@dataclass
class ToolResult:
    """Result from executing a tool."""

    name: str
    args: dict
    status: str  # "success" | "error" | "pending_confirmation"
    data: Any = None
    error_message: Optional[str] = None


def _error(name: str, args: dict, message: str) -> ToolResult:
    return ToolResult(name=name, args=args, status="error", error_message=message)


def _normalise_employee_id(employee_id: Optional[str]) -> str:
    return (employee_id or "").strip().upper()


def _request_to_dict(r) -> dict:
    return {
        "request_id": r.request_id,
        "employee_id": r.employee_id,
        "leave_type": r.leave_type,
        "start_date": r.start_date,
        "end_date": r.end_date,
        "working_days": r.working_days,
        "status": r.status,
        "created_at": r.created_at,
    }


# ──────────────────────────────────────────────────────────
# Tool 1: get_leave_balance
# ──────────────────────────────────────────────────────────

def get_leave_balance(
    requester: Employee,
    employee_id: str,
    leave_type: Optional[str] = None,
) -> ToolResult:
    """Get the remaining leave balance of an employee.

    Who may use it: The employee themselves, their manager, or HR.
    """
    employee_id = _normalise_employee_id(employee_id)
    args = {"employee_id": employee_id}
    if leave_type:
        leave_type = leave_type.strip().lower()
        args["leave_type"] = leave_type

    try:
        check_leave_data_access(requester, employee_id)
    except PermissionError as e:
        return _error("get_leave_balance", args, e.message)

    if not EmployeeRepository().exists(employee_id):
        # Same fixed message as a permission failure (Section 7.3).
        return _error("get_leave_balance", args, ACCESS_DENIED_MESSAGE)

    if leave_type and leave_type not in VALID_LEAVE_TYPES:
        return _error(
            "get_leave_balance", args,
            f"Unknown leave type '{leave_type}'. Valid types are: annual, sick.",
        )

    balances = LeaveRepository().get_balance(employee_id, leave_type)
    data = {
        "balances": [
            {
                "employee_id": b.employee_id,
                "leave_type": b.leave_type,
                "balance_days": b.balance_days,
            }
            for b in balances
        ]
    }
    return ToolResult(name="get_leave_balance", args=args, status="success", data=data)


# ──────────────────────────────────────────────────────────
# Tool 2: list_leave_requests
# ──────────────────────────────────────────────────────────

def list_leave_requests(
    requester: Employee,
    employee_id: str,
    status: Optional[str] = None,
) -> ToolResult:
    """List leave requests for an employee.

    Who may use it: The employee themselves, their manager, or HR.
    """
    employee_id = _normalise_employee_id(employee_id)
    args = {"employee_id": employee_id}
    if status:
        status = status.strip().lower()
        args["status"] = status

    try:
        check_leave_data_access(requester, employee_id)
    except PermissionError as e:
        return _error("list_leave_requests", args, e.message)

    if not EmployeeRepository().exists(employee_id):
        return _error("list_leave_requests", args, ACCESS_DENIED_MESSAGE)

    if status and status not in VALID_STATUSES:
        return _error(
            "list_leave_requests", args,
            f"Unknown status '{status}'. Valid statuses are: pending, approved, cancelled.",
        )

    requests = LeaveRepository().get_requests(employee_id, status)
    data = {"requests": [_request_to_dict(r) for r in requests]}
    return ToolResult(name="list_leave_requests", args=args, status="success", data=data)


# ──────────────────────────────────────────────────────────
# Tool 3: get_team_leave
# ──────────────────────────────────────────────────────────

def get_team_leave(
    requester: Employee,
    start_date: str,
    end_date: str,
) -> ToolResult:
    """Get pending and approved leave for the requester's direct reports.

    Who may use it: Managers only.
    """
    args = {"start_date": start_date, "end_date": end_date}

    try:
        report_ids = check_team_leave_access(requester)
    except PermissionError as e:
        return _error("get_team_leave", args, e.message)

    try:
        start_dt = date.fromisoformat(start_date)
        end_dt = date.fromisoformat(end_date)
    except (TypeError, ValueError):
        return _error("get_team_leave", args, "Invalid date format. Use YYYY-MM-DD.")
    if end_dt < start_dt:
        return _error("get_team_leave", args, "End date must be on or after start date.")

    requests = LeaveRepository().get_team_leave(report_ids, start_date, end_date)

    # Managers may see their direct reports' names (Section 7.4).
    emp_repo = EmployeeRepository()
    name_map: dict[str, Optional[str]] = {}
    for r in requests:
        if r.employee_id not in name_map:
            name_map[r.employee_id] = emp_repo.get_safe_name(r.employee_id, requester)

    data = {
        "team_size": len(report_ids),
        "team_leave": [
            {
                "request_id": r.request_id,
                "employee_id": r.employee_id,
                "employee_name": name_map.get(r.employee_id) or r.employee_id,
                "leave_type": r.leave_type,
                "start_date": r.start_date,
                "end_date": r.end_date,
                "working_days": r.working_days,
                "status": r.status,
            }
            for r in requests
        ],
    }
    return ToolResult(name="get_team_leave", args=args, status="success", data=data)


# ──────────────────────────────────────────────────────────
# Tool 4: apply_leave
# ──────────────────────────────────────────────────────────

def validate_leave_application(
    requester: Employee,
    leave_type: Optional[str],
    start_date: Optional[str],
    end_date: Optional[str],
) -> tuple[Optional[str], dict]:
    """Run every apply_leave rule (Section 6.2).

    Returns:
        (error_message, details). ``error_message`` is None when valid;
        ``details`` then holds working_days, balance and balance_after.
    """
    today = get_settings().today

    if not leave_type:
        return "Please tell me the leave type (annual or sick).", {}
    if leave_type not in VALID_LEAVE_TYPES:
        return f"Invalid leave type '{leave_type}'. Must be 'annual' or 'sick'.", {}

    try:
        start_dt = date.fromisoformat(start_date or "")
        end_dt = date.fromisoformat(end_date or "")
    except ValueError:
        return "Invalid date format. Use YYYY-MM-DD.", {}

    if start_dt.year != 2026 or end_dt.year != 2026:
        return "Leave dates must be within the year 2026.", {}

    if start_dt < today:
        return (
            f"The start date {start_dt.isoformat()} is in the past. Leave must start "
            f"on or after today ({today.isoformat()}).",
            {},
        )

    if end_dt < start_dt:
        return (
            f"The end date {end_dt.isoformat()} is before the start date "
            f"{start_dt.isoformat()}.",
            {},
        )

    working_days = count_working_days(start_dt, end_dt, requester.region, role=requester.role)
    if working_days == 0:
        return (
            f"{start_dt.isoformat()} to {end_dt.isoformat()} has zero working days "
            "(only weekends or public holidays), so there is nothing to apply for.",
            {},
        )

    leave_repo = LeaveRepository()
    balances = leave_repo.get_balance(requester.employee_id, leave_type)
    if not balances:
        return f"You don't have a {leave_type} leave balance on record.", {}
    balance = balances[0].balance_days
    if working_days > balance:
        return (
            f"Insufficient {leave_type} leave balance: this request needs {working_days} "
            f"working days but you have {_fmt_days(balance)} days available.",
            {},
        )

    if leave_repo.has_overlapping_requests(requester.employee_id, start_date, end_date):
        return (
            "You already have a pending or approved leave request that overlaps "
            f"{start_dt.isoformat()} to {end_dt.isoformat()}.",
            {},
        )

    return None, {
        "working_days": working_days,
        "balance": balance,
        "balance_after": balance - working_days,
    }


def apply_leave(
    requester: Employee,
    leave_type: str,
    start_date: str,
    end_date: str,
    confirmed: bool = False,
) -> ToolResult:
    """Apply for leave.

    Who may use it: The requester, for themselves only.
    Two-step: first proposes (pending_confirmation), then executes on confirm.
    """
    leave_type = (leave_type or "").strip().lower()
    args = {
        "leave_type": leave_type,
        "start_date": start_date,
        "end_date": end_date,
    }

    error, details = validate_leave_application(requester, leave_type, start_date, end_date)
    if error:
        return _error("apply_leave", args, error)

    working_days = details["working_days"]
    if not confirmed:
        return ToolResult(
            name="apply_leave",
            args=args,
            status="pending_confirmation",
            data={
                "leave_type": leave_type,
                "start_date": start_date,
                "end_date": end_date,
                "working_days": working_days,
                "balance": details["balance"],
                "balance_after": details["balance_after"],
            },
        )

    # Execute: the repository re-checks overlap and balance inside one
    # IMMEDIATE transaction, then inserts and subtracts atomically.
    try:
        check_self_only(requester, requester.employee_id)
        request_id = LeaveRepository().create_leave_request(
            employee_id=requester.employee_id,
            leave_type=leave_type,
            start_date=start_date,
            end_date=end_date,
            working_days=working_days,
        )
    except (LeaveWriteError, PermissionError) as e:
        return _error("apply_leave", args, e.message)

    return ToolResult(
        name="apply_leave",
        args=args,
        status="success",
        data={
            "request_id": request_id,
            "leave_type": leave_type,
            "start_date": start_date,
            "end_date": end_date,
            "working_days": working_days,
            "status": "pending",
            "balance_remaining": details["balance_after"],
        },
    )


# ──────────────────────────────────────────────────────────
# Tool 5: cancel_leave
# ──────────────────────────────────────────────────────────

def cancel_leave(
    requester: Employee,
    request_id: str,
    confirmed: bool = False,
) -> ToolResult:
    """Cancel one of the requester's own leave requests.

    Who may use it: The requester, for themselves only.
    Two-step: first proposes, then executes on confirm.
    """
    request_id = (request_id or "").strip().lower()
    args = {"request_id": request_id}
    today = get_settings().today

    leave_repo = LeaveRepository()
    request = leave_repo.get_request_by_id(request_id) if request_id else None
    if request is None or request.employee_id != requester.employee_id:
        return _error("cancel_leave", args, REQUEST_NOT_FOUND_MESSAGE)

    if request.status not in ("pending", "approved"):
        return _error(
            "cancel_leave", args,
            f"This request is already {request.status}. Only pending or approved "
            "requests can be cancelled.",
        )

    if date.fromisoformat(request.start_date) <= today:
        return _error(
            "cancel_leave", args,
            f"This request started on {request.start_date}, which is not after today "
            f"({today.isoformat()}). Only requests that start in the future can be cancelled.",
        )

    details = _request_to_dict(request)
    if not confirmed:
        return ToolResult(
            name="cancel_leave", args=args, status="pending_confirmation", data=details
        )

    # Execute: status change and balance restore in one IMMEDIATE transaction
    # (ownership, status and start date are re-checked inside it).
    try:
        cancelled = leave_repo.cancel_leave_request(request_id, requester.employee_id, today)
    except LeaveWriteError as e:
        message = (
            REQUEST_NOT_FOUND_MESSAGE if "couldn't find" in e.message else e.message
        )
        return _error("cancel_leave", args, message)

    balances = leave_repo.get_balance(requester.employee_id, cancelled.leave_type)
    details.update(
        status="cancelled",
        restored_days=cancelled.working_days,
        balance_after=balances[0].balance_days if balances else None,
    )
    return ToolResult(name="cancel_leave", args=args, status="success", data=details)


# ──────────────────────────────────────────────────────────
# Tool Registry
# ──────────────────────────────────────────────────────────

# Maps tool names to their functions and schemas (for LLM function calling)
TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "get_leave_balance",
            "description": "Returns the remaining leave balance of one employee. "
            "Use when the user asks about their leave balance or days remaining.",
            "parameters": {
                "type": "object",
                "properties": {
                    "employee_id": {
                        "type": "string",
                        "description": "The employee ID to check balance for",
                    },
                    "leave_type": {
                        "type": "string",
                        "enum": ["annual", "sick"],
                        "description": "Optional: type of leave to check",
                    },
                },
                "required": ["employee_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_leave_requests",
            "description": "Returns the leave requests of one employee. "
            "Use when the user asks about their leave requests or leave history.",
            "parameters": {
                "type": "object",
                "properties": {
                    "employee_id": {
                        "type": "string",
                        "description": "The employee ID",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["pending", "approved", "cancelled"],
                        "description": "Optional: filter by status",
                    },
                },
                "required": ["employee_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_team_leave",
            "description": "Returns the pending and approved leave of the requester's "
            "direct reports that overlaps a date range. Managers only.",
            "parameters": {
                "type": "object",
                "properties": {
                    "start_date": {
                        "type": "string",
                        "description": "Start date in YYYY-MM-DD format",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "End date in YYYY-MM-DD format",
                    },
                },
                "required": ["start_date", "end_date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apply_leave",
            "description": "Creates a leave request for the requester. "
            "Use when the user wants to apply for leave. "
            "Always ask for leave_type if not specified.",
            "parameters": {
                "type": "object",
                "properties": {
                    "leave_type": {
                        "type": "string",
                        "enum": ["annual", "sick"],
                        "description": "Type of leave: annual or sick",
                    },
                    "start_date": {
                        "type": "string",
                        "description": "Start date in YYYY-MM-DD format",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "End date in YYYY-MM-DD format",
                    },
                },
                "required": ["leave_type", "start_date", "end_date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_leave",
            "description": "Cancels one of the requester's own leave requests. "
            "Use when the user wants to cancel a pending or approved leave request.",
            "parameters": {
                "type": "object",
                "properties": {
                    "request_id": {
                        "type": "string",
                        "description": "The request ID to cancel",
                    },
                },
                "required": ["request_id"],
            },
        },
    },
]
