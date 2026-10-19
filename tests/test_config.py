"""Configuration: LLM key detection and OpenAI-compatible endpoints."""

import pytest

from app.config import Settings
from app.orchestration.llm_client import LLMClient, LLMError, parse_json_object


@pytest.mark.parametrize("key, expected", [
    (None, False),
    ("", False),
    ("   ", False),
    ("sk-...", False),            # placeholder copied from docs
    ("your-openai-key", False),
    ("ollama", True),             # any real-looking value counts
    ("sk-proj-abc123", True),
])
def test_llm_configured_ignores_placeholders(key, expected):
    settings = Settings(llm_provider="openai", openai_api_key=key)
    assert settings.llm_configured is expected


def test_openai_base_url_is_used_for_compatible_servers():
    client = LLMClient()
    client.settings = Settings(
        llm_provider="openai", openai_api_key="ollama",
        openai_base_url="http://localhost:11434/v1",
    )
    client.provider = "openai"
    assert str(client._get_client().base_url).rstrip("/") == "http://localhost:11434/v1"


def test_default_openai_endpoint_when_base_url_empty(monkeypatch):
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    client = LLMClient()
    client.settings = Settings(llm_provider="openai", openai_api_key="sk-proj-abc123",
                               openai_base_url="")
    client.provider = "openai"
    assert "api.openai.com" in str(client._get_client().base_url)


def _client_with(**overrides) -> LLMClient:
    client = LLMClient()
    client.settings = Settings(llm_provider="openai", openai_api_key="sk-proj-abc123", **overrides)
    client.provider = "openai"
    return client


def test_llm_call_is_abandoned_at_the_deadline():
    import time

    client = _client_with(llm_deadline_seconds=0.3)
    client._complete_openai = lambda *args: time.sleep(3) or "{}"
    started = time.monotonic()
    with pytest.raises(LLMError, match="deadline"):
        client.complete("s", "u", purpose="test", corpus_tokens=10)
    assert time.monotonic() - started < 2


@pytest.mark.parametrize("raw, expected", [
    ('{"reasoning": {"enabled": false}}', {"reasoning": {"enabled": False}}),
    ("", None),
    ("not json", None),
    ("[1, 2]", None),
])
def test_openai_extra_body(raw, expected):
    assert _client_with(openai_extra_body=raw)._extra_body() == expected


@pytest.mark.parametrize("text, answer", [
    ('{"status": "answered", "answer": "A"}', "A"),
    ('<think>use {P1}</think>\n{"status": "answered", "answer": "B"}', "B"),
    ('Reasoning {x} first.\n{"answer": "C", "used_passages": ["P1"]}\nThanks {y}', "C"),
])
def test_parse_json_object_tolerates_reasoning(text, answer):
    assert parse_json_object(text)["answer"] == answer


@pytest.mark.parametrize("text", ["", "The user is asking", '{"status": "answ'])
def test_parse_json_object_rejects_non_json(text):
    assert parse_json_object(text) is None
