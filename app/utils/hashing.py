"""Canonical serialisation and hashing helpers.

Every piece of evidence, every trace event and every report is hashed over a
*canonical* JSON representation (sorted keys, no insignificant whitespace) so
that the same logical content always produces the same SHA-256 digest.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any


def _default(obj: Any) -> Any:
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, Path):
        return obj.as_posix()
    if isinstance(obj, (set, frozenset)):
        return sorted(obj, key=str)
    if isinstance(obj, bytes):
        return obj.hex()
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        return dump(mode="json")
    return str(obj)


def canonical_json(obj: Any) -> str:
    """Deterministic JSON encoding used for hashing."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_default)


def sha256_hex(data: str | bytes) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def hash_obj(obj: Any) -> str:
    """SHA-256 of the canonical JSON form of ``obj``."""
    return sha256_hex(canonical_json(obj))


def to_jsonable(obj: Any) -> Any:
    """Round-trip through canonical JSON to obtain plain JSON-compatible data."""
    return json.loads(canonical_json(obj))
