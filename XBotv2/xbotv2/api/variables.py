"""Read-only runtime variables shared by configuration consumers."""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from pathlib import Path
from types import MappingProxyType

from xbotv2.api.paths import RuntimePaths, ThreadPaths

_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_MARKDOWN_VAR_BLOCK = re.compile(
    r"^```var[ \t]*\r?\n[ \t]*\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)\}[ \t]*"
    r"\r?\n```[ \t]*$",
    re.MULTILINE,
)


class RuntimeVariables(Mapping[str, str]):
    """Immutable runtime values expanded from ``${name}`` references."""

    __slots__ = ("_values",)

    def __init__(self, values: Mapping[str, str | Path] | None = None) -> None:
        normalized: dict[str, str] = {}
        for name, value in (values or {}).items():
            if not _NAME.fullmatch(name):
                raise ValueError(f"Invalid runtime variable name: {name}")
            normalized[name] = str(Path(value).expanduser().resolve())
        object.__setattr__(self, "_values", MappingProxyType(normalized))

    @classmethod
    def from_roots(
        cls,
        *,
        workspace: Path | str,
        data_dir: Path | str,
        session_dir: Path | str | None = None,
        thread_dir: Path | str | None = None,
        state_dir: Path | str | None = None,
    ) -> "RuntimeVariables":
        workspace_path = Path(workspace).expanduser().resolve()
        data_path = Path(data_dir).expanduser().resolve()
        values: dict[str, Path | str] = {
            "workspace": workspace_path,
            "data_dir": data_path,
            "config_dir": data_path / "config",
            "custom_config_dir": workspace_path / ".xbot",
        }
        if session_dir is not None:
            values["session_dir"] = session_dir
        if thread_dir is not None:
            values["thread_dir"] = thread_dir
        if state_dir is not None:
            state_path = Path(state_dir)
            values.update({
                "state_dir": state_path,
                "plugin_states": state_path / "plugin_states",
                "artifacts": state_path / "artifacts",
                "tool_results": state_path / "artifacts" / "tool_results",
            })
        return cls(values)

    @classmethod
    def for_thread(
        cls,
        runtime: RuntimePaths,
        workspace: Path | str,
        thread: ThreadPaths,
    ) -> "RuntimeVariables":
        return cls.from_roots(
            workspace=workspace,
            data_dir=runtime.data_dir,
            session_dir=thread.session.root,
            thread_dir=thread.root,
            state_dir=thread.state_dir,
        )

    def __getitem__(self, name: str) -> str:
        return self._values[name]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def expand(
        self,
        value: str,
        *,
        source: str = "value",
    ) -> str:
        """Expand references and reject unknown variable names."""
        return self._replace(value, source=source, regex=False)

    def expand_regex(self, value: str, *, source: str = "regex") -> str:
        """Expand references as escaped literals inside a regular expression."""
        return self._replace(value, source=source, regex=True)

    def expand_markdown(self, value: str, *, source: str = "Markdown") -> str:
        """Replace explicit Markdown ``var`` blocks without touching other text."""
        def replacement(match: re.Match[str]) -> str:
            name = match.group("name")
            self._require(name, source)
            return self._values[name]

        return _MARKDOWN_VAR_BLOCK.sub(replacement, value)

    def reference_name(self, value: str, *, source: str = "value") -> str | None:
        """Return the variable name when *value* is exactly one reference."""
        match = _REFERENCE.fullmatch(value)
        if match is None:
            return None
        name = match.group(1)
        self._require(name, source)
        return name

    def _replace(
        self,
        value: str,
        *,
        source: str,
        regex: bool,
    ) -> str:
        def replacement(match: re.Match[str]) -> str:
            name = match.group(1)
            self._require(name, source)
            result = self._values[name]
            return re.escape(result) if regex else result

        return _REFERENCE.sub(replacement, value)

    def _require(self, name: str, source: str) -> None:
        if name not in self._values:
            raise ValueError(f"Unknown runtime variable ${{{name}}} in {source}")


__all__ = ["RuntimeVariables"]
