"""Thin HTTP client for the Veltrix PeopleOps Assistant API.

The Streamlit UI only talks to the service through its public API
(``GET /health`` and ``POST /chat``), so every permission check, retrieval
rule and LLM call stays on the server. This module has no Streamlit import,
so it can be unit-tested on its own.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

import requests

# Friendly messages for the API's documented error codes (Section 9.1).
ERROR_MESSAGES = {
    400: "The message could not be sent (the request was invalid). Please try again.",
    401: "That employee ID is not recognised. Pick another profile in the sidebar.",
    403: "This conversation belongs to another employee. Start a new conversation.",
    503: "The assistant is still starting up (indexing documents). Try again in a moment.",
}


@dataclass
class ChatResult:
    """Outcome of one ``POST /chat`` call."""

    ok: bool
    status_code: Optional[int]
    data: dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    latency_s: float = 0.0


class VeltrixAPI:
    """Minimal client for ``/health`` and ``/chat``."""

    def __init__(self, base_url: str, timeout: float = 35.0, session: Any = None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout  # the server answers within 27 s by design
        self._http = session or requests.Session()

    def health(self) -> str:
        """Return "ok", "starting", "error" or "offline"."""
        try:
            response = self._http.get(f"{self.base_url}/health", timeout=5)
        except requests.RequestException:
            return "offline"
        if response.status_code == 200:
            return "ok"
        try:
            return str(response.json().get("status", "starting"))
        except ValueError:
            return "starting"

    def chat(self, employee_id: str, session_id: str, message: str) -> ChatResult:
        """Send one message and map transport/API errors to friendly text."""
        started = time.monotonic()
        try:
            response = self._http.post(
                f"{self.base_url}/chat",
                headers={"X-Employee-Id": employee_id},
                json={"session_id": session_id, "message": message},
                timeout=self.timeout,
            )
        except requests.Timeout:
            return ChatResult(False, None, error="The assistant took too long to reply. Please try again.",
                              latency_s=time.monotonic() - started)
        except requests.RequestException:
            return ChatResult(False, None,
                              error=f"Cannot reach the assistant API at {self.base_url}. Is the server running?",
                              latency_s=time.monotonic() - started)

        latency = time.monotonic() - started
        if response.status_code == 200:
            try:
                return ChatResult(True, 200, data=response.json(), latency_s=latency)
            except ValueError:
                return ChatResult(False, 200, error="The API returned an unreadable response.",
                                  latency_s=latency)
        message_text = ERROR_MESSAGES.get(
            response.status_code, f"Unexpected error from the API (HTTP {response.status_code})."
        )
        return ChatResult(False, response.status_code, error=message_text, latency_s=latency)
