"""Project JSON Schema properties into simple editor controls."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import JsonValue

FieldKind = Literal["boolean", "integer", "number", "string", "enum", "json"]


@dataclass(frozen=True, slots=True)
class SchemaField:
    name: str
    label: str
    kind: FieldKind
    schema: dict[str, JsonValue]
    nullable: bool = False
    options: tuple[JsonValue, ...] = ()


def schema_fields(schema: dict[str, JsonValue] | None) -> tuple[SchemaField, ...]:
    """Return top-level fields; unsupported shapes remain editable as JSON."""
    if not schema or schema.get("type") != "object":
        return ()
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return ()
    fields = []
    for name, source in properties.items():
        if not isinstance(name, str) or not isinstance(source, dict):
            return ()
        field = dict(source)
        nullable = False
        alternatives = field.pop("anyOf", None)
        if isinstance(alternatives, list):
            non_null = [item for item in alternatives if isinstance(item, dict) and item.get("type") != "null"]
            nulls = [item for item in alternatives if isinstance(item, dict) and item.get("type") == "null"]
            if len(non_null) == 1 and len(non_null) + len(nulls) == len(alternatives):
                field.update(non_null[0])
                nullable = bool(nulls)
            else:
                field = dict(source)
        enum = field.get("enum")
        options: tuple[JsonValue, ...] = ()
        if isinstance(enum, list) and enum and all(value is None or isinstance(value, (str, int, float, bool)) for value in enum):
            kind: FieldKind = "enum"
            options = tuple(enum)
            if nullable and None not in options:
                options += (None,)
        elif field.get("type") in ("boolean", "integer", "number", "string") and not any(key in field for key in ("$ref", "oneOf", "allOf", "not", "items", "anyOf")):
            kind = field["type"]
        else:
            kind = "json"
        fields.append(SchemaField(name, str(field.get("title") or name), kind, field, nullable, options))
    return tuple(fields)
