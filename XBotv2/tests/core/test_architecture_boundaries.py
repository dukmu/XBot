"""Dependency direction gates for the stable extension architecture."""

import ast
from pathlib import Path

# Top-level plugin packages (moved from builtin_plugins/ in the flat layout).
_PLUGIN_PACKAGES = {
    "goal",
    "todolist",
    "skills",
    "mcp",
    "compact",
    "agents",
    "browser",
    "token_manager",
    "workspace_instructions",
}

# Top-level runtime packages that are implementation, not public API.
_RUNTIME_PACKAGES = {
    "core",
    "tools",
    "config",
    "persistence",
    "llm",
    "protocol",
    "tui",
    "acp",
    "loader",
    "client",
    "web_server",
}

# Top-level directories that are not Python packages.
_NON_PACKAGE_DIRS = {"tests", "data", "docs", "web", "web_dist"}


def _imported_modules(tree: ast.AST) -> list[str]:
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            modules.append(node.module or "")
        elif isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
    return modules


def test_builtin_plugins_only_import_public_xbot_api():
    root = Path(__file__).parents[2]
    violations = []
    for name in sorted(_PLUGIN_PACKAGES):
        pkg = root / name
        for path in pkg.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for module in _imported_modules(tree):
                if module.split(".", 1)[0] in _RUNTIME_PACKAGES:
                    violations.append(f"{name}/{path.relative_to(pkg)} imports {module}")
    assert violations == []


def test_core_never_imports_builtin_plugins():
    root = Path(__file__).parents[2]
    violations = []
    for pkg_dir in sorted(root.iterdir()):
        if not pkg_dir.is_dir() or pkg_dir.name.startswith("."):
            continue
        if pkg_dir.name in _PLUGIN_PACKAGES or pkg_dir.name in _NON_PACKAGE_DIRS:
            continue
        for path in pkg_dir.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for module in _imported_modules(tree):
                if module.split(".", 1)[0] in _PLUGIN_PACKAGES:
                    violations.append(f"{pkg_dir.name}/{path.relative_to(pkg_dir)}")
    assert violations == []


def test_public_api_does_not_import_runtime_implementations():
    root = Path(__file__).parents[2] / "api"
    violations = []
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for module in _imported_modules(tree):
            is_runtime_import = (
                module.split(".", 1)[0] in _RUNTIME_PACKAGES
            )
            if is_runtime_import:
                violations.append(f"{path.name} imports {module}")
    assert violations == []
