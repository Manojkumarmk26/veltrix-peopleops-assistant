"""LLM client abstraction — supports OpenAI, Anthropic, and Google Gemini.

Every call logs the number of corpus tokens it carries (Section 4.3) together
with the purpose, provider, model and latency. Prompt *content* is never
logged, so personal data cannot leak into logs through this module.
"""

import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Optional

from app.config import get_settings
from app.ingestion.chunker import count_tokens

logger = logging.getLogger(__name__)

# Runs provider calls so a hard wall-clock deadline can be enforced. A call
# that overruns is abandoned (its result is discarded when it finishes).
_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="llm")


class LLMError(Exception):
    """Raised when the LLM provider cannot produce a response."""


class LLMClient:
    """Unified LLM client supporting multiple providers."""

    def __init__(self):
        self.settings = get_settings()
        self.provider = self.settings.llm_provider
        self._client = None

    @property
    def available(self) -> bool:
        """True when an API key for the configured provider is set."""
        return self.settings.llm_configured

    def _extra_body(self) -> Optional[dict]:
        """Parse OPENAI_EXTRA_BODY once; invalid JSON is ignored with a warning."""
        raw = (self.settings.openai_extra_body or "").strip()
        if not raw:
            return None
        try:
            value = json.loads(raw)
        except ValueError:
            logger.warning("OPENAI_EXTRA_BODY is not valid JSON; ignoring it")
            return None
        if not isinstance(value, dict):
            logger.warning("OPENAI_EXTRA_BODY must be a JSON object; ignoring it")
            return None
        return value

    def _get_client(self):
        """Lazy-initialize the provider client."""
        if self._client is not None:
            return self._client

        timeout = self.settings.llm_timeout_seconds
        retries = self.settings.llm_max_retries
        if self.provider == "openai":
            from openai import OpenAI

            self._client = OpenAI(
                api_key=self.settings.get_api_key(),
                base_url=(self.settings.openai_base_url or "").strip() or None,
                timeout=timeout,
                max_retries=retries,
            )
        elif self.provider == "anthropic":
            import anthropic

            self._client = anthropic.Anthropic(
                api_key=self.settings.get_api_key(), timeout=timeout, max_retries=retries
            )
        elif self.provider == "google":
            import google.generativeai as genai

            genai.configure(api_key=self.settings.get_api_key())
            self._client = genai
        else:
            raise ValueError(f"Unsupported LLM provider: {self.provider}")

        return self._client

    # ── Primary API ─────────────────────────────────────────────────────────

    def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        purpose: str,
        corpus_tokens: int,
        json_mode: bool = True,
        max_tokens: int = 900,
        history: Optional[list[dict]] = None,
    ) -> str:
        """Run one completion and return the text.

        Args:
            system_prompt: Instructions only (never corpus content).
            user_prompt: The request, including any retrieved passages.
            purpose: Short label for logs ("plan", "answer", ...).
            corpus_tokens: cl100k_base tokens of corpus text in this call.
            json_mode: Ask the provider for a JSON object where supported.
            max_tokens: Output token cap.
            history: Optional earlier turns [{"role", "content"}].

        Raises:
            LLMError: on provider failure or timeout.
        """
        if corpus_tokens > self.settings.max_corpus_tokens:
            # Hard stop: never send more corpus text than the budget allows.
            raise LLMError(
                f"Refusing LLM call with {corpus_tokens} corpus tokens "
                f"(limit {self.settings.max_corpus_tokens})"
            )

        messages = list(history or []) + [{"role": "user", "content": user_prompt}]
        if self.provider == "openai":
            call = (self._complete_openai, system_prompt, messages, json_mode, max_tokens)
        elif self.provider == "anthropic":
            call = (self._complete_anthropic, system_prompt, messages, max_tokens)
        elif self.provider == "google":
            call = (self._complete_google, system_prompt, messages, json_mode, max_tokens)
        else:
            raise LLMError(f"Unsupported provider: {self.provider}")

        deadline = self.settings.llm_deadline_seconds
        started = time.monotonic()
        try:
            future = _EXECUTOR.submit(*call)
            text = future.result(timeout=deadline)
        except FutureTimeout:
            future.cancel()
            logger.error(
                "LLM call exceeded deadline purpose=%s provider=%s corpus_tokens=%d deadline=%.1fs",
                purpose, self.provider, corpus_tokens, deadline,
            )
            raise LLMError(f"LLM call exceeded {deadline:.0f}s deadline") from None
        except LLMError:
            raise
        except Exception as e:
            logger.error(
                "LLM call failed purpose=%s provider=%s corpus_tokens=%d error=%s",
                purpose, self.provider, corpus_tokens, type(e).__name__,
            )
            raise LLMError(str(e)) from e
        finally:
            elapsed = time.monotonic() - started

        logger.info(
            "LLM call purpose=%s provider=%s model=%s corpus_tokens=%d "
            "prompt_tokens=%d latency=%.2fs",
            purpose,
            self.provider,
            self.settings.get_llm_model(),
            corpus_tokens,
            count_tokens(system_prompt) + sum(count_tokens(m["content"]) for m in messages),
            elapsed,
        )
        return text

    def _complete_openai(self, system_prompt, messages, json_mode, max_tokens) -> str:
        client = self._get_client()
        kwargs = {
            "model": self.settings.get_llm_model(),
            "messages": [{"role": "system", "content": system_prompt}] + messages,
            "temperature": self.settings.llm_temperature,
            "max_tokens": max_tokens,
            "seed": self.settings.llm_seed,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        extra = self._extra_body()
        if extra:
            kwargs["extra_body"] = extra
        response = client.chat.completions.create(**kwargs)
        return response.choices[0].message.content or ""

    def _complete_anthropic(self, system_prompt, messages, max_tokens) -> str:
        client = self._get_client()
        response = client.messages.create(
            model=self.settings.get_llm_model(),
            system=system_prompt,
            messages=messages,
            max_tokens=max_tokens,
            temperature=self.settings.llm_temperature,
        )
        return "".join(b.text for b in response.content if getattr(b, "type", "") == "text")

    def _complete_google(self, system_prompt, messages, json_mode, max_tokens) -> str:
        genai = self._get_client()
        model = genai.GenerativeModel(
            self.settings.get_llm_model(), system_instruction=system_prompt
        )
        contents = [
            {"role": "user" if m["role"] == "user" else "model", "parts": [{"text": m["content"]}]}
            for m in messages
        ]
        config = {"temperature": self.settings.llm_temperature, "max_output_tokens": max_tokens}
        if json_mode:
            config["response_mime_type"] = "application/json"
        response = model.generate_content(
            contents,
            generation_config=config,
            request_options={"timeout": self.settings.llm_timeout_seconds},
        )
        parts = []
        for candidate in response.candidates or []:
            for part in candidate.content.parts:
                if getattr(part, "text", None):
                    parts.append(part.text)
        return "".join(parts)

    # ── Legacy helpers (kept for compatibility) ─────────────────────────────

    def chat_with_tools(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        system_prompt: Optional[str] = None,
    ) -> dict:
        """Send a chat completion request (tools are not used by the orchestrator).

        Returns:
            Dict with keys: content, tool_calls, finish_reason
        """
        history = messages[:-1]
        last = messages[-1]["content"] if messages else ""
        text = self.complete(
            system_prompt or "",
            last,
            purpose="chat",
            corpus_tokens=0,
            json_mode=False,
            history=history,
        )
        return {"content": text, "tool_calls": [], "finish_reason": "stop"}

    def chat_simple(
        self,
        messages: list[dict],
        system_prompt: Optional[str] = None,
    ) -> str:
        """Simple chat completion without tools. Returns just the text content."""
        return self.chat_with_tools(messages, tools=None, system_prompt=system_prompt)["content"]


_THINK_BLOCK = re.compile(r"<think>.*?(</think>|$)", re.DOTALL | re.IGNORECASE)


def parse_json_object(text: str) -> Optional[dict]:
    """Extract and parse a JSON object from LLM response text.

    Reasoning models sometimes wrap the object in prose or ``<think>`` blocks.
    The last decodable object that has a "status" or "answer" key wins;
    otherwise the last decodable object.
    """
    if not text:
        return None
    try:
        value = json.loads(text.strip())
        return value if isinstance(value, dict) else None
    except Exception:
        pass
    cleaned = _THINK_BLOCK.sub("", text)
    decoder = json.JSONDecoder()
    found: list[dict] = []
    pos = cleaned.find("{")
    while pos != -1:
        try:
            value, end = decoder.raw_decode(cleaned, pos)
        except ValueError:
            pos = cleaned.find("{", pos + 1)
            continue
        if isinstance(value, dict):
            found.append(value)
        pos = cleaned.find("{", end)
    if not found:
        return None
    keyed = [v for v in found if "status" in v or "answer" in v]
    return (keyed or found)[-1]
