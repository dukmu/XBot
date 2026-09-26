"""Only producer schemas the TUI can preserve faithfully may be edited."""

from types import SimpleNamespace
from XBotv2.tui.view.plugin_config import (
    SupportedSchema,
    UnsupportedSchema,
    apply_changes,
    plugin_schema,
)


def test_schema_projects_supported_scalars_and_nullable_values() -> None:
    descriptor = SimpleNamespace(
        plugin_id="sample",
        name="Sample",
        editable=True,
        config_schema={
            "type": "object",
            "properties": {
                "enabled": {"type": "boolean", "default": True},
                "workers": {
                    "anyOf": [{"type": "integer"}, {"type": "null"}],
                    "default": None,
                },
                "mode": {"type": "string", "enum": ["fast", "safe"]},
            },
        },
    )

    result = plugin_schema(descriptor)

    assert isinstance(result, SupportedSchema)
    assert [(field.name, field.kind) for field in result.fields] == [
        ("enabled", "boolean"),
        ("workers", "integer"),
        ("mode", "enum"),
    ]
    workers = result.fields[1]
    assert workers.nullable is True
    assert result.fields[2].options == ("fast", "safe")


def test_nested_and_sensitive_schema_is_explicitly_unsupported() -> None:
    for property_schema, expected in (
        ({"type": "array", "items": {"type": "string"}}, "unsupported"),
        ({"type": "string", "writeOnly": True}, "sensitive"),
    ):
        descriptor = SimpleNamespace(
            plugin_id="sample",
            name="Sample",
            editable=True,
            config_schema={
                "type": "object",
                "properties": {"value": property_schema},
            },
        )

        result = plugin_schema(descriptor)

        assert isinstance(result, UnsupportedSchema)
        assert expected in result.reason


def test_patch_merge_preserves_only_this_scopes_explicit_values() -> None:
    assert apply_changes(
        {"keep": 2},
        {"enabled": False},
        ("remove_me",),
    ) == {"keep": 2, "enabled": False}
