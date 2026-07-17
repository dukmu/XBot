"""Small helpers for source-delimited synthetic prompts."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from xml.etree import ElementTree
from xml.sax.saxutils import escape, quoteattr

_TAG_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
CACHED_CONTENT_KEY = "xbotv2_cached_content"
DISPLAY_CONTENT_KEY = "xbotv2_display_content"
MESSAGE_FORMAT_KEY = "xbotv2_message_format"


def prompt_element(
    name: str,
    content: str,
    *,
    attributes: Mapping[str, object] | None = None,
) -> str:
    """Render one XML prompt element, escaping all untrusted values."""
    opening = _opening_tag(name, attributes)
    return f"{opening}\n{escape(_xml_text(str(content)))}\n</{name}>"


def prompt_container(
    name: str,
    children: Iterable[str],
    *,
    attributes: Mapping[str, object] | None = None,
) -> str:
    """Wrap already-rendered prompt elements in a validated container."""
    opening = _opening_tag(name, attributes)
    body = "\n\n".join(child for child in children if child)
    return f"{opening}\n{body}\n</{name}>"


def cached_content_prompt(
    *,
    kind: str,
    cache_path: str,
    original_chars: int,
    omitted_chars: int,
    beginning: str,
    ending: str,
    sha256: str | None = None,
    inline_limit_chars: int | None = None,
) -> str:
    """Render one cache reference without exposing raw text as markup."""
    metadata = {
        "kind": kind,
        "original_chars": original_chars,
        "omitted_chars": omitted_chars,
        "inline_limit_chars": inline_limit_chars,
    }
    children = [prompt_element("cache_path", cache_path)]
    if sha256:
        children.append(prompt_element("sha256", sha256))
    children.extend([
        prompt_container(
            "preview",
            [
                prompt_element("beginning", beginning),
                prompt_element("ending", ending),
            ],
        ),
        prompt_element(
            "read_instruction",
            "Read the cached file with filesystem_read using offset and limit "
            "before acting when omitted content may matter.",
        ),
    ])
    return prompt_container("cached_content", children, attributes=metadata)


def tool_result_display_content(content: str) -> str:
    """Extract client-facing text from a structured Tool result."""
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError:
        return content
    if root.tag != "tool_result":
        return content
    text = root.findtext("content")
    if text is not None:
        return _rendered_element_text(text)
    cached = root.find("cached_content")
    if cached is not None:
        path = (cached.findtext("cache_path") or "").strip()
        original_chars = cached.attrib.get("original_chars", "unknown")
        return f"Tool result cached at {path} ({original_chars} characters)."
    data = root.findtext("data")
    return _rendered_element_text(data) if data is not None else ""


def _rendered_element_text(value: str) -> str:
    if value.startswith("\n") and value.endswith("\n"):
        return value[1:-1]
    return value


def _opening_tag(
    name: str,
    attributes: Mapping[str, object] | None,
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
    "CACHED_CONTENT_KEY",
    "DISPLAY_CONTENT_KEY",
    "MESSAGE_FORMAT_KEY",
    "cached_content_prompt",
    "prompt_container",
    "prompt_element",
    "tool_result_display_content",
]
