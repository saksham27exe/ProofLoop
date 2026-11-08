"""Deterministic before/after comparison and value-matching rules.

No LLM is involved anywhere in this module: equality, counts and checksums
are computed directly.
"""

from __future__ import annotations

import hashlib
from typing import Any

from app.environments.base import FieldSpec, Snapshot
from app.verification.models import FieldDelta, RecordChange, StateDiff


def diff_snapshots(before: Snapshot, after: Snapshot) -> StateDiff:
    changes: list[RecordChange] = []
    for collection in sorted(set(before) | set(after)):
        b = before.get(collection, {})
        a = after.get(collection, {})
        for key in sorted(set(b) | set(a), key=_key_order):
            if key not in a:
                changes.append(RecordChange(collection=collection, key=key, change_type="removed", before=b[key]))
            elif key not in b:
                changes.append(RecordChange(collection=collection, key=key, change_type="added", after=a[key]))
            elif a[key] != b[key]:
                fields = {
                    f: FieldDelta(before=b[key].get(f), after=a[key].get(f))
                    for f in sorted(set(b[key]) | set(a[key]))
                    if b[key].get(f) != a[key].get(f)
                }
                changes.append(
                    RecordChange(collection=collection, key=key, change_type="modified", before=b[key], after=a[key],
                                 changed_fields=fields)
                )
    return StateDiff(changes=changes)


def _key_order(key: str) -> tuple:
    return (0, int(key), "") if key.isdigit() else (1, 0, key)


def values_equal(actual: Any, expected: Any, spec: FieldSpec | None = None) -> bool:
    """Comparison rules (documented in docs/verification_engine.md):

    * ``None`` only equals ``None``.
    * integer fields compare numerically;
    * e-mail and enum fields compare case-insensitively after trimming;
    * text (file content) compares exactly - every byte matters;
    * everything else compares as trimmed strings (case-sensitive).
    """
    if actual is None or expected is None:
        return actual is None and expected is None
    ftype = spec.type if spec else "string"
    if ftype == "text":
        return str(actual) == str(expected)
    if ftype == "integer":
        try:
            return int(actual) == int(expected)
        except (TypeError, ValueError):
            return False
    if ftype == "number":
        try:
            a, e = float(actual), float(expected)
        except (TypeError, ValueError):
            return False
        return abs(a - e) <= 1e-9 * max(1.0, abs(a), abs(e))
    if ftype in ("email", "enum"):
        return str(actual).strip().lower() == str(expected).strip().lower()
    return str(actual).strip() == str(expected).strip()


def content_sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def find_by_match(rows: dict[str, dict[str, Any]], match: dict[str, Any], specs: dict[str, FieldSpec]) -> list[str]:
    return [k for k, row in rows.items() if all(values_equal(row.get(f), v, specs.get(f)) for f, v in match.items())]


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def condition_holds(row: dict[str, Any], field: str, op: str, expected: Any, spec: FieldSpec | None = None) -> bool:
    """Evaluate one selection condition against a snapshot row (verifier semantics, documented):

    ``=``/``!=`` use :func:`values_equal`; ``<``/``>`` compare numerically when both sides are numbers,
    otherwise as strings (ISO-8601 dates order correctly); ``contains`` is a case-insensitive substring test.
    A missing/NULL value never satisfies an ordering or ``contains`` condition.
    """
    actual = row.get(field)
    if op == "=":
        return values_equal(actual, expected, spec)
    if op == "!=":
        return not values_equal(actual, expected, spec)
    if actual is None or expected is None:
        return False
    if op == "contains":
        return str(expected).strip().lower() in str(actual).lower()
    a, e = _as_number(actual), _as_number(expected)
    left, right = (a, e) if a is not None and e is not None else (str(actual), str(expected))
    return {"<": left < right, "<=": left <= right, ">": left > right, ">=": left >= right}.get(op, False)  # type: ignore[operator]


def find_by_conditions(rows: dict[str, dict[str, Any]], conditions: list[Any], specs: dict[str, FieldSpec]) -> list[str]:
    """Keys of rows satisfying *all* conditions (objects with ``field``, ``op`` and ``value``)."""
    return [k for k, row in rows.items()
            if all(condition_holds(row, c.field, c.op, c.value, specs.get(c.field)) for c in conditions)]


def match_score(row: dict[str, Any], expected: dict[str, Any], specs: dict[str, FieldSpec]) -> int:
    return sum(1 for f, v in expected.items() if values_equal(row.get(f), v, specs.get(f)))


def compact_record(record: dict[str, Any] | None, max_len: int = 160) -> dict[str, Any] | None:
    """Shorten long string values for display in reports."""
    if record is None:
        return None
    out: dict[str, Any] = {}
    for k, v in record.items():
        if isinstance(v, str) and len(v) > max_len:
            out[k] = v[:max_len] + f"... ({len(v)} chars)"
        else:
            out[k] = v
    return out
