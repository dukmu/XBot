"""Typed notifications owned by application composition."""

from __future__ import annotations

from dataclasses import dataclass

from XBotv2.core import ClientEvent


RUNTIME_EVENT = "runtime/event"


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    client_event: ClientEvent


__all__ = ["RUNTIME_EVENT", "RuntimeEvent"]
