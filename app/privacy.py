"""Personal-data protection helpers (Section 7.4).

* ``mask_email``: first letter + ``***@domain``.
* ``sanitize_answer``: last line of defence for model-written text — masks
  e-mail addresses and removes phone numbers before an answer leaves the API.
* ``PIIRedactingFilter``: logging filter that applies the same redaction to
  every log record, so personal data cannot reach the logs even by mistake.
* ``configure_logging``: installs the filter on the root handlers.
"""

import logging
import re

_EMAIL_RE = re.compile(r"\b([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
# Candidate phone numbers: 10+ digits possibly separated by spaces, dots,
# dashes or brackets. Not preceded/followed by word characters or hyphens so
# UUID segments and ISO dates are left alone (see _redact_phone).
_PHONE_CANDIDATE_RE = re.compile(r"(?<![\w-])\+?\(?\d[\d\s().-]{8,}\d(?![\w-])")
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

PHONE_PLACEHOLDER = "[phone withheld]"


def mask_email(email: str) -> str:
    """Return ``f***@domain`` for an e-mail address."""
    local, _, domain = (email or "").partition("@")
    if not local or not domain:
        return "***"
    return f"{local[0]}***@{domain}"


def _mask_email_match(m: re.Match) -> str:
    return f"{m.group(1)}***@{m.group(2)}"


def _redact_phone(m: re.Match) -> str:
    text = m.group(0)
    digits = sum(ch.isdigit() for ch in text)
    if digits < 10:
        return text
    # Leave date ranges such as "2026-11-09 2026-11-13" untouched.
    if _ISO_DATE_RE.fullmatch(text.strip()) or len(_ISO_DATE_RE.findall(text)) * 8 >= digits:
        return text
    return PHONE_PLACEHOLDER


def redact_text(text: str) -> str:
    """Mask e-mails and remove phone numbers from free text."""
    if not text:
        return text
    text = _EMAIL_RE.sub(_mask_email_match, text)
    return _PHONE_CANDIDATE_RE.sub(_redact_phone, text)


def sanitize_answer(text: str) -> str:
    """Apply personal-data redaction to an answer shown to the user."""
    return redact_text(text)


class PIIRedactingFilter(logging.Filter):
    """Redacts e-mails and phone numbers from log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - malformed record, keep as is
            return True
        redacted = redact_text(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def configure_logging(level: str = "INFO") -> None:
    """Configure root logging once, with PII redaction on every handler."""
    root = logging.getLogger()
    if not any(isinstance(f, PIIRedactingFilter) for h in root.handlers for f in h.filters):
        if not root.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(
                logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
            )
            root.addHandler(handler)
        for handler in root.handlers:
            handler.addFilter(PIIRedactingFilter())
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        for handler in logging.getLogger(name).handlers:
            if not any(isinstance(f, PIIRedactingFilter) for f in handler.filters):
                handler.addFilter(PIIRedactingFilter())
    root.setLevel(getattr(logging, (level or "INFO").upper(), logging.INFO))
    # Third-party libraries can log request payloads at DEBUG; keep them quiet.
    for noisy in ("httpx", "httpcore", "openai", "anthropic", "urllib3", "sentence_transformers"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
