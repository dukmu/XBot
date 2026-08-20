"""Public declarations for configuration and policy plugins."""

from XBotv2.config.contracts import (
    GET_POLICY,
    UPDATE_POLICY,
    PatchPolicy,
    PolicySnapshot,
)
from XBotv2.config.events import POLICY_CHANGED, PolicyChanged
from XBotv2.config.services import SettingsPort

__all__ = [
    "ConfigReloadResponse",
    "GET_POLICY",
    "PatchPolicy",
    "POLICY_CHANGED",
    "PermissionDecision",
    "PolicyChanged",
    "PolicySnapshot",
    "SandboxKey",
    "SandboxValue",
    "SessionPolicyPatch",
    "SessionPolicyResponse",
    "SettingsPort",
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
