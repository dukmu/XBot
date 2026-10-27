"""Resolve coretools filesystem calls to physical operations."""

from pydantic import JsonValue

_MERGED_TOOL_OPERATIONS = {
    "read": {
        "utf8": "read",
        "binary": "read_bytes",
        "stat": "stat",
        "media": "read_bytes",
        "list": "list",
    },
    "edit": {
        "write": "write",
        "replace": "edit",
        "patch": "patch",
    },
    "path": {
        "move": "move",
        "copy": "copy",
        "delete": "delete",
        "mkdir": "mkdir",
    },
    "search": {
        "content": "search",
        "name": "find",
    },
}
_MERGED_TOOL_DEFAULTS = {
    "read": "utf8",
    "edit": "replace",
    "path": "mkdir",
    "search": "content",
}


def resolve_operation(tool_name: str, args: dict[str, JsonValue]) -> str | None:
    """Resolve the concrete filesystem operation one tool call performs.

    Merged tools (``read`` / ``edit`` / ``path`` / ``search``) select the
    backend operation from their ``mode`` / ``operation`` argument, so
    permission path checks must resolve per call instead of per tool name.
    """
    modes = _MERGED_TOOL_OPERATIONS.get(tool_name)
    if modes is not None:
        selector = "operation" if tool_name == "path" else "mode"
        return modes.get(
            str(args.get(selector) or ""),
            modes[_MERGED_TOOL_DEFAULTS[tool_name]],
        )
    return None

