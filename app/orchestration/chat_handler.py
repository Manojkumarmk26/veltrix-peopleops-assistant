"""Main chat orchestration handler for Veltrix PeopleOps Assistant.

Handles message processing, routing, tool invocation, two-phase confirmations,
retrieval, personal data masking, and API response generation.

Pipeline for every message (all decisions that affect permissions or writes
are deterministic; the LLM only writes answers from retrieved passages):

1. Safety guards — prompt injection / false authority claims, off-topic.
2. Pending write proposal — confirm, decline, change (re-propose) or discard.
3. Planning — intent and slots (``planner.plan_message``), dates
   (``dates.parse_date_range``), bulk and personal-data checks.
4. Target employee resolution and the fixed-message refusal for anyone the
   requester may not see (Section 7.3).
5. Dispatch to a route: tool, rag, hybrid, clarify, no_answer or refuse.

Every response satisfies the route contract of Section 8.1, enforced in
``_respond``.
"""

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from app.config import get_settings
from app.data_access.employee_repo import Employee, EmployeeRepository
from app.data_access.leave_repo import LeaveRepository, LeaveRequest, _fmt_days
from app.orchestration import planner
from app.orchestration.dates import extract_reference_date, find_date_mentions, parse_date_range
from app.orchestration.llm_client import LLMClient, LLMError, parse_json_object
from app.orchestration.prompts import build_answer_prompt, get_system_prompt
from app.orchestration.session import Session, SessionManager
from app.privacy import sanitize_answer
from app.retrieval.precedence import precedence_key
from app.retrieval.retriever import Passage, RetrievalResult, Retriever
from app.tools.leave_tools import (
    ToolResult,
    apply_leave,
    cancel_leave,
    get_leave_balance,
    get_team_leave,
    list_leave_requests,
)
from app.tools.permissions import ACCESS_DENIED_MESSAGE, PermissionError, check_leave_data_access

logger = logging.getLogger(__name__)

# ── Fixed messages ───────────────────────────────────────────────────────────

REFUSE_INJECTION = (
    "I can't help with that. I follow Veltrix's access rules, which can't be changed from "
    "the chat, and I don't share my instructions. I'm happy to help with HR policies, "
    "holidays or your own leave."
)
REFUSE_OFF_TOPIC = (
    "Sorry, I can only help with Veltrix HR topics: company policies, public holidays and "
    "your leave (balances, requests, applying or cancelling)."
)
REFUSE_BULK = (
    "I can't share data about multiple employees at once. I can help with your own leave"
    " — and managers can ask about their team's leave."
)
REFUSE_PERSONAL_DATA = (
    "For privacy reasons I can't share personal contact details such as phone numbers "
    "or e-mail addresses."
)
REFUSE_WRITE_FOR_OTHERS = "I can only apply for or cancel leave on your own behalf."
REFUSE_TEAM_NOT_MANAGER = (
    "Team leave is only available to managers for their direct reports. I can help "
    "with your own leave instead."
)
NO_ANSWER = (
    "I couldn't find an answer to that in the documents available to you. Please contact "
    "the HR team (People Team) for help with this."
)
GREETING_REPLY = (
    "Hi! I can answer questions about Veltrix HR policies and public holidays, check your "
    "leave balance and requests, and apply for or cancel leave. What would you like to do?"
)
CONFIRM_HINT = "Reply \"yes\" to confirm, or tell me what to change."

GREETING_RE = re.compile(
    r"^(hi|hello|hey|hiya|good (morning|afternoon|evening)|thanks|thank you|thx|cheers)"
    r"[\s!.,]*(there|team|assistant)?[\s!.]*$",
    re.IGNORECASE,
)
_QUESTION_START = re.compile(
    r"^(what|when|where|who|which|why|how|can|could|may|do|does|is|are|should|will|would)\b",
    re.IGNORECASE,
)
_CANCEL_CONFIRM_RE = re.compile(
    r"^(yes|yeah|yep|sure|ok|okay|please)?[\s,]*(cancel|withdraw)\s+(it|that|this|the request|"
    r"the leave)(\s+please)?[\s.!]*$",
    re.IGNORECASE,
)
_ORDINALS = {"first": 0, "1st": 0, "second": 1, "2nd": 1, "third": 2, "3rd": 2, "last": -1}
_END_CUE = re.compile(
    r"(until|till|through|thru|end date( to| is| on)?|ending( on)?|end( on)?|"
    r"extend\w*( it| the leave| this)?( to| until| till)?)\s*(on\s+)?$",
    re.IGNORECASE,
)
_START_CUE = re.compile(
    r"(from|starting( on| from)?|start date( to| is| on)?|start( on)?|begin\w*( on)?)\s*(on\s+)?$",
    re.IGNORECASE,
)


@dataclass
class DocAnswer:
    """Outcome of answering from documents."""

    status: str  # answered | partial | not_covered | out_of_scope | none
    answer: str = ""
    citations: list = field(default_factory=list)
    corpus_tokens: int = 0


def _fmt_date(iso: str) -> str:
    try:
        return date.fromisoformat(iso).strftime("%a %d %b %Y")
    except (TypeError, ValueError):
        return str(iso)


def _fmt_range(start: str, end: str) -> str:
    return _fmt_date(start) if start == end else f"{_fmt_date(start)} to {_fmt_date(end)}"


def _plural(n: float, word: str = "day") -> str:
    return f"{_fmt_days(n)} {word}{'' if float(n) == 1 else 's'}"


class ChatHandler:
    """Orchestrates conversations, tool dispatch, retrieval, and response routing."""

    def __init__(
        self,
        retriever: Optional[Retriever] = None,
        session_manager: Optional[SessionManager] = None,
        llm_client: Optional[LLMClient] = None,
    ):
        self.settings = get_settings()
        self.retriever = retriever
        self.session_manager = session_manager or SessionManager()
        self.llm_client = llm_client or LLMClient()
        self.employees = EmployeeRepository()
        self.leave_repo = LeaveRepository()

    # ── Public API ───────────────────────────────────────────────────────────

    def handle_message(self, session_id: str, requester: Employee, message: str) -> dict:
        """Handle a message from an authenticated employee in a session.

        Raises:
            SessionOwnershipError: if the session belongs to another employee.
        """
        request_id = str(uuid.uuid4())
        started = time.monotonic()
        session = self.session_manager.get_or_create(session_id, requester.employee_id)

        with session.lock:  # one message at a time per session
            response = self._process(session, requester, (message or "").strip())
            self.session_manager.add_message(session_id, "user", message)
            self.session_manager.add_message(session_id, "assistant", response["answer"])

        response["request_id"] = request_id
        logger.info(
            "chat request_id=%s employee=%s route=%s tools=%s pending=%s citations=%d latency=%.2fs",
            request_id,
            requester.employee_id,
            response["route"],
            [f"{t['name']}:{t['status']}" for t in response["tool_calls"]],
            response["pending_confirmation"],
            len(response["citations"]),
            time.monotonic() - started,
        )
        return response

    # ── Main pipeline ────────────────────────────────────────────────────────

    def _process(self, session: Session, requester: Employee, text: str) -> dict:
        today = self.settings.today

        # 1. Safety guards (refused messages also discard any proposal/draft).
        guard = planner.detect_guard(text, requester.role)
        if guard:
            self._reset(session)
            return self._respond(REFUSE_INJECTION if guard == "injection" else REFUSE_OFF_TOPIC,
                                 "refuse")

        mentions, date_info = find_date_mentions(text, today, self.settings.default_year)
        plan = planner.plan_message(text, requester.role, has_dates=bool(mentions))

        # 2. A pending write proposal is resolved before anything else.
        pending = session.pending_action
        if pending is not None:
            if planner.is_confirmation(text) or (
                pending.tool_name == "cancel_leave" and _CANCEL_CONFIRM_RE.match(text)
            ):
                return self._execute_pending(session, requester)
            if planner.is_decline(text):
                self._reset(session)
                return self._respond(
                    "OK, I've discarded that request — nothing has been changed.", "clarify"
                )
            if pending.tool_name == "apply_leave" and self._is_change(text, plan, mentions):
                return self._change_pending(session, requester, text)
            # Anything else discards the proposal and is handled normally.
            self.session_manager.clear_pending_action(session.session_id)
        elif planner.is_confirmation(text):
            return self._respond(
                "There's nothing waiting for your confirmation right now. What would you like to do?",
                "clarify",
            )
        elif planner.is_decline(text) and len(text.split()) <= 3 and plan.intent != "cancel":
            # "no" / "never mind" with nothing pending; "cancel that leave" is a cancel request.
            session.draft = None
            return self._respond("OK. Is there anything else I can help you with?", "clarify")

        # 3. Follow-up to an earlier clarifying question.
        draft, session.draft = session.draft, None
        if draft and draft.get("intent") == "apply" and self._continues_apply(text, plan, mentions):
            return self._handle_apply(session, requester, text, plan, draft=draft)
        if draft and draft.get("intent") == "cancel":
            if plan.request_id:
                return self._propose_cancel(session, requester, plan.request_id)
            followed = self._continue_cancel(session, requester, text, plan, draft)
            if followed is not None:
                return followed

        if GREETING_RE.match(text):
            return self._respond(GREETING_REPLY, "clarify")

        # 4. Bulk and personal-data requests.
        if planner.is_bulk_request(text, requester.role, plan.intent):
            return self._respond(REFUSE_BULK, "refuse")
        if planner.PERSONAL_DATA_REQUEST.search(text) and not re.search(
            r"\b(update|change|correct|edit)\b", text, re.IGNORECASE
        ):
            return self._respond(REFUSE_PERSONAL_DATA, "refuse")

        # 5. Who is the question about?
        target, refusal = self._resolve_target(requester, plan)
        if refusal is not None:
            return refusal

        intent = plan.intent
        if intent in ("apply", "cancel") and target.employee_id != requester.employee_id:
            return self._respond(REFUSE_WRITE_FOR_OTHERS, "refuse")
        if intent == "apply":
            return self._handle_apply(session, requester, text, plan)
        if intent == "cancel":
            return self._handle_cancel(session, requester, text, plan)
        if intent == "team_leave":
            return self._handle_team_leave(requester, text)
        if intent == "balance" and not plan.wants_docs:
            return self._handle_balance(requester, target, plan)
        if intent in ("future_balance", "balance"):
            return self._handle_hybrid(requester, target, text, plan)
        if intent == "requests":
            return self._handle_requests(requester, target, plan)
        return self._handle_policy(requester, text)

    # ── Response construction ────────────────────────────────────────────────

    @staticmethod
    def _respond(
        answer: str,
        route: str,
        citations: Optional[list] = None,
        tool_results: Optional[list[ToolResult]] = None,
        pending: bool = False,
    ) -> dict:
        """Build the API response and enforce the route contract (Section 8.1)."""
        citations = list(citations or [])
        tool_calls = [
            {"name": t.name, "args": t.args, "status": t.status} for t in (tool_results or [])
        ]
        if route in ("clarify", "no_answer", "refuse"):
            citations, tool_calls, pending = [], [], False
        elif route == "tool":
            citations = []
        elif route == "rag":
            tool_calls, pending = [], False
        if route == "rag" and not citations:
            route = "no_answer"
            answer = answer or NO_ANSWER
        if route == "hybrid" and not citations:
            route = "tool"
        if route in ("tool", "hybrid") and not tool_calls:
            route, citations = "no_answer", []
        return {
            "request_id": "",
            "answer": answer,
            "route": route,
            "citations": citations,
            "tool_calls": tool_calls,
            "pending_confirmation": bool(pending and route == "tool"),
        }

    def _reset(self, session: Session) -> None:
        session.draft = None
        self.session_manager.clear_pending_action(session.session_id)

    # ── Target resolution (Section 7.3) ──────────────────────────────────────

    def _resolve_target(self, requester: Employee, plan) -> tuple[Employee, Optional[dict]]:
        """Return (target employee, refusal response or None)."""
        data_intents = ("balance", "future_balance", "requests", "apply", "cancel", "team_leave")
        ids = [i for i in plan.target_employee_ids if i != requester.employee_id]
        if ids:
            refusal = self._check_target(requester, ids[0])
            if refusal is not None:
                return requester, refusal
            return self.employees.get_by_id(ids[0]) or requester, None

        if plan.intent not in data_intents:
            return requester, None

        if plan.target_names:
            name = plan.target_names[0]
            if self._is_own_name(requester, name):
                return requester, None
            visible = []
            for emp in self.employees.find_by_name(name):
                try:
                    check_leave_data_access(requester, emp.employee_id)
                    visible.append(emp)
                except PermissionError:
                    continue
            if not visible:
                return requester, self._respond(ACCESS_DENIED_MESSAGE, "refuse")
            if len(visible) > 1:
                options = ", ".join(e.employee_id for e in visible)
                return requester, self._respond(
                    f"More than one employee you can view matches that name ({options}). "
                    "Which employee ID do you mean?",
                    "clarify",
                )
            return visible[0], None

        if plan.other_person:
            return requester, self._respond(ACCESS_DENIED_MESSAGE, "refuse")
        return requester, None

    def _check_target(self, requester: Employee, employee_id: str) -> Optional[dict]:
        """Fixed refusal unless the requester may view this employee and they exist."""
        try:
            check_leave_data_access(requester, employee_id)
        except PermissionError:
            return self._respond(ACCESS_DENIED_MESSAGE, "refuse")
        if not self.employees.exists(employee_id):
            return self._respond(ACCESS_DENIED_MESSAGE, "refuse")
        return None

    @staticmethod
    def _is_own_name(requester: Employee, name: str) -> bool:
        parts = requester.full_name.lower().split()
        return name.lower() in (requester.full_name.lower(), parts[0] if parts else "")

    def _subject(self, requester: Employee, target: Employee) -> tuple[str, str]:
        """('You', 'your') or ('Name (E1234)', "Name (E1234)'s") for permitted targets."""
        if target.employee_id == requester.employee_id:
            return "You", "your"
        name = self.employees.get_safe_name(target.employee_id, requester)
        label = f"{name} ({target.employee_id})" if name else target.employee_id
        return label, f"{label}'s"

    # ── Tool routes ──────────────────────────────────────────────────────────

    def _handle_balance(self, requester: Employee, target: Employee, plan) -> dict:
        result = get_leave_balance(requester, target.employee_id, plan.leave_type)
        if result.status != "success":
            return self._respond(result.error_message or ACCESS_DENIED_MESSAGE, "tool",
                                 tool_results=[result])
        return self._respond(self._balance_text(requester, target, result), "tool",
                             tool_results=[result])

    def _balance_text(self, requester: Employee, target: Employee, result: ToolResult) -> str:
        who, whose = self._subject(requester, target)
        balances = result.data.get("balances", [])
        if not balances:
            return f"No leave balance is recorded for {whose if whose != 'your' else 'you'}."
        if len(balances) == 1:
            b = balances[0]
            verb = "have" if who == "You" else "has"
            return f"{who} {verb} {_plural(b['balance_days'])} of {b['leave_type']} leave remaining."
        lines = [f"- {b['leave_type'].capitalize()}: {_plural(b['balance_days'])}" for b in balances]
        return f"{whose[0].upper() + whose[1:]} current leave balances:\n" + "\n".join(lines)

    def _handle_requests(self, requester: Employee, target: Employee, plan) -> dict:
        result = list_leave_requests(requester, target.employee_id, plan.status)
        if result.status != "success":
            return self._respond(result.error_message or ACCESS_DENIED_MESSAGE, "tool",
                                 tool_results=[result])
        return self._respond(self._requests_text(requester, target, result, plan.status), "tool",
                             tool_results=[result])

    def _requests_text(self, requester, target, result: ToolResult, status: Optional[str]) -> str:
        who, whose = self._subject(requester, target)
        requests = result.data.get("requests", [])
        qualifier = f"{status} " if status else ""
        if not requests:
            owner = "You have" if who == "You" else f"{who} has"
            return f"{owner} no {qualifier}leave requests."
        lines = [
            f"- {r['leave_type'].capitalize()} leave, {_fmt_range(r['start_date'], r['end_date'])} "
            f"({_plural(r['working_days'], 'working day')}) — {r['status']} "
            f"(request ID {r['request_id']})"
            for r in requests
        ]
        return f"{whose[0].upper() + whose[1:]} {qualifier}leave requests:\n" + "\n".join(lines)

    def _handle_team_leave(self, requester: Employee, text: str) -> dict:
        if requester.role != "manager":
            return self._respond(REFUSE_TEAM_NOT_MANAGER, "refuse")
        today = self.settings.today
        dates = parse_date_range(text, today, self.settings.default_year)
        if dates.ambiguous or dates.invalid:
            return self._respond(
                "Which dates should I check for your team? Please give a specific date range "
                "(for example, 16 to 20 November).",
                "clarify",
            )
        if dates.found:
            start, end = dates.start, dates.end
        else:
            start, end = today, max(today, date(self.settings.default_year, 12, 31))
        result = get_team_leave(requester, start.isoformat(), end.isoformat())
        if result.status != "success":
            return self._respond(result.error_message, "tool", tool_results=[result])
        rows = result.data.get("team_leave", [])
        period = _fmt_range(start.isoformat(), end.isoformat())
        if not rows:
            answer = (f"None of your {result.data.get('team_size', 0)} direct reports has pending "
                      f"or approved leave between {period}.")
        else:
            lines = [
                f"- {r['employee_name']} ({r['employee_id']}): {r['leave_type']} leave, "
                f"{_fmt_range(r['start_date'], r['end_date'])} "
                f"({_plural(r['working_days'], 'working day')}, {r['status']})"
                for r in rows
            ]
            answer = f"Team leave overlapping {period}:\n" + "\n".join(lines)
        return self._respond(answer, "tool", tool_results=[result])

    # ── apply_leave (two-phase) ──────────────────────────────────────────────

    def _continues_apply(self, text: str, plan, mentions) -> bool:
        """Does this message answer the clarifying question of an apply draft?"""
        if plan.intent == "apply":
            return True
        provides = bool(mentions) or plan.leave_type is not None or bool(
            re.search(r"\b(this week|next month|this month)\b", text, re.IGNORECASE)
        )
        if not provides or plan.intent in ("cancel", "team_leave", "requests", "balance",
                                           "future_balance"):
            return False
        return not text.endswith("?") and not _QUESTION_START.match(text) and len(text.split()) <= 12

    def _handle_apply(self, session: Session, requester: Employee, text: str, plan,
                      draft: Optional[dict] = None) -> dict:
        today = self.settings.today
        draft = draft or {}
        leave_type = plan.leave_type or draft.get("leave_type")
        dates = parse_date_range(text, today, self.settings.default_year)
        start = dates.start.isoformat() if dates.found else draft.get("start_date")
        end = dates.end.isoformat() if dates.found else draft.get("end_date")

        missing = []
        if not leave_type:
            missing.append("the leave type (annual or sick)")
        date_question = None
        if dates.ambiguous or dates.invalid:
            date_question = "the exact dates (that date could be read more than one way or isn't valid)"
        elif dates.found and dates.month_only:
            date_question = f"the exact start and end dates within {dates.start.strftime('%B')}"
        elif dates.found and dates.past_phrase:
            date_question = ("which dates you mean — that date has already passed this year "
                             "(leave must start today or later)")
        elif not start or not end:
            date_question = "the start and end dates"
        if date_question:
            missing.append(date_question)
            if dates.ambiguous or dates.invalid or dates.month_only or dates.past_phrase:
                start = end = None

        if missing:
            session.draft = {"intent": "apply", "leave_type": leave_type,
                             "start_date": start, "end_date": end}
            return self._respond(
                "To apply for leave, please tell me " + self._join(missing) + ".", "clarify"
            )
        return self._propose_apply(session, requester, leave_type, start, end)

    def _propose_apply(self, session: Session, requester: Employee, leave_type: str,
                       start: str, end: str) -> dict:
        result = apply_leave(requester, leave_type, start, end, confirmed=False)
        if result.status != "pending_confirmation":
            self._reset(session)
            return self._respond(
                f"I can't submit that leave request: {result.error_message}", "tool",
                tool_results=[result],
            )
        self.session_manager.set_pending_action(session.session_id, "apply_leave", result.args)
        d = result.data
        answer = (
            "Please confirm this leave request:\n"
            f"- Type: {leave_type} leave\n"
            f"- Dates: {_fmt_range(start, end)}\n"
            f"- Working days: {d['working_days']}\n"
            f"- {leave_type.capitalize()} balance: {_fmt_days(d['balance'])} days now, "
            f"{_fmt_days(d['balance_after'])} days after this request\n\n" + CONFIRM_HINT
        )
        return self._respond(answer, "tool", tool_results=[result], pending=True)

    def _is_change(self, text: str, plan, mentions) -> bool:
        if plan.intent in ("cancel", "team_leave", "requests", "balance", "future_balance"):
            return False
        if plan.intent == "apply":
            return True
        provides = bool(mentions) or plan.leave_type is not None
        return provides and (bool(planner.CHANGE_CUES.search(text)) or len(text.split()) <= 6)

    def _change_pending(self, session: Session, requester: Employee, text: str) -> dict:
        """Merge the user's change into the pending apply proposal and re-propose."""
        pending = session.pending_action
        args = dict(pending.args)
        today = self.settings.today
        new_type = planner.detect_leave_type(text)
        if new_type:
            args["leave_type"] = new_type

        anchor = date.fromisoformat(args["start_date"])
        mentions, info = find_date_mentions(text, today, self.settings.default_year, anchor=anchor)
        if info.ambiguous or info.invalid:
            return self._respond(
                "Which exact dates would you like instead? Your earlier proposal is still "
                "waiting — reply \"yes\" to confirm it unchanged.",
                "clarify",
            )
        if len(mentions) >= 2:
            args["start_date"] = mentions[0].start.isoformat()
            args["end_date"] = mentions[-1].end.isoformat()
        elif len(mentions) == 1:
            m = mentions[0]
            prefix = text[:m.pos].strip()
            words = text[m.pos:].split()
            head = words[0].lower() if words else ""
            if head in ("until", "till", "through", "thru", "to", "from", "on"):
                prefix = f"{prefix} {head}".strip()
            if m.kind in ("range", "week", "month") or m.start != m.end:
                args["start_date"], args["end_date"] = m.start.isoformat(), m.end.isoformat()
            elif _END_CUE.search(prefix):
                args["end_date"] = m.end.isoformat()
            elif _START_CUE.search(prefix):
                args["start_date"] = m.start.isoformat()
                if m.start > date.fromisoformat(args["end_date"]):
                    args["end_date"] = m.start.isoformat()
            else:
                args["start_date"] = args["end_date"] = m.start.isoformat()

        self.session_manager.clear_pending_action(session.session_id)
        return self._propose_apply(session, requester, args["leave_type"],
                                   args["start_date"], args["end_date"])

    # ── cancel_leave (two-phase) ─────────────────────────────────────────────

    def _handle_cancel(self, session: Session, requester: Employee, text: str, plan) -> dict:
        if plan.request_id:
            return self._propose_cancel(session, requester, plan.request_id)

        today = self.settings.today
        candidates = self.leave_repo.get_cancellable_requests(requester.employee_id, today)
        candidates = self._filter_requests(candidates, text, plan)
        if not candidates:
            listing = list_leave_requests(requester, requester.employee_id)
            body = self._requests_text(requester, requester, listing, None)
            return self._respond(
                "I couldn't find a pending or approved request of yours starting after today "
                "that matches. Only those can be cancelled.\n\n" + body,
                "tool", tool_results=[listing],
            )
        if len(candidates) == 1:
            return self._propose_cancel(session, requester, candidates[0].request_id)

        session.draft = {"intent": "cancel", "candidates": [c.request_id for c in candidates]}
        lines = [
            f"{i}. {c.leave_type.capitalize()} leave, {_fmt_range(c.start_date, c.end_date)} "
            f"({c.status}) — request ID {c.request_id}"
            for i, c in enumerate(candidates, 1)
        ]
        return self._respond(
            "Which request would you like to cancel?\n" + "\n".join(lines)
            + "\n\nReply with the number, the dates or the request ID.",
            "clarify",
        )

    def _filter_requests(self, requests: list[LeaveRequest], text: str, plan) -> list[LeaveRequest]:
        today = self.settings.today
        out = requests
        if plan.leave_type:
            out = [r for r in out if r.leave_type == plan.leave_type]
        mentions, _ = find_date_mentions(text, today, self.settings.default_year)
        if mentions:
            start = min(m.start for m in mentions)
            end = max(m.end for m in mentions)
            out = [r for r in out
                   if date.fromisoformat(r.start_date) <= end and date.fromisoformat(r.end_date) >= start]
        return out

    def _continue_cancel(self, session, requester, text, plan, draft) -> Optional[dict]:
        by_id = {r.request_id: r for r in self.leave_repo.get_cancellable_requests(
            requester.employee_id, self.settings.today)}
        candidates = [by_id[i] for i in draft["candidates"] if i in by_id]
        lower = text.lower().strip(" .!")
        choice = None
        number = re.fullmatch(r"(?:number\s+|no\.?\s*|#)?(\d{1,2})", lower)
        if number and 1 <= int(number.group(1)) <= len(candidates):
            choice = candidates[int(number.group(1)) - 1]
        else:
            for word, idx in _ORDINALS.items():
                if re.search(rf"\b{word}\b", lower) and candidates:
                    if idx < len(candidates):
                        choice = candidates[idx]
                    break
        if choice is None:
            filtered = self._filter_requests(candidates, text, plan)
            if (plan.leave_type or find_date_mentions(text, self.settings.today)[0]) and filtered:
                if len(filtered) == 1:
                    choice = filtered[0]
                else:
                    session.draft = {"intent": "cancel",
                                     "candidates": [c.request_id for c in filtered]}
                    return self._respond(
                        "More than one request still matches. Please give the request ID:\n"
                        + "\n".join(f"- {c.request_id} ({_fmt_range(c.start_date, c.end_date)})"
                                    for c in filtered),
                        "clarify",
                    )
        if choice is None:
            return None
        return self._propose_cancel(session, requester, choice.request_id)

    def _propose_cancel(self, session: Session, requester: Employee, request_id: str) -> dict:
        result = cancel_leave(requester, request_id, confirmed=False)
        if result.status != "pending_confirmation":
            self._reset(session)
            return self._respond(f"I can't cancel that request: {result.error_message}", "tool",
                                 tool_results=[result])
        self.session_manager.set_pending_action(session.session_id, "cancel_leave", result.args)
        d = result.data
        answer = (
            "Please confirm you want to cancel this leave request:\n"
            f"- Request ID: {d['request_id']}\n"
            f"- Type: {d['leave_type']} leave\n"
            f"- Dates: {_fmt_range(d['start_date'], d['end_date'])}\n"
            f"- Status: {d['status']}\n"
            f"- {_plural(d['working_days'])} will be returned to your {d['leave_type']} balance\n\n"
            "Reply \"yes\" to confirm."
        )
        return self._respond(answer, "tool", tool_results=[result], pending=True)

    def _execute_pending(self, session: Session, requester: Employee) -> dict:
        pending = session.pending_action
        self._reset(session)  # a proposal can be executed at most once
        if pending.tool_name == "apply_leave":
            result = apply_leave(requester, confirmed=True, **pending.args)
            if result.status != "success":
                return self._respond(f"I couldn't submit the leave request: {result.error_message}",
                                     "tool", tool_results=[result])
            d = result.data
            answer = (
                f"Done — your {d['leave_type']} leave request for "
                f"{_fmt_range(d['start_date'], d['end_date'])} ({_plural(d['working_days'], 'working day')}) "
                f"has been submitted and is pending approval.\n"
                f"- Request ID: {d['request_id']}\n"
                f"- Remaining {d['leave_type']} balance: {_fmt_days(d['balance_remaining'])} days"
            )
            return self._respond(answer, "tool", tool_results=[result])

        result = cancel_leave(requester, confirmed=True, **pending.args)
        if result.status != "success":
            return self._respond(f"I couldn't cancel the request: {result.error_message}", "tool",
                                 tool_results=[result])
        d = result.data
        balance = (f" Your {d['leave_type']} balance is now {_fmt_days(d['balance_after'])} days."
                   if d.get("balance_after") is not None else "")
        answer = (
            f"Done — request {d['request_id']} ({d['leave_type']} leave, "
            f"{_fmt_range(d['start_date'], d['end_date'])}) has been cancelled and "
            f"{_plural(d['restored_days'])} returned to your balance.{balance}"
        )
        return self._respond(answer, "tool", tool_results=[result])

    # ── Document routes ──────────────────────────────────────────────────────

    def _handle_policy(self, requester: Employee, text: str) -> dict:
        ref_date = extract_reference_date(text, self.settings.today, self.settings.default_year)
        doc = self._answer_from_docs(requester, text, text, ref_date)
        if doc.status in ("answered", "partial"):
            return self._respond(doc.answer, "rag", citations=doc.citations)
        if doc.status == "out_of_scope":
            return self._respond(REFUSE_OFF_TOPIC, "refuse")
        if doc.status == "none" and not planner.HR_SCOPE.search(text):
            return self._respond(REFUSE_OFF_TOPIC, "refuse")
        return self._respond(NO_ANSWER, "no_answer")

    def _handle_hybrid(self, requester: Employee, target: Employee, text: str, plan) -> dict:
        balance = get_leave_balance(requester, target.employee_id, plan.leave_type)
        if balance.status != "success":
            return self._respond(balance.error_message or ACCESS_DENIED_MESSAGE, "tool",
                                 tool_results=[balance])
        balance_text = self._balance_text(requester, target, balance)

        today = self.settings.today
        years = max(0, (today - date.fromisoformat(target.join_date)).days // 365) \
            if target.join_date else None
        # Calendar facts only (no accrual): the dates on which monthly credits
        # fall for the rest of the year, and the next service anniversary, so
        # the model applies the documented rates without counting months itself.
        month_ends = []
        for month in range(today.month, 13):
            nxt = date(today.year + (month == 12), month % 12 + 1, 1)
            month_ends.append((nxt - timedelta(days=1)).isoformat())
        next_anniversary = None
        if target.join_date:
            joined = date.fromisoformat(target.join_date)
            for year in (today.year, today.year + 1):
                try:
                    candidate = joined.replace(year=year)
                except ValueError:  # 29 February
                    candidate = date(year, 3, 1)
                if candidate > today:
                    next_anniversary = candidate.isoformat()
                    break
        tools_block = json.dumps({
            "tool": "get_leave_balance",
            "employee_id": target.employee_id,
            "region": target.region,
            "level": target.level,
            "completed_years_of_service": years,
            "next_service_anniversary": next_anniversary,
            "today": today.isoformat(),
            "remaining_month_end_dates_this_year": month_ends,
            "balances": balance.data.get("balances", []),
        }, indent=1)
        query = text
        if plan.intent == "future_balance":
            query = f"{text} leave accrual per month carry forward limit"
        doc = self._answer_from_docs(requester, text, query, None, tools_block=tools_block,
                                     fallback_prefix=balance_text)
        if doc.status in ("answered", "partial"):
            return self._respond(doc.answer, "hybrid", citations=doc.citations,
                                 tool_results=[balance])
        return self._respond(
            balance_text + "\n\nI couldn't find the rules needed to work out more than this in "
            "the documents available to you — please contact the HR team for details.",
            "tool", tool_results=[balance],
        )

    def _answer_from_docs(
        self,
        requester: Employee,
        question: str,
        query: str,
        reference_date: Optional[date],
        tools_block: str = "",
        fallback_prefix: str = "",
    ) -> DocAnswer:
        """Retrieve permitted passages and compose an answer (LLM or extractive)."""
        if self.retriever is None:
            return DocAnswer("none")
        result = self.retriever.retrieve_context(query, requester, reference_date)
        if not result.passages:
            return DocAnswer("none")

        if self.llm_client.available:
            try:
                return self._llm_answer(requester, question, result, tools_block)
            except LLMError as e:
                logger.warning("LLM unavailable, using extractive answer: %s", type(e).__name__)
        return self._extractive_answer(result, fallback_prefix,
                                       self.settings.extractive_min_score)

    def _llm_answer(self, requester: Employee, question: str, result: RetrievalResult,
                    tools_block: str) -> DocAnswer:
        today = self.settings.today
        p_labels = {f"P{i}": p for i, p in enumerate(result.passages, 1)}
        u_labels = {f"U{i}": p for i, p in enumerate(result.upcoming, 1)}
        passages_block = "\n\n".join(self._passage_block(lbl, p) for lbl, p in p_labels.items())
        upcoming_block = "\n\n".join(
            self._passage_block(lbl, p, upcoming=True) for lbl, p in u_labels.items()
        )
        system_prompt = get_system_prompt(
            requester.employee_id, requester.role, requester.region, today.isoformat()
        )
        user_prompt = build_answer_prompt(
            question, passages_block, upcoming_block, tools_block,
            result.reference_date.isoformat(), today.isoformat(),
        )
        raw = self.llm_client.complete(
            system_prompt, user_prompt, purpose="answer", corpus_tokens=result.corpus_tokens,
        )
        data = parse_json_object(raw)
        if data is None:
            # Raw text may be a reasoning model's chain-of-thought or a cut-off
            # reply; it is never shown. The caller falls back to quoting passages.
            raise LLMError("empty response" if not raw.strip() else "response was not a JSON object")

        status = str(data.get("status", "answered")).strip().lower()
        answer = sanitize_answer(str(data.get("answer", "")).strip())
        if status in ("not_covered", "out_of_scope"):
            return DocAnswer(status, answer, [], result.corpus_tokens)
        if not answer:
            return DocAnswer("not_covered", "", [], result.corpus_tokens)

        citations, seen, cited_docs = [], set(), set()
        for label in data.get("used_passages") or []:
            p = p_labels.get(str(label).strip().upper())
            if p and (p.doc_id, p.locator) not in seen:
                seen.add((p.doc_id, p.locator))
                cited_docs.add(p.doc_id)
                citations.append({"doc_id": p.doc_id, "locator": p.locator})
        if not citations:
            first = result.passages[0]
            citations.append({"doc_id": first.doc_id, "locator": first.locator})
            cited_docs.add(first.doc_id)
        for label in data.get("used_upcoming") or []:
            p = u_labels.get(str(label).strip().upper())
            # Only cite an upcoming document that would win on its effective date.
            if p and not (set(p.outranked_by) & cited_docs) and (p.doc_id, p.locator) not in seen:
                seen.add((p.doc_id, p.locator))
                citations.append({"doc_id": p.doc_id, "locator": p.locator})
        return DocAnswer("partial" if status == "partial" else "answered", answer, citations,
                         result.corpus_tokens)

    @staticmethod
    def _passage_block(label: str, p: Passage, upcoming: bool = False) -> str:
        meta = p.metadata or {}
        regions = ",".join(meta.get("regions", [])) or "GLOBAL"
        header = (f"[{label}] doc_id={p.doc_id}; type={meta.get('doc_type', '?')}; "
                  f"regions={regions}; effective={meta.get('effective_date', '?')}")
        if upcoming:
            header += f"; outranked_by={','.join(p.outranked_by[:4]) or 'none'}"
        return f"{header}\n{p.text}"

    @staticmethod
    def _extractive_answer(result: RetrievalResult, prefix: str = "",
                           min_score: float = 0.0) -> DocAnswer:
        """Answer without an LLM: quote the most relevant permitted passages.

        Without a model to judge coverage, a stricter relevance bar applies.
        Near-top passages are shown in Section 5 precedence order (authority,
        scope, recency) with their type and effective date.
        """
        if result.top_score < min_score:
            return DocAnswer("not_covered", "", [], result.corpus_tokens)
        close = [p for p in result.passages if p.score >= result.top_score - 0.08]
        top = sorted(close, key=lambda p: (precedence_key(p.metadata or {"doc_type": "wiki"}),
                                           -p.score))[:2]
        quotes, citations = [], []
        for p in top:
            body = p.text.split("\n", 1)[1] if p.text.startswith("[") and "\n" in p.text else p.text
            body = " ".join(body.split())
            if len(body) > 450:
                cut = body.rfind(". ", 0, 450)
                body = body[: cut + 1 if cut > 150 else 450].rstrip() + " …"
            meta = p.metadata or {}
            quotes.append(
                f"- {p.doc_id} ({meta.get('doc_type', 'document')}, effective "
                f"{meta.get('effective_date', '?')}), {p.locator}: {body}"
            )
            citations.append({"doc_id": p.doc_id, "locator": p.locator})
        intro = ("Here is what the documents that apply to you say (highest-precedence "
                 "document first):")
        answer = (prefix + "\n\n" if prefix else "") + intro + "\n" + "\n".join(quotes)
        return DocAnswer("answered", sanitize_answer(answer), citations, result.corpus_tokens)

    @staticmethod
    def _join(items: list[str]) -> str:
        if len(items) <= 1:
            return "".join(items)
        return ", ".join(items[:-1]) + " and " + items[-1]
