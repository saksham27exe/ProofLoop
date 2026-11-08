"""Deterministic rule-based task interpreter.

Used when no local LLM is available ("verification-only mode"), for
reproducible benchmarks, and as the planner of the scripted sample agent.
It understands a documented grammar of common instructions, for example::

    Update the email address of employee ID 1042 to new@example.com
    Set customer 105 phone number to +1-555-0199 and tier to gold
    Create a new employee record with name John Doe and department Engineering
    Delete customer 112
    Create a ticket titled 'VPN drops hourly' with priority high
    Close ticket 12 / Assign ticket 4 to Priya Sharma / Mark ticket 9 as in progress
    Create a file reports/summary.txt with content 'Q3 done'
    Overwrite the file notes/todo.md with 'all done'
    Delete the file logs/agent.log

Anything it cannot parse is reported as an *ambiguity* - it never guesses.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.environments.base import EntitySpec, EnvironmentSchema
from app.interpretation.verification_plan import Condition, InterpretationDraft

_QUOTE_RE = re.compile(r"'([^']*)'|\"([^\"]*)\"|“([^”]*)”|‘([^’]*)’|`([^`]*)`")
_PLACEHOLDER_RE = re.compile(r"__Q(\d+)__")

_VERB_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("DELETE", re.compile(r"\b(delete|remove|erase|drop|purge|get\s+rid\s+of)\b", re.I)),
    ("UPDATE", re.compile(r"\b(update|change|set|modify|edit|rename|assign|reassign|close|resolve|reopen|mark|"
                          r"overwrite|replace|correct)\b", re.I)),
    # A bare "new" only signals creation at the start ("New employee: ..."); in "its new phone is ..." it
    # describes a value and must not turn an update into a create.
    ("CREATE", re.compile(r"\b(create|add|insert|register|onboard|write|save)\b|^\s*new\b", re.I)),
    ("READ", re.compile(r"\b(read|show|get(?!\s+rid\s+of)|fetch|look\s+up|lookup|view|display|retrieve)\b", re.I)),
]
# Inflected destructive verbs ("get customer 112 deleted") only count towards conflict detection.
_DESTRUCTIVE_INFLECTED_RE = re.compile(r"\b(deleted|deleting|removed|removing|erased|erasing|dropped|dropping|purged|purging)\b", re.I)
_OPEN_TICKET_RE = re.compile(r"^\s*(open|raise|file|log|submit)\s+(a|an)\b", re.I)
_APPEND_RE = re.compile(r"\bappend\b", re.I)
_SEPARATOR_BEFORE_FIELD = r"(?:^|,|;|\band\b|\bwith\b|\bhaving\b|\bset\b|\bsetting\b|\bits\b|\bthe\b|\bplus\b|\balso\b|'s\b|’s\b)"
_TRAILING_CLAUSE_RE = re.compile(
    r"\s*[,;]\s*(?=(?:please|thanks|thank\s+you|update|change|set|do|make|apply|save|and\s+then|then|asap|now)\b)",
    re.I,
)
# Sentence end after a word of 3+ characters (so initials such as "Priya S. Sharma" survive), or a spaced dash.
_SENTENCE_BREAK_RE = re.compile(r"(?<=[A-Za-z0-9]{3})[.!?]\s+(?=\S)|\s+(?:--|—|–)\s+")
_TRAILING_JUNK_RE = re.compile(r"(?:\s*(?:,|;|\band\b|\bwith\b|\bhaving\b|\bplus\b|\balso\b|\bthe\b))+\s*$", re.I)
_PATH_TOKEN_RE = re.compile(r"(?<![@\w])((?:[\w\-]+/)*[\w\-]+\.[A-Za-z0-9]{1,8})(?![\w@])")
_NAME_RE = r"(__Q\d+__|[A-Z][\w'’.\-]*(?:\s+[A-Z][\w'’.\-]*)*)"
_CONNECTOR_RE = re.compile(r"^\s*(?:(?:to|as|of|is)\b|=|:|->)\s*", re.I)


@dataclass
class _Ctx:
    original: str
    text: str  # with quoted segments replaced by placeholders
    quotes: list[str] = field(default_factory=list)

    def restore(self, value: str) -> str:
        return _PLACEHOLDER_RE.sub(lambda m: self.quotes[int(m.group(1))], value)


def _prepare(instruction: str) -> _Ctx:
    text = " ".join(instruction.strip().split())
    quotes: list[str] = []

    def _sub(match: re.Match[str]) -> str:
        quotes.append(next(g for g in match.groups() if g is not None))
        return f"__Q{len(quotes) - 1}__"

    text = _QUOTE_RE.sub(_sub, text)
    text = re.sub(r"[.!?]+$", "", text).strip()
    return _Ctx(original=instruction, text=text, quotes=quotes)


def _clean_value(ctx: _Ctx, raw: str) -> str:
    # "+1-555-0177, update it" / "gold, thanks": a trailing clause after a comma is not part of the value.
    raw = _TRAILING_CLAUSE_RE.split(raw, maxsplit=1)[0]
    # "Dana Fox. Also, delete ticket 2" / "Sales -- actually ...": an unquoted value ends at the sentence or dash
    # break; anything after it is another sentence, never part of the value (quote values that contain them).
    raw = _SENTENCE_BREAK_RE.split(raw, maxsplit=1)[0]
    value = _TRAILING_JUNK_RE.sub("", raw.strip()).strip()
    value = re.sub(r"[.,;]+$", "", value).strip()
    is_quoted = bool(re.fullmatch(r"__Q\d+__", value))
    value = ctx.restore(value)
    return value if is_quoted else value.strip().strip("'\"")


def _detect_operation(ctx: _Ctx) -> str | None:
    if _OPEN_TICKET_RE.search(ctx.text):
        return "CREATE"
    best: tuple[int, str] | None = None
    for op, pattern in _VERB_PATTERNS:
        m = pattern.search(ctx.text)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), op)
    return best[1] if best else None


def _mentioned_operations(ctx: _Ctx) -> set[str]:
    """Every kind of operation the (unquoted) instruction mentions - more than one means it is not a single task."""
    ops = {op for op, pattern in _VERB_PATTERNS if pattern.search(ctx.text)}
    if _OPEN_TICKET_RE.search(ctx.text):
        ops.discard("READ")
        ops.add("CREATE")
    if _DESTRUCTIVE_INFLECTED_RE.search(ctx.text):
        ops.add("DELETE")
    return ops


def _entity_aliases(entity: EntitySpec) -> list[str]:
    names = {entity.name, entity.collection, entity.display_name.lower(), *[a.lower() for a in entity.aliases]}
    return sorted(names, key=len, reverse=True)


def _detect_entity(ctx: _Ctx, schema: EnvironmentSchema) -> tuple[EntitySpec | None, re.Match[str] | None]:
    best: tuple[int, EntitySpec, re.Match[str]] | None = None
    for entity in schema.entities:
        alt = "|".join(re.escape(a) for a in _entity_aliases(entity))
        m = re.search(rf"\b({alt})\b", ctx.text, re.I)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), entity, m)
    if best:
        return best[1], best[2]
    if len(schema.entities) == 1:
        return schema.entities[0], None
    return None, None


def _field_phrases(entity: EntitySpec) -> dict[str, str]:
    phrases: dict[str, str] = {}
    for f in entity.fields:
        if f.name in entity.managed_fields or f.name == entity.key_field:
            continue
        for phrase in {f.name.replace("_", " "), f.name, *f.synonyms}:
            phrases.setdefault(phrase.lower(), f.name)
    return phrases


def _phrase_alt(phrases: dict[str, str]) -> str:
    return "|".join(re.escape(p) for p in sorted(phrases, key=len, reverse=True))


def _extract_record_target(ctx: _Ctx, entity: EntitySpec) -> tuple[str | None, dict[str, str], re.Match[str] | None]:
    alias_alt = "|".join(re.escape(a) for a in _entity_aliases(entity))
    m = re.search(
        rf"\b(?:{alias_alt})\b(?:\s+(?:record|entry|account))?\s*(?:id|ID|Id|#|no\.?|number)?\s*[:#]?\s*(\d+)\b",
        ctx.text, re.I,
    )
    if m:
        return m.group(1), {}, m
    m = re.search(r"(?:\bid\b|#)\s*[:#]?\s*(\d+)\b", ctx.text, re.I)
    if m:
        return m.group(1), {}, m
    m = re.search(rf"\b(?:named|called|with\s+(?:the\s+)?name)\s+{_NAME_RE}", ctx.text)
    if m:
        return None, {entity.label_field: ctx.restore(m.group(1))}, m
    m = re.search(rf"{_NAME_RE}(?:'s|’s)\b", ctx.text)
    if m and m.group(1).lower() not in _entity_aliases(entity):
        return None, {entity.label_field: ctx.restore(m.group(1))}, m
    return None, {}, None


def _assignments(ctx: _Ctx, segment: str, phrases: dict[str, str]) -> dict[str, str]:
    """Parse ``field [to|=|:|as] value`` pairs separated by and/with/commas."""
    if not segment.strip():
        return {}
    alt = _phrase_alt(phrases)
    pattern = re.compile(rf"{_SEPARATOR_BEFORE_FIELD}\s*(?:(?:a|an|new)\s+)?\b(?P<field>{alt})\b", re.I)
    hits = list(pattern.finditer(segment))
    values: dict[str, str] = {}
    for i, hit in enumerate(hits):
        end = hits[i + 1].start() if i + 1 < len(hits) else len(segment)
        raw = _CONNECTOR_RE.sub("", segment[hit.end():end], count=1)
        value = _clean_value(ctx, raw)
        if value:
            values.setdefault(phrases[hit.group("field").lower()], value)
    return values


def _field_first_assignments(ctx: _Ctx, phrases: dict[str, str]) -> dict[str, str] | None:
    """``<verb> the f1 [and f2] of <target> to v1 [and v2]``."""
    alt = _phrase_alt(phrases)
    m = re.search(
        rf"\b(?:the\s+|its\s+)?(?P<fields>(?:{alt})(?:\s*(?:,|and)\s*(?:the\s+)?(?:{alt}))*)\s+(?:of|for)\s+"
        rf"(?P<tpart>.+?)\s+to\s+(?P<values>.+)$",
        ctx.text, re.I,
    )
    if not m:
        return None
    field_names = [phrases[f.strip().lower()] for f in re.split(r"\s*(?:,|\band\b)\s*(?:the\s+)?", m.group("fields"), flags=re.I) if f.strip()]
    raw_values = m.group("values")
    extra: dict[str, str] = {}
    # "... to v1 and phone to v2": a trailing assignment list after the first value.
    tail = re.search(rf"\s+and\s+(?:the\s+|its\s+)?(?:{alt})\s*(?:(?:to|as|is)\b|=|:)", raw_values, re.I)
    if tail and len(field_names) == 1:
        extra = _assignments(ctx, raw_values[tail.start():], phrases)
        raw_values = raw_values[: tail.start()]
    if len(field_names) == 1:
        parts = [raw_values]
    else:
        parts = [p for p in re.split(r"\s*,\s*(?:and\s+)?|\s+and\s+", raw_values) if p.strip()]
        if len(parts) != len(field_names):
            return {"__mismatch__": "x"}
    result = {f: _clean_value(ctx, v) for f, v in zip(field_names, parts, strict=True)}
    for k, v in extra.items():
        result.setdefault(k, v)
    return result


def _ticket_verbs(ctx: _Ctx, entity: EntitySpec) -> dict[str, str]:
    values: dict[str, str] = {}
    if not entity.field("status"):
        return values
    t = ctx.text
    if re.match(r"^\s*close\b", t, re.I):
        values["status"] = "closed"
    elif re.match(r"^\s*resolve\b", t, re.I):
        values["status"] = "resolved"
    elif re.match(r"^\s*reopen\b", t, re.I):
        values["status"] = "open"
    m = re.match(r"^\s*mark\b.*?\bas\s+(.+?)(?:\s+and\b|$)", t, re.I)
    if m:
        values["status"] = _clean_value(ctx, m.group(1))
    m = re.match(r"^\s*(?:re)?assign\b.*?\bto\s+(.+?)(?:\s+(?:and|with)\b|$)", t, re.I)
    if m and entity.field("assignee"):
        values["assignee"] = _clean_value(ctx, m.group(1))
    m = re.search(r"\b(low|medium|high|critical)[\s-]+priority\b", t, re.I)
    if m and entity.field("priority"):
        values["priority"] = m.group(1).lower()
    return values


def _parse_file(ctx: _Ctx, op: str | None, draft: InterpretationDraft) -> None:
    t = ctx.text
    path: str | None = None
    path_placeholder: str | None = None
    for i, q in enumerate(ctx.quotes):
        if ("/" in q or re.fullmatch(r"[\w\-. ]+\.[A-Za-z0-9]{1,8}", q)) and "\n" not in q and len(q) < 200 and " " not in q.strip():
            if f"__Q{i}__" in t:
                path, path_placeholder = q, f"__Q{i}__"
                break
    if path is None:
        m = _PATH_TOKEN_RE.search(t)
        if m:
            path = m.group(1)
    if path:
        draft.target_id = path
    else:
        draft.ambiguities.append("No file path was found in the instruction.")
    if op in ("DELETE", "READ") or op is None:
        return
    content: str | None = None
    text_wo_path = t.replace(path_placeholder, " ") if path_placeholder else t
    m = re.search(r"\b(?:with\s+(?:the\s+)?(?:content|contents|text|body)|containing|that\s+says|saying)\s*(?::|of)?\s*(.+)$", text_wo_path, re.I)
    if m:
        content = _clean_value(ctx, m.group(1))
    else:
        m = re.search(r"\b(?:write|save|put)\s+(.+?)\s+(?:to|into|in)\s+(?:the\s+)?(?:new\s+)?(?:file\b)?", text_wo_path, re.I)
        if m:
            content = _clean_value(ctx, m.group(1))
        else:
            m = re.search(r"\bwith\s+(__Q\d+__)", text_wo_path)
            if m:
                content = ctx.restore(m.group(1))
    if content is None:
        draft.ambiguities.append("The expected file content was not stated.")
    else:
        draft.values["content"] = content
    if re.search(r"\b(write|save|put|overwrite|replace)\b", t, re.I):
        draft.allow_overwrite = True


_CMP_WORDS = [
    (r"(?:is\s+)?(?:greater|more|higher)\s+than|is\s+above|is\s+over|>", ">"),
    (r"(?:is\s+)?(?:less|lower|fewer)\s+than|is\s+below|is\s+under|<", "<"),
    (r"contains|includes", "contains"),
    (r"is\s+not|!=|<>", "!="),
    (r"is|equals|=|of", "="),
]


def _parse_bulk(ctx: _Ctx, op: str | None, entity: EntitySpec, phrases: dict[str, str], draft: InterpretationDraft) -> None:
    """Strict grammar for tasks on every matching record, e.g.

    * ``Set the tier of all customers where city is Oslo to gold``
    * ``Set tier to gold for all customers where city is Oslo``
    * ``Delete all orders where status is cancelled``

    Anything else that talks about "all/every" records is refused (left to the local LLM).
    """
    draft.target_scope = "all"
    if op not in ("UPDATE", "DELETE"):
        draft.ambiguities.append("Only updates and deletes of all matching records are understood by the rule-based interpreter.")
        return
    alt = _phrase_alt(phrases)
    alias_alt = "|".join(re.escape(a) for a in _entity_aliases(entity))
    t = ctx.text
    sel = re.search(rf"\b(?:all|every|each)\s+(?:of\s+the\s+)?(?:{alias_alt})\s+(?:where|whose|with|having)\s+(?P<cond>.+)$", t, re.I)
    if not sel:
        draft.ambiguities.append("State which records are meant, e.g. 'all customers where city is Oslo'.")
        return
    cond_text = sel.group("cond")
    values: dict[str, str] = {}
    if op == "UPDATE":
        # "<field> to <value> for all ... where <cond>" or "... where <cond> to <value>" (field named before 'of all').
        head = t[: sel.start()]
        m = re.search(rf"\b(?:the\s+)?(?P<field>{alt})\s+(?:to|=|as)\s+(?P<value>.+?)\s+(?:for|of|on)\s*$", head, re.I)
        if m:
            values[phrases[m.group("field").lower()]] = _clean_value(ctx, m.group("value"))
        else:
            m = re.search(rf"\b(?:the\s+)?(?P<field>{alt})\s+of\s*$", head, re.I)
            tail = re.search(r"\s+to\s+(?P<value>.+)$", cond_text, re.I)
            if m and tail:
                values[phrases[m.group("field").lower()]] = _clean_value(ctx, tail.group("value"))
                cond_text = cond_text[: tail.start()]
        if not values:
            draft.ambiguities.append("No new value was found for the matching records.")
            return
        draft.values = values
    for part in re.split(r"\s+and\s+", cond_text, flags=re.I):
        m = re.match(rf"^\s*(?:the\s+)?(?P<field>{alt})\s+(?P<rest>.+)$", part, re.I)
        if not m:
            draft.ambiguities.append(f"Could not read the condition {ctx.restore(part.strip())!r}.")
            return
        rest = m.group("rest")
        for pattern, sym in _CMP_WORDS:
            cm = re.match(rf"^(?:{pattern})\s+(?P<value>.+)$", rest, re.I)
            if cm:
                draft.conditions.append(Condition(field=phrases[m.group("field").lower()], op=sym,  # type: ignore[arg-type]
                                                  value=_clean_value(ctx, cm.group("value"))))
                break
        else:
            draft.ambiguities.append(f"Could not read the comparison in {ctx.restore(part.strip())!r}.")
            return


def parse_instruction(instruction: str, schema: EnvironmentSchema) -> InterpretationDraft:
    """Interpret ``instruction`` against ``schema`` using deterministic rules."""
    draft = InterpretationDraft()
    if not instruction or not instruction.strip():
        draft.ambiguities.append("The instruction is empty.")
        return draft
    ctx = _prepare(instruction)
    if _APPEND_RE.search(ctx.text):
        draft.ambiguities.append("Append operations are not supported by the rule-based interpreter.")
        return draft
    op = _detect_operation(ctx)
    draft.operation = op  # type: ignore[assignment]
    if op is None:
        draft.ambiguities.append("Could not determine the intended operation (create, update, delete or read).")
    mentioned = _mentioned_operations(ctx)
    if len(mentioned) > 1:
        draft.ambiguities.append(
            f"The instruction mentions several operations ({', '.join(sorted(mentioned))}); "
            "the rule-based interpreter only accepts one task per instruction."
        )

    entity, entity_match = _detect_entity(ctx, schema)
    if entity is None:
        names = ", ".join(e.name for e in schema.entities)
        draft.ambiguities.append(f"Could not determine which kind of record is meant ({names}).")
        return draft
    draft.entity = entity.name

    if entity.key_type == "path":
        _parse_file(ctx, op, draft)
        return draft

    phrases = _field_phrases(entity)
    alias_alt = "|".join(re.escape(a) for a in _entity_aliases(entity))
    if re.search(rf"\b(all|every|each)\s+(?:of\s+the\s+)?(?:{alias_alt})\b", ctx.text, re.I):
        _parse_bulk(ctx, op, entity, phrases, draft)
        return draft
    if op in ("UPDATE", "DELETE", "READ"):
        key, match, target_m = _extract_record_target(ctx, entity)
        draft.target_id = key
        draft.target_match = match
        if key is None and not match:
            draft.ambiguities.append(f"No target {entity.display_name.lower()} (id or name) was identified.")
        if op == "UPDATE":
            values = _ticket_verbs(ctx, entity)
            ff = _field_first_assignments(ctx, phrases)
            if ff and "__mismatch__" in ff:
                draft.ambiguities.append("The number of fields and values in the instruction do not match.")
            elif ff:
                values.update(ff)
            else:
                segment = ctx.text[target_m.end():] if target_m else ctx.text
                values.update({k: v for k, v in _assignments(ctx, segment, phrases).items() if k not in values})
            # A name used to *identify* the target is not a value to set.
            for k in list(values):
                if k in match and values[k] == match[k]:
                    values.pop(k)
            draft.values = values
            if not values:
                draft.ambiguities.append("No field values to change were found in the instruction.")
    elif op == "CREATE":
        segment = ctx.text[entity_match.end():] if entity_match else ctx.text
        values = _ticket_verbs(ctx, entity)
        values.update({k: v for k, v in _assignments(ctx, segment, phrases).items() if k not in values})
        draft.values = values
        if not values:
            draft.ambiguities.append("No field values for the new record were found in the instruction.")
    return draft
