"""Helpers shared by the test modules."""

import uuid

from app.data_access.database import get_database


def chat(client, employee_id, message, session_id=None):
    """POST /chat and return (status_code, json)."""
    response = client.post(
        "/chat",
        json={"session_id": session_id or str(uuid.uuid4()), "message": message},
        headers={"X-Employee-Id": employee_id} if employee_id else {},
    )
    return response.status_code, response.json()


def balance(employee_id, leave_type):
    row = get_database().execute_one(
        "SELECT balance_days FROM leave_balances WHERE employee_id = ? AND leave_type = ?",
        (employee_id, leave_type),
    )
    return row["balance_days"]


def check_route_contract(body):
    """Section 8.1: citations/tool_calls per route."""
    route, cites, tools = body["route"], body["citations"], body["tool_calls"]
    assert set(body) == {"request_id", "answer", "route", "citations", "tool_calls",
                         "pending_confirmation"}
    if route == "rag":
        assert cites and not tools
    elif route == "tool":
        assert not cites and tools
    elif route == "hybrid":
        assert cites and tools
    else:
        assert route in ("clarify", "no_answer", "refuse")
        assert not cites and not tools
    if body["pending_confirmation"]:
        assert route == "tool"
        assert tools[-1]["status"] == "pending_confirmation"
    for c in cites:
        assert set(c) == {"doc_id", "locator"} and c["doc_id"] and c["locator"]
    for t in tools:
        assert set(t) == {"name", "args", "status"}
        assert t["status"] in ("success", "error", "pending_confirmation")
