"""Public declarations for configuration and policy plugins."""

from XBotv2.config.contracts import (
    GET_POLICY,
    UPDATE_POLICY,
    PatchPolicy,
    PolicySnapshot,
)
__all__ = [
    "ConfigReloadResponse",
    "GET_POLICY",
    "PatchPolicy",
    "PermissionDecision",
    "PolicySnapshot",
    "SandboxKey",
    "SandboxValue",
    "SessionPolicyPatch",
    "SessionPolicyResponse",
    "UPDATE_POLICY",
]

_PROTOCOL_EXPORTS = {
    "ConfigReloadResponse",
    "PermissionDecision",
    "SandboxKey",
    "SandboxValue",
    "SessionPolicyPatch",
    "SessionPolicyResponse",
}


def __getattr__(name: str) -> object:
    if name not in _PROTOCOL_EXPORTS:
        raise AttributeError(name)
    from XBotv2.config import protocol

    return getattr(protocol, name)
