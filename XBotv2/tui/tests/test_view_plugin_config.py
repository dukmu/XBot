"""JSON schema control selection is independent of resource identity."""

from XBotv2.tui.impl.view.plugin_config import schema_fields
from XBotv2.tui.impl.view.settings import _parse_value
import pytest


def test_schema_fields_include_scalars_and_complex_json():
    fields = schema_fields({"type": "object", "properties": {
        "enabled": {"type": "boolean"},
        "count": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        "mode": {"type": "string", "enum": ["a", "b"]},
        "items": {"type": "array", "items": {"type": "object"}},
    }})
    assert [(field.name, field.kind) for field in fields] == [
        ("enabled", "boolean"), ("count", "integer"), ("mode", "enum"), ("items", "json")
    ]
    assert fields[1].nullable
    assert _parse_value(fields[3], '[{"x": true}]') == [{"x": True}]
    with pytest.raises(ValueError):
        _parse_value(fields[3], '[NaN]')
