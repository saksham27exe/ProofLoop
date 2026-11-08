"""Security utilities: redaction of sensitive values and safe path handling."""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Any

REDACTED = "***REDACTED***"

# Keys whose values must never be persisted in traces, prompts or reports.
_SENSITIVE_KEY_RE = re.compile(
    r"(pass(word|wd|phrase)?|secret|token|api[_-]?key|apikey|authori[sz]ation|auth[_-]?header|"
    r"private[_-]?key|ssn|social[_-]?security|credit[_-]?card|card[_-]?number|cvv|cvc|iban|"
    r"connection[_-]?string|conn[_-]?str|cookie|session[_-]?id)",
    re.IGNORECASE,
)

# Value patterns that look like secrets regardless of the key they sit under.
_SENSITIVE_VALUE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bBearer\s+[A-Za-z0-9\-._~+/]{8,}=*", re.IGNORECASE),
    re.compile(r"\b(sk|pk|rk)-[A-Za-z0-9_\-]{16,}\b"),
    re.compile(r"\bnvapi-[A-Za-z0-9_\-]{16,}\b"),  # NVIDIA NIM API keys
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),  # US SSN shape
    re.compile(r"\b(?:\d[ -]?){13,16}\b"),  # payment card shape
    re.compile(r"(?i)\b(postgres(?:ql)?|mysql|mongodb(?:\+srv)?|mssql|redis)://[^\s'\"]+"),
]

MAX_STRING_LENGTH = 4000


def is_sensitive_key(key: str) -> bool:
    return bool(_SENSITIVE_KEY_RE.search(key))


def redact_string(value: str, max_length: int = MAX_STRING_LENGTH) -> str:
    out = value
    for pattern in _SENSITIVE_VALUE_PATTERNS:
        out = pattern.sub(REDACTED, out)
    if len(out) > max_length:
        out = out[:max_length] + f"... [truncated {len(out) - max_length} chars]"
    return out


def redact(obj: Any, max_length: int = MAX_STRING_LENGTH, _depth: int = 0) -> Any:
    """Return a deep copy of ``obj`` with sensitive keys/values replaced.

    Applied to every tool argument / tool response before it is stored in the
    evidence trace, and to anything that could reach an LLM prompt.
    """
    if _depth > 12:
        return "[max depth]"
    if isinstance(obj, dict):
        result: dict[str, Any] = {}
        for k, v in obj.items():
            key = str(k)
            if is_sensitive_key(key):
                result[key] = REDACTED
            else:
                result[key] = redact(v, max_length, _depth + 1)
        return result
    if isinstance(obj, (list, tuple)):
        return [redact(v, max_length, _depth + 1) for v in obj]
    if isinstance(obj, str):
        return redact_string(obj, max_length)
    return obj


class UnsafePathError(ValueError):
    """Raised when a user/agent supplied path would escape its sandbox."""


_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def normalize_relative_path(raw: str) -> str:
    """Normalise an agent/user supplied relative path to POSIX form.

    Rejects absolute paths, drive letters, parent-directory traversal, empty
    segments and reserved device names. Returns e.g. ``reports/summary.txt``.
    """
    if raw is None:
        raise UnsafePathError("Path is required")
    text = str(raw).strip().strip("'\"`").replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    if not text:
        raise UnsafePathError("Path is empty")
    if text.startswith("/") or _WINDOWS_DRIVE_RE.match(text) or text.startswith("~"):
        raise UnsafePathError(f"Absolute paths are not allowed: {raw!r}")
    parts = PurePosixPath(text).parts
    if any(p in ("..", "") for p in parts):
        raise UnsafePathError(f"Path traversal is not allowed: {raw!r}")
    reserved = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
    for part in parts:
        if part.split(".")[0].lower() in reserved:
            raise UnsafePathError(f"Reserved device name in path: {raw!r}")
        if any(ch in part for ch in '<>:"|?*') or any(ord(ch) < 32 for ch in part):
            raise UnsafePathError(f"Illegal character in path: {raw!r}")
    if len(text) > 240:
        raise UnsafePathError("Path is too long")
    return PurePosixPath(*parts).as_posix()


def safe_join(root: Path, relative: str) -> Path:
    """Join ``relative`` onto ``root`` and guarantee the result stays inside it."""
    rel = normalize_relative_path(relative)
    root_resolved = root.resolve()
    candidate = (root_resolved / rel).resolve()
    if candidate != root_resolved and root_resolved not in candidate.parents:
        raise UnsafePathError(f"Path escapes sandbox: {relative!r}")
    return candidate
