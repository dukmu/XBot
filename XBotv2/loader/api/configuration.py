"""Schema validation shared by configuration editors."""
from typing import Any
from pydantic import BaseModel

def validate_plugin_config(schema: Any, raw_config: Any) -> Any:
    """Validate an explicit configuration edit against the producer's schema.

    Activation validation belongs to XCore; generic tree reads do not call
    this across every plugin, which would defeat activation failure isolation.
    """
    if isinstance(schema, type) and issubclass(schema, BaseModel):
        return schema.model_validate(raw_config or {})
    return raw_config


def plugin_config_json_schema(schema: Any) -> dict[str, Any]:
    """Project a plugin-owned Pydantic model into standard JSON Schema."""
    if isinstance(schema, type) and issubclass(schema, BaseModel):
        return schema.model_json_schema(mode="serialization")
    raise TypeError("plugin does not declare a Pydantic config model")


