"""Agent-facing tool owned by the sandbox component.

The ``request_sandbox_access`` tool is the agent-initiated counterpart to
``/sandbox add``: it presents a single sandbox resource rule to the human
through the permission approval flow, and when the human grants it the
requested rule is merged into the session-level sandbox policy.

All policy mutations live here (in :func:`build_apply_sandbox_access`); the
plugin only wires dependencies and registers the resulting Tool. State
changes go through ``SettingsPort.update_policy`` so the same persistence
and lifecycle guarantees apply as for ``/sandbox add`` (``POLICY_CHANGED``
is emitted, the rule survives resume, ``/sandbox reset`` still removes it).

The ``XBotv2.config`` import is deferred into the apply factory to dodge a
package-level circular import (``config.contracts`` triggers
``context_builder`` indirectly through ``agentloop``).
"""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Literal

from pydantic import JsonValue

from XBotv2.core.parts import TextPart
from XBotv2.core.tools import Tool, ToolError, ToolFailed, ToolOutput, ToolSucceeded
from XBotv2.permissions import (
    Allowed,
    Approval,
    ApprovalPort,
    NamedPermission,
    PermissionRequest,
)
from XBotv2.permissions.approval import request_decision
from XBotv2.sandbox.contracts import PathAccess, SandboxResourceConfig

if TYPE_CHECKING:
    from XBotv2.config import PatchPolicy, SettingsPort


# Identifier used both as the request source (so the client UI can branch
# on it) and in tests. Keep it a module-level constant.
Source = "request_sandbox_access"

# Literal accepted by the tool signature. Mirrors ``PathAccess`` plus the
# ``allow`` alias documented in ``SandboxConfig.resources``.
AccessLiteral = Literal["allow", "readwrite", "readonly", "deny"]

_ACCESS_BY_NAME: dict[str, PathAccess] = {
    "allow": "allow",
    "readwrite": "readwrite",
    "rw": "readwrite",
    "readonly": "readonly",
    "ro": "readonly",
    "deny": "deny",
}

# ``request_decision`` expects the apply callable to return an Approval;
# the sandbox-specific apply passes the decision through unchanged and
# performs its policy mutation as a side effect.
ApplyDecision = Callable[
    [PermissionRequest, Approval], Awaitable[Approval]
]


class RequestSandboxAccessTool:
    """Agent Tool handler with explicit permission and settings dependencies."""

    def __init__(
        self,
        approval: ApprovalPort,
        *,
        apply_decision: ApplyDecision,
    ) -> None:
        self._approval = approval
        self._apply_decision = apply_decision

    async def invoke(
        self,
        path: str,
        access: AccessLiteral = "readonly",
        reason: str = "",
    ) -> ToolSucceeded | ToolFailed:
        """Ask the human to widen the session sandbox for one resource.

        path: Absolute or workspace-relative path to grant access to.
        access: Required access level for the resource (``allow`` /
            ``readwrite`` / ``readonly`` / ``deny``).
        reason: Explain why the upcoming tool call needs this access.

        The human approves the request through the permission UI. The
        scope the human picks (once / session) is *ignored* — sandbox
        rules have no "one-shot" model, so a granted rule is always
        appended to the session-level sandbox ``resources`` list via
        ``/sandbox add``. ``/sandbox resources`` lists it and
        ``/sandbox remove <index>`` reverts it. Denial leaves the policy
        untouched. The tool does not change sandbox enforcement paths
        beyond the granted rule.
        """
        try:
            normalized_path, access_value = _normalize_request(path, access)
        except ValueError as exc:
            return ToolFailed(
                error=ToolError(code="invalid_sandbox_request", message=str(exc)),
                output=ToolOutput(),
            )
        if not reason.strip():
            return ToolFailed(
                error=ToolError(
                    code="invalid_sandbox_request",
                    message="reason must not be empty",
                ),
                output=ToolOutput(),
            )

        request = PermissionRequest(
            interaction_id=f"sandbox-access:{secrets.token_hex(8)}",
            source=Source,
            subject=NamedPermission(
                tool="sandbox.add_resource",
                params={
                    "path": normalized_path,
                    "access": access_value,
                },
            ),
            reason=reason,
            resume_supported=False,
        )
        decision = await request_decision(
            self._approval, request, self._apply_decision,
        )
        if not isinstance(decision, Allowed):
            return ToolFailed(
                error=ToolError(
                    code="sandbox_access_denied",
                    message="Sandbox access request was denied.",
                ),
                output=ToolOutput(),
            )
        return ToolSucceeded(
            output=ToolOutput(
                parts=(TextPart(text=(
                    f"Sandbox access granted for {normalized_path} "
                    f"({access_value}, scope={decision.scope})."
                )),),
            ),
        )

    def as_tool(self) -> Tool:
        return Tool.from_function(self.invoke, name="request_sandbox_access")


def _normalize_request(path: str, access: str) -> tuple[str, PathAccess]:
    cleaned = path.strip()
    if not cleaned:
        raise ValueError("path must not be empty")
    access_value = _ACCESS_BY_NAME.get(access.lower())
    if access_value is None:
        raise ValueError(
            "access must be one of: allow, readwrite, readonly, deny",
        )
    return cleaned, access_value


def build_sandbox_resource(payload: dict[str, JsonValue]) -> SandboxResourceConfig:
    """Coerce a serialised resource dict into a typed contract."""
    return SandboxResourceConfig.model_validate(payload)


def build_apply_sandbox_access(
    settings: "SettingsPort",
) -> ApplyDecision:
    """Build the ``apply_decision`` callable for the sandbox access tool.

    On an ``Allowed`` decision the returned coroutine always appends the
    rule to the session-level sandbox ``resources`` list via
    ``SettingsPort.update_policy``. The ``decision.scope`` chosen by the
    human in the permission UI is ignored: sandbox rules have no
    "one-shot" model, so every grant is session-scoped. ``Denied``
    short-circuits with no policy side effect. The decision is passed
    through to ``request_decision`` unchanged so it can read
    ``decision.kind`` and ``decision.scope``.

    The ``XBotv2.config`` import is deferred into the helper to avoid a
    package-level circular import (see the module docstring).
    """

    async def apply_decision(
        request: PermissionRequest,
        decision: Approval,
    ) -> Approval:
        if isinstance(decision, Allowed):
            await _apply_to_session_policy(settings, request)
        return decision

    return apply_decision


async def _apply_to_session_policy(
    settings: "SettingsPort",
    request: PermissionRequest,
) -> None:
    if not isinstance(request.subject, NamedPermission):
        return
    path_value = request.subject.params.get("path")
    access_value = request.subject.params.get("access")
    if not isinstance(path_value, str) or not isinstance(access_value, str):
        return
    if access_value not in {"allow", "readwrite", "readonly", "deny"}:
        return
    merged = _append_resource(settings, path_value, access_value)
    # Deferred import: see module docstring.
    from XBotv2.config import PatchPolicy

    await settings.update_policy(
        PatchPolicy(sandbox={"resources": merged}),
    )


def _append_resource(
    settings: "SettingsPort",
    path: str,
    access: str,
) -> list[dict[str, JsonValue]]:
    """Return the session-level ``resources`` list with ``{path, access}``
    inserted at the head, matching ``/sandbox add`` exactly.

    The tool never dedups or replaces prior rules — operators clean up
    with ``/sandbox remove <index>`` if they want a different access level
    for an already-granted path. This keeps the tool's policy mutation
    boringly identical to the human command.
    """
    snapshot = settings.policy()
    session = snapshot.policy.get("sandbox", {})
    existing = session.get("resources", [])
    if not isinstance(existing, list):
        existing = []
    existing.insert(0, {"path": path, "access": access})
    return existing


__all__ = [
    "AccessLiteral",
    "ApplyDecision",
    "RequestSandboxAccessTool",
    "Source",
    "build_apply_sandbox_access",
    "build_sandbox_resource",
]
