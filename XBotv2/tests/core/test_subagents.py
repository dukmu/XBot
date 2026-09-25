"""Subagents are owner-defined jobs, not a parallel lifecycle registry."""

from dataclasses import dataclass

import pytest

from XBotv2.subagents.contracts import SubagentAgentError
from XBotv2.subagents.service import AgentJobSpec, SubagentLauncher


@dataclass(frozen=True)
class _Definition:
    name: str
    mode: str


class _Catalog:
    def __init__(self, definition=None):
        self.definition = definition

    def get(self, _name):
        return self.definition


class _Session:
    def new_thread_id(self, name):
        return f"child-{name}"


class _Children:
    def __init__(self):
        self.request = None

    async def spawn(self, request, _lifecycle):
        self.request = request
        return "child"


def _launcher(definition):
    children = _Children()
    launcher = SubagentLauncher(
        catalog=_Catalog(definition),
        session=_Session(),
        children=children,
        lifecycle=object(),
        parent_permissions=object(),
        client_events=None,
    )
    return launcher, children


def test_agent_job_spec_carries_plugin_owned_kind_and_label():
    spec = AgentJobSpec(agent="explorer", prompt="inspect", label="Inspect")
    assert spec.kind == "subagent"
    assert spec.label == "Inspect"


@pytest.mark.asyncio
async def test_launcher_rejects_unknown_primary_and_empty_requests():
    for definition, prompt in (
        (None, "inspect"),
        (_Definition("default", "primary"), "inspect"),
        (_Definition("explorer", "subagent"), "   "),
    ):
        launcher, _children = _launcher(definition)
        with pytest.raises(SubagentAgentError):
            await launcher.spawn_subagent("explorer", prompt)


@pytest.mark.asyncio
async def test_launcher_passes_one_typed_child_request_to_application_owner():
    definition = _Definition("explorer", "subagent")
    launcher, children = _launcher(definition)
    child = await launcher.spawn_subagent("explorer", "inspect repository")
    assert child == "child"
    assert children.request.definition is definition
    assert children.request.thread_id == "child-explorer"
    assert children.request.prompt == "inspect repository"
