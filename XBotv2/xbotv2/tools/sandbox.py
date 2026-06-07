"""Sandbox policy with BubblewrapBackend for tool execution.

BubblewrapBackend controls the total lifecycle of sandboxed tool calls:
mount setup, process spawn, communication, timeout, and result formatting.
Path access is enforced at the OS level through mount specifications, not
through Python-level path extraction and checking.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Literal

from xbotv2.tools.sandbox_bwrap import BubblewrapBackend, SandboxMountSpec, backend_available

PathAccess = Literal["readwrite", "readonly", "deny", "ask"]


@dataclass
class SandboxResourceRule:
    path: str
    access: PathAccess = "readonly"

    def matches(self, target: str) -> bool:
        try:
            Path(target).relative_to(self.path)
            return True
        except ValueError:
            return False


class SandboxPolicy:
    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        data_root: Path | str = "/tmp/xbotv2-data",
        workspace_root: Path | str = "/tmp/xbotv2-workspace",
        enabled: bool = True,
    ) -> None:
        self.enabled = enabled
        self.data_root = Path(data_root).resolve()
        self.workspace_root = Path(workspace_root).resolve()
        self._backend = BubblewrapBackend(self.workspace_root)
        self._rules: list[SandboxResourceRule] = []

        if config:
            self._load_config(config)
        self._rules.append(SandboxResourceRule(path=str(self.workspace_root), access="readwrite"))
        self._rules.append(SandboxResourceRule(path=str(self.data_root), access="readonly"))

    @property
    def backend_available(self) -> bool:
        return backend_available()

    def add_rule(self, path: str, access: PathAccess) -> None:
        self._rules.insert(0, SandboxResourceRule(path=path, access=access))

    # ------------------------------------------------------------------
    # Sandbox capabilities (system I/O isolated via bwrap)
    # ------------------------------------------------------------------

    async def run_shell(self, command: str, cwd: str | None = None) -> str:
        spec = self._mount_specs()
        return await self._backend.run(["/bin/sh", "-lc", command], spec, cwd=cwd)

    async def read_file(self, path: str, offset: int = 0, limit: int = 2000) -> str:
        spec = self._mount_specs()
        script = (
            "import json, sys, pathlib"
            "; p = pathlib.Path(sys.argv[1])"
            "; o = int(sys.argv[2])"
            "; lim = int(sys.argv[3])"
            "; t = p.read_text('utf-8')"
            "; lines = t.splitlines()"
            "; start = max(0, o)"
            "; end = len(lines) if lim <= 0 else min(len(lines), start + lim)"
            "; sel = lines[start:end]"
            "; s = p.stat()"
            "; r = {"
            "  'ok':True,'path':str(p),'resolved_path':str(p.resolve()),"
            "  'kind':'file','size_bytes':s.st_size,'mtime':s.st_mtime,"
            "  'line_count':len(lines),'offset':start,'limit':lim,"
            "  'returned_lines':len(sel),'truncated_before':start>0,"
            "  'truncated_after':end<len(lines),'content':chr(10).join(sel)}"
            "; sys.stdout.write(json.dumps(r,ensure_ascii=False))"
        )
        return await self._backend.run(
            ["python3", "-c", script, str(self.workspace_root / path), str(offset), str(limit)],
            spec,
        )

    async def write_file(self, path: str, content: str) -> str:
        spec = self._mount_specs()
        resolved = str(self.workspace_root / path)
        script = (
            "import json, sys, pathlib"
            "; p = pathlib.Path(sys.argv[1])"
            "; c = sys.stdin.read()"
            "; p.parent.mkdir(parents=True, exist_ok=True)"
            "; p.write_text(c, 'utf-8')"
            "; s = p.stat()"
            "; r = {'ok':True,'path':str(p),'bytes_written':len(c.encode('utf-8')),"
            "  'size_bytes':s.st_size,'mtime':s.st_mtime,"
            "  'line_count':len(c.splitlines())}"
            "; sys.stdout.write(json.dumps(r,ensure_ascii=False))"
        )
        return await self._backend.run(
            ["python3", "-c", script, resolved],
            spec,
            stdin=content,
        )

    async def list_dir(self, path: str = ".", recursive: bool = False, max_entries: int = 500) -> str:
        spec = self._mount_specs()
        script = (
            "import json, sys, pathlib"
            "; p = pathlib.Path(sys.argv[1])"
            "; recursive = sys.argv[2] == '1'"
            "; max_entries = int(sys.argv[3])"
            "; it = p.rglob('*') if recursive else p.iterdir()"
            "; entries = sorted(it, key=lambda x: (not x.is_dir(), str(x)))"
            "; limited = entries[:max_entries] if max_entries > 0 else entries"
            "; def meta(e):"
            "  s = e.stat();"
            "  return {'name':e.name,'path':str(e),'relative_path':str(e.relative_to(p)),"
            "    'kind':'directory' if e.is_dir() else 'file','size_bytes':s.st_size,'mtime':s.st_mtime}"
            "; r = {'ok':True,'path':str(p),'resolved_path':str(p.resolve()),"
            "  'kind':'directory','recursive':recursive,'entry_count':len(entries),"
            "  'returned_entries':len(limited),'truncated':max_entries>0 and len(entries)>max_entries,"
            "  'entries':[meta(e) for e in limited]}"
            "; sys.stdout.write(json.dumps(r,ensure_ascii=False))"
        )
        return await self._backend.run(
            ["python3", "-c", script,
             str(self.workspace_root / path),
             "1" if recursive else "0",
             str(max_entries)],
            spec,
        )

    # ------------------------------------------------------------------
    # Mount spec assembly
    # ------------------------------------------------------------------

    def _mount_specs(self) -> list[SandboxMountSpec]:
        mounts: list[SandboxMountSpec] = []

        mounts.append(SandboxMountSpec(
            source=self.workspace_root, target=self.workspace_root,
            access="readwrite", kind="dir",
        ))

        for rule in self._rules:
            resolved = self.resolve_resource_path(rule.path)
            if resolved == str(self.workspace_root):
                continue
            kind = _path_kind(resolved)
            if rule.access == "readwrite":
                mounts.append(SandboxMountSpec(Path(resolved), Path(resolved), "readwrite", kind))
            elif rule.access == "readonly":
                mounts.append(SandboxMountSpec(Path(resolved), Path(resolved), "readonly", kind))

        return mounts

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    def resolve_resource_path(self, path: str) -> str:
        p = Path(path)
        return str(p.resolve() if p.is_absolute() else (self.data_root / p).resolve())

    def describe(self) -> str:
        if self.enabled:
            available = "available" if self.backend_available else "unavailable"
            return (
                f"Sandbox enabled (bwrap: {available}). Workspace: {self.workspace_root}. "
                f"All file I/O and shell commands run inside bubblewrap."
            )
        return f"Sandbox disabled. Workspace: {self.workspace_root}."

    # ------------------------------------------------------------------
    # Config loading
    # ------------------------------------------------------------------

    def _load_config(self, config: dict[str, Any]) -> None:
        self.enabled = config.get("enabled", self.enabled)
        for rule_data in config.get("resources", []):
            path = _expand_path_placeholders(
                str(rule_data.get("path", "")),
                str(self.workspace_root),
                str(self.data_root),
            )
            access = rule_data.get("access", "readonly")
            self._rules.append(SandboxResourceRule(path=path, access=access))


def _path_kind(path_str: str) -> Literal["file", "dir"]:
    p = Path(path_str)
    if p.exists() and p.is_file():
        return "file"
    return "dir"


def _expand_path_placeholders(path: str, workspace: str, data_dir: str) -> str:
    return path.replace("{{ workspace }}", workspace).replace("{{ data_dir }}", data_dir)
