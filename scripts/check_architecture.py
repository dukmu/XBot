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
ENGINE = PACKAGE / "agentloop" / "engine.py"
TOOL_RUNTIME = PACKAGE / "agentloop" / "tool_runtime.py"
CORE_EVENTS = PACKAGE / "core" / "events.py"
SESSION = PACKAGE / "session" / "runtime.py"
BOOTSTRAP = PACKAGE / "bootstrap.py"
APPLICATION_PLUGIN = PACKAGE / "application" / "plugin.py"
APPLICATION_CONFIG = PACKAGE / "application_config"
SESSION_PLUGIN = PACKAGE / "session" / "plugin.py"
SESSION_ENTITY = PACKAGE / "session" / "session.py"
PERSISTENCE_PLUGIN = PACKAGE / "persistence" / "plugin.py"
USAGE_PLUGIN = PACKAGE / "usage" / "plugin.py"
APPLICATION_APP = PACKAGE / "application" / "app.py"
APPLICATION_BOOT = PACKAGE / "application" / "boot.py"
AGENT_OPERATIONS = PACKAGE / "agents" / "service_component.py"
APPLICATION_AGENTS = PACKAGE / "application" / "agents.py"
APPLICATION_MODEL = PACKAGE / "application" / "model.py"
AGENTS_SERVICE = PACKAGE / "agents" / "service.py"
APPLICATION_CONFIG_SEED = PACKAGE / "application" / "config_seed.py"
APPLICATION_TREE = PACKAGE / "application" / "tree.py"
SERVER_APP = PACKAGE / "application" / "server.py"
HTTP_SERVER = PACKAGE / "server" / "http.py"
ACP_AGENT = PACKAGE / "acp" / "xbot_agent.py"
XCORE_TREE = PACKAGE / "xcore.yaml"
TOOLS = (
    PACKAGE / "agentloop" / "tool_service.py",
    TOOL_RUNTIME,
    PACKAGE / "agentloop" / "tool_registry.py",
)

ENGINE_ALLOWED_XBOT_ROOTS = {"agentloop", "core"}
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
    "commands",
    "contracts",  # Transitional declaration module.
    "events",
    "invariant",
    "invariants",
    "protocol",
    "services",
    "types",
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
    "serial",
    "set",
    "state",
    "stop",
    "unset",
}
ACP_FORBIDDEN_MODULES = {
    "XBotv2.application.app",
    "XBotv2.config.loader",
    "XBotv2.persistence.store",
    "XBotv2.session.manager",
    "XBotv2.session.runtime",
    "XBotv2.session.session",
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


def _string_values(node: ast.AST | None) -> set[str]:
    """Extract string constants from a list/tuple node (empty on other nodes)."""
    if not isinstance(node, (ast.List, ast.Tuple)):
        return set()
    return {
        item.value
        for item in node.elts
        if isinstance(item, ast.Constant) and isinstance(item.value, str)
    }


def _literal_string(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
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


def _inject_values(node: ast.AST | None) -> tuple[set[str], set[str]]:
    if isinstance(node, (ast.List, ast.Tuple)):
        return _string_values(node), set()
    if not isinstance(node, ast.Dict):
        return set(), set()
    required: set[str] = set()
    optional: set[str] = set()
    for key, value in zip(node.keys, node.values):
        label = _literal_string(key)
        if label == "required":
            required.update(_string_values(value))
        elif label == "optional":
            optional.update(_string_values(value))
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
    path = _module_path(f"XBotv2.{module}")
    if path is None:
        return None
    if path.name == "__init__.py":
        plugin_path = path.parent / "plugin.py"
        if plugin_path.is_file():
            return plugin_path
    return path


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


def _provided_services(node: ast.AST) -> set[str]:
    services: set[str] = set()
    for item in ast.walk(node):
        if not isinstance(item, ast.Call) or not isinstance(item.func, ast.Attribute):
            continue
        if item.func.attr != "set" or not item.args:
            continue
        service = _literal_string(item.args[0])
        if service:
            services.add(service)
    return services


def _plugin_specs() -> list[PluginSpec]:
    document = yaml.safe_load(XCORE_TREE.read_text(encoding="utf-8")) or []
    specs: list[PluginSpec] = []
    for entry in document:
        if not isinstance(entry, dict) or not entry.get("name"):
            continue
        module = str(entry["name"])
        path = _plugin_source(module)
        if path is None:
            continue
        plugin_class = _plugin_class(path)
        if plugin_class is None:
            continue
        required, optional = _inject_values(
            _assignment_value(plugin_class.body, "inject")
        )
        raw_profiles = entry.get("profiles")
        profiles = (
            frozenset({str(raw_profiles)})
            if isinstance(raw_profiles, str)
            else frozenset(str(item) for item in raw_profiles)
            if isinstance(raw_profiles, list)
            else frozenset({"agent"})
        )
        specs.append(PluginSpec(
            entry_id=str(entry.get("id") or module),
            module=module,
            profiles=profiles,
            path=path,
            line=plugin_class.lineno,
            required=frozenset(required),
            optional=frozenset(optional),
            provided=frozenset(_provided_services(plugin_class)),
        ))
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


def check_core_event_context() -> list[Violation]:
    violations: list[Violation] = []
    for node in _tree(CORE_EVENTS).body:
        if not isinstance(node, ast.ClassDef) or node.name != "EventContext":
            continue
        for item in node.body:
            if (
                isinstance(item, ast.AnnAssign)
                and isinstance(item.target, ast.Name)
                and item.target.id in EVENT_CONTEXT_FORBIDDEN_PLUGIN_FIELDS
            ):
                violations.append(Violation(
                    CORE_EVENTS,
                    item.lineno,
                    "core-plugin-payload",
                    f"EventContext names plugin operation {item.target.id!r}",
                ))
    return violations


def check_agentloop_imports() -> list[Violation]:
    violations: list[Violation] = []
    root = PACKAGE / "agentloop"
    plugin = root / "plugin.py"
    if plugin.exists():
        violations.append(Violation(
            plugin,
            1,
            "loop-plugin",
            "application/plugin composition must not live in agentloop",
        ))
    agent_registry = root / "agent_registry.py"
    if agent_registry.exists():
        violations.append(Violation(
            agent_registry,
            1,
            "loop-agent-registry",
            "Agent registry belongs to application ownership, not the loop",
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
            "cross-capability use cases belong to application ownership",
        ))
    for path in sorted(root.glob("*.py")):
        if path.name == "protocol.py":
            continue
        violations.extend(
            _imports(path, ENGINE_ALLOWED_XBOT_ROOTS, "loop-import")
        )
    return violations


def check_application_startup() -> list[Violation]:
    """Keep application startup under its sole owning package."""
    violations: list[Violation] = []
    if BOOTSTRAP.exists():
        violations.append(Violation(
            BOOTSTRAP,
            1,
            "application-startup",
            "remove the legacy bootstrap module; startup belongs to XBotv2.application",
        ))
    if APPLICATION_PLUGIN.exists():
        violations.append(Violation(
            APPLICATION_PLUGIN,
            1,
            "application-plugin",
            "application is an app/composition root, not a feature plugin",
        ))
    if APPLICATION_CONFIG.exists() and any(APPLICATION_CONFIG.rglob("*.py")):
        violations.append(Violation(
            APPLICATION_CONFIG,
            1,
            "application-config-plugin",
            "launch facts belong to application; user configuration belongs to config",
        ))
    if APPLICATION_CONFIG_SEED.exists():
        violations.append(Violation(
            APPLICATION_CONFIG_SEED,
            1,
            "application-config-ownership",
            "configuration materialization belongs to XBotv2.config",
        ))
    if APPLICATION_AGENTS.exists():
        violations.append(Violation(
            APPLICATION_AGENTS,
            1,
            "application-agent-service",
            "Agent registry and creation belong to the agents service",
        ))
    if APPLICATION_MODEL.exists():
        violations.append(Violation(
            APPLICATION_MODEL,
            1,
            "application-model-service",
            "the selected model binding belongs to the LLM service",
        ))
    if "application_config" in XCORE_TREE.read_text(encoding="utf-8"):
        violations.append(Violation(
            XCORE_TREE,
            1,
            "application-config-plugin",
            "the plugin tree must not carry application launch facts",
        ))
    for node in ast.walk(_tree(APPLICATION_APP)):
        if isinstance(node, ast.ImportFrom) and node.module == "XBotv2.loader":
            violations.append(Violation(
                APPLICATION_APP,
                node.lineno,
                "application-profile-mixing",
                "Agent startup must delegate plugin-profile composition",
            ))
        if (
            isinstance(node, ast.ImportFrom)
            and node.module
            and (
                node.module.startswith("XBotv2.agentloop")
                or node.module == "XBotv2.agents.service"
                or node.module == "XBotv2.llm.service"
            )
        ):
            violations.append(Violation(
                APPLICATION_APP,
                node.lineno,
                "application-instance-construction",
                f"application imports concrete runtime service {node.module}",
            ))
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "Engine"
        ):
            violations.append(Violation(
                APPLICATION_APP,
                node.lineno,
                "application-engine-construction",
                "application must create Agents through ctx.agents",
            ))
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and "plugin_tree" in node.name
        ):
            violations.append(Violation(
                APPLICATION_APP,
                node.lineno,
                "application-profile-mixing",
                "plugin-tree parsing belongs to the application tree module",
            ))
    for node in ast.walk(_tree(APPLICATION_BOOT)):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "boot_application"
            and any(arg.arg == "services" for arg in node.args.kwonlyargs)
        ):
            violations.append(Violation(
                APPLICATION_BOOT,
                node.lineno,
                "application-boot-mixing",
                "generic boot must receive app preparation, not a service bag",
            ))
    for node in ast.walk(_tree(APPLICATION_TREE)):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "resolve_llm_config"
        ):
            violations.append(Violation(
                APPLICATION_TREE,
                node.lineno,
                "application-config-ownership",
                "applications consume mounted LLM services; they do not export config readers",
            ))
    tree_source = XCORE_TREE.read_text(encoding="utf-8")
    for required in ("agents.service_component", "agentloop.factory"):
        if required not in tree_source:
            violations.append(Violation(
                XCORE_TREE,
                1,
                "application-agent-factory",
                f"default composition must mount {required}",
            ))
    for node in ast.walk(_tree(AGENTS_SERVICE)):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.startswith("XBotv2.agentloop")
            and node.module.rsplit(".", 1)[-1] not in PUBLIC_DECLARATION_MODULES
        ):
            violations.append(Violation(
                AGENTS_SERVICE,
                node.lineno,
                "agents-loop-construction",
                "agents service must delegate through its registered factory",
            ))
    for node in ast.walk(_tree(AGENT_OPERATIONS)):
        if not isinstance(node, ast.Attribute):
            continue
        is_service_access = (
            isinstance(node.value, ast.Name)
            and node.value.id == "services"
        ) or (
            isinstance(node.value, ast.Attribute)
            and node.value.attr == "services"
        )
        if is_service_access and node.attr in {"llm", "model", "state_store"}:
            violations.append(Violation(
                AGENT_OPERATIONS,
                node.lineno,
                "application-agent-reassembly",
                f"Agent operations must not assemble runtime service {node.attr!r}",
            ))
        if node.attr in {"apply_definition", "apply_provider", "apply_tools"}:
            violations.append(Violation(
                AGENT_OPERATIONS,
                node.lineno,
                "application-agent-reassembly",
                f"Agent operations must use a high-level agents service, not {node.attr}()",
            ))
        if (
            node.attr == "configure"
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "engine"
        ):
            violations.append(Violation(
                AGENT_OPERATIONS,
                node.lineno,
                "application-agent-reassembly",
                "Agent operations must not configure Engine directly",
            ))
    for path in (SERVER_APP,):
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.ImportFrom) and node.module == "XBotv2.loader":
                violations.append(Violation(
                    path,
                    node.lineno,
                    "application-profile-mixing",
                    "server startup must delegate profile composition",
                ))
    for node in ast.walk(_tree(HTTP_SERVER)):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module
            and node.module in {
                "XBotv2.application",
                "XBotv2.application.tree",
                "XBotv2.config.tree",
            }
        ):
            violations.append(Violation(
                HTTP_SERVER,
                node.lineno,
                "protocol-application-config",
                "the protocol consumes injected services; it must not parse an application profile",
            ))
    for path in (PACKAGE / "application").glob("*.py"):
        for node in ast.walk(_tree(path)):
            if (
                isinstance(node, ast.Constant)
                and node.value == "application_config"
            ):
                violations.append(Violation(
                    path,
                    node.lineno,
                    "application-config-roundtrip",
                    "application must not round-trip launch facts through a plugin",
                ))
    for path in (SESSION_PLUGIN, SESSION_ENTITY):
        for node in ast.walk(_tree(path)):
            if (
                isinstance(node, ast.Constant)
                and node.value == "engine_factory"
            ) or (
                isinstance(node, ast.Name)
                and node.id == "engine_factory"
            ) or (
                isinstance(node, ast.Attribute)
                and node.attr == "engine_factory"
            ):
                violations.append(Violation(
                    path,
                    node.lineno,
                    "application-instance-factory",
                    "session must request child applications, not construct Engines",
                ))
    session_source = SESSION_PLUGIN.read_text(encoding="utf-8")
    if "LoopState(" not in session_source:
        violations.append(Violation(
            SESSION_PLUGIN,
            1,
            "session-state-ownership",
            "session must create the core LoopState",
        ))
    for node in ast.walk(_tree(PERSISTENCE_PLUGIN)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "LoopState"
        ):
            violations.append(Violation(
                PERSISTENCE_PLUGIN,
                node.lineno,
                "persistence-state-ownership",
                "persistence may hydrate LoopState but must not construct it",
            ))
    for node in ast.walk(_tree(USAGE_PLUGIN)):
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "state_store"
        ) or (
            isinstance(node, ast.Constant)
            and node.value == "state_store"
        ):
            violations.append(Violation(
                USAGE_PLUGIN,
                node.lineno,
                "usage-persistence-coupling",
                "usage owns its snapshot and must not depend on state_store",
            ))
    for node in ast.walk(_tree(SESSION_ENTITY)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if (
                node.func.attr == "get"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value in {"engine", "usage"}
            ):
                violations.append(Violation(
                    SESSION_ENTITY,
                    node.lineno,
                    "session-feature-service",
                    "session identity must not discover Engine or usage services",
                ))
        if isinstance(node, ast.ClassDef) and "Engine" in node.name:
            violations.append(Violation(
                SESSION_ENTITY,
                node.lineno,
                "application-child-lifecycle",
                "child Engine lifecycle belongs to application ownership",
            ))
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
        if "__pycache__" in path.parts:
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
    roots.update(spec.module.split(".", 1)[0] for spec in _plugin_specs())
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
    if imported is None or imported == owner or imported in SHARED_DECLARATION_ROOTS:
        return None
    if imported == "protocol":
        if owner in TRANSPORT_ROOTS or path.name == "protocol.py":
            return None
        return Violation(
            path,
            node.lineno,
            "plugin-wire-import",
            f"capability package imports wire contract {module}",
        )
    if imported not in plugin_roots:
        return None
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
    if module.rsplit(".", 1)[-1] not in PUBLIC_DECLARATION_MODULES:
        return Violation(
            path,
            node.lineno,
            "plugin-concrete-import",
            f"imports plugin implementation {module}",
        )
    declaration_path = _module_path(module)
    exports = _explicit_exports(declaration_path) if declaration_path else None
    if exports is None:
        return Violation(
            path,
            node.lineno,
            "plugin-implicit-public-module",
            f"public declaration module {module} has no explicit __all__",
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


def check_plugin_imports() -> list[Violation]:
    violations: list[Violation] = []
    plugin_roots = _plugin_roots()
    for owner in sorted(plugin_roots):
        root = PACKAGE / owner
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts:
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
                    path != TOOL_RUNTIME
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
    """Package roots may re-export declarations, never implementations."""
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
            if module_owner != owner:
                violations.append(Violation(
                    path,
                    node.lineno,
                    "plugin-foreign-reexport",
                    f"re-exports {', '.join(exposed)} from {node.module}",
                ))
                continue
            if node.module.rsplit(".", 1)[-1] not in PUBLIC_DECLARATION_MODULES:
                violations.append(Violation(
                    path,
                    node.lineno,
                    "plugin-concrete-reexport",
                    f"re-exports implementation {', '.join(exposed)} from {node.module}",
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
    def __init__(self, context_name: str, root: ast.AST) -> None:
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
            service = _literal_string(node.args[1])
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
                service = _literal_string(node.args[0])
                if service:
                    self._record(service, node.lineno)
        self.generic_visit(node)


def _plugin_context_accesses(spec: PluginSpec) -> tuple[dict[str, int], list[int]]:
    plugin_class = _plugin_class(spec.path)
    if plugin_class is None:
        return {}, []
    accesses: dict[str, int] = {}
    locators: list[int] = []
    apply = next(
        (
            node
            for node in plugin_class.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "apply"
        ),
        None,
    )
    if apply is not None:
        arguments = [*apply.args.args, *apply.args.kwonlyargs]
        context_name = next(
            (argument.arg for argument in arguments if argument.arg == "ctx"),
            "ctx",
        )
        visitor = _ContextAccessVisitor(context_name, apply)
        visitor.visit(apply)
        accesses.update(visitor.services)
        locators.extend(visitor.locators)
    for node in ast.walk(plugin_class):
        if not isinstance(node, ast.Attribute):
            continue
        if (
            isinstance(node.value, ast.Attribute)
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "self"
            and node.value.attr == "ctx"
        ):
            if node.attr == "services":
                locators.append(node.lineno)
            elif node.attr not in XCORE_CONTEXT_API:
                accesses.setdefault(node.attr, node.lineno)
    return accesses, locators


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
    for profile in ("agent", "server", "session-host"):
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
            *check_core_event_context(),
            *check_agentloop_imports(),
            *check_application_startup(),
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
