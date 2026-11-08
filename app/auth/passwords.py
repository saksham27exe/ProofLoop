"""Password hashing with PBKDF2-HMAC-SHA256 (Python standard library only).

Encoded format: ``pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64>``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets

ALGORITHM = "pbkdf2_sha256"
_SALT_BYTES = 16
_USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{2,31}$")


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str, iterations: int) -> str:
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{ALGORITHM}${iterations}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations_text, salt_text, hash_text = encoded.split("$", 3)
        if algorithm != ALGORITHM:
            return False
        iterations = int(iterations_text)
        expected = _unb64(hash_text)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), _unb64(salt_text), iterations)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, expected)


def needs_rehash(encoded: str, iterations: int) -> bool:
    try:
        return int(encoded.split("$")[1]) < iterations
    except (IndexError, ValueError):
        return True


def password_problems(password: str, username: str | None = None) -> list[str]:
    """Return human-readable reasons why ``password`` is too weak (empty list = OK)."""
    problems: list[str] = []
    if len(password) < 8:
        problems.append("must be at least 8 characters long")
    if len(password) > 128:
        problems.append("must be at most 128 characters long")
    if not re.search(r"[A-Za-z]", password):
        problems.append("must contain a letter")
    if not re.search(r"\d", password):
        problems.append("must contain a digit")
    if username and password.lower() == username.lower():
        problems.append("must not equal the username")
    if password.lower() in {"password1", "password123", "12345678a", "qwerty123", "admin1234", "letmein123"}:
        problems.append("is too common")
    return problems


def valid_username(username: str) -> bool:
    return bool(_USERNAME_RE.match(username or ""))
