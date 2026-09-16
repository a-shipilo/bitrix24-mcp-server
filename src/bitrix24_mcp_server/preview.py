"""Human-readable descriptions of pending write operations."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

MAX_VALUE_LENGTH = 300


def format_value(value: Any) -> str:
    if value is None or value in ("", [], {}):
        return "—"
    if isinstance(value, bool):
        text = "да" if value else "нет"
    elif isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False)
    else:
        text = str(value)
    if len(text) > MAX_VALUE_LENGTH:
        text = text[:MAX_VALUE_LENGTH] + "…"
    return text


def upper_snake_to_camel(name: str) -> str:
    """``RESPONSIBLE_ID`` -> ``responsibleId``: request fields are UPPER_CASE, responses are camelCase."""
    head, *tail = name.lower().split("_")
    return head + "".join(part.capitalize() for part in tail)


def lookup(record: Mapping[str, Any], key: str) -> Any:
    if key in record:
        return record[key]
    return record.get(upper_snake_to_camel(key))


def field_lines(fields: Mapping[str, Any]) -> list[str]:
    return [f"{key}: {format_value(value)}" for key, value in fields.items()]


def change_lines(current: Mapping[str, Any], changes: Mapping[str, Any]) -> list[str]:
    return [f"{key}: {format_value(lookup(current, key))} → {format_value(value)}" for key, value in changes.items()]


def build_summary(title: str, portal_url: str, lines: Iterable[str] = ()) -> str:
    return "\n".join([title, f"Портал: {portal_url}", *(f"• {line}" for line in lines)])


def quoted(name: Any) -> str:
    return f" «{name}»" if name else ""
