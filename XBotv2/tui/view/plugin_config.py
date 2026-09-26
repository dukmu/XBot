"""Schema projection for the Textual plugin configuration editor.

Only scalar JSON Schema properties are rendered as controls. A schema the TUI
cannot preserve faithfully is rejected explicitly rather than edited through a
generic JSON escape hatch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic import JsonValue

if TYPE_CHECKING:
    from XBotv2.config import PluginConfigDescriptor

FieldKind = Literal["boolean", "integer", "number", "string", "enum"]


@dataclass(frozen=True, slots=True)
class SchemaField:
    name: str
    label: str
    kind: FieldKind
    schema: dict[str, JsonValue]
    nullable: bool = False
    options: tuple[JsonValue, ...] = ()


@dataclass(frozen=True, slots=True)
class SupportedSchema:
    fields: tuple[SchemaField, ...]


@dataclass(frozen=True, slots=True)
class UnsupportedSchema:
    reason: str


PluginSchema = SupportedSchema | UnsupportedSchema


def plugin_schema(descriptor: PluginConfigDescriptor) -> PluginSchema:
    """Resolve a producer schema to controls the TUI can safely round-trip."""
    if not descriptor.editable:
        return UnsupportedSchema(
            descriptor.unavailable_reason or "This plugin is not editable."
        )
    schema = descriptor.config_schema
    if schema is None or schema.get("type") != "object":
        return UnsupportedSchema("The plugin does not declare an editable object schema.")
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return UnsupportedSchema("The plugin schema has no property declarations.")

    fields: list[SchemaField] = []
    for name, property_schema in properties.items():
        if not isinstance(name, str) or not isinstance(property_schema, dict):
            return UnsupportedSchema("The plugin schema contains an invalid property.")
        field = _schema_field(name, property_schema)
        if isinstance(field, UnsupportedSchema):
            return field
        fields.append(field)
    return SupportedSchema(tuple(fields))


def apply_changes(
    scope_config: dict[str, JsonValue],
    changes: dict[str, JsonValue],
    removals: tuple[str, ...],
) -> dict[str, JsonValue]:
    """Apply only user-edited values to this scope's explicit overlay."""
    result = dict(scope_config)
    for name in removals:
        result.pop(name, None)
    result.update(changes)
    return result


def _schema_field(
    name: str, original: dict[str, JsonValue]
) -> SchemaField | UnsupportedSchema:
    if original.get("writeOnly") is True or original.get("format") == "password":
        return UnsupportedSchema(
            f"{name} is sensitive and has no safe update control."
        )
    if original.get("readOnly") is True:
        return UnsupportedSchema(f"{name} is read-only.")

    schema = dict(original)
    nullable = False
    alternatives = schema.pop("anyOf", None)
    if alternatives is not None:
        if not isinstance(alternatives, list):
            return UnsupportedSchema(f"{name} has an unsupported anyOf schema.")
        values = [item for item in alternatives if isinstance(item, dict)]
        null_values = [item for item in values if item.get("type") == "null"]
        values = [item for item in values if item.get("type") != "null"]
        if len(values) != 1 or len(null_values) + len(values) != len(alternatives):
            return UnsupportedSchema(f"{name} has an unsupported union schema.")
        schema.update(values[0])
        nullable = bool(null_values)

    if any(key in schema for key in ("$ref", "oneOf", "allOf", "not", "items")):
        return UnsupportedSchema(f"{name} uses an unsupported schema construct.")

    enum = schema.get("enum")
    if isinstance(enum, list):
        if not enum or not all(_scalar_option(value) for value in enum):
            return UnsupportedSchema(f"{name} has unsupported enum values.")
        return SchemaField(
            name=name,
            label=str(schema.get("title") or name.replace("_", " ").title()),
            kind="enum",
            schema=schema,
            nullable=nullable,
            options=tuple(enum),
        )

    kind = schema.get("type")
    if kind not in {"boolean", "integer", "number", "string"}:
        return UnsupportedSchema(f"{name} uses unsupported type {kind!r}.")
    return SchemaField(
        name=name,
        label=str(schema.get("title") or name.replace("_", " ").title()),
        kind=kind,
        schema=schema,
        nullable=nullable,
    )


def _scalar_option(value: JsonValue) -> bool:
    return value is None or isinstance(value, (str, bool, int, float))


__all__ = [
    "FieldKind",
    "PluginSchema",
    "SchemaField",
    "SupportedSchema",
    "UnsupportedSchema",
    "apply_changes",
    "plugin_schema",
]
