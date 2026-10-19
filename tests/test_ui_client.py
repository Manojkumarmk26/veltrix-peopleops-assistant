"""Streamlit UI client: request shape and friendly error mapping (no network)."""

import pytest

requests = pytest.importorskip("requests")

from ui.api_client import ERROR_MESSAGES, VeltrixAPI  # noqa: E402


class FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    def __init__(self, response=None, exc=None):
        self.response, self.exc, self.calls = response, exc, []

    def _do(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if self.exc:
            raise self.exc
        return self.response

    def get(self, url, **kwargs):
        return self._do("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._do("POST", url, **kwargs)


def test_chat_sends_header_and_body():
    body = {"request_id": "r", "answer": "hi", "route": "tool", "citations": [],
            "tool_calls": [], "pending_confirmation": False}
    session = FakeSession(FakeResponse(200, body))
    result = VeltrixAPI("http://api:8000/", session=session).chat("E2004", "s1", "balance?")
    method, url, kwargs = session.calls[0]
    assert (method, url) == ("POST", "http://api:8000/chat")
    assert kwargs["headers"] == {"X-Employee-Id": "E2004"}
    assert kwargs["json"] == {"session_id": "s1", "message": "balance?"}
    assert result.ok and result.data["answer"] == "hi"


@pytest.mark.parametrize("code", [400, 401, 403, 503])
def test_documented_errors_are_friendly(code):
    result = VeltrixAPI("http://api", session=FakeSession(FakeResponse(code, {"detail": "x"}))).chat("E1", "s", "m")
    assert not result.ok and result.status_code == code
    assert result.error == ERROR_MESSAGES[code]


def test_unreachable_api():
    session = FakeSession(exc=requests.ConnectionError("refused"))
    api = VeltrixAPI("http://api", session=session)
    result = api.chat("E1", "s", "m")
    assert not result.ok and result.status_code is None and "Cannot reach" in result.error
    assert api.health() == "offline"


@pytest.mark.parametrize("code, payload, expected", [
    (200, {"status": "ok"}, "ok"),
    (503, {"status": "starting"}, "starting"),
    (503, {"status": "error"}, "error"),
])
def test_health(code, payload, expected):
    assert VeltrixAPI("http://api", session=FakeSession(FakeResponse(code, payload))).health() == expected
