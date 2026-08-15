"""Schema DSL tests: validation, defaults, error paths."""

from __future__ import annotations

import pytest

from xcore import S, SchemaValidationError


def test_string_and_number():
    schema = S.object({
        "name": S.string(),
        "count": S.number(),
    })
    result = schema.validate({"name": "x", "count": 3})
    assert result == {"name": "x", "count": 3}
    with pytest.raises(SchemaValidationError) as excinfo:
        schema.validate({"name": 5, "count": 3})
    assert "$.name" in str(excinfo.value)
    # bool is not a number
    with pytest.raises(SchemaValidationError):
        S.number().validate(True)


def test_defaults_are_applied_and_deep_copied():
    schema = S.object({
        "mode": S.string().default("auto"),
        "opts": S.object({
            "retries": S.number().default(3),
        }).default({"retries": 1}),
    })
    result = schema.validate({})
    assert result == {"mode": "auto", "opts": {"retries": 1}}
    result["opts"]["retries"] = 99
    again = schema.validate({})
    assert again["opts"]["retries"] == 1  # defaults never shared/mutated


def test_required_missing_raises_with_path():
    schema = S.object({"name": S.string()})
    with pytest.raises(SchemaValidationError) as excinfo:
        schema.validate({})
    assert "$.name" in str(excinfo.value)


def test_optional_omits_or_none():
    schema = S.object({
        "required": S.string(),
        "optional": S.string().optional(),
    })
    result = schema.validate({"required": "x"})
    assert "optional" not in result or result["optional"] is None


def test_object_keeps_unknown_keys_by_default():
    result = S.object({"a": S.string()}).validate({"a": "x", "extra": 1})
    assert result == {"a": "x", "extra": 1}


def test_object_strict_drops_unknown_keys():
    schema = S.object({"a": S.string()}).strict()
    assert schema.validate({"a": "x", "extra": 1}) == {"a": "x"}


def test_array_recurses_with_index_path():
    schema = S.array(S.number())
    assert schema.validate([1, 2, 3]) == [1, 2, 3]
    with pytest.raises(SchemaValidationError) as excinfo:
        schema.validate([1, "x"])
    assert "$[1]" in str(excinfo.value)


def test_union_tries_branches_in_order():
    schema = S.union([S.number(), S.string()])
    assert schema.validate("x") == "x"
    assert schema.validate(5) == 5
    with pytest.raises(SchemaValidationError):
        schema.validate([])


def test_enum_and_const():
    assert S.enum(["a", "b"]).validate("a") == "a"
    with pytest.raises(SchemaValidationError):
        S.enum(["a", "b"]).validate("c")
    assert S.const("fixed").validate("fixed") == "fixed"
    with pytest.raises(SchemaValidationError):
        S.const("fixed").validate("other")


def test_any_accepts_everything():
    assert S.any().validate({"anything": [1, None]}) == {"anything": [1, None]}


def test_validate_config_loose_dict_mode():
    from xcore.schema import validate_config

    assert validate_config({"a": 1}, {"b": 2}) == {"a": 1, "b": 2}
    assert validate_config(None, {"b": 2}) == {"b": 2}
    assert validate_config(None, None, default={"x": 1}) == {"x": 1}


def test_schema_immutability_of_builders():
    base = S.string()
    with_default = base.default("d")
    assert base.validate("v") == "v"
    with pytest.raises(SchemaValidationError):
        base.validate(None)  # base was not mutated
    assert with_default.validate(None) == "d"


def test_optional_on_whole_schema():
    schema = S.number().optional()
    assert schema.validate(None) is None
