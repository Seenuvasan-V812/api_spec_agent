"""Structured logging with secret redaction.

Every log record passes through :class:`RedactingFilter`, which masks values
of known secret settings and anything that looks like an API key. Secrets are
registered at config-load time via :func:`register_secret`.
"""

from __future__ import annotations

import logging
import re

_SECRET_VALUES: set[str] = set()

#: Common API-key shapes (Google ``AIza...``, OpenAI ``sk-...``, Anthropic
#: ``sk-ant-...``) — defense in depth beyond registered values.
_KEY_PATTERNS = [
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),
    re.compile(r"sk-ant-[0-9A-Za-z_\-]{20,}"),
    re.compile(r"sk-[0-9A-Za-z_\-]{20,}"),
    re.compile(r"(?i)(api[_-]?key|authorization|token|secret)([\"']?\s*[:=]\s*[\"']?)([^\s\"',;]{8,})"),
]

REDACTED = "***REDACTED***"


def register_secret(value: str | None) -> None:
    """Register a secret value so it is masked in all subsequent log output."""
    if value and len(value) >= 6:
        _SECRET_VALUES.add(value)


def redact(text: str) -> str:
    for secret in _SECRET_VALUES:
        if secret in text:
            text = text.replace(secret, REDACTED)
    for pattern in _KEY_PATTERNS[:3]:
        text = pattern.sub(REDACTED, text)
    text = _KEY_PATTERNS[3].sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - malformed log args must not crash logging
            return True
        redacted = redact(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def configure_logging(verbose: bool = False) -> None:
    root = logging.getLogger("openapi_agent")
    if not any(isinstance(f, RedactingFilter) for f in root.filters):
        root.addFilter(RedactingFilter())
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        handler.addFilter(RedactingFilter())
        root.addHandler(handler)
    root.setLevel(logging.DEBUG if verbose else logging.INFO)


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(f"openapi_agent.{name}")
    if not any(isinstance(f, RedactingFilter) for f in logger.filters):
        logger.addFilter(RedactingFilter())
    return logger
