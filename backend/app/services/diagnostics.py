from __future__ import annotations

import re


_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"(?i)(api[_-]?key|token|password|secret)(\s*[=:]\s*)[^\s,;]+"),
    re.compile(r"(https?://)[^/@\s]+@"),
)


def safe_error_message(value: object, limit: int = 2_000) -> str:
    message = " ".join(str(value).split())
    for pattern in _SECRET_PATTERNS:
        if pattern.pattern.startswith("(https?"):
            message = pattern.sub(r"\1[REDACTED]@", message)
        elif "api[_-]?key" in pattern.pattern:
            message = pattern.sub(r"\1\2[REDACTED]", message)
        elif pattern.pattern.startswith("(?i)\\b(bearer"):
            message = pattern.sub(r"\1[REDACTED]", message)
        else:
            message = pattern.sub("sk-[REDACTED]", message)
    return message[:limit]


__all__ = ["safe_error_message"]
