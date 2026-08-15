"""Declarative config schemas (the ``S`` DSL).

A schemastery-flavoured, dependency-free schema mini-language: declare the
shape of a plugin config, then ``schema.validate(config)`` returns a *copy*
with defaults merged in, or raises :class:`SchemaValidationError` carrying a
dotted path to the offending field.

Semantics (aligned with schemastery's core):

- ``S.object`` keeps unknown keys by default; ``.strict()`` drops them.
- A missing key is filled with ``.default(v)`` when set, omitted when
  ``.optional()``, otherwise it is a required-key error.
- Defaults are deep-copied so they are never shared or mutated.
- ``S.number`` rejects booleans; ``S.union`` tries branches in order.
"""

from __future__ import annotations

import copy
from typing import Any

from xcore.errors import SchemaValidationError

_MISSING = object()


def _path_text(path: tuple[str | int, ...]) -> str:
    return "$" + "".join(
        f"[{part}]" if isinstance(part, int) else f".{part}" for part in path
    )


class Schema:
    """Base class of the schema DSL. Instances are immutable builders."""

    __slots__ = ("_default", "_optional", "_description")

    def __init__(
        self,
        *,
        default: Any = _MISSING,
        optional: bool = False,
        description: str | None = None,
    ) -> None:
        object.__setattr__(self, "_default", default)
        object.__setattr__(self, "_optional", optional)
        object.__setattr__(self, "_description", description)

    # -- validation ---------------------------------------------------------

    def validate(self, config: Any) -> Any:
        """Validate ``config`` and return a validated copy with defaults applied."""
        if config is None:
            return self._apply_missing("")
        return self._validate(config, ())

    def _validate(self, config: Any, path: tuple[str | int, ...]) -> Any:
        raise NotImplementedError

    def _apply_missing(self, path: str) -> Any:
        if self._default is not _MISSING:
            return copy.deepcopy(self._default)
        if self._optional:
            return None
        raise SchemaValidationError(path, "missing required value")

    def _fail(self, path: tuple[str | int, ...], message: str) -> Any:
        raise SchemaValidationError(_path_text(path), message)

    # -- builders -----------------------------------------------------------

    def default(self, value: Any) -> "Schema":
        """Fill missing values with ``value`` (deep-copied on application)."""
        return self._clone(default=value)

    def optional(self) -> "Schema":
        """Allow the value to be absent (resolved to ``None``)."""
        return self._clone(optional=True)

    def description(self, text: str) -> "Schema":
        """Attach a human-readable description (documentation only)."""
        return self._clone(description=text)

    def _clone(self, **overrides: Any) -> "Schema":
        clone = copy.copy(self)
        for key, value in overrides.items():
            object.__setattr__(clone, f"_{key}", value)
        return clone


class AnySchema(Schema):
    __slots__ = ()

    def _validate(self, config: Any, path: tuple[str | int, ...]) -> Any:
        return copy.deepcopy(config)


class ConstSchema(Schema):
    __slots__ = ("_value",)

    def __init__(self, value: Any, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        object.__setattr__(self, "_value", value)

    def _validate(self, config: Any, path: tuple[str | int, ...]) -> Any:
        if config != self._value:
            return self._fail(path, f"expected const {self._value!r} but got {config!r}")
        return copy.deepcopy(self._value)


class StringSchema(Schema):
    __slots__ = ()

    def _validate(self, config: Any, path: tuple[str | int, ...]) -> Any:
        if not isinstance(config, str):
            return self._fail(path, f"expected string but got {type(config).__name__}")
        return config


class NumberSchema(Schema):
    __slots__ = ()

    def _validate(self, config: Any, path: tuple[str | int, ...]) -> Any:
        if isinstance(config, bool) or not isinstance(config, (int, float)):
            return self._fail(path, f"expected number but got {type(config).__name__}")
        return config


class BooleanSchema(Schema):
    __slots__ = ()

    def _validate(self, config: Any, path: tuple[str | int, ...]) -> Any:
        if not isinstance(config, bool):
            return self._fail(path, f"expected boolean but got {type(config).__name__}")
        return config


class ArraySchema(Schema):
    __slots__ = ("_item",)

    def __init__(self, item: Schema, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        if not isinstance(item, Schema):
            raise TypeError("S.array requires a Schema item")
        object.__setattr__(self, "_item", item)

    def _validate(self, config: Any, path: tuple[str | int, ...]) -> Any:
        if not isinstance(config, list):
            return self._fail(path, f"expected array but got {type(config).__name__}")
        return [
            self._item._validate(value, (*path, index))
            for index, value in enumerate(config)
        ]


class ObjectSchema(Schema):
    __slots__ = ("_shape", "_strict")

    def __init__(self, shape: dict[str, Schema], *, strict: bool = False, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        if not isinstance(shape, dict) or not all(
            isinstance(key, str) and isinstance(value, Schema)
            for key, value in shape.items()
        ):
            raise TypeError("S.object requires a {name: Schema} mapping")
        object.__setattr__(self, "_shape", dict(shape))
        object.__setattr__(self, "_strict", strict)

    def strict(self) -> "ObjectSchema":
        """Drop unknown keys instead of keeping them (schemastery semantics)."""
        clone = self._clone(strict=True)
        return clone

    def validate(self, config: Any) -> Any:
        # Koishi plugin convention: an object schema with no config at all
        # validates as an empty object (per-property defaults then apply).
        if config is None:
            config = {}
        return super().validate(config)

    def _validate(self, config: Any, path: tuple[str | int, ...]) -> Any:
        if not isinstance(config, dict):
            return self._fail(path, f"expected object but got {type(config).__name__}")
        result: dict[str, Any] = {}
        for key, schema in self._shape.items():
            if key in config:
                result[key] = schema._validate(config[key], (*path, key))
            else:
                result[key] = schema._apply_missing(_path_text((*path, key)))
        for key, value in config.items():
            if key not in self._shape:
                if self._strict:
                    continue
                result[key] = copy.deepcopy(value)
        return result


class UnionSchema(Schema):
    __slots__ = ("_branches",)

    def __init__(self, branches: list[Schema], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        if not isinstance(branches, list) or not branches or not all(
            isinstance(branch, Schema) for branch in branches
        ):
            raise TypeError("S.union requires a non-empty list of schemas")
        object.__setattr__(self, "_branches", list(branches))

    def _validate(self, config: Any, path: tuple[str | int, ...]) -> Any:
        errors: list[str] = []
        for branch in self._branches:
            try:
                return branch._validate(config, path)
            except SchemaValidationError as exc:
                errors.append(str(exc))
        raise SchemaValidationError(
            _path_text(path), "no union branch matched: " + "; ".join(errors)
        )


class EnumSchema(Schema):
    __slots__ = ("_values",)

    def __init__(self, values: list[Any], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        object.__setattr__(self, "_values", list(values))

    def _validate(self, config: Any, path: tuple[str | int, ...]) -> Any:
        if config not in self._values:
            return self._fail(
                path, f"expected one of {self._values!r} but got {config!r}"
            )
        return copy.deepcopy(config)


class SchemaNamespace:
    """Fluent entry points of the ``S`` DSL."""

    @staticmethod
    def any() -> AnySchema:
        return AnySchema()

    @staticmethod
    def const(value: Any) -> ConstSchema:
        return ConstSchema(value)

    @staticmethod
    def string() -> StringSchema:
        return StringSchema()

    @staticmethod
    def number() -> NumberSchema:
        return NumberSchema()

    @staticmethod
    def boolean() -> BooleanSchema:
        return BooleanSchema()

    @staticmethod
    def array(item: Schema) -> ArraySchema:
        return ArraySchema(item)

    @staticmethod
    def object(shape: dict[str, Schema]) -> ObjectSchema:
        return ObjectSchema(shape)

    @staticmethod
    def union(branches: list[Schema]) -> UnionSchema:
        return UnionSchema(branches)

    @staticmethod
    def enum(values: list[Any]) -> EnumSchema:
        return EnumSchema(values)


S = SchemaNamespace()


def validate_config(
    config_schema: Any, raw_config: Any, default: Any = None
) -> Any:
    """Validate a plugin config against its declared schema.

    ``config_schema`` may be an ``S`` schema (strict validation + defaults), a
    plain dict (loose mode: shallow-merge defaults), or ``None`` (pass-through
    of ``raw_config or default``).
    """
    if isinstance(config_schema, Schema):
        return config_schema.validate(raw_config)
    if isinstance(config_schema, dict):
        merged = dict(config_schema)
        if isinstance(raw_config, dict):
            merged.update(raw_config)
        return merged
    return raw_config if raw_config is not None else default


__all__ = [
    "S",
    "Schema",
    "validate_config",
    "SchemaValidationError",
]
