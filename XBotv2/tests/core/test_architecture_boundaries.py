"""Dependency direction gates for the all-plugin extension architecture.

Every capability is a plugin package under ``XBotv2`` (``<pkg>/plugin.py``
exporting ``plugin``), wired by the declarative tree (``xcore.yaml``).
Boundaries:

* capability plugins import another plugin's declarations from its package
  root; explicit declaration modules remain temporarily accepted during the
  migration, but concrete implementation modules are forbidden;
* service plugins never import capability plugins;
* contract modules (the ``XBotv2.core`` surface) never import plugin
  implementations (the shared ``XBotv2.config`` library is allowed).
"""

import ast
from pathlib import Path

# In-tree capability plugins (the "builtin" plugin set).
_CAPABILITY_PLUGINS = {
    "goal",
    "todolist",
    "skills",
    "mcp_plugin",
    "compact",
    "agents",
    "browser",
    "token_manager",
    "workspace_instructions",
}

# Service plugins (each provides XCore services / event listeners).
_SERVICE_PLUGINS = {
    "config",
    "persistence",
    "session",
    "jobs",
    "llm",
    "tools",
    "commands",
    "prompts",
    "sandbox",
    "permissions",
    "context_builder",
    "coretools",
    "agentloop",
    "core",
}

# Shared contract packages plugins may import.
_CONTRACT_PACKAGES = {"XBotv2.core", "XBotv2.jobs"}
_PUBLIC_DECLARATION_MODULES = {
    "commands",
    "contracts",
    "events",
    "invariants",
    "services",
    "types",
}

# The shared configuration library (types + parsing), allowed for contracts.
_ALLOWED_CONTRACT_IMPORTS = _CONTRACT_PACKAGES | {"XBotv2.config"}

# core/ modules that are engine implementation, not contract surface.
# (The engine implementation lives in XBotv2.agentloop; core holds contracts.)
_CORE_IMPLEMENTATION: set[str] = set()

# Top-level directories that are not Python plugin packages.
_NON_PACKAGE_DIRS = {"tests", "data", "docs", "web", "web_dist", "__pycache__"}


def _imported_modules(tree: ast.AST) -> list[str]:
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            modules.append(node.module or "")
        elif isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
    return modules


def _first_segment(module: str) -> str:
    return module.split(".", 1)[0]


def test_capability_plugins_only_import_public_contracts():
    root = Path(__file__).parents[2]
    violations = []
    for name in sorted(_CAPABILITY_PLUGINS):
        pkg = root / name
        for path in pkg.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for module in _imported_modules(tree):
                if not module.startswith("XBotv2."):
                    continue
                parts = module.split(".")
                is_public_package_root = (
                    len(parts) == 2
                    and (root / parts[1] / "__init__.py").is_file()
                )
                allowed = (
                    is_public_package_root
                    or module in _CONTRACT_PACKAGES
                    or module.startswith("XBotv2.core.")
                    or module.startswith("XBotv2.jobs.")
                    or module.startswith(f"XBotv2.{name}.")
                    or module.rsplit(".", 1)[-1]
                    in _PUBLIC_DECLARATION_MODULES
                )
                if not allowed and path.name in ("router.py", "events.py"):
                    allowed = (
                        module.startswith("XBotv2.server.")
                        or module.startswith("XBotv2.protocol.")
                        or any(
                            module == f"XBotv2.{svc}"
                            or module.startswith(f"XBotv2.{svc}.")
                            for svc in _SERVICE_PLUGINS
                        )
                    )
                if not allowed:
                    violations.append(
                        f"{name}/{path.relative_to(pkg)} imports {module}"
                    )
    assert violations == []


def test_service_plugins_never_import_capability_plugins():
    root = Path(__file__).parents[2]
    violations = []
    for pkg_dir in sorted(root.iterdir()):
        if not pkg_dir.is_dir() or pkg_dir.name in _NON_PACKAGE_DIRS:
            continue
        if pkg_dir.name not in _SERVICE_PLUGINS:
            continue
        for path in pkg_dir.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for module in _imported_modules(tree):
                if _first_segment(module) in _CAPABILITY_PLUGINS:
                    violations.append(
                        f"{pkg_dir.name}/{path.relative_to(pkg_dir)} imports {module}"
                    )
    assert violations == []


def test_core_contract_modules_do_not_import_plugin_implementations():
    root = Path(__file__).parents[2] / "core"
    violations = []
    for path in root.glob("*.py"):
        if path.name in _CORE_IMPLEMENTATION or path.name == "__init__.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for module in _imported_modules(tree):
            if module.startswith("XBotv2."):
                allowed = any(
                    module == name or module.startswith(f"{name}.")
                    for name in _ALLOWED_CONTRACT_IMPORTS
                )
                if not allowed:
                    violations.append(f"{path.name} imports {module}")
    assert violations == []


def test_protocol_modules_do_not_register_xcore_plugins():
    root = Path(__file__).parents[2]
    violations = []
    for path in root.rglob("protocol.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                if any(
                    isinstance(target, ast.Name) and target.id == "plugin"
                    for target in targets
                ):
                    violations.append(str(path.relative_to(root)))
    assert violations == []
