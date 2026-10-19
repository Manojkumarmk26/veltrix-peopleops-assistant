"""Session management — tracks conversations and pending confirmations.

Sessions live in process memory (single-instance deployment, see README
Limitations). A session belongs to the first employee who used it; any other
employee gets HTTP 403 (Section 9.1). Each session has its own lock so that
two concurrent messages in the same session cannot both act on one pending
write proposal.
"""

import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Lock, RLock
from typing import Optional

logger = logging.getLogger(__name__)

MAX_SESSIONS = 10_000
MAX_HISTORY_MESSAGES = 20


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SessionOwnershipError(ValueError):
    """Raised when a session is used by an employee who does not own it."""

    def __init__(self, session_id: str):
        # Message kept for backwards compatibility with callers matching on it.
        super().__init__("Session belongs to a different employee")
        self.session_id = session_id


@dataclass
class PendingAction:
    """A proposed write action awaiting confirmation."""

    tool_name: str
    args: dict
    proposed_at: datetime


@dataclass
class Session:
    """A conversation session."""

    session_id: str
    employee_id: str
    created_at: datetime = field(default_factory=_utcnow)
    messages: list[dict] = field(default_factory=list)
    pending_action: Optional[PendingAction] = None
    # Slots collected so far when the assistant asked a clarifying question.
    draft: Optional[dict] = None
    lock: RLock = field(default_factory=RLock, repr=False, compare=False)


class SessionManager:
    """Thread-safe session management."""

    def __init__(self, max_sessions: int = MAX_SESSIONS):
        self._sessions: "OrderedDict[str, Session]" = OrderedDict()
        self._lock = Lock()
        self._max_sessions = max_sessions

    def get_or_create(self, session_id: str, employee_id: str) -> Session:
        """Get an existing session or create a new one.

        Raises:
            SessionOwnershipError: (a ValueError) if the session exists but
                belongs to a different employee (HTTP 403).
        """
        with self._lock:
            session = self._sessions.get(session_id)
            if session is not None:
                if session.employee_id != employee_id:
                    raise SessionOwnershipError(session_id)
                self._sessions.move_to_end(session_id)
                return session

            session = Session(session_id=session_id, employee_id=employee_id)
            self._sessions[session_id] = session
            # Bound memory: evict the least recently used sessions.
            while len(self._sessions) > self._max_sessions:
                self._sessions.popitem(last=False)
            logger.info("New session created for employee=%s", employee_id)
            return session

    def add_message(self, session_id: str, role: str, content: str) -> None:
        """Add a message to a session's history (bounded)."""
        with self._lock:
            session = self._sessions.get(session_id)
            if session is not None:
                session.messages.append({"role": role, "content": content})
                del session.messages[:-MAX_HISTORY_MESSAGES]

    def set_pending_action(self, session_id: str, tool_name: str, args: dict) -> None:
        """Set the pending write action (at most one per session)."""
        with self._lock:
            session = self._sessions.get(session_id)
            if session is not None:
                session.pending_action = PendingAction(
                    tool_name=tool_name, args=dict(args), proposed_at=_utcnow()
                )
                logger.info("Pending action set: tool=%s", tool_name)

    def get_pending_action(self, session_id: str) -> Optional[PendingAction]:
        """Get the pending action for a session, if any."""
        with self._lock:
            session = self._sessions.get(session_id)
            return session.pending_action if session else None

    def clear_pending_action(self, session_id: str) -> None:
        """Clear any pending action for a session."""
        with self._lock:
            session = self._sessions.get(session_id)
            if session is not None:
                session.pending_action = None

    def get_history(self, session_id: str, max_turns: int = 10) -> list[dict]:
        """Get recent conversation history for a session."""
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return []
            return list(session.messages[-max_turns * 2:])

    def exists(self, session_id: str) -> bool:
        """Check if a session exists."""
        with self._lock:
            return session_id in self._sessions
