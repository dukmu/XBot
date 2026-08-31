"""Strict persisted and projected Workspace models."""

from __future__ import annotations

from typing import Literal

from pydantic import ValidationInfo, field_validator

from XBotv2.core.state import JsonStateModel


class WorkspaceRecord(JsonStateModel):
    id: str
    path: str
    title: str
    session_ids: tuple[str, ...] = ()
    created_at: str
    updated_at: str

    @field_validator("session_ids", mode="before")
    @classmethod
    def _session_ids(cls, value: object) -> tuple[object, ...]:
        if not isinstance(value, (list, tuple)):
            raise TypeError("WorkspaceRecord.session_ids must be a list")
        return tuple(value)


class WorkspaceSnapshot(JsonStateModel):
    schema_version: Literal[1] = 1
    items: tuple[WorkspaceRecord, ...] = ()
    archived_session_ids: tuple[str, ...] = ()

    @field_validator("items", "archived_session_ids", mode="before")
    @classmethod
    def _tuple_fields(
        cls,
        value: object,
        info: ValidationInfo,
    ) -> tuple[object, ...]:
        if not isinstance(value, (list, tuple)):
            raise TypeError(f"WorkspaceSnapshot.{info.field_name} must be a list")
        return tuple(value)


class WorkspaceView(JsonStateModel):
    workspace_id: str
    path: str
    title: str
    session_ids: tuple[str, ...] = ()
    created_at: str
    updated_at: str


class WorkspaceListing(JsonStateModel):
    items: tuple[WorkspaceView, ...] = ()
    archived_session_ids: tuple[str, ...] = ()


__all__ = ["WorkspaceListing", "WorkspaceRecord", "WorkspaceSnapshot", "WorkspaceView"]
