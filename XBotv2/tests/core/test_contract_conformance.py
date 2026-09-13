"""Contract declarations must be tied to their implementations.

Every Protocol declared in a ``contracts.py`` module is walked; each concrete
class that explicitly inherits it must satisfy all declared members. A
protocol with an implementation that never inherits it is a gap the suite
flags instead of letting the two drift apart silently.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from pathlib import Path

import pytest
from typing import Protocol

import XBotv2

_CORE = Path(__file__).resolve().parents[2]  # XBotv2/
_PROTOCOL_MODULES = sorted(
    str(path.relative_to(_CORE)).replace("/", ".").removesuffix(".py")
    for path in _CORE.glob("*/contracts.py")
)


def _import_package_tree() -> None:
    """Load every XBotv2 module so ``__subclasses__`` can see implementors."""
    seen = set()
    for info in pkgutil.walk_packages(XBotv2.__path__, XBotv2.__name__ + "."):
        if info.name in seen:
            continue
        seen.add(info.name)
        try:
            importlib.import_module(info.name)
        except ImportError:
            pass  # optional/closed external deps (e.g. providers, acp plugin)


def _declared_members(protocol) -> dict[str, object]:
    members: dict[str, object] = {}
    for base in protocol.__mro__:
        for name, value in base.__dict__.items():
            if name.startswith("_"):
                continue
            members[name] = value
    for name in getattr(protocol, "__annotations__", {}):
        members.setdefault(name, None)
    return members


def _implementors(protocol):
    seen: set[type] = set()
    stack = [protocol]
    while stack:
        cls = stack.pop()
        for sub in cls.__subclasses__():
            if sub not in seen:
                seen.add(sub)
                stack.append(sub)
    return sorted(seen, key=lambda cls: f"{cls.__module__}.{cls.__name__}")


@pytest.fixture(scope="module", autouse=True)
def _loaded_package():
    _import_package_tree()
    yield


@pytest.mark.parametrize("module_name", _PROTOCOL_MODULES)
def test_every_protocol_is_satisfied_by_its_implementors(module_name, _loaded_package):
    module = importlib.import_module(module_name)
    problems: list[str] = []
    for name, protocol in vars(module).items():
        if not inspect.isclass(protocol) or not issubclass(protocol, Protocol):
            continue
        members = _declared_members(protocol)
        implementors = _implementors(protocol)
        for cls in implementors:
            for member, value in members.items():
                if inspect.isfunction(value) or callable(getattr(protocol, member, None)):
                    if not callable(getattr(cls, member, None)):
                        problems.append(
                            f"{cls.__module__}.{cls.__name__} missing callable "
                            f"'{member}' declared by {name}"
                        )
    assert not problems, "\n".join(problems)


# Protocols whose implementations cannot inherit them: concrete views over an
# application, adapters handed to third-party surfaces, or classes whose base
# is fixed elsewhere.  The manifest keeps each declaration tied to the code
# that fulfils it, so a member added to the protocol fails here too.
_BOUND_IMPLEMENTATIONS: dict[str, tuple[tuple[str, str], ...]] = {
    "XBotv2.agentloop.contracts.InboxSink": (
        ("XBotv2.persistence.store", "InboxStore"),
    ),
    "XBotv2.application.contracts.AgentApplicationPort": (
        ("XBotv2.application.host", "MountedAgentApplication"),
    ),
    "XBotv2.application.contracts.ApplicationEventsPort": (
        ("xcore", "Context"),
    ),
    "XBotv2.application.contracts.ArtifactStorePort": (
        ("XBotv2.core.filesystem.artifacts", "ArtifactStore"),
    ),
    "XBotv2.application.contracts.SessionHistoryPort": (
        ("XBotv2.session.session", "Session"),
    ),
    "XBotv2.application.contracts.InteractionWaiterPort": (
        ("XBotv2.interactions.interactions", "InteractionWaiter"),
    ),
    "XBotv2.application.contracts.ChildApplicationsPort": (
        ("XBotv2.application.child", "ChildApplications"),
    ),
    "XBotv2.application.contracts.ChildApplication": (
        ("XBotv2.application.child", "ChildApplicationSession"),
    ),
    "XBotv2.context_builder.contracts.PromptFragmentRegistry": (
        ("XBotv2.context_builder.builder", "ContextBuilder"),
    ),

    "XBotv2.jobs.contracts.TextOutputStorePort": (
        ("XBotv2.jobs.output", "TextOutputStore"),
    ),
    "XBotv2.jobs.contracts.JobOutputFactoryPort": (
        ("XBotv2.jobs.runner", "_OutputFactory"),
    ),
    "XBotv2.jobs.contracts.JobsCommandPort": (
        ("XBotv2.jobs.registry", "JobRegistry"),
    ),
    "XBotv2.persistence.contracts.StatePort": (
        ("xcore.state", "StateService"),
    ),
    "XBotv2.persistence.contracts.ThreadPersistenceFactory": (
        ("XBotv2.persistence.plugin", "thread_persistence_factory"),
    ),
    "XBotv2.session.contracts.AgentApplicationFactory": (
        ("XBotv2.application.app", "create_agent_application"),
    ),
    "XBotv2.workspaces.contracts.WorkspaceEventSubscription": (
        ("XBotv2.workspaces.events", "WorkspaceEventSubscription"),
    ),
}


@pytest.mark.parametrize("protocol_path", sorted(_BOUND_IMPLEMENTATIONS))
def test_bound_implementation_satisfies_every_declared_member(
    protocol_path, _loaded_package
):
    module_name, _, protocol_name = protocol_path.rpartition(".")
    protocol = getattr(importlib.import_module(module_name), protocol_name)
    members = _declared_members(protocol)
    problems: list[str] = []
    for implementation_path in _BOUND_IMPLEMENTATIONS[protocol_path]:
        implementation = _resolve(implementation_path)
        for member, value in members.items():
            declared_callable = callable(value) or callable(
                getattr(protocol, member, None)
            )
            actual = getattr(implementation, member, None)
            if declared_callable and not callable(actual):
                problems.append(
                    f"{implementation_path[0]}.{implementation_path[1]} missing "
                    f"callable '{member}' declared by {protocol_name}"
                )
            elif not declared_callable and actual is None:
                problems.append(
                    f"{implementation_path[0]}.{implementation_path[1]} missing "
                    f"attribute '{member}' declared by {protocol_name}"
                )
    assert not problems, "\n".join(problems)


def _resolve(path: tuple[str, str]):
    return getattr(importlib.import_module(path[0]), path[1])
