"""Agent Skills registry — discovery, parsing, and caching of SKILL.md files.

Compatible with the agentskills.io standard, used by Claude Code and OpenCode.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_DISCOVERY_PATHS = [
    ".claude/skills",
    ".agents/skills",
    ".opencode/skills",
]
_GLOBAL_PATHS = [
    Path.home() / ".claude/skills",
    Path.home() / ".agents/skills",
    Path.home() / ".config/opencode/skills",
]


@dataclass
class Skill:
    name: str
    description: str
    path: Path
    content: str
    frontmatter: dict[str, object] = field(default_factory=dict)
    allowed_tools: list[str] = field(default_factory=list)
    disallowed_tools: list[str] = field(default_factory=list)
    disable_model_invocation: bool = False
    scope: str = "project"


class SkillRegistry:
    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}

    def discover(self, workspace: Path) -> None:
        self._skills.clear()
        self._scan_project(workspace)
        self._scan_global()

    def list_skills(self) -> list[Skill]:
        return sorted(self._skills.values(), key=lambda s: s.name)

    def load_skill(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def _scan_project(self, workspace: Path) -> None:
        current = workspace.resolve()
        git_root = self._find_git_root(current)
        while current != current.parent:
            for discovery_path in _DISCOVERY_PATHS:
                skills_dir = current / discovery_path
                self._scan_dir(skills_dir, "project")
            if current == git_root or current == Path("/"):
                break
            current = current.parent

    def _scan_global(self) -> None:
        for skills_dir in _GLOBAL_PATHS:
            self._scan_dir(skills_dir, "global")

    def _scan_dir(self, skills_dir: Path, scope: str) -> None:
        if not skills_dir.is_dir():
            return
        for skill_dir in sorted(skills_dir.iterdir()):
            if not skill_dir.is_dir():
                continue
            skill_file = skill_dir / "SKILL.md"
            if not skill_file.is_file():
                continue
            skill = self._parse(skill_file, scope)
            if skill and skill.name not in self._skills:
                self._skills[skill.name] = skill

    def _parse(self, path: Path, scope: str) -> Skill | None:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

        m = _FRONTMATTER_RE.match(text)
        if not m:
            return None

        try:
            fm = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError:
            return None

        if not isinstance(fm, dict):
            return None

        name = str(fm.get("name") or "").strip()
        if not name or not _NAME_RE.match(name):
            return None
        if name != path.parent.name:
            return None

        description = str(fm.get("description") or "").strip()[:1024]
        if not description:
            return None

        content = text[m.end():].strip()

        allowed = fm.get("allowed-tools") or fm.get("allowed_tools") or []
        if isinstance(allowed, str):
            allowed = [t.strip() for t in allowed.split(",") if t.strip()]
        disallowed = fm.get("disallowed-tools") or fm.get("disallowed_tools") or []
        if isinstance(disallowed, str):
            disallowed = [t.strip() for t in disallowed.split(",") if t.strip()]
        disable_model_invocation = fm.get(
            "disable-model-invocation",
            fm.get("disable_model_invocation", False),
        )
        if not isinstance(disable_model_invocation, bool):
            return None

        return Skill(
            name=name,
            description=description,
            path=path,
            content=content,
            frontmatter=fm,
            allowed_tools=list(allowed),
            disallowed_tools=list(disallowed),
            disable_model_invocation=disable_model_invocation,
            scope=scope,
        )

    @staticmethod
    def _find_git_root(path: Path) -> Path:
        current = path
        while current != current.parent:
            if (current / ".git").is_dir():
                return current
            current = current.parent
        return path
