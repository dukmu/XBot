"""Small helpers for source-delimited synthetic prompts."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from xml.sax.saxutils import escape, quoteattr
from pydantic import JsonValue

_TAG_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
MESSAGE_FORMAT_KEY = "xbotv2_message_format"


def prompt_element(
    name: str,
    content: str,
    *,
    attributes: Mapping[str, JsonValue] | None = None,
) -> str:
    """Render one XML prompt element, escaping all untrusted values."""
    opening = _opening_tag(name, attributes)
    return f"{opening}\n{escape(_xml_text(str(content)))}\n</{name}>"


def prompt_container(
    name: str,
    children: Iterable[str],
    *,
    attributes: Mapping[str, JsonValue] | None = None,
) -> str:
    """Wrap already-rendered prompt elements in a validated container."""
    opening = _opening_tag(name, attributes)
    body = "\n\n".join(child for child in children if child)
    return f"{opening}\n{body}\n</{name}>"


def _opening_tag(
    name: str,
    attributes: Mapping[str, JsonValue] | None,
) -> str:
    if not _TAG_NAME.fullmatch(name):
        raise ValueError(f"Invalid prompt element name: {name!r}")
    rendered = []
    for key, value in sorted((attributes or {}).items()):
        if not _TAG_NAME.fullmatch(key):
            raise ValueError(f"Invalid prompt attribute name: {key!r}")
        if value is not None:
            rendered.append(f"{key}={quoteattr(_xml_text(str(value)))}")
    suffix = f" {' '.join(rendered)}" if rendered else ""
    return f"<{name}{suffix}>"


def _xml_text(value: str) -> str:
    return "".join(
        character if _is_xml_character(ord(character)) else "\ufffd"
        for character in value
    )


def _is_xml_character(codepoint: int) -> bool:
    return (
        codepoint in {0x9, 0xA, 0xD}
        or 0x20 <= codepoint <= 0xD7FF
        or 0xE000 <= codepoint <= 0xFFFD
        or 0x10000 <= codepoint <= 0x10FFFF
    )


__all__ = [
    "MESSAGE_FORMAT_KEY",
    "prompt_container",
    "prompt_element",
]
