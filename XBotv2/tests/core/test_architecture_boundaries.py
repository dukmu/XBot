"""Dependency direction gates for the stable extension architecture."""

import ast
from pathlib import Path


def test_builtin_plugins_only_import_public_xbot_api():
    root = Path(__file__).parents[2] / "builtin_plugins"
    violations = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module.startswith("xbotv2.") and module != "xbotv2.api":
                    violations.append(f"{path.relative_to(root)} imports {module}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("xbotv2.") and alias.name != "xbotv2.api":
                        violations.append(
                            f"{path.relative_to(root)} imports {alias.name}"
                        )
    assert violations == []


def test_core_never_imports_builtin_plugins():
    root = Path(__file__).parents[2] / "xbotv2"
    violations = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            modules = []
            if isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            elif isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            if any(module.startswith("builtin_plugins") for module in modules):
                violations.append(str(path.relative_to(root)))
    assert violations == []


def test_public_api_does_not_import_runtime_implementations():
    root = Path(__file__).parents[2] / "xbotv2" / "api"
    violations = []
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            modules = []
            if isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            elif isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            for module in modules:
                is_runtime_import = (
                    module.startswith("xbotv2.")
                    and not module.startswith("xbotv2.api")
                )
                if is_runtime_import:
                    violations.append(f"{path.name} imports {module}")
    assert violations == []
