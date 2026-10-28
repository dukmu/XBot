#!/usr/bin/env python3
"""Fail when loop/tool code crosses plugin ownership boundaries.

This check intentionally describes the target architecture, not the current
implementation.  A failing result is an actionable migration list; tests do
not override these failures.
"""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import yaml


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "XBotv2"
ENGINE = PACKAGE / "agentloop" / "impl" / "engine.py"
TOOL_RUNTIME = PACKAGE / "agentloop" / "impl" / "tool_runtime.py"
AGENTLOOP_EVENTS = PACKAGE / "agentloop" / "api" / "events.py"
SESSION = PACKAGE / "agentloop" / "impl" / "execution.py"
BOOTSTRAP = PACKAGE / "bootstrap.py"
APPLICATION_PLUGIN = PACKAGE / "application" / "plugin.py"
APPLICATION_CONFIG = PACKAGE / "application_config"
SESSION_PLUGIN = PACKAGE / "session" / "impl" / "plugin.py"
SESSION_ENTITY = PACKAGE / "session" / "impl" / "session.py"
PERSISTENCE_PLUGIN = PACKAGE / "persistence" / "impl" / "plugin.py"
USAGE_PLUGIN = PACKAGE / "usage" / "impl" / "plugin.py"
APPLICATION_APP = PACKAGE / "application" / "app.py"
APPLICATION_BOOT = PACKAGE / "application" / "boot.py"
AGENT_OPERATIONS = PACKAGE / "agents" / "impl" / "plugin.py"
APPLICATION_AGENTS = PACKAGE / "application" / "agents.py"
APPLICATION_MODEL = PACKAGE / "application" / "model.py"
AGENTS_SERVICE = PACKAGE / "agents" / "impl" / "service.py"
APPLICATION_CONFIG_SEED = PACKAGE / "application" / "config_seed.py"
APPLICATION_TREE = PACKAGE / "application" / "tree.py"
SERVER_APP = PACKAGE / "application" / "server.py"
HTTP_SERVER = PACKAGE / "server" / "impl" / "http.py"
ACP_AGENT = PACKAGE / "acp_plugin" / "impl" / "xbot_agent.py"
XCORE_TREE = PACKAGE / "xcore.yaml"
TOOLS = (
    PACKAGE / "agentloop" / "impl" / "tool_service.py",
    TOOL_RUNTIME,
    PACKAGE / "agentloop" / "impl" / "tool_registry.py",
)

ENGINE_ALLOWED_XBOT_ROOTS = {
    "agentloop",
    "agents",
    "context_builder",
    "core",
    "llm",
    "session",
}
TOOLS_ALLOWED_XBOT_ROOTS = {"agentloop", "core"}
TOOL_SERVICE_FORBIDDEN_LITERALS = {
    "approval",
    "ask_user",
    "interactions",
    "job_registry",
    "permission_request",
    "permissions",
    "request_permission",
    "sandbox",
    "shell",
    "workspace_root",
}
TOOL_SERVICE_FORBIDDEN_STATE = {"ctx", "plugin_ctx", "services"}
ENGINE_FORBIDDEN_PARAMETERS = {
    "agent_registry",
    "content_cache",
    "context_builder",
    "job_registry",
    "permission_system",
    "plugin_ctx",
    "plugin_loader",
    "runtime_variables",
    "sandbox_policy",
    "state_store",
    "tool_registry",
}
ENGINE_FORBIDDEN_STATE = ENGINE_FORBIDDEN_PARAMETERS | {
    "drain_inbox",
    "input_window",
    "permission_waiter",
    "request_continuation",
    "session_usage",
    "take_pending_fold",
    "user_input_waiter",
    "runtime_event_sink",
}
ENGINE_FORBIDDEN_METHODS = {
    "save_messages",
    "run_context_maintenance",
    "_handle_compaction",
    "replace_history",
    "emit_runtime_event",
}
ENGINE_FORBIDDEN_EVENT_NAMES = {
    "BEFORE_STATE_PERSIST",
    "STATE_PERSIST",
    "AFTER_STATE_PERSIST",
    "PRE_COMPACT",
    "POST_COMPACT",
}
SESSION_FORBIDDEN_INPUT_STATE = {
    "pending_fold",
    "fold_output",
    "drain_inbox",
    "take_pending_fold",
}
SESSION_FORBIDDEN_FEATURE_SERVICES = {
    "approval",
    "interactions",
    "jobs",
    "permissions",
    "persistence",
    "sandbox",
    "usage",
}
EVENT_CONTEXT_FORBIDDEN_PLUGIN_FIELDS = {
    "compact_reason",
    "compact_metrics",
    "permission_decision",
    "permission_scope",
    "permission_rule",
    "history_operation",
    "emit",
    "invoke_model",
    "llm",
    "request_user_input",
    "services",
}
PUBLIC_DECLARATION_MODULES = {
    "contracts",  # Transitional declaration module.
    "events",
    "protocol",
}
SHARED_DECLARATION_ROOTS = {"core"}
TRANSPORT_ROOTS = {"acp", "client", "server", "tui"}
XCORE_CONTEXT_API = {
    "bail",
    "dispose",
    "effect",
    "emit",
    "extend",
    "filter",
    "get",
    "has",
    "inject",
    "isolate",
    "middleware",
    "on",
    "once",
    "parallel",
    "plugin",
    "require",
    "provide",
    "scope",
    "pre_stop",
    "close",
    "serial",
    "set",
    "state",
    "stop",
    "unset", "root", "parent", "name", "is_active", "config",
}
ACP_FORBIDDEN_MODULES = {
    "XBotv2.application.app",
    "XBotv2.config.api.tree",
    "XBotv2.persistence.impl.store",
    "XBotv2.session.impl.manager",
    "XBotv2.session.impl.runtime",
    "XBotv2.session.impl.session",
}
ACP_FORBIDDEN_RUNTIME_ATTRIBUTES = {"engine", "manager", "runtime", "services"}


@dataclass(frozen=True, order=True)
class Violation:
    path: Path
    line: int
    rule: str
    detail: str

    def render(self) -> str:
        relative = self.path.relative_to(ROOT)
        return f"{relative}:{self.line}: {self.rule}: {self.detail}"


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _string_values(node: ast.AST | None, path: Path | None = None) -> set[str]:
    """Resolve string or imported ServiceKey entries without importing plugins."""
    if not isinstance(node, (ast.List, ast.Tuple)):
        return set()
    return {value for item in node.elts if (value := _literal_string(item, path)) is not None}


def _literal_string(node: ast.AST | None, path: Path | None = None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if not isinstance(node, ast.Name) or path is None:
        return None
    # Follow the actual imported declaration, including local import aliases.
    for statement in _tree(path).body:
        if not isinstance(statement, ast.ImportFrom) or not statement.module:
            continue
        for alias in statement.names:
            if (alias.asname or alias.name) != node.id:
                continue
            declaration_path = _module_path(statement.module)
            if declaration_path is None:
                return None
            value = _assignment_value(_tree(declaration_path).body, alias.name)
            constructor = value.func if isinstance(value, ast.Call) else None
            if isinstance(constructor, ast.Subscript):
                constructor = constructor.value
            if (
                isinstance(value, ast.Call)
                and isinstance(constructor, ast.Name)
                and constructor.id == "ServiceKey"
                and value.args
            ):
                return _literal_string(value.args[0])
    return None


def _assignment_value(
    body: Iterable[ast.stmt],
    name: str,
) -> ast.AST | None:
    for node in body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name
            for target in node.targets
        ):
            return node.value
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
        ):
            return node.value
    return None


def _inject_values(node: ast.AST | None, path: Path) -> tuple[set[str], set[str]]:
    if isinstance(node, (ast.List, ast.Tuple)):
        return _string_values(node, path), set()
    if not isinstance(node, ast.Dict):
        return set(), set()
    required: set[str] = set()
    optional: set[str] = set()
    for key, value in zip(node.keys, node.values):
        label = _literal_string(key)
        if label == "required":
            required.update(_string_values(value, path))
        elif label == "optional":
            optional.update(_string_values(value, path))
    return required, optional


def _explicit_exports(path: Path) -> set[str] | None:
    value = _assignment_value(_tree(path).body, "__all__")
    if not isinstance(value, (ast.List, ast.Tuple)):
        return None
    return _string_values(value)


def _module_path(module: str) -> Path | None:
    parts = module.split(".")
    if not parts or parts[0] != "XBotv2":
        return None
    candidate = ROOT.joinpath(*parts).with_suffix(".py")
    if candidate.is_file():
        return candidate
    package = ROOT.joinpath(*parts, "__init__.py")
    return package if package.is_file() else None


def _plugin_source(module: str) -> Path | None:
    path = PACKAGE / module / "impl" / "plugin.py"
    return path if path.is_file() else _module_path(module)


def _plugin_class(path: Path) -> ast.ClassDef | None:
    tree = _tree(path)
    class_name: str | None = None
    for node in tree.body:
        if not isinstance(node, ast.Assign) or not any(
            isinstance(target, ast.Name) and target.id == "plugin"
            for target in node.targets
        ):
            continue
        if isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name):
            class_name = node.value.func.id
            break
    if class_name is None:
        return None
    return next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == class_name
        ),
        None,
    )


@dataclass(frozen=True)
class PluginSpec:
    entry_id: str
    module: str
    profiles: frozenset[str]
    path: Path
    line: int
    required: frozenset[str]
    optional: frozenset[str]
    provided: frozenset[str]


def _provided_services(node: ast.AST, path: Path) -> set[str]:
    services: set[str] = set()
    for item in ast.walk(node):
        if not isinstance(item, ast.Call) or not isinstance(item.func, ast.Attribute):
            continue
        if item.func.attr not in {"set", "provide"} or not item.args:
            continue
        service = _literal_string(item.args[0], path)
        if service:
            services.add(service)
    return services


def _facet_declaration(tree, facet, field):
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == facet and target.attr == field:
                    return node.value
    return None


def _resolved_declaration(value, tree):
    if isinstance(value, ast.Name):
        resolved = _assignment_value(tree.body, value.id)
        return _resolved_declaration(resolved, tree) if resolved is not None else value
    if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
        if value.func.id == "getattr" and len(value.args) >= 2 and isinstance(value.args[0], ast.Name):
            cls = next((node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == value.args[0].id), None)
            if cls is not None:
                resolved = _assignment_value(cls.body, value.args[1].value)
                return _resolved_declaration(resolved, tree) if resolved is not None else ast.Tuple(elts=[])
        if value.func.id in {"tuple", "list"} and value.args:
            return _resolved_declaration(value.args[0], tree)
    return value


def _plugin_specs() -> list[PluginSpec]:
    """Read actual scope facets and their independently activated callbacks."""
    document = yaml.safe_load(XCORE_TREE.read_text(encoding="utf-8")) or []
    specs = []
    for entry in document:
        if not isinstance(entry, dict) or not entry.get("name") or entry.get("disabled"):
            continue
        module = str(entry["name"])
        path = _plugin_source(module)
        if path is None:
            continue
        tree = _tree(path)
        for facet in tree.body:
            if not isinstance(facet, (ast.FunctionDef, ast.AsyncFunctionDef)) or facet.name not in {"mount_process", "mount_session", "mount_thread"}:
                continue
            kind = facet.name.removeprefix("mount_")
            required, optional = _inject_values(_resolved_declaration(_facet_declaration(tree, facet.name, "inject"), tree), path)
            provided = _string_values(_resolved_declaration(_facet_declaration(tree, facet.name, "provide"), tree), path)
            specs.append(PluginSpec(str(entry.get("id") or module)+":"+facet.name, module+":"+facet.name, frozenset({kind}), path, facet.lineno, frozenset(required), frozenset(optional), frozenset(provided)))
    return specs


def _module_root(module: str) -> str | None:
    parts = module.split(".")
    if len(parts) < 2 or parts[0] != "XBotv2":
        return None
    return parts[1]


def _imports(path: Path, allowed_roots: set[str], rule: str) -> list[Violation]:
    violations: list[Violation] = []
    for node in ast.walk(_tree(path)):
        modules: list[str] = []
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules = [node.module]
        for module in modules:
            root = _module_root(module)
            if root is not None and root not in allowed_roots:
                violations.append(
                    Violation(path, node.lineno, rule, f"imports {module}")
                )
    return violations


def _is_context_get(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    function = node.func
    if not isinstance(function, ast.Attribute) or function.attr in {"get_all", "get_registered"}:
        return False
    if function.attr != "get":
        return False
    owner = function.value
    if isinstance(owner, ast.Name):
        return owner.id in {"ctx", "plugin_ctx", "services"}
    return (
        isinstance(owner, ast.Attribute)
        and isinstance(owner.value, ast.Name)
        and owner.value.id == "self"
        and owner.attr in {"ctx", "plugin_ctx", "services"}
    )


def _is_service_bag_get(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    if node.func.attr != "get":
        return False
    owner = node.func.value
    if isinstance(owner, ast.Name):
        return owner.id == "services"
    return (
        isinstance(owner, ast.Attribute)
        and owner.attr == "services"
    )


def check_engine() -> list[Violation]:
    tree = _tree(ENGINE)
    violations = _imports(
        ENGINE,
        ENGINE_ALLOWED_XBOT_ROOTS,
        "loop-import",
    )
    for node in ast.walk(tree):
        if _is_context_get(node):
            violations.append(
                Violation(
                    ENGINE,
                    node.lineno,
                    "loop-service-locator",
                    "loop must not discover plugin services with ctx.get()",
                )
            )
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in ENGINE_FORBIDDEN_METHODS:
                violations.append(
                    Violation(
                        ENGINE,
                        node.lineno,
                        "loop-method",
                        f"Engine owns non-loop operation {node.name!r}",
                    )
                )
            if node.name == "__init__":
                names = {argument.arg for argument in (*node.args.args, *node.args.kwonlyargs)}
                for name in sorted(names & ENGINE_FORBIDDEN_PARAMETERS):
                    violations.append(
                        Violation(
                            ENGINE,
                            node.lineno,
                            "loop-constructor",
                            f"Engine.__init__ owns non-loop dependency {name!r}",
                        )
                    )
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "Events"
            and node.attr in ENGINE_FORBIDDEN_EVENT_NAMES
        ):
            violations.append(Violation(
                ENGINE,
                node.lineno,
                "loop-plugin-event",
                f"Engine dispatches plugin-owned event Events.{node.attr}",
            ))
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"
                    and target.attr in ENGINE_FORBIDDEN_STATE
                ):
                    violations.append(
                        Violation(
                            ENGINE,
                            node.lineno,
                            "loop-state",
                            f"Engine owns plugin state {target.attr!r}",
                        )
                    )
    return violations


def check_agentloop_event_context() -> list[Violation]:
    violations: list[Violation] = []
    for node in _tree(AGENTLOOP_EVENTS).body:
        if not isinstance(node, ast.ClassDef) or node.name != "EventContext":
            continue
        for item in node.body:
            if (
                isinstance(item, ast.AnnAssign)
                and isinstance(item.target, ast.Name)
                and item.target.id in EVENT_CONTEXT_FORBIDDEN_PLUGIN_FIELDS
            ):
                violations.append(Violation(
                    AGENTLOOP_EVENTS,
                    item.lineno,
                    "loop-plugin-payload",
                    f"EventContext names plugin operation {item.target.id!r}",
                ))
    return violations


def check_agentloop_imports() -> list[Violation]:
    violations: list[Violation] = []
    root = PACKAGE / "agentloop"
    agent_registry = root / "agent_registry.py"
    if agent_registry.exists():
        violations.append(Violation(
            agent_registry,
            1,
            "loop-agent-registry",
            "Agent definitions belong to agents, not the execution loop",
        ))
    logging_config = root / "logging_config.py"
    if logging_config.exists():
        violations.append(Violation(
            logging_config,
            1,
            "loop-infrastructure",
            "process logging configuration does not belong to the loop",
        ))
    session_operations = PACKAGE / "session" / "operations.py"
    if session_operations.exists():
        violations.append(Violation(
            session_operations,
            1,
            "application-operations",
            "cross-capability forwarding does not belong to session",
        ))
    for path in sorted(root.rglob("*.py")):
        if "tests" in path.relative_to(root).parts:
            continue
        if path == ENGINE:
            continue
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported = _module_root(node.module)
                if imported not in {None, "agentloop"} and not node.module.startswith(f"XBotv2.{imported}.api."):
                    violations.append(Violation(path, node.lineno, "loop-import", f"imports non-public owner code {node.module}"))
    return violations


def check_runtime_ownership() -> list[Violation]:
    """Check the real process/scope composition, with no application layer."""
    violations: list[Violation] = []
    for path in (PACKAGE / "application").rglob("*.py"):
        violations.append(Violation(path, 1, "removed-application", "application composition has been removed"))
    forbidden = {"MountedAgentApplication", "AgentApplicationPort", "AgentApplicationOptions", "AgentApplicationFactory", "AgentLoopFactory", "AgentLoopFactoryPort", "LoopFactoryOptions", "THREAD_RUNNER", "ThreadRunnerPort"}
    forwarding = {"dispatch", "dispatch_all", "send_message", "submit_runtime_input", "respond_permission", "respond_user_input", "cancel_interaction", "artifact", "messages", "message_page", "trajectory_page", "pending_inputs", "update_pending_input", "regenerate_message", "interrupt"}
    for path in PACKAGE.rglob("*.py"):
        if "tests" in path.parts or "__pycache__" in path.parts:
            continue
        for node in ast.walk(_tree(path)):
            if isinstance(node, (ast.Name, ast.ClassDef)) and (node.id if isinstance(node, ast.Name) else node.name) in forbidden:
                violations.append(Violation(path, node.lineno, "removed-runtime-facade", "obsolete application/factory/execution facade"))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Engine" and path.parent != PACKAGE / "agentloop" / "impl":
                violations.append(Violation(path, node.lineno, "engine-construction", "Engine is constructed only by agentloop implementation"))
            if path == PACKAGE / "session" / "impl" / "manager.py" and isinstance(node, ast.ClassDef) and node.name == "SessionManager":
                for method in node.body:
                    if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)) and method.name in forwarding:
                        violations.append(Violation(path, method.lineno, "session-business-forwarding", "session owns lifecycle and scope location; call the target owner service"))
    for path in PACKAGE.glob("*/impl/plugin.py"):
        tree = _tree(path)
        facets = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in {"mount_process", "mount_session", "mount_thread"}]
        if not facets:
            violations.append(Violation(path, 1, "scope-facets", "plugin must declare at least one scope facet"))
        for facet in facets:
            for field in ("inject", "provide"):
                if _facet_declaration(tree, facet.name, field) is None:
                    violations.append(Violation(path, facet.lineno, "scope-facet-declaration", f"{facet.name} must declare {field}"))
    return violations


def check_inbox() -> list[Violation]:
    """Keep all model-visible queued content in the agent-owned inbox."""
    violations: list[Violation] = []
    legacy = PACKAGE / "inbox"
    if legacy.exists() and any(legacy.rglob("*.py")):
        violations.append(Violation(
            legacy,
            1,
            "inbox-ownership",
            "the inbox must live inside XBotv2/agentloop",
        ))
    for path in PACKAGE.rglob("*.py"):
        if "__pycache__" in path.parts or "tests" in path.relative_to(PACKAGE).parts:
            continue
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            else:
                modules = []
            for module in modules:
                if module == "XBotv2.inbox" or module.startswith("XBotv2.inbox."):
                    violations.append(Violation(
                        path,
                        node.lineno,
                        "inbox-import",
                        f"imports removed standalone inbox {module}",
                    ))
    for node in ast.walk(_tree(SESSION)):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value in SESSION_FORBIDDEN_FEATURE_SERVICES
        ):
            violations.append(Violation(
                SESSION,
                node.lineno,
                "session-feature-service",
                f"session transport names feature service {node.value!r}",
            ))
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                name = (
                    target.attr
                    if isinstance(target, ast.Attribute)
                    else target.id if isinstance(target, ast.Name) else ""
                )
                if name in SESSION_FORBIDDEN_INPUT_STATE:
                    violations.append(Violation(
                        SESSION,
                        node.lineno,
                        "inbox-side-channel",
                        f"session owns removed input queue {name!r}",
                    ))
    return violations


def check_tools() -> list[Violation]:
    violations: list[Violation] = []
    legacy_package = PACKAGE / "tools"
    if legacy_package.exists():
        violations.append(
            Violation(
                legacy_package,
                1,
                "tools-ownership",
                "XBotv2/tools must live under XBotv2/agentloop as a loop service",
            )
        )
    for path in TOOLS:
        violations.extend(_imports(path, TOOLS_ALLOWED_XBOT_ROOTS, "tools-import"))
        for node in ast.walk(_tree(path)):
            if _is_context_get(node):
                violations.append(
                    Violation(
                        path,
                        node.lineno,
                        "tools-service-locator",
                        "tool execution must not discover another plugin service",
                    )
                )
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                literal = node.value.strip().lower()
                if literal in TOOL_SERVICE_FORBIDDEN_LITERALS:
                    violations.append(
                        Violation(
                            path,
                            node.lineno,
                            "tools-plugin-special-case",
                            f"tool service names plugin-owned concept {node.value!r}",
                        )
                    )
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if (
                        isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "self"
                        and target.attr in TOOL_SERVICE_FORBIDDEN_STATE
                    ):
                        violations.append(Violation(
                            path,
                            node.lineno,
                            "tools-container-state",
                            f"tool service stores application container {target.attr!r}",
                        ))
    return violations


def _plugin_roots() -> set[str]:
    roots = {path.parent.name for path in PACKAGE.glob("*/plugin.py")}
    roots.update(path.parent.parent.name for path in PACKAGE.glob("*/impl/plugin.py"))
    roots.update(path.parent.name for path in PACKAGE.glob("*/api") if path.is_dir())
    roots.update(spec.module.split(":", 1)[0].split(".", 1)[0] for spec in _plugin_specs())
    return roots


def _public_import_violation(
    *,
    path: Path,
    owner: str,
    node: ast.Import | ast.ImportFrom,
    module: str,
    plugin_roots: set[str],
) -> Violation | None:
    imported = _module_root(module)
    if imported is None or imported == owner:
        return None
    if imported == "core":
        if module.startswith("XBotv2.core.api."):
            return None
        return Violation(
            path, node.lineno, "shared-implementation-import",
            f"imports shared implementation outside core.api: {module}",
        )
    if imported == "protocol":
        # Capability-owned route adapters consume transport envelopes here.
        if owner in TRANSPORT_ROOTS or path.name in {"protocol.py", "router.py", "http.py", "client_http.py"}:
            return None
        return Violation(
            path,
            node.lineno,
            "plugin-wire-import",
            f"capability package imports wire contract {module}",
        )
    if imported not in plugin_roots:
        return None
    if (PACKAGE / imported / "api").is_dir():
        if module.startswith(f"XBotv2.{imported}.api."):
            return None
        return Violation(path, node.lineno, "plugin-concrete-import",
                         f"imports plugin implementation {module}")
    package_root = module == f"XBotv2.{imported}"
    if package_root:
        declaration_path = _module_path(module)
        exports = _explicit_exports(declaration_path) if declaration_path else None
        if exports is None:
            return Violation(
                path,
                node.lineno,
                "plugin-implicit-public-module",
                f"plugin package {module} has no explicit __all__",
            )
        if isinstance(node, ast.ImportFrom):
            imported_names = {
                alias.name for alias in node.names if alias.name != "*"
            }
            missing = sorted(imported_names - exports)
            if missing or any(alias.name == "*" for alias in node.names):
                detail = "*" if not missing else ", ".join(missing)
                return Violation(
                    path,
                    node.lineno,
                    "plugin-private-symbol-import",
                    f"imports non-public symbol(s) {detail} from {module}",
                )
        return None
    if module.rsplit(".", 1)[-1] in PUBLIC_DECLARATION_MODULES:
        return None
    return Violation(
        path,
        node.lineno,
        "plugin-concrete-import",
        f"imports plugin implementation {module}",
    )


def check_plugin_imports() -> list[Violation]:
    violations: list[Violation] = []
    plugin_roots = _plugin_roots()
    for owner in sorted(plugin_roots):
        root = PACKAGE / owner
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts or "tests" in path.relative_to(PACKAGE).parts:
                continue
            parsed = _tree(path)
            for node in ast.walk(parsed):
                if _is_service_bag_get(node):
                    violations.append(Violation(
                        path,
                        node.lineno,
                        "plugin-service-locator",
                        "plugin discovers runtime services through a service bag",
                    ))
                if (
                    isinstance(node, ast.ClassDef)
                    and node.name.endswith("Service")
                    and any(
                        isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and item.name == "__getattr__"
                        for item in node.body
                    )
                ):
                    violations.append(Violation(
                        path,
                        node.lineno,
                        "plugin-service-proxy",
                        f"{node.name} exposes implementation through __getattr__",
                    ))
                if (
                    owner != "agentloop"
                    and
                    isinstance(node, ast.Attribute)
                    and node.attr == "registry"
                    and (
                        isinstance(node.value, ast.Attribute)
                        and node.value.attr in {"agents", "jobs", "tools"}
                    )
                ):
                    violations.append(Violation(
                        path,
                        node.lineno,
                        "plugin-registry-leak",
                        f"plugin bypasses the {node.value.attr!r} public service",
                    ))
                if (
                    owner != "persistence"
                    and isinstance(node, ast.Attribute)
                    and node.attr == "state_store"
                ):
                    violations.append(Violation(
                        path,
                        node.lineno,
                        "plugin-persistence-coupling",
                        "capabilities consume persistence Protocols, not state_store",
                    ))
                if (
                    path not in {AGENTLOOP_EVENTS, TOOL_RUNTIME}
                    and
                    isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "Events"
                    and node.attr == "BEFORE_TOOL_CALL"
                ):
                    violations.append(Violation(
                        path,
                        node.lineno,
                        "plugin-tool-policy-event",
                        "tool policy belongs on one monotonic ctx.tools guard",
                    ))
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    modules = [node.module]
                else:
                    modules = []
                for module in modules:
                    violation = _public_import_violation(
                        path=path,
                        owner=owner,
                        node=node,
                        module=module,
                        plugin_roots=plugin_roots,
                    )
                    if violation is not None:
                        violations.append(violation)
    return violations


def check_plugin_reexports() -> list[Violation]:
    """Check only re-exports that cross plugin ownership boundaries."""
    violations: list[Violation] = []
    for owner in sorted(_plugin_roots()):
        path = PACKAGE / owner / "__init__.py"
        if not path.is_file():
            continue
        exports = _explicit_exports(path) or set()
        for node in _tree(path).body:
            if not isinstance(node, ast.ImportFrom) or not node.module:
                continue
            imported = {alias.asname or alias.name for alias in node.names}
            exposed = sorted(imported & exports)
            if not exposed:
                continue
            module_owner = _module_root(node.module)
            if module_owner != owner and module_owner not in SHARED_DECLARATION_ROOTS:
                if node.module.rsplit(".", 1)[-1] in PUBLIC_DECLARATION_MODULES:
                    continue
                violations.append(Violation(
                    path,
                    node.lineno,
                    "plugin-foreign-reexport",
                    f"re-exports {', '.join(exposed)} from {node.module}",
                ))
    return violations


def check_transport_host_boundaries() -> list[Violation]:
    """Transport adapters consume public host ports, never live runtimes."""
    violations: list[Violation] = []
    for node in ast.walk(_tree(ACP_AGENT)):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules = [node.module]
        else:
            modules = []
        for module in modules:
            if module in ACP_FORBIDDEN_MODULES:
                violations.append(Violation(
                    ACP_AGENT,
                    node.lineno,
                    "transport-runtime-import",
                    f"ACP imports concrete host implementation {module}",
                ))
        if (
            isinstance(node, ast.Attribute)
            and node.attr in ACP_FORBIDDEN_RUNTIME_ATTRIBUTES
        ):
            violations.append(Violation(
                ACP_AGENT,
                node.lineno,
                "transport-runtime-access",
                f"ACP accesses concrete runtime attribute {node.attr!r}",
            ))
    return violations


class _ContextAccessVisitor(ast.NodeVisitor):
    def __init__(self, context_name: str, root: ast.AST, path: Path) -> None:
        self.path = path
        self.context_name = context_name
        self.root = root
        self.services: dict[str, int] = {}
        self.locators: list[int] = []

    def _record(self, name: str, line: int) -> None:
        if name not in XCORE_CONTEXT_API:
            self.services.setdefault(name, line)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if node is not self.root and any(
            argument.arg == self.context_name
            for argument in (*node.args.args, *node.args.kwonlyargs)
        ):
            return
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.visit_FunctionDef(node)  # type: ignore[arg-type]

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.value, ast.Name) and node.value.id == self.context_name:
            if node.attr == "services":
                self.locators.append(node.lineno)
            else:
                self._record(node.attr, node.lineno)
        if (
            isinstance(node.value, ast.Attribute)
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "self"
            and node.value.attr == "ctx"
        ):
            if node.attr == "services":
                self.locators.append(node.lineno)
            else:
                self._record(node.attr, node.lineno)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if (
            isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == self.context_name
        ):
            self.locators.append(node.lineno)
            service = _literal_string(node.args[1], self.path)
            if service:
                self._record(service, node.lineno)
        if isinstance(node.func, ast.Attribute) and node.func.attr in {
            "get", "has", "require"
        } and node.args:
            owner = node.func.value
            is_context = (
                isinstance(owner, ast.Name) and owner.id == self.context_name
            ) or (
                isinstance(owner, ast.Attribute)
                and isinstance(owner.value, ast.Name)
                and owner.value.id == "self"
                and owner.attr == "ctx"
            )
            if is_context:
                service = _literal_string(node.args[0], self.path)
                if service:
                    self._record(service, node.lineno)
        self.generic_visit(node)


def _plugin_context_accesses(spec: PluginSpec) -> tuple[dict[str, int], list[int]]:
    tree = _tree(spec.path)
    name = spec.module.split(":", 1)[1]
    facet = next(node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name)
    functions = {node.name: node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
    queue = [(facet, set(spec.required | spec.optional | spec.provided))]
    seen = set()
    missing = {}
    locators = []
    while queue:
        node, inherited = queue.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        visitor = _ContextAccessVisitor("ctx", node, spec.path)
        visitor.visit(node)
        for service, line in visitor.services.items():
            if service not in inherited:
                missing.setdefault(service, line)
        locators.extend(visitor.locators)
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            if isinstance(call.func, ast.Name) and call.func.id in functions:
                queue.append((functions[call.func.id], inherited))
            if isinstance(call.func, ast.Attribute) and call.func.attr == "inject" and len(call.args) >= 2:
                required, optional = _inject_values(_resolved_declaration(call.args[0], tree), spec.path)
                callback = call.args[1]
                if isinstance(callback, ast.Call) and isinstance(callback.func, ast.Name) and callback.func.id == "partial":
                    callback = callback.args[0]
                if isinstance(callback, ast.Name) and callback.id in functions:
                    queue.append((functions[callback.id], inherited | required | optional))
            targets = [child.func.id for child in ast.walk(call) if isinstance(child, ast.Call) and isinstance(child.func, ast.Name) and child.func.id in classes]
            for target in targets:
                cls = classes[target]
                required, optional = _inject_values(_assignment_value(cls.body, "inject"), spec.path)
                apply = next((method for method in cls.body if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)) and method.name == "apply"), None)
                if apply is not None:
                    queue.append((apply, inherited | required | optional))
    return missing, locators


def _required_cycles(specs: list[PluginSpec], profile: str) -> list[list[PluginSpec]]:
    active = [spec for spec in specs if profile in spec.profiles]
    providers: dict[str, list[PluginSpec]] = {}
    for spec in active:
        for service in spec.provided:
            providers.setdefault(service, []).append(spec)
    edges = {
        spec: {
            provider
            for service in spec.required
            for provider in providers.get(service, [])
            if provider != spec
        }
        for spec in active
    }
    index = 0
    indexes: dict[PluginSpec, int] = {}
    lowlinks: dict[PluginSpec, int] = {}
    stack: list[PluginSpec] = []
    stacked: set[PluginSpec] = set()
    cycles: list[list[PluginSpec]] = []

    def visit(spec: PluginSpec) -> None:
        nonlocal index
        indexes[spec] = index
        lowlinks[spec] = index
        index += 1
        stack.append(spec)
        stacked.add(spec)
        for target in edges[spec]:
            if target not in indexes:
                visit(target)
                lowlinks[spec] = min(lowlinks[spec], lowlinks[target])
            elif target in stacked:
                lowlinks[spec] = min(lowlinks[spec], indexes[target])
        if lowlinks[spec] != indexes[spec]:
            return
        component: list[PluginSpec] = []
        while stack:
            member = stack.pop()
            stacked.remove(member)
            component.append(member)
            if member == spec:
                break
        if len(component) > 1:
            cycles.append(component)

    for spec in active:
        if spec not in indexes:
            visit(spec)
    return cycles


def check_plugin_dependencies() -> list[Violation]:
    violations: list[Violation] = []
    specs = _plugin_specs()
    seen_modules: set[str] = set()
    for spec in specs:
        if spec.module in seen_modules:
            continue
        seen_modules.add(spec.module)
        accesses, locators = _plugin_context_accesses(spec)
        declared = set(spec.required | spec.optional | spec.provided)
        for service, line in sorted(accesses.items()):
            if service not in declared:
                violations.append(Violation(
                    spec.path,
                    line,
                    "plugin-undeclared-service",
                    f"plugin reads service {service!r} without injecting it",
                ))
        for line in sorted(set(locators)):
            violations.append(Violation(
                spec.path,
                line,
                "plugin-service-locator",
                "plugin accesses a whole service bag or dynamic context attribute",
            ))
    for profile in ("process", "session", "thread"):
        for cycle in _required_cycles(specs, profile):
            ordered = sorted(cycle, key=lambda item: item.entry_id)
            violations.append(Violation(
                ordered[0].path,
                ordered[0].line,
                "plugin-inject-cycle",
                f"{profile} profile required-service cycle: "
                + " -> ".join(spec.entry_id for spec in ordered)
                + f" -> {ordered[0].entry_id}",
            ))
    return violations


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--scope",
        choices=("all", "loop", "tools", "plugins"),
        default="all",
    )
    args = parser.parse_args()
    checks = {
        "loop": lambda: [
            *check_engine(),
            *check_agentloop_event_context(),
            *check_agentloop_imports(),
            *check_runtime_ownership(),
            *check_inbox(),
        ],
        "tools": check_tools,
        "plugins": lambda: [
            *check_plugin_imports(),
            *check_plugin_reexports(),
            *check_plugin_dependencies(),
            *check_transport_host_boundaries(),
        ],
    }
    selected = checks.values() if args.scope == "all" else (checks[args.scope],)
    violations = sorted({item for check in selected for item in check()})
    if not violations:
        print("architecture boundaries: ok")
        return 0
    for violation in violations:
        print(violation.render())
    print(f"architecture boundaries: {len(violations)} violation(s)")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
