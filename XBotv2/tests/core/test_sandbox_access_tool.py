"""Tests for ``request_sandbox_access`` agent tool."""

from __future__ import annotations

import json

import pytest

from XBotv2.permissions import (
    Allowed,
    Denied,
    NamedPermission,
    PermissionRequest,
)
from XBotv2.sandbox.tools import (
    AccessLiteral,
    RequestSandboxAccessTool,
    Source,
)


class _RecordingApply:
    def __init__(self) -> None:
        self.calls: list[tuple[PermissionRequest, Allowed | Denied]] = []

    async def __call__(self, request, decision):
        # Mirror the real apply contract: return the (possibly modified)
        # Approval so request_decision can read ``decision.kind``.
        self.calls.append((request, decision))
        return decision


class _FakeApproval:
    def __init__(self, response) -> None:
        self.response = response
        self.requests: list[PermissionRequest] = []

    async def request(self, request: PermissionRequest):
        self.requests.append(request)
        return self.response


def _make_tool(approval_response=None):
    approval = _FakeApproval(approval_response)
    apply = _RecordingApply()
    tool = RequestSandboxAccessTool(approval, apply_decision=apply)
    return tool, approval, apply


def _outcome_text(outcome) -> str:
    parts = getattr(outcome, "output", None)
    if parts is None:
        return ""
    return "".join(
        getattr(part, "text", "")
        for part in getattr(parts, "parts", ())
    )


class TestSchema:
    def test_access_literal_matches_path_access_values(self):
        assert AccessLiteral.__args__ == ("allow", "readwrite", "readonly", "deny")

    def test_tool_has_request_sandbox_access_name(self):
        tool, _approval, _apply = _make_tool(approval_response=Allowed(scope="once"))
        from XBotv2.core.tools import Tool
        assert isinstance(tool.as_tool(), Tool)
        assert tool.as_tool().name == "request_sandbox_access"

    def test_schema_advertises_path_access_reason(self):
        tool, _approval, _apply = _make_tool(approval_response=Allowed(scope="once"))
        schema = tool.as_tool().parameters
        props = schema["properties"]
        assert "path" in props and "access" in props and "reason" in props
        # ``path`` and ``reason`` are free-form strings; ``access`` is the
        # four-value enum that lets the runtime reject typos.
        assert props["access"]["enum"] == ["allow", "readwrite", "readonly", "deny"]


class TestInputValidation:
    @pytest.mark.parametrize(
        "path, access, reason",
        [
            ("", "readonly", "x"),
            ("   ", "readonly", "x"),
            ("/tmp", "bogus", "x"),
                ("/tmp", "readonly", ""),
            ("/tmp", "readonly", "   "),
        ],
    )
    @pytest.mark.asyncio
    async def test_invalid_request_rejected_before_approval(self, path, access, reason):
        tool, approval, apply = _make_tool(approval_response=Allowed(scope="once"))
        outcome = await tool.invoke(path=path, access=access, reason=reason)
        assert outcome.error.code == "invalid_sandbox_request"
        # Approval must NOT have been consulted for invalid input — saving
        # the human a pointless interaction.
        assert approval.requests == []
        assert apply.calls == []

    @pytest.mark.asyncio
    async def test_reason_is_stripped_before_check(self):
        tool, approval, _apply = _make_tool(approval_response=Allowed(scope="once"))
        outcome = await tool.invoke(path="/tmp", access="readonly", reason="\t ")
        assert outcome.error.code == "invalid_sandbox_request"


class TestApprovalPath:
    @pytest.mark.asyncio
    async def test_human_denial_yields_sandbox_access_denied(self):
        tool, _approval, apply = _make_tool(
            approval_response=Denied(reason="not needed"),
        )
        outcome = await tool.invoke(
            path="/data", access="readonly", reason="need it",
        )
        assert outcome.error.code == "sandbox_access_denied"
        # ``request_decision`` calls apply unconditionally; the sandbox
        # apply callable is expected to be a no-op pass-through on denial.
        assert len(apply.calls) == 1
        _request, decision = apply.calls[0]
        assert isinstance(decision, Denied)

    @pytest.mark.asyncio
    async def test_approval_payload_uses_named_permission_with_resource(self):
        tool, approval, _apply = _make_tool(approval_response=Allowed(scope="once"))
        await tool.invoke(path="/data", access="readonly", reason="for foo")
        assert len(approval.requests) == 1
        request = approval.requests[0]
        assert request.source == Source
        assert isinstance(request.subject, NamedPermission)
        assert request.subject.tool == "sandbox.add_resource"
        assert request.subject.params == {"path": "/data", "access": "readonly"}
        assert request.reason == "for foo"

    @pytest.mark.asyncio
    async def test_grant_calls_apply_with_allowed_decision(self):
        tool, approval, apply = _make_tool(
            approval_response=Allowed(scope="session"),
        )
        outcome = await tool.invoke(
            path="/data", access="readonly", reason="for foo",
        )
        assert len(apply.calls) == 1
        request, decision = apply.calls[0]
        assert isinstance(decision, Allowed) and decision.scope == "session"
        assert request is approval.requests[0]
        text = _outcome_text(outcome)
        assert "granted" in text.lower()
        assert "/data" in text
        assert "readonly" in text
        assert "session" in text

    @pytest.mark.asyncio
    async def test_grant_writes_session_resource_via_settings(self):
        """On approval, ``update_policy`` is called with the new resource
        list so the sandbox policy actually allows the path.
        """
        from XBotv2.sandbox.tools import build_apply_sandbox_access

        captured: dict[str, object] = {}

        from types import SimpleNamespace

        class _FakeSettings:
            def policy(self):
                return SimpleNamespace(policy={"sandbox": {}})

            async def update_policy(self, patch):
                captured["patch"] = patch
                return SimpleNamespace(policy={"sandbox": {"resources": patch.sandbox.get("resources", [])}})

        tool = RequestSandboxAccessTool(
            _FakeApproval(Allowed(scope="once")),
            apply_decision=build_apply_sandbox_access(_FakeSettings()),
        )
        outcome = await tool.invoke(
            path="/data", access="readwrite", reason="for foo",
        )
        assert not getattr(outcome, "error", None)
        assert "patch" in captured
        assert captured["patch"].sandbox == {
            "resources": [{"path": "/data", "access": "readwrite"}],
        }

    @pytest.mark.asyncio
    async def test_grant_inserts_new_rule_at_head_of_session_resources(self):
        """The new rule is appended at the head of the session resources,
        matching ``/sandbox add`` exactly. The tool never dedups or replaces
        prior rules — that is left to ``/sandbox remove``.
        """
        from XBotv2.sandbox.tools import build_apply_sandbox_access

        from types import SimpleNamespace

        class _FakeSettings:
            def __init__(self):
                self.updates: list[dict[str, object]] = []

            def policy(self):
                return SimpleNamespace(
                    policy={
                        "sandbox": {
                            "resources": [
                                {"path": "/old", "access": "readonly"},
                                {"path": "/other", "access": "readonly"},
                            ],
                        },
                    },
                )

            async def update_policy(self, patch):
                self.updates.append(patch.sandbox)
                return SimpleNamespace(policy=patch.sandbox)

        settings = _FakeSettings()
        tool = RequestSandboxAccessTool(
            _FakeApproval(Allowed(scope="once")),
            apply_decision=build_apply_sandbox_access(settings),
        )
        await tool.invoke(path="/data", access="readwrite", reason="upgrade")
        assert len(settings.updates) == 1
        new_resources = settings.updates[0]["resources"]
        assert new_resources == [
            {"path": "/data", "access": "readwrite"},
            {"path": "/old", "access": "readonly"},
            {"path": "/other", "access": "readonly"},
        ]

    @pytest.mark.asyncio
    async def test_grant_does_not_silently_replace_prior_rule_for_same_path(self):
        """If the operator already granted the same path with a different
        access, the new rule is appended, not deduped. ``/sandbox remove``
        is the only path that shrinks the list. (Test pinpoints the design
        choice; flip it deliberately if the team wants replacement instead.)
        """
        from XBotv2.sandbox.tools import build_apply_sandbox_access

        from types import SimpleNamespace

        class _FakeSettings:
            def __init__(self):
                self.updates: list[dict[str, object]] = []

            def policy(self):
                return SimpleNamespace(
                    policy={
                        "sandbox": {
                            "resources": [
                                {"path": "/data", "access": "readonly"},
                            ],
                        },
                    },
                )

            async def update_policy(self, patch):
                self.updates.append(patch.sandbox)
                return SimpleNamespace(policy=patch.sandbox)

        settings = _FakeSettings()
        tool = RequestSandboxAccessTool(
            _FakeApproval(Allowed(scope="once")),
            apply_decision=build_apply_sandbox_access(settings),
        )
        await tool.invoke(path="/data", access="readwrite", reason="upgrade")
        assert settings.updates[0]["resources"] == [
            {"path": "/data", "access": "readwrite"},
            {"path": "/data", "access": "readonly"},
        ]

    @pytest.mark.asyncio
    async def test_request_id_is_unique_and_prefixed(self):
        tool, approval, _apply = _make_tool(approval_response=Allowed(scope="once"))
        await tool.invoke(path="/a", access="readonly", reason="r1")
        await tool.invoke(path="/b", access="readonly", reason="r2")
        ids = [r.interaction_id for r in approval.requests]
        assert len(ids) == 2
        assert ids[0] != ids[1]
        assert all(rid.startswith("sandbox-access:") for rid in ids)
        assert all(len(rid.split(":", 1)[1]) == 16 for rid in ids)


class TestApply:
    @pytest.mark.asyncio
    async def test_request_decision_is_called_with_apply(self):
        """``request_decision`` must pass the apply callable through so an
        Allowed outcome invokes it before the tool returns success. This
        guards against a future refactor that drops the apply hook.
        """
        seen: list[tuple[PermissionRequest, Allowed | Denied]] = []

        async def apply(request, decision):
            seen.append((request, decision))
            return decision

        approval = _FakeApproval(Allowed(scope="once"))
        tool = RequestSandboxAccessTool(approval, apply_decision=apply)
        await tool.invoke(path="/x", access="readonly", reason="why")
        assert len(seen) == 1
